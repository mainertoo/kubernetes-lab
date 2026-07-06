#!/usr/bin/env bash
# ops-node-watchdog — Hermes ops-node (DGX Spark / spark-fef1) health check.
#
# DEPLOYMENT (host-local, reference copy lives here in the repo):
#   - Installed on the Spark at ~/scripts/ops-node-watchdog.sh (user mainertoo).
#   - Invoked every 6h by Hermes' `ops-node-watchdog` cron, whose in-pod wrapper
#     (/opt/data/scripts/ops-node-watchdog-wrapper.sh) runs:
#       ssh ops-01 'bash /home/mainertoo/scripts/ops-node-watchdog.sh'
#     The `ops-01` SSH alias resolves to the Spark (see apps/base/hermes/
#     ssh-ops-node.conf). Output is delivered to Discord only on alerts.
#   - On a Spark rebuild, copy this file to ~/scripts/ and `chmod +x` it.
#
# Silent on success; prints alert lines only when something is wrong.
# (Superseded the zwave-js-era watchdog when Hermes' ops node moved to the Spark.)
set -u
alerts=()
add() { alerts+=("$1"); }

host=$(hostname 2>/dev/null || true)
[[ "$host" == spark-fef1* ]] || add "unexpected watchdog host: ${host:-unknown} (expected spark-fef1)"

# Root disk >= 85%
ru=$(df -P / | awk 'NR==2{gsub(/%/,"",$5);print $5}')
if [[ -z "${ru:-}" ]]; then add "could not read root disk usage"
elif (( ru >= 85 )); then add "root filesystem high: ${ru}%"; fi

# Unified memory >= 95% (vLLM legitimately uses a lot; only flag extreme pressure)
mp=$(free | awk '/Mem:/{if($2>0)printf "%d",($3/$2)*100}')
[[ -n "${mp:-}" ]] && (( mp >= 95 )) && add "memory pressure: ${mp}% used"

# Swap active
if command -v swapon >/dev/null 2>&1; then
  swapon --show=NAME --noheadings 2>/dev/null | grep -q . || add "no active swap"
fi

# Docker daemon + LLM containers healthy
if command -v docker >/dev/null 2>&1; then
  docker info >/dev/null 2>&1 || add "docker daemon not responding"
  for c in vllm mxbai-embed syncthing; do
    st=$(docker inspect -f '{{.State.Status}}{{if .State.Health}}/{{.State.Health.Status}}{{end}}' "$c" 2>/dev/null || echo missing)
    case "$st" in
      running/healthy|running) : ;;
      missing) add "container $c missing" ;;
      *) add "container $c not healthy: $st" ;;
    esac
  done
else
  add "docker not installed"
fi

# LLM endpoints respond
curl -fsS -o /dev/null --max-time 8 http://localhost:8000/v1/models 2>/dev/null || add "vLLM :8000 not responding"
curl -fsS -o /dev/null --max-time 8 http://localhost:8081/v1/models 2>/dev/null || add "mxbai-embed :8081 not responding"

# GPU present
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi >/dev/null 2>&1 || add "nvidia-smi failed (GPU may have dropped)"
else
  add "nvidia-smi not available"
fi

# Obsidian vault: surface Syncthing conflict files (agent memory integrity)
cc=$(find /home/mainertoo/obsidian-vault -name "*.sync-conflict-*" 2>/dev/null | wc -l)
(( cc > 0 )) && add "obsidian vault has $cc sync-conflict file(s)"

if (( ${#alerts[@]} > 0 )); then
  echo "⚠️ ops-node-watchdog (spark-fef1) — ${#alerts[@]} alert(s):"
  for a in "${alerts[@]}"; do echo "  • $a"; done
fi
