terraform {
  required_version = ">= 0.14"  
  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = ">=0.69.0"
    }
  }
}

provider "proxmox" {
  # Proxmox API endpoint
  endpoint = var.pm_url

  # Build the token in the required format: USER@REALM!TOKENID=UUID
  api_token = "${var.pm_token_id}=${var.pm_token_secret}"

  # Ignore self-signed cert issues
  insecure = var.pm_tls_insecure

  # SSH access to the Proxmox node for certain operations
  ssh {
    username = var.pm_ssh_username
    password = var.pm_ssh_password
  }
}

