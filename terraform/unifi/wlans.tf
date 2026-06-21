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

resource "unifi_wlan" "kids" {
  name          = "mainertoo_zone_kids"
  security      = "wpapsk"
  passphrase    = var.kids_psk
  network_id    = unifi_network.vlan["kids"].id
  ap_group_ids  = [data.unifi_ap_group.default.id]
  user_group_id = data.unifi_user_group.default.id
}
