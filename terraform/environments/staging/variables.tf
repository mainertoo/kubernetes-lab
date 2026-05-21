###################################
# Proxmox connection (sensitive)  #
###################################
variable "pm_url" {
  description = "The url for the proxmox api on the host"
  type        = string
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

variable "pm_ssh_username" {
  description = "The user used by proxmox provider to access proxmox node"
  type        = string
}

variable "pm_ssh_password" {
  description = "The password for the user used by proxmox provider to access proxmox node"
  type        = string
  sensitive   = true
}

variable "ubuntu_password" {
  description = "Password for the ubuntu user (used by cloud-init)"
  type        = string
  sensitive   = true
}
