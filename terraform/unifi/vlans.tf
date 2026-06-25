# Phase A — additive VLANs for the home-network segmentation project.
# Source of truth: docs/network-vlan-design.md
#   (wiki infrastructure/networking/vlan-design, id 194).
#
# Management(1) / Kubernetes(90) / Ceph(99) are intentionally NOT managed here,
# so a Terraform mistake can never touch critical infra — only the 6 new VLANs
# below. All keep gateway .1, DHCP pool .50-.250, and the resilient DNS list.

locals {
  # Resilient resolver list (2026-06-24): AdGuard .50 (pve-mac) -> AdGuard .53
  # (pve-ugreen, OFF-rack) -> UDM .1 (gateway, always up). One resolver dying never
  # kills DNS. Also set live by scripts/unifi/dns-resilience.py (which additionally
  # covers the non-TF-managed Default/VLAN-1 + VLAN 90), so apply stays a no-op.
  dns_servers = ["192.168.1.50", "192.168.1.53", "192.168.1.1"]

  # The two AdGuard resolvers that live IN the Internal zone (VLAN 1). The firewall
  # DNS-allow rules for the segmented zones reference these (the .1 gateway resolver
  # is reached separately via the predefined Gateway UDP allow). Subset of dns_servers.
  internal_dns = ["192.168.1.50", "192.168.1.53"]

  vlans = {
    trusted = { id = 10, name = "Trusted" }
    iot     = { id = 20, name = "IoT" }
    guest   = { id = 30, name = "Guest" }
    kids    = { id = 40, name = "Kids" }
    cameras = { id = 50, name = "Cameras" }
    dmz     = { id = 60, name = "Servers-DMZ" }
  }
}

resource "unifi_network" "vlan" {
  for_each = local.vlans

  name    = each.value.name
  purpose = "corporate"

  vlan_id = each.value.id
  subnet  = "192.168.${each.value.id}.1/24"

  dhcp_enabled = true
  dhcp_start   = "192.168.${each.value.id}.50"
  dhcp_stop    = "192.168.${each.value.id}.250"
  dhcp_dns     = local.dns_servers

  # Design §"Deferred IoT phase": leave IGMP snooping OFF so Sonos / mDNS / SSDP
  # multicast flows once IoT moves to VLAN 20.
  igmp_snooping = false

  lifecycle {
    # The controller defaults per-network multicast_dns to true. We don't manage
    # cross-VLAN mDNS reflection until Phase D, so ignore the attribute rather
    # than fight the provider's true->null drift (which also errors on update in
    # filipowm v1.0.0). Phase D removes this and sets it true on Trusted + IoT.
    #
    # IPv6 (Matter ULA on VLAN 20 etc.) is set out-of-band by
    # scripts/unifi/vlan-postapply.py — the filipowm provider can't manage it
    # (update errors `not found` in v1.0.0). Ignore the IPv6 fields so a plain
    # `terraform apply` does NOT strip the Matter ULA (which breaks cross-VLAN
    # Matter) and plans stay clean.
    ignore_changes = [
      multicast_dns,
      ipv6_interface_type,
      ipv6_ra_enable,
      ipv6_ra_priority,
      ipv6_static_subnet,
    ]
  }
}
