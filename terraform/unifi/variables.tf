variable "guest_psk" {
  description = "WPA2 passphrase for mainertoo_zone_guest (VLAN 30). Injected as TF_VAR_guest_psk from scripts/unifi/credentials.sops.yaml via tf.sh."
  type        = string
  sensitive   = true
}

variable "kids_psk" {
  description = "WPA2 passphrase for mainertoo_zone_kids (VLAN 40). Injected as TF_VAR_kids_psk from scripts/unifi/credentials.sops.yaml via tf.sh."
  type        = string
  sensitive   = true
}
