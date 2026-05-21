#!/bin/bash

# Production K3s cluster — reads VM info from the production terraform env.
STATE_FILE="$HOME/kubernetes-lab/terraform/environments/production/terraform.tfstate"

# Run Terraform output -json with the specified state file to get the Terraform output
INPUT_JSON=$(terraform output -json -state="$STATE_FILE")

# Extract VM details using jq
masters=$(echo "$INPUT_JSON" | jq -r '.vm_info.value[] | select(.vm_name | contains("master")) | .ip_address')
workers=$(echo "$INPUT_JSON" | jq -r '.vm_info.value[] | select(.vm_name | contains("worker")) | .ip_address')

if [ -z "$masters" ] || [ -z "$workers" ]; then
  echo "Error: Failed to retrieve IP addresses for masters or workers." >&2
  exit 1
fi

# Function to create a properly formatted JSON array for hosts
generate_hosts() {
  local host_list=$1
  local formatted_hosts=()

  for ip in $host_list; do
    formatted_hosts+=("\"$ip\"")
  done

  echo "${formatted_hosts[@]}" | tr ' ' ','
}

# Create JSON output for Ansible dynamic inventory.
# Group names are kept as "master" / "worker" so playbooks stay
# cluster-agnostic; per-cluster vars (k3s_version, kube_vip_ip,
# metallb pool, etc.) live under group_vars/all.yml beside this script.
cat <<EOF
{
  "all": {
    "hosts": [
      $(generate_hosts "$masters"),
      $(generate_hosts "$workers")
    ],
    "vars": {
      "ansible_user": "ubuntu",
      "ansible_ssh_private_key_file": "$HOME/.ssh/id_ed25519_k3s"
    }
  },
  "master": {
    "hosts": [
      $(generate_hosts "$masters")
    ]
  },
  "worker": {
    "hosts": [
      $(generate_hosts "$workers")
    ]
  },
  "k3s_cluster": {
    "children": [
      "master",
      "worker"
    ]
  }
}
EOF
