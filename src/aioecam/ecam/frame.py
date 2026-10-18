"""ECAM frame codec.

Wire layout (both directions)::

    direction | length | opcode | flags | payload ... | crc_hi | crc_lo

- ``direction``: ``0x0D`` app -> machine, ``0xD0`` machine -> app
- ``length``: number of bytes after the direction byte, CRC included
- ``crc``: CRC-16/CCITT (poly 0x1021, init 0x1D0F) over everything before it
"""

from __future__ import annotations

from dataclasses import dataclass

from aioecam.exceptions import FrameError

REQUEST = 0x0D
RESPONSE = 0xD0

_HEADER = 4  # direction, length, opcode, flags
_CRC = 2


def crc16(data: bytes) -> int:
    """CRC-16/CCITT with the ECAM init value 0x1D0F."""
    crc = 0x1D0F
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


@dataclass(frozen=True, slots=True)
class Frame:
    direction: int
    opcode: int
    flags: int
    payload: bytes = b""

    def encode(self) -> bytes:
        # length byte itself + opcode + flags + payload + crc
        length = _HEADER - 1 + len(self.payload) + _CRC
        if length > 0xFF:
            raise FrameError(f"payload too long ({len(self.payload)} bytes)")
        body = bytes((self.direction, length, self.opcode, self.flags)) + self.payload
        return body + crc16(body).to_bytes(2, "big")

    @classmethod
    def decode(cls, raw: bytes) -> tuple[Frame, bytes]:
        """Parse one frame from the start of ``raw``.

        Returns the frame and whatever follows it (Ayla property values append a
        timestamp and sometimes an app id after the CRC).
        """
        if len(raw) < _HEADER + _CRC:
            raise FrameError(f"frame too short ({len(raw)} bytes)")
        direction, length = raw[0], raw[1]
        if direction not in (REQUEST, RESPONSE):
            raise FrameError(f"unknown direction byte 0x{direction:02x}")
        end = length + 1
        if length < _HEADER - 1 + _CRC or len(raw) < end:
            raise FrameError(f"length byte {length} inconsistent with {len(raw)} bytes")
        body, crc = raw[: end - _CRC], raw[end - _CRC : end]
        if crc16(body) != int.from_bytes(crc, "big"):
            raise FrameError("CRC mismatch")
        frame = cls(direction=direction, opcode=raw[2], flags=raw[3], payload=bytes(body[_HEADER:]))
        return frame, bytes(raw[end:])

    @classmethod
    def from_hex(cls, hex_frame: str) -> Frame:
        frame, rest = cls.decode(bytes.fromhex(hex_frame))
        if rest:
            raise FrameError(f"{len(rest)} trailing bytes after frame")
        return frame
