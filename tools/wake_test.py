"""Wake the machine from standby over LAN only and record every stage.

Variants (one per run, each run is a fresh LAN session):

  plain     POST local_reg, then the 0x84 wake frame (what aioecam does)
  announce  as plain, but write device_connected (unix time) first, as the
            Coffee Link app does when it connects to a Soul
  dsn       as plain, but open the session with POST local_reg.json?dsn=<DSN>,
            as the Ayla SDK in the app does

The cloud is never used, whatever ~/.config/aioecam.json contains. Secrets
are never printed; the DSN is shortened. A JSON report is written next to
the output so runs can be compared.

    python tools/wake_test.py plain
    python tools/wake_test.py announce --wait 90
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp
from yarl import URL

from aioecam.device import EcamDevice
from aioecam.discovery import probe_module
from aioecam.ecam import commands
from aioecam.ecam.profile import load_profile
from aioecam.exceptions import EcamError
from aioecam.lan import link as link_module
from aioecam.lan.server import LanServer

T0 = time.monotonic()
EVENTS: list[dict[str, Any]] = []


def log(stage: str, **data: Any) -> None:
    event = {"t": round(time.monotonic() - T0, 2), "clock": time.strftime("%H:%M:%S"),
             "stage": stage, **data}  # fmt: skip
    EVENTS.append(event)
    extra = "  ".join(f"{k}={v}" for k, v in data.items())
    print(f"{event['t']:7.2f}s  {event['clock']}  {stage:<22} {extra}", flush=True)


def short(dsn: str | None) -> str:
    return f"{dsn[:6]}…{dsn[-3:]}" if dsn else "-"


def patch_link(variant: str, dsn: str) -> None:
    """Instrument the link: log local_reg calls and command polls; add ?dsn if asked."""
    original_reg = link_module.LanLink._local_reg
    original_poll = link_module.LanLink.handle_commands_poll

    async def local_reg(self: Any, method: str) -> bool:
        url = self._url
        if variant == "dsn" and method == "POST":
            self._url = str(URL(url).with_query(dsn=dsn))
        try:
            ok = await original_reg(self, method)
        finally:
            self._url = url
        log(f"local_reg {method}", http=self.stats.last_registration_status,
            dsn_param=variant == "dsn" and method == "POST")  # fmt: skip
        return ok

    def commands_poll(self: Any) -> dict[str, Any]:
        pending = len(self._outgoing)
        result = original_poll(self)
        if pending:
            log("module fetched command", queued_before=pending)
        return result

    link_module.LanLink._local_reg = local_reg  # type: ignore[method-assign]
    link_module.LanLink.handle_commands_poll = commands_poll  # type: ignore[method-assign]


async def run(args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    device_ip = args.device or config["device_ip"]
    profile = load_profile(config.get("profile") or "ecam61x")
    result: dict[str, Any] = {"variant": args.variant, "started": time.strftime("%F %T"),
                              "levels": {}}  # fmt: skip
    async with aiohttp.ClientSession() as http:
        try:
            module = await probe_module(http, device_ip, port=args.device_port)
        except (aiohttp.ClientError, TimeoutError) as err:
            log("module unreachable", error=type(err).__name__)
            result["levels"]["module_reachable"] = False
            return result
        log("regtoken.json", dsn=short(module.dsn), lan_ready=module.lan_ready)
        result["levels"]["module_reachable"] = True
        result["lan_ready"] = module.lan_ready
        if not module.lan_ready:
            log("abort", reason="module reports no DSN: it refuses LAN mode")
            return result
        assert module.dsn
        patch_link(args.variant, module.dsn)
        async with LanServer(http, port=args.port) as server:
            link = server.add_link(device_ip=device_ip, lan_key=config["lan_key"],
                                   dsn=module.dsn, key_id=config.get("key_id"),
                                   device_port=args.device_port)  # fmt: skip
            link.on_state(lambda s: log("link state", state=str(s)))
            ecam = EcamDevice(link, profile, cloud=None)  # LAN only, on purpose
            try:
                await link.wait_active(args.session_timeout)
            except (EcamError, TimeoutError):
                log("no LAN session", http=link.stats.last_registration_status,
                    key_exchanges=link.stats.key_exchanges)  # fmt: skip
                result["levels"]["session_active"] = False
                return result
            result["levels"]["session_active"] = True
            before = await ecam.refresh()
            log("monitor before", status=before.status, action=before.action,
                payload_ts=before.timestamp)  # fmt: skip
            result["before"] = {"status": before.status, "payload_ts": before.timestamp}
            if before.status != 0 and not args.force:
                log("abort", reason="machine is not in standby (status != 0)")
                return result
            if args.variant == "announce":
                await ecam.announce_session(force=True)
                log("device_connected written")
                await asyncio.sleep(1.0)
            try:
                await ecam.send(commands.power_on(), timeout=10)
            except (EcamError, TimeoutError) as err:
                log("wake frame not fetched", error=type(err).__name__)
                result["levels"]["command_fetched"] = False
                return result
            sent = time.monotonic()
            log("wake frame fetched by module")
            result["levels"]["command_fetched"] = True
            last = (before.status, before.action, before.timestamp)
            woke_after: float | None = None
            while time.monotonic() - sent < args.wait:
                try:
                    # Offline the module only publishes a new monitor frame on
                    # request (0x75); it lands shortly after the request returns.
                    await ecam.send(commands.monitor_request(), timeout=5)
                    await asyncio.sleep(2.0)
                    state = await ecam.refresh()
                except EcamError as err:
                    log("monitor read failed", error=type(err).__name__)
                    continue
                now = (state.status, state.action, state.timestamp)
                if now != last:
                    log("monitor changed", status=state.status, action=state.action,
                        progress=state.progress, payload_ts=state.timestamp)  # fmt: skip
                    last = now
                if state.status != 0 and woke_after is None:
                    woke_after = round(time.monotonic() - sent, 1)
                    log("machine reacts", seconds_after_fetch=woke_after)
                    if not args.watch_until_ready:
                        break
                if state.status == 7 and args.watch_until_ready:
                    log("ready")
                    break
            result["levels"]["machine_reacted"] = woke_after is not None
            result["seconds_to_reaction"] = woke_after
            if woke_after is None:
                log("no reaction", waited_s=args.wait)
            s = link.stats
            result["link"] = {"registrations": s.registrations, "key_exchanges": s.key_exchanges,
                              "liveness_failures": s.liveness_failures}  # fmt: skip
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("variant", choices=["plain", "announce", "dsn"])
    parser.add_argument("--key-file", default="~/.config/aioecam.json")
    parser.add_argument("--device", help="machine IP (default: from the key file)")
    parser.add_argument("--wait", type=float, default=60.0, help="seconds to wait for a reaction")
    parser.add_argument("--watch-until-ready", action="store_true")
    parser.add_argument("--session-timeout", type=float, default=15.0)
    parser.add_argument("--force", action="store_true", help="run even if not in standby")
    parser.add_argument("--report-dir", default=".")
    parser.add_argument("--port", type=int, default=10280, help=argparse.SUPPRESS)
    parser.add_argument("--device-port", type=int, default=80, help=argparse.SUPPRESS)
    args = parser.parse_args()
    config = json.loads(Path(args.key_file).expanduser().read_text())
    result = asyncio.run(run(args, config))
    result["events"] = EVENTS
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = Path(args.report_dir) / f"wake-{args.variant}-{stamp}.json"
    out.write_text(json.dumps(result, indent=2))
    levels = result["levels"]
    print("\nresult:", ", ".join(f"{k}={'yes' if v else 'no'}" for k, v in levels.items()))
    print(f"report: {out}")
    sys.exit(0 if levels.get("machine_reacted") else 1)


if __name__ == "__main__":
    main()
