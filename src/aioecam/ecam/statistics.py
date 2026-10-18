"""A2 statistics: the machine's own lifetime counters.

Request ``0D 08 A2 0F <start_id:2> <count:1> <crc>`` returns up to ``count``
existing parameters with id >= start_id. The id space is sparse, so paging
continues at the last returned id + 1 until a page comes back shorter than
requested. Reply payload: repeated ``<id:2><value:4>``.

Interpretation of IDs is model specific and only done where it was verified on
real hardware; everything else is kept as raw/unknown.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from aioecam.ecam.frame import REQUEST, RESPONSE, Frame
from aioecam.exceptions import FrameError

OPCODE_STATISTICS = 0xA2
MAX_COUNT = 10
_ENTRY = 6


def statistics_request(start_id: int, count: int = MAX_COUNT) -> Frame:
    if not 0 <= start_id <= 0xFFFF:
        raise ValueError("start_id must fit into 16 bits")
    if not 1 <= count <= MAX_COUNT:
        raise ValueError(f"count must be between 1 and {MAX_COUNT}")
    return Frame(REQUEST, OPCODE_STATISTICS, 0x0F, start_id.to_bytes(2, "big") + bytes([count]))


def is_statistics_response(frame: Frame) -> bool:
    return frame.direction == RESPONSE and frame.opcode == OPCODE_STATISTICS


def parse_statistics(frame: Frame) -> dict[int, int]:
    if not is_statistics_response(frame):
        raise FrameError(f"not an A2 response (opcode 0x{frame.opcode:02x})")
    payload = frame.payload
    if not payload or len(payload) % _ENTRY:
        raise FrameError(f"A2 payload of {len(payload)} bytes is not a list of 6-byte entries")
    return {
        int.from_bytes(payload[i : i + 2], "big"): int.from_bytes(payload[i + 2 : i + 6], "big")
        for i in range(0, len(payload), _ENTRY)
    }


# --- ECAM61x (PrimaDonna Soul) interpretation -------------------------------
# Ported from the cremalink fork (Styx85), hardware basis: ECAM610.75.MB.
# Confirmed IDs get names; unknown IDs are never guessed from values alone.

ECAM61X_DIRECT: dict[int, str] = {
    # maintenance
    100: "descale_load_raw",  # weighted accumulator, resets to 0 after descaling; no unit
    105: "descale_count",
    108: "filter_replacements",
    115: "grounds_container_clean_count",
    # top-level beverage categories
    3000: "total_black_beverages",
    3001: "total_milk_coffee_beverages",
    3002: "total_other_beverages",
    3003: "total_milk_only_beverages",
    # beverages
    3004: "total_espressos",
    3005: "espresso",
    3006: "coffee",
    3007: "long_coffee",
    3008: "doppio",
    3009: "americano",
    3010: "cappuccino",
    3011: "latte_macchiato",
    3012: "caffe_latte",
    3013: "flat_white",
    3014: "espresso_macchiato",
    3015: "hot_milk",
    3016: "cappuccino_doppio",
    3017: "cappuccino_mix",
    # ECAM610.75: one 100 ml hot-water command (beverage 0x10) incremented
    # 3019 from 2 to 3; 3018 stayed at 0. The old "tea" label was wrong.
    # 3018 has no confirmed meaning and remains in raw/unknown statistics.
    3019: "hot_water",
    3020: "coffee_pot",
    3037: "espresso_soul",
    3046: "over_ice",
    43000: "custom_milk_coffee_beverages",
    # 3000 + 3001 + 3002 + 3003 == 43010 on real hardware (396+5231+2+180 = 5809)
    43010: "total_beverages",
}

# raw / 2000 = litres. 106 counts rinses and filter activation water but not the
# descaling programme itself; 109 resets on a completed filter replacement.
ECAM61X_WATER_L: dict[int, str] = {
    106: "total_water_l",
    109: "water_since_filter_change_l",
}
_WATER_DIVISOR = 2000.0

_INTERPRETED_FAMILIES = frozenset({"ecam61x"})


@dataclass(frozen=True)
class StatisticsSnapshot:
    known: dict[str, int | float] = field(default_factory=dict)
    unknown: dict[int, int] = field(default_factory=dict)
    raw: dict[int, int] = field(default_factory=dict)


def interpret(family: str, raw: Mapping[int, int]) -> StatisticsSnapshot:
    """Split a raw A2 table into confirmed values and untouched unknowns."""
    table = {int(k): int(v) for k, v in raw.items()}
    if family not in _INTERPRETED_FAMILIES:
        return StatisticsSnapshot(unknown=dict(table), raw=table)

    known: dict[str, int | float] = {}
    for sid, name in ECAM61X_DIRECT.items():
        if sid in table:
            known[name] = table[sid]
    for sid, name in ECAM61X_WATER_L.items():
        if sid in table:
            known[name] = table[sid] / _WATER_DIVISOR
    # The machine's "with milk" display value.
    if 3001 in table and 3003 in table:
        known["total_milk_beverages"] = table[3001] + table[3003]
    categories = (3000, 3001, 3002, 3003)
    if "total_beverages" not in known and all(c in table for c in categories):
        known["total_beverages"] = sum(table[c] for c in categories)

    interpreted = set(ECAM61X_DIRECT) | set(ECAM61X_WATER_L)
    unknown = {sid: v for sid, v in table.items() if sid not in interpreted}
    return StatisticsSnapshot(known=known, unknown=unknown, raw=table)
