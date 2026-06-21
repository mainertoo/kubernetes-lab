# Phase B — Zone-Based Firewall.
#
# Strategy: keep the predefined `Internal` zone holding the critical + trusted
# VLANs (1 Mgmt, 10 Trusted, 90 K8s, 99 Ceph) so the cluster's inter-VLAN flows
# (K8s<->Ceph<->NFS) are never disturbed. Move ONLY the empty untrusted VLANs
# (20/30/40/50/60) into new custom zones. Assigning a network to a custom zone
# removes it from Internal automatically (a network belongs to exactly one zone).
#
# STAGE 1 (this file, now): zones only — no policies yet. Once a VLAN leaves
# Internal it is default-deny inter-zone, which for these EMPTY VLANs is the
# desired isolated baseline. STAGE 2 adds the allow policies (internet, DNS,
# Internal->zone management, Kids->media). See firewall_policies.tf.

data "unifi_firewall_zone" "internal" { name = "Internal" }
data "unifi_firewall_zone" "external" { name = "External" }

resource "unifi_firewall_zone" "iot" {
  name     = "IoT"
  networks = [unifi_network.vlan["iot"].id]
}

resource "unifi_firewall_zone" "untrusted" {
  name = "Untrusted"
  networks = [
    unifi_network.vlan["guest"].id,
    unifi_network.vlan["kids"].id,
    unifi_network.vlan["dmz"].id,
  ]
}

resource "unifi_firewall_zone" "cameras" {
  name     = "Cameras"
  networks = [unifi_network.vlan["cameras"].id]
}
