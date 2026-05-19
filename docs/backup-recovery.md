# Backup Recovery Runbook

> Recovery procedures for every backup layer documented in `docs/backup-architecture.md`. Read that first if you need context.
>
> Convention: `$` = run on your laptop, `kopia#` = inside the kopia LXC, `pbs#` = on the PBS VM, `pve-X#` = on a Proxmox node, `k8s$` = on a k3s master with kubectl.

---

## 0. Triage — what's broken?

Run this first. It tells you which layers are usable.

```bash
# Layer 1 (PBS reachable + datastore mounted)
$ ssh pbs 'mount | grep pbs-backups; proxmox-backup-manager datastore list'

# Layer 2 (volsync + Garage healthy)
$ ssh pve-ugreen 'kubectl get pods -n volsync-system; \
  kubectl get pods -n garage 2>/dev/null || kubectl get pods -A | grep garage; \
  kubectl get replicationsources -A | head'

# Layer 3+4+5 (Kopia repo healthy)
$ ssh kopia 'kopia repository status; kopia snapshot list | tail -20'

# Layer 10 (CNPG operator + clusters healthy + recent backups)
$ kubectl -n cnpg-system get pods -l app.kubernetes.io/name=cloudnative-pg
$ kubectl get cluster.postgresql.cnpg.io -A
$ kubectl get scheduledbackup -A
$ kubectl get backup -A --sort-by='.status.startedAt' | tail

# Inventory: latest k3s-pv-index CSV
$ ssh ubuntu@192.168.90.161 'sudo head -20 /var/backups/pv-index-k3s.csv'

# Inventory: latest .meta.txt for an RBD image
$ ssh pve-ugreen 'ls /mnt/pve/cephfs-swarm/rbd-backup/ | head; \
  ls /mnt/pve/cephfs-swarm/rbd-backup/$(ls /mnt/pve/cephfs-swarm/rbd-backup | head -1)'
```

If Kopia, PBS, volsync, AND CNPG all pass, you have all backup paths available.

---

## 1. Restore a single PVC — preferred: volsync bootstrap (Layer 2)

Use this when the cluster is healthy and you want to roll a single PVC back to a snapshot in Garage.

### Pattern

```yaml
# apps/base/<app>/kustomization.yaml
components:
  - ../../../../components/volsync-v2/bootstrap   # was: components/volsync-v2

# apps/production/<app>/kustomization.yaml: bump VOLSYNC_RESTORE_TOKEN to force re-restore
postBuild:
  substitute:
    APP: <app>
    VOLSYNC_RESTORE_TOKEN: "<bump-this-on-each-restore>"
```

### Steps

1. Stop the app — scale Deployment/StatefulSet to 0 *or* suspend the HelmRelease:
   ```bash
   k8s$ flux suspend hr -n <ns> <app>
   k8s$ kubectl -n <ns> scale deploy <app> --replicas=0
   ```
2. Delete the existing PVC (the bootstrap component re-creates it from a `dataSourceRef`):
   ```bash
   k8s$ kubectl -n <ns> delete pvc <app>
   ```
3. Switch the app's kustomization to use `components/volsync-v2/bootstrap` (instead of `volsync-v2`). Commit and push.
4. Flux reconciles → `ReplicationDestination/<app>-bootstrap` runs in `manual` mode, pulls latest snapshot, stages a CSI VolumeSnapshot. The new PVC is created with `dataSourceRef` pointing at it; the volume populator waits for the RD then provisions.
5. Watch:
   ```bash
   k8s$ kubectl -n <ns> get replicationdestination -w
   k8s$ kubectl -n <ns> get pvc -w
   ```
6. Once the PVC is `Bound` and the RD is `Synchronized`, scale the app back up / resume the HelmRelease.
7. **Settle**: switch the kustomization back from `volsync-v2/bootstrap` → plain `volsync-v2` and commit. Leaving bootstrap is idempotent (`dataSourceRef` only consulted at PVC creation), but settling keeps the cluster on the canonical pattern.

### Restore a *specific older* snapshot (not latest)

Add `restoreAsOf: "2026-04-01T00:00:00Z"` to the RD spec via a kustomize patch in the bootstrap component (or `previous: 2` to skip the most recent N).

### Useful: side-by-side restore (don't disturb live PVC)

Use `components/volsync-v2/restore` instead. Creates `<app>-restore` PVC alongside the live one, populated from S3. Bump `VOLSYNC_RESTORE_TOKEN` to refresh. Mount it from a debug pod to inspect.

---

## 1b. Restore a CNPG postgres cluster (Layer 10)

For any of the 8 CNPG-managed databases (`joplin-db`, `zilean-db`, `riven-db`, `dawarich-db`, `authentik-db`, `opencut-cnpg-db`, `sparky-fitness-cnpg-db`, `wiki-js-cnpg-db`).

### Restore the LATEST state

Just delete the Cluster pod — CNPG promotes the standby (single-instance clusters self-heal from WAL on the same PVC). No backup needed. WAL streaming to S3 is for *point-in-time* recovery, not last-write recovery.

```bash
k8s$ kubectl -n <ns> delete pod <cluster>-1
# CNPG auto-creates a replacement on the same PVC; data intact
```

### Restore to a SPECIFIC point in time (within 30-day retention)

Use the [`components/cnpg-cluster/recovery`](../components/cnpg-cluster/recovery/)
Component. It creates a fresh Cluster restored from the S3 base backup + WAL
chain. Full walk-through (per-app and cluster-nuke variants, plus PITR target
patching) lives in [`cnpg-disaster-recovery.md`](cnpg-disaster-recovery.md).

Tested 2026-05-19 against joplin-db: 26 tables / row counts identical / ~2.5 min
recovery time for a 10 GiB cluster.

### Restore the entire 8-app fleet (cluster-nuke scenario)

See [`cnpg-disaster-recovery.md`](cnpg-disaster-recovery.md) §2. Each app's
`db-cnpg.yaml` flips its `components:` reference from `cnpg-cluster` to
`cnpg-cluster/recovery`; Flux reconciles all 8 in parallel.

### Operator-only recovery

The CNPG operator pod itself is stateless. A fresh Flux reconcile of `infrastructure/controllers/cnpg/` re-creates it. The Cluster CRDs in app namespaces survive across operator restarts.

### What you cannot recover from S3 alone

- **Manually-created postgres roles** that aren't captured in `managed.roles`. Sparky-fitness's `sparky_app` IS captured (in `apps/base/sparky-fitness/db-cnpg/sparky-fitness-app-db-secret.sops.yaml` + the patches in `db-cnpg.yaml`). Future apps with similar least-privilege patterns should follow the same.
- **Grants on existing tables** if the cluster is recovered via `bootstrap.initdb` instead of `bootstrap.recovery`. The S3 backup includes grants; initdb does not. Default privileges set via `postInitApplicationSQL` cover only future tables.

---

## 2. Restore a single PVC — fallback: Kopia (Layer 3 or 4)

Use when volsync/Garage is unavailable, or when you want a snapshot older than volsync retention (24h hourly / 7d daily / 5w weekly / 3m monthly).

### 2a. RBD-backed PVC (e.g. most apps)

The RBD images live in the `/mnt/rbd-backup` Kopia source as `<csi-vol-XXX>/<csi-vol-XXX>-YYYY-MM-DD.img` (renamed 2026-05-08; older snapshots are still under the deprecated `/mnt/cephfs` source as `rbd-backup/<csi-vol-XXX>/...`).

1. Identify the csi-vol UUID for the PVC:
   ```bash
   k8s$ kubectl get pv $(kubectl -n <ns> get pvc <pvc> -o jsonpath='{.spec.volumeName}') \
       -o jsonpath='{.spec.csi.volumeAttributes.imageName}'
   # → e.g. csi-vol-fa7e...
   ```
   Or from the inventory CSV:
   ```bash
   $ ssh ubuntu@192.168.90.161 'sudo grep <pvc> /var/backups/rbd-pv-index-k3s.csv'
   ```

2. Mount the desired Kopia snapshot read-only. For 2026-05-08 onward use `/mnt/rbd-backup`; for older points-in-time use `/mnt/cephfs`:
   ```bash
   kopia# mkdir /mnt/restore
   # Recent snapshot (2026-05-08+):
   kopia# kopia snapshot list /mnt/rbd-backup | tail -10        # pick the snapshot ID
   # Older (pre-rename):
   kopia# kopia snapshot list /mnt/cephfs     | tail -10        # pick the snapshot ID (rbd-backup/ subdir)
   kopia# kopia snapshot mount <snapshot-id> /mnt/restore
   ```

3. Find the image. Path differs depending on which source you mounted:
   ```bash
   # If mounted from /mnt/rbd-backup (post-2026-05-08):
   kopia# ls /mnt/restore/csi-vol-fa7e.../
   # If mounted from /mnt/cephfs (pre-rename):
   kopia# ls /mnt/restore/rbd-backup/csi-vol-fa7e.../
   # Either way:
   #   csi-vol-fa7e...-DATE.img
   #   csi-vol-fa7e...-DATE.meta.txt
   kopia# cat /mnt/restore/.../*.meta.txt   # confirm it's the right PVC
   ```

4. Push the image into a fresh Ceph RBD volume on a master VM:
   ```bash
   # SCP the image to a master VM (or a node that has rbd CLI + ceph keyring)
   $ ssh kopia 'cat /mnt/restore/csi-vol-fa7e.../csi-vol-fa7e...-DATE.img' \
     | ssh ubuntu@192.168.90.161 'sudo tee /tmp/restore.img > /dev/null'

   # On the master:
   k8s$ sudo rbd import --image-format 2 /tmp/restore.img k3s-rbd/restore-<pvc>
   ```

5. Stop the app, manually create a PV pointing at `restore-<pvc>`, bind a new PVC, and let the app pick it up. Or use the safer **clone-then-rename** approach:

   ```bash
   k8s$ kubectl -n <ns> scale deploy <app> --replicas=0
   k8s$ # Edit the existing PV to release its claim and remove finalizers (after backing up the spec!)
   k8s$ # Delete the existing PVC
   k8s$ # Apply a new PV pointing at restore-<pvc> and a matching PVC with the same name as before
   k8s$ kubectl -n <ns> scale deploy <app> --replicas=1
   ```

6. Cleanup: unmount kopia, delete the temp `.img`:
   ```bash
   kopia# kopia mount unmount /mnt/restore
   ```

### 2b. CephFS-backed PVC

CephFS PVCs are stored as subvolume directory trees inside `/mnt/cephfs-k3s/volumes/csi/csi-vol-XXX/`. Same pattern, simpler restore — just `cp -a` the contents.

1. Identify subvolume name (CSV inventory or):
   ```bash
   k8s$ kubectl get pv $(kubectl -n <ns> get pvc <pvc> -o jsonpath='{.spec.volumeName}') \
       -o jsonpath='{.spec.csi.volumeAttributes.subvolumeName}'
   ```

2. Mount the kopia snapshot of `/mnt/cephfs-k3s`:
   ```bash
   kopia# kopia snapshot list /mnt/cephfs-k3s
   kopia# kopia snapshot mount <snapshot-id> /mnt/restore
   ```

3. The data is at `/mnt/restore/volumes/csi/<subvolume>/<subvolume>/`. Copy it into a freshly-provisioned PVC:
   ```bash
   k8s$ # Provision a new empty PVC of same size+SC, attach a debug pod
   k8s$ kubectl run rsync-helper -n <ns> --rm -it --image=alpine --overrides='{"spec":{"containers":[{...mount target PVC at /target...}]}}'
   # (or use a Job — see Volsync's mover image as a template)
   $ rsync -aHAX <kopia-mounted-source>/ <target-pvc-mount>/
   ```

   Pragmatic shortcut: use a one-shot `volsync-v2/bootstrap` workflow but point it at a manually-created restic snapshot. More setup; only worth it if doing many at once.

4. Bind the new PVC to the app, scale up.

---

## 3. Restore a single VM/LXC from PBS (Layer 1)

### From the PBS web UI (preferred)

1. https://pbs:8007 → datastore `pbs-backups` → pick the VM/LXC ID → pick a snapshot timestamp
2. *Restore* button → choose target node + storage
3. Watch task in PBS log

### From the CLI

```bash
# List snapshots for a guest
pbs# proxmox-backup-client snapshot list --repository localhost:pbs-backups vm/<vmid>

# Restore VM
pve-mac# qmrestore pbs-backups:backup/vm/<vmid>/<timestamp> <new-vmid> --storage local-zfs --force 0

# Restore LXC
pve-mac# pct restore <new-vmid> pbs-backups:backup/ct/<lxcid>/<timestamp> --storage local-zfs
```

### File-level extract from a VM backup

```bash
pbs# proxmox-backup-client mount --repository localhost:pbs-backups \
        vm/<vmid>/<timestamp> --keyfile ... root.pxar /mnt/pbs-extract
# read files; umount when done
pbs# proxmox-backup-client unmount /mnt/pbs-extract
```

---

## 4. Cluster-nuke recovery (no k3s, but Proxmox still up)

Order of operations: provision infra → bootstrap → restore PVCs.

1. **Re-provision K3s VMs**: `cd terraform && terraform apply` (rebuilds VMs 661-666).
2. **K3s install**: `ansible-playbook -i ansible/k3s-cluster/inventory/dynamic_terraform_inventory.sh ansible/k3s-cluster/playbooks/k3s_install.yml`.
3. **Bootstrap Flux**:
   ```bash
   k8s$ kubectl create ns flux-system
   # Paste age key from your password manager
   k8s$ kubectl -n flux-system create secret generic sops-age --from-file=age.agekey=<your-age-key>
   k8s$ flux bootstrap github --owner=mainertoo --repository=kubernetes-lab \
       --branch=master --path=./clusters/production
   ```
4. **Wait for `infrastructure` and `flux-system` Kustomizations to be Ready**:
   ```bash
   k8s$ flux get all -A --no-header | awk '$2=="False"'
   ```
5. **For each app needing restore**, switch its kustomization to `components/volsync-v2/bootstrap` and commit. Flux reconciles, volsync pulls each PVC's latest snapshot from Garage.
   - You can switch them all at once if the volsync controller is healthy — they parallelize.
6. **For each CNPG cluster** (8 postgres apps), edit `apps/production/<app>/db-cnpg.yaml` to swap the `components:` reference from `cnpg-cluster` → `cnpg-cluster/recovery`. Full procedure in [`cnpg-disaster-recovery.md`](cnpg-disaster-recovery.md) §2. Flux reconciles all 8 in parallel.
7. **Settle**: once all bootstraps complete, switch each kustomization back to plain `volsync-v2` (and flip each CNPG `components:` reference back to plain `cnpg-cluster`) in a follow-up commit.

### Concrete PR for a mass-restore

Worth keeping a branch `dr/cluster-rebuild` that has every app's kustomization pre-switched to bootstrap. Merge it on day-1, settle on day-2.

---

## 5. Total Ceph loss recovery (Ceph cluster gone, K3s gone)

Worst-case path. Sequence:

1. **Rebuild Proxmox + Ceph** (Terraform doesn't manage Ceph today; manual). Recreate pools `k3s-rbd`, `k3s-fs-data`, `k3s-fs-metadata`, `ceph-shared`, `ceph-swarm.{meta,data}`.
2. **Re-establish CephFS** with the same `mds_namespace` names (`k3s-fs`, `ceph-swarm`) so existing CSI volume handles validate.
3. **Mount Kopia repo** on a recovery VM (any host with `/mnt/zbackup` available — or a fresh Linux VM with a fresh `kopia repository connect filesystem --path=...`).
4. **Re-import RBD images first** (Layer 3 path). For most-recent state use `/mnt/rbd-backup`; for older point-in-time use `/mnt/cephfs`:
   ```bash
   kopia# kopia snapshot list /mnt/rbd-backup | tail -5         # pick the day you want
   kopia# kopia snapshot mount <id> /mnt/restore
   # For every csi-vol-XXX directory under /mnt/restore/:
   for dir in /mnt/restore/*/; do
       img=$(ls $dir/*.img | head -1)
       name=$(basename $dir)
       cat $dir/*.meta.txt    # confirm namespace + PVC mapping
       rbd import "$img" "k3s-rbd/$name"
   done
   # If using a pre-2026-05-08 snapshot, paths are /mnt/restore/rbd-backup/*/
   ```
5. **Re-import CephFS subvolumes** (Layer 4 path): same idea, `rsync -aHAX` from `/mnt/restore/volumes/csi/<subvol>/<subvol>/` into the freshly-created subvolumes (after `ceph fs subvolume create k3s-fs <name> <group>`).
6. **Reconstruct PV objects** in K8s: for every entry in the `/var/backups/pv-index-k3s.csv` (recovered from the latest PBS backup of master-1's VM disk), create a `PersistentVolume` of type `csi` with `volumeHandle` pointing at the import. Same `claimRef` ns/name lets the existing PVC objects bind.
7. **Re-bootstrap Flux + apps** (steps 3-6 of §4). Apps that don't have data in volsync but DO in Kopia: skip the bootstrap component, just let them start with the PVCs you reconstructed by hand.

This path is the most painful and least exercised. **Test pieces of it quarterly** — see Recommendation P1.

---

## 6. Loss of QNAP (volume corruption / dead RAID)

Today: catastrophic. PBS backups gone. Garage gone. Volsync data gone. The Kopia repo on zbackup is the only survivor.

After implementing Findings 1+2 and offsite (§11 of architecture doc): zbackup contains a recent snapshot of PBS's content-addressed store and Garage's chunk store. Recovery from there:

1. Stand up new NAS (or reuse repaired QNAP).
2. Restore `/share/CACHEDEV1_DATA/proxmox/proxmox-backup-server/` from a kopia snapshot of `/mnt/qnap_pbs`.
3. Restore `/share/CACHEDEV1_DATA/garage/` from a kopia snapshot of `/mnt/qnap_garage`.
4. Restore `/share/CACHEDEV1_DATA/Container/` from `/mnt/qnap_container` — needed before Garage container can be restarted.
5. Restore `/share/CACHEDEV1_DATA/appdata/` from `/mnt/qnap_appdata` — QNAP service state.
4. Re-export NFS, point PBS and Garage at the restored paths.
5. Walk through §4 (cluster-nuke) using the recovered Garage repos.

---

## 7. Loss of zbackup pool (UGREEN failure)

Backup-of-backup. PBS, Garage, and the live cluster are independent. Direct losses:

- All historical Kopia history (cephfs, cephfs-k3s, qnap_alldata)
- The `rbd-nightly-backup.sh` history beyond today (today's run still lives on cephfs-swarm)

Recovery: replace the disks, recreate the ZFS mirror, `kopia repository create filesystem`, accept that history < `today` is lost. Ongoing volsync data and PBS data are unaffected.

After offsite is implemented: pull the offsite repo back into the rebuilt zbackup with `kopia repository sync-from`.

---

## 8. Loss of Garage S3 (mid-cluster)

**Garage runs as a Docker container natively on the QNAP** (not in K8s). If the container or its data dies, volsync repos all unreachable; cluster keeps running, just no new backups.

1. Restore Garage's data on the QNAP:
   - If the QNAP filesystem at `/share/CACHEDEV1_DATA/garage/` is intact: restart the Garage container on QNAP (Container Station UI or QNAP CLI) — done.
   - If the data is corrupt/missing: restore from Kopia snapshot of `/mnt/qnap_garage` over NFS to QNAP, then restart container.
2. Verify by listing one app's repo:
   ```bash
   k8s$ kubectl -n <ns> exec -it <app>-pod -- env | grep RESTIC
   # Or run a temporary mover Job with the secret
   ```
3. Trigger a re-sync of every ReplicationSource: `kubectl annotate rs <app> -n <ns> volsync.backube/triggered=manual`.

---

## 9. Verify a backup actually works (do this quarterly)

The only test of a backup is a restore. Run one quarterly:

### Volsync side-by-side restore

```bash
# Pick a small app (e.g. dumb)
$ git checkout -b restore-test/dumb
# Switch apps/production/dumb/kustomization.yaml to use volsync-v2/restore
# Commit/push, wait for Flux
k8s$ kubectl -n dumb get pvc dumb-restore   # should be Bound, populated
k8s$ kubectl -n dumb exec -it <pod> -- ls /restore-mount   # data present?
# Roll back the branch when done
```

### PBS test restore

```bash
# Restore a small LXC (e.g. adguard, ID 104) to a temporary VMID
pve-mac# pct restore 999 pbs-backups:backup/ct/104/<timestamp> --storage local-zfs --hostname adguard-test
pve-mac# pct start 999
pve-mac# pct enter 999     # validate
pve-mac# pct stop 999 && pct destroy 999 --purge
```

### Kopia repo verify

```bash
kopia# kopia snapshot verify --verify-files-percent=1 --max-failures-per-source=10
# 1% sample verifies blob integrity without scanning all 658 GB.
```

---

## 10. Inventory cheat sheet

When you have an opaque blob name (csi-vol-XXX, an RBD image name, a subvolume name) and need to know what app it belongs to:

```bash
# Source 1: live cluster (if up)
k8s$ kubectl get pv -o json | jq -r '.items[] | select(.spec.csi.volumeAttributes.imageName=="<image>")
        | "\(.spec.claimRef.namespace)/\(.spec.claimRef.name)"'

# Source 2: latest CSV index (preserved in PBS backup of master VM disk)
$ ssh ubuntu@192.168.90.161 'sudo grep <image-or-subvol> /var/backups/pv-index-k3s.csv'

# Source 3: .meta.txt next to the image
# On the live cephfs-swarm filesystem (today's RBD exports only):
kopia# cat /mnt/cephfs/rbd-backup/<csi-vol-XXX>/*.meta.txt
# In Kopia for historical data:
#   post-2026-05-08:  kopia snapshot mount <id-from-/mnt/rbd-backup>     /mnt/restore  (path: /mnt/restore/<csi-vol>/)
#   pre-rename:       kopia snapshot mount <id-from-/mnt/cephfs>         /mnt/restore  (path: /mnt/restore/rbd-backup/<csi-vol>/)
```

The CSV path survives if Ceph is gone; the .meta.txt path survives if PBS is gone. Both surviving in different blast radii is the whole point of having two.

---

## 11. Common pitfalls

- **Forgetting to `flux suspend hr`** before deleting a PVC for restore — Flux re-creates the resources before volsync can stage the dataSource.
- **Switching to `volsync-v2/bootstrap` without bumping `VOLSYNC_RESTORE_TOKEN`** — RD doesn't re-fire if the spec is unchanged.
- **Restoring an RBD image into the wrong pool** — `k3s-rbd` is the live pool; importing to `kube-rbd` (legacy, empty) won't be usable.
- **PBS file-level extract requires the encryption keyfile** if the backup was encrypted. PBS backups here are unencrypted today.
- **Kopia mount on a privileged LXC** is fine; on an unprivileged LXC, FUSE may not work — use `kopia restore` to a target dir instead.
- **`rbd export` from a busy pool can take a long time** — schedule restores during quiet hours, or do `rbd snap create` first and export the snap.
