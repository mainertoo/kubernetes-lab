#!/usr/bin/env python3
"""
inventory.py — inventory-as-code for Homebox (https://homebox.lab.mainertoo.com).

Define your locations, labels (tags) and items declaratively in a YAML spec, then
apply it idempotently against the live Homebox instance. Dry-run by default.

Workflow:
  ./inventory.py whoami                       # verify auth + show the group you're in
  ./inventory.py pull -o current.yaml         # snapshot live state into an editable spec
  # ... hand-edit inventory.yaml ...
  ./inventory.py apply inventory.yaml         # PLAN only — writes nothing
  ./inventory.py apply inventory.yaml --commit
  ./inventory.py barcode 0049000028911        # look up a product by EAN/UPC

Idempotency model (no state file — the live server IS the state):
  * locations  matched by full path        (Garage/Workbench/Bin 1)
  * tags       matched by name             (case-insensitive)
  * items      matched by name + location  (case-insensitive)
Existing objects are reconciled to the spec (PATCH/PUT only when a field differs).
Renames are NOT tracked — renaming a node in the spec creates a new object. Delete
the old one in the UI, or `pull` first to see current names.

Homebox 0.26 entities API: the old `/v1/items*` + `/v1/locations*` endpoints were
replaced by a single unified `/v1/entities*` model. Items and locations are both
entities, distinguished by their `entityType` (`global.item` vs `global.location`,
discovered via `/v1/entity-types`). An item's location is its `parent`. New items do
not get an assetId on create, so `apply --commit` runs `/v1/actions/ensure-asset-ids`
afterwards to assign the next sequential `000-NNN`. Tags are still `/v1/tags`.

Auth: reads scripts/homebox/credentials.sops.yaml via `sops -d`, or env
HOMEBOX_URL / HOMEBOX_USERNAME / HOMEBOX_PASSWORD (or HOMEBOX_TOKEN to skip login).
Homebox issues short-lived JWTs; this tool logs in fresh on every run.

Stdlib only (urllib) + PyYAML. No pip install beyond pyyaml.
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

# Entity fields settable on create vs. only via a follow-up update.
# (entityTypeId + parentId are injected by the caller, not driven from the spec.)
CREATE_FIELDS = {"name", "description", "entityTypeId", "parentId", "quantity", "tagIds"}
# Extra scalar fields we let the spec drive (carried through to the EntityUpdate).
EXTRA_ITEM_FIELDS = {
    "notes", "manufacturer", "modelNumber", "serialNumber",
    "insured", "purchasePrice", "purchaseFrom", "purchaseDate",
}


# ─────────────────────────── auth / http ───────────────────────────
def load_creds() -> dict:
    env = {
        k: os.environ.get(f"HOMEBOX_{k.upper()}")
        for k in ("url", "username", "password", "token")
    }
    if env["url"] and (env["token"] or (env["username"] and env["password"])):
        return env
    if not CRED_FILE.exists():
        sys.exit(
            f"No usable env creds and {CRED_FILE} not found.\n"
            "Set HOMEBOX_URL + HOMEBOX_USERNAME/HOMEBOX_PASSWORD (or HOMEBOX_TOKEN),\n"
            "or create credentials.sops.yaml (see README)."
        )
    try:
        out = subprocess.run(
            ["sops", "-d", str(CRED_FILE)], capture_output=True, text=True, check=True
        ).stdout
    except FileNotFoundError:
        sys.exit("`sops` not on PATH — install it or use HOMEBOX_* env vars.")
    except subprocess.CalledProcessError as e:
        sys.exit(f"sops -d failed: {e.stderr}")
    data = (yaml.safe_load(out) or {}).get("stringData", {})
    for k in env:
        env[k] = env[k] or data.get(f"HOMEBOX_{k.upper()}")
    if not env["url"]:
        sys.exit("credentials file missing HOMEBOX_URL")
    if not (env["token"] or (env["username"] and env["password"])):
        sys.exit("credentials file needs HOMEBOX_TOKEN or HOMEBOX_USERNAME+HOMEBOX_PASSWORD")
    return env


class Client:
    def __init__(self, creds: dict, insecure: bool = False):
        self.base = creds["url"].rstrip("/") + "/api"
        self.ctx = ssl._create_unverified_context() if insecure else None
        self.token = creds.get("token") or self._login(creds)
        self._etypes: list[dict] | None = None

    def _login(self, creds: dict) -> str:
        body = {
            "username": creds["username"],
            "password": creds["password"],
            "stayLoggedIn": False,
        }
        res = self._req("POST", "/v1/users/login", body=body, auth=False)
        tok = res.get("token", "")
        # Homebox returns the token already prefixed with "Bearer ".
        return tok[len("Bearer "):] if tok.startswith("Bearer ") else tok

    def _req(self, method, path, body=None, params=None, auth=True):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v not in (None, "")}
            )
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if auth:
            req.add_header("Authorization", f"Bearer {self.token}")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, context=self.ctx) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:800]
            sys.exit(f"{method} {path} -> {e.code} {e.reason}\n{detail}")
        except urllib.error.URLError as e:
            sys.exit(f"{method} {path} -> connection error: {e.reason}")

    def get(self, path, params=None):
        return self._req("GET", path, params=params)

    def post(self, path, body=None):
        return self._req("POST", path, body=body)

    def put(self, path, body):
        return self._req("PUT", path, body=body)

    def entity_type_id(self, *, location: bool) -> str:
        """Resolve the `global.item` / `global.location` entityType id (cached)."""
        if self._etypes is None:
            self._etypes = self.get("/v1/entity-types") or []
        want = "global.location" if location else "global.item"
        for t in self._etypes:
            if t.get("name") == want:
                return t["id"]
        # Fall back to the isLocation flag if the global types were renamed.
        for t in self._etypes:
            if bool(t.get("isLocation")) == location:
                return t["id"]
        sys.exit(f"no entity-type found for {'location' if location else 'item'}")


# ─────────────────────────── live-state readers ───────────────────────────
def location_paths(client: Client) -> dict[str, str]:
    """Return {full/path: id} for every location, lower-cased keys."""
    tree = client.get("/v1/entities/tree") or []
    out: dict[str, str] = {}

    def walk(node, prefix):
        if node.get("type", "location") != "location":
            return  # items can appear in the tree too — skip them
        path = f"{prefix}/{node['name']}" if prefix else node["name"]
        out[path.lower()] = node["id"]
        for child in node.get("children") or []:
            walk(child, path)

    for top in tree:
        walk(top, "")
    return out


def tags_by_name(client: Client) -> dict[str, dict]:
    return {t["name"].lower(): t for t in (client.get("/v1/tags") or [])}


def all_items(client: Client) -> list[dict]:
    """Paginate GET /v1/entities fully (returns items only — locations live in the tree)."""
    items, page = [], 1
    while True:
        res = client.get("/v1/entities", params={"page": page, "pageSize": 200}) or {}
        batch = res.get("items") or []
        items.extend(batch)
        total = res.get("total", len(items))
        if not batch or len(items) >= total:
            break
        page += 1
    return items


def item_location_id(item: dict) -> str | None:
    """An item's location is its parent entity (0.26 unified model)."""
    parent = item.get("parent")
    return parent["id"] if parent else None


# ─────────────────────────── apply engine ───────────────────────────
class Planner:
    def __init__(self, client: Client, commit: bool, update: bool):
        self.c = client
        self.commit = commit
        self.update = update
        self.actions: list[str] = []
        self.items_created = 0

    def log(self, verb, what):
        tag = "  apply" if self.commit else "    plan"
        self.actions.append(f"{tag}  {verb:8} {what}")
        print(f"{tag}  {verb:8} {what}")

    # -- locations (recursive, parent before child) --
    def ensure_locations(self, nodes, parent_id, parent_path, live):
        for node in nodes or []:
            name = node["name"]
            path = f"{parent_path}/{name}" if parent_path else name
            loc_id = live.get(path.lower())
            if loc_id is None:
                self.log("create", f"location  {path}")
                if self.commit:
                    body = {"name": name, "description": node.get("description", ""),
                            "parentId": parent_id,
                            "entityTypeId": self.c.entity_type_id(location=True)}
                    loc_id = self.c.post("/v1/entities", body)["id"]
                    live[path.lower()] = loc_id
                else:
                    loc_id = f"<new:{path}>"  # placeholder so children can be planned
            self.ensure_locations(node.get("children"), loc_id, path, live)

    # -- tags --
    def ensure_tags(self, specs, live):
        for spec in specs or []:
            name = spec["name"]
            tag = live.get(name.lower())
            if tag is None:
                self.log("create", f"tag       {name}")
                if self.commit:
                    body = {k: spec[k] for k in ("name", "color", "icon", "description")
                            if k in spec}
                    live[name.lower()] = self.c.post("/v1/tags", body)
            elif self.update:
                diff = {k: spec[k] for k in ("color", "icon", "description")
                        if k in spec and spec[k] != tag.get(k)}
                if diff:
                    self.log("update", f"tag       {name}  {diff}")
                    if self.commit:
                        body = {"id": tag["id"], "name": name, **{
                            "color": spec.get("color", tag.get("color", "")),
                            "icon": spec.get("icon", tag.get("icon", "")),
                            "description": spec.get("description", tag.get("description", "")),
                        }}
                        live[name.lower()] = self.c.put(f"/v1/tags/{tag['id']}", body)

    # -- items --
    def ensure_items(self, specs, loc_paths, tags, live_items):
        index = {(i["name"].lower(), item_location_id(i)): i for i in live_items}
        for spec in specs or []:
            name = spec["name"]
            loc_path = spec.get("location")
            loc_id = loc_paths.get((loc_path or "").lower()) if loc_path else None
            if loc_path and loc_id is None:
                self.log("SKIP", f"item      {name}  (location '{loc_path}' not found)")
                continue
            tag_ids = []
            for tname in spec.get("tags", []) or []:
                t = tags.get(tname.lower())
                if not t:
                    self.log("SKIP", f"item      {name}  (tag '{tname}' not found)")
                    tag_ids = None
                    break
                tag_ids.append(t["id"] if isinstance(t, dict) else t)
            if tag_ids is None:
                continue

            existing = index.get((name.lower(), loc_id))
            if existing is None:
                self.log("create", f"item      {name}  @ {loc_path or '(no location)'}")
                if self.commit:
                    body = {"name": name, "description": spec.get("description", ""),
                            "parentId": loc_id, "quantity": spec.get("quantity", 1),
                            "tagIds": tag_ids,
                            "entityTypeId": self.c.entity_type_id(location=False)}
                    created = self.c.post("/v1/entities", {k: v for k, v in body.items()
                                                           if k in CREATE_FIELDS})
                    self.items_created += 1
                    self._apply_extra(created["id"], spec, loc_id, tag_ids, name)
            elif self.update:
                self._maybe_update(existing, spec, loc_id, tag_ids, name)

    def _apply_extra(self, item_id, spec, loc_id, tag_ids, name):
        extra = {k: spec[k] for k in EXTRA_ITEM_FIELDS if k in spec}
        if not extra:
            return
        full = self.c.get(f"/v1/entities/{item_id}")
        body = self._update_body(full, spec, loc_id, tag_ids)
        self.c.put(f"/v1/entities/{item_id}", body)

    def _maybe_update(self, existing, spec, loc_id, tag_ids, name):
        full = self.c.get(f"/v1/entities/{existing['id']}")
        cur_tags = sorted(t["id"] for t in (full.get("tags") or []))
        want = {
            "description": spec.get("description", full.get("description", "")),
            "quantity": spec.get("quantity", full.get("quantity", 1)),
            "tagIds": sorted(tag_ids),
        }
        for k in EXTRA_ITEM_FIELDS:
            if k in spec:
                want[k] = spec[k]
        diff = {}
        if want["description"] != full.get("description", ""):
            diff["description"] = want["description"]
        if want["quantity"] != full.get("quantity"):
            diff["quantity"] = want["quantity"]
        if want["tagIds"] != cur_tags:
            diff["tagIds"] = want["tagIds"]
        for k in EXTRA_ITEM_FIELDS:
            if k in spec and spec[k] != full.get(k):
                diff[k] = spec[k]
        if not diff:
            return
        self.log("update", f"item      {name}  {list(diff)}")
        if self.commit:
            body = self._update_body(full, spec, loc_id, tag_ids)
            self.c.put(f"/v1/entities/{existing['id']}", body)

    @staticmethod
    def _update_body(full, spec, loc_id, tag_ids):
        """Overlay spec fields onto the current EntityOut to form a safe EntityUpdate."""
        body = {
            "id": full["id"],
            "name": spec.get("name", full["name"]),
            "description": spec.get("description", full.get("description", "")),
            "quantity": spec.get("quantity", full.get("quantity", 1)),
            "parentId": loc_id or item_location_id(full),
            "entityTypeId": (full.get("entityType") or {}).get("id"),
            "tagIds": tag_ids if tag_ids is not None else
                      [t["id"] for t in (full.get("tags") or [])],
            "insured": full.get("insured", False),
            "archived": full.get("archived", False),
            "lifetimeWarranty": full.get("lifetimeWarranty", False),
        }
        for k in EXTRA_ITEM_FIELDS:
            if k in spec:
                body[k] = spec[k]
            elif k in full and full[k] not in (None, ""):
                body[k] = full[k]
        return body


# ─────────────────────────── commands ───────────────────────────
def cmd_whoami(client, args):
    me = client.get("/v1/users/self")["item"]
    print(f"user:  {me['name']} <{me['email']}>")
    print(f"group: {me.get('groupName', '?')}  (items live in this group)")
    stats = client.get("/v1/groups/statistics") or {}
    print(f"stats: {stats.get('totalItems', '?')} items, "
          f"{stats.get('totalLocations', '?')} locations, "
          f"{stats.get('totalLabels', stats.get('totalTags', '?'))} tags")


def cmd_pull(client, args):
    loc_tree = client.get("/v1/entities/tree") or []

    def conv(node):
        out = {"name": node["name"]}
        kids = [conv(c) for c in (node.get("children") or [])
                if c.get("type", "location") == "location"]
        if kids:
            out["children"] = kids
        return out

    paths = location_paths(client)
    id_to_path = {v: k for k, v in paths.items()}
    tags = [{"name": t["name"], **({"color": t["color"]} if t.get("color") else {}),
             **({"icon": t["icon"]} if t.get("icon") else {})}
            for t in (client.get("/v1/tags") or [])]
    items = []
    for it in all_items(client):
        row = {"name": it["name"], "quantity": it.get("quantity", 1)}
        loc_id = item_location_id(it)
        if loc_id and loc_id in id_to_path:
            row["location"] = id_to_path[loc_id]
        if it.get("description"):
            row["description"] = it["description"]
        items.append(row)
    spec = {
        "locations": [conv(n) for n in loc_tree if n.get("type", "location") == "location"],
        "tags": tags,
        "items": items,
    }
    text = yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=100)
    if args.output:
        Path(args.output).write_text(text)
        print(f"wrote {args.output}  "
              f"({len(spec['locations'])} top locations, {len(tags)} tags, {len(items)} items)")
    else:
        print(text)


def cmd_apply(client, args):
    spec = yaml.safe_load(Path(args.spec).read_text()) or {}
    print(f"{'COMMIT' if args.commit else 'DRY-RUN'}  applying {args.spec}\n")
    planner = Planner(client, commit=args.commit, update=not args.no_update)

    live_locs = location_paths(client)
    planner.ensure_locations(spec.get("locations"), None, "", live_locs)
    live_tags = tags_by_name(client)
    planner.ensure_tags(spec.get("tags"), live_tags)

    # Re-read so item resolution sees freshly-created locations/tags (commit mode).
    if args.commit:
        live_locs = location_paths(client)
        live_tags = tags_by_name(client)
    planner.ensure_items(spec.get("items"), live_locs, live_tags, all_items(client))

    # New entities are created without an assetId — backfill the next 000-NNN.
    if args.commit and planner.items_created:
        res = client.post("/v1/actions/ensure-asset-ids") or {}
        print(f"\n  ensure-asset-ids: assigned {res.get('completed', '?')} asset id(s).")

    if not planner.actions:
        print("  nothing to do — live state already matches the spec.")
    elif not args.commit:
        print(f"\n  {len(planner.actions)} change(s) planned. Re-run with --commit to apply.")
    else:
        print(f"\n  applied {len(planner.actions)} change(s).")


def cmd_barcode(client, args):
    res = client.get("/v1/products/search-from-barcode", params={"data": args.code})
    print(json.dumps(res, indent=2))


def cmd_token(client, args):
    """Mint a long-lived (stayLoggedIn) JWT to paste as HOMEBOX_TOKEN.

    Homebox 0.26 added static `hb_…` API keys, but this login JWT is still the
    simplest scripted credential. It expires (see expiresAt); username/password in
    creds is more durable for a script.
    """
    creds = load_creds()
    if not (creds.get("username") and creds.get("password")):
        sys.exit("token: needs HOMEBOX_USERNAME + HOMEBOX_PASSWORD (not a stored token).")
    res = client._req("POST", "/v1/users/login", auth=False, body={
        "username": creds["username"], "password": creds["password"], "stayLoggedIn": True})
    tok = res.get("token", "")
    tok = tok[len("Bearer "):] if tok.startswith("Bearer ") else tok
    print(f"# expires: {res.get('expiresAt', '?')}")
    print(f"HOMEBOX_TOKEN: {tok}")


def main():
    ap = argparse.ArgumentParser(description="inventory-as-code for Homebox")
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami", help="verify auth + show your group/stats")

    p = sub.add_parser("pull", help="snapshot live state into a spec")
    p.add_argument("-o", "--output", help="write to file (default: stdout)")

    p = sub.add_parser("apply", help="reconcile a spec into Homebox (dry-run default)")
    p.add_argument("spec", help="path to the inventory YAML spec")
    p.add_argument("--commit", action="store_true", help="actually write changes")
    p.add_argument("--no-update", action="store_true",
                   help="create-only; never modify existing objects")

    p = sub.add_parser("barcode", help="look up a product by EAN/UPC")
    p.add_argument("code", help="barcode digits")

    sub.add_parser("token", help="mint a long-lived JWT to paste as HOMEBOX_TOKEN")

    args = ap.parse_args()
    client = Client(load_creds(), insecure=args.insecure)
    {"whoami": cmd_whoami, "pull": cmd_pull, "apply": cmd_apply,
     "barcode": cmd_barcode, "token": cmd_token}[args.cmd](client, args)


if __name__ == "__main__":
    main()
