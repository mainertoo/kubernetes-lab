module "cluster" {
  source = "../../modules/k3s-cluster"

  cluster_name = "staging"

  # 1 master + 2 workers, spread one VM per Proxmox host.
  k3s_master_count     = 1
  k3s_master_ips       = ["192.168.90.167/24"]
  k3s_master_vmids     = [671]
  k3s_master_cores     = 2
  k3s_master_memory    = "4096"
  k3s_master_disk_size = "50"

  k3s_worker_count = 2
  k3s_worker_ips = [
    "192.168.90.168/24",
    "192.168.90.169/24",
  ]
  k3s_worker_vmids     = [672, 673]
  k3s_worker_cores     = 2
  k3s_worker_memory    = "4096"
  k3s_worker_disk_size = "50"

  # Snippets / USB mapping / image download land on pve-mammoth.
  # Per-VM placement spreads master + workers across all 3 hosts.
  pm_node_name         = "pve-mammoth"
  pm_master_node_names = ["pve-mammoth"]
  pm_worker_node_names = ["pve-whistler", "pve-zermatt"]

  # Staging has no Zigbee dongle — skip the USB hardware mapping.
  # Also avoids name collision with production's usb_passthrough mapping
  # (Proxmox hardware mappings are cluster-wide).
  usb_mapping_enabled = false

  ssh_public_key_path = "../../ssh_host_ed25519.pub"

  ubuntu_password = var.ubuntu_password
}

output "vm_info" {
  value = module.cluster.vm_info
}
