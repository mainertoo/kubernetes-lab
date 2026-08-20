# Phase B STAGE 2 — zone policies.
#
# UniFi auto-created predefined per-zone-pair defaults when ZBF activated:
#   Internal -> Internal/External/Gateway/Vpn/Dmz = ALLOW  (untouched)
#   Internal -> {IoT,Cameras,Untrusted}           = BLOCK  (we override below)
#   {IoT,Cameras,Untrusted} -> *                  = BLOCK  (we add allows below)
# UniFi ZBF is NOT purely stateful for cross-zone return traffic — an ALLOW only
# permits the forward direction. Without auto_allow_return_traffic, the device's
# REPLY (e.g. IoT->Internal) hits the "Block All" and the connection times out.
# So Internal->{IoT,Untrusted,Cameras} MUST set auto_allow_return_traffic so Home
# Assistant (and any Internal host) can actually talk to segmented devices. This
# only allows ESTABLISHED return traffic — it does NOT let a segmented zone
# initiate new connections into Internal.

# internal_dns (["192.168.1.50", "192.168.1.53"]) is defined in vlans.tf and reused
# here — both AdGuard resolvers live in the Internal zone (.50 on pve-mac, .53 on
# pve-ugreen/off-rack). The .1 gateway resolver is a Gateway-zone host, reached via
# the predefined Gateway UDP allow, so it's not listed here.

# --- Internal (Mgmt/Trusted/K8s/Ceph) may initiate into the segmented zones ---
# (needed for Home Assistant -> IoT control, admin access, camera viewing).
resource "unifi_firewall_zone_policy" "internal_to_iot" {
  name                      = "Internal to IoT"
  action                    = "ALLOW"
  auto_allow_return_traffic = true
  source                    = { zone_id = data.unifi_firewall_zone.internal.id }
  destination               = { zone_id = unifi_firewall_zone.iot.id }
}

resource "unifi_firewall_zone_policy" "internal_to_untrusted" {
  name                      = "Internal to Untrusted"
  action                    = "ALLOW"
  auto_allow_return_traffic = true
  source                    = { zone_id = data.unifi_firewall_zone.internal.id }
  destination               = { zone_id = unifi_firewall_zone.untrusted.id }
}

resource "unifi_firewall_zone_policy" "internal_to_cameras" {
  name                      = "Internal to Cameras"
  action                    = "ALLOW"
  auto_allow_return_traffic = true
  source                    = { zone_id = data.unifi_firewall_zone.internal.id }
  destination               = { zone_id = unifi_firewall_zone.cameras.id }
}

# --- Internet (to External). Cameras intentionally OMITTED -> no internet. ---
resource "unifi_firewall_zone_policy" "iot_to_internet" {
  name        = "IoT to Internet"
  action      = "ALLOW"
  source      = { zone_id = unifi_firewall_zone.iot.id }
  destination = { zone_id = data.unifi_firewall_zone.external.id }
}

resource "unifi_firewall_zone_policy" "untrusted_to_internet" {
  name        = "Untrusted to Internet"
  action      = "ALLOW"
  source      = { zone_id = unifi_firewall_zone.untrusted.id }
  destination = { zone_id = data.unifi_firewall_zone.external.id }
}

# --- DNS to AdGuard (.1.50) so the segmented zones can resolve (DHCP hands out
#     .1.50 as resolver). Narrow allow to that host:53 only. ---
resource "unifi_firewall_zone_policy" "iot_dns" {
  name        = "IoT DNS to AdGuard"
  action      = "ALLOW"
  protocol    = "tcp_udp"
  source      = { zone_id = unifi_firewall_zone.iot.id }
  destination = { zone_id = data.unifi_firewall_zone.internal.id, ips = local.internal_dns, port = "53" }
}

resource "unifi_firewall_zone_policy" "untrusted_dns" {
  name        = "Untrusted DNS to AdGuard"
  action      = "ALLOW"
  protocol    = "tcp_udp"
  source      = { zone_id = unifi_firewall_zone.untrusted.id }
  destination = { zone_id = data.unifi_firewall_zone.internal.id, ips = local.internal_dns, port = "53" }
}

resource "unifi_firewall_zone_policy" "cameras_dns" {
  name        = "Cameras DNS to AdGuard"
  action      = "ALLOW"
  protocol    = "tcp_udp"
  source      = { zone_id = unifi_firewall_zone.cameras.id }
  destination = { zone_id = data.unifi_firewall_zone.internal.id, ips = local.internal_dns, port = "53" }
}

# --- Block segmented zones from the gateway's MANAGEMENT services (router UI / SSH).
#     The predefined "Allow All <zone>->Gateway" (needed for DHCP) otherwise exposes
#     the UDM admin login to Guest/IoT clients. These zones need NO TCP to the
#     gateway (DHCP is UDP; DNS goes to AdGuard in Internal; internet is forwarded),
#     so blocking all TCP to the Gateway zone kills the UI/SSH while leaving DHCP and
#     routing intact. Cameras omitted (UniFi Protect needs gateway access). ---
data "unifi_firewall_zone" "gateway" { name = "Gateway" }

resource "unifi_firewall_zone_policy" "untrusted_block_gw_mgmt" {
  name        = "Untrusted block gateway TCP"
  action      = "BLOCK"
  protocol    = "tcp"
  source      = { zone_id = unifi_firewall_zone.untrusted.id }
  destination = { zone_id = data.unifi_firewall_zone.gateway.id }
}

resource "unifi_firewall_zone_policy" "iot_block_gw_mgmt" {
  name        = "IoT block gateway TCP"
  action      = "BLOCK"
  protocol    = "tcp"
  source      = { zone_id = unifi_firewall_zone.iot.id }
  destination = { zone_id = data.unifi_firewall_zone.gateway.id }
}

# --- IoT (VLAN 20) -> Spoolman, via the Traefik LB in the K8s pool (VLAN 90, Internal
#     zone). The QIDI Max 4 runs Klipper/Moonraker; Moonraker's [spoolman] component
#     connects OUTBOUND from the printer to Spoolman's API to decrement spool weight as
#     a print runs. That direction is IoT->Internal, which the predefined default blocks.
#     Narrow single-host allow, same shape as the DNS rules above — and a single IP, so
#     it sidesteps the provider's no-range/no-CIDR limit that deferred the Kids rule.
#     No auto_allow_return_traffic needed: Traefik's reply is Internal->IoT, already
#     covered by internal_to_iot above (same reason the DNS allows work without it).
#     DNS for spoolman.lab.mainertoo.com already resolves via the iot_dns rule.
resource "unifi_firewall_zone_policy" "iot_to_spoolman" {
  name        = "IoT to Spoolman (Traefik)"
  action      = "ALLOW"
  protocol    = "tcp"
  source      = { zone_id = unifi_firewall_zone.iot.id }
  destination = { zone_id = data.unifi_firewall_zone.internal.id, ips = [local.traefik_lb], port = "443" }
}

locals {
  # Traefik's MetalLB address (metallb-system/mainertoo-l2-pool 192.168.90.180-.199).
  # Everything on *.lab.mainertoo.com is fronted here, so this one host is all a
  # segmented zone needs to reach an in-cluster service by name over TLS.
  traefik_lb = "192.168.90.180"
}

# --- Kids (VLAN 40) -> MetalLB media pool: DEFERRED to a follow-up. The provider's
#     ips field takes pure IPs only (no range/CIDR), so the .180-.199 pool needs a
#     UniFi address group (unifi_firewall_group + ip_group_id). Add once the exact
#     media LB IPs Kids need are confirmed. Until then Kids->Internal stays blocked.
