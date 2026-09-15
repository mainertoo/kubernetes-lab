# ansible/fleet — OS patching for the whole homelab

These playbooks patch everything outside Flux: Proxmox hosts, the K3s VMs, LXC containers and standalone VMs. They're built for a monthly run, and every host-specific trap is a check the playbooks run.

Run everything from this directory, so `ansible.cfg` and the inventory get picked up:

```bash
cd ansible/fleet
```

## Playbooks

| Playbook | What it does | Reboots |
|---|---|---|
| `fleet_status.yml` | Read-only report for every host: OS, kernel, uptime, last upgrade, pending/security/kernel packages, reboot needed, package-list age, kernel pin | none |
| `lxc_patch.yml` | `apt dist-upgrade` inside each container via `pct exec`, then a health check (DNS on TCP 53, tailscaled `Running`, kopia server). Secondaries go before primaries. Skips kopia-lxc while a snapshot is running. | none |
| `vm_patch.yml` | Packages on `pbs` (over SSH, waits for PBS tasks to finish) and `zwave-js` (through the QEMU guest agent). `spark` only runs with `--limit spark -K`. | none; their host reboot restarts them |
| `k3s_os_upgrade.yml -e target=k3s_staging\|k3s_production` | Rolling, one node at a time. Detailed below. | each node once |
| `pve_host_patch.yml` | Rolling, one host at a time. Detailed below. | each host once |
| `pve_kernel_cleanup.yml [-e kernel_cleanup_apply=true]` | Purges Proxmox kernel/header packages that `proxmox-boot-tool` wouldn't boot. Keeps the running, pinned/manual and automatically selected kernels. Aborts if apt would remove anything else. Report-only without the flag. `pve_host_patch.yml` runs it first. | none |

**`k3s_os_upgrade.yml`** runs these steps on each node:
1. Skip the node if it's already on the target release, on its newest kernel and schedulable (re-running = resuming).
2. Wait for vzdump on its Proxmox host; gate on the cluster.
3. Take an etcd snapshot (server nodes only).
4. Cordon, then drain with `--disable-eviction --skip-wait-for-delete-timeout=120`.
5. Run `apt full-upgrade`, then `do-release-upgrade` if the node is older than `target_version` (26.04).
6. GPU nodes: `i915-sriov-dkms` must be built for the kernel that will boot.
7. Reboot.
8. Verify the release, the k3s service, the `rbd`/`ceph`/`nfs` kernel modules, the `lan0` address and the render node.
9. Wait for Ready, uncordon, wait for pods to settle, soak.

**`pve_host_patch.yml`** runs these steps on each host:
1. **Gate:** Proxmox quorate, Ceph healthy (only `AUTH_INSECURE_*` ignored) with all PGs `active+clean`, all K3s nodes Ready, vzdump idle.
2. **Kernel cleanup:** purge kernels that won't boot, so DKMS and initramfs only build for real ones (`-e kernel_cleanup=false` to skip).
3. **GPU driver (before apt):** hosts with `i915_sriov_dkms_version` move to that release first, with rollback. A new kernel's DKMS hook fails on a driver that can't build for it.
4. **Prep:** `dpkg --configure -a`, `apt dist-upgrade`, drain the host's K3s nodes, set Ceph `noout`/`norebalance`, unpin the kernel, confirm a ZFS module and every DKMS module exist for the boot kernel.
5. **Reboot:** wait for busy jobs, shut down the K3s VMs, reboot.
6. **Verify:** expected kernel, VF count and host i915 version, host-specific checks, NFS storages responsive, quorum, this host's mon/OSD/MDS back.
7. **Finish:** unset the flags, wait for health, check the worker VM's GPU, uncordon one node at a time, check `gpu.intel.com/i915` is advertised, soak.

## Recommended order

1. `fleet_status.yml` — baseline.
2. `lxc_patch.yml` and `vm_patch.yml` — low risk; can run alongside step 3.
3. `k3s_os_upgrade.yml -e target=k3s_staging` — canary for release upgrades.
4. `k3s_os_upgrade.yml -e target=k3s_production` — workers first, then masters.
5. **QNAP firmware, if pending** — shut down the PBS VM first. A NAS firmware update wedges every NFSv4 client (hosts and K3s VMs) until reboot, so do it *before* the host phase and put any wedged host first.
6. `pve_host_patch.yml` — Ceph order: mon/OSD hosts mammoth → zermatt → whistler, then MDS-only mac → s13, then client-only ugreen. It purges unused kernels first, because DKMS builds a new driver for every kernel with headers (~100 min on the N100 hosts with ~25 kernels).
7. `fleet_status.yml` — confirm. Also check `ceph mgr services` (the dashboard Endpoints must name the active mgr), DNS on `.50`/`.53`, and zwave-js-ui after any s13 reboot.

Everything stops at the first failure (`any_errors_fatal`). A failed node or host is **left cordoned, with Ceph flags still set**, so you can inspect it. Fix it, then re-run with `--limit <host>`. The playbooks are safe to re-run: a node already on the target release skips the release upgrade.

## iGPU SR-IOV driver

Hosts that expose Iris Xe / Alder Lake-N VFs (the MS-01s, s13, ugreen) and the production workers run the out-of-tree **i915-sriov-dkms**, pinned to one version on both sides: `i915_sriov_dkms_version` here and in `ansible/k3s-cluster/inventory/*/group_vars/all.yml`. Before bumping it, check the release's `BUILD_EXCLUSIVE_KERNEL` covers every PVE and Ubuntu kernel that will boot. `dkms status <module>` ignores its filter in DKMS 3.2.2, so always parse `dkms status | grep '^<module>/'`.

## Host-specific checks encoded here

| Host | Check | Why |
|---|---|---|
| all PVE | ZFS module present for the kernel that will boot | No module means no `zbackup` pool on ugreen, and ZFS storage fails anywhere else |
| all PVE | Ceph commands run from *another* mon host | They keep working while this host reboots |
| all PVE | `noout` + `norebalance` around every reboot | Keeps Ceph from rebalancing while a host is briefly down |
| pve-whistler | `/etc/default/grub.d/cpu-debug.cfg` before reboot, `isolcpus=4,5` in `/proc/cmdline` after | CPU defect on P-core 2 |
| pve-s13 | Ceph public net reachable after boot; otherwise `ip link set <usb-nic> up && ifreload -a` | A cold boot once brought the USB NIC up after networking, leaving `vmbr9` without a port |
| pve-ugreen | Waits for `rbd-nightly-backup.sh` and kopia snapshots; afterwards `zpool status zbackup` and the CephFS mounts | Backup host; its clock is HDT |
| all PVE | NFS storages mounted and answering `stat` after reboot | A QNAP firmware update wedges NFS clients until reboot |
| SR-IOV hosts | Driver swapped before apt; VFs + host i915 version verified after | Old driver fails the new kernel's DKMS hook; GPU apps silently fall back to CPU |
| s13, mac, ugreen | Every DKMS module (r8152 USB NIC) built for the boot kernel; headers installed if missing | pve-mac has no `proxmox-default-headers` |
| K3s nodes | Drain uses `--disable-eviction` | Single-instance CNPG PDBs block eviction forever |
| K3s nodes | `modprobe --dry-run rbd ceph nfs` after the release upgrade | Storage drivers need these kernel modules |

## Secrets — this repo is public

`mainertoo/kubernetes-lab` is **public**, so nothing sensitive may be committed here, in any file type.

- **Nothing in `ansible/fleet/` holds a credential.**
  - SSH uses keys already on the operator's machine; the inventory only references their paths.
  - PVE hosts are reached through `~/.ssh/config` aliases.
  - The `spark` sudo password is typed at the `-K` prompt and never stored.
  - `zwave-js` is driven through the Proxmox guest agent, so it needs no password.
- **If a playbook ever needs a secret** (become password, API token, webhook), put it in a `*.sops.yaml` file under `ansible/`. The root `.sops.yaml` has a rule for `^ansible/.+\.sops\.ya?ml$` that encrypts every value. Check for `ENC[` before committing, because a `.sops.yaml` suffix alone doesn't guarantee encryption. **Never use `ansible-vault` files or plaintext `vars` for secrets.**
- **`ansible/fleet/.gitignore`** blocks retry files, fact caches, logs, probe output and SOPS plaintext temp files.
- **Output can leak credentials:** never print process command lines on kopia-lxc (`pgrep -f`, not `pgrep -af`/`ps`), because the kopia server takes its credentials as arguments.
- **etcd snapshots** (`k3s etcd-snapshot save`, taken before touching masters) contain every Kubernetes Secret. They stay on the master VMs under `/var/lib/rancher/k3s/server/db/snapshots/`. Never copy them into the repo.

## Kernel pins

`unpin_kernel: true` (the default) removes any `proxmox-boot-tool` pin, so each host boots its newest installed kernel. Pass `-e unpin_kernel=false` to keep pins. The playbook still asserts the host booted the kernel it expected.

## Inventory

`inventory/hosts.yml` is static on purpose: it records which K3s VM (VMID and node name) lives on which Proxmox host, which the host playbook needs for draining. Update it whenever you add, move or rebuild a VM.
