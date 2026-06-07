# Label-driven volsync backups — how to set up backups for an app

This is the current pattern for backing up an app's PersistentVolumeClaim. It
replaced the retired `components/volsync-v2` Kustomize Components (removed in
PR #558) — there is nothing per-app to wire up beyond two labels.

Engine: **Kopia**. (The cluster ran Restic through early 2026; that is fully
removed — single engine, single repo, single policy.)

## TL;DR

Put two labels on the PVC:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: my-app
  labels:
    backup: daily          # daily | hourly  → schedule
    backup-engine: kopia    # required — the only supported engine
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: ceph-rbd
  resources:
    requests:
      storage: 5Gi
```

[`ClusterPolicy/volsync-pvc-backup-restore-kopia`](../infrastructure/controllers/kyverno/policies/volsync-pvc-backup-restore-kopia.yaml)
sees the labels and generates three resources, all owned by Kyverno's
background controller with `synchronize: true` — remove the labels and they are
garbage-collected:

| Resource | Name | Purpose |
|---|---|---|
| `Secret` | `volsync-<pvc>` | Kopia repo connection — `KOPIA_REPOSITORY` + `KOPIA_PASSWORD` + `KOPIA_S3_*` + AWS creds |
| `ReplicationSource` | `<pvc>-backup` | VolSync RS — runs the backup on the labelled schedule |
| `ReplicationDestination` | `<pvc>-backup` | VolSync RD — `dataSourceRef` populator for restore-on-fresh-PVC |

A companion policy,
[`volsync-pvc-engine-required`](../infrastructure/controllers/kyverno/policies/volsync-pvc-engine-required.yaml),
rejects a PVC that has a `backup` label but no `backup-engine: kopia` — you
cannot half-configure it.

## Choose `daily` or `hourly`

| Label | Schedule | Use when |
|---|---|---|
| `backup: daily` | `<min> 2 * * *` (daily, 02:MM UTC) | Config-only / low-churn data. **Default.** |
| `backup: hourly` | `<min> * * * *` (top of every hour) | High-churn data — non-CNPG databases, password vaults, frequently-edited content |

`<min> = length(namespace) % 60` — spreads schedules so backups don't all fire
at once. Retention is handled centrally by Kopia repo policy (and the
`KopiaMaintenance` CRD), not per-ReplicationSource.

Note: in-cluster Postgres is **not** backed up this way — those apps use
CloudNativePG (Layer 10, WAL streaming + base backups). CNPG-managed PVCs are
explicitly excluded from this policy.

## How the point-in-time copy is taken (storage classes)

`copyMethod: Snapshot` — VolSync takes a crash-consistent `VolumeSnapshot`, then
provisions a point-in-time (PiT) copy PVC from it, and the mover backs up the
copy. How the PiT copy is provisioned depends on the source PVC's storage class:

| Source storage class | PiT copy StorageClass | PiT access mode | Mechanism |
|---|---|---|---|
| `ceph-rbd` (default) | `ceph-rbd` | `ReadWriteOnce` | RBD copy-on-write clone — cheap, near-instant |
| `cephfs` | `cephfs-backingsnapshot` | `ReadOnlyMany` | **Shallow snapshot mount** — ceph-csi serves the snapshot's `.snap` directory read-only; zero-copy, no subvolume clone |

The cephfs `backingSnapshot` path (Phase 2, 2026-05-22) exists because a full
CephFS subvolume clone copies the tree file-by-file inside ceph-mgr — its cost
scales with **inode count**, not bytes. High-file-count PVCs (e.g. `dumb`,
~290k files) overran the CSI provisioner's `CreateVolume` timeout and wedged in
a retry loop. The shallow mount eliminates the clone entirely while keeping the
backup point-in-time correct. The policy picks the class/access-mode via its
`pitStorageClass` / `pitAccessMode` context variables; see
`infrastructure/controllers/storage/cephfs/storageclass-cephfs-backingsnapshot.yaml`.

## Optional escape hatch — skip the restore

To bring up a fresh empty PVC (recovering from corruption rather than restoring
from backup), set both annotations:

```yaml
metadata:
  annotations:
    volsync.backup/skip-restore: "true"
    volsync.backup/skip-restore-reason: "rebuilding from scratch after corruption 2026-05-15"
```

While `skip-restore=true` is on the PVC:
- the mutate rule does NOT inject `dataSourceRef` → the PVC binds empty;
- the generate rules **skip** — no Secret, no RS, no RD. The PVC is unprotected
  until the annotation is removed.

This is intentional: an "empty-PVC-but-still-backing-up" PVC would age good
historical snapshots out by churn. Resuming protection should be a deliberate
act. Alerting fires at T+1h (info) and T+24h (warn) for any PVC still carrying
`skip-restore=true`.

## Optional — larger mover cache

The mover cache PVC defaults to 5 GiB. For large sources, override:

```yaml
metadata:
  annotations:
    volsync.backup/cache-capacity: 10Gi
```

## Excluded namespaces

Adding the backup labels in any of these namespaces is a **no-op** — the policy
excludes them to avoid admission deadlocks during bootstrap:

`flux-system`, `kube-system`, `kyverno`, `volsync-system`, `cert-manager`,
`traefik`, `ceph-csi-rbd`, `ceph-csi-cephfs`, `monitoring`, `tailscale`,
`cloudflared`, `newt`, `intel-gpu`.

## The shared Kopia repo

All apps back up into one Garage S3 bucket: `s3://garage.lab.mainertoo.com/volsync-kopia`
(`KOPIA_S3_BUCKET=volsync-kopia`). Each app is a distinct Kopia **source
identity** — `<pvc>-backup@<namespace>:/data` (the RS's `username` / `hostname`
/ `sourcePathOverride`). Kopia is a concurrent multi-writer repo and
deduplicates blobs across every app.

Cluster credentials live in `Secret/flux-system/volsync-kopia-shared-base`
(`KOPIA_PASSWORD` + AWS keys). The per-PVC `volsync-<pvc>` Secret the policy
generates is a scoped copy of those plus the hardcoded `KOPIA_REPOSITORY` /
`KOPIA_S3_*` values.

Periodic repo maintenance runs daily at **09:00 UTC** via a `KopiaMaintenance`
CR (`infrastructure/controllers/volsync/app/volsync-kopia-maintenance.yaml`)
— compacts index blobs, GCs unreferenced contents. One CR for the whole
fleet (maintenance is repo-wide). Without it, the index fragments and every
backup mover prints a "Found too many index blobs" warning.

## Troubleshooting

### "I added the labels but nothing happened"

```bash
kubectl -n <ns> get policyreport
kubectl -n <ns> get secret volsync-<pvc>
kubectl -n <ns> get replicationsource.volsync.backube <pvc>-backup
kubectl -n <ns> get replicationdestination.volsync.backube <pvc>-backup
```

If the trio is missing: confirm the namespace isn't excluded, both labels are
present, and `spec.storageClassName` is `cephfs` or `ceph-rbd`. Kyverno's
background-controller logs are in the `kyverno` namespace.

### "Backup runs but the oracle says exists=false"

The restore oracle is `pvc-plumber` (Kopia, HTTP-only) in `volsync-system`:

```bash
kubectl -n volsync-system port-forward svc/pvc-plumber 18080:80 &
curl -s http://localhost:18080/exists/<ns>/<pvc> | jq .
```

`authoritative: false` → pvc-plumber can't reach the Kopia repo (check the pod
is Ready and Garage is reachable). `authoritative: true, exists: false` → either
no snapshots for that source identity yet (check the RS `status`), **or** the
movers and the oracle are reading different repos.

> ⚠️ **The two-repos (prefix/root) trap.** If `authoritative: true, exists:
> false` for an app whose RS shows recent successful syncs, the movers are
> probably writing under an S3 **prefix** the oracle doesn't read. The mover's
> `entry.sh` derives the prefix from the path segment of `KOPIA_REPOSITORY`,
> while pvc-plumber always reads the bucket **root** — so a stray path in
> `KOPIA_REPOSITORY` splits one bucket into two repos that never meet, and the
> oracle confidently reports `exists: false` for the whole fleet. Confirm by
> listing both repos (connect with and without `--prefix=volsync-kopia/`); the
> fix is to keep `KOPIA_REPOSITORY` path-less (root). **And if you ever change the
> repo location, you must also delete the `volsync-{src,dst}-<pvc>-backup-cache`
> PVCs** — the mover skips reconnect when `/cache/kopia.config` already points
> somewhere ("Repository already connected"), so a warm cache keeps using the old
> location. Full write-up: `docs/volsync-kopia-oracle-prefix-mismatch.md`.

### Validation mode

The kopia policy runs `validationFailureAction: Audit` — it never blocks PVC
admission; violations are `PolicyReport` entries only. The
`volsync-pvc-engine-required` policy does deny a PVC carrying `backup` without
`backup-engine: kopia`.

## Retiring an app's backups

Remove the `backup` label — Kyverno garbage-collects the Secret/RS/RD. **Old
Kopia snapshots stay in the shared repo** until pruned manually
(`kopia snapshot list --all` then `kopia snapshot delete` against the
`<pvc>-backup@<ns>` identity, using the shared key + password).

## Special case — `dumb` (label-driven, plus a `.kopiaignore`)

`dumb` **is** an ordinary label-driven cephfs app now: its PVC carries
`backup: daily` + `backup-engine: kopia`, and the Kyverno policy generates the
Kopia Secret + RS/RD using the cephfs `backingSnapshot` shallow-mount path
(zero-copy, no clone). It was the canary for that Phase-2 fix and was folded back
onto the policy in PR #571 — the hand-written RS/RD was retired, the same Kopia
source identity (`dumb-backup@dumb`) preserved, no history lost.

**`.kopiaignore` (lives in the PVC, not git):** a `/.kopiaignore` at the PVC root
excludes `data/neutarr/*/.cache/` — neutarr's poetry/pip HTTP cache. It's
ephemeral (rebuilt every boot) and its leaf dirs are created `root:root 0700`
under setgid parents, so the gid-1000 Kopia mover can't descend them. Without the
exclude, every backup failed with ~44 `permission denied` fatal errors →
`Failed to create snapshot` (added + verified 2026-06-02). Because the file is
PVC-resident rather than GitOps-managed, it rides along inside the backup but is
**not** recreated by a from-scratch repo deploy — re-add it after a bare PVC
restore. To force a fresh snapshot that picks up an edited `.kopiaignore`, delete
the backingsnapshot PVC `<rs>-src` (deleting the VolumeSnapshot alone deadlocks on
the `as-source-protection` finalizer).
