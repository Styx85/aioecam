"""Parsers for machine answers (``0xD0`` frames) beyond monitor and statistics.

Answers echo the request opcode and flag; routing keys on that echo, never on
the property the frame arrived under. Ported from lodzen's ECAM layer
(https://github.com/lodzen/cremalink, AGPL-3.0, ``cremalink/ecam/answers.py``,
``profiles.py``, ``settings.py``, ``catalog.py``), adapted to work on already
CRC-checked :class:`~aioecam.ecam.frame.Frame` objects. The recipe/TLV grammar
there is in turn credited to actabi's ``delonghi_coffeelink`` (MIT).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from aioecam.ecam.commands import (
    OPCODE_PROFILE_NAMES,
    OPCODE_PROFILE_SELECT,
    OPCODE_READ_EXTENDED,
    OPCODE_READ_PARAM,
    OPCODE_RECIPE,
    OPCODE_SYNC_TIMEOUT,
    OPCODE_WRITE_PARAM,
    WIDE_TAGS,
)
from aioecam.ecam.frame import RESPONSE, Frame
from aioecam.exceptions import FrameError


def _require(frame: Frame, *opcodes: int) -> bytes:
    if frame.direction != RESPONSE or frame.opcode not in opcodes:
        wanted = "/".join(f"0x{o:02x}" for o in opcodes)
        raise FrameError(f"expected answer {wanted}, got 0x{frame.opcode:02x}")
    return frame.payload


def is_sync_timeout(frame: Frame) -> bool:
    """``0xE1``: the machine was busy and aborted the request (the app retries)."""
    return frame.direction == RESPONSE and frame.opcode == OPCODE_SYNC_TIMEOUT


def parse_write_ack(frame: Frame) -> tuple[int, bool]:
    """``0x90`` ack -> ``(param_id, committed)``; ``<param:2> <status:1>``, 0 = ok."""
    p = _require(frame, OPCODE_WRITE_PARAM)
    if len(p) < 3:
        raise FrameError("truncated write ack")
    return int.from_bytes(p[0:2], "big"), p[2] == 0


def parse_profile_select_ack(frame: Frame) -> tuple[int, bool]:
    """``0xA9`` ack -> ``(slot, ok)``; ``<slot:1> <status:1>``, 0 = ok."""
    p = _require(frame, OPCODE_PROFILE_SELECT)
    if len(p) < 2:
        raise FrameError("truncated profile select ack")
    return p[0], p[1] == 0


def parse_params(frame: Frame) -> dict[int, int]:
    """``0x95``/``0xA1`` parameter list -> ``{param_id: value}``.

    ``<start:2> <u32>...``; an ack echo without values or the all-zero
    "unknown parameter" answer yield ``{}``.
    """
    p = _require(frame, OPCODE_READ_PARAM, OPCODE_READ_EXTENDED)
    if len(p) < 6 or not any(p):
        return {}
    start = int.from_bytes(p[0:2], "big")
    values = p[2:]
    return {
        start + i // 4: int.from_bytes(values[i : i + 4], "big")
        for i in range(0, len(values) - 3, 4)
    }


@dataclass(frozen=True)
class ProfileSlot:
    slot: int  # 1-based
    name: str
    icon: int
    mug: int | None = None  # Eletta layout only


def _decode_name(raw: bytes) -> str | None:
    name = raw.decode("utf-16-be", errors="ignore").split("\x00", 1)[0].strip()
    # 0x00/0xFF padding decodes to control or non-characters: not a name.
    if not name or not any(not unicodedata.category(c).startswith("C") for c in name):
        return None
    return name


def parse_profile_names(frame: Frame, *, entry_size: int = 21) -> list[ProfileSlot]:
    """``0xA4`` -> occupied slots. ``<first> <last> <entries...> <trailing>``.

    Entries are a 20-byte UTF-16BE name plus an icon byte (21), on Eletta plus
    a mug byte (22). Empty slots are left out.
    """
    p = _require(frame, OPCODE_PROFILE_NAMES)
    if len(p) < 2:
        return []
    first, last = p[0], p[1]
    body = p[2:]
    slots = []
    for i in range(len(body) // entry_size):
        index = first + i
        if index > last:
            break
        entry = body[i * entry_size : (i + 1) * entry_size]
        name = _decode_name(entry[:20])
        if name is None:
            continue
        mug = entry[21] if entry_size == 22 else None
        slots.append(ProfileSlot(slot=index, name=name, icon=entry[20], mug=mug))
    return slots


def parse_tlv(data: bytes) -> dict[int, int] | None:
    """Recipe TLVs -> ``{tag: value}`` in wire order; ``None`` unless the walk
    consumes the data exactly (a partial parse could build a wrong brew)."""
    out: dict[int, int] = {}
    i = 0
    while i < len(data):
        tag = data[i]
        width = 2 if tag in WIDE_TAGS else 1
        if i + 1 + width > len(data):
            return None
        out[tag] = int.from_bytes(data[i + 1 : i + 1 + width], "big")
        i += 1 + width
    return out


@dataclass(frozen=True)
class Recipe:
    slot: int
    beverage_id: int
    params: dict[int, int]


def parse_recipe(frame: Frame) -> Recipe:
    """``0xA6`` -> the profile-scoped recipe of one beverage: ``<slot> <beverage> <TLV...>``."""
    p = _require(frame, OPCODE_RECIPE)
    if len(p) < 2:
        raise FrameError("truncated recipe answer")
    params = parse_tlv(p[2:])
    if params is None:
        raise FrameError("recipe TLVs do not parse")
    return Recipe(slot=p[0], beverage_id=p[1], params=params)
