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

Run tools **without** the wrapper to hit the home/lab instance.

## Registry overrides in git (`ha-registry.py`)

HA has **no YAML for the entity/device registry**. Entity names, aliases, `device_class`
overrides, hidden flags, areas and Assist exposure all live in `/config/.storage`, which HA
owns and rewrites at runtime. `homeassistant.customize` is legacy and only reaches
`friendly_name`/`icon`/`device_class` — it cannot express aliases, exposure, hidden or areas.

So a bare-metal restore brings **template entities** back from git
(`apps/base/home-assistant/home-assistant-templates.yaml`) and loses **every override**.

`registry-overrides.yaml` is the desired state; `ha-registry.py` converges the live registry
onto it:

```bash
./ha-registry.py check     # diff only, exit 1 if drifted — safe, read-only
./ha-registry.py apply     # converge, printing every change
./ha-registry.py export lock.foo switch.bar   # seed new blocks from live state
```

Always seed new entries with `export` rather than hand-typing them, then paste the block in.

**Matching:** devices by **MAC** (device_ids are regenerated when a device is removed and
re-added); entities by `entity_id`, which is pinned by the template's `unique_id` — never
change a `unique_id`.

**`aliases` is a closed set.** An entity listed in the manifest with no `aliases:` key must have
none, and a stray alias added in the UI is reported as drift. Aliases are the voice-routing
surface: a duplicate one is what makes Assist answer about the wrong device, so tolerating
undeclared aliases would defeat the file's purpose. Every other field is managed only where
declared, so the manifest can be adopted incrementally.

**It does not create entities** — it only decorates ones that already exist. Template entities
arrive via Flux; integration entities appear when their device is added.

A declared entity or device that is **missing** is reported and skipped rather than crashing, so
running it mid-restore is safe. `check` **exits 1** in that case — a half-restored instance must
not report "in sync". `apply` converges what it can and tells you to re-run once the rest exists.

```
?? lock.does_not_exist_yet: not in the registry — skipped
0 drifted field(s), 2 declared object(s) MISSING
```

`-f/--file` works either side of the subcommand (`check -f other.yaml`).

 The wrapper refuses to
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
