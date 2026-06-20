locals {
  master_node_names = length(var.pm_master_node_names) > 0 ? var.pm_master_node_names : [for _ in range(var.k3s_master_count) : var.pm_node_name]
  worker_node_names = length(var.pm_worker_node_names) > 0 ? var.pm_worker_node_names : [for _ in range(var.k3s_worker_count) : var.pm_node_name]

  # Padded per-worker hostpci list — empty string at an index = no
  # passthrough on that worker. Padding guarantees a defined entry for
  # every worker count.index so the dynamic block below can index
  # safely without terraform's eager evaluation tripping on
  # out-of-bounds reads when worker_hostpci_ids is shorter (or empty).
  worker_hostpci_per_vm = [
    for i in range(var.k3s_worker_count) :
    try(var.worker_hostpci_ids[i], "")
  ]

  # Snippet filename prefix — auto-derived from cluster_name when
  # snippets_per_host is true, so two clusters writing snippets to the
  # same Proxmox host don't collide on file_name. Empty string when
  # snippets_per_host is false to preserve the legacy single-host
  # behavior (matters for production whose existing state predates
  # the per-host model).
  snippet_file_prefix = var.snippets_per_host ? "${var.cluster_name}-" : ""

  # Set of Proxmox hosts that actually run at least one VM in this
  # cluster — drives the per-host snippet upload when
  # snippets_per_host = true. Empty when false (the legacy
  # user_data_cloud_config resource handles that case).
  snippet_hosts = var.snippets_per_host ? toset(concat(local.master_node_names, local.worker_node_names)) : toset([])
}

data "local_file" "ssh_public_key" {
  filename = var.ssh_public_key_path
}

resource "proxmox_virtual_environment_hardware_mapping_usb" "usb_device" {
  count = var.usb_mapping_enabled ? 1 : 0

  comment = "USB Device"
  name    = "usb_passthrough"

  map = [
    {
      comment = "Sonoff Zigbee 3.0 USB Dongle Plus"
      id      = var.host_usb
      node    = var.pm_node_name
    },
  ]
}

resource "proxmox_virtual_environment_file" "user_data_cloud_config" {
  content_type = var.pm_snippet_content_type
  datastore_id = var.pm_datastore_id
  node_name    = var.pm_node_name

  # source_raw.data is consumed by cloud-init at first VM boot; updating
  # it post-deploy doesn't re-trigger cloud-init on existing VMs and
  # causes the file_id to change, cascading into VM replacement.
  lifecycle {
    ignore_changes = [source_raw]
  }

  source_raw {
    data = <<-EOF
    #cloud-config
    users:
      - default
      - name: ubuntu
        groups:
          - sudo
        shell: /bin/bash
        ssh_authorized_keys:
          - ${trimspace(data.local_file.ssh_public_key.content)}
        sudo: ALL=(ALL) NOPASSWD:ALL
        lock_passwd: false

    chpasswd:
      list: |
        ubuntu:${var.ubuntu_password}
      expire: False

    ssh_pwauth: True

    package_update: true
    package_upgrade: true

    runcmd:
      - apt update
      - apt install -y qemu-guest-agent net-tools nfs-common
      - apt install -y linux-modules-extra-$(uname -r) || true
      - apt install -y unattended-upgrades
      - dpkg-reconfigure -f noninteractive unattended-upgrades
      - timedatectl set-timezone America/Los_Angeles
      - systemctl enable qemu-guest-agent
      - systemctl restart qemu-guest-agent || systemctl start qemu-guest-agent
      - echo "cloud-init complete" > /var/log/cloud-init-custom.log
    EOF

    file_name = "user-data-cloud-config.yaml"
  }
}

# Per-host user_data snippet — only created when snippets_per_host = true.
# Uploads the same cloud-init user-data file to every Proxmox host that has
# at least one VM in this cluster, so a VM landing on a non-default host
# can find its cicustom user_data locally.
resource "proxmox_virtual_environment_file" "user_data_cloud_config_per_host" {
  for_each = local.snippet_hosts

  content_type = var.pm_snippet_content_type
  datastore_id = var.pm_datastore_id
  node_name    = each.value

  lifecycle {
    ignore_changes = [source_raw]
  }

  source_raw {
    data = <<-EOF
    #cloud-config
    users:
      - default
      - name: ubuntu
        groups:
          - sudo
        shell: /bin/bash
        ssh_authorized_keys:
          - ${trimspace(data.local_file.ssh_public_key.content)}
        sudo: ALL=(ALL) NOPASSWD:ALL
        lock_passwd: false

    chpasswd:
      list: |
        ubuntu:${var.ubuntu_password}
      expire: False

    ssh_pwauth: True

    package_update: true
    package_upgrade: true

    runcmd:
      - apt update
      - apt install -y qemu-guest-agent net-tools nfs-common
      - apt install -y linux-modules-extra-$(uname -r) || true
      - apt install -y unattended-upgrades
      - dpkg-reconfigure -f noninteractive unattended-upgrades
      - timedatectl set-timezone America/Los_Angeles
      - systemctl enable qemu-guest-agent
      - systemctl restart qemu-guest-agent || systemctl start qemu-guest-agent
      - echo "cloud-init complete" > /var/log/cloud-init-custom.log
    EOF

    file_name = "${local.snippet_file_prefix}user-data-cloud-config.yaml"
  }
}

resource "proxmox_virtual_environment_file" "metadata_cloud_config_master" {
  content_type = var.pm_snippet_content_type
  datastore_id = var.pm_datastore_id
  # When snippets_per_host is on, the metadata file lives on the host
  # that runs this specific master VM. When off (legacy / production),
  # all metadata files live on pm_node_name.
  node_name = var.snippets_per_host ? local.master_node_names[count.index] : var.pm_node_name

  count = var.k3s_master_count

  source_raw {
    data = <<-EOF
    #cloud-config
    local-hostname: ${join("", [var.cluster_name, var.k3s_master_name_prefix, count.index + 1])}
    EOF

    file_name = "${local.snippet_file_prefix}metadata-cloud-config-k3s-master-${count.index + 1}.yaml"
  }
}

resource "proxmox_virtual_environment_file" "metadata_cloud_config_worker" {
  content_type = var.pm_snippet_content_type
  datastore_id = var.pm_datastore_id
  node_name    = var.snippets_per_host ? local.worker_node_names[count.index] : var.pm_node_name

  count = var.k3s_worker_count

  source_raw {
    data = <<-EOF
    #cloud-config
    local-hostname: ${join("", [var.cluster_name, var.k3s_worker_name_prefix, count.index + 1])}
    EOF

    file_name = "${local.snippet_file_prefix}metadata-cloud-config-k3s-worker-${count.index + 1}.yaml"
  }
}

resource "proxmox_virtual_environment_vm" "proxmox_vm_master" {
  count     = var.k3s_master_count
  name      = join("", [var.cluster_name, var.k3s_master_name_prefix, count.index + 1])
  node_name = local.master_node_names[count.index]
  vm_id     = var.k3s_master_vmids[count.index]

  initialization {
    datastore_id = var.pm_datastore_id
    ip_config {
      ipv4 {
        address = var.k3s_master_ips[count.index]
        gateway = var.gateway
      }
    }

    # user_data picks the same-host per-cluster snippet when
    # snippets_per_host is on, else the legacy single-host file.
    # In either case lifecycle.ignore_changes = [initialization]
    # below means a flip between the two only affects new VM creates.
    user_data_file_id = var.snippets_per_host ? proxmox_virtual_environment_file.user_data_cloud_config_per_host[local.master_node_names[count.index]].id : proxmox_virtual_environment_file.user_data_cloud_config.id
    meta_data_file_id = proxmox_virtual_environment_file.metadata_cloud_config_master[count.index].id
  }

  agent {
    enabled = var.qemu_agent
  }

  cpu {
    cores   = var.k3s_master_cores
    sockets = var.k3s_master_sockets
    type    = var.k3s_cpu_type
  }

  memory {
    dedicated = var.k3s_master_memory
  }

  disk {
    datastore_id = var.storage_pool
    file_id      = proxmox_virtual_environment_download_file.ubuntu_cloud_image.id
    interface    = var.disk_interface
    iothread     = var.disk_iothread
    discard      = var.disk_discard
    size         = var.k3s_master_disk_size
    file_format  = var.disk_file_format
  }

  network_device {
    bridge  = var.network_bridge
    vlan_id = var.network_vlan_id
  }

  lifecycle {
    ignore_changes = [
      initialization,
      boot_order,
      delete_unreferenced_disks_on_destroy,
      purge_on_destroy,
      tags,
      disk[0].file_id,
      # Disk storage is placed operationally (e.g. master-1's etcd was moved off a slow
      # local NVMe to the k3s-rbd Ceph pool via `qm move-disk`). Ignore datastore_id so a
      # later `terraform apply` does NOT drag the disk back onto var.storage_pool.
      disk[0].datastore_id,
    ]
  }
}

resource "proxmox_virtual_environment_vm" "proxmox_vm_worker" {
  count     = var.k3s_worker_count
  name      = join("", [var.cluster_name, var.k3s_worker_name_prefix, count.index + 1])
  node_name = local.worker_node_names[count.index]
  vm_id     = var.k3s_worker_vmids[count.index]

  initialization {
    datastore_id = var.pm_datastore_id
    ip_config {
      ipv4 {
        address = var.k3s_worker_ips[count.index]
        gateway = var.gateway
      }
    }

    user_data_file_id = var.snippets_per_host ? proxmox_virtual_environment_file.user_data_cloud_config_per_host[local.worker_node_names[count.index]].id : proxmox_virtual_environment_file.user_data_cloud_config.id
    meta_data_file_id = proxmox_virtual_environment_file.metadata_cloud_config_worker[count.index].id
  }

  agent {
    enabled = var.qemu_agent
  }

  cpu {
    cores   = var.k3s_worker_cores
    sockets = var.k3s_worker_sockets
    type    = var.k3s_cpu_type
  }

  memory {
    dedicated = var.k3s_worker_memory
  }

  disk {
    datastore_id = var.storage_pool
    file_id      = proxmox_virtual_environment_download_file.ubuntu_cloud_image.id
    interface    = var.disk_interface
    iothread     = var.disk_iothread
    discard      = var.disk_discard
    size         = var.k3s_worker_disk_size
    file_format  = var.disk_file_format
  }

  network_device {
    bridge  = var.network_bridge
    vlan_id = var.network_vlan_id
  }

  dynamic "network_device" {
    for_each = var.worker_extra_nic_enabled ? [1] : []
    content {
      bridge  = var.worker_extra_nic_bridge
      vlan_id = var.worker_extra_nic_vlan_id
    }
  }

  dynamic "hostpci" {
    for_each = local.worker_hostpci_per_vm[count.index] != "" ? [local.worker_hostpci_per_vm[count.index]] : []
    content {
      device = "hostpci0"
      id     = hostpci.value
      pcie   = false
      rombar = true
      xvga   = false
    }
  }

  lifecycle {
    ignore_changes = [
      initialization,
      boot_order,
      delete_unreferenced_disks_on_destroy,
      purge_on_destroy,
      tags,
      disk[0].file_id,
    ]
  }
}

resource "proxmox_virtual_environment_download_file" "ubuntu_cloud_image" {
  content_type = var.pm_cloud_image_content_type
  datastore_id = var.pm_datastore_id
  node_name    = var.pm_node_name

  url = var.pm_cloud_image_url

  # The Ubuntu "current" URL is republished periodically (a few MB
  # between stable builds). Without overwrite=false, the provider sees
  # the size mismatch and replaces this resource, which would cascade
  # into every VM via disk.file_id. To pull a newer image, bump the
  # URL to a specific dated build (e.g. ".../20260520/...") and apply.
  overwrite = false
}
