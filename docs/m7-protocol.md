# M7: settings, profiles, recipes and monitor requests

This note describes the implementation on `feat/m7-ecam-commands`, the earlier
PrimaDonna Soul ECAM610.75 handoff, and the local hardware follow-up on
2026-10-18. Codec tests, fake-device tests and hardware measurements are
separate evidence.

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

The local follow-up exposed two additional behaviours:

- A profile command can leave its acknowledgement in `data_response` without
  publishing it to the client. Reading the property retrieves it. An initial
  read can still return the previous slot's acknowledgement: selecting slot 2
  produced `d007a9f001003b3c` before `d007a9f002006e6f`. Ignore acknowledgements
  for other slots instead of reporting them as rejection of this command.
- A second identical recipe query timed out after the first succeeded. For
  repeated recipe reads, remember the property that carried a matching answer
  and read that property if no answer arrives. Cache the property name, not
  the recipe value. On a new device object, the first successful answer is
  still needed to learn a recipe's property; there is no complete static
  recipe-property map. The cause of the missing second publication remains
  unproven.

Profile selection and recipe reads use bounded property polling after 0.5 s
without an answer, within the command timeout. Unrelated frames are still
filtered by opcode, profile slot and beverage ID. Acknowledgements in the
module are cached state, not a transaction nonce; a cached acknowledgement
alone cannot establish when the control board acted.

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

Recipes are scoped to a profile slot. Tag names follow the parameter enum of
the Coffee Link app 4.9.6 (static analysis, 2026-10-19): `0x00` TEMP,
`0x01` coffee, `0x02` TASTE (aroma/strength step), `0x08` double, `0x09` milk,
`0x0D` tea temperature, `0x0F` hot water, `0x18` programmable, `0x19` visible,
`0x1A` visible in programming, `0x1B` INDEX_LENGTH, `0x1C` accessory.
Preserve the recipe order and apply the app's tag filtering. The trailer is
`(profile << 2) | operation` (app enum: 0 delete, 1 save, 2 prepare,
3 prepare and save); a normal brew uses 2. Earlier versions called these
bits "accessory"; the accessory is recipe tag `0x1C`. All values are raw protocol values, not degrees
Celsius or universal display scales.

**Correction (2026-10-19):** `0x1B` was earlier documented here and exported
by aioecam as *temperature*. The app names it `INDEX_LENGTH`, a size index
(the app's size enum knows SMALL=0, MID=1, LARGE=2, NOT_SET=255; live values
such as 4 do not fit it, so the exact meaning stays open). Measurements below
that say "temperature" for `0x1B` report the raw value of that tag correctly;
only the name was wrong. Temperatures are tags `0x00` and `0x0D`.

Tag `0x19` (VISIBLE): the app hides recipes with `0` and, for normal
beverages, shows an info page instead of a brew button for `2`. On the Soul,
tea, coffee pot, mug to go and brew over ice carry `2`. This is app
behaviour, not a proven firmware lock, and `1` does not guarantee that a
beverage can be made with the installed hardware. The tests
rebuild every stored beverage frame byte for byte for both model families.

The Eletta codec includes a 22-byte profile-name entry and an `0xE8` handshake
builder. The device API does not implement Eletta's write session, and no
Eletta live validation is claimed.

## Reported live results at handoff

- Part A: auto-off raw `0` (15 min), water hardness raw `2` (level 3).
- Part A: profile 1 espresso recipe 40 ml, tag `0x1B` raw 1, intensity raw 4;
  hot-water recipe 250 ml, tag `0x1B` raw 1.
- Part B: wake succeeded, ready after approximately 35 seconds; three profiles
  were read and a monitor request completed.
- While awake: alarms `00` and accessory `none` were reported. The remaining
  physical-change and write checks were completed locally below.

## Local hardware follow-up, 2026-10-18

Times below are Europe/Berlin (UTC+02:00). The operator confirmed that the HA
integration was disabled and performed the physical changes. The existing
private LAN key worked; no new cloud login or cloud wake fallback was used.

| Check | Observation | Result / limits |
| --- | --- | --- |
| Standby monitor | Cached and requested: status `0`, accessory `4`, switches `4002`, alarms `01000000`, unchanged timestamp | A frozen prior frame, not evidence of current tank or accessory state |
| LAN wake | Connected 22:07:59, waking at 22:08:03, ready at 22:08:32 | Approximately 33 s from connection to ready; no alarm |
| Tank removal | 22:08:58: switches `1000`, alarms `00200000` | Change detected while awake |
| Tank replacement | 22:09:14: switches `0000`, alarms `00000000` | Alarm cleared |
| Milk accessory attached | 22:09:54: accessory `2` (`latte_crema_hot`), switches `0001` | Correct accessory recognised |
| Milk accessory removed | 22:10:17: accessory `0`, switches `0000` | Returned to no accessory; no alarm |
| Auto-off write | Read `0` (15 min), wrote/read back `1` (30 min) at 22:11:02 | Write confirmed by machine acknowledgement and readback; display not independently confirmed |
| Auto-off restoration | Wrote/read back `0` at 22:11:05 | Original 15-minute value restored in `finally` |
| Profile switch | Slot 2 acknowledged 22:12:56, slot 1 acknowledged 22:13:00 | Operator also confirmed the `1 → 2 → 1` sequence on the display; test script polled and matched the requested slot |
| Custom hot water | Sent `0d0d83f010010f00641b0106e5e1` at 22:16:20; dispensing status `11`; ready at 22:17:03 | One 100 ml command, no alarm; operator confirmed approximately 100 ml in the container |
| Recipe preservation | Hot-water recipe before and after: water `250`, tag `0x1B` = `1`, tag `0x19` = `1` | The one-off 100 ml command did not change the saved recipe |

The live hot-water test used the successfully read recipe directly with
`commands.brew`, `send` and monitor confirmation after `brew_custom`'s second
recipe read timed out **before** sending any beverage command. The final
recipe-polling implementation is regression-tested with the fake machine;
it has not been rerun through `brew_custom` on hardware. No additional drink
was dispensed for software verification.

The tank/accessory measurement alternated a normal monitor-property read
with an explicit monitor request, about every 2–3 seconds. These results prove
that awake changes are observable with polling. They do **not** establish
whether changes would be pushed unsolicited, nor whether explicit monitor
requests are necessary compared with plain property reads. Those comparisons
remain open.

For this Soul, hot water comes from the central coffee/hot-water outlet
(`A14`); an attachable hot-water nozzle is not required. Accessory `0` must
not block hot water. The trailer's low bits are the operation mode
(prepare = 2), not an accessory code, and unrelated to the monitor's accessory
enum. See the
manufacturer's [manual, sections 2.1 and 13](https://dls.delonghigroup.com/files/docs/5/EN-57132C8859.pdf).

The test LAN server was closed after completion. The operator was told to
reactivate HA; HA reactivation and subsequent HA state delivery have not been
verified here.

### Follow-up: hot-water A2 counter correction

After the test, the operator confirmed that the statistic labelled "Tea" had
increased from 2 to 3. The fresh HA A2 snapshot at
`2026-10-18T20:37:21.305742+00:00` contained `3018 = 0` and `3019 = 3`.
The tested machine has no tea function according to the operator, and its
stored `hot_water` command uses beverage ID `0x10`; the profile contains no
separate tea command. The inherited mapping of `3018` to hot water and
`3019` to tea was therefore incorrect for this hardware.

From version `0.1.0b4`, `3019` maps to `hot_water`; no `tea` statistic is
invented, and `3018` is preserved as unknown/raw. The client did not collect
a pre-brew A2 snapshot: the 2 → 3 delta is operator evidence rather than a
saved pair of client snapshots. Other models still need owner confirmation.

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
