#!/usr/bin/env python3
"""Make DHCP DNS resilient + clean up a stray IPv6 RA on the UniFi UDM.

Background (2026-06-24 UPS maintenance): the LAN had a DNS single-point-of-failure.
Most VLANs handed out only AdGuard 192.168.1.50 (an LXC on pve-mac); the cluster
nodes rode 192.168.1.1 (the UDM). When the responsible host was down, the VLAN lost
name resolution -> no internet. This sets a 3-server resolver list everywhere so one
resolver dying never kills DNS:

    1: 192.168.1.50  AdGuard (pve-mac)     - ad-blocking primary
    2: 192.168.1.53  AdGuard (pve-ugreen)  - ad-blocking, OUTSIDE the rack
    3: 192.168.1.1   UDM-Pro SE            - gateway-native, always up

Also flips ipv6_ra_enabled off on VLAN 99 (Shared-Storage / Ceph) -- a leftover RA
with no IPv6 subnet, surplus to the MATTER_ULA={1,20} scoping in vlan-postapply.py.

ARCHITECTURE NOTE: VLANs 10/20/30/40/50/60 are Terraform-managed
(terraform/unifi/vlans.tf, local.dns_servers). This script writes them live for
immediate effect; keep the TF code in sync (it now points at the same 3 servers) so a
future `terraform apply` is a no-op for DNS. VLANs 1/90 (cluster) and 99 are NOT
TF-managed (deliberately, to keep TF away from critical infra) so they live only here.

Read-only by default; pass --apply to write. Auth reuses the ui-toolkit API key
(same as netinfo.py / vlan-postapply.py).
"""
import argparse
import json
import ssl
import subprocess
import sys
import urllib.request as u
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
UI_SECRET = REPO / "apps/base/ui-toolkit/ui-toolkit-secret.sops.yaml"

DNS = ["192.168.1.50", "192.168.1.53", "192.168.1.1"]
DNS_VLANS = {1, 10, 20, 30, 40, 50, 60, 90}   # cluster (1, 90) + IoT-family (10-60)
RA_OFF_VLANS = {99}                            # leftover RA on the Ceph VLAN


def load_auth():
    out = subprocess.run(["sops", "-d", str(UI_SECRET)],
                         capture_output=True, text=True, check=True).stdout
    sd = (yaml.safe_load(out) or {}).get("stringData", {})
    return sd["UNIFI_CONTROLLER_URL"].rstrip("/"), sd["UNIFI_API_KEY"]


class Client:
    def __init__(self, url, key):
        self.base = url
        self.key = key
        self.ctx = ssl._create_unverified_context()  # UDM self-signed cert

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
    nets = c.req("/proxy/network/api/s/default/rest/networkconf")["data"]

    pending = {}   # _id -> (name, vlan, modified_obj)
    lines = []
    for n in nets:
        if n.get("purpose") == "wan":
            continue
        vid, name, nid = n.get("vlan"), n.get("name"), n["_id"]
        changed = False
        # The untagged native LAN ("Default") carries no `vlan` field but IS the
        # cluster's VLAN 1 (where the workers' lan0 lives + gets its resolver).
        in_dns_scope = (vid in DNS_VLANS) or (name == "Default" and n.get("purpose") == "corporate")

        if in_dns_scope:
            cur = [n.get(f"dhcpd_dns_{i}") for i in (1, 2, 3, 4)]
            cur = [x for x in cur if x]
            if not n.get("dhcpd_dns_enabled") or cur != DNS:
                n["dhcpd_dns_enabled"] = True
                for i in (1, 2, 3, 4):
                    n.pop(f"dhcpd_dns_{i}", None)
                for i, ip in enumerate(DNS, 1):
                    n[f"dhcpd_dns_{i}"] = ip
                lines.append(f"  VLAN {str(vid):>3}  {str(name):<16}  DNS {cur or 'auto'} -> {DNS}")
                changed = True

        if vid in RA_OFF_VLANS and n.get("ipv6_ra_enabled"):
            n["ipv6_ra_enabled"] = False
            lines.append(f"  VLAN {str(vid):>3}  {str(name):<16}  ipv6_ra_enabled True -> False")
            changed = True

        if changed:
            pending[nid] = (name, vid, n)

    if not pending:
        print("Nothing to change — already converged.")
        return

    print(f"{'APPLY' if args.apply else 'DRY-RUN'} — {len(pending)} network(s), {len(lines)} change(s):")
    print("\n".join(lines))
    if not args.apply:
        print("\nRe-run with --apply to write. DHCP changes take effect on client lease renewal.")
        return

    for nid, (name, vid, obj) in pending.items():
        c.req(f"/proxy/network/api/s/default/rest/networkconf/{nid}", obj, "PUT")
        print(f"  ✓ wrote VLAN {vid} {name}")
    print(f"\nApplied {len(pending)} update(s). DHCP DNS takes effect on lease renewal "
          "(reboot a client or `resolvectl flush-caches` to force).")


if __name__ == "__main__":
    main()
