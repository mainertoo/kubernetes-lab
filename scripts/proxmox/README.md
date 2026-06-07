# Proxmox tooling

Query (and optionally control) the Proxmox VE hosts by API — node/guest/storage
inventory and, most usefully for the VLAN work, each host's **bridges + VLAN tags**.

## Auth

Env vars `PROXMOX_URL` / `PROXMOX_TOKEN_ID` / `PROXMOX_TOKEN_SECRET` (+ optional
`PROXMOX_HOSTS`), or `sops -d credentials.sops.yaml` as a fallback. Auth is an API
**token**, sent as `Authorization: PVEAPIToken=<user@realm!tokenid>=<secret>`.

Mint a dedicated **non-root** `claude@pve` user + token (privilege-separation OFF) and
grant it a read+write role (`Datastore.Audit, VM.Audit, VM.PowerMgmt, VM.Snapshot,
Sys.Audit, Sys.Modify`) at path `/`. Keep it separate from the Terraform provisioning
token. See the header comment in `credentials.sops.yaml` for the click-path.

The main `PROXMOX_URL` should point at a cluster member (mammoth/whistler/zermatt) — its
`/nodes` + `/cluster/resources` cover the whole cluster. Add standalone hosts
(pve-mac/pve-s13/pve-ugreen/pve-s12) to `PROXMOX_HOSTS`; the same token must exist on each.

## Usage

```bash
./pvinfo.py nodes                 # cluster nodes (cpu/mem/uptime/status)
./pvinfo.py vms [--running]       # all guests (qemu + lxc) across hosts
./pvinfo.py storage               # storage pools per node
./pvinfo.py network [--node NAME] # bridges + VLAN tags per host  ← VLAN-migration check
./pvinfo.py map -o pve-map.yaml   # structured dump (nodes + guests + per-host network)
./pvinfo.py wiki -o /tmp/pve.md   # Wiki.js markdown page

# Writes — DRY-RUN by default; print intent unless --yes is passed:
./pvinfo.py vm reboot 701 --yes
./pvinfo.py snapshot 701 pre-vlan-change --description "before VLAN trunk edit" --yes
```

## Notes

- **Writes are guarded.** Every `vm`/`snapshot` call prints what it *would* do and exits
  unless you add `--yes`. The tool resolves which host owns a VMID via `/cluster/resources`.
- `network` is the migration workhorse: it shows `vmbr*` bridges, `vlan-aware`, allowed
  `vids`, per-iface `tag`, and CIDRs — use it to confirm VLAN trunks reach the K3s VMs
  before/after any UniFi switch-port change.
- `pve-map.yaml` is generated (gitignored). The tool is the source of truth — regenerate.
- TLS is unverified (PVE ships a self-signed cert), matching `scripts/unifi/netinfo.py`.
