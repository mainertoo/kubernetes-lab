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

# No hub SSID: the HomePods stay on mainertoo_zone and are put on IoT (VLAN 20) per client
# with UniFi's Virtual Network Override (2026-09-28). A dedicated mainertoo_zone_hubs SSID
# (5 GHz only — U6 Lite/LR allow 4 SSIDs per radio and 2.4 GHz is full) was created and
# then removed as unused. Any new SSID must be 5 GHz-only or limited to the U7 AP group.

resource "unifi_wlan" "kids" {
  name          = "mainertoo_zone_kids"
  security      = "wpapsk"
  passphrase    = var.kids_psk
  network_id    = unifi_network.vlan["kids"].id
  ap_group_ids  = [data.unifi_ap_group.default.id]
  user_group_id = data.unifi_user_group.default.id
}
