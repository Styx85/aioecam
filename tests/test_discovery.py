from collections.abc import AsyncIterator

import aiohttp
import pytest

from aioecam.discovery import probe_module
from tests.fake_device import FakeDevice


@pytest.fixture
async def device() -> AsyncIterator[FakeDevice]:
    dev = FakeDevice(lan_key="x")
    await dev.start()
    yield dev
    await dev.stop()


async def test_probe_ready_module(device: FakeDevice) -> None:
    async with aiohttp.ClientSession() as http:
        info = await probe_module(http, device.ip, port=device.port)
    assert info.dsn == "AC000W000000001" and info.lan_ready
    assert info.registration_type == "AP-Mode"


async def test_probe_module_without_cloud(device: FakeDevice) -> None:
    device.lan_enabled = False
    async with aiohttp.ClientSession() as http:
        info = await probe_module(http, device.ip, port=device.port)
    assert info.dsn is None and not info.lan_ready
