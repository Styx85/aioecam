"""A simulated ECAM machine behind a FakeDevice.

Status sequences follow what an ECAM610.75 did on 2026-10-18:
wake 0 -> 1 -> 7, standby 7 -> 2 -> 0. While off it pushes nothing and the
module keeps answering with the frozen last frame.
"""

from __future__ import annotations

import asyncio

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
        device.properties[MONITOR] = monitor_value(0, action=2, progress=100)
        device.on_write = self._on_write

    async def _set(self, status: int, *, push: bool = True) -> None:
        self.status = status
        self.device.properties[MONITOR] = monitor_value(status)
        if push:
            await self.device.push(MONITOR, self.device.properties[MONITOR])

    async def _on_write(self, name: str, value: str) -> None:
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
            await self._set(3)  # dispensing
            await asyncio.sleep(self.step)
            await self._set(7)
        elif frame.opcode == OPCODE_STATISTICS:
            await self._answer_statistics(frame)

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
