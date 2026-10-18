"""A fake Gigya + Ayla cloud on one local server, for login and API tests."""

from __future__ import annotations

import base64
import itertools
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from aioecam.cloud.region import Region, load_region

EMAIL, PASSWORD = "user@example.com", "correct horse"


class FakeCloud:
    def __init__(self) -> None:
        self.region: Region = load_region("eu")
        self.devices: list[dict[str, Any]] = []
        self.lan: dict[str, dict[str, Any]] = {}  # dsn -> lan.json payload
        self.datapoints: list[tuple[str, str, str]] = []
        self.on_datapoint: Callable[[str, str, str], Awaitable[None]] | None = None
        self.refreshes = 0
        self.revoke_access_once = False
        self._tokens = itertools.count(1)
        self._refresh = "refresh-0"
        self._access = ""
        self._runner: web.AppRunner | None = None

    async def start(self) -> Region:
        key = self.region.gigya_api_key
        app = web.Application()
        r = app.router
        r.add_get(f"/oidc/op/v1.0/{key}/authorize", self._authorize)
        r.add_get(f"/oidc/op/v1.0/{key}/authorize/continue", self._continue)
        r.add_post(f"/oidc/op/v1.0/{key}/token", self._token)
        r.add_get("/socialize.getIDs", self._ids)
        r.add_post("/accounts.login", self._login)
        r.add_post("/socialize.getUserInfo", self._userinfo)
        r.add_get("/OIDCConsentPage.php", self._consent)
        r.add_post("/api/v1/token_sign_in", self._sign_in)
        r.add_post("/users/refresh_token.json", self._refresh_token)
        r.add_get("/apiv1/devices.json", self._devices)
        r.add_get("/apiv1/devices/{dsn}/lan.json", self._lan)
        r.add_get("/apiv1/devices/{dsn}/connection_config.json", self._not_found)
        r.add_post("/apiv1/dsns/{dsn}/properties/{name}/datapoints.json", self._datapoint)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        return self.region.with_base_url(f"http://127.0.0.1:{port}")

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    # --- Gigya / OIDC ----------------------------------------------------------

    async def _authorize(self, req: web.Request) -> web.Response:
        assert req.query["client_id"] == self.region.oidc_client_id
        raise web.HTTPFound("https://example.invalid/login?context=ctx-1")

    async def _ids(self, req: web.Request) -> web.Response:
        assert req.query["APIKey"] == self.region.gigya_api_key
        return web.json_response({"ucid": "u1", "gmid": "g1", "gmidTicket": "t1"})

    async def _login(self, req: web.Request) -> web.Response:
        form = await req.post()
        if form["loginID"] != EMAIL or form["password"] != PASSWORD:
            return web.json_response({"errorCode": 403042, "errorMessage": "Invalid LoginID"})
        assert form["gmid"] == "g1"
        return web.json_response({"sessionInfo": {"login_token": "lt-1"}})

    async def _userinfo(self, req: web.Request) -> web.Response:
        assert (await req.post())["login_token"] == "lt-1"
        return web.json_response({"UID": "uid", "UIDSignature": "s", "signatureTimestamp": "1"})

    async def _consent(self, req: web.Request) -> web.Response:
        assert req.query["context"] == "ctx-1" and req.query["UID"] == "uid"
        html = "<script>const consentObj2Sig = 'consent-sig';</script>"
        return web.Response(text=html, content_type="text/html")

    async def _continue(self, req: web.Request) -> web.Response:
        assert req.query["sig"] == "consent-sig" and req.query["gmidTicket"] == "t1"
        raise web.HTTPFound("https://google.it/?code=code-1")

    async def _token(self, req: web.Request) -> web.Response:
        expected = base64.b64encode(
            f"{self.region.oidc_client_id}:{self.region.oidc_client_secret}".encode()
        ).decode()
        assert req.headers["Authorization"] == f"Basic {expected}"
        assert (await req.post())["code"] == "code-1"
        return web.json_response({"access_token": "idp-1"})

    # --- Ayla user service ---------------------------------------------------------

    async def _sign_in(self, req: web.Request) -> web.Response:
        form = await req.post()
        assert form["app_id"] == self.region.ayla_app_id and form["token"] == "idp-1"
        return web.json_response({"refresh_token": self._refresh})

    async def _refresh_token(self, req: web.Request) -> web.Response:
        body = await req.json()
        if body["user"]["refresh_token"] != self._refresh:
            return web.json_response({"error": "invalid"}, status=401)
        self.refreshes += 1
        n = next(self._tokens)
        self._refresh, self._access = f"refresh-{n}", f"access-{n}"
        return web.json_response(
            {"access_token": self._access, "refresh_token": self._refresh, "expires_in": 86400}
        )

    # --- Ayla device service ---------------------------------------------------------

    def _authorized(self, req: web.Request) -> bool:
        if self.revoke_access_once:
            self.revoke_access_once = False
            return False
        return req.headers.get("Authorization") == f"auth_token {self._access}"

    async def _devices(self, req: web.Request) -> web.Response:
        if not self._authorized(req):
            return web.Response(status=401)
        return web.json_response([{"device": d} for d in self.devices])

    async def _lan(self, req: web.Request) -> web.Response:
        if not self._authorized(req):
            return web.Response(status=401)
        payload = self.lan.get(req.match_info["dsn"])
        return web.json_response(payload) if payload else web.Response(status=404)

    async def _not_found(self, req: web.Request) -> web.Response:
        return web.Response(status=404)

    async def _datapoint(self, req: web.Request) -> web.Response:
        if not self._authorized(req):
            return web.Response(status=401)
        dsn, name = req.match_info["dsn"], req.match_info["name"]
        value = (await req.json())["datapoint"]["value"]
        self.datapoints.append((dsn, name, value))
        if self.on_datapoint:
            await self.on_datapoint(dsn, name, value)
        return web.json_response({"datapoint": {"value": value}}, status=201)
