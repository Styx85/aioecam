"""The HTTP callback server machines connect to in LAN mode.

One server serves every machine; requests are routed by the peer address.
Requests from addresses that were not added are refused.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
from aiohttp import web

from aioecam.exceptions import NotConnected
from aioecam.lan.crypto import SessionError
from aioecam.lan.link import LanLink
from aioecam.lan.net import source_ip_towards

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 10280

Handler = Callable[[LanLink, dict[str, Any]], Awaitable[web.Response]]


class LanServer:
    def __init__(
        self,
        http: aiohttp.ClientSession | None = None,
        *,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        advertise_ip: str | None = None,
    ) -> None:
        self._http = http
        self._own_http = http is None
        self._host = host
        self._port = port
        self._advertise_ip = advertise_ip
        self._links: dict[str, LanLink] = {}
        self._runner: web.AppRunner | None = None

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._own_http:
            self._http = aiohttp.ClientSession()
        app = web.Application(client_max_size=64 * 1024)
        app.router.add_post("/local_lan/key_exchange.json", self._wrap(self._key_exchange))
        app.router.add_route("*", "/local_lan/commands.json", self._wrap(self._commands))
        app.router.add_post("/local_lan/property/datapoint.json", self._wrap(self._datapoint))
        app.router.add_post("/local_lan/property/datapoint/ack.json", self._wrap(self._datapoint))
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        if self._port == 0:  # tests: pick the port the OS assigned
            server = site._server
            sockets = getattr(server, "sockets", None) or []
            self._port = sockets[0].getsockname()[1]
        _LOGGER.debug("LAN callback server listening on %s:%s", self._host, self._port)

    async def close(self) -> None:
        for link in list(self._links.values()):
            await link.close()
        self._links.clear()
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        if self._own_http and self._http:
            await self._http.close()

    async def __aenter__(self) -> LanServer:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def port(self) -> int:
        return self._port

    # --- links ---------------------------------------------------------------

    def add_link(self, *, device_ip: str, lan_key: str, dsn: str, **options: Any) -> LanLink:
        """Create and start the link for one machine."""
        if self._http is None:
            raise RuntimeError("server not started")
        if device_ip in self._links:
            raise ValueError(f"{device_ip} is already linked")
        advertise_ip = self._advertise_ip or source_ip_towards(device_ip)
        link = LanLink(
            device_ip=device_ip,
            lan_key=lan_key,
            dsn=dsn,
            http=self._http,
            advertise=lambda: (advertise_ip, self._port),
            **options,
        )
        self._links[device_ip] = link
        link.start()
        return link

    async def remove_link(self, device_ip: str) -> None:
        link = self._links.pop(device_ip, None)
        if link:
            await link.close()

    # --- request handling ----------------------------------------------------

    def _wrap(self, handler: Handler) -> Callable[[web.Request], Awaitable[web.Response]]:
        async def route(request: web.Request) -> web.Response:
            link = self._links.get(request.remote or "")
            if link is None:
                _LOGGER.debug("refusing %s from unknown peer %s", request.path, request.remote)
                return web.Response(status=403)
            body: dict[str, Any] = {}
            raw = await request.read()
            if raw:
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    return web.Response(status=400)
                if not isinstance(parsed, dict):
                    return web.Response(status=400)
                body = parsed
            try:
                return await handler(link, body)
            except NotConnected:
                return web.Response(status=404)
            except SessionError as exc:
                _LOGGER.debug("%s: rejected %s: %s", link.device_ip, request.path, exc)
                return web.Response(status=400)

        return route

    @staticmethod
    async def _key_exchange(link: LanLink, body: dict[str, Any]) -> web.Response:
        return web.json_response(link.handle_key_exchange(body), status=202)

    @staticmethod
    async def _commands(link: LanLink, body: dict[str, Any]) -> web.Response:
        return web.json_response(link.handle_commands_poll())

    @staticmethod
    async def _datapoint(link: LanLink, body: dict[str, Any]) -> web.Response:
        link.handle_datapoint(body)
        return web.json_response({})
