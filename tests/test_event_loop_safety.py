"""Things Home Assistant flags as bugs: file I/O in the event loop, unretrieved exceptions."""

from __future__ import annotations

import asyncio
import gc
from importlib import resources
from typing import Any

import pytest

from aioecam.cloud.region import load_region
from aioecam.ecam.profile import available_families, load_profile, profile_for_oem_model
from aioecam.lan.link import _fail


def test_no_file_access_after_import(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("file access after import")

    monkeypatch.setattr(resources, "files", forbidden)
    assert available_families() == ["ecam45x", "ecam61x"]
    assert load_profile("ecam61x").family == "ecam61x"
    assert profile_for_oem_model("DL-millcore").family == "ecam61x"  # used by onboarding
    assert load_region("eu").name == "eu"


async def test_failed_future_nobody_awaits_is_not_reported() -> None:
    reported: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda _loop, ctx: reported.append(ctx))
    fut: asyncio.Future[None] = loop.create_future()
    _fail(fut, RuntimeError("link closed"))
    del fut
    gc.collect()
    await asyncio.sleep(0)
    assert reported == []
