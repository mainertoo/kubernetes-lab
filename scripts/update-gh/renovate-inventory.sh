#!/usr/bin/env bash
# Renovate PR inventory for the update-gh skill.
#   renovate-inventory.sh               all open Renovate PRs: state, CI, changed lines
#   renovate-inventory.sh <PR#>...      only those PRs
#   renovate-inventory.sh -R mainertoo/home_server [PR#...]
#       same, for another repo (home_server has no CI: shows "CI: none")
#   renovate-inventory.sh --bazarr <sha256-hex>
#       diff bazarr Alembic migrations: running pod vs the new digest (needs crane)
set -euo pipefail
REPO=mainertoo/kubernetes-lab
if [[ "${1:-}" == "-R" ]]; then
  REPO="${2:?usage: -R owner/repo}"
  shift 2
fi

if [[ "${1:-}" == "--bazarr" ]]; then
  new="${2:?usage: --bazarr <sha256 hex of new digest>}"
  new="${new#sha256:}"
  work=$(mktemp -d)
  trap 'rm -rf "$work"' EXIT
  mkdir "$work/new"
  pod=$(kubectl -n media get pod -l app.kubernetes.io/name=bazarr -o name | head -1)
  kubectl exec -n media "$pod" -c main -- sh -c \
    'cd /app/bazarr/bin/migrations/versions && for f in $(ls *.py | sort); do md5sum "$f"; done' |
    awk '{print $1, $2}' >"$work/running"
  crane export "lscr.io/linuxserver/bazarr@sha256:$new" - |
    tar -x -C "$work/new" --strip-components=5 'app/bazarr/bin/migrations/versions'
  (cd "$work/new" && for f in $(ls *.py | sort); do md5 -r "$f"; done) | awk '{print $1, $2}' >"$work/new.txt"
  echo "running: $(wc -l <"$work/running") files   new: $(wc -l <"$work/new.txt") files"
  if diff "$work/running" "$work/new.txt"; then
    echo "IDENTICAL: no schema work on startup, safe to merge"
  else
    echo "DIFFERENT: new or changed migration, read it before merging"
    exit 1
  fi
  exit 0
fi

if [[ $# -gt 0 ]]; then
  prs="$*"
else
  prs=$(gh pr list -R "$REPO" --author app/renovate --state open --limit 100 --json number --jq '.[].number')
fi
[[ -z "$prs" ]] && { echo "No open Renovate PRs."; exit 0; }

for n in $prs; do
  gh pr view "$n" -R "$REPO" --json number,title,mergeable,statusCheckRollup --jq '
    "=== #\(.number) \(.title)  [\(.mergeable)]  CI: " +
    (([.statusCheckRollup[]? | (if (.conclusion // "") != "" then .conclusion else (.state // .status // "PENDING") end)] | group_by(.) | map("\(.[0])x\(length)") | join(" ")) as $ci | if $ci == "" then "none" else $ci end)'
  gh pr diff "$n" -R "$REPO" | grep -E '^(\+\+\+ b/|[-+] )' | sed 's|^+++ b/|  file: |' | head -30
done
