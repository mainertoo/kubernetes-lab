# Home network VLAN segmentation — design

Status: **planned** (build phase = tooling + docs done; data-plane changes pending on-site).
Owner: mainertoo · Last updated 2026-06-07.

## Why

The LAN is flat: critical infra (6 PVE hosts, QNAP, PBS, kopia, z-wave), the main LAN,
main WiFi, and ~80 IoT devices all share `192.168.1.0/24`. Only Kubernetes
(`192.168.90.0/24`) and Ceph (`192.168.99.0/24`) are segmented. Goal: a robust,
zone-segmented network — trusted humans, guests, kids, IoT, cameras, DMZ each isolated —
**without re-IPing critical infra** (keeps every SSH alias, Traefik backend, NFS PV, and
Ceph mon literal working). The IoT relocation (the disruptive part) is deferred to its own
phase; this doc captures the whole target so that phase is pre-scoped.

## VLAN scheme (third octet = VLAN ID)

| VLAN | Subnet | Name | Purpose | Status |
|------|--------|------|---------|--------|
| 1  | 192.168.1.0/24  | **Management / Cluster-Infra** | PVE hosts, QNAP, PBS, kopia, z-wave, core gear, admin box | KEEP as-is |
| 10 | 192.168.10.0/24 | **Trusted** | Personal devices + `mainertoo_zone` WiFi; also Sonos, Apple TV/HomePod, HA hub-side | NEW |
| 20 | 192.168.20.0/24 | **IoT** | Smart-home devices + `mainertoo_zone_IoT` WiFi (2.4␣GHz) | NEW (devices migrate later) |
| 30 | 192.168.30.0/24 | **Guest** | `mainertoo_zone_guest` — internet-only, isolated | NEW |
| 40 | 192.168.40.0/24 | **Kids** | `mainertoo_zone_kids` — filtered internet | NEW |
| 50 | 192.168.50.0/24 | **Cameras** | UniFi Protect (G4 doorbell, UP Chime) — no internet | NEW |
| 60 | 192.168.60.0/24 | **Servers / DMZ** | Externally-exposed / semi-trusted workloads | NEW |
| 90 | 192.168.90.0/24 | **Kubernetes** | K3s nodes, kube-vip `.160`, MetalLB `.180-.199` | KEEP |
| 99 | 192.168.99.0/24 | **Ceph** | mon/osd mesh on the USB/TB NIC | KEEP |

Per-VLAN DHCP: gateway `.1`, pool `.50–.250`, DNS → **AdGuard `192.168.1.50`** (local
resolution + filtering; Kids → a stricter AdGuard client profile keyed on `192.168.40.0/24`).

## Firewall posture — Tiered (UniFi Zone-Based Firewall)

Two tiers. **Trusted tier** = Trusted(10) + Management(1) + K8s(90): mutually open (low
rule count). **Untrusted tier** = Guest(30), IoT(20), Kids(40), Cameras(50), DMZ(60):
hard default-deny to all internal networks, then a short explicit allow-list. Return
traffic of any allowed session is automatic ("established/related"/"Return Traffic"), so
allows are one-directional.

| From ↓ / To → | Mgmt(1) | Trusted(10) | K8s LB(.90.18x) | IoT(20) | Internet |
|---|---|---|---|---|---|
| **Trusted(10)** | allow | allow | allow | allow (initiate) | allow |
| **Management(1)** | allow | allow | allow | – | allow |
| **K8s(90)** | NFS .1.252 / Ceph .99 | allow | allow | – | allow |
| **IoT(20)** | block | block (replies only) | – | – | allow |
| **Kids(40)** | block | block | allow (media) | block | allow (filtered) |
| **Guest(30)** | block | block | block | block | allow (isolated) |
| **Cameras(50)** | Protect ctrl only | view from Trusted | – | – | **block** |
| **DMZ(60)** | block | replies only | – | – | allow |

All client VLANs (10/20/40) → allow to AdGuard DNS `192.168.1.50`. Ceph(99) reachable only
from K8s(90) + PVE hosts.

**Guest AirPlay/casting (NOT recommended).** Letting Guest(30) AirPlay to Trusted Apple
devices needs mDNS reflection Guest→Trusted *plus* allows to AirPlay ports — it punches the
untrusted guest zone into Trusted and widens attack surface on devices with a history of
wormable zero-click AirPlay RCE (AirBorne, 2025). Leave guests internet-only; if casting is
wanted, dedicate an AirPlay receiver in the Guest/media zone rather than exposing Trusted
Apple gear.

## WiFi (keep the `mainertoo_zone_<zone>` convention)

`mainertoo_zone` → VLAN 10 · `mainertoo_zone_guest` → VLAN 30 · `mainertoo_zone_kids` →
VLAN 40 · `mainertoo_zone_IoT` → VLAN 20 (2.4␣GHz, retag deferred to the IoT phase).
Guest SSID: enable client isolation. All APs are UniFi → SSID-to-VLAN tagging handles
trunking; inter-switch uplinks already carry all networks.

## Coupling notes / blast radius

`.1`, `.90`, `.99` keep their IPs → **no change** to: SSH aliases (`~/.ssh/config`),
`terraform/**`, `ansible/**` group_vars (kube-vip, MetalLB), Traefik
`file-provider-pve-config.yaml`, NFS PVs (`192.168.1.252`), Ceph mon literals, and the
Home-Assistant macvlan NAD (`192.168.1.240-.250`). New VLANs are purely additive. Only
new firewall **allow** rules are needed so VLANs 10/20/40 reach AdGuard `.1.50` and the
MetalLB app pool `.90.18x`.

## Deferred IoT phase — the multicast/mDNS recipe (its own session)

Moving ~80 IoT devices to VLAN 20 breaks smart-home discovery unless multicast crosses the
Trusted↔IoT boundary correctly. Recipe distilled from Terry White (*UniFi IoT VLAN
Firewall Rules for Apple Home & Matter*) and Ethernet Blueprint (*Full Control of Sonos and
Home Assistant with UniFi FW Rules*) — both directly applicable here (we run Sonos + Home
Assistant; possibly HomeKit/Matter):

1. **mDNS reflection** — Settings → Networks → Multicast: enable **IoT Auto Discovery
   (mDNS)** spanning **both** Trusted and IoT. (UniFi's built-in reflector; no separate
   Avahi needed.)
2. **Multicast Filtering (IGMP Snooping)** — select **NO** networks (leave off) so Sonos /
   mDNS / SSDP multicast flows.
3. **WiFi → IoT SSID → Advanced → Hi-Capacity Tuning → "Multicast to Unicast": unchecked.**
4. **Firewall** (matches the tiered model): top rule **Allow return/established** any→any;
   then **Block IoT → Trusted**; optional **Allow IoT → Apple Home hub IP** (specific host)
   above the block for status pushes. Trusted→IoT initiate stays allowed.
5. **Matter-over-WiFi requires IPv6 link-local routing.** Add a ULA IPv6 network to **both**
   Trusted and IoT (Static, e.g. Trusted `fd00:1::1/64`, IoT `fd00:20::1/64`; Client
   assignment SLAAC; Router Advertisement on) so the Home hub can route to Matter devices.
   Skip only if no Matter-over-WiFi devices are in play.
6. **Hub/device placement & HA↔Sonos**: keep on **Trusted (10)** — Apple TV / HomePod,
   **Home Assistant**, and **Sonos**. **Home-Assistant's macvlan NAD retargets from `.1` to
   VLAN 10 (Trusted), NOT IoT** — co-locating HA + Sonos on one L2 means HA↔Sonos control
   *and* Sonos's UPnP/SSDP event callbacks work natively, with **no cross-VLAN reflection or
   firewall holes for Sonos**. (Putting HA on IoT instead would strand it behind the blocked
   IoT→Trusted direction from Sonos — avoid.) Put on **IoT (20)** — Hue / Lutron / Aqara /
   SwitchBot bridges and the bulk of sensors/plugs; HA reaches them via the **Trusted→IoT
   allow** (already in the tiered model) + mDNS reflection, with a narrow **IoT→HA** allow
   only for integrations that push to HA (e.g. ESPHome native API / webhook callbacks).
   Caveat: cross-VLAN SSDP/UPnP isn't reflected like mDNS — for any IoT device HA discovers
   via SSDP, pin its IP in the integration.
7. After applying: **restart Apple Home hubs**; hard-reboot (10␣s unplug) smart devices.

### Local-control integrations (LocalTuya / Meross LAN)

Cloud-free devices controlled directly by Home Assistant work **as-is** under the tiered
model, because they use a *pull* model — HA (Trusted) opens the connection down to the
device (IoT) and the device answers on that established session, which the **Trusted→IoT
allow** + automatic return-traffic already permit:

- **LocalTuya** — HA initiates a connection to the device IP (Tuya local protocol, TCP
  6668) using the device's local key; push updates ride the *same* HA-initiated connection.
  **No inbound rule needed.**
- **Meross LAN (HTTP/polling mode)** — HA polls the device over HTTP (port 80); HA
  initiates. **No inbound rule needed.**
- **Meross LAN (MQTT push mode)** — only if a local MQTT broker (e.g. Mosquitto on HA) is
  used for instant state, the *device* connects inbound to the broker (IoT→Trusted), which
  is blocked by default. Either add **one narrow allow: IoT → HA MQTT (TCP 1883)** (this is
  the same "narrow IoT→HA allow for push integrations" noted above) **or** stay on
  polling mode (zero extra rules, state updates a few seconds slower).

**DHCP reservations required:** LocalTuya/Meross address devices by **IP + local key**, so
give every locally-controlled device a **DHCP reservation (static lease) on VLAN 20** so the
integration's pinned IP stays valid across reboots and the VLAN move.

### IoT migration mechanics — reset vs. SSID-move

Moving WiFi IoT devices to VLAN 20 does **not** require factory-resetting them if the SSID
is *moved* rather than *replaced*:

- **Preferred — move the SSID, not the devices:** stand up the VLAN-20 IoT SSID with the
  **same name *and* password** the devices already use. Devices roam to it transparently,
  pull a new VLAN-20 lease, and keep working — **no factory reset, no re-pairing, no new
  local keys**. Only follow-up: each device gets a new IP (covered by the DHCP reservations
  above) and a one-time reboot to grab the lease cleanly. Clean only when IoT devices are
  already on a *dedicated* SSID (not mixed with phones/laptops); if mixed, move humans onto
  the Trusted SSID first, then rebind the old SSID to VLAN 20.
- **Fallback — new differently-named SSID:** cheap WiFi devices store one SSID with no UI to
  change it, so they must be **factory-reset and re-onboarded** via the manufacturer app,
  and LocalTuya/Meross re-pointed (a reset can rotate the device's local key). Budget this
  only for stragglers that refuse to roam in the preferred path.

## Verification

`scripts/unifi/netinfo.py verify` diffs live VLANs against this scheme (run on-LAN /
Tailscale; `TARGET_VLANS` in the script is the source of truth). `scripts/proxmox/pvinfo.py
network` confirms the PVE-side bridge VLAN trunks reach the K3s VMs. Per-VLAN smoke test: a
throwaway client gets an in-subnet DHCP lease, resolves via AdGuard, reaches the internet,
and is correctly blocked from Management.

Sources: Terry White — https://terrywhite.com/unifi-iot-vlan-firewall-rules-for-apple-matter-users/ ·
Ubiquiti Zone-Based Firewalls — https://help.ui.com/hc/en-us/articles/115003173168-Zone-Based-Firewalls-in-UniFi
