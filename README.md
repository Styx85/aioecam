# aioecam

Async, local-first Python client for De'Longhi ECAM coffee machines with
Coffee Link Wi-Fi (Ayla module), e.g. PrimaDonna Soul (ECAM610/612) and
Eletta Explore (ECAM452).

- **Local first:** talks to the machine directly over Ayla LAN mode; no add-on,
  no extra server process.
- **Cloud second:** the De'Longhi/Ayla cloud is only used to fetch the LAN key
  during onboarding and, optionally, as a fallback for waking the machine.
- **Pure asyncio**, dependencies limited to `aiohttp` and `cryptography`, built
  to be embedded in Home Assistant.

> Status: beta. See [docs/plan.md](docs/plan.md) for architecture and
> milestones. The protocol findings this library is built on are documented
> publicly in [ecam-lan-protocol.md](https://github.com/Styx85/cremaevolution-ha/blob/main/docs/ecam-lan-protocol.md).

## Hardware validation

Live testing has been performed on a **PrimaDonna Soul ECAM610.75**. Verified
operations include LAN wake-up, settings and recipe reads, tank and milk
accessory monitoring while awake, auto-off writes with restoration, profile
switching, and a one-off 100 ml hot-water command.

**Other models have not been tested on hardware by this project.** This
includes other ECAM610/612/613 variants and the Eletta Explore (ECAM452).
Their coverage is limited to codec, captured-frame and simulated-device tests;
passing these tests does not confirm that they work on a real machine.
Model-specific behaviour needs confirmation from owners of those machines.
Eletta write sessions are not implemented.

Not every code path has been verified live, even on the ECAM610.75: the final
recipe-polling fix is covered by simulated-device tests, and whether monitor
changes require polling remains open. Detailed results and limitations are
documented in the [M7 protocol notes](docs/m7-protocol.md).

## Command line

```bash
python -m aioecam --device 192.168.1.50 status --watch 30
python -m aioecam --device 192.168.1.50 on          # confirmed by the monitor
python -m aioecam --device 192.168.1.50 brew espresso
python -m aioecam --device 192.168.1.50 stats       # full A2 table
```

The LAN key comes from `~/.config/aioecam.json` (`{"lan_key": "..."}`) or
`AIOECAM_LAN_KEY`.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check . && .venv/bin/mypy && .venv/bin/pytest
```

## Credits

Protocol knowledge builds on [cremalink](https://github.com/miditkl/cremalink)
(AGPL-3.0) and the public protocol write-ups of the De'Longhi community.
Not affiliated with De'Longhi or Ayla Networks.

## License

AGPL-3.0-or-later

### Passive catalogue and receipt diagnostics

`EcamDevice.catalog.snapshot()` reports only catalogue properties already received
on the LAN link. It sends no reads, writes or session announcements. Descriptor
(B0 F0), recipe (A6 F0), priority (A8 F0) and name-metadata frames are checked for
length, CRC and payload structure. Unknown parameter tags stay numeric; priority
lists do not establish availability. Source properties, reception/last-valid times
and validation errors are included. Bad updates retain the last valid data with
an explicit error. Reversed min/max bounds remain errors. Raw defaults outside those bounds are
preserved with `default_in_range: false`; their meaning remains unconfirmed.
Storage is bounded to 512 properties and resets with the device object.

Household names are redacted at parse time; serial, PIN and settings frames are
excluded. The catalogue does not enable drinks or change brew validation.
`last_datapoint_at`, `last_monitor_at` and `monitor_query_*` distinguish receipt,
state changes and the last monitor-read outcome. Repeated valid frames count as
new receipts. Successful reads may still return cached standby data; reception
time does not prove a new physical state. `machine_info` keeps only observed,
allowlisted model/firmware properties. Missing metadata remains unknown.
