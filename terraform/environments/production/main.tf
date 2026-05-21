module "cluster" {
  source = "../../modules/k3s-cluster"

  # Cluster identity — kept as "mainertoo" to preserve existing VM names
  # (mainertoo-k3s-master-1, etc). Renaming would force VM recreation.
  cluster_name = "mainertoo"

  # 3 masters + 3 workers
  k3s_master_count = 3
  k3s_master_ips = [
    "192.168.90.161/24",
    "192.168.90.162/24",
    "192.168.90.163/24",
  ]
  k3s_master_vmids = [661, 662, 663]

  k3s_worker_count = 3
  k3s_worker_ips = [
    "192.168.90.164/24",
    "192.168.90.165/24",
    "192.168.90.166/24",
  ]
  k3s_worker_vmids = [664, 665, 666]

  # Proxmox node placement.
  # pm_node_name = the default Proxmox host used for cluster-wide resources
  # (USB hardware mapping, cloud image download, cloud-init snippets) and
  # the fallback for any VM whose per-VM list entry is empty.
  #
  # pm_master_node_names / pm_worker_node_names override pm_node_name per VM
  # by index. Populated from the actual Proxmox VM distribution at the time
  # of the Phase 2 state refresh (TODO: set after running -refresh-only).
  pm_node_name = "pve-whistler"
  # pm_master_node_names = ["pve-???", "pve-???", "pve-???"]
  # pm_worker_node_names = ["pve-???", "pve-???", "pve-???"]

  # USB Zigbee passthrough mapping exists on the production cluster.
  usb_mapping_enabled = true

  # Shared SSH public key at terraform/ root.
  ssh_public_key_path = "../../ssh_host_ed25519.pub"

  # Sensitive — pulled from this env's tfvars.
  ubuntu_password = var.ubuntu_password
}

output "vm_info" {
  value = module.cluster.vm_info
}
