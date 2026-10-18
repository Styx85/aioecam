"""Unauthenticated module probe via ``regtoken.json``."""

from __future__ import annotations

from dataclasses import dataclass

import aiohttp


@dataclass(frozen=True, slots=True)
class ModuleInfo:
    dsn: str | None
    registered: bool
    registration_type: str | None

    @property
    def lan_ready(self) -> bool:
        """False after a module restart without cloud: it has no DSN and refuses LAN mode."""
        return bool(self.dsn)


async def probe_module(
    http: aiohttp.ClientSession, device_ip: str, *, port: int = 80, timeout: float = 5.0
) -> ModuleInfo:
    """Ask the Ayla module who it is. Raises ``aiohttp.ClientError`` if unreachable."""
    async with http.get(
        f"http://{device_ip}:{port}/regtoken.json",
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)
    return ModuleInfo(
        dsn=data.get("host_symname") or None,
        registered=bool(data.get("registered")),
        registration_type=data.get("registration_type") or None,
    )
