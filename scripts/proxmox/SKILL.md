---
name: proxmox
description: Query and control the homelab Proxmox VE hosts by API — list nodes/VMs/storage, dump per-host bridges + VLAN tags (useful for network/VLAN work), and run guarded VM power/snapshot actions. Use whenever you need live hypervisor state or to act on a VM/host. Wraps scripts/proxmox/pvinfo.py in the kubernetes-lab repo; handles token auth from a SOPS secret automatically.
---

# Proxmox

A helper CLI (`pvinfo.py`, in `scripts/proxmox/` of the kubernetes-lab repo) for the
Proxmox VE REST API across the homelab's PVE hosts.

## Auth
Uses `PROXMOX_URL` / `PROXMOX_TOKEN_ID` / `PROXMOX_TOKEN_SECRET` (+ optional
`PROXMOX_HOSTS`) from the environment, else `sops -d scripts/proxmox/credentials.sops.yaml`.
Auth is an API token (`Authorization: PVEAPIToken=<id>=<secret>`); the token is a
dedicated non-root `claude@pve` token. Never print the secret. If the secret is missing
or unfilled, point the user at `scripts/proxmox/README.md` to mint the token.

## When to use
- Need live cluster state: which VMs are up, where a VMID runs, node CPU/mem, storage.
- **Network/VLAN work**: `pvinfo.py network` shows each host's `vmbr*` bridges, VLAN-aware
  flag, allowed `vids`, and per-iface `tag` — the way to verify VLAN trunks reach the K3s
  VMs before/after a UniFi switch-port change.
- Need to act on a VM (power/snapshot). **Writes are dry-run unless `--yes`** is passed —
  honor the repo rule "never apply directly / ask before acting"; confirm with the user
  before adding `--yes`.

## Commands
```bash
PVE=~/kubernetes-lab/scripts/proxmox/pvinfo.py
python3 $PVE nodes                  # nodes: status/cpu/mem/uptime
python3 $PVE vms [--running]        # all guests (qemu+lxc) across hosts
python3 $PVE storage                # storage pools per node
python3 $PVE network [--node NAME]  # bridges + VLAN tags per host
python3 $PVE map -o /tmp/pve.yaml   # structured dump
python3 $PVE wiki -o /tmp/pve.md    # Wiki.js markdown page

# Guarded writes (omit --yes to preview):
python3 $PVE vm reboot 701 --yes
python3 $PVE snapshot 701 pre-change --description "..." --yes
```

## Notes
- The main URL should target a cluster member; standalone hosts go in `PROXMOX_HOSTS`.
- Pair with `scripts/unifi/netinfo.py` (the UniFi read tooling) when validating network
  changes end-to-end (switch/VLAN on the PVE side, gateway/SSID/firewall on the UniFi side).
