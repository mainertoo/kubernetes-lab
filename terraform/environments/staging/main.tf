module "cluster" {
  source = "../../modules/k3s-cluster"

  cluster_name = "staging"

  # 1 master + 2 workers, spread one VM per Proxmox host.
  k3s_master_count     = 1
  k3s_master_ips       = ["192.168.90.167/24"]
  k3s_master_vmids     = [671]
  k3s_master_cores     = 2
  k3s_master_memory    = "8192"
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

  # Each VM is created on its final Proxmox host. Cloud-init snippets and
  # the cloud image live in per-host `local` storage, so snippets_per_host
  # puts a copy of both on every host that runs a VM, under names prefixed
  # with the cluster name (on pve-whistler the unprefixed names are
  # production's; a staging worker once booted there with a production
  # hostname, 2026-05-21).
  #
  # Until the rebuild of 2026-11-28 this cluster used the older route: all
  # three VMs created on pve-mammoth, workers live-migrated afterwards.
  # A node_name change makes `terraform plan` replace the VM, so move VMs
  # with `qm migrate` and then record the new host here.
  pm_node_name         = "pve-mammoth"
  pm_master_node_names = ["pve-mammoth"]
  pm_worker_node_names = ["pve-whistler", "pve-zermatt"]
  snippets_per_host    = true

  # VM disks go on Ceph (k3s-rbd), not on pve-mammoth's `local` storage.
  # `local` there is the slow SPCC SSD that also holds the host's root
  # filesystem; a busy VM disk on it stalled the host and its Ceph monitor
  # on 2026-11-25 (and production master-1 on 2026-06-20). RBD images are
  # raw. The module ignores later datastore changes, so a worker's disk can
  # be moved to its new host's local NVMe when it is migrated.
  storage_pool     = "k3s-rbd"
  disk_file_format = "raw"

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
