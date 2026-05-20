# cnpg-cluster Component

Reusable Component family that wires an app into a CloudNativePG-managed PostgreSQL
cluster with WAL streaming + base backups to Garage S3 via the
plugin-barman-cloud CNPG-I plugin.

## Layout (post 2026-05-20 overlay refactor)

```
components/cnpg-cluster/
├── base/                    ← shared spec (Cluster minus bootstrap, ObjectStore, Secret, ScheduledBackup)
│   ├── cluster.yaml
│   ├── objectstore.yaml
│   ├── s3-secret.yaml
│   ├── scheduledbackup.yaml
│   └── kustomization.yaml
├── initdb/                  ← overlay: adds spec.bootstrap.initdb (fresh empty DB)
│   ├── bootstrap-patch.yaml
│   └── kustomization.yaml
└── recovery/                ← overlay: adds spec.bootstrap.recovery + externalClusters[] (DR)
    ├── bootstrap-patch.yaml ← pre-creates v0..v10 externalClusters[] entries
    └── kustomization.yaml
```

**Consumers reference BOTH `base/` AND exactly one of `initdb/` or `recovery/`:**

```yaml
# apps/production/<app>/db-cnpg.yaml
spec:
  components:
    - ../../../components/cnpg-cluster/base
    - ../../../components/cnpg-cluster/initdb   # ← swap "initdb" → "recovery" for DR
```

Use `scripts/dr-flip.sh enable <db>` to flip safely (handles lineage versioning +
DR-during-DR guard automatically). See
[`docs/cnpg-disaster-recovery.md`](../../docs/cnpg-disaster-recovery.md) for the
operational runbook.

## What renders

Both overlay paths render:

- `Cluster` — postgres cluster (1+ instances), backup wired via the
  `barman-cloud.cloudnative-pg.io` plugin with **lineage-suffixed serverName**
  (`${APP}-${CNPG_LINEAGE}`, default `v1`). Every DR bumps the lineage so
  new WAL writes go to a fresh S3 prefix.
- `${APP}-store` `ObjectStore` — plugin-barman-cloud destination config (S3
  path, credentials, compression, retention).
  `destinationPath: s3://${S3_BUCKET}/cnpg/${APP}` — the `serverName` segment
  in the actual S3 path (`<destinationPath>/<serverName>/{base,wals}/...`)
  is appended by the plugin, NOT by the ObjectStore. One ObjectStore is
  sufficient for all lineages.
- `${APP}-cnpg-s3` `Secret` — S3 credentials (populated from
  `volsync-garage-base` Secret in `flux-system` via Flux postBuild).
- `ScheduledBackup` — daily base backup using `method: plugin` (default
  04:00 UTC, override via `CNPG_BACKUP_SCHEDULE`).

`initdb/` overlay additionally renders `spec.bootstrap.initdb` (fresh DB).
`recovery/` overlay additionally renders `spec.bootstrap.recovery` + 11
`externalClusters[]` entries (v0..v10).

Requires `infrastructure/controllers/cnpg-barman-plugin` to be installed
and the `barman-cloud` plugin pod Ready in `cnpg-system`.

## Companion Services CNPG creates automatically

- `${APP}-rw` — primary (writes go here)
- `${APP}-ro` — read-only replicas (only meaningful with `instances >= 2`)
- `${APP}-r` — any instance (round-robin)

## Required substitutions

| Variable | Description |
|---|---|
| `APP` | Cluster name (also Service prefix) |
| `APP_NAMESPACE` | Where to deploy |
| `CNPG_DB_NAME` | Initial database created at bootstrap (initdb overlay only — ignored by recovery) |
| `CNPG_DB_OWNER` | Owner role (initdb overlay only) |

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
| `CNPG_LINEAGE` | `v1` | Lineage suffix appended to serverName. **Managed by `scripts/dr-flip.sh`** — do not hand-edit. Bumped on every DR. |
| `CNPG_BACKUP_SCHEDULE` | `0 0 4 * * *` | 6-field cron (CNPG format with seconds) |
| `CNPG_CPU_REQUEST` | `100m` | |
| `CNPG_MEMORY_REQUEST` | `256Mi` | |
| `CNPG_MEMORY_LIMIT` | `1Gi` | |

## App credentials

CNPG auto-generates a Secret `${APP}-app` with `username` (= owner) and `password`.
Apps should mount/reference this Secret. Standard `kubernetes.io/basic-auth` keys.

## Backup paths in Garage

- WAL + base backups (current lineage): `s3://${S3_BUCKET}/cnpg/${APP}/${APP}-v${CNPG_LINEAGE}/{base,wals}/`
- Pre-refactor unversioned prefix (v0 escape hatch, accessible until evidence-gated cleanup PR lands):
  `s3://${S3_BUCKET}/cnpg/${APP}/${APP}/{base,wals}/`

## Per-app customization (canonical example: sparky-fitness)

The base + overlay Components select the **bootstrap mode** only. Per-app
customization (extra `spec.managed.roles`, `postInitApplicationSQL`, custom
images, etc.) goes in the consumer Flux Kustomization's `spec.patches:` block.

`apps/production/sparky-fitness/db-cnpg.yaml` is the canonical reference —
it adds a `managed.roles` entry for the `sparky_app` login role + an
`ALTER DEFAULT PRIVILEGES` postInitApplicationSQL while still using the
shared base + initdb overlay. Pattern:

```yaml
spec:
  components:
    - ../../../../components/cnpg-cluster/base
    - ../../../../components/cnpg-cluster/initdb

  patches:
    - target:
        kind: Cluster
        name: \${APP}              # literal — Flux postBuild rewrites both sides
      patch: |-
        apiVersion: postgresql.cnpg.io/v1
        kind: Cluster
        metadata:
          name: ${APP}
        spec:
          managed:
            roles: [...]
          bootstrap:
            initdb:
              postInitApplicationSQL: [...]
```

Strategic-merge patches compose with the overlay-injected `spec.bootstrap.initdb`
correctly — `database`, `owner`, `postInitApplicationSQL`, AND `managed.roles`
all coexist in the final rendered Cluster.

## Restore (point-in-time or full DR)

Use `scripts/dr-flip.sh enable <db>` (or `--all` for cluster-nuke). It handles:

- Component swap (initdb → recovery)
- Lineage bump (v(N) → v(N+1))
- `CNPG_RESTORE_FROM_LINEAGE` set to v(N) (the prior lineage)
- DR-during-DR safety guard
- `--restore-from-lineage v0` override for the migration-window escape hatch
- Atomic transaction-dir staging (all-or-nothing edits)

Full walk-through in [`docs/cnpg-disaster-recovery.md`](../../docs/cnpg-disaster-recovery.md).

Plugin docs reference: <https://cloudnative-pg.io/plugin-barman-cloud/>
