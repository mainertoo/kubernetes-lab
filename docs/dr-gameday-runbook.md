# DR Game-Day Runbook — full cold-swap restore drill

> **Purpose.** Prove, end-to-end, that the homelab can be rebuilt from nothing —
> Terraform → Ansible → Flux → VolSync/Kopia PVC restore → CNPG restore — and
> measure the real recovery time. A backup you have never restored is a
> hypothesis; whole-cluster DR stays a hypothesis until you stand the whole
> cluster back up from cold. This is that test.
>
> **Companion docs (read alongside):** [`backup-recovery.md`](backup-recovery.md)
> §4 (the cluster-nuke rebuild sequence this drill exercises), §1b/§1 (CNPG +
> label-driven PVC restore), [`cnpg-disaster-recovery.md`](cnpg-disaster-recovery.md)
> §2 (fleet CNPG restore), [`backup-architecture.md`](backup-architecture.md) §8b
> (off-cluster secret prerequisites), [`ha-architecture.md`](ha-architecture.md),
> [`label-driven-backups.md`](label-driven-backups.md),
> [`volsync-kopia-oracle-prefix-mismatch.md`](volsync-kopia-oracle-prefix-mismatch.md)
> (the restore-oracle fix this drill is the ultimate validation of).

---

## 0. The model — cold-swap, parallel, restore-only

This homelab does **not** have spare node hardware to run a second full cluster
beside production. So the drill **borrows** production's hardware for a few hours:

```
  ┌─ before ──────────────┐   ┌─ during gameday ───────────┐   ┌─ after ──────────────┐
  │ prod+staging (9 VMs)  │   │ 9 VMs POWERED OFF (frozen) │   │ 9 VMs powered back on │
  │ running               │ → │ gameday cluster runs on    │ → │ auto-recover from     │
  │ data on Ceph          │   │ the freed compute,         │   │ INTACT Ceph volumes;  │
  │                       │   │ ISOLATED + RESTORE-ONLY,   │   │ gameday destroyed     │
  │                       │   │ restoring from backups     │   │                       │
  └───────────────────────┘   └────────────────────────────┘   └───────────────────────┘
```

**Two facts make this safe:**

1. **The 9 K3s VMs are only powered *off*, never destroyed.** Powering them back
   on returns production to its exact prior state.
2. **The data does not live in the VMs.** It lives on the **external Ceph
   cluster** (`192.168.99.12-14`, on the Proxmox hosts), which keeps running the
   whole time — as do **Garage S3** and **PBS** (both on the QNAP / UGREEN,
   independent of the K3s VMs). So the gameday cluster has live backups to
   restore *from*, and production's real volumes are sitting untouched the whole
   time.

**Consequence — the single most important rule of this drill:** you do **not**
restore gameday data back into production. Production comes back by itself from
its intact Ceph volumes. The only direction of risk is the *reverse* — the
gameday cluster writing into the **shared** backup repo / WAL archive and
polluting production's history. The entire isolation section (§2) exists to
prevent that. Run the gameday **restore-only**.

**RTO expectation.** The "10-minute restore" figure is just the **PVC
auto-restore phase** (oracle → populator). The *full* cold-rebuild RTO —
Terraform + Ansible + Flux bootstrap + reconcile + VolSync restore + CNPG
restore — is realistically **a few hours**. Measuring that number is a primary
output of this drill.

---

## 1. Success criteria

The drill passes when, on the gameday cluster:

- [ ] All Flux Kustomizations reconcile Ready (minus the deliberately-suspended
      external-publishing ones — see §2).
- [ ] Every backed-up PVC binds **populated** (not empty) via the label-driven
      populator.
- [ ] All 8 CNPG databases recover and report expected row counts.
- [ ] A representative **data spot-check** passes for a sample across storage
      classes and engines (e.g. Vaultwarden entries present, Plex library
      visible, a CNPG table row-count matches a known value, a CephFS RWX app's
      files present).
- [ ] **RTO recorded** (clock: start = begin `terraform apply`; stop = last
      sampled app green + data verified).

Then production swaps back cleanly (§9) and the gameday is destroyed (§10) with
**no residue** in the shared backup repo, WAL archive, Ceph, or tailnet.

---

## 2. Isolation landmines — MUST handle before you start

Because production is *off*, the gameday can reuse external identities without a
live collision — but the **shared persistence layers stay live**, and that is
where the danger is.

| # | Shared thing | Risk if ignored | Mitigation |
|---|---|---|---|
| 1 | **Ceph capacity** (`192.168.99.x`, stays up) | Restore provisions a *second* full set of RBD/CephFS volumes **alongside** production's still-allocated ones → ~2× used capacity → `ceph df` exhaustion mid-restore. **This is usually the binding constraint.** | Pre-check `ceph df` headroom (§3). If tight, restore a **representative subset** of apps, not all ~70. |
| 2 | **Kopia backup repo** (Garage, stays up) | Gameday movers back up with the **same source identities** (`hostname=<ns>`, `username=<pvc>-backup`) into the same repo → pollutes production's restore history. A second `KopiaMaintenance` could GC it. | **Restore-only:** do not let backups run. Suspend the backup ClusterPolicy / scale `volsync-system-volsync` to 0 after restore; suspend `KopiaMaintenance`. (Restore *reads* are safe.) See [[the oracle fix]] context — restore now depends on movers+oracle agreeing on the bucket root. |
| 3 | **CNPG WAL archive** (Garage, stays up) | Gameday CNPG archiving WAL to the same object-store path forks production's timeline → breaks future PITR. | Recover via `bootstrap.recovery` (read-only) and **suspend `ScheduledBackup`** on the gameday clusters; don't let them archive. |
| 4 | **Network identity** | If the gameday claims prod's node IPs / VIP / MetalLB pool and a prod VM later powers on, they collide; worse, **cloudflared/newt/tailscale would publish the *gameday* cluster at production's public URLs**. | Use a **separate VMID + IP + VIP + MetalLB range** (§3). **Suspend external publishers** on the gameday (`cloudflared`, `newt`, `tailscale-operator`, any external-DNS) and verify **internally** (kubectl / port-forward), not via `*.lab` / `*.mainertoo.com`. |
| 5 | **Let's Encrypt rate limits** | Re-issuing every wildcard cert from a throwaway cluster can hit LE limits. | Don't depend on fresh TLS — verify over `port-forward`/HTTP. (cert-manager certs are not part of the success criteria.) |
| 6 | **sops-age key** | Flux can't decrypt SOPS secrets without it → nothing reconciles. | Stage `~/.config/sops/age/keys.txt` from 1Password **before** the window (§3). |

What you do **not** need to worry about: Garage, PBS, and the Ceph cluster all
run outside the K3s VMs and stay up — they are your restore source.

---

## 3. Pre-flight (do days before, while production is healthy)

1. **Stage off-cluster secrets** (1Password) per
   [`backup-architecture.md` §8b](backup-architecture.md): `~/.ssh/id_ed25519_k3s`,
   `~/.config/sops/age/keys.txt`. Note: the gameday uses its **own** Terraform
   state (next step), so you do **not** need production's tfstate — and must not
   reuse it.

2. **Ceph capacity pre-check** (the likely limiter):
   ```bash
   $ ssh pve-mammoth 'ceph df'           # need headroom ≈ used space of the apps you'll restore
   $ ssh pve-mammoth 'ceph osd pool ls detail | grep -E "k3s-rbd|k3s-fs"'
   ```
   If free space < used, plan to restore a **subset** (set a smaller
   `apps/<gameday>/kustomization.yaml`, or restore-skip the big-data apps).

3. **Create the gameday Terraform environment** — a **separate state** so a stray
   `apply` can never touch production's VMs (the cardinal risk —
   [[feedback_bpg_proxmox_cascade_replacement]]):
   - New dir `terraform/environments/gameday/` (copy of `production/`) with its
     **own** `provider.tf` backend / `terraform.tfstate`, reusing
     `modules/k3s-cluster`.
   - **Distinct, non-colliding values** (all free in `192.168.90.0/24`; prod uses
     `.160-.166/.180-.199`, staging `.167-.169/.200-.219`):

     | Setting | Production | **Gameday** |
     |---|---|---|
     | master VMIDs | 661-663 | **691-693** |
     | worker VMIDs | 664-666 | **694-696** |
     | master IPs | .161-.163 | **.171-.173** |
     | worker IPs | .164-.166 | **.174-.176** |
     | kube-vip VIP | .160 | **.170** |
     | MetalLB pool | .180-.199 | **.220-.239** |

   - A matching **gameday Ansible inventory** (`inventory/gameday/`) with those
     IPs + `kube_vip_ip: 192.168.90.170` + `metallb_pool_range:
     192.168.90.220-192.168.90.239`.

4. **Prepare restore-only / isolation up front.** Cleanest is a **`dr/gameday`
   git branch** whose `clusters/<gameday>` entrypoint:
   - drops/suspends the backup `KopiaMaintenance` + leaves the Kyverno backup
     policy in place but plans to scale `volsync` to 0 post-restore,
   - removes `cloudflared`, `newt`, `tailscale-operator`, external-DNS from the
     controllers list,
   - points CNPG apps at `cnpg-cluster/recovery` and suspends `ScheduledBackup`.

   If you skip the branch, keep a written **suspend-list** to run by hand
   immediately after bootstrap (§6).

5. **Prove the rollback path independently.** Your rollback *is* "power the 9 VMs
   back on and etcd quorum reforms." Before betting the gameday on it, do a
   standalone **graceful full-cluster power-cycle** of production once (off-hours):
   `qm shutdown` all 9 → confirm stopped → `qm start` all 9 → confirm
   `flux get all -A` healthy. If a node was shut down ungracefully and etcd needs
   manual recovery, you want to learn that **now**, not during the swap-back.

6. **Schedule the window**, announce downtime (a few hours), and have console
   access to the Proxmox hosts.

---

## 4. Phase A — quiesce & preserve production

1. **Belt-and-suspenders snapshot** (optional but recommended): PBS-backup or
   take a Proxmox snapshot of the 9 VMs so even a corrupted shutdown is
   recoverable independent of Ceph.
2. **Graceful shutdown** (lets etcd flush cleanly):
   ```bash
   # On each PVE host, ACPI shutdown — do NOT destroy
   pve-X# for id in 661 662 663 664 665 666 671 672 673; do qm shutdown $id; done
   pve-X# qm list | grep -E '661|662|663|664|665|666|671|672|673'   # confirm 'stopped'
   ```
3. **Start the RTO clock** when you begin Phase B.

> ⚠️ Never `qm destroy` these. The whole rollback depends on them being intact.

---

## 5. Phase B — provision the gameday cluster

Separate state = cannot touch production's VMs.

```bash
$ cd terraform/environments/gameday
$ terraform init
$ terraform plan      # CONFIRM: only creates VMIDs 691-696, touches nothing in 661-673
$ terraform apply
```

Install K3s + HA networking with the **gameday** inventory:

```bash
$ ansible-playbook -i ansible/k3s-cluster/inventory/gameday/dynamic_terraform_inventory.sh \
    ansible/k3s-cluster/playbooks/k3s_install.yml
# kube-vip (.170) + MetalLB (.220-.239) per the gameday group_vars
```

---

## 6. Phase C — bootstrap Flux (restore-only)

```bash
k8s$ kubectl create ns flux-system
k8s$ kubectl -n flux-system create secret generic sops-age \
       --from-file=age.agekey=$HOME/.config/sops/age/keys.txt
k8s$ flux bootstrap github --owner=mainertoo --repository=kubernetes-lab \
       --branch=dr/gameday --path=./clusters/gameday        # or master + manual suspend
```

If you bootstrapped `master` instead of a `dr/gameday` branch, **immediately**
apply the isolation suspend-list (landmines §2):

```bash
# stop external publishers from serving the gameday at prod URLs
k8s$ flux suspend kustomization <cloudflared> <newt> <tailscale> <external-dns>
# stop backup writes to the shared repo (do AFTER restore completes — see §8)
k8s$ kubectl -n volsync-system scale deploy volsync-system-volsync --replicas=0
k8s$ kubectl -n volsync-system delete kopiamaintenance volsync-kopia-shared --ignore-not-found
# stop CNPG WAL archiving
k8s$ kubectl get scheduledbackup -A -o name | xargs -I{} kubectl patch {} --type merge -p '{"spec":{"suspend":true}}'
```

---

## 7. Phase D — restore data

This is the standard cluster-nuke restore — follow
[`backup-recovery.md` §4](backup-recovery.md) steps 4-7, summarized:

- **PVCs auto-restore.** As Flux reconciles `apps/`, each PVC carrying
  `backup: daily|hourly` + `backup-engine: kopia` is created fresh; Kyverno
  injects `dataSourceRef → ReplicationDestination/<pvc>-backup` and the volume
  populator pulls each PVC's latest Kopia snapshot from the **bucket-root** repo.
  *(This is exactly the chain validated in the oracle-prefix fix — the drill
  re-proves it at full scale.)*
  ```bash
  k8s$ kubectl get replicationdestination -A
  k8s$ kubectl get pvc -A | grep -v Bound      # should drain to empty as populators finish
  ```
- **CNPG (8 dbs):** flip each app's `db-cnpg.yaml` `components:` from
  `cnpg-cluster` → `cnpg-cluster/recovery` (or use the `dr/gameday` branch where
  it's pre-flipped). Full procedure: [`cnpg-disaster-recovery.md` §2](cnpg-disaster-recovery.md).

Once PVC populators have finished, enforce **restore-only** (scale `volsync` to
0) so no gameday backup is written.

---

## 8. Phase E — verify & measure

Verify **internally** (no external DNS / TLS dependency):

```bash
k8s$ flux get all -A --no-header | awk '$4=="False"'      # only suspended ones should show
k8s$ kubectl get pods -A | grep -vE 'Running|Completed'
# Data spot-checks (representative sample):
k8s$ kubectl -n vaultwarden exec deploy/vaultwarden -- sqlite3 /data/db.sqlite3 'select count(*) from ciphers;'
k8s$ kubectl -n media port-forward svc/plex 32400 & curl -s localhost:32400/library/sections | head
k8s$ kubectl -n <cnpg-ns> exec <cluster>-1 -- psql -tAc 'select count(*) from <known_table>;'
k8s$ # a CephFS RWX app: exec in and ls the shared mount
```

**Record the RTO** (start of `terraform apply` → last sampled app green + data
verified) and any issues found, in the log table (§12).

---

## 9. Phase F — swap back to production

```bash
# 1. Power the gameday cluster OFF
pve-X# for id in 691 692 693 694 695 696; do qm stop $id; done
# 2. Power production + staging back ON
pve-X# for id in 661 662 663 664 665 666 671 672 673; do qm start $id; done
# 3. Verify production recovered (it re-attaches its INTACT Ceph volumes)
k8s$ kubectl config use-context production
k8s$ flux get all -A --no-header | awk '$4=="False"'
k8s$ kubectl get pods -A | grep -vE 'Running|Completed'
k8s$ # spot-check 2-3 real apps
```

> **Do NOT** restore gameday backups into production. Production's data was frozen
> intact on Ceph and returns automatically; and with restore-only enforced, the
> gameday wrote nothing to the shared repo anyway.

---

## 10. Phase G — teardown & cleanup

1. **Destroy the gameday cluster** (separate state — cannot touch prod):
   ```bash
   $ cd terraform/environments/gameday && terraform destroy
   ```
2. **Reclaim Ceph.** Destroying the VMs out from under Flux skips PVC pruning, so
   the RBD images / CephFS subvolumes the gameday created during the window may
   be orphaned. Identify and remove them (they're the csi-vols created during the
   window — cross-check against production's live set, which is *not* in this
   list):
   ```bash
   pve-X# rbd ls k3s-rbd | wc -l        # should return to the pre-drill count
   pve-X# ceph fs subvolume ls k3s-fs csi
   # rbd rm / ceph fs subvolume rm only the gameday-created ones (confirm via creation time / not in prod PV list)
   ```
   Then confirm `ceph df` returns to the pre-drill baseline.
3. **Confirm no shared-repo residue** (restore-only should mean none):
   ```bash
   # transient inspect pod (see oracle-prefix doc §6): no gameday snapshots, no forked CNPG timeline
   ```
4. **Tailnet / external:** remove any gameday tailscale nodes or external
   registrations that leaked despite suspension.
5. **VolSync alert hygiene** on production: if any `VolSyncKopiaRepoDisconnected`
   lingers, `kubectl -n volsync-system rollout restart deploy volsync-system-volsync`
   ([[feedback_decommission_backed_up_app_orphans_volsync]]).

---

## 11. Abort / rollback (any phase)

At **any** point — provisioning fails, restore stalls, you run out of Ceph,
anything — the rollback is identical and fast:

```bash
pve-X# for id in 691 692 693 694 695 696; do qm stop $id; done      # kill gameday
pve-X# for id in 661 662 663 664 665 666 671 672 673; do qm start $id; done  # bring prod back
```

Because production was only powered off, recovery is immediate. Then teardown the
gameday (§10) at your leisure. There is no state in which production cannot be
brought straight back.

---

## 12. Cadence & RTO log

Run **quarterly** (and after any change to the provisioning/restore chain). The
only test of a backup is a restore; the only test of DR is a rebuild.

| Date | Scope (full / subset) | Measured RTO | Issues found | Follow-up |
|---|---|---|---|---|
| _e.g. 2026-0Q_ | | | | |

> This drill is also the **ultimate end-to-end validation of the label-driven
> restore chain** (VolSync movers ↔ Kopia bucket-root ↔ pvc-plumber oracle ↔
> Kyverno populator). If gameday apps come up *with their data*, that chain is
> proven at full scale — which no single-PVC test can claim.
