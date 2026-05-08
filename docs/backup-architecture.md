# Backup Architecture

> Audited 2026-05-07 by reading every host, cron entry, script body, and config end-to-end. Captures **the actual state of the system on that date**, not the way I described it from memory. Companion runbook: `docs/backup-recovery.md`.

---

## TL;DR

Five independent backup layers, each with its own schedule and target:

| # | What | Tool | Source → Target |
|---|---|---|---|
| 1 | VMs + LXCs | PVE vzdump → PBS | All guests except PBS itself → `pbs-backups` datastore (NFS to QNAP) |
| 2 | K8s app PVCs (live) | volsync (Restic) | PVC snapshot → Garage S3 (Docker container on QNAP) |
| 3 | Ceph RBD images | `rbd-nightly-backup.sh` → Kopia | `rbd export` → CephFS staging → Kopia source `/mnt/rbd-backup` → zbackup ZFS |
| 4 | Ceph FS subvolumes | Kopia (kernel mount) | CephFS `k3s-fs` → Kopia source `/mnt/cephfs-k3s` → zbackup ZFS |
| 5 | QNAP `/QNAS` subtree | Kopia (NFS mount) | QNAP `/share/CACHEDEV1_DATA/QNAS` → Kopia source `/mnt/qnap_alldata` → zbackup ZFS |
| 6 | PBS datastore mirror (F1) | Kopia (NFS RO) | QNAP `/proxmox/proxmox-backup-server` → Kopia source `/mnt/qnap_pbs` → zbackup |
| 7 | Garage S3 mirror (F2) | Kopia (NFS RO + subdir bind) | QNAP `/share/CACHEDEV1_DATA/garage` → Kopia source `/mnt/qnap_garage` → zbackup |
| 8 | QNAP Container Station mirror | Kopia (NFS RO + subdir bind) | QNAP `/share/CACHEDEV1_DATA/Container` → Kopia source `/mnt/qnap_container` → zbackup |
| 9 | QNAP appdata mirror | Kopia (NFS RO + subdir bind) | QNAP `/share/CACHEDEV1_DATA/appdata` → Kopia source `/mnt/qnap_appdata` → zbackup |

**Originally surfaced critical gaps** (PBS data, Garage data, QNAP Container/appdata) **all closed 2026-05-08**: every layer now mirrors to zbackup ZFS via Kopia. Same-day follow-up renamed Layer 3 source from `/mnt/cephfs` → `/mnt/rbd-backup` and cleaned ~122 GB of legacy docker-swarm content from cephfs.

---

## 1. Hardware & networks

```
                     ┌─────────────────────────────────────────────────────┐
                     │                  Management LAN 192.168.1.0/24       │
                     └────────┬───────────┬───────────┬─────────────┬──────┘
                              │           │           │             │
       ┌──────────────────────┼───────────┼───────────┼─────────────┼─────────────┐
       │                      │           │           │             │             │
  ┌────┴─────┐         ┌──────┴──┐  ┌─────┴───┐  ┌────┴─────┐  ┌────┴─────┐  ┌────┴─────┐
  │ pve-     │  TB4    │ pve-    │  │ pve-    │  │ pve-mac  │  │ pve-     │  │  qnas    │
  │ mammoth  │ <-----> │ whistler│  │ zermatt │  │  .250    │  │ ugreen   │  │  .252    │
  │  .109    │         │  .107   │  │  .108   │  │          │  │  .251    │  │          │
  │          │         │         │  │         │  │ pbs(VM)  │  │          │  │ TS-X32   │
  │ k3s-m1   │         │ k3s-m2  │  │ k3s-m3  │  │  →.245   │  │ kopia-lxc│  │ 6×14TB   │
  │ k3s-w1   │         │ k3s-w2  │  │ k3s-w3  │  │ adguard  │  │ Win11    │  │ +2×2TB   │
  │ plex(LXC)│         │         │  │         │  │ tailscale│  │ zwave    │  │ cache    │
  │ ceph osd │         │ ceph osd│  │ ceph osd│  │          │  │          │  │ Garage   │
  │ 0,3      │         │ 2,5     │  │ 1,4     │  │          │  │ ZFS pool │  │ container│
  └──────────┘         └─────────┘  └─────────┘  └──────────┘  │ "zbackup"│  │ (?)      │
                                                               │ 2×24TB   │  └──────────┘
                                                               │ mirror   │
                                                               └──────────┘

  Ceph public/cluster network 192.168.99.0/24 (Thunderbolt 4 mesh, mons on .12/.13/.14)
  K3s LAN 192.168.90.0/24 (k3s-master-{1,2,3} = .161/.162/.163, workers .164/.165/.166)
```

### Hosts at a glance

| Host | SSH name | IP | Role |
|---|---|---|---|
| Proxmox node 1 | `pve-mammoth` | 192.168.1.109 | Ceph OSD 0,3 / k3s-master-1 + worker-1 / plex LXC |
| Proxmox node 2 | `pve-whistler` | 192.168.1.107 | Ceph OSD 2,5 / k3s-master-2 + worker-2 |
| Proxmox node 3 | `pve-zermatt` | 192.168.1.108 | Ceph OSD 1,4 / k3s-master-3 + worker-3 |
| Mac mini Proxmox | `pve-mac` | 192.168.1.250 | Hosts PBS VM + adguard + tailscale LXCs |
| UGREEN NAS Proxmox | `pve-ugreen` | 192.168.1.251 | ZFS `zbackup` pool, kopia-lxc, zwave-js |
| QNAP TS-X32 | `qnas` | 192.168.1.252 | NFS exports, Garage S3 backend (NFS-backed) |
| PBS VM | `pbs` | 192.168.1.245 | Proxmox Backup Server, datastore on NFS |
| Kopia LXC | `kopia` | DHCP on vmbr0 | All Kopia snapshot jobs |
| K3s masters | `ubuntu@192.168.90.16{1,2,3}` | — | Control plane, run `k3s-pv-index.sh` cron |

### Storage pools

| Pool | Type | Where | Capacity | Used |
|---|---|---|---|---|
| Ceph cluster | 6 × 2 TB NVMe (3 hosts × 2) | mammoth/whistler/zermatt | 11 TiB raw | 2.8 TiB (25.78%) |
| `zbackup` | ZFS mirror, 2 × 24 TB SATA | pve-ugreen | 21.8 TiB | 658 GB (3%) |
| QNAP main | RAID-? on 6 × 14 TB + 2 × 2 TB cache | qnas | 71.9 TiB | 31.5 TiB (44%) |

Ceph pools breakdown (`ceph df`):

| Pool | Used | Purpose |
|---|---|---|
| `ceph-shared` | 699 GiB | Shared RWX images (legacy docker-swarm era, still mounted as cephfs-swarm) |
| `ceph-swarm.meta` / `.data` | 484 MiB / 343 GiB | Old cephfs (was docker-swarm); now used as **rbd-backup staging** |
| `k3s-fs-metadata` / `k3s-fs-data` | 3.0 GiB / 1.7 TiB | Live CephFS PVCs for k3s |
| `k3s-rbd` | 129 GiB (68 GiB stored) | Live RBD PVCs for k3s |
| `kube-rbd` | 12 KiB | Empty (legacy, can be removed) |
| `.mgr` | 6.3 MiB | Ceph metadata |

---

## 2. Layer 1 — VM/LXC backups (vzdump → PBS → QNAP)

```
[every guest in cluster]                            [pve-mac VM]              [QNAP NFS]
       │                                                  │                          │
       │ vzdump @ 02:00 daily                             │                          │
       │ (configured in /etc/pve/jobs.cfg, runs on each   │                          │
       │  PVE node from cron via /etc/pve/vzdump.cron)    │                          │
       └─────────► snapshot mode, exclude=299 ───────────►│                          │
                                                          │ /etc/proxmox-backup/     │
                                                          │   datastore.cfg          │
                                                          │   path /mnt/pbs-backups  │
                                                          │   gc-schedule daily      │
                                                          ├─ NFS mount ─────────────►│
                                                          │   192.168.1.252:/proxmox/proxmox-backup-server
                                                          │                          │
                                                          │ Verify job v-e00654e0    │
                                                          │   schedule: monthly      │
                                                          │   ignore-verified: true  │
                                                          │                          │
                                                          │ Prune                    │
                                                          │   keep-last: 17          │
                                                          │   keep-daily: 7          │
                                                          │   keep-weekly: 8         │
                                                          │   keep-monthly: 2        │
```

### Configuration

- **Job source of truth**: `/etc/pve/jobs.cfg` (cluster-wide, lives on the corosync filesystem)
  ```
  vzdump: backup-f4e795e8-28be
      schedule 02:00
      all 1
      enabled 1
      exclude 299
      mode snapshot
      storage pbs-backups
  ```
- **vzdump.cron** (`/etc/pve/vzdump.cron`, symlinked from `/etc/cron.d/vzdump` on every PVE node) is **empty** — the new "Backup Job" UI writes to `jobs.cfg` and `pvescheduler.service` triggers it; the cron file is legacy.
- **PBS VM**: `pve-mac` qemu-server VMID **299**, 2 cores / 4 GB RAM, root disk on `shared-nfs` (so the PBS VM itself is on NFS — can be re-imported on any node).
- **PBS datastore** mounts `192.168.1.252:/proxmox/proxmox-backup-server` → `/mnt/pbs-backups`, used 32 TB / 41 TB free.
- **No `sync.cfg`**: PBS has no peer to push to. Cannot replicate to a remote PBS today even if one were stood up.
- **GC**: daily, datastore-level.
- **Verify**: monthly (`v-e00654e0-3168`, `ignore-verified: true`, `outdated-after: 30`).

### Where the data physically lands

`/share/CACHEDEV1_DATA/proxmox/proxmox-backup-server/{ct,vm,.chunks}/...` on the QNAP. **~2.0 TB total** (per NFS-side walk; the original audit's 301 GB number came from a QNAP-local `du` that under-reported).

---

## 3. Layer 2 — K8s app PVCs (volsync → Garage S3)

```
[K8s PVC, e.g. media/jellyfin]
        │
        │ ReplicationSource ${APP} (volsync.backube/v1alpha1)
        │   trigger.schedule: ${VOLSYNC_SCHEDULE:=05 00/12 * * *}   ── every 12h at :05
        │   restic.copyMethod: Snapshot
        │   restic.repository: ${APP}-volsync                       ── per-app Secret
        │   retain: { hourly: 24, daily: 7, weekly: 5, monthly: 3 }
        │
        ▼
[volsync mover Job]   creates CSI VolumeSnapshot → mounts as PVC clone
        │             + ${APP}-volsync cache PVC (10Gi on ceph-rbd)
        │
        ▼
   restic push to:  s3:https://<garage-endpoint>/<bucket>/${APP}-volsync
        │           encrypted with RESTIC_PASSWORD from volsync-garage-base
        │
        ▼
[Garage S3 — Docker container running natively on QNAP]
   ingress: garage.lab.mainertoo.com + garageui.lab.mainertoo.com → 192.168.90.180
   data on local QNAP filesystem: /share/CACHEDEV1_DATA/garage/{data,meta,config}
   ~168 GB on QNAP today
```

### Configuration

- **Component**: `components/volsync-v2/{,backup-only,bootstrap,restore}` (current). The doc `docs/volsync-storage-recovery.md` describes a future label-driven Kyverno-generated design that is **not yet implemented** — the current state uses these explicit Components per app.
- **Default schedule**: `05 00/12 * * *` (every 12 hours at :05). Can be overridden per app via `VOLSYNC_SCHEDULE` substitution.
- **Default retention** (per-app restic repo): 24 hourly, 7 daily, 5 weekly, 3 monthly (in `components/volsync-v2/backup/volsync-replicationsource.yaml`).
- **Master credential**: SOPS-encrypted Secret `volsync-garage-base` in `flux-system`. Per-app Secrets are derived via Flux `postBuild.substituteFrom`.
- **chart**: `oci://ghcr.io/home-operations/charts-mirror/volsync` tag `0.15.0`.
- **Live state (2026-05-07 audit)**:
  - 50+ ReplicationSources, all synced within 17 minutes of audit time.
  - Notable long-running: `crafty` 17m, `dumb` 2h51m, `jellyfin` 52m, `plex` 53m (the big media metadata DBs).
  - **`dawarich-db`** and **`authentik-postgresql`** are PostgreSQL volumes backed up via volsync raw-PVC. **This is crash-consistent, not app-consistent** — restoring may produce a corrupt database. CNPG with native backup is the proper solution.

---

## 4. Layer 3 — Ceph RBD nightly export (rbd → CephFS staging → Kopia)

```
[k3s-rbd Ceph pool]
   148 RBD images (csi-vol-XXX, csi-snap-XXX)
        │
        │ /usr/local/sbin/rbd-nightly-backup.sh on pve-ugreen
        │   cron: 30 1 * * * (01:30 daily)
        │
        ▼
For each image:
  1. mkdir /mnt/pve/cephfs-swarm/rbd-backup/<image>/
  2. find . -mindepth 1 -maxdepth 1 -exec rm -rf {} +     ← clears prior run
  3. rbd export k3s-rbd/<image> <image>-YYYY-MM-DD.img.tmp
  4. mv tmp → final
  5. emit <image>-YYYY-MM-DD.meta.txt    (namespace, PVC name, PV size,
                                           Flux git SHA, hostname, timestamp)
        │
        ▼
[CephFS k3s-fs (mounted on pve-ugreen at /mnt/pve/cephfs-swarm)]
   /mnt/pve/cephfs-swarm/rbd-backup/<image>/<image>-YYYY-MM-DD.img(+.meta.txt)
        │
        │ Kopia cron job at 02:45 picks up this directory
        │ (see Layer 4 below — rbd-backup is a subtree of /mnt/cephfs)
        ▼
[Kopia repo on /mnt/zbackup/kopia-repo]
   historical retention provided by Kopia (only "today" lives in CephFS)
```

### Notes

- The "wipe and re-export" pattern is intentional: ceph-fs holds only the most recent export, Kopia provides history via its repo.
- Script needs `rbd ls` / `rbd export` / `kubectl` / `jq`. Runs on pve-ugreen (which has Ceph keyring + kubeconfig at `/root/.kube/config`).
- **The `.meta.txt` file is one of two RBD-PV inventory mechanisms** (the other is `k3s-pv-index.sh` on each master, see Layer 6).
- **`POOL` is hardcoded to `k3s-rbd`** — `ceph-shared` (the legacy docker-swarm RBD pool, 699 GiB) is **not** exported. Mostly fine, since that pool's data is also accessible via cephfs-swarm and gets snapshotted that way, but worth flagging.
- The `KCTL` query for PV info uses `/root/.kube/config` on pve-ugreen — if that config rotates, the script silently continues with `unknown` metadata.

---

## 5. Layer 4 — CephFS Kopia snapshot (k3s + swarm filesystems)

```
[CephFS k3s-fs]   (live PVCs)              [CephFS ceph-swarm]   (legacy + rbd staging)
   192.168.99.11/12/13:/                      192.168.99.12/13/14:/
   mds_namespace=k3s-fs                       mds_namespace=ceph-swarm
        │                                            │
        │ kernel ceph mount (in kopia-lxc)           │ kernel ceph mount (pve-ugreen
        │ /mnt/cephfs-k3s                            │   → /mnt/pve/cephfs-swarm
        │                                            │   → bind-mounted into LXC at /mnt/cephfs)
        ▼                                            ▼
                          [kopia-lxc, LXC 111 on pve-ugreen]
                            cron 45 2 * * *  → kopia snapshot create /mnt/cephfs --parallel=4
                            cron 10 3 * * *  → kopia snapshot create /mnt/cephfs-k3s --parallel=4
                                                │
                                                ▼
                          /mnt/zbackup/kopia-repo  (filesystem repo, BLAKE2B-256 / AES256-GCM)
                            global retention: hourly 48, daily 7, weekly 4, monthly 12, annual 3
                            (from kopia policy show --global)
```

### State (2026-05-07)

| Source | Latest snapshot | Size | Retention buckets populated |
|---|---|---|---|
| `/mnt/cephfs` | 2026-05-07 02:45 UTC | 1.0 TB | `latest-1, daily-1, weekly-1, monthly-1, annual-1` (full chain back to 2025-11-30) |
| `/mnt/cephfs-k3s` | 2026-05-07 (running) | (running, 28 GB cached) | Confirmed daily, log shows ~28 GB cached + sub-GB delta per run |
| `/mnt/qnap_alldata` | per cron 03:30 | 27 TB last full | Daily, multi-week history in repo |

### Issues

- `/var/log/kopia-cephfs.log` is **76 MB**, no rotation. Same for `kopia-cephfs-k3s.log` and `kopia-qnap.log`.
- `/mnt/cephfs-k3s` snapshot logs **745+ "fatal errors" per run** from `volumes/_deleting/*` paths disappearing mid-walk — these are CephFS CSI tombstones being garbage-collected concurrent with the scan. The snapshot still completes; the errors are noise. **Fixed during this audit** with `kopia policy set --add-ignore "/volumes/_deleting/" /mnt/cephfs-k3s`.
- Kopia `0.22.3`. Web UI `kopia-server.service` running. Repo is **filesystem-backed**, not S3-backed.

---

## 6. Layer 5 — QNAP backup → zbackup

```
QNAP   /share/CACHEDEV1_DATA/   (top-level, 31 TB used)
  ├── QNAS/         ────────► exported as /QNAS via NFS ────► Kopia /mnt/qnap_alldata (29 TB)
  │     ├── data/        (29 TB — predominantly user data; "data" subtree)
  │     ├── backup/      (86 GB)
  │     ├── Computer Drive Backup/  (5.1 GB)
  │     ├── @Recycle/    (178 GB)
  │     └── temp/        (7.3 GB)
  │
  ├── proxmox/      ────────► /proxmox NFS ──► PBS (RW) + Kopia /mnt/qnap_pbs (RO) ──► zbackup ✅ F1
  │     └── proxmox-backup-server/{ct,vm,.chunks}/...   (~2.0 TB)
  │
  ├── garage/       ────────► (volume-root NFSv3 RO) ──► Kopia /mnt/qnap_garage (RO) ──► zbackup ✅ F2
  │     ├── data/   (Docker-on-QNAP Garage container's S3 object chunks)
  │     ├── meta/
  │     └── config/   (~168 GB)
  │
  ├── pve-ha/       ────────► /pve-ha NFS, used as `shared-nfs` PVE storage (52 KB — almost empty today)
  │
  └── Container/, appdata/, Public/, TimeMachine/, ...   (also NOT in Kopia)
```

### Cron

```
# kopia-lxc /var/spool/cron/crontabs/root
30 3 * * * /usr/bin/kopia snapshot create /mnt/qnap_alldata --parallel=8 >> /var/log/kopia-qnap.log 2>&1
```

This is a cleanly-running daily job — multi-month history visible in `kopia snapshot list /mnt/qnap_alldata`. It just doesn't cover `proxmox/`, `garage/`, `Container/`, `appdata/`, `Public/`, `TimeMachine/`.

---

## 7. Inventory mechanisms

Two parallel inventories, neither alone is sufficient — they cover different dimensions:

### 7a. `rbd-nightly-backup.sh` — `.meta.txt` files alongside images

- **Where**: pve-ugreen, runs `01:30` daily.
- **Output**: For every csi-vol image in pool `k3s-rbd`, writes `<image>-YYYY-MM-DD.meta.txt` next to the `.img` export inside `/mnt/pve/cephfs-swarm/rbd-backup/<image>/`.
- **Captured by**: Layer 4 Kopia snapshot of `/mnt/cephfs` at 02:45.
- **Contents**: namespace, PVC name, PV name, size, Flux git SHA at backup time, host, timestamp.
- **Purpose**: Lets you tell which app a given `csi-vol-XXX.img` belongs to during a from-scratch restore.

### 7b. `k3s-pv-index.sh` — CSV index inside each master VM

- **Where**: `/usr/local/sbin/k3s-pv-index.sh` on `mainertoo-k3s-master-{1,2,3}`.
- **Cron** (root):
  - master-1: `5 1 * * *`
  - master-2: `7 1 * * *`
  - master-3: `9 1 * * *`
- **Output**: 3 files in `/var/backups/` inside the master VM:
  - `rbd-pv-index-k3s.csv` — all RBD-driver PVs (host, node UUID, ns, pvc, pv, image-name, volume-name)
  - `cephfs-pv-index-k3s.csv` — all CephFS-driver PVs (subvolume name, fs name, group)
  - `pv-index-k3s.csv` — combined
- **Captured by**: Layer 1 vzdump of the master VM disks at 02:00 → PBS → QNAP.
- **Source kubeconfig**: `/etc/rancher/k3s/k3s.yaml` (so it works even if kubectl tooling on the host is replaced).

### Why both

`.meta.txt` is restored in the same Kopia run as the `.img` files (recovery is single-source). The CSV index is captured in a different chain (PBS) and gives a per-host, per-master view useful when only PBS is available.

---

## 8. Schedule timeline (24h view, all times America/Tijuana except where noted)

```
00:05 UTC ──── volsync ReplicationSource sweep (12-hourly, ${VOLSYNC_SCHEDULE} default)
01:05/07/09 ── k3s-pv-index.sh on master-1/2/3  → /var/backups/*.csv
01:30 ──────── rbd-nightly-backup.sh on pve-ugreen  → /mnt/pve/cephfs-swarm/rbd-backup/
02:00 ──────── PVE vzdump (jobs.cfg) all guests except 299  → PBS → QNAP
02:45 ──────── kopia snapshot /mnt/cephfs (incl. rbd-backup/)  → zbackup
03:00 ──────── /var/tmp/vzdumptmp* cleanup on pve-ugreen
03:10 ──────── kopia snapshot /mnt/cephfs-k3s  → zbackup
03:30 ──────── kopia snapshot /mnt/qnap_alldata  → zbackup
12:05 UTC ──── volsync ReplicationSource sweep (second daily run)
daily ──────── PBS GC (datastore: pbs-backups)
daily ──────── PBS prune (keep 17/7/8/2 last/daily/weekly/monthly)
monthly ────── PBS verify job v-e00654e0-3168 (ignore-verified=true)
```

Window from 01:30 to ~04:00 is the densest. RBD export → CephFS settle → Kopia all serialize cleanly because each consumer reads what the prior step wrote.

---

## 9. Critical findings

> All audit-day P0 findings (F1, F2) and the same-day follow-up (Container/appdata gap, rbd-backup rename, cephfs cleanup, ignore rules) closed on 2026-05-08. Detailed change-log lives in `docs/backup-system-wiki.md` §9. The findings remain documented below for context — and so future audits can verify the decisions still hold.

### Finding 1 — PBS data: secondary copy via Kopia ✅ implemented 2026-05-08

PBS writes **~2.0 TB** of vzdump backups to QNAP via NFS (the original audit estimate of 301 GB came from a QNAP-local `du` that under-reported; NFS walk shows 2.0 TB). The Kopia QNAP job mounts `qnas:/QNAS` (which is `/share/CACHEDEV1_DATA/QNAS`), **not** `/share/CACHEDEV1_DATA/proxmox`. Before fix: the single QNAP volume was the only copy.

**Fix applied** — see the wiki doc (`docs/backup-system-wiki.md`) §9 F1 for full details. Summary: NFSv4 RO mount on pve-ugreen + LXC mp4 RO bind + cron `45 4 * * * kopia snapshot create /mnt/qnap_pbs`. Initial 2 TB seed running 2026-05-08.

**Failure mode**: QNAP volume corruption, RAID failure across more disks than parity tolerates, NAS firmware bug, ransomware against the NFS share — any of these takes out every VM/LXC backup in one event.

**Fix shape (not yet applied)**:

1. On `pve-ugreen`: edit `/etc/pve/lxc/111.conf` to add a new mount point:
   ```
   mp4: /mnt/pve/proxmox-backup-server,mp=/mnt/qnap_pbs
   ```
   Where `/mnt/pve/proxmox-backup-server` is a new NFS mount on the host pointing at `192.168.1.252:/proxmox/proxmox-backup-server`. Add a corresponding `pvesm add nfs` entry or extend `/etc/fstab`.
2. Restart LXC 111.
3. Add cron line to kopia-lxc root crontab:
   ```
   45 4 * * * /usr/bin/kopia snapshot create /mnt/qnap_pbs --parallel=4 >> /var/log/kopia-pbs.log 2>&1
   ```
   The 04:45 slot is after the existing Kopia jobs and after PBS daily prune (which finishes early), so the snapshot captures a stable repo.
4. (Optional) Add a kopia ignore rule for `.chunks/.tmp` files if PBS leaves any during GC.

Note: PBS uses a chunk-based, content-addressed store. Snapshotting it with Kopia (also content-addressed) is correct in principle but somewhat redundant — Kopia will re-hash chunks. Acceptable cost for the safety it buys.

### Finding 2 — Garage S3: secondary copy via Kopia ✅ implemented 2026-05-08

**Architecture correction from original audit**: Garage runs as a Docker container natively on the QNAP (not in K8s as the original audit guessed). Ingresses `garage.lab.mainertoo.com` and `garageui.lab.mainertoo.com` → `192.168.90.180`. Data lives on QNAP filesystem at `/share/CACHEDEV1_DATA/garage/{data,meta,config}` (~168 GB).

**Fix applied** — see the wiki doc (`docs/backup-system-wiki.md`) §9 F2 for full details. Summary: NFSv3 RO mount of QNAP volume root on pve-ugreen + LXC mp5 binds only the `garage/` subdir RO + cron `30 5 * * * kopia snapshot create /mnt/qnap_garage`. Initial 168 GB seed running 2026-05-08.

**Fix shape**:

1. Same pattern as above: NFS-mount `qnas:/share/CACHEDEV1_DATA/garage` (or a child path) on pve-ugreen, mp into kopia-lxc.
2. Cron `30 4 * * *` on kopia-lxc.
3. Important caveat: Garage data is partial-byte-changing object chunks; backing it up while Garage is writing can produce a snapshot-internally-inconsistent state. Two ways to handle:
   - **Acceptable risk**: Garage uses content-addressed chunks too; an inconsistent snapshot is an inconsistent set of chunks, restic on top of restic-backed-up-chunks should still be recoverable in nearly all cases. Run weekly during a quieter window.
   - **Cleaner**: configure Garage's built-in S3-replication to a second bucket (or `rclone sync`) on a real off-cluster target, before falling back to Kopia.

I recommend documenting this gap and fixing it the *clean* way (rclone or Garage replication) — see §11 (offsite DR), since the same target solves both this and the offsite gap.

### Finding 3 — Single failure domain on QNAP — partially mitigated by F1+F2

PBS, Garage S3, the K8s NFS-CSI provisioner targets, the `pve-ha` shared-storage VM disks, and the Kopia QNAS source are all on the same QNAP. **After F1+F2 (2026-05-08)**: PBS and Garage data now escape to zbackup. Remaining single-point: K8s NFS-CSI provisioner targets and `pve-ha` shared storage (small/empty today). Full mitigation needs the offsite target in §11.

### Finding 4 — Two PostgreSQL databases backed up via volsync raw-PVC *(severity: MEDIUM)*

`dawarich-db` and `authentik-postgresql` both use volsync ReplicationSource directly on a live Postgres data dir. This is crash-consistent, not app-consistent. If Postgres is mid-write during the snapshot, restore can produce a corrupted DB requiring `pg_resetwal` or worse.

**Fix shape**: migrate both to CNPG with native logical/physical backup to S3, then add a CNPG-aware retention policy. This is a separate project — flag for the next session.

### Finding 5 — No PBS sync target; no offsite copy of any layer *(severity: MEDIUM)*

`/etc/proxmox-backup/sync.cfg` is empty. PBS cannot push to a remote PBS even if one were stood up. Combined with Findings 1–3, the backup graph terminates at zbackup (in the same physical room, same power circuit, same admin credentials as everything else).

See §11 (offsite DR comparison).

### Finding 6 — PBS prune keeps only 2 monthly snapshots *(severity: LOW)*

Daily 7 / weekly 8 is fine. Monthly 2 means a problem detected ≥3 months after introduction has no clean rollback. Recommend `keep-monthly: 6` (covers any "did we change something six months ago" scenario at low storage cost).

### Finding 7 — Kopia logs unrotated *(severity: LOW)*

`/var/log/kopia-cephfs.log` is 76 MB. No `/etc/logrotate.d/kopia` entry. Will eventually fill the LXC root.

**Fix**: drop a logrotate file on kopia-lxc:
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

### Finding 8 — No explicit Kopia maintenance cron *(severity: LOW)*

Kopia 0.22 runs automatic maintenance per the repo metadata. Belt-and-braces: `0 5 * * 0 /usr/bin/kopia maintenance run --full --safety=full >> /var/log/kopia-maint.log 2>&1` weekly on Sunday.

### Finding 9 — `kube-rbd` Ceph pool empty *(severity: trivial)*

Legacy pool. Removable. Does not affect backups.

### Finding 10 — `mp2` from the kopia LXC config (`/mnt/pve/cephfs-k3s`) is not visible on the host *(severity: trivial)*

Investigation showed the LXC mounts CephFS directly via the kernel ceph client (not through the host bind-mount), so the host's missing `/mnt/pve/cephfs-k3s` is harmless. The LXC works because it's privileged.

---

## 10. Recommendations (prioritized)

| Priority | Action | Effort | Buys you |
|---|---|---|---|
| ~~P0~~ ✅ | ~~Mirror PBS dir into Kopia (Finding 1)~~ — **done 2026-05-08** | 30 min | A second copy of every VM/LXC backup |
| ~~P0~~ ✅ | ~~Mirror Garage dir into Kopia (Finding 2)~~ — **done 2026-05-08** | 1 hr | A second copy of every volsync repo |
| ~~P0~~ ✅ | ~~Mirror QNAP Container/appdata into Kopia~~ — **done 2026-05-08** | 30 min | Garage container + QNAP service state recoverable |
| ~~P1~~ ✅ | ~~Rename Layer 3 source `/mnt/cephfs` → `/mnt/rbd-backup`~~ — **done 2026-05-08** | 15 min | Source name matches purpose; cleaner UI |
| ~~P1~~ ✅ | ~~Clean up legacy ~122 GB on cephfs-swarm pool~~ — **done 2026-05-08** | passive | Stop daily-snapshotting docker-swarm corpse |
| ~~P1~~ ✅ | ~~Add `/volumes/_deleting/` ignore on cephfs-k3s~~ — **done 2026-05-08** | 1 min | Silence ~750 fatal-error log lines per run |
| P0 | Stand up an offsite target (see §11) — *waiting on offsite NAS hardware* | 1–8 hr depending on choice | Off-site copy = real disaster recovery |
| P1 | Bump PBS `keep-monthly` to 6 | 1 min | Longer window to detect data-corruption regressions |
| P1 | Add logrotate for `/var/log/kopia-*.log` | 5 min | Prevent kopia-lxc / filling |
| P1 | Add weekly `kopia maintenance run --full` cron | 5 min | Repo health, faster restores |
| P1 | Add monthly `kopia snapshot verify --verify-files-percent=1` cron (per source) | 10 min | Catch repo corruption before you need it |
| P1 | Test-restore a VM and a PVC quarterly (calendar reminder) | 30 min/quarter | Backup is unproven until restored |
| P2 | Migrate `dawarich-db` and `authentik-postgresql` to CNPG | 2–4 hr each | App-consistent DB backups |
| P2 | Document this audit's findings in CLAUDE.md so future sessions don't re-discover | 5 min | Continuity |
| P3 | Add a Garage SnapshotClass or per-bucket replication to make Garage HA | half-day | Resilience inside cluster |
| P3 | Drop legacy `kube-rbd` Ceph pool | 5 min | Cleanup |

---

## 11. Offsite DR — three options compared

You don't have offsite today. Below: side-by-side of the three serious paths.

### Option A — Garage S3 mirror (Garage → Garage)

Run a second Garage instance at the offsite NAS. Configure Garage native bucket-level replication.

| | |
|---|---|
| **What it backs up** | Volsync (Layer 2) only — does not solve PBS/Kopia layers |
| **Bandwidth efficiency** | High — Garage replicates only changed object chunks |
| **Compression** | Restic already compresses; Garage just stores chunks |
| **Encryption** | Restic data is end-to-end encrypted via `RESTIC_PASSWORD` (zero-knowledge for the offsite host) |
| **Operational complexity** | Medium-high — second Garage cluster, keys, network rules |
| **Recovery from offsite** | Point volsync at the offsite Garage endpoint and restore normally. Restic doesn't care which instance serves the chunks. |
| **Costs you a hop** | No — direct from Garage in cluster |
| **Failure mode** | If volsync is broken, this won't help. If Garage *itself* is corrupt, this dutifully replicates the corruption. |
| **Best when** | Volsync is your most-restored layer (true here) AND you're comfortable running two Garages |

### Option B — Ceph RGW S3 mirror (in-house RGW + offsite)

Stand up RGW (Ceph's S3 gateway) inside the existing Ceph cluster, point a backup tool (rclone / restic) at it, and replicate the bucket offsite.

| | |
|---|---|
| **What it backs up** | Whatever you point at it — but you have to point things at it explicitly |
| **Operational complexity** | High — RGW requires a realm, zonegroup, zone setup; replication is "multisite" config |
| **Wins** | If you ever wanted to phase out Garage, RGW is the natural successor; lives in a system you already operate |
| **Losses** | Adds a new service in the same blast radius (Ceph). Doesn't solve PBS/Kopia layers either. Operational debt ≥ Garage already. |
| **Best when** | You're already deep in Ceph multisite, or you want to consolidate volsync onto RGW |

### Option C — Kopia over SSH/SFTP to offsite NAS

Add the offsite NAS as a second Kopia repository (Kopia supports SFTP repos natively, or filesystem over SSHFS). Run `kopia snapshot create` against the same sources, *or* `kopia repo sync-to` from local zbackup.

| | |
|---|---|
| **What it backs up** | Layers 3+4+5 (everything Kopia already does), AND if you mp the PBS+Garage dirs (Findings 1+2) it covers Layers 1+2 too. |
| **Bandwidth efficiency** | Highest — Kopia's content-addressed repo with `repo sync-to` ships only new content, end-to-end encrypted, deduplicated |
| **Compression** | Yes (zstd / s2 in 0.22) |
| **Encryption** | AES-256 with your password — offsite host can be untrusted |
| **Operational complexity** | Lowest — one extra `kopia repo sync-to` cron, one SSH key, one SFTP user on offsite |
| **Recovery from offsite** | `kopia repo connect sftp ...` from anywhere with the password; restore as normal. Or pull the repo back to zbackup first. |
| **Failure mode** | Same compromise considerations as A — if the local Kopia repo is corrupted, sync-to ships the corruption. Mitigated by `kopia maintenance run --full --safety=full` weekly + monthly verify. |
| **Best when** | Default for "simplest 3-2-1 in a homelab". Kopia already exists here; this is the lowest-marginal-cost option. |

### Recommendation

**Option C, with caveats**:

1. Add Findings 1+2 fixes first (mp PBS and Garage into kopia-lxc). After this, all 5 layers funnel into the same Kopia repo on zbackup. Once that's true, you have **one thing to ship offsite**.
2. Use `kopia repository sync-to` (Kopia repo→repo replication) rather than re-snapshotting to a remote repo. Repo→repo is delta-only and uses no source-side I/O.
3. Target: any cheap NAS at a friend/parent/colo running SSHFS or SFTP. 6 TB external HDD on a Raspberry Pi suffices.
4. Tailscale for the transport. Keys live in `~/.ssh` on kopia-lxc.
5. Monthly: `kopia repository verify` against the offsite repo. This is what proves you have a real second copy.

Add a comparison decision later if the offsite host turns out to be S3-shaped (B2, Wasabi, R2): swap "Kopia SFTP repo" → "Kopia S3 repo" — same shape, same shipping pattern.

---

## 12. Remaining open questions (for a future session)

- Should `ceph-shared` (the legacy 699 GB cephfs-swarm pool) be migrated off and the pool deleted? It's not part of any active app, but data still lives there.
- Is `pve-ha` storage on QNAP still used? It shows 52 KB. May be deletable.
- Should we move PBS off `shared-nfs` (current root disk location) onto Ceph? Currently if QNAP dies, PBS itself is unrecoverable — though new PBS can be reinstalled and pointed at the (now unreadable) datastore. Lower priority than offsite.
- Should the `.meta.txt` and CSV-index inventory mechanisms be unified into a single tool? They overlap and might diverge over time.
