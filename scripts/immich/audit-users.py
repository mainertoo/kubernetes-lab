#!/usr/bin/env python3
"""
audit-users.py — reconcile Immich accounts with Authentik identities by email.

Immich links an OAuth login to an EXISTING user when the IdP `email` claim
matches that user's email; otherwise (with autoRegister on) it creates a new
user. So "merging" existing accounts == making sure each Authentik user's email
equals the email on their existing Immich account BEFORE they first SSO-login.

This script is READ-ONLY. It prints, for the members of the Authentik
`Immich Users` group:
  - MATCH    : email already matches an existing Immich user  -> will auto-link
  - NEW      : no Immich user with that email                 -> autoRegister creates one
and lists Immich users that have no Authentik counterpart (informational).

Auth: Immich admin API key from scripts/immich/credentials.sops.yaml (or env
IMMICH_URL/IMMICH_API_KEY). Authentik is read via `ak shell` in the worker pod
(no token needed), same as docs/immich-sso-runbook.md.

Usage:
  audit-users.py                       # report against the 'Immich Users' group
  audit-users.py --group "Immich Users"
  audit-users.py --all-authentik       # compare ALL authentik users, not just the group
"""

import json
import os
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
CRED_FILE = HERE / "credentials.sops.yaml"
AUTHENTIK_NS = "authentik"


def sops_creds() -> dict:
    if not CRED_FILE.exists():
        return {}
    try:
        out = subprocess.run(
            ["sops", "-d", str(CRED_FILE)], capture_output=True, text=True, check=True
        ).stdout
    except subprocess.CalledProcessError as e:
        sys.exit(f"sops -d failed: {e.stderr}")
    import yaml

    return (yaml.safe_load(out) or {}).get("stringData", {})


def immich_users() -> list:
    data = sops_creds()
    url = (os.environ.get("IMMICH_URL") or data.get("IMMICH_URL") or "").rstrip("/")
    key = os.environ.get("IMMICH_API_KEY") or data.get("IMMICH_API_KEY")
    if not (url and key):
        sys.exit("Missing IMMICH_URL / IMMICH_API_KEY (env or creds file).")
    req = urllib.request.Request(url + "/api/users", method="GET")
    req.add_header("x-api-key", key)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        sys.exit(f"GET /api/users -> HTTP {e.code}: {e.read().decode()[:300]}")


def authentik_worker_pod() -> str:
    out = subprocess.run(
        ["kubectl", "get", "pods", "-n", AUTHENTIK_NS, "-l",
         "app.kubernetes.io/component=worker", "-o",
         "jsonpath={.items[0].metadata.name}"],
        capture_output=True, text=True,
    ).stdout.strip()
    if not out:
        # fall back to a name match
        out = subprocess.run(
            ["kubectl", "get", "pods", "-n", AUTHENTIK_NS, "-o",
             "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}"],
            capture_output=True, text=True,
        ).stdout
        for line in out.splitlines():
            if "worker" in line:
                return line.strip()
        sys.exit("Could not find the authentik worker pod.")
    return out


def authentik_users(group: str | None, all_users: bool) -> list:
    pod = authentik_worker_pod()
    if all_users:
        pyfilter = "users = User.objects.all()"
    else:
        pyfilter = (
            f"g = Group.objects.filter(name={group!r}).first()\n"
            "users = g.users.all() if g else []\n"
            "if g is None: print('__NOGROUP__')"
        )
    code = (
        "from authentik.core.models import User, Group\n"
        "import json\n"
        f"{pyfilter}\n"
        "print('__JSON__' + json.dumps([{'username':u.username,'email':u.email,'name':u.name} for u in users]))\n"
    )
    out = subprocess.run(
        ["kubectl", "exec", "-n", AUTHENTIK_NS, pod, "--", "ak", "shell", "-c", code],
        capture_output=True, text=True,
    ).stdout
    if "__NOGROUP__" in out:
        sys.exit(f"Authentik group {group!r} not found.")
    for line in out.splitlines():
        if line.startswith("__JSON__"):
            return json.loads(line[len("__JSON__"):])
    sys.exit("Could not parse authentik users from ak shell output.")


def main():
    args = sys.argv[1:]
    group = "Immich Users"
    if "--group" in args:
        group = args[args.index("--group") + 1]
    all_users = "--all-authentik" in args

    iusers = immich_users()
    iemails = {u.get("email", "").lower(): u for u in iusers if u.get("email")}
    ausers = authentik_users(None if all_users else group, all_users)

    scope = "ALL Authentik users" if all_users else f"group '{group}'"
    print(f"Comparing {scope} ({len(ausers)}) against Immich users ({len(iusers)}):\n")
    matched_emails = set()
    for u in sorted(ausers, key=lambda x: x.get("email", "")):
        email = (u.get("email") or "").lower()
        if not email:
            print(f"  WARN  {u['username']:<24} has NO email in Authentik (cannot link)")
            continue
        if email in iemails:
            matched_emails.add(email)
            print(f"  MATCH {email:<32} <-> Immich '{iemails[email].get('name','')}' (auto-link)")
        else:
            print(f"  NEW   {email:<32} (no Immich account -> autoRegister will create one)")

    orphan = [u for e, u in iemails.items() if e not in matched_emails]
    if orphan:
        print(f"\nImmich users with no counterpart in {scope} (informational):")
        for u in orphan:
            print(f"  -     {u.get('email'):<32} '{u.get('name','')}' isAdmin={u.get('isAdmin')}")


if __name__ == "__main__":
    main()
