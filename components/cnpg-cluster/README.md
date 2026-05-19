# cnpg-cluster Component

Reusable Component that wires an app into a CloudNativePG-managed PostgreSQL cluster
with WAL streaming + base backups to Garage S3.

## TL;DR — pick a variant

| Variant | When to use |
|---|---|
| `cnpg-cluster` (this dir) | New app deployment OR existing CNPG-managed app. `bootstrap.initdb` creates a fresh empty database. |
| [`cnpg-cluster/recovery`](recovery/) | Disaster recovery: cluster nuke, accidental DB drop, side-by-side PITR test. `bootstrap.recovery` restores from a barman-cloud S3 base backup + WAL chain. Flip back to the base variant after the Cluster reaches `Ready`. |

See [`docs/cnpg-disaster-recovery.md`](../../docs/cnpg-disaster-recovery.md) for the full
recovery runbook.

## What it renders

Both variants render:

- `Cluster` — the postgres cluster (1+ instances), backup wired via the
  `barman-cloud.cloudnative-pg.io` plugin
- `${APP}-store` ObjectStore — plugin-barman-cloud destination config (S3
  path, credentials, compression, retention). Replaced the deprecated in-tree
  `spec.backup.barmanObjectStore` field.
- `${APP}-cnpg-s3` Secret — S3 credentials for backup (populated from `volsync-garage-base`)
- `ScheduledBackup` — daily base backup using `method: plugin` (default 04:00 UTC)

The base variant adds `spec.bootstrap.initdb` (fresh empty database). The
recovery variant swaps that for `spec.bootstrap.recovery` + an
`externalClusters[].plugin` entry pointing at the source cluster's ObjectStore.

Requires `infrastructure/controllers/cnpg-barman-plugin` to be installed and the
`barman-cloud` plugin pod Ready.

## Companion Services CNPG creates automatically

- `${APP}-rw` — primary (writes go here)
- `${APP}-ro` — read-only replicas (only meaningful with `instances >= 2`)
- `${APP}-r` — any instance (round-robin)

## Required substitutions

| Variable | Description |
|---|---|
| `APP` | Cluster name (also Service prefix) |
| `APP_NAMESPACE` | Where to deploy |
| `CNPG_DB_NAME` | Initial database created at bootstrap |
| `CNPG_DB_OWNER` | Owner role for the initial database |

## Required substituteFrom Secret

`volsync-garage-base` (in `flux-system`) — must provide:
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `S3_ENDPOINT`
- `S3_BUCKET`

## Optional substitutions

| Variable | Default | Notes |
|---|---|---|
| `CNPG_INSTANCES` | `1` | Bump to 2+ for HA |
| `CNPG_IMAGE` | `ghcr.io/cloudnative-pg/postgresql:17.5-bookworm` | Override for postgres 15/16 or postgis |
| `CNPG_STORAGE_SIZE` | `2Gi` | |
| `CNPG_STORAGECLASS` | `ceph-rbd` | |
| `CNPG_RETENTION` | `30d` | Backup retention window |
| `CNPG_BACKUP_SCHEDULE` | `0 0 4 * * *` | 6-field cron (CNPG format with seconds) |
| `CNPG_CPU_REQUEST` | `100m` | |
| `CNPG_MEMORY_REQUEST` | `256Mi` | |
| `CNPG_MEMORY_LIMIT` | `1Gi` | |

## App credentials

CNPG auto-generates a Secret `${APP}-app` with `username` (= owner) and `password`.
Apps should mount/reference this Secret instead of the previous app-managed
postgres-secret. Standard kubernetes.io/basic-auth keys.

## Backup paths in Garage

- WAL + base backups: `s3://${S3_BUCKET}/cnpg/${APP}/`
- One CNPG-managed prefix per app, isolated from volsync's per-app restic repos.

## Restore (point-in-time or full DR)

Use the [`recovery/`](recovery/) variant. Same substitutions as the base
Component, plus:

| Variable | Default | Notes |
|---|---|---|
| `APP_RESTORE_FROM` | `${APP}` | Name of the source cluster in S3. Override only for side-by-side restore (e.g. `APP=joplin-db-restored APP_RESTORE_FROM=joplin-db`). |

Two scenarios are walked through end-to-end in
[`docs/cnpg-disaster-recovery.md`](../../docs/cnpg-disaster-recovery.md):
single-cluster PITR (with a Kustomize patch adding `recoveryTarget.targetTime`)
and 8-cluster cluster-nuke recovery.

### Pre-conditions for the recovery variant

- **Source ObjectStore must exist in the namespace.** Cluster-nuke restore is
  fine (`APP == APP_RESTORE_FROM`; the recovery Component creates the same
  ObjectStore the source app would). For side-by-side restore, the source
  app's base Component must already be deployed.
- **Plugin operator must be Ready.** `kubectl -n cnpg-system get deploy
  barman-cloud` should be `1/1` before applying.
- **`serverName` lives in plugin parameters, NOT in the ObjectStore.** The
  ObjectStore CRD forbids `spec.configuration.serverName`. The Component sets
  serverName on both `spec.plugins[].parameters.serverName` (own backup path)
  and `externalClusters[].plugin.parameters.serverName` (source restore path)
  to `${APP_RESTORE_FROM}`. Don't customize this without reading the
  runbook's pitfalls section.

### Validated 2026-05-19

- Vanilla postgres 16 (joplin-db) — recovery to latest WAL: 2m18s; data identical
- PITR drill (joplin-db) — target between two timestamped sentinel rows; the
  later row was correctly excluded from the restored cluster
- Custom-image postgis 17 (dawarich-db, 5 GiB) — recovery: 91s; all 5
  extensions restored; row counts identical across 5 sample tables;
  `SELECT PostGIS_Version()` callable

Reference: https://cloudnative-pg.io/plugin-barman-cloud/
