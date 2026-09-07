#!/usr/bin/env python3
"""Declarative Home Assistant registry overrides — desired state in git.

HA has no YAML for the entity/device registry: names, aliases, device_class
overrides, hidden flags, areas and Assist exposure all live in `.storage`, which
HA owns and rewrites at runtime. So a bare-metal restore brings template
entities back from git but loses every override.

This converges the live registry onto `registry-overrides.yaml`:

    ./ha-registry.py check                 # diff only, exit 1 if drifted
    ./ha-registry.py apply                 # converge, printing each change
    ./ha-registry.py export lock.x sw.y    # dump live state as manifest YAML

Devices are matched by MAC, not device_id — device_ids are regenerated when a
device is removed and re-added, MACs are not.

`aliases` is asserted as a closed set: an entity listed here with no `aliases:`
key must have none, and a stray alias added in the UI is reported as drift.
Other fields are managed only where declared.

Works against the remote instance too:  ./ha-remote ./ha-registry.py check
"""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "registry-overrides.yaml"

# Entity fields we manage. `expose` is not a registry field — it goes through
# homeassistant/expose_entity — so it is handled separately.
ENTITY_FIELDS = ("name", "icon", "area_id", "aliases", "device_class", "hidden_by")


def load_hass():
    spec = importlib.util.spec_from_file_location("hass", HERE / "hass.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Registry:
    def __init__(self, ha):
        self.ha = ha
        self._n = 0

    def call(self, payload):
        self._n += 1
        payload = dict(payload, id=self._n)
        self.ha.ws.send(payload)
        while True:
            msg = self.ha.ws.recv()
            if msg.get("id") == self._n and msg.get("type") == "result":
                if not msg.get("success"):
                    raise SystemExit(f"WS {payload['type']} failed: {json.dumps(msg.get('error'))}")
                return msg.get("result")

    def entity(self, entity_id):
        return self.call({"type": "config/entity_registry/get", "entity_id": entity_id})

    def devices(self):
        return self.call({"type": "config/device_registry/list"})


def norm_mac(v):
    return (v or "").replace(":", "").replace("-", "").lower()


def device_by_mac(devices, mac):
    want = norm_mac(mac)
    for d in devices:
        for conn in d.get("connections", []):
            if len(conn) == 2 and conn[0] == "mac" and norm_mac(conn[1]) == want:
                return d
    return None


def exposed(entry):
    return ((entry.get("options") or {}).get("conversation") or {}).get("should_expose")


def entity_diff(want, live):
    """Return {field: (live, want)} for fields that differ.

    `aliases` is ALWAYS asserted as a closed set — omitting it means "this entity
    must have none". Aliases are the voice-routing surface, and a stray one added
    out of band is exactly what makes Assist answer about the wrong device, so
    tolerating undeclared aliases would defeat the point of this file. Every other
    field is managed only when the manifest declares it, so the manifest can be
    adopted incrementally without nulling settings it says nothing about.
    """
    out = {}
    cur_aliases = sorted(a for a in (live.get("aliases") or []) if a)
    want_aliases = sorted(want.get("aliases") or [])
    if cur_aliases != want_aliases:
        out["aliases"] = (cur_aliases, want_aliases)
    for f in ENTITY_FIELDS:
        if f == "aliases" or f not in want:
            continue
        if live.get(f) != want[f]:
            out[f] = (live.get(f), want[f])
    if "expose" in want and exposed(live) != want["expose"]:
        out["expose"] = (exposed(live), want["expose"])
    return out


def cmd_export(reg, args):
    import yaml
    ents = {}
    for eid in args.entity_ids:
        e = reg.entity(eid)
        block = {}
        for f in ENTITY_FIELDS:
            v = e.get(f)
            if f == "aliases":
                v = sorted(a for a in (v or []) if a)
                if not v:
                    continue
            if v is not None:
                block[f] = v
        ex = exposed(e)
        if ex is not None:
            block["expose"] = ex
        ents[eid] = block
    print(yaml.safe_dump({"entities": ents}, sort_keys=True, allow_unicode=True))


def run(reg, manifest, apply_changes):
    import yaml
    spec = yaml.safe_load(manifest.read_text()) or {}
    drift = 0

    devices = reg.devices()
    for d in spec.get("devices") or []:
        mac = d["mac"]
        live = device_by_mac(devices, mac)
        label = d.get("comment", mac)
        if live is None:
            print(f"  ?? device {label}: no device with MAC {mac} — skipped")
            continue
        changes = {}
        if "area" in d and live.get("area_id") != d["area"]:
            changes["area_id"] = (live.get("area_id"), d["area"])
        if "name_by_user" in d and live.get("name_by_user") != d["name_by_user"]:
            changes["name_by_user"] = (live.get("name_by_user"), d["name_by_user"])
        for f, (cur, new) in changes.items():
            drift += 1
            print(f"  {'APPLY' if apply_changes else 'DRIFT'} device {label}: {f}: {cur!r} -> {new!r}")
        if changes and apply_changes:
            reg.call({"type": "config/device_registry/update", "device_id": live["id"],
                      **{f: v[1] for f, v in changes.items()}})

    for eid, want in (spec.get("entities") or {}).items():
        try:
            live = reg.entity(eid)
        except SystemExit:
            print(f"  ?? {eid}: not in the registry — skipped")
            continue
        d = entity_diff(want, live)
        if not d:
            continue
        for f, (cur, new) in d.items():
            drift += 1
            print(f"  {'APPLY' if apply_changes else 'DRIFT'} {eid}: {f}: {cur!r} -> {new!r}")
        if apply_changes:
            # take the desired value from the diff, not from `want` — a closed-set
            # field (aliases) can be in the diff while absent from the manifest.
            payload = {f: new for f, (_cur, new) in d.items() if f != "expose"}
            if payload:
                reg.call({"type": "config/entity_registry/update", "entity_id": eid, **payload})
            if "expose" in d:
                reg.call({"type": "homeassistant/expose_entity", "assistants": ["conversation"],
                          "entity_ids": [eid], "should_expose": d["expose"][1]})
    return drift


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="report drift, exit 1 if any")
    sub.add_parser("apply", help="converge the registry onto the manifest")
    p = sub.add_parser("export", help="dump live overrides for entities as manifest YAML")
    p.add_argument("entity_ids", nargs="+")
    ap.add_argument("-f", "--file", default=str(MANIFEST))
    args = ap.parse_args()

    m = load_hass()
    ha = m.HA(m.load_creds())
    reg = Registry(ha)
    try:
        if args.cmd == "export":
            cmd_export(reg, args)
            return
        manifest = Path(args.file)
        if not manifest.exists():
            sys.exit(f"manifest not found: {manifest}")
        drift = run(reg, manifest, apply_changes=(args.cmd == "apply"))
    finally:
        ha.close()

    if args.cmd == "check":
        print(f"\n{drift} drifted field(s)" if drift else "\nin sync ✅")
        sys.exit(1 if drift else 0)
    print(f"\napplied {drift} change(s)" if drift else "\nalready in sync ✅")


if __name__ == "__main__":
    main()
