"""EcamDevice: the machine as one object, with confirmed commands.

A command only counts as done once the monitor shows its effect. Measured on
an ECAM610.75: the machine does not reliably push monitor changes, it answers
reads. So confirmation polls, and ``start_polling`` keeps ``state`` current
(fast while awake, slow in standby, where the module returns a frozen frame).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

from aioecam._util import remover as _remover
from aioecam.ecam import commands
from aioecam.ecam.answers import (
    ProfileSlot,
    Recipe,
    is_sync_timeout,
    parse_params,
    parse_profile_names,
    parse_profile_select_ack,
    parse_recipe,
    parse_write_ack,
)
from aioecam.ecam.catalog import PassiveCatalog
from aioecam.ecam.frame import RESPONSE, Frame
from aioecam.ecam.monitor import MachineState
from aioecam.ecam.profile import Profile
from aioecam.ecam.settings import SETTINGS, Setting
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
from aioecam.lan.link import DatapointCallback, LinkState
from aioecam.lan.messages import Datapoint

_LOGGER = logging.getLogger(__name__)

STATUS_OFF = 0
STATUS_GOING_TO_SLEEP = 2

StateCallback = Callable[[MachineState], None]
T = TypeVar("T")

# Recipe TLV tags. Names follow the parameter enum of the Coffee Link app
# 4.9.6 (statically analysed, 2026-10-19). Values are raw protocol units.
# Tag 0x1B is INDEX_LENGTH (a size index), not a temperature: an earlier
# TAG_TEMPERATURE = 0x1B was wrong and has been removed.
TAG_TEMP = 0x00
TAG_COFFEE_ML = 0x01
TAG_INTENSITY = 0x02  # app: TASTE (aroma/strength step)
TAG_DOUBLE = 0x08  # app: DUExPER
TAG_MILK = 0x09
TAG_TEA_TEMP = 0x0D  # app: THE_TEMP
TAG_WATER_ML = 0x0F  # app: HOT_WATER
TAG_PROGRAMMABLE = 0x18
TAG_VISIBLE = 0x19  # app: 0 hidden, 1 normal, 2 info instead of brew
TAG_INDEX_LENGTH = 0x1B
TAG_ACCESSORY = 0x1C  # app: ACCESSORIO
TAG_NAMES = {
    TAG_TEMP: "temperature",
    TAG_COFFEE_ML: "coffee_ml",
    TAG_INTENSITY: "intensity",
    0x03: "grind",
    0x04: "blend",
    0x05: "infusion_speed",
    0x06: "preinfusion",
    0x07: "crema",
    TAG_DOUBLE: "double",
    TAG_MILK: "milk",
    0x0A: "milk_temperature",
    0x0B: "milk_froth",
    0x0C: "inversion",
    TAG_TEA_TEMP: "tea_temperature",
    0x0E: "tea_profile",
    TAG_WATER_ML: "water_ml",
    TAG_PROGRAMMABLE: "programmable",
    TAG_VISIBLE: "visible",
    0x1A: "visible_in_programming",
    TAG_INDEX_LENGTH: "index_length",
    TAG_ACCESSORY: "accessory",
    0x1F: "iced",
    0x20: "mug_size",
    0x26: "app_intensity",
}

# The write session lives about 300 s; renew well before (the app uses 240 s).
SESSION_REFRESH = 240.0


# A frame whose payload timestamp is older than the current one by up to this
# much is a delayed answer that arrived out of order. A larger jump back is
# treated as a clock reset of the module (e.g. after a restart without
# internet), so the state can never get stuck on an old timestamp.
REORDER_WINDOW = 3600


def _reordered(state: MachineState, current: MachineState) -> bool:
    """True if ``state`` is older than ``current`` and arrived out of order."""
    if state.timestamp is None or current.timestamp is None:
        return False
    return 0 < current.timestamp - state.timestamp <= REORDER_WINDOW


def _newer(state: MachineState, before: MachineState | None) -> bool:
    """True if ``state`` is a frame published after ``before``."""
    if before is None:
        return True
    if state.timestamp is not None and before.timestamp is not None:
        if _reordered(state, before):
            return False
        return state.timestamp != before.timestamp or state != before
    return state != before


class Link(Protocol):
    """What the device needs from a transport (``LanLink`` and ``CloudLink`` fit)."""

    dsn: str
    device_ip: str
    state: LinkState

    def subscribe(
        self, callback: DatapointCallback, name: str | None = None
    ) -> Callable[[], None]: ...
    async def write(
        self, name: str, value: str, *, timeout: float = 10.0, newline: bool = True
    ) -> None: ...
    async def read(self, name: str, *, timeout: float = 10.0) -> Datapoint: ...


def _pushes_monitor(link: Link) -> bool:
    """True for transports that deliver new values by themselves (the cloud
    poll): asking the machine to republish would only cost requests."""
    return bool(getattr(link, "pushes_monitor", False))


def _quiet(link: Link) -> dict[str, Any]:
    """Write options for a keep-alive: no fast polling on a polling transport."""
    if _pushes_monitor(link):
        return {"newline": False, "fast_poll": False}
    return {"newline": False}


class CloudWriter(Protocol):
    """What the wake fallback needs from a cloud client (``AylaCloud`` fits)."""

    async def write_property(self, dsn: str, name: str, value: str) -> None: ...


ProgressCallback = Callable[[dict[str, int]], None]


class EcamDevice:
    def __init__(
        self,
        link: Link,
        profile: Profile,
        *,
        app_id: bytes | None = None,
        poll_interval: float = 3.0,
        cloud: CloudWriter | None = None,
    ) -> None:
        self.link = link
        self.profile = profile
        # The Coffee Link app signs Eletta ("striker") requests with its id.
        if app_id is None and profile.session == "striker":
            app_id = commands.APP_SIGNATURE
        self._app_id = app_id
        self._cloud = cloud
        self._session_at = 0.0
        self.catalog = PassiveCatalog()
        self.machine_info: dict[str, str | int] = {}
        self.last_datapoint_at: float | None = None
        self.last_monitor_at: float | None = None
        self.monitor_query_started_at: float | None = None
        self.monitor_query_finished_at: float | None = None
        self.monitor_query_result: str | None = None
        self.monitor_query_error: str | None = None
        self.last_monitor_error_at: float | None = None
        self.last_monitor_error: str | None = None
        self.cloud_fallbacks = 0  # diagnostics: how often the cloud had to step in
        self._poll_interval = poll_interval
        self.state: MachineState | None = None
        self._listeners: list[StateCallback] = []
        self._changed = asyncio.Event()
        self._responses: list[asyncio.Queue[tuple[Frame, str]]] = []
        self._answer_properties: dict[Frame, str] = {}
        self._poller: asyncio.Task[None] | None = None
        link.subscribe(self._on_monitor, name=profile.monitor_property)
        # Answers are routed by the echoed opcode, never by property name: the
        # Soul answers A2 on data_response but republishes 0x95 settings and
        # 0xA6 recipes into their own properties (d282_mchn_sett_aoff,
        # d039_1_rec_espresso, ...), measured live.
        link.subscribe(self._on_response)

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
        self.monitor_query_started_at = time.time()
        self.monitor_query_finished_at = None
        self.monitor_query_result = "in_progress"
        self.monitor_query_error = None
        try:
            point = await self.link.read(self.profile.monitor_property, timeout=timeout)
            # Validate this response independently: an old state must never turn
            # a malformed response into a successful monitor query.
            if not isinstance(point.value, str):
                raise FrameError("monitor is not a frame value")
            MachineState.from_value(point.value)
            self._on_monitor(point)
            self.monitor_query_result = "response_received"
            assert self.state is not None
            return self.state
        except asyncio.CancelledError:
            self.monitor_query_result = "cancelled"
            raise
        except Exception as exc:
            self.monitor_query_result = "failed"
            self.monitor_query_error = type(exc).__name__
            self.last_monitor_error_at = time.time()
            self.last_monitor_error = self.monitor_query_error
            raise
        finally:
            self.monitor_query_finished_at = time.time()

    def _on_monitor(self, point: Datapoint) -> None:
        if not isinstance(point.value, str):
            return
        try:
            state = MachineState.from_value(point.value)
        except FrameError as exc:
            _LOGGER.debug("ignoring undecodable monitor value: %s", exc)
            return
        self.last_monitor_at = time.time()
        if state == self.state:
            return
        if self.state is not None and _reordered(state, self.state):
            _LOGGER.debug("ignoring out-of-order monitor frame (ts %s < %s)",
                          state.timestamp, self.state.timestamp)  # fmt: skip
            return
        self.state = state
        self._changed.set()
        for callback in list(self._listeners):
            try:
                callback(state)
            except Exception:
                _LOGGER.exception("state callback failed")

    def _on_response(self, point: Datapoint) -> None:
        self.last_datapoint_at = time.time()
        self.catalog.observe(point.name, point.value, self.last_datapoint_at)
        if point.name in {
            "d560_versione_sw",
            "d561_machine_model",
            "d562_versione_sw_ui",
            "software_version",
        } and (isinstance(point.value, (str, int)) and not isinstance(point.value, bool)):
            self.machine_info[point.name] = (
                point.value[:256] if isinstance(point.value, str) else point.value
            )
        if not self._responses or not isinstance(point.value, str):
            return
        try:
            frame = decode_value(point.value).frame
        except FrameError:
            return  # not a frame-carrying property (timestamps, versions, ...)
        if frame.direction != RESPONSE:
            return  # e.g. echoes of our own data_request
        for queue in self._responses:
            queue.put_nowait((frame, point.name))

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

        Without internet, waking worked locally within about 2 s in every test
        (2026-10-19, standby up to 13 min). An earlier "failed" offline wake was
        most likely a missed confirmation: the offline module only publishes a
        new monitor frame on request, see ``request_monitor``. The fallback
        stays for cases not yet tested (overnight standby).
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
                if _pushes_monitor(self.link):
                    continue  # the transport's own poll brings the next frame
                # No push: ask for a fresh frame. A plain read is not enough; the
                # module may keep serving the frame from before the command.
                with contextlib.suppress(CommandTimeout):
                    await self.request_monitor(timeout=min(3.0, max(remaining, 0.1)))

    # --- background polling ------------------------------------------------------

    def start_polling(self, *, awake: float = 3.0, asleep: float = 60.0) -> None:
        """Keep ``state`` current by requesting monitor frames in the background."""
        if self._poller is None:
            self._poller = asyncio.create_task(self._poll(awake, asleep), name="aioecam-poll")

    async def stop_polling(self) -> None:
        if self._poller:
            self._poller.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poller
            self._poller = None

    async def _poll(self, awake: float, asleep: float) -> None:
        if _pushes_monitor(self.link):
            return  # the transport polls by itself; requests would only cost budget
        while True:
            if self.available:
                try:
                    await self.request_monitor(timeout=min(awake, 3.0))
                except EcamError as exc:
                    _LOGGER.debug("monitor poll failed: %s", exc)
            sleeping = self.state is None or self.state.status == STATUS_OFF
            await asyncio.sleep(asleep if sleeping else awake)

    # --- statistics -----------------------------------------------------------

    async def _exchange(
        self,
        frame: Frame,
        accept: Callable[[Frame], T | None],
        *,
        timeout: float,
        what: str,
        poll_answer: bool = False,
    ) -> T:
        """Send ``frame`` and return the first answer ``accept`` maps to a result.

        Answers arrive as datapoints of any property (see ``__init__``).
        ``0xE1`` means the machine was busy and dropped the request.
        """
        queue: asyncio.Queue[tuple[Frame, str]] = asyncio.Queue()
        self._responses.append(queue)
        expect = getattr(self.link, "expect_answers", None)
        if expect is not None:
            # Polling transport: answers may land in any property, look at all.
            expect(timeout)
        try:
            await self.send(frame, timeout=timeout)
            async with asyncio.timeout(timeout):
                while True:
                    if poll_answer:
                        try:
                            answer, name = await asyncio.wait_for(queue.get(), 0.5)
                        except TimeoutError:
                            if _pushes_monitor(self.link):
                                continue  # the transport's poll fetches answers
                            # The module can retain an answer without republishing it.
                            # Learn the property from a successful answer, not its value.
                            name = self._answer_properties.get(
                                frame, self.profile.properties["data_response"]
                            )
                            with contextlib.suppress(CommandTimeout):
                                await self.link.read(name, timeout=1.0)
                            continue
                    else:
                        answer, name = await queue.get()
                    if is_sync_timeout(answer):
                        raise CommandRejected(f"machine busy, {what} aborted (0xE1)")
                    result = accept(answer)
                    if result is not None:
                        self._answer_properties[frame] = name
                        return result
        except TimeoutError:
            raise CommandTimeout(f"no answer for {what} within {timeout}s") from None
        finally:
            self._responses.remove(queue)

    async def read_statistics_page(
        self, start_id: int, count: int = MAX_COUNT, *, timeout: float = 20.0
    ) -> dict[int, int]:
        def accept(frame: Frame) -> dict[int, int] | None:
            if not is_statistics_response(frame):
                return None
            page = parse_statistics(frame)
            # A2 answers with the first *existing* id >= start_id.
            return page if page and min(page) >= start_id else None

        return await self._exchange(
            statistics_request(start_id, count), accept, timeout=timeout,
            what=f"A2 page from {start_id}",
        )  # fmt: skip

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

    # --- session-gated writes, profiles, settings, recipes (M7) ----------------
    # Ported from lodzen's ECAM layer (https://github.com/lodzen/cremalink).

    async def announce_session(self, *, force: bool = False) -> None:
        """Open the write session ``0x90``/``0xA9`` writes need.

        Soul: write the current unix time to ``device_connected`` (the session
        lives about 300 s; it is refreshed after 240 s). Without it, writes are
        ignored or only staged. Eletta ("striker") sessions are not implemented.
        """
        if self.profile.session != "device_connected":
            raise EcamError(f"{self.profile.family}: session type {self.profile.session!r} "
                            "is not supported yet")  # fmt: skip
        now = time.time()
        if not force and now - self._session_at < SESSION_REFRESH:
            return
        await self.link.write(
            self.profile.properties["device_connected"], str(int(now)), newline=False
        )
        self._session_at = now

    async def keep_cloud_session(self) -> None:
        """Keep the cloud relaying commands; call about every 4 minutes.

        Reported by sk7n4k3d/delonghi-ha: without it the cloud session ends
        after about 5 minutes and writes are accepted but never reach the
        machine. Eletta: ``app_device_connected`` with the timestamp and the
        app's id. Machines without that property: a monitor request.
        """
        if self.profile.session == "striker":
            payload = int(time.time()).to_bytes(4, "big") + (self._app_id or b"")
            await self.link.write(
                self.profile.properties["device_connected"],
                base64.b64encode(payload).decode("ascii"),
                **_quiet(self.link),
            )
        else:
            write = commands.request_write(
                self.profile, commands.monitor_request(), app_id=self._app_id
            )
            await self.link.write(write.name, write.value, **_quiet(self.link))

    async def request_monitor(self, *, timeout: float = 3.0) -> MachineState:
        """Ask the machine for a fresh monitor frame (``0D 05 75 0F``) and wait for it.

        Measured on an ECAM610.75 with its internet access blocked: the module
        did not publish state changes by itself (not even waking up or
        switching off); a new monitor frame appeared only after this request,
        about 1-2 s after the module had fetched it. Reading straight away
        returned the previous frame. So wait for a frame with a newer payload
        timestamp (or a different state). If none arrives within ``timeout``
        the latest frame is returned and ``monitor_query_result`` says
        ``no_new_frame``; the module does not always republish an unchanged
        state.
        """
        if _pushes_monitor(self.link):
            # Over the cloud the machine publishes changes itself (it is online);
            # the latest cloud value is as fresh as it gets.
            return await self.refresh(timeout=max(timeout, 0.5))
        before = self.state
        # One deadline for the whole operation, including the module fetching
        # the request: callers such as wait_for() rely on it being bounded.
        deadline = time.monotonic() + timeout
        await self.send(commands.monitor_request(), timeout=max(timeout, 0.1))
        while True:
            self._changed.clear()
            remaining = deadline - time.monotonic()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), min(0.5, max(remaining, 0.0)))
            remaining = deadline - time.monotonic()
            try:
                async with asyncio.timeout(max(remaining, 0.5)):
                    state = await self.refresh(timeout=max(remaining, 0.5))
            except (TimeoutError, CommandTimeout):
                if self.state is None:
                    raise CommandTimeout(f"no monitor frame within {timeout}s") from None
                self.monitor_query_result = "no_new_frame"
                return self.state
            if _newer(state, before):
                return state
            if time.monotonic() >= deadline:
                self.monitor_query_result = "no_new_frame"
                return state

    async def profiles(self, *, timeout: float = 10.0) -> list[ProfileSlot]:
        """The machine's occupied user profiles (``0xA4``)."""
        count = self.profile.profile_slots

        def accept(frame: Frame) -> list[ProfileSlot] | None:
            if frame.opcode != commands.OPCODE_PROFILE_NAMES:
                return None
            return parse_profile_names(frame, entry_size=self.profile.profile_entry_size)

        return await self._exchange(
            commands.read_profile_names(1, count), accept, timeout=timeout, what="profile names"
        )

    async def select_profile(self, slot: int, *, timeout: float = 10.0) -> None:
        """Switch the active user profile (``0xA9``), confirmed by the machine's ack."""
        if not 1 <= slot <= self.profile.profile_slots:
            raise ValueError(f"profile slot must be 1..{self.profile.profile_slots}")
        await self.announce_session()

        def accept(frame: Frame) -> tuple[int, bool] | None:
            if frame.opcode != commands.OPCODE_PROFILE_SELECT:
                return None
            acked, ok = parse_profile_select_ack(frame)
            return (acked, ok) if acked == slot else None

        acked, ok = await self._exchange(
            commands.profile_select(slot),
            accept,
            timeout=timeout,
            what=f"profile {slot}",
            poll_answer=True,
        )
        if not ok or acked != slot:
            raise CommandRejected(f"machine refused profile {slot} (ack for {acked}, ok={ok})")

    def _setting(self, key: str) -> Setting:
        if key not in self.profile.settings:
            raise KeyError(f"{self.profile.family} has no writable setting {key!r}")
        return SETTINGS[key]

    async def read_setting(self, key: str, *, timeout: float = 10.0) -> int:
        """Raw value of a writable setting (``0x95`` read)."""
        setting = self._setting(key)

        def accept(frame: Frame) -> int | None:
            if frame.opcode not in (commands.OPCODE_READ_PARAM, commands.OPCODE_READ_EXTENDED):
                return None
            return parse_params(frame).get(setting.param_id)

        return await self._exchange(
            commands.read_param(setting.param_id), accept, timeout=timeout, what=f"read {key}"
        )

    async def write_setting(self, key: str, value: int, *, timeout: float = 10.0) -> None:
        """Write a setting (``0x90``) and confirm it by reading it back."""
        setting = self._setting(key)
        if value not in setting.options:
            raise ValueError(f"{key}: value must be one of {sorted(setting.options)}")
        await self.announce_session()

        def accept(frame: Frame) -> bool | None:
            if frame.opcode != commands.OPCODE_WRITE_PARAM:
                return None
            param, ok = parse_write_ack(frame)
            return ok if param == setting.param_id else None

        if not await self._exchange(
            commands.write_param(setting.param_id, value), accept, timeout=timeout,
            what=f"write {key}",
        ):  # fmt: skip
            raise CommandRejected(f"machine refused {key}={value}")
        if (current := await self.read_setting(key, timeout=timeout)) != value:
            raise CommandRejected(f"{key} reads back {current}, not {value}")

    def _beverage_frame(self, beverage: str) -> Frame:
        frame = self.profile.command(beverage)
        if frame.opcode != commands.OPCODE_BREW:
            raise KeyError(f"{beverage!r} is not a beverage")
        return frame

    async def read_recipe(self, beverage: str, *, slot: int = 1, timeout: float = 10.0) -> Recipe:
        """A profile's current recipe for a stored beverage (``0xA6``)."""
        beverage_id = self._beverage_frame(beverage).payload[0]

        def accept(frame: Frame) -> Recipe | None:
            if frame.opcode != commands.OPCODE_RECIPE:
                return None
            recipe = parse_recipe(frame)
            return recipe if (recipe.slot, recipe.beverage_id) == (slot, beverage_id) else None

        return await self._exchange(
            commands.read_recipe(slot, beverage_id), accept, timeout=timeout,
            what=f"recipe {beverage} (profile {slot})", poll_answer=True,
        )  # fmt: skip

    async def brew_custom(
        self,
        beverage: str,
        overrides: dict[int, int],
        *,
        slot: int = 1,
        confirm_timeout: float = 30.0,
    ) -> MachineState:
        """Brew with the machine's own recipe for ``slot``, changing only ``overrides``.

        ``overrides`` maps recipe tags to values, e.g. ``{TAG_COFFEE_ML: 40}``.
        Tags the recipe does not contain are refused rather than invented.
        """
        self._beverage_frame(beverage)  # unknown beverages raise here
        recipe = await self.read_recipe(beverage, slot=slot)
        unknown = set(overrides) - set(recipe.params)
        if unknown:
            raise ValueError(f"{beverage} has no parameter(s) {sorted(unknown)}")
        params = {**recipe.params, **overrides}
        # Always PREPARE: a custom amount must never save over the stored recipe,
        # whatever the stored command's trailer says.
        frame = commands.brew(
            recipe.beverage_id, params, slot=slot, operation=commands.BrewOperation.PREPARE
        )
        state = await self.refresh()
        if not self.profile.is_ready(state):
            raise CommandRejected(f"machine is not ready for {beverage}")
        await self.send(frame)
        return await self.wait_for(self.profile.is_busy, confirm_timeout, f"start {beverage}")
