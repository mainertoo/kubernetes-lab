#!/usr/bin/env python3
"""
configure-oauth.py — config-as-code for Immich's Authentik OIDC/SSO.

Immich does NOT read OAuth settings from env vars (unlike mealie/grafana); they
live in the database and are set via the admin UI or `PUT /api/system-config`.
This script merges ONLY the `oauth` block into Immich's live system config so
the rest of the config (and the admin UI's editability) is untouched.

Auth + values: reads scripts/immich/credentials.sops.yaml via `sops -d`, or the
matching env vars. The Authentik provider/app/group were created out-of-band via
`ak shell` (see docs/immich-sso-runbook.md); this only configures the Immich side.

Usage:
  configure-oauth.py            # dry-run: show the oauth diff, change nothing
  configure-oauth.py --commit   # apply the merged oauth block
  configure-oauth.py --show     # print the current live oauth block and exit

Env overrides (skip sops): IMMICH_URL, IMMICH_API_KEY, OAUTH_ISSUER_URL,
OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET.
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

# Static OAuth knobs (non-secret). Issuer/client come from creds.
OAUTH_STATIC = {
    "enabled": True,
    "scope": "openid email profile",
    "buttonText": "Login with Authentik",
    "autoRegister": False,       # existing Immich accounts only (link by email);
                                 # no new account is created on unmatched SSO login
    "autoLaunch": False,         # keep the password form as the default
    "storageLabelClaim": "preferred_username",
    "roleClaim": "immich_role",
    "mobileOverrideEnabled": False,  # rely on app.immich:///oauth-callback scheme
    "mobileRedirectUri": "",
}


def _sops_creds() -> dict:
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


def load_creds() -> dict:
    data = _sops_creds()

    def pick(name):
        return os.environ.get(name) or data.get(name)

    creds = {
        "IMMICH_URL": (pick("IMMICH_URL") or "").rstrip("/"),
        "IMMICH_API_KEY": pick("IMMICH_API_KEY"),
        "OAUTH_ISSUER_URL": pick("OAUTH_ISSUER_URL"),
        "OAUTH_CLIENT_ID": pick("OAUTH_CLIENT_ID"),
        "OAUTH_CLIENT_SECRET": pick("OAUTH_CLIENT_SECRET"),
    }
    missing = [k for k, v in creds.items() if not v]
    if missing:
        sys.exit(
            "Missing credentials: "
            + ", ".join(missing)
            + f"\nSet via env or fill {CRED_FILE} (then `sops -e -i` it)."
        )
    if "PLACEHOLDER" in str(creds["IMMICH_API_KEY"]).upper():
        sys.exit(
            "IMMICH_API_KEY is still the placeholder. Mint an admin API key in "
            "Immich (Account Settings > API Keys) and store it in the creds file."
        )
    return creds


def api(creds, method, path, body=None):
    url = creds["IMMICH_URL"] + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("x-api-key", creds["IMMICH_API_KEY"])
    req.add_header("Accept", "application/json")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {path} -> HTTP {e.code}: {e.read().decode()[:500]}")
    except urllib.error.URLError as e:
        sys.exit(f"{method} {path} -> {e}")


def desired_oauth(creds):
    block = dict(OAUTH_STATIC)
    block["issuerUrl"] = creds["OAUTH_ISSUER_URL"]
    block["clientId"] = creds["OAUTH_CLIENT_ID"]
    block["clientSecret"] = creds["OAUTH_CLIENT_SECRET"]
    return block


def redact(block):
    b = dict(block)
    if b.get("clientSecret"):
        b["clientSecret"] = "***REDACTED***"
    return b


def main():
    args = set(sys.argv[1:])
    creds = load_creds()
    cfg = api(creds, "GET", "/api/system-config")

    if "--show" in args:
        print(json.dumps(redact(cfg.get("oauth", {})), indent=2))
        return

    current = cfg.get("oauth", {})
    desired = desired_oauth(creds)
    merged = dict(current)
    merged.update(desired)

    changed = {k: (current.get(k), v) for k, v in desired.items() if current.get(k) != v}
    if not changed:
        print("oauth block already matches desired config. Nothing to do.")
        return

    print("oauth changes:" + (" (DRY-RUN)" if "--commit" not in args else ""))
    for k, (old, new) in changed.items():
        if k == "clientSecret":
            old, new = ("***" if old else ""), "***REDACTED***"
        print(f"  {k}: {old!r} -> {new!r}")

    if "--commit" not in args:
        print("\nRe-run with --commit to apply.")
        return

    cfg["oauth"] = merged
    api(creds, "PUT", "/api/system-config", cfg)
    print("\nApplied. Verify the 'Login with Authentik' button on the Immich login page.")


if __name__ == "__main__":
    main()
