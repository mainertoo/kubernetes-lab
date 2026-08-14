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

Finally, Matter-over-WiFi works across VLANs only with routable IPv6 (ULA) +
Router Advertisement/SLAAC on BOTH the controller and the device VLANs, plus the
global mDNS reflector and IGMP snooping OFF (the Terry White "UniFi IoT VLAN
Firewall Rules for Apple Home & Matter" recipe, cross-referenced in
docs/network-vlan-design.md). The filipowm provider cannot manage IPv6 on a
unifi_network (its update path errors `not found` in v1.0.0), so we set the ULA
prefixes here, on the MINIMAL set of VLANs that actually need it:
    VLAN 20 (IoT) = Matter devices live here
    VLAN 1        = matter-server's --primary-interface (lan0); needs a routable
                    ULA to reach the VLAN-20 devices.
Without this, matter-server only has link-local IPv6, which does not route
cross-VLAN -> commissioned Matter nodes show available=False / "No Response".

Deliberately NOT VLAN 10 or 90: a ULA RA advertises a default IPv6 route with no
internet behind it. General clients (Windows/Mac/iPhone) then try IPv6, fail, and
show "no internet" / stall — VLAN 10 (Trusted) carries laptops+phones and VLAN 90
carries the k3s nodes, so IPv6 stays OFF there. Matter devices still on VLAN 10
(the sonoff bedside plugs) should be re-onboarded onto VLAN 20.

Lastly, Samsung SmartTVs refuse the WebSocket pairing handshake unless the client
appears to be on the TV's own subnet, so Home Assistant (which reaches the IoT
VLAN from 192.168.90.165) cannot pair with a TV on VLAN 20 — it gets
"auth_missing" with no popup ever shown. A narrow SNAT masquerades HA to the IoT
gateway address for tcp/8002 only. The provider has no SNAT resource (only
unifi_port_forward = DNAT), so Terraform cannot manage it either. See SAMSUNG_SNAT.

This script reconciles all of the above, idempotently. Run it after any
`terraform apply` that (re)creates the VLAN networks. Read-only by default;
pass --apply to write.

Auth: reuses the UniFi API key from apps/base/ui-toolkit/ui-toolkit-secret.sops.yaml
(same as netinfo.py), or env UNIFI_API_KEY + UNIFI_API.
"""
import argparse, json, os, ssl, subprocess, sys, urllib.request as u
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
UI_SECRET = REPO / "apps/base/ui-toolkit/ui-toolkit-secret.sops.yaml"
TARGET_VLANS = {10, 20, 30, 40, 50, 60}
REQUIRED_NET_FIELDS = {"is_nat": True, "gateway_type": "default"}

# Matter fabric ULA prefixes — MINIMAL set ONLY: VLAN 20 (Matter devices) + VLAN 1
# (matter-server's lan0 / --primary-interface). NOT VLAN 10 (Trusted client WiFi) or
# VLAN 90 (k3s nodes): a no-internet ULA default route breaks general clients there.
MATTER_ULA = {1: "fd00:1::1/64", 20: "fd00:20::1/64"}

# Samsung SmartTV SNAT. Samsung refuses the WebSocket *pairing* handshake unless the
# client appears to be on the TV's own subnet — see the samsungtv code owner in
# home-assistant/core#104092: "Samsung SmartTV does not allow WebSocket connections
# across different subnets or VLANs ... It may be possible to bypass this issue by
# using IP masquerading or a proxy." Home Assistant runs hostNetwork and reaches the
# IoT VLAN from eth0 (`ip route get 192.168.20.x` -> src 192.168.90.165), so the TVs
# see a VLAN-90 source and answer ms.channel.timeOut on :8002 and
# ms.channel.unauthorized on :8001 — HA reports "auth_missing" with NO popup shown.
# Masquerading HA to the VLAN-20 gateway address satisfies the check.
#
# Deliberately narrow: HA's single IP -> tcp/8002 only. HA tries WEBSOCKET_PORTS
# (8002, 8001) in order and returns on the first success, so 8002 alone is enough and
# nothing else on the IoT VLAN is touched.
SAMSUNG_SNAT = {
    "description": "HA -> Samsung TV control (Samsung requires same-subnet source)",
    "enabled": True,
    "exclude": False,
    "ip_version": "IPV4",
    "is_predefined": False,
    "logging": False,
    "protocol": "tcp",
    "setting_preference": "auto",
    "type": "SNAT",
    "out_interface": "IOT_NETWORK_ID",       # resolved at runtime from vlan 20
    "ip_address": "192.168.20.1",            # translated source = IoT gateway
    "source_filter": {
        "filter_type": "ADDRESS_AND_PORT", "address": "192.168.90.165",
        "firewall_group_ids": [], "invert_address": False, "invert_port": False,
    },
    "destination_filter": {
        "filter_type": "ADDRESS_AND_PORT", "address": "192.168.20.0/24", "port": "8002",
        "firewall_group_ids": [], "invert_address": False, "invert_port": False,
    },
}


def _ipv6_fields(subnet):
    return {
        "ipv6_interface_type": "static",
        "ipv6_subnet": subnet,
        "ipv6_ra_enabled": True,
        "ipv6_ra_priority": "high",
        "ipv6_client_address_assignment": "slaac",
    }


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


def fix_matter_ipv6(c, apply):
    """Ensure ULA IPv6 + RA/SLAAC on the Matter-fabric VLANs, and verify the global
    mDNS reflector + IGMP-off (required for cross-VLAN Matter discovery)."""
    nets = c.req("/proxy/network/api/s/default/rest/networkconf")
    changed = []
    for n in nets:
        v = n.get("vlan") or (1 if n.get("name") == "Default" else None)
        if v not in MATTER_ULA:
            continue
        want = _ipv6_fields(MATTER_ULA[v])
        bad = {k: val for k, val in want.items() if n.get(k) != val}
        if bad:
            print(f"  VLAN {v:3} {n['name']:14} IPv6 needs {sorted(bad)}")
            if apply:
                n.update(want)
                c.req(f"/proxy/network/api/s/default/rest/networkconf/{n['_id']}", n, "PUT")
            changed.append(v)
    # mDNS reflector + IGMP are global settings; verify (read-only) — these are the
    # discovery half of the recipe and must not regress.
    settings = c.req("/proxy/network/api/s/default/get/setting")
    mdns = next((s for s in settings if s.get("key") == "mdns"), {})
    igmp = next((s for s in settings if s.get("key") == "igmp_snooping"), {})
    if mdns.get("enabled_for") != "all":
        print(f"  WARN: mDNS reflector enabled_for={mdns.get('enabled_for')!r} (want 'all' for Matter discovery)")
    if igmp.get("enabled"):
        print("  WARN: IGMP snooping is ON (Terry White recipe wants it OFF — it drops Matter/Apple discovery)")
    return changed


def fix_samsung_snat(c, apply):
    """Ensure the HA -> Samsung TV SNAT rule exists (see SAMSUNG_SNAT above).

    The filipowm provider has no SNAT resource (only unifi_port_forward = DNAT), so
    Terraform cannot manage this — hence it lives here.

    API traps on /v2/api/site/<site>/nat, all discovered the hard way:
      * filter_type enum is [NONE, FIREWALL_GROUPS, IID_AND_PORT, NETWORK_CONF,
        ADDRESS_AND_PORT] — plain "ADDRESS" is NOT valid.
      * an EMPTY-STRING "port" is rejected as "Invalid Port or Port Range"; omit the
        key entirely when you don't want a port filter. The error names the port even
        when the real problem is the *source* filter, which is thoroughly misleading.
      * create returns HTTP **201**, not 200.
    """
    nat_path = "/proxy/network/v2/api/site/default/nat"
    rules = c.req(nat_path)
    want = json.loads(json.dumps(SAMSUNG_SNAT))
    nets = c.req("/proxy/network/api/s/default/rest/networkconf")
    iot = next((n for n in nets if n.get("vlan") == 20), None)
    if not iot:
        print("  WARN: no VLAN 20 network found; skipping Samsung SNAT")
        return []
    want["out_interface"] = iot["_id"]
    for r in rules:
        if r.get("type") == "SNAT" and r.get("description") == want["description"]:
            drift = {k: (r.get(k), want[k]) for k in
                     ("enabled", "ip_address", "protocol", "out_interface")
                     if r.get(k) != want[k]}
            if drift:
                print(f"  Samsung SNAT exists but drifted: {drift}")
                if apply:
                    r.update({k: want[k] for k in drift})
                    c.req(f"{nat_path}/{r['_id']}", r, "PUT")
                return ["samsung-snat"]
            print("  Samsung SNAT present and correct")
            return []
    print(f"  Samsung SNAT MISSING -> {want['source_filter']['address']} to "
          f"{want['destination_filter']['address']}:{want['destination_filter']['port']}")
    if apply:
        c.req(nat_path, want, "POST")
    return ["samsung-snat"]


def fix_trunks(c, apply):
    devs = {d["mac"]: d for d in c.req("/proxy/network/api/s/default/stat/device")}
    infra = {m for m, d in devs.items() if d.get("type") in ("usw", "udm", "uap")}
    # Authoritative downstream map: (upstream_mac, upstream_port) -> the device that
    # uplinks through it. Built from each device's own uplink record, NOT LLDP (which
    # can be empty/stale) — this is what reliably catches inter-switch DOWNlinks (e.g.
    # a 2.5G switch port feeding a 16-port switch, which is_uplink does NOT flag).
    downlink = {}
    for d in devs.values():
        up = d.get("uplink") or {}
        if up.get("uplink_mac") and up.get("uplink_remote_port"):
            downlink[(up["uplink_mac"], up["uplink_remote_port"])] = d
    fixed_devs = set()
    for d in devs.values():
        if d.get("type") not in ("usw", "udm"):
            continue
        ovmap = {o.get("port_idx"): o for o in d.get("port_overrides", [])}
        to_fix = []
        for pt in d.get("port_table", []):
            pi = pt.get("port_idx")
            o = ovmap.get(pi, {})
            # Does this port carry an AP or another switch/gateway? (uplink map first,
            # then is_uplink + LLDP as fallbacks.)
            dn = downlink.get((d["mac"], pi))
            nbrs = {n.get("chassis_id", "").lower() for n in (pt.get("lldp_table") or [])}
            feeds_infra = (
                (dn is not None and dn.get("type") in ("uap", "usw", "udm"))
                or pt.get("is_uplink")
                or (nbrs & {x.lower() for x in infra})
            )
            if not feeds_infra:
                continue
            # Blocking tagged VLANs? Check the legacy `forward` field AND the
            # authoritative `tagged_vlan_mgmt` (the field that actually governs tagged
            # VLANs in current UniFi — block_all silently drops them even when
            # forward != native), in both the effective port_table and the override.
            if (pt.get("forward") == "native" or pt.get("tagged_vlan_mgmt") == "block_all"
                    or o.get("forward") == "native" or o.get("tagged_vlan_mgmt") == "block_all"):
                to_fix.append(pi)
        if not to_fix:
            continue
        print(f"  {d.get('name')}: ports {sorted(to_fix)} -> trunk all (forward=all, clear block_all)")
        if apply:
            ov = d.get("port_overrides", [])
            have = {o.get("port_idx") for o in ov}
            for o in ov:
                if o.get("port_idx") in to_fix:
                    o["forward"] = "all"
                    o.pop("tagged_vlan_mgmt", None)
                    o.pop("excluded_networkconf_ids", None)
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
    print("[Matter fabric: ULA IPv6 + RA/SLAAC + mDNS/IGMP check]")
    v6 = fix_matter_ipv6(c, args.apply)
    print("[Samsung TV SNAT: HA -> IoT same-subnet masquerade]")
    snat = fix_samsung_snat(c, args.apply)
    print("[trunks: AP + inter-switch uplink ports]")
    devs = fix_trunks(c, args.apply)
    if args.apply and (nets or devs or v6):
        # gateway must reprovision for network field / IPv6 changes; switches for trunks
        macs = set(devs) | ({"d0:21:f9:d9:4c:03"} if (nets or v6) else set())
        for m in macs:
            c.req("/proxy/network/api/s/default/cmd/devmgr", {"cmd": "force-provision", "mac": m}, "POST")
        print(f"\nforce-provisioned {len(macs)} device(s); allow ~90s to settle.")
    if not nets and not devs and not v6 and not snat:
        print("\nAll good — nothing to remediate.")


if __name__ == "__main__":
    main()
