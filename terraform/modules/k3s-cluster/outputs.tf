output "vm_info" {
  description = "Per-VM info consumed by the Ansible dynamic inventory: cluster_name (for grouping), name, ip_address, role, node (Proxmox host)."
  value = [
    for vm in concat(
      proxmox_virtual_environment_vm.proxmox_vm_master,
      proxmox_virtual_environment_vm.proxmox_vm_worker
    ) :
    {
      vm_name      = vm.name
      ip_address   = vm.ipv4_addresses[1][0]
      node         = vm.node_name
      cluster_name = var.cluster_name
      # role inferred from vm name prefix: matches the *-master-N / *-worker-N pattern
      role = strcontains(vm.name, var.k3s_master_name_prefix) ? "master" : "worker"
    }
  ]
}
