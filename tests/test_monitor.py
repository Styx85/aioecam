import json
from pathlib import Path

import pytest

from aioecam.ecam.frame import Frame
from aioecam.ecam.monitor import MachineState
from aioecam.exceptions import FrameError

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "frames.json").read_text())

# Observed on the real machine (ECAM610.75, 2026-10-18) together with what it physically did.
OBSERVED = [
    # (value, status, action, progress, what the machine was doing)
    ("0BJ1DwRAAgEAAAJkAAAAAAA/+mrU6XE=", 0, 2, 100, "off / standby"),
    ("0BJ1DwBEAAEAAQEAAAAAAABR2GrU/XM=", 1, 1, 0, "waking up right after the wakeup command"),
    ("0BJ1DwAGAAAAAQVDAAAAAACAAWrU/YY=", 1, 5, 67, "heating while waking up"),
    ("0BJ1DwAAAAAABwAAAAAAAADWo2rU/ZE=", 7, 0, 0, "ready"),
    ("0BJ1DwAEAAAAAgQAAAAAAABSD2rU/PQ=", 2, 4, 0, "going to sleep, rinsing"),
]


@pytest.mark.parametrize(("value", "status", "action", "progress", "_doing"), OBSERVED)
def test_observed_states(value: str, status: int, action: int, progress: int, _doing: str) -> None:
    state = MachineState.from_value(value)
    assert (state.status, state.action, state.progress) == (status, action, progress)


@pytest.mark.parametrize("value", FIXTURES["monitor"])
def test_all_captured_monitor_frames_parse(value: str) -> None:
    state = MachineState.from_value(value)
    assert 0 <= state.progress <= 100
    assert len(state.alarms) == 4 and len(state.switches) == 2


def test_timestamp_is_kept() -> None:
    assert MachineState.from_value(OBSERVED[0][0]).timestamp == 0x6AD4E971


@pytest.mark.parametrize("value", FIXTURES["other"])
def test_non_monitor_frames_rejected(value: str) -> None:
    with pytest.raises(FrameError, match="not a monitor frame"):
        MachineState.from_value(value)


def test_wrong_payload_length() -> None:
    frame = Frame(0xD0, 0x75, 0x0F, b"\x00" * 12)
    with pytest.raises(FrameError, match="12 bytes"):
        MachineState.from_frame(frame)


def test_bit_access() -> None:
    state = MachineState.from_value(OBSERVED[0][0])  # alarms 01 00 00 00, switches 40 02
    assert state.bit("alarms", 0, 0)
    assert not state.bit("alarms", 0, 1)
    assert state.bit("switches", 0, 6)
    assert state.bit("switches", 1, 1)
    assert not state.bit("alarms", 7, 0)  # out of range is False, not an error
