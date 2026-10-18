"""Payloads exchanged inside an Ayla LAN session (the ``data`` of a sealed message)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DATAPOINT_URI = "/local_lan/property/datapoint.json"


def property_set(
    dsn: str, name: str, value: str, base_type: str = "string", *, newline: bool = True
) -> dict[str, Any]:
    """App -> machine: write a property.

    Frame-carrying strings (``data_request``) get a trailing newline like the
    official app sends them; plain values such as the ``device_connected``
    timestamp are written as they are (``newline=False``).
    """
    if newline and base_type == "string" and not value.endswith("\n"):
        value += "\n"
    return {
        "properties": [
            {"property": {"base_type": base_type, "dsn": dsn, "name": name, "value": value}}
        ]
    }


def property_get(name: str, cmd_id: int) -> dict[str, Any]:
    """App -> machine: ask the machine to POST the current value of a property."""
    return {
        "cmds": [
            {
                "cmd": {
                    "cmd_id": cmd_id,
                    "data": "",
                    "method": "GET",
                    "resource": f"property.json?name={name}",
                    "uri": DATAPOINT_URI,
                }
            }
        ]
    }


@dataclass(frozen=True, slots=True)
class Datapoint:
    """Machine -> app: one property value."""

    name: str
    value: Any
    base_type: str | None = None

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> Datapoint | None:
        name = data.get("name")
        if not isinstance(name, str):
            return None
        return cls(name=name, value=data.get("value"), base_type=data.get("base_type"))
