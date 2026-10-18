"""Command line tool: ``python -m aioecam --device IP <command>``.

The LAN key is read from a JSON file ``{"lan_key": "..."}`` (default
``~/.config/aioecam.json``) or the ``AIOECAM_LAN_KEY`` environment variable;
it is never printed. Stop other LAN clients first: the machine keeps only one
LAN registration.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

import aiohttp

from aioecam.device import EcamDevice
from aioecam.discovery import probe_module
from aioecam.ecam.monitor import MachineState
from aioecam.ecam.profile import available_families, load_profile
from aioecam.exceptions import EcamError
from aioecam.lan.server import LanServer

DEFAULT_KEY_FILE = Path("~/.config/aioecam.json")


def _lan_key(path: Path) -> str:
    if key := os.environ.get("AIOECAM_LAN_KEY"):
        return key
    file = path.expanduser()
    if not file.exists():
        sys.exit(f'no LAN key: set AIOECAM_LAN_KEY or create {file} with {{"lan_key": "..."}}')
    key = json.loads(file.read_text()).get("lan_key")
    if not isinstance(key, str) or not key:
        sys.exit(f"{file} has no lan_key")
    return key


def _describe(ecam: EcamDevice, state: MachineState) -> str:
    profile = ecam.profile
    flags = [name for name, on in profile.read_flags(state).items() if on]
    return (
        f"{time.strftime('%H:%M:%S')}  {profile.status_name(state)} "
        f"(status {state.status}, action {state.action}, progress {state.progress}%)"
        f"  accessory={profile.accessory_name(state)}"
        + (f"  flags={','.join(flags)}" if flags else "")
    )


async def _run(args: argparse.Namespace) -> None:
    profile = load_profile(args.profile)
    if args.command == "beverages":  # offline
        for name, stored in sorted(profile.commands.items()):
            print(f"{name:<22} {stored.label}")
        return
    lan_key = _lan_key(args.key_file)  # fail before touching the network
    async with aiohttp.ClientSession() as http:
        module = await probe_module(http, args.device)
        if not module.lan_ready:
            sys.exit(
                "the Wi-Fi module reports no DSN and refuses LAN mode; it needs to reach "
                "the De'Longhi cloud once after a restart"
            )
        assert module.dsn
        async with LanServer(http, port=args.port) as server:
            link = server.add_link(device_ip=args.device, lan_key=lan_key, dsn=module.dsn)
            ecam = EcamDevice(link, profile)
            await link.wait_active(10)
            await _command(args, ecam)


async def _command(args: argparse.Namespace, ecam: EcamDevice) -> None:
    cmd = args.command
    if cmd == "status":
        print(_describe(ecam, await ecam.refresh()))
        if args.watch:
            ecam.subscribe(lambda s: print(_describe(ecam, s)))
            await asyncio.sleep(args.watch)
            s = ecam.link.stats
            print(
                f"link: registrations={s.registrations} key_exchanges={s.key_exchanges} "
                f"liveness_failures={s.liveness_failures} rejected={s.rejected_messages}"
            )
    elif cmd == "on":
        print(_describe(ecam, await ecam.power_on()))
    elif cmd == "off":
        print(_describe(ecam, await ecam.power_off()))
    elif cmd == "brew":
        print(_describe(ecam, await ecam.brew(args.beverage)))
    elif cmd == "stop":
        await ecam.stop()
        print("stop sent")
    elif cmd == "stats":
        snap = await ecam.read_statistics(
            progress=lambda p: print(f"  page {p['page']}: {p['returned']} values", file=sys.stderr)
        )
        print(json.dumps({"known": snap.known, "unknown": snap.unknown}, indent=2))


def main() -> None:
    p = argparse.ArgumentParser(
        prog="python -m aioecam",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--device", required=True, help="machine IP address")
    p.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    p.add_argument("--profile", default="ecam61x", choices=available_families())
    p.add_argument("--port", type=int, default=10280, help="local callback port")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="read the machine state")
    status.add_argument(
        "--watch",
        type=float,
        default=0,
        metavar="SECONDS",
        help="keep the session open and print every change",
    )
    sub.add_parser("on", help="wake up (confirmed)")
    sub.add_parser("off", help="go to standby (confirmed)")
    brew = sub.add_parser("brew", help="start a stored beverage (machine must be ready)")
    brew.add_argument("beverage")
    sub.add_parser("stop", help="stop the current beverage")
    sub.add_parser("beverages", help="list stored beverages")
    sub.add_parser("stats", help="read the A2 statistics table")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    try:
        asyncio.run(_run(args))
    except EcamError as exc:
        sys.exit(f"error: {exc}")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
