#!/usr/bin/env python3
"""
hass.py — pull Home Assistant's device picture and sync it into Homebox inventory.

Home Assistant knows manufacturer / model / room (area) / MAC / integration / firmware
for every device it manages — but only in its **device registry**, which is NOT exposed
over the REST API. It lives behind the authenticated **WebSocket API**. This tool speaks
that protocol (with a tiny embedded stdlib WebSocket client — no pip deps), pulls the
device + area + entity registries, then cross-matches to Homebox **by MAC address** to:

  * gap-fill make/model on Homebox items that already have the MAC (in serialNumber),
  * relocate those items into the right room when HA knows the area,
  * add the physical devices HA knows that Homebox is missing.

It is the HA-side companion to scripts/unifi/netinfo.py (which seeded Homebox from the
UDM SE) and reuses scripts/homebox/inventory.py for all Homebox access.

Subcommands:
  ./hass.py ping                       # auth check: HA version + device/area counts
  ./hass.py devices                    # human table: name · make · model · room · MAC · integration
  ./hass.py pull -o ha-devices.yaml    # full structured dump of HA's physical devices
  ./hass.py sync                       # PLAN the Homebox enrich/relocate/create (writes nothing)
  ./hass.py sync --commit              # apply the plan to Homebox
  ./hass.py wiki                       # (stub — a later task)

Matching is by MAC, normalized lowercase colon-form — the same convention netinfo.py used
to write MACs into each Homebox item's serialNumber field. Existing make/model values are
NEVER clobbered (gap-fill only); conflicts are reported, not overwritten. Relocation only
moves items that are currently in 'Unsorted (auto-import)' or unset.

Auth: scripts/home-assistant/credentials.sops.yaml (via `sops -d`), or env
HASS_URL / HASS_TOKEN. The token is a HA Long-Lived Access Token
(Profile → Security → Long-Lived Access Tokens → Create Token).

Stdlib only (socket/ssl/urllib) + PyYAML. Reuses ../homebox/inventory.py as a module.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import ssl
import struct
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: pip3 install pyyaml")

HERE = Path(__file__).resolve().parent
CRED_FILE = HERE / "credentials.sops.yaml"
HOMEBOX_DIR = HERE.parent / "homebox"

# Import the existing Homebox tool as a library (Client, helpers, safe update body).
sys.path.insert(0, str(HOMEBOX_DIR))
try:
    import inventory as hbox  # type: ignore
except Exception as e:  # pragma: no cover
    sys.exit(f"could not import ../homebox/inventory.py: {e}")

# Homebox locations to treat as "not really placed" → eligible for relocation.
UNPLACED_LOCATIONS = {"unsorted (auto-import)", "unsorted", ""}
HA_TAG = "HomeAssistant"


# ─────────────────────────── auth / creds ───────────────────────────
def load_creds() -> dict:
    url = os.environ.get("HASS_URL")
    token = os.environ.get("HASS_TOKEN")
    if url and token:
        return {"url": url, "token": token}
    if not CRED_FILE.exists():
        sys.exit(
            f"No HASS_URL/HASS_TOKEN env and {CRED_FILE} not found.\n"
            "Create credentials.sops.yaml (see README) or export HASS_URL + HASS_TOKEN."
        )
    try:
        out = subprocess.run(
            ["sops", "-d", str(CRED_FILE)], capture_output=True, text=True, check=True
        ).stdout
    except FileNotFoundError:
        sys.exit("`sops` not on PATH — install it or use HASS_* env vars.")
    except subprocess.CalledProcessError as e:
        sys.exit(f"sops -d failed: {e.stderr}")
    data = (yaml.safe_load(out) or {}).get("stringData", {})
    url = url or data.get("HASS_URL")
    token = token or data.get("HASS_TOKEN")
    if not url or not token:
        sys.exit("credentials file missing HASS_URL or HASS_TOKEN")
    if "PASTE-YOUR" in token or "PLACEHOLDER" in token.upper():
        sys.exit("credentials.sops.yaml still has the placeholder token — paste your "
                 "HA Long-Lived Access Token and save (auto-encrypts).")
    return {"url": url, "token": token}


# ───────────────────── minimal stdlib WebSocket client ─────────────────────
class WSClient:
    """Just enough RFC6455 to talk to HA: TLS, client-masked text frames, no fragments."""

    def __init__(self, url: str, insecure: bool = False, timeout: float = 20.0):
        # https://host[:port]  →  wss://host[:port]/api/websocket
        u = url.rstrip("/")
        secure = u.startswith("https://") or u.startswith("wss://")
        host = u.split("://", 1)[1]
        host = host.split("/", 1)[0]
        if ":" in host:
            hostname, port = host.split(":", 1)
            port = int(port)
        else:
            hostname, port = host, (443 if secure else 80)
        self.path = "/api/websocket"
        raw = socket.create_connection((hostname, port), timeout=timeout)
        if secure:
            ctx = ssl._create_unverified_context() if insecure else ssl.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=hostname)
        self.sock = raw
        self._buf = b""
        self._handshake(hostname, port)

    def _handshake(self, hostname, port):
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {hostname}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        # Read headers up to the blank line.
        while b"\r\n\r\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WS handshake: server closed before response")
            self._buf += chunk
        head, self._buf = self._buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if "101" not in status:
            raise ConnectionError(f"WS handshake failed: {status}")

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("WS: connection closed mid-frame")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send(self, obj: dict):
        payload = json.dumps(obj).encode()
        header = bytearray([0x81])  # FIN + text
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv(self) -> dict:
        """Read one full text message (handles control frames + fragmentation)."""
        data = b""
        while True:
            b0, b1 = self._recv_exact(2)
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length)
            if masked:
                payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
            if opcode == 0x8:  # close
                raise ConnectionError("WS: server sent close frame")
            if opcode == 0x9:  # ping → pong
                self._pong(payload)
                continue
            if opcode == 0xA:  # pong
                continue
            data += payload
            if fin:
                return json.loads(data.decode())

    def _pong(self, payload: bytes):
        header = bytearray([0x8A])
        n = len(payload)
        header.append(0x80 | n)  # control frames are always < 126
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


# ─────────────────────────── HA fetch ───────────────────────────
class HA:
    def __init__(self, creds: dict, insecure: bool = False):
        self.ws = WSClient(creds["url"], insecure=insecure)
        self._id = 0
        self.ha_version = "?"
        self._auth(creds["token"])

    def _auth(self, token: str):
        msg = self.ws.recv()
        if msg.get("type") != "auth_required":
            raise ConnectionError(f"unexpected first message: {msg}")
        self.ha_version = msg.get("ha_version", "?")
        self.ws.send({"type": "auth", "access_token": token})
        res = self.ws.recv()
        if res.get("type") != "auth_ok":
            raise SystemExit(
                "HA auth failed — the token was rejected "
                f"({res.get('message', res.get('type'))}). Mint a fresh Long-Lived "
                "Access Token in HA and update credentials.sops.yaml."
            )
        self.ha_version = res.get("ha_version", self.ha_version)

    def cmd(self, type_: str) -> list:
        self._id += 1
        mid = self._id
        self.ws.send({"id": mid, "type": type_})
        while True:
            msg = self.ws.recv()
            if msg.get("id") != mid:
                continue  # skip events / other ids
            if not msg.get("success", False):
                raise SystemExit(f"HA command {type_!r} failed: {msg.get('error')}")
            return msg.get("result", [])

    def registries(self) -> dict:
        return {
            "devices": self.cmd("config/device_registry/list"),
            "areas": self.cmd("config/area_registry/list"),
            "entities": self.cmd("config/entity_registry/list"),
        }

    def close(self):
        self.ws.close()


# ─────────────────────────── normalization ───────────────────────────
def norm_mac(value) -> str:
    """Return a lowercase colon-form MAC, or '' if it isn't a MAC."""
    if not value:
        return ""
    s = str(value).strip().lower().replace("-", ":")
    hexs = s.replace(":", "")
    if len(hexs) == 12 and all(c in "0123456789abcdef" for c in hexs):
        if hexs == "000000000000":
            return ""  # all-zero placeholder MAC (e.g. some DLNA devices)
        return ":".join(hexs[i:i + 2] for i in range(0, 12, 2))
    return ""


def device_macs(dev: dict) -> list[str]:
    out = []
    for conn in dev.get("connections") or []:
        # connections come as [type, value] pairs
        if isinstance(conn, (list, tuple)) and len(conn) == 2 and conn[0] == "mac":
            m = norm_mac(conn[1])
            if m and m not in out:
                out.append(m)
    return out


def device_name(dev: dict) -> str:
    return (dev.get("name_by_user") or dev.get("name") or "").strip()


def integration_of(dev: dict, entities: list, ent_by_device: dict) -> str:
    """Best-effort integration/platform label for a device (e.g. hue, esphome, zwave_js)."""
    plats = sorted({e.get("platform") for e in ent_by_device.get(dev["id"], [])
                    if e.get("platform")})
    return plats[0] if plats else ""


def physical_devices(reg: dict, include_all: bool = False):
    """Filter HA's device registry to real hardware. Returns (devices, n_skipped)."""
    devices = reg["devices"]
    ent_by_device: dict[str, list] = {}
    for e in reg["entities"]:
        ent_by_device.setdefault(e.get("device_id"), []).append(e)
    area_name = {a["area_id"]: a.get("name", "") for a in reg["areas"]}

    out, skipped = [], 0
    for d in devices:
        macs = device_macs(d)
        mfr = (d.get("manufacturer") or "").strip()
        has_hw = bool(mfr) or bool(d.get("connections"))
        is_service = d.get("entry_type") == "service"
        # manufacturer "Home Assistant" = HA-internal virtual devices (HomeKit/HomeBridge
        # bridges exposing entities to Apple Home) with generated MACs — not hardware, and
        # the real device is registered separately by its own integration.
        is_virtual = mfr.lower() == "home assistant"
        if not include_all and (not has_hw or is_virtual or (is_service and not macs)):
            skipped += 1
            continue
        out.append({
            "id": d["id"],
            "name": device_name(d),
            "manufacturer": (d.get("manufacturer") or "").strip(),
            "model": (d.get("model") or "").strip(),
            "area": area_name.get(d.get("area_id"), ""),
            "macs": macs,
            "integration": integration_of(d, reg["entities"], ent_by_device),
            "sw_version": (d.get("sw_version") or "").strip(),
            "entry_type": d.get("entry_type") or "",
        })
    out.sort(key=lambda x: (x["area"], x["name"].lower()))
    return out, skipped


# ─────────────────────────── area → location map ───────────────────────────
# HA area names that map onto an existing Homebox location path (lowercased keys).
AREA_ALIASES = {
    "living room": "Main Floor/Living Room",
}

# HA areas to nest under an existing floor parent instead of creating top-level
# (the existing tree nests rooms under Main Floor / Upstairs Floor). Lowercased keys.
AREA_NESTING = {
    "main floor bathroom": "Main Floor/Bathroom",
    "main floor hallway":  "Main Floor/Hallway",
    "upstairs hallway":    "Upstairs Floor/Upstairs Hallway",
}


def build_area_map(devices, loc_paths: dict) -> dict:
    """area name -> Homebox location PATH. Reuse an existing location when the area name
    matches a location's full path, alias, or leaf name; otherwise return the area name
    itself, which sync() will create as a new top-level location.

    Returned paths are chosen so that `path.lower()` resolves against loc_paths (whose
    keys are lowercased) for any location that already exists.
    """
    # leaf name (lowercased) -> existing full path (lowercased)
    leaf_to_path = {}
    for low_path in sorted(loc_paths):
        leaf = low_path.rsplit("/", 1)[-1]
        leaf_to_path.setdefault(leaf, low_path)
    out = {}
    for area in sorted({d["area"] for d in devices if d["area"]}):
        low = area.lower()
        if low in AREA_NESTING:
            out[area] = AREA_NESTING[low]          # nest under an existing floor parent
        elif low in AREA_ALIASES:
            out[area] = AREA_ALIASES[low]          # e.g. "Main Floor/Living Room"
        elif low in loc_paths:
            out[area] = area                       # top-level location already exists
        elif low in leaf_to_path:
            out[area] = leaf_to_path[low]          # matches a nested location's leaf
        else:
            out[area] = area                       # new top-level location
    return out


# ─────────────────────────── Homebox side ───────────────────────────
def homebox_index(hb) -> tuple[dict, dict, list, dict]:
    """Return (mac -> item, ha-serial -> item, all details, loc_paths).

    The ha-serial index ('ha:<device_id>' values we write for MAC-less HA devices) makes
    re-runs idempotent — without it, every no-MAC device would be re-created each run."""
    items = hbox.all_items(hb)
    details = {}
    mac_index = {}
    ha_index = {}
    for it in items:
        full = hb.get("/v1/items/" + it["id"])
        details[it["id"]] = full
        sn = (full.get("serialNumber") or "").strip()
        m = norm_mac(sn)
        if m:
            mac_index[m] = full
        elif sn.lower().startswith("ha:"):
            ha_index[sn.lower()] = full
    loc_paths = hbox.location_paths(hb)
    return mac_index, ha_index, list(details.values()), loc_paths


def ensure_location_path(hb, path: str, loc_paths: dict, commit: bool) -> str:
    """Resolve a slash-path to a location id, creating any missing segments under their
    parent. loc_paths is keyed by lowercased path. In dry-run, missing segments get a
    '<new:...>' sentinel id (truthy, so relocation preview works) and nothing is written."""
    parent_id = None
    cur = ""
    for name in path.split("/"):
        cur = f"{cur}/{name}" if cur else name
        key = cur.lower()
        if key in loc_paths:
            parent_id = loc_paths[key]
            continue
        print(f"  plan   create   location  {cur}")
        if commit:
            created = hb.post("/v1/locations", {"name": name, "parentId": parent_id})
            parent_id = created["id"]
        else:
            parent_id = f"<new:{cur}>"
        loc_paths[key] = parent_id
    return loc_paths[path.lower()]


def current_location_path(full: dict, loc_paths: dict) -> str:
    loc = full.get("location") or {}
    lid = loc.get("id")
    if not lid:
        return ""
    id_to_path = {v: k for k, v in loc_paths.items()}
    return id_to_path.get(lid, (loc.get("name") or "").lower())


# ─────────────────────────── commands ───────────────────────────
def cmd_ping(args):
    ha = HA(load_creds(), insecure=args.insecure)
    reg = ha.registries()
    ha.close()
    phys, skipped = physical_devices(reg, include_all=args.all)
    print(f"Home Assistant {ha.ha_version} — auth OK")
    print(f"devices: {len(reg['devices'])} total, {len(phys)} physical "
          f"({skipped} pseudo/service skipped)")
    print(f"areas:   {len(reg['areas'])}")
    print(f"entities:{len(reg['entities'])}")


def cmd_devices(args):
    ha = HA(load_creds(), insecure=args.insecure)
    reg = ha.registries()
    ha.close()
    phys, skipped = physical_devices(reg, include_all=args.all)
    w = {"name": 28, "mfr": 16, "model": 20, "area": 16, "intg": 12}
    hdr = (f"{'NAME':<{w['name']}} {'MAKE':<{w['mfr']}} {'MODEL':<{w['model']}} "
           f"{'ROOM':<{w['area']}} {'INTEGRATION':<{w['intg']}} MAC")
    print(hdr)
    print("-" * len(hdr))
    for d in phys:
        print(f"{d['name'][:w['name']]:<{w['name']}} "
              f"{d['manufacturer'][:w['mfr']]:<{w['mfr']}} "
              f"{d['model'][:w['model']]:<{w['model']}} "
              f"{d['area'][:w['area']]:<{w['area']}} "
              f"{d['integration'][:w['intg']]:<{w['intg']}} "
              f"{(d['macs'][0] if d['macs'] else '—')}")
    print(f"\n{len(phys)} physical devices ({skipped} skipped). "
          f"{sum(1 for d in phys if d['macs'])} have a MAC.")


def cmd_pull(args):
    ha = HA(load_creds(), insecure=args.insecure)
    reg = ha.registries()
    ha.close()
    phys, skipped = physical_devices(reg, include_all=args.all)
    spec = {"devices": phys}
    text = yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=100)
    if args.output:
        Path(args.output).write_text(text)
        print(f"wrote {args.output}  ({len(phys)} devices, {skipped} skipped)")
    else:
        print(text)


def cmd_sync(args):
    creds_ha = load_creds()
    ha = HA(creds_ha, insecure=args.insecure)
    reg = ha.registries()
    ha.close()
    phys, skipped = physical_devices(reg, include_all=args.all)

    hb = hbox.Client(hbox.load_creds(), insecure=args.insecure)
    print("reading Homebox items (full detail) …", file=sys.stderr)
    mac_index, ha_index, details, loc_paths = homebox_index(hb)
    area_map = build_area_map(phys, loc_paths)

    commit = args.commit
    banner = "COMMIT" if commit else "DRY-RUN"
    print(f"\n{banner}  HA {ha.ha_version} → Homebox\n")

    # Homebox requires a location on create — devices with no HA area land here.
    fallback_path = "Unsorted (auto-import)"
    fallback_id = loc_paths.get(fallback_path.lower())

    # 1) area → location mapping (review block)
    print("AREA → LOCATION MAP")
    for area in sorted(area_map):
        path = area_map[area]
        exists = path.lower() in loc_paths
        print(f"  {area:<22} → {path}{'' if exists else '   (NEW location)'}")
    new_locs = sorted({area_map[a] for a in area_map if area_map[a].lower() not in loc_paths})
    print()

    actions = {"enrich": 0, "relocate": 0, "create": 0, "noop": 0}
    conflicts = []

    # 2) ensure new locations exist (creating any missing parent segments first)
    for path in new_locs:
        ensure_location_path(hb, path, loc_paths, commit)

    # 3) ensure tags (HomeAssistant + per-integration)
    want_tags = {HA_TAG}
    for d in phys:
        if d["integration"]:
            want_tags.add(d["integration"])
    live_tags = hbox.tags_by_name(hb)
    tag_id = {}
    for t in sorted(want_tags):
        existing = live_tags.get(t.lower())
        if existing:
            tag_id[t.lower()] = existing["id"]
        else:
            print(f"  plan   create   tag       {t}")
            if commit:
                created = hb.post("/v1/tags", {"name": t})
                tag_id[t.lower()] = created["id"]

    # 4) reconcile each physical device
    print()
    for d in phys:
        match = None
        for m in d["macs"]:
            if m in mac_index:
                match = mac_index[m]
                break
        if match is None and not d["macs"]:
            match = ha_index.get(f"ha:{d['id']}".lower())  # idempotent re-match for MAC-less
        target_path = area_map.get(d["area"], "") if d["area"] else ""
        target_loc_id = loc_paths.get(target_path.lower()) if target_path else None

        if match:
            full = match
            spec = {}
            # gap-fill make/model only when empty; record conflicts otherwise
            if d["manufacturer"]:
                cur = (full.get("manufacturer") or "").strip()
                if not cur:
                    spec["manufacturer"] = d["manufacturer"]
                elif cur.lower() != d["manufacturer"].lower():
                    conflicts.append(f"{full['name']}: manufacturer Homebox={cur!r} HA={d['manufacturer']!r}")
            if d["model"]:
                cur = (full.get("modelNumber") or "").strip()
                if not cur:
                    spec["modelNumber"] = d["model"]
                elif cur.lower() != d["model"].lower():
                    conflicts.append(f"{full['name']}: model Homebox={cur!r} HA={d['model']!r}")
            # relocate only if currently unplaced and we know the area
            cur_path = current_location_path(full, loc_paths)
            relocate = False
            new_loc_id = None
            if target_loc_id and cur_path.rsplit("/", 1)[-1] in UNPLACED_LOCATIONS:
                new_loc_id = target_loc_id
                relocate = True
            # provenance note
            note = ha_note(d)
            existing_notes = (full.get("notes") or "").strip()
            if note not in existing_notes:
                spec["notes"] = (existing_notes + ("\n" if existing_notes else "") + note).strip()

            if not spec and not relocate:
                actions["noop"] += 1
                continue
            verbs = []
            if "manufacturer" in spec or "modelNumber" in spec:
                verbs.append("enrich")
                actions["enrich"] += 1
            if relocate:
                verbs.append(f"relocate→{target_path}")
                actions["relocate"] += 1
            if "notes" in spec and not verbs:
                verbs.append("note")
            print(f"  plan   {'/'.join(verbs):24} {full['name']}  [{d['macs'][0]}]")
            if commit:
                body = hbox.Planner._update_body(full, spec, new_loc_id, None)
                hb.put("/v1/items/" + full["id"], body)
        else:
            # new HA-only device
            actions["create"] += 1
            name = d["name"] or f"HA device {d['id'][:8]}"
            serial = d["macs"][0] if d["macs"] else f"ha:{d['id']}"
            create_loc_id = target_loc_id or fallback_id
            loc_disp = target_path or fallback_path
            print(f"  plan   create                   {name}  @ {loc_disp}  [{serial}]")
            if commit:
                tags = [tag_id[HA_TAG.lower()]]
                if d["integration"] and d["integration"].lower() in tag_id:
                    tags.append(tag_id[d["integration"].lower()])
                body = {"name": name, "locationId": create_loc_id, "quantity": 1,
                        "tagIds": tags}
                created = hb.post("/v1/items", {k: v for k, v in body.items()
                                                if k in hbox.CREATE_FIELDS})
                full = hb.get("/v1/items/" + created["id"])
                spec = {"name": name, "serialNumber": serial, "notes": ha_note(d)}
                if d["manufacturer"]:
                    spec["manufacturer"] = d["manufacturer"]
                if d["model"]:
                    spec["modelNumber"] = d["model"]
                put_body = hbox.Planner._update_body(full, spec, create_loc_id, tags)
                hb.put("/v1/items/" + created["id"], put_body)

    # 5) summary
    print(f"\nSUMMARY ({banner})")
    print(f"  HA physical devices : {len(phys)}  ({skipped} skipped, "
          f"{sum(1 for d in phys if d['macs'])} with MAC)")
    print(f"  matched in Homebox  : {sum(1 for d in phys if any(m in mac_index for m in d['macs']))}")
    print(f"  enrich (make/model) : {actions['enrich']}")
    print(f"  relocate            : {actions['relocate']}")
    print(f"  create (HA-only)    : {actions['create']}")
    print(f"  new locations       : {len(new_locs)}")
    print(f"  already complete    : {actions['noop']}")
    if conflicts:
        print(f"\nCONFLICTS ({len(conflicts)}) — Homebox value kept, NOT overwritten:")
        for c in conflicts:
            print(f"  ! {c}")
    if not commit:
        print("\n  DRY-RUN — re-run with --commit to write these changes to Homebox.")


def ha_note(d: dict) -> str:
    bits = ["HA"]
    if d["integration"]:
        bits.append(d["integration"])
    if d["sw_version"]:
        bits.append(f"fw {d['sw_version']}")
    if d["area"]:
        bits.append(f"area {d['area']}")
    return " · ".join(bits)


def cmd_wiki(args):
    sys.exit("`wiki` is a stub for a later task (HA device/area wiki page). Not built yet.")


# ─────────────────────────── main ───────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Home Assistant → Homebox inventory sync.")
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification")
    ap.add_argument("--all", action="store_true",
                    help="include HA pseudo/service devices (debug)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ping", help="auth check + counts")
    sub.add_parser("devices", help="human-readable device table")
    p = sub.add_parser("pull", help="dump HA physical devices to YAML")
    p.add_argument("-o", "--output")
    p = sub.add_parser("sync", help="enrich/relocate/create Homebox items (dry-run default)")
    p.add_argument("--commit", action="store_true", help="write changes (default: plan only)")
    sub.add_parser("wiki", help="(stub) HA wiki page — later task")

    args = ap.parse_args()
    {
        "ping": cmd_ping,
        "devices": cmd_devices,
        "pull": cmd_pull,
        "sync": cmd_sync,
        "wiki": cmd_wiki,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
