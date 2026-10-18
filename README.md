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

> Status: pre-alpha. See [docs/plan.md](docs/plan.md) for architecture and
> milestones. The protocol findings this library is built on are documented
> publicly in [ecam-lan-protocol.md](https://github.com/Styx85/cremalink-ha/blob/main/docs/ecam-lan-protocol.md).

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
