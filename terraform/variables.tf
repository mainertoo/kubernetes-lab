###########
# General #
###########
variable "cluster_name" {
  description = "Name of cluster"
  type        = string
  default     = "mainertoo"
}

variable "gateway" {
  description = "Gateway IP"
  type        = string
  # Your VLAN 90 gateway
  default     = "192.168.90.1"
}

variable "ci_template" {
  description = "Source cloud init template to clone in Proxmox."
  type        = string
  # Your cloud-init template/image name
  default     = "questing-server-cloudimg-amd64.img"
}

variable "qemu_agent" {
  description = "Enable qemu guest agent"
  type        = bool
  default     = true
}

variable "os_type" {
  description = "OS Type, cloud-init"
  type        = string
  default     = "cloud-init"
}

variable "disk_type" {
  description = "Disk type to use for volumes"
  type        = string
  default     = "scsi"
}

variable "disk_interface" {
  description = "Disk interface to use for volumes"
  type        = string
  default     = "virtio0"
}

variable "disk_iothread" {
  description = "Enable iothread for disk"
  type        = bool
  default     = true
}

variable "disk_discard" {
  description = "Set block storage discard"
  type        = string
  default     = "on"
}

variable "storage_pool" {
  description = "Storage pool in Proxmox to use for volumes"
  type        = string
  # Your storage ID
  default     = "local"
}

variable "bios" {
  description = "BIOS Setting for Proxmox VM"
  type        = string
  default     = "seabios"
}

variable "scsihw" {
  description = "Bios SCSI Controller for Proxmox VM"
  type        = string
  default     = "virtio-scsi-pci"
}

variable "ipv4_address" {
  description = "IPV4 Address"
  type        = string
  # Usually overridden per-VM; leave as dhcp by default
  default     = "dhcp"
}

variable "network_bridge" {
  description = "Network device bridge"
  type        = string
  # Your VLAN 90 bridge
  default     = "vmbr0.90"
}

variable "host_usb" {
  description = "Host usb device (Sonoff Zigbee Gateway)"
  type        = string
  default     = "10c4:ea60"
}

variable "k3s_cpu_type" {
  description = "The emulated CPU type"
  type        = string
  default     = "host"
}

#######
# SSH #
#######
# (SSH key / user are usually set elsewhere or via cloud-init; nothing to change here)

##########
# Master #
##########
variable "k3s_master_count" {
  description = "Number of master nodes"
  type        = number
  # You want 3 masters
  default     = 3
}

variable "k3s_master_disk_size" {
  description = "Disk size of the controlplane nodes (GB)"
  type        = string
  # 50 GB per master
  default     = "50"
}

variable "k3s_master_cores" {
  description = "Cores for each controlplane node"
  type        = number
  # 4 cores per master
  default     = 4
}

variable "k3s_master_memory" {
  description = "Memory for each controlplane node (MB)"
  type        = string
  # 8 GB per master
  default     = "8192"
}

variable "k3s_master_name_prefix" {
  description = "Prefix for the controlplane node name"
  type        = string
  default     = "-k3s-master-"
}

variable "k3s_master_ips" {
  description = "IPv4 addresses for k3s master nodes"
  type        = list(string)
  default     = [
    "192.168.90.161/24",
    "192.168.90.162/24",
    "192.168.90.163/24",
  ]
}

# VM IDs for masters
variable "k3s_master_vmids" {
  description = "VM IDs for the master nodes"
  type        = list(number)
  default     = [
    661,
    662,
    663,
  ]
}

##########
# Worker #
##########
variable "k3s_worker_count" {
  description = "Number of worker nodes"
  type        = number
  # You want 3 workers
  default     = 3
}

variable "k3s_worker_disk_size" {
  description = "Disk size of the worker nodes (GB)"
  type        = string
  # 400 GB per worker
  default     = "400"
}

variable "k3s_worker_cores" {
  description = "Cores for each worker node"
  type        = number
  # 6 cores per worker
  default     = 6
}

variable "k3s_worker_memory" {
  description = "Memory for each worker node (MB)"
  type        = string
  # 16 GB per worker
  default     = "16384"
}

variable "k3s_worker_name_prefix" {
  description = "Prefix for the worker node name"
  type        = string
  default     = "-k3s-worker-"
}

variable "k3s_worker_ips" {
  description = "IPv4 addresses for k3s worker nodes"
  type        = list(string)
  default     = [
    "192.168.90.164/24",
    "192.168.90.165/24",
    "192.168.90.166/24",
  ]
}

# VM IDs for workers
variable "k3s_worker_vmids" {
  description = "VM IDs for the worker nodes"
  type        = list(number)
  default     = [
    664,
    665,
    666,
  ]
}

###########
# Proxmox #
###########
variable "pm_url" {
  description = "The url for the proxmox api on the host"
  type        = string
  # Your Proxmox URL
  default     = "https://192.168.1.107:8006/"
}

variable "pm_token_secret" {
  description = "The token secret for the proxmox secret"
  type        = string
  sensitive   = true
}

variable "pm_token_id" {
  description = "The token id for the proxmox secret"
  type        = string
}

variable "pm_tls_insecure" {
  description = "Set to true to ignore certificate errors"
  type        = bool
  default     = true
}

variable "pm_node_name" {
  description = "name of the proxmox node to create the VMs on"
  type        = string
  # Your node name
  default     = "pve-whistler"
}

variable "pm_datastore_id" {
  description = "Id of the proxmox datastore used for snippets"
  type        = string
  # You’re using 'local' storage
  default     = "local"
}

variable "pm_snippet_content_type" {
  description = "Content type used for proxmox snippets"
  type        = string
  default     = "snippets"
}

variable "pm_cloud_image_content_type" {
  description = "Content type used for proxmox cloud images"
  type        = string
  default     = "iso"
}

variable "pm_cloud_image_url" {
  description = "Url for the Cloud image to be cloned onto proxmox"
  type        = string
  # You already have questing-server-cloudimg-amd64.img locally; this may be unused now
  default     = "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img"
}

variable "pm_ssh_username" {
  description = "The user used by proxmox provider to access prommox node"
  type        = string
  # If this module actually SSHes to the node, you can set this to a real user you have (e.g. root)
  default     = "terraform-prov"
}

variable "pm_ssh_password" {
  description = "The password for the user used by proxmox provider to access prommox node"
  type        = string
  sensitive   = true
}