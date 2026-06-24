# Multus + Home Assistant failover runbook (staging-first)

> ⏸ **PAUSED 2026-06-24 for a UPS power update (graceful shutdown of all cluster nodes).**
> All manifests are committed + INERT (nothing reconciles). **RESUME:** after the cluster is back
> and verified healthy (nodes Ready, `flux get all -A` reconciled, ceph `HEALTH_OK`), merge PR
> #1020, then run **Phase 1 activation** below: back up `10-flannel.conflist` per node → ready the
> node-level rollback → uncomment the `infra-multus` block in
> `clusters/staging/infrastructure.yaml` → verify `00-multus.conf` delegates to flannel + a fresh
> pod still networks.

**Status:** PLAN — not yet executed (PAUSED, see above). Design validated across three adversarial
Codex passes (incl. a review of the real manifests); IP plan resolved + reservations applied;
staging manifests built (rke2-multus v4.2.418) and inert.
**Goal:** let Home Assistant + matter-server survive a worker-2 node failure by giving HA a
**node-independent LAN IP** via a Multus macvlan interface (dropping `hostNetwork`), proven on
staging before touching production.

## Why this, and why carefully

- HA runs `hostNetwork: true`, so it holds **worker-2's** LAN IP. matter-server is the same
  (`--primary-interface lan0`). Both pinned to worker-2 via `nodeSelector`.
- Failover prereqs ARE met: PVCs are **ceph-rbd**; **all three workers have a live `lan0` on
  VLAN 1** (verified: worker-1 `.246`, worker-2 `.247`, worker-3 `.248`). The only real blocker
  was HA's **node-bound IP** — integrations that hit HA by raw IP break if the IP changes.
- **Prior incident:** a previous Multus install destabilized the cluster (considered a rebuild).
  Multus inserts into the CNI chain for *every* pod → prove it on staging, treat prod as a CNI
  change in a maintenance window.

## IP plan (RESOLVED + partially applied)

- HA's current `.247` is *worker-2's* node IP (HA only borrows it via hostNetwork); a macvlan
  child cannot reuse its parent's IP, and "use the node's IP" means HA's IP changes every
  failover. So HA needs its **own** reserved address.
- **APPLIED already** (UniFi DHCP reservations, VLAN 1):
  - `k3s-worker-1-lan0` `bc:24:11:66:09:38` → `.246`
  - `k3s-worker-2-lan0` `bc:24:11:c9:0c:4a` → `.247`
  - `k3s-worker-3-lan0` `bc:24:11:85:70:90` → `.248`
  - `home-assistant-macvlan` (pinned MAC **`02:00:00:00:02:44`**) → **`.244`** (held)
- **TODO at cutover:** repoint the handful of integrations that reach HA by raw IP `.247 → .244`.
  (Enumerate them in pre-work.)

## ⚠️ K3s-specific facts this runbook is built around

K3s does NOT use standard CNI locations. Verify per node; defaults:
- **CNI config dir:** `/var/lib/rancher/k3s/agent/etc/cni/net.d/` (flannel ships
  `10-flannel.conflist`). NOT `/etc/cni/net.d`.
- **CNI binary dir (durable):** `/var/lib/rancher/k3s/data/cni/`. Do NOT target
  `…/data/current/bin` — `current` is a per-version symlink and binaries there vanish on a K3s
  upgrade (K3s issue #10869). The Multus DaemonSet's `--cni-bin-dir` + hostPath must point at the
  durable `data/cni` path.
- **K3s auto-manifest dir:** `/var/lib/rancher/k3s/server/manifests/` auto-applies AND
  auto-deletes its contents. **Do NOT deploy Multus there** — it would fight Flux. Multus is
  Flux-owned only.
- The Multus DaemonSet must set `--cni-conf-dir=/var/lib/rancher/k3s/agent/etc/cni/net.d` and
  `--cni-bin-dir=/var/lib/rancher/k3s/data/cni` with matching hostPath mounts, and chain flannel
  as the default delegate (see Phase 1).

## Pre-work (preflight evidence — capture before EACH phase)

- [ ] `flux get all -A` clean; `kubectl get nodes` + `get pods -A` healthy.
- [ ] Enumerate the integrations/devices that reach HA at `.247` (these get repointed to `.244`).
- [ ] On each node, inventory `/var/lib/rancher/k3s/agent/etc/cni/net.d/` + `…/data/cni/`;
      **back up `10-flannel.conflist`** off-node.
- [ ] Render the exact Multus DaemonSet (`flux build`); confirm hostPaths = the K3s dirs above.
- [ ] Prepare + review the **revert commit** before applying the forward commit.

## Phase 0 — Staging `lan0` + L2 validation (hard gate for Phase 2)

Staging TF does NOT set `worker_extra_nic` → staging workers have no `lan0`.
- [ ] Add `worker_extra_nic_enabled = true` (+ bridge/vlan mirroring prod) to staging TF; apply.
- [ ] Verify `lan0` up on **both** staging workers with the expected VLAN/addressing/MTU.
- [ ] **L2 validation (new):** macvlan emits an *additional* MAC per pod. Confirm the **Proxmox
      bridge** permits it (no VM-NIC MAC filter / `disable MAC learning`) and the **upstream
      switch** port has no MAC-limit/port-security that would drop it. This must pass on staging
      before the staging result is treated as production-representative.

## Phase 1 — Install Multus on **staging** (own Flux Kustomization, health-gated)

Use the **K3s-official `rke2-multus` Helm chart** (NOT a hand-vendored daemonset) — it sets the
K3s CNI paths and auto-detects the existing flannel config as the default delegate, which is the
supported path on K3s. Cluster is `v1.35.5+k3s1` (prod + staging), past the Oct-2024 cutoff, so
the fixed bin dir `/var/lib/rancher/k3s/data/cni` is correct.
1. Add the `rke2-charts` `HelmRepository` (`https://rke2-charts.rancher.io`) and a **pinned**
   `rke2-multus` `HelmRelease` in `infrastructure/controllers-staging/multus/`, values:
   - `config.cni_conf.confDir: /var/lib/rancher/k3s/agent/etc/cni/net.d`
   - `config.cni_conf.binDir: /var/lib/rancher/k3s/data/cni/`
   - `config.cni_conf.multusAutoconfigDir: /var/lib/rancher/k3s/agent/etc/cni/net.d`
     (this is the **flannel-chaining** mechanism — Multus auto-reads the existing
     `10-flannel.conflist` as the default delegate; no manual `00-multus.conf` to maintain).
   - A dedicated Flux `Kustomization`, `wait: true` + `healthChecks` on the multus DaemonSet. Not
     bundled with any app change.
2. **Activation gate (the `multusConfFile: auto` race):** before/at activation, confirm
   `10-flannel.conflist` already exists on **every** node. `auto` generates `00-multus.conf` from
   whatever default CNI config it finds; if it runs before flannel's conflist is present it can
   produce a delegate with no/wrong source. After the DaemonSet rolls, **inspect the generated
   `00-multus.conf` on each node** and confirm its `delegates` point at flannel (not an empty or
   fallback delegate) before trusting it.
3. **Verify:** DaemonSet Ready on all 3 staging nodes; `00-multus.conf` present + delegating to
   flannel; flannel conflist intact; **every** pod still networks (prove via a **freshly created**
   test pod: pod-to-pod + CoreDNS).
4. **Rollback:** node-level (see Rollback) — removing the DaemonSet alone is NOT sufficient.

## Phase 2 — Static-IP macvlan NAD + test pod on **staging**

Entry gate: Phase 0 (incl. L2 validation) + Phase 1 clean.
1. macvlan NAD: bridge mode, parent `lan0`, **`"ipam": { "type": "static" }`** (no host-local
   range). IP supplied per-pod via the `k8s.v1.cni.cncf.io/networks` annotation (deterministic
   across nodes).
2. Test pod (`netshoot`), **no** `hostNetwork`, with `nodeSelector`/affinity constraining it to a
   `lan0`-equipped worker (so it can't land on a node without `lan0` and fail for the wrong
   reason), annotation pinning a test MAC + a free test IP.
3. **Verify:** pod gets `net1` + the exact IP; pings gateway + LAN host; mDNS works; delete +
   reschedule to the **other** `lan0` worker → **same IP**; confirm clean IP release after delete.

## Phase 3 — HA-pattern validation on **staging**

1. Stand-in HA-like pod with the static-IP macvlan NAD (pinned MAC + IP), **no** `hostNetwork`.
2. **macvlan↔host isolation matrix** (child can't reach its own parent host) — from the pod test:
   own node IP; kube API/VIP; a ClusterIP service whose endpoint is **on the same node**; ingress
   health paths; any node-local dependency. Any required same-node path that fails = design blocker.
3. **Verify:** holds IP across a forced reschedule; LAN mDNS works; Service/IngressRoute routes to it.
4. Proceed only if Phases 0–3 are clean with ZERO pod-networking regressions.

## Phase 4 — Promote to **production** (maintenance window, TWO commits)

**Commit A — Multus only** in `infrastructure/controllers/multus/` (same pinned **chart version**
— `rke2-multus v4.2.418`, which pins the image build-tags; full digest-pinning is optional
hardening, not required — and the same K3s dirs). Verify prod pods keep networking immediately
(fresh-pod test per worker).

**Commit B — HA migration** (only after Multus is observed Ready on every eligible worker; the HA
+ matter-server Kustomizations gain `dependsOn` the Multus Kustomization):
- **HA:** drop `hostNetwork`/`ClusterFirstWithHostNet`; attach `lan-macvlan` (static IPAM) with
  `mac: 02:00:00:00:02:44`, `ips: [192.168.1.244/24]`. Repoint the raw-IP integrations `.247 → .244`.
  - **(blocker c) `strategy: Recreate`** on the HA controller. With a pinned MAC + static IP, a
    rolling update would briefly run two pods both claiming `.244`/`02:00:00:00:02:44` → ARP/IP
    split-brain. Recreate guarantees the old pod is gone before the new one starts.
  - **(blocker k) two-level affinity, not "preferred + allow elsewhere":** **REQUIRED = a
    `lan0`-equipped node, PREFERRED = worker-2.** `lan0` exists only on workers, never masters —
    without the *required* constraint HA could schedule to a master, where the macvlan CNI call
    fails and the pod hangs. Label the three workers `node.mainertoo/lan0=true` and make a
    `requiredDuringScheduling` nodeAffinity on that label (more explicit/robust than relying on the
    absence of the control-plane role), plus a `preferred` weight on worker-2.
- **matter-server (stays as-is, by design):** it needs **no** stable IP — HA reaches it via the
  ClusterIP service and it is the *initiator* toward Matter devices (devices don't need a fixed
  controller IP), with state in its ceph-rbd PVC. So keep `hostNetwork` + `--primary-interface
  lan0`. It ALSO needs `lan0`, so give it the **same REQUIRED `node.mainertoo/lan0=true` +
  PREFERRED worker-2** affinity (not a bare "relax"). Its LAN IP changes on failover — harmless.
  (Moving it to Multus too is optional, not required.)

**Verify (named acceptance):** HA reachable at **`.244`** from an external LAN host; each
enumerated raw-IP integration works; HomeKit bridge advertises; a known mDNS service is discovered
by HA; Matter nodes 5/6/22 `available=True`.

## Failover test + ceph-rbd RWO reality

Both HA + matter-server use **RWO ceph-rbd**. Behavior differs by failure type — test BOTH:
- **Graceful (planned drain):** `kubectl drain worker-2 --ignore-daemonsets --delete-emptydir-data`
  (note abort criteria if a PDB/DaemonSet blocks). Pod terminates → RBD detaches cleanly →
  reattaches on the new node. Acceptance: new pod on a **different** worker, volume `Attached`,
  mount succeeds, app Ready, HA reachable at `.244` from an external host, integration checklist
  passes. `uncordon` after.
- **Ungraceful (node crash):** an RWO RBD stays attached to the dead node → the replacement pod
  hangs **`Multi-Attach`** until the volume is force-detached. k8s recovers this only once the dead
  node carries the out-of-service taint. **Exact recovery step** (only after the node is confirmed
  truly dead — applying this to a live node causes data corruption):
  `kubectl taint node <dead-node> node.kubernetes.io/out-of-service=nodeshutdown:NoExecute` (add a
  matching `:NoSchedule` taint too). Verify: the old `VolumeAttachment` is deleted
  (`kubectl get volumeattachment | grep <pvc>`), the pod leaves `Multi-Attach`/`ContainerCreating`,
  RBD reattaches, app Ready. **Remove the taint** once the node is recovered. **Document this**:
  true-crash failover is NOT fully automatic without that taint — so the realistic win is fast recovery on
  *planned* maintenance + a known, short manual step on an unplanned crash. (No `fsGroup` is set on
  HA today; verify cross-node mount ownership during the graceful test and add `fsGroup` +
  `fsGroupChangePolicy: OnRootMismatch` if ownership is wrong.)

## Rollback (CNI-incident grade)

- **App-level (HA migration):** revert Commit B → Flux reconcile → HA/matter-server back to
  `hostNetwork` + hard `nodeSelector`; integrations back to `.247`.
- **CNI-level (Multus rollout failed), per node, ordered:**
  1. Remove the Multus Flux Kustomization (DaemonSet + RBAC) — and confirm the DaemonSet pod is
     gone on the node before touching configs, so it can't recreate `00-multus.conf`.
  2. Restore backed-up `10-flannel.conflist` as the **only** conf in
     `/var/lib/rancher/k3s/agent/etc/cni/net.d/`; remove `00-multus.conf`.
  3. `systemctl restart k3s-agent` (workers) / `k3s` (server).
  4. Prove recovery: a **freshly scheduled** pod gets an IP + DNS.
- **API inaccessible:** recover nodes individually over SSH (servers first) with the steps above;
  keep flannel-conf backups + console access ready before starting.

## Success criteria

- **Staging:** Multus installed, no pod-networking regression; static-IP macvlan pod keeps its IP
  across a node move + does mDNS; L2 (MAC) validation passed; host-isolation matrix clean.
- **Prod:** HA at **`.244`**, enumerated raw-IP integrations + HomeKit + mDNS working, Matter
  working; **graceful** drain of worker-2 → auto-recovery with IP preserved (verified from an
  external host); **ungraceful** path documented + the `out-of-service` step rehearsed.

## Notes

- ISP IPv6 / dual-stack: not enabled (WAN `wan_type_v6 = disabled`); unrelated.
- This is **active-passive auto-recovery** (~1–2 min) for planned moves; an unplanned crash needs
  the `out-of-service` taint step (above). Not zero-downtime — HA is single-instance stateful.
- Cross-ref: `apps/base/home-assistant/matter-server/matter-server-release.yaml`
  (`--primary-interface lan0`, `--log-level-sdk none`).
