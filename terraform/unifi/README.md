# UniFi network — Terraform

Declarative management of the home-network VLAN segmentation (the additive parts:
VLANs, SSIDs, switch port profiles, and the Zone-Based Firewall). Companion to
`docs/network-vlan-design.md` and `docs/network-vlan-migration-runbook.md`.

Provider: [`filipowm/unifi`](https://registry.terraform.io/providers/filipowm/unifi)
(`~> 1.0`) — a maintained fork of `paultyng/unifi` that adds Zone-Based Firewall
(`unifi_firewall_zone` / `unifi_firewall_zone_policy`) support.

## Scope / safety

- Manages **only the 6 new VLANs** (10/20/30/40/50/60) and, in later phases, the
  new SSIDs, port profiles, and firewall zones.
- **Does NOT** import or manage Management(1), Kubernetes(90), or Ceph(99) — those
  stay UI-managed so no Terraform mistake can touch critical infra.
- State (`*.tfstate`) and `*.tfvars` are gitignored repo-wide; never commit them.

## Why username/password (not the API key)

UniFi's official Integration API key is read-mostly today (write/config scope is
rolling out through 2026), so it can read networks but cannot create them. The
provider's write path uses a local-admin **username + password** against the
controller's internal API. Create a dedicated local admin:

> UniFi → Settings → Admins & Users → Add New Admin → **Restrict to local access
> only**, give it a strong password and full Network-app management rights.
> Username `claude-tf` (matches the creds template).

## Credentials

Stored SOPS-encrypted at `scripts/unifi/credentials.sops.yaml` (same pattern as
`scripts/proxmox/credentials.sops.yaml`). Bootstrap:

```bash
cp scripts/unifi/credentials.sops.yaml.example scripts/unifi/credentials.sops.yaml
# edit: set the real UNIFI_PASSWORD
sops --encrypt --in-place scripts/unifi/credentials.sops.yaml
grep -q 'ENC\[' scripts/unifi/credentials.sops.yaml && echo "encrypted OK"
```

## Run

`tf.sh` decrypts the creds into `UNIFI_*` env vars, then runs terraform:

```bash
cd terraform/unifi
./tf.sh init
./tf.sh plan      # review every change before applying
./tf.sh apply
```

Verify against the target scheme afterward:

```bash
python3 scripts/unifi/netinfo.py verify
```

## Phases (see the migration runbook)

| Phase | Files | Status |
|-------|-------|--------|
| A — VLANs | `vlans.tf` | scaffolded |
| A — SSIDs | `wlans.tf` (todo) | — |
| port trunks | `port_profiles.tf` (todo) | — |
| B — firewall | `firewall.tf` (todo) | — |
