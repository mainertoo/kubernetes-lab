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

# 2. CNPG operator AND barman-cloud plugin running
$ kubectl -n cnpg-system get pods -l app.kubernetes.io/name=cloudnative-pg
$ kubectl -n cnpg-system get deploy barman-cloud      # must be 1/1 Ready

# 3. Source backups exist for the target app(s)
# Brittle `${APP%-db}` shell-expansion bug from prior versions is fixed; use yq lookup
# that handles all DB naming patterns (e.g. opencut-cnpg-db, media/* nested paths).
$ APP=joplin-db   # or any of the 8
$ F=$(rg --files apps/production | rg 'db-cnpg.yaml$' | xargs -I{} sh -c "yq -e \".spec.postBuild.substitute.APP == \\\"$APP\\\"\" {} >/dev/null 2>&1 && echo {}" | head -1)
$ NS=$(yq -r '.spec.postBuild.substitute.APP_NAMESPACE' "$F")
$ LINEAGE=$(yq -r '.spec.postBuild.substitute.CNPG_LINEAGE // "v1"' "$F")
$ kubectl -n "$NS" exec "${APP}-1" -c postgres -- barman-cloud-backup-list \
    --endpoint-url https://garage.lab.mainertoo.com \
    "s3://volsync/cnpg/${APP}" "${APP}-${LINEAGE}" | tail -5
# expect: at least one base backup, most recent within last 24h
# For pre-refactor v0 backups, use serverName=${APP} (no -vN suffix).
```

If the **live** cluster is gone (cluster-nuke scenario), step 3 can't run inside it.
Use an external `barman-cloud-backup-list` from anywhere with the credentials, or
just trust that ScheduledBackups have been running daily — the Backup CRs visible
in `kubectl get backup -A` history are the inventory.

---

## 1. Single-cluster restore (in-place)

**Updated 2026-05-20 to use the overlay refactor + `scripts/dr-flip.sh`.** This
in-place flow is the common case: an existing DB has bad data and you want it
restored from its own S3 backup history into the same cluster name. (For a
side-by-side restore that keeps the original live, see §1c.)

### Step 1 — Flip the DB to recovery mode

```bash
# Optional: restore from the v0 (pre-refactor unversioned) lineage instead of
# the default v(current-1). The migration-window escape hatch.
./scripts/dr-flip.sh enable joplin-db
# or for v0:
./scripts/dr-flip.sh enable --restore-from-lineage v0 joplin-db

# Review changes:
git diff apps/production/joplin/db-cnpg.yaml
# Expected:
#  - components[] entry flipped from /initdb to /recovery
#  - CNPG_LINEAGE: v1 → v2  (bumped)
#  - CNPG_RESTORE_FROM_LINEAGE: v0 → v1  (set to the prior lineage)

git add apps/production/joplin/db-cnpg.yaml
git commit -m "dr(joplin-db): flip to recovery, restore from v1, write to v2"
git push
```

### Step 2 — Force CNPG to re-evaluate bootstrap

CNPG evaluates `spec.bootstrap` ONLY at Cluster CREATION, never on update.
To force a fresh bootstrap evaluation we MUST delete + recreate the live
Cluster. Wait for Flux to reconcile the spec change first (~2 min), then:

```bash
kubectl -n <ns> delete cluster.postgresql.cnpg.io <db>
kubectl -n <ns> delete pvc -l cnpg.io/cluster=<db>
# Wait for PVCs to fully terminate (~30-90s)
kubectl -n <ns> get pvc -l cnpg.io/cluster=<db>
# Trigger Flux to recreate the Cluster (now in recovery mode)
flux reconcile kustomization <db>-cnpg --with-source
```

### Step 3 — Watch the recovery

```bash
kubectl -n <ns> get cluster.postgresql.cnpg.io <db> -w
kubectl -n <ns> get pods | grep <db>

# Once <db>-1-full-recovery-* pod is Running, tail its logs
kubectl -n <ns> logs <db>-1-full-recovery-XXXXX -f
```

Look for `"restored log file ..."` (WAL pulling) → `"consistent recovery state
reached"` → Cluster status `Cluster in healthy state`.

### Step 4 — Verify data + restart consumer apps

```bash
kubectl -n <ns> exec <db>-1 -c postgres -- psql -U postgres -d <dbname> -c "\dt"
kubectl -n <ns> rollout restart deployment/<app>
```

### Step 5 — Post-recovery settle gate (MANDATORY before disable)

**Skipping this step puts the next DR at risk of an unrecoverable hollow
lineage.** See §1b below for the full checklist.

### Step 6 — Settle: flip back to initdb mode

```bash
# Run the 3 evidence commands in §1b first. Then:
./scripts/dr-flip.sh disable --i-verified-post-recovery-base-backup joplin-db
git add apps/production/joplin/db-cnpg.yaml
git commit -m "settle: joplin-db back to initdb mode at v2"
git push
```

Lineage values stay at `v2 / v1` — the live cluster IS on v2, the spec MUST
reflect that. `bootstrap.initdb` is a no-op on an existing cluster.

---

## 1b. Post-recovery settle checklist (MANDATORY)

After every DR (in-place OR side-by-side) and BEFORE `dr-flip.sh disable`:

```bash
# 1. Trigger an immediate base backup on the recovered cluster
kubectl -n <ns> create -f - <<EOF
apiVersion: postgresql.cnpg.io/v1
kind: Backup
metadata:
  name: post-recovery-base-$(date +%s)
  namespace: <ns>
spec:
  cluster: { name: <db> }
  method: plugin
  pluginConfiguration: { name: barman-cloud.cloudnative-pg.io }
EOF

# 2. Wait for it to complete
kubectl -n <ns> get backup -l cnpg.io/cluster=<db> --sort-by=.status.startedAt

# 3. Verify the base backup exists at the new lineage in S3
LINEAGE=$(yq -r '.spec.postBuild.substitute.CNPG_LINEAGE' apps/production/.../<db>/db-cnpg.yaml)
kubectl -n <ns> exec <db>-1 -c postgres -- barman-cloud-backup-list \
  --endpoint-url https://garage.lab.mainertoo.com \
  s3://volsync/cnpg/<db> <db>-${LINEAGE} | tail -3
```

**Why this matters:** if the current lineage's S3 prefix has no base backup
when the NEXT DR happens, the recovery cluster will fail with "no target
backup found." The dr-flip.sh `disable` banner prints this same checklist
before each invocation (skippable only with `--i-verified-post-recovery-base-backup`
or, for CI, `--no-settle-warning` gated on `CI=true`/`BATS_TEST=1`).

---

## 1c. Side-by-side restore (don't disturb live)

Rare but useful (verifying a backup is good, testing a recovery procedure).
Manual today; deploy a temporary Flux Kustomization with `APP_RESTORE_FROM`
pointing at the source cluster and a different `APP` name:

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

### Step 1 — Flip all 8 to recovery mode with one script invocation

```bash
# During the migration window when no v1 base backups exist yet, pass v0:
./scripts/dr-flip.sh enable --restore-from-lineage v0 --all
# After v1 base backups exist (post-cluster-restored steady state), default
# (no flag) works — auto-computes restore-from from current lineage:
./scripts/dr-flip.sh enable --all

git add apps/production/
git commit -m "dr(cnpg-cluster-nuke): flip all 8 DBs to recovery mode"
git push
```

That's the entire spec change. The script handles:

- Component swap on all 8 files (`initdb` → `recovery`, depth-agnostic)
- Lineage bump on all 8 (`v(N)` → `v(N+1)`)
- `CNPG_RESTORE_FROM_LINEAGE` setting per-file
- Atomic transaction-dir staging (all 8 succeed or none change)

Verify with `./scripts/dr-flip.sh status` before pushing.

**Operator goal:** one PR, one git operation. Matches the volsync/Kopia
cluster-nuke ergonomics on the PVC side.

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

### Step 4 — Post-recovery settle gate (MANDATORY before disable)

For EACH of the 8 clusters, complete the §1b checklist (trigger immediate
base backup + verify it landed at the new lineage's S3 prefix). Failure to
do this puts the NEXT DR at risk of an unrecoverable hollow lineage.

### Step 5 — Settle: flip back to initdb mode

```bash
./scripts/dr-flip.sh disable --i-verified-post-recovery-base-backup --all
git add apps/production/
git commit -m "settle: cnpg-cluster-nuke recovery complete"
git push
```

Lineage values stay at their bumped state (the live clusters ARE on those
lineages; spec MUST reflect that). `bootstrap.initdb` is a no-op on existing
clusters.

---

## 3. Pitfalls

### "no target backup found" error

Symptom: full-recovery pod logs `"error":"no target backup found"`. The plugin
read the S3 path but found nothing.

Causes:
- **Wrong `serverName` on the plugin parameters.** The plugin uses
  `spec.plugins[].parameters.serverName` (and the symmetric value in
  `externalClusters[].plugin.parameters.serverName`) as the S3 sub-path. The
  recovery Component sets both to `${APP_RESTORE_FROM}` by default. If you
  customize, both must match the source cluster's actual barman server name.
- **Wrong `barmanObjectName`.** Must match an existing ObjectStore in the
  namespace whose `spec.configuration.destinationPath` points at the source
  cluster's S3 prefix.
- **Source ObjectStore missing.** In a side-by-side restore, the source app's
  ObjectStore must exist in the namespace before the recovery cluster starts.
  Cluster-nuke restore is fine because the recovery Component creates the same
  ObjectStore the source app would.
- **Source backups don't exist yet.** New cluster < 24h old hasn't taken its
  first ScheduledBackup; only WAL is present. The plugin needs at least one
  base backup to start recovery.

Diagnostic — list backups via barman-cloud-backup-list from any pod with creds:
```bash
kubectl -n <ns> exec <any-cnpg-pod> -c postgres -- \
    env AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_DEFAULT_REGION=... \
    barman-cloud-backup-list \
    --endpoint-url https://garage.lab.mainertoo.com \
    s3://volsync/cnpg/<source-app> <source-app>
```

If this returns nothing, the destinationPath or serverName is wrong.

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

### Plugin operator not Ready

The plugin-barman-cloud Deployment lives in `cnpg-system`. If the plugin pod
is down, any Cluster spec referencing it via `spec.plugins[]` fails to
reconcile. Symptom: Cluster sits in `Setting up primary` indefinitely, plugin
sidecar container CrashLoops, or the Cluster's `status.conditions` complain
about an unknown plugin name.

Check + remediate:
```bash
kubectl -n cnpg-system get deploy barman-cloud
kubectl -n cnpg-system rollout status deploy/barman-cloud --timeout=2m
kubectl -n cnpg-system logs deploy/barman-cloud --tail=50
```

If the plugin is gone entirely (Flux suspended? infra Kustomization NotReady?),
recover by reconciling `flux-system/infra-controllers`. Cluster-nuke recovery
requires the plugin to be Ready before any Cluster spec applies.

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

Also validated 2026-05-19: the PITR codepath. Drilled with two timestamped
inserts T1 (before) and T2 (after) into a sentinel database; recovery with
`recoveryTarget.targetTime` set between them yielded the T1 row only and not
T2 — proving the plugin honors WAL stop points correctly.

Plugin-migration validation 2026-05-19: same joplin-db recovery, this time
through the plugin-barman-cloud spec (`spec.plugins[]` + ObjectStore) against
the SAME source S3 data originally written by the in-tree barman. Recovery
succeeded in 2m18s; subsequent `kubectl create backup --method=plugin`
completed in 45s. Confirms the plugin reads existing in-tree-barman data and
writes new backups in a wire-compatible format.

All test artifacts were torn down post-verification — no production state was
modified.
