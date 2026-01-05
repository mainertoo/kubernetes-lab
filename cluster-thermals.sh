#!/usr/bin/env bash
set -euo pipefail

HOSTS=("pve-mammoth" "pve-whistler" "pve-zermatt")
SSH_OPTS=(-o BatchMode=no -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5)

REMOTE_SCRIPT='
set -euo pipefail

echo "=== HOST: $(hostname -f) ==="
echo "TIME: $(date -Is)"
echo "KERNEL: $(uname -r)"
echo "UPTIME: $(uptime -p || true)"
echo "LOAD:   $(cut -d" " -f1-3 /proc/loadavg 2>/dev/null || true)"
echo

echo "--- CPU + BOARD SENSORS (sensors) ---"
if command -v sensors >/dev/null 2>&1; then
  sensors || true
else
  echo "sensors not installed"
fi
echo

echo "--- THERMAL ZONES (sysfs) ---"
if [ -d /sys/class/thermal ]; then
  for z in /sys/class/thermal/thermal_zone*; do
    [ -e "$z/type" ] || continue
    t="$(cat "$z/type" 2>/dev/null || true)"
    tmp="$(cat "$z/temp" 2>/dev/null || true)"
    # temp is usually millidegC
    if [[ "$tmp" =~ ^[0-9]+$ ]]; then
      echo "$z  type=$t  temp_mC=$tmp  temp_C=$(awk "BEGIN{print $tmp/1000}")"
    else
      echo "$z  type=$t  temp_raw=$tmp"
    fi
  done
else
  echo "No /sys/class/thermal found"
fi
echo

echo "--- NVME LIST ---"
if command -v nvme >/dev/null 2>&1; then
  nvme list || true
else
  echo "nvme-cli not installed"
fi
echo

echo "--- NVME SMART SUMMARY (temps/warnings/errors) ---"
if command -v nvme >/dev/null 2>&1; then
  for ctrl in /dev/nvme[0-9]; do
    [ -e "$ctrl" ] || continue
    echo "## $ctrl"
    # Pull the key fields we care about
    nvme smart-log "$ctrl" 2>/dev/null | egrep -i \
      "critical_warning|temperature|Temperature Sensor|Warning Temperature Time|Critical Composite Temperature Time|percentage_used|unsafe_shutdowns|media_errors|num_err_log_entries|throttle" \
      || true
    echo
  done
else
  echo "nvme-cli not installed"
fi

echo "--- NVME ERROR LOG (if any) ---"
if command -v nvme >/dev/null 2>&1; then
  for ctrl in /dev/nvme[0-9]; do
    [ -e "$ctrl" ] || continue
    echo "## $ctrl"
    nvme error-log "$ctrl" 2>/dev/null | head -n 40 || true
    echo
  done
fi

echo "--- KERNEL LOG: thermal/throttle/nvme/pcie/aer/mce/edac/ras (last 250 lines match) ---"
journalctl -k -b --no-pager 2>/dev/null | egrep -i \
  "thermal|throttl|overheat|critical temperature|power limit|pwr|nvme|pcie|aer|fatal|uncorrect|mce|machine check|hardware error|edac|ras|ecc|memory failure|cpu error|cache error" \
  | tail -n 250 || true
echo

echo "--- QUICK WARM-UP TEST (optional): 90s cpu+vm, then sensors again ---"
if command -v stress-ng >/dev/null 2>&1; then
  echo "Running: stress-ng --cpu 2 --vm 1 --vm-bytes 25% --timeout 90s"
  stress-ng --cpu 2 --vm 1 --vm-bytes 25% --timeout 90s --metrics-brief || true
else
  echo "stress-ng not installed; skipping warm-up."
fi
echo

echo "--- SENSORS AFTER WARM-UP ---"
if command -v sensors >/dev/null 2>&1; then
  sensors || true
fi
echo
'

for h in "${HOSTS[@]}"; do
  echo "#################################################################"
  echo "### CONNECTING: root@${h}"
  echo "#################################################################"
  ssh "${SSH_OPTS[@]}" "root@${h}" "$REMOTE_SCRIPT" || {
    echo "!! FAILED on ${h}"
  }
  echo
done
