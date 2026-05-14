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

## Migrated apps

Format: ✅ or ❌ | namespace/pvc | YYYY-MM-DD | snapshot count | notes

- [x] **homepage/homepage** | 2026-05-13 | 22 snapshots | first migration; pattern locked in [05-volsync-shared-migrate-homepage.yaml](./05-volsync-shared-migrate-homepage.yaml)
- [x] **donetick/donetick** | 2026-05-13 | 25 snapshots | first template+script validation
- [x] **dumb/dumb** | 2026-05-14 | 28 snapshots | first run with auto-cleanup bug fix verified
