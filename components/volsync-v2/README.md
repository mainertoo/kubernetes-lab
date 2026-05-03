# volsync-v2 Components

Reusable Kustomize Components that wire apps into the volsync backup/restore lifecycle. Pick the variant that matches the app's PVC ownership and lifecycle stage.

S3 backend: **Garage**, deployed in-cluster. Each app uses a dedicated restic repo named `${APP}-volsync` whose credentials live in a per-app `Secret` provided by `components/volsync/remote/`. Master credentials and S3 endpoint live in the `volsync-garage-base` Secret in `flux-system`; per-app Secrets are derived from it via Flux postBuild substitution.

> The `volsync-v2/` directory will be renamed to `volsync/` once the legacy `components/volsync/remote/` Secret-only Component is folded in (Phase 4 of the volsync cleanup project).

---

## TL;DR — pick a variant

| Variant | PVC ownership | Pick this when |
|---|---|---|
| `volsync-v2` | volsync owns the empty `${APP}` PVC | New app, no pre-existing data. The canonical pattern for any healthy production app. |
| `volsync-v2/backup-only` | App owns its own PVC (HelmRelease persistence, StatefulSet `volumeClaimTemplates`, or static manifest in `apps/base/<app>/`) | Volsync only adds a `ReplicationSource`; PVC creation is the app's responsibility. |
| `volsync-v2/bootstrap` | volsync owns `${APP}` PVC, populated on first boot from an S3 restic snapshot | First-boot data restore. Used for staging→production migrations, cross-cluster moves, and disaster recovery. |
| `volsync-v2/restore` | Creates a separate `${APP}-restore` PVC populated from S3 | Disaster-recovery probe. Spin up a copy of the data alongside the live PVC without disturbing it. |

All four use the same `${APP}-volsync` restic repo and the same per-app credentials. The first three are mutually exclusive in any given app's kustomization (they each provision the `${APP}` PVC differently — combining them would collide on the PVC name).

---

## Component anatomy

### `volsync-v2` (default)

```text
components/volsync-v2/
├── kustomization.yaml          # parent — includes pvc/ + backup/
├── pvc/
│   ├── kustomization.yaml
│   └── pvc.yaml                # empty ${APP} PVC, no dataSource
└── backup/
    ├── kustomization.yaml
    └── volsync-replicationsource.yaml   # ${APP} ReplicationSource → S3
```

Renders into the app's namespace:

- `PersistentVolumeClaim/${APP}` — empty, sized via `${CAPACITY}`
- `ReplicationSource/${APP}` — backs up the PVC to `${APP}-volsync` restic repo on `${VOLSYNC_SCHEDULE:=05 00/12 * * *}` (every 12h at :05 by default)

The app's `apps/base/<app>/kustomization.yaml` **must comment out** any static `${APP}-pvc.yaml` declaration so it doesn't fight with the volsync-managed PVC.

### `volsync-v2/backup-only`

```text
components/volsync-v2/backup-only/
├── kustomization.yaml
└── volsync-replicationsource.yaml   # ${APP} RS, sourcePVC=${VOLSYNC_SOURCE_PVC}
```

Renders one resource: a `ReplicationSource` targeting `${VOLSYNC_SOURCE_PVC}`. The PVC is created by the app itself (HelmRelease persistence block, StatefulSet `volumeClaimTemplates`, or a static manifest in `apps/base/<app>/`).

Used today by `grocy` and `mealie` (HelmRelease-managed PVCs). Also useful as the **pre-flight backup** for an app that's about to be migrated — see the playbook below.

### `volsync-v2/bootstrap`

```text
components/volsync-v2/bootstrap/
├── kustomization.yaml          # parent — pvc-with-datasource + ../backup + replicationdestination
├── pvc-with-datasource/
│   ├── kustomization.yaml
│   └── pvc.yaml                # ${APP} PVC with dataSourceRef → ${APP}-bootstrap RD
└── replicationdestination/
    ├── kustomization.yaml
    └── volsync-replicationdestination.yaml   # one-shot manual trigger, pulls from S3
```

Renders three resources:

- `ReplicationDestination/${APP}-bootstrap` — `manual` trigger, runs once on creation, pulls the latest snapshot from `${APP}-volsync` restic repo and stages it as a CSI VolumeSnapshot
- `PersistentVolumeClaim/${APP}` — provisioned with `dataSourceRef: ReplicationDestination/${APP}-bootstrap`. The CSI driver (via the volsync VolumePopulator) waits for the RD's snapshot to be ready, then provisions the PV from it
- `ReplicationSource/${APP}` — same ongoing scheduled backup as plain `volsync-v2`

After the PVC is provisioned with restored data and the app boots cleanly, switch the app's kustomization from `volsync-v2/bootstrap` → plain `volsync-v2` (the **settle** step). The bootstrap RD is then pruned. `dataSourceRef` is consulted only at PVC creation, so leaving the bootstrap component in place by accident is harmless once the PVC exists — but settling keeps the cluster on the canonical pattern.

### `volsync-v2/restore`

```text
components/volsync-v2/restore/
├── kustomization.yaml
└── manifests/
    ├── kustomization.yaml
    ├── restore-pvc.yaml         # ${APP}-restore PVC with dataSourceRef
    └── volsync-replicationdestination.yaml   # ${APP}-restore RD
```

Creates `${APP}-restore` (a separate PVC) populated from S3. The live `${APP}` PVC is untouched. Useful for:

- Verifying that backups in S3 are actually restorable
- Cloning prod data into a sandbox namespace
- Pre-checking a disaster-recovery procedure before performing it

Bumping `VOLSYNC_RESTORE_TOKEN` triggers a fresh restore.

---

## How `bootstrap` actually works (the mechanics)

The bootstrap pattern relies on three pieces working in concert: volsync's restic-S3 backend, Kubernetes' VolumePopulator framework, and the CSI driver's snapshot dataSource support.

### The flow, step by step

```text
1. Kustomize render produces three new objects in the namespace:
     - ReplicationDestination/${APP}-bootstrap    (manual trigger)
     - PersistentVolumeClaim/${APP}                (dataSourceRef → that RD)
     - ReplicationSource/${APP}                   (schedule)

2. volsync-controller sees the new RD. Its trigger is `manual: bootstrap-once`,
   so it runs immediately. It:
     a. Provisions a temporary cache PVC for restic
     b. Restic-pulls the latest snapshot from ${APP}-volsync repo into a
        temporary "destination" PVC
     c. Snapshots that destination PVC via CSI (creates a VolumeSnapshot)
     d. Reports `Status.LatestImage` = the VolumeSnapshot, and condition
        `Synchronizing=False, reason=Completed`

3. The Kubernetes VolumePopulator controller (installed alongside volsync) sees
   the ${APP} PVC has `dataSourceRef.kind=ReplicationDestination`. It:
     a. Waits for the RD's `LatestImage` to be set
     b. Creates a transient `vs-prime-<uuid>` PVC bound to a clone of the
        snapshotted PV
     c. Triggers CSI to provision the real ${APP} PVC from that vs-prime PVC
        (effectively a CSI clone)
     d. Cleans up the vs-prime PVC after ${APP} is bound

4. ${APP} PVC is now Bound, populated with the restored data. The HelmRelease's
   pod that's been waiting for the PVC mounts it and starts.

5. ReplicationSource/${APP} runs on its first scheduled tick, doing an
   incremental restic backup on top of the existing repo (the same snapshots
   bootstrap pulled from). No full re-backup.
```

### Events you'll see

Watch them with `kubectl -n <ns> get events --sort-by=.lastTimestamp -w`. Expected sequence on a fresh bootstrap:

```text
Normal   Provisioning            persistentvolumeclaim/vs-prime-<uuid>
Normal   SnapshotReady           volumesnapshot/volsync-${APP}-bootstrap-dest-<ts>
Normal   SnapshotCreated         volumesnapshot/volsync-${APP}-bootstrap-dest-<ts>
Normal   ProvisioningSucceeded   persistentvolumeclaim/vs-prime-<uuid>
Normal   SuccessfulAttachVolume  pod/<app>-<rs>-<id>
Normal   Pulling                 pod/<app>-<rs>-<id>
Normal   Created                 pod/<app>-<rs>-<id>     init-config
Normal   Started                 pod/<app>-<rs>-<id>     app
Normal   InstallSucceeded        helmrelease/${APP}
Normal   VolSyncPopulatorFinished persistentvolumeclaim/${APP}
Warning  ClaimMisbound           persistentvolumeclaim/vs-prime-<uuid>   (transient — both PVCs bound to same PV mid-clone)
```

The `ClaimMisbound` warning on `vs-prime-*` is **expected** during the populate window. The vs-prime PVC and the real `${APP}` PVC briefly point at the same PV while the clone is in-flight; the populator deletes vs-prime once the clone finishes. Don't treat it as an error.

---

## Required substitution variables

Set in the app's per-app Flux `Kustomization` (`apps/production/<app>/kustomization.yaml`) under `spec.postBuild.substitute`. The `volsync-garage-base` Secret in `flux-system` provides the restic repo URL/credentials via `substituteFrom`.

### Common (all variants)

| Var | Example | Notes |
|---|---|---|
| `APP` | `donetick` | PVC name, RS/RD name, restic repo Secret name |
| `APP_NAMESPACE` | `donetick` | Where volsync resources land |
| `VOLSYNC_STORAGECLASS` | `ceph-rbd` / `cephfs` | Storage class for `${APP}` PVC and snapshot temp PVCs |
| `VOLSYNC_SNAPSHOTCLASS` | `ceph-rbd-snapclass` / `cephfs-snapclass` | **Must match the storage class.** |
| `VOLSYNC_ACCESSMODES` | `ReadWriteOnce` / `ReadWriteMany` | App-side access mode |
| `VOLSYNC_SNAP_ACCESSMODES` | `ReadWriteOnce` / `ReadWriteMany` | **For CephFS apps set to `ReadWriteMany`.** |
| `CAPACITY` | `5Gi` | App PVC size |
| `VOLSYNC_REPO_PATH` | `donetick/data` | Restic repo path within the bucket (`s3://volsync/<path>`) |
| `VOLSYNC_CACHE_STORAGECLASS` | `ceph-rbd` | Mover cache PVC storage class |
| `VOLSYNC_CACHE_ACCESSMODES` | `ReadWriteOnce` | Mover cache PVC access mode |
| `VOLSYNC_CACHE_CAPACITY` | `1Gi`–`10Gi` | Cache size; rule of thumb 25–50% of source PVC |
| `VOLSYNC_PUID` / `VOLSYNC_PGID` | `1000` | **Backup** mover uid/gid — must match what the app runs as so the mover can read its files during RS snapshot |
| `VOLSYNC_RESTORE_PUID` / `VOLSYNC_RESTORE_PGID` | `1000` | **Restore** mover uid/gid for the bootstrap RD. Defaults to `1000` regardless of `VOLSYNC_PUID`. See note below. |

> **Root-uid apps (`VOLSYNC_PUID=0`):** volsync's mover container drops `CAP_CHOWN` (hardcoded, not configurable). Even running as uid 0, restic cannot `lchown` files and exits non-zero, causing the bootstrap job to fail. The fix is to leave `VOLSYNC_RESTORE_PUID` at the default `1000`. The restored files will be owned by uid 1000, but apps running as root bypass permission checks and can read them normally.
>
> **Postgres / uid-999 apps:** You MUST set `VOLSYNC_RESTORE_PUID: "999"` and `VOLSYNC_RESTORE_PGID: "999"` explicitly. Postgres cannot read files owned by uid 1000.
>
> **All other apps (uid 1000):** The default is correct — no action needed.

### Optional (have sane defaults)

| Var | Default | Notes |
|---|---|---|
| `VOLSYNC_SCHEDULE` | `05 00/12 * * *` | RS cron. Override for hourly/daily during a PoC. |
| `VOLSYNC_BOOTSTRAP_TOKEN` (bootstrap only) | `bootstrap-once` | Bumping re-triggers the bootstrap RD |
| `VOLSYNC_RESTORE_TOKEN` (restore only) | `restore-once` | Bumping re-triggers the DR-probe RD |

### `backup-only` (additional)

| Var | Notes |
|---|---|
| `VOLSYNC_SOURCE_PVC` | Name of the existing PVC the workload mounts. For Bitnami StatefulSets, this is something like `data-myapp-postgresql-0`, NOT just `myapp-postgresql`. |

---

## Playbook: staging → production migration with data preservation

This is the canonical 3-stage flow we used for the donetick PoC (#131 → #136 → settle PR). Total wall-clock for the actual migration step is under 2 minutes; the pre-flight backup is the long pole (length depends on data size and the schedule cron you pick).

### Stage 1: pre-flight backup (separate PR)

Goal: get the app's existing data into Garage S3 *before* anything destructive happens.

The app currently lives in `apps/staging/` with a static PVC. We add a per-app Flux Kustomization in `apps/production/<app>/` that uses `volsync-v2/backup-only` against the existing PVC, pointed at a stub kustomize base so it doesn't conflict with the staging-tier ownership of namespace/HelmRelease/etc.

```yaml
# apps/production/<app>/kustomization.yaml
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: <app>
  namespace: flux-system
spec:
  interval: 1h
  path: ./apps/base/_volsync-stub          # <-- empty kustomize base, just an anchor
  targetNamespace: <app>
  prune: true
  decryption: { provider: sops, secretRef: { name: sops-age } }
  sourceRef: { kind: GitRepository, name: flux-system, namespace: flux-system }
  components:
    - ../../../components/volsync-v2/backup-only
    - ../../../components/volsync/remote
  postBuild:
    substitute:
      APP: <app>
      VOLSYNC_SOURCE_PVC: <app>            # the existing staging-tier PVC name
      VOLSYNC_REPO_PATH: <app>/data
      VOLSYNC_STORAGECLASS: ceph-rbd
      VOLSYNC_SNAPSHOTCLASS: ceph-rbd-snapclass
      VOLSYNC_SCHEDULE: "0 * * * *"        # hourly during PoC; remove in stage 3
      # ... cache/PUID/PGID
    substituteFrom:
      - kind: Secret
        name: volsync-garage-base
```

Also list the new file in `apps/production/kustomization.yaml`. **Do not** touch `apps/staging/`.

The `apps/base/_volsync-stub/` directory contains only:

```yaml
# apps/base/_volsync-stub/kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources: []
```

It exists solely to give the per-app Flux Kustomization a `path:` target where it can stack components without rendering any real resources. The components add the RS + Secret on top.

After merge, verify:

```bash
kubectl -n <app> get replicationsource <app> -o jsonpath='{.status.lastSyncTime}{"\n"}{.status.latestMoverStatus.result}'
# Want: a recent timestamp + "Successful"
```

The same stub is reused for every app that goes through this flow — it's not per-app overhead.

### Stage 2: migration (separate PR)

Once the backup is verified in S3:

1. Update the per-app Flux Kustomization:
   - `path: ./apps/base/_volsync-stub` → `./apps/base/<app>`
   - `components: volsync-v2/backup-only` → `volsync-v2/bootstrap`
   - Add the canonical companion components: `gatus/internal`, `gatus/external`
   - Drop `VOLSYNC_SOURCE_PVC` (volsync owns the PVC now)
   - Add `CAPACITY`, `SUBDOMAIN`, `GATUS_*`
   - **Keep** `VOLSYNC_SCHEDULE: "0 * * * *"` for now (settle to default in stage 3)
2. In `apps/base/<app>/kustomization.yaml`, comment out `<app>-pvc.yaml`:

   ```yaml
   resources:
     - <app>-namespace.yaml
     # PVC owned by volsync-v2 (bootstrap on first deploy, plain after settle)
     # - <app>-pvc.yaml
     - <app>-secret.sops.yaml
     - <app>-release.yaml
     - <app>-ingressroute.yaml
   ```

3. In `apps/staging/kustomization.yaml`, comment out the `- <app>/` line.
4. Delete `apps/staging/<app>/`.

On reconcile:

- The `apps` (staging) Flux Kustomization sees `<app>` is no longer in its rendered set and prunes it. Because of `prune: true` and the ceph-rbd default reclaim policy of `Delete`, the old PV's RBD image is destroyed — **the in-cluster copy of the data is gone at this moment**. The S3 backup is the only copy.
- The `<app>` per-app Flux Kustomization renders `apps/base/<app>` plus the bootstrap component. The new `${APP}` PVC is created with `dataSourceRef`, the bootstrap RD runs, the populator chain produces a populated PVC, and the HelmRelease starts.

Verify:

```bash
kubectl -n <app> get pvc <app>
# Want: STATUS=Bound, new VOLUME UID
kubectl -n <app> get pvc <app> -o jsonpath='{.spec.dataSourceRef}{"\n"}'
# Want: ReplicationDestination kind, <app>-bootstrap name
kubectl -n <app> get pod
# Want: 1/1 Running for the app pod
```

Open the app's UI; sanity-check that user data is intact.

### Stage 3: settle (separate PR)

Once the data is verified intact and at least one **scheduled** RS run has succeeded after the migration (proving incremental backups continue cleanly on the same restic repo):

1. In the per-app Flux Kustomization:
   - `components: volsync-v2/bootstrap` → `volsync-v2`
   - Remove the `VOLSYNC_SCHEDULE: "0 * * * *"` line (canonical 12h resumes)

That's the whole settle commit. On reconcile, Flux prunes the now-orphaned `${APP}-bootstrap` ReplicationDestination. The PVC remains (it already exists; `dataSourceRef` is only consulted at creation). The RS continues unchanged.

---

## Other scenarios this enables

### Cross-cluster migration (future second cluster)

When the homelab grows to a real two-cluster setup (small staging cluster + production), the bootstrap pattern moves an app between clusters with no special tooling:

1. Source cluster keeps backing up to Garage via `volsync-v2`.
2. Destination cluster deploys the app with `volsync-v2/bootstrap` — first boot pulls from the same restic repo in Garage.
3. Source cluster's app is removed (or kept as a warm standby).

The S3 restic repo is the bridge; the cluster boundary is invisible to the data. Only requirement: both clusters have the same `volsync-garage-base` Secret in `flux-system` (or each cluster has its own Secret pointing at the same Garage backend with the same restic password).

### Disaster recovery — restore a lost cluster from S3

After rebuilding a fresh cluster and bootstrapping Flux with the same Garage credentials Secret, every app deployed with `volsync-v2/bootstrap` self-restores from its S3 backup on first reconcile. No per-app manual restore steps.

This is the strongest argument for migrating apps onto `bootstrap` even if they don't strictly need data preservation today: the cluster as a whole becomes restorable from S3 alone.

### Verifying a backup is actually restorable (without risking the live PVC)

Use `volsync-v2/restore`. It creates a separate `${APP}-restore` PVC populated from S3. Mount it in a debug pod, inspect contents, then delete the PVC + RD. The live `${APP}` PVC is never touched.

---

## Troubleshooting

### Bootstrap RD doesn't complete

```bash
kubectl -n <app> get replicationdestination <app>-bootstrap -o yaml
```

Look for `status.conditions`. Common causes:

- **Restic repo doesn't exist or has no snapshots** — usually means Stage 1 (pre-flight) didn't actually run a successful backup. Check `kubectl -n <app> get replicationsource` from the pre-flight PR; `lastSyncTime` should be set.
- **`volsync-garage-base` Secret missing values** — verify with `kubectl -n flux-system get secret volsync-garage-base -o jsonpath='{.data}'`. Should have `RESTIC_PASSWORD`, `AWS_ACCESS_KEY_ID`, etc.
- **Snapshot class mismatch** — `VOLSYNC_SNAPSHOTCLASS` must match the storage class. CephFS apps often forget to set both `VOLSYNC_STORAGECLASS=cephfs` and `VOLSYNC_SNAPSHOTCLASS=cephfs-snapclass`.

### PVC stuck in Pending with no events

Volsync VolumePopulator only triggers once the RD reports `Status.LatestImage`. If the RD is still running, the PVC waits. This is normal up to ~2 minutes for a small PVC; longer for big ones.

If it hangs forever, check the volsync controller logs:

```bash
kubectl -n volsync-system logs -l app.kubernetes.io/name=volsync --tail=200
```

### Pod starts but the app reports "no data"

Three possibilities:

1. **Wrong subPath in the HelmRelease.** The PVC contains the restored data at the same paths it was backed up from. If your HelmRelease mounts a different `subPath` than what was on the staging PVC, you see an empty mount. Check `apps/base/<app>/<app>-release.yaml` `persistence:` block.
2. **Wrong restic repo path.** `VOLSYNC_REPO_PATH` mismatches between Stage 1 and Stage 2 → bootstrap restores from a different (or empty) repo. Both stages must use the exact same `VOLSYNC_REPO_PATH`.
3. **File ownership broken.** If the app runs as a non-1000 user but `VOLSYNC_PUID/PGID` weren't set, the restored files have wrong ownership and the app can't read them. Fix `VOLSYNC_PUID/PGID` and re-bootstrap (delete the PVC and bump `VOLSYNC_BOOTSTRAP_TOKEN`).

### `ClaimMisbound` warning on `vs-prime-*` PVC

Expected and transient — see "Events you'll see" above. Disappears once the populator finishes (~5–30 seconds).

### "Two RS objects targeting the same PVC" after Stage 2

You forgot to remove the Stage 1 backup-only setup, or the Stage 2 PR was merged before Stage 1 was cleaned up. Both `backup-only` and `bootstrap`'s `backup/` produce a `ReplicationSource/${APP}` — same name, no conflict, but redundant. The Stage 2 PR should replace, not stack. If you see this, just merge a fix removing the duplicate.

---

## Maintenance

- `volsync-v2` will be renamed to `volsync` once `components/volsync/remote/` is folded in (Phase 4 of the volsync cleanup project).
- Keep this README in sync with the actual component shape — diverging docs are worse than no docs. When you add a substitution variable to one of the manifests, update the corresponding table here in the same PR.
- The `apps/base/_volsync-stub/` shared empty base is referenced by every app's pre-flight PR. Don't delete it until all apps have completed their Stage 2 migration.
