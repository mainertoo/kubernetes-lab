"""Open Notebook API client (plan §6 pinned contracts).

Hardcoded against the 2026-05-30 fixture at
apps/base/pocket-bridge/contracts/open-notebook-2026-05-30.json. Each
method matches one §6 row with the documented verb + success code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from config import Config

log = logging.getLogger(__name__)


class OpenNotebookError(Exception):
    def __init__(self, msg: str, status_code: int = 0):
        super().__init__(msg)
        self.status_code = status_code


@dataclass
class SourceInfo:
    id: str
    title: str
    embedded: bool
    embedded_chunks: int
    status: str | None


class OpenNotebookClient:
    """Async HTTP client. Bearer optional (plan §6 auth note)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        headers = {"Content-Type": "application/json"}
        if cfg.open_notebook_api_key:
            headers["Authorization"] = f"Bearer {cfg.open_notebook_api_key}"
        self._client = httpx.AsyncClient(
            base_url=cfg.open_notebook_base_url,
            headers=headers,
            timeout=httpx.Timeout(30.0, connect=5.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def ping(self) -> bool:
        try:
            r = await self._client.get("/api/auth/status")
            return r.status_code == 200
        except Exception:
            return False

    # ---- notebooks ----------------------------------------------------------
    async def list_notebooks(self) -> list[dict[str, Any]]:
        r = await self._client.get("/api/notebooks")
        self._require(r, 200, "list_notebooks")
        return r.json()

    async def create_notebook(self, name: str, description: str = "") -> dict[str, Any]:
        r = await self._client.post(
            "/api/notebooks", json={"name": name, "description": description}
        )
        # Live API at 2026-05-31 returns 200, NOT 201 as the v8 fixture
        # documented. Re-pinned via the same probe; fixture file updated in
        # this PR. Only this endpoint drifted; others still match.
        self._require(r, 200, "create_notebook")
        return r.json()

    # ---- sources ------------------------------------------------------------
    async def create_source_text(
        self,
        *,
        notebook_ids: list[str],
        content: str,
        title: str,
        async_processing: bool = True,
    ) -> dict[str, Any]:
        """POST /api/sources/json — embed=true required (plan D13/F16)."""
        body = {
            "type": "text",
            "notebooks": notebook_ids,
            "content": content,
            "title": title,
            "transformations": [],
            "embed": True,
            "async_processing": async_processing,
        }
        r = await self._client.post("/api/sources/json", json=body)
        self._require(r, 200, "create_source_text")
        return r.json()

    async def get_source(self, source_id: str) -> dict[str, Any] | None:
        r = await self._client.get(f"/api/sources/{source_id}")
        if r.status_code == 404:
            return None
        self._require(r, 200, "get_source")
        return r.json()

    async def delete_source(self, source_id: str) -> bool:
        """Returns True on success, False if 404 (already gone)."""
        r = await self._client.delete(f"/api/sources/{source_id}")
        if r.status_code in (200, 204):
            return True
        if r.status_code == 404:
            return False
        raise OpenNotebookError(f"delete_source {source_id}: HTTP {r.status_code}", r.status_code)

    async def list_sources_for_notebook(
        self, notebook_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """GET /api/sources — paginated, max 100/page (plan §6 v6 F5-003)."""
        r = await self._client.get(
            "/api/sources",
            params={
                "notebook_id": notebook_id,
                "limit": limit,
                "offset": offset,
                "sort_by": "updated",
                "sort_order": "desc",
            },
        )
        self._require(r, 200, "list_sources_for_notebook")
        return r.json()

    async def find_source_by_marker(
        self, notebook_id: str, marker: str, *, max_pages: int = 5
    ) -> dict[str, Any] | None:
        """D16 / plan §7.1 step 9b — paginated marker-lookup, 5-page cap default."""
        for page in range(max_pages):
            items = await self.list_sources_for_notebook(
                notebook_id, limit=100, offset=page * 100
            )
            for it in items:
                t = it.get("title") or ""
                if marker in t:
                    return it
            if len(items) < 100:
                return None
        return None

    async def any_source_with_marker(
        self, notebook_id: str, marker: str, *, max_pages: int = 5
    ) -> bool:
        """Plan v11 P10-004 helper — bounded existence check."""
        return await self.find_source_by_marker(notebook_id, marker, max_pages=max_pages) is not None

    # ---- notes --------------------------------------------------------------
    async def list_notes_for_notebook(self, notebook_id: str) -> list[dict[str, Any]]:
        """GET /api/notes — single-shot, no pagination (plan §6)."""
        r = await self._client.get("/api/notes", params={"notebook_id": notebook_id})
        self._require(r, 200, "list_notes_for_notebook")
        return r.json()

    async def create_note(
        self, *, notebook_id: str, title: str, content: str, note_type: str = "human"
    ) -> dict[str, Any]:
        body = {
            "title": title,
            "content": content,
            "note_type": note_type,
            "notebook_id": notebook_id,
        }
        r = await self._client.post("/api/notes", json=body)
        self._require(r, 200, "create_note")
        return r.json()

    async def get_note(self, note_id: str) -> dict[str, Any] | None:
        r = await self._client.get(f"/api/notes/{note_id}")
        if r.status_code == 404:
            return None
        self._require(r, 200, "get_note")
        return r.json()

    # ---- internal -----------------------------------------------------------
    def _require(self, r: httpx.Response, expected: int, op: str) -> None:
        if r.status_code != expected:
            raise OpenNotebookError(
                f"{op}: expected HTTP {expected}, got {r.status_code}: {r.text[:200]}",
                r.status_code,
            )
