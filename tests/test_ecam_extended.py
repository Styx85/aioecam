"""Request builders and answer parsers ported from lodzen's ECAM layer.

Real frames below are LAN captures from a PrimaDonna Soul published in
https://github.com/lodzen/cremalink (tests/fixtures/ecam, AGPL-3.0); profile
names there are anonymised ("Alice", "Bob").
"""

from __future__ import annotations

import pytest

from aioecam.ecam import commands
from aioecam.ecam.answers import (
    is_sync_timeout,
    parse_params,
    parse_profile_names,
    parse_profile_select_ack,
    parse_recipe,
    parse_tlv,
    parse_write_ack,
)
from aioecam.ecam.frame import RESPONSE, Frame
from aioecam.ecam.profile import load_profile
from aioecam.ecam.settings import SETTINGS
from aioecam.ecam.value import decode_value
from aioecam.exceptions import FrameError

# lodzen captures (Soul, LAN)
D282_AUTO_OFF = "0AuVDwA+AAAAAmOc"  # stored value of param 0x3E = 2 ("1 h")
D039_RECIPE_ESPRESSO_P1 = "0BSm8AEBCAABACgbAQIEBAAZAaPq"  # profile 1, espresso
D034_PROFILES_1_3 = (
    "0Eek8AEDAEEAbABpAGMAZQAAAAAAAAAAAAAQAEIAbwBiAAAAAAAAAAAAAAAAAAAF"
    "AFAAcgBvAGYAaQBsACAAMwAAAAAJAKhz"
)
WRITE_ACK_AUTO_OFF = "0AiQ8AA+AMsDasOXRw=="  # data_response after writing 0x3E
PROFILE_SELECT_ACK_1 = "d007a9f001003b3c"  # also seen by us when the app woke the machine


def frame_of(value: str) -> Frame:
    return decode_value(value).frame


# --- request builders --------------------------------------------------------------


def test_monitor_request() -> None:
    frame = commands.monitor_request()
    assert frame.encode()[:4].hex() == "0d05750f"
    assert Frame.from_hex(frame.encode().hex()) == frame


def test_profile_select_matches_capture() -> None:
    assert commands.profile_select(1).encode().hex() == "0d06a9f001d7c0"
    with pytest.raises(ValueError):
        commands.profile_select(0)


def test_read_param_matches_capture() -> None:
    assert commands.read_param(0x3E).encode().hex() == "0d0795f0003e56bc"


def test_write_param_flag_rule() -> None:
    assert commands.write_param(0x3E, 2).flags == 0x0F  # < 1000
    assert commands.write_param(1000, 2).flags == 0xF0
    # The capture used F0 explicitly; the override reproduces it byte for byte.
    assert commands.write_param(0x3E, 2, flag=0xF0).encode().hex() == "0d0b90f0003e0000000204d0"


def test_read_recipe_and_profile_names_frames() -> None:
    assert commands.read_recipe(1, 1).encode()[:6].hex() == "0d07a6f00101"
    assert commands.read_profile_names(1, 3).encode()[:6].hex() == "0d07a4f00103"


def test_striker_handshake() -> None:
    assert commands.striker_handshake().encode()[:5].hex() == "0d06e8f000"


@pytest.mark.parametrize("family", ["ecam61x", "ecam45x"])
def test_every_stored_beverage_rebuilds_byte_for_byte(family: str) -> None:
    """The parametric builder reproduces every stored 0x83 frame of the device maps."""
    rebuilt = 0
    for name, stored in load_profile(family).commands.items():
        frame = stored.frame
        if frame.opcode != commands.OPCODE_BREW or frame.payload[1] != commands.BrewAction.START:
            continue
        p = frame.payload
        recipe = parse_tlv(p[2:-1])
        assert recipe is not None, name
        built = commands.brew(p[0], recipe, slot=p[-1] >> 2, operation=p[-1] & 3)
        assert built == frame, f"{name}: {built.encode().hex()} != {frame.encode().hex()}"
        rebuilt += 1
    assert rebuilt >= 15


def test_brew_from_machine_recipe_drops_what_the_app_drops() -> None:
    recipe = parse_recipe(frame_of(D039_RECIPE_ESPRESSO_P1))
    built = commands.brew(recipe.beverage_id, recipe.params, slot=recipe.slot)
    # tag 0x08 and tag 0x19 (25) are never sent; order follows the machine's recipe
    assert built.payload.hex() == "0101" + "010028" + "1b01" + "0204" + "0400" + "06"


def test_encode_tlv_rejects_overflow() -> None:
    with pytest.raises(ValueError):
        commands.encode_tlv({0x02: 300})


# --- answers ------------------------------------------------------------------------


def test_stored_setting_value() -> None:
    assert parse_params(frame_of(D282_AUTO_OFF)) == {0x3E: 2}
    assert SETTINGS["auto_off"].options[2] == "1 h"


def test_param_list_edge_cases() -> None:
    echo = Frame(RESPONSE, 0x95, 0xF0, bytes.fromhex("003e"))
    unknown = Frame(RESPONSE, 0x95, 0xF0, bytes(36))
    assert parse_params(echo) == {} and parse_params(unknown) == {}


def test_write_ack() -> None:
    assert parse_write_ack(frame_of(WRITE_ACK_AUTO_OFF)) == (0x3E, True)


def test_profile_select_ack() -> None:
    assert parse_profile_select_ack(Frame.from_hex(PROFILE_SELECT_ACK_1)) == (1, True)


def test_profile_names() -> None:
    slots = parse_profile_names(frame_of(D034_PROFILES_1_3))
    assert [(s.slot, s.name) for s in slots] == [(1, "Alice"), (2, "Bob"), (3, "Profil 3")]


def test_recipe() -> None:
    recipe = parse_recipe(frame_of(D039_RECIPE_ESPRESSO_P1))
    assert (recipe.slot, recipe.beverage_id) == (1, 1)
    assert recipe.params == {0x08: 0, 0x01: 40, 0x1B: 1, 0x02: 4, 0x04: 0, 0x19: 1}


def test_tlv_partial_walk_is_refused() -> None:
    assert parse_tlv(bytes.fromhex("0100")) is None  # wide tag cut short


def test_wrong_answer_type() -> None:
    with pytest.raises(FrameError, match="expected answer"):
        parse_write_ack(Frame.from_hex(PROFILE_SELECT_ACK_1))


def test_sync_timeout() -> None:
    assert is_sync_timeout(Frame(RESPONSE, 0xE1, 0xF0, b""))
    assert not is_sync_timeout(Frame.from_hex(PROFILE_SELECT_ACK_1))


def test_recipe_tag_names_follow_the_app() -> None:
    import aioecam.device as device

    # Coffee Link 4.9.6: 0x1B is INDEX_LENGTH, temperatures are 0x00 and 0x0D
    assert device.TAG_NAMES[0x1B] == "index_length"
    assert device.TAG_NAMES[device.TAG_TEMP] == "temperature" and device.TAG_TEMP == 0x00
    assert device.TAG_TEA_TEMP == 0x0D and device.TAG_VISIBLE == 0x19
    assert not hasattr(device, "TAG_TEMPERATURE")


def test_stored_brew_frames_are_all_prepare() -> None:
    from aioecam.ecam.profile import available_families, load_profile

    for family in available_families():
        for name, cmd in load_profile(family).commands.items():
            if cmd.frame.opcode == commands.OPCODE_BREW:
                assert cmd.frame.payload[-1] & 3 == commands.BrewOperation.PREPARE, name


def test_brew_trailer_is_profile_and_operation() -> None:
    frame = commands.brew(16, {0x0F: 100}, slot=1)
    assert frame.payload[-1] == (1 << 2) | 2  # PREPARE, as the app sends it
    save = commands.brew(16, {0x0F: 100}, slot=1, operation=commands.BrewOperation.SAVE)
    assert save.payload[-1] == (1 << 2) | 1
    with pytest.raises(ValueError):
        commands.brew(16, {0x0F: 100}, slot=1, operation=6)  # inversion: not modelled
