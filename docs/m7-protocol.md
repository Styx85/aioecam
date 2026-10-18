# M7: settings, profiles, recipes and monitor requests

This note describes the implementation on `feat/m7-ecam-commands` and the
handoff from the earlier PrimaDonna Soul ECAM610.75 test session. The live
results below were reported in that handoff; they have not been repeated in
this cloud environment. Codec tests and fake-device tests are separate evidence.

## Answer routing

The Soul does not send every command answer through `data_response`:

| Operation | Opcode | Reported answer property |
| --- | --- | --- |
| Statistics | `0xA2` | `data_response` |
| Read setting | `0x95` | Setting property, e.g. `d282_mchn_sett_aoff` |
| Read recipe | `0xA6` | Recipe property, e.g. `d039_1_rec_espresso` |

Decode frame-carrying datapoints from every property, verify the frame and its
`0xD0` response direction, then match the opcode and operation identifiers.
Ignore non-frame properties and `0x0D` request echoes. Monitor state still
comes only from the model's monitor property. An `0xE1` response means the
machine aborted a request while busy; it is not a successful answer.

Settings and recipes were readable while the machine was in standby even
though the monitor value remained unchanged. A live LAN session therefore
does not imply that the control board produces current monitor data.

## Implemented operations

| Request | Meaning | Confirmation / parsing |
| --- | --- | --- |
| `0x75 / 0x0F` | Request monitor | Read monitor property after sending |
| `0xA4` | Read profile names | One-based range; UTF-16BE names |
| `0xA9` | Select profile | Slot and status in acknowledgement |
| `0x95` | Read parameter | Two-byte parameter ID followed by four-byte values |
| `0x90` | Write parameter | Parameter acknowledgement, then read back |
| `0xA6` | Read recipe | Match profile slot and beverage ID; decode TLV |
| `0x83` | Start parameterised beverage | Read stored recipe, override known tags, confirm busy state |

Soul write operations announce a session by writing a bare Unix timestamp to
`device_connected`, without a trailing newline. The implementation refreshes
that announcement after 240 seconds. The upstream implementation reports a
session lifetime of approximately 300 seconds; session expiry and renewal
have not been measured here.

Known settings:

- Auto-off: parameter `0x003E`, values `0/1/2/3` = 15 min / 30 min / 1 h / 3 h.
- Water hardness: parameter `0x0032`, values `0..3` = displayed levels 1..4.

Recipes are scoped to a profile slot. Known tags include coffee volume
(`0x01`), intensity (`0x02`), milk (`0x09`), water volume (`0x0F`) and
temperature (`0x1B`). Preserve the recipe order, apply the app's tag filtering,
and retain the model's accessory bits. Temperature and intensity are raw
protocol values, not degrees Celsius or universal display scales. The tests
rebuild every stored beverage frame byte for byte for both model families.

The Eletta codec includes a 22-byte profile-name entry and an `0xE8` handshake
builder. The device API does not implement Eletta's write session, and no
Eletta live validation is claimed.

## Reported live results at handoff

- Part A: auto-off raw `0` (15 min), water hardness raw `2` (level 3).
- Part A: profile 1 espresso recipe 40 ml, temperature raw 1, intensity raw 4;
  hot-water recipe 250 ml, temperature raw 1.
- Part B: wake succeeded, ready after approximately 35 seconds; three profiles
  were read and a monitor request completed.
- While awake: alarms `00` and accessory `none` were reported. Changes caused
  by moving the tank and milk accessory remain unverified.
- Part C setting writes, profile switching and custom-volume hot water remain
  unverified on hardware.

## Resume the live checks

Run from the LAN test host with the existing private key file and the machine
able to reach the callback listener. Stop the HA integration and other LAN
clients for the duration; checking port 10280 is useful evidence but does not
by itself prove that the HA config entry is unloaded. Confirm its state in HA.
Do not infer the current machine state from the earlier handoff.

1. Read status, settings and profile-1 recipes again. The CLI commands are
   `status`, `settings`, `recipe espresso --slot 1`, and
   `recipe hot_water --slot 1`. Supply the correct `--device` and `--key-file`.
2. For Part B, compare `monitor` output before and after each physical tank or
   accessory change. Record cached and requested frames, status, action,
   alarms and accessory. The `monitor` command requests a frame, but its
   subsequent property read alone does not prove the value was regenerated.
   An unchanged value is a result to investigate, not a passing freshness test.
3. For Part C, read and record auto-off, choose a different supported value,
   write it and verify the display and readback. Restore the recorded value
   in a `finally` block and verify restoration separately, even on failure.
4. Record the current active profile from the display (the profile-name list
   does not identify the active one). Select profile 2, verify the display,
   then restore the recorded profile and verify it.
5. With a suitable container and the operator present, use
   `EcamDevice.brew_custom("hot_water", {TAG_WATER_ML: requested_ml}, slot=1)`.
   Choose the volume with the operator before sending it. The CLI currently
   exposes stored beverages only. Confirm start, completion and actual output;
   monitor busy confirmation alone does not establish the delivered volume.
6. Restore changed settings/profile, close the test LAN server, and reactivate
   the HA integration. Check that HA receives current state again. Record any
   restoration failure explicitly.

## Sources

The ECAM extensions were ported from [lodzen/cremalink](https://github.com/lodzen/cremalink)
(AGPL-3.0); its recipe/TLV grammar credits actabi's `delonghi_coffeelink` (MIT).
The reported upstream discussion is [lodzen/cremalink#4](https://github.com/lodzen/cremalink/issues/4).
See [the protocol notes](https://github.com/Styx85/cremalink-ha/blob/main/docs/ecam-lan-protocol.md)
for the earlier LAN measurements.
