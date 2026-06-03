# Dispatcharr playlist-as-code

Curate custom IPTV playlists for [Dispatcharr](https://dispatcharr.lab.mainertoo.com)
from code, instead of clicking through 8,000+ streams in the UI.

## The model

In Dispatcharr, a **playlist == a Channel Profile**. The pipeline is:

```
M3U source ─▶ Stream (raw parsed entry) ─▶ Channel (curated, 1+ streams = failover)
                                              └─▶ Channel Profile (the "playlist")
```

Each profile is served to players at:

| Player   | URL                                                        |
|----------|------------------------------------------------------------|
| Jellyfin | M3U `…/output/m3u/<profile>` + guide `…/output/epg/<profile>` |
| Plex     | HDHomeRun tuner `…/hdhr/<profile>`                          |

## Workflow

```bash
./playlist.py sources                      # list your M3U sources
./playlist.py groups --nonempty            # browse channel groups

# 1. EXPORT matching streams to an editable spec
./playlist.py export --group "CA | News,CA | Sports" \
    --profile "Living Room" --no-adult -o living-room.yaml

# 2. EDIT living-room.yaml — rename, renumber, drop rows, merge stream IDs into one
#    channel for failover, set tvg_id for EPG. Keys starting with "_" are ignored.

# 3. APPLY — dry-run first (default), then commit
./playlist.py apply living-room.yaml          # shows plan, writes nothing
./playlist.py apply living-room.yaml --commit # creates groups+channels+profile

# 4. URLs to paste into Plex/Jellyfin
./playlist.py urls "Living Room"
```

`--commit` writes the created channel `id`s back into the spec, so re-applying the
same file is an exact, idempotent update (rename/renumber/reorder and re-apply).

## Curated "Core" lineup

`curate_core.py` builds `core.yaml` — a ~107-channel mainstream lineup (US/CA locals,
news, entertainment incl. playoff carriers, US+CA sports, French) merged across **both**
sources for failover. Edit the block definitions at the top to taste, then:

```bash
./curate_core.py                          # (re)build core.yaml
./playlist.py apply core.yaml --isolate   # dry-run; --isolate = dedicated channels
./playlist.py apply core.yaml --isolate --commit
```

`--isolate` creates **dedicated** channels for this profile and never reuses/edits your
existing channels by name — so it can't disturb your other profiles (News/Sports/MLB/…).
Channel ids are written back into `core.yaml` on commit for idempotent re-apply.

## EPG guide data + logos (important)

The REST API create does **not** auto-link EPG/logos like the UI does. After
`apply ... --commit`, run:

```bash
./link-epg-logos.sh Core
```

This sets each channel's logo (from its stream's `logo_url`) and links it to the matching
EPGData by `tvg_id`, then refreshes the active EPG sources.

Two things that bit us setting up `Core`:
1. **EPG match needs the provider's own EPG source.** The stream `tvg_id`s (e.g. `ESPN.us`,
   `TSN1.ca`) only exist in the `Empire-Eros-DM` / `Empire-Delta-DM` XMLTV sources — the
   community (jesmann) sources use different ids. Those provider sources must be **active**.
2. **Dispatcharr only stores programmes for *mapped* channels at fetch time** — so you must
   link channels first (above), *then* the refresh keeps their programmes. Linking after a
   stale fetch shows channels with 0 programmes until the next refresh.

Plex/Jellyfin cache the guide — reload guide data in the client after a refresh.

## ⚠️ Name-collision footgun

`apply` matches spec channels to **existing** channels by **name** (global, not
per-profile). If a name already exists, the plan shows `~ update [id]` and `--commit`
will **PATCH that shared channel** — changing its number/group/streams everywhere it's
used (e.g. your existing News/Sports/MLB profiles). Always read the dry-run `~ update`
lines first. To avoid touching an existing channel, rename the row in your spec.

## Auth

Reads `credentials.sops.yaml` (SOPS-encrypted) via `sops -d`, or env
`DISPATCHARR_URL` / `DISPATCHARR_API_KEY`. The API key is sent as
`Authorization: ApiKey <key>`. Re-mint/revoke in the Dispatcharr UI (Settings) or via
the pod's Django shell (`User.api_key`).

Stdlib + PyYAML only — no `pip install` beyond `pyyaml`.
