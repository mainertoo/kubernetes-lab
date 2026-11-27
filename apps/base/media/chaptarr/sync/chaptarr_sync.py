#!/usr/bin/env python3
"""Two-way, loop-free book sync between Chaptarr and the existing book stack.

Chaptarr keeps its OWN library folders (books/chaptarr-{audiobooks,ebooks}).
This job, run by a CronJob, adds books across the boundary. It never deletes,
renames or moves anything in the existing libraries:

  INBOUND  (existing stack -> Chaptarr)
    * audiobook folders that appear in books/audiobooks (Audiobookshelf /
      ReadMeABook) or books/shelfmark-audiobooks are imported into Chaptarr
      with ManualImport in COPY mode. Chaptarr has "use hardlinks" on and these
      are on the same NFS filesystem, so the import is a hardlink (no extra
      space) and the source is untouched.
    * ebooks that appear in the Calibre library (CWA / Shelfmark) are copied
      (CephFS -> NFS, so a real copy) into an inbox Chaptarr can see, then
      imported with ManualImport in MOVE mode (inbox -> Chaptarr root).
    Chaptarr identifies each book itself (manualimport preview ->
    suggestedForeignAuthorId/BookId) and adds unknown authors monitoring only
    that SPECIFIC book, so it does not start grabbing the author's catalogue.

  OUTBOUND (Chaptarr downloads -> existing stack)
    * only books Chaptarr itself DOWNLOADED (history BookFileImported events
      with a download client) are exported:
        - audiobooks: hardlinked into books/audiobooks/<same relative path>
          (Audiobookshelf; ReadMeABook sees them through Audiobookshelf)
        - ebooks: copied into the CWA ingest folder (CWA files them into
          Calibre; Shelfmark sees them through CWA)

  LOOP GUARD (SQLite ledger on its own PVC)
    * folders created by OUTBOUND are recorded and never imported back;
    * files Chaptarr imported via INBOUND have no download client, so they
      never qualify for OUTBOUND; their dropped paths are also recorded;
    * ebooks exported to CWA are remembered by normalised author+title, so
      the Calibre copy CWA makes is not imported back into Chaptarr.

  FIRST RUN = BASELINE. Everything already in the libraries is recorded as
  "baseline" and NOT imported (new books only), unless BACKFILL=true.

  MODE=dry-run (default) only reads and logs what it would do; the only state
  it writes is the baseline/pending bookkeeping in the ledger. MODE=apply acts.

Standard library only (runs on python:alpine).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_bool(name: str, default: bool = False) -> bool:
    v = env(name, "true" if default else "false").lower()
    return v in ("1", "true", "yes", "on")


MODE = env("MODE", "dry-run").lower()
APPLY = MODE == "apply"
INBOUND = env_bool("INBOUND", True)
OUTBOUND = env_bool("OUTBOUND", True)
BACKFILL = env_bool("BACKFILL", False)

CHAPTARR_URL = env("CHAPTARR_URL", "http://chaptarr.media.svc.cluster.local:8789").rstrip("/")
CHAPTARR_API_KEY = env("CHAPTARR_API_KEY")

AUDIO_SOURCES = [p for p in env(
    "AUDIO_SOURCES",
    "/data/media/books/audiobooks:/data/media/books/shelfmark-audiobooks").split(":") if p]
CALIBRE_LIBRARY = env("CALIBRE_LIBRARY", "/calibre-library")
CHAPTARR_AUDIO_ROOT = env("CHAPTARR_AUDIO_ROOT", "/data/media/books/chaptarr-audiobooks")
CHAPTARR_EBOOK_ROOT = env("CHAPTARR_EBOOK_ROOT", "/data/media/books/chaptarr-ebooks")
INBOX = env("INBOX", "/data/media/books/.chaptarr-sync-inbox")
EXPORT_AUDIO_DEST = env("EXPORT_AUDIO_DEST", "/data/media/books/audiobooks")
CWA_INGEST = env("CWA_INGEST", "/cwa-book-ingest")
STATE_DB = env("STATE_DB", "/state/ledger.sqlite")
REPORT = env("REPORT", "/state/last-run.json")

MIN_AGE_MINUTES = int(env("MIN_AGE_MINUTES", "30"))
MAX_IMPORTS_PER_RUN = int(env("MAX_IMPORTS_PER_RUN", "20"))
MAX_ATTEMPTS = int(env("MAX_ATTEMPTS", "6"))
COMMAND_TIMEOUT = int(env("COMMAND_TIMEOUT", "900"))

AUDIO_EXT = {".m4b", ".mp3", ".m4a", ".flac", ".opus", ".aac", ".ogg", ".wma"}
EBOOK_PREF = [".epub", ".azw3", ".mobi", ".pdf", ".kepub"]
EBOOK_EXT = set(EBOOK_PREF)

log = logging.getLogger("chaptarr-sync")
report: dict = {"mode": MODE, "started": datetime.now(timezone.utc).isoformat(),
                "inbound": {}, "outbound": {}, "errors": []}


# --------------------------------------------------------------------- ledger
def db_open() -> sqlite3.Connection:
    Path(STATE_DB).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(STATE_DB)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS items (
            kind TEXT, path TEXT, status TEXT, attempts INTEGER DEFAULT 0,
            first_seen TEXT, last_try TEXT, note TEXT,
            PRIMARY KEY (kind, path));
        CREATE TABLE IF NOT EXISTS dropped (path TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS origin (path TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS exported_titles (key TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS exports (
            history_id INTEGER PRIMARY KEY, kind TEXT, src TEXT, dest TEXT,
            status TEXT, ts TEXT);
    """)
    return con


def meta_get(con, key, default=None):
    r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def meta_set(con, key, value):
    con.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ----------------------------------------------------------------- chaptarr api
def api(method: str, path: str, body=None, timeout=120):
    url = f"{CHAPTARR_URL}/api/v1/{path.lstrip('/')}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "X-Api-Key": CHAPTARR_API_KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Chaptarr {method} {path} -> HTTP {e.code}: {e.read().decode()[:300]}")


def run_command(body: dict) -> dict:
    cmd = api("POST", "command", body)
    cid = cmd["id"]
    deadline = time.time() + COMMAND_TIMEOUT
    while time.time() < deadline:
        c = api("GET", f"command/{cid}")
        if c.get("status") in ("completed", "failed", "aborted", "cancelled", "orphaned"):
            return c
        time.sleep(3)
    raise RuntimeError(f"command {body.get('name')} {cid} did not finish in {COMMAND_TIMEOUT}s")


def history_imports(since_id: int = 0, max_pages: int = 50):
    """BookFileImported (eventType 3) records, newest first, id > since_id."""
    out = []
    for page in range(1, max_pages + 1):
        res = api("GET", f"history?page={page}&pageSize=100&sortKey=date&sortDirection=descending"
                         f"&eventType=3&includeAuthor=true&includeBook=true")
        recs = res.get("records", []) if isinstance(res, dict) else res
        if not recs:
            break
        stop = False
        for r in recs:
            if r["id"] <= since_id:
                stop = True
                break
            out.append(r)
        if stop or len(recs) < 100:
            break
    return out


def hdata(rec: dict, key: str) -> str:
    d = rec.get("data") or {}
    for k in (key, key[0].lower() + key[1:], key.lower()):
        if d.get(k):
            return str(d[k])
    return ""


# -------------------------------------------------------------------- helpers
def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def title_key(author: str, title: str) -> str:
    # word-order-insensitive author ("Alanson, Craig" == "Craig Alanson")
    a = " ".join(sorted(norm(author).split()))
    return f"{a}|{norm(title)}"


def newest_mtime(d: Path) -> float:
    m = 0.0
    for f in d.iterdir():
        try:
            m = max(m, f.stat().st_mtime)
        except OSError:
            pass
    return m


def under(p: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.abspath(p), os.path.abspath(root)]) == os.path.abspath(root)
    except ValueError:
        return False


def audio_book_dirs(root: str):
    """Directories that DIRECTLY contain audio files = one book each."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        if any(Path(f).suffix.lower() in AUDIO_EXT for f in filenames):
            yield dirpath


def calibre_book_dirs(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        if any(Path(f).suffix.lower() in EBOOK_EXT for f in filenames):
            yield dirpath


def best_ebook(d: str) -> Path | None:
    files = [p for p in Path(d).iterdir() if p.suffix.lower() in EBOOK_EXT]
    files.sort(key=lambda p: EBOOK_PREF.index(p.suffix.lower()))
    return files[0] if files else None


def opf_author_title(d: str):
    opf = Path(d) / "metadata.opf"
    if not opf.exists():
        return "", ""
    try:
        t = ET.parse(opf)
        ns = {"dc": "http://purl.org/dc/elements/1.1/"}
        title = (t.find(".//dc:title", ns).text or "") if t.find(".//dc:title", ns) is not None else ""
        creator = (t.find(".//dc:creator", ns).text or "") if t.find(".//dc:creator", ns) is not None else ""
        return creator, title
    except ET.ParseError:
        return "", ""


# -------------------------------------------------------------------- inbound
def discover(con) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    origin = {r[0] for r in con.execute("SELECT path FROM origin")}
    for src in AUDIO_SOURCES:
        if not os.path.isdir(src):
            log.warning("audio source missing: %s", src)
            continue
        for d in audio_book_dirs(src):
            if any(under(d, o) for o in origin) or under(d, CHAPTARR_AUDIO_ROOT) or under(d, INBOX):
                continue
            found.append(("audio", d))
    if os.path.isdir(CALIBRE_LIBRARY):
        for d in calibre_book_dirs(CALIBRE_LIBRARY):
            found.append(("ebook", d))
    else:
        log.warning("calibre library missing: %s", CALIBRE_LIBRARY)
    return found


def inbound(con):
    stats = {"discovered": 0, "baseline": 0, "new_pending": 0, "too_fresh": 0, "imported": 0,
             "would_import": 0, "unmatched": 0, "retry": 0, "failed": 0, "skipped_exported": 0}
    found = discover(con)
    stats["discovered"] = len(found)
    known = {(k, p) for k, p in con.execute("SELECT kind, path FROM items")}

    if meta_get(con, "baseline_done") is None:
        status = "pending" if BACKFILL else "baseline"
        con.executemany("INSERT OR IGNORE INTO items(kind,path,status,first_seen) VALUES(?,?,?,?)",
                        [(k, p, status, now()) for k, p in found])
        meta_set(con, "baseline_done", now())
        con.commit()
        stats["baseline"] = 0 if BACKFILL else len(found)
        log.info("first run: recorded %d existing book folders as %s%s", len(found), status,
                 " (BACKFILL)" if BACKFILL else " (new books only from now on)")
        if not BACKFILL:
            report["inbound"] = stats
            return
        known = {(k, p) for k, p in found}
    elif BACKFILL:
        # BACKFILL turned on after the baseline was recorded: queue the baseline
        # (the pre-existing library) for import. Pending items are processed
        # MAX_IMPORTS_PER_RUN at a time, oldest first, so this drains gradually.
        n = con.execute("UPDATE items SET status='pending', note='backfill' WHERE status='baseline'").rowcount
        if n:
            con.commit()
            log.info("BACKFILL: queued %d baseline book folders for import", n)

    cutoff = time.time() - MIN_AGE_MINUTES * 60
    exported = {r[0] for r in con.execute("SELECT key FROM exported_titles")}
    for kind, path in found:
        if (kind, path) in known:
            continue
        if newest_mtime(Path(path)) > cutoff:
            stats["too_fresh"] += 1
            continue
        if kind == "ebook":
            a, t = opf_author_title(path)
            if a and t and title_key(a, t) in exported:
                con.execute("INSERT OR IGNORE INTO items(kind,path,status,first_seen,note) VALUES(?,?,?,?,?)",
                            (kind, path, "exported-origin", now(), "CWA copy of a Chaptarr export"))
                stats["skipped_exported"] += 1
                continue
        con.execute("INSERT OR IGNORE INTO items(kind,path,status,first_seen) VALUES(?,?,?,?)",
                    (kind, path, "pending", now()))
        stats["new_pending"] += 1
    con.commit()

    # genuinely new books first, then the backfill queue (oldest first)
    pending = con.execute("SELECT kind, path, attempts FROM items WHERE status='pending' "
                          "ORDER BY (COALESCE(note,'')='backfill'), first_seen, path LIMIT ?",
                          (MAX_IMPORTS_PER_RUN,)).fetchall()
    for kind, path, attempts in pending:
        try:
            outcome, note = import_one(con, kind, path)
        except Exception as e:  # noqa: BLE001 - keep going, record and retry
            outcome, note = "retry", f"error: {e}"
            report["errors"].append(f"inbound {path}: {e}")
        if outcome == "retry":
            attempts += 1
            status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
            stats["failed" if status == "failed" else "retry"] += 1
        else:
            # dry-run never settles an item: it stays pending for the first apply run
            status = outcome if APPLY else "pending"
            stats[{"imported": "imported", "would_import": "would_import",
                   "unmatched": "unmatched"}[outcome]] += 1
        con.execute("UPDATE items SET status=?, attempts=?, last_try=?, note=? WHERE kind=? AND path=?",
                    (status, attempts, now(), note[:500], kind, path))
        con.commit()
    report["inbound"] = stats


def preview(folder: str) -> list[dict]:
    q = urllib.parse.urlencode({"folder": folder, "filterExistingFiles": "true",
                                "replaceExistingFiles": "false"})
    items = api("GET", f"manualimport?{q}", timeout=600) or []
    # only files that sit DIRECTLY in this folder (sub-folders are their own books)
    return [i for i in items if os.path.dirname(i["path"]) == folder.rstrip("/")]


def import_one(con, kind: str, path: str) -> tuple[str, str]:
    staged = None
    folder = path
    if kind == "ebook":
        src = best_ebook(path)
        if not src:
            return "unmatched", "no ebook file"
        if APPLY:
            staged = Path(INBOX) / re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.relpath(path, CALIBRE_LIBRARY))
            staged.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, staged / src.name)
            folder = str(staged)
        else:
            log.info("[dry-run] ebook %s: would copy %s to the inbox and import it", path, src.name)
            return "would_import", f"would import {src.name}"

    items = preview(folder)
    if not items:
        cleanup(staged)
        return "unmatched", "manualimport found no importable files (already in Chaptarr?)"
    bad = [i for i in items if i.get("rejections") or not i.get("suggestedForeignAuthorId")
           or not i.get("suggestedForeignBookId")]
    books = {(i.get("suggestedForeignAuthorId"), i.get("suggestedForeignBookId")) for i in items}
    if bad or len(books) != 1:
        reason = "; ".join(r.get("reason", "") for i in bad for r in i.get("rejections", [])) \
            or ("no metadata match" if bad else f"files matched {len(books)} different books")
        cleanup(staged)
        return "unmatched", reason
    i0 = items[0]
    label = f"{i0.get('suggestedAuthorName')} / {i0.get('suggestedBookTitle')}"
    if not APPLY:
        log.info("[dry-run] %s %s: would import %d file(s) as %s", kind, path, len(items), label)
        return "would_import", f"would import as {label}"

    files = [{
        "path": i["path"],
        "foreignAuthorId": i.get("suggestedForeignAuthorId"),
        "foreignAuthorName": i.get("suggestedAuthorName"),
        "foreignBookId": i.get("suggestedForeignBookId"),
        "foreignBookTitle": i.get("suggestedBookTitle"),
        "foreignEditionId": i.get("suggestedForeignEditionId"),
        "foreignEditionTitle": i.get("suggestedEditionTitle"),
        "selectionSource": 1,  # UserMetadataSuggestion: lets Chaptarr add the author
        "quality": i.get("quality"),
        "indexerFlags": 0,
        "disableReleaseSwitching": False,
    } for i in items]
    start_id = max([r["id"] for r in history_imports(max_pages=1)] or [0])
    cmd = run_command({"name": "ManualImport", "files": files,
                       "importMode": "move" if kind == "ebook" else "copy",
                       "replaceExistingFiles": False})
    paths = {f["path"] for f in files}
    done = [r for r in history_imports(since_id=start_id) if hdata(r, "DroppedPath") in paths]
    con.executemany("INSERT OR IGNORE INTO dropped(path) VALUES(?)", [(p,) for p in paths])
    if done:
        log.info("imported %s %s as %s (%d file(s))", kind, path, label, len(done))
        if staged and not any(staged.iterdir()):
            cleanup(staged)
        return "imported", f"imported as {label}"
    msg = cmd.get("message") or cmd.get("exception") or cmd.get("status")
    log.warning("not imported yet %s %s: %s", kind, path, msg)
    cleanup(staged)
    return "retry", f"not imported: {msg}"


def cleanup(staged: Path | None):
    if staged and staged.exists() and under(str(staged), INBOX):
        shutil.rmtree(staged, ignore_errors=True)


# ------------------------------------------------------------------- outbound
def outbound(con):
    stats = {"candidates": 0, "exported": 0, "would_export": 0, "skipped": 0, "errors": 0}
    last = int(meta_get(con, "outbound_last_history_id", "0"))
    recs = history_imports(since_id=last)
    if meta_get(con, "outbound_baseline_done") is None:
        newest = max([r["id"] for r in recs] or [last])
        meta_set(con, "outbound_baseline_done", now())
        meta_set(con, "outbound_last_history_id", newest)
        con.commit()
        log.info("first run: outbound starts after history id %d (earlier imports are not exported)", newest)
        report["outbound"] = stats
        return
    dropped = {r[0] for r in con.execute("SELECT path FROM dropped")}
    newest_done = last
    for r in sorted(recs, key=lambda x: x["id"]):
        imported = hdata(r, "ImportedPath")
        if not (r.get("downloadId") and hdata(r, "DownloadClient")) or hdata(r, "DroppedPath") in dropped:
            stats["skipped"] += 1
            newest_done = r["id"]
            continue
        if not imported or not os.path.exists(imported):
            stats["skipped"] += 1
            newest_done = r["id"]
            continue
        stats["candidates"] += 1
        try:
            if under(imported, CHAPTARR_AUDIO_ROOT):
                dest_dir = Path(EXPORT_AUDIO_DEST) / os.path.relpath(os.path.dirname(imported), CHAPTARR_AUDIO_ROOT)
                dest = dest_dir / os.path.basename(imported)
                if not APPLY:
                    log.info("[dry-run] would hardlink audiobook %s -> %s", imported, dest)
                    stats["would_export"] += 1
                    continue
                dest_dir.mkdir(parents=True, exist_ok=True)
                if not dest.exists():
                    os.link(imported, dest)
                con.execute("INSERT OR IGNORE INTO origin(path) VALUES(?)", (str(dest_dir),))
                con.execute("INSERT OR REPLACE INTO exports VALUES(?,?,?,?,?,?)",
                            (r["id"], "audio", imported, str(dest), "exported", now()))
            elif under(imported, CHAPTARR_EBOOK_ROOT):
                dest = Path(CWA_INGEST) / os.path.basename(imported)
                book = (r.get("book") or {})
                author = ((r.get("author") or {}).get("authorName")) or ""
                if not APPLY:
                    log.info("[dry-run] would copy ebook %s -> %s", imported, dest)
                    stats["would_export"] += 1
                    continue
                shutil.copy2(imported, dest)
                if author and book.get("title"):
                    con.execute("INSERT OR IGNORE INTO exported_titles(key) VALUES(?)",
                                (title_key(author, book["title"]),))
                con.execute("INSERT OR REPLACE INTO exports VALUES(?,?,?,?,?,?)",
                            (r["id"], "ebook", imported, str(dest), "exported", now()))
            else:
                stats["skipped"] += 1
                newest_done = r["id"]
                continue
            stats["exported"] += 1
            log.info("exported %s -> %s", imported, dest)
            newest_done = r["id"]
            con.commit()
        except Exception as e:  # noqa: BLE001
            stats["errors"] += 1
            report["errors"].append(f"outbound {imported}: {e}")
            log.error("export failed for %s: %s (will retry next run)", imported, e)
            break  # keep order; retry from here next run
    if APPLY:
        meta_set(con, "outbound_last_history_id", newest_done)
        con.commit()
    report["outbound"] = stats


# ----------------------------------------------------------------------- main
def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    if not CHAPTARR_API_KEY:
        log.error("CHAPTARR_API_KEY is not set")
        return 2
    log.info("chaptarr-sync mode=%s inbound=%s outbound=%s backfill=%s", MODE, INBOUND, OUTBOUND, BACKFILL)
    con = db_open()
    try:
        status = api("GET", "system/status")
        log.info("Chaptarr %s reachable", status.get("version"))
        if INBOUND:
            inbound(con)
        if OUTBOUND:
            outbound(con)
    except Exception as e:  # noqa: BLE001
        report["errors"].append(str(e))
        log.exception("sync aborted")
    finally:
        report["finished"] = datetime.now(timezone.utc).isoformat()
        report["ledger"] = dict(con.execute("SELECT status, COUNT(*) FROM items GROUP BY status").fetchall())
        try:
            Path(REPORT).write_text(json.dumps(report, indent=2))
        except OSError:
            pass
        con.close()
    log.info("summary: %s", json.dumps({k: report[k] for k in ("inbound", "outbound", "ledger")}))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
