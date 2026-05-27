# Ceph Cluster Tuning — 2026-05-07

Operational record of the Ceph cluster slow-op investigation and tuning pass.
Sister doc to `project_ceph_osd_health.md` in the user's auto-memory.

## Cluster baseline

- **Version:** Ceph Squid 19.2.3
- **Hosts:** 3× Minisforum MS-01 (i9-13900H, kernel 6.17.13-6-pve)
- **OSDs:** 6 total, 2 per host, all consumer NVMe
- **Replication:** size 3 / min_size 2, 273 PGs, 26 % used (2.8 TiB / 11 TiB)
- **Cluster network:** TB4 ring on `en05`/`en06`, MTU 65520, OpenFabric routing on `dummy_tb4` /32 IPs (10.100.0.0/16)
- **Public network:** 192.168.99.0/24

### Drive ↔ OSD map

| OSD | Host | Device | Model | Wear | Notes |
|-----|------|--------|-------|------|-------|
| 0 | mammoth | nvme0n1 | Samsung 990 EVO Plus 2TB | 3 % | 60 °C |
| 3 | mammoth | nvme2n1 | Samsung 990 EVO Plus 2TB | 6 % | slow-op alerted |
| 2 | whistler | nvme1n1 | Samsung **990 EVO** (non-Plus) 2TB | 4 % | slow-op alerted, on CPU-defect host |
| 5 | whistler | nvme0n1 | Samsung 990 EVO Plus 2TB | 10 % | clean |
| 1 | zermatt | nvme1n1 | Samsung 990 EVO Plus 2TB | 7 % | clean |
| 4 | zermatt | nvme0n1 | **SPCC M.2 PCIe SSD** | 6 % | 64-68 °C, climbs under load, **replace** |

## Initial state

```
HEALTH_WARN
  - 2 OSD(s) experiencing slow operations in BlueStore (osd.2, osd.3)
  - 11 pgs not deep-scrubbed in time
  - 14 daemons have recently crashed
  - 1 mgr modules have recently crashed
```

## What was done

### Initial cleanup (pre-Phase-0)
- `ceph crash archive-all` — archived 14 daemon crashes + 1 mgr module crash. Most were osd.2 on whistler with daily ~10:30 UTC clockwork pattern, consistent with the now-isolated CPU defect on cores 4-5.
- Manually queued the 11 overdue PGs for deep-scrub via `for pg in ...; do ceph pg deep-scrub $pg; done`.
  - Note: Ceph does **not** auto-schedule PGs once they fall onto the `PG_NOT_DEEP_SCRUBBED` backlog on this cluster. Always queue manually.

### Phase 0 — read-only audit (no changes)

**Drive thermals under sustained load (5× sample, ~30 s):**
osd.2 and osd.3 both stable at **131 °F (55 °C) — not thermally throttling**.
SPCC on zermatt climbed 147→154 °F (64-68 °C) during the same window.
→ Thermal hypothesis for the kv-commit-slow asymmetry between osd.5 (clean) and osd.2/3 (slow events) is **disproven**.

**TB4 ring iperf3 (bidirectional, MTU 65520):**

| Path | Bandwidth | Retransmits |
|------|-----------|-------------|
| mammoth → zermatt | 22.0 Gbps | 244 |
| zermatt → mammoth | 14.8 Gbps | 264 |
| mammoth → whistler | 23.0 Gbps | 246 |
| whistler → mammoth | 15.3 Gbps | 203 |
| whistler ↔ zermatt | 21.6 / 23.3 Gbps | 175 / 182 |

Zero errors / drops on every interface. OpenFabric IS-IS adjacencies stable, no flaps in journal since the May 6 reboot. Asymmetry into mammoth (~15 Gbps) vs out (~22 Gbps) noted but irrelevant — actual Ceph cluster traffic is ~5 MiB/s in `ceph -s`, four orders of magnitude below capacity. Network exonerated.

**Override audit via `ceph daemon osd.3 config diff`:**

Real overrides identified (default → current):

| Param | Default | Pre | Post-Phase-1 |
|-------|---------|-----|--------------|
| `bluestore_cache_trim_interval` | 0.05 s | **200 s (4000×)** | 0.05 s |
| `bluestore_throttle_bytes` | 64 MiB | 256 MiB (4×) | 64 MiB |
| `bluestore_throttle_deferred_bytes` | 128 MiB | 128 MiB (no-op override) | (cleaned up) |
| `osd_recovery_op_priority` | 3 | 1 | 3 |
| `osd_max_scrubs` | 3 | 4 | 2 |
| `osd_scrub_during_recovery` | false | true | false |
| `osd_scrub_priority` | 5 | 6 | 5 |
| `ms_async_op_threads` | 3 | 8 (2.7×) | 3 |
| `ms_dispatch_throttle_bytes` | 100 MB | 1 GiB (10×) | 100 MB |
| `ms_tcp_prefetch_max_size` | 4 KB | 64 KB (16×) | 4 KB |
| `osd_client_message_size_cap` | 500 MB | 1 GiB | 500 MB |
| `osd_client_message_cap` | 256 | 1000 | 256 |

Confirmed *not* overrides (matched Squid 19 defaults despite appearing in `ceph config dump`):
`bluefs_buffered_io=true`, `bluestore_sync_submit_transaction=false`, `osd_op_num_shards_ssd=8`, `osd_op_num_threads_per_shard_ssd=2`, `bluestore_max_deferred_txc=32`, `bluestore_deferred_batch_ops=0`, `bluestore_prefer_deferred_size_ssd=0`.

Still overridden but deferred to Phase 3 testing:
- `bluestore_compression_mode=aggressive` + `bluestore_compression_algorithm=lz4` + `bluestore_compression_required_ratio=0.7` (vs defaults `none`/`snappy`/`0.875`)
- `bluestore_cache_size_ssd=4 GiB` (default 3 GiB)
- `osd_memory_target=8 GiB` (default 4 GiB)
- `bluestore_block_db_size=5 GiB`, `bluestore_block_wal_size=1 GiB` (collocated tunings, leave)

### Phase 1 — config reverts (no daemon restarts, all live)

12 changes applied via `ceph config rm/set`. All took effect on running OSDs without restart, except the messenger threading params which need restart to fully apply.

`BLUESTORE_SLOW_OP_ALERT` muted for 24 h.

### Phase 0 → T+10 min comparison (osd.2 / osd.3 / osd.5)

| Metric | osd.2 PRE → T+10 | osd.3 PRE → T+10 | osd.5 PRE → T+10 |
|--------|------------------|------------------|------------------|
| `slow_committed_kv_count` | 29 → 29 (Δ 0) | 25 → 25 (Δ 0) | 0 → 0 (Δ 0) |
| `slow_aio_wait_count` | 0 → 0 | 2 → 2 | 0 → 0 |
| `state_deferred_queued_lat avgcount` | +1 529 events | +2 667 events | +3 182 events |
| `state_deferred_queued_lat avgtime (s)` | 0.331 → 0.352 | 0.441 → 0.446 | 0.439 → 0.451 |

**Result:** Zero new slow events accumulated over 10 minutes post-Phase-1. The 29/25 historical numbers are frozen — those are pre-mitigation events, not ongoing damage. Deferred-queue average latency is dominated by historical sample weight (millions of events) and won't move quickly on short windows.

### Sample slow op evidence (pre-Phase-1, osd.3 historic dump)

A 10.9 s op breakdown captured before changes:

```
queued_for_pg → reached_pg : 7.66 s   (PG-queue wait)
sub_op_commit_rec (osd.1) : 0.61 s
op_commit (BlueStore)     : 2.56 s
total                     : 10.9 s
```

This single sample is the strongest evidence for the BlueStore-backpressure hypothesis: deep PG queue depth + multi-second commit latency on consumer NVMe under sustained sync-write pressure.

## Root-cause synthesis

Three contributors to slow ops, in descending confidence:

1. **`bluestore_cache_trim_interval = 200 s`** (vs default 0.05 s) — almost certainly a misconfigured tuning carried over from prior troubleshooting. Cache only trimmed every ~3 minutes, causing memory-pressure spikes and stale-eviction bursts. Reverted in Phase 1.
2. **Messenger-layer over-tuning** (`ms_async_op_threads=8`, `ms_dispatch_throttle_bytes=1 GiB`, `ms_tcp_prefetch_max_size=64 KB`, plus inflated client message caps) — increased per-OSD scheduling overhead and queue depth without proportional benefit on a 6-OSD homelab. Reverted.
3. **Excessive concurrent scrubs** (`osd_max_scrubs=4`, `osd_scrub_during_recovery=true`, `osd_scrub_priority=6`) — added sustained read pressure that consumer NVMe doesn't tolerate well alongside client writes. Reduced to defaults (or `osd_max_scrubs=2`).

Whistler CPU-defect tie-in: the daily ~10:30 UTC osd.2 crashes from late April through May 5 fit a recurring workload (likely scheduled scrub) running on the bad cores 4-5. Since `isolcpus=4,5` was applied on May 6, **zero new crashes** as of this writing.

## Phase 2 — OSD restarts + mclock recalibration

### OSD restarts (sequential, one host at a time)

- `ceph-osd@3.service` on pve-mammoth — restart issued 14:34:59, back `up` in ~13 s, cluster `HEALTH_OK` ~22 s after restart.
- `ceph-osd@2.service` on pve-whistler — restart issued 15:37:32, back `up` in ~17 s, cluster `HEALTH_OK` ~23 s after restart.

Both daemons came back with **`slow_committed_kv_count = 0`** and **`slow_aio_wait_count = 0`**, clearing the underlying alert (the 24h mute became unused — `ceph -s` no longer shows the muted annotation).

### mclock IOPS recalibration

Ran `ceph tell osd.X bench 12288000 4096 4194304 100` (4KB-block IOPS test, the official mclock bench) on each OSD. Updated `osd_mclock_max_capacity_iops_ssd` per-OSD to the fresh measurement.

| OSD | Old cap | New measured | Δ | Notes |
|-----|---------|--------------|---|-------|
| osd.0 | 55 878 | **53 812** | -4 % | mammoth, Samsung 990 EVO+ |
| osd.1 | 78 611 | **56 044** | **-29 %** | zermatt, Samsung 990 EVO+ |
| osd.2 | 73 968 | **60 343** | -18 % | whistler, Samsung 990 EVO (non-Plus) |
| osd.3 | 79 941 | **57 715** | **-28 %** | mammoth, Samsung 990 EVO+ |
| osd.4 | 57 218 | **50 027** | -13 % | zermatt, **SPCC** (slowest, replace) |
| osd.5 | 78 910 | **67 435** | -15 % | whistler, Samsung 990 EVO+ (fastest) |

**Key finding:** every OSD measured 13-29 % lower than its stored cap. mclock had been over-provisioning capacity, letting through more concurrent ops than drives could service. Stale capacity model corrected. The osd.5 → osd.4 spread (67k → 50k IOPS) is real drive variance, not measurement noise. osd.5 is the fastest *and* has zero historical slow-kv events — the asymmetry between osd.5 (clean) and osd.2/3 (slow events) tracks raw drive throughput.

## Final state after Phase 1+2

```
HEALTH_OK
  6/6 OSDs up
  273/273 pgs active+clean (modulo periodic scrubs)
  Slow-op alert cleared at source (mute auto-released)
```

13 config overrides reverted to Squid 19 defaults, 6 mclock caps recalibrated to fresh measurements, BLUESTORE_SLOW_OP_ALERT cleared by counter reset.

## Phase 3+ (deferred)

- 3a: ✅ Completed — `passive` compression on osd.3, reverted 2026-05-12 (see Phase 3a outcome below).
- 3b: ✅ Applied 2026-05-12 — `osd_memory_target` 8 GiB → 6 GiB cluster-wide (see Phase 3b section below). Pending T+24h / T+48h evaluation.
- 4a: **ACTIVE** as of 2026-05-27 — replace SPCC drive on zermatt (osd.4). 64-68 °C under load (75 °C observed 2026-05-27), 8 920 power-on hours, lowest IOPS. Standard `ceph osd out 4` → drain → zap → bootstrap. See "2026-05-27 — new DB_DEVICE_STALLED_READ_ALERT on osd.4" below.
- 4b: 7-day watch on whistler for new MCE / segfault / osd.2 crash to confirm `isolcpus=4,5` mitigates the Raptor Lake P-core defect.

## Phase 3a — passive compression test on osd.3 (started 2026-05-10)

### Cluster state at kickoff
- `HEALTH_WARN` from 7 overdue deep-scrubs only (queued manually at start of session, draining over next several hours)
- Slow-op alert: still cleared. `slow_committed_kv_count = 0` and `slow_aio_wait_count = 0` on osd.2/3/5
- Crashes: zero new since 2026-05-07 10:36 UTC (3+ days clean since Phase 1+2 + isolcpus)
- Active Prometheus alerts: only `CephHealthWarn` (fed by the deep-scrub backlog)

### Change applied
```
ceph config set osd.3 bluestore_compression_mode passive
```
Verified live via `ceph daemon osd.3 config get` and `ceph tell osd.3 config get` — both report `passive`. (`ceph config show osd.3` is misleading and still reports `aggressive`; trust the daemon socket.)

Cluster default remains `osd / bluestore_compression_mode = aggressive` (with `lz4` algorithm, `0.7` ratio).

### Pre-change baseline (T-0)

Compression effectiveness (cumulative since OSD start):

| OSD | compressed | allocated | original | savings |
|-----|-----------|-----------|----------|---------|
| osd.0 | 48.1 GiB | 53.9 GiB | 147.9 GiB | ~64 % |
| osd.3 | 55.6 GiB | 62.4 GiB | 175.7 GiB | ~65 % |
| osd.5 | 30.3 GiB | 34.9 GiB | 107.1 GiB | ~67 % |

Latency / event counters at baseline:

| Metric | osd.0 (control) | osd.3 (test) | osd.5 (control) |
|--------|-----------------|---------------|------------------|
| `slow_committed_kv_count` | 0 | 0 | 0 |
| `slow_aio_wait_count` | 0 | 0 | 0 |
| `kv_sync_lat avgtime` | 5.72 ms | 6.33 ms | 5.78 ms |
| `state_kv_queued_lat avgtime` | 5.90 ms | 7.53 ms | 6.48 ms |
| `state_deferred_queued_lat avgtime` | 0.418 s | 0.251 s | 0.284 s |
| `compress_lat avgtime` | 25 µs | 27 µs | 22 µs |

osd.0 and osd.5 are the controls (same 990 EVO Plus drive model as osd.3, both still on `aggressive`). osd.0 currently has the highest deferred-queue latency, osd.3 the lowest — note osd.3's was lower even before the change.

### Observation plan

- Re-sample at T+24h and T+48h: `slow_committed_kv_count`, `slow_aio_wait_count`, `state_deferred_queued_lat avgtime`, `kv_sync_lat avgtime`, `compress_lat avgtime`, `compressed_original` (to measure how much new data was actually compressed under `passive`).
- If osd.3 deferred-queue avgtime drops noticeably vs osd.0/osd.5 *and* slow counters stay at 0 → roll `passive` cluster-wide.
- If osd.3 deferred-queue avgtime stays flat or rises → revert and call compression a non-issue.

### Phase 3a outcome (2026-05-12) — REVERTED

Ran ~2.8 days with osd.3 on `passive`. T+48h head-to-head (cumulative counters since OSD restart, `/tmp/ceph-phase3a-t48h/osd{0,1,3,5}.json`):

| OSD | Mode | Uptime | `deferred_queue_lat avgtime` | `slow_committed_kv` | `slow_aio_wait` |
|-----|------|--------|------------------------------|---------------------|------------------|
| osd.0 mammoth | aggressive | 7.7d | **0.361 s** | 0 | 0 |
| osd.3 mammoth | passive    | 5.3d (2.8d passive) | **0.244 s** | **120** | **10** |
| osd.5 whistler | aggressive | 6.0d | 0.256 s | 0 | 0 |
| osd.1 zermatt | aggressive | 5.8d | 0.269 s | 20 | 0 |

Imputed compression-eval rate on osd.3 after passive engaged dropped ~64 % (~4.75 M/day → ~1.72 M/day), confirming passive was mechanically working as intended.

**Decision rule evaluation:** latency improvement was real (32 % drop vs same-host control osd.0) but **slow counters did not stay at 0**. osd.3 had the *highest* `slow_committed_kv` of any OSD (6× osd.1's count), and slow ops also fired on the aggressive control osd.1 — so passive did not address the original problem (`BLUESTORE_SLOW_OP_ALERT`), which was the reason for the experiment.

**Reverted 2026-05-12 ~21:25 local:**
```
ceph config rm osd.3 bluestore_compression_mode
```
No daemon restart needed; takes effect live. Verified via `ceph daemon osd.3 config get bluestore_compression_mode` → `aggressive`. Cluster is back to uniform `aggressive + lz4 + 0.7 ratio` on all OSDs.

The 32 % avg latency win was not load-bearing — average deferred-queue latency was already well below any threshold that triggers alerts; making it lower did not change observed cluster behavior. Operational cleanliness (no per-OSD overrides to mentally subtract during future debugging) outweighed keeping the win.

## Phase 3b — `osd_memory_target` 8 GiB → 6 GiB (2026-05-12)

### Rationale

- `osd_memory_target=8 GiB` was 2× the Squid 19 default of 4 GiB; original reason for the bump is not documented and predates this project.
- With `bluestore_cache_trim_interval` corrected back to 0.05 s in Phase 1, the case for an outsized cache target weakens — the cache turns over fast enough that 6 GiB pairs well with the trim interval without thrashing.
- Frees ~4 GiB per host (12 GiB cluster-wide). Concretely: pve-zermatt's history of slab leaks (osd.4 SPCC + AIO failures, see `project_ceph_osd_health`) means any host memory pressure relief is welcome insurance.
- Phase 3a closure rules out compression as a contributor — `osd_memory_target` is the next remaining variable on the original change list.

### Cluster state at kickoff

- `HEALTH_WARN` (slow-op alert on osd.1 + osd.3, cumulative since their last restart 5-7 days ago)
- 273/273 PGs `active+clean`, 6/6 OSDs `up`, MONs quorate, no recent crashes
- Phase 3a reverted; uniform `bluestore_compression_mode=aggressive` cluster-wide

### Pre-change baseline (T-0)

Saved to workstation `/tmp/ceph-phase3b/osd{0..5}_pre.json`. Headline counters at the moment of cutover:

| OSD | `deferred_queue_lat avgtime` | `slow_committed_kv` | `slow_aio_wait` |
|-----|------------------------------|---------------------|------------------|
| osd.0 (mammoth, 990 EVO+) | 0.362 s | 0 | 0 |
| osd.1 (zermatt, 990 EVO+) | 0.271 s | 20 | 0 |
| osd.2 (whistler, 990 EVO non-Plus) | 0.287 s | 0 | 0 |
| osd.3 (mammoth, 990 EVO+) | 0.245 s | 120 | 10 |
| osd.4 (zermatt, SPCC) | 0.331 s | 0 | 0 |
| osd.5 (whistler, 990 EVO+) | 0.257 s | 0 | 0 |

Host memory at kickoff:

| Host | RAM total | Used | Free | Buff/cache | Available | Swap used |
|------|-----------|------|------|------------|-----------|-----------|
| pve-mammoth | 94 GiB | 59 GiB | 14 GiB | 21 GiB | 34 GiB | 2 GiB / 7 GiB |
| pve-whistler | 94 GiB | 56 GiB | 5 GiB | 33 GiB | 37 GiB | 0 / 39 GiB |
| pve-zermatt | 94 GiB | 60 GiB | 14 GiB | 20 GiB | 33 GiB | 2 GiB / 39 GiB |

### Change applied

```
ceph config set osd osd_memory_target 6442450944
```

Then sequential OSD restarts, one host at a time, waiting for full `273 active+clean` between each:

| OSD | Host | Restart issued | Cluster back to `active+clean` |
|-----|------|----------------|--------------------------------|
| osd.0 | pve-mammoth | 23:37 PDT | ~50 s |
| osd.3 | pve-mammoth | +1 min     | ~55 s |
| osd.2 | pve-whistler | following | ~110 s |
| osd.5 | pve-whistler | following | ~60 s |
| osd.1 | pve-zermatt | following | ~50 s |
| osd.4 | pve-zermatt | following | ~115 s |

All daemons came back with `slow_committed_kv_count=0` and `slow_aio_wait_count=0` (counters zero at restart), and `ceph daemon osd.N config get osd_memory_target` reported `6442450944` on every OSD. Final state: `HEALTH_OK`.

Side benefit: the restart cycle cleared the `BLUESTORE_SLOW_OP_ALERT` warnings (osd.1 and osd.3) at source.

### Observation plan

- Re-sample at T+24h and T+48h: `slow_committed_kv_count`, `slow_aio_wait_count`, `state_deferred_queued_lat avgtime`, `kv_sync_lat avgtime`, and host-level memory pressure (`free`, `/proc/slabinfo` on pve-zermatt).
- Watch for any new slow-op alerts in the Prometheus `CephHealthWarn` window.
- Compare against the same metrics from Phase 3a's T+48h capture (matching uptime windows). With compression no longer a variable, any reduction in slow-op accumulation rate can be attributed cleanly to the memory-target drop.
- **Decision rule:**
  - Slow counters stay at 0 across the 48h window *and* host memory pressure on pve-zermatt is reduced → keep 6 GiB cluster-wide, mark Phase 3b a win, move to Phase 4a (SPCC drive replacement).
  - Slow ops resume at meaningful rates → revert: `ceph config set osd osd_memory_target 8589934592`, restart OSDs sequentially again, treat memory target as non-factor and move to Phase 4a regardless.

## 2026-05-23 scrub-warn cleanup

Recurring `PG_NOT_DEEP_SCRUBBED` warnings (every few days, 1-5 PGs each)
were finally traced to a **mon vs OSD config split** on
`osd_deep_scrub_interval`:

| Source                                  | Value                                   | Why                                                                   |
|-----------------------------------------|-----------------------------------------|-----------------------------------------------------------------------|
| `ceph config dump` (override)           | `1 209 600 s = 14 d`, section **`osd`** | the override sat in the `osd` section only                            |
| OSD daemons (`daemon osd.0 config get`) | 14 d ✓                                  | OSDs read the `osd` section                                           |
| mon daemons (`daemon mon.* config get`) | **7 d**                                 | mons do NOT read the `osd` section, so they kept the upstream default |

`PG_NOT_DEEP_SCRUBBED` is evaluated by the **mon**, so the warning fired
at `7 d × (1 + 0.75) = 12.25 d` even though OSDs only intended to scrub
every 14 d. The scrub backlog cron then force-queued those PGs, and the
cycle repeated.

**Changes applied (2026-05-23):**

```bash
# Make the interval visible to both mons and OSDs.
ceph config set global osd_deep_scrub_interval 1209600

# Lift the warn ratio so the threshold reflects reality (35 d).
ceph config set global mon_warn_pg_not_deep_scrubbed_ratio 1.5

# Remove the now-redundant osd-section override that caused the split.
ceph config rm osd osd_deep_scrub_interval
```

Verified post-change:

- `ceph config dump` shows both keys under `global`, no `osd` duplicate.
- `mon.pve-mammoth` and `osd.0` both report `osd_deep_scrub_interval =
  1 209 600`.
- `PG_NOT_DEEP_SCRUBBED` cleared from `ceph health`.

**Effective behavior now:**

- OSD scheduler tries to deep-scrub every PG every 14 d (unchanged).
- Mon warns at 35 d, so the cron only ever fires for genuine
  starvation — drift up to 14 → 35 d is silent. The cron stays as a
  safety net.

**Why not just disable the cron and lift the warn threshold?** Because
on this hardware (consumer NVMe + `osd_max_scrubs=2`) PGs *do*
occasionally drift past the natural 14 d window into the 35 d zone;
the cron is the only mechanism that catches those. Tentacle adds an
OSD-side overdue queue that should eliminate this need — see
`ceph-tentacle-upgrade-plan.md` § "Why upgrade".

## 2026-05-27 — new `DB_DEVICE_STALLED_READ_ALERT` on osd.4

### Trigger

`ceph -s` came back `HEALTH_WARN` with two checks, both pinned to **osd.4**:

```
[WRN] BLUESTORE_SLOW_OP_ALERT: 1 OSD(s) experiencing slow operations in BlueStore
     osd.4 observed slow operation indications in BlueStore
[WRN] DB_DEVICE_STALLED_READ_ALERT: 1 OSD(s) experiencing stalled read in db device of BlueFS
     osd.4 observed stalled read indications in DB device
```

`DB_DEVICE_STALLED_READ_ALERT` is new in Squid and was not seen on this
cluster before today. The slow-op alert is the same one we raised the
threshold for on 2026-05-22 (`bluestore_slow_ops_warn_threshold = 10`),
re-firing because osd.4 now crosses 10 slow ops in the 24 h window.

### Threshold knobs (for reference)

Both alerts are sliding-window count-vs-threshold checks:

| Alert                          | Threshold knob                              | Default | Window knob                                | Default        |
|--------------------------------|---------------------------------------------|---------|--------------------------------------------|----------------|
| `BLUESTORE_SLOW_OP_ALERT`      | `bluestore_slow_ops_warn_threshold`         | 1       | `bluestore_slow_ops_warn_lifetime`         | 86400 s (24 h) |
| `DB_DEVICE_STALLED_READ_ALERT` | `bdev_stalled_read_warn_threshold`          | 1       | `bdev_stalled_read_warn_lifetime`          | 86400 s (24 h) |

Already overridden: `bluestore_slow_ops_warn_threshold = 10`. The
`bdev_stalled_read_*` knobs are still at defaults.

### Drive snapshot

`/dev/nvme0n1` on pve-zermatt = `SPCC_M.2_PCIe_SSD_A20250117N302KG00592` (osd.4).
The 990 EVO Plus 2 TB on `/dev/nvme1n1` on the same host is osd.1 — easy to
mix up at swap time; don't.

SMART (NVMe Log 0x02, NSID 0xffffffff):

| Field                              | Value                       |
|------------------------------------|-----------------------------|
| Critical Warning                   | 0x00                        |
| Temperature                        | **75 °C** (climbed 73→75 during diagnostics under live load) |
| Available Spare / Threshold        | 99 % / 32 %                 |
| Percentage Used                    | 7 %                         |
| Data Units Read / Written          | 55.4 TB / 51.6 TB           |
| Power On Hours                     | 8 920                       |
| Unsafe Shutdowns                   | 31                          |
| Media and Data Integrity Errors    | 0                           |
| Error Information Log Entries      | 0                           |
| Warning Comp. Temperature Time     | 43 min                      |
| Critical Comp. Temperature Time    | 32 min                      |

Kernel log (`dmesg -T --since "7 days ago"`): **zero** NVMe controller resets,
I/O errors, or aborts on nvme0. The drive isn't dropping I/O — it's slow under
load and runs hot, exactly the pattern this drive has shown since Phase 0
(64-68 °C under sustained load was the earlier observation; 75 °C today is a
notch worse, mid-diagnostics so the load wasn't even synthetic).

Bluestore perf counters on osd.4 (lifetime, since last restart 2026-05-12):

| Counter                          | Value          |
|----------------------------------|----------------|
| `slow_committed_kv_count`        | 74 343         |
| `slow_aio_wait_count`            | 53             |
| `slow_read_wait_aio_count`       | 6              |
| `state_kv_queued_lat avgtime`    | 15.5 ms        |
| `kv_sync_lat avgtime`            | 1.47 ms        |
| `read_wait_aio_lat avgtime`      | 1.43 ms        |

The new alert maps to the modest `slow_read_wait_aio_count = 6` over the
lifetime of the OSD — 6 events is plenty to trip a threshold-1 alert in a
24 h window. The other five OSDs are silent on all of these counters.

### Interpretation

Same drive, same root cause as every previous BLUESTORE_SLOW_OP_ALERT on this
OSD — Squid simply added a new alert flavor that catches BlueFS DB-side read
stalls in addition to the existing BlueStore slow-op alert. SMART is clean
(0 errors, 99 % spare, 7 % used), but the SPCC is sustained-load-slow and
thermally marginal. This is not a new failure mode; it's the same drive
getting incrementally worse.

### Decision

**Do not bump `bdev_stalled_read_warn_threshold`.** Raising the slow-op
threshold on 2026-05-22 was already a paint-job over the same underlying
drive problem; doubling down by also raising the stalled-read threshold
would hide the very signal Phase 4a is supposed to address. The fix is the
drive swap, not the alert knob.

**Phase 4a priority bumped from "deferred / low" to ACTIVE.** Procure the
replacement NVMe and execute the standard drain/zap/bootstrap when it
arrives.

### Mute applied (operational hygiene)

Until the drive lands, both alerts are expected noise. Muted both for 30 d,
sticky, so the cluster reads `HEALTH_OK` while still showing the mutes for
transparency:

```bash
ceph health mute BLUESTORE_SLOW_OP_ALERT 30d --sticky
ceph health mute DB_DEVICE_STALLED_READ_ALERT 30d --sticky
```

Verified — `ceph -s` reports:

```
health: HEALTH_OK
        (muted: BLUESTORE_SLOW_OP_ALERT(4w) DB_DEVICE_STALLED_READ_ALERT(4w))
```

**Mute expires ~2026-06-26.** If the drive isn't replaced by then, the
alerts will reappear and either need to be re-muted (another 30 d max — do
not extend to 90 d, the mute outlives the rationale) or executed against.

**After the drive swap**, explicitly unmute so the post-swap state is
genuinely verified rather than letting the mute lapse silently:

```bash
ceph health unmute BLUESTORE_SLOW_OP_ALERT
ceph health unmute DB_DEVICE_STALLED_READ_ALERT
```

If either alert reappears within 24 h of unmute on the *new* drive, that's
a real problem and not a known-degraded-SPCC artifact.

## Files

Baseline & post-snapshot perf dumps saved on the workstation:

Phase 0–2 (`/tmp/ceph-baseline/`): `osd2_perf_pre.json`, `osd3_perf_pre.json`, `osd5_perf_pre.json`; `osd*_perf_t0.json`; `osd*_perf_t10.json`; `osd3_config_diff.json`, `osd3_config_diff_post.json`.

Phase 3a (`/tmp/ceph-phase3a/`): `osd0_perf_pre.json`, `osd3_perf_pre.json`, `osd5_perf_pre.json` (control + test baselines); `osd3_perf_t0.json` (right after `passive` set); `osd3_config_diff_pre.json`, `osd3_config_diff_post.json`. T+48h snapshots in `/tmp/ceph-phase3a-t48h/osd{0,1,3,5}.json`.

Phase 3b (`/tmp/ceph-phase3b/`): `osd{0..5}_pre.json` (T-0 baselines captured just before the cluster-wide `osd_memory_target` change), `osd{0..5}_t0.json` (post-restart snapshots, all slow counters at 0).
