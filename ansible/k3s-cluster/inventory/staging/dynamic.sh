#!/bin/bash

# Staging K3s cluster — reads VM info from the staging terraform env.
STATE_FILE="$HOME/kubernetes-lab/terraform/environments/staging/terraform.tfstate"

# Staging state doesn't exist yet (created on first apply in Phase 4).
# Emit an empty inventory rather than failing so `ansible-inventory --list`
# tooling and CI scans don't blow up before the cluster is provisioned.
if [ ! -f "$STATE_FILE" ]; then
  cat <<'EOF'
{
  "all": { "hosts": [], "vars": { "ansible_user": "ubuntu" } },
  "master": { "hosts": [] },
  "worker": { "hosts": [] },
  "k3s_cluster": { "children": ["master", "worker"] }
}
EOF
  exit 0
fi

INPUT_JSON=$(terraform output -json -state="$STATE_FILE")

masters=$(echo "$INPUT_JSON" | jq -r '.vm_info.value[] | select(.vm_name | contains("master")) | .ip_address')
workers=$(echo "$INPUT_JSON" | jq -r '.vm_info.value[] | select(.vm_name | contains("worker")) | .ip_address')

if [ -z "$masters" ] || [ -z "$workers" ]; then
  echo "Error: Failed to retrieve IP addresses for masters or workers." >&2
  exit 1
fi

generate_hosts() {
  local host_list=$1
  local formatted_hosts=()
  for ip in $host_list; do
    formatted_hosts+=("\"$ip\"")
  done
  echo "${formatted_hosts[@]}" | tr ' ' ','
}

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
