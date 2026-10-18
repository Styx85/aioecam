"""Command line tool: ``python -m aioecam --device IP <command>``.

``onboard --email you@example.com --save`` logs in (password prompted, not
echoed), finds the machine and stores LAN key, IP and refresh token in
``~/.config/aioecam.json`` (mode 600). Alternatively provide
``{"lan_key": "..."}`` there or ``AIOECAM_LAN_KEY``. Secrets are never printed.
With a refresh token stored, ``on`` falls back to the cloud if the local
wake-up has no effect. Stop other LAN clients first: the machine keeps only one
LAN registration.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp

from aioecam.cloud.api import AylaCloud
from aioecam.cloud.onboarding import onboard
from aioecam.cloud.region import load_region
from aioecam.device import EcamDevice
from aioecam.discovery import probe_module
from aioecam.ecam.monitor import MachineState
from aioecam.ecam.profile import available_families, load_profile
from aioecam.exceptions import EcamError
from aioecam.lan.server import LanServer

DEFAULT_KEY_FILE = Path("~/.config/aioecam.json")


def _config(path: Path) -> dict[str, Any]:
    file = path.expanduser()
    data: dict[str, Any] = json.loads(file.read_text()) if file.exists() else {}
    if key := os.environ.get("AIOECAM_LAN_KEY"):
        data["lan_key"] = key
    if not isinstance(data.get("lan_key"), str) or not data["lan_key"]:
        sys.exit(
            f"no LAN key: run `onboard --save`, set AIOECAM_LAN_KEY or create {file} "
            'with {"lan_key": "..."}'
        )
    return data


def _save_private(path: Path, data: dict[str, Any]) -> None:
    """Write a JSON file readable only by the user (it holds a LAN key and a token)."""
    file = path.expanduser()
    file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)
    os.chmod(file, 0o600)  # also tighten a file that existed before


async def _onboard(args: argparse.Namespace) -> None:
    password = getpass.getpass(f"Coffee Link password for {args.email}: ")
    async with aiohttp.ClientSession() as http:
        result = await onboard(http, args.email, password)
    if not result.machines:
        sys.exit("no coffee machines on this account")
    for m in result.machines:
        d = m.device
        print(
            f"{d.dsn}  {d.name or '?'}  oem={d.oem_model}  profile={m.profile_family or '?'}  "
            f"lan={'yes' if d.lan_enabled else 'no'}  ip={d.lan_ip or '-'}  "
            f"lan_key={'found' if m.lan_key else 'missing'}  {d.connection_status or ''}"
        )
    if not args.save:
        return
    candidates = [
        m for m in result.machines if m.lan_key and (not args.dsn or m.device.dsn == args.dsn)
    ]
    if len(candidates) != 1:
        sys.exit("--save needs exactly one machine with a LAN key; choose one with --dsn")
    m = candidates[0]
    _save_private(
        args.key_file,
        {
            "dsn": m.device.dsn,
            "device_ip": m.device.lan_ip,
            "profile": m.profile_family,
            "lan_key": m.lan_key,
            "key_id": m.key_id,
            "refresh_token": result.refresh_token,
        },
    )
    print(f"saved to {args.key_file.expanduser()} (mode 600)")


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
    if args.command == "onboard":
        await _onboard(args)
        return
    if args.command == "beverages":  # offline
        for name, stored in sorted(load_profile(args.profile or "ecam61x").commands.items()):
            print(f"{name:<22} {stored.label}")
        return
    config = _config(args.key_file)  # fail before touching the network
    device_ip = args.device or config.get("device_ip")
    if not device_ip:
        sys.exit("no machine address: pass --device or run `onboard --save`")
    profile = load_profile(args.profile or config.get("profile") or "ecam61x")
    async with aiohttp.ClientSession() as http:
        module = await probe_module(http, device_ip)
        if not module.lan_ready:
            sys.exit(
                "the Wi-Fi module reports no DSN and refuses LAN mode; it needs to reach "
                "the De'Longhi cloud once after a restart"
            )
        assert module.dsn
        cloud = None
        if config.get("refresh_token"):
            # Enables the cloud wake fallback; keep the rotating token current on disk.
            def persist(token: str) -> None:
                _save_private(args.key_file, {**config, "refresh_token": token})

            cloud = AylaCloud(http, load_region("eu"), config["refresh_token"],
                              on_refresh_token=persist)  # fmt: skip
        async with LanServer(http, port=args.port) as server:
            link = server.add_link(device_ip=device_ip, lan_key=config["lan_key"], dsn=module.dsn,
                                   key_id=config.get("key_id"))  # fmt: skip
            ecam = EcamDevice(link, profile, cloud=cloud)
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
        if ecam.cloud_fallbacks:
            print("(the local wake-up had no effect; woken via the cloud)")
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
    p.add_argument("--device", help="machine IP address (default: from the key file)")
    p.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    p.add_argument("--profile", choices=available_families(), help="default: from the key file")
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
    ob = sub.add_parser("onboard", help="log in to Coffee Link, list machines, fetch LAN keys")
    ob.add_argument("--email", required=True)
    ob.add_argument("--save", action="store_true", help="store key, IP and token in --key-file")
    ob.add_argument("--dsn", help="which machine to save if the account has several")
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
