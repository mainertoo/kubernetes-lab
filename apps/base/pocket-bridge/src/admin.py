"""Admin port :8081 — /admin/replay + /healthz (plan §7.3, §7.3a)."""

from __future__ import annotations

import asyncio
import hmac
import logging
import uuid
from typing import Any

from fastapi import FastAPI, Header, HTTPException

import metrics as M
from config import Config
from ingest import lease_refresher, run_ingest, source_marker
from open_notebook import OpenNotebookClient, OpenNotebookError
from pocket import PocketAPIClient, parse_summary_completed
from poller import verify_embed
from state import StateMachine

log = logging.getLogger(__name__)


def _check_bearer(provided: str, expected: str) -> None:
    if not provided or not provided.lower().startswith("bearer "):
        M.replay_total.labels(result="bearer_fail").inc()
        raise HTTPException(status_code=401, detail="missing bearer")
    token = provided.split(None, 1)[1]
    if not hmac.compare_digest(token, expected):
        M.replay_total.labels(result="bearer_fail").inc()
        raise HTTPException(status_code=401, detail="bearer mismatch")


_FLAG_REJECTED_PAIRS = (
    ("repair_embeddings_only", "reset_state"),
    ("repair_embeddings_only", "unbounded_scan"),
)


def _validate_replay_flags(body: dict[str, Any]) -> None:
    """Plan v9/v10 §7.3 flag-validation matrix."""
    for a, b in _FLAG_REJECTED_PAIRS:
        if bool(body.get(a)) and bool(body.get(b)):
            raise HTTPException(
                status_code=422, detail=f"flags {a} + {b} are mutually exclusive"
            )


def build_admin_app(
    *,
    cfg: Config,
    sm: StateMachine,
    on: OpenNotebookClient,
    pocket: PocketAPIClient,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {
            "redis_up": await sm.ping(),
            "open_notebook_up": await on.ping(),
        }

    @app.post("/admin/replay")
    async def replay(
        body: dict[str, Any],
        authorization: str = Header(default="", alias="Authorization"),
    ) -> dict:
        _check_bearer(authorization, cfg.replay_admin_token)
        _validate_replay_flags(body)

        rid = body.get("recording_id") or ""
        if not rid:
            raise HTTPException(status_code=400, detail="recording_id required")
        reset_state = bool(body.get("reset_state"))
        force_delete_lock = bool(body.get("force_delete_lock"))
        unbounded_scan = bool(body.get("unbounded_scan"))
        repair_only = bool(body.get("repair_embeddings_only"))

        # Step 2 — Pocket fetch FIRST (no state change on failure, plan §7.3 step 2 / F15)
        try:
            recording = await pocket.get_recording(rid)
        except FileNotFoundError:
            M.replay_total.labels(result="pocket_fetch_fail").inc()
            raise HTTPException(status_code=404, detail="pocket recording not found")
        except Exception as e:
            M.replay_total.labels(result="pocket_fetch_fail").inc()
            raise HTTPException(status_code=502, detail=str(e)) from e

        # Step 4 — live-lock check (F4-003)
        lock_val = await sm.r.get(f"pocket:lock:{rid}")
        if lock_val is not None and not force_delete_lock:
            M.replay_total.labels(result="lock_held").inc()
            raise HTTPException(
                status_code=409,
                detail="active worker holds lease; pass force_delete_lock=true to override",
            )
        if force_delete_lock:
            await sm.force_delete_lock(rid)
            log.warning("operator forced lock release for %s", rid)

        # Step 5 — complete-state guard
        state_val = await sm.r.get(f"pocket:state:{rid}")
        state = state_val.decode() if isinstance(state_val, bytes) else (state_val or "")
        if state == "complete" and not reset_state and not repair_only:
            M.replay_total.labels(result="already_complete").inc()
            raise HTTPException(
                status_code=409,
                detail="already complete; pass reset_state=true to override",
            )

        # Branch to §7.3a repair flow
        if repair_only:
            return await _run_repair_flow(
                cfg=cfg, sm=sm, on=on, recording=recording, recording_id=rid
            )

        # Step 6 — legitimate reset (replay_reset_total, NOT non_monotonic)
        if reset_state:
            await sm.reset_state(rid)
            M.replay_reset_total.inc()

        # Step 7 — verify cached source/note IDs still exist (F3-002)
        ids = await sm.get_ids(rid)
        if "source_id" in ids:
            if await on.get_source(ids["source_id"]) is None:
                await sm.r.hdel(f"pocket:ids:{rid}", "source_id")
        for kind in ("summary", "action_items"):
            note_field = f"note_{kind}_id"
            if note_field in ids:
                if await on.get_note(ids[note_field]) is None:
                    await sm.r.hdel(f"pocket:ids:{rid}", note_field)

        # Step 8-10 — acquire lease + synthesize payload + run ingest
        owner_uuid = str(uuid.uuid4())
        acq = await sm.acquire_and_dispatch(rid, owner_uuid)
        if acq.action in ("dedup", "in_progress", "embed_pending"):
            M.replay_total.labels(result="ingest_fail").inc()
            raise HTTPException(
                status_code=409, detail=f"replay cannot proceed in state '{acq.action}'"
            )

        # Synthesize a webhook-like payload from the Pocket recording
        synth = {"event": "summary.completed", **recording}
        payload = parse_summary_completed(synth)

        start_t = asyncio.get_event_loop().time()
        refresher = asyncio.create_task(lease_refresher(sm, rid, owner_uuid, cfg))
        try:
            # If start: must advance to received first
            if acq.action == "start":
                r = await sm.advance_state(rid, owner_uuid, "received", ["none"])
                if not r.ok:
                    raise RuntimeError(r.new_or_reason)
                cur = "received"
            else:
                cur = acq.current_state
            result = await run_ingest(
                cfg=cfg, sm=sm, on=on, payload=payload, owner_uuid=owner_uuid,
                current_state=cur, started_at=start_t,
            )
            asyncio.create_task(
                verify_embed(cfg=cfg, sm=sm, on=on, recording_id=rid, source_id=result.source_id)
            )
        except Exception as e:
            log.exception("replay ingest failed for %s", rid)
            await sm.release_lock(rid, owner_uuid)
            M.replay_total.labels(result="ingest_fail").inc()
            raise HTTPException(status_code=500, detail=str(e)) from e
        finally:
            refresher.cancel()

        await sm.release_lock(rid, owner_uuid)
        M.replay_total.labels(result="success").inc()
        return {
            "result": "success",
            "recording_id": rid,
            "source_id": result.source_id,
            "note_ids": result.note_ids,
        }

    return app


async def _run_repair_flow(
    *,
    cfg: Config,
    sm: StateMachine,
    on: OpenNotebookClient,
    recording: dict[str, Any],
    recording_id: str,
) -> dict:
    """Plan §7.3a — embed-pending recovery via DELETE + re-ingest."""
    rid = recording_id
    marker = source_marker(rid)

    # R1 — find existing source by title marker; need a notebook to look in.
    # Use the cached primary notebook from Redis ids (or any tag cache); if neither,
    # we don't know where to scan — return 404 to surface the missing context.
    ids = await sm.get_ids(rid)
    # Bridge stores source/notes but not the notebook id explicitly; recompute
    # from the recording's tags using the cached map.
    synth = {"event": "summary.completed", **recording}
    payload = parse_summary_completed(synth)
    if not payload.tags:
        # Default notebook (Pocket Inbox)
        primary = await sm.get_cached_tag("__default__")
    else:
        primary = await sm.get_cached_tag(payload.tags[0])
    if not primary:
        # Resolve fresh (creates if needed)
        from ingest import resolve_notebooks_for_tags

        notebook_ids = await resolve_notebooks_for_tags(on, sm, payload.tags)
        primary = notebook_ids[0]

    existing = await on.find_source_by_marker(primary, marker)
    if existing is None:
        M.replay_total.labels(result="repair_target_missing").inc()
        raise HTTPException(
            status_code=404, detail="no source with pocket-id marker found"
        )
    old_source_id = existing["id"]

    # R3 — DELETE the old source
    try:
        await on.delete_source(old_source_id)
    except OpenNotebookError as e:
        M.replay_total.labels(result="repair_delete_fail").inc()
        raise HTTPException(status_code=502, detail=f"DELETE source failed: {e}") from e

    # R4 — clear cached source_id
    await sm.r.hdel(f"pocket:ids:{rid}", "source_id")

    # R5 — Lua-CAS embed_pending → received (plan v10 F9-002)
    r = await sm.repair_revert_to_received(rid)
    if not r.ok:
        M.replay_total.labels(result="repair_state_drifted").inc()
        raise HTTPException(
            status_code=409,
            detail=f"state changed during repair: now {r.current_state}",
        )
    M.replay_reset_total.inc()
    await sm.mark_embed_pending_at(rid)  # will be reset again on advance; harmless

    # R6 — acquire lease with new owner_uuid + run normal ingest
    owner_uuid = str(uuid.uuid4())
    acq = await sm.acquire_and_dispatch(rid, owner_uuid)
    if acq.action not in ("start", "resume"):
        raise HTTPException(status_code=409, detail=f"repair acquire returned {acq.action}")

    start_t = asyncio.get_event_loop().time()
    refresher = asyncio.create_task(lease_refresher(sm, rid, owner_uuid, cfg))
    try:
        result = await run_ingest(
            cfg=cfg, sm=sm, on=on, payload=payload, owner_uuid=owner_uuid,
            current_state="received", started_at=start_t,
        )
        asyncio.create_task(
            verify_embed(cfg=cfg, sm=sm, on=on, recording_id=rid, source_id=result.source_id)
        )
    finally:
        refresher.cancel()
        await sm.release_lock(rid, owner_uuid)

    M.replay_total.labels(result="embed_repair_dispatched").inc()
    return {
        "status": "embed_repair_dispatched",
        "old_source_id": old_source_id,
        "new_source_id": result.source_id,
        "embed": "pending",
    }
