from __future__ import annotations

import base64
from datetime import date

import pytest

from aioecam.ecam.frame import REQUEST, RESPONSE, Frame
from aioecam.ecam.serial import SerialInfo, parse_serial


def value(text: bytes, *, direction: int = RESPONSE, opcode: int = 0xA1) -> str:
    raw = Frame(direction, opcode, 0x0F, b"\x00\x11" + text).encode()
    return base64.b64encode(raw).decode()


def test_parses_and_keeps_only_what_does_not_identify_the_machine() -> None:
    info = parse_serial(value(b"123456ZZ250701A9876\x00"))
    assert info == SerialInfo("123456", "ZZ", date(2025, 7, 1))
    assert "9876" not in repr(info)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        42,
        "ECAM61075MB12345",
        "not base64!",
        value(b"123456ZZ251301A9876\x00"),  # month 13
        value(b"123456ZZ250231A9876\x00"),  # 31 February
        value(b"123456ZZ250701A98"),  # too short
        value(b"123456ZZ250701A9876\x00", opcode=0x95),
        value(b"123456ZZ250701A9876\x00", direction=REQUEST),
    ],
)
def test_other_formats_are_unknown(bad: object) -> None:
    assert parse_serial(bad) is None
