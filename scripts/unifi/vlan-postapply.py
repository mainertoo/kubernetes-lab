#!/usr/bin/env python3
"""Post-apply remediation for the Terraform-managed UniFi VLANs.

The `filipowm/unifi` Terraform provider creates `corporate` networks WITHOUT two
fields the UDM needs to bring the network up as a normal routed/NAT'd LAN:

    is_nat        -> must be True  (else no NAT to WAN; gateway won't route it)
    gateway_type  -> must be "default" (else the gateway never creates the L3
                     interface / DHCP server -> clients get no lease)

Separately, because this network historically ran flat on the untagged native
VLAN, AP-uplink and inter-switch-uplink switch ports were left `forward=native`
(`tagged_vlan_mgmt=block_all`), which DROPS tagged-VLAN frames. Every infra port
(AP links + switch/gateway uplinks) must be `forward=all` to trunk the VLANs.

This script reconciles both, idempotently. Run it after any `terraform apply`
that (re)creates the VLAN networks. Read-only by default; pass --apply to write.

Auth: reuses the UniFi API key from apps/base/ui-toolkit/ui-toolkit-secret.sops.yaml
(same as netinfo.py), or env UNIFI_API_KEY + UNIFI_API.
"""
import argparse, json, os, ssl, subprocess, sys, urllib.request as u
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
UI_SECRET = REPO / "apps/base/ui-toolkit/ui-toolkit-secret.sops.yaml"
TARGET_VLANS = {10, 20, 30, 40, 50, 60}
REQUIRED_NET_FIELDS = {"is_nat": True, "gateway_type": "default"}


def load_auth():
    url = os.environ.get("UNIFI_API") or os.environ.get("UNIFI_CONTROLLER_URL")
    key = os.environ.get("UNIFI_API_KEY")
    if not key:
        out = subprocess.run(["sops", "-d", str(UI_SECRET)], capture_output=True, text=True, check=True).stdout
        import yaml
        sd = yaml.safe_load(out)["stringData"]
        key = key or sd.get("UNIFI_API_KEY")
        url = url or sd.get("UNIFI_CONTROLLER_URL")
    return (url or "https://192.168.1.1").rstrip("/"), key


class UniFi:
    def __init__(self, url, key):
        self.base, self.key = url, key
        ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        self.op = u.build_opener(u.HTTPSHandler(context=ctx))

    def req(self, path, data=None, method="GET"):
        h = {"X-API-KEY": self.key, "Content-Type": "application/json"}
        r = self.op.open(u.Request(self.base + path, data=(json.dumps(data).encode() if data else None), headers=h, method=method))
        body = json.loads(r.read())
        return body["data"] if isinstance(body, dict) and "data" in body else body


def fix_networks(c, apply):
    nets = c.req("/proxy/network/api/s/default/rest/networkconf")
    changed = []
    for n in nets:
        if n.get("vlan") not in TARGET_VLANS:
            continue
        bad = {k: v for k, v in REQUIRED_NET_FIELDS.items() if n.get(k) != v}
        if bad:
            print(f"  VLAN {n['vlan']:3} {n['name']:14} needs {bad}")
            if apply:
                n.update(REQUIRED_NET_FIELDS)
                c.req(f"/proxy/network/api/s/default/rest/networkconf/{n['_id']}", n, "PUT")
            changed.append(n["vlan"])
    return changed


def fix_trunks(c, apply):
    devs = {d["mac"]: d for d in c.req("/proxy/network/api/s/default/stat/device")}
    infra = {m for m, d in devs.items() if d.get("type") in ("usw", "udm", "uap")}
    fixed_devs = set()
    for d in devs.values():
        if d.get("type") not in ("usw", "udm"):
            continue
        to_fix = []
        for pt in d.get("port_table", []):
            if pt.get("forward") != "native":
                continue
            nbrs = {n.get("chassis_id", "").lower() for n in (pt.get("lldp_table") or [])}
            if pt.get("is_uplink") or (nbrs & {x.lower() for x in infra}):
                to_fix.append(pt.get("port_idx"))
        if not to_fix:
            continue
        print(f"  {d.get('name')}: ports {to_fix} forward=native -> all")
        if apply:
            ov = d.get("port_overrides", [])
            have = {o.get("port_idx") for o in ov}
            for o in ov:
                if o.get("port_idx") in to_fix:
                    o["forward"] = "all"; o.pop("tagged_vlan_mgmt", None)
            for p in to_fix:
                if p not in have:
                    ov.append({"port_idx": p, "forward": "all"})
            c.req(f"/proxy/network/api/s/default/rest/device/{d['_id']}", {"port_overrides": ov}, "PUT")
            fixed_devs.add(d["mac"])
    return fixed_devs


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = ap.parse_args()
    url, key = load_auth()
    if not key:
        sys.exit("no UNIFI_API_KEY (set env or ensure sops can decrypt ui-toolkit secret)")
    c = UniFi(url, key)
    mode = "APPLY" if args.apply else "DRY-RUN (use --apply to write)"
    print(f"== UniFi VLAN post-apply remediation [{mode}] ==\n[networks: is_nat/gateway_type]")
    nets = fix_networks(c, args.apply)
    print("[trunks: AP + inter-switch uplink ports]")
    devs = fix_trunks(c, args.apply)
    if args.apply and (nets or devs):
        # gateway must reprovision for network field changes; switches for trunks
        macs = set(devs) | ({"d0:21:f9:d9:4c:03"} if nets else set())
        for m in macs:
            c.req("/proxy/network/api/s/default/cmd/devmgr", {"cmd": "force-provision", "mac": m}, "POST")
        print(f"\nforce-provisioned {len(macs)} device(s); allow ~90s to settle.")
    if not nets and not devs:
        print("\nAll good — nothing to remediate.")


if __name__ == "__main__":
    main()
