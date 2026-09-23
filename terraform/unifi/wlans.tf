# Phase A — new SSIDs. Broadcasting a NEW SSID moves no existing client, so this
# is non-disruptive. The existing `mainertoo_zone` retag to VLAN 10 is Phase C
# (it moves your devices) and `mainertoo_zone_IoT` is Phase D — neither is
# managed here yet.

data "unifi_ap_group" "default" {}
data "unifi_user_group" "default" {}

resource "unifi_wlan" "guest" {
  name          = "mainertoo_zone_guest"
  security      = "wpapsk"
  passphrase    = var.guest_psk
  network_id    = unifi_network.vlan["guest"].id
  ap_group_ids  = [data.unifi_ap_group.default.id]
  user_group_id = data.unifi_user_group.default.id

  # Guest SSID: isolate clients from each other (design doc Phase A step 2).
  l2_isolation = true
}

# Apple home hubs (HomePods) on IoT. mainertoo_zone_IoT is 2.4 GHz-only for cheap
# IoT radios; HomePods get their own SSID with 5 GHz for AirPlay quality. Same VLAN
# as the Thread border router + matter-server (see firewall_policies.tf
# apple_hubs_to_internal). Only the HomePods join it (the Apple TV is wired).
#
# 5 GHz ONLY — not just preference: the U6 Lite/U6 LR APs cap at 4 SSIDs per radio
# and their 2.4 GHz radio already carries zone/IoT/kids/guest, so a dual-band 5th
# SSID fails with api.err.TooManyWirelessNetwork. The 5 GHz radio had 3 (IoT is
# 2.4-only). Any further SSID must also avoid 2.4 GHz, or drop to the U7 AP group.
resource "unifi_wlan" "hubs" {
  name          = "mainertoo_zone_hubs"
  security      = "wpapsk"
  passphrase    = var.hubs_psk
  network_id    = unifi_network.vlan["iot"].id
  ap_group_ids  = [data.unifi_ap_group.default.id]
  user_group_id = data.unifi_user_group.default.id
  wlan_band     = "5g"
}

resource "unifi_wlan" "kids" {
  name          = "mainertoo_zone_kids"
  security      = "wpapsk"
  passphrase    = var.kids_psk
  network_id    = unifi_network.vlan["kids"].id
  ap_group_ids  = [data.unifi_ap_group.default.id]
  user_group_id = data.unifi_user_group.default.id
}
