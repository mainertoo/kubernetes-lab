#!/usr/bin/env python3
"""haauto.py — helper for authoring, shipping and debugging lab Home Assistant automations.

Talks to the LAB HA (not Bell) using the credentials and stdlib WebSocket client in
scripts/home-assistant/hass.py (SOPS-decrypted; HASS_URL/HASS_TOKEN
env vars override). Read-only except `enable` / `disable`.

  haauto.py find <term> [--domain light,fan] [--area master_bedroom]
  haauto.py audit <entity_id>...            # automations that already touch these (or their devices)
  haauto.py check <package.yaml>            # entity ids used vs live HA; templates rendered
  haauto.py template '<jinja>'              # render a template in live HA
  haauto.py logbook <entity_id>... [--minutes 60]   # with "caused by" context
  haauto.py trace <automation_entity_or_id> [--runs 3]
  haauto.py verify <automation_config_id>... [--wait]   # after merge: running, loaded, on
  haauto.py enable|disable <automation_entity>
"""
import argparse
import datetime
import importlib.util
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# hass.py sits one level up (scripts/home-assistant/); resolve() follows the
# ~/.claude/skills/ha-automation symlink back into the repo.
HASS_PY = Path(__file__).resolve().parent.parent / "hass.py"
spec = importlib.util.spec_from_file_location("hass", HASS_PY)
hass = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hass)

CREDS = hass.load_creds()
_ws = None
_n = [1000]

ENTITY_RE = re.compile(
    r"\b(?:alarm_control_panel|automation|binary_sensor|button|camera|climate|counter|cover|"
    r"device_tracker|event|fan|humidifier|input_boolean|input_button|input_datetime|"
    r"input_number|input_select|input_text|light|lock|media_player|number|person|scene|"
    r"script|select|sensor|siren|switch|text|time|timer|vacuum|valve|water_heater|zone)"
    r"\.[a-z0-9_]+\b")


def ws(type_, **kw):
    global _ws
    if _ws is None:
        _ws = hass.HA(CREDS)
    _n[0] += 1
    _ws.ws.send(dict(kw, type=type_, id=_n[0]))
    while True:
        msg = _ws.ws.recv()
        if msg.get("id") == _n[0] and msg.get("type") == "result":
            if not msg.get("success"):
                raise SystemExit(f"WS {type_} failed: {msg.get('error')}")
            return msg.get("result")


def rest(method, path, body=None, raw=False):
    req = urllib.request.Request(
        CREDS["url"].rstrip("/") + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + CREDS["token"], "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as f:
        text = f.read().decode()
    return text if raw else (json.loads(text) if text else None)


def registries():
    ents = {e["entity_id"]: e for e in ws("config/entity_registry/list")}
    devs = {d["id"]: d for d in ws("config/device_registry/list")}
    areas = {a["area_id"]: a["name"] for a in ws("config/area_registry/list")}
    states = {s["entity_id"]: s for s in ws("get_states")}
    return ents, devs, areas, states


def area_of(eid, ents, devs):
    e = ents.get(eid) or {}
    return e.get("area_id") or (devs.get(e.get("device_id")) or {}).get("area_id")


def quirks(eid, ents, states):
    """Flags that change how an automation must treat the entity."""
    e, s = ents.get(eid) or {}, states.get(eid) or {}
    attrs = s.get("attributes", {})
    out = []
    if attrs.get("assumed_state"):
        out.append("ASSUMED-STATE")
    if e.get("platform") == "bond":
        out.append("BOND(on/off may be one RF toggle)")
    if e.get("platform") == "switch_as_x":
        out.append("switch_as_x")
    if e.get("platform") == "template":
        out.append("template(no device/area unless set)")
    if e.get("hidden_by"):
        out.append(f"hidden:{e['hidden_by']}")
    if e.get("disabled_by"):
        out.append(f"DISABLED:{e['disabled_by']}")
    if s.get("state") in ("unavailable", "unknown"):
        out.append(s["state"].upper())
    return out


def cmd_find(a):
    ents, devs, areas, states = registries()
    term = a.term.lower()
    doms = set(a.domain.split(",")) if a.domain else None
    rows = []
    for eid, s in states.items():
        if doms and eid.split(".")[0] not in doms:
            continue
        ar = area_of(eid, ents, devs)
        if a.area and ar != a.area:
            continue
        name = s["attributes"].get("friendly_name", "")
        if term not in eid.lower() and term not in name.lower() and term not in (areas.get(ar) or "").lower():
            continue
        e = ents.get(eid) or {}
        rows.append((eid, name, s["state"], s["attributes"].get("device_class") or "",
                     areas.get(ar, "—"), e.get("platform", ""), " ".join(quirks(eid, ents, states))))
    for r in sorted(rows):
        print("%-50s %-34s %-11s %-10s %-18s %-12s %s" % (r[0], r[1][:34], r[2][:11], r[3][:10], r[4][:18], r[5][:12], r[6]))
    print(f"\n{len(rows)} match(es)")


def automation_configs():
    for s in ws("get_states"):
        if s["entity_id"].startswith("automation."):
            try:
                cfg = ws("automation/config", entity_id=s["entity_id"])["config"]
            except SystemExit:
                cfg = {}
            yield s, cfg


def cmd_audit(a):
    ents, devs, _, _ = registries()
    needles = {}
    for eid in a.entities:
        needles[eid] = eid
        e = ents.get(eid) or {}
        if e.get("device_id"):
            needles[e["device_id"]] = f"device of {eid}"
        if e.get("id"):
            needles[e["id"]] = f"registry id of {eid}"  # device-action entity refs use this
    hit = 0
    for s, cfg in automation_configs():
        blob = json.dumps(cfg)
        why = sorted({label for n, label in needles.items() if n in blob})
        if why:
            hit += 1
            src = "package/YAML" if not str(cfg.get("id", "")).isdigit() else "UI editor"
            print(f"{s['entity_id']}  [{s['state']}, {src}, last {str(s['attributes'].get('last_triggered'))[:19]}]")
            print(f"    references: {', '.join(why)}")
    print(f"\n{hit} automation(s) reference these")


def defined_in(doc):
    """Entity ids a package defines itself (not live until merged)."""
    out = set()
    for dom in ("input_boolean", "input_number", "input_select", "input_text", "input_datetime",
                "input_button", "timer", "counter", "script"):
        for key in (doc.get(dom) or {}):
            out.add(f"{dom}.{key}")
    return out


def script_variables(node):
    """Names declared in any `variables:` block — only bound at runtime."""
    out = set()
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "variables" and isinstance(v, dict):
                out |= set(v)
            out |= script_variables(v)
    elif isinstance(node, list):
        for v in node:
            out |= script_variables(v)
    return out


def cmd_check(a):
    import yaml
    text = Path(a.file).read_text()
    doc = yaml.safe_load(text) or {}
    own = defined_in(doc)
    _, _, _, states = registries()
    services = {f"{d}.{s}" for d, svcs in ws("get_services").items() for s in svcs}
    used = sorted(set(ENTITY_RE.findall(text)) - services)
    bad = 0
    for eid in used:
        if eid in states:
            print(f"  ok      {eid}  ({states[eid]['state']})")
        elif eid in own:
            print(f"  new     {eid}  (defined by this package)")
        else:
            bad += 1
            print(f"  MISSING {eid}")
    # Render every standalone {{ }} template that doesn't depend on trigger/wait/this/vars.
    print()
    runtime = {"trigger", "wait", "this", "repeat"} | script_variables(doc)
    rt_re = re.compile(r"\b(" + "|".join(map(re.escape, sorted(runtime))) + r")\b")
    for tpl in re.findall(r"\{\{.*?\}\}", text, re.S):
        if rt_re.search(tpl):
            print(f"  skip    {' '.join(tpl.split())[:90]}  (runtime-only variables)")
            continue
        try:
            print(f"  render  {' '.join(tpl.split())[:70]}  ->  {rest('POST', '/api/template', {'template': tpl}, raw=True)[:60]}")
        except urllib.error.HTTPError as ex:
            bad += 1
            print(f"  ERROR   {' '.join(tpl.split())[:70]}  ->  {ex.read().decode()[:120]}")
    sys.exit(1 if bad else 0)


def cmd_template(a):
    print(rest("POST", "/api/template", {"template": a.template}, raw=True))


def cmd_logbook(a):
    start = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=a.minutes)).isoformat()
    for e in rest("GET", f"/api/logbook/{start}?entity={','.join(a.entities)}"):
        when = datetime.datetime.fromisoformat(e["when"]).astimezone().strftime("%H:%M:%S")
        cause = e.get("context_entity_id") or e.get("context_event_type") or ""
        print(f"{when}  {e.get('entity_id', ''):50} {e.get('state') or e.get('message', '')}"
              + (f"   ← {cause}" if cause else ""))


def resolve_automation(ref):
    for s in ws("get_states"):
        if s["entity_id"].startswith("automation.") and ref in (s["entity_id"], s["attributes"].get("id")):
            return s
    raise SystemExit(f"no automation matching {ref!r}")


def cmd_trace(a):
    s = resolve_automation(a.automation)
    item = s["attributes"]["id"]
    runs = sorted(ws("trace/list", domain="automation", item_id=item),
                  key=lambda r: r["timestamp"]["start"], reverse=True)[:a.runs]
    if not runs:
        print("no stored traces")
    for r in runs:
        t = ws("trace/get", domain="automation", item_id=item, run_id=r["run_id"])
        start = datetime.datetime.fromisoformat(r["timestamp"]["start"]).astimezone().strftime("%m-%d %H:%M:%S")
        print(f"== {start}  {r.get('state')}  trigger: {r.get('trigger')}  {r.get('script_execution') or ''}")
        for path, steps in t.get("trace", {}).items():
            for st in steps:
                res = st.get("result") or {}
                short = {k: v for k, v in res.items() if k in ("result", "params", "wait", "stop", "error", "choice")}
                print(f"   {path:40} {json.dumps(short, default=str)[:150]}")
        if t.get("error"):
            print(f"   ERROR: {t['error']}")


def wait_running(timeout=300):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if rest("GET", "/api/config").get("state") == "RUNNING":
                return True
        except Exception:
            pass
        time.sleep(5)
    return False


def cmd_verify(a):
    if a.wait and not wait_running():
        raise SystemExit("HA not RUNNING after 5 min")
    cc = rest("POST", "/api/config/core/check_config")
    print(f"check_config: {cc['result']}" + (f"  errors: {cc['errors']}" if cc.get("errors") else ""))
    rc = 0
    for cid in a.ids:
        try:
            s = resolve_automation(cid)
        except SystemExit:
            print(f"  MISSING automation id {cid}")
            rc = 1
            continue
        flag = "" if s["state"] == "on" else "   ⚠️ OFF — HA restores a runtime-disabled automation across restarts; `enable` it"
        print(f"  {s['entity_id']}: {s['state']}{flag}")
        rc |= s["state"] != "on"
    errs = [l for l in ws("system_log/list") if any(c in str(l.get("message")) for c in a.ids)
            or "automation" in l.get("name", "")]
    for l in errs[-10:]:
        print(f"  log {l['level']}: {str(l['message'])[:160]}")
    sys.exit(rc)


def cmd_toggle(a):
    s = resolve_automation(a.automation)
    svc = "turn_on" if a.cmd == "enable" else "turn_off"
    body = {"entity_id": s["entity_id"]}
    if svc == "turn_off":
        body["stop_actions"] = True
    rest("POST", f"/api/services/automation/{svc}", body)
    print(s["entity_id"], resolve_automation(s["entity_id"])["state"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("find"); p.add_argument("term"); p.add_argument("--domain"); p.add_argument("--area")
    p.set_defaults(fn=cmd_find)
    p = sub.add_parser("audit"); p.add_argument("entities", nargs="+"); p.set_defaults(fn=cmd_audit)
    p = sub.add_parser("check"); p.add_argument("file"); p.set_defaults(fn=cmd_check)
    p = sub.add_parser("template"); p.add_argument("template"); p.set_defaults(fn=cmd_template)
    p = sub.add_parser("logbook"); p.add_argument("entities", nargs="+"); p.add_argument("--minutes", type=int, default=60)
    p.set_defaults(fn=cmd_logbook)
    p = sub.add_parser("trace"); p.add_argument("automation"); p.add_argument("--runs", type=int, default=3)
    p.set_defaults(fn=cmd_trace)
    p = sub.add_parser("verify"); p.add_argument("ids", nargs="+"); p.add_argument("--wait", action="store_true")
    p.set_defaults(fn=cmd_verify)
    for c in ("enable", "disable"):
        p = sub.add_parser(c); p.add_argument("automation"); p.set_defaults(fn=cmd_toggle)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
