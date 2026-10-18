"""Ayla property values that carry ECAM frames.

The machine exchanges frames through string properties (``data_request``,
``d302_monitor``, ...). Each value is base64 of::

    frame | unix timestamp (4 bytes, big endian) | [app id (4 bytes), Eletta models]
"""

from __future__ import annotations

import base64
import binascii
import time
from dataclasses import dataclass

from aioecam.ecam.frame import Frame
from aioecam.exceptions import FrameError


@dataclass(frozen=True, slots=True)
class FrameValue:
    frame: Frame
    timestamp: int | None
    app_id: bytes | None


def encode_value(frame: Frame, *, timestamp: int | None = None, app_id: bytes | None = None) -> str:
    """Wrap a frame as a property value. ``timestamp`` defaults to now."""
    ts = int(time.time()) if timestamp is None else timestamp
    raw = frame.encode() + ts.to_bytes(4, "big") + (app_id or b"")
    return base64.b64encode(raw).decode("ascii")


def decode_value(value: str) -> FrameValue:
    """Parse a property value. Missing base64 padding is tolerated."""
    text = value.strip()
    try:
        raw = base64.b64decode(text + "=" * (-len(text) % 4), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise FrameError(f"not base64: {exc}") from exc
    frame, rest = Frame.decode(raw)
    timestamp = int.from_bytes(rest[:4], "big") if len(rest) >= 4 else None
    app_id = rest[4:8] if len(rest) >= 8 else None
    return FrameValue(frame=frame, timestamp=timestamp, app_id=app_id)
