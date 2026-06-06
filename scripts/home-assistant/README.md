# Home Assistant → Homebox inventory sync

Pull Home Assistant's **device registry** (make / model / room / MAC / integration /
firmware) and cross-match it into [Homebox](https://homebox.lab.mainertoo.com) **by MAC**.
The HA-side companion to [`scripts/unifi/netinfo.py`](../unifi/), which seeded Homebox
from the UDM SE. All Homebox access reuses [`scripts/homebox/inventory.py`](../homebox/).

## Why a WebSocket client

HA's device registry — the only place manufacturer/model/area/MAC live — is **not** on
the REST API. It's only reachable over HA's authenticated WebSocket API. `hass.py`
embeds a tiny stdlib WebSocket client (no pip deps) to speak it.

## Auth

`HASS_TOKEN` is a HA **Long-Lived Access Token**: HA → your profile → **Security** →
*Long-Lived Access Tokens* → **Create Token**. Paste it into `credentials.sops.yaml`
(`stringData.HASS_TOKEN`) and save — VS Code auto-encrypts `*.sops.yaml`. Verify before
committing:

```bash
grep -q 'ENC\[' credentials.sops.yaml && echo encrypted   # must print "encrypted"
```

Env vars `HASS_URL` / `HASS_TOKEN` override the file.

## Usage

```bash
./hass.py ping                 # auth check — HA version + device/area/entity counts
./hass.py devices              # table: name · make · model · room · integration · MAC
./hass.py pull -o ha-devices.yaml   # full structured dump of physical devices

./hass.py sync                 # PLAN the Homebox changes — writes NOTHING
./hass.py sync --commit        # apply: enrich + relocate + create
```

Global flags go **before** the subcommand: `./hass.py --insecure ping`, `./hass.py --all devices`.

## What `sync` does (matched by MAC → Homebox item id)

| HA device                                   | Action                                                              |
|---------------------------------------------|--------------------------------------------------------------------|
| MAC matches a Homebox item's `serialNumber` | **gap-fill** make/model (only if empty), **relocate** if the item is in `Unsorted (auto-import)`/unset and HA knows the area, append an `HA: …` provenance note |
| no MAC match                                | **create** a new item (make/model, serialNumber = MAC or `ha:<id>`, room, tags `HomeAssistant`+integration) |
| make/model disagree with a non-empty value  | **conflict** — Homebox value kept, printed in the report, never overwritten |

It matches Homebox items **by id** (not name+location), so relocations are safe — unlike
`inventory.py apply`, whose `(name, location)` matching would *duplicate* a moved item.
That's why the Homebox write lives here rather than going through an `inventory.py` spec.

Areas → locations: an HA area reuses an existing Homebox location when names line up
(see `AREA_ALIASES` for explicit mappings like *Living Room → Main Floor/Living Room*);
otherwise a new top-level location is created. The full map is printed for review every run.

## Conventions & caveats

- **Dry-run is the default.** Always review the area map + summary + conflicts before `--commit`.
- **MACs** are normalized lowercase colon-form — matching the serialNumber convention netinfo.py wrote.
- **Gap-fill only** — hand-curated make/model in Homebox is never clobbered.
- **Relocation is conservative** — only items currently in `Unsorted (auto-import)` (or
  unplaced) are moved; anything you've already filed by hand is left alone.
- Devices without a MAC (Zigbee/Z-Wave/Matter) can't match UniFi-seeded items; they come
  in as new items keyed by `ha:<device_id>` so re-runs stay idempotent.
- `ha-devices.yaml` is **generated** (gitignored) — the live HA API is the source of truth.

## Stack

Stdlib only (`socket`/`ssl`/`urllib`) + PyYAML. Imports `../homebox/inventory.py`
(`Client`, `load_creds`, `all_items`, `location_paths`, `Planner._update_body`).
