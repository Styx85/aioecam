"""De'Longhi account login (Gigya) and Ayla tokens.

Two ways to an Ayla refresh token:

- ``login`` walks the eight steps of the Coffee Link web login (Gigya OIDC).
  Ported from cremalink's ``authenticate_cloud``. Step 5 reads a signature out
  of an HTML consent page, so it is the most fragile part of the library.
- ``login_mobile`` does what the current Coffee Link app does: a Gigya mobile
  login, a signed ``accounts.getJWT`` and Ayla's ``token_sign_in`` with the
  app's own Ayla app id. Taken from actabi/delonghi_coffeelink (MIT).

``sign_in`` tries the mobile login first, then the web login. Failures raise
``AuthError`` naming the step. Credentials and tokens are never logged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import aiohttp

from aioecam.cloud.region import Region
from aioecam.exceptions import AuthError, EcamError

_SCOPE = "openid email profile UID comfort en alexa"
_REDIRECT = "https://google.it"
_PAGE_URL = "https://aylaopenid.delonghigroup.com/"

WRONG_CREDENTIALS = 403042  # Gigya: invalid login ID or password

LoginFlow = Literal["web", "mobile"]
# The app's own login first: fewer steps, no scraped page. The web login is
# the fallback. Measured on an ECAM610.75 account: a mobile-login token reads
# the LAN key too.
DEFAULT_FLOWS: tuple[LoginFlow, ...] = ("mobile", "web")

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float

    def __repr__(self) -> str:
        return f"Tokens(expires_at={self.expires_at:.0f})"


def _query(url: str, name: str) -> str | None:
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get(name, [None])[0]


def _gigya_code(data: dict[str, Any]) -> int | None:
    """Gigya's error code as an int; anything else is not echoed."""
    code = data.get("errorCode")
    return code if isinstance(code, int) and not isinstance(code, bool) else None


async def _json(resp: aiohttp.ClientResponse, step: str) -> dict[str, Any]:
    try:
        data = await resp.json(content_type=None)
    except (aiohttp.ContentTypeError, ValueError) as exc:
        raise AuthError(f"login step {step}: unexpected response ({resp.status})") from exc
    if not isinstance(data, dict):
        raise AuthError(f"login step {step}: unexpected response")
    return data


async def login(
    http: aiohttp.ClientSession, region: Region, email: str, password: str, language: str = "en"
) -> str:
    """Sign in with the Coffee Link account; returns an Ayla refresh token."""
    browser = {"User-Agent": region.browser_agent}
    # Field values mirror cremalink's requests-based login byte for byte
    # (requests renders booleans as "True").
    key = region.gigya_api_key
    common = {
        "APIKey": key,
        "sdk": "js_latest",
        "sdkBuild": region.gigya_sdk_build,
        "pageURL": _PAGE_URL,
        "format": "json",
    }

    # 1. authorize -> context
    async with http.get(
        f"{region.fidm_url}/oidc/op/v1.0/{key}/authorize",
        headers=browser,
        params={"client_id": region.oidc_client_id, "response_type": "code",
                "redirect_uri": _REDIRECT, "scope": _SCOPE, "nonce": str(int(time.time()))},
        allow_redirects=False,
    ) as resp:  # fmt: skip
        context = _query(resp.headers.get("Location", ""), "context")
    if not context:
        raise AuthError("login step 1 (authorize): no context")

    # 2. device ids
    async with http.get(
        f"{region.socialize_url}/socialize.getIDs",
        headers=browser,
        params={**common, "includeTicket": "True"},
    ) as resp:
        ids = await _json(resp, "2 (getIDs)")

    # 3. account login
    risk = json.dumps({"b0": 4494, "b1": [0, 2, 2, 0], "b2": 2, "b3": [], "b4": 2, "b5": 1,
                       "b6": region.browser_agent,
                       "b7": [{"name": "PDF Viewer", "filename": "internal-pdf-viewer",
                               "length": 2}],
                       "b8": time.strftime("%H:%M:%S"), "b9": 0,
                       "b10": {"state": "denied"}, "b11": False,
                       "b13": [5, "440|956|24", False, True]})  # fmt: skip
    async with http.post(
        f"{region.accounts_url}/accounts.login",
        headers=browser,
        data={**common, "loginID": email, "password": password, "sessionExpiration": "7884009",
              "targetEnv": "jssdk", "include": "profile,data,emails,subscriptions,preferences",
              "includeUserInfo": "True", "loginMode": "standard", "lang": language,
              "riskContext": urllib.parse.quote(risk), "source": "showScreenSet",
              "authMode": "cookie", "gmid": ids.get("gmid", ""), "ucid": ids.get("ucid", "")},
    ) as resp:  # fmt: skip
        account = await _json(resp, "3 (login)")
    session = account.get("sessionInfo")
    if not isinstance(session, dict) or "login_token" not in session:
        # Gigya reports e.g. errorCode 403042 for wrong credentials; never echo the payload.
        gigya = _gigya_code(account)
        raise AuthError(
            f"login rejected (Gigya error {gigya if gigya is not None else 'unknown'})",
            gigya_code=gigya,
        )
    login_token = session["login_token"]

    # 4. user info -> signed UID
    async with http.post(
        f"{region.socialize_url}/socialize.getUserInfo",
        headers=browser,
        data={**common, "enabledProviders": "*", "login_token": login_token,
              "authMode": "cookie", "gmid": ids.get("gmid", ""), "ucid": ids.get("ucid", "")},
    ) as resp:  # fmt: skip
        user = await _json(resp, "4 (getUserInfo)")
    try:
        uid, uid_sig, sig_ts = user["UID"], user["UIDSignature"], user["signatureTimestamp"]
    except KeyError:
        raise AuthError("login step 4 (getUserInfo): incomplete user info") from None

    # 5. consent page -> signature
    async with http.get(
        f"{region.consent_url}/OIDCConsentPage.php",
        headers=browser,
        params={"lang": language, "context": context, "clientID": region.oidc_client_id,
                "scope": _SCOPE.replace(" ", "+"), "UID": uid, "UIDSignature": uid_sig,
                "signatureTimestamp": sig_ts},
    ) as resp:  # fmt: skip
        page = await resp.text()
    marker = "const consentObj2Sig = '"
    if marker not in page:
        raise AuthError("login step 5 (consent): signature not found, the page layout changed")
    consent_sig = page.split(marker, 1)[1].split("';", 1)[0]

    # 6. continue authorization -> code
    consent = json.dumps({"scope": _SCOPE, "clientID": region.oidc_client_id,
                          "context": context, "UID": uid, "consent": True},
                         separators=(",", ":"))  # fmt: skip
    async with http.get(
        f"{region.fidm_url}/oidc/op/v1.0/{key}/authorize/continue",
        headers=browser,
        params={"context": context, "login_token": login_token, "consent": consent,
                "sig": consent_sig, "gmidTicket": ids.get("gmidTicket", "")},
        allow_redirects=False,
    ) as resp:  # fmt: skip
        code = _query(resp.headers.get("Location", ""), "code")
    if not code:
        raise AuthError("login step 6 (authorize/continue): no authorization code")

    # 7. code -> OIDC access token
    basic = base64.b64encode(
        f"{region.oidc_client_id}:{region.oidc_client_secret}".encode()
    ).decode()
    async with http.post(
        f"{region.fidm_url}/oidc/op/v1.0/{key}/token",
        headers={"User-Agent": region.token_agent, "Authorization": f"Basic {basic}"},
        data={"code": code, "grant_type": "authorization_code", "redirect_uri": _REDIRECT},
    ) as resp:
        idp = await _json(resp, "7 (token)")
    if "access_token" not in idp:
        raise AuthError("login step 7 (token): no access token")

    # 8. OIDC token -> Ayla refresh token
    async with http.post(
        f"{region.ayla_user_url}/api/v1/token_sign_in",
        headers={"User-Agent": region.token_agent},
        data={"app_id": region.ayla_app_id, "app_secret": region.ayla_app_secret,
              "token": idp["access_token"]},
    ) as resp:  # fmt: skip
        ayla = await _json(resp, "8 (token_sign_in)")
    refresh: Any = ayla.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        raise AuthError("login step 8 (token_sign_in): no refresh token")
    return refresh


def _gigya_signature(secret: str, url: str, params: dict[str, str]) -> str:
    """OAuth 1 style HMAC-SHA1 request signature with the Gigya session secret."""
    query = "&".join(f"{k}={urllib.parse.quote(v, safe='')}" for k, v in sorted(params.items()))
    base = f"POST&{urllib.parse.quote(url, safe='')}&{urllib.parse.quote(query, safe='')}"
    digest = hmac.new(base64.b64decode(secret), base.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


async def login_mobile(
    http: aiohttp.ClientSession, region: Region, email: str, password: str
) -> str:
    """Sign in like the Coffee Link app; returns an Ayla refresh token."""
    key = region.gigya_api_key

    # 1. account login -> session token and secret
    async with http.post(
        f"{region.accounts_url}/accounts.login",
        data={"apiKey": key, "loginID": email, "password": password, "format": "json",
              "targetEnv": "mobile"},
    ) as resp:  # fmt: skip
        account = await _json(resp, "mobile 1 (login)")
    code = _gigya_code(account)
    session = account.get("sessionInfo")
    if code != 0 or not isinstance(session, dict):
        raise AuthError(
            f"mobile login rejected (Gigya error {code if code is not None else 'unknown'})",
            gigya_code=code,
        )
    token, secret = session.get("sessionToken"), session.get("sessionSecret")
    if not isinstance(token, str) or not isinstance(secret, str):
        raise AuthError("mobile login step 1 (login): no session")

    # 2. signed request -> JWT
    url = f"{region.accounts_url}/accounts.getJWT"
    timestamp = str(int(time.time()))
    params = {"apiKey": key, "oauth_token": token, "format": "json",
              "timestamp": timestamp, "nonce": f"{timestamp}_1"}  # fmt: skip
    try:
        params["sig"] = _gigya_signature(secret, url, params)
    except ValueError:
        raise AuthError("mobile login step 2 (getJWT): unusable session secret") from None
    async with http.post(url, data=params) as resp:
        jwt = await _json(resp, "mobile 2 (getJWT)")
    id_token = jwt.get("id_token")
    if _gigya_code(jwt) != 0 or not isinstance(id_token, str):
        code = _gigya_code(jwt)
        raise AuthError(
            f"mobile login step 2 (getJWT): Gigya error {code if code is not None else 'unknown'}",
            gigya_code=code,
        )

    # 3. JWT -> Ayla refresh token
    async with http.post(
        f"{region.ayla_user_url}/api/v1/token_sign_in",
        headers={"User-Agent": region.token_agent},
        data={"app_id": region.ayla_mobile_app_id, "app_secret": region.ayla_mobile_app_secret,
              "token": id_token},
    ) as resp:  # fmt: skip
        ayla = await _json(resp, "mobile 3 (token_sign_in)")
    refresh: Any = ayla.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        raise AuthError("mobile login step 3 (token_sign_in): no refresh token")
    return refresh


async def sign_in(
    http: aiohttp.ClientSession,
    region: Region,
    email: str,
    password: str,
    *,
    flows: Sequence[LoginFlow] = DEFAULT_FLOWS,
) -> str:
    """Try the login flows in order; returns the first Ayla refresh token.

    A wrong email or password ends it at once: another flow would fail the
    same way and only count as one more failed attempt on the account. A
    connection problem moves on too, since the flows use partly different
    hosts. The last flow's error is raised.
    """
    if not flows:
        raise ValueError("no login flow given")
    error: Exception | None = None
    for flow in flows:
        try:
            if flow == "web":
                return await login(http, region, email, password)
            return await login_mobile(http, region, email, password)
        except AuthError as err:
            if err.gigya_code == WRONG_CREDENTIALS:
                raise
            _LOGGER.debug("%s login failed: %s", flow, err)
            error = err
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("%s login failed: %s", flow, type(err).__name__)
            error = err
    assert error is not None
    raise error


async def refresh_tokens(http: aiohttp.ClientSession, region: Region, refresh_token: str) -> Tokens:
    """Exchange a refresh token for a new access/refresh token pair."""
    try:
        async with http.post(
            f"{region.ayla_user_url}/users/refresh_token.json",
            headers={"User-Agent": region.token_agent},
            json={"user": {"refresh_token": refresh_token}},
        ) as resp:
            if resp.status in (401, 403, 422):
                raise AuthError(f"refresh token rejected (HTTP {resp.status})")
            if resp.status != 200:
                raise AuthError(f"token refresh failed (HTTP {resp.status})")
            data = await _json(resp, "refresh")
    except (aiohttp.ClientError, TimeoutError) as exc:
        # Not an auth problem: the service is unreachable. Keep it distinguishable.
        raise EcamError(f"token refresh failed: {exc}") from exc
    try:
        return Tokens(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=time.time() + float(data.get("expires_in", 3600)),
        )
    except KeyError:
        raise AuthError("token refresh returned no tokens") from None
