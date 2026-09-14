#!/bin/sh
# Read-only update-status probe. Prints one TSV line. Never modifies the host.
. /etc/os-release 2>/dev/null
os="${PRETTY_NAME:-?}"
kern=$(uname -r)
up=$(awk '{printf "%dd", $1/86400}' /proc/uptime)
lists=$(ls -t /var/lib/apt/lists/*_Packages /var/lib/apt/lists/*Packages.lz4 2>/dev/null | head -1)
if [ -n "$lists" ]; then lists_age=$(( ( $(date +%s) - $(stat -c %Y "$lists") ) / 86400 ))d; else lists_age=?; fi
last=$(zgrep -h '^End-Date' /var/log/apt/history.log* 2>/dev/null | sort | tail -1 | cut -d' ' -f2)
upg=$(apt list --upgradable 2>/dev/null | grep -c upgradable)
sec=$(apt list --upgradable 2>/dev/null | grep -ciE 'security')
kpkg=$(apt list --upgradable 2>/dev/null | grep -cE '^(linux-image|proxmox-kernel|pve-kernel|linux-generic|linux-virtual)')
rr=no; [ -f /var/run/reboot-required ] && rr=yes
# newest installed kernel vs running (catches "installed but not booted")
newest=$(ls /boot/vmlinuz-* 2>/dev/null | sed 's|/boot/vmlinuz-||' | sort -V | tail -1)
[ -n "$newest" ] && [ "$newest" != "$kern" ] && rr="${rr}/newer-kernel:$newest"
ua=off; systemctl is-enabled unattended-upgrades >/dev/null 2>&1 && ua=on
pin=""; command -v proxmox-boot-tool >/dev/null 2>&1 && pin=$(proxmox-boot-tool kernel list 2>/dev/null | sed -n '/Pinned kernel/{n;p}' | tr -d ' ')
printf '%s\t%s\t%s\tup=%s\tlast_upgrade=%s\tpending=%s\tsec=%s\tkernel_pkgs=%s\treboot=%s\tlists=%s\tunattended=%s\tpin=%s\n' \
  "$(hostname)" "$os" "$kern" "$up" "${last:-never}" "$upg" "$sec" "$kpkg" "$rr" "$lists_age" "$ua" "${pin:--}"
