"""The LanLink interface over the Ayla cloud, for machines without LAN mode.

Some machines never get a LAN key: the cloud reports ``lan_enabled: false``
for an Eletta Explore (``DL-striker-cb``), for example. They can still be
reached the way the Coffee Link app reaches them: a write becomes a cloud
datapoint that the cloud relays to the machine, and the machine sends its
properties to the cloud.

``CloudLink`` polls the watched properties in one request and passes on every
value whose cloud timestamp (``data_updated_at``) moved forward, like a
datapoint pushed in LAN mode. Ayla limits requests to roughly 200 per hour
and IP (reported by sk7n4k3d/delonghi-ha), so the poll is slow by default and
fast only for a short window after a command. A ``201`` for a write only
means the cloud accepted it, not that the machine received it; callers
confirm effects from the monitor as in LAN mode.

The cloud keeps serving a machine's last values after it went offline, so
the link also checks the machine's cloud connection every few minutes and
counts as down while the machine is not ``Online``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from aioecam._util import remover as _remover
from aioecam.cloud.api import AylaCloud
from aioecam.exceptions import AuthError, CommandTimeout, NotConnected
from aioecam.lan.link import DatapointCallback, LinkState, StateCallback
from aioecam.lan.messages import Datapoint

_LOGGER = logging.getLogger(__name__)

# Consecutive failed polls before the link counts as down.
FAILURE_LIMIT = 3
ONLINE = "Online"


@dataclass
class CloudLinkStats:
    """Counters for diagnostics."""

    requests: int = 0
    failures: int = 0
    last_error: str | None = None
    auth_failed: bool = False  # the cloud rejected the sign-in: ask for a new one
    machine_status: str | None = None  # the machine's cloud connection
    last_poll: float | None = None
    last_datapoint: float | None = None


class CloudLink:
    # Values reach us by polling; asking the machine to republish its monitor
    # would cost requests without making the poll any faster.
    pushes_monitor = True

    def __init__(
        self,
        cloud: AylaCloud,
        *,
        dsn: str,
        watch: Iterable[str],
        device_ip: str | None = None,
        interval: float = 30.0,
        fast_interval: float = 5.0,
        fast_window: float = 60.0,
        status_interval: float = 300.0,
        max_backoff: float = 300.0,
        max_requests_per_hour: int = 180,
    ) -> None:
        self.dsn = dsn
        self.device_ip = device_ip or "cloud"
        self._cloud = cloud
        self._watch = list(dict.fromkeys(watch))
        self._interval = interval
        self._fast_interval = fast_interval
        self._fast_window = fast_window
        self._status_interval = status_interval
        self._max_backoff = max_backoff
        self._max_requests = max_requests_per_hour
        self._request_times: deque[float] = deque()

        self.state = LinkState.IDLE
        self.stats = CloudLinkStats()
        self._seen: dict[str, Any] = {}
        self._subscribers: list[tuple[str | None, DatapointCallback]] = []
        self._state_callbacks: list[StateCallback] = []
        self._fast_until = 0.0
        self._all_until = 0.0
        self._status_at = 0.0
        self._fetching = asyncio.Lock()
        self._wake = asyncio.Event()
        self._active = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"aioecam-cloud-{self.dsn[-4:]}")

    async def close(self) -> None:
        try:
            if self._task:
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
        finally:
            self._set_state(LinkState.CLOSED)

    async def wait_active(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._active.wait(), timeout)
        except TimeoutError:
            detail = f": {self.stats.last_error}" if self.stats.last_error else ""
            raise NotConnected(f"no cloud connection after {timeout}s{detail}") from None

    # --- public API (same as LanLink) -------------------------------------

    def subscribe(self, callback: DatapointCallback, name: str | None = None) -> Callable[[], None]:
        entry = (name, callback)
        self._subscribers.append(entry)
        return _remover(self._subscribers, entry)

    def on_state(self, callback: StateCallback) -> Callable[[], None]:
        self._state_callbacks.append(callback)
        return _remover(self._state_callbacks, callback)

    def watch(self, name: str) -> None:
        """Include ``name`` in every poll from now on."""
        if name not in self._watch:
            self._watch.append(name)

    async def expect_answers(self, seconds: float) -> None:
        """Call before sending a request: polls all properties fast for ``seconds``.

        Some answers land in their own property (``d282_mchn_sett_aoff``,
        ``d039_1_rec_espresso``, ...). The cloud still holds older answers
        there, so this first notes every property's current timestamp; only
        values sent after that are passed on.
        """
        await self._fetch(None, announce_new=False)
        now = time.monotonic()
        self._all_until = max(self._all_until, now + seconds)
        self._fast_until = max(self._fast_until, now + seconds)
        self._wake.set()

    async def write(
        self,
        name: str,
        value: str,
        *,
        timeout: float = 10.0,
        newline: bool = True,
        fast_poll: bool = True,
    ) -> None:
        """Create a datapoint in the cloud. ``newline`` is a LAN detail and ignored;
        ``fast_poll=False`` (keep-alives) does not shorten the poll interval."""
        self._require_usable()
        try:
            async with asyncio.timeout(timeout):
                self._spend()  # commands always go out; the poll gives way
                await self._cloud.write_property(self.dsn, name, value)
        except TimeoutError:
            raise CommandTimeout(f"cloud did not accept write {name} within {timeout}s") from None
        if fast_poll:
            # Answers and state changes follow within seconds: look sooner.
            self._fast_until = max(self._fast_until, time.monotonic() + self._fast_window)
            self._wake.set()

    async def read(self, name: str, *, timeout: float = 10.0) -> Datapoint:
        """The cloud's latest value of ``name``; new values also go to subscribers."""
        self._require_usable()
        self.watch(name)
        try:
            async with asyncio.timeout(timeout):
                records = await self._fetch([name])
        except TimeoutError:
            raise CommandTimeout(f"no {name} from the cloud within {timeout}s") from None
        if name not in records:
            raise CommandTimeout(f"the cloud has no value for {name}")
        record = records[name]
        return Datapoint(name=name, value=record.get("value"), base_type=record.get("base_type"))

    # --- internals ---------------------------------------------------------

    def _require_usable(self) -> None:
        if self.state is LinkState.CLOSED:
            raise NotConnected("link closed")
        status = self.stats.machine_status
        if status is not None and status != ONLINE:
            raise NotConnected(f"the machine is {status} in the cloud")

    def _spend(self) -> None:
        self.stats.requests += 1
        self._request_times.append(time.monotonic())

    def _used(self) -> int:
        """Requests in the last hour."""
        cutoff = time.monotonic() - 3600
        while self._request_times and self._request_times[0] < cutoff:
            self._request_times.popleft()
        return len(self._request_times)

    async def _fetch(
        self, names: list[str] | None, *, announce_new: bool = True
    ) -> dict[str, dict[str, Any]]:
        # One fetch at a time: a slow older answer must not overtake a newer one.
        async with self._fetching:
            self._spend()
            records = await self._cloud.read_properties(self.dsn, names)
            self._deliver(records, announce_new=announce_new)
            return records

    def _deliver(self, records: dict[str, dict[str, Any]], *, announce_new: bool = True) -> None:
        for name, record in records.items():
            stamp = record.get("data_updated_at")
            previous = self._seen.get(name)
            # ISO timestamps compare as text; only values sent after the last
            # one count, so an old answer is never passed on twice.
            if not isinstance(stamp, str) or (previous is not None and stamp <= previous):
                continue
            self._seen[name] = stamp
            if previous is None and not announce_new:
                continue  # baseline only: this value predates the request
            point = Datapoint(
                name=name, value=record.get("value"), base_type=record.get("base_type")
            )
            self.stats.last_datapoint = time.time()
            for wanted, callback in list(self._subscribers):
                if wanted is None or wanted == name:
                    try:
                        callback(point)
                    except Exception:
                        _LOGGER.exception("datapoint callback failed")

    def _set_state(self, state: LinkState) -> None:
        if state is self.state:
            return
        _LOGGER.debug("cloud link %s: %s -> %s", self.dsn[-4:], self.state, state)
        self.state = state
        if state is LinkState.ACTIVE:
            self._active.set()
        else:
            self._active.clear()
        for callback in list(self._state_callbacks):
            try:
                callback(state)
            except Exception:
                _LOGGER.exception("state callback failed")

    def _delay(self) -> float:
        used = self._used()
        if used >= self._max_requests and self._request_times:
            # Budget spent: wait until the oldest request leaves the hour.
            return max(self._request_times[0] + 3600 - time.monotonic(), self._interval)
        # Keep the last quarter of the budget for commands and their answers.
        fast = time.monotonic() < self._fast_until and used < self._max_requests * 0.75
        return self._fast_interval if fast else self._interval

    async def _poll_once(self) -> None:
        now = time.monotonic()
        if now - self._status_at >= self._status_interval or self.stats.machine_status != ONLINE:
            self._spend()
            self.stats.machine_status = await self._cloud.connection_status(self.dsn)
            self._status_at = now
        if self.stats.machine_status != ONLINE:
            # The cloud would keep serving the last values of an offline machine.
            self._set_state(LinkState.IDLE)
            return
        await self._fetch(None if now < self._all_until else self._watch)
        self._set_state(LinkState.ACTIVE)

    async def _run(self) -> None:
        self._set_state(LinkState.REGISTERING)
        failures = 0
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # CloudError, AuthError, malformed answers
                failures += 1
                self.stats.failures += 1
                self.stats.last_error = str(err) or type(err).__name__
                self.stats.auth_failed = isinstance(err, AuthError)
                _LOGGER.debug("cloud poll failed (%s in a row): %s", failures, err)
                if failures >= FAILURE_LIMIT or self.stats.auth_failed:
                    self._set_state(LinkState.IDLE)
                delay = min(self._interval * 2 ** min(failures - 1, 16), self._max_backoff)
            else:
                failures = 0
                self.stats.last_error = None
                self.stats.auth_failed = False
                self.stats.last_poll = time.time()
                delay = self._interval if self.state is not LinkState.ACTIVE else self._delay()
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), delay)
            # A command moved the window forward: honour the fast interval from now.
            if self._wake.is_set():
                await asyncio.sleep(min(self._fast_interval, self._delay()))
