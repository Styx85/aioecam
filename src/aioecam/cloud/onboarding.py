"""One call from account credentials to everything a LAN link needs."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp

from aioecam.cloud.api import AylaCloud, CloudDevice
from aioecam.cloud.auth import login
from aioecam.cloud.region import Region, load_region
from aioecam.ecam.profile import profile_for_oem_model

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class OnboardedMachine:
    device: CloudDevice
    profile_family: str | None  # None: unknown model, ask the user
    lan_key: str | None  # None: the cloud has no LAN key, cloud-only machine
    key_id: int | None

    def __repr__(self) -> str:
        return (
            f"OnboardedMachine(dsn={self.device.dsn!r}, family={self.profile_family!r}, "
            f"lan_key={'set' if self.lan_key else None})"
        )


@dataclass(frozen=True)
class Onboarding:
    refresh_token: str
    machines: list[OnboardedMachine]

    def __repr__(self) -> str:
        return f"Onboarding(machines={self.machines!r})"


async def onboard(
    http: aiohttp.ClientSession,
    email: str,
    password: str,
    *,
    region: Region | None = None,
) -> Onboarding:
    """Log in, list the coffee machines and fetch their LAN keys and profiles."""
    reg = region or load_region("eu")
    refresh_token = await login(http, reg, email, password)
    cloud = AylaCloud(http, reg, refresh_token)
    machines = []
    for device in await cloud.devices():
        if not device.is_coffee_machine:
            continue
        try:
            family: str | None = profile_for_oem_model(device.oem_model or "").family
        except LookupError:
            family = None
        lan = await cloud.lan_config(device.dsn) if device.lan_enabled else None
        machines.append(
            OnboardedMachine(
                device=device,
                profile_family=family,
                lan_key=lan.lan_key if lan else None,
                key_id=lan.key_id if lan else None,
            )
        )
    return Onboarding(refresh_token=cloud.refresh_token, machines=machines)
