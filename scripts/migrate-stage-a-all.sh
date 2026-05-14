#!/bin/bash
# Phase 3 Stage A bulk rollout driver.
#
# Iterates every volsync ReplicationSource that uses the restic mover,
# skips entries already recorded in apps/archive/PHASE3-STAGE-A-LOG.md,
# and runs scripts/migrate-stage-a.sh against the remaining ones in
# sequence. Appends a log line per success. Continue-on-failure: one
# bad app doesn't block the rest; failures are summarized at the end
# so you can retry each one manually with the single-app driver.
#
# Usage:
#   scripts/migrate-stage-a-all.sh                     # all pending, interactive
#   scripts/migrate-stage-a-all.sh --dry-run           # list what would run
#   scripts/migrate-stage-a-all.sh --yes               # skip confirmation
#   scripts/migrate-stage-a-all.sh --max 5             # cap to first 5 pending
#   scripts/migrate-stage-a-all.sh --include media     # only ns/rs containing "media"
#   scripts/migrate-stage-a-all.sh --exclude plex,jellyfin
#
# Filter patterns are simple substring matches against either the
# namespace or the ReplicationSource name (case-sensitive). Comma-
# separated for multiple substrings; ANY match wins for include, ALL
# misses required for exclude.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="${REPO_ROOT}/apps/archive/PHASE3-STAGE-A-LOG.md"
DRIVER="${REPO_ROOT}/scripts/migrate-stage-a.sh"

DRY_RUN=0
SKIP_CONFIRM=0
MAX=""
INCLUDE=""
EXCLUDE=""

usage() {
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 64
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --yes|-y)  SKIP_CONFIRM=1; shift ;;
    --max)     MAX="$2"; shift 2 ;;
    --include) INCLUDE="$2"; shift 2 ;;
    --exclude) EXCLUDE="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "Unknown arg: $1" >&2; usage ;;
  esac
done

[ -x "$DRIVER" ] || { echo "ERROR: $DRIVER not found or not executable" >&2; exit 1; }
[ -f "$LOG" ]    || { echo "ERROR: $LOG missing" >&2; exit 1; }

# Returns 0 if $1 is in the comma-separated list $2, treating each list
# entry as a substring of $1.
matches_any() {
  local needle="$1" list="$2" IFS=',' part
  for part in $list; do
    [ -n "$part" ] || continue
    [[ "$needle" == *"$part"* ]] && return 0
  done
  return 1
}

# Pull the set of already-migrated tags from the log. A migrated entry
# is "**<ns>/<pvc>**" inside a checked-off bullet, e.g.:
#   - [x] **homepage/homepage** | 2026-05-13 | 22 snapshots
MIGRATED="$(grep -E '^\s*-\s*\[x\]\s*\*\*[^*]+\*\*' "$LOG" 2>/dev/null \
              | grep -oE '\*\*[^*]+\*\*' | tr -d '*' | sort -u || true)"

# Enumerate restic-mover ReplicationSources cluster-wide.
RS_JSON="$(kubectl get replicationsource -A -o json)"
TOTAL_RS=$(echo "$RS_JSON" | jq '[.items[] | select(.spec.restic != null)] | length')

PENDING=()
SKIPPED_DONE=()
SKIPPED_FILTER=()
while IFS='|' read -r ns rs pvc; do
  key="$ns/$pvc"
  if [ -n "$MIGRATED" ] && printf '%s\n' "$MIGRATED" | grep -qx "$key"; then
    SKIPPED_DONE+=("$key")
    continue
  fi
  if [ -n "$INCLUDE" ]; then
    if ! { matches_any "$ns" "$INCLUDE" || matches_any "$rs" "$INCLUDE"; }; then
      SKIPPED_FILTER+=("$key (no match for include)")
      continue
    fi
  fi
  if [ -n "$EXCLUDE" ]; then
    if matches_any "$ns" "$EXCLUDE" || matches_any "$rs" "$EXCLUDE"; then
      SKIPPED_FILTER+=("$key (matched exclude)")
      continue
    fi
  fi
  PENDING+=("$ns|$rs|$pvc")
done < <(echo "$RS_JSON" | jq -r '.items[] | select(.spec.restic != null) | "\(.metadata.namespace)|\(.metadata.name)|\(.spec.sourcePVC)"')

# Apply --max cap
if [ -n "$MAX" ] && [ "${#PENDING[@]}" -gt "$MAX" ]; then
  CAPPED=$(( ${#PENDING[@]} - MAX ))
  PENDING=("${PENDING[@]:0:$MAX}")
else
  CAPPED=0
fi

echo "=== Phase 3 Stage A bulk rollout ==="
echo "  Total restic RSes in cluster:  $TOTAL_RS"
echo "  Already migrated (in log):     ${#SKIPPED_DONE[@]}"
echo "  Filtered out:                  ${#SKIPPED_FILTER[@]}"
[ "$CAPPED" -gt 0 ] && echo "  Capped at --max=$MAX (skipped $CAPPED additional pending)"
echo "  Pending this run:              ${#PENDING[@]}"
echo ""

if [ ${#PENDING[@]} -eq 0 ]; then
  echo "Nothing to do. All restic RSes are either migrated, filtered, or cluster has no restic RSes."
  exit 0
fi

if [ "$DRY_RUN" -eq 1 ]; then
  echo "Would migrate (in this order):"
  for entry in "${PENDING[@]}"; do
    IFS='|' read -r ns rs pvc <<< "$entry"
    printf "  %-30s rs=%-25s pvc=%s\n" "$ns" "$rs" "$pvc"
  done
  exit 0
fi

if [ "$SKIP_CONFIRM" -ne 1 ]; then
  echo "Will sequentially run scripts/migrate-stage-a.sh for ${#PENDING[@]} app(s)."
  echo "Successes are auto-appended to $LOG; commit when done."
  echo ""
  printf 'Continue? [y/N] '
  read -r ans
  case "$ans" in [yY]|[yY][eE][sS]) ;; *) echo "Aborted."; exit 0 ;; esac
fi

SUCCEEDED=()
FAILED=()
START_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

for entry in "${PENDING[@]}"; do
  IFS='|' read -r ns rs pvc <<< "$entry"
  echo ""
  echo "================================================================"
  printf "MIGRATING %s/%s  (PVC=%s)\n" "$ns" "$rs" "$pvc"
  echo "================================================================"

  # Capture driver stdout so we can extract the snapshot count even
  # though the driver auto-cleans the Job afterward. Tee to terminal
  # for live progress.
  out_file="$(mktemp -t mig-XXXX.log)"
  if "$DRIVER" "$ns" "$rs" 2>&1 | tee "$out_file"; then
    count="$(grep -oE '[0-9]+ snapshots' "$out_file" | head -n1 | grep -oE '[0-9]+' || echo '?')"
    SUCCEEDED+=("$ns/$pvc:$count")
    # Append to log under a "Bulk rollout (<start ts>)" subsection so
    # multiple bulk runs are visually grouped. Create the subsection
    # header once per run.
    if ! grep -qF "## Bulk rollout $START_TS" "$LOG"; then
      printf '\n## Bulk rollout %s\n\n' "$START_TS" >> "$LOG"
    fi
    printf -- '- [x] **%s** | %s | %s snapshots\n' "$ns/$pvc" "$(date -u +%Y-%m-%d)" "$count" >> "$LOG"
  else
    FAILED+=("$ns/$rs")
  fi
  rm -f "$out_file"
done

echo ""
echo "================================================================"
echo "BULK ROLLOUT SUMMARY"
echo "================================================================"
echo "  Succeeded: ${#SUCCEEDED[@]}"
# Bash 3.2 (macOS default) treats "${arr[@]}" on an empty array as
# unbound under set -u, so length-guard each for-loop instead of
# trusting array expansion.
if [ ${#SUCCEEDED[@]} -gt 0 ]; then
  for s in "${SUCCEEDED[@]}"; do
    echo "    ✓ ${s/:/ — } snapshots"
  done
fi
echo "  Failed:    ${#FAILED[@]}"
if [ ${#FAILED[@]} -gt 0 ]; then
  for f in "${FAILED[@]}"; do
    echo "    ✗ $f"
  done
fi

if [ ${#FAILED[@]} -gt 0 ]; then
  echo ""
  echo "Retry failed apps with:"
  for f in "${FAILED[@]}"; do
    ns="${f%/*}"; rs="${f#*/}"
    echo "  scripts/migrate-stage-a.sh $ns $rs"
  done
fi

if [ ${#SUCCEEDED[@]} -gt 0 ]; then
  echo ""
  echo "Review the log changes and commit:"
  echo "  git diff $LOG"
  echo "  git add $LOG && git commit -m 'docs(volsync): Stage A bulk rollout ${START_TS}'"
fi

# Exit code: 0 if everything succeeded, 1 if any failed (so callers can
# detect failures even in non-interactive use).
[ ${#FAILED[@]} -eq 0 ]
