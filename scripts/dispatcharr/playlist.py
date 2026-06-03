#!/usr/bin/env python3
"""
playlist.py — playlist-as-code for Dispatcharr.

Workflow:
  1. export  — pull matching streams from your M3U sources into an editable YAML spec
  2. (edit)  — hand-edit the spec: rename, renumber, group, merge streams for failover,
               drop the ones you don't want, set tvg_id for EPG
  3. apply   — idempotently create channel-groups + channels and bundle them into a
               Channel Profile (the thing Plex/Jellyfin point at). Dry-run by default.
  4. urls    — print the Plex (HDHomeRun) and Jellyfin (M3U + XMLTV) output URLs

A "playlist" in Dispatcharr == a Channel Profile. Output is served per-profile.

Auth: reads scripts/dispatcharr/credentials.sops.yaml via `sops -d`, or env
DISPATCHARR_URL / DISPATCHARR_API_KEY. Header sent: `Authorization: ApiKey <key>`.

Stdlib only (urllib) + PyYAML. No pip install needed.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: pip3 install pyyaml")

HERE = Path(__file__).resolve().parent
CRED_FILE = HERE / "credentials.sops.yaml"


# ─────────────────────────── auth / http ───────────────────────────
def load_creds() -> tuple[str, str]:
    url = os.environ.get("DISPATCHARR_URL")
    key = os.environ.get("DISPATCHARR_API_KEY")
    if url and key:
        return url.rstrip("/"), key
    if not CRED_FILE.exists():
        sys.exit(f"No env creds and {CRED_FILE} not found.")
    try:
        out = subprocess.run(
            ["sops", "-d", str(CRED_FILE)], capture_output=True, text=True, check=True
        ).stdout
    except subprocess.CalledProcessError as e:
        sys.exit(f"sops -d failed: {e.stderr}")
    data = (yaml.safe_load(out) or {}).get("stringData", {})
    url = url or data.get("DISPATCHARR_URL")
    key = key or data.get("DISPATCHARR_API_KEY")
    if not (url and key):
        sys.exit("credentials file missing DISPATCHARR_URL / DISPATCHARR_API_KEY")
    return url.rstrip("/"), key


class Client:
    def __init__(self, base: str, key: str, insecure: bool = False):
        self.base = base
        self.key = key
        self.ctx = ssl._create_unverified_context() if insecure else None

    def _req(self, method: str, path: str, body=None, params=None) -> object:
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v not in (None, "")}
            )
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"ApiKey {self.key}")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, context=self.ctx) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            sys.exit(f"{method} {path} -> {e.code} {e.reason}\n{e.read().decode()[:800]}")

    def get(self, path, params=None):
        return self._req("GET", path, params=params)

    def post(self, path, body):
        return self._req("POST", path, body=body)

    def patch(self, path, body):
        return self._req("PATCH", path, body=body)

    def paginate(self, path, params=None):
        """Yield results across DRF pages."""
        params = dict(params or {})
        params["page"] = 1
        while True:
            page = self.get(path, params)
            if isinstance(page, list):  # non-paginated endpoint
                yield from page
                return
            yield from page.get("results", [])
            if not page.get("next"):
                return
            params["page"] += 1


# ─────────────────────────── commands ───────────────────────────
def cmd_sources(c: Client, _a):
    for s in c.get("/api/m3u/accounts/"):
        groups = len(s.get("channel_groups", []))
        print(f"  [{s['id']:>3}] {s['name']:<28} active={s['is_active']} "
              f"max_streams={s.get('max_streams')} groups={groups}")


def cmd_groups(c: Client, a):
    rows = sorted(c.get("/api/channels/groups/"), key=lambda g: g["name"].lower())
    for g in rows:
        streams = sum(m.get("stream_count", 0) for m in g.get("m3u_accounts", []))
        if a.nonempty and streams == 0 and g.get("channel_count", 0) == 0:
            continue
        print(f"  [{g['id']:>4}] {g['name']:<40} channels={g.get('channel_count',0):>4} "
              f"streams~{streams}")


def _query_streams(c: Client, a):
    params = {
        "name": a.name,
        "channel_group_name": a.group,  # OrIn: comma-separated, icontains
        "m3u_account": a.account,
        "m3u_account_name": a.account_name,
        "search": a.search,
        "ordering": "name",
    }
    gmap = {g["id"]: g["name"] for g in c.get("/api/channels/groups/")}
    amap = {s["id"]: s["name"] for s in c.get("/api/m3u/accounts/")}
    out = []
    for s in c.paginate("/api/channels/streams/", params):
        if getattr(a, "no_adult", False) and s.get("is_adult"):
            continue
        s["_group"] = gmap.get(s.get("channel_group"), "")
        s["_acct"] = amap.get(s.get("m3u_account"), str(s.get("m3u_account")))
        out.append(s)
    return out


def cmd_streams(c: Client, a):
    rows = _query_streams(c, a)
    for s in rows:
        print(f"  [{s['id']:>6}] {s['_acct']:<16} {s['_group']:<22} "
              f"tvg={s.get('tvg_id') or '-':<18} {s['name']}")
    print(f"  ── {len(rows)} streams")


def _redact(url: str) -> str:
    """Strip Xtream creds from a stream URL so exported specs are safe to commit."""
    if not url:
        return ""
    import re
    url = re.sub(r"(/(?:live|movie|series))/[^/]+/[^/]+/", r"\1/REDACTED/REDACTED/", url)
    url = re.sub(r"(username|password|token)=[^&]*", r"\1=REDACTED", url)
    return url


def _norm(name: str) -> str:
    """Normalize a stream name for cross-source merge matching."""
    import re
    n = name.lower()
    n = re.sub(r"[▀-▟■-◿⬀-⯿ᴬ-ᵪ]", "", n)  # decorative
    n = re.sub(r"\b(fhd|uhd|4k|hd|sd|h265|hevc|raw|backup|us|usa|ca|\(.*?\))\b", "", n)
    n = re.sub(r"[^a-z0-9]+", " ", n).strip()
    return n


def cmd_export(c: Client, a):
    rows = _query_streams(c, a)
    if a.merge:
        buckets: dict = {}
        for s in rows:
            key = ("tvg:" + s["tvg_id"]) if s.get("tvg_id") else ("name:" + _norm(s["name"]))
            buckets.setdefault(key, []).append(s)
        channels = []
        for _, group in sorted(buckets.items(), key=lambda kv: kv[1][0]["name"].lower()):
            first = group[0]
            channels.append({
                "name": first["name"],
                "number": None,
                "group": first["_group"],
                "tvg_id": first.get("tvg_id") or "",
                "streams": [s["id"] for s in group],     # multi-source = failover
                "enabled": True,
                "_sources": [f"{s['_acct']}:{s['id']}" for s in group],
            })
            if a.limit and len(channels) >= a.limit:
                break
    else:
        channels = [{
            "name": s["name"], "number": None, "group": s["_group"],
            "tvg_id": s.get("tvg_id") or "", "streams": [s["id"]], "enabled": True,
            "_source": _redact(s.get("url", "")), "_account": s["_acct"],
        } for s in rows[: a.limit or None]]
    spec = {"profile": a.profile, "auto_number_start": a.start, "channels": channels}
    out = Path(a.output)
    out.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=200))
    print(f"Wrote {len(channels)} channels ({len(rows)} streams) -> {out}")
    print("Edit it, then:  ./playlist.py apply", out, " (add --commit to write)")


def _resolve_group(c: Client, name: str, cache: dict, commit: bool):
    if not name:
        return None
    if name in cache:
        return cache[name]
    existing = {g["name"]: g["id"] for g in c.get("/api/channels/groups/")}
    if name in existing:
        cache[name] = existing[name]
        return cache[name]
    if not commit:
        print(f"    + would create group '{name}'")
        cache[name] = f"<new:{name}>"
        return cache[name]
    g = c.post("/api/channels/groups/", {"name": name})
    print(f"    + created group '{name}' id={g['id']}")
    cache[name] = g["id"]
    return g["id"]


def cmd_apply(c: Client, a):
    spec = yaml.safe_load(Path(a.spec).read_text())
    profile_name = spec["profile"]
    commit = a.commit
    auto = spec.get("auto_number_start")
    next_num = float(auto) if auto else None

    # name -> existing channel id (global)
    existing = {ch["name"]: ch["id"] for ch in c.paginate("/api/channels/channels/")}
    gcache: dict = {}
    assigned_ids: list[int] = []
    created = updated = 0

    print(f"{'COMMIT' if commit else 'DRY-RUN'}: profile '{profile_name}', "
          f"{len(spec['channels'])} channels")
    for ch in spec["channels"]:
        payload = {
            "name": ch["name"],
            "streams": ch.get("streams", []),
            "tvg_id": ch.get("tvg_id") or "",
        }
        num = ch.get("number")
        if num in (None, "") and next_num is not None:
            num, next_num = next_num, next_num + 1
        if num not in (None, ""):
            payload["channel_number"] = float(num)
        gid = _resolve_group(c, ch.get("group", ""), gcache, commit)
        if isinstance(gid, int):
            payload["channel_group_id"] = gid

        cid = ch.get("id") if a.isolate else (ch.get("id") or existing.get(ch["name"]))
        if cid:
            print(f"  ~ update [{cid}] {ch['name']}  num={payload.get('channel_number')}")
            if commit:
                c.patch(f"/api/channels/channels/{cid}/", payload)
            updated += 1
        else:
            print(f"  + create     {ch['name']}  num={payload.get('channel_number')} "
                  f"streams={payload['streams']}")
            if commit:
                res = c.post("/api/channels/channels/", payload)
                cid = res["id"]
                ch["id"] = cid  # write back for exact re-apply
            created += 1
        if isinstance(cid, int):
            assigned_ids.append(cid)

    # ensure profile + membership
    profiles = {p["name"]: p["id"] for p in c.get("/api/channels/profiles/")}
    pid = profiles.get(profile_name)
    if not pid:
        print(f"  + {'create' if commit else 'would create'} profile '{profile_name}'")
        if commit:
            pid = c.post("/api/channels/profiles/", {"name": profile_name})["id"]
    if commit and pid:
        # New profiles auto-enable ALL channels — set an exact roster: ours on, rest off.
        # Scoped to this profile only; other profiles are untouched.
        want = set(assigned_ids)
        all_ids = [ch["id"] for ch in c.paginate("/api/channels/channels/")]
        c.patch(
            f"/api/channels/profiles/{pid}/channels/bulk-update/",
            {"channels": [{"channel_id": i, "enabled": i in want} for i in all_ids]},
        )
        print(f"  = profile '{profile_name}': {len(want)} enabled, "
              f"{len(all_ids) - len(want)} disabled")
        Path(a.spec).write_text(
            yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=200)
        )

    print(f"\n{'Committed' if commit else 'Planned'}: {created} created, {updated} updated.")
    if not commit:
        print("Re-run with --commit to apply.")
    else:
        _print_urls(c.base, profile_name)


def _print_urls(base: str, profile: str):
    p = urllib.parse.quote(profile)
    print(f"\nOutput URLs for profile '{profile}':")
    print(f"  M3U (Jellyfin tuner):   {base}/output/m3u/{p}")
    print(f"  XMLTV (Jellyfin guide): {base}/output/epg/{p}")
    print(f"  HDHomeRun (Plex):       {base}/hdhr/{p}   (add as DVR/tuner in Plex)")


def cmd_urls(c: Client, a):
    _print_urls(c.base, a.profile)


# ─────────────────────────── cli ───────────────────────────
def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--insecure", action="store_true", help="skip TLS verification")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("sources").set_defaults(fn=cmd_sources)

    g = sub.add_parser("groups")
    g.add_argument("--nonempty", action="store_true", help="hide empty groups")
    g.set_defaults(fn=cmd_groups)

    def add_filters(sp):
        sp.add_argument("--name", help="stream name contains")
        sp.add_argument("--group", help="channel group name(s), comma-sep, contains")
        sp.add_argument("--account", help="m3u account id(s), comma-separated")
        sp.add_argument("--account-name", help="m3u account name contains")
        sp.add_argument("--search", help="free-text search (name or group)")
        sp.add_argument("--no-adult", action="store_true", help="drop is_adult streams")

    ls = sub.add_parser("streams", help="list matching streams (id/account/group/name)")
    add_filters(ls)
    ls.set_defaults(fn=cmd_streams)

    e = sub.add_parser("export", help="dump matching streams to an editable spec")
    add_filters(e)
    e.add_argument("-o", "--output", default="playlist.yaml")
    e.add_argument("--profile", default="My Playlist", help="target Channel Profile name")
    e.add_argument("--merge", action="store_true",
                   help="merge same-named streams across sources into one channel (failover)")
    e.add_argument("--limit", type=int, help="cap number of channels exported")
    e.add_argument("--start", type=int, default=1000, help="auto-number start")
    e.set_defaults(fn=cmd_export)

    ap = sub.add_parser("apply", help="reconcile a spec into Dispatcharr (dry-run default)")
    ap.add_argument("spec")
    ap.add_argument("--commit", action="store_true", help="actually write changes")
    ap.add_argument("--isolate", action="store_true",
                    help="never reuse existing channels by name — create dedicated ones "
                         "(self-contained playlist; won't touch your other profiles)")
    ap.set_defaults(fn=cmd_apply)

    u = sub.add_parser("urls")
    u.add_argument("profile")
    u.set_defaults(fn=cmd_urls)

    a = p.parse_args()
    base, key = load_creds()
    a.fn(Client(base, key, insecure=a.insecure), a)


if __name__ == "__main__":
    main()
