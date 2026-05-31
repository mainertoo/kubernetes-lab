"""Environment-driven config + Lua script loader (plan §7.6)."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

LUA_DIR = Path(__file__).parent / "lua"


@dataclass(frozen=True)
class Config:
    # Secrets — refuse start if either is empty (plan F8/F11)
    pocket_webhook_secret: str
    replay_admin_token: str
    pocket_api_token: str
    open_notebook_api_key: str  # may be empty — bridge still runs (plan §6 auth note)

    # Network targets
    open_notebook_base_url: str
    pocket_api_base_url: str
    redis_host: str
    redis_port: int

    # Ports (plan D15)
    webhook_port: int
    admin_port: int
    metrics_port: int

    # Behaviour knobs
    timestamp_window_seconds: int  # HMAC freshness (plan §7.1 step 4)
    body_size_limit_bytes: int  # plan §7.1 step 1 / F12
    lease_ttl_seconds: int  # plan §7.6
    lease_refresh_interval_seconds: int  # plan §7.6
    state_ttl_seconds: int  # 30d post-complete
    embed_pending_at_ttl_seconds: int  # timestamp key TTL (plan §7.1 step 11 / P10-001)
    embed_stalled_marker_ttl_seconds: int  # plan §7.8 Layer B / P10-005
    seen_recording_ttl_seconds: int  # legacy alias for state TTL
    # Capture mode (plan §5 Phase 3a / F2-006)
    capture_fixture: bool


def _required(name: str) -> str:
    v = os.environ.get(name, "")
    if not v:
        sys.stderr.write(f"FATAL: required env var {name} is empty\n")
        sys.exit(2)
    return v


def load() -> Config:
    return Config(
        pocket_webhook_secret=_required("POCKET_WEBHOOK_SECRET"),
        replay_admin_token=_required("REPLAY_ADMIN_TOKEN"),
        pocket_api_token=_required("POCKET_API_TOKEN"),
        open_notebook_api_key=os.environ.get("OPEN_NOTEBOOK_API_KEY", ""),
        open_notebook_base_url=os.environ.get(
            "OPEN_NOTEBOOK_BASE_URL",
            "http://open-notebook.open-notebook.svc.cluster.local:5055",
        ),
        pocket_api_base_url=os.environ.get(
            "POCKET_API_BASE_URL", "https://api.usepocket.com/v1"
        ),
        redis_host=os.environ.get("REDIS_HOST", "127.0.0.1"),
        redis_port=int(os.environ.get("REDIS_PORT", "6379")),
        webhook_port=int(os.environ.get("WEBHOOK_PORT", "8080")),
        admin_port=int(os.environ.get("ADMIN_PORT", "8081")),
        metrics_port=int(os.environ.get("METRICS_PORT", "8082")),
        timestamp_window_seconds=int(os.environ.get("HMAC_TIMESTAMP_WINDOW_S", "300")),
        body_size_limit_bytes=int(os.environ.get("BODY_SIZE_LIMIT_BYTES", str(1024 * 1024))),
        lease_ttl_seconds=int(os.environ.get("LEASE_TTL_S", "60")),
        lease_refresh_interval_seconds=int(os.environ.get("LEASE_REFRESH_S", "20")),
        state_ttl_seconds=int(os.environ.get("STATE_TTL_S", str(30 * 24 * 3600))),
        embed_pending_at_ttl_seconds=int(
            os.environ.get("EMBED_PENDING_AT_TTL_S", str(30 * 24 * 3600))
        ),
        embed_stalled_marker_ttl_seconds=int(
            os.environ.get("EMBED_STALLED_MARKER_TTL_S", str(24 * 3600))
        ),
        seen_recording_ttl_seconds=int(
            os.environ.get("SEEN_RECORDING_TTL_S", str(30 * 24 * 3600))
        ),
        capture_fixture=os.environ.get("POCKET_CAPTURE_FIXTURE", "").lower() in ("1", "true", "yes"),
    )


def load_lua_script(name: str) -> str:
    return (LUA_DIR / f"{name}.lua").read_text()
