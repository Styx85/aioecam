"""One machine's Ayla LAN link: registration, liveness and message exchange.

The machine is the HTTP client in LAN mode: after we register with
``local_reg.json`` it performs a key exchange with our callback server and
then polls ``commands.json`` whenever we notify it.

Measured on an ECAM610.75 (see docs/plan.md):

- With a live session the machine ignores ``PUT local_reg`` for re-keying but
  always re-keys on ``POST``. So a new session is only ever started with POST,
  and session keys are never thrown away proactively.
- ``PUT local_reg`` with ``notify=1`` makes the machine poll within a fraction
  of a second. That poll is the liveness signal; without it the session is
  considered dead and we register again with POST.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import itertools
import logging
import os
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import aiohttp

from aioecam._util import remover as _remover
from aioecam.exceptions import CommandTimeout, NotConnected
from aioecam.lan.crypto import LanSession, SessionError
from aioecam.lan.messages import Datapoint, property_get, property_set

_LOGGER = logging.getLogger(__name__)


class LinkState(StrEnum):
    IDLE = "idle"
    REGISTERING = "registering"
    ACTIVE = "active"
    CLOSED = "closed"


@dataclass
class LinkStats:
    """Counters for diagnostics."""

    registrations: int = 0
    key_exchanges: int = 0
    liveness_failures: int = 0
    rejected_messages: int = 0
    # HTTP status of the last local_reg; None if the machine was unreachable.
    # 404 means the Wi-Fi module refuses LAN mode, seen after a module restart
    # without cloud connectivity (regtoken.json then has an empty host_symname).
    last_registration_status: int | None = None
    last_key_exchange: float | None = None
    last_contact: float | None = None
    last_datapoint: float | None = None


@dataclass
class _Outgoing:
    data: dict[str, Any]
    delivered: asyncio.Future[None] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )


def _fail(fut: asyncio.Future[Any], error: BaseException) -> None:
    """Fail a future and mark its exception as retrieved: a caller that already
    gave up (timeout, cancellation) must not cause "exception was never retrieved"."""
    fut.set_exception(error)
    fut.exception()


DatapointCallback = Callable[[Datapoint], None]
StateCallback = Callable[[LinkState], None]


class LanLink:
    def __init__(
        self,
        *,
        device_ip: str,
        lan_key: str,
        dsn: str,
        http: aiohttp.ClientSession,
        advertise: Callable[[], tuple[str, int]],
        device_port: int = 80,
        key_id: int | None = None,
        keepalive: float = 20.0,
        probe_timeout: float = 5.0,
        register_timeout: float = 5.0,
        max_backoff: float = 60.0,
    ) -> None:
        self.device_ip = device_ip
        self.dsn = dsn
        self._lan_key = lan_key
        self._key_id = key_id
        self._http = http
        self._advertise = advertise
        self._url = f"http://{device_ip}:{device_port}/local_reg.json"
        self._keepalive = keepalive
        self._probe_timeout = probe_timeout
        self._register_timeout = register_timeout
        self._max_backoff = max_backoff

        self.state = LinkState.IDLE
        self.stats = LinkStats()
        self._session: LanSession | None = None
        self._outgoing: deque[_Outgoing] = deque()
        self._waiters: dict[str, list[asyncio.Future[Datapoint]]] = {}
        self._subscribers: list[tuple[str | None, DatapointCallback]] = []
        self._state_callbacks: list[StateCallback] = []
        self._cmd_ids = itertools.count(1)
        self._key_exchanged = asyncio.Event()
        self._polled = asyncio.Event()
        self._wake = asyncio.Event()
        self._active = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"aioecam-link-{self.device_ip}")

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._set_state(LinkState.CLOSED)
        self._session = None
        error = NotConnected("link closed")
        for item in self._outgoing:
            if not item.delivered.done():
                _fail(item.delivered, error)
        self._outgoing.clear()
        for futures in self._waiters.values():
            for fut in futures:
                if not fut.done():
                    _fail(fut, error)
        self._waiters.clear()

    async def wait_active(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._active.wait(), timeout)
        except TimeoutError:
            raise NotConnected(f"no LAN session with {self.device_ip} after {timeout}s") from None

    # --- public API --------------------------------------------------------

    def subscribe(self, callback: DatapointCallback, name: str | None = None) -> Callable[[], None]:
        """Call ``callback`` for every datapoint (or only those named ``name``)."""
        entry = (name, callback)
        self._subscribers.append(entry)
        return _remover(self._subscribers, entry)

    def on_state(self, callback: StateCallback) -> Callable[[], None]:
        self._state_callbacks.append(callback)
        return _remover(self._state_callbacks, callback)

    async def write(
        self, name: str, value: str, *, timeout: float = 10.0, newline: bool = True
    ) -> None:
        """Write a property; returns once the machine has fetched it."""
        item = _Outgoing(property_set(self.dsn, name, value, newline=newline))
        await self._send(item, timeout, f"write {name}")

    async def read(self, name: str, *, timeout: float = 10.0) -> Datapoint:
        """Ask the machine for a property and wait for its datapoint."""
        fut: asyncio.Future[Datapoint] = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(name, []).append(fut)
        try:
            await self._send(
                _Outgoing(property_get(name, next(self._cmd_ids))), timeout, f"read {name}"
            )
            return await asyncio.wait_for(asyncio.shield(fut), timeout)
        except TimeoutError:
            raise CommandTimeout(f"no {name} datapoint within {timeout}s") from None
        finally:
            waiters = self._waiters.get(name, [])
            if fut in waiters:
                waiters.remove(fut)

    async def _send(self, item: _Outgoing, timeout: float, what: str) -> None:
        if self.state is LinkState.CLOSED:
            raise NotConnected("link closed")
        self._outgoing.append(item)
        self._wake.set()
        try:
            await asyncio.wait_for(asyncio.shield(item.delivered), timeout)
        except TimeoutError:
            with contextlib.suppress(ValueError):
                self._outgoing.remove(item)
            raise CommandTimeout(f"machine did not fetch {what} within {timeout}s") from None

    # --- called by the callback server -------------------------------------

    def handle_key_exchange(self, body: dict[str, Any]) -> dict[str, Any]:
        kx = body.get("key_exchange")
        if not isinstance(kx, dict) or "random_1" not in kx or "time_1" not in kx:
            raise SessionError("malformed key_exchange")
        if self._key_id is not None and kx.get("key_id") != self._key_id:
            raise SessionError(f"unexpected key_id {kx.get('key_id')}")
        random_2 = base64.b64encode(os.urandom(12)).decode("ascii")
        time_2 = str(time.time_ns() // 1_000_000)
        self._session = LanSession(
            self._lan_key,
            random_1=str(kx["random_1"]),
            random_2=random_2,
            time_1=str(kx["time_1"]),
            time_2=time_2,
        )
        self.stats.key_exchanges += 1
        self.stats.last_key_exchange = self._touch()
        _LOGGER.debug("%s: key exchange (key_id %s)", self.device_ip, kx.get("key_id"))
        self._key_exchanged.set()
        return {"random_2": random_2, "time_2": int(time_2)}

    def handle_commands_poll(self) -> dict[str, Any]:
        session = self._require_session()
        self._touch()
        self._polled.set()
        while self._outgoing and self._outgoing[0].delivered.done():
            self._outgoing.popleft()  # timed out by the caller in the meantime
        item = self._outgoing.popleft() if self._outgoing else None
        sealed = session.seal(item.data if item else {})
        if item:
            item.delivered.set_result(None)
        if self._outgoing:
            self._wake.set()
        return sealed

    def handle_datapoint(self, body: dict[str, Any]) -> None:
        session = self._require_session()
        self._touch()
        try:
            message = session.open(body)
        except SessionError:
            self.stats.rejected_messages += 1
            raise
        data = message.get("data")
        point = Datapoint.from_data(data) if isinstance(data, dict) else None
        if point is None:
            _LOGGER.debug("%s: ignoring datapoint without name", self.device_ip)
            return
        self.stats.last_datapoint = time.time()
        for fut in self._waiters.pop(point.name, []):
            if not fut.done():
                fut.set_result(point)
        for name, callback in list(self._subscribers):
            if name is None or name == point.name:
                try:
                    callback(point)
                except Exception:
                    _LOGGER.exception("datapoint callback failed")

    # --- internals ---------------------------------------------------------

    def _require_session(self) -> LanSession:
        if self._session is None:
            raise NotConnected("no session yet")
        return self._session

    def _touch(self) -> float:
        now = time.time()
        self.stats.last_contact = now
        return now

    def _set_state(self, state: LinkState) -> None:
        if state is self.state:
            return
        _LOGGER.debug("%s: %s -> %s", self.device_ip, self.state, state)
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

    async def _local_reg(self, method: str) -> bool:
        ip, port = self._advertise()
        body = {"local_reg": {"ip": ip, "notify": 1, "port": port, "uri": "/local_lan"}}
        try:
            async with self._http.request(
                method, self._url, json=body, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                status = resp.status
        except (aiohttp.ClientError, TimeoutError) as exc:
            _LOGGER.debug("%s: %s local_reg failed: %s", self.device_ip, method, exc)
            self.stats.last_registration_status = None
            return False
        if status == 404 and self.stats.last_registration_status != 404:
            _LOGGER.warning(
                "%s: Wi-Fi module refuses LAN mode (HTTP 404). It usually needs a cloud "
                "connection after a restart; check that it can reach the internet",
                self.device_ip,
            )
        elif not 200 <= status < 300:
            _LOGGER.debug("%s: %s local_reg -> HTTP %s", self.device_ip, method, status)
        self.stats.last_registration_status = status
        return 200 <= status < 300

    async def _register(self) -> bool:
        self._set_state(LinkState.REGISTERING)
        self._key_exchanged.clear()
        self.stats.registrations += 1
        if not await self._local_reg("POST"):
            return False
        try:
            await asyncio.wait_for(self._key_exchanged.wait(), self._register_timeout)
        except TimeoutError:
            return False
        return True

    async def _alive(self) -> bool:
        """Notify the machine and require a commands poll in response."""
        self._polled.clear()
        if not await self._local_reg("PUT"):
            return False
        try:
            await asyncio.wait_for(self._polled.wait(), self._probe_timeout)
        except TimeoutError:
            return False
        return True

    async def _run(self) -> None:
        backoff = 1.0
        while True:
            if not await self._register():
                self._set_state(LinkState.IDLE)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff)
                continue
            backoff = 1.0
            self._set_state(LinkState.ACTIVE)
            # The key exchange is followed by a poll on its own; queued work goes out then.
            while True:
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), self._keepalive)
                if not await self._alive():
                    self.stats.liveness_failures += 1
                    _LOGGER.info("%s: no response to notify, registering again", self.device_ip)
                    break
