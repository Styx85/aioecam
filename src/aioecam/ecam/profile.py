"""Model profiles: property names, state names, flag bits and stored commands.

Profiles live as JSON in ``aioecam/ecam/profiles/``, one file per model family.
Flags known to be wrong on a family are left out instead of being published.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any

from aioecam.ecam.frame import Frame
from aioecam.ecam.monitor import MachineState


@dataclass(frozen=True, slots=True)
class FlagBit:
    source: str  # "switches" or "alarms"
    byte: int
    bit: int


@dataclass(frozen=True, slots=True)
class StoredCommand:
    label: str
    frame: Frame


@dataclass(frozen=True)
class Profile:
    family: str
    models: tuple[str, ...]
    properties: dict[str, str]
    idle_status: frozenset[int]
    ready_status: int
    status_names: dict[int, str]
    accessory_names: dict[int, str]
    flags: dict[str, FlagBit]
    commands: dict[str, StoredCommand]

    @property
    def monitor_property(self) -> str:
        return self.properties["monitor"]

    @property
    def request_property(self) -> str:
        return self.properties["data_request"]

    def command(self, name: str) -> Frame:
        try:
            return self.commands[name].frame
        except KeyError:
            raise KeyError(f"{self.family} has no command {name!r}") from None

    def status_name(self, state: MachineState) -> str:
        return self.status_names.get(state.status, f"unknown_{state.status}")

    def accessory_name(self, state: MachineState) -> str:
        return self.accessory_names.get(state.accessory, f"unknown_{state.accessory}")

    def read_flags(self, state: MachineState) -> dict[str, bool]:
        return {name: state.bit(f.source, f.byte, f.bit) for name, f in self.flags.items()}

    def is_ready(self, state: MachineState) -> bool:
        """Ready and doing nothing: the only state in which a beverage may start."""
        return state.status == self.ready_status and state.action == 0

    def is_busy(self, state: MachineState) -> bool:
        # Standby keeps action != 0 for internal routines, so outside "ready"
        # busy follows the status (cremalink#249). But a beverage runs with
        # status staying "ready" and only action/progress changing (measured:
        # espresso on ECAM610.75 went through actions 4, 6, 8, 9, 11).
        if state.status == self.ready_status:
            return state.action != 0
        return state.status not in self.idle_status


def _parse(data: dict[str, Any]) -> Profile:
    return Profile(
        family=data["family"],
        models=tuple(data["models"]),
        properties=dict(data["properties"]),
        idle_status=frozenset(data["idle_status"]),
        ready_status=int(data["ready_status"]),
        status_names={int(k): v for k, v in data["status"].items()},
        accessory_names={int(k): v for k, v in data["accessory"].items()},
        flags={k: FlagBit(v["source"], v["byte"], v["bit"]) for k, v in data["flags"].items()},
        commands={
            k: StoredCommand(v["label"], Frame.from_hex(v["frame"]))
            for k, v in data["commands"].items()
        },
    )


@cache
def load_profile(family: str) -> Profile:
    text = resources.files("aioecam.ecam.profiles").joinpath(f"{family}.json").read_text("utf-8")
    return _parse(json.loads(text))


def available_families() -> list[str]:
    files = resources.files("aioecam.ecam.profiles").iterdir()
    return sorted(f.name.removesuffix(".json") for f in files if f.name.endswith(".json"))


def profile_for_model(model: str) -> Profile:
    """Find the profile for a model string such as ``"ECAM610.75.MB"`` or ``"ECAM 452.67.G"``."""
    key = model.upper().replace(" ", "")
    for family in available_families():
        profile = load_profile(family)
        if any(key.startswith(m) for m in profile.models):
            return profile
    raise LookupError(f"no profile for model {model!r}")
