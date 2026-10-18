"""Monitor frames (opcode 0x75, "DATA2"): the machine's live state.

Payload layout (13 bytes)::

    0      accessory
    1..2   switches
    3..4   alarms (low)
    5      status
    6      action
    7      progress (0..100)
    8..9   alarms (high)
    10..12 unused

While the machine sleeps, the Wi-Fi module keeps answering with the last frame
it saw, byte for byte (timestamp included). An unchanged monitor therefore does
not mean the connection is dead.
"""

from __future__ import annotations

from dataclasses import dataclass

from aioecam.ecam.frame import RESPONSE, Frame
from aioecam.ecam.value import decode_value
from aioecam.exceptions import FrameError

OPCODE_MONITOR = 0x75
_PAYLOAD_LEN = 13


@dataclass(frozen=True, slots=True)
class MachineState:
    accessory: int
    switches: bytes
    alarms: bytes
    status: int
    action: int
    progress: int
    timestamp: int | None = None

    @classmethod
    def from_frame(cls, frame: Frame, timestamp: int | None = None) -> MachineState:
        if frame.direction != RESPONSE or frame.opcode != OPCODE_MONITOR:
            raise FrameError(
                f"not a monitor frame (direction 0x{frame.direction:02x}, "
                f"opcode 0x{frame.opcode:02x})"
            )
        p = frame.payload
        if len(p) != _PAYLOAD_LEN:
            raise FrameError(f"monitor payload has {len(p)} bytes, expected {_PAYLOAD_LEN}")
        return cls(
            accessory=p[0],
            switches=bytes(p[1:3]),
            alarms=bytes(p[3:5] + p[8:10]),
            status=p[5],
            action=p[6],
            progress=p[7],
            timestamp=timestamp,
        )

    @classmethod
    def from_value(cls, value: str) -> MachineState:
        decoded = decode_value(value)
        return cls.from_frame(decoded.frame, decoded.timestamp)

    def bit(self, source: str, byte: int, bit: int) -> bool:
        """Read a flag bit from ``switches`` or ``alarms``."""
        data = {"switches": self.switches, "alarms": self.alarms}[source]
        return byte < len(data) and bool(data[byte] >> bit & 1)
