"""EcamDevice: the machine as one object, with confirmed commands.

A command only counts as done once the monitor shows its effect. Measured on
an ECAM610.75: the machine does not reliably push monitor changes, it answers
reads. So confirmation polls, and ``start_polling`` keeps ``state`` current
(fast while awake, slow in standby, where the module returns a frozen frame).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Protocol

from aioecam._util import remover as _remover
from aioecam.ecam import commands
from aioecam.ecam.frame import Frame
from aioecam.ecam.monitor import MachineState
from aioecam.ecam.profile import Profile
from aioecam.ecam.statistics import (
    MAX_COUNT,
    StatisticsSnapshot,
    interpret,
    is_statistics_response,
    parse_statistics,
    statistics_request,
)
from aioecam.ecam.value import decode_value
from aioecam.exceptions import CommandRejected, CommandTimeout, EcamError, FrameError
from aioecam.lan.link import LanLink, LinkState
from aioecam.lan.messages import Datapoint

_LOGGER = logging.getLogger(__name__)

STATUS_OFF = 0
STATUS_GOING_TO_SLEEP = 2

StateCallback = Callable[[MachineState], None]


class CloudWriter(Protocol):
    """What the wake fallback needs from a cloud client (``AylaCloud`` fits)."""

    async def write_property(self, dsn: str, name: str, value: str) -> None: ...


ProgressCallback = Callable[[dict[str, int]], None]


class EcamDevice:
    def __init__(
        self,
        link: LanLink,
        profile: Profile,
        *,
        app_id: bytes | None = None,
        poll_interval: float = 3.0,
        cloud: CloudWriter | None = None,
    ) -> None:
        self.link = link
        self.profile = profile
        self._app_id = app_id
        self._cloud = cloud
        self.cloud_fallbacks = 0  # diagnostics: how often the cloud had to step in
        self._poll_interval = poll_interval
        self.state: MachineState | None = None
        self._listeners: list[StateCallback] = []
        self._changed = asyncio.Event()
        self._responses: list[asyncio.Queue[Frame]] = []
        self._poller: asyncio.Task[None] | None = None
        link.subscribe(self._on_monitor, name=profile.monitor_property)
        if "data_response" in profile.properties:
            link.subscribe(self._on_response, name=profile.properties["data_response"])

    # --- state -------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self.link.state is LinkState.ACTIVE

    def subscribe(self, callback: StateCallback) -> Callable[[], None]:
        self._listeners.append(callback)
        return _remover(self._listeners, callback)

    def is_busy(self) -> bool:
        return self.state is not None and self.profile.is_busy(self.state)

    async def refresh(self, *, timeout: float = 10.0) -> MachineState:
        point = await self.link.read(self.profile.monitor_property, timeout=timeout)
        self._on_monitor(point)  # also delivered via subscription; idempotent
        assert self.state is not None
        return self.state

    def _on_monitor(self, point: Datapoint) -> None:
        if not isinstance(point.value, str):
            return
        try:
            state = MachineState.from_value(point.value)
        except FrameError as exc:
            _LOGGER.debug("ignoring undecodable monitor value: %s", exc)
            return
        if state == self.state:
            return
        self.state = state
        self._changed.set()
        for callback in list(self._listeners):
            try:
                callback(state)
            except Exception:
                _LOGGER.exception("state callback failed")

    def _on_response(self, point: Datapoint) -> None:
        if not isinstance(point.value, str):
            return
        try:
            frame = decode_value(point.value).frame
        except FrameError:
            return
        for queue in self._responses:
            queue.put_nowait(frame)

    # --- commands ------------------------------------------------------------

    async def send(self, frame: Frame, *, timeout: float = 10.0) -> None:
        """Hand a raw frame to the machine (no confirmation)."""
        write = commands.request_write(self.profile, frame, app_id=self._app_id)
        await self.link.write(write.name, write.value, timeout=timeout)

    async def power_on(
        self, *, confirm_timeout: float = 60.0, local_grace: float = 15.0
    ) -> MachineState:
        """Wake the machine locally; if it does not react within ``local_grace``
        and a cloud client is configured, send the same frame through the cloud.

        Waking without internet failed once in testing (after a long standby),
        hence the fallback.
        """
        state = await self.refresh()
        if state.status != STATUS_OFF:
            return state
        frame = commands.power_on()
        await self.send(frame)
        awake: Callable[[MachineState], bool] = lambda s: s.status != STATUS_OFF  # noqa: E731
        if self._cloud is None:
            return await self.wait_for(awake, confirm_timeout, "power on")
        try:
            return await self.wait_for(awake, min(local_grace, confirm_timeout), "power on")
        except CommandTimeout:
            remaining = confirm_timeout - local_grace
            if remaining <= 0:
                raise
        _LOGGER.info("%s: no reaction to the local wake-up, sending it via the cloud",
                     self.link.device_ip)  # fmt: skip
        write = commands.request_write(self.profile, frame, app_id=self._app_id)
        await self._cloud.write_property(self.link.dsn, write.name, write.value)
        self.cloud_fallbacks += 1
        return await self.wait_for(awake, remaining, "power on (cloud fallback)")

    async def power_off(self, *, confirm_timeout: float = 60.0) -> MachineState:
        state = await self.refresh()
        if state.status in (STATUS_OFF, STATUS_GOING_TO_SLEEP):
            return state
        await self.send(commands.power_off())
        return await self.wait_for(
            lambda s: s.status in (STATUS_OFF, STATUS_GOING_TO_SLEEP), confirm_timeout, "power off"
        )

    async def brew(self, beverage: str, *, confirm_timeout: float = 30.0) -> MachineState:
        """Start a stored beverage; confirmed once the machine reports activity.

        The status stays "ready" while a beverage runs; only action/progress
        change, which ``Profile.is_busy`` accounts for.
        """
        frame = self.profile.command(beverage)
        state = await self.refresh()
        if not self.profile.is_ready(state):
            raise CommandRejected(
                f"machine is not ready for {beverage} ({self.profile.status_name(state)}, "
                f"action {state.action})"
            )
        await self.send(frame)
        return await self.wait_for(self.profile.is_busy, confirm_timeout, f"start {beverage}")

    async def wait_until_ready(self, *, timeout: float = 180.0) -> MachineState:
        """Wait until the machine is ready and idle (after waking or a beverage)."""
        return await self.wait_for(self.profile.is_ready, timeout, "ready")

    async def stop(self) -> None:
        await self.send(self.profile.command("stop"))

    async def wait_for(
        self, done: Callable[[MachineState], bool], timeout: float, what: str
    ) -> MachineState:
        deadline = time.monotonic() + timeout
        while True:
            if self.state is not None and done(self.state):
                return self.state
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                current = self.profile.status_name(self.state) if self.state else "unknown"
                raise CommandTimeout(f"{what} not confirmed within {timeout}s (status {current})")
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), min(self._poll_interval, remaining))
            except TimeoutError:
                # No push: the machine may still be asleep or slow; ask actively.
                with contextlib.suppress(CommandTimeout):
                    await self.refresh(timeout=min(5.0, max(remaining, 0.1)))

    # --- background polling ------------------------------------------------------

    def start_polling(self, *, awake: float = 3.0, asleep: float = 60.0) -> None:
        """Keep ``state`` current by reading the monitor in the background."""
        if self._poller is None:
            self._poller = asyncio.create_task(self._poll(awake, asleep), name="aioecam-poll")

    async def stop_polling(self) -> None:
        if self._poller:
            self._poller.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poller
            self._poller = None

    async def _poll(self, awake: float, asleep: float) -> None:
        while True:
            if self.available:
                try:
                    await self.refresh(timeout=min(awake * 2, 10.0))
                except EcamError as exc:
                    _LOGGER.debug("monitor poll failed: %s", exc)
            sleeping = self.state is None or self.state.status == STATUS_OFF
            await asyncio.sleep(asleep if sleeping else awake)

    # --- statistics -----------------------------------------------------------

    async def read_statistics_page(
        self, start_id: int, count: int = MAX_COUNT, *, timeout: float = 20.0
    ) -> dict[int, int]:
        if "data_response" not in self.profile.properties:
            raise EcamError(f"{self.profile.family} has no known data_response property")
        queue: asyncio.Queue[Frame] = asyncio.Queue()
        self._responses.append(queue)
        try:
            await self.send(statistics_request(start_id, count), timeout=timeout)
            async with asyncio.timeout(timeout):
                while True:
                    frame = await queue.get()
                    if not is_statistics_response(frame):
                        continue
                    page = parse_statistics(frame)
                    # A2 answers with the first *existing* id >= start_id.
                    if page and min(page) >= start_id:
                        return page
        except TimeoutError:
            raise CommandTimeout(f"no A2 reply for start_id={start_id} within {timeout}s") from None
        finally:
            self._responses.remove(queue)

    async def read_statistics(
        self,
        *,
        start_id: int = 100,
        page_size: int = MAX_COUNT,
        page_timeout: float = 20.0,
        retry_pause: float = 5.0,
        max_pages: int = 100,
        progress: ProgressCallback | None = None,
    ) -> StatisticsSnapshot:
        """Read the complete sparse A2 table.

        A timed-out page is retried with one parameter less (some firmware
        cannot answer full pages in time). A timeout is never treated as the
        end of the table; only a genuinely shorter page is.
        """
        raw: dict[int, int] = {}
        next_id = start_id
        for page_number in range(1, max_pages + 1):
            count = page_size
            while True:
                try:
                    page = await self.read_statistics_page(next_id, count, timeout=page_timeout)
                    break
                except CommandTimeout:
                    if count == 1:
                        raise
                    count -= 1
                    await asyncio.sleep(retry_pause)
            raw.update(page)
            last_id = max(page)
            if progress:
                progress({"page": page_number, "returned": len(page), "collected": len(raw)})
            if len(page) < count or last_id == 0xFFFF:
                return interpret(self.profile.family, raw)
            next_id = last_id + 1
        raise EcamError(f"A2 paging exceeded {max_pages} pages")
