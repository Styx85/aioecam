"""A simulated ECAM machine behind a FakeDevice.

Behaviour follows what an ECAM610.75 did on 2026-10-18:
wake 0 -> 1 -> 7, standby 7 -> 2 -> 0, and a beverage keeps status 7 while
only action/progress change (espresso: actions 4, 6, 8, 9, 11, then 0).
By default changes are NOT pushed; the new value is only seen on a read,
which is what the real machine did in an aioecam session.
"""

from __future__ import annotations

import asyncio
import time

from aioecam.ecam.frame import RESPONSE, Frame
from aioecam.ecam.statistics import OPCODE_STATISTICS
from aioecam.ecam.value import decode_value, encode_value
from tests.fake_device import FakeDevice

MONITOR = "d302_monitor"
RESPONSE_PROP = "data_response"


def monitor_value(status: int, action: int = 0, progress: int = 0) -> str:
    payload = bytes([0, 0, 0, 0, 0, status, action, progress, 0, 0, 0, 0, 0])
    return encode_value(Frame(RESPONSE, 0x75, 0x0F, payload))


class FakeMachine:
    def __init__(self, device: FakeDevice, *, statistics: dict[int, int] | None = None) -> None:
        self.device = device
        self.statistics = dict(sorted((statistics or {}).items()))
        self.status = 0
        self.step = 0.05  # seconds between status changes
        self.ignore_wakeup = False  # e.g. module without cloud after a long standby
        self.max_a2_page = 10  # firmware that times out on bigger pages answers nothing
        self.received: list[Frame] = []
        self.push_changes = False
        # M7: write session, settings, profiles, recipes (lodzen's Soul captures)
        self.session_at: float | None = None
        self.require_session = True
        self.settings = {0x3E: 2, 0x32: 1}
        self.profile = 1
        self.profile_names_value: str | None = None
        self.recipes: dict[tuple[int, int], str] = {}
        self.monitor_requests = 0
        device.properties[MONITOR] = monitor_value(0, action=2, progress=100)
        device.on_write = self._on_write

    async def _set(self, status: int, action: int = 0, progress: int = 0) -> None:
        self.status = status
        self.device.properties[MONITOR] = monitor_value(status, action, progress)
        if self.push_changes:
            await self.device.push(MONITOR, self.device.properties[MONITOR])

    def _session_open(self) -> bool:
        return not self.require_session or (
            self.session_at is not None and time.time() - self.session_at < 300
        )

    async def _respond(self, opcode: int, flags: int, payload: bytes) -> None:
        await self.device.push(RESPONSE_PROP, encode_value(Frame(RESPONSE, opcode, flags, payload)))

    async def _on_write(self, name: str, value: str) -> None:
        if name == "device_connected":
            assert value.isdigit(), f"session announce must be a bare timestamp, got {value!r}"
            self.session_at = time.time()
            return
        if name != "data_request":
            return
        frame = decode_value(value).frame
        self.received.append(frame)
        if frame.opcode == 0x84 and frame.payload == b"\x02\x01":
            if self.ignore_wakeup or self.status != 0:
                return
            await self._set(1)
            await asyncio.sleep(self.step)
            await self._set(7)
        elif frame.opcode == 0x84 and frame.payload == b"\x01\x01":
            await self._set(2)
            await asyncio.sleep(self.step)
            await self._set(0)
        elif frame.opcode == 0x83 and self.status == 7:
            for action, progress in ((4, 0), (8, 5), (11, 50), (11, 90)):
                await self._set(7, action, progress)
                await asyncio.sleep(self.step)
            await self._set(7)
        elif frame.opcode == OPCODE_STATISTICS:
            await self._answer_statistics(frame)
        elif frame.opcode == 0x75 and not frame.payload:
            self.monitor_requests += 1
            await self.device.push(MONITOR, self.device.properties[MONITOR])
        elif frame.opcode == 0x95:
            param = int.from_bytes(frame.payload[:2], "big")
            value = self.settings.get(param, 0)
            # Measured on the Soul: republished into its own property, not data_response.
            reply = encode_value(
                Frame(RESPONSE, 0x95, 0xF0, frame.payload[:2] + value.to_bytes(4, "big"))
            )
            await self.device.push(f"d{param:03d}_mchn_sett", reply)
        elif frame.opcode == 0x90:
            param = int.from_bytes(frame.payload[:2], "big")
            if self._session_open():  # without a session the write is silently dropped
                self.settings[param] = int.from_bytes(frame.payload[2:6], "big")
            await self._respond(0x90, frame.flags, frame.payload[:2] + b"\x00")
        elif frame.opcode == 0xA9:
            slot = frame.payload[0]
            ok = self._session_open()
            if ok:
                self.profile = slot
            await self._respond(0xA9, 0xF0, bytes([slot, 0 if ok else 1]))
        elif frame.opcode == 0xA4 and self.profile_names_value:
            await self.device.push(RESPONSE_PROP, self.profile_names_value)
        elif frame.opcode == 0xA6:
            key = (frame.payload[0], frame.payload[1])
            if key in self.recipes:  # republished, e.g. d039_1_rec_espresso
                await self.device.push(f"d0xx_{key[0]}_rec_{key[1]}", self.recipes[key])

    async def _answer_statistics(self, request: Frame) -> None:
        start = int.from_bytes(request.payload[:2], "big")
        count = request.payload[2]
        if count > self.max_a2_page:
            return  # silently too slow, like the real firmware
        ids = [i for i in self.statistics if i >= start][:count]
        payload = b"".join(
            i.to_bytes(2, "big") + self.statistics[i].to_bytes(4, "big") for i in ids
        )
        reply = encode_value(Frame(RESPONSE, OPCODE_STATISTICS, 0x0F, payload))
        await self.device.push(RESPONSE_PROP, reply)
