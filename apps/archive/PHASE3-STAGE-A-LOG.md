# Phase 3 Stage A — migration log

Tracks per-app idempotent Stage A migrations from legacy `s3://volsync/<app>/<path>` restic repos into the shared `s3://volsync-shared/restic` repo with `<ns>/<pvc>` tags. Part of the volsync label-driven restore project (see [`docs/volsync-storage-recovery.md`](../../docs/volsync-storage-recovery.md) §4.1).

## How to run

```bash
scripts/migrate-stage-a.sh <namespace> <replication-source-name>
```

The script mirrors the legacy Secret, runs a one-shot Job via [`volsync-stage-a-template.yaml`](./volsync-stage-a-template.yaml), waits for completion, cleans up on success, and prints the line to append below. Commit the log update in the same PR as any code/manifest changes for traceability.

## Garage permissions (required before any Job will succeed)

The shared key (`AWS_ACCESS_KEY_ID` in `pvc-plumber-restic-creds`, currently `GK52d356f67e5183cd058faa83`) must have **read** access on the legacy `volsync` bucket. Grant once on the QNAS Garage:

```bash
ssh qnas '/share/CACHEDEV1_DATA/.qpkg/container-station/bin/docker exec garage /garage bucket allow --read --key GK52d356f67e5183cd058faa83 volsync'
```

Read is sufficient — the Job uses `--no-lock` to avoid trying to write the from-repo lock file. Step 1's `restic tag --add` does write to the legacy repo, but it uses the legacy bucket's own credentials (mirrored from each `<app>-volsync` Secret), not the shared key.

After Phase 5 finishes (all apps migrated, legacy buckets ready to retire), revoke with `garage bucket deny --read --key <id> volsync`.

## Intentional skips (no migration needed)

These ReplicationSources are enumerated by the bulk wrapper but have no migratable history because the underlying PVC is empty by design.

- **media/jackett-downloads** — Jackett is an indexer, not a downloader. Real downloads land in the bittorrent client's PVC or in cephfs symlinks consumed by Riven. The `jackett-downloads` PVC is transient/empty, so the volsync mover produces empty or quickly-pruned snapshots and Garage shows no repo at `s3://volsync/media/jackett/downloads`. Re-enumerate at Phase 5 cutover to confirm the RS itself can be retired (label-driven design doesn't need an `exists=true` answer for an empty PVC — fresh provision is correct).

## Migrated apps

Format: ✅ or ❌ | namespace/pvc | YYYY-MM-DD | snapshot count | notes

- [x] **homepage/homepage** | 2026-05-13 | 22 snapshots | first migration; pattern locked in [05-volsync-shared-migrate-homepage.yaml](./05-volsync-shared-migrate-homepage.yaml)
- [x] **donetick/donetick** | 2026-05-13 | 25 snapshots | first template+script validation
- [x] **dumb/dumb** | 2026-05-14 | 28 snapshots | first run with auto-cleanup bug fix verified

## Bulk rollout 2026-05-14T03:24:10Z

- [x] **dumbassets/dumbassets** | 2026-05-14 | 23 snapshots
- [x] **dumbdo/dumbdo** | 2026-05-14 | 23 snapshots
- [x] **expense-owl/expense-owl** | 2026-05-14 | 25 snapshots

## Bulk rollout 2026-05-14T03:26:01Z

- [x] **actual-budget/actual-budget** | 2026-05-14 | 28 snapshots

## Bulk rollout 2026-05-14T03:33:48Z

- [x] **authentik/authentik-media** | 2026-05-14 | 28 snapshots
- [x] **code-server/code-server** | 2026-05-14 | 25 snapshots
- [x] **crafty/crafty** | 2026-05-14 | 25 snapshots
- [x] **dawarich/dawarich-media** | 2026-05-14 | 28 snapshots
- [x] **grafana/grafana** | 2026-05-14 | 21 snapshots
- [x] **grocy/grocy** | 2026-05-14 | 25 snapshots
- [x] **home-assistant/esphome** | 2026-05-14 | 27 snapshots
- [x] **home-assistant/home-assistant** | 2026-05-14 | 28 snapshots
- [x] **home-assistant/matter-server** | 2026-05-14 | 28 snapshots
- [x] **home-assistant/mosquitto** | 2026-05-14 | 28 snapshots

## Bulk rollout 2026-05-14T03:44:20Z

- [x] **home-assistant/node-red** | 2026-05-14 | 28 snapshots
- [x] **home-assistant/zigbee2mqtt** | 2026-05-14 | 28 snapshots
- [x] **homebox/homebox** | 2026-05-14 | 18 snapshots
- [x] **kitchenowl/kitchenowl** | 2026-05-14 | 25 snapshots
- [x] **lcshare/lcshare** | 2026-05-14 | 22 snapshots
- [x] **mealie/mealie-data** | 2026-05-14 | 25 snapshots
- [x] **media/audiobookshelf** | 2026-05-14 | 28 snapshots
- [x] **media/bazarr** | 2026-05-14 | 28 snapshots
- [x] **media/calibre** | 2026-05-14 | 28 snapshots
- [x] **media/calibre-library-cephfs** | 2026-05-14 | 28 snapshots
- [x] **media/calibre-web-automated** | 2026-05-14 | 27 snapshots
- [x] **media/cinesync** | 2026-05-14 | 28 snapshots
- [x] **media/decypharr** | 2026-05-14 | 27 snapshots
- [x] **media/dispatcharr** | 2026-05-14 | 28 snapshots
- [x] **media/jackett-config** | 2026-05-14 | 28 snapshots
- [x] **media/jellyfin** | 2026-05-14 | 28 snapshots
- [x] **media/lidarr** | 2026-05-14 | 28 snapshots
- [x] **media/notifiarr** | 2026-05-14 | 28 snapshots
- [x] **media/notifiarr-shared** | 2026-05-14 | 28 snapshots

## Bulk rollout 2026-05-14T05:18:58Z

- [x] **media/nzbdav** | 2026-05-14 | 28 snapshots
- [x] **media/plex** | 2026-05-14 | 28 snapshots
- [x] **media/prowlarr** | 2026-05-14 | 28 snapshots
- [x] **media/radarr** | 2026-05-14 | 28 snapshots
- [x] **media/radarr4k** | 2026-05-14 | 28 snapshots
- [x] **media/readmeabook** | 2026-05-14 | 27 snapshots
- [x] **media/riven-data-pvc** | 2026-05-14 | 28 snapshots
- [x] **media/seerr** | 2026-05-14 | 28 snapshots
- [x] **media/shared-media-pvc** | 2026-05-14 | 28 snapshots
- [x] **media/shelfmark** | 2026-05-14 | 27 snapshots
- [x] **media/sonarr** | 2026-05-14 | 28 snapshots
- [x] **media/sonarr4k** | 2026-05-14 | 28 snapshots
- [x] **media/sym-prowlarr** | 2026-05-14 | 28 snapshots
- [x] **media/sym-radarr** | 2026-05-14 | 28 snapshots
- [x] **media/sym-radarr4k** | 2026-05-14 | 28 snapshots
- [x] **media/sym-sonarr** | 2026-05-14 | 28 snapshots
- [x] **media/sym-sonarr4k** | 2026-05-14 | 28 snapshots
- [x] **media/tautulli** | 2026-05-14 | 28 snapshots
- [x] **media/tracearr-redis** | 2026-05-14 | 28 snapshots
- [x] **media/tracearr-timescale** | 2026-05-14 | 28 snapshots

## Bulk rollout 2026-05-14T06:09:09Z

- [x] **media/zilean-data** | 2026-05-14 | 28 snapshots
- [x] **media/zilean-pgadmin-data** | 2026-05-14 | 28 snapshots
- [x] **media/zilean-tmp** | 2026-05-14 | 28 snapshots
- [x] **memos/memos** | 2026-05-14 | 25 snapshots
- [x] **open-notebook/open-notebook** | 2026-05-14 | 14 snapshots
- [x] **paperless-ngx/paperless-ngx** | 2026-05-14 | 28 snapshots
- [x] **scrypted/scrypted** | 2026-05-14 | 25 snapshots
- [x] **sparky-fitness/sparky-fitness-media** | 2026-05-14 | 28 snapshots
- [x] **tandoor/tandoor** | 2026-05-14 | 25 snapshots
- [x] **ui-toolkit/ui-toolkit** | 2026-05-14 | 28 snapshots
- [x] **vaultwarden/vaultwarden** | 2026-05-14 | 28 snapshots
- [x] **wallos/wallos** | 2026-05-14 | 14 snapshots
- [x] **wiki-js/wiki-js-data** | 2026-05-14 | 25 snapshots
