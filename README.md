# Hephaestus 🛠️ 

A homelab forged with **Proxmox**, **Terraform**, **Ansible**, **K3s**, **Flux**, and **Renovate**.

---

## 🚀 Overview

**Hephaestus** is a mono repository for managing my Kubernetes-based homelab. It handles everything from provisioning and configuring virtual machine nodes to automating application deployments and managing backups.

I'm transitioning from a setup based on Proxmox VMs and LXC containers to a more modern, Kubernetes-native environment. The goal is to automate everything—spinning up bare-metal servers, bootstrapping K3s, deploying apps, and keeping everything in sync through CI/CD workflows.

This project is a learning playground, a passion project, hopefully—a way to achieve a wife approved home automation setup.

---

## 🌐 Core stack

| Layer          | Tooling                                                                                                      |
| -------------- | ------------------------------------------------------------------------------------------------------------ |
| Virtualization | [Proxmox VE](https://www.proxmox.com/en/)                                                                    |
| Provisioning   | [Terraform](https://www.terraform.io/) + [Cloud-Init](https://cloudinit.readthedocs.io/)                     |
| Bootstrapping  | [Ansible](https://www.ansible.com/)                                                                          |
| Kubernetes     | [K3s](https://k3s.io/)                                                                                       |
| GitOps         | [Flux](https://fluxcd.io/)                                                                                   |
| Secrets Management   | [SOPS](https://github.com/mozilla/sops)                          |
| Ingress Controller   | [Traefik](https://doc.traefik.io/traefik/)                          |
|CSI storage   | [democratic-csi](https://github.com/democratic-csi/democratic-csi)                          |
|Backup/Recovery   | [volsync](https://volsync.readthedocs.io/en/stable/)                          |
| Dependency Management   | [Renovate](https://github.com/renovatebot/renovate)                          |
| Observability  | TBD (Prometheus,Loki,Grafana...) |

---

## 🛠️ Goals

- Replace my current Proxmox-based VM/LXC infrastructure with a Kubernetes-first approach
- Automate everything from bare metal provisioning to app deployment
- Maintain a declarative, self-healing, and idempotent system
- Learn and implement best practices around GitOps, CI/CD, Kubernetes security, and infrastructure automation

---

## 📊 Current Status

> \_"Under active development. Expect chaos, pain, and possibly fire."

- ✅ Proxmox environment running
- ✅ Terraform/Ansible bootstrapping complete
- ✅ Two K3s clusters deployed: **production** (3M + 3W, the live homelab) and **staging** (1M + 2W testbed)
- ✅ Flux desired state deployments — both clusters reconcile their own `clusters/<name>/` entrypoint
- ✅ Automatic backup and restore of Persistent Volumes ([volsync](https://volsync.readthedocs.io/en/stable/))
- ✅ Migrate all LXC applications
- ⏳ Setup observability/monitoring

### Cluster overview

| Cluster | Nodes | VIPs / Pools | Public hostname | Internal hostname |
| --- | --- | --- | --- | --- |
| production | 3 masters (`192.168.90.161-163`) + 3 workers (`.164-.166`) | kube-vip `.160`, MetalLB `.180-.199` | `*.mainertoo.com` (via pangolin + newt) | `*.lab.mainertoo.com` (via AdGuard) |
| staging | 1 master (`.167`) + 2 workers (`.168-.169`) | kube-vip `.170`, MetalLB `.200-.219` | `*.staging.mainertoo.com` (LAN-only until newt registered) | — |

Local kubeconfig: `~/.kube/config` with contexts `production` and `staging`. Switch via `kubectl config use-context <name>`.

---

## 📂 Repository Structure

```
Hephaestus/
├── ansible/                # Per-cluster inventory + K3s lifecycle playbooks (see ansible/README.md)
├── apps/
│   ├── base/               # All app definitions
│   ├── production/         # Apps active on production cluster
│   ├── staging/            # Apps active on staging cluster (opt-in, currently empty)
│   └── archive/            # Disabled/old manifests
├── clusters/
│   ├── production/         # Flux entrypoint for production
│   └── staging/            # Flux entrypoint for staging
├── components/             # Reusable Kustomize Components (volsync, cnpg-cluster, ...)
├── docs/                   # Architecture, runbooks, plans
├── infrastructure/
│   ├── controllers/                # Production-side controllers (full set)
│   ├── controllers-staging/        # Staging-side controllers (minimal opt-in)
│   ├── configs/cert-manager/{production,staging}/   # Per-cluster issuers + wildcard certs
│   ├── repositories/               # HelmRepository / OCIRepository CRDs
│   ├── secrets-prod/               # SOPS-encrypted Secrets — production only
│   └── secrets-shared/             # SOPS-encrypted Secrets — both clusters (cert-manager ns + Cloudflare DNS01 token)
├── terraform/
│   ├── modules/k3s-cluster/        # Reusable Proxmox VM module (see terraform/README.md)
│   └── environments/{production,staging}/   # Per-cluster module call + tfstate
└── README.md
```

---

## 🌐 Inspirations

Much inspiration has been taken from the incredible [onedr0p/home-ops](https://github.com/onedr0p/home-ops)/[cluster-template](https://github.com/onedr0p/cluster-template) repository, among others in the self-hosted and GitOps communities. Join the [Home Operations](https://discord.gg/home-operations) Discord community!

---

## 💥 Things I Broke

- **[2025-03-27] The Great Namespace Refactor Disaster**\
  [Commit 3faa0e2](https://github.com/willnotcy/Hephaestus/commit/3faa0e28c636eecd0b08e4a1e607efecfc216ff7) — Moved all `namespace.yaml` files for better structure... and accidentally wiped every application and all their persistent volumes. Learned the hard way how the prune option for Flux kustomizations is applied.

---