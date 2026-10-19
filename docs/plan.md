# aioecam – Architektur- und Umsetzungsplan

Stand: 2026-10-18 (M0–M4 erledigt) · Ziel: eigene async Python-Library für De'Longhi ECAM
(Coffee Link / Ayla) plus eine HACS-Integration **ohne Add-on**. Local first,
Cloud nur für Onboarding und als Fallback.

---

## 1. Was die Tests vom 18.10. für das Design festlegen

Gemessen an einer PrimaDonna Soul ECAM610.75 (192.168.178.51) mit
`tools/lan_probe.py`:

| Befund | Messung | Konsequenz |
|---|---|---|
| Erstregistrierung per **POST**, Refresh per **PUT** | Bei laufender Session: POST 10/10 neuer Key-Exchange, PUT 0/10 | Nie Keys verwerfen und auf ein PUT hoffen. Neue Session immer per POST. |
| Request-Header egal | minimal und httpx-Stil identisch | Kein Raw-Socket-Hack nötig, aiohttp-Client reicht |
| Session ohne WAN stabil | 15 min WAN gesperrt: 16/16 Sessions, 20 Monitor-Reads | LAN ist vollwertiger Primärkanal |
| Ausschalten ohne WAN | Status 7 → 2 → 0 | lokal |
| Wecken ohne WAN | 2026-10-19: 3× ok in ~2 s (5–13 min Standby); der frühere Fehlschlag war vermutlich nur eine verpasste Bestätigung (Modul meldet offline nur auf 0x75-Anfrage) | lokal wecken, per 0x75 auf frischen Frame prüfen, optional Cloud-Fallback |
| Maschine pusht fremde Properties | `app_device_connected`, `data_request`-Echos kommen als Datapoint | Datapoints **nach Property-Namen** routen (cremalink-Bug) |
| Standby-Monitor ist eingefroren | identischer Frame inkl. Timestamp, solange die Platine schläft | "unverändert" ≠ "Verbindung tot"; Health über Session, nicht über Monitor-Änderung |
| Wassertank-Bit unzuverlässig (Soul) | `alarms[0]` Bit 0 = 1 bei vollem Tank | Profile pro Modell, unsichere Flags weglassen (vgl. cremalink#250) |
| Handshake schnell | Key-Exchange + erster Monitor < 0,2 s | aggressive Timeouts möglich (5–10 s) |

Ursache der "hohlen Sessions" in cremalink: `rekey_job` verwirft alle 60 s die
Keys und registriert per PUT; die Maschine ignoriert das und pollt mit der alten
Session weiter → keine Monitor-Daten, Kommandos → 502.

---

## 2. Prinzipien

1. **Eine Verantwortung pro Schicht:** Codec ↔ Transport ↔ Gerät ↔ HA strikt getrennt.
2. **Pure asyncio**, keine Threads, keine Blocking-Calls (HA-tauglich).
3. **Push statt Poll:** Die Maschine liefert Datapoints, wir verteilen Events.
4. **Jede Aktion wird bestätigt:** Ein Kommando gilt erst als erfolgreich, wenn der Monitor die Zustandsänderung zeigt (oder es kommt ein klarer Fehler).
5. **Minimale Abhängigkeiten:** `aiohttp` + `cryptography` (beide bereits in HA), kein pycryptodome, kein FastAPI.
6. **Testbar ohne Hardware:** Fake-Device, das die Geräteseite des LAN-Protokolls spricht, plus echte Frames als Fixtures.

---

## 3. Architektur der Library

```
aioecam/
  ecam/                  # reines Maschinenprotokoll, keine I/O
    frame.py             # Frame bauen/parsen, CRC-16 (init 0x1D0F)
    commands.py          # power_on/off, stop, brew(beverage, params), monitor_refresh, A2-Requests
    monitor.py           # DATA2 → MachineState (status, action, progress, alarms, accessory)
    statistics.py        # A2-Tabelle lesen/interpretieren (Port aus eigenem Fork-Code)
    profiles/            # JSON pro Modellfamilie: Property-Namen, Status-/Action-Namen, Alarmbits, Getränke
      ecam610.json  ecam612.json  ecam452.json
    models.py            # Seriennummer → Modell (d270_serialnumber[1:6])
  lan/                   # Ayla LAN mode
    crypto.py            # Key-Ableitung, verkettete CBC-Streams, HMAC (Rollen app/device)
    server.py            # EIN aiohttp-Server für alle Maschinen (Routing per Quell-IP + key_id)
    link.py              # Zustandsmaschine pro Maschine (siehe 3.1)
    messages.py          # property-set, property-get (cmds), datapoint, ack
  cloud/                 # optional
    auth.py              # Gigya → Ayla Token, Refresh, Regionen EU/US/CN
    api.py               # Geräteliste, LAN-Config (lanip_key, key_id, IP), Properties, Datapoint schreiben
  device.py              # Fassade: EcamDevice
  discovery.py           # regtoken.json-Probe (DSN ohne Login), später DHCP-Hinweise
  exceptions.py          # EcamError → NotConnected, CommandTimeout, CommandRejected, AuthError …
tests/
  fake_device.py         # spielt die Geräteseite (gleiche crypto.py, Rolle "device")
  fixtures/frames/       # echte Monitor-Frames aus den Probe-Logs
```

### 3.1 LAN-Link: Zustandsmaschine pro Maschine

```
IDLE ──POST local_reg──▶ REGISTERING ──key_exchange──▶ ACTIVE
  ▲                           │ timeout 5 s                 │
  └──── backoff (2,4,8…60 s) ◀┘                             │
ACTIVE: PUT local_reg (notify=0) alle 20 s als Keep-alive
        PUT local_reg (notify=1) wenn Kommandos warten
        keine Polls/Datapoints > 45 s  ──▶ IDLE (neu per POST)
        neuer key_exchange (z. B. Maschine startet neu) ──▶ neue Session übernehmen
```

- **Kein periodischer Zwangs-Rekey.** Neuer Key-Exchange nur, wenn die Maschine ihn anstößt oder die Session tot ist (dann POST).
- Kommandos in einer Queue mit `asyncio.Future`; `commands.json` liefert immer genau ein Element oder ein leeres Paket.
- Datapoints werden nach `name` an Abonnenten verteilt (`monitor`, `data_response`, `app_device_connected`, …). Unbekannte Properties werden geloggt und nicht als Monitor interpretiert.
- Antworten an das ESP32 bewusst simpel halten: `Content-Length`, kein chunked encoding, kein Keep-alive-Zwang (mit aiohttp verifizieren, siehe Risiken).
- Konflikterkennung: Häufen sich fremde Key-Exchanges oder bricht die Session ständig ab, wird ein Diagnose-Event gemeldet (z. B. wenn die Coffee-Link-App im LAN-Modus den Slot belegt).

### 3.2 Geräte-Fassade (öffentliche API, Skizze)

```python
async with LanServer(port=10280) as server:            # einmal pro Prozess
    dev = EcamDevice(server, ip="192.168.178.51", lan_key=..., profile="ecam610",
                     cloud=CloudFallback(...) | None)
    await dev.connect()
    dev.subscribe(lambda state: ...)                     # MachineState bei jeder Änderung
    await dev.power_on(confirm_timeout=15)               # lokal, prüft status!=0, sonst Cloud-Fallback
    await dev.brew("espresso", coffee_ml=40, aroma=3)    # Parameter → Frame + CRC
    await dev.stop()
    stats = await dev.read_statistics()                  # A2, seitenweise, mit Fortschritt
```

- `power_on`: lokal senden → bis `confirm_timeout` auf Status ≠ 0 warten → falls nicht und Cloud konfiguriert: Datapoint über Cloud → erneut warten → sonst `CommandTimeout`.
- `connected`/`available` hängt am Link-Zustand, nicht an Monitor-Änderungen.

### 3.3 Cloud (nur wo nötig)

- Onboarding: Login, Maschinen finden, `lanip_key` + `key_id` + IP holen, Modell erkennen.
- Fallback: Wecken; später optional DSS-Push für Nutzer ohne LAN-Zugang.
- Komplett async (aiohttp), Token-Refresh serialisiert, Regionen konfigurierbar (EU-Defaults, US-Werte aus cremalink#90).

---

## 4. Home-Assistant-Integration

- **Kein Add-on mehr.** Die Integration startet beim ersten Config-Entry den `LanServer` im HA-Prozess (eigener Port, Default 10280 → bestehende Firewall-Regeln passen weiter). Advertised IP über `homeassistant.components.network.async_get_source_ip`, im Options-Flow überschreibbar (Docker, VLANs).
- **Coordinator push-basiert:** `async_set_updated_data` aus dem `subscribe`-Callback; kein 1-s-Polling mehr. Statistik-Coordinator bleibt langsam getaktet (A2-Lesen ist teuer).
- **Config-Flow:** Cloud-Login (Discovery, Key, Modell automatisch) als Standard; manueller Flow (IP + Key) bleibt; Reauth, Reconfigure (neue IP); Option "Cloud-Fallback zum Wecken" an/aus.
- **Entitäten:** bestehende Übersetzungen (EN/DE), A2-Statistiken, Wartungswerte und Buttons übernehmen. Binärsensoren nur für Flags, die das Profil definiert (vgl. cremalink-ha#78).
- **Migration für deine Installation:** Domain `cremalink_ha` bleibt im Fork, Config-Entry `version` 1 → 2 mit `async_migrate_entry` (Add-on-URL raus, LAN-Key/IP rein). Unique-IDs (`{entry_id}_{key}`) bleiben → Historie und Dashboards bleiben erhalten.
- **Diagnostics:** Link-Zustand, Zahl der Key-Exchanges und Re-Registrierungen, letzter Monitor-Frame, Fehlerzähler; Key/Token redacted.
- **Qualität:** `manifest.json` mit `aioecam==x.y.z` von PyPI (nicht Git-Pin), hassfest + HACS-Action in CI.

---

## 5. Meilensteine

| # | Inhalt | Fertig, wenn … |
|---|---|---|
| **M0** | Repo `aioecam` (pyproject, uv, ruff, mypy, pytest, GitHub Actions), Lizenz festgelegt | CI grün auf leerem Paket |
| **M1** | `ecam/`: Frame + CRC, Monitor-Parser, Profile ECAM610/612/452, Kommandos aus den Device-Maps | Alle Frames aus den Probe-Logs werden korrekt geparst; erzeugte Kommando-Frames stimmen byte-genau mit den Device-Maps überein |
| **M2** | `lan/`: Krypto, Server, Link-Zustandsmaschine, Fake-Device | Fake-Device-Tests: Erst-POST, Keep-alive, Session-Tod → POST, Maschinen-Neustart, zwei Geräte gleichzeitig |
| **M3** | `device.py`: Kommandos mit Bestätigung, A2-Statistiken (Port), CLI `python -m aioecam` | An der echten Soul: Monitor, an/aus, ein Getränk, Statistik lesen |
| **M4** | `cloud/`: async Auth, LAN-Config, Wake-Fallback | Onboarding nur mit E-Mail/Passwort; Wecken-Fallback getestet (WAN an) |
| **M5** | HA-Integration im Fork auf aioecam umgestellt (Branch), Migration v1→v2, Add-on entfernt | Deine Installation läuft ohne Add-on, alte Entitäten und Historie unverändert |
| **M6** | Härtung: 24-h-Soak (inkl. Standby-Nacht, WAN-Sperre), Release `aioecam 0.1.0` auf PyPI, HACS-Release | Keine hängende Session im Soak; Weck-Erfolgsquote nach langem Standby gemessen |
| **M6b** | Rename: ohne Add-on und ohne cremalink-Core ist es nicht mehr "Cremalink". Neuer Name für Integration, Repo, HACS-Eintrag und Icon-Texte; Credits an cremalink bleiben. Die HA-Domain `cremalink_ha` nur mit eigener Migration ändern (sonst verlieren Installationen ihre Entitäten); erst Anzeigename/Repo, Domain separat entscheiden | Neuer Name überall sichtbar, bestehende Installationen laufen ohne Neueinrichtung weiter |
| **M7** | Optional: parametrische Getränke, Profile/Benutzer (cremalink-ha#47), Eletta-App-ID-Handshake, US-Region, Gespräch mit Upstream | — |

Reihenfolge M1 → M2 → M3 ist zwingend; M4 kann parallel zu M3 laufen.

---

## 6. Risiken und offene Punkte

- **Wecken nach langem Standby ohne WAN:** nach kurzem Standby gemessen ok (2026-10-19, `tools/wake_test.py`); Nachttest steht noch aus.
- **HTTP-Eigenheiten des ESP32:** FiveElements berichtet, dass der Client der Maschine bestimmte Response-Framings ablehnt. Unser Raw-Server lief fehlerfrei; aiohttp-Responses müssen in M2 an der echten Maschine verifiziert werden.
- **Nur ein LAN-Slot:** Ein zweiter LAN-Client (z. B. die App im LAN-Modus oder ein vergessenes Add-on) verdrängt uns. Lösung: Erkennung plus klare Diagnose, kein Kampf um den Slot.
- **Erreichbarkeit HA ← Maschine:** Die Maschine muss den HA-Port erreichen können (Docker-Bridge, VLAN, Firewall). Doku und Config-Flow-Check (Test-Registrierung beim Einrichten).
- **ECAM452/Eletta:** anderes Property-Set (`app_*`), App-ID-Handshake und Heartbeat für Getränke (cremalink#224). Erst nach M6.
- **Lizenz** (siehe 7).

---

## 7. Entscheidungen (getroffen am 2026-10-18)

1. **Lizenz:** AGPL-3.0-or-later. Code aus cremalink (Cloud-Auth, Device-Maps, Monitor-Profile) darf mit Attribution übernommen werden.
2. **Name/Repo:** `aioecam`, neues Repo `Styx85/aioecam`, Veröffentlichung auf PyPI.
3. **Integration:** im Fork `Styx85/cremalink-ha` mit Domain `cremalink_ha`; Config-Entry-Migration v1 → v2, Unique-IDs bleiben.
