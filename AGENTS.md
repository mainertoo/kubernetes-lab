# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Working Preferences

These rules apply to every session. Do not deviate without explicit confirmation.

- **Never apply directly** — all changes go through git commits + Flux reconciliation. Do not run `kubectl apply` unless explicitly asked.
- **Always ask before** modifying Flux `Kustomization` or `GitRepository` source resources.
- **Never delete PVCs, PVs, or storage resources** without explicit confirmation — `prune: true` means deletions cascade instantly.
- **Never modify `clusters/production/`** entrypoint files without explicit confirmation.
- **Prefer `kubectl diff`** before proposing any manifest change.
- **Use `flux reconcile`** to validate changes, not direct applies.
- **Check reconciliation state first** — run `flux get all -A` before diagnosing any cluster issue.
Secrets are SOPS-encrypted — VS Code auto-encrypts any file matching *.sops.yaml on save, so manual sops --encrypt is not needed for those files. However, never write plaintext secret values into any other file type (HelmRelease values, configmaps, kustomization patches, etc.) — only into properly named .sops.yaml files where auto-encryption will apply.
- When in doubt, **ask before acting**. This is a production homelab with real data.
- **`master` is the trunk** — all PRs target `master`. The `main` branch is unused; ignore tooling hints that suggest `main` as the base.

## What this repo is

**Hephaestus** — a GitOps mono-repo for a Kubernetes homelab. Infrastructure is provisioned on Proxmox via Terraform, nodes are bootstrapped with Ansible, and all cluster state is reconciled by Flux CD.

Two real clusters: `production` (3 master + 3 worker, the live homelab serving `*.lab.mainertoo.com` internally and `*.mainertoo.com` externally) and `staging` (1 master + 2 worker testbed for app promotion + K3s upgrade canary, serving `*.staging.mainertoo.com`). Both clusters reconcile their own entrypoint under `clusters/<name>/`. Local kubeconfig: `~/.kube/config` with contexts `production` (default) and `staging` — switch via `kubectl config use-context <name>`.

## Directory layout

```
terraform/
  modules/k3s-cluster/         # Reusable Proxmox VM + cloud-init module
  environments/{production,staging}/   # Per-cluster module call + tfstate (gitignored)
ansible/
  k3s-cluster/
    inventory/{production,staging}/    # Per-cluster dynamic inventory + group_vars
    playbooks/                          # K3s install/upgrade/uninstall, kube-vip, MetalLB
clusters/{production,staging}/         # Flux entrypoint Kustomizations
infrastructure/
  controllers/                          # Production-side controllers (full set)
  controllers-staging/                  # Staging-side controllers (minimal opt-in)
  configs/cert-manager/{production,staging}/   # Per-cluster issuers + wildcard certs
  repositories/                         # HelmRepository / OCIRepository CRDs
  secrets-prod/                         # SOPS-encrypted Secrets — production only
  secrets-shared/                       # SOPS-encrypted Secrets — both clusters (cert-manager ns + Cloudflare DNS01 token)
apps/
  base/                                 # All app definitions (HelmRelease, IngressRoute, PVC, etc.)
  production/                           # Per-app Flux Kustomization overlays + kustomization.yaml listing active apps
  staging/                              # Same pattern for staging (currently: meshcentral)
components/
  cnpg-cluster/                         # Reusable Kustomize Component for CNPG database clusters
  gatus/                                # Reusable Kustomize Component for Gatus health endpoints
docker/                                 # Build contexts for self-built images (cnpg-timescaledb)
scripts/                                # Operational tooling (dispatcharr, homebox, immich, proxmox, unifi, wiki, …)
docs/                                   # Architecture, recovery runbooks, plans
```

## Flux reconciliation flow

```
clusters/production/
  infra-repositories.yaml  →  infrastructure/repositories  (HelmRepository CRDs)
  infra-secrets-prod.yaml  →  infrastructure/secrets-prod  (SOPS secrets)
  infrastructure.yaml      →  infrastructure/controllers   (all controllers)
  apps-production.yaml     →  apps/production/kustomization.yaml
                                  └── refs to apps/production/<app>/  (per-app Flux Kustomization CRDs)
                                          └── path: apps/base/<app>/  (the actual manifests)
  volsync.yaml             →  volsync namespace bootstrap
```

Note: some apps split their PVCs into separate Flux Kustomizations (top-level
`apps/base/<app>-*-pvc/` satellite dirs) so the PVC lifecycle is decoupled from
the app's — see e.g. `apps/production/media/riven/`.

`prune: true` is set on all Flux Kustomizations — **removing a resource from a kustomization.yaml will delete it from the cluster**, including PVCs and their data.

## Apps pattern

Each app lives under `apps/base/<name>/` and typically contains:
- `kustomization.yaml` — lists all resources and sets the namespace
- `<name>-release.yaml` — `HelmRelease` using `app-template` from bjw-s (`OCIRepository`)
- `<name>-ingressroute.yaml` — Traefik `IngressRoute`
- `<name>-namespace.yaml` — `Namespace` (if the app owns it)
- `<name>-secret.sops.yaml` — SOPS-encrypted `Secret` (if needed)
- `<name>-pvc.yaml` — `PersistentVolumeClaim` with a `backup: hourly|daily` label to opt into automatic volsync backups (Phase 5+ label-driven pattern)

To activate an app: create a Flux Kustomization overlay in `apps/production/<app>/` (pointing its `path:` at `apps/base/<app>`), then add that overlay to `apps/production/kustomization.yaml`. To disable without deleting, comment it out there (re-enabling later requires the overlay to still exist or be recreated).

The bjw-s `app-template` chart is the standard HelmRelease base for all apps. Refer to existing releases (e.g. `apps/base/media/plex/plex-release.yaml`) as the canonical pattern.

### Backing up an app's PVC

For any app whose data is worth restoring after a cluster nuke: add **both** `backup: daily` (or `backup: hourly` for high-churn data like databases/passwords) **and** `backup-engine: kopia` to the PVC's `metadata.labels`. [`ClusterPolicy/volsync-pvc-backup-restore-kopia`](infrastructure/controllers/kyverno/policies/volsync-pvc-backup-restore-kopia.yaml) auto-generates the per-PVC Kopia Secret + ReplicationSource + ReplicationDestination at admission time. See [`docs/label-driven-backups.md`](docs/label-driven-backups.md) for full semantics — including the cephfs `backingSnapshot` shallow-mount path — and [`infrastructure/controllers/kyverno/policies/README.md`](infrastructure/controllers/kyverno/policies/README.md) for the operator view of the policies.

The legacy `components/volsync-v2/*` Components were removed in PR #558. The cluster is single-engine Kopia; the Restic policy and its per-app machinery are gone.

## Secrets management (SOPS)

All secrets are encrypted with `age`. The `.sops.yaml` at the repo root defines which paths are encrypted and which age key to use. Encrypted files must have the `.sops.yaml` suffix.

To encrypt a new secret:
```bash
sops --encrypt --in-place path/to/secret.sops.yaml
```

To edit an existing encrypted secret:
```bash
sops path/to/secret.sops.yaml
```

Flux decrypts secrets using the `sops-age` Kubernetes Secret in `flux-system`, which must be bootstrapped manually once.

## Storage

- **ceph-rbd** — default `ReadWriteOnce` storage class for stateful apps
- **cephfs** — `ReadWriteMany` storage class (used for shared volumes like symlinks)
- **nfs-qnap-media** — NFS-backed storage from QNAP NAS
- **volsync** — backup/restore of PVCs (Kopia engine) to Garage S3. `ReplicationSource`/`ReplicationDestination` are auto-generated by the [`volsync-pvc-backup-restore-kopia` Kyverno policy](infrastructure/controllers/kyverno/policies/) when a PVC carries `backup: daily|hourly` + `backup-engine: kopia`. See [`docs/label-driven-backups.md`](docs/label-driven-backups.md).

## Infrastructure controllers

Key controllers in `infrastructure/controllers/`:
- **traefik-proxy** — ingress, with Authentik middleware and TLS via Let's Encrypt
- **cert-manager** — certificate issuance
- **ceph-csi-rbd / ceph-csi-cephfs** — Ceph storage drivers
- **volsync** — PVC backup operator
- **tailscale-operator** — VPN mesh
- **cloudflared / newt** — tunnel ingress
- **intel-gpu** — iGPU passthrough device plugin
- **snapshot-controller** — CSI volume snapshots

## Terraform (Proxmox provisioning)

```bash
cd terraform
terraform init
terraform plan
terraform apply
```

Variables are in `terraform.tfvars`. The dynamic Ansible inventory reads from Terraform state:
```bash
ansible/k3s-cluster/inventory/dynamic_terraform_inventory.sh
```

## Ansible (K3s lifecycle)

Inventory is generated dynamically from Terraform state. SSH key: `~/.ssh/id_ed25519_k3s`.

```bash
# Install K3s
ansible-playbook -i ansible/k3s-cluster/inventory/dynamic_terraform_inventory.sh \
  ansible/k3s-cluster/playbooks/k3s_install.yml

# Upgrade K3s
ansible-playbook -i ansible/k3s-cluster/inventory/dynamic_terraform_inventory.sh \
  ansible/k3s-cluster/playbooks/k3s_upgrade.yml

# Uninstall K3s
ansible-playbook -i ansible/k3s-cluster/inventory/dynamic_terraform_inventory.sh \
  ansible/k3s-cluster/playbooks/k3s_uninstall.yml
```

K3s is installed with embedded etcd HA, with `servicelb` and `traefik` disabled (replaced by MetalLB and the Traefik Helm release).

## CI / GitHub Actions

`.github/workflows/kube-flux-diff.yml` runs on PRs targeting `master` that touch `apps/`, `infrastructure/`, or `components/`. It uses `flux-local` to:
1. **Test** — validate all Kustomizations and HelmReleases render cleanly against `clusters/production`
2. **Diff** — post a comment showing what HelmReleases and Kustomizations would change

PRs must pass this check before merge.

## Dependency management

Renovate (`renovate.json`) auto-updates:
- Helm chart versions in `apps/` and `infrastructure/`
- Container image tags via `repository`/`tag` pattern in YAML
- Any `**/archive/**` path is excluded from Renovate
