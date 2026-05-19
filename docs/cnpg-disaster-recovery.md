# CNPG Disaster Recovery Runbook

> Recovering CloudNativePG-managed postgres clusters from Garage S3 backups. Covers
> two scenarios: (a) restoring a single cluster (PITR or accidental DB drop) and
> (b) recovering the entire 8-cluster fleet during a full cluster nuke.
>
> Companion to [`docs/backup-recovery.md`](backup-recovery.md). Read that first for
> the broader layered backup model. This document covers only the CNPG slice
> (Layer 10).

---

## 0. Pre-flight

Before starting recovery, confirm:

```bash
# 1. Garage S3 reachable + bucket alive
$ ssh pve-ugreen 'curl -sf https://garage.lab.mainertoo.com/ -o /dev/null && echo ok'
$ ssh pve-ugreen 'docker exec garage garage bucket list | grep volsync'

# 2. CNPG operator running
$ kubectl -n cnpg-system get pods -l app.kubernetes.io/name=cloudnative-pg

# 3. Source backups exist for the target app(s)
$ APP=joplin-db   # or any of the 8
$ kubectl -n $(yq '.spec.postBuild.substitute.APP_NAMESPACE' apps/production/${APP%-db}/db-cnpg.yaml) \
    exec ${APP}-1 -c postgres -- barman-cloud-backup-list \
    --endpoint-url https://garage.lab.mainertoo.com \
    s3://volsync/cnpg/${APP} ${APP} | tail -5
# expect: at least one base backup, most recent within last 24h
```

If the **live** cluster is gone (cluster-nuke scenario), step 3 can't run inside it.
Use an external `barman-cloud-backup-list` from anywhere with the credentials, or
just trust that ScheduledBackups have been running daily — the Backup CRs visible
in `kubectl get backup -A` history are the inventory.

---

## 1. Single-cluster restore (PITR or fresh restore)

Two sub-scenarios with the same Component:

| Need | What to do |
|---|---|
| Restore to latest WAL (e.g. accidental DROP TABLE that was just committed) | Side-by-side: deploy a NEW Cluster (`<app>-restored`) using the recovery Component pointing at the original's S3 path. Cut the app over once verified. |
| Restore to a specific timestamp within 30-day retention | Same as above + a Kustomize patch adding `recoveryTarget.targetTime`. |

### Step 1 — Add a temporary Flux Kustomization for the restored cluster

Create `apps/production/<app>/db-cnpg-restore.yaml` (temporary — delete after cutover
is complete):

```yaml
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: <app>-db-restore
  namespace: flux-system
spec:
  interval: 1h
  path: ./apps/base/empty
  targetNamespace: <app-namespace>
  prune: true
  retryInterval: 2m
  timeout: 10m
  wait: false
  decryption:
    provider: sops
    secretRef:
      name: sops-age
  sourceRef:
    kind: GitRepository
    name: flux-system
    namespace: flux-system

  components:
    - ../../../components/cnpg-cluster/recovery

  postBuild:
    substitute:
      APP: <app>-db-restored          # NEW cluster name (don't clash with the live one)
      APP_NAMESPACE: <app-namespace>
      APP_RESTORE_FROM: <app>-db      # source cluster's name in S3

      # Match the source cluster's image + storage exactly
      CNPG_IMAGE: ghcr.io/cloudnative-pg/postgresql:16.10-bookworm
      CNPG_STORAGE_SIZE: 10Gi
      CNPG_STORAGECLASS: ceph-rbd
      CNPG_INSTANCES: "1"

      CNPG_BACKUP_SCHEDULE: "0 0 0 * * 0"   # placeholder; deleted before it fires
      CNPG_RETENTION: "30d"

      CNPG_CPU_REQUEST: "100m"
      CNPG_MEMORY_REQUEST: "256Mi"
      CNPG_MEMORY_LIMIT: "1Gi"

    substituteFrom:
      - kind: Secret
        name: volsync-garage-base
```

Add to `apps/production/<app>/kustomization.yaml` (or wherever sibling Flux
Kustomizations are listed). Commit, push.

#### Adding a PITR target time

Add a `patches:` block to the Flux Kustomization above:

```yaml
spec:
  patches:
    - target:
        group: postgresql.cnpg.io
        kind: Cluster
        name: <app>-db-restored
      patch: |
        - op: add
          path: /spec/bootstrap/recovery/recoveryTarget
          value:
            targetTime: "2026-05-09 14:23:51"
```

Without a `recoveryTarget`, CNPG recovers to the latest available WAL.

### Step 2 — Watch CNPG run the recovery

```bash
kubectl -n <ns> get cluster.postgresql.cnpg.io <app>-db-restored -w
kubectl -n <ns> get pods -l cnpg.io/cluster=<app>-db-restored
```

CNPG creates a `<app>-db-restored-1-full-recovery-*` pod that:
1. Provisions the PVC
2. `barman-cloud-restore` pulls the base backup from S3 to `/var/lib/postgresql/data`
3. WAL replay (latest unless `recoveryTarget` is set)
4. Promotes to primary
5. Renames to a normal `<app>-db-restored-1` pod

For a small DB (joplin-db is ~50 MB of actual data in a 10 GiB PVC), this took
~2.5 minutes end-to-end in the 2026-05-19 validation: 2 min in `Setting up primary`
(base-backup download + WAL replay), then ~30 sec to promote and start accepting
connections. Larger DBs scale with base-backup download time over the Garage link.

### Step 3 — Verify

```bash
# Login + sanity check
kubectl -n <ns> exec <app>-db-restored-1 -c postgres -- \
    psql -U postgres -d <app-db-name> -c "\dt"
kubectl -n <ns> exec <app>-db-restored-1 -c postgres -- \
    psql -U postgres -d <app-db-name> -c "SELECT count(*) FROM <some-table>"

# Compare against the live cluster (if it's still running)
kubectl -n <ns> exec <app>-db-1 -c postgres -- \
    psql -U postgres -d <app-db-name> -c "SELECT count(*) FROM <some-table>"
```

Row counts should match (unless using PITR to a time before recent writes).

### Step 4 — Cut the app over (only for full restore, NOT side-by-side test)

Edit the app's HelmRelease values to point at the restored cluster's `-rw` Service:

```yaml
# apps/base/<app>/<app>-release.yaml
env:
  - name: POSTGRES_HOST
    value: <app>-db-restored-rw   # was: <app>-db-rw
```

Or whatever env / connection-string mechanism the app uses. The CNPG-generated
`<app>-db-restored-app` Secret has the credentials (same shape as the original).

After the app is stable on the restored cluster:
1. Delete the original cluster: `kubectl -n <ns> delete cluster.postgresql.cnpg.io <app>-db`
2. Edit the Flux Kustomization at `apps/production/<app>/db-cnpg.yaml` to use the new
   `<app>-db-restored` name (and flip back to the base `components/cnpg-cluster`
   Component — see Step 5)
3. Delete `apps/production/<app>/db-cnpg-restore.yaml`

### Step 5 — Settle (flip back to base Component)

Once `<app>-db-restored` is healthy and the app has cut over:

```yaml
# apps/production/<app>/db-cnpg.yaml
spec:
  postBuild:
    substitute:
      APP: <app>-db-restored        # was: <app>-db

      CNPG_DB_NAME: <db-name>       # restore Component doesn't need these,
      CNPG_DB_OWNER: <owner>        # base Component requires them.
      # ... rest as before ...

  components:
    - ../../../components/cnpg-cluster   # was: cnpg-cluster/recovery
```

`bootstrap.initdb` is a no-op on a Cluster that's already initialized, so flipping
back is safe — it's purely hygiene so the spec describes ongoing state, not the
recovery moment.

---

## 2. Cluster-nuke recovery (all 8 clusters)

Order of operations: provision infra → bootstrap → restore PVCs → restore CNPG.
This section covers the CNPG step. The other steps live in
[`backup-recovery.md`](backup-recovery.md) §4.

### Pre-conditions

- K3s cluster up and Flux bootstrapped
- `flux-system` namespace exists with `sops-age` Secret restored
- `infrastructure` Kustomization Ready (CNPG operator, Garage, networking)
- All 8 app namespaces created (Flux will create these as the apps reconcile)

### Step 1 — Switch all 8 db-cnpg.yaml files to the recovery Component

For each of:
- `apps/production/joplin/db-cnpg.yaml`
- `apps/production/authentik/db-cnpg.yaml`
- `apps/production/dawarich/db-cnpg.yaml`
- `apps/production/opencut/db-cnpg.yaml`
- `apps/production/sparky-fitness/db-cnpg.yaml`
- `apps/production/wiki-js/db-cnpg.yaml`
- `apps/production/media/riven/db-cnpg.yaml`
- `apps/production/media/zilean/db-cnpg.yaml`

Edit the `components:` list and substitution block:

```yaml
spec:
  components:
    - ../../../components/cnpg-cluster/recovery   # was: cnpg-cluster

  postBuild:
    substitute:
      APP: <unchanged>           # SAME name as before — restores into the original
      # APP_RESTORE_FROM defaults to APP, so it points at the same S3 path.
      # CNPG_DB_NAME and CNPG_DB_OWNER are no-ops in recovery mode but harmless to keep.
      # ... all other substitutions unchanged ...
```

Commit + push as one PR (`dr/cnpg-cluster-nuke-restore`).

### Step 2 — Reconcile and watch all 8 in parallel

```bash
flux reconcile source git flux-system
flux reconcile kustomization apps
# CNPG creates 8 *-full-recovery-* pods in parallel
kubectl get cluster.postgresql.cnpg.io -A -w
```

Each cluster takes 1-5 min depending on DB size (dawarich is the largest at ~2.3 GiB).

### Step 3 — App cutover (automatic)

Apps connect via their `-rw` Service. As soon as each restored cluster reaches
`Cluster in healthy state`, the app's pod (still pending or restarting from
the cluster nuke) succeeds at connecting. No manual cutover needed because the
cluster name is preserved (`APP` unchanged).

### Step 4 — Settle (flip back to base Component)

After all 8 clusters are healthy and apps are running, open a settle PR that flips
each `db-cnpg.yaml` back to `components/cnpg-cluster`. This is purely hygiene —
the recovery Component would continue to work, but the spec describes ongoing
state (initdb is no-op on initialized clusters).

---

## 3. Pitfalls

### "no target backup found" error

Symptom: full-recovery pod logs `"error":"no target backup found"`. CNPG looked
at the S3 path but found nothing.

Causes:
- **Wrong `externalClusters[].name` (or missing `serverName`).** CNPG defaults the
  barman server name to `externalClusters[].name`. The recovery Component sets both
  to `${APP_RESTORE_FROM}` so the actual S3 path resolves to
  `<destinationPath>/<serverName>/base/...`. If you customize, keep them aligned.
- **Wrong `destinationPath`.** Must point at the bucket prefix that contains the
  source cluster's `base/` and `wals/` subdirs.
- **Source backups don't exist yet.** New cluster < 24h old hasn't taken its first
  ScheduledBackup; only WAL is present. CNPG needs at least one base backup.

Diagnostic:
```bash
# Run this from any pod with barman + the S3 creds
barman-cloud-backup-list \
    --endpoint-url https://garage.lab.mainertoo.com \
    s3://volsync/cnpg/<source-app> <source-app>
```

If this returns nothing, the source path or server-name is wrong.

### Existing PVC conflict during cluster-nuke restore

If `<app>-db-1` PVC survived the cluster nuke (rare — Flux prune would have
removed it, but possible if the namespace was preserved), CNPG will try to attach
it to the new Cluster and skip the recovery. Symptom: pod comes up Running but
data is stale (= whatever was on the surviving PVC).

Fix: delete the PVC before applying the recovery Kustomization.
```bash
kubectl -n <ns> delete pvc <app>-db-1
```

### App secret race during cutover

The recovery Component generates a fresh `${APP}-app` Secret (CNPG creates it at
cluster init). If the app's HelmRelease references credentials from the OLD
secret name (e.g. the legacy bitnami `<app>-postgresql`), the app won't pick up
the new credentials.

Verify by inspecting the env:
```bash
kubectl -n <ns> exec <app-pod> -- env | grep -iE 'postgres|database'
```

If the connection uses the wrong Secret, this is a Phase 2/3 CNPG migration gap
that should already be fixed for the 8 migrated apps — they all read from
`<app>-db-app`. New apps following this pattern should do the same.

### Custom-image apps (postgis, timescaledb)

Apps with extensions need:
- **dawarich** (postgis) — uses a postgis-enabled CNPG image. The recovery
  Component picks this up via the `CNPG_IMAGE` substitution. As long as the same
  image tag is set, extensions restore cleanly.
- **tracearr-timescale** (timescaledb, pending CNPG migration per
  [`project_tracearr_timescale_cnpg_migration`](../...)) — will need the
  timescale pre/post_restore() pattern documented in that project memory.

Custom images must match between source and recovery clusters or the WAL replay
will fail on unknown extension functions.

### `barmanObjectStore` deprecation warning

CNPG 1.30+ will remove native barman support in favor of the Barman Cloud Plugin.
Both the base and recovery Components use the deprecated `spec.backup.barmanObjectStore`
+ `spec.externalClusters[].barmanObjectStore`. Migration to the plugin is a
separate project — not gating on this runbook.

---

## 4. Verification: tested 2026-05-19

This runbook was validated against `joplin-db` via a side-by-side restore:

- Source: `joplin-db` (postgres 16.10, 10 GiB PVC, daily base backups since 2026-05-08)
- Target: `joplin-db-restored` (new Cluster, same namespace, same image, recovery
  Component with `APP_RESTORE_FROM=joplin-db`)
- Result: 26 tables restored, row counts matched the source (`items=29`,
  `users=1`, `knex_migrations=47`)
- Recovery time: ~2.5 minutes from `kubectl apply` to `Cluster in healthy state`

The first attempt failed with `"no target backup found"` because the original
externalCluster name used a `-source` suffix that diverged from the actual barman
`serverName` (`joplin-db`). Fix: keep `externalClusters[].name` and `serverName`
both equal to `${APP_RESTORE_FROM}`. The recovery Component does this by default;
captured in §3 as a pitfall for anyone who customizes.

The test artifacts were torn down post-verification — no production state was
modified.
