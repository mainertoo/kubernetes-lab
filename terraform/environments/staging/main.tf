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
  #
  # All 3 staging VMs land on pve-mammoth at terraform creation time
  # because Proxmox snippets are stored in per-host `local` storage —
  # creating a VM on a different host whose local doesn't have the
  # snippet file either fails at VM start (zermatt) or silently reads
  # whatever same-named snippet that host happens to have, which on
  # pve-whistler is production's snippet, and the staging worker booted
  # with hostname `mainertoo-k3s-worker-1` (verified 2026-05-21).
  #
  # Post-Phase-4 workflow: bring the cluster up here, install K3s, then
  # live-migrate worker-1 → pve-whistler and worker-2 → pve-zermatt via
  # the Proxmox UI. Cloud-init has already been consumed at first boot
  # so the migration is safe. Then `terraform apply -refresh-only` to
  # update state and update this file to record the post-migration
  # placement.
  pm_node_name         = "pve-mammoth"
  pm_master_node_names = ["pve-mammoth"]
  pm_worker_node_names = ["pve-mammoth", "pve-mammoth"]

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
