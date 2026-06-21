#!/usr/bin/env python3
"""portainer.py — stand up / manage Docker stacks on the homelab Portainer-EE.

Talks to the central Portainer-EE REST API (default https://192.168.1.252:9443)
with a personal API token (`X-API-Key`). Lets you deploy stacks to environments
*outside* the K3s cluster — the QNAP (local), the VPS / DGX Spark / zwave-js
(edge agents) — the same way the docker-spark vllm/mxbai stacks were created:
as git-repository stacks pulled from mainertoo/home_server, or from a local
compose file.

Auth reuses the local-tooling pattern (env vars -> `sops -d` fallback), mirroring
scripts/proxmox/pvinfo.py and scripts/unifi/netinfo.py:
    PORTAINER_URL       (default https://192.168.1.252:9443)
    PORTAINER_API_KEY   (else read from scripts/portainer/credentials.sops.yaml)
The token is a personal access token (My account -> Access tokens). Never print it.

Writes (create / redeploy / rm) are DRY-RUN unless --yes is passed, honoring the
repo rule "never apply directly / ask before acting".

Examples:
    portainer.py endpoints
    portainer.py stacks
    portainer.py git-creds
    portainer.py create-git --name myapp --endpoint 16 \
        --compose-path docker-spark/myapp/docker-compose.yml \
        --env SOME_TOKEN --yes
    portainer.py create-compose --name myapp --endpoint 2 --file ./docker-compose.yml --yes
    portainer.py redeploy --id 100 --endpoint 16 --yes
    portainer.py rm --id 100 --endpoint 16 --yes
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
CREDS = HERE / "credentials.sops.yaml"
DEFAULT_URL = "https://192.168.1.252:9443"
# Default git remote for repository stacks (this homelab's private Docker repo).
DEFAULT_REPO = "https://github.com/mainertoo/home_server.git"


def load_creds() -> tuple[str, str]:
    """Return (base_url, api_key) from env, falling back to the SOPS secret."""
    url = os.environ.get("PORTAINER_URL")
    key = os.environ.get("PORTAINER_API_KEY")
    if not key and CREDS.exists():
        try:
            out = subprocess.run(["sops", "-d", str(CREDS)],
                                 capture_output=True, text=True, check=True).stdout
        except FileNotFoundError:
            sys.exit("`sops` not on PATH — install it or set PORTAINER_API_KEY.")
        except subprocess.CalledProcessError as e:
            sys.exit(f"sops -d failed: {e.stderr.strip()}")
        sd = (yaml.safe_load(out) or {}).get("stringData", {})
        url = url or sd.get("PORTAINER_URL")
        key = key or sd.get("PORTAINER_API_KEY")
    if key == "REPLACE_WITH_ptr_TOKEN":
        sys.exit("scripts/portainer/credentials.sops.yaml still holds the placeholder "
                 "token — see scripts/portainer/README.md to fill it.")
    if not key:
        sys.exit("no PORTAINER_API_KEY (set env or fill scripts/portainer/"
                 "credentials.sops.yaml — see scripts/portainer/README.md).")
    return (url or DEFAULT_URL).rstrip("/"), key


class Portainer:
    def __init__(self, base_url: str, api_key: str):
        self.base = base_url
        self.key = api_key
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE  # internal IP, self-signed cert

    def _req(self, method: str, path: str, body: dict | None = None):
        url = f"{self.base}/api/{path.lstrip('/')}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"X-API-Key": self.key,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=120) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:500]
            sys.exit(f"Portainer API {e.code} on {method} {path}: {detail}")
        except urllib.error.URLError as e:
            sys.exit(f"cannot reach Portainer at {self.base}: {e.reason}")

    def me(self):
        return self._req("GET", "users/me")

    def endpoints(self):
        return self._req("GET", "endpoints")

    def stacks(self):
        return self._req("GET", "stacks")

    def git_creds(self):
        uid = self.me().get("Id")
        return self._req("GET", f"users/{uid}/gitcredentials")

    def create_git(self, payload: dict, endpoint_id: int):
        return self._req("POST",
                         f"stacks/create/standalone/repository?endpointId={endpoint_id}",
                         payload)

    def create_string(self, payload: dict, endpoint_id: int):
        return self._req("POST",
                         f"stacks/create/standalone/string?endpointId={endpoint_id}",
                         payload)

    def redeploy(self, stack_id: int, endpoint_id: int, payload: dict):
        return self._req("PUT",
                         f"stacks/{stack_id}/git/redeploy?endpointId={endpoint_id}",
                         payload)

    def delete(self, stack_id: int, endpoint_id: int):
        return self._req("DELETE", f"stacks/{stack_id}?endpointId={endpoint_id}")


_EP_TYPE = {1: "docker", 2: "agent", 3: "azure", 4: "edge", 5: "k8s", 6: "k8s-edge", 7: "k8s-edge"}


def _env_pairs(items: list[str]) -> list[dict]:
    """Parse --env KEY=VALUE, or --env KEY to pull VALUE from the environment
    (so secrets don't have to sit on the command line)."""
    out = []
    for it in items or []:
        if "=" in it:
            k, v = it.split("=", 1)
        else:
            k = it
            v = os.environ.get(it)
            if v is None:
                sys.exit(f"--env {it}: no '=' and ${it} is not set in the environment")
        out.append({"name": k, "value": v})
    return out


def cmd_endpoints(p: Portainer, _):
    for e in p.endpoints():
        t = _EP_TYPE.get(e.get("Type"), e.get("Type"))
        edge = (e.get("EdgeID") or "")[:8]
        print(f"{e['Id']:>4}  {e['Name']:<22} {t:<7} {('edge:' + edge) if edge else ''}")


def cmd_stacks(p: Portainer, _):
    for s in p.stacks():
        gc = s.get("GitConfig") or {}
        au = (s.get("AutoUpdate") or {}).get("Interval", "")
        src = f"{gc.get('ConfigFilePath')} @ {gc.get('ReferenceName')}" if gc else "(compose)"
        print(f"{s['Id']:>4}  {s.get('Name'):<22} ep={s.get('EndpointId')}  "
              f"auto={au or '-':<4}  {src}")


def cmd_git_creds(p: Portainer, _):
    creds = p.git_creds()
    if isinstance(creds, dict):
        creds = creds.get("gitCredentials", [])
    for c in creds:
        print(f"{c.get('id'):>4}  {c.get('name'):<24} user={c.get('username')}")


def cmd_create_git(p: Portainer, a):
    payload = {
        "name": a.name,
        "repositoryURL": a.repo,
        "repositoryReferenceName": a.ref,
        "composeFile": a.compose_path,
        "repositoryAuthentication": a.git_cred_id is not None,
        "env": _env_pairs(a.env),
        "autoUpdate": None if a.no_autoupdate else
                      {"interval": a.autoupdate, "forcePullImage": a.force_pull,
                       "forceUpdate": False},
    }
    if a.git_cred_id is not None:
        payload["repositoryGitCredentialID"] = a.git_cred_id
    _maybe_write(a, "create git stack",
                 f"{a.name!r} on endpoint {a.endpoint} from {a.repo} "
                 f"[{a.compose_path} @ {a.ref}], autoUpdate="
                 f"{'off' if a.no_autoupdate else a.autoupdate}, "
                 f"env={[e['name'] for e in payload['env']]}",
                 lambda: p.create_git(payload, a.endpoint))


def cmd_create_compose(p: Portainer, a):
    content = Path(a.file).read_text()
    payload = {"name": a.name, "stackFileContent": content, "env": _env_pairs(a.env)}
    _maybe_write(a, "create compose stack",
                 f"{a.name!r} on endpoint {a.endpoint} from {a.file} "
                 f"({len(content)} bytes), env={[e['name'] for e in payload['env']]}",
                 lambda: p.create_string(payload, a.endpoint))


def cmd_redeploy(p: Portainer, a):
    payload = {"repositoryAuthentication": a.git_cred_id is not None,
               "pullImage": a.pull, "prune": False}
    if a.ref:
        payload["repositoryReferenceName"] = a.ref
    if a.git_cred_id is not None:
        payload["repositoryGitCredentialID"] = a.git_cred_id
    _maybe_write(a, "git-redeploy stack",
                 f"stack {a.id} on endpoint {a.endpoint}"
                 f"{(' -> ' + a.ref) if a.ref else ''}, pullImage={a.pull}",
                 lambda: p.redeploy(a.id, a.endpoint, payload))


def cmd_rm(p: Portainer, a):
    _maybe_write(a, "DELETE stack",
                 f"stack {a.id} on endpoint {a.endpoint} (containers + networks removed)",
                 lambda: p.delete(a.id, a.endpoint))


def _maybe_write(a, verb: str, desc: str, action):
    if not getattr(a, "yes", False):
        print(f"DRY-RUN: would {verb}: {desc}")
        print("  re-run with --yes to apply.")
        return
    res = action()
    if isinstance(res, dict) and res.get("Id"):
        gc = res.get("GitConfig") or {}
        print(f"OK: {verb} -> stack id={res['Id']} name={res.get('Name')} "
              f"ref={gc.get('ReferenceName', '')}")
    else:
        print(f"OK: {verb} ({desc})")


def main():
    ap = argparse.ArgumentParser(description="Manage Portainer stacks outside the K3s cluster.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("endpoints", help="list environments (find the endpoint id)")
    sub.add_parser("stacks", help="list stacks")
    sub.add_parser("git-creds", help="list your stored git credentials (find --git-cred-id)")

    g = sub.add_parser("create-git", help="deploy a git-repository stack")
    g.add_argument("--name", required=True)
    g.add_argument("--endpoint", type=int, required=True, help="endpoint id (see `endpoints`)")
    g.add_argument("--repo", default=DEFAULT_REPO)
    g.add_argument("--ref", default="refs/heads/main")
    g.add_argument("--compose-path", required=True, help="path to the compose file in the repo")
    g.add_argument("--git-cred-id", type=int, help="git credential id (see `git-creds`); omit for public repo")
    g.add_argument("--env", action="append", help="KEY=VALUE, or KEY to read $KEY (repeatable)")
    g.add_argument("--autoupdate", default="5m", help="git poll interval (default 5m)")
    g.add_argument("--no-autoupdate", action="store_true")
    g.add_argument("--force-pull", action="store_true", help="re-pull image each poll (avoid on moving tags)")
    g.add_argument("--yes", action="store_true")

    c = sub.add_parser("create-compose", help="deploy a stack from a local compose file")
    c.add_argument("--name", required=True)
    c.add_argument("--endpoint", type=int, required=True)
    c.add_argument("--file", required=True)
    c.add_argument("--env", action="append", help="KEY=VALUE, or KEY to read $KEY (repeatable)")
    c.add_argument("--yes", action="store_true")

    r = sub.add_parser("redeploy", help="git-redeploy a stack (pull latest)")
    r.add_argument("--id", type=int, required=True)
    r.add_argument("--endpoint", type=int, required=True)
    r.add_argument("--ref", help="re-pin reference, e.g. refs/heads/main")
    r.add_argument("--git-cred-id", type=int)
    r.add_argument("--pull", action="store_true", help="also re-pull images")
    r.add_argument("--yes", action="store_true")

    d = sub.add_parser("rm", help="delete a stack")
    d.add_argument("--id", type=int, required=True)
    d.add_argument("--endpoint", type=int, required=True)
    d.add_argument("--yes", action="store_true")

    args = ap.parse_args()
    base, key = load_creds()
    p = Portainer(base, key)
    {
        "endpoints": cmd_endpoints, "stacks": cmd_stacks, "git-creds": cmd_git_creds,
        "create-git": cmd_create_git, "create-compose": cmd_create_compose,
        "redeploy": cmd_redeploy, "rm": cmd_rm,
    }[args.cmd](p, args)


if __name__ == "__main__":
    main()
