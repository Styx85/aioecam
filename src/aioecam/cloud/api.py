"""Ayla cloud API: devices, LAN configuration, property reads and writes.

Used for onboarding (find machines, fetch the LAN key) and as a fallback
transport. Tokens refresh automatically; callers persist the rotating refresh
token through ``on_refresh_token``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import aiohttp

from aioecam.cloud.auth import Tokens, refresh_tokens
from aioecam.cloud.region import Region
from aioecam.exceptions import AuthError, EcamError

_LOGGER = logging.getLogger(__name__)

# Ayla oem_model prefixes of De'Longhi appliances that are not coffee machines.
NON_COFFEE_OEM_PREFIXES = ("DL-pac",)


class CloudError(EcamError):
    """The cloud answered with an unexpected error."""


@dataclass(frozen=True)
class CloudDevice:
    dsn: str
    name: str | None
    oem_model: str | None
    lan_enabled: bool
    lan_ip: str | None
    connection_status: str | None

    @property
    def is_coffee_machine(self) -> bool:
        return not (self.oem_model or "").startswith(NON_COFFEE_OEM_PREFIXES)


@dataclass(frozen=True)
class LanConfig:
    lan_key: str
    key_id: int | None

    def __repr__(self) -> str:
        return f"LanConfig(key_id={self.key_id})"


class AylaCloud:
    def __init__(
        self,
        http: aiohttp.ClientSession,
        region: Region,
        refresh_token: str,
        *,
        on_refresh_token: Callable[[str], None] | None = None,
    ) -> None:
        self._http = http
        self._region = region
        self._refresh_token = refresh_token
        self._on_refresh_token = on_refresh_token
        self._tokens: Tokens | None = None
        self._lock = asyncio.Lock()

    @property
    def refresh_token(self) -> str:
        return self._refresh_token

    async def _access_token(self, *, force: bool = False) -> str:
        async with self._lock:
            if force or self._tokens is None or self._tokens.expires_at - time.time() < 60:
                self._tokens = await refresh_tokens(self._http, self._region, self._refresh_token)
                if self._tokens.refresh_token != self._refresh_token:
                    self._refresh_token = self._tokens.refresh_token
                    if self._on_refresh_token:
                        self._on_refresh_token(self._refresh_token)
            return self._tokens.access_token

    async def _request(
        self, method: str, path: str, *, json: Any = None, allow_404: bool = False
    ) -> Any:
        url = f"{self._region.ayla_ads_url}/apiv1{path}"
        for attempt in (1, 2):
            headers = {
                "User-Agent": self._region.api_agent,
                "Authorization": f"auth_token {await self._access_token(force=attempt == 2)}",
                "Accept": "application/json",
            }
            try:
                async with self._http.request(
                    method, url, headers=headers, json=json, timeout=aiohttp.ClientTimeout(total=20)
                ) as resp:
                    if resp.status == 401 and attempt == 1:
                        continue  # access token revoked early: refresh once
                    if resp.status == 401:
                        raise AuthError("cloud rejected a fresh access token")
                    if resp.status == 404 and allow_404:
                        return None
                    if resp.status >= 400:
                        raise CloudError(f"{method} {path} -> HTTP {resp.status}")
                    if resp.status == 204:
                        return None
                    return await resp.json(content_type=None)
            except (aiohttp.ClientError, TimeoutError) as exc:
                raise CloudError(f"{method} {path} failed: {exc}") from exc
        raise AssertionError("unreachable")

    # --- devices -------------------------------------------------------------

    async def devices(self) -> list[CloudDevice]:
        raw = await self._request("GET", "/devices.json") or []
        result = []
        for entry in raw:
            d = entry.get("device", entry)
            result.append(
                CloudDevice(
                    dsn=d["dsn"],
                    name=d.get("product_name"),
                    oem_model=d.get("oem_model") or d.get("model"),
                    lan_enabled=bool(d.get("lan_enabled")),
                    lan_ip=d.get("lan_ip"),
                    connection_status=d.get("connection_status"),
                )
            )
        return result

    async def lan_config(self, dsn: str) -> LanConfig | None:
        """LAN key of a machine, or None if the cloud has none for it."""
        lan = await self._request("GET", f"/devices/{dsn}/lan.json", allow_404=True)
        lanip = (lan or {}).get("lanip") or {}
        if lanip.get("lanip_key"):
            key_id = lanip.get("lanip_key_id")
            return LanConfig(lanip["lanip_key"], int(key_id) if key_id is not None else None)
        # Some accounts/firmware expose it here instead.
        alt = await self._request("GET", f"/devices/{dsn}/connection_config.json", allow_404=True)
        if alt and alt.get("local_key"):
            key_id = alt.get("local_key_id")
            return LanConfig(alt["local_key"], int(key_id) if key_id is not None else None)
        return None

    # --- properties --------------------------------------------------------------

    async def read_property(self, dsn: str, name: str) -> Any:
        data = await self._request("GET", f"/dsns/{dsn}/properties/{name}.json", allow_404=True)
        return (data or {}).get("property", {}).get("value")

    async def read_properties(self, dsn: str, names: list[str]) -> dict[str, dict[str, Any]]:
        """Several properties in one request, keyed by name (missing ones left out).

        Each entry is the cloud's property record: ``value``, ``base_type`` and
        ``data_updated_at`` (when the machine last sent it).
        """
        query = "&".join(f"names[]={name}" for name in names)
        data = await self._request("GET", f"/dsns/{dsn}/properties.json?{query}") or []
        result = {}
        for entry in data:
            prop = entry.get("property", entry)
            if isinstance(prop.get("name"), str):
                result[prop["name"]] = prop
        return result

    async def write_property(self, dsn: str, name: str, value: str) -> None:
        """Create a datapoint; the cloud forwards it to the machine when it is online."""
        await self._request(
            "POST",
            f"/dsns/{dsn}/properties/{name}/datapoints.json",
            json={"datapoint": {"value": value}},
        )
