###########
# General #
###########
variable "cluster_name" {
  description = "Name of cluster (prefix for VM names: <cluster_name><master_prefix><n>)"
  type        = string
}

variable "gateway" {
  description = "Gateway IP"
  type        = string
  default     = "192.168.90.1"
}

variable "ci_template" {
  description = "Source cloud init template to clone in Proxmox."
  type        = string
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
  default     = "dhcp"
}

variable "network_bridge" {
  description = "Network device bridge"
  type        = string
  default     = "vmbr0"
}

variable "network_vlan_id" {
  description = "VLAN ID for the VM network interface"
  type        = number
  default     = 90
}

variable "host_usb" {
  description = "Host USB device (Sonoff Zigbee Gateway)"
  type        = string
  default     = "10c4:ea60"
}

variable "usb_mapping_enabled" {
  description = "Whether to create the Proxmox USB hardware mapping. Disable for clusters that don't need USB passthrough (e.g. staging)."
  type        = bool
  default     = true
}

variable "k3s_cpu_type" {
  description = "The emulated CPU type"
  type        = string
  default     = "host"
}

variable "ssh_public_key_path" {
  description = "Path (relative to the consuming root module) to the SSH public key injected into the cluster nodes via cloud-init."
  type        = string
  default     = "./ssh_host_ed25519.pub"
}

##########
# Master #
##########
variable "k3s_master_count" {
  description = "Number of master nodes"
  type        = number
  default     = 3
}

variable "k3s_master_disk_size" {
  description = "Disk size of the controlplane nodes (GB)"
  type        = string
  default     = "50"
}

variable "k3s_master_cores" {
  description = "Cores for each controlplane node"
  type        = number
  default     = 4
}

variable "k3s_master_memory" {
  description = "Memory for each controlplane node (MB)"
  type        = string
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
}

variable "k3s_master_vmids" {
  description = "VM IDs for the master nodes"
  type        = list(number)
}

##########
# Worker #
##########
variable "k3s_worker_count" {
  description = "Number of worker nodes"
  type        = number
  default     = 3
}

variable "k3s_worker_disk_size" {
  description = "Disk size of the worker nodes (GB)"
  type        = string
  default     = "400"
}

variable "k3s_worker_cores" {
  description = "Cores for each worker node"
  type        = number
  default     = 6
}

variable "k3s_worker_memory" {
  description = "Memory for each worker node (MB)"
  type        = string
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
}

variable "k3s_worker_vmids" {
  description = "VM IDs for the worker nodes"
  type        = list(number)
}

###########
# Proxmox #
###########
variable "pm_node_name" {
  description = "Default Proxmox node name. Used for cluster-wide resources (USB mapping, download file, cloud-init snippets) and as the fallback per-VM node when the per-VM lists below are empty."
  type        = string
}

variable "pm_master_node_names" {
  description = "Optional per-master-VM Proxmox node placement. When non-empty, length must equal k3s_master_count and each entry overrides pm_node_name for that master VM. Empty list (default) falls back to scalar pm_node_name for every master."
  type        = list(string)
  default     = []
}

variable "pm_worker_node_names" {
  description = "Optional per-worker-VM Proxmox node placement. When non-empty, length must equal k3s_worker_count and each entry overrides pm_node_name for that worker VM. Empty list (default) falls back to scalar pm_node_name for every worker."
  type        = list(string)
  default     = []
}

variable "pm_datastore_id" {
  description = "Id of the proxmox datastore used for snippets"
  type        = string
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
  default     = "https://cloud-images.ubuntu.com/questing/current/questing-server-cloudimg-amd64.img"
}

variable "ubuntu_password" {
  description = "Password for the ubuntu user (used by cloud-init)"
  type        = string
  sensitive   = true
}
