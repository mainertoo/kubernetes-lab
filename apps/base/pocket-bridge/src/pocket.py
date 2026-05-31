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
    """Conservative extraction; relies on _safe_extract for each path."""
    return PocketPayload(
        recording_id=_safe_extract(body, "recording.id") or _safe_extract(body, "recording_id") or "",
        event=_safe_extract(body, "event") or _safe_extract(body, "event_type") or "",
        title=_safe_extract(body, "recording.title") or _safe_extract(body, "title"),
        transcript=(
            _safe_extract(body, "transcript.text")
            or _safe_extract(body, "transcript")
            or _safe_extract(body, "recording.transcript")
        ),
        summary=(
            _safe_extract(body, "summary.text")
            or _safe_extract(body, "summary")
            or _safe_extract(body, "summarizations.0.v2.summary")
        ),
        action_items=(
            _safe_extract(body, "action_items")
            or _safe_extract(body, "summary.action_items")
            or _safe_extract(body, "summarizations.0.v2.actionItems")
        ),
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
