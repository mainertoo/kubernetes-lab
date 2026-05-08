# cnpg-cluster Component

Reusable Component that wires an app into a CloudNativePG-managed PostgreSQL cluster
with WAL streaming + base backups to Garage S3.

## What it renders

- `Cluster` — the postgres cluster (1+ instances)
- `${APP}-cnpg-s3` Secret — S3 credentials for backup (populated from `volsync-garage-base`)
- `ScheduledBackup` — daily base backup (default 04:00 UTC)

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

## Restore (point-in-time)

```yaml
spec:
  bootstrap:
    recovery:
      source: <name-of-source-cluster>
      recoveryTarget:
        targetTime: "2026-05-08 14:23:51"
  externalClusters:
    - name: <name-of-source-cluster>
      barmanObjectStore:
        destinationPath: s3://${S3_BUCKET}/cnpg/<source-app>
        # ... credentials etc.
```

See https://cloudnative-pg.io/documentation/current/recovery/ for the full procedure.
