#!/usr/bin/env python3
"""
netinfo.py — pull the live network from the UniFi Network Integration API.

Reads the UDM SE's adopted devices (gateway/switches/APs) and connected clients,
and can emit:
  * a structured network map (YAML/JSON) — source for wiki docs
  * a Homebox inventory spec for the network *gear* (consumed by ../homebox/inventory.py)

Auth reuses the existing in-cluster secret apps/base/ui-toolkit/ui-toolkit-secret.sops.yaml
(keys UNIFI_API_KEY + UNIFI_CONTROLLER_URL) via `sops -d`, or env UNIFI_API_KEY /
UNIFI_CONTROLLER_URL. No new credential needed.

Subcommands:
  ./netinfo.py devices                 # adopted UniFi devices (table)
  ./netinfo.py clients [--wired]       # connected clients (table)
  ./netinfo.py map -o network-map.yaml # full structured map (devices + clients)
  ./netinfo.py homebox -o net.yaml     # Homebox spec for the network gear

Stdlib only (urllib) + PyYAML.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: pip3 install pyyaml")

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
UI_SECRET = REPO / "apps/base/ui-toolkit/ui-toolkit-secret.sops.yaml"

# Map a room keyword found in a device name to a Homebox location path.
ROOM_MAP = {
    "GARAGE": "Garage",
    "ATTIC": "Attic",
    "LIVING_ROOM": "Main Floor/Living Room",
    "LIVINGROOM": "Main Floor/Living Room",
    "KITCHEN": "Main Floor/Kitchen",
    "OFFICE": "Upstairs Floor/Office",
    "MASONCLOSET": "Mason Closet",
    "MASON": "Mason Closet",
    "STUDIO": "Studio",
    "LOFT": "Loft",
}
DEFAULT_ROOM = "Mason Closet"  # network hub — review/move in the dry-run


# ─────────────────────────── auth / http ───────────────────────────
def load_creds() -> tuple[str, str]:
    url = os.environ.get("UNIFI_CONTROLLER_URL")
    key = os.environ.get("UNIFI_API_KEY")
    if url and key:
        return url.rstrip("/"), key
    if not UI_SECRET.exists():
        sys.exit(f"No env creds and {UI_SECRET} not found.")
    try:
        out = subprocess.run(["sops", "-d", str(UI_SECRET)],
                             capture_output=True, text=True, check=True).stdout
    except FileNotFoundError:
        sys.exit("`sops` not on PATH — install it or set UNIFI_* env vars.")
    except subprocess.CalledProcessError as e:
        sys.exit(f"sops -d failed: {e.stderr}")
    sd = (yaml.safe_load(out) or {}).get("stringData", {})
    url = url or sd.get("UNIFI_CONTROLLER_URL")
    key = key or sd.get("UNIFI_API_KEY")
    if not (url and key):
        sys.exit("secret missing UNIFI_CONTROLLER_URL / UNIFI_API_KEY")
    return url.rstrip("/"), key


class UniFi:
    def __init__(self, url: str, key: str):
        self.base = f"{url}/proxy/network/integration/v1"
        self.key = key
        self.ctx = ssl._create_unverified_context()  # UDM uses a self-signed cert
        self.site = self._site_id()

    def _get(self, path: str, params: dict | None = None) -> list | dict:
        url = self.base + path
        if params:
            from urllib.parse import urlencode
            url += "?" + urlencode(params)
        req = urllib.request.Request(url)
        req.add_header("X-API-KEY", self.key)
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, context=self.ctx) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            sys.exit(f"GET {path} -> {e.code} {e.reason}\n{e.read().decode()[:400]}")

    def _paged(self, path: str) -> list:
        out, offset = [], 0
        while True:
            page = self._get(path, {"limit": 200, "offset": offset})
            data = page.get("data", [])
            out.extend(data)
            offset += len(data)
            if offset >= page.get("totalCount", len(out)) or not data:
                break
        return out

    def _site_id(self) -> str:
        sites = self._get("/sites").get("data", [])
        if not sites:
            sys.exit("no UniFi sites returned")
        return sites[0]["id"]

    def devices(self) -> list:
        return self._paged(f"/sites/{self.site}/devices")

    def clients(self) -> list:
        return self._paged(f"/sites/{self.site}/clients")

    def uplinks(self, devices: list) -> dict:
        """Return {deviceId: uplinkDeviceId|None} by fetching each device detail."""
        out = {}
        for d in devices:
            det = self._get(f"/sites/{self.site}/devices/{d['id']}")
            out[d["id"]] = (det.get("uplink") or {}).get("deviceId")
        return out


# ─────────────────────────── classification ───────────────────────────
def categorize(model: str, name: str) -> str:
    m = (model or "").upper()
    if "DREAM MACHINE" in m or m.startswith("UDM") or m.startswith("UXG"):
        return "Gateway"
    if m.startswith("USW") or "SWITCH" in m or "AGGREGATION" in m:
        return "Switch"
    if m.startswith(("U6", "U7", "UAP", "U5")) or " AP" in m:
        return "Access Point"
    return "UniFi Device"


def room_for(name: str) -> str:
    up = (name or "").upper()
    for kw, loc in ROOM_MAP.items():
        if kw in up:
            return loc
    return DEFAULT_ROOM


# ─────────────────────────── commands ───────────────────────────
def cmd_devices(u: UniFi, args):
    rows = sorted(u.devices(), key=lambda x: x.get("name", ""))
    for d in rows:
        cat = categorize(d.get("model", ""), d.get("name", ""))
        print(f"  {d.get('name','?'):28} {d.get('model','?'):22} {cat:13} "
              f"{d.get('ipAddress','?'):15} {d.get('macAddress','?')}  {d.get('state','')}")
    print(f"\n  {len(rows)} devices")


def cmd_clients(u: UniFi, args):
    rows = u.clients()
    if args.wired:
        rows = [c for c in rows if c.get("type") == "WIRED"]
    for c in sorted(rows, key=lambda x: x.get("ipAddress", "")):
        nm = c.get("name") or c.get("hostname") or c.get("macAddress")
        print(f"  {str(nm)[:30]:30} {c.get('ipAddress','?'):15} {c.get('macAddress','?')} {c.get('type','')}")
    print(f"\n  {len(rows)} clients")


def build_map(u: UniFi) -> dict:
    devs = u.devices()
    by_id = {d["id"]: d for d in devs}
    clients = u.clients()
    return {
        "site": u.site,
        "devices": [
            {"name": d.get("name"), "model": d.get("model"),
             "category": categorize(d.get("model", ""), d.get("name", "")),
             "ip": d.get("ipAddress"), "mac": d.get("macAddress"),
             "firmware": d.get("firmwareVersion"), "state": d.get("state")}
            for d in sorted(devs, key=lambda x: x.get("name", ""))
        ],
        "clients": [
            {"name": c.get("name") or c.get("hostname"), "ip": c.get("ipAddress"),
             "mac": c.get("macAddress"), "type": c.get("type"),
             "uplink": (by_id.get(c.get("uplinkDeviceId") or "", {}) or {}).get("name")}
            for c in sorted(clients, key=lambda x: x.get("ipAddress") or "")
        ],
    }


def cmd_map(u: UniFi, args):
    m = build_map(u)
    text = yaml.safe_dump(m, sort_keys=False, allow_unicode=True, width=120)
    if args.output:
        Path(args.output).write_text(text)
        print(f"wrote {args.output}  ({len(m['devices'])} devices, {len(m['clients'])} clients)")
    else:
        print(text)


UNSORTED = "Unsorted (auto-import)"


def cmd_homebox(u: UniFi, args):
    """Emit a Homebox inventory spec for network gear (+ all clients with --all-clients)."""
    from collections import Counter
    devs = u.devices()
    clients = u.clients()
    qnas_macs = {c.get("macAddress") for c in clients if (c.get("name") or "").upper().startswith("QNAS")}
    items, locs = [], set()

    for d in sorted(devs, key=lambda x: x.get("name", "")):
        cat = categorize(d.get("model", ""), d.get("name", ""))
        loc = room_for(d.get("name", ""))
        locs.add(loc)
        items.append({
            "name": d.get("name"),
            "location": loc,
            "tags": ["UniFi", cat],
            "manufacturer": "Ubiquiti",
            "modelNumber": d.get("model"),
            "serialNumber": d.get("macAddress"),
            "description": f"UniFi {cat}",
            "notes": f"IP {d.get('ipAddress')} · fw {d.get('firmwareVersion')}",
        })

    # QNAP — collapse its wired NIC client(s) into one asset.
    qnas = [c for c in clients if (c.get("name") or "").upper().startswith("QNAS")]
    if qnas:
        ips = ", ".join(sorted(c.get("ipAddress", "?") for c in qnas))
        locs.add(DEFAULT_ROOM)
        items.append({
            "name": "QNAP NAS", "location": DEFAULT_ROOM, "tags": ["NAS"],
            "manufacturer": "QNAP", "modelNumber": "TS-632X",
            "serialNumber": sorted(c.get("macAddress", "") for c in qnas)[0],
            "description": "QNAP NAS (QNAS)",
            "notes": f"IPs {ips} · MACs {', '.join(sorted(c.get('macAddress','?') for c in qnas))}",
        })

    tag_set = ["UniFi", "Gateway", "Switch", "Access Point", "NAS"]
    if args.all_clients:
        tag_set += ["Wired", "Wireless"]
        id_to_name = {d["id"]: d.get("name") for d in devs}
        # Guarantee unique item names (Homebox is matched by name); append MAC on collisions.
        named = [(c.get("name") or c.get("hostname") or c.get("macAddress"), c) for c in clients]
        counts = Counter(n for n, _ in named)
        for name, c in named:
            if c.get("macAddress") in qnas_macs:
                continue  # already represented by the QNAP asset
            if counts[name] > 1 or not name:
                name = f"{name or 'device'} ({c.get('macAddress')})"
            items.append({
                "name": name, "location": UNSORTED,
                "tags": ["Wired" if c.get("type") == "WIRED" else "Wireless"],
                "serialNumber": c.get("macAddress"),
                "notes": f"IP {c.get('ipAddress')} · uplink {id_to_name.get(c.get('uplinkDeviceId'),'?')}",
            })
        locs.add(UNSORTED)

    # Build location tree from the flat set of paths.
    tree: dict = {}
    for path in sorted(locs):
        node = tree
        for part in path.split("/"):
            node = node.setdefault(part, {})

    def to_list(node):
        return [{"name": n, **({"children": to_list(k)} if k else {})}
                for n, k in node.items()]

    spec = {
        "locations": to_list(tree),
        "tags": [{"name": t} for t in tag_set],
        "items": items,
    }
    text = yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=120)
    if args.output:
        Path(args.output).write_text(text)
        print(f"wrote {args.output}  ({len(items)} items across {len(locs)} locations)")
        print("locations used:", ", ".join(sorted(locs)))
    else:
        print(text)


def cmd_wiki(u: UniFi, args):
    """Generate a Wiki.js network page (markdown): inventory, topology, client/IP map."""
    devs = u.devices()
    clients = u.clients()
    id_to = {d["id"]: d for d in devs}
    uplinks = u.uplinks(devs)
    online = sum(1 for d in devs if d.get("state") == "ONLINE")

    L = []
    L.append("# Network — UniFi Topology & Inventory\n")
    L.append("> Auto-generated from the UDM SE UniFi Network Integration API "
             "by `scripts/unifi/netinfo.py wiki`. Do not hand-edit — re-run to refresh.\n")
    L.append(f"**Gateway:** {next((d['name'] for d in devs if categorize(d.get('model',''),d.get('name',''))=='Gateway'),'?')} "
             f"· **Devices:** {len(devs)} ({online} online) · **Clients:** {len(clients)}\n")

    # Topology tree by uplink.
    children: dict = {}
    roots = []
    for d in devs:
        up = uplinks.get(d["id"])
        if up and up in id_to:
            children.setdefault(up, []).append(d["id"])
        else:
            roots.append(d["id"])

    L.append("## Topology\n")
    L.append("```")

    def render(did, depth):
        d = id_to[did]
        cat = categorize(d.get("model", ""), d.get("name", ""))
        cl = sum(1 for c in clients if c.get("uplinkDeviceId") == did)
        L.append("  " * depth + f"├─ {d.get('name')}  [{d.get('model')}]  "
                 f"{d.get('ipAddress')}  ({cl} clients)")
        for kid in sorted(children.get(did, []), key=lambda i: id_to[i].get("name", "")):
            render(kid, depth + 1)

    for r in sorted(roots, key=lambda i: id_to[i].get("name", "")):
        render(r, 0)
    L.append("```\n")

    # Device inventory table.
    L.append("## Devices\n")
    L.append("| Name | Model | Type | IP | MAC | Firmware | State |")
    L.append("|------|-------|------|----|-----|----------|-------|")
    for d in sorted(devs, key=lambda x: x.get("name", "")):
        cat = categorize(d.get("model", ""), d.get("name", ""))
        L.append(f"| {d.get('name')} | {d.get('model')} | {cat} | {d.get('ipAddress')} "
                 f"| `{d.get('macAddress')}` | {d.get('firmwareVersion','')} | {d.get('state','')} |")

    # Client / IP map.
    L.append("\n## Clients\n")
    L.append("| Name | IP | MAC | Conn | Uplink |")
    L.append("|------|----|-----|------|--------|")
    def ipkey(c):
        try: return tuple(int(o) for o in (c.get("ipAddress") or "0.0.0.0").split("."))
        except Exception: return (0, 0, 0, 0)
    for c in sorted(clients, key=ipkey):
        nm = c.get("name") or c.get("hostname") or c.get("macAddress")
        up = (id_to.get(c.get("uplinkDeviceId") or "") or {}).get("name", "—")
        L.append(f"| {nm} | {c.get('ipAddress','')} | `{c.get('macAddress')}` "
                 f"| {c.get('type','')} | {up} |")

    text = "\n".join(L) + "\n"
    if args.output:
        Path(args.output).write_text(text)
        print(f"wrote {args.output}  ({len(text)} bytes, {len(devs)} devices, {len(clients)} clients)")
    else:
        print(text)


def main():
    ap = argparse.ArgumentParser(description="UniFi network info / inventory")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("devices")
    p = sub.add_parser("clients"); p.add_argument("--wired", action="store_true")
    p = sub.add_parser("map"); p.add_argument("-o", "--output")
    p = sub.add_parser("homebox")
    p.add_argument("-o", "--output")
    p.add_argument("--all-clients", action="store_true",
                   help="also import every client (in 'Unsorted (auto-import)')")
    p = sub.add_parser("wiki"); p.add_argument("-o", "--output")
    args = ap.parse_args()
    u = UniFi(*load_creds())
    {"devices": cmd_devices, "clients": cmd_clients, "map": cmd_map,
     "homebox": cmd_homebox, "wiki": cmd_wiki}[args.cmd](u, args)


if __name__ == "__main__":
    main()
