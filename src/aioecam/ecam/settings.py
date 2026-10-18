"""Writable machine settings (``0x95`` read, ``0x90`` write).

Only settings that lodzen verified on a PrimaDonna Soul are listed
(https://github.com/lodzen/cremalink, ``cremalink/ecam/settings.py``). A profile
opts in per setting with its ``settings`` list.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Setting:
    key: str
    param_id: int
    options: dict[int, str]  # raw value -> label


SETTINGS: dict[str, Setting] = {
    # Labels as shown on the machine's display.
    "auto_off": Setting("auto_off", 0x003E, {0: "15 min", 1: "30 min", 2: "1 h", 3: "3 h"}),
    # Stored as a 0-based index; the machine shows level = value + 1.
    "water_hardness": Setting(
        "water_hardness", 0x0032, {0: "level_1", 1: "level_2", 2: "level_3", 3: "level_4"}
    ),
}
