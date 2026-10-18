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
> milestones.

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
