"""The machine's serial number property (``d270_serialnumber``).

A response frame (opcode ``0xA1``) with two leading bytes and then ASCII text::

    CCCCCC EE YY MM DD L PPPP

product code (SKU), variant, production date, a letter and a production number.
Layout from sk7n4k3d/delonghi-ha (MIT); checked on an ECAM610.75. The letter and
the production number identify the single machine, so they are not kept.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from aioecam.ecam.frame import RESPONSE
from aioecam.ecam.value import decode_value
from aioecam.exceptions import FrameError

SERIAL_PROPERTY = "d270_serialnumber"
OPCODE_SERIAL = 0xA1

_LAYOUT = re.compile(rb"(\d{6})([0-9A-Z]{2})(\d{2})(\d{2})(\d{2})[0-9A-Z]\d{4}\x00*")


@dataclass(frozen=True, slots=True)
class SerialInfo:
    sku: str
    variant: str
    production_date: date


def parse_serial(value: object) -> SerialInfo | None:
    """The non-identifying parts of a serial value; ``None`` for any other format."""
    if not isinstance(value, str):
        return None
    try:
        frame = decode_value(value).frame
    except FrameError:
        return None
    if frame.direction != RESPONSE or frame.opcode != OPCODE_SERIAL:
        return None
    match = _LAYOUT.fullmatch(frame.payload[2:])
    if match is None:
        return None
    sku, variant, year, month, day = (part.decode("ascii") for part in match.groups())
    try:
        production_date = date(2000 + int(year), int(month), int(day))
    except ValueError:
        return None
    return SerialInfo(sku, variant, production_date)
