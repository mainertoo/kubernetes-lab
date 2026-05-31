"""POST /webhook/pocket — plan §7.1.

Steps 1-7 (HMAC + dispatcher) live here; steps 8-14 are in ingest.py.
Includes capture-mode (§5 Phase 3a F2-006) for fixture grabbing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request, Response

import metrics as M
from config import Config
from ingest import lease_refresher, run_ingest
from open_notebook import OpenNotebookClient
from pocket import HmacError, PocketPayload, TimestampError, parse_summary_completed, verify_hmac
from poller import verify_embed
from state import StateMachine

log = logging.getLogger(__name__)


def build_webhook_app(
    *,
    cfg: Config,
    sm: StateMachine,
    on: OpenNotebookClient,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/webhook/pocket")
    async def receive(
        request: Request,
        # Phase 3a live discovery (HeyPocket-Webhook/1.0): signature/timestamp
        # arrive under x-heypocket-* headers, NOT the plan-time-guessed
        # Pocket-* names. Timestamps are milliseconds (handled in pocket.py).
        pocket_signature: str = Header(default="", alias="x-heypocket-signature"),
        pocket_timestamp: str = Header(default="", alias="x-heypocket-timestamp"),
    ) -> dict:
        # Step 1 — body size limit (F12). Read bounded.
        body = await request.body()
        if len(body) > cfg.body_size_limit_bytes:
            M.webhook_total.labels(event="?", result="body_too_large").inc()
            raise HTTPException(status_code=413, detail="body too large")

        # Phase 3a F2-006 / F3 fixture capture — moved BEFORE HMAC so the next
        # test event captures the body even while HMAC canonical form is still
        # under investigation. Headers + body together let us derive Pocket's
        # signing scheme locally.
        if cfg.capture_fixture:
            log.warning(
                "CAPTURE MODE headers=%s",
                {k: v for k, v in request.headers.items()},
            )
            log.warning("CAPTURE MODE body=%s", body.decode(errors="replace"))
            try:
                captured = Path("/tmp") / f"pocket-fixture-{uuid.uuid4().hex[:8]}.json"
                captured.write_text(body.decode(errors="replace"))
                log.warning("CAPTURE MODE wrote fixture to %s", captured)
            except Exception as e:
                log.warning("CAPTURE MODE file-write failed (logs are authoritative): %s", e)
            M.webhook_total.labels(event="capture", result="success").inc()
            return {"captured": "logs+/tmp"}

        # Steps 3-5 — header validation + HMAC + timestamp window
        try:
            verify_hmac(
                raw_body=body,
                signature_header=pocket_signature,
                timestamp_header=pocket_timestamp,
                secret=cfg.pocket_webhook_secret,
                window_seconds=cfg.timestamp_window_seconds,
            )
        except TimestampError as e:
            M.webhook_total.labels(event="?", result="timestamp_fail").inc()
            log.warning("timestamp_fail: %s", e)
            raise HTTPException(status_code=401, detail="timestamp window") from e
        except HmacError as e:
            M.webhook_total.labels(event="?", result="hmac_fail").inc()
            log.warning("hmac_fail: %s", e)
            raise HTTPException(status_code=401, detail="hmac mismatch") from e

        # Step 6 — parse + filter
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            M.webhook_total.labels(event="?", result="hmac_fail").inc()
            raise HTTPException(status_code=400, detail="invalid json")

        event = (data.get("event") or data.get("event_type") or "").lower()
        if event != "summary.completed":
            M.webhook_total.labels(event=event or "unknown", result="non_summary").inc()
            return {"skipped": "non-summary event", "event": event}

        payload = parse_summary_completed(data)
        rid = payload.recording_id
        if not rid:
            M.webhook_total.labels(event=event, result="payload_field_missing").inc()
            raise HTTPException(status_code=400, detail="missing recording_id")

        # Step 7 — atomic lease + state dispatcher
        owner_uuid = str(uuid.uuid4())
        acq = await sm.acquire_and_dispatch(rid, owner_uuid)

        if acq.action == "dedup":
            M.webhook_total.labels(event=event, result="duplicate").inc()
            M.state_cas_rejected_total.labels(reason="already_complete").inc()
            return {"skipped": "duplicate", "recording_id": rid}
        if acq.action == "embed_pending":
            M.webhook_total.labels(event=event, result="embed_pending").inc()
            M.state_cas_rejected_total.labels(reason="awaiting_embed").inc()
            return {"deferred": "awaiting_embed_verify", "recording_id": rid}
        if acq.action == "in_progress":
            M.webhook_total.labels(event=event, result="in_progress").inc()
            M.state_cas_rejected_total.labels(reason="concurrent_in_progress").inc()
            return {"deferred": "concurrent", "recording_id": rid}

        # start | resume — lock acquired
        if acq.action == "start":
            r = await sm.advance_state(rid, owner_uuid, "received", ["none"])
            if not r.ok:
                # Released the lock for cleanliness
                await sm.release_lock(rid, owner_uuid)
                M.state_cas_rejected_total.labels(reason="non_monotonic").inc()
                raise HTTPException(
                    status_code=409, detail=f"start state advance failed: {r.new_or_reason}"
                )
            current_state = "received"
            M.ingest_state_total.labels(state="received", source="webhook").inc()
        else:  # resume
            current_state = acq.current_state
            M.lease_expired_resume_total.inc()

        start_t = asyncio.get_event_loop().time()
        refresher = asyncio.create_task(lease_refresher(sm, rid, owner_uuid, cfg))
        try:
            result = await run_ingest(
                cfg=cfg,
                sm=sm,
                on=on,
                payload=payload,
                owner_uuid=owner_uuid,
                current_state=current_state,
                started_at=start_t,
            )
            # Step 12-14 — schedule verify, release lock, return
            asyncio.create_task(
                verify_embed(
                    cfg=cfg,
                    sm=sm,
                    on=on,
                    recording_id=rid,
                    source_id=result.source_id,
                )
            )
        except Exception as e:
            log.exception("ingest failed for recording=%s", rid)
            await sm.release_lock(rid, owner_uuid)
            M.webhook_total.labels(event=event, result="open_notebook_error").inc()
            raise HTTPException(status_code=500, detail=str(e)) from e
        finally:
            refresher.cancel()

        await sm.release_lock(rid, owner_uuid)
        M.lease_held_seconds.observe(asyncio.get_event_loop().time() - start_t)
        M.webhook_total.labels(event=event, result="success").inc()
        return {
            "recording_id": rid,
            "source_id": result.source_id,
            "note_ids": result.note_ids,
            "notebooks": result.notebooks,
            "embed": result.embed_status,
        }

    return app
