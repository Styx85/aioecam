"""Read-only live check against a real machine. Sends no machine commands.

Connects through LanServer/LanLink, reads the monitor once, then keeps the
session alive for --seconds and reports re-keys and liveness failures.

    python examples/live_read.py --device 192.168.178.51 --key-file ~/.config/cremalink-probe.json

Stop any other LAN client (e.g. the Cremalink add-on) first: the machine keeps
only one LAN registration.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
import urllib.request
from pathlib import Path

from aioecam.ecam.monitor import MachineState
from aioecam.ecam.profile import load_profile
from aioecam.lan.server import LanServer


def read_dsn(device_ip: str) -> str:
    """The module answers regtoken.json without authentication; host_symname is the DSN."""
    with urllib.request.urlopen(f"http://{device_ip}/regtoken.json", timeout=5) as r:
        dsn: str = json.load(r)["host_symname"]
        return dsn


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--device", required=True)
    p.add_argument("--key-file", type=Path, default=Path("~/.config/cremalink-probe.json"))
    p.add_argument("--port", type=int, default=10290)
    p.add_argument("--seconds", type=float, default=90)
    p.add_argument("--profile", default="ecam61x")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    lan_key = json.loads(args.key_file.expanduser().read_text())["lan_key"]
    dsn = await asyncio.to_thread(read_dsn, args.device)
    profile = load_profile(args.profile)

    def show(prefix: str, value: str) -> None:
        state = MachineState.from_value(value)
        print(
            f"{time.strftime('%H:%M:%S')}  {prefix}: {profile.status_name(state)} "
            f"(status {state.status}, action {state.action}, progress {state.progress})"
        )

    async with LanServer(port=args.port) as server:
        link = server.add_link(device_ip=args.device, lan_key=lan_key, dsn=dsn)
        link.subscribe(lambda p: show("push", p.value), name=profile.monitor_property)
        t0 = time.monotonic()
        await link.wait_active(10)
        print(f"session active after {time.monotonic() - t0:.2f}s")
        show("read", (await link.read(profile.monitor_property, timeout=5)).value)
        await asyncio.sleep(args.seconds)
        s = link.stats
        print(
            f"after {args.seconds:.0f}s: state={link.state} registrations={s.registrations} "
            f"key_exchanges={s.key_exchanges} liveness_failures={s.liveness_failures} "
            f"rejected={s.rejected_messages}"
        )


if __name__ == "__main__":
    asyncio.run(main())
