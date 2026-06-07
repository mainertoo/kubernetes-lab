#!/usr/bin/env python3
"""
pvinfo.py — query (and optionally control) the Proxmox VE hosts by API.

Talks to the Proxmox VE REST API (`/api2/json`) with an API *token*. Reads the
cluster's nodes/VMs/storage and — most usefully for the VLAN migration — each
host's network bridges and their VLAN tags. A small set of guarded write actions
(VM power + snapshot) is available behind an explicit `--yes`.

Auth reuses the local-tooling pattern (env vars → `sops -d` fallback), mirroring
scripts/unifi/netinfo.py. Credentials live in scripts/proxmox/credentials.sops.yaml
(keys PROXMOX_URL, PROXMOX_TOKEN_ID, PROXMOX_TOKEN_SECRET, optional PROXMOX_HOSTS),
or env PROXMOX_URL / PROXMOX_TOKEN_ID / PROXMOX_TOKEN_SECRET / PROXMOX_HOSTS.

The token is `USER@REALM!TOKENID` + its secret; the request header is
  Authorization: PVEAPIToken=<USER@REALM!TOKENID>=<SECRET>

Subcommands:
  ./pvinfo.py nodes                      # cluster nodes (cpu/mem/uptime/status)
  ./pvinfo.py vms [--running]            # all guests (qemu + lxc) across hosts
  ./pvinfo.py storage                    # storage pools per node
  ./pvinfo.py network [--node NAME]      # bridges + VLAN tags per host (migration check)
  ./pvinfo.py map -o pve-map.yaml        # structured dump (nodes+guests+storage+network)
  ./pvinfo.py wiki -o pve.md             # Wiki.js markdown page
  # writes (default = dry-run; require --yes to execute):
  ./pvinfo.py vm start|stop|shutdown|reboot <vmid> [--yes]
  ./pvinfo.py snapshot <vmid> <name> [--description TEXT] [--yes]

Stdlib only (urllib) + PyYAML.
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
CREDS = HERE / "credentials.sops.yaml"


# ─────────────────────────── auth / http ───────────────────────────
def load_creds() -> tuple[str, str, str, list[str]]:
    """Return (primary_url, token_id, token_secret, extra_host_urls)."""
    url = os.environ.get("PROXMOX_URL")
    tid = os.environ.get("PROXMOX_TOKEN_ID")
    sec = os.environ.get("PROXMOX_TOKEN_SECRET")
    hosts_env = os.environ.get("PROXMOX_HOSTS")
    if not (url and tid and sec):
        if not CREDS.exists():
            sys.exit(f"No env creds and {CREDS} not found.")
        try:
            out = subprocess.run(["sops", "-d", str(CREDS)],
                                 capture_output=True, text=True, check=True).stdout
        except FileNotFoundError:
            sys.exit("`sops` not on PATH — install it or set PROXMOX_* env vars.")
        except subprocess.CalledProcessError as e:
            sys.exit(f"sops -d failed: {e.stderr}")
        sd = (yaml.safe_load(out) or {}).get("stringData", {})
        url = url or sd.get("PROXMOX_URL")
        tid = tid or sd.get("PROXMOX_TOKEN_ID")
        sec = sec or sd.get("PROXMOX_TOKEN_SECRET")
        hosts_env = hosts_env or sd.get("PROXMOX_HOSTS")
    if not (url and tid and sec):
        sys.exit("missing PROXMOX_URL / PROXMOX_TOKEN_ID / PROXMOX_TOKEN_SECRET")
    extra = [h.strip() for h in str(hosts_env or "").replace(",", "\n").splitlines() if h.strip()]
    return url.rstrip("/"), tid, sec, extra


class Proxmox:
    """A single PVE API endpoint. A cluster member returns the whole cluster's
    /nodes + /cluster/resources; a standalone host returns just itself."""

    def __init__(self, url: str, token_id: str, token_secret: str):
        self.base = f"{url}/api2/json"
        self.auth = f"PVEAPIToken={token_id}={token_secret}"
        self.ctx = ssl._create_unverified_context()  # PVE uses a self-signed cert

    def _req(self, method: str, path: str, data: dict | None = None) -> dict | list:
        url = self.base + path
        body = urllib.parse.urlencode(data).encode() if data else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", self.auth)
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=20) as r:
                return json.loads(r.read()).get("data")
        except urllib.error.HTTPError as e:
            sys.exit(f"{method} {path} -> {e.code} {e.reason}\n{e.read().decode()[:400]}")
        except urllib.error.URLError as e:
            sys.exit(f"{method} {path} -> unreachable: {e.reason}")

    def get(self, path: str) -> dict | list:
        return self._req("GET", path)

    def post(self, path: str, data: dict | None = None) -> dict | list:
        return self._req("POST", path, data)

    # high-level reads
    def nodes(self) -> list:
        return self.get("/nodes") or []

    def resources(self, kind: str | None = None) -> list:
        path = "/cluster/resources" + (f"?type={kind}" if kind else "")
        return self.get(path) or []

    def node_network(self, node: str) -> list:
        return self.get(f"/nodes/{node}/network") or []

    def node_storage(self, node: str) -> list:
        return self.get(f"/nodes/{node}/storage") or []


def clients() -> tuple[list[Proxmox], dict]:
    """Build a Proxmox client per configured endpoint. Returns (clients, meta)."""
    url, tid, sec, extra = load_creds()
    cs = [Proxmox(url, tid, sec)]
    for h in extra:
        cs.append(Proxmox(h.rstrip("/"), tid, sec))
    return cs, {"primary": url, "token_id": tid, "extra": extra}


def merged_nodes(cs: list[Proxmox]) -> dict:
    """{node_name: (node_dict, client_that_can_reach_it)} deduped across endpoints."""
    out: dict = {}
    for c in cs:
        for n in c.nodes():
            name = n.get("node")
            if name and name not in out:
                out[name] = (n, c)
    return out


def merged_resources(cs: list[Proxmox], kind: str | None = None) -> list:
    seen, out = set(), []
    for c in cs:
        for r in c.resources(kind):
            key = (r.get("type"), r.get("id"))
            if key not in seen:
                seen.add(key)
                out.append((r, c))
    return out


# ─────────────────────────── helpers ───────────────────────────
def gib(b) -> str:
    try:
        return f"{int(b) / 1024**3:.0f}G"
    except (TypeError, ValueError):
        return "?"


def pct(used, total) -> str:
    try:
        return f"{100 * float(used) / float(total):.0f}%"
    except (TypeError, ValueError, ZeroDivisionError):
        return "?"


def find_guest(cs: list[Proxmox], vmid: str) -> tuple[dict, Proxmox] | None:
    for r, c in merged_resources(cs):
        if r.get("type") in ("qemu", "lxc") and str(r.get("vmid")) == str(vmid):
            return r, c
    return None


# ─────────────────────────── read commands ───────────────────────────
def cmd_nodes(cs, args):
    nodes = merged_nodes(cs)
    for name, (n, _) in sorted(nodes.items()):
        up = n.get("uptime", 0)
        days = f"{up // 86400}d" if up else "—"
        print(f"  {name:16} {n.get('status','?'):8} "
              f"cpu {pct(n.get('cpu', 0), 1):>4}  "
              f"mem {gib(n.get('mem'))}/{gib(n.get('maxmem'))}  up {days}")
    print(f"\n  {len(nodes)} nodes")


def cmd_vms(cs, args):
    rows = [r for r, _ in merged_resources(cs) if r.get("type") in ("qemu", "lxc")]
    if args.running:
        rows = [r for r in rows if r.get("status") == "running"]
    for r in sorted(rows, key=lambda x: (x.get("node", ""), int(x.get("vmid", 0)))):
        print(f"  {str(r.get('vmid')):>6}  {r.get('type'):4} {r.get('status','?'):8} "
              f"{str(r.get('name',''))[:28]:28} @ {r.get('node','?'):14} "
              f"mem {gib(r.get('maxmem'))}  disk {gib(r.get('maxdisk'))}")
    print(f"\n  {len(rows)} guests")


def cmd_storage(cs, args):
    nodes = merged_nodes(cs)
    for name, (_, c) in sorted(nodes.items()):
        for s in sorted(c.node_storage(name), key=lambda x: x.get("storage", "")):
            if not s.get("active", 1):
                continue
            print(f"  {name:14} {s.get('storage',''):18} {s.get('type',''):10} "
                  f"{gib(s.get('used'))}/{gib(s.get('total'))} ({pct(s.get('used'), s.get('total'))}) "
                  f"[{s.get('content','')}]")


def _ifaces(c: Proxmox, node: str) -> list:
    """Return bridge/bond/vlan interfaces with their VLAN-relevant fields."""
    out = []
    for i in c.node_network(node):
        if i.get("type") in ("bridge", "OVSBridge", "vlan", "bond"):
            out.append(i)
    return out


def cmd_network(cs, args):
    nodes = merged_nodes(cs)
    targets = {args.node: nodes[args.node]} if args.node and args.node in nodes else nodes
    if args.node and args.node not in nodes:
        sys.exit(f"node {args.node!r} not found; known: {', '.join(sorted(nodes))}")
    for name, (_, c) in sorted(targets.items()):
        print(f"\n  {name}")
        for i in sorted(_ifaces(c, name), key=lambda x: x.get("iface", "")):
            tag = i.get("tag")
            vids = i.get("bridge_vids") or i.get("bridge_vlan_aware")
            extra = []
            if i.get("bridge_ports"):
                extra.append(f"ports={i['bridge_ports']}")
            if i.get("bridge_vlan_aware"):
                extra.append("vlan-aware")
            if vids and vids != 1:
                extra.append(f"vids={vids}")
            if tag:
                extra.append(f"tag={tag}")
            if i.get("cidr"):
                extra.append(i["cidr"])
            print(f"    {i.get('iface',''):12} {i.get('type',''):10} "
                  f"{i.get('active') and 'up' or 'down':4} {' '.join(str(e) for e in extra)}")


def build_map(cs) -> dict:
    nodes = merged_nodes(cs)
    guests = [r for r, _ in merged_resources(cs) if r.get("type") in ("qemu", "lxc")]
    return {
        "nodes": [
            {"node": name, "status": n.get("status"),
             "cpu": round(float(n.get("cpu", 0)), 3),
             "mem": n.get("mem"), "maxmem": n.get("maxmem"), "uptime": n.get("uptime"),
             "network": [
                 {"iface": i.get("iface"), "type": i.get("type"),
                  "active": i.get("active"), "ports": i.get("bridge_ports"),
                  "vlan_aware": i.get("bridge_vlan_aware"), "vids": i.get("bridge_vids"),
                  "tag": i.get("tag"), "cidr": i.get("cidr")}
                 for i in sorted(_ifaces(c, name), key=lambda x: x.get("iface", ""))
             ]}
            for name, (n, c) in sorted(nodes.items())
        ],
        "guests": [
            {"vmid": r.get("vmid"), "name": r.get("name"), "type": r.get("type"),
             "status": r.get("status"), "node": r.get("node"),
             "maxmem": r.get("maxmem"), "maxdisk": r.get("maxdisk")}
            for r in sorted(guests, key=lambda x: int(x.get("vmid", 0)))
        ],
    }


def cmd_map(cs, args):
    m = build_map(cs)
    text = yaml.safe_dump(m, sort_keys=False, allow_unicode=True, width=120)
    if args.output:
        Path(args.output).write_text(text)
        print(f"wrote {args.output}  ({len(m['nodes'])} nodes, {len(m['guests'])} guests)")
    else:
        print(text)


def cmd_wiki(cs, args):
    m = build_map(cs)
    L = ["# Proxmox — Cluster Inventory & Host Networking\n",
         "> Auto-generated by `scripts/proxmox/pvinfo.py wiki`. Do not hand-edit — re-run to refresh.\n",
         f"**Nodes:** {len(m['nodes'])} · **Guests:** {len(m['guests'])}\n",
         "## Nodes\n",
         "| Node | Status | Mem | Uptime |",
         "|------|--------|-----|--------|"]
    for n in m["nodes"]:
        up = n.get("uptime") or 0
        L.append(f"| {n['node']} | {n['status']} | {gib(n['mem'])}/{gib(n['maxmem'])} "
                 f"| {up // 86400}d |")
    L.append("\n## Host networking (bridges + VLAN tags)\n")
    for n in m["nodes"]:
        L.append(f"### {n['node']}\n")
        L.append("| Interface | Type | Active | Ports | VLAN-aware | VIDs | Tag | CIDR |")
        L.append("|-----------|------|--------|-------|-----------|------|-----|------|")
        for i in n["network"]:
            L.append(f"| {i.get('iface','')} | {i.get('type','')} | {bool(i.get('active'))} "
                     f"| {i.get('ports') or ''} | {i.get('vlan_aware') or ''} "
                     f"| {i.get('vids') or ''} | {i.get('tag') or ''} | {i.get('cidr') or ''} |")
        L.append("")
    L.append("## Guests\n")
    L.append("| VMID | Name | Type | Status | Node |")
    L.append("|------|------|------|--------|------|")
    for g in m["guests"]:
        L.append(f"| {g['vmid']} | {g.get('name','')} | {g['type']} | {g['status']} | {g['node']} |")
    text = "\n".join(L) + "\n"
    if args.output:
        Path(args.output).write_text(text)
        print(f"wrote {args.output}  ({len(text)} bytes)")
    else:
        print(text)


# ─────────────────────────── write commands (guarded) ───────────────────────────
def _require_yes(args, action: str) -> bool:
    if not getattr(args, "yes", False):
        print(f"DRY-RUN: would {action}. Re-run with --yes to execute.")
        return False
    return True


def cmd_vm(cs, args):
    found = find_guest(cs, args.vmid)
    if not found:
        sys.exit(f"guest {args.vmid} not found in cluster resources")
    r, c = found
    if r.get("type") != "qemu":
        sys.exit(f"vmid {args.vmid} is a {r.get('type')}, not a qemu VM")
    node, name = r.get("node"), r.get("name", "")
    action = f"{args.action} VM {args.vmid} ({name}) on {node}"
    if not _require_yes(args, action):
        return
    task = c.post(f"/nodes/{node}/qemu/{args.vmid}/status/{args.action}")
    print(f"OK: {action} — task {task}")


def cmd_snapshot(cs, args):
    found = find_guest(cs, args.vmid)
    if not found:
        sys.exit(f"guest {args.vmid} not found in cluster resources")
    r, c = found
    node, name, kind = r.get("node"), r.get("name", ""), r.get("type")
    action = f"snapshot {kind} {args.vmid} ({name}) on {node} as {args.name!r}"
    if not _require_yes(args, action):
        return
    data = {"snapname": args.name}
    if args.description:
        data["description"] = args.description
    task = c.post(f"/nodes/{node}/{kind}/{args.vmid}/snapshot", data)
    print(f"OK: {action} — task {task}")


# ─────────────────────────── cli ───────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Proxmox VE inventory / control")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("nodes")
    p = sub.add_parser("vms"); p.add_argument("--running", action="store_true")
    sub.add_parser("storage")
    p = sub.add_parser("network"); p.add_argument("--node")
    p = sub.add_parser("map"); p.add_argument("-o", "--output")
    p = sub.add_parser("wiki"); p.add_argument("-o", "--output")

    p = sub.add_parser("vm", help="VM power control (guarded)")
    p.add_argument("action", choices=["start", "stop", "shutdown", "reboot"])
    p.add_argument("vmid")
    p.add_argument("--yes", action="store_true", help="actually execute (default dry-run)")

    p = sub.add_parser("snapshot", help="take a guest snapshot (guarded)")
    p.add_argument("vmid")
    p.add_argument("name")
    p.add_argument("--description")
    p.add_argument("--yes", action="store_true", help="actually execute (default dry-run)")

    args = ap.parse_args()
    cs, _ = clients()
    {
        "nodes": cmd_nodes, "vms": cmd_vms, "storage": cmd_storage,
        "network": cmd_network, "map": cmd_map, "wiki": cmd_wiki,
        "vm": cmd_vm, "snapshot": cmd_snapshot,
    }[args.cmd](cs, args)


if __name__ == "__main__":
    main()
