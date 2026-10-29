from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import pytest

from aioecam.cloud.api import AylaCloud
from aioecam.device import TAG_COFFEE_ML, TAG_MILK, TAG_WATER_ML, EcamDevice
from aioecam.ecam import commands
from aioecam.ecam.frame import RESPONSE, Frame
from aioecam.ecam.monitor import MachineState
from aioecam.ecam.profile import load_profile
from aioecam.ecam.value import decode_value, encode_value
from aioecam.exceptions import CommandRejected, CommandTimeout, FrameError
from aioecam.lan.server import LanServer
from tests.fake_cloud import FakeCloud
from tests.fake_device import FakeDevice
from tests.fake_machine import FakeMachine

KEY = "dGVzdGtleXRlc3RrZXl0ZXN0a2V5dGVzdA=="
FAST = {"keepalive": 0.5, "probe_timeout": 0.5, "register_timeout": 0.5, "max_backoff": 0.3}
# Sparse table like the real machine: 13 entries, ids not contiguous.
TABLE = {100: 981155, 101: 410, 105: 30, 106: 2736033, 108: 22, 109: 54380, 111: 18,
         115: 4325, 116: 1632, 3000: 396, 3001: 5231, 3002: 2, 3003: 180}  # fmt: skip


@pytest.fixture
async def rig() -> AsyncIterator[tuple[EcamDevice, FakeMachine]]:
    device = FakeDevice(lan_key=KEY)
    await device.start()
    machine = FakeMachine(device, statistics=TABLE)
    async with LanServer(host="127.0.0.1", port=0, advertise_ip="127.0.0.1") as server:
        link = server.add_link(
            device_ip=device.ip, lan_key=KEY, dsn=device.dsn, device_port=device.port, **FAST
        )
        ecam = EcamDevice(link, load_profile("ecam61x"), poll_interval=0.2)
        await link.wait_active(3)
        yield ecam, machine
    await device.stop()


async def test_refresh_reads_frozen_standby_frame(rig: tuple[EcamDevice, FakeMachine]) -> None:
    ecam, _ = rig
    state = await ecam.refresh()
    assert state.status == 0 and state.action == 2  # standby keeps action != 0
    assert not ecam.is_busy()


def commands_sent(machine: FakeMachine) -> list[Any]:
    """Frames sent to the machine, without the monitor requests used for polling."""
    return [f for f in machine.received if not (f.opcode == 0x75 and not f.payload)]


async def test_power_on_confirmed(rig: tuple[EcamDevice, FakeMachine]) -> None:
    ecam, machine = rig
    seen: list[MachineState] = []
    ecam.subscribe(seen.append)
    state = await ecam.power_on(confirm_timeout=3)
    assert state.status in (1, 7)
    assert commands_sent(machine)[-1].payload == b"\x02\x01"
    assert seen and seen[-1] == ecam.state


async def test_power_on_when_already_on_sends_nothing(
    rig: tuple[EcamDevice, FakeMachine],
) -> None:
    ecam, machine = rig
    await ecam.power_on(confirm_timeout=3)
    sent = len(machine.received)
    await ecam.power_on(confirm_timeout=3)
    assert len(machine.received) == sent


async def test_power_on_not_confirmed_times_out(rig: tuple[EcamDevice, FakeMachine]) -> None:
    ecam, machine = rig
    machine.ignore_wakeup = True
    with pytest.raises(CommandTimeout, match="power on not confirmed"):
        await ecam.power_on(confirm_timeout=1)


async def test_power_off_confirmed(rig: tuple[EcamDevice, FakeMachine]) -> None:
    ecam, _ = rig
    await ecam.power_on(confirm_timeout=3)
    await ecam.wait_until_ready(timeout=3)
    state = await ecam.power_off(confirm_timeout=3)
    assert state.status in (0, 2)


async def test_brew_requires_ready(rig: tuple[EcamDevice, FakeMachine]) -> None:
    ecam, machine = rig
    with pytest.raises(CommandRejected, match="not ready"):
        await ecam.brew("espresso")
    assert not [f for f in machine.received if f.opcode == 0x83]


async def test_unknown_beverage(rig: tuple[EcamDevice, FakeMachine]) -> None:
    ecam, _ = rig
    with pytest.raises(KeyError):
        await ecam.brew("teleport")


async def test_read_statistics_pages_sparse_table(rig: tuple[EcamDevice, FakeMachine]) -> None:
    ecam, _ = rig
    pages: list[dict[str, int]] = []
    snap = await ecam.read_statistics(progress=pages.append, page_timeout=2)
    assert snap.raw == TABLE
    assert [p["returned"] for p in pages] == [10, 3]
    assert snap.known["descale_count"] == 30
    assert snap.unknown == {101: 410, 111: 18, 116: 1632}


async def test_read_statistics_adapts_page_size(rig: tuple[EcamDevice, FakeMachine]) -> None:
    ecam, machine = rig
    machine.max_a2_page = 8  # full pages time out on this "firmware"
    snap = await ecam.read_statistics(page_timeout=0.3, retry_pause=0)
    assert snap.raw == TABLE
    requested = [f.payload[2] for f in machine.received if f.opcode == 0xA2]
    assert requested[:3] == [10, 9, 8]


async def test_statistics_timeout_on_silent_machine(rig: tuple[EcamDevice, FakeMachine]) -> None:
    ecam, machine = rig
    machine.max_a2_page = 0
    with pytest.raises(CommandTimeout):
        await ecam.read_statistics(page_timeout=0.2, retry_pause=0, page_size=2)


async def test_brew_is_confirmed_although_status_stays_ready(
    rig: tuple[EcamDevice, FakeMachine],
) -> None:
    ecam, machine = rig
    machine.step = 0.4  # slow enough for the poller to see the running beverage
    await ecam.power_on(confirm_timeout=5)
    await ecam.wait_until_ready(timeout=5)
    state = await ecam.brew("espresso", confirm_timeout=3)
    assert commands_sent(machine)[-1] == ecam.profile.command("espresso")
    assert state.status == 7 and state.action != 0 and ecam.is_busy()
    await ecam.wait_until_ready(timeout=5)
    assert not ecam.is_busy()


async def test_brew_refused_while_ready_but_busy(rig: tuple[EcamDevice, FakeMachine]) -> None:
    ecam, machine = rig
    machine.step = 0.5
    await ecam.power_on(confirm_timeout=5)
    await ecam.wait_until_ready(timeout=5)
    await ecam.brew("espresso", confirm_timeout=3)
    with pytest.raises(CommandRejected, match="action"):
        await ecam.brew("espresso")


async def test_polling_keeps_state_current_without_pushes(
    rig: tuple[EcamDevice, FakeMachine],
) -> None:
    ecam, _ = rig
    seen: list[int] = []
    ecam.subscribe(lambda s: seen.append(s.status))
    ecam.start_polling(awake=0.1, asleep=0.1)
    try:
        await ecam.send(ecam.profile.command("wakeup"))  # unconfirmed raw send
        await ecam.wait_until_ready(timeout=3)
        assert 1 in seen and seen[-1] == 7  # saw the wake-up sequence by polling alone
    finally:
        await ecam.stop_polling()


async def test_pushes_are_used_when_the_machine_sends_them(
    rig: tuple[EcamDevice, FakeMachine],
) -> None:
    ecam, machine = rig
    machine.push_changes = True
    await ecam.refresh()
    seen: list[int] = []
    ecam.subscribe(lambda s: seen.append(s.status))
    await ecam.send(ecam.profile.command("wakeup"))
    await ecam.wait_until_ready(timeout=3)
    assert 1 in seen and 7 in seen


@pytest.fixture
async def cloud_rig() -> AsyncIterator[tuple[EcamDevice, FakeMachine, FakeCloud]]:
    cloud = FakeCloud()
    region = await cloud.start()
    device = FakeDevice(lan_key=KEY)
    await device.start()
    machine = FakeMachine(device, statistics=TABLE)

    async def deliver(dsn: str, name: str, value: str) -> None:
        # The cloud path reaches the machine even when the local one did not.
        machine.ignore_wakeup = False
        await machine._on_write(name, value)

    cloud.on_datapoint = deliver
    async with (
        aiohttp.ClientSession() as http,
        LanServer(http, host="127.0.0.1", port=0, advertise_ip="127.0.0.1") as server,
    ):
        link = server.add_link(
            device_ip=device.ip, lan_key=KEY, dsn=device.dsn, device_port=device.port, **FAST
        )
        api = AylaCloud(http, region, "refresh-0")
        ecam = EcamDevice(link, load_profile("ecam61x"), poll_interval=0.2, cloud=api)
        await link.wait_active(3)
        yield ecam, machine, cloud
    await device.stop()
    await cloud.stop()


async def test_wake_falls_back_to_cloud(
    cloud_rig: tuple[EcamDevice, FakeMachine, FakeCloud],
) -> None:
    ecam, machine, cloud = cloud_rig
    machine.ignore_wakeup = True  # local wake-up has no effect
    state = await ecam.power_on(confirm_timeout=5, local_grace=0.6)
    assert state.status != 0
    assert ecam.cloud_fallbacks == 1
    ((dsn, name, value),) = cloud.datapoints
    assert (dsn, name) == (ecam.link.dsn, "data_request")
    assert decode_value(value).frame == commands.power_on()


async def test_wake_without_fallback_when_local_works(
    cloud_rig: tuple[EcamDevice, FakeMachine, FakeCloud],
) -> None:
    ecam, _, cloud = cloud_rig
    await ecam.power_on(confirm_timeout=5, local_grace=2)
    assert ecam.cloud_fallbacks == 0 and cloud.datapoints == []


async def test_unsubscribe_twice_is_harmless(rig: tuple[EcamDevice, FakeMachine]) -> None:
    ecam, _ = rig
    unsubscribe = ecam.subscribe(lambda s: None)
    unsubscribe()
    unsubscribe()  # e.g. Home Assistant and the integration both shutting down
    off = ecam.link.on_state(lambda s: None)
    off()
    off()


# --- M7: session, profiles, settings, recipes ------------------------------------------

D034_PROFILES_1_3 = (
    "0Eek8AEDAEEAbABpAGMAZQAAAAAAAAAAAAAQAEIAbwBiAAAAAAAAAAAAAAAAAAAF"
    "AFAAcgBvAGYAaQBsACAAMwAAAAAJAKhz"
)  # lodzen capture, anonymised names
D039_RECIPE_ESPRESSO_P1 = "0BSm8AEBCAABACgbAQIEBAAZAaPq"  # lodzen capture


async def test_session_announce_is_a_bare_timestamp(rig: tuple[EcamDevice, FakeMachine]) -> None:
    ecam, machine = rig
    await ecam.announce_session()
    await eventually_true(lambda: machine.session_at is not None)
    first = machine.session_at
    await ecam.announce_session()  # fresh: not repeated
    assert machine.session_at == first


async def test_select_profile(rig: tuple[EcamDevice, FakeMachine]) -> None:
    ecam, machine = rig
    await ecam.select_profile(3, timeout=3)
    assert machine.profile == 3
    with pytest.raises(ValueError):
        await ecam.select_profile(6)


async def test_select_profile_refused_without_session(rig: tuple[EcamDevice, FakeMachine]) -> None:
    ecam, machine = rig
    ecam._session_at = time.time()  # pretend a session exists the machine never saw
    with pytest.raises(CommandRejected, match="refused profile"):
        await ecam.select_profile(2, timeout=3)
    assert machine.profile == 1


async def test_profile_selection_waits_past_previous_slots_ack(
    rig: tuple[EcamDevice, FakeMachine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ecam, machine = rig
    respond = machine._respond

    async def stale_then_current(opcode: int, flags: int, payload: bytes) -> None:
        if opcode == 0xA9:
            await respond(opcode, flags, b"\x01\x00")
            await asyncio.sleep(0.05)
        await respond(opcode, flags, payload)

    monkeypatch.setattr(machine, "_respond", stale_then_current)
    await ecam.select_profile(2, timeout=3)
    assert machine.profile == 2


async def test_profile_ack_can_require_a_property_read(
    rig: tuple[EcamDevice, FakeMachine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ecam, machine = rig
    respond = machine._respond

    async def store_only(opcode: int, flags: int, payload: bytes) -> None:
        if opcode == 0xA9:
            machine.device.properties["data_response"] = encode_value(
                Frame(RESPONSE, opcode, flags, payload)
            )
        else:
            await respond(opcode, flags, payload)

    monkeypatch.setattr(machine, "_respond", store_only)
    await ecam.select_profile(2, timeout=3)
    await ecam.select_profile(1, timeout=3)
    assert machine.profile == 1
    assert not ecam._responses


async def test_polled_profile_selection_still_reports_timeout(
    rig: tuple[EcamDevice, FakeMachine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ecam, machine = rig

    async def silent(opcode: int, flags: int, payload: bytes) -> None:
        pass

    monkeypatch.setattr(machine, "_respond", silent)
    with pytest.raises(CommandTimeout, match="profile 2"):
        await ecam.select_profile(2, timeout=0.7)
    assert not ecam._responses
    assert not ecam.link._waiters.get("data_response")


async def test_profiles(rig: tuple[EcamDevice, FakeMachine]) -> None:
    ecam, machine = rig
    machine.profile_names_value = D034_PROFILES_1_3
    slots = await ecam.profiles(timeout=3)
    assert [s.name for s in slots] == ["Alice", "Bob", "Profil 3"]


async def test_settings_roundtrip(rig: tuple[EcamDevice, FakeMachine]) -> None:
    ecam, machine = rig
    assert await ecam.read_setting("auto_off", timeout=3) == 2
    await ecam.write_setting("auto_off", 1, timeout=3)
    assert machine.settings[0x3E] == 1


async def test_setting_write_dropped_by_machine_is_detected(
    rig: tuple[EcamDevice, FakeMachine],
) -> None:
    ecam, _ = rig
    ecam._session_at = time.time()  # machine has no session: the write is dropped
    with pytest.raises(CommandRejected, match="reads back 2"):
        await ecam.write_setting("auto_off", 0, timeout=3)


async def test_setting_validation(rig: tuple[EcamDevice, FakeMachine]) -> None:
    ecam, _ = rig
    with pytest.raises(ValueError):
        await ecam.write_setting("auto_off", 9)
    with pytest.raises(KeyError):
        await ecam.read_setting("cup_light")


async def test_read_recipe(rig: tuple[EcamDevice, FakeMachine]) -> None:
    ecam, machine = rig
    machine.recipes[(1, 1)] = D039_RECIPE_ESPRESSO_P1
    recipe = await ecam.read_recipe("espresso", slot=1, timeout=3)
    assert recipe.params[TAG_COFFEE_ML] == 40


async def test_repeated_recipe_read_uses_learned_property_not_cached_value(
    rig: tuple[EcamDevice, FakeMachine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ecam, machine = rig
    machine.recipes[(1, 1)] = D039_RECIPE_ESPRESSO_P1
    push = machine.device.push
    published = False

    async def publish_recipe_once(name: str, value: str) -> int:
        nonlocal published
        machine.device.properties[name] = value
        if "_rec_" in name:
            if published:
                return 200
            published = True
        return await push(name, value)

    monkeypatch.setattr(machine.device, "push", publish_recipe_once)
    assert (await ecam.read_recipe("espresso", timeout=3)).params[TAG_COFFEE_ML] == 40
    old = decode_value(D039_RECIPE_ESPRESSO_P1).frame
    machine.recipes[(1, 1)] = encode_value(
        Frame(
            old.direction,
            old.opcode,
            old.flags,
            old.payload.replace(bytes.fromhex("010028"), bytes.fromhex("010032")),
        )
    )
    assert (await ecam.read_recipe("espresso", timeout=3)).params[TAG_COFFEE_ML] == 50
    assert not ecam._responses


async def test_hot_water_after_recipe_read_without_second_publication(
    rig: tuple[EcamDevice, FakeMachine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ecam, machine = rig
    machine.step = 0.2
    recipe = Frame(RESPONSE, 0xA6, 0xF0, bytes.fromhex("011019010f00fa1b01"))
    machine.recipes[(1, 0x10)] = encode_value(recipe)
    push = machine.device.push
    published = False

    async def publish_recipe_once(name: str, value: str) -> int:
        nonlocal published
        machine.device.properties[name] = value
        if "_rec_" in name:
            if published:
                return 200
            published = True
        return await push(name, value)

    monkeypatch.setattr(machine.device, "push", publish_recipe_once)
    await ecam.power_on(confirm_timeout=3)
    await ecam.wait_until_ready(timeout=3)
    assert (await ecam.read_recipe("hot_water", timeout=3)).params[TAG_WATER_ML] == 250
    await ecam.brew_custom("hot_water", {TAG_WATER_ML: 100}, slot=1)
    sent = [f for f in machine.received if f.opcode == 0x83]
    assert len(sent) == 1
    assert sent[0].encode().hex() == "0d0d83f010010f00641b0106e5e1"
    assert decode_value(machine.recipes[(1, 0x10)]).frame == recipe


async def test_brew_custom_changes_only_the_override(rig: tuple[EcamDevice, FakeMachine]) -> None:
    ecam, machine = rig
    machine.step = 0.4
    machine.recipes[(1, 1)] = D039_RECIPE_ESPRESSO_P1
    await ecam.power_on(confirm_timeout=5)
    await ecam.wait_until_ready(timeout=5)
    await ecam.brew_custom("espresso", {TAG_COFFEE_ML: 30}, slot=1)
    sent = [f for f in machine.received if f.opcode == 0x83][-1]
    # coffee 30 ml (0x001e), everything else as the machine's recipe, filtered like the app
    assert sent.payload.hex() == "0101" + "01001e" + "1b01" + "0204" + "0400" + "06"


async def test_brew_custom_refuses_unknown_parameter(rig: tuple[EcamDevice, FakeMachine]) -> None:
    ecam, machine = rig
    machine.recipes[(1, 1)] = D039_RECIPE_ESPRESSO_P1
    with pytest.raises(ValueError, match="no parameter"):
        await ecam.brew_custom("espresso", {TAG_MILK: 100}, slot=1)


async def test_monitor_request(rig: tuple[EcamDevice, FakeMachine]) -> None:
    ecam, machine = rig
    state = await ecam.request_monitor(timeout=3)
    assert machine.monitor_requests == 1 and state.status == 0


async def eventually_true(check: Any, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not check():  # noqa: ASYNC110 - test helper
            await asyncio.sleep(0.02)


async def test_passive_diagnostics_identical_monitor_and_no_writes(
    rig: tuple[EcamDevice, FakeMachine],
) -> None:
    ecam, machine = rig
    await ecam.refresh()
    before = ecam.last_monitor_at
    seen = []
    ecam.subscribe(seen.append)
    await asyncio.sleep(0.01)
    await ecam.refresh()
    assert ecam.last_monitor_at > before
    assert seen == []  # fresh receipt, unchanged contents
    assert ecam.monitor_query_result == "response_received"
    count = len(machine.received)
    await machine.device.push(
        "arbitrary_recipe_name",
        encode_value(Frame(RESPONSE, 0xA6, 0xF0, bytes.fromhex("01 01 01 0028"))),
    )
    assert ecam.catalog.snapshot()["entries"][0]["data"]["parameters"] == {1: 40}
    assert len(machine.received) == count  # observation never sends a command
    original_value = machine.device.properties[ecam.profile.monitor_property]
    valid_at = ecam.last_monitor_at
    machine.device.properties[ecam.profile.monitor_property] = "not a monitor"
    with pytest.raises(FrameError):
        await ecam.refresh()
    assert ecam.monitor_query_result == "failed"
    assert ecam.monitor_query_error == "FrameError"
    assert ecam.last_monitor_at == valid_at
    assert ecam.last_monitor_error_at is not None
    machine.device.properties[ecam.profile.monitor_property] = original_value
    await ecam.refresh()
    assert ecam.monitor_query_result == "response_received"
    assert ecam.monitor_query_error is None
    assert ecam.last_monitor_error == "FrameError"  # retained after recovery


async def test_request_monitor_waits_for_the_late_frame(
    rig: tuple[EcamDevice, FakeMachine],
) -> None:
    # Measured offline (WAN blocked): no published changes; a 0x75 request
    # publishes the current state 1-2 s after the module fetched it.
    ecam, machine = rig
    machine.publish_on_request_only = True
    machine.publish_delay = 0.4
    before = await ecam.refresh()
    await machine._set(7)  # e.g. woken at the display: nothing is published
    assert (await ecam.refresh()).status == 0
    state = await ecam.request_monitor(timeout=2)
    assert state.status == 7 and state.timestamp > before.timestamp
    assert ecam.monitor_query_result == "response_received"


async def test_request_monitor_without_new_frame_returns_latest(
    rig: tuple[EcamDevice, FakeMachine],
) -> None:
    ecam, machine = rig
    machine.publish_on_request_only = True
    machine.publish_delay = 5.0  # later than our timeout
    before = await ecam.refresh()
    state = await ecam.request_monitor(timeout=0.3)
    assert state == before and ecam.monitor_query_result == "no_new_frame"


async def test_power_on_and_off_confirmed_by_the_offline_module(
    rig: tuple[EcamDevice, FakeMachine],
) -> None:
    ecam, machine = rig
    machine.publish_on_request_only = True
    machine.publish_delay = 0.3
    machine.step = 0.2
    state = await ecam.power_on(confirm_timeout=5)
    assert state.status in (1, 7)
    await ecam.wait_until_ready(timeout=5)
    state = await ecam.power_off(confirm_timeout=5)
    assert state.status in (0, 2)
    assert machine.monitor_requests >= 2


async def test_cloud_fallback_not_used_when_offline_wake_worked(
    cloud_rig: tuple[EcamDevice, FakeMachine, FakeCloud],
) -> None:
    # Before the fix the stale frame looked like "still asleep" and the cloud
    # was asked to wake an already running machine.
    ecam, machine, cloud = cloud_rig
    machine.publish_on_request_only = True
    machine.publish_delay = 0.3
    state = await ecam.power_on(confirm_timeout=5, local_grace=2)
    assert state.status in (1, 7)
    assert ecam.cloud_fallbacks == 0 and cloud.datapoints == []


async def test_request_monitor_is_bounded_by_its_timeout(
    rig: tuple[EcamDevice, FakeMachine],
) -> None:
    # Codex review A4: the send alone used a fixed 10 s timeout, so
    # request_monitor(timeout=0.5) could block for 10 s and more.
    ecam, machine = rig
    await ecam.refresh()
    machine.device.responsive = False  # the module stops fetching commands
    await asyncio.sleep(0.5)  # let a poll still running from refresh() finish
    started = time.monotonic()
    with pytest.raises(CommandTimeout):
        await ecam.request_monitor(timeout=0.5)
    assert time.monotonic() - started < 2.0


async def test_out_of_order_monitor_frame_is_ignored(rig: tuple[EcamDevice, FakeMachine]) -> None:
    # Codex review A5: a delayed older frame replaced the newer state.
    from aioecam.lan.messages import Datapoint
    from tests.fake_machine import MONITOR, monitor_value

    ecam, _ = rig
    now = int(time.time())
    ecam._on_monitor(Datapoint(MONITOR, monitor_value(7, action=8, timestamp=now), "string"))
    ecam._on_monitor(Datapoint(MONITOR, monitor_value(7, timestamp=now - 5), "string"))
    assert ecam.state is not None and ecam.state.action == 8 and ecam.state.timestamp == now
    # A much older timestamp is a module clock reset, not reordering: accept it.
    ecam._on_monitor(Datapoint(MONITOR, monitor_value(0, timestamp=now - 86400), "string"))
    assert ecam.state.status == 0
