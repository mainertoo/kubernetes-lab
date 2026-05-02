# volsync-v2 Components

Reusable Kustomize Components that wire apps into the volsync backup/restore lifecycle. Pick the variant that matches the app's PVC ownership and lifecycle stage.

S3 backend: Garage. Each app uses a dedicated restic repo named `${APP}-volsync` whose credentials live in a per-app `Secret` provided by `components/volsync/remote/`. (The `volsync-v2` parent directory will eventually be renamed to `volsync` once consolidation finishes.)

---

## Component variants

| Variant | PVC ownership | Use when |
|---|---|---|
| `volsync-v2` (default) | volsync owns the empty `${APP}` PVC | New app, no pre-existing data. Default canonical pattern. |
| `volsync-v2/backup-only` | App owns its own PVC (HelmRelease persistence, StatefulSet `volumeClaimTemplates`, or a static PVC manifest in `apps/base/<app>/`) | Volsync only adds a `ReplicationSource`; PVC creation is the app's responsibility. |
| `volsync-v2/bootstrap` | volsync owns the `${APP}` PVC, populated on first boot from an S3 restic snapshot | First-boot data restore. Used for staging→production migrations, cross-cluster moves, and disaster recovery. |
| `volsync-v2/restore` | Creates a separate `${APP}-restore` PVC populated from S3 | Disaster-recovery probe. Lets you spin up a copy of the data alongside the live PVC without disturbing it. |

All four use the same `${APP}-volsync` restic repo and the same per-app credentials. They are mutually exclusive in any given app's kustomization (don't combine `volsync-v2` and `bootstrap` — the PVC name `${APP}` would collide).

---

## How each variant is wired

### `volsync-v2` (default — empty PVC + backup)

```text
volsync-v2/kustomization.yaml
├── pvc/                  → empty ${APP} PVC, volsync-owned
└── backup/               → ${APP} ReplicationSource (scheduled backup to S3)
```

App's `apps/base/<app>/kustomization.yaml` **must comment out** any static `${APP}-pvc.yaml` declaration so it doesn't fight with the volsync-managed PVC.

### `volsync-v2/backup-only`

```text
volsync-v2/backup-only/kustomization.yaml
└── volsync-replicationsource.yaml   → ReplicationSource targeting VOLSYNC_SOURCE_PVC
```

The PVC is created by the app itself. Set `VOLSYNC_SOURCE_PVC` in the app's `postBuild.substitute` to the actual PVC name the workload mounts.

Used today by `grocy` and `mealie` (both have HelmRelease-managed PVCs).

### `volsync-v2/bootstrap` (first-boot from S3 + backup)

```text
volsync-v2/bootstrap/kustomization.yaml
├── pvc-with-datasource/  → ${APP} PVC with dataSourceRef → ${APP}-bootstrap RD
├── ../backup/            → reuses the canonical backup ReplicationSource
└── replicationdestination/ → ${APP}-bootstrap RD (manual trigger, pulls from S3)
```

When the kustomization first reconciles:

1. The `${APP}-bootstrap` ReplicationDestination is created with `trigger: manual: bootstrap-once`. Volsync pulls the latest snapshot from `${APP}-volsync` restic repo.
2. The `${APP}` PVC is provisioned by the CSI driver using the snapshot the RD produced (`dataSourceRef`). On creation the PVC contains the restored data.
3. The `${APP}` ReplicationSource starts the normal scheduled backup cadence — incremental on top of the existing restic repo (no full re-backup).

After data is verified intact and the first scheduled backup has run successfully, switch the app's kustomization from `volsync-v2/bootstrap` to plain `volsync-v2`. The PVC and RS persist; the bootstrap RD is pruned.

`dataSourceRef` is consulted only at PVC creation, so leaving the bootstrap component in place by accident is harmless once the PVC exists. Remove it anyway to keep the cluster on the canonical pattern.

### `volsync-v2/restore` (DR probe)

Creates `${APP}-restore` (a separate PVC) populated from S3. The live `${APP}` PVC is untouched. Useful for:

- Verifying that backups in S3 are actually restorable
- Cloning prod data into a sandbox namespace
- Pre-checking a disaster-recovery procedure before performing it

Bumping `VOLSYNC_RESTORE_TOKEN` triggers a fresh restore.

---

## Required substitution variables

Set in the app's per-app Flux `Kustomization` (`apps/production/<app>/kustomization.yaml`) under `spec.postBuild.substitute`. `volsync-garage-base` Secret provides the restic repo URL/credentials via `substituteFrom`.

### Common (all variants)

| Var | Example | Notes |
|---|---|---|
| `APP` | `donetick` | Used as PVC name, RS/RD name, restic repo name. |
| `APP_NAMESPACE` | `donetick` | Where the volsync resources are created. |
| `VOLSYNC_STORAGECLASS` | `ceph-rbd` / `cephfs` | Storage class for the `${APP}` PVC and snapshot temp PVCs. |
| `VOLSYNC_SNAPSHOTCLASS` | `ceph-rbd-snapclass` / `cephfs-snapclass` | Must match the storage class. |
| `VOLSYNC_ACCESSMODES` | `ReadWriteOnce` / `ReadWriteMany` | App-side access mode. |
| `VOLSYNC_SNAP_ACCESSMODES` | `ReadWriteOnce` / `ReadWriteMany` | **For CephFS apps set to `ReadWriteMany`.** |
| `CAPACITY` | `5Gi` | App PVC size. |
| `VOLSYNC_REPO_PATH` | `donetick/data` | Restic repo path within the bucket. |
| `VOLSYNC_CACHE_*` | (defaults usually fine) | Mover cache PVC settings. |
| `VOLSYNC_PUID` / `VOLSYNC_PGID` | `1000` | Mover security context. |

### `bootstrap` (additional)

| Var | Default | Notes |
|---|---|---|
| `VOLSYNC_BOOTSTRAP_TOKEN` | `bootstrap-once` | Bumping this re-triggers the bootstrap RD. Usually leave at default. |

### `backup-only` (additional)

| Var | Notes |
|---|---|
| `VOLSYNC_SOURCE_PVC` | The actual PVC name the workload mounts (e.g., `data-myapp-postgresql-0` for a Bitnami StatefulSet). |

---

## Playbook: staging → production migration with data preservation

Use this when an app currently lives in `apps/staging/` (with a static PVC declaration in `apps/base/<app>/`) and needs to move to the canonical `apps/production/` tier without losing data.

### 1. Pre-flight — back up the staging PVC to S3

Create a one-off `ReplicationSource` for the staging app's existing PVC. Write to the **same** `${APP}-volsync` restic repo path the production app will use. Wait for the first backup to complete and verify in Garage.

```bash
# Verify the snapshot exists
kubectl get replicationsource -n <ns> <app> -o jsonpath='{.status.latestMoverStatus}'
```

### 2. Belt-and-suspenders — local snapshot

Take a CSI VolumeSnapshot of the source PVC as a cluster-local recovery point:

```yaml
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: <app>-pre-migration
  namespace: <app>
spec:
  volumeSnapshotClassName: ceph-rbd-snapclass    # or cephfs-snapclass
  source:
    persistentVolumeClaimName: <app>
```

### 3. Migration PR

In one commit:

- Remove `<app>` from `apps/staging/kustomization.yaml`.
- Comment out `<app>-pvc.yaml` in `apps/base/<app>/kustomization.yaml` (volsync will own the PVC).
- Add `apps/production/<app>/kustomization.yaml` with `components/volsync-v2/bootstrap` (NOT plain `volsync-v2`).
- Add `apps/production/<app>` to `apps/production/kustomization.yaml`.

Reconcile and verify: the `${APP}-bootstrap` ReplicationDestination should report `Completed`, the `${APP}` PVC should bind with intact data, and the app pod should start.

### 4. Post-migration cleanup

Once the next scheduled backup has run successfully (12h+):

- Open a follow-up PR switching the app from `volsync-v2/bootstrap` to plain `volsync-v2`.
- Delete the pre-migration VolumeSnapshot from step 2.

---

## Other scenarios this enables

### Cross-cluster migration (future)

When the homelab grows to a real two-cluster setup (small staging cluster + production), the bootstrap pattern moves an app between clusters with no special tooling:

1. Source cluster keeps backing up to Garage via `volsync-v2`.
2. Destination cluster deploys the app with `volsync-v2/bootstrap` — first boot pulls from the same restic repo in Garage.
3. Source cluster's app is removed.

The S3 restic repo is the bridge; the cluster boundary is invisible to the data.

### Disaster recovery — restore a lost cluster from S3

After rebuilding a fresh cluster and bootstrapping Flux with the same Garage credentials Secret, every app deployed with `volsync-v2/bootstrap` will restore itself from its S3 backup on first reconcile. No manual restore steps per app.

---

## Maintenance

- `volsync-v2` will be renamed to `volsync` once the v1 leftovers under `components/volsync/remote/` are folded in (Phase 4).
- Keep this README in sync with the actual component shape — diverging docs are worse than no docs.
