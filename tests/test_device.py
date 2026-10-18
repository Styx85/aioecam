from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from aioecam.device import EcamDevice
from aioecam.ecam.monitor import MachineState
from aioecam.ecam.profile import load_profile
from aioecam.exceptions import CommandRejected, CommandTimeout
from aioecam.lan.server import LanServer
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


async def test_power_on_confirmed(rig: tuple[EcamDevice, FakeMachine]) -> None:
    ecam, machine = rig
    seen: list[MachineState] = []
    ecam.subscribe(seen.append)
    state = await ecam.power_on(confirm_timeout=3)
    assert state.status in (1, 7)
    assert machine.received[-1].payload == b"\x02\x01"
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
    await ecam._wait_for(lambda s: s.status == 7, 3, "ready")
    state = await ecam.power_off(confirm_timeout=3)
    assert state.status in (0, 2)


async def test_brew_requires_ready(rig: tuple[EcamDevice, FakeMachine]) -> None:
    ecam, machine = rig
    with pytest.raises(CommandRejected, match="not ready"):
        await ecam.brew("espresso")
    assert not [f for f in machine.received if f.opcode == 0x83]


async def test_brew_confirmed(rig: tuple[EcamDevice, FakeMachine]) -> None:
    ecam, machine = rig
    await ecam.power_on(confirm_timeout=3)
    await ecam._wait_for(lambda s: s.status == 7, 3, "ready")
    await ecam.brew("espresso", confirm_timeout=3)
    assert machine.received[-1] == ecam.profile.command("espresso")


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
