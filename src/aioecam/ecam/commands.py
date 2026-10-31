"""Command frames and how they are handed to the machine.

Power control (opcode 0x84) is the same on every family, so it is built here
instead of being looked up. Beverages come from the profile's stored commands
or are built from the machine's own recipe (``brew``).

The request families beyond power, statistics and stored beverages are ported
from lodzen's ECAM layer (https://github.com/lodzen/cremalink, AGPL-3.0,
``cremalink/ecam/builder.py``), which reverse-engineered them from the Coffee
Link app and live captures. Each builder notes that origin; anything marked
"APK" was not yet seen on the wire by us.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from aioecam.ecam.frame import REQUEST, Frame
from aioecam.ecam.profile import Profile
from aioecam.ecam.value import encode_value

OPCODE_POWER = 0x84
OPCODE_MONITOR = 0x75
OPCODE_BREW = 0x83
OPCODE_WRITE_PARAM = 0x90
OPCODE_READ_PARAM = 0x95
OPCODE_READ_EXTENDED = 0xA1
OPCODE_PROFILE_NAMES = 0xA4
OPCODE_RECIPE = 0xA6
OPCODE_PROFILE_SELECT = 0xA9
OPCODE_STRIKER_HANDSHAKE = 0xE8
OPCODE_SYNC_TIMEOUT = 0xE1  # answer only: the machine aborted the request (busy)

# Wide (16-bit) recipe TLV tags; every other tag carries one byte.
WIDE_TAGS = frozenset({0x01, 0x09, 0x0F})
# The app's filter for which recipe parameters go into a 0x83 brew frame.
_BREW_DROP_TAGS = frozenset({0x08})
_BREW_KEEP_EXTENDED = frozenset({27, 28, 31, 33, 38, 39})


class BrewAction(IntEnum):
    START = 0x01
    STOP = 0x02


class BrewOperation(IntEnum):
    """Low two bits of the ``0x83`` trailer (Coffee Link 4.9.6 enum).

    The app builds the trailer as ``(profile << 2) | operation`` and uses
    PREPARE for a normal brew. SAVE and DELETE change the stored recipe; the
    app's "inversion" variants (5-7) do not fit two bits and are not modelled.
    """

    DELETE = 0
    SAVE = 1
    PREPARE = 2
    PREPARE_AND_SAVE = 3


def power_on() -> Frame:
    return Frame(REQUEST, OPCODE_POWER, 0x0F, b"\x02\x01")


def power_off() -> Frame:
    return Frame(REQUEST, OPCODE_POWER, 0x0F, b"\x01\x01")


def refresh_app_id() -> Frame:
    return Frame(REQUEST, OPCODE_POWER, 0x0F, b"\x03\x02")


# Appended after the timestamp to requests for Eletta ("striker") machines,
# as the Coffee Link app does. From sk7n4k3d/delonghi-ha (MIT), which drives
# Eletta Explore machines through the cloud with it.
APP_SIGNATURE = bytes([0x20, 0x40, 0x35, 0xEF])


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


# --- requests ported from lodzen's ECAM layer ---------------------------------


def monitor_request() -> Frame:
    """``0D 05 75 0F``: ask the machine for a fresh monitor frame (APK ``V``)."""
    return Frame(REQUEST, OPCODE_MONITOR, 0x0F, b"")


def profile_select(slot: int) -> Frame:
    """``0D 06 A9 F0 <slot>``: switch the active user profile (verified live by
    lodzen). A gated write: announce the session first."""
    if not 1 <= slot <= 0xFF:
        raise ValueError(f"profile slot out of range: {slot}")
    return Frame(REQUEST, OPCODE_PROFILE_SELECT, 0xF0, bytes([slot]))


def read_profile_names(first: int, count: int) -> Frame:
    """``0D 07 A4 F0 <first> <count>``: read profile names (APK ``t0``)."""
    if not 0 <= first <= 0xFF or not 1 <= count <= 0xFF:
        raise ValueError("profile name range out of bounds")
    return Frame(REQUEST, OPCODE_PROFILE_NAMES, 0xF0, bytes([first, count]))


def read_param(param_id: int) -> Frame:
    """``0D 07 95 F0 <param:2>``: read one machine parameter (captured)."""
    if not 0 <= param_id <= 0xFFFF:
        raise ValueError(f"param id out of range: {param_id}")
    return Frame(REQUEST, OPCODE_READ_PARAM, 0xF0, param_id.to_bytes(2, "big"))


def write_param(param_id: int, value: int, *, flag: int | None = None) -> Frame:
    """``0D 0B 90 <flag> <param:2> <value:4>``: write a machine parameter.

    Flag follows the app's rule: ``0xF0`` for param >= 1000, else ``0x0F``.
    A gated write: announce the session first, or it only stages transiently.
    """
    if not 0 <= param_id <= 0xFFFF:
        raise ValueError(f"param id out of range: {param_id}")
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f"value out of range: {value}")
    if flag is None:
        flag = 0xF0 if param_id >= 1000 else 0x0F
    return Frame(
        REQUEST, OPCODE_WRITE_PARAM, flag, param_id.to_bytes(2, "big") + value.to_bytes(4, "big")
    )


def read_recipe(slot: int, beverage_id: int) -> Frame:
    """``0D 07 A6 F0 <slot> <beverage>``: a profile's recipe for one beverage (APK ``M0``)."""
    if not 1 <= slot <= 0xFF or not 0 <= beverage_id <= 0xFF:
        raise ValueError("recipe read arguments out of range")
    return Frame(REQUEST, OPCODE_RECIPE, 0xF0, bytes([slot, beverage_id]))


def striker_handshake() -> Frame:
    """``0D 06 E8 F0 00``: Eletta ("striker") session handshake (APK ``z0``; untested here)."""
    return Frame(REQUEST, OPCODE_STRIKER_HANDSHAKE, 0xF0, b"\x00")


def brew_param_included(tag: int) -> bool:
    """The app's rule for which recipe parameters go on the wire (APK ``E0``)."""
    if tag in _BREW_DROP_TAGS:
        return False
    return tag < 23 or tag in _BREW_KEEP_EXTENDED


def encode_tlv(params: dict[int, int]) -> bytes:
    """Recipe parameters -> TLV bytes, keeping the given order.

    The order is preserved on purpose: the machine's own frames are not sorted
    (espresso on ECAM610: 01, 1B, 02, 04), and a recipe read from the machine
    should go back out exactly as it came in.
    """
    out = bytearray()
    for tag in params:
        width = 2 if tag in WIDE_TAGS else 1
        value = params[tag]
        if not 0 <= value < 1 << (8 * width):
            raise ValueError(f"value {value} does not fit tag 0x{tag:02x}")
        out += bytes([tag]) + value.to_bytes(width, "big")
    return bytes(out)


def brew(
    beverage_id: int,
    recipe: dict[int, int],
    *,
    slot: int,
    operation: BrewOperation = BrewOperation.PREPARE,
    action: BrewAction = BrewAction.START,
) -> Frame:
    """``0D LEN 83 F0 <bev> <action> <TLV...> <(slot << 2) | operation>``.

    ``recipe`` must be the same profile's values (recipes are profile-scoped);
    parameters the app never sends are filtered out here. The trailer's low
    bits are the operation (prepare/save/delete), not an accessory code; the
    accessory is recipe tag ``0x1C``.
    """
    if not 0 <= beverage_id <= 0xFF:
        raise ValueError(f"beverage id out of range: {beverage_id}")
    if not 0 <= slot <= 0x3F:
        raise ValueError("slot out of range")
    operation = BrewOperation(operation)
    tlv = encode_tlv({t: v for t, v in recipe.items() if brew_param_included(t)})
    trailer = bytes([(slot << 2) | operation])
    return Frame(REQUEST, OPCODE_BREW, 0xF0, bytes([beverage_id, int(action)]) + tlv + trailer)
