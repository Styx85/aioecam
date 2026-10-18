from __future__ import annotations

from collections.abc import AsyncIterator

import aiohttp
import pytest

from aioecam.cloud.api import AylaCloud
from aioecam.cloud.auth import login
from aioecam.cloud.onboarding import onboard
from aioecam.cloud.region import Region, load_region
from aioecam.exceptions import AuthError
from tests.fake_cloud import EMAIL, PASSWORD, FakeCloud

SOUL = {"dsn": "AC000W000000001", "product_name": "PrimaDonna Soul", "oem_model": "DL-millcore",
        "lan_enabled": True, "lan_ip": "192.168.1.50", "connection_status": "Online"}  # fmt: skip
ELETTA = {"dsn": "AC000W000000002", "product_name": "Eletta", "oem_model": "DL-striker-cb",
          "lan_enabled": False, "connection_status": "Online"}  # fmt: skip
AIRCON = {"dsn": "AC000W000000003", "product_name": "Pinguino", "oem_model": "DL-pac-n90",
          "lan_enabled": True}  # fmt: skip


@pytest.fixture
async def cloud() -> AsyncIterator[tuple[FakeCloud, Region, aiohttp.ClientSession]]:
    fake = FakeCloud()
    region = await fake.start()
    async with aiohttp.ClientSession() as http:
        yield fake, region, http
    await fake.stop()


def test_region_hides_secrets() -> None:
    region = load_region("eu")
    assert repr(region) == "Region('eu')"
    assert region.ayla_app_secret not in repr(region)


def test_unknown_region() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        load_region("mars")


async def test_login_walks_all_steps(
    cloud: tuple[FakeCloud, Region, aiohttp.ClientSession],
) -> None:
    _, region, http = cloud
    assert await login(http, region, EMAIL, PASSWORD) == "refresh-0"


async def test_wrong_password(cloud: tuple[FakeCloud, Region, aiohttp.ClientSession]) -> None:
    _, region, http = cloud
    with pytest.raises(AuthError, match="403042") as err:
        await login(http, region, EMAIL, "wrong")
    assert "wrong" not in str(err.value) and EMAIL not in str(err.value)


async def test_onboarding(cloud: tuple[FakeCloud, Region, aiohttp.ClientSession]) -> None:
    fake, region, http = cloud
    fake.devices = [SOUL, ELETTA, AIRCON]
    fake.lan[SOUL["dsn"]] = {"lanip": {"lanip_key": "S0ULKEY==", "lanip_key_id": 58915}}
    result = await onboard(http, EMAIL, PASSWORD, region=region)
    soul, eletta = result.machines  # the air conditioner is skipped
    assert (soul.profile_family, soul.lan_key, soul.key_id) == ("ecam61x", "S0ULKEY==", 58915)
    assert soul.device.lan_ip == "192.168.1.50"
    assert (eletta.profile_family, eletta.lan_key) == ("ecam45x", None)  # LAN disabled
    assert result.refresh_token == "refresh-1"  # rotated once
    assert "S0ULKEY" not in repr(result)


async def test_token_rotation_and_revoked_access(
    cloud: tuple[FakeCloud, Region, aiohttp.ClientSession],
) -> None:
    fake, region, http = cloud
    fake.devices = [SOUL]
    saved: list[str] = []
    api = AylaCloud(http, region, "refresh-0", on_refresh_token=saved.append)
    assert [d.dsn for d in await api.devices()] == [SOUL["dsn"]]
    fake.revoke_access_once = True  # next call gets 401 -> refresh once, retry
    assert len(await api.devices()) == 1
    assert fake.refreshes == 2 and saved == ["refresh-1", "refresh-2"]
    assert api.refresh_token == "refresh-2"


async def test_stale_refresh_token(cloud: tuple[FakeCloud, Region, aiohttp.ClientSession]) -> None:
    _, region, http = cloud
    api = AylaCloud(http, region, "refresh-stale")
    with pytest.raises(AuthError, match="rejected"):
        await api.devices()


async def test_write_property(cloud: tuple[FakeCloud, Region, aiohttp.ClientSession]) -> None:
    fake, region, http = cloud
    api = AylaCloud(http, region, "refresh-0")
    await api.write_property(SOUL["dsn"], "data_request", "DQeEDwIBVRI=")
    assert fake.datapoints == [(SOUL["dsn"], "data_request", "DQeEDwIBVRI=")]
