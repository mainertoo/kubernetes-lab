#!/usr/bin/env bash
# K3s / containerd AppArmor-fix watch for the update-gh skill.
#
# Background (2026-11-24): Ubuntu kernels >= 7.0.0-41/42 force an
# `<profile>//&unconfined` AppArmor stack on container processes. containerd's
# default profile then blocks self-signals: gosu livelocks and mariadbd hangs
# (LP#2172640, LP#2172422). The fix is containerd#14487, backported to
# release/2.0, 2.3 and 2.4 but NOT 2.2. While it is missing we keep:
#   - the prod kernel hold (k3s_os_upgrade.yml -e kernel_hold=hold), and
#   - appArmorProfile: Unconfined on the staging openemr/openmrs MariaDB pods (PR #2422).
#
# This prints, for the pinned K3s version and the latest K3s patch of each
# minor >= ours, the bundled containerd and whether it contains the fix.
# Exit 0 = nothing actionable; exit 10 = a K3s release with the fix exists (act on it).
set -euo pipefail
REPO_ROOT="$(git -C "$(dirname "$(readlink -f "$0")")" rev-parse --show-toplevel)"

# containerd series -> merge commit of the fix on that branch
fix_sha() {
  case "$1" in
    2.0) echo 633e520b4ba8774a16ed87282e9cb806600421a8 ;;  # #14589
    2.3) echo 35b0b51e798e23e57d210762dcbe68559eae94e3 ;;  # #14590
    2.4) echo d15a976af209580c4484520a3713068bb96f3d34 ;;  # #14591
    *)   echo "" ;;                                         # 2.2: no backport (yet?)
  esac
}

# "yes" if upstream containerd tag $1 (e.g. v2.3.8) contains the fix
has_fix() {
  local tag="$1" series sha status
  series=$(echo "$tag" | sed -E 's/^v([0-9]+\.[0-9]+).*/\1/')
  sha=$(fix_sha "$series")
  if [[ -z "$sha" ]]; then
    # Re-check: a 2.2 backport may have appeared since this script was written.
    if gh pr list -R containerd/containerd --state merged --base "release/$series" \
         --search "stacked profile peers" --json number --jq 'length' 2>/dev/null | grep -qv '^0$'; then
      echo "backport-merged-on-release/$series(update fix_sha)"
    else
      echo "no-backport"
    fi
    return
  fi
  status=$(gh api "repos/containerd/containerd/compare/$sha...$tag" --jq .status 2>/dev/null || echo error)
  case "$status" in ahead|identical) echo yes ;; *) echo no ;; esac
}

containerd_of() {  # K3s tag -> upstream containerd tag (strip -k3sN)
  gh release view "$1" -R k3s-io/k3s --json body --jq .body 2>/dev/null |
    grep -oE '\| Containerd \| \[v[0-9.]+' | grep -oE 'v[0-9.]+' | head -1
}

pinned=$(sed -nE 's/^k3s_version: "?([^"]+)"?/\1/p' "$REPO_ROOT/ansible/k3s-cluster/inventory/production/group_vars/all.yml")
pinned_minor=$(echo "$pinned" | sed -E 's/^v1\.([0-9]+)\..*/\1/')
echo "Pinned K3s (production): $pinned"
running=$(kubectl get nodes --context production -o jsonpath='{.items[0].status.nodeInfo.containerRuntimeVersion}' 2>/dev/null || echo "?")
echo "Running containerd (prod node 0): $running"
echo

actionable=0
printf '%-16s %-10s %s\n' K3S CONTAINERD HAS_FIX
for tag in "$pinned" $(gh release list -R k3s-io/k3s --limit 30 --exclude-pre-releases --json tagName --jq '.[].tagName' |
             awk -F. -v m="$pinned_minor" '{split($2,a,"."); if (a[1]+0 >= m) print}' |
             sort -t. -k2,2n -k3,3n | awk -F. '{last[$2]=$0} END {for (k in last) print last[k]}' | sort -t. -k2,2n); do
  cd_tag=$(containerd_of "$tag"); [[ -z "$cd_tag" ]] && { printf '%-16s %-10s %s\n' "$tag" "?" "?"; continue; }
  fix=$(has_fix "$cd_tag")
  printf '%-16s %-10s %s\n' "$tag" "$cd_tag" "$fix"
  [[ "$fix" == yes ]] && actionable=1
done
echo
if [[ $actionable -eq 1 ]]; then
  echo "ACTION: a K3s release bundles the AppArmor fix. Follow the update-gh skill's K3s section"
  echo "(staging K3s upgrade -> gosu test without override -> revert PR #2422 -> prod K3s -> kernel unhold)."
  exit 10
fi
echo "No K3s release bundles the containerd AppArmor fix yet. Keep the kernel hold + staging workaround."
