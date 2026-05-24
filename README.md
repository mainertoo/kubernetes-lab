# mainertoo / kubernetes-lab 🏠⚙️

A homelab created with **Proxmox**, **Terraform**, **Ansible**, **K3s**, **Flux**, **Ceph**, and a lot of GitOps.

Originally forked from [willnotcy/Hephaestus](https://github.com/willnotcy/Hephaestus) — full credit and thanks for the bones and skeleton of this lab. The repo has since diverged significantly: new clusters, new storage backend, new backup engine, new CNPG-managed databases, new observability stack, and a lot of restructuring.

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

- Replace the old Proxmox-VM/LXC and docker swarm sprawl with a Kubernetes-first approach
- Automate everything from bare-metal provisioning to app deployment
- Maintain a declarative, self-healing, idempotent system
- Treat backups as a first-class concern — label-driven, single-engine, restore-tested
- Learn GitOps, Kubernetes, Ceph, and storage recovery the hard way

---

## 📊 Current Status

> _"Under active development. Expect mistakes, pain, and stupd oversights."_

- ✅ Proxmox environment running across three nodes (`pve-mammoth`, `pve-whistler`, `pve-zermatt`) ... a call to great mountains
- ✅ Terraform + Ansible bootstrap end-to-end
- ✅ Two K3s clusters: **production** (3 master + 3 worker) and **staging** (1 master + 2 worker)
- ✅ Flux reconciliation — each cluster owns its own `clusters/<name>/` entrypoint
- ✅ All legacy LXC apps migrated to Kubernetes
- ✅ Label-driven backups: every PVC opts in with `backup: hourly|daily` + `backup-engine: kopia` and Kyverno generates the rest
- ✅ All Postgres workloads on CloudNativePG with S3 WAL + base backups
- ✅ Observability stack: kube-prometheus-stack + Loki + Grafana + Alloy
- ⏳ Offsite DR (waiting on hardware)
- ℹ️ The current `controllers/` + `controllers-staging/` and `secrets-prod/` + `secrets-shared/` layout works and is stable. A future, more idiomatic Kustomize `base/ + production/ + staging/` overlay restructure is a known deferred refactor — not blocking, parked by choice.

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
- [`observability.md`](docs/observability.md) — Prometheus + Alertmanager + Grafana + Loki + Alloy, custom alerts, dashboards, LogQL starters

---

## 💡 Recommendations (if you're building something like this)

A handful of things I wish I had done from day one, paid for the hard way:

- **Lock down `.gitignore` from commit zero.** Decide what _should_ be in the repo, then exclude everything else by default. I made the opposite call early on and ended up doing multiple sensitive-content audits plus a `git filter-repo` history scrub to remove tokens, webhook URLs, and `tfvars` files that had snuck in. Far cheaper to keep them out than to scrub them out later.

- **Go public early if you're going to lean on AI + PR-driven workflows.** Private repos cap GitHub Actions at 2,000 free minutes/month, and an LLM-assisted "open small PR → CI runs → merge" loop chews through that ceiling fast (every PR runs `flux-local` test + diff). Public repos get unlimited standard-runner minutes. If your only reason for staying private is "I haven't audited it yet," sequencing the audit and the public flip earlier in the project is the better trade.

- **Design for N clusters from day one.** This repo grew up as a single-cluster setup; bolting on a second cluster (staging) later meant retrofitting per-environment overlay paths, splitting Flux Kustomizations, and figuring out which controllers were truly shared vs production-only after the fact. If you might ever want a staging or dev cluster, set up `clusters/<name>/`, `apps/<name>/`, and per-environment infra paths from the first commit — even if only one of them is wired up at the start.

- **SOPS: decide where the age private key lives _before_ you encrypt anything.** Pick a durable home (password manager, hardware-backed keystore, an offline backup that isn't on the same disk as the cluster you're protecting) and document the bootstrap path. Then configure your editor to auto-encrypt on save — the VS Code SOPS extension handles `*.sops.yaml` files cleanly. Critical: before every commit, verify no `.decrypted` or scratch-file artifacts have leaked into the working tree, and confirm encrypted files actually start with `ENC[`. A `*.sops.yaml` filename does **not** guarantee the contents are encrypted.

- **Backups: pick one engine, opt in by label, restore-test on a real schedule.** I burned through Restic → shared-Restic → Kopia before settling. Kopia handles concurrent multi-writer cleanly; Restic needs exclusive locks for `forget`/`prune`, which becomes brutal at fleet scale. If you start fresh, start with Kopia. And whatever engine: drive opt-in via PVC labels + a Kyverno generate policy, not hand-rolled per-app `ReplicationSource` YAML.

- **Use CloudNativePG for _any_ Postgres-backed app — even small ones.** Don't bother with a chart's bundled `bitnami/postgresql` or a hand-rolled StatefulSet. CNPG gives you S3 WAL + base backups, PITR, automated failover, and a sane operator path — and migrating later is meaningfully harder than starting there (8 apps × roughly half-a-day each in my case).

- **Home Assistant on Kubernetes works — but with a tax.** Every HA add-on that's "just a checkbox" on HAOS becomes its own Kubernetes Deployment with its own PVC, Service, and ingress in this world. ESPHome, Zigbee2MQTT, Matter Server, AppDaemon, etc. all turn into first-class manifests. If you're heavily addon-dependent, weigh that overhead against running HAOS as a VM and only Kubernetes-ifying the things that actually benefit.

- **Don't rename a Flux `Kustomization`'s `metadata.name` if `prune: true` is set.** Flux treats `name` as identity — a rename is a delete-old + create-new — and `prune` cascades that delete instantly, including PVCs. Rename the `spec.path` directory instead and keep the CR name. Painful enough to deserve its own warning line.

- **Set up Renovate on day one.** A repo this size left to drift becomes difficult to upgrade later. Renovate-managed Helm chart + image bumps with auto-merge for patch versions = continual small wins instead of one giant scary upgrade weekend.

---

## 🌐 Inspirations

Huge thanks to the broader homelab + GitOps community, especially:

- [willnotcy/Hephaestus](https://github.com/willnotcy/Hephaestus) — the original fork this repo grew out of
- [mitchross/talos-argocd-proxmox](https://github.com/mitchross/talos-argocd-proxmox) — the Kopia-based, label-driven VolSync pattern in this repo (every PVC opts in with `backup: hourly|daily` + `backup-engine: kopia`, with Kyverno generating the rest) was directly adapted from mitchross's design. Huge credit — it reshaped how this cluster does backups.
- [onedr0p/home-ops](https://github.com/onedr0p/home-ops) / [cluster-template](https://github.com/onedr0p/cluster-template)
- The [Home Operations](https://discord.gg/home-operations) Discord
- [bjw-s/helm-charts](https://github.com/bjw-s/helm-charts) — `app-template` is the canonical HelmRelease base for every app in this repo

---
