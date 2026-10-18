"""A fake Ayla LAN-mode module, behaving like the measured ECAM610.75.

- ``POST local_reg``: always starts a new key exchange.
- ``PUT local_reg`` with a live session: no re-key, only polls commands when
  ``notify`` is set. Without a session it behaves like POST.
- After each non-empty command it polls again until the queue is empty.

It runs its HTTP server and client on its own loopback address so the
callback server can tell machines apart by peer address, as on a real LAN.
"""

from __future__ import annotations

import asyncio
import base64
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
from aiohttp import web

from aioecam.lan.crypto import LanSession


class FakeDevice:
    def __init__(
        self,
        *,
        ip: str = "127.0.0.2",
        lan_key: str,
        key_id: int = 4242,
        properties: dict[str, str] | None = None,
        echo_writes: bool = False,
    ) -> None:
        self.ip = ip
        self.lan_key = lan_key
        self.key_id = key_id
        self.properties = dict(properties or {})
        self.echo_writes = echo_writes
        self.writes: list[tuple[str, str]] = []
        self.registrations: list[str] = []  # "POST" / "PUT"
        self.key_exchanges = 0
        self.responsive = True  # False: accepts local_reg but never calls back
        # False: module restarted without cloud; no DSN in regtoken.json, local_reg -> 404
        self.lan_enabled = True
        # Hook for a simulated machine behind the module: called for every property write.
        self.on_write: Callable[[str, str], Awaitable[None]] | None = None
        self.dsn = "AC000W000000001"
        self.port = 0
        self._session: LanSession | None = None
        self._target: str | None = None
        self._runner: web.AppRunner | None = None
        self._http: aiohttp.ClientSession | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        app = web.Application()
        app.router.add_route("*", "/local_reg.json", self._local_reg)
        app.router.add_get("/regtoken.json", self._regtoken)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.ip, 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        connector = aiohttp.TCPConnector(local_addr=(self.ip, 0))
        self._http = aiohttp.ClientSession(connector=connector)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._http:
            await self._http.close()
        if self._runner:
            await self._runner.cleanup()

    # --- behaviour controls used by tests ------------------------------------

    def drop_session(self) -> None:
        """Forget the session, e.g. after a power cut of the Wi-Fi module."""
        self._session = None

    def restart(self) -> None:
        """Module reboot: it re-keys on its own with the last registered app."""
        self._spawn(self._key_exchange_and_poll())

    async def push(self, name: str, value: str) -> int:
        """Send an unsolicited datapoint, as the machine does on state changes."""
        return await self._post_datapoint(name, value)

    # --- protocol ---------------------------------------------------------------

    async def _regtoken(self, request: web.Request) -> web.Response:
        # Real module answers like this (text/html content type on some firmware).
        if self.lan_enabled:
            data = {"regtoken": "f3af0e", "registered": 1, "registration_type": "AP-Mode",
                    "host_symname": self.dsn}  # fmt: skip
        else:
            data = {"regtoken": None, "registered": 1, "registration_type": "",
                    "host_symname": ""}  # fmt: skip
        return web.json_response(data)

    async def _local_reg(self, request: web.Request) -> web.Response:
        body = await request.json()
        reg = body["local_reg"]
        self.registrations.append(request.method)
        if not self.lan_enabled:
            return web.Response(status=404)
        self._target = f"http://{reg['ip']}:{reg['port']}{reg['uri']}"
        if not self.responsive:
            return web.Response(status=202)
        if request.method == "POST" or self._session is None:
            self._spawn(self._key_exchange_and_poll())
        elif reg.get("notify"):
            self._spawn(self._poll_until_empty())
        return web.Response(status=202)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _key_exchange_and_poll(self) -> None:
        assert self._http and self._target
        random_1 = base64.b64encode(os.urandom(12)).decode()
        time_1 = str(int(time.monotonic() * 1000))
        kx = {"ver": 1, "proto": 1, "key_id": self.key_id, "random_1": random_1, "time_1": time_1}
        async with self._http.post(
            f"{self._target}/key_exchange.json", json={"key_exchange": kx}
        ) as r:
            if r.status != 202:
                return
            reply = await r.json()
        self._session = LanSession(
            self.lan_key,
            random_1=random_1,
            random_2=reply["random_2"],
            time_1=time_1,
            time_2=str(reply["time_2"]),
            role="device",
        )
        self.key_exchanges += 1
        await self._poll_until_empty()

    async def _poll_until_empty(self) -> None:
        async with self._lock:  # the real module polls one request at a time
            while self._session is not None:
                assert self._http and self._target
                async with self._http.get(f"{self._target}/commands.json") as r:
                    if r.status != 200:
                        return
                    sealed = await r.json()
                data = self._session.open(sealed)["data"]
                if not data:
                    return
                await self._apply(data)

    async def _apply(self, data: dict[str, Any]) -> None:
        for entry in data.get("properties", []):
            prop = entry["property"]
            value = prop["value"].rstrip("\n")
            self.properties[prop["name"]] = value
            self.writes.append((prop["name"], value))
            if self.echo_writes:
                await self._post_datapoint(prop["name"], value)
            if self.on_write:
                self._spawn(self.on_write(prop["name"], value))
        for entry in data.get("cmds", []):
            name = entry["cmd"]["resource"].split("name=", 1)[1]
            if name in self.properties:
                await self._post_datapoint(name, self.properties[name])

    async def _post_datapoint(self, name: str, value: str) -> int:
        assert self._http and self._target and self._session
        sealed = self._session.seal({"name": name, "value": value, "base_type": "string"})
        async with self._http.post(f"{self._target}/property/datapoint.json", json=sealed) as r:
            return r.status
