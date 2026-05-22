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

There is **no urgency**: Squid is a supported Proxmox Ceph release until
roughly **September 2026**. Do this deliberately, in its own maintenance
window, after the current Ceph/CSI/dumb work has settled.

## Current state (verified 2026-05-22)

| Component | Version | Notes |
|---|---|---|
| Proxmox VE | `pve-manager/9.1.16` | all 3 hosts (mammoth, whistler, zermatt) |
| Ceph | `19.2.3-pve4` (Squid) | all daemons; `pveceph`-managed |
| Kernel | `6.17.13-6-pve` | |
| ceph-csi | `v3.16.2` | pinned, PR #559 — supports Pacific+ incl. Tentacle |

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

- **ceph-csi `readAffinity` + Tentacle data-loss bug.** ceph-csi v3.16.2
  release notes warn that **Tentacle v20.2.0** could cause CephFS data
  loss when `readAffinity` is enabled. This cluster does **not** enable
  `readAffinity` today — keep it that way until confirmed fixed.
  Target Tentacle **≥ 20.2.1** and re-read the ceph-csi notes before
  ever turning `readAffinity` on.
- **ceph-csi compatibility.** v3.16.2 supports Ceph "Pacific and above",
  so Tentacle 20.2.x is covered — no CSI change needed for the upgrade
  itself. (Separately, ceph-csi 3.16+ recommends the Ceph-CSI-Operator
  deployment model over raw manifests; that is its own future migration,
  unrelated to this upgrade.)
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

- **2026-05-22** — Plan written. Prerequisites confirmed already met on
  PVE 9.1.16 / Ceph 19.2.3-pve4. Not scheduled; deferred until the
  Ceph/CSI/dumb work settles. Target: before Squid EOL (~Sept 2026).
