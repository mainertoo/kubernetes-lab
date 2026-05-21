module "cluster" {
  source = "../../modules/k3s-cluster"

  # Cluster identity — kept as "mainertoo" to preserve existing VM names
  # (mainertoo-k3s-master-1, etc). Renaming would force VM recreation.
  cluster_name = "mainertoo"

  ##############
  # Masters    #
  ##############
  k3s_master_count = 3
  k3s_master_ips = [
    "192.168.90.161/24",
    "192.168.90.162/24",
    "192.168.90.163/24",
  ]
  k3s_master_vmids = [661, 662, 663]
  # Master sizing kept at module defaults (4 cores × 1 socket, 8 GiB).

  ##############
  # Workers    #
  ##############
  k3s_worker_count = 3
  k3s_worker_ips = [
    "192.168.90.164/24",
    "192.168.90.165/24",
    "192.168.90.166/24",
  ]
  k3s_worker_vmids = [664, 665, 666]

  # Workers were resized post-deploy: 4 cores × 2 sockets, 32 GiB.
  k3s_worker_cores   = 4
  k3s_worker_sockets = 2
  k3s_worker_memory  = "32768"

  ##############
  # Proxmox    #
  ##############
  # pm_node_name = default host for cluster-wide resources (USB hardware
  # mapping, cloud-init snippets, Ubuntu image download). Kept at
  # pve-whistler because the snippets and download file historically
  # live there.
  pm_node_name = "pve-whistler"

  # Per-VM node placement, captured from the live Proxmox cluster on
  # 2026-05-21. One master + one worker on each Proxmox host.
  pm_master_node_names = ["pve-mammoth", "pve-whistler", "pve-zermatt"]
  pm_worker_node_names = ["pve-mammoth", "pve-whistler", "pve-zermatt"]

  ##############
  # Storage    #
  ##############
  # VM root disks were migrated from local qcow2 → Ceph RBD raw.
  disk_file_format = "raw"

  ##############
  # GPU + NIC  #
  ##############
  # Intel iGPU vGPU partition per worker (one partition per Proxmox host;
  # device id .4/.5/.6 happens to match worker index because workers and
  # hosts are aligned by index). Used by Plex/Jellyfin/etc. for hardware
  # transcoding.
  worker_hostpci_ids = ["0000:00:02.4", "0000:00:02.5", "0000:00:02.6"]

  # Second NIC on each worker, untagged vmbr0, used for L2 access to the
  # 192.168.1.0/24 network (mDNS / Home Assistant local discovery).
  # DHCP assigns .246/.247/.248 to worker-1/2/3 inside the VM.
  worker_extra_nic_enabled = true
  worker_extra_nic_bridge  = "vmbr0"
  worker_extra_nic_vlan_id = 0

  # USB Zigbee passthrough mapping (cluster-wide name "usb_passthrough").
  usb_mapping_enabled = true

  # Shared SSH public key at terraform/ root.
  ssh_public_key_path = "../../ssh_host_ed25519.pub"

  # Sensitive — pulled from this env's tfvars.
  ubuntu_password = var.ubuntu_password
}

output "vm_info" {
  value = module.cluster.vm_info
}
