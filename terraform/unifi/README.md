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

## Auth — API key (not username/password)

This controller (UniFi Network 10.4.57) supports per-request **API-key** auth, which
the `filipowm/unifi` provider drives against the internal write-path. We use it
because it does **not** hit the login endpoint — so it avoids the login
rate-limit/403 churn that the username/password path causes under Terraform's
repeated plan/apply cycles. (Note: the *official* Integration API is read-only for
config today; the provider does NOT use that surface — it uses the internal API,
where the same key has full write scope.)

The key is the existing UniFi key also used by `scripts/unifi/netinfo.py`. A local
admin account (`claude-tf`) was created during bootstrap and is now an unused
emergency fallback — keep it or delete it; auth no longer depends on it.

## Credentials

Stored SOPS-encrypted at `scripts/unifi/credentials.sops.yaml` (same pattern as
`scripts/proxmox/credentials.sops.yaml`), holding `UNIFI_API_KEY`, `UNIFI_API`,
`UNIFI_INSECURE`, `UNIFI_SITE`, and the SSID `TF_VAR_*_psk` passphrases. Re-encrypt
after any edit and confirm:

```bash
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
