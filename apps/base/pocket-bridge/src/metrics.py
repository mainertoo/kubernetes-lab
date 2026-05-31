"""Prometheus metrics registry (plan §8.1)."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# §7.1 webhook results
webhook_total = Counter(
    "webhook_total",
    "Pocket webhook deliveries by event + result",
    ["event", "result"],
)

# §7.1 step 7 / §7.7 — Lua-driven state-machine rejections
state_cas_rejected_total = Counter(
    "state_cas_rejected_total",
    "Lua state-machine rejections by reason",
    ["reason"],
)

# §7.1 monotonic transitions (state-aware advance per F6-001)
ingest_state_total = Counter(
    "ingest_state_total",
    "Per-state transitions by source (webhook / startup_recovery / periodic_recovery)",
    ["state", "source"],
)

# §7.1 ingest pipeline timings
ingest_seconds = Histogram(
    "ingest_seconds",
    "Per-phase timings for webhook ingest",
    ["phase"],
)

# §7.1 step 8 / §7.4 tag cache
tag_cache_hits_total = Counter(
    "tag_cache_hits_total",
    "Tag → notebook cache outcomes",
    ["result"],  # hit / miss / stale_evicted
)
notebook_ensure_total = Counter(
    "notebook_ensure_total",
    "Tag → notebook resolution outcomes",
    ["result"],  # found_in_cache / found_via_list / created / stale_cache_unrecoverable
)

# §7.3 / §7.3a replay outcomes (v12 P10-007/P10-008 — full enum)
replay_total = Counter(
    "replay_total",
    "Replay endpoint outcomes",
    ["result"],
)

# §8.1 — Open Notebook write metrics (F24)
open_notebook_write_total = Counter(
    "open_notebook_write_total",
    "Per-operation Open Notebook write outcomes",
    ["operation", "result"],  # source_post / summary_note_post / action_items_note_post
)

# §8.1 health gauges
redis_up = Gauge("redis_up", "Bridge → Redis reachability (1=up)")
open_notebook_up = Gauge("open_notebook_up", "Bridge → Open Notebook reachability (1=up)")
open_notebook_ping_total = Counter(
    "open_notebook_ping_total",
    "Open Notebook health probes",
    ["result"],
)
open_notebook_notes_per_notebook = Gauge(
    "open_notebook_notes_per_notebook",
    "Note count observed during §7.1 step 10a fetch",
    ["notebook_id"],
)
open_notebook_stale_commands_total = Gauge(
    "open_notebook_stale_commands_total",
    "Open Notebook commands with status=new and age > 5min",
)

# §7.6 lease + ownership
lease_held_seconds = Histogram(
    "lease_held_seconds",
    "Worker lease held duration (acquire → release)",
)
lease_expired_resume_total = Counter(
    "lease_expired_resume_total",
    "Times a Pocket retry hit the `resume` action (prior worker's lease expired)",
)
lease_ownership_lost_total = Counter(
    "lease_ownership_lost_total",
    "Times an in-flight worker detected ownership loss and aborted (P10-001 / F4-001)",
)
replay_reset_total = Counter(
    "replay_reset_total",
    "Legitimate /admin/replay state resets (separates from non_monotonic corruption signal)",
)

# §7.7/§7.8 embed-verification subsystem
embed_verification_seconds = Histogram(
    "embed_verification_seconds",
    "Time from notes_created → embedded:true confirmed",
)
embed_poller_aborted_total = Counter(
    "embed_poller_aborted_total",
    "§7.7 poller aborts: source 404 during poll (split per v11 P10-009)",
    ["reason"],  # repair_replaced_source / source_missing_unexpected
)
embed_poller_restarted_total = Counter(
    "embed_poller_restarted_total",
    "§7.8 Layer A re-dispatched §7.7 poller for embed_pending recording at startup",
)
embed_recovery_corrupt_state_total = Counter(
    "embed_recovery_corrupt_state_total",
    "§7.8 found embed_pending state with no corresponding pocket:ids entry",
    ["reason"],
)
open_notebook_embed_stalled_total = Counter(
    "open_notebook_embed_stalled_total",
    "embed_pending source not observed embedded:true within allowed window",
    ["recording_id", "reason"],  # poll_timeout / startup_recovery / periodic_scan
)
