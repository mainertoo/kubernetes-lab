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

**`k3s_os_upgrade.yml`** runs these steps on each node:
1. Wait for vzdump on its Proxmox host; gate on the cluster.
2. Take an etcd snapshot (server nodes only).
3. Cordon, then drain with `--disable-eviction`.
4. Run `apt full-upgrade`, then `do-release-upgrade` if the node is older than `target_version` (26.04).
5. Reboot.
6. Verify the release, the k3s service, the `rbd`/`ceph`/`nfs` kernel modules and the `lan0` address.
7. Wait for Ready, uncordon, wait for pods to settle, soak.

**`pve_host_patch.yml`** runs these steps on each host:
1. **Gate:** Proxmox quorate, Ceph `HEALTH_OK` with all PGs `active+clean`, all K3s nodes Ready, vzdump idle.
2. **Prep:** `apt dist-upgrade`, drain the host's K3s nodes, set Ceph `noout`/`norebalance`, unpin the kernel, confirm a ZFS module exists for the boot kernel.
3. **Reboot:** wait for busy jobs, shut down the K3s VMs, reboot.
4. **Verify:** expected kernel, host-specific checks, quorum, this host's mon/OSD/MDS back.
5. **Finish:** unset the flags, wait for `HEALTH_OK`, uncordon one node at a time, soak.

## Recommended order

1. `fleet_status.yml` — baseline.
2. `lxc_patch.yml` and `vm_patch.yml` — low risk; can run alongside step 3.
3. `k3s_os_upgrade.yml -e target=k3s_staging` — canary for release upgrades.
4. `k3s_os_upgrade.yml -e target=k3s_production` — workers first, then masters.
5. `pve_host_patch.yml` — Ceph order: mon/OSD hosts zermatt → mammoth → whistler, then MDS-only mac → s13, then client-only ugreen.
6. `fleet_status.yml` — confirm.

Everything stops at the first failure (`any_errors_fatal`). A failed node or host is **left cordoned, with Ceph flags still set**, so you can inspect it. Fix it, then re-run with `--limit <host>`. The playbooks are safe to re-run: a node already on the target release skips the release upgrade.

## Host-specific checks encoded here

| Host | Check | Why |
|---|---|---|
| all PVE | ZFS module present for the kernel that will boot | No module means no `zbackup` pool on ugreen, and ZFS storage fails anywhere else |
| all PVE | Ceph commands run from *another* mon host | They keep working while this host reboots |
| all PVE | `noout` + `norebalance` around every reboot | Keeps Ceph from rebalancing while a host is briefly down |
| pve-whistler | `/etc/default/grub.d/cpu-debug.cfg` before reboot, `isolcpus=4,5` in `/proc/cmdline` after | CPU defect on P-core 2 |
| pve-s13 | Ceph public net reachable after boot; otherwise `ip link set <usb-nic> up && ifreload -a` | A cold boot once brought the USB NIC up after networking, leaving `vmbr9` without a port |
| pve-ugreen | Waits for `rbd-nightly-backup.sh` and kopia snapshots; afterwards `zpool status zbackup` and the CephFS mounts | Backup host; its clock is HDT |
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
