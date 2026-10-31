from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import pytest

from aioecam.cloud.api import AylaCloud, CloudError
from aioecam.cloud.link import CloudLink
from aioecam.device import EcamDevice
from aioecam.ecam import commands
from aioecam.ecam.profile import load_profile
from aioecam.ecam.value import decode_value
from aioecam.exceptions import AuthError, CommandTimeout, NotConnected
from aioecam.lan.link import LinkState
from tests.fake_cloud import CloudMachine, FakeCloud
from tests.fake_machine import MONITOR, RESPONSE_PROP, FakeMachine

DSN = "AC000W000000009"
FAST = {"interval": 0.2, "fast_interval": 0.05, "fast_window": 2.0, "max_backoff": 0.2}
TABLE = {100: 981155, 101: 410, 3000: 396}


@pytest.fixture
async def rig() -> AsyncIterator[tuple[EcamDevice, FakeMachine, CloudMachine, FakeCloud]]:
    fake = FakeCloud()
    region = await fake.start()
    module = CloudMachine(fake, DSN)
    machine = FakeMachine(module, statistics=TABLE)  # type: ignore[arg-type]
    async with aiohttp.ClientSession() as http:
        cloud = AylaCloud(http, region, "refresh-0")
        link = CloudLink(cloud, dsn=DSN, watch=[MONITOR, RESPONSE_PROP], **FAST)
        ecam = EcamDevice(link, load_profile("ecam61x"))
        link.start()
        await link.wait_active(2)
        yield ecam, machine, module, fake
        await link.close()
    await fake.stop()


async def test_power_on_and_off_through_the_cloud(rig: Any) -> None:
    ecam, machine, _, _ = rig
    assert ecam.available and ecam.state is not None and ecam.state.status == 0
    assert (await ecam.power_on(confirm_timeout=5)).status == 7
    assert (await ecam.power_off(confirm_timeout=5)).status == 0
    # The machine publishes changes itself; no monitor requests over the cloud.
    assert machine.monitor_requests == 0


async def test_statistics_answers_arrive_through_the_poll(rig: Any) -> None:
    ecam, *_ = rig
    snapshot = await asyncio.wait_for(ecam.read_statistics(page_timeout=3), 10)
    assert snapshot.raw == TABLE


async def test_accepted_write_is_not_proof_of_delivery(rig: Any) -> None:
    ecam, machine, module, fake = rig
    module.online = False  # the cloud takes the write but never relays it
    with pytest.raises(CommandTimeout):
        await ecam.power_on(confirm_timeout=1)
    assert fake.datapoints and machine.status == 0


async def test_idle_poll_stays_slow(rig: Any) -> None:
    _, _, _, fake = rig
    before = fake.property_reads
    await asyncio.sleep(1.0)
    assert fake.property_reads - before <= 6  # 0.2 s interval, no fast window


async def test_link_goes_down_after_failed_polls_and_recovers(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ecam, *_ = rig
    link: CloudLink = ecam.link  # type: ignore[assignment]
    real = link._cloud.read_properties
    failing = True

    async def flaky(dsn: str, names: list[str] | None) -> dict[str, dict[str, Any]]:
        if failing:
            raise CloudError("GET properties -> HTTP 500")
        return await real(dsn, names)

    monkeypatch.setattr(link._cloud, "read_properties", flaky)
    for _ in range(50):
        if link.state is LinkState.IDLE:
            break
        await asyncio.sleep(0.05)
    assert link.state is LinkState.IDLE and not ecam.available
    assert link.stats.last_error == "GET properties -> HTTP 500"
    failing = False
    await link.wait_active(2)
    assert ecam.available


async def test_eletta_writes_carry_the_app_signature(rig: Any) -> None:
    _, _, _, fake = rig
    cloud_link: CloudLink = rig[0].link
    eletta = EcamDevice(cloud_link, load_profile("ecam45x"))
    await eletta.send(commands.monitor_request())
    await eletta.keep_cloud_session()
    (_, request_name, request), (_, keepalive_name, keepalive) = fake.datapoints[-2:]
    assert request_name == "app_data_request"
    assert decode_value(request).app_id == commands.APP_SIGNATURE
    assert keepalive_name == "app_device_connected"
    assert base64.b64decode(keepalive)[4:] == commands.APP_SIGNATURE


async def test_settings_answer_in_its_own_property_arrives(rig: Any) -> None:
    ecam, *_ = rig
    # The Soul republishes 0x95 answers into d062_mchn_sett, not data_response.
    assert await ecam.read_setting("auto_off", timeout=3) == 2


async def test_keepalive_does_not_start_fast_polling(rig: Any) -> None:
    ecam, _, _, fake = rig
    await ecam.keep_cloud_session()
    before = fake.property_reads
    await asyncio.sleep(0.5)
    assert fake.property_reads - before <= 3  # 0.2 s interval, not 0.05 s


async def test_an_old_answer_is_never_passed_on_twice(rig: Any) -> None:
    ecam, *_ = rig
    link: CloudLink = ecam.link
    seen: list[str] = []
    link.subscribe(lambda p: seen.append(p.value), name="x")
    link._deliver({"x": {"value": "new", "data_updated_at": "2026-10-31T08:00:02Z"}})
    link._deliver({"x": {"value": "old", "data_updated_at": "2026-10-31T08:00:01Z"}})
    link._deliver({"x": {"value": "new", "data_updated_at": "2026-10-31T08:00:02Z"}})
    assert seen == ["new"]


async def test_offline_machine_is_not_available(rig: Any) -> None:
    ecam, _, _, fake = rig
    link: CloudLink = ecam.link
    fake.connection_status[DSN] = "Offline"
    link._status_at = 0  # check the connection on the next poll
    link._wake.set()
    for _ in range(40):
        if not ecam.available:
            break
        await asyncio.sleep(0.05)
    assert not ecam.available and link.stats.machine_status == "Offline"
    with pytest.raises(NotConnected, match="Offline"):
        await ecam.refresh()
    fake.connection_status[DSN] = "Online"
    await link.wait_active(2)


async def test_rejected_sign_in_is_reported(rig: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    ecam, *_ = rig
    link: CloudLink = ecam.link

    async def rejected(dsn: str, names: list[str] | None) -> dict[str, dict[str, Any]]:
        raise AuthError("cloud rejected a fresh access token")

    monkeypatch.setattr(link._cloud, "read_properties", rejected)
    link._wake.set()
    for _ in range(40):
        if link.stats.auth_failed:
            break
        await asyncio.sleep(0.05)
    assert link.stats.auth_failed and link.state is LinkState.IDLE


async def test_unexpected_error_does_not_stop_the_poll(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ecam, *_ = rig
    link: CloudLink = ecam.link
    real = link._cloud.read_properties
    calls = 0

    async def broken_once(dsn: str, names: list[str] | None) -> dict[str, dict[str, Any]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TypeError("malformed answer")
        return await real(dsn, names)

    monkeypatch.setattr(link._cloud, "read_properties", broken_once)
    link._wake.set()
    for _ in range(40):
        if calls >= 2 and link.state is LinkState.ACTIVE:
            break
        await asyncio.sleep(0.05)
    assert calls >= 2 and link.state is LinkState.ACTIVE
    await link.close()
    assert link.state is LinkState.CLOSED


async def test_soul_keepalive_is_a_monitor_request(rig: Any) -> None:
    ecam, _, _, fake = rig
    await ecam.keep_cloud_session()
    _, name, value = fake.datapoints[-1]
    assert name == "data_request"
    assert decode_value(value).frame == commands.monitor_request()
    assert decode_value(value).app_id is None
