"""§7.7 embed-verification poller + §7.8 startup + periodic recovery.

verify_embed runs once per recording after notes_created; complete the
state machine to `complete` when Open Notebook flips embedded=true,
otherwise leave in embed_pending and emit stalled metric.

Two recovery layers wake idle bridges back to truth:
- start_recovery_scan: ONCE at boot (Layer A, plan §7.8)
- periodic_stale_scanner: every 5 min as defense in depth (Layer B)
"""

from __future__ import annotations

import asyncio
import logging
import time

import metrics as M
from config import Config
from ingest import source_marker
from open_notebook import OpenNotebookClient
from state import StateMachine

log = logging.getLogger(__name__)

# Cumulative poll: 5, 20, 80, 260, 740 seconds ≈ 12m20s (plan v11 D17/F8-004)
POLL_INTERVALS_S: tuple[int, ...] = (5, 15, 60, 180, 480)
POLL_TOTAL_S = sum(POLL_INTERVALS_S)  # 740


def _intervals_after_skip(skip_under: float) -> list[int]:
    """Plan v12 P11-003 — first retained interval shortened to budget remainder."""
    if skip_under <= 0:
        return list(POLL_INTERVALS_S)
    cum = 0
    retained: list[int] = []
    for i in POLL_INTERVALS_S:
        cum += i
        if cum > skip_under:
            retained.append((cum - int(skip_under)) if not retained else i)
    return retained


async def verify_embed(
    *,
    cfg: Config,
    sm: StateMachine,
    on: OpenNotebookClient,
    recording_id: str,
    source_id: str,
    skip_intervals_under: float = 0.0,
) -> None:
    """Plan §7.7. Fire-and-forget; never raises."""
    rid = recording_id
    intervals = _intervals_after_skip(skip_intervals_under)
    start = time.monotonic()
    try:
        for delay in intervals:
            await asyncio.sleep(delay)
            src = await on.get_source(source_id)
            if src is None:
                # Source 404'd mid-poll (plan §7.7 v9 F8-006 + v10 F9-005 split reasons)
                # Look up whether a replacement source has been ingested via repair
                marker = source_marker(rid)
                # Need a notebook to look in — bridge doesn't know which from here,
                # so do a best-effort scan across the cached primary notebook (ids map)
                ids = await sm.get_ids(rid)
                replaced = False
                if ids:
                    # Notebook ID isn't stored per-recording today; if we had it we'd
                    # use it here. Treat as missing_unexpected when we can't confirm.
                    pass
                reason = (
                    "repair_replaced_source" if replaced else "source_missing_unexpected"
                )
                M.embed_poller_aborted_total.labels(reason=reason).inc()
                return
            if src.get("embedded") is True:
                r = await sm.complete_after_embed(rid)
                if r.ok:
                    M.ingest_state_total.labels(state="complete", source="webhook").inc()
                    M.embed_verification_seconds.observe(time.monotonic() - start)
                # already_complete is fine (another worker beat us); not_embed_pending
                # is logged but harmless
                return
        # Timeout — poll window exhausted, source still embedded=false
        await sm.set_stalled_marker_if_absent(rid)
        M.open_notebook_embed_stalled_total.labels(
            recording_id=rid, reason="poll_timeout"
        ).inc()
        log.error("embed stalled: source=%s recording=%s after %ds", source_id, rid, POLL_TOTAL_S)
    except Exception:
        log.exception("verify_embed crashed for recording=%s; state preserved", rid)


async def start_recovery_scan(
    *, cfg: Config, sm: StateMachine, on: OpenNotebookClient
) -> None:
    """Plan §7.8 Layer A — runs ONCE at bridge boot before serving traffic."""
    log.info("startup embed_pending recovery scan starting")
    n_re_dispatched = 0
    n_completed = 0
    n_stalled = 0
    n_corrupt = 0

    async for state_key in sm.scan_state_keys():
        state_val = await sm.r.get(state_key)
        if state_val is None:
            continue
        s = state_val.decode() if isinstance(state_val, bytes) else state_val
        if s != "embed_pending":
            continue

        rid = state_key.split(":")[-1]
        ids = await sm.get_ids(rid)
        source_id = ids.get("source_id", "")
        if not source_id:
            M.embed_recovery_corrupt_state_total.labels(reason="missing_source_id").inc()
            log.error(
                "corrupt embed_pending state: recording=%s missing source_id; "
                "manual recovery: /admin/replay reset_state=true",
                rid,
            )
            n_corrupt += 1
            continue

        # ALWAYS GET the source first (v10 F9-001)
        try:
            src = await on.get_source(source_id)
        except Exception:
            log.exception("startup-recovery: GET source failed for %s", rid)
            continue
        if src is None:
            M.embed_poller_aborted_total.labels(reason="source_missing_unexpected").inc()
            continue
        if src.get("embedded") is True:
            r = await sm.complete_after_embed(rid)
            if r.ok:
                M.ingest_state_total.labels(state="complete", source="startup_recovery").inc()
                n_completed += 1
            continue
        # Not embedded yet — age-check via stored timestamp (v10 F9-001 / v11 P10-001)
        age = await sm.get_embed_pending_age_s(rid)
        if age > POLL_TOTAL_S:
            M.open_notebook_embed_stalled_total.labels(
                recording_id=rid, reason="startup_recovery"
            ).inc()
            n_stalled += 1
        else:
            # Re-dispatch §7.7 poller for the REMAINING window (skip_intervals_under)
            asyncio.create_task(
                verify_embed(
                    cfg=cfg,
                    sm=sm,
                    on=on,
                    recording_id=rid,
                    source_id=source_id,
                    skip_intervals_under=age,
                )
            )
            M.embed_poller_restarted_total.inc()
            n_re_dispatched += 1

    log.info(
        "startup recovery scan done: re_dispatched=%d completed=%d stalled=%d corrupt=%d",
        n_re_dispatched, n_completed, n_stalled, n_corrupt,
    )


async def periodic_stale_scanner(
    *, cfg: Config, sm: StateMachine, on: OpenNotebookClient, interval_seconds: int = 300
) -> None:
    """Plan §7.8 Layer B — runs every 5 min while bridge is up."""
    log.info("periodic stale-embed scanner started (interval=%ds)", interval_seconds)
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await _scan_once(cfg, sm, on)
        except Exception:
            log.exception("periodic stale scanner iteration failed; continuing")


async def _scan_once(cfg: Config, sm: StateMachine, on: OpenNotebookClient) -> None:
    # Also publishes open_notebook_stale_commands_total (F8-008): the dedicated
    # /api/commands/jobs probe was removed because that endpoint returns [] on
    # open_notebook 1.10.0 (F7-Live-003). Instead we count recordings still stuck
    # in embed_pending past the embed window using the source GET already done
    # here — a live proxy for "worker not draining its command queue" that
    # auto-clears to 0 once the backlog embeds.
    stuck = 0
    async for state_key in sm.scan_state_keys():
        state_val = await sm.r.get(state_key)
        if state_val is None:
            continue
        s = state_val.decode() if isinstance(state_val, bytes) else state_val
        if s != "embed_pending":
            continue
        rid = state_key.split(":")[-1]
        ids = await sm.get_ids(rid)
        source_id = ids.get("source_id", "")
        if not source_id:
            M.embed_recovery_corrupt_state_total.labels(reason="missing_source_id").inc()
            continue
        # Always GET source first (v11 P10-005)
        try:
            src = await on.get_source(source_id)
        except Exception:
            continue
        if src is None:
            M.embed_poller_aborted_total.labels(reason="source_missing_unexpected").inc()
            continue
        if src.get("embedded") is True:
            r = await sm.complete_after_embed(rid)
            if r.ok:
                M.ingest_state_total.labels(state="complete", source="periodic_recovery").inc()
            continue
        age = await sm.get_embed_pending_age_s(rid)
        if age > 15 * 60:
            # Still embed_pending past the embed window → worker likely wedged.
            stuck += 1
            already = await sm.has_stalled_marker(rid)
            if not already:
                M.open_notebook_embed_stalled_total.labels(
                    recording_id=rid, reason="periodic_scan"
                ).inc()
                await sm.set_stalled_marker_if_absent(rid)
    M.open_notebook_stale_commands_total.set(stuck)
