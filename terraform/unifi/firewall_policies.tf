# Phase B STAGE 2 — zone policies.
#
# UniFi auto-created predefined per-zone-pair defaults when ZBF activated:
#   Internal -> Internal/External/Gateway/Vpn/Dmz = ALLOW  (untouched)
#   Internal -> {IoT,Cameras,Untrusted}           = BLOCK  (we override below)
#   {IoT,Cameras,Untrusted} -> *                  = BLOCK  (we add allows below)
# Return traffic for an ALLOW is handled statefully (established/related), so we
# deliberately do NOT set auto_allow_return_traffic — that would let a segmented
# zone INITIATE back into Internal, which we don't want.

# adguard_dns ("192.168.1.50") is defined in vlans.tf and reused here.

# --- Internal (Mgmt/Trusted/K8s/Ceph) may initiate into the segmented zones ---
# (needed for Home Assistant -> IoT control, admin access, camera viewing).
resource "unifi_firewall_zone_policy" "internal_to_iot" {
  name        = "Internal to IoT"
  action      = "ALLOW"
  source      = { zone_id = data.unifi_firewall_zone.internal.id }
  destination = { zone_id = unifi_firewall_zone.iot.id }
}

resource "unifi_firewall_zone_policy" "internal_to_untrusted" {
  name        = "Internal to Untrusted"
  action      = "ALLOW"
  source      = { zone_id = data.unifi_firewall_zone.internal.id }
  destination = { zone_id = unifi_firewall_zone.untrusted.id }
}

resource "unifi_firewall_zone_policy" "internal_to_cameras" {
  name        = "Internal to Cameras"
  action      = "ALLOW"
  source      = { zone_id = data.unifi_firewall_zone.internal.id }
  destination = { zone_id = unifi_firewall_zone.cameras.id }
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
  destination = { zone_id = data.unifi_firewall_zone.internal.id, ips = [local.adguard_dns], port = "53" }
}

resource "unifi_firewall_zone_policy" "untrusted_dns" {
  name        = "Untrusted DNS to AdGuard"
  action      = "ALLOW"
  protocol    = "tcp_udp"
  source      = { zone_id = unifi_firewall_zone.untrusted.id }
  destination = { zone_id = data.unifi_firewall_zone.internal.id, ips = [local.adguard_dns], port = "53" }
}

resource "unifi_firewall_zone_policy" "cameras_dns" {
  name        = "Cameras DNS to AdGuard"
  action      = "ALLOW"
  protocol    = "tcp_udp"
  source      = { zone_id = unifi_firewall_zone.cameras.id }
  destination = { zone_id = data.unifi_firewall_zone.internal.id, ips = [local.adguard_dns], port = "53" }
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

# --- Kids (VLAN 40) -> MetalLB media pool: DEFERRED to a follow-up. The provider's
#     ips field takes pure IPs only (no range/CIDR), so the .180-.199 pool needs a
#     UniFi address group (unifi_firewall_group + ip_group_id). Add once the exact
#     media LB IPs Kids need are confirmed. Until then Kids->Internal stays blocked.
