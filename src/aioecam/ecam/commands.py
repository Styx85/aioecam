"""Command frames and how they are handed to the machine.

Power control (opcode 0x84) is the same on every family, so it is built here
instead of being looked up. Beverages come from the profile's stored commands;
a parametric beverage builder follows later (plan M7).
"""

from __future__ import annotations

from dataclasses import dataclass

from aioecam.ecam.frame import REQUEST, Frame
from aioecam.ecam.profile import Profile
from aioecam.ecam.value import encode_value

OPCODE_POWER = 0x84


def power_on() -> Frame:
    return Frame(REQUEST, OPCODE_POWER, 0x0F, b"\x02\x01")


def power_off() -> Frame:
    return Frame(REQUEST, OPCODE_POWER, 0x0F, b"\x01\x01")


def refresh_app_id() -> Frame:
    return Frame(REQUEST, OPCODE_POWER, 0x0F, b"\x03\x02")


@dataclass(frozen=True, slots=True)
class PropertyWrite:
    """A string property write, ready for any transport (LAN or cloud)."""

    name: str
    value: str


def request_write(
    profile: Profile,
    frame: Frame,
    *,
    timestamp: int | None = None,
    app_id: bytes | None = None,
) -> PropertyWrite:
    """Wrap a frame as a write to the profile's ``data_request`` property."""
    return PropertyWrite(
        profile.request_property, encode_value(frame, timestamp=timestamp, app_id=app_id)
    )
