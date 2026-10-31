"""A fake Gigya + Ayla cloud on one local server, for login and API tests."""

from __future__ import annotations

import base64
import itertools
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from aioecam.cloud.region import Region, load_region

EMAIL, PASSWORD = "user@example.com", "correct horse"


class CloudProperties(dict[str, str]):
    """Property values with the time the machine last sent each one."""

    def __init__(self) -> None:
        super().__init__()
        self.updated_at: dict[str, str] = {}
        self._clock = itertools.count(1)

    def __setitem__(self, name: str, value: str) -> None:
        super().__setitem__(name, value)
        self.updated_at[name] = f"2026-10-31T08:{next(self._clock):05d}Z"

    def record(self, name: str) -> dict[str, Any]:
        return {"name": name, "base_type": "string", "value": self[name],
                "data_updated_at": self.updated_at[name]}  # fmt: skip


class CloudMachine:
    """The surface FakeMachine uses from a FakeDevice, backed by the fake cloud:
    writes arrive as cloud datapoints, pushes just update the cloud's value."""

    def __init__(self, cloud: FakeCloud, dsn: str) -> None:
        self.properties = cloud.properties.setdefault(dsn, CloudProperties())
        self.on_write: Callable[[str, str], Awaitable[None]] | None = None
        self.online = True  # False: the cloud accepts writes but never relays them
        cloud.on_datapoint = self._relay

    async def push(self, name: str, value: str) -> int:
        self.properties[name] = value
        return 200

    async def _relay(self, dsn: str, name: str, value: str) -> None:
        if self.online and self.on_write:
            await self.on_write(name, value)


class FakeCloud:
    def __init__(self) -> None:
        self.region: Region = load_region("eu")
        self.devices: list[dict[str, Any]] = []
        self.lan: dict[str, dict[str, Any]] = {}  # dsn -> lan.json payload
        self.lan_status: dict[str, int] = {}  # dsn -> forced lan.json error status
        self.datapoints: list[tuple[str, str, str]] = []
        self.properties: dict[str, CloudProperties] = {}  # dsn -> what the machine sent
        self.property_reads = 0
        self.connection_status: dict[str, str] = {}  # dsn -> e.g. "Offline"
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
        r.add_get("/apiv1/dsns/{dsn}.json", self._device)
        r.add_get("/apiv1/dsns/{dsn}/properties.json", self._read_properties)
        r.add_get("/apiv1/dsns/{dsn}/properties/{name}.json", self._read_property)
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
        if status := self.lan_status.get(req.match_info["dsn"]):
            return web.Response(status=status)
        payload = self.lan.get(req.match_info["dsn"])
        return web.json_response(payload) if payload else web.Response(status=404)

    async def _not_found(self, req: web.Request) -> web.Response:
        return web.Response(status=404)

    async def _read_properties(self, req: web.Request) -> web.Response:
        if not self._authorized(req):
            return web.Response(status=401)
        self.property_reads += 1
        props = self.properties.get(req.match_info["dsn"], CloudProperties())
        names = req.query.getall("names[]", []) or list(props)
        return web.json_response([{"property": props.record(n)} for n in names if n in props])

    async def _device(self, req: web.Request) -> web.Response:
        if not self._authorized(req):
            return web.Response(status=401)
        status = self.connection_status.get(req.match_info["dsn"], "Online")
        return web.json_response({"device": {"connection_status": status}})

    async def _read_property(self, req: web.Request) -> web.Response:
        if not self._authorized(req):
            return web.Response(status=401)
        self.property_reads += 1
        props = self.properties.get(req.match_info["dsn"], CloudProperties())
        name = req.match_info["name"]
        if name not in props:
            return web.Response(status=404)
        return web.json_response({"property": props.record(name)})

    async def _datapoint(self, req: web.Request) -> web.Response:
        if not self._authorized(req):
            return web.Response(status=401)
        dsn, name = req.match_info["dsn"], req.match_info["name"]
        value = (await req.json())["datapoint"]["value"]
        self.datapoints.append((dsn, name, value))
        if self.on_datapoint:
            await self.on_datapoint(dsn, name, value)
        return web.json_response({"datapoint": {"value": value}}, status=201)
