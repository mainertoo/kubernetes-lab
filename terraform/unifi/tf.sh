#!/usr/bin/env bash
# Wrapper: decrypt the UniFi admin creds from SOPS into UNIFI_* env vars, then
# run terraform with them. Keeps secrets out of HCL, tfvars, and shell history.
#
# Usage:  ./tf.sh init   |   ./tf.sh plan   |   ./tf.sh apply   |   ./tf.sh <subcmd>
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo_root="$(git -C "$here" rev-parse --show-toplevel)"
creds="${repo_root}/scripts/unifi/credentials.sops.yaml"

if [[ ! -f "$creds" ]]; then
  echo "ERROR: $creds not found — create + SOPS-encrypt it first (see README.md)." >&2
  exit 1
fi

# Export UNIFI_* from the encrypted stringData block (same shape as the Proxmox creds).
while IFS='=' read -r k v; do
  export "$k=$v"
done < <(sops -d "$creds" | python3 -c "import sys,yaml; d=yaml.safe_load(sys.stdin)['stringData']; [print(f'{k}={v}') for k,v in d.items()]")

cd "$here"
exec terraform "$@"
