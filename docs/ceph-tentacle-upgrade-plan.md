# Ceph Squid → Tentacle Upgrade Plan

Planning doc for moving the Proxmox-managed Ceph cluster from
**Squid 19.2.3** to **Tentacle 20.2.x**. Not yet scheduled — this is the
reference to follow when the upgrade is executed.

Sister docs: `ceph-tuning-2026-05-07.md` (config tuning history),
`ceph-scrub-queue-cron.md` (scrub backlog workaround).

## Why upgrade

1. **Dashboard SSO.** Tentacle's ceph-dashboard gains native
   **OAuth2 / OIDC** login (via an oauth2-proxy integration), so the
   dashboard can authenticate against Authentik instead of the legacy
   SAML-only path. This is the primary motivation.
2. **Performance.** Tentacle ships BlueStore / RocksDB refinements and
   mClock scheduler improvements. Given this cluster's slow-op history
   on consumer NVMe (see `ceph-tuning-2026-05-07.md`), these *may* help
   — but treat that as a bonus, not a guaranteed fix.
3. **Scrub scheduler improvements (secondary).** Tentacle introduces an
   OSD-side overdue-scrub queue that promotes the priority of PGs that
   have drifted past their warn threshold, plus an mClock `balanced`
   profile re-tuned to raise the minimum reservation for the scrub QoS
   class. Together these directly target the "PGs never scrub under
   steady client IO" starvation pattern that this cluster works around
   with the `ceph-scrub-queue-cron.md` cron. Also: `ceph pg dump` gains
   a `scrub_schedule` column and `ceph health detail` for
   `PG_NOT_DEEP_SCRUBBED` reports *why* a PG missed (load threshold,
   `osd_max_scrubs` full, reservation denied, …). On Tentacle the cron
   will likely be redundant — keep it ~30 d post-upgrade to verify,
   then retire.

There is **no urgency**: Squid is a supported Proxmox Ceph release until
roughly **September 2026**. Do this deliberately, in its own maintenance
window, after the current Ceph/CSI/dumb work has settled.

## Current state (verified 2026-05-22)

| Component | Version | Notes |
|---|---|---|
| Proxmox VE | `pve-manager/9.1.16` | all 3 hosts (mammoth, whistler, zermatt) |
| Ceph | `19.2.3-pve4` (Squid) | all daemons; `pveceph`-managed |
| Kernel | `6.17.13-6-pve` | |
| ceph-csi | `v3.17.0` | bumped 2026-05-26 (PR #626) — Tentacle base image + Tentacle CI |
| snapshot-controller | `v8.5.0` (chart 5.0.4) | bumped 2026-05-26 (PRs #631/#632); upstream piraeus source |
| csi-snapshotter sidecar (in cephcsi pods) | `v8.5.0` | aligned with controller (PR #625) |
| csi-attacher sidecar | `v4.12.0` | bumped 2026-05-26 (PR #623) |
| csi-node-driver-registrar | `v2.17.0` | bumped 2026-05-26 (PR #625) |

**Prerequisites for the Squid → Tentacle Ceph upgrade are already met:**
the Proxmox `Ceph_Squid_to_Tentacle` procedure requires `pve-manager`
≥ 9.1.4 and Ceph ≥ `19.2.3-pve3`; the cluster is on 9.1.16 / 19.2.3-pve4.

So this is **not** a large PVE 8→9 jump. It is two light, independent
steps.

## Phase 1 — (optional) PVE 9.1 → 9.2

Proxmox VE **9.2** (released 2026-05-21) ships both Ceph Squid 19.2.3 and
Tentacle 20.2.1, and makes Tentacle the default for fresh installs. The
Ceph upgrade does **not** strictly require 9.2 — 9.1.16 already satisfies
the minimums — but bringing PVE current first is good hygiene.

This is an ordinary PVE point upgrade, one node at a time:

```bash
# Per host, rolling. Migrate/shut guests off the node first if desired.
apt update
apt dist-upgrade
# reboot if a new kernel landed
```

Verify `pveversion` reads 9.2.x on all three hosts before Phase 2.
Re-check the official upgrade notes at the time — there is no PVE 9.1→9.2
wiki article assumed here; it is a normal `apt` point upgrade.

## Phase 2 — Ceph Squid → Tentacle

Follow the official procedure: **<https://pve.proxmox.com/wiki/Ceph_Squid_to_Tentacle>**
(re-read it at execution time — the steps below are a summary, not a
substitute).

Pre-flight:

- [ ] Cluster `HEALTH_OK`, all PGs `active+clean`, no `BLUESTORE_SLOW_OP`
      alert, scrub backlog drained (queue overdue PGs first).
- [ ] All 3 mons in quorum, all 6 OSDs `up`/`in`.
- [ ] A current off-cluster backup checkpoint exists (Kopia layer on
      pve-ugreen + CNPG/volsync).
- [ ] `noout` set for the duration: `ceph osd set noout`.

Outline:

1. On each host, switch the Proxmox Ceph repo from `ceph-squid` to
   `ceph-tentacle` (no-subscription variant), then `apt update`.
2. `apt dist-upgrade` to pull Tentacle packages — does **not** restart
   running daemons yet.
3. Restart daemons **in order**, one at a time, waiting for health
   between each: **mons → mgrs → OSDs → MDS**. Restart OSDs host-by-host.
4. After all daemons report the Tentacle version
   (`ceph versions`), unset `noout` and clear any
   `require-osd-release` gate per the wiki.
5. `ceph osd require-osd-release tentacle` once all OSDs are upgraded.

Expected downtime: none for clients if daemons are cycled one at a time
and `min_size 2` holds (the cluster is `size 3 / min_size 2`).

## Caveats — must check before/after

- **ceph-csi `readAffinity` + Tentacle data-loss bug — RESOLVED in v3.17.**
  ceph-csi v3.16.2 release notes warned that **Tentacle v20.2.0** could
  cause CephFS data loss when `readAffinity` is enabled. The underlying
  issue ([ceph-csi #5772](https://github.com/ceph/ceph-csi/issues/5772))
  was fixed during the v3.17 dev cycle; this cluster is on v3.17.0 since
  2026-05-26. As belt-and-suspenders, `readAffinity` is **not** enabled
  here — keep it that way and target Tentacle **≥ 20.2.1** if ever
  turning it on.
- **ceph-csi compatibility — Tentacle-ready since 2026-05-26.** v3.17.0
  is the first cephcsi release built on the Tentacle base image with
  Tentacle CI coverage (upstream PRs #5856 base-image switch, #5672
  Tentacle Rook CI). Squid (the current cluster daemon version) remains
  fully supported in v3.17 per upstream README. No CSI change needed
  when the daemons flip. (Separately, ceph-csi 3.17+ recommends the
  Ceph-CSI-Operator deployment model over raw manifests; that is its
  own future migration, unrelated to this upgrade.)
- **mClock / tuning.** The per-OSD `osd_mclock_max_capacity_iops_ssd`
  caps and `osd_memory_target=6 GiB` from the May 2026 tuning pass carry
  forward as plain config — but re-validate slow-op behaviour after the
  upgrade; Tentacle's scheduler changes may shift the picture.

## Phase 3 — Dashboard SSO (the payoff)

After the cluster is on Tentacle and healthy, configure the ceph-dashboard
OAuth2/OIDC integration against Authentik. This is a dashboard-only change
with no storage risk; details to be worked out at the time against the
Tentacle dashboard docs and the existing Authentik provider setup.

## Rollback posture

Daemon-by-daemon upgrades are not trivially reversible once
`require-osd-release tentacle` is set. The real safety net is the
off-cluster backup checkpoint (pre-flight item above). Do not start
Phase 2 without it. If a daemon fails to come up on Tentacle, stop,
leave the rest on Squid (mixed-version runs briefly during the upgrade
anyway), and diagnose before proceeding.

## Status

- **2026-06-03** — **Phase 3 (dashboard SSO) COMPLETE.** ceph-dashboard
  OAuth2/OIDC SSO against Authentik via a self-hosted **oauth2-proxy**
  (manifests in [`infrastructure/controllers/ceph-dashboard/`](../infrastructure/controllers/ceph-dashboard/),
  PRs #719/#720). Native OAuth2 (`ceph orch apply mgmt-gateway`) needs
  cephadm — unavailable on pveceph — and SAML hits a `python3-xmlsec`/
  `libxml2` mismatch that crashes the mgr, so oauth2-proxy injects the
  id_token as `X-Access-Token` for the dashboard's oauth2 mode. Object
  Gateway dashboard feature disabled (`ceph dashboard feature disable rgw`)
  — no RGW on this cluster by design (S3 = Garage).

  > ### ⚠️ Host-level dashboard patches — NOT in git, reverted by upgrades
  > Two `ceph-mgr-dashboard` files are patched **directly on all 3 mgr
  > hosts** (`pve-mammoth`, `pve-whistler`, `pve-zermatt`) to make the
  > Tentacle dashboard work. **The next `ceph-mgr-dashboard` package
  > upgrade WILL overwrite them** (also when the upstream Proxmox fixes
  > likely land). After any such upgrade, re-apply both and restart the
  > active mgr:
  >
  > 1. **`controllers/smb.py`** — Proxmox ships the dashboard's `smb`
  >    controller but not the `smb` mgr module it imports →
  >    `ModuleNotFoundError: No module named 'smb'` crashes the whole
  >    dashboard. Fix: `mv .../dashboard/controllers/smb.py{,.disabled-tentacle-smb-bug}`
  >    + clear `__pycache__/smb.*`.
  > 2. **`services/auth/auth.py`** (~line 168) — assumes every token has a
  >    `sub` claim in oauth2 mode; the dashboard's own session JWTs / the
  >    local `admin` user lack it → `KeyError: 'sub'` → intermittent 500 on
  >    `/ui-api/motd`. Fix: `decoded_message['username'] = decoded_message.get('sub') or decoded_message.get('username')`.
  >
  > Originals are kept beside each file (`*.bak-tentacle-sub-bug`,
  > `*.disabled-tentacle-smb-bug`). Also requires `python3-jmespath` on the
  > mgr hosts (for the oauth2 `roles_path`).

- **2026-06-02** — **Phase 2 COMPLETE. Cluster is on Tentacle 20.2.1-pve1,
  `HEALTH_OK`.** All 17 daemons upgraded (3 mon, 3 mgr, 6 osd, 5 mds),
  `min_mon_release 20 (tentacle)`, `require_osd_release tentacle` set,
  273 PGs `active+clean`, `noout` cleared. No client downtime; brief
  per-fs MDS failover only. Procedure followed the official wiki
  (repo flip `ceph-squid`→`ceph-tentacle` in `/etc/apt/sources.list.d/ceph.sources`
  → `apt full-upgrade` → restart mons→mgrs→OSDs(host-by-host)→MDS).
  - **Correction to "Current state" table below: the Ceph cluster spans
    FIVE hosts, not three.** Beyond the 3 OSD/mon hosts (mammoth,
    whistler, zermatt) there are two MDS-only PVE cluster members that
    also needed the repo flip + `apt full-upgrade`: **pve-mac**
    (192.168.1.250, k3s-fs standby-replay) and **pve-s13**
    (192.168.1.20, plain standby; nodeid 1, the cluster's founding
    node). pve-ugreen (192.168.1.251) is in the PVE cluster but runs no
    ceph daemons — left untouched. Both pve-mac and pve-s13 had inert
    pending kernel/pve-manager updates that were NOT applied to keep the
    window ceph-only (pve-mac still pending a 6.17 kernel + 9.2.2→9.2.3;
    pve-s13 was already 9.2.3 and pulled ceph-only).
  - **MDS handling:** both filesystems already at `max_mds 1`, so no rank
    reduction — only disable `allow_standby_replay` → cycle standbys →
    cycle each active (failover to a hot standby) → re-enable
    `allow_standby_replay`. All on-version (every MDS host pre-upgraded),
    so no cross-version-failover risk.
  - **Post-upgrade validation:** `HEALTH_OK`, no new crashes, all 195 app
    pods Running, no `FailedMount` events, rbd + cephfs provisioners
    clean, all PVCs `Bound`, k3s-fs serving ~300 reqs/s immediately after
    failover. ceph-csi unchanged (v3.17.0, already Tentacle-aware).
  - **Unrelated pre-existing finding (NOT caused by this upgrade):** a
    leaked cephfs `VolumeAttachment`
    (`csi-8a0f6be4…`, PV `pvc-6d82a04f-…`, node `mainertoo-k3s-worker-3`)
    has been stuck terminating since 2026-01-26 — the external-attacher
    finalizer loops every 5 min trying to detach an already-deleted PV.
    Harmless log spam; safe cleanup = strip the finalizer from the VA.
  - **Remaining: Phase 3** (dashboard OAuth2/OIDC against Authentik) — not
    yet started; dashboard-only, no storage risk.
- **2026-05-22** — Plan written. Prerequisites confirmed already met on
  PVE 9.1.16 / Ceph 19.2.3-pve4. Not scheduled; deferred until the
  Ceph/CSI/dumb work settles. Target: before Squid EOL (~Sept 2026).
- **2026-05-26** — **CSI client prereq complete.** cephcsi v3.16.2 →
  v3.17.0 across cephfs/rbd/nfs manifests (PR #626 + `pods` RBAC
  follow-up #627). Sidecars and snapshot-controller all aligned at
  v8.5.0 (PRs #623, #625, #631, #632). One side-quest (cephfs PV
  controller sidecar, PR #628) reverted as incompatible with the
  label-driven `backingSnapshot` ROX pattern — see saved memory
  `feedback_cephfs_backingsnapshot_incompatible_with_pv_controller`.
  Cluster's CSI stack is now Tentacle-aware against the still-Squid
  daemons; daemon upgrade still unscheduled.
