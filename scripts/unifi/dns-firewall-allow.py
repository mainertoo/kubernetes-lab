#!/usr/bin/env python3
"""Widen the segmented-zone DNS-allow firewall rules to include the OFF-rack AdGuard
(192.168.1.53) alongside 192.168.1.50, so the IoT / Untrusted / Cameras zones can fail
over to it (instead of only the gateway) when .50 is down.

Companion to dns-resilience.py and the terraform/unifi/firewall_policies.tf
`internal_dns = [.50, .53]` change — this makes that change live now; the TF code keeps
it from drifting back. Touches only the 3 "<zone> DNS to AdGuard" ALLOW policies; it
adds one IP to each destination.ips and re-PUTs the otherwise-identical object.

Dry-run by default; --apply to write. Reuses the ui-toolkit API key.
"""
import argparse
import json
import ssl
import subprocess
import urllib.request as u
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
UI_SECRET = REPO / "apps/base/ui-toolkit/ui-toolkit-secret.sops.yaml"
BASE2 = "/proxy/network/v2/api/site/default"
ADD_IP = "192.168.1.53"
TARGET_SUFFIX = "DNS to AdGuard"


def load_auth():
    out = subprocess.run(["sops", "-d", str(UI_SECRET)],
                         capture_output=True, text=True, check=True).stdout
    sd = (yaml.safe_load(out) or {}).get("stringData", {})
    return sd["UNIFI_CONTROLLER_URL"].rstrip("/"), sd["UNIFI_API_KEY"]


class Client:
    def __init__(self, url, key):
        self.base = url
        self.key = key
        self.ctx = ssl._create_unverified_context()

    def req(self, path, data=None, method="GET"):
        body = json.dumps(data).encode() if data is not None else None
        r = u.Request(self.base + path, data=body, method=method)
        r.add_header("X-API-KEY", self.key)
        r.add_header("Content-Type", "application/json")
        r.add_header("Accept", "application/json")
        return json.loads(u.urlopen(r, context=self.ctx).read())


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = ap.parse_args()

    url, key = load_auth()
    c = Client(url, key)
    pols = c.req(f"{BASE2}/firewall-policies")
    targets = [p for p in pols if (p.get("name") or "").endswith(TARGET_SUFFIX)]

    changed = []
    for p in targets:
        ips = list(p.get("destination", {}).get("ips", []))
        if ADD_IP not in ips:
            changed.append((p, ips, ips + [ADD_IP]))

    if not changed:
        print(f"All {len(targets)} DNS-allow policies already include {ADD_IP} — nothing to do.")
        return

    print(f"{'APPLY' if args.apply else 'DRY-RUN'} — add {ADD_IP} to {len(changed)} policy(ies):")
    for p, old, new in changed:
        print(f"  {p['name']:<26} ips {old} -> {new}")
    if not args.apply:
        print("\nRe-run with --apply to write.")
        return

    for p, old, new in changed:
        p["destination"]["ips"] = new
        c.req(f"{BASE2}/firewall-policies/{p['_id']}", p, "PUT")
        print(f"  ✓ {p['name']}")
    print("\nDone. Segmented zones can now resolve via .53 (off-rack AdGuard) as well as .50.")


if __name__ == "__main__":
    main()
