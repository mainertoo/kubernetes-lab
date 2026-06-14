#!/usr/bin/env python3
"""
configure-oidc.py — config-as-code for Chaptarr's Authentik OIDC/SSO.

Chaptarr (a Readarr fork) has NATIVE OIDC, but the OIDC settings are NOT reliably
settable via env on this alpha (the Servarr env binding only documents
`<App>__Auth__Method`; the Oidc* keys are unverified). They DO live in the host
config (config.xml on the /config PVC) and are set via the REST API. This script
merges ONLY the OIDC fields into Chaptarr's live host config, mirroring the
immich `scripts/immich/configure-oauth.py` pattern — the rest of the host config
is read back and preserved.

The Authentik provider (pk 160) + application "chaptarr" were created out-of-band
via `ak shell` against the production Authentik (see README.md); this only
configures the Chaptarr side.

Usage:
  configure-oidc.py            # dry-run: show the OIDC diff, change nothing
  configure-oidc.py --commit   # apply the merged OIDC fields
  configure-oidc.py --show     # print the current live OIDC fields and exit

Env overrides (skip sops): CHAPTARR_URL, CHAPTARR_API_KEY, OIDC_AUTHORITY,
OIDC_CLIENT_ID, OIDC_CLIENT_SECRET.

Note: staging serves a Let's Encrypt STAGING cert (untrusted), so TLS
verification is disabled for the host API call. Traffic is LAN/Tailscale-internal.
"""

import json
import os
import ssl
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
CRED_FILE = HERE / "credentials.sops.yaml"

# Non-secret OIDC knobs. Authority/client come from creds.
OIDC_STATIC = {
    "authenticationMethod": "oidc",      # replaces Plex
    "authenticationRequired": "enabled",
    "oidcScopes": "openid profile email",
    "oidcAllowAnyVerifiedUser": True,    # any Authentik-verified user; tighten via
                                         # oidcAllowedEmailDomains if you want to
                                         # restrict to specific accounts.
}
# staging LE-staging cert is untrusted -> don't verify the host API call
_TLS = ssl.create_default_context()
_TLS.check_hostname = False
_TLS.verify_mode = ssl.CERT_NONE


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
        "CHAPTARR_URL": (pick("CHAPTARR_URL") or "").rstrip("/"),
        "CHAPTARR_API_KEY": pick("CHAPTARR_API_KEY"),
        "OIDC_AUTHORITY": pick("OIDC_AUTHORITY"),
        "OIDC_CLIENT_ID": pick("OIDC_CLIENT_ID"),
        "OIDC_CLIENT_SECRET": pick("OIDC_CLIENT_SECRET"),
    }
    missing = [k for k, v in creds.items() if not v]
    if missing:
        sys.exit(
            "Missing credentials: "
            + ", ".join(missing)
            + f"\nSet via env or fill {CRED_FILE} (then `sops -e -i` it)."
        )
    return creds


def api(creds, method, path, body=None):
    url = creds["CHAPTARR_URL"] + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Api-Key", creds["CHAPTARR_API_KEY"])
    req.add_header("Accept", "application/json")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, context=_TLS) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {path} -> HTTP {e.code}: {e.read().decode()[:500]}")
    except urllib.error.URLError as e:
        sys.exit(f"{method} {path} -> {e}")


def desired_oidc(creds):
    block = dict(OIDC_STATIC)
    block["oidcAuthority"] = creds["OIDC_AUTHORITY"]
    block["oidcClientId"] = creds["OIDC_CLIENT_ID"]
    block["oidcClientSecret"] = creds["OIDC_CLIENT_SECRET"]
    return block


def main():
    args = set(sys.argv[1:])
    creds = load_creds()
    cfg = api(creds, "GET", "/api/v1/config/host")
    desired = desired_oidc(creds)

    if "--show" in args:
        shown = {k: ("***" if k == "oidcClientSecret" and cfg.get(k) else cfg.get(k))
                 for k in desired}
        print(json.dumps(shown, indent=2))
        return

    changed = {k: (cfg.get(k), v) for k, v in desired.items() if cfg.get(k) != v}
    if not changed:
        print("OIDC fields already match desired config. Nothing to do.")
        return

    print("OIDC changes:" + (" (DRY-RUN)" if "--commit" not in args else ""))
    for k, (old, new) in changed.items():
        if k == "oidcClientSecret":
            old, new = ("***" if old else ""), "***REDACTED***"
        print(f"  {k}: {old!r} -> {new!r}")

    if "--commit" not in args:
        print("\nRe-run with --commit to apply.")
        return

    cfg.update(desired)
    api(creds, "PUT", f"/api/v1/config/host/{cfg['id']}", cfg)
    print("\nApplied. Restart Chaptarr (rollout restart) so the OIDC middleware "
          "re-initialises, then test login at the Chaptarr URL.")


if __name__ == "__main__":
    main()
