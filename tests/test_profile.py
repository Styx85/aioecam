import pytest

from aioecam.ecam.frame import REQUEST
from aioecam.ecam.monitor import MachineState
from aioecam.ecam.profile import available_families, load_profile, profile_for_model

READY = "0BJ1DwAAAAAABwAAAAAAAADWo2rU/ZE="
WAKING = "0BJ1DwAGAAAAAQVDAAAAAACAAWrU/YY="
STANDBY = "0BJ1DwRAAgEAAAJkAAAAAAA/+mrU6XE="


def test_families_bundled() -> None:
    assert available_families() == ["ecam45x", "ecam61x"]


@pytest.mark.parametrize("family", ["ecam45x", "ecam61x"])
def test_profile_is_complete(family: str) -> None:
    profile = load_profile(family)
    assert {"monitor", "data_request"} <= profile.properties.keys()
    assert {"wakeup", "standby", "stop", "espresso"} <= profile.commands.keys()
    # loading already validated every CRC; also check they are app -> machine frames
    assert all(c.frame.direction == REQUEST for c in profile.commands.values())


@pytest.mark.parametrize(
    ("model", "family"),
    [
        ("ECAM610.75.MB", "ecam61x"),
        ("ecam 612.55.sb", "ecam61x"),
        ("ECAM452.67.G", "ecam45x"),
        ("ECAM450.65.G", "ecam45x"),
    ],
)
def test_profile_for_model(model: str, family: str) -> None:
    assert profile_for_model(model).family == family


def test_unknown_model() -> None:
    with pytest.raises(LookupError):
        profile_for_model("EXAM440.55.B")


def test_soul_states() -> None:
    soul = load_profile("ecam61x")
    ready, waking, standby = (MachineState.from_value(v) for v in (READY, WAKING, STANDBY))
    assert soul.status_name(ready) == "ready" and not soul.is_busy(ready)
    assert soul.status_name(waking) == "waking_up" and soul.is_busy(waking)
    # standby keeps action != 0; it must still not count as busy (cremalink#249)
    assert standby.action != 0 and not soul.is_busy(standby)
    assert soul.monitor_property == "d302_monitor"


def test_soul_has_no_watertank_empty_flag() -> None:
    # bit 0 of alarms[0] was set with a freshly filled tank on the ECAM610.75
    soul = load_profile("ecam61x")
    assert "is_watertank_empty" not in soul.read_flags(MachineState.from_value(STANDBY))


def test_unknown_status_name() -> None:
    soul = load_profile("ecam61x")
    state = MachineState(0, b"\0\0", b"\0\0\0\0", status=99, action=0, progress=0)
    assert soul.status_name(state) == "unknown_99"


def test_unknown_command() -> None:
    with pytest.raises(KeyError, match="no command"):
        load_profile("ecam61x").command("teleport")


def test_busy_while_brewing_with_status_ready() -> None:
    soul = load_profile("ecam61x")
    brewing = MachineState(0, b"\0\0", b"\0\0\0\0", status=7, action=11, progress=44)
    idle = MachineState(0, b"\0\0", b"\0\0\0\0", status=7, action=0, progress=0)
    assert soul.is_busy(brewing) and not soul.is_ready(brewing)
    assert not soul.is_busy(idle) and soul.is_ready(idle)
