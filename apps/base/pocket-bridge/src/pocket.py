"""Pocket webhook + API plumbing.

HMAC verification per plan §7.1 steps 1-5 + §9 (compare_digest, F12/F13).
Conservative payload extraction per §5 Phase 1 (F2-006) — wrapped
_safe_extract returns None on missing fields rather than raising, so
schema drift from Pocket surfaces as `payload_field_missing` metric
rather than a 500. Real schema is pinned in Phase 3a (post-first-delivery).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from config import Config

log = logging.getLogger(__name__)


class HmacError(Exception):
    pass


class TimestampError(Exception):
    pass


def verify_hmac(
    *,
    raw_body: bytes,
    signature_header: str,
    timestamp_header: str,
    secret: str,
    window_seconds: int,
    now_s: float | None = None,
) -> None:
    """Raises HmacError / TimestampError on rejection. Plan §7.1 steps 3-5.

    Header format guard runs BEFORE the cryptographic compare (F13).
    """
    # Header format guards. Phase 3a discovery: x-heypocket-timestamp is
    # milliseconds since epoch (e.g. 1780240286263), NOT seconds. Convert to
    # seconds for window comparison.
    try:
        ts_ms = int(timestamp_header)
    except (TypeError, ValueError):
        raise TimestampError(f"malformed timestamp header: {timestamp_header!r}")
    ts = ts_ms / 1000.0
    if not signature_header or any(c not in "0123456789abcdefABCDEF" for c in signature_header):
        raise HmacError("malformed signature header")

    # Window check before HMAC compare (cheap reject for replay)
    cur = now_s if now_s is not None else time.time()
    if abs(cur - ts) > window_seconds:
        raise TimestampError(
            f"timestamp outside ±{window_seconds}s window (delta={cur-ts:.0f}s, ts_ms={ts_ms})"
        )

    # HMAC canonical form is being derived empirically in Phase 3a — capture
    # body + signature, compute several candidate forms locally, pick the
    # matching one. Until then this is a placeholder using `<ts_ms>.<body>`
    # (analogous to Stripe). HMAC failure is expected; capture-mode in
    # webhook.py short-circuits before this is reached during diagnosis.
    msg = f"{ts_ms}.".encode() + raw_body
    expected = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature_header):
        raise HmacError("hmac mismatch")


# ---------------------------------------------------------------------------
# Conservative payload extraction (plan §5 Phase 1 F2-006)
# ---------------------------------------------------------------------------

def _safe_extract(payload: Any, path: str, default: Any = None) -> Any:
    """Walk a dotted path; return default on any miss (no exception)."""
    cur: Any = payload
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return default
        else:
            return default
        if cur is None:
            return default
    return cur


@dataclass
class PocketPayload:
    """What the bridge extracts from a summary.completed webhook body.

    All fields optional — missing ones increment payload_field_missing
    rather than failing the request. Hardened in Phase 3a after capturing
    a real fixture (§5 Phase 3a F2-006).
    """

    recording_id: str
    event: str
    title: str | None
    transcript: str | None
    summary: str | None
    action_items: list[str] | None
    tags: list[str]
    raw: dict[str, Any]


def parse_summary_completed(body: dict[str, Any]) -> PocketPayload:
    """Extract from real HeyPocket schema (Phase 3a fixture-pinned 2026-05-31).

    Fixture: apps/base/pocket-bridge/contracts/pocket-test-event-2026-05-31.json
    Real fields observed on the captured "test" event:
      recording.id, recording.title
      summarizations[0].summary.markdown      # primary summary text
      summarizations[0].summary.bulletPoints  # array of strings
      summarizations[0].actionItems[].task    # array of dicts; we pull .task
      transcript[] = [{speaker, text, start, end}, ...]
      (tags absent in test event; real recordings TBD)
    """
    # Transcript: array of segments — concatenate with speaker prefix
    transcript_segs = _safe_extract(body, "transcript")
    transcript_text: str | None = None
    if isinstance(transcript_segs, list) and transcript_segs:
        lines = []
        for seg in transcript_segs:
            if not isinstance(seg, dict):
                continue
            spk = seg.get("speaker") or "?"
            txt = seg.get("text") or ""
            if txt:
                lines.append(f"{spk}: {txt}")
        transcript_text = "\n".join(lines) if lines else None

    # Summary: prefer markdown; fall back to bulletPoints joined
    summary_text: str | None = _safe_extract(body, "summarizations.0.summary.markdown")
    if not summary_text:
        bullets = _safe_extract(body, "summarizations.0.summary.bulletPoints")
        if isinstance(bullets, list) and bullets:
            summary_text = "\n".join(f"- {b}" for b in bullets if b)

    # Action items: each is a dict with .task (+ assignee, dueDate, completed)
    raw_actions = _safe_extract(body, "summarizations.0.actionItems")
    action_items: list[str] | None = None
    if isinstance(raw_actions, list) and raw_actions:
        action_items = []
        for it in raw_actions:
            if isinstance(it, dict):
                task = it.get("task") or ""
                if task:
                    assignee = it.get("assignee")
                    suffix = f" (assigned: {assignee})" if assignee else ""
                    action_items.append(f"{task}{suffix}")
            elif isinstance(it, str):
                action_items.append(it)

    return PocketPayload(
        recording_id=_safe_extract(body, "recording.id") or _safe_extract(body, "recording_id") or "",
        event=_safe_extract(body, "event") or _safe_extract(body, "event_type") or "",
        title=_safe_extract(body, "recording.title") or _safe_extract(body, "title"),
        transcript=transcript_text,
        summary=summary_text,
        action_items=action_items,
        tags=_safe_extract(body, "tags", []) or [],
        raw=body,
    )


# ---------------------------------------------------------------------------
# Pocket API (used by /admin/replay step 2 — plan §7.3)
# ---------------------------------------------------------------------------

class PocketAPIClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._client = httpx.AsyncClient(
            base_url=cfg.pocket_api_base_url,
            headers={
                "Authorization": f"Bearer {cfg.pocket_api_token}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(30.0, connect=5.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_recording(self, recording_id: str) -> dict[str, Any]:
        """Returns Pocket's full recording representation (for /admin/replay)."""
        r = await self._client.get(f"/recordings/{recording_id}")
        if r.status_code == 404:
            raise FileNotFoundError(f"pocket recording {recording_id} not found")
        if r.status_code != 200:
            raise RuntimeError(f"pocket GET /recordings/{recording_id}: HTTP {r.status_code}")
        return r.json()
