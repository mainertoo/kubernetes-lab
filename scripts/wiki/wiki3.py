#!/usr/bin/env python3
"""Wiki.js v3 admin CLI for the homelab wiki (REST API at /_api).

Base URL: $WIKI3_URL, default https://wiki.lab.mainertoo.com (v3 since the 2026-11-11 cutover;
wiki-v3.lab is an alias until v2 is retired).

Auth (secrets never printed), all read from the SOPS file
apps/base/wiki/wiki-secret.sops.yaml in the kubernetes-lab repo:
  - reads use the API key CLAUDE_API_KEY (Administrators group)
  - writes (save/move/delete/render) need a USER in v3 - API keys are refused -
    so the script logs in as ADMIN_EMAIL/ADMIN_PASS (wiki-admin) and uses that
    session cookie. Edits are therefore authored by "Administrator".

v3 does not render markdown on save (the browser editor normally sends the
HTML), so every create/update is followed by a server-side re-render.

Commands:
  wiki3.py list [--filter SUBSTR]
  wiki3.py get  (--id UUID | --path PATH) [--meta]
  wiki3.py upsert --path PATH --title T (--file F | --content-stdin)
                  [--tags a,b,c] [--desc D]   idempotent by path; keeps tags/desc if omitted
  wiki3.py move   (--id UUID | --path PATH) --to NEWPATH
  wiki3.py delete (--id UUID | --path PATH)
  wiki3.py render (--id UUID | --path PATH)
"""
import os, sys, json, argparse, subprocess, http.cookiejar, urllib.request, urllib.error

BASE = os.environ.get("WIKI3_URL", "https://wiki.lab.mainertoo.com").rstrip("/")
SITE = "9644217c-99d8-4807-bd79-36b085047845"
LOCAL_STRATEGY = "5a528c4c-0a82-4ad2-96a5-2b23811e6588"
REPO = os.environ.get("KLAB_REPO", os.path.expanduser("~/kubernetes-lab"))
SOPS_FILE = os.path.join(REPO, "apps/base/wiki/wiki-secret.sops.yaml")

_secrets = {}
def secret(key):
    if key not in _secrets:
        try:
            _secrets[key] = subprocess.check_output(
                ["sops", "-d", "--extract", f'["stringData"]["{key}"]', SOPS_FILE],
                text=True, stderr=subprocess.DEVNULL).strip()
        except Exception as e:
            sys.exit(f"cannot read {key} from {SOPS_FILE}: {e}")
    return _secrets[key]

_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))
_session = False

def call(method, path, body=None, user=False):
    """user=True: use the wiki-admin session (writes); else the API key."""
    global _session
    # -> Fastify rejects a JSON content-type with no body (e.g. DELETE)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if user and not _session:
        login()
    if not user:
        headers["Authorization"] = f"Bearer {secret('CLAUDE_API_KEY')}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + "/_api" + path, data=data, method=method, headers=headers)
    try:
        raw = _opener.open(req, timeout=60).read()
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="replace")[:500]
        raise SystemExit(f"{method} {path} -> {e.code}: {msg}")
    return json.loads(raw) if raw else {}

def login():
    global _session
    body = {"strategyId": LOCAL_STRATEGY, "username": secret("ADMIN_EMAIL"), "password": secret("ADMIN_PASS")}
    req = urllib.request.Request(f"{BASE}/_api/sites/{SITE}/auth/login", data=json.dumps(body).encode(),
                                 method="PUT", headers={"Content-Type": "application/json"})
    try:
        r = json.loads(_opener.open(req, timeout=60).read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"login failed -> {e.code}: {e.read().decode(errors='replace')[:300]}")
    if not r.get("authenticated"):
        raise SystemExit(f"login not authenticated: {r.get('nextAction')}")
    _session = True

def pages_list():
    return call("GET", f"/sites/{SITE}/tree/pages?depth=10&limit=1000")

def find_id(path):
    path = path.strip("/").lower()
    for p in pages_list():
        if p["path"] == path:
            return p["id"]
    return None

def resolve(a):
    pid = a.id or find_id(a.path or "")
    if not pid:
        sys.exit("page not found")
    return pid

def page_single(pid):
    return call("GET", f"/sites/{SITE}/pages/{pid}?withContent=true")

def render(pid):
    return call("POST", f"/sites/{SITE}/pages/{pid}/render", {}, user=True)

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").add_argument("--filter", default="")
    def target(p):
        p.add_argument("--id"); p.add_argument("--path")
        return p
    g = target(sub.add_parser("get")); g.add_argument("--meta", action="store_true")
    u = sub.add_parser("upsert")
    u.add_argument("--path", required=True); u.add_argument("--title", required=True)
    u.add_argument("--file"); u.add_argument("--content-stdin", action="store_true")
    u.add_argument("--tags"); u.add_argument("--desc")
    target(sub.add_parser("move")).add_argument("--to", required=True)
    target(sub.add_parser("delete"))
    target(sub.add_parser("render"))
    a = ap.parse_args()

    if a.cmd == "list":
        for p in sorted(pages_list(), key=lambda p: p["path"]):
            if a.filter.lower() in (p["path"] + p["title"]).lower():
                print(f'{p["id"]}  {p["path"]:56} | {p["title"]}')
    elif a.cmd == "get":
        p = page_single(resolve(a))
        if a.meta:
            print(json.dumps({k: p.get(k) for k in ("id", "path", "title", "description", "tags", "editor",
                              "publishState", "updatedAt")}, indent=2))
        else:
            print(p["content"])
    elif a.cmd == "upsert":
        if a.content_stdin:
            content = sys.stdin.read()
        elif a.file:
            content = open(os.path.expanduser(a.file)).read()
        else:
            sys.exit("need --file or --content-stdin")
        path = a.path.strip("/").lower()
        tags = [t.strip() for t in a.tags.split(",") if t.strip()] if a.tags is not None else None
        pid = find_id(path)
        body = {"title": a.title[:255], "content": content, "editor": "markdown"}
        if pid:
            if tags is not None: body["tags"] = tags
            if a.desc is not None: body["description"] = a.desc[:255]
            call("PATCH", f"/sites/{SITE}/pages/{pid}", body, user=True)
            verb = "UPDATED"
        else:
            body.update({"path": path, "locale": "en", "publishState": "published",
                         "tags": tags or [], "description": (a.desc or "")[:255]})
            r = call("POST", f"/sites/{SITE}/pages", body, user=True)
            pid = (r.get("page") or {}).get("id") or find_id(path)
            verb = "CREATED"
        render(pid)
        print(f"{verb} id={pid} {path} (rendered)")
    elif a.cmd == "move":
        pid = resolve(a)
        call("PUT", f"/sites/{SITE}/pages/{pid}/path",
             {"path": a.to.strip("/").lower(), "locale": "en"}, user=True)
        print(f"MOVED id={pid} -> {a.to}")
    elif a.cmd == "delete":
        pid = resolve(a)
        call("DELETE", f"/sites/{SITE}/pages/{pid}", user=True)
        print(f"DELETED id={pid}")
    elif a.cmd == "render":
        pid = resolve(a)
        render(pid)
        print(f"RENDER queued id={pid}")

if __name__ == "__main__":
    main()
