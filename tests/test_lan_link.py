from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

import aiohttp
import pytest

from aioecam.exceptions import CommandTimeout, NotConnected
from aioecam.lan.link import LanLink, LinkState
from aioecam.lan.messages import Datapoint
from aioecam.lan.server import LanServer
from tests.fake_device import FakeDevice

KEY = "dGVzdGtleXRlc3RrZXl0ZXN0a2V5dGVzdA=="
MONITOR = "0BJ1DwAAAAAABwAAAAAAAADWo2rU/ZE="
FAST = {"keepalive": 0.2, "probe_timeout": 0.5, "register_timeout": 0.5, "max_backoff": 0.3}


async def eventually(check: Callable[[], bool], timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not check():  # noqa: ASYNC110 - polling arbitrary test conditions
            await asyncio.sleep(0.02)


@pytest.fixture
async def server() -> AsyncIterator[LanServer]:
    async with LanServer(host="127.0.0.1", port=0, advertise_ip="127.0.0.1") as srv:
        yield srv


@pytest.fixture
async def device() -> AsyncIterator[FakeDevice]:
    dev = FakeDevice(lan_key=KEY, properties={"d302_monitor": MONITOR})
    await dev.start()
    yield dev
    await dev.stop()


async def connected(link: LanLink, device: FakeDevice, key_exchanges: int = 1) -> None:
    """Both sides done: the link is active and the device holds the session."""
    await link.wait_active(3)
    await eventually(lambda: device.key_exchanges >= key_exchanges)


def link_to(server: LanServer, device: FakeDevice, **options: float | int) -> LanLink:
    return server.add_link(
        device_ip=device.ip,
        lan_key=device.lan_key,
        dsn="AC000W000000001",
        device_port=device.port,
        **{**FAST, **options},
    )


async def test_connects_with_post(server: LanServer, device: FakeDevice) -> None:
    link = link_to(server, device, key_id=device.key_id)
    await link.wait_active(2)
    assert link.state is LinkState.ACTIVE
    assert device.registrations[0] == "POST"
    await eventually(lambda: device.key_exchanges == 1)  # device finishes after our reply


async def test_read_property(server: LanServer, device: FakeDevice) -> None:
    link = link_to(server, device)
    await link.wait_active(2)
    point = await link.read("d302_monitor", timeout=2)
    assert point == Datapoint("d302_monitor", MONITOR, "string")


async def test_write_property(server: LanServer, device: FakeDevice) -> None:
    link = link_to(server, device)
    await link.wait_active(2)
    await link.write("data_request", "DQeEDwIBVRJq1PRB", timeout=2)
    await eventually(lambda: device.writes == [("data_request", "DQeEDwIBVRJq1PRB")])


async def test_write_queued_before_session_is_delivered(
    server: LanServer, device: FakeDevice
) -> None:
    link = link_to(server, device)
    await link.write("data_request", "early", timeout=2)  # no wait_active first
    await eventually(lambda: device.writes == [("data_request", "early")])


async def test_keepalive_never_rekeys(server: LanServer, device: FakeDevice) -> None:
    link = link_to(server, device)
    await link.wait_active(2)
    await eventually(lambda: device.registrations.count("PUT") >= 3)
    assert device.key_exchanges == 1
    assert link.stats.liveness_failures == 0


async def test_push_routed_by_name(server: LanServer, device: FakeDevice) -> None:
    link = link_to(server, device)
    await connected(link, device)
    monitor: list[Datapoint] = []
    everything: list[Datapoint] = []
    link.subscribe(monitor.append, name="d302_monitor")
    link.subscribe(everything.append)
    await device.push("app_device_connected", "1792338839")
    await device.push("d302_monitor", MONITOR)
    await eventually(lambda: len(everything) == 2)
    # the cremalink bug: unrelated pushes must not end up as monitor values
    assert [p.name for p in monitor] == ["d302_monitor"]


async def test_dead_session_is_detected_and_reregistered(
    server: LanServer, device: FakeDevice
) -> None:
    link = link_to(server, device)
    await link.wait_active(2)
    states: list[LinkState] = []
    link.on_state(states.append)
    device.responsive = False  # accepts local_reg, never calls back
    await eventually(lambda: link.state is not LinkState.ACTIVE)
    assert link.stats.liveness_failures >= 1
    device.responsive = True
    device.drop_session()
    await connected(link, device, key_exchanges=2)
    assert LinkState.REGISTERING in states
    assert (await link.read("d302_monitor", timeout=2)).value == MONITOR


async def test_device_restart_rekeys_in_place(server: LanServer, device: FakeDevice) -> None:
    link = link_to(server, device)
    await link.wait_active(2)
    device.restart()
    await eventually(lambda: link.stats.key_exchanges == 2)
    assert link.state is LinkState.ACTIVE
    assert (await link.read("d302_monitor", timeout=2)).value == MONITOR


async def test_wrong_key_id_refused(server: LanServer, device: FakeDevice) -> None:
    link = link_to(server, device, key_id=device.key_id + 1)
    with pytest.raises(NotConnected):
        await link.wait_active(1)
    assert device.key_exchanges == 0


async def test_wrong_lan_key_rejected(server: LanServer, device: FakeDevice) -> None:
    link = server.add_link(
        device_ip=device.ip,
        lan_key="b3RoZXJrZXlvdGhlcmtleW90aGVya2V5MTI=",
        dsn="AC000W000000001",
        device_port=device.port,
        **FAST,
    )
    await connected(link, device)  # the key exchange itself is unauthenticated
    assert await device.push("d302_monitor", MONITOR) == 400
    assert link.stats.rejected_messages == 1


async def test_unknown_peer_refused(server: LanServer) -> None:
    connector = aiohttp.TCPConnector(local_addr=("127.0.0.3", 0))
    async with (
        aiohttp.ClientSession(connector=connector) as http,
        http.get(f"http://127.0.0.1:{server.port}/local_lan/commands.json") as resp,
    ):
        assert resp.status == 403


async def test_offline_device_times_out(server: LanServer) -> None:
    link = server.add_link(
        device_ip="127.0.0.4", lan_key=KEY, dsn="x", device_port=9, **FAST
    )  # nothing listens there
    with pytest.raises(CommandTimeout):
        await link.write("data_request", "x", timeout=0.5)
    assert link.state is not LinkState.ACTIVE


async def test_two_machines(server: LanServer, device: FakeDevice) -> None:
    other = FakeDevice(ip="127.0.0.5", lan_key=KEY, properties={"d302_monitor": "other"})
    await other.start()
    try:
        first, second = link_to(server, device), link_to(server, other)
        await first.wait_active(2)
        await second.wait_active(2)
        assert (await first.read("d302_monitor", timeout=2)).value == MONITOR
        assert (await second.read("d302_monitor", timeout=2)).value == "other"
    finally:
        await other.stop()


async def test_close_fails_pending_work(server: LanServer) -> None:
    link = server.add_link(device_ip="127.0.0.6", lan_key=KEY, dsn="x", device_port=9, **FAST)
    pending = asyncio.create_task(link.write("data_request", "x", timeout=5))
    await asyncio.sleep(0.1)
    await server.remove_link("127.0.0.6")
    with pytest.raises(NotConnected):
        await pending
    assert link.state is LinkState.CLOSED


async def test_module_without_cloud_refuses_lan_mode(
    server: LanServer, device: FakeDevice, caplog: pytest.LogCaptureFixture
) -> None:
    device.lan_enabled = False
    link = link_to(server, device)
    await eventually(lambda: device.registrations.count("POST") >= 3)
    assert link.state is not LinkState.ACTIVE
    assert link.stats.last_registration_status == 404
    warnings = [r for r in caplog.records if "refuses LAN mode" in r.getMessage()]
    assert len(warnings) == 1  # logged once, not on every retry
    device.lan_enabled = True  # cloud is back
    await connected(link, device)
    assert link.stats.last_registration_status == 202


async def test_request_dropped_by_the_module_is_not_an_error(
    server: LanServer, device: FakeDevice, caplog: pytest.LogCaptureFixture
) -> None:
    # Seen in HA logs: the module closed its HTTP request mid-body (Wi-Fi
    # hiccup) and aiohttp logged a ConnectionResetError traceback.
    link = server.add_link(
        device_ip=device.ip, lan_key=KEY, dsn=device.dsn, device_port=device.port
    )

    class DroppedRequest:
        remote = device.ip
        path = "/local_lan/commands.json"

        async def read(self) -> bytes:
            raise ConnectionResetError("Connection lost")

    async def never_called(link: LanLink, body: dict) -> aiohttp.web.Response:
        raise AssertionError("handler must not run for a dropped request")

    route = server._wrap(never_called)
    with caplog.at_level("DEBUG", logger="aioecam.lan.server"):
        response = await route(DroppedRequest())  # type: ignore[arg-type]
    assert response.status == 400
    assert "dropped mid-request" in caplog.text
    assert not [r for r in caplog.records if r.levelname in ("ERROR", "WARNING")]
    await link.close()


async def test_write_queued_during_a_probe_is_not_delayed_to_the_keepalive(
    server: LanServer, device: FakeDevice
) -> None:
    # Codex review A1: the loop cleared the wake-up signal after a probe, so a
    # write queued while the probe ran waited for the next keepalive (20 s in
    # HA) and timed out after 10 s ("machine did not fetch write ...").
    link = link_to(server, device, keepalive=30.0)
    await connected(link, device)
    original = link._alive
    queued: list[asyncio.Task[None]] = []

    async def alive_then_queue() -> bool:
        ok = await original()
        if not queued:
            queued.append(
                asyncio.create_task(link.write("data_request", "DQeEDwIBVRJq1PRB", timeout=3))
            )
            await asyncio.sleep(0)  # the write is queued and has set the wake-up signal
        return ok

    link._alive = alive_then_queue  # type: ignore[method-assign]
    link._wake.set()  # trigger one probe now instead of after 30 s
    await eventually(lambda: bool(queued))
    await asyncio.wait_for(queued[0], 5)  # raises CommandTimeout without the fix
    assert ("data_request", "DQeEDwIBVRJq1PRB") in device.writes


async def test_cancelled_write_is_not_delivered_later(
    server: LanServer, device: FakeDevice
) -> None:
    # Codex review A2: a cancelled caller left its command queued, so it could
    # still be executed after reconnecting (e.g. a brew the caller gave up on).
    link = link_to(server, device)
    await connected(link, device)
    device.responsive = False  # the module stops fetching commands for now
    task = asyncio.create_task(link.write("data_request", "DQeEDwIBVRJq1PRB", timeout=10))
    await eventually(lambda: len(link._outgoing) == 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not link._outgoing
    device.responsive = True
    await link.write("data_request", "DQeEDwIBVRJq1PRB", timeout=3)  # the module polls again
    await asyncio.sleep(0.3)  # give a stale queued command the chance to go out too
    assert device.writes == [("data_request", "DQeEDwIBVRJq1PRB")]  # once, not twice
