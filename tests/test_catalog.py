"""A passive catalogue must not turn corrupt/missing data into capabilities."""

import base64

import pytest

from aioecam.ecam.catalog import PassiveCatalog
from aioecam.ecam.frame import RESPONSE, Frame
from aioecam.ecam.value import encode_value


def value(opcode: int, payload: bytes) -> str:
    return encode_value(Frame(RESPONSE, opcode, 0xF0, payload), timestamp=1)


def test_ranges_recipes_unknown_tags_and_priority() -> None:
    catalog = PassiveCatalog()
    # Descriptor: coffee min/default/max, then an unknown narrow parameter.
    catalog.observe("any_name", value(0xB0, bytes.fromhex("01 01 0014 0028 00b4 7e 00 02 09")), 1)
    catalog.observe("recipe", value(0xA6, bytes.fromhex("01 01 01 0028 7e 03")), 2)
    catalog.observe("priority", value(0xA8, bytes([1, 16])), 3)
    entries = catalog.snapshot()["entries"]
    assert entries[0]["data"]["ranges"][1] == {"min": 20, "default": 40, "max": 180}
    assert entries[1]["data"]["parameters"] == {1: 40, 126: 3}
    assert entries[2]["data"]["beverage_order"] == [16]  # never drops first entry
    assert catalog.snapshot()["scope"] == "observed_properties_only"


@pytest.mark.parametrize("corruption", ["crc", "length", "tlv", "bounds"])
def test_invalid_update_retains_last_good_with_error(corruption: str) -> None:
    catalog = PassiveCatalog()
    good = value(0xB0, bytes.fromhex("01 02 01 03 05"))
    catalog.observe("x", good, 1)
    if corruption in {"crc", "length"}:
        raw = bytearray(base64.b64decode(good))
        raw[1 if corruption == "length" else 5] ^= 0x40
        bad = base64.b64encode(raw).decode()
    else:
        bad = value(0xB0, bytes.fromhex("01 02 05" if corruption == "tlv" else "01 02 05 03 01"))
    catalog.observe("x", bad, 2)
    entry = catalog.snapshot()["entries"][0]
    assert entry["last_valid_at"] == 1 and entry["received_at"] == 2
    assert entry["last_error"] is not None
    assert entry["data"]["ranges"][2]["default"] == 3


def test_names_private_settings_ignored_and_storage_bounded() -> None:
    catalog = PassiveCatalog(max_properties=2)
    for opcode in (0x95, 0xA1, 0x75):
        catalog.observe("secret", value(opcode, b"serial PIN secret"), 1)
    assert catalog.snapshot()["property_count"] == 0
    catalog.observe("names", value(0xA4, b"\x01\x03" + "Private Name".encode("utf-16-be")), 1)
    snapshot = catalog.snapshot()
    assert "Private" not in str(snapshot) and snapshot["entries"][0]["data"]["names"] == "redacted"
    snapshot["entries"][0]["data"]["first_slot"] = 99
    assert catalog.snapshot()["entries"][0]["data"]["first_slot"] == 1
    for i in range(3):
        catalog.observe(f"r{i}", value(0xA6, bytes([1, i])), i)
    assert catalog.snapshot()["property_count"] == 2 and catalog.evictions == 2


@pytest.mark.parametrize("bad", ["not base64!", None, "x" * 2048])
def test_unreadable_update_is_not_left_looking_current(bad: object) -> None:
    catalog = PassiveCatalog()
    catalog.observe("x", value(0xA6, bytes.fromhex("01 01 01 0028")), 1)
    catalog.observe("x", bad, 2)
    entry = catalog.snapshot()["entries"][0]
    assert entry["last_error"] == "invalid_frame_or_payload"
    assert entry["last_valid_at"] == 1 and entry["received_at"] == 2


def test_echo_and_wrong_flags_cannot_create_catalogue_entries() -> None:
    catalog = PassiveCatalog()
    catalog.observe("echo", encode_value(Frame(0x0D, 0xA6, 0xF0, b"\x01\x01")), 1)
    catalog.observe("other", encode_value(Frame(RESPONSE, 0xA6, 0x0F, b"\x01\x01")), 1)
    assert catalog.snapshot()["entries"] == []
