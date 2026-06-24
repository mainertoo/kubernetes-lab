# Multus + Home Assistant failover runbook (staging-first)

**Status:** PLAN — not yet executed. Revised after an adversarial Codex review (4 blockers +
4 highs folded in). Validate on staging, re-review, then implement.
**Goal:** let Home Assistant + matter-server survive a worker-2 node failure by giving HA a
**node-independent LAN IP** via a Multus macvlan interface (dropping `hostNetwork`), proven on
staging before touching production.

## Why this, and why carefully

- HA runs `hostNetwork: true`, so it holds **worker-2's** LAN IP (`lan0` / VLAN 1). matter-server
  is the same (`--primary-interface lan0`). Both pinned to worker-2 via `nodeSelector`.
- Other failover prerequisites are already met: PVCs are **ceph-rbd** (attach on any node) and
  `worker_extra_nic` (`lan0`) exists on **every prod worker**. The only real blocker is HA's
  **node-bound IP** — anything addressing HA by raw IP (webhooks, push integrations, the
  HomeKit bridge advertising HA's address) breaks if its IP changes.
- **Prior incident:** a previous Multus attempt destabilized the cluster badly enough to
  consider rebuilding from backup. Multus inserts into the CNI chain for *every* pod. The
  install is the dangerous step → **prove it on staging first**, and treat the prod step as a
  CNI change in a maintenance window, not a routine deploy.

## ⚠️ K3s-specific facts this runbook is built around

K3s does **not** use the standard CNI locations. Verify on each node during pre-work; defaults:
- **CNI config dir:** `/var/lib/rancher/k3s/agent/etc/cni/net.d/` (flannel ships
  `10-flannel.conflist` here). **NOT** `/etc/cni/net.d`.
- **CNI binary dir:** `/var/lib/rancher/k3s/data/current/bin/` (symlink to the active data dir).
- **K3s auto-manifest dir:** `/var/lib/rancher/k3s/server/manifests/` — K3s **auto-applies and
  auto-deletes** anything here. **Do NOT deploy Multus via this dir** — it would fight Flux and
  leave CNI in an undefined state. Multus is owned by Flux only.
- The Multus DaemonSet **must** be configured to read/write the K3s CNI conf+bin dirs above
  (via its `--cni-conf-dir` / `--cni-bin-dir` and matching hostPath mounts). Writing to the
  default dirs is the #1 way to silently half-install Multus on K3s.

## Red flags resolved in this revision (from Codex review)

1. CNI paths corrected to the K3s locations everywhere (was the most dangerous gap).
2. Static IP is now an explicit **`type: static`** IPAM requirement, not the inert host-local
   range NAD (which is per-node and would hand HA a different IP after a move).
3. Adding `lan0` to **staging** workers is now a **hard Phase-2 entry gate**, not a "decide later."
4. Multus is a **separate, health-gated Flux Kustomization** that the HA migration `dependsOn`,
   so a single commit can't race Multus startup against HA pod scheduling.
5. One pinned install method (vendored manifests as a Flux Kustomization, version+digest).
6. macvlan↔host isolation is now an executable test matrix.
7. mDNS / Matter acceptance are named real-device tests, not "mDNS works."
8. Rollback now has node-level K3s recovery steps + an API-inaccessible escalation path.

## Pre-work (preflight evidence — capture before EACH phase)

- [ ] `flux get all -A` clean; `kubectl get nodes` + `kubectl get pods -A` all healthy.
- [ ] Record HA + matter-server `hostNetwork`, exact LAN IPs, `nodeSelector`, PVC storage classes.
- [ ] **Identify HA's current LAN IP** (the one integrations use = worker-2 `lan0`, ~`192.168.1.247`)
      and the explicit list of integrations/devices that reach HA by raw IP. This IP is what we pin.
- [ ] On each node: inventory `/var/lib/rancher/k3s/agent/etc/cni/net.d/` and the CNI bin dir;
      **back up `10-flannel.conflist`** (and any siblings) off-node.
- [ ] Render the exact Multus DaemonSet that will be applied (`flux build` / `kustomize build`),
      confirm its hostPath mounts point at the K3s CNI dirs, and review it.
- [ ] Prepare (and peer/Codex-review) the **revert commit** before applying the forward commit.

## Phase 0 — Provision `lan0` on staging (hard gate for Phase 2)

Staging TF (`terraform/environments/staging`) does **not** set `worker_extra_nic`, so staging
workers have no `lan0`. Without it, staging cannot test the production macvlan parent.
- [ ] Add `worker_extra_nic_enabled = true` (+ bridge/vlan to mirror prod) to staging TF; apply.
- [ ] Verify `lan0` is up on **both** staging workers with the expected VLAN/addressing/MTU.
- Acceptance: testing on the staging primary NIC is *supplemental only* and never substitutes
  for `lan0` validation.

## Phase 1 — Install Multus on **staging** (own Flux Kustomization, health-gated)

1. Vendor the Multus thick-plugin manifests into `infrastructure/controllers-staging/multus/`
   (pinned version **and digest**), configured for the **K3s CNI conf+bin dirs**, with flannel
   as the default delegate. Add a dedicated Flux Kustomization with `wait: true` + `healthChecks`
   on the Multus DaemonSet. Do **not** bundle it with any app change.
2. **Verify (the whole point):**
   - Multus DaemonSet Ready on **all 3** staging nodes.
   - `/var/lib/rancher/k3s/agent/etc/cni/net.d/` shows Multus as primary delegating to flannel;
     flannel conflist intact.
   - **Every** pod still networks: `kubectl get pods -A` all Running; from a **freshly created**
     test pod, prove pod-to-pod connectivity + CoreDNS resolution (a fresh pod is what catches a
     broken CNI; existing pods keep their old netns).
3. **Rollback (node-level, see Rollback section):** removing the DaemonSet is **not** sufficient
   by itself — restore the flannel conflist from backup as primary and restart `k3s-agent` per
   node, then prove a fresh pod schedules + networks.

## Phase 2 — macvlan NAD (static IPAM) + throwaway test pod on **staging**

Entry gate: Phase 0 (`lan0` on staging) + Phase 1 complete and clean.
1. Create a macvlan NAD with **`"ipam": { "type": "static" }`** (no host-local range), bridge
   mode, parent = `lan0`. The IP is supplied per-pod via the `k8s.v1.cni.cncf.io/networks`
   annotation `ips: ["192.168.1.<test>/24"]` — deterministic across nodes by construction.
2. Deploy a `netshoot` test pod with that annotation, **no** `hostNetwork`.
3. **Verify:** pod gets `net1` + the exact requested IP; pings gateway + a LAN host; mDNS works
   (`avahi-browse`). Delete + reschedule onto the **other** staging worker → **same IP**, still
   reachable. Confirm clean IP release (no leak) after deletion.
4. **Rollback:** delete test pod + NAD (Multus stays).

## Phase 3 — HA-pattern validation on **staging**

1. Stand-in HA-like pod (or HA on staging if acceptable) with the static-IP macvlan NAD, **no**
   `hostNetwork`.
2. **Host-isolation test matrix (macvlan child cannot reach its own parent host):** from the pod,
   test → its own node IP; the kube API / VIP; a ClusterIP service whose endpoint is **on the
   same node**; ingress health paths; any node-local dependency. Record expected source interface
   + pass/fail for each. Any required same-node path that fails is a design blocker.
3. **Verify:** holds its IP across a forced reschedule; LAN mDNS discovery works; Service /
   IngressRoute still routes to it.
4. Proceed to prod only if Phases 0–3 are clean with **zero** pod-networking regressions.

## Phase 4 — Promote to **production** (maintenance window, two separate commits)

**Commit A — Multus only:** add the same pinned Multus to `infrastructure/controllers/multus/`
as its own health-gated Flux Kustomization.
- Verify prod pods keep networking immediately after the DaemonSet rolls (fresh-pod test on each
  worker). This is the highest-risk moment — rollback ready.

**Commit B — HA migration (only after Multus is observed Ready on every eligible worker):** the
HA Kustomization (and matter-server) gains `dependsOn` the Multus Kustomization.
- HA: drop `hostNetwork`/`ClusterFirstWithHostNet`; attach `lan-macvlan` (static IPAM) with
  `ips: [<HA's current IP>/24]`; `nodeSelector` → **preferred** node-affinity (prefer worker-2,
  allow elsewhere).
- matter-server: needs no stable IP (reached via ClusterIP, talks out via `lan0`), so simplest is
  to **keep it as-is but relax its `nodeSelector`** → preferred affinity. Keep `--primary-interface`
  aligned with whatever interface carries its LAN/mDNS.

**Verify (named acceptance tests):**
- HA reachable at the **same** IP from an external LAN host (not just in-cluster).
- A named real device/integration that pushes to HA by IP still works; HomeKit bridge still
  advertises; a known mDNS service is discovered by HA.
- Matter nodes 5/6/22 `available=True` after HA + matter-server land; a controlled commissioning
  test succeeds if safely available.
- **Failover test:** `kubectl drain worker-2 --ignore-daemonsets --delete-emptydir-data` (note
  abort criteria if PDB/DaemonSet blocks); confirm HA + matter-server rescheduled to a
  **different** worker; verify HA's preserved IP from an external host; run the integration
  checklist; `uncordon` after.

## Rollback (CNI-incident grade)

- **App-level (HA migration):** revert Commit B → Flux reconcile → HA/matter-server back to
  `hostNetwork` + hard `nodeSelector`.
- **CNI-level (Multus rollout failed):** ordered, per node:
  1. Remove the Multus Flux Kustomization (DaemonSet + RBAC).
  2. On each node, restore the backed-up `10-flannel.conflist` as the **primary/only** conf in
     `/var/lib/rancher/k3s/agent/etc/cni/net.d/`; remove `00-multus.conf`.
  3. `systemctl restart k3s-agent` (workers) / `k3s` (server) per node.
  4. Prove recovery: a **freshly scheduled** pod gets an IP + DNS on that node.
- **API inaccessible escalation:** if the control plane VIP/API is unreachable, recover nodes
  individually over SSH using the steps above (servers first), then re-check `kubectl`. Keep the
  flannel conf backups and node console access available before starting.

## Success criteria

- **Staging:** Multus installed with **no** pod-networking regression; static-IP macvlan pod
  keeps its exact IP across a node move and does mDNS; host-isolation matrix has no required-path
  failures.
- **Prod:** HA on the **same IP**, named raw-IP integrations + HomeKit + mDNS working, Matter
  working; draining worker-2 yields automatic recovery of HA + matter-server with the IP
  preserved, verified from an external LAN host.

## Out of scope / notes

- ISP IPv6 / dual-stack: not enabled (WAN `wan_type_v6 = disabled`); unrelated.
- This is **active-passive auto-recovery** (~1–2 min: reschedule + ceph-rbd reattach + reload),
  not zero-downtime — HA is single-instance stateful.
- Cross-reference: matter-server networking (`--primary-interface lan0`, `--log-level-sdk none`)
  in `apps/base/home-assistant/matter-server/matter-server-release.yaml`.
