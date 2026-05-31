"""Core ingest flow shared by webhook + admin/replay paths (plan §7.1).

Pulled into a module so both `webhook.py` and `admin.py` invoke the
same state-machine + title-marker + state-aware-advance logic.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass

import metrics as M
from config import Config
from open_notebook import OpenNotebookClient, OpenNotebookError
from pocket import PocketPayload
from state import StateMachine

log = logging.getLogger(__name__)


POCKET_INBOX_NOTEBOOK = "Pocket Inbox"
TAG_DEFAULT_KEY = "__default__"


def source_marker(rid: str) -> str:
    return f"[pocket-id:{rid}]"


def note_marker(rid: str, kind: str) -> str:
    return f"[pocket-id:{rid} kind:{kind}]"


@dataclass
class IngestResult:
    recording_id: str
    source_id: str
    note_ids: dict[str, str]  # kind -> id
    notebooks: list[str]
    embed_status: str  # "pending" | "completed"


async def resolve_notebooks_for_tags(
    on: OpenNotebookClient, sm: StateMachine, tags: list[str]
) -> list[str]:
    """Plan §7.1 step 8 + §7.5. Falls back to Pocket Inbox for zero-tag (F2)."""
    if not tags:
        return [await resolve_one_tag(on, sm, POCKET_INBOX_NOTEBOOK, cache_as=TAG_DEFAULT_KEY)]
    return [await resolve_one_tag(on, sm, t) for t in tags]


async def resolve_one_tag(
    on: OpenNotebookClient, sm: StateMachine, tag: str, *, cache_as: str | None = None
) -> str:
    cache_key = cache_as or tag
    cached = await sm.get_cached_tag(cache_key)
    if cached:
        M.tag_cache_hits_total.labels(result="hit").inc()
        return cached
    M.tag_cache_hits_total.labels(result="miss").inc()
    # No name-filter param on /api/notebooks; scan client-side
    notebooks = await on.list_notebooks()
    for nb in notebooks:
        if nb.get("name") == tag:
            await sm.cache_tag(cache_key, nb["id"])
            M.notebook_ensure_total.labels(result="found_via_list").inc()
            return nb["id"]
    # Not found — create
    created = await on.create_notebook(tag, description=f"Auto-created for Pocket tag '{tag}'")
    await sm.cache_tag(cache_key, created["id"])
    M.notebook_ensure_total.labels(result="created").inc()
    return created["id"]


async def lease_refresher(sm: StateMachine, rid: str, owner_uuid: str, cfg: Config) -> None:
    """§7.6 — periodic refresh while ingest is running. Aborts caller on ownership loss."""
    while True:
        await asyncio.sleep(cfg.lease_refresh_interval_seconds)
        ok = await sm.refresh_lock(rid, owner_uuid)
        if not ok:
            M.lease_ownership_lost_total.inc()
            raise RuntimeError(f"lease ownership lost for {rid}; aborting")


async def run_ingest(
    *,
    cfg: Config,
    sm: StateMachine,
    on: OpenNotebookClient,
    payload: PocketPayload,
    owner_uuid: str,
    current_state: str,
    started_at: float,
) -> IngestResult:
    """Plan §7.1 steps 8-14 (sans HMAC/lease — caller already holds those).

    Handles state-aware skipping (§7.1 step 9e/10c/11 per F6-001) and the
    title-marker pre-create lookup (D16). Caller manages refresh + release.
    """
    rid = payload.recording_id

    # Step 8 — tag resolution
    notebook_ids = await resolve_notebooks_for_tags(on, sm, payload.tags)
    primary_notebook = notebook_ids[0]

    # Step 9 — source idempotency check + POST + state-aware advance
    ids = await sm.get_ids(rid)
    cached_source_id = ids.get("source_id")
    source_id: str

    if cached_source_id:
        # Resume path — verify it still exists (plan §7.3 step 7 spirit; cheap)
        existing = await on.get_source(cached_source_id)
        if existing is None:
            # Stale cache; clear and fall through to lookup/create
            ids.pop("source_id", None)
            cached_source_id = None

    if cached_source_id:
        source_id = cached_source_id
    else:
        # D16 pre-create lookup
        existing = await on.find_source_by_marker(primary_notebook, source_marker(rid))
        if existing is not None:
            source_id = existing["id"]
            await sm.store_source_id(rid, source_id)
        else:
            # Step 9c — create fresh
            title = f"{payload.title or 'Pocket recording'} {source_marker(rid)}"
            content = payload.transcript or "(transcript unavailable)"
            if payload.transcript is None:
                M.webhook_total.labels(event=payload.event, result="payload_field_missing").inc()
            try:
                src = await on.create_source_text(
                    notebook_ids=notebook_ids, content=content, title=title
                )
            except OpenNotebookError as e:
                M.open_notebook_write_total.labels(
                    operation="source_post", result="fail"
                ).inc()
                raise
            source_id = src["id"]
            await sm.store_source_id(rid, source_id)
            M.open_notebook_write_total.labels(
                operation="source_post", result="success"
            ).inc()

    # Step 9e — state-aware advance to source_created (F6-001 + F7-002 in-memory update)
    if current_state == "received":
        r = await sm.advance_state(rid, owner_uuid, "source_created", ["received"])
        if not r.ok:
            raise RuntimeError(f"advance_state→source_created rejected: {r.new_or_reason}")
        current_state = "source_created"
        M.ingest_state_total.labels(state="source_created", source="webhook").inc()
    # If state >= source_created on resume, skip the advance (per F6-001)

    # Step 10 — notes idempotency + POST + state-aware advance
    if current_state != "notes_created":
        # Single-shot fetch all notes in the primary notebook (plan §6 / no pagination)
        existing_notes = await on.list_notes_for_notebook(primary_notebook)
        M.open_notebook_notes_per_notebook.labels(notebook_id=primary_notebook).set(
            len(existing_notes)
        )

        note_ids = {}
        for kind, content_field, display in (
            ("summary", payload.summary, "Summary"),
            ("action_items", payload.action_items, "Action items"),
        ):
            marker = note_marker(rid, kind)
            existing = next((n for n in existing_notes if marker in (n.get("title") or "")), None)
            cached = ids.get(f"note_{kind}_id")
            if existing is not None:
                note_ids[kind] = existing["id"]
                await sm.store_note_id(rid, kind, existing["id"])
            elif cached:
                note_ids[kind] = cached
            else:
                # POST new
                body = _format_note_body(kind, content_field)
                if content_field is None:
                    M.webhook_total.labels(
                        event=payload.event, result="payload_field_missing"
                    ).inc()
                try:
                    note = await on.create_note(
                        notebook_id=primary_notebook,
                        title=f"{display} {marker}",
                        content=body,
                        note_type="ai",
                    )
                except OpenNotebookError:
                    M.open_notebook_write_total.labels(
                        operation=f"{kind}_note_post", result="fail"
                    ).inc()
                    raise
                note_ids[kind] = note["id"]
                await sm.store_note_id(rid, kind, note["id"])
                M.open_notebook_write_total.labels(
                    operation=f"{kind}_note_post", result="success"
                ).inc()

        # Step 10c — state-aware advance to notes_created
        if current_state == "source_created":
            r = await sm.advance_state(
                rid, owner_uuid, "notes_created", ["source_created"]
            )
            if not r.ok:
                raise RuntimeError(f"advance_state→notes_created rejected: {r.new_or_reason}")
            current_state = "notes_created"
            M.ingest_state_total.labels(state="notes_created", source="webhook").inc()
    else:
        # Already notes_created on resume; ids are in pocket:ids:<rid>
        note_ids = {
            "summary": ids.get("note_summary_id", ""),
            "action_items": ids.get("note_action_items_id", ""),
        }

    # Step 11 — state-aware advance to embed_pending (v8 D17) + record timestamp (P10-001)
    if current_state == "notes_created":
        r = await sm.advance_state(
            rid, owner_uuid, "embed_pending", ["notes_created"]
        )
        if not r.ok:
            raise RuntimeError(f"advance_state→embed_pending rejected: {r.new_or_reason}")
        await sm.mark_embed_pending_at(rid)
        M.ingest_state_total.labels(state="embed_pending", source="webhook").inc()

    return IngestResult(
        recording_id=rid,
        source_id=source_id,
        note_ids=note_ids,
        notebooks=notebook_ids,
        embed_status="pending",
    )


def _format_note_body(kind: str, value: object) -> str:
    if value is None:
        return f"(no {kind} provided by Pocket)"
    if isinstance(value, list):
        return "\n".join(f"- {item}" for item in value)
    return str(value)
