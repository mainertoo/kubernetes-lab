"""Redis state-machine wrapper + Lua script registration (plan §7.6)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator

import redis.asyncio as redis  # type: ignore

from config import Config, load_lua_script

log = logging.getLogger(__name__)


def state_key(rid: str) -> str:
    return f"pocket:state:{rid}"


def lock_key(rid: str) -> str:
    return f"pocket:lock:{rid}"


def ids_key(rid: str) -> str:
    return f"pocket:ids:{rid}"


def embed_pending_at_key(rid: str) -> str:
    return f"pocket:embed_pending_at:{rid}"


def embed_stalled_key(rid: str) -> str:
    return f"pocket:embed_stalled:{rid}"


def tag_cache_key(tag: str) -> str:
    return f"pocket:tag:{tag}"


def utc_iso_now() -> str:
    """Wall-clock UTC ISO 8601 (plan v11 P10-001)."""
    return datetime.now(timezone.utc).isoformat()


def parse_iso_age_seconds(iso: str) -> float:
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()


@dataclass
class AcquireResult:
    action: str  # dedup | embed_pending | in_progress | resume | start
    current_state: str
    owner_uuid: str  # empty if no lock taken


@dataclass
class AdvanceResult:
    ok: bool
    new_or_reason: str
    current_state: str = ""


class StateMachine:
    """Wraps Redis + 6 Lua scripts. All public methods are async."""

    def __init__(self, cfg: Config, r: redis.Redis):
        self.cfg = cfg
        self.r = r
        # SCRIPT LOAD all 6 once at startup (plan §7.6)
        self._sha: dict[str, str] = {}

    async def setup(self) -> None:
        for name in (
            "acquire_and_dispatch",
            "refresh_lock",
            "release_lock",
            "advance_state",
            "complete_after_embed",
            "repair_revert_to_received",
        ):
            self._sha[name] = await self.r.script_load(load_lua_script(name))

    async def acquire_and_dispatch(self, rid: str, owner_uuid: str) -> AcquireResult:
        res = await self.r.evalsha(
            self._sha["acquire_and_dispatch"],
            2,
            state_key(rid),
            lock_key(rid),
            self.cfg.lease_ttl_seconds,
            owner_uuid,
        )
        return AcquireResult(
            action=res[0].decode() if isinstance(res[0], bytes) else res[0],
            current_state=res[1].decode() if isinstance(res[1], bytes) else res[1],
            owner_uuid=(res[2].decode() if isinstance(res[2], bytes) else res[2]) or "",
        )

    async def refresh_lock(self, rid: str, owner_uuid: str) -> bool:
        ok = await self.r.evalsha(
            self._sha["refresh_lock"],
            1,
            lock_key(rid),
            owner_uuid,
            self.cfg.lease_ttl_seconds,
        )
        return bool(ok)

    async def release_lock(self, rid: str, owner_uuid: str) -> bool:
        ok = await self.r.evalsha(self._sha["release_lock"], 1, lock_key(rid), owner_uuid)
        return bool(ok)

    async def advance_state(
        self,
        rid: str,
        owner_uuid: str,
        new_state: str,
        allowed_prior: list[str],
        ttl_seconds: int | None = None,
    ) -> AdvanceResult:
        ttl = ttl_seconds if ttl_seconds is not None else self.cfg.state_ttl_seconds
        res = await self.r.evalsha(
            self._sha["advance_state"],
            2,
            state_key(rid),
            lock_key(rid),
            owner_uuid,
            new_state,
            ttl,
            *allowed_prior,
        )
        ok = bool(res[0])
        if ok:
            return AdvanceResult(True, res[1].decode() if isinstance(res[1], bytes) else res[1])
        reason = res[1].decode() if isinstance(res[1], bytes) else res[1]
        current = res[2].decode() if isinstance(res[2], bytes) else res[2]
        return AdvanceResult(False, reason, current)

    async def complete_after_embed(self, rid: str) -> AdvanceResult:
        res = await self.r.evalsha(
            self._sha["complete_after_embed"],
            2,
            state_key(rid),
            embed_stalled_key(rid),
            self.cfg.state_ttl_seconds,
        )
        ok = bool(res[0])
        msg = res[1].decode() if isinstance(res[1], bytes) else res[1]
        if ok:
            return AdvanceResult(True, msg)
        current = res[2].decode() if isinstance(res[2], bytes) else (res[2] if len(res) > 2 else "")
        return AdvanceResult(False, msg, current)

    async def repair_revert_to_received(self, rid: str) -> AdvanceResult:
        res = await self.r.evalsha(self._sha["repair_revert_to_received"], 1, state_key(rid))
        ok = bool(res[0])
        msg = res[1].decode() if isinstance(res[1], bytes) else res[1]
        if ok:
            return AdvanceResult(True, msg)
        current = res[2].decode() if isinstance(res[2], bytes) else (res[2] if len(res) > 2 else "")
        return AdvanceResult(False, msg, current)

    # Convenience accessors over pocket:ids:<rid> Hash
    async def store_source_id(self, rid: str, source_id: str) -> None:
        await self.r.hset(ids_key(rid), "source_id", source_id)
        await self.r.expire(ids_key(rid), self.cfg.state_ttl_seconds)

    async def store_note_id(self, rid: str, kind: str, note_id: str) -> None:
        await self.r.hset(ids_key(rid), f"note_{kind}_id", note_id)
        await self.r.expire(ids_key(rid), self.cfg.state_ttl_seconds)

    async def get_ids(self, rid: str) -> dict[str, str]:
        raw = await self.r.hgetall(ids_key(rid))
        return {
            (k.decode() if isinstance(k, bytes) else k): (
                v.decode() if isinstance(v, bytes) else v
            )
            for k, v in raw.items()
        }

    async def clear_ids(self, rid: str) -> None:
        await self.r.delete(ids_key(rid))

    # Timestamps for §7.8 age math (plan v11 P10-001)
    async def mark_embed_pending_at(self, rid: str) -> None:
        await self.r.set(
            embed_pending_at_key(rid),
            utc_iso_now(),
            ex=self.cfg.embed_pending_at_ttl_seconds,
        )

    async def get_embed_pending_age_s(self, rid: str) -> float:
        v = await self.r.get(embed_pending_at_key(rid))
        if not v:
            return 0.0
        iso = v.decode() if isinstance(v, bytes) else v
        return parse_iso_age_seconds(iso)

    # Stalled marker (plan §7.7/§7.8)
    async def set_stalled_marker_if_absent(self, rid: str) -> bool:
        return bool(
            await self.r.set(
                embed_stalled_key(rid),
                "1",
                ex=self.cfg.embed_stalled_marker_ttl_seconds,
                nx=True,
            )
        )

    # Tag cache (plan §7.4)
    async def cache_tag(self, tag: str, notebook_id: str) -> None:
        await self.r.set(tag_cache_key(tag), notebook_id)

    async def get_cached_tag(self, tag: str) -> str | None:
        v = await self.r.get(tag_cache_key(tag))
        if v is None:
            return None
        return v.decode() if isinstance(v, bytes) else v

    async def evict_tag(self, tag: str) -> None:
        await self.r.delete(tag_cache_key(tag))

    # Lock force-delete (plan §7.3 force_delete_lock=true)
    async def force_delete_lock(self, rid: str) -> bool:
        return bool(await self.r.delete(lock_key(rid)))

    # State reset (plan §7.3 reset_state=true)
    async def reset_state(self, rid: str) -> None:
        await self.r.delete(state_key(rid), embed_pending_at_key(rid), embed_stalled_key(rid))

    # §7.8 scanning support
    async def scan_state_keys(self) -> AsyncIterator[str]:
        async for k in self.r.scan_iter(match="pocket:state:*", count=200):
            yield k.decode() if isinstance(k, bytes) else k

    async def has_stalled_marker(self, rid: str) -> bool:
        return bool(await self.r.exists(embed_stalled_key(rid)))

    async def ping(self) -> bool:
        try:
            return bool(await self.r.ping())
        except Exception:
            return False


async def make_redis(cfg: Config) -> redis.Redis:
    return redis.Redis(host=cfg.redis_host, port=cfg.redis_port, decode_responses=False)
