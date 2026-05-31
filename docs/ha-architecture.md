# High-Availability Architecture

How control-plane services and core controllers in this cluster survive a worker node going down. Implemented in PRs #231, #232, #233 (May 2026).

---

## Cluster baseline

- 3 master nodes (control-plane + embedded etcd)
- 3 worker nodes
- Storage: Ceph (RWO via `ceph-rbd`, RWX via `cephfs`) + NFS
- Ingress: Traefik behind MetalLB-allocated LoadBalancer IP
- GitOps: Flux v2

Failure scenario this is designed for: **one worker node disappears unexpectedly.** Two-worker loss is degraded; the design uses `whenUnsatisfiable: ScheduleAnyway` everywhere so scheduling never deadlocks under double-failure.

---

## The headline fix: eviction grace

The `DefaultTolerationSeconds` admission plugin gives every pod 300s tolerationSeconds on `node.kubernetes.io/{not-ready,unreachable}`. That's a **5-minute wait** before a failed-node's pods get evicted and rescheduled.

Lowered to **30 seconds** by setting two kube-apiserver flags. Because k3s embeds the apiserver, the flags live in `/etc/rancher/k3s/config.yaml.d/10-apiserver-tuning.yaml` (a drop-in directory that k3s ≥ 1.21 merges over the base config — leaving the existing `disable: [traefik, servicelb]` etc. untouched):

```yaml
kube-apiserver-arg:
  - default-not-ready-toleration-seconds=30
  - default-unreachable-toleration-seconds=30
```

Applied via Ansible playbook `ansible/k3s-cluster/playbooks/k3s_server_config.yml` with `serial: 1` for a rolling restart of the masters.

### Verifying the flags are live

```bash
kubectl run ha-test --image=busybox --restart=Never --dry-run=server \
  -o jsonpath='{.spec.tolerations[*].tolerationSeconds}' -- sleep 1
# Expected: 30 30
```

The DefaultTolerationSeconds plugin runs on every pod create, so server-side dry-run reveals the effective value.

---

## Per-controller HA matrix

| Controller / namespace | Replicas | PDB `minAvailable` | Topology spread | Configured via |
|---|---|---|---|---|
| **Flux** (`flux-system`) — kustomize / helm / notification | 2 each | 1 | hostname, ScheduleAnyway | Kustomize patches in `clusters/{staging,production}/flux-system/kustomization.yaml` + `flux-pdbs.yaml` |
| **Flux** (`flux-system`) — source-controller | **1** (intentional, see gotchas) | — | — | Chart default |
| **cert-manager** (`cert-manager`) — controller / webhook / cainjector | 2 each | 1 | hostname, ScheduleAnyway | HelmRelease values |
| **Kyverno** (`kyverno`) — admission / background / cleanup / reports | 2 each | 1 | hostname, ScheduleAnyway | HelmRelease values |
| **Traefik** (`traefik-system`) | 3 | 1 | hostname, ScheduleAnyway | HelmRelease values |
| **snapshot-controller** (`snapshot-controller-system`) | 2 | 1 | hostname, ScheduleAnyway | HelmRelease values + explicit PDB |
| **metallb-controller** (`metallb-system`) | 2 | 1 | hostname, ScheduleAnyway | Ansible playbook (kubectl patch) |
| **rancher** (`cattle-system`) | 3 | 1 | hostname, antiAffinity preferred | HelmRelease values + explicit PDB |

**DaemonSets** (already inherently HA, one per node, no change needed): metallb-speaker, ceph-csi-rbd / cephfs node plugins, csi-nfsplugin, intel-gpu-plugin, alloy, kube-prometheus-stack-prometheus-node-exporter, kube-vip.

---

## Why `ScheduleAnyway` over `DoNotSchedule`

Strict spread (`DoNotSchedule`) would leave pods Pending if topology can't be satisfied — e.g. with N replicas > N schedulable nodes during multi-node failure. `ScheduleAnyway` gives the scheduler a soft preference: real spread under healthy conditions, gracefully degrades to clustered placement under double-failure rather than hanging.

Trade-off: under happy-path scaling, the scheduler can occasionally cluster pods on the same node. The fix is a single pod delete — the deployment respawns and the scheduler picks a less-occupied node.

---

## Two-stage rollout pattern (Ansible + GitOps)

When a change spans the cluster bootstrap layer (k3s flags, MetalLB native install) **and** the GitOps layer (Helm/Flux), the rollout has two stages:

- **Stage 1 — Ansible.** Run the playbook against masters/all nodes. Idempotent, rolling, verifiable in real time.
- **Stage 2 — GitOps.** Commit, PR with `kube-flux-diff` CI verification, merge → Flux reconciles within the hour (or trigger explicitly with `flux reconcile kustomization flux-system -n flux-system --with-source`).

Stage 1 is self-contained and can ship without Stage 2; that's how the eviction-grace fix went out before the controller-scaling PR.

---

## Subtle gotchas

### source-controller stays at 1 replica (different from the other Flux controllers)

source-controller's HTTP server on port 9090 only binds **after** the pod acquires the leader lease. The other three Flux controllers use `/healthz` on port 9440 for readiness, which binds at startup, so a 2-replica deploy of those shows `2/2 Ready` and behaves cleanly. source-controller, however, would show `1/2 Ready` indefinitely — its standby pod is intentionally NotReady to keep the Service from routing requests to a pod whose 9090 isn't listening yet.

We initially shipped `replicas: 2` for source-controller anyway (PR #231), reasoning that the `1/2 Ready` was cosmetic and failover from the hot-standby was ~15s. That broke when kube-prometheus-stack started firing `KubeDeploymentReplicasMismatch` and `KubeDeploymentRolloutStuck` after the standard 15-minute threshold — they're real alerts seeing real-looking-broken state.

The fix is to leave source-controller at the chart default (`replicas: 1`):

- Recovery on node failure: ~45s (30s eviction grace + ~15s pod restart) — comparable to the hot-standby benefit, since source-controller's source cache lives on `emptyDir` and is wiped on pod death anyway. The standby would be re-fetching from origin on takeover, just like a fresh pod.
- No alert noise.
- Drains don't block (no PDB on a 1-replica deploy means voluntary disruption is allowed).

The other three Flux controllers (`kustomize`, `helm`, `notification`) keep `replicas: 2` + PDB + topology spread because their readiness model is normal.

### Helm release name vs. HelmRelease metadata.name

When Flux's `spec.releaseName` is unset and `spec.targetNamespace` is set, the actual Helm release name defaults to `<targetNamespace>-<metadata.name>`. The chart sees this as `.Release.Name`, so any `app.kubernetes.io/instance: {{ .Release.Name }}` template renders the **long form**.

flux-local's CI diff renderer doesn't replicate this convention — it uses the bare `metadata.name` — so labels in the diff comment can look short-form even when live pods carry the long form. This caused a labelSelector scare during PR #232 review.

Pinning `spec.releaseName` explicitly (PR #233) makes the diff renderer agree with reality. Recommended for any HelmRelease where flux-local diffs matter.

### Traefik `policy/v1beta1` PDB in flux-local diffs

The Traefik chart picks v1 vs v1beta1 via `Capabilities.APIVersions.Has "policy/v1"`. flux-local renders without cluster capabilities → falls back to v1beta1. Live install on K8s ≥ 1.25 renders v1 correctly. Cosmetic only.

### Traefik persistence

The chart enables a 128Mi PVC by default at `/data` (or `/certs` if `persistence.path` is overridden). Because cert-manager handles ACME and there are zero `certResolver` references in the repo, the volume is unused. With `persistence.enabled: false` the volume becomes an `emptyDir` and Traefik can scale across all 3 workers — previously the local-path PVC pinned it to one node.

---

## Intentional singletons (do **not** scale these)

These look like HA gaps but aren't:

- **cloudflared** — each replica creates an independent tunnel connection to Cloudflare's edge. Multi-replica = multi-tunnel; only one service (seerr) uses it; the cluster's primary ingress is via Pangolin/newt.
- **newt** (Pangolin agent) — same architectural pattern as cloudflared. Multi-newt HA is supported by Pangolin server but requires explicit server-side configuration; not the default and not currently set up.
- **tailscale-operator** — research complete (May 2026): the chart does **not** expose `replicaCount`, leader election, PDB, or topologySpreadConstraints. The operator deployment is hardcoded to 1 replica in the chart template, and there's no `Lease` object in the `tailscale` namespace (operator doesn't run leader election by default). Multi-instance without leader election would split-brain on Tailscale-Service reconciliation. Mitigation: existing `ts-*` StatefulSet tunnels keep running while the operator is down — only *new* tunnel creation pauses, and the 30s eviction grace already cuts pod-recreation time from ~5min to ~45s. Don't add a PDB either — `minAvailable: 1` with `replicas: 1` blocks all voluntary drains.
- **home-assistant**, **matter-server**, **esphome** — pinned to `worker-2` for hardware/mDNS reasons. Scaling out requires either RWX state migration or accepting a hostNetwork single-pod design.
- **pocket-bridge** — sidecar Redis holds the per-pod state machine (UUID-fenced locks, `embed_pending` timestamps, source/note ID cache). Two replicas would race the Lua state transitions on a shared Redis OR diverge if Redis stayed sidecar. Multi-replica requires externalizing Redis + rethinking the lease model. Single-replica is fine because crash recovery is handled by §7.8 startup-scan + every-5-min periodic stale-scanner. See [`docs/plans/pocket-to-open-notebook-pipeline.md`](plans/pocket-to-open-notebook-pipeline.md).

---

## What's still single-replica (acknowledged)

- **fleet-controller** — runs leader election; could go to 2-3 with PDB, not addressed yet.
- **rancher-webhook** — managed automatically by Rancher itself, not by Flux. Default 1 replica.
- **kube-prometheus-stack** components — Prometheus is a single replica with persistent storage; clustering it is a Thanos / Mimir conversation, not a quick HA fix.

---

## Verification commands

After any change in this area, the following checks should all pass:

```bash
# All controllers at desired replica count
kubectl get deploy -A | grep -E '(flux-system|cert-manager|kyverno|traefik-system|snapshot-controller-system|metallb-system)'

# All PDBs present
kubectl get pdb -A
# Expect ~12: 3 (flux: kustomize/helm/notification) + 3 (cert-manager) + 4 (kyverno) + 1 (traefik) + 1 (snapshot-controller) + 1 (metallb) + 1 pre-existing (authentik-postgresql) -- source-controller has no PDB by design

# Topology spread is actually spreading pods (no clustering)
for ns in flux-system cert-manager kyverno traefik-system snapshot-controller-system metallb-system; do
  echo "=== $ns ==="
  kubectl get pods -n $ns -o wide --no-headers | awk '{print $7}' | sort | uniq -c
done

# Eviction grace is 30s (cluster-wide)
kubectl run probe --image=busybox --restart=Never --dry-run=server \
  -o jsonpath='{.spec.tolerations[*].tolerationSeconds}' -- sleep 1
# Expect: 30 30
```

---

## Files involved

GitOps changes:
- `clusters/{staging,production}/flux-system/kustomization.yaml` (Flux controller patches)
- `clusters/{staging,production}/flux-system/flux-pdbs.yaml` (Flux PDBs)
- `infrastructure/controllers/cert-manager/release.yaml`
- `infrastructure/controllers/kyverno/kyverno-release.yaml`
- `infrastructure/controllers/traefik-proxy/traefik-values.yaml`
- `infrastructure/controllers/snapshot-controller/snapshot-controller-release.yaml` + `snapshot-controller-pdb.yaml`

Ansible changes:
- `ansible/k3s-cluster/playbooks/k3s_server_config.yml` + `templates/k3s_config.yaml.j2` (eviction grace)
- `ansible/k3s-cluster/playbooks/metallb_install.yml` (metallb HA tasks)
