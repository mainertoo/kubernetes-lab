# mainertoo / kubernetes-lab 🏠⚙️

A homelab forged with **Proxmox**, **Terraform**, **Ansible**, **K3s**, **Flux**, **Ceph**, and a lot of GitOps.

Originally forked from [willnotcy/Hephaestus](https://github.com/willnotcy/Hephaestus) — full credit and thanks for the bones of this lab. The repo has since diverged significantly: new clusters, new storage backend, new backup engine, new CNPG-managed databases, new observability stack, and a lot of restructuring.

---

## 🚀 Overview

A mono-repo for everything that runs my Kubernetes homelab: Proxmox VM provisioning, K3s bootstrap, every controller, every app, every backup policy, and every certificate. Two clusters reconcile from this one repository via Flux CD.

The goal is a fully declarative, self-healing setup where the entire stack — from bare Proxmox hosts all the way up to running apps with backed-up data — can be rebuilt from this repo plus a couple of bootstrap secrets.

This is a learning project, a passion project, and a "can I keep my house's automation up to my own standards while still seeing my family" project.

---

## 🤖 A note on AI assistance

A meaningful portion of this repo — refactors, new apps, controller wiring, runbooks, migration plans, and the backup architecture — has been built and iterated on with the help of LLMs (primarily **Claude Code** and **ChatGPT**). Some of it is straight "vibe coding," some of it is careful pair-design with the model. I'm calling that out up front rather than pretending otherwise.

Everything in here runs in production on my own hardware, against my own data, with my own backups. Treat patterns here as ideas to learn from, not as gospel.

---

## 🌐 Core stack

| Layer | Tooling |
| --- | --- |
| Virtualization | [Proxmox VE](https://www.proxmox.com/en/) |
| Provisioning | [Terraform](https://www.terraform.io/) + [Cloud-Init](https://cloudinit.readthedocs.io/) (via [bpg/proxmox](https://registry.terraform.io/providers/bpg/proxmox/latest)) |
| Bootstrapping | [Ansible](https://www.ansible.com/) |
| Kubernetes | [K3s](https://k3s.io/) (embedded etcd HA, traefik/servicelb disabled) |
| GitOps | [Flux CD](https://fluxcd.io/) |
| Control-plane VIP / LB | [kube-vip](https://kube-vip.io/) + [MetalLB](https://metallb.universe.tf/) |
| Ingress | [Traefik](https://doc.traefik.io/traefik/) with [Authentik](https://goauthentik.io/) middleware |
| Certificates | [cert-manager](https://cert-manager.io/) + Let's Encrypt (DNS-01 via Cloudflare) |
| Block / shared storage | [Ceph](https://ceph.io/) — `ceph-rbd` (RWO) + `cephfs` (RWX) via the Ceph CSI drivers |
| Backup / recovery | [VolSync](https://volsync.readthedocs.io/) with [Kopia](https://kopia.io/) → [Garage](https://garagehq.deuxfleurs.fr/) S3, plus host-level Kopia LXC snapshots |
| Postgres | [CloudNativePG](https://cloudnative-pg.io/) with `plugin-barman-cloud` for WAL/base backups |
| Policy / generation | [Kyverno](https://kyverno.io/) (label-driven backup wiring, mutation policies) |
| Secrets management | [SOPS](https://github.com/getsops/sops) + age, decrypted in-cluster by Flux |
| External access | [Pangolin](https://github.com/fosrl/pangolin) + [newt](https://github.com/fosrl/newt) tunnels, [Cloudflared](https://github.com/cloudflare/cloudflared), [Tailscale Operator](https://tailscale.com/kb/1236/kubernetes-operator) |
| Observability | [kube-prometheus-stack](https://github.com/prometheus-community/helm-charts), [Grafana](https://grafana.com/), [Loki](https://grafana.com/oss/loki/) + [Alloy](https://grafana.com/docs/alloy/) |
| Dependency management | [Renovate](https://github.com/renovatebot/renovate) |
| CI | GitHub Actions running [flux-local](https://github.com/allenporter/flux-local) for render + diff on every PR |

---

## 🛠️ Goals

- Replace the old Proxmox-VM/LXC sprawl with a Kubernetes-first approach
- Automate everything from bare-metal provisioning to app deployment
- Maintain a declarative, self-healing, idempotent system
- Treat backups as a first-class concern — label-driven, single-engine, restore-tested
- Learn GitOps, Kubernetes, Ceph, and storage recovery the hard way

---

## 📊 Current Status

> _"Under active development. Expect chaos, pain, and occasionally fire."_

- ✅ Proxmox environment running across three nodes (`pve-mammoth`, `pve-whistler`, `pve-zermatt`)
- ✅ Terraform + Ansible bootstrap end-to-end
- ✅ Two K3s clusters: **production** (3 master + 3 worker) and **staging** (1 master + 2 worker)
- ✅ Flux reconciliation — each cluster owns its own `clusters/<name>/` entrypoint
- ✅ All legacy LXC apps migrated to Kubernetes
- ✅ Label-driven backups: every PVC opts in with `backup: hourly|daily` + `backup-engine: kopia` and Kyverno generates the rest
- ✅ All Postgres workloads on CloudNativePG with S3 WAL + base backups
- ✅ Observability stack: kube-prometheus-stack + Loki + Grafana + Alloy
- ⏳ Offsite DR (waiting on hardware)
- ⏳ Optional refactor to a full `base/ + production/ + staging/` overlay layout

### Cluster overview

| Cluster | Nodes | VIPs / Pools | Public hostname | Internal hostname |
| --- | --- | --- | --- | --- |
| production | 3 masters (`192.168.90.161-163`) + 3 workers (`.164-.166`) | kube-vip `.160`, MetalLB `.180-.199` | `*.mainertoo.com` (via pangolin + newt) | `*.lab.mainertoo.com` (via AdGuard) |
| staging | 1 master (`.167`) + 2 workers (`.168-.169`) | kube-vip `.170`, MetalLB `.200-.219` | `*.staging.mainertoo.com` (LAN-only until newt registered) | — |

Local kubeconfig: `~/.kube/config` with contexts `production` and `staging`. Switch via `kubectl config use-context <name>`.

---

## 📂 Repository structure

```text
kubernetes-lab/
├── ansible/                # Per-cluster inventory + K3s lifecycle playbooks (see ansible/README.md)
├── apps/
│   ├── base/               # All app definitions (HelmRelease, IngressRoute, PVC, etc.)
│   ├── production/         # Apps active on the production cluster
│   ├── staging/            # Apps active on the staging cluster (opt-in, currently empty)
│   └── archive/            # Disabled / historical manifests (not reconciled)
├── clusters/
│   ├── production/         # Flux entrypoint for production
│   └── staging/            # Flux entrypoint for staging
├── components/             # Reusable Kustomize Components (volsync, cnpg-cluster, ...)
├── docs/                   # Architecture notes, runbooks, plans (see below)
├── infrastructure/
│   ├── controllers/                # Production-side controllers (full set)
│   ├── controllers-staging/        # Staging-side controllers (minimal opt-in)
│   ├── configs/cert-manager/{production,staging}/   # Per-cluster issuers + wildcard certs
│   ├── repositories/               # HelmRepository / OCIRepository CRDs
│   ├── secrets-prod/               # SOPS-encrypted Secrets — production only
│   └── secrets-shared/             # SOPS-encrypted Secrets — both clusters
├── terraform/
│   ├── modules/k3s-cluster/        # Reusable Proxmox VM module (see terraform/README.md)
│   └── environments/{production,staging}/   # Per-cluster module call + tfstate
└── README.md
```

---

## 📚 Docs

Architecture notes and runbooks live in [`docs/`](docs/). A few worth highlighting:

- [`ha-architecture.md`](docs/ha-architecture.md) — cluster-wide HA pattern (eviction grace, PDBs, topology spread)
- [`label-driven-backups.md`](docs/label-driven-backups.md) — how PVC labels drive VolSync + Kyverno-generated backup wiring
- [`volsync-kopia-transition.md`](docs/volsync-kopia-transition.md) — moving the fleet from Restic to Kopia
- [`backup-architecture.md`](docs/backup-architecture.md) / [`backup-recovery.md`](docs/backup-recovery.md) — backup layers + restore procedures
- [`cnpg-disaster-recovery.md`](docs/cnpg-disaster-recovery.md) — CloudNativePG cluster recovery
- [`ceph-tuning-2026-05-07.md`](docs/ceph-tuning-2026-05-07.md) — the homelab Ceph tuning pass

---

## 🌐 Inspirations

Huge thanks to the broader homelab + GitOps community, especially:

- [willnotcy/Hephaestus](https://github.com/willnotcy/Hephaestus) — the original fork this repo grew out of
- [mitchross/talos-argocd-proxmox](https://github.com/mitchross/talos-argocd-proxmox) — the Kopia-based, label-driven VolSync pattern in this repo (every PVC opts in with `backup: hourly|daily` + `backup-engine: kopia`, with Kyverno generating the rest) was directly adapted from mitchross's design. Huge credit — it reshaped how this cluster does backups.
- [onedr0p/home-ops](https://github.com/onedr0p/home-ops) / [cluster-template](https://github.com/onedr0p/cluster-template)
- The [Home Operations](https://discord.gg/home-operations) Discord
- [bjw-s/helm-charts](https://github.com/bjw-s/helm-charts) — `app-template` is the canonical HelmRelease base for every app in this repo

---
