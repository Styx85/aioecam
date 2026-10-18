import json
from pathlib import Path

import pytest

from aioecam.ecam.frame import REQUEST, RESPONSE, Frame, crc16
from aioecam.ecam.value import decode_value, encode_value
from aioecam.exceptions import FrameError

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "frames.json").read_text())


def test_crc_known_command() -> None:
    # wakeup from the cremalink device maps
    assert crc16(bytes.fromhex("0d07840f0201")) == 0x5512


def test_encode_matches_device_map() -> None:
    frame = Frame(REQUEST, 0x84, 0x0F, b"\x02\x01")
    assert frame.encode().hex() == "0d07840f02015512"


def test_from_hex_roundtrip() -> None:
    frame = Frame.from_hex("0d07840f01010041")
    assert (frame.opcode, frame.flags, frame.payload) == (0x84, 0x0F, b"\x01\x01")
    assert frame.encode().hex() == "0d07840f01010041"


def test_bad_crc_rejected() -> None:
    with pytest.raises(FrameError, match="CRC"):
        Frame.from_hex("0d07840f02015513")


@pytest.mark.parametrize(
    "raw",
    ["0d07", "0d0f840f02015512", "ff07840f02015512", "0d03840f0201"],
)
def test_malformed_rejected(raw: str) -> None:
    with pytest.raises(FrameError):
        Frame.decode(bytes.fromhex(raw))


def test_trailing_bytes_rejected_by_from_hex() -> None:
    with pytest.raises(FrameError, match="trailing"):
        Frame.from_hex("0d07840f0201551200")


@pytest.mark.parametrize("value", FIXTURES["monitor"] + FIXTURES["other"])
def test_all_captured_values_decode(value: str) -> None:
    decoded = decode_value(value)
    assert decoded.frame.direction in (REQUEST, RESPONSE)
    assert decoded.timestamp is not None and decoded.timestamp > 1_700_000_000


def test_value_with_app_id() -> None:
    # data_request echo written by the official app: refreshAppId + timestamp + app id
    decoded = decode_value("DQeEDwMCVkBq1PRBIEA17w==")
    assert decoded.frame.opcode == 0x84 and decoded.frame.payload == b"\x03\x02"
    assert decoded.app_id == bytes.fromhex("204035ef")


def test_unpadded_value() -> None:
    assert decode_value("0Aep8AEAOzxq1Ouc").frame.opcode == 0xA9


def test_encode_value_roundtrip() -> None:
    frame = Frame.from_hex("0d07840f02015512")
    value = encode_value(frame, timestamp=1_792_338_835, app_id=b"\xc0\xff\xee\xee")
    decoded = decode_value(value)
    assert decoded.frame == frame
    assert decoded.timestamp == 1_792_338_835
    assert decoded.app_id == b"\xc0\xff\xee\xee"


def test_non_base64_value() -> None:
    with pytest.raises(FrameError):
        decode_value("not a frame!")
