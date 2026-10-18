"""Cloud endpoints and app constants per region."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from functools import cache
from importlib import resources


@dataclass(frozen=True)
class Region:
    name: str
    gigya_api_key: str
    oidc_client_id: str
    oidc_client_secret: str
    gigya_sdk_build: str
    ayla_app_id: str
    ayla_app_secret: str
    fidm_url: str
    socialize_url: str
    accounts_url: str
    consent_url: str
    ayla_user_url: str
    ayla_ads_url: str
    browser_agent: str
    token_agent: str
    api_agent: str

    def __repr__(self) -> str:  # keep secrets out of logs and tracebacks
        return f"Region({self.name!r})"

    def with_base_url(self, base: str) -> Region:
        """Point every host at one base URL (tests)."""
        return replace(
            self,
            fidm_url=base,
            socialize_url=base,
            accounts_url=base,
            consent_url=base,
            ayla_user_url=base,
            ayla_ads_url=base,
        )


@cache
def load_region(name: str = "eu") -> Region:
    raw = json.loads(resources.files("aioecam.cloud").joinpath("constants.json").read_text("utf-8"))
    try:
        values = raw["regions"][name]
    except KeyError:
        raise ValueError(f"unsupported region {name!r}") from None
    agents = raw["user_agents"]
    return Region(
        name=name,
        browser_agent=agents["browser"],
        token_agent=agents["token"],
        api_agent=agents["api"],
        **values,
    )
