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

- 3b: After 3a settles, drop `osd_memory_target` 8 GiB → 6 GiB (default 4 GiB is conservative; 6 GiB pairs well with 0.05s trim interval).
- 4a: Replace SPCC drive on zermatt (osd.4). 64-68 °C under load, 8 433 power-on hours, lowest IOPS. Standard `ceph osd out 4` → drain → zap → bootstrap.
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

## Files

Baseline & post-snapshot perf dumps saved on the workstation:

Phase 0–2 (`/tmp/ceph-baseline/`): `osd2_perf_pre.json`, `osd3_perf_pre.json`, `osd5_perf_pre.json`; `osd*_perf_t0.json`; `osd*_perf_t10.json`; `osd3_config_diff.json`, `osd3_config_diff_post.json`.

Phase 3a (`/tmp/ceph-phase3a/`): `osd0_perf_pre.json`, `osd3_perf_pre.json`, `osd5_perf_pre.json` (control + test baselines); `osd3_perf_t0.json` (right after `passive` set); `osd3_config_diff_pre.json`, `osd3_config_diff_post.json`.
