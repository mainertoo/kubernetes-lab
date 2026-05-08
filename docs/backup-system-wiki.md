# Backup System — Complete Reference

> Self-contained reference for the homelab backup system. Designed to survive the loss of any host: every custom script, cron entry, and config file is reproduced verbatim below. Drop into wiki-js as one page, or split by `## ` heading if your wiki prefers smaller pages.
>
> Companion files in this repo: `docs/backup-architecture.md` (longer prose form) and `docs/backup-recovery.md` (procedure-only).
>
> Last full audit: **2026-05-07**. Re-audit annually or after any major change.

---

## Table of contents

1. [Overview](#1-overview)
2. [Hardware & networks](#2-hardware--networks)
3. [Storage pools and capacities](#3-storage-pools-and-capacities)
4. [The five backup layers](#4-the-five-backup-layers)
5. [Schedule timeline (24 h)](#5-schedule-timeline-24-h)
6. [Custom scripts (full source)](#6-custom-scripts-full-source)
7. [Cron entries (verbatim, by host)](#7-cron-entries-verbatim-by-host)
8. [Configuration files (verbatim)](#8-configuration-files-verbatim)
9. [Critical findings & gaps](#9-critical-findings--gaps)
10. [Recommendations (prioritized)](#10-recommendations-prioritized)
11. [Offsite DR comparison](#11-offsite-dr-comparison)
12. [Recovery runbook](#12-recovery-runbook)
13. [Inventory cheat sheet](#13-inventory-cheat-sheet)
14. [Common pitfalls](#14-common-pitfalls)

---

## 1. Overview

Five independent backup layers, each with its own schedule and target:

| # | What | Tool | Source → Target |
|---|---|---|---|
| 1 | VMs + LXCs | PVE vzdump → PBS | All guests except PBS itself → `pbs-backups` datastore (NFS to QNAP) |
| 2 | K8s app PVCs (live) | volsync (Restic) | PVC snapshot → Garage S3 (NFS-backed PVC on QNAP) |
| 3 | Ceph RBD images | `rbd-nightly-backup.sh` → Kopia | `rbd export` → CephFS staging → Kopia → zbackup ZFS |
| 4 | Ceph FS subvolumes | Kopia (kernel mount) | CephFS `k3s-fs` → Kopia → zbackup ZFS |
| 5 | QNAP `/QNAS` subtree | Kopia (NFS mount) | QNAP `/share/CACHEDEV1_DATA/QNAS` → Kopia → zbackup ZFS |

Plus two parallel **inventory mechanisms** (so blob names like `csi-vol-fa7e…` can be traced back to the originating PVC during recovery):

- `rbd-nightly-backup.sh` writes `<image>-YYYY-MM-DD.meta.txt` next to each RBD export → captured by Layer 4 Kopia run.
- `k3s-pv-index.sh` writes RBD + CephFS PV CSV indexes to `/var/backups/` inside each k3s master VM → captured by Layer 1 PBS backup at 02:00.

**Two critical gaps** identified 2026-05-07:

1. **PBS data (~301 GB) has no secondary copy** — Kopia's QNAS job covers `/share/CACHEDEV1_DATA/QNAS`, not `/share/CACHEDEV1_DATA/proxmox`.
2. **Garage S3 data (~167 GB) has no secondary copy** — same reason; lives at `/share/CACHEDEV1_DATA/garage`.

See [§9 Critical findings](#9-critical-findings--gaps) for fix shapes.

---

## 2. Hardware & networks

```
                     ┌─────────────────────────────────────────────────────┐
                     │              Management LAN 192.168.1.0/24           │
                     └────────┬───────────┬───────────┬─────────────┬──────┘
                              │           │           │             │
       ┌──────────────────────┼───────────┼───────────┼─────────────┼─────────────┐
       │                      │           │           │             │             │
  ┌────┴─────┐         ┌──────┴──┐  ┌─────┴───┐  ┌────┴─────┐  ┌────┴─────┐  ┌────┴─────┐
  │ pve-     │  TB4    │ pve-    │  │ pve-    │  │ pve-mac  │  │ pve-     │  │  qnas    │
  │ mammoth  │ <-----> │ whistler│  │ zermatt │  │  .250    │  │ ugreen   │  │  .252    │
  │  .109    │         │  .107   │  │  .108   │  │          │  │  .251    │  │ TS-X32   │
  │ k3s-m1   │         │ k3s-m2  │  │ k3s-m3  │  │ pbs(VM)  │  │ kopia-lxc│  │ 6×14TB   │
  │ k3s-w1   │         │ k3s-w2  │  │ k3s-w3  │  │  →.245   │  │ Win11    │  │ +2×2TB   │
  │ plex(LXC)│         │         │  │         │  │ adguard  │  │ zwave    │  │ cache    │
  │ ceph 0,3 │         │ ceph 2,5│  │ ceph 1,4│  │ tailscale│  │ ZFS pool │  │ Garage   │
  │          │         │         │  │         │  │          │  │ "zbackup"│  │ container│
  └──────────┘         └─────────┘  └─────────┘  └──────────┘  │ 2×24TB   │  │ NFS-bk   │
                                                               │ mirror   │  └──────────┘
                                                               └──────────┘

  Ceph public/cluster network 192.168.99.0/24 (Thunderbolt 4 mesh, mons on .12/.13/.14)
  K3s LAN 192.168.90.0/24 (k3s-master-{1,2,3} = .161/.162/.163, workers .164/.165/.166)
```

### Hosts

| Host | SSH name | IP | Role |
|---|---|---|---|
| Proxmox node 1 | `pve-mammoth` | 192.168.1.109 | Ceph OSD 0,3 / k3s-master-1 + worker-1 / plex LXC |
| Proxmox node 2 | `pve-whistler` | 192.168.1.107 | Ceph OSD 2,5 / k3s-master-2 + worker-2 |
| Proxmox node 3 | `pve-zermatt` | 192.168.1.108 | Ceph OSD 1,4 / k3s-master-3 + worker-3 |
| Mac mini Proxmox | `pve-mac` | 192.168.1.250 | Hosts PBS VM + adguard + tailscale LXCs |
| UGREEN NAS Proxmox | `pve-ugreen` | 192.168.1.251 | ZFS `zbackup` pool, kopia-lxc, zwave-js |
| QNAP TS-X32 | `qnas` | 192.168.1.252 | NFS exports, Garage S3 backend |
| PBS VM | `pbs` | 192.168.1.245 | Proxmox Backup Server |
| Kopia LXC | `kopia` | DHCP on vmbr0 | All Kopia snapshot jobs |
| K3s masters | `ubuntu@192.168.90.16{1,2,3}` | — | Run `k3s-pv-index.sh` cron |

---

## 3. Storage pools and capacities

| Pool | Type | Where | Capacity | Used (audit date) |
|---|---|---|---|---|
| Ceph cluster | 6 × 2 TB NVMe | mammoth/whistler/zermatt | 11 TiB raw | 2.8 TiB (25.78%) |
| `zbackup` | ZFS mirror, 2 × 24 TB SATA | pve-ugreen | 21.8 TiB | 658 GB (3%) |
| QNAP main | RAID-? on 6 × 14 TB + 2 × 2 TB cache | qnas | 71.9 TiB | 31.5 TiB (44%) |

### Ceph pools

| Pool | Used | Purpose |
|---|---|---|
| `ceph-shared` | 699 GiB | Shared RWX images (legacy docker-swarm era; cephfs-swarm) |
| `ceph-swarm.meta` / `.data` | 484 MiB / 343 GiB | Old cephfs (was docker-swarm). **Now used as RBD backup staging** at `/mnt/pve/cephfs-swarm/rbd-backup/`. |
| `k3s-fs-metadata` / `k3s-fs-data` | 3.0 GiB / 1.7 TiB | Live CephFS PVCs for k3s |
| `k3s-rbd` | 129 GiB (68 GiB stored) | Live RBD PVCs for k3s |
| `kube-rbd` | 12 KiB | Empty (legacy, can be removed) |
| `.mgr` | 6.3 MiB | Ceph metadata |

### Filesystem mounts (key ones)

| Source | Mount point | Where |
|---|---|---|
| `192.168.99.12/13/14:/` (mds_namespace=ceph-swarm) | `/mnt/pve/cephfs-swarm` | mammoth, whistler, zermatt, ugreen, kopia-lxc (as `/mnt/cephfs`) |
| `192.168.99.11/12/13:/` (mds_namespace=k3s-fs) | `/mnt/cephfs-k3s` | kopia-lxc (kernel ceph client; bind via mp2) |
| `192.168.1.252:/QNAS` | `/mnt/pve/tank` (host) → `/mnt/qnap_alldata` (in kopia-lxc) | All PVE hosts + kopia-lxc |
| `192.168.1.252:/pve-ha` | `/mnt/pve/shared-nfs` | All PVE hosts (PVE shared storage class) |
| `192.168.1.252:/proxmox/proxmox-backup-server` | `/mnt/pbs-backups` | PBS VM only |
| `zbackup` (ZFS) | `/zbackup` (host) → `/mnt/zbackup` (in kopia-lxc) | pve-ugreen, kopia-lxc |

---

## 4. The five backup layers

### Layer 1 — VM/LXC backups (vzdump → PBS → QNAP)

```
[every guest in cluster]                            [pve-mac VM]              [QNAP NFS]
       │                                                  │                          │
       │ vzdump @ 02:00 daily                             │                          │
       │ (configured in /etc/pve/jobs.cfg)                │                          │
       └─────────► snapshot mode, exclude=299 ───────────►│ ────── NFS mount ───────►│
                                                          │   192.168.1.252:/proxmox/proxmox-backup-server
                                                          │ Verify: monthly | Prune: daily
                                                          │ Keep last/daily/weekly/monthly = 17/7/8/2
```

- Job source of truth: `/etc/pve/jobs.cfg` (cluster-wide, on the corosync filesystem)
- Trigger: `pvescheduler.service` (the legacy `/etc/pve/vzdump.cron` is empty)
- PBS VM is `pve-mac:299`, root disk on `shared-nfs` NFS, 2 cores / 4 GB
- PBS datastore: `/mnt/pbs-backups`, 32 TB used / 41 TB free
- Verify job `v-e00654e0-3168` runs monthly with `ignore-verified: true`
- No `sync.cfg` (no peer PBS to push to)

### Layer 2 — K8s app PVCs (volsync → Garage S3)

```
[K8s PVC, e.g. media/jellyfin]
        │
        │ ReplicationSource ${APP}
        │   trigger.schedule: ${VOLSYNC_SCHEDULE:=05 00/12 * * *}   ── every 12h at :05
        │   restic.copyMethod: Snapshot
        │   restic.repository: ${APP}-volsync                       ── per-app Secret
        │   retain: { hourly: 24, daily: 7, weekly: 5, monthly: 3 }
        ▼
[volsync mover Job]   creates CSI VolumeSnapshot → mounts as PVC clone
        │             + ${APP}-volsync cache PVC (10Gi on ceph-rbd)
        ▼
   restic push to:  s3:https://<garage-endpoint>/<bucket>/${APP}-volsync
        ▼
[Garage S3, in-cluster Deployment]
   data PVC → NFS-backed → 192.168.1.252:/share/CACHEDEV1_DATA/garage   (~167 GB)
```

- Component sources: `components/volsync-v2/{,backup-only,bootstrap,restore}`
- Master credential: SOPS-encrypted Secret `volsync-garage-base` in `flux-system`
- Per-app secrets derived via Flux `postBuild.substituteFrom`
- Helm chart: `oci://ghcr.io/home-operations/charts-mirror/volsync` tag `0.15.0`
- 77 ReplicationSources active (heaviest namespace: `media` with 39). `dawarich-db` and `authentik-postgresql` are PostgreSQL volumes — **crash-consistent only**, see [§9](#9-critical-findings--gaps).

### Layer 3 — Ceph RBD nightly export

```
[k3s-rbd Ceph pool, ~148 RBD images]
        │
        │ /usr/local/sbin/rbd-nightly-backup.sh on pve-ugreen, cron 30 1 * * *
        │
For each image:
  1. mkdir /mnt/pve/cephfs-swarm/rbd-backup/<image>/
  2. find . -mindepth 1 -maxdepth 1 -exec rm -rf {} +     ← clears prior run
  3. rbd export k3s-rbd/<image> <image>-YYYY-MM-DD.img.tmp
  4. mv tmp → final
  5. emit <image>-YYYY-MM-DD.meta.txt    (namespace, PVC name, size, Flux SHA, etc.)
        │
        ▼
[CephFS k3s-fs at /mnt/pve/cephfs-swarm/rbd-backup/]   ← Kopia picks this up at 02:45
```

Pattern is intentional: cephfs-swarm holds only the latest export, Kopia provides historical retention. See [§6 source](#6a-rbd-nightly-backupsh) for the full script.

### Layer 4 — CephFS Kopia snapshots

```
[CephFS k3s-fs]   (live PVCs)              [CephFS ceph-swarm]   (legacy + rbd-backup staging)
        │                                            │
        │  kernel ceph mount                         │  bind-mount via LXC mp0
        │  /mnt/cephfs-k3s                           │  /mnt/cephfs
        ▼                                            ▼
                          [kopia-lxc, LXC 111 on pve-ugreen]
                            cron 45 2 * * *  → kopia snapshot create /mnt/cephfs --parallel=4
                            cron 10 3 * * *  → kopia snapshot create /mnt/cephfs-k3s --parallel=4
                                                │
                                                ▼
                          /mnt/zbackup/kopia-repo  (filesystem repo, BLAKE2B-256 / AES256-GCM)
                            global retention: 48 hourly / 7 daily / 4 weekly / 24 monthly / 3 annual
```

`/mnt/cephfs-k3s` ignore rule added 2026-05-07: `kopia policy set --add-ignore "/volumes/_deleting/" /mnt/cephfs-k3s` — silences ~745 fatal-error messages per run from CSI tombstones being garbage-collected during the scan.

### Layer 5 — QNAP `/QNAS` subtree → Kopia

```
QNAP   /share/CACHEDEV1_DATA/   (31 TB used)
  ├── QNAS/         ────────► /QNAS NFS export ────► Kopia /mnt/qnap_alldata (29 TB) ─► zbackup
  │     ├── data/        (29 TB — predominantly user data)
  │     ├── backup/      (86 GB)
  │     └── ...
  ├── proxmox/      ────────► /proxmox NFS ────► PBS only (NOT in Kopia)  ⚠ 301 GB
  ├── garage/       ────────► NFS-backed PVC for Garage    ⚠ NOT in Kopia  167 GB
  ├── pve-ha/       ────────► /pve-ha NFS, "shared-nfs" PVE storage (52 KB)
  └── Container/, appdata/, Public/, TimeMachine/ — also not in Kopia
```

Cron on kopia-lxc: `30 3 * * * /usr/bin/kopia snapshot create /mnt/qnap_alldata --parallel=8`

---

## 5. Schedule timeline (24 h)

```
00:05 UTC ──── volsync ReplicationSource sweep (12-hourly default)
01:05/07/09 ── k3s-pv-index.sh on master-1/2/3  → /var/backups/*.csv
01:30 ──────── rbd-nightly-backup.sh on pve-ugreen  → /mnt/pve/cephfs-swarm/rbd-backup/
02:00 ──────── PVE vzdump (all guests except 299)  → PBS → QNAP
02:45 ──────── kopia snapshot /mnt/cephfs (incl. rbd-backup/)  → zbackup
03:00 ──────── /var/tmp/vzdumptmp* cleanup on pve-ugreen
03:10 ──────── kopia snapshot /mnt/cephfs-k3s  → zbackup
03:30 ──────── kopia snapshot /mnt/qnap_alldata  → zbackup
12:05 UTC ──── volsync ReplicationSource sweep (second daily run)
daily ──────── PBS GC + prune (keep 17/7/8/2 last/daily/weekly/monthly)
monthly ────── PBS verify job v-e00654e0-3168 (ignore-verified=true)
```

Window 01:30–04:00 is the densest. Each consumer reads what the prior step wrote (rbd-export → cephfs settle → kopia pulls), serialized by clock.

---

## 6. Custom scripts (full source)

### 6a. `rbd-nightly-backup.sh`

**Lives at**: `/usr/local/sbin/rbd-nightly-backup.sh` on `pve-ugreen`
**Cron**: `30 1 * * * /usr/local/sbin/rbd-nightly-backup.sh >> /var/log/rbd-nightly-backup.log 2>&1`
**Dependencies**: `rbd`, `kubectl`, `jq`, `stat`, `base64` (all present on a default Proxmox install + jq from apt)
**Required state on host**:

- `/mnt/pve/cephfs-swarm` mounted (CephFS swarm namespace) — script aborts if not mounted
- `/etc/ceph/ceph.conf` + ceph keyring readable (so `rbd ls` and `rbd export` work)
- `/root/.kube/config` valid kubeconfig for the k3s cluster (for PV / HelmRelease metadata lookups)

```bash
#!/bin/bash
set -euo pipefail

# Make sure cron can find binaries like kubectl, jq, rbd, stat, etc.
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# RBD pool to back up
POOL="k3s-rbd"

# Host-side CephFS mount point
CEPHFS_HOST_ROOT="/mnt/pve/cephfs-swarm"

# Where RBD exports will be written
BACKUP_ROOT="${CEPHFS_HOST_ROOT}/rbd-backup"

# Explicit kubeconfig for the k3s cluster
KUBECONFIG="/root/.kube/config"
KCTL="kubectl --kubeconfig=${KUBECONFIG}"

echo "[RBD BACKUP] Starting backup for pool ${POOL} at $(date)"

# --- Ensure CephFS is mounted ---
if ! mountpoint -q "${CEPHFS_HOST_ROOT}"; then
  echo "[RBD BACKUP] ERROR: ${CEPHFS_HOST_ROOT} is not mounted. Aborting." >&2
  exit 1
fi

mkdir -p "${BACKUP_ROOT}"

# --- Clear previous backup contents (only keep latest run on pve-ugreen) ---
echo "[RBD BACKUP] Clearing previous contents of ${BACKUP_ROOT}"
find "${BACKUP_ROOT}" -mindepth 1 -maxdepth 1 -exec rm -rf {} + || true

# --- List all RBD images ---
IMAGES=$(rbd ls "${POOL}" || true)
if [[ -z "${IMAGES}" ]]; then
  echo "[RBD BACKUP] No RBD images found in pool ${POOL}. Exiting."
  exit 0
fi

# --- Fetch PV list once for efficiency ---
echo "[RBD BACKUP] Fetching PV list via kubectl..."
PV_JSON=$(${KCTL} get pv -o json 2>/dev/null || echo '{"items":[]}')

# --- Fetch Flux Git SHA (apps Kustomization) once ---
FLUX_REV=$(${KCTL} -n flux-system get kustomization apps -o jsonpath='{.status.lastAppliedRevision}' 2>/dev/null || echo "unknown")

# Loop over each RBD image
for IMG in ${IMAGES}; do
  IMG_DIR="${BACKUP_ROOT}/${IMG}"
  mkdir -p "${IMG_DIR}"

  DATE=$(date +"%Y-%m-%d")
  OUT="${IMG_DIR}/${IMG}-${DATE}.img"
  TMP="${OUT}.tmp"
  META="${IMG_DIR}/${IMG}-${DATE}.meta.txt"

  echo "[RBD BACKUP] Exporting ${POOL}/${IMG} -> ${OUT}"

  if rbd export "${POOL}/${IMG}" "${TMP}"; then
    mv "${TMP}" "${OUT}"
    echo "[RBD BACKUP] Completed export for ${IMG}"
  else
    echo "[RBD BACKUP] ERROR exporting ${IMG}" >&2
    rm -f "${TMP}" || true
    continue
  fi

  # --- Base metadata ---
  {
    echo "backup_timestamp=$(date --iso-8601=seconds)"
    echo "pool=${POOL}"
    echo "image=${IMG}"
    echo "export_path=${OUT}"
    echo "export_size_bytes=$(stat -c%s "${OUT}")"
    echo "flux_last_applied_revision=${FLUX_REV}"
    echo
  } > "${META}"

  # --- Find matching PV for this image ---
  # Strategy:
  #   1) If .spec.csi.volumeAttributes.imageName == IMG, use that.
  #   2) Else, strip "csi-vol-" -> UUID and match volumeHandle suffix.
  MATCH_B64=$(
    echo "${PV_JSON}" | jq -r --arg IMG "${IMG}" '
      .items[]
      | ($IMG | sub("^csi-vol-"; "")) as $uuid
      | select(
          ((.spec.csi.volumeAttributes.imageName? // "") == $IMG)
          or (((.spec.csi.volumeHandle // "") | endswith($uuid)))
        )
      | @base64
    ' 2>/dev/null || true
  )

  if [[ -n "${MATCH_B64}" ]]; then
    MATCH=$(echo "${MATCH_B64}" | base64 -d)

    PV_NAME=$(echo "${MATCH}" | jq -r '.metadata.name')
    PVC_NS=$(echo "${MATCH}" | jq -r '.spec.claimRef.namespace')
    PVC_NAME=$(echo "${MATCH}" | jq -r '.spec.claimRef.name')
    SC_NAME=$(echo "${MATCH}" | jq -r '.spec.storageClassName')
    VOL_HANDLE=$(echo "${MATCH}" | jq -r '.spec.csi.volumeHandle')

    {
      echo "pv_name=${PV_NAME}"
      echo "pvc_namespace=${PVC_NS}"
      echo "pvc_name=${PVC_NAME}"
      echo "storage_class=${SC_NAME}"
      echo "volume_handle=${VOL_HANDLE}"
    } >> "${META}"

    # --- Generic app/component derived from PVC ---
    {
      echo "app=${PVC_NS:-unknown}"
      echo "component=${PVC_NAME:-unknown}"
    } >> "${META}"

    # --- Try to capture HelmRelease info in this namespace (if any) ---
    HR_JSON=$(${KCTL} -n "${PVC_NS}" get helmrelease -o json 2>/dev/null || echo '{"items":[]}')
    HR_COUNT=$(echo "${HR_JSON}" | jq '.items | length')

    if [[ "${HR_COUNT}" -gt 0 ]]; then
      HR_NAME=$(echo "${HR_JSON}" | jq -r '.items[0].metadata.name')
      HR_LAST_APPLIED=$(echo "${HR_JSON}" | jq -r '.items[0].status.lastAppliedRevision // empty')
      HR_CHART_VERSION=$(echo "${HR_JSON}" | jq -r '.items[0].status.history[-1].chartVersion // empty')

      {
        echo "helmrelease_name=${HR_NAME}"
        echo "helmrelease_last_applied_revision=${HR_LAST_APPLIED}"
        echo "helmrelease_chart_version=${HR_CHART_VERSION}"
      } >> "${META}"
    fi

    # --- OPTIONAL special-case: authentik PostgreSQL DB password ---
    if [[ "${PVC_NS}" == "authentik" && "${PVC_NAME}" == "data-authentik-postgresql-0" ]]; then
      DB_PASSWORD=$(
        ${KCTL} -n authentik get secret authentik-postgresql -o json 2>/dev/null \
        | jq -r '.data.password // empty' \
        | base64 -d 2>/dev/null || true
      )

      if [[ -n "${DB_PASSWORD}" ]]; then
        echo "db_password=${DB_PASSWORD}" >> "${META}"
      else
        echo "# db_password not found or unreadable" >> "${META}"
      fi
    fi

    echo "# PV/PVC mapping found for image=${IMG}" >> "${META}"
  else
    echo "# No PV/PVC mapping found for image=${IMG}" >> "${META}"
  fi

  echo "[RBD BACKUP] Wrote metadata: ${META}"
done

echo "[RBD BACKUP] Finished backup run at $(date)"
```

> ⚠ **Security note**: lines 138–146 capture the Authentik Postgres password into the `.meta.txt` file in plaintext. The file lives one day on CephFS (read access restricted to ceph admin keyring holders) before being snapshotted into the (encrypted) Kopia repo. If you ever take the wiki public, that's fine — only the *mechanism* leaks, not the value. Consider replacing the special-case with a SOPS-encrypted backup of the *whole* authentik-postgresql Secret instead, so this script doesn't need a per-app branch.

### 6b. `k3s-pv-index.sh`

**Lives at**: `/usr/local/sbin/k3s-pv-index.sh` on each k3s master VM (`mainertoo-k3s-master-1`, `-2`, `-3`)
**Cron** (root, staggered to avoid all three writing simultaneously):

```
# master-1
5 1 * * * /usr/local/sbin/k3s-pv-index.sh >/var/log/k3s-pv-index.log 2>&1
# master-2
7 1 * * * /usr/local/sbin/k3s-pv-index.sh >/var/log/k3s-pv-index.log 2>&1
# master-3
9 1 * * * /usr/local/sbin/k3s-pv-index.sh >/var/log/k3s-pv-index.log 2>&1
```

**Output**: 3 CSVs in `/var/backups/` inside the master VM. Captured on the next vzdump run (02:00) into PBS.

```bash
#!/bin/bash
set -euo pipefail

# Make sure cron can find kubectl, jq, etc.
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Output directory INSIDE the VM
INDEX_DIR="/var/backups"

# Output files
RBD_FILE="${INDEX_DIR}/rbd-pv-index-k3s.csv"
CEPHFS_FILE="${INDEX_DIR}/cephfs-pv-index-k3s.csv"
COMBINED_FILE="${INDEX_DIR}/pv-index-k3s.csv"

# k3s kubeconfig (root can read this)
KUBECONFIG="/etc/rancher/k3s/k3s.yaml"
KCTL="kubectl --kubeconfig=${KUBECONFIG}"

mkdir -p "${INDEX_DIR}"

TMP_RBD="$(mktemp)"
TMP_CEPHFS="$(mktemp)"
TMP_ALL="$(mktemp)"
trap 'rm -f "$TMP_RBD" "$TMP_CEPHFS" "$TMP_ALL"' EXIT

HOST="$(hostname)"
TS="$(date --iso-8601=seconds)"

# Best-effort node UUID (matches what dmidecode calls system-uuid)
NODE_UUID="unknown"
if [[ -r /sys/class/dmi/id/product_uuid ]]; then
  NODE_UUID="$(cat /sys/class/dmi/id/product_uuid | tr '[:lower:]' '[:upper:]' || true)"
fi
if [[ -z "${NODE_UUID}" ]]; then
  NODE_UUID="unknown"
fi

# Grab PVs once
PV_JSON="$(${KCTL} get pv -o json)"

########################################
# RBD inventory
########################################
{
  echo "# RBD PV index for k3s cluster"
  echo "# Host: ${HOST}"
  echo "# Node UUID: ${NODE_UUID}"
  echo "# Generated: ${TS}"
  echo "#"
  echo "# Columns:"
  echo "# node_hostname,node_uuid,type,pvc_namespace,pvc_name,pv_name,storage_class,volume_handle,image_name,volume_name"
  echo

  echo "${PV_JSON}" | jq -r --arg host "${HOST}" --arg uuid "${NODE_UUID}" '
    .items[]
    | select(.spec.csi.driver == "rbd.csi.ceph.com")
    | [
        $host,
        $uuid,
        "rbd",
        (.spec.claimRef.namespace // ""),
        (.spec.claimRef.name // ""),
        .metadata.name,
        (.spec.storageClassName // ""),
        (.spec.csi.volumeHandle // ""),
        (.spec.csi.volumeAttributes.imageName // ""),
        (.spec.csi.volumeAttributes.volumeName // "")
      ]
    | @csv
  '
} > "${TMP_RBD}"

mv "${TMP_RBD}" "${RBD_FILE}"

########################################
# CephFS inventory (inventory only)
########################################
{
  echo "# CephFS PV index for k3s cluster"
  echo "# Host: ${HOST}"
  echo "# Node UUID: ${NODE_UUID}"
  echo "# Generated: ${TS}"
  echo "#"
  echo "# Columns:"
  echo "# node_hostname,node_uuid,type,pvc_namespace,pvc_name,pv_name,storage_class,volume_handle,subvolume_name,fs_name,subvolume_group"
  echo

  echo "${PV_JSON}" | jq -r --arg host "${HOST}" --arg uuid "${NODE_UUID}" '
    .items[]
    | select(.spec.csi.driver == "cephfs.csi.ceph.com")
    | [
        $host,
        $uuid,
        "cephfs",
        (.spec.claimRef.namespace // ""),
        (.spec.claimRef.name // ""),
        .metadata.name,
        (.spec.storageClassName // ""),
        (.spec.csi.volumeHandle // ""),
        (.spec.csi.volumeAttributes.subvolumeName // ""),
        (.spec.csi.volumeAttributes.fsName // ""),
        (.spec.csi.volumeAttributes.subvolumeGroup // "")
      ]
    | @csv
  '
} > "${TMP_CEPHFS}"

mv "${TMP_CEPHFS}" "${CEPHFS_FILE}"

########################################
# Combined (optional)
########################################
{
  echo "# Combined PV index for k3s cluster (RBD + CephFS)"
  echo "# Host: ${HOST}"
  echo "# Node UUID: ${NODE_UUID}"
  echo "# Generated: ${TS}"
  echo "#"
  echo "# Columns:"
  echo "# node_hostname,node_uuid,type,pvc_namespace,pvc_name,pv_name,storage_class,volume_handle,detail1,detail2,detail3"
  echo

  echo "${PV_JSON}" | jq -r --arg host "${HOST}" --arg uuid "${NODE_UUID}" '
    .items[]
    | if .spec.csi.driver == "rbd.csi.ceph.com" then
        [
          $host,
          $uuid,
          "rbd",
          (.spec.claimRef.namespace // ""),
          (.spec.claimRef.name // ""),
          .metadata.name,
          (.spec.storageClassName // ""),
          (.spec.csi.volumeHandle // ""),
          (.spec.csi.volumeAttributes.imageName // ""),
          (.spec.csi.volumeAttributes.volumeName // ""),
          ""
        ]
      elif .spec.csi.driver == "cephfs.csi.ceph.com" then
        [
          $host,
          $uuid,
          "cephfs",
          (.spec.claimRef.namespace // ""),
          (.spec.claimRef.name // ""),
          .metadata.name,
          (.spec.storageClassName // ""),
          (.spec.csi.volumeHandle // ""),
          (.spec.csi.volumeAttributes.subvolumeName // ""),
          (.spec.csi.volumeAttributes.fsName // ""),
          (.spec.csi.volumeAttributes.subvolumeGroup // "")
        ]
      else
        empty
      end
    | @csv
  '
} > "${TMP_ALL}"

mv "${TMP_ALL}" "${COMBINED_FILE}"

echo "[k3s-pv-index] Wrote:"
echo "  - ${RBD_FILE}"
echo "  - ${CEPHFS_FILE}"
echo "  - ${COMBINED_FILE}"
echo "[k3s-pv-index] Node UUID: ${NODE_UUID}"
```

---

## 7. Cron entries (verbatim, by host)

### `pve-ugreen` (root crontab)

```cron
# nightly ceph-rbd backup to run before Kopia sends it to zbackup
30 1 * * * /usr/local/sbin/rbd-nightly-backup.sh >> /var/log/rbd-nightly-backup.log 2>&1
0 3 * * * rm -rf /var/tmp/vzdumptmp*
```

### `kopia` LXC (root crontab)

```cron
# Daily CephFS backup to zbackup via Kopia
45 2 * * * /usr/bin/kopia snapshot create /mnt/cephfs --parallel=4 >> /var/log/kopia-cephfs.log 2>&1
10 3 * * * /usr/bin/kopia snapshot create /mnt/cephfs-k3s --parallel=4 >> /var/log/kopia-cephfs-k3s.log 2>&1
30 3 * * * /usr/bin/kopia snapshot create /mnt/qnap_alldata --parallel=8 >> /var/log/kopia-qnap.log 2>&1
```

### k3s masters (root crontab on each — staggered minutes)

```cron
# master-1 (192.168.90.161)
5 1 * * * /usr/local/sbin/k3s-pv-index.sh >/var/log/k3s-pv-index.log 2>&1

# master-2 (192.168.90.162)
7 1 * * * /usr/local/sbin/k3s-pv-index.sh >/var/log/k3s-pv-index.log 2>&1

# master-3 (192.168.90.163)
9 1 * * * /usr/local/sbin/k3s-pv-index.sh >/var/log/k3s-pv-index.log 2>&1
```

### `pve-mammoth` / `pve-whistler` / `pve-zermatt` / `pve-mac`

Empty (vzdump runs from `pvescheduler.service` reading `/etc/pve/jobs.cfg`, not from cron).

---

## 8. Configuration files (verbatim)

### Kopia LXC mountpoints — `/etc/pve/lxc/111.conf` on `pve-ugreen`

```
arch: amd64
cores: 2
features: nesting=1
hostname: kopia-lxc
memory: 4096
mp0: /mnt/pve/cephfs-swarm,mp=/mnt/cephfs
mp1: /zbackup,mp=/mnt/zbackup
mp2: /mnt/pve/cephfs-k3s,mp=/mnt/cephfs-k3s
mp3: /mnt/pve/tank,mp=/mnt/qnap_alldata
net0: name=eth0,bridge=vmbr0,hwaddr=BC:24:11:A2:BB:B1,ip=dhcp,type=veth
onboot: 1
ostype: debian
rootfs: local:111/vm-111-disk-0.raw,size=50G
swap: 0
tags: backup;kopia
unprivileged: 0
```

> Note: `unprivileged: 0` (privileged). Required so the kernel CephFS client and ZFS bind-mount work cleanly.

### Kopia repository — `kopia repository status` on kopia-lxc

```
Description:         Repository in Filesystem: /mnt/zbackup/kopia-repo
Storage type:        filesystem
Storage capacity:    23.8 TB
Storage available:   23.1 TB
Hash:                BLAKE2B-256-128
Encryption:          AES256-GCM-HMAC-SHA256
Splitter:            DYNAMIC-4M-BUZHASH
Format version:      3
Content compression: true
Epoch Manager:       enabled
```

### Kopia global retention policy

```
Annual snapshots:                        3
Monthly snapshots:                      24
Weekly snapshots:                        4
Daily snapshots:                         7
Hourly snapshots:                       48
Latest snapshots:                       10
```

### Kopia per-source ignore rules (post-fix)

```
/mnt/cephfs-k3s   →   ignore:  /volumes/_deleting/
```

Apply with `kopia policy set --add-ignore "/volumes/_deleting/" /mnt/cephfs-k3s` on kopia-lxc.

### PBS — `/etc/proxmox-backup/datastore.cfg`

```
datastore: pbs-backups
	gc-schedule daily
	notification-mode notification-system
	path /mnt/pbs-backups
```

### PBS — `/etc/proxmox-backup/prune.cfg`

```
prune: default-pbs-backups-2b425983-d26
	keep-daily 7
	keep-last 17
	keep-monthly 2
	keep-weekly 8
	schedule daily
	store pbs-backups
```

### PBS — verify job (queryable, no cfg file)

```
id: v-e00654e0-3168
store: pbs-backups
schedule: monthly
ignore-verified: 1
outdated-after: 30 days
```

### PBS — `/etc/fstab` (datastore mount)

```
/dev/pbs/root / ext4 errors=remount-ro 0 1
/dev/pbs/swap none swap sw 0 0
proc /proc proc defaults 0 0
192.168.1.252:/proxmox/proxmox-backup-server /mnt/pbs-backups nfs defaults 0 0
```

### PVE backup job — `/etc/pve/jobs.cfg` (cluster-wide)

```
vzdump: backup-f4e795e8-28be
	schedule 02:00
	all 1
	enabled 1
	exclude 299
	fleecing 0
	mode snapshot
	notes-template {{guestname}}
	storage pbs-backups
```

### Volsync ReplicationSource template — `components/volsync-v2/backup/volsync-replicationsource.yaml`

```yaml
apiVersion: volsync.backube/v1alpha1
kind: ReplicationSource
metadata:
  name: "${APP}"
spec:
  sourcePVC: "${APP}"
  trigger:
    schedule: "${VOLSYNC_SCHEDULE:=05 00/12 * * *}"
  restic:
    copyMethod: Snapshot
    pruneIntervalDays: 14
    repository: "${APP}-volsync"
    volumeSnapshotClassName: "${VOLSYNC_SNAPSHOTCLASS:=ceph-rbd-snapclass}"
    cacheCapacity: "${VOLSYNC_CACHE_CAPACITY:=10Gi}"
    cacheStorageClassName: "${VOLSYNC_CACHE_STORAGECLASS:=ceph-rbd}"
    cacheAccessModes: ["${VOLSYNC_CACHE_ACCESSMODES:=ReadWriteOnce}"]
    storageClassName: "${VOLSYNC_STORAGECLASS:=ceph-rbd}"
    accessModes: ["${VOLSYNC_SNAP_ACCESSMODES:=ReadWriteOnce}"]
    moverSecurityContext:
      runAsUser: ${VOLSYNC_PUID:=1000}
      runAsGroup: ${VOLSYNC_PGID:=1000}
      fsGroup: ${VOLSYNC_PGID:=1000}
    retain:
      hourly: 24
      daily: 7
      weekly: 5
      monthly: 3
```

### QNAP NFS exports (relevant ones)

```
/share/CACHEDEV1_DATA/CACHEDEV1_DATA      *(ro)  192.168.1.0/24(rw)        # legacy "all data" root export
/share/NFSv=4/ALLDATA                     *(ro)  192.168.1.0/24(rw)        # alias of above via NFSv4 root
/share/CACHEDEV1_DATA/QNAS                  192.168.1.20(rw)               # used by Kopia /mnt/qnap_alldata
/share/CACHEDEV1_DATA/proxmox               192.168.1.20,1.245(rw)         # PBS datastore (NOT Kopia)
/share/CACHEDEV1_DATA/pve-ha                192.168.1.20,1.244,1.253(rw)   # PVE shared-nfs
/share/CACHEDEV1_DATA/Container             192.168.1.0/24(rw)             # docker-swarm era; not in Kopia
```

---

## 9. Critical findings & gaps

### F1 — PBS data has no secondary copy *(severity: HIGH)*

PBS writes 301 GB to `/share/CACHEDEV1_DATA/proxmox/proxmox-backup-server/` on QNAP. Kopia's QNAS job mounts `qnas:/QNAS` (= `/share/CACHEDEV1_DATA/QNAS`) only. Single QNAP volume = single point of failure for every VM/LXC backup.

**Fix shape**:

1. On `pve-ugreen` add NFS mount to `/etc/fstab`:
   ```
   192.168.1.252:/proxmox/proxmox-backup-server  /mnt/pve/proxmox-backup-server  nfs  defaults,_netdev  0  0
   ```
2. Edit `/etc/pve/lxc/111.conf` and add:
   ```
   mp4: /mnt/pve/proxmox-backup-server,mp=/mnt/qnap_pbs
   ```
3. Restart LXC 111: `pct restart 111`
4. Add to kopia LXC root crontab:
   ```cron
   45 4 * * * /usr/bin/kopia snapshot create /mnt/qnap_pbs --parallel=4 >> /var/log/kopia-pbs.log 2>&1
   ```
   (04:45 is after PBS daily prune completes; captures a stable repo state.)

### F2 — Garage S3 has no secondary copy *(severity: HIGH)*

Garage's data PVC is NFS-backed by `qnas:/share/CACHEDEV1_DATA/garage` (167 GB). Lose the QNAP and every volsync repo dies with it.

**Fix shape**: same NFS-mount-then-Kopia pattern as F1, with cron at `30 4 * * *`. Caveat: Garage chunks change mid-flight; expect some snapshots to be internally inconsistent. Mitigated by content-addressing on both sides — recoverability remains good but not perfect. Cleaner long-term option: configure Garage native bucket replication or `rclone sync` to a real second target.

### F3 — Single failure domain on QNAP (compounds F1+F2)

PBS, Garage, K8s NFS-CSI, `pve-ha` shared storage, and Kopia's QNAS source are all on one QNAP. Kopia → zbackup is the only path that escapes this domain, and only for one of those four. Mitigated by F1+F2 fixes plus the offsite proposal in [§11](#11-offsite-dr-comparison).

### F4 — PostgreSQL backed up via raw-PVC volsync *(severity: MEDIUM)*

`dawarich-db` and `authentik-postgresql` are PostgreSQL data volumes backed up by volsync directly. **Crash-consistent, not app-consistent**. Restore may produce a corrupt database needing `pg_resetwal` or worse.

**Fix shape**: migrate both to CNPG with native logical/physical backup to S3. Separate project. Until then, keep the current backup but add a periodic `pg_dump`-to-volsync sidecar.

### F5 — No PBS sync target; no offsite copy of any layer *(severity: MEDIUM)*

`/etc/proxmox-backup/sync.cfg` is empty. The backup graph terminates at zbackup, in the same physical room and on the same admin credentials as everything else. See [§11](#11-offsite-dr-comparison).

### F6 — PBS prune keeps only 2 monthly snapshots *(severity: LOW)*

A regression detected ≥3 months after introduction has no clean rollback. Recommend `keep-monthly: 6`.

### F7 — Kopia logs unrotated *(severity: LOW)*

`/var/log/kopia-cephfs.log` is 76 MB. Will eventually fill the LXC root.

**Fix**: drop on kopia-lxc:

```
# /etc/logrotate.d/kopia
/var/log/kopia-*.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    copytruncate
}
```

### F8 — Kopia maintenance is auto-scheduled; explicit cron is belt-and-braces *(severity: LOW)*

Verified during audit: Kopia 0.22 has auto-maintenance enabled on this repo:

- **Quick cycle**: every 1 h
- **Full cycle**: every 24 h
- **Cleanup-logs**: runs daily ~21:50 UTC (visible in `kopia maintenance info` history)

So this is fine as-is. Optional belt-and-braces explicit weekly cron:

```cron
0 5 * * 0 /usr/bin/kopia maintenance run --full --safety=full >> /var/log/kopia-maint.log 2>&1
```

### F9 — `kube-rbd` pool empty *(severity: trivial)*

Legacy. Removable. No backup impact.

### F10 — `mp2` LXC mountpoint not visible on host *(severity: trivial)*

The LXC mounts CephFS k3s-fs directly via the kernel ceph client (privileged container), bypassing the host's `mp2` bind. Harmless today but means the LXC depends on having ceph keyring + connectivity directly inside it. If the LXC ever moves to `unprivileged`, this breaks.

---

## 10. Recommendations (prioritized)

| Priority | Action | Effort | Buys you |
|---|---|---|---|
| P0 | Mirror PBS dir into Kopia (F1) | 30 min | A second copy of every VM/LXC backup |
| P0 | Mirror Garage dir into Kopia, OR set up Garage replication / rclone (F2) | 1 hr / 4 hr | A second copy of every volsync repo |
| P0 | Stand up an offsite target (see §11) | 1–8 hr | Real disaster recovery |
| P1 | Bump PBS `keep-monthly` to 6 | 1 min | Longer regression-detection window |
| P1 | Add logrotate for `/var/log/kopia-*.log` | 5 min | Prevent kopia-lxc / filling |
| P1 | Add weekly `kopia maintenance run --full` cron | 5 min | Repo health, faster restores |
| P1 | Add monthly `kopia snapshot verify --verify-files-percent=1` per source | 10 min | Catch repo corruption early |
| P1 | Test-restore a VM and a PVC quarterly (calendar reminder) | 30 min/qtr | Backup is unproven until restored |
| P2 | Migrate `dawarich-db` and `authentik-postgresql` to CNPG | 2–4 hr each | App-consistent DB backups |
| P2 | Replace `db_password=` special-case in `rbd-nightly-backup.sh` with SOPS-encrypted backup of the whole Secret | 1 hr | Cleaner secret handling |
| P2 | Document this audit's findings in CLAUDE.md | 5 min | Continuity for future work |
| P3 | Drop legacy `kube-rbd` Ceph pool | 5 min | Cleanup |
| P3 | Garage replication or HA config | half-day | Resilience inside cluster |

---

## 11. Offsite DR comparison

Three serious paths. Differences:

### Option A — Garage S3 mirror (Garage → Garage)

| Pro | Con |
|---|---|
| Native to volsync's S3 backend | Only covers Layer 2; PBS/Kopia layers still local |
| Restic data is end-to-end encrypted (offsite host can be untrusted) | Requires running a second Garage cluster (operational complexity) |
| Replication is delta-only (chunks) | If volsync produces broken backups, this faithfully replicates them |
| | Recovery: re-point volsync at offsite endpoint |

### Option B — Ceph RGW S3 mirror

| Pro | Con |
|---|---|
| If you ever phase out Garage, RGW is the natural successor | RGW multisite setup is heavyweight (realm/zonegroup/zone) |
| Lives in the system you already operate | Adds blast-radius surface in same physical cluster |
| | Doesn't solve PBS/Kopia layers either |

### Option C — Kopia repository sync to offsite

| Pro | Con |
|---|---|
| Covers Layers 3+4+5 already; AND once F1+F2 are fixed, it covers ALL layers | Same compromise risk: corrupt local repo → corrupt offsite repo (mitigated by `kopia maintenance run --full --safety=full` weekly + monthly verify) |
| Lowest operational complexity (`kopia repository sync-to` over SFTP) | Bandwidth dominated by content-addressed deltas — small after first seed |
| End-to-end encryption with restic-equivalent password (offsite host untrusted) | Initial seed of 658 GB → 1+ TB (after F1+F2) takes time over residential upload |
| Recovery: `kopia repository connect sftp ...` from anywhere | |

### Recommendation: **Option C, after fixing F1 + F2**

Reasoning:

1. F1+F2 funnel everything into one Kopia repo on zbackup. After that, you have **one thing** to ship offsite.
2. `kopia repository sync-to` ships only repo deltas, end-to-end encrypted, deduplicated.
3. Target options (cheapest first): Raspberry Pi + 6 TB external HDD at a friend's house over Tailscale. Or B2/Wasabi/R2 (`kopia repository connect s3`).
4. Verify monthly: `kopia repository verify` against the offsite repo. This is what proves you have a real second copy — not just a configured one.
5. Migration path if the offsite host turns out to be S3-shaped (B2/Wasabi/R2): swap "Kopia SFTP repo" → "Kopia S3 repo". Same shape.

---

## 12. Recovery runbook

### 12.0 Triage — what's broken?

```bash
# Layer 1 (PBS reachable + datastore mounted)
$ ssh pbs 'mount | grep pbs-backups; proxmox-backup-manager datastore list'

# Layer 2 (volsync + Garage healthy)
$ ssh pve-ugreen 'kubectl get pods -n volsync-system; kubectl get pods -A | grep garage; kubectl get replicationsources -A | head'

# Layer 3+4+5 (Kopia repo healthy)
$ ssh kopia 'kopia repository status; kopia snapshot list | tail -20'

# Inventory: latest CSV
$ ssh ubuntu@192.168.90.161 'sudo head -20 /var/backups/pv-index-k3s.csv'

# Inventory: latest .meta.txt
$ ssh pve-ugreen 'ls /mnt/pve/cephfs-swarm/rbd-backup/ | head -1'
```

### 12.1 Restore a single PVC — preferred: volsync bootstrap (Layer 2)

```yaml
# apps/base/<app>/kustomization.yaml
components:
  - ../../../../components/volsync-v2/bootstrap   # was: components/volsync-v2

# apps/production/<app>/kustomization.yaml: bump to force re-restore
postBuild:
  substitute:
    APP: <app>
    VOLSYNC_RESTORE_TOKEN: "2026-05-07-restore-1"
```

Steps:

1. `flux suspend hr -n <ns> <app>` and scale Deployment/StatefulSet to 0
2. `kubectl -n <ns> delete pvc <app>`
3. Switch the kustomization to `volsync-v2/bootstrap`, commit, push
4. Watch: `kubectl -n <ns> get replicationdestination -w` then `kubectl -n <ns> get pvc -w`
5. Once PVC is `Bound` and RD is `Synchronized`, scale the app back up / resume the HelmRelease
6. **Settle**: switch the kustomization back to plain `volsync-v2`

### 12.2 Restore a single PVC — fallback: Kopia (Layer 3 or 4)

#### RBD-backed PVC

```bash
# 1. Identify the csi-vol UUID
k8s$ kubectl get pv $(kubectl -n <ns> get pvc <pvc> -o jsonpath='{.spec.volumeName}') \
     -o jsonpath='{.spec.csi.volumeAttributes.imageName}'
# Or from the inventory CSV:
$ ssh ubuntu@192.168.90.161 'sudo grep <pvc> /var/backups/rbd-pv-index-k3s.csv'

# 2. Mount the desired Kopia snapshot
kopia# kopia snapshot list /mnt/cephfs | tail -10
kopia# kopia snapshot mount <snapshot-id> /mnt/restore

# 3. Find image; verify with .meta.txt
kopia# ls /mnt/restore/rbd-backup/csi-vol-fa7e.../
kopia# cat /mnt/restore/rbd-backup/csi-vol-fa7e.../*.meta.txt

# 4. Push to a new RBD volume on a master VM
$ ssh kopia 'cat /mnt/restore/rbd-backup/csi-vol-fa7e.../csi-vol-fa7e...-2026-05-01.img' \
   | ssh ubuntu@192.168.90.161 'sudo tee /tmp/restore.img > /dev/null'
k8s$ sudo rbd import --image-format 2 /tmp/restore.img k3s-rbd/restore-<pvc>

# 5. Stop app, swap PV/PVC, start app (or use clone-and-rename)
# 6. Cleanup
kopia# kopia mount unmount /mnt/restore
```

#### CephFS-backed PVC

```bash
k8s$ kubectl get pv $(kubectl -n <ns> get pvc <pvc> -o jsonpath='{.spec.volumeName}') \
     -o jsonpath='{.spec.csi.volumeAttributes.subvolumeName}'

kopia# kopia snapshot list /mnt/cephfs-k3s
kopia# kopia snapshot mount <snapshot-id> /mnt/restore

# Data lives at /mnt/restore/volumes/csi/<subvolume>/<subvolume>/
# Provision a fresh empty PVC, attach a debug pod, then:
$ rsync -aHAX <kopia-source>/ <target-pvc-mount>/
```

### 12.3 Restore a single VM/LXC from PBS

Web UI: https://pbs:8007 → datastore `pbs-backups` → pick the VM/LXC ID → pick a snapshot → *Restore*.

CLI:

```bash
pve-mac# qmrestore pbs-backups:backup/vm/<vmid>/<timestamp> <new-vmid> --storage local-zfs --force 0
pve-mac# pct restore <new-vmid> pbs-backups:backup/ct/<lxcid>/<timestamp> --storage local-zfs
```

File-level extract:

```bash
pbs# proxmox-backup-client mount --repository localhost:pbs-backups \
        vm/<vmid>/<timestamp> root.pxar /mnt/pbs-extract
pbs# proxmox-backup-client unmount /mnt/pbs-extract
```

### 12.4 Cluster-nuke recovery (no k3s, but Proxmox up)

1. `cd terraform && terraform apply` — rebuilds VMs 661–666
2. `ansible-playbook -i ansible/k3s-cluster/inventory/dynamic_terraform_inventory.sh ansible/k3s-cluster/playbooks/k3s_install.yml`
3. Bootstrap Flux:
   ```bash
   k8s$ kubectl create ns flux-system
   k8s$ kubectl -n flux-system create secret generic sops-age --from-file=age.agekey=<your-age-key>
   k8s$ flux bootstrap github --owner=mainertoo --repository=kubernetes-lab \
        --branch=master --path=./clusters/production
   ```
4. Wait for `infrastructure` and `flux-system` Kustomizations to be Ready
5. For each app needing restore, switch kustomization to `components/volsync-v2/bootstrap` and commit. Flux reconciles, volsync pulls each PVC's latest snapshot from Garage.
6. Settle: switch each kustomization back to plain `volsync-v2` in a follow-up commit.

### 12.5 Total Ceph loss recovery

Sequence:

1. Rebuild Proxmox + Ceph (manual; not Terraform-managed). Recreate pools `k3s-rbd`, `k3s-fs-{data,metadata}`, `ceph-shared`, `ceph-swarm.{meta,data}`.
2. Re-establish CephFS with the same `mds_namespace` names (`k3s-fs`, `ceph-swarm`) so existing CSI volume handles validate.
3. Mount Kopia repo on a recovery VM.
4. Re-import RBD images:
   ```bash
   kopia# kopia snapshot mount <id> /mnt/restore
   for dir in /mnt/restore/rbd-backup/*/; do
       img=$(ls $dir/*.img | head -1)
       name=$(basename $dir)
       cat $dir/*.meta.txt
       rbd import "$img" "k3s-rbd/$name"
   done
   ```
5. Re-import CephFS subvolumes: `ceph fs subvolume create k3s-fs <name> <group>`, then `rsync -aHAX` from `/mnt/restore/volumes/csi/<subvol>/<subvol>/`.
6. Reconstruct PV objects in K8s using the latest `/var/backups/pv-index-k3s.csv` (recovered from PBS backup of master-1's VM disk).
7. Re-bootstrap Flux + apps (steps 3-6 of 12.4).

### 12.6 Loss of QNAP

Today: catastrophic for PBS + Garage + volsync history.
After F1+F2 fixes: Kopia snapshots of `/mnt/qnap_pbs` and `/mnt/qnap_garage` on zbackup are the source of truth. Procedure:

1. Stand up new NAS / repaired QNAP.
2. Restore `/share/CACHEDEV1_DATA/proxmox/proxmox-backup-server/` from kopia snapshot of `/mnt/qnap_pbs`.
3. Restore `/share/CACHEDEV1_DATA/garage/` from kopia snapshot of `/mnt/qnap_garage`.
4. Re-export NFS, point PBS and Garage at the restored paths.
5. Walk through 12.4 (cluster-nuke).

### 12.7 Loss of zbackup pool

Direct losses: all historical Kopia data; the `rbd-nightly-backup.sh` history beyond today (today's run still on cephfs-swarm).

Recovery:

1. Replace disks, recreate ZFS mirror, `kopia repository create filesystem`.
2. After offsite is implemented: pull the offsite repo back with `kopia repository sync-from`.
3. Live cluster + PBS continue unaffected.

### 12.8 Loss of Garage S3

```bash
# 1. Verify the data on QNAP NFS is intact
$ ssh qnas 'ls /share/CACHEDEV1_DATA/garage/'

# 2. Re-deploy Garage HelmRelease (Flux will re-mount the same NFS PVC)

# 3. Verify by listing one app's repo
k8s$ # Run a temporary mover Job pointing at the existing Secret

# 4. Trigger a re-sync
k8s$ kubectl annotate replicationsource <app> -n <ns> volsync.backube/triggered=manual
```

### 12.9 Quarterly verification

The only test of a backup is a restore. Each quarter, run:

```bash
# Volsync side-by-side: pick a small app like "dumb"
# Switch kustomization to volsync-v2/restore, bump VOLSYNC_RESTORE_TOKEN, watch <app>-restore PVC

# PBS test restore
pve-mac# pct restore 999 pbs-backups:backup/ct/104/<timestamp> --storage local-zfs
pve-mac# pct start 999 && pct enter 999  # validate
pve-mac# pct stop 999 && pct destroy 999 --purge

# Kopia repo verify
kopia# kopia snapshot verify --verify-files-percent=1 --max-failures-per-source=10
```

---

## 13. Inventory cheat sheet

When you have an opaque blob name (`csi-vol-XXX`, an RBD image name, a subvolume name) and need to know what app it belongs to:

```bash
# Source 1: live cluster (if up)
k8s$ kubectl get pv -o json | jq -r '.items[] | select(.spec.csi.volumeAttributes.imageName=="<image>")
        | "\(.spec.claimRef.namespace)/\(.spec.claimRef.name)"'

# Source 2: latest CSV index (preserved in PBS backup of master VM disk)
$ ssh ubuntu@192.168.90.161 'sudo grep <image-or-subvol> /var/backups/pv-index-k3s.csv'

# Source 3: .meta.txt next to the image in cephfs-swarm/Kopia
kopia# cat /mnt/cephfs/rbd-backup/<csi-vol-XXX>/*.meta.txt
# Or via a kopia snapshot mount of /mnt/cephfs first
```

The CSV path survives if Ceph is gone; the `.meta.txt` path survives if PBS is gone. Both surviving in different blast radii is the point of having two.

---

## 14. Common pitfalls

- **Forgetting to `flux suspend hr`** before deleting a PVC for restore — Flux re-creates the resources before volsync can stage the dataSource.
- **Switching to `volsync-v2/bootstrap` without bumping `VOLSYNC_RESTORE_TOKEN`** — RD doesn't re-fire if the spec is unchanged.
- **Restoring an RBD image into the wrong pool** — `k3s-rbd` is the live pool; importing to `kube-rbd` (legacy, empty) won't be usable.
- **PBS file-level extract requires the encryption keyfile** if the backup was encrypted. PBS backups here are unencrypted today.
- **Kopia mount on a privileged LXC** is fine; on an unprivileged LXC, FUSE may not work — use `kopia restore` to a target dir instead.
- **`rbd export` from a busy pool can take a long time** — schedule restores during quiet hours, or `rbd snap create` first and export the snap.
- **The k3s-pv-index CSV files only survive in PBS** — if PBS is also gone, you have to fall back to the `.meta.txt` files in Kopia's cephfs snapshots. Both should always be present in normal operation.

---

*Last updated: 2026-05-07. Audit by Claude Code session, captured every host and config.*

---

## Appendix A — Live Kopia state (2026-05-07)

> Reference snapshot of `kopia repository status`, `kopia maintenance info`, and recent `kopia snapshot list` output captured during audit. Future audits should diff against this.

### Repository

```
Description:         Repository in Filesystem: /mnt/zbackup/kopia-repo
Hostname:            kopia-lxc
Username:            root

Storage type:        filesystem
Storage capacity:    23.8 TB
Storage available:   23.1 TB
Storage config:      {
                       "path": "/mnt/zbackup/kopia-repo"
                     }

Hash:                BLAKE2B-256-128
Encryption:          AES256-GCM-HMAC-SHA256
Splitter:            DYNAMIC-4M-BUZHASH
Format version:      3
Content compression: true
Index Format:        v2
Epoch Manager:       enabled
Current Epoch:       83
```

### Maintenance schedule

```
Owner: root@kopia-lxc

Quick Cycle:
  scheduled: true
  interval:  1h0m0s

Full Cycle:
  scheduled: true
  interval:  24h0m0s

Log Retention:
  max count:       10000
  max age of logs: 720h (30 days)
  max total size:  1.1 GB

Object Lock Extension: disabled
```

Daily `cleanup-logs` run completes successfully (verified back to 2026-04-14 in maintenance history).

### Snapshot summary by source (2026-05-07)

| Source | # snapshots | Latest size | Latest timestamp | Notes |
|---|---|---|---|---|
| `/mnt/cephfs` | 16 | 1.0 TB | 2026-05-07 02:45 UTC | History back to 2025-11-30. Includes rbd-backup staging dir (~148 csi-vol exports). |
| `/mnt/cephfs-k3s` | 15 | 557.2 GB | 2026-05-07 03:10 UTC | `errors:751` on latest run (CephFS `_deleting/` tombstones — silenced post-audit by ignore rule) |
| `/mnt/qnap_alldata` | 14 | 279.1 GB | 2026-05-07 03:30 UTC | The "29 TB" total is the *source* size including subdirs not stored after dedup/compression — the encrypted+deduped repr is 279 GB |

### Recent snapshot history (last 3 per source)

```
/mnt/cephfs:
  2026-05-04 02:45 UTC   1 TB    files:279872 dirs:276195    (latest-3,daily-3)
  2026-05-06 02:45 UTC   1 TB    files:279858 dirs:276188    (latest-2,daily-2)
  2026-05-07 02:45 UTC   1 TB    files:279858 dirs:276188    (latest-1,daily-1,weekly-1,monthly-1,annual-1)

/mnt/cephfs-k3s:
  2026-05-04 03:10 UTC   517.1 GB  files:1665091 dirs:1817864          (latest-3,daily-3)
  2026-05-06 03:10 UTC   552.1 GB  files:2139984 dirs:1947996  err:41  (latest-2,hourly-2,daily-2)
  2026-05-07 03:10 UTC   557.2 GB  files:2151006 dirs:1961780  err:751 (latest-1,hourly-1,daily-1,weekly-1,monthly-1,annual-1)

/mnt/qnap_alldata:
  2026-05-05 03:30 UTC   278.6 GB  files:86181 dirs:59344    (latest-3,hourly-3,daily-3)
  2026-05-06 03:30 UTC   279.1 GB  files:86470 dirs:59607    (latest-2,hourly-2,daily-2)
  2026-05-07 03:30 UTC   279.1 GB  files:86533 dirs:59694    (latest-1,hourly-1,daily-1,weekly-1,monthly-1,annual-1)
```

### Per-source ignore rules (post-audit)

```
/mnt/cephfs-k3s   →   /volumes/_deleting/
```

---

## Appendix B — Live K8s / volsync state (2026-05-07)

### ReplicationSource counts (77 total)

| Namespace | Count |
|---|---|
| media | 39 |
| home-assistant | 6 |
| wiki-js | 2 |
| sparky-fitness | 2 |
| dawarich | 2 |
| authentik | 2 |
| (each of the rest) | 1 |
| Other singletons | wallos, vaultwarden, ui-toolkit, tandoor, scrypted, paperless-ngx, open-notebook, memos, mealie, … |

### Sync health (audit time)

- **Failures or never-synced**: none
- **Most recent runs ≤ 1 h**: 76 of 77 ReplicationSources
- **Long-running outlier on most recent run**: `dumb/dumb` took 2 h 51 m (large delta, single occurrence)

### RBD backup staging (`/mnt/pve/cephfs-swarm/rbd-backup/`)

- **148 csi-vol-/csi-snap- directories** present at audit time
- Sample `.meta.txt` (from `csi-snap-d632538c-…`):

```
backup_timestamp=2026-05-07T01:30:06-09:00
pool=k3s-rbd
image=csi-snap-d632538c-2b11-48ed-b2cf-50bcbc702023
export_path=/mnt/pve/cephfs-swarm/rbd-backup/csi-snap-…/csi-snap-…-2026-05-07.img
export_size_bytes=1073741824
flux_last_applied_revision=master@sha1:0f7c0079cf833557db4fa638189bc010dee826e0

# No PV/PVC mapping found for image=csi-snap-d632538c-2b11-48ed-b2cf-50bcbc702023
```

> **Note**: the `# No PV/PVC mapping found` line appears for `csi-snap-` (CSI snapshots, not PVCs). The matching logic in `rbd-nightly-backup.sh` only matches CSI volumes (`csi-vol-…`) against PVs. CSI snapshots have no `claimRef`, so they fall through. Behavior is correct, but it's worth knowing that the `.meta.txt` files for snapshots are intentionally sparse.

---

## Appendix C — Sample of `pv-index-k3s.csv` (master-1, generated 2026-05-08 01:05 UTC)

> First 17 rows for reference. The full file lives in `/var/backups/` on each master VM and is captured into PBS by the 02:00 vzdump.

```csv
# Combined PV index for k3s cluster (RBD + CephFS)
# Host: mainertoo-k3s-master-1
# Node UUID: 96663D46-DEA2-4653-9A2A-6910436D1A3C
# Generated: 2026-05-08T01:05:01+02:00
#
# Columns:
# node_hostname,node_uuid,type,pvc_namespace,pvc_name,pv_name,storage_class,volume_handle,detail1,detail2,detail3

"mainertoo-k3s-master-1","…","cephfs","media","jackett-downloads","pvc-00bdbed2-…","cephfs","…-ad45836b-…","csi-vol-ad45836b-…","k3s-fs",""
"mainertoo-k3s-master-1","…","rbd","media","volsync-src-readmeabook-cache","pvc-02989b58-…","ceph-rbd","…-06c6ab31-…","csi-vol-06c6ab31-…","",""
"mainertoo-k3s-master-1","…","rbd","donetick","donetick","pvc-03b09cb4-…","ceph-rbd","…-89564c0f-…","csi-vol-89564c0f-…","",""
"mainertoo-k3s-master-1","…","cephfs","media","plex","pvc-0467676d-…","cephfs","…-85cdea20-…","csi-vol-85cdea20-…","k3s-fs",""
"mainertoo-k3s-master-1","…","rbd","joplin","joplin","pvc-0c1c4556-…","ceph-rbd","…-2d25f4eb-…","csi-vol-2d25f4eb-…","",""
"mainertoo-k3s-master-1","…","rbd","media","riven-data-pvc","pvc-0cc67548-…","ceph-rbd","…-fd10d8a3-…","csi-vol-fd10d8a3-…","",""
"mainertoo-k3s-master-1","…","rbd","home-assistant","node-red","pvc-0d715e73-…","ceph-rbd","…-1d0c4b09-…","csi-vol-1d0c4b09-…","",""
"mainertoo-k3s-master-1","…","rbd","scrypted","scrypted","pvc-0da25803-…","ceph-rbd","…-d831cd3d-…","csi-vol-d831cd3d-…","",""
"mainertoo-k3s-master-1","…","rbd","homepage","volsync-src-homepage-cache","pvc-0fd7d8de-…","ceph-rbd","…-cbf8d571-…","csi-vol-cbf8d571-…","",""
"mainertoo-k3s-master-1","…","rbd","media","volsync-src-radarr4k-cache","pvc-1695d36d-…","ceph-rbd","…-a2560d7b-…","csi-vol-a2560d7b-…","",""
"mainertoo-k3s-master-1","…","rbd","dawarich","volsync-src-dawarich-media-cache","pvc-195fc442-…","ceph-rbd","…-eed072da-…","csi-vol-eed072da-…","",""
"mainertoo-k3s-master-1","…","rbd","dumbassets","volsync-src-dumbassets-cache","pvc-1bcf5742-…","ceph-rbd","…-039a17d6-…","csi-vol-039a17d6-…","",""
"mainertoo-k3s-master-1","…","rbd","grafana","volsync-src-grafana-cache","pvc-1dcb3cd3-…","ceph-rbd","…-66aa1a6c-…","csi-vol-66aa1a6c-…","",""
"mainertoo-k3s-master-1","…","rbd","media","volsync-src-shared-media-pvc-cache","pvc-208f9edc-…","ceph-rbd","…-4d16a79b-…","csi-vol-4d16a79b-…","",""
"mainertoo-k3s-master-1","…","rbd","authentik","volsync-src-authentik-media-cache","pvc-226a951f-…","ceph-rbd","…-f75811a5-…","csi-vol-f75811a5-…","",""
"mainertoo-k3s-master-1","…","cephfs","media","notifiarr-shared","pvc-24e14b03-…","cephfs","…-426e6b4b-…","csi-vol-426e6b4b-…","k3s-fs",""
"mainertoo-k3s-master-1","…","rbd","media","volsync-src-riven-backend-pvc-cache","pvc-256671e2-…","ceph-rbd","…-3448d72d-…","csi-vol-3448d72d-…","",""
…
```

> The volume_handle / fsid columns are truncated above (`…`) for readability — the live CSV has the full IDs needed for an actual restore mapping.

---

## Appendix D — Live PBS state (2026-05-07)

```
Datastore:    pbs-backups
Path:         /mnt/pbs-backups (NFS to qnas:/proxmox/proxmox-backup-server)
Used:         32 TB / 72 TB available (44%)
PBS uptime:   3d 2h 15m
PBS version:  4.2.0-1
GC schedule:  daily
Prune:        keep-last 17, keep-daily 7, keep-weekly 8, keep-monthly 2
Verify job:   v-e00654e0-3168, schedule monthly, ignore-verified=true
Sync jobs:    none configured
Remote PBS:   none configured
```

---

## Appendix E — How to refresh these appendices

Run during a future audit:

```bash
# Kopia state
ssh kopia '
  echo "=== REPO ===";   kopia repository status
  echo "=== MAINT ==="; kopia maintenance info
  for src in /mnt/cephfs /mnt/cephfs-k3s /mnt/qnap_alldata; do
    echo "=== $src ==="; kopia snapshot list "$src" | tail -3
  done
'

# K8s state
ssh pve-ugreen '
  kubectl --kubeconfig=/root/.kube/config get replicationsources -A --no-headers | wc -l
  kubectl --kubeconfig=/root/.kube/config get replicationsources -A --no-headers | awk "{print \$1}" | sort | uniq -c | sort -rn
  ls /mnt/pve/cephfs-swarm/rbd-backup/ | wc -l
'

# PBS state
ssh pbs '
  df -h /mnt/pbs-backups
  proxmox-backup-manager datastore list
  proxmox-backup-manager verify-job list
'

# CSV inventory sample
ssh ubuntu@192.168.90.161 'sudo head -25 /var/backups/pv-index-k3s.csv'
```

Replace the dated values in Appendices A–D and bump the "Last updated" line.
