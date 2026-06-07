#!/usr/bin/env bash
set -u

alerts=()

add_alert() {
  alerts+=("$1")
}

run_check() {
  local name="$1"
  shift
  local output
  if ! output=$("$@" 2>&1); then
    add_alert "$name failed: ${output//$'\n'/ | }"
  fi
}

hostname_f=$(hostname -f 2>/dev/null || hostname 2>/dev/null || true)
if [[ "$hostname_f" != zwave-js* ]]; then
  add_alert "unexpected watchdog host: ${hostname_f:-unknown}; expected zwave-js/ops-01"
fi

root_use=$(df -P / | awk 'NR==2 {gsub(/%/, "", $5); print $5}')
if [[ -z "${root_use:-}" ]]; then
  add_alert "could not determine root filesystem usage"
elif (( root_use >= 80 )); then
  add_alert "root filesystem usage high: ${root_use}%"
fi

SWAPON_BIN=$(command -v swapon || command -v /sbin/swapon || true)
if [[ -n "$SWAPON_BIN" ]]; then
  if ! "$SWAPON_BIN" --show=NAME --noheadings | grep -qx '/swapfile'; then
    add_alert "expected /swapfile is not active"
  fi
else
  add_alert "swapon command not available"
fi

if command -v docker >/dev/null 2>&1; then
  for c in zwave-js-ui portainer_agent; do
    status=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || true)
    if [[ "$status" != "running" ]]; then
      add_alert "docker container $c not running (status=${status:-missing})"
    fi
  done
else
  add_alert "docker command not available"
fi

run_check "gh auth status" gh auth status
gh_ssh_output=$(ssh -o BatchMode=yes -o ConnectTimeout=8 -T git@github.com 2>&1 || true)
if ! grep -q "successfully authenticated" <<<"$gh_ssh_output"; then
  add_alert "GitHub SSH auth failed: ${gh_ssh_output//$'\n'/ | }"
fi

run_check "kubectl get nodes" kubectl get nodes --request-timeout=10s --no-headers
if command -v kubectl >/dev/null 2>&1; then
  not_ready=$(kubectl get nodes --no-headers 2>/dev/null | awk '$2 != "Ready" {print $1":"$2}' | paste -sd, -)
  if [[ -n "${not_ready:-}" ]]; then
    add_alert "k3s nodes not Ready: $not_ready"
  fi
fi
run_check "flux get kustomizations" flux get kustomizations -A

for h in pve-s13 pve-mammoth pve-whistler pve-zermatt pve-ugreen pve-mac qnas pbs vps; do
  run_check "ssh $h" ssh -o BatchMode=yes -o ConnectTimeout=5 "$h" 'hostname 2>/dev/null || uname -n'
done

if ((${#alerts[@]} > 0)); then
  {
    echo "OPS NODE WATCHDOG ALERT ($(date -Is))"
    echo "Host: ${hostname_f:-unknown}"
    echo
    for a in "${alerts[@]}"; do
      echo "- $a"
    done
  }
fi
