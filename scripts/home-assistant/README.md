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

### Second instance: a remote family HA

A second Home Assistant reached over the tailnet — administered from here, but hosted at
someone else's house. **Its hostname and address are deliberately not written down in
this repo** (see below); both live encrypted in `credentials-remote-ha.sops.yaml`. The
`ha-remote` wrapper exports `HASS_URL`/`HASS_TOKEN` from that file and execs whatever you
give it, so *any* tool here can target either instance without the address appearing in
source, shell history, or command output:

```bash
./ha-remote ./hass.py ping                        # same tooling, other house
./ha-remote ./ha-ws system_log/list --summary     # recent errors/warnings
./ha-remote bash                                  # interactive subshell
```

`ha-ws` is a general-purpose Home Assistant **WebSocket command runner** — `hass.py` only
exposes the registry calls the Homebox sync needs, so `ha-ws` is the escape hatch for
troubleshooting (`system_log/list`, `repairs/list_issues`, any `config/*_registry/list`).
It works on either instance: bare for the lab HA, under `ha-remote` for the other one.
Two gotchas it documents: subscription-style commands (`system_health/info`) return
nothing through it, and `config/config_entries/list` isn't a WS command at all — that one
is REST (`GET /api/config/config_entries/entry`).

Run tools **without** the wrapper to hit the home/lab instance. The wrapper refuses to
run while the placeholder token is in place, and warns if the file is still plaintext.

**Why the address is withheld.** This repo is public. The tailnet domain itself already
appears in committed manifests, so any *new* hostname written here is a fresh, indexable
disclosure — and this one points at a third party's home, which is not ours to publish.
Keeping it in `stringData` costs nothing (SOPS encrypts that block) and keeps the repo
from being a directory of other people's front doors. Apply the same rule to any future
family/remote instance: address in the encrypted file, never in Markdown.

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
