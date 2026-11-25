#!/usr/bin/env python3
"""home_server deploy-drift check for the update-gh skill.

mainertoo/home_server holds Docker compose stacks deployed OUTSIDE the K3s
cluster by two deployers that both poll git:

  * Komodo  (Core on QNAS, 192.168.1.252:9120): spark + vps stacks, procedure
    `git-auto-deploy` (DeployStackIfChanged *, every 5 min).
  * Portainer (QNAS, :9443): QNAS (ep 2), zwave-js (ep 15), matter (ep 17)
    stacks, most with git auto-update 5m.

A merged Renovate PR is only "done" once the running container uses the new
image. This script compares, per live stack, the image refs in the compose file
at home_server@main with what is deployed, and reports Komodo server and
auto-deploy health. (2026-11 incident: both Komodo periphery agents silently
lost Core during the QNAS hang, and nothing deployed for 3 weeks while PRs
looked merged.)

Read-only. Exit 0 = all in sync; 1 = drift or deployer unhealthy.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "portainer"))
import portainer as pt  # noqa: E402

GH_REPO = "mainertoo/home_server"
KOMODO_CREDS = REPO_ROOT / "scripts" / "komodo" / "credentials.sops.yaml"
LIVE_PORTAINER_EPS = {2: "qnas", 15: "zwave-js", 17: "matter"}

problems: list[str] = []
notes: list[str] = []


def gh_file(path: str) -> str:
    out = subprocess.run(["gh", "api", f"repos/{GH_REPO}/contents/{path}?ref=main",
                          "--jq", ".content"], capture_output=True, text=True)
    if out.returncode != 0:
        return ""
    return base64.b64decode(out.stdout).decode()


def norm(img: str) -> str:
    img = img.split("@")[0]
    for p in ("docker.io/library/", "docker.io/", "library/"):
        if img.startswith(p):
            img = img[len(p):]
    if ":" not in img.rsplit("/", 1)[-1]:
        img += ":latest"
    return img


def compose_images(text: str) -> set[str]:
    imgs = set()
    for line in text.splitlines():
        m = re.match(r"^\s*image:\s*['\"]?([^'\"#\s]+)", line)
        if m:
            imgs.add(norm(m.group(1)))
    return imgs


def compare(label: str, repo: set[str], live: set[str]):
    repo = {i for i in repo if "${" not in i}
    if not live:
        # Usually a leftover Portainer stack record for an app that moved
        # (e.g. immich -> K3s). Reported, not counted: confirm and delete it.
        print(f"  note  {label}: no running containers (stale stack record, or down?)")
        notes.append(f"{label}: no running containers")
        return
    missing = sorted(repo - live)
    if not missing:
        print(f"  ok    {label}")
        return
    base = {i.rsplit(":", 1)[0]: i for i in live}
    for m in missing:
        was = base.get(m.rsplit(":", 1)[0], "not running")
        print(f"  DRIFT {label}: repo {m}  live {was}")
        problems.append(f"{label}: {m} not deployed (live {was})")


# ------------------------------------------------------------------ Komodo
def komodo():
    sd = json.loads(subprocess.run(
        ["sops", "-d", "--output-type", "json", str(KOMODO_CREDS)],
        capture_output=True, text=True, check=True).stdout)["stringData"]
    url, key, sec = sd["KOMODO_URL"], sd["KOMODO_ADMIN_API_KEY"], sd["KOMODO_ADMIN_API_SECRET"]

    def call(typ: str, params: dict | None = None):
        req = urllib.request.Request(
            f"{url}/read", method="POST",
            data=json.dumps({"type": typ, "params": params or {}}).encode(),
            headers={"X-Api-Key": key, "X-Api-Secret": sec,
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode() or "null")

    print("Komodo")
    servers = {s["id"]: s for s in call("ListServers")}
    for s in servers.values():
        st = s["info"]["state"]
        print(f"  {'ok   ' if st == 'Ok' else 'DOWN '} server {s['name']}: {st}")
        if st != "Ok":
            problems.append(f"Komodo server {s['name']} is {st}: periphery disconnected? "
                            "`docker restart komodo-periphery` on that host")
    for p in call("ListProcedures"):
        if p["name"] != "git-auto-deploy":
            continue
        info = p["info"]
        scheduled = info.get("next_scheduled_run") is not None
        ok = scheduled and info["state"] == "Ok"
        print(f"  {'ok   ' if ok else 'WARN '} git-auto-deploy: last={info['state']} "
              f"scheduled={scheduled}")
        if not scheduled:
            problems.append("Komodo git-auto-deploy schedule is DISABLED")
        if info["state"] != "Ok":
            problems.append(f"Komodo git-auto-deploy last run {info['state']}")
    for st in call("ListStacks"):
        full = call("GetStack", {"stack": st["id"]})
        cfg = full["config"]
        run_dir = (cfg.get("run_directory") or "").strip("./")
        repo: set[str] = set()
        for f in cfg.get("file_paths") or ["docker-compose.yml", "docker-compose.yaml",
                                            "compose.yaml", "dock-compose.yaml"]:
            repo = compose_images(gh_file(f"{run_dir}/{f}" if run_dir else f))
            if repo:
                break
        # deployed_services[].image = the ref Komodo last deployed (keeps the tag
        # when digest-pinned; ListStacks services[].image is a bare sha256 ID then).
        live = {norm(d["image"]) for d in (full["info"].get("deployed_services") or [])
                if d.get("image")}
        srv = servers.get(st["info"].get("server_id"), {}).get("name", "?")
        label = f"komodo/{srv}/{st['name']}"
        if st["info"].get("state") != "running":
            print(f"  WARN  {label}: state={st['info'].get('state')}")
            problems.append(f"{label}: state {st['info'].get('state')}")
        compare(label, repo, live)


# --------------------------------------------------------------- Portainer
def portainer():
    url, k = pt.load_creds()
    p = pt.Portainer(url, k)
    print("Portainer")
    for s in p.stacks():
        ep = s.get("EndpointId")
        gc = s.get("GitConfig") or {}
        if ep not in LIVE_PORTAINER_EPS or not gc:
            continue
        auto = (s.get("AutoUpdate") or {}).get("Interval")
        repo = compose_images(gh_file(gc["ConfigFilePath"]))
        flt = json.dumps({"label": [f"com.docker.compose.project={s['Name']}"]})
        cs = p._req("GET", f"endpoints/{ep}/docker/containers/json?all=1&filters="
                    + urllib.parse.quote(flt))
        live = {norm(c["Image"]) for c in cs if c.get("State") == "running"}
        label = (f"portainer/{LIVE_PORTAINER_EPS[ep]}/{s['Name']}"
                 + ("" if auto else " (no auto-update: redeploy by hand)"))
        compare(label, repo, live)


if __name__ == "__main__":
    try:
        komodo()
    except Exception as e:  # keep going so Portainer is still checked
        print(f"  ERROR Komodo check failed: {e}")
        problems.append(f"Komodo check failed: {e}")
    portainer()
    print()
    if notes:
        print("NOTES:")
        for x in notes:
            print(f"  - {x}")
    if problems:
        print("ATTENTION:")
        for x in problems:
            print(f"  - {x}")
        sys.exit(1)
    print("home_server: every live stack runs what main says; deployers healthy.")
