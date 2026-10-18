from aioecam.ecam import commands
from aioecam.ecam.profile import load_profile
from aioecam.ecam.value import decode_value


def test_power_frames_match_profiles() -> None:
    for family in ("ecam45x", "ecam61x"):
        profile = load_profile(family)
        assert commands.power_on() == profile.command("wakeup")
        assert commands.power_off() == profile.command("standby")


def test_refresh_app_id_matches_eletta_profile() -> None:
    assert commands.refresh_app_id() == load_profile("ecam45x").command("refresh")


def test_request_write_soul() -> None:
    soul = load_profile("ecam61x")
    write = commands.request_write(soul, commands.power_on(), timestamp=1_792_339_810)
    assert write.name == "data_request"
    decoded = decode_value(write.value)
    assert decoded.frame == commands.power_on()
    assert decoded.timestamp == 1_792_339_810
    assert decoded.app_id is None


def test_request_write_eletta_uses_its_property_and_app_id() -> None:
    eletta = load_profile("ecam45x")
    write = commands.request_write(
        eletta, eletta.command("espresso"), timestamp=1, app_id=bytes.fromhex("c0ffeeee")
    )
    assert write.name == "app_data_request"
    assert decode_value(write.value).app_id == bytes.fromhex("c0ffeeee")
