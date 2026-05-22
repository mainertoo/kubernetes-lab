# terraform/

Proxmox VM provisioning for the K3s clusters. One reusable module, one
terraform state per cluster.

## Layout

```
terraform/
├── modules/
│   └── k3s-cluster/             # The VM + cloud-init module — consumed by both envs
│       ├── main.tf              # Proxmox resources (VMs, snippets, image download, USB mapping)
│       ├── variables.tf
│       ├── outputs.tf           # vm_info — consumed by Ansible's dynamic inventory
│       └── versions.tf
├── environments/
│   ├── production/              # Live homelab K3s cluster (mainertoo-*, 3M + 3W)
│   │   ├── main.tf              # Module call with current production values
│   │   ├── provider.tf
│   │   ├── variables.tf         # Sensitive var declarations
│   │   ├── terraform.tfvars     # Secrets (gitignored)
│   │   ├── terraform.tfvars.example
│   │   └── terraform.tfstate    # Per-env state (gitignored)
│   └── staging/                 # Future staging cluster (1M + 2W) — no state until Phase 4 apply
│       └── (same layout)
├── ssh_host_ed25519.pub         # Shared SSH public key injected into all VMs via cloud-init
└── terraform.tfstate.pre-phase2-2026-05-21   # Frozen legacy state — safe to delete after a few weeks
```

State files (`terraform.tfstate`, `terraform.tfstate.backup`) and any
`*.tfvars` are gitignored. `terraform.tfvars.example` shows the shape
of what you need without secrets.

## Common operations

All commands run from the per-environment directory. Always confirm
which environment you're in before applying.

```bash
cd terraform/environments/production    # or .../staging

terraform init                          # once after cloning, or after module changes
terraform plan                          # always start here
terraform apply                         # only after reading the plan
terraform apply -refresh-only           # sync state with reality without making real changes
terraform state list                    # see what's tracked
terraform output vm_info                # dump VM IPs / nodes / roles (Ansible reads this)
```

## Module API surface

Everything important is in `modules/k3s-cluster/variables.tf`. Highlights:

| Variable | What it does |
|---|---|
| `cluster_name` | Prefix for VM names (`<cluster_name>-k3s-master-N`). Changing this on an existing cluster forces VM recreation — don't. |
| `k3s_master_count` / `k3s_worker_count` | How many of each. |
| `k3s_master_ips` / `k3s_worker_ips` | Static IPs in `192.168.90.x/24` form, one per VM. |
| `k3s_master_vmids` / `k3s_worker_vmids` | Proxmox VMIDs (must be unique cluster-wide). |
| `pm_node_name` | Default Proxmox host. Used for cluster-wide resources (USB mapping, cloud-init snippets, image download). |
| `pm_master_node_names` / `pm_worker_node_names` | Optional per-VM Proxmox host placement (list, indexed by count). When non-empty, overrides `pm_node_name` per VM. Production uses this to spread across mammoth/whistler/zermatt. Empty list = scalar `pm_node_name` for all VMs. |
| `worker_hostpci_ids` | Optional per-worker PCI passthrough ID (e.g. `"0000:00:02.4"` for an Intel iGPU vGPU partition). Empty string at an index = no passthrough for that worker. |
| `worker_extra_nic_enabled` (+ bridge/vlan) | Adds a second virtio NIC to every worker. Production uses it for L2 access to `192.168.1.0/24` (mDNS / Home Assistant local discovery). |
| `k3s_*_cores` / `_sockets` / `_memory` / `_disk_size` | VM sizing. |
| `disk_file_format` | `qcow2` (default) for local Proxmox storage, `raw` for Ceph RBD. |
| `usb_mapping_enabled` | The Sonoff Zigbee USB hardware mapping is cluster-wide in Proxmox, so only one cluster can own it — production has it, staging skips it. |
| `snippets_per_host` | **Set this `true` when building a new cluster from scratch with VMs spread across multiple Proxmox hosts.** Uploads cloud-init snippets (user-data + per-VM metadata) to every host that runs a VM, and auto-prefixes filenames with `cluster_name` so two clusters writing to the same host don't collide. When `false` (default), all snippets live on `pm_node_name` only — fine for an existing cluster whose VMs were created on the snippet host and live-migrated afterwards (production's case). |
| `ubuntu_password` (sensitive) | Set in tfvars only. |

## Gotchas / considerations for future updates

### 1. The cascade-replacement traps

Two attributes in `bpg/proxmox` will, without lifecycle protection,
cascade-replace every VM in the cluster on the next apply. Both are
already neutralized in the module — when adding new resources of the
same kind, carry the same protections forward.

- **`proxmox_virtual_environment_download_file.size`** — the Ubuntu
  "current" cloud image URL is republished every few weeks (a few MB
  delta). Without `overwrite = false` on the download resource,
  terraform sees the size mismatch and replaces the image, which
  cascades into every VM via `disk.file_id`. The module ships with
  `overwrite = false` plus `lifecycle.ignore_changes = [disk[0].file_id]`
  on every VM as belt-and-suspenders.
- **`proxmox_virtual_environment_file.user_data_cloud_config.source_raw`** —
  cloud-init is consumed at first VM boot; updating user-data
  post-deploy doesn't re-trigger it on running VMs. The module ships
  with `lifecycle.ignore_changes = [source_raw]` on this resource.

To intentionally refresh the Ubuntu image: bump `pm_cloud_image_url`
to a dated build (e.g. `.../questing/20260520/...`) and run
`terraform apply -replace=module.cluster.proxmox_virtual_environment_download_file.ubuntu_cloud_image`.
VMs won't be touched.

### 2. Other lifecycle ignores worth knowing about

Every VM has `lifecycle.ignore_changes` covering provider-version
churn that's not real drift:

- `initialization` — Proxmox API surfaces cloud-init artifacts
  post-boot (`user_account`, `meta_data_file_id` → null, etc.) that
  are noise.
- `boot_order`, `purge_on_destroy`, `tags`, `delete_unreferenced_disks_on_destroy` —
  attributes the provider auto-populates with defaults.

If you add a new VM resource and don't include this block, the next
`terraform plan` will show a lot of cosmetic diffs.

### 3. Adding a new environment

1. `mkdir -p terraform/environments/<name>`
2. Copy `provider.tf`, `variables.tf`, and `terraform.tfvars.example`
   from `production/`. Adjust defaults (Proxmox URL, SSH username).
3. Write a fresh `main.tf` with a `module "cluster"` block. Pick
   non-overlapping IPs, VMIDs, kube-vip VIP, MetalLB pool range.
4. `terraform init`, fill in `terraform.tfvars`, then `terraform apply`.

### 4. Adding new module resources

Any new `proxmox_virtual_environment_vm`, `_file`, or `_download_file`
resource you add to the module needs the same `lifecycle.ignore_changes`
blocks as the existing ones. Otherwise you'll re-introduce the cascade
traps documented above.

### 5. `bpg/proxmox` deprecation warning

The provider will rename
`proxmox_virtual_environment_hardware_mapping_usb` → `proxmox_hardware_mapping_usb`
in v1.0. Renaming now would force a state migration (`terraform state mv`)
and isn't worth doing until v1.0 actually lands. The warning is harmless
until then.

### 6. State-vs-reality drift

Phase 2 of the two-cluster restoration discovered significant drift
between terraform state and reality (workers had been resized, disks
migrated to Ceph RBD, GPU passthrough added, etc.). The fix was
`terraform apply -refresh-only` + encode all the surfaced drift in
the module + env config.

**Habit**: any time you change something via the Proxmox UI/CLI
(resize a VM, add hardware, migrate disks), reflect it in the module
or the env's `main.tf` and run `terraform plan` until it shows
zero changes. Otherwise the next `apply` will silently revert your
manual change.

### 7. Fresh-cluster rebuild: snippet placement

Proxmox stores cloud-init snippets in per-host `local` storage, not on
shared/cluster-wide storage. If you `terraform apply` a brand-new
cluster whose VMs land on hosts other than `pm_node_name`, the VM-start
either fails (snippet missing) or — worse — silently reads a same-named
snippet from a different cluster that lives on that host (hostname
collision: a staging VM came up with production's hostname during the
Phase 4 bootstrap on 2026-05-21).

Two ways out:

- **Build-from-scratch (new cluster):** set `snippets_per_host = true`
  on the module call. Snippets get uploaded to every host that hosts a
  VM, with filenames auto-prefixed by `cluster_name` to avoid
  collisions. VMs can spread across hosts on the very first
  `terraform apply`.
- **Existing cluster (production, current staging):** keep the default
  `snippets_per_host = false`. Initial apply lands every VM on
  `pm_node_name`; live-migrate workers to their target hosts via the
  Proxmox UI afterwards; then `terraform apply -refresh-only` to sync
  state. Cloud-init has been consumed by then, so the migration is
  safe even though the VMs' `cicustom` references still point at the
  original host's snippet path.

### 8. Sensitive files

- `terraform/environments/*/terraform.tfvars` — has the Proxmox API
  token, SSH password, ubuntu user password. Gitignored. Don't commit.
- `terraform/environments/*/terraform.tfstate*` — has decrypted SSH
  keys, MAC addresses, full provider state. Gitignored. Don't commit.

The legacy `terraform/terraform.tfvars.bak` (tracked in git history)
may contain real secrets and should be sanitized — see project
memory for context.

## Related

- VM IPs / placement output flows into `ansible/k3s-cluster/inventory/<env>/dynamic.sh`.
- After provisioning, run the Ansible playbooks in
  `ansible/k3s-cluster/playbooks/` to install K3s + kube-vip + MetalLB.
