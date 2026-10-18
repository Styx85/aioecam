"""Passive, bounded catalogue of CRC-checked incoming recipe properties.

No I/O. Property names are provenance, never protocol selectors. Text-bearing
frames are represented by slot metadata only: household names never leave the
parser. Missing descriptors/recipes remain unknown, not unsupported.
"""

from __future__ import annotations

import base64
import binascii
from collections import OrderedDict
from copy import deepcopy
from typing import Any

from aioecam.ecam.answers import parse_tlv
from aioecam.ecam.commands import WIDE_TAGS
from aioecam.ecam.frame import RESPONSE
from aioecam.ecam.value import decode_value
from aioecam.exceptions import FrameError

FAMILIES = {
    0xB0: "descriptor",
    0xA6: "recipe",
    0xA8: "priority",
    0xA4: "profile_names",
    0xAA: "recipe_names",
    0xBA: "bean_names",
}


def parse_ranges(payload: bytes) -> dict[int, dict[str, int]]:
    """Descriptor triples; reject a partial walk or contradictory bounds."""
    result = {}
    offset = 0
    while offset < len(payload):
        tag = payload[offset]
        width = 2 if tag in WIDE_TAGS else 1
        end = offset + 1 + 3 * width
        if end > len(payload) or tag in result:
            raise FrameError("invalid descriptor parameters")
        start = offset + 1
        low, default, high = (
            int.from_bytes(payload[start + i * width : start + (i + 1) * width], "big")
            for i in range(3)
        )
        if not low <= default <= high:
            raise FrameError("inconsistent descriptor bounds")
        result[tag] = {"min": low, "default": default, "max": high}
        offset = end
    return result


def _payload(opcode: int, payload: bytes) -> dict[str, Any]:
    if not payload:
        raise FrameError("empty catalogue payload")
    if opcode == 0xB0:
        return {"beverage_id": payload[0], "ranges": parse_ranges(payload[1:])}
    if opcode == 0xA8:
        return {"profile": payload[0], "beverage_order": list(payload[1:])}
    if len(payload) < 2:
        raise FrameError("truncated catalogue header")
    if opcode == 0xA6:
        params = parse_tlv(payload[2:])
        if params is None:
            raise FrameError("incomplete recipe parameters")
        return {"profile": payload[0], "beverage_id": payload[1], "parameters": params}
    # Names may contain multiple fixed-width entries, with model-specific layouts.
    # Keep only the range; do not guess names/icons or expose any text/raw bytes.
    return {"first_slot": payload[0], "last_slot": payload[1], "names": "redacted"}


class PassiveCatalog:
    """Last valid parsed value per property, plus latest validation outcome.

    A later bad frame preserves the last good value with an explicit error and
    its original timestamp. Entries are LRU-bounded; no unbounded packet log.
    """

    def __init__(self, *, max_properties: int = 512) -> None:
        if max_properties < 1:
            raise ValueError("max_properties must be positive")
        self.max_properties = max_properties
        self._entries: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.evictions = 0

    def _invalid(self, name: str, received_at: float) -> None:
        if name in self._entries:
            entry = self._entries.pop(name)
            entry.update(received_at=received_at, last_error="invalid_frame_or_payload")
            self._entries[name] = entry

    def observe(self, name: str, value: object, received_at: float) -> None:
        if not isinstance(value, str) or len(value) > 1024:
            self._invalid(name, received_at)
            return
        # Inspect only the header before full validation. Serial/settings/PIN and
        # command echoes must never be retained, including when their CRC is bad.
        try:
            text = value.strip()
            raw = base64.b64decode(text + "=" * (-len(text) % 4), validate=True)
        except (ValueError, binascii.Error):
            self._invalid(name, received_at)
            return
        if len(raw) < 4 or raw[0] != RESPONSE or raw[2] not in FAMILIES or raw[3] != 0xF0:
            self._invalid(name, received_at)
            return
        opcode = raw[2]
        previous = self._entries.pop(name, {})
        entry = {
            **previous,
            "property": name,
            "family": FAMILIES[opcode],
            "received_at": received_at,
            "last_error": None,
        }
        # A property changing family must not inherit an unrelated parsed value.
        if previous.get("family") != entry["family"]:
            entry.pop("data", None)
            entry.pop("last_valid_at", None)
        try:
            frame = decode_value(value).frame
            entry["data"] = _payload(opcode, frame.payload)
            entry["last_valid_at"] = received_at
        except FrameError:
            entry["last_error"] = "invalid_frame_or_payload"
        self._entries[name] = entry
        if len(self._entries) > self.max_properties:
            self._entries.popitem(last=False)
            self.evictions += 1

    def snapshot(self) -> dict[str, Any]:
        """JSON-compatible diagnostic data; values do not assert brewability."""
        return {
            "mode": "passive",
            "scope": "observed_properties_only",
            "property_count": len(self._entries),
            "evictions": self.evictions,
            "entries": deepcopy(list(self._entries.values())),
        }
