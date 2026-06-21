# Phase A — additive VLANs for the home-network segmentation project.
# Source of truth: docs/network-vlan-design.md
#   (wiki infrastructure/networking/vlan-design, id 194).
#
# Management(1) / Kubernetes(90) / Ceph(99) are intentionally NOT managed here,
# so a Terraform mistake can never touch critical infra — only the 6 new VLANs
# below. All keep gateway .1, DHCP pool .50-.250, and AdGuard for DNS.

locals {
  adguard_dns = "192.168.1.50"

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
  dhcp_dns     = [local.adguard_dns]

  # Design §"Deferred IoT phase": leave IGMP snooping OFF so Sonos / mDNS / SSDP
  # multicast flows once IoT moves to VLAN 20.
  igmp_snooping = false

  lifecycle {
    # The controller defaults per-network multicast_dns to true. We don't manage
    # cross-VLAN mDNS reflection until Phase D, so ignore the attribute rather
    # than fight the provider's true->null drift (which also errors on update in
    # filipowm v1.0.0). Phase D removes this and sets it true on Trusted + IoT.
    ignore_changes = [multicast_dns]
  }
}
