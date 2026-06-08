# Home network VLAN migration — on-site runbook

Companion to [`network-vlan-design.md`](network-vlan-design.md). **Do these steps on-site**
(or via a verified out-of-band path) — firewall changes are the only real remote-lockout
risk. Each step is reversible; the build phase (Proxmox skill, UniFi verify tooling, these
docs) is already done and changes nothing on the wire.

Pre-flight:
- Snapshot the UniFi config: Settings → System → Backups → download a backup first.
- Have a wired laptop on Management (`.1`) as your escape hatch — never lock yourself out
  via WiFi-only access.
- `scripts/unifi/netinfo.py verify` (run on-LAN) to record the starting state.

## Phase A — Additive VLANs + SSIDs (non-disruptive; nothing moves yet)

Creating networks and *new* SSIDs does not move any existing client. Skip the IoT retag.

1. **Create VLANs** — Settings → Networks → New Virtual Network, once per row in the design
   table (10 Trusted, 20 IoT, 30 Guest, 40 Kids, 50 Cameras, 60 DMZ). For each: VLAN ID =
   third octet, Gateway/Subnet `192.168.<id>.1/24`, DHCP range `.50–.250`, DHCP DNS →
   `192.168.1.50` (AdGuard). Leave Management(1)/K8s(90)/Ceph(99) untouched.
2. **Create new SSIDs** — Settings → WiFi → Add: `mainertoo_zone_guest`→30,
   `mainertoo_zone_kids`→40. Guest SSID → enable Client Device Isolation. **Do not** create
   or retag the IoT SSID yet.
3. **Verify**: `netinfo.py networks` lists the new VLANs; `netinfo.py verify` shows them
   present. New SSIDs broadcast; existing clients unaffected.

## Phase B — Zone-Based Firewall (the careful part)

Build zones and policies before relying on them. Test from a throwaway client per zone.

4. **Zones** — Settings → Firewall → Zone-Based Firewall. Put 1/10/90 in a **Trusted** zone;
   30/40/50/60 each in (or grouped as) an **Untrusted** zone; 20 in an **IoT** zone.
5. **Policies** (per the matrix in the design doc):
   - Trusted ↔ Trusted-tier: allow.
   - Untrusted zones → internet: allow; → all internal (RFC1918): block.
   - Kids → MetalLB pool `192.168.90.180-.199`: allow (media); else block.
   - Every VLAN → AdGuard `192.168.1.50`: allow.
   - Keep existing K8s→`192.168.1.252` (NFS) and K8s→`192.168.99.0/24` (Ceph) allows.
6. **Test each zone** with a throwaway client: in-subnet DHCP lease, AdGuard resolves,
   internet works, **and** Management is unreachable from Guest/Kids/IoT. Roll back the
   offending policy immediately if an escape-hatch path breaks.
7. **Verify**: `netinfo.py firewall` and `netinfo.py verify`.

## Phase C — Retag trusted WiFi (moves your devices; quick)

8. Retag `mainertoo_zone` → VLAN 10. Your phones/laptops re-DHCP onto `192.168.10.x`.
   Confirm SSH to PVE hosts (`.1`) and kubectl still work from a Trusted client (Trusted→Mgmt
   is allowed).

## Phase D — IoT relocation (DEFERRED — separate session)

Do **not** start until you can babysit the smart home. Follow the multicast/mDNS recipe in
the design doc §"Deferred IoT phase": enable IoT Auto Discovery (mDNS) across Trusted+IoT,
disable IGMP-snooping multicast filtering, uncheck Multicast-to-Unicast on the IoT SSID,
add the stateful IoT firewall rules, add ULA IPv6 to Trusted+IoT if using Matter, retarget
the **Home-Assistant macvlan** NAD from `.1` to **VLAN 10 (Trusted, alongside Sonos)** — so
HA↔Sonos stays on one L2 — then retag `mainertoo_zone_IoT`→20 and migrate devices. HA
reaches IoT devices via the Trusted→IoT allow + mDNS reflection (add a narrow IoT→HA allow
only for push integrations). Keep Sonos / Apple TV / HomePod / HA on Trusted. Restart Home
hubs and hard-reboot devices afterward.

## Rollback

Each phase is independent. To undo: delete the new firewall policies (Phase B), re-tag
`mainertoo_zone` back to its original network (Phase C), or restore the pre-flight UniFi
backup wholesale. New empty VLANs/SSIDs left in place are harmless.
