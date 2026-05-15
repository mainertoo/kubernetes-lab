#!/bin/bash
# Phase 5 per-app cutover driver for the volsync label-driven restore
# project. Runs the full per-app cycle distilled from the homepage +
# donetick + mealie manual walkthroughs:
#
#   1. kubectl label PVC `backup: <hourly|daily>` (triggers Kyverno
#      to generate the volsync-<pvc> Secret + <pvc>-backup RS + RD trio)
#   2. Stage B — suspend Flux, pause legacy RS, drain in-flight Jobs,
#      final restic copy via migrate-stage-a.sh, verify oracle, delete
#      legacy RS. Exits with Flux suspended.
#   3. Generate the 5b PR — static PVC manifest with the label + the
#      live PVC's dataSourceRef (mirrored if non-null), uncomment the
#      base kustomization, prune volsync-v2 + volsync/remote Components
#      and VOLSYNC_* substitutes from the production overlay. Push,
#      open PR, wait for CI green, prompt for merge.
#   4. Stage C — verify 5b is in flux-source-controller's git state
#      (positive: PVC label present in render; negative: no legacy RS
#      in render), resume Flux, wait for Ready=True.
#
# Usage:
#   scripts/migrate-stage-bc.sh <namespace> <rs-name> [options]
#
# Required positional:
#   <namespace>         App's k8s namespace
#   <rs-name>           Legacy ReplicationSource name (often == app name)
#
# Options:
#   --label hourly|daily   Default daily. High-churn apps (postgres,
#                          vaultwarden, sensor logs) should use hourly.
#   --skip-pr              Do Stage B + print 5b plan, but don't
#                          open the PR. Useful for first-time tests.
#   --no-confirm           Skip the interactive Stage B confirmation
#                          (still prompts to merge the PR).
#   --auto-merge           Auto-merge the PR after CI green (gh pr
#                          merge --squash --auto). Default off; use
#                          when batch-running.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ─── arg parsing ────────────────────────────────────────────────────
BACKUP_LABEL="daily"
SKIP_PR=0
NO_CONFIRM=0
AUTO_MERGE=0
NS=""
RS=""

usage() {
  sed -n '2,33p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 64
}

while [ $# -gt 0 ]; do
  case "$1" in
    --label)       BACKUP_LABEL="$2"; shift 2 ;;
    --skip-pr)     SKIP_PR=1; shift ;;
    --no-confirm)  NO_CONFIRM=1; shift ;;
    --auto-merge)  AUTO_MERGE=1; shift ;;
    -h|--help)     usage ;;
    *)
      if [ -z "$NS" ]; then NS="$1"
      elif [ -z "$RS" ]; then RS="$1"
      else echo "ERROR: unexpected arg '$1'" >&2; usage; fi
      shift ;;
  esac
done

[ -n "$NS" ] && [ -n "$RS" ] || usage
[[ "$BACKUP_LABEL" == "hourly" || "$BACKUP_LABEL" == "daily" ]] || {
  echo "ERROR: --label must be hourly or daily" >&2; exit 1; }

# ─── discovery ──────────────────────────────────────────────────────
echo "================================================================"
echo "Phase 5 per-app cutover: $NS / $RS"
echo "================================================================"

PVC="$(kubectl -n "$NS" get replicationsource "$RS" -o jsonpath='{.spec.sourcePVC}' 2>/dev/null || true)"
LEGACY_SECRET="$(kubectl -n "$NS" get replicationsource "$RS" -o jsonpath='{.spec.restic.repository}' 2>/dev/null || true)"

if [ -z "$PVC" ] || [ -z "$LEGACY_SECRET" ]; then
  echo "ERROR: ReplicationSource $NS/$RS not found, or missing spec.sourcePVC/spec.restic.repository." >&2
  echo "       Has this app already been cut over? Run 'kubectl -n $NS get rs.volsync.backube'" >&2
  exit 1
fi

# Live PVC inspection — capacity, storage class, access mode, existing
# dataSourceRef (to mirror in the 5b manifest if non-null).
PVC_JSON="$(kubectl -n "$NS" get pvc "$PVC" -o json 2>/dev/null || true)"
[ -n "$PVC_JSON" ] || { echo "ERROR: PVC $NS/$PVC not found" >&2; exit 1; }

SC="$(echo "$PVC_JSON" | jq -r '.spec.storageClassName')"
CAP="$(echo "$PVC_JSON" | jq -r '.spec.resources.requests.storage')"
ACCESS="$(echo "$PVC_JSON" | jq -r '.spec.accessModes[0]')"
LIVE_REF="$(echo "$PVC_JSON" | jq -c '.spec.dataSourceRef // null')"

# App's git layout — base manifests + production overlay path.
BASE_DIR="$REPO_ROOT/apps/base/$NS"
PROD_FILE="$REPO_ROOT/apps/production/$NS/kustomization.yaml"
[ -d "$BASE_DIR" ]   || { echo "ERROR: $BASE_DIR not a directory" >&2; exit 1; }
[ -f "$PROD_FILE" ]  || { echo "ERROR: $PROD_FILE not found" >&2; exit 1; }

echo "  PVC:           $PVC"
echo "  Storage:       $SC, $ACCESS, $CAP"
echo "  Legacy Secret: $LEGACY_SECRET"
echo "  Live ref:      $LIVE_REF"
echo "  Backup label:  $BACKUP_LABEL"
echo "  Branch:        feat/volsync-phase5b-$NS"
echo "  Tag (Kyverno): $NS/$PVC"

# ─── preflight checks ───────────────────────────────────────────────
preflight() {
  local already
  already="$(kubectl -n flux-system get kustomization "$NS" -o jsonpath='{.spec.suspend}' 2>/dev/null || true)"
  if [ "$already" = "true" ]; then
    echo "ERROR: Flux Kustomization $NS is already suspended. Previous cutover stuck?" >&2
    echo "       Inspect: kubectl -n flux-system get kustomization $NS -o yaml" >&2
    exit 2
  fi
  if ! kubectl -n "$NS" get pvc "$PVC" >/dev/null 2>&1; then
    echo "ERROR: PVC $NS/$PVC vanished between discovery and preflight" >&2
    exit 3
  fi
}

# ─── Stage B helpers ────────────────────────────────────────────────
trap_state() {
  # Restore-on-abort safety net: if Stage B aborts mid-way, un-pause
  # the legacy RS so the cluster keeps backing up.
  if [ "${STAGE_B_DONE:-0}" -eq 1 ]; then return; fi
  echo ""
  echo "ABORT TRAP: Stage B did not complete cleanly. Attempting rollback."
  if [ "${PAUSED:-0}" -eq 1 ] && kubectl -n "$NS" get replicationsource.volsync.backube "$RS" >/dev/null 2>&1; then
    echo "  un-pausing legacy RS $NS/$RS"
    kubectl -n "$NS" patch replicationsource.volsync.backube "$RS" --type merge -p '{"spec":{"paused":false}}' || true
  fi
  if [ "${FLUX_SUSPENDED:-0}" -eq 1 ]; then
    echo "  resuming Flux $NS"
    flux resume kustomization "$NS" -n flux-system || true
  fi
}

# ─── Stage B ────────────────────────────────────────────────────────
run_stage_b() {
  echo ""
  echo "─── STAGE B ────────────────────────────────────────────────"

  STAGE_B_DONE=0; PAUSED=0; FLUX_SUSPENDED=0
  trap trap_state EXIT

  # 1. Label PVC. Triggers Kyverno generate; mutate is scoped to
  #    CREATE-only (PR #357) so this is now safe on bound PVCs.
  echo "  [1/6] Labeling PVC backup=$BACKUP_LABEL"
  kubectl -n "$NS" label pvc "$PVC" "backup=$BACKUP_LABEL" --overwrite >/dev/null

  # 2. Wait for Kyverno to generate the trio (≤30s in practice).
  echo "  [2/6] Waiting for Kyverno to generate Secret + RS + RD..."
  local i=0
  while ! kubectl -n "$NS" get replicationsource.volsync.backube "${PVC}-backup" >/dev/null 2>&1; do
    sleep 2; i=$((i+1))
    [ $i -gt 30 ] && { echo "ERROR: Kyverno generate did not produce $NS/${PVC}-backup within 60s" >&2; exit 4; }
  done
  kubectl -n "$NS" get secret "volsync-$PVC" >/dev/null 2>&1 || \
    { echo "ERROR: volsync-$PVC Secret missing after RS generate" >&2; exit 4; }

  # 3. Suspend Flux. Stage B's runtime changes (pause, delete RS)
  #    must not be reverted by a reconcile from the still-old manifest.
  echo "  [3/6] Suspending Flux Kustomization $NS"
  flux suspend kustomization "$NS" -n flux-system >/dev/null
  FLUX_SUSPENDED=1

  # 4. Pause the legacy RS and drain any in-flight mover Jobs.
  echo "  [4/6] Pausing legacy RS $RS and draining in-flight Jobs"
  kubectl -n "$NS" patch replicationsource.volsync.backube "$RS" \
    --type merge -p '{"spec":{"paused":true}}' >/dev/null
  PAUSED=1
  if kubectl -n "$NS" get jobs -l volsync.backube/source-name="$PVC" \
       -o jsonpath='{.items[?(@.status.active>0)].metadata.name}' 2>/dev/null | grep -q .; then
    kubectl -n "$NS" wait --for=condition=complete --timeout=10m \
      job -l volsync.backube/source-name="$PVC" || \
      { echo "ERROR: legacy mover Jobs did not drain in 10m" >&2; exit 5; }
  fi

  # 5. Final catch-up restic copy — idempotent, captures any
  #    snapshots that landed since Phase 3 Stage A.
  echo "  [5/6] Final catch-up restic copy (idempotent)"
  if ! "$REPO_ROOT/scripts/migrate-stage-a.sh" "$NS" "$RS" >/tmp/stage-a-$NS.log 2>&1; then
    echo "ERROR: migrate-stage-a.sh failed; tail of log:" >&2
    tail -20 "/tmp/stage-a-$NS.log" >&2
    exit 6
  fi
  grep "STAGE A OK" "/tmp/stage-a-$NS.log" | tail -1
  rm -f "/tmp/stage-a-$NS.log"

  # 6. Verify the oracle agrees, then delete the legacy RS.
  echo "  [6/6] Oracle check then delete legacy RS"
  kubectl -n volsync-system port-forward svc/pvc-plumber-restic 18080:8080 >/dev/null 2>&1 &
  local pf=$!; sleep 2
  local oracle
  oracle="$(curl -fsS "http://localhost:18080/exists/$NS/$PVC" 2>&1 || true)"
  kill $pf 2>/dev/null || true; wait 2>/dev/null || true
  if [ "$(echo "$oracle" | jq -r '.exists // false')" != "true" ]; then
    echo "ERROR: oracle returned $oracle — refusing to delete legacy RS" >&2
    exit 7
  fi
  kubectl -n "$NS" delete replicationsource.volsync.backube "$RS" >/dev/null
  STAGE_B_DONE=1

  echo "  STAGE B OK — Flux $NS remains suspended; legacy RS deleted."
}

# ─── 5b PR generation ───────────────────────────────────────────────
# Rewrites the production overlay (drops volsync-v2 Components +
# VOLSYNC_* substitutes via Ruby) and the base manifests (writes a
# static PVC YAML with backup label + dataSourceRef mirror, ensures
# the base kustomization references it).
generate_5b() {
  echo ""
  echo "─── 5b PR ──────────────────────────────────────────────────"

  local pvc_file="$BASE_DIR/$PVC-pvc.yaml"
  local base_kust="$BASE_DIR/kustomization.yaml"

  # 1. Write the static PVC manifest.
  echo "  [1/4] Writing $pvc_file"
  cat > "$pvc_file" <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: $PVC
  labels:
    # Phase 5b of the volsync label-driven restore project.
    # ClusterPolicy volsync-pvc-backup-restore matches this label and
    # generates volsync-$PVC Secret + $PVC-backup RS + RD in this ns.
    backup: $BACKUP_LABEL
spec:
  accessModes:
    - $ACCESS
  storageClassName: $SC
EOF
  # If live PVC has a dataSourceRef, mirror it (immutable post-bind).
  if [ "$LIVE_REF" != "null" ] && [ -n "$LIVE_REF" ]; then
    local ref_apigroup ref_kind ref_name
    ref_apigroup="$(echo "$LIVE_REF" | jq -r '.apiGroup // empty')"
    ref_kind="$(echo "$LIVE_REF" | jq -r '.kind // empty')"
    ref_name="$(echo "$LIVE_REF" | jq -r '.name // empty')"
    cat >> "$pvc_file" <<EOF
  # Stale ref left over from the volsync-v2/bootstrap variant's
  # one-time populator (the bootstrap RD itself is long gone).
  # dataSourceRef is immutable on a bound PVC, so we mirror it here
  # to keep Flux's dry-run happy. On a fresh cluster rebuild,
  # Kyverno's mutate rule (CREATE-only) swaps it for $PVC-backup
  # at admission before bind.
  dataSourceRef:
    apiGroup: $ref_apigroup
    kind: $ref_kind
    name: $ref_name
EOF
  fi
  cat >> "$pvc_file" <<EOF
  resources:
    requests:
      storage: $CAP
EOF

  # 2. Ensure the base kustomization references the PVC manifest.
  # NOTE: macOS BSD sed/grep don't honor GNU `\s`. Use POSIX
  # `[[:space:]]` everywhere (caught on crafty — sed silently failed
  # to uncomment, leaving the rendered PVC without its backup label).
  echo "  [2/4] Ensuring $base_kust references $PVC-pvc.yaml"
  if grep -qE "^[[:space:]]*-[[:space:]]+$PVC-pvc\.yaml[[:space:]]*\$" "$base_kust"; then
    echo "        already referenced (idempotent)"
  elif grep -qE "^[[:space:]]*#[[:space:]]*-[[:space:]]+$PVC-pvc\.yaml[[:space:]]*\$" "$base_kust"; then
    # Uncomment the existing line.
    sed -i.bak -E "s|^([[:space:]]*)#[[:space:]]*-[[:space:]]+($PVC-pvc\.yaml[[:space:]]*)\$|\1- \2|" "$base_kust" && rm "$base_kust.bak"
    echo "        uncommented existing reference"
  else
    # Append. Guard against the file ending without a trailing
    # newline (the new entry would otherwise join the last line).
    [ -z "$(tail -c 1 "$base_kust" 2>/dev/null)" ] || echo "" >> "$base_kust"
    printf -- "  - %s-pvc.yaml\n" "$PVC" >> "$base_kust"
    echo "        appended reference"
  fi

  # 3. Prune volsync-v2 + volsync/remote Components and VOLSYNC_*
  #    substitutes from the production overlay (via Ruby — yq isn't
  #    on this box; ruby yaml lib ports cleanly).
  echo "  [3/4] Pruning volsync wiring from $PROD_FILE"
  ruby -ryaml -e '
    path = ARGV[0]
    doc = YAML.load_file(path)
    spec = doc["spec"] || {}

    # Components: drop anything pointing at volsync-v2 or volsync/remote
    if spec["components"]
      spec["components"] = spec["components"].reject do |c|
        c.is_a?(String) && (c.include?("volsync-v2") || c.include?("volsync/remote"))
      end
      spec.delete("components") if spec["components"].empty?
    end

    # postBuild.substitute: drop VOLSYNC_* / CAPACITY keys
    pb = spec["postBuild"]
    if pb && pb["substitute"]
      pb["substitute"].delete_if do |k, _|
        k.start_with?("VOLSYNC_") || k == "CAPACITY"
      end
    end

    # postBuild.substituteFrom: drop volsync-garage-base
    if pb && pb["substituteFrom"]
      pb["substituteFrom"].reject! do |s|
        s.is_a?(Hash) && s["name"] == "volsync-garage-base"
      end
      pb.delete("substituteFrom") if pb["substituteFrom"].empty?
    end

    File.write(path, doc.to_yaml)
  ' "$PROD_FILE"
  # Remove the YAML doc separator that Ruby always emits at the top.
  sed -i.bak -E '1{/^---$/d;}' "$PROD_FILE" && rm "$PROD_FILE.bak"
  # Prepend the doc-start marker + header comment for consistency.
  local tmp_header; tmp_header="$(mktemp)"
  cat > "$tmp_header" <<EOF
---
# yaml-language-server: \$schema=https://kube-schemas.pages.dev/kustomize.toolkit.fluxcd.io/kustomization_v1.json
# Phase 5b (settled) — $NS on production with label-driven volsync
# backup. components/volsync-v2 + volsync/remote removed; PVC owned by
# apps/base/$NS/$PVC-pvc.yaml with \`backup: $BACKUP_LABEL\`.
EOF
  cat "$tmp_header" "$PROD_FILE" > "$PROD_FILE.new" && mv "$PROD_FILE.new" "$PROD_FILE"
  rm "$tmp_header"

  # 4. Local sanity check — kustomize build must render cleanly + show
  #    the PVC with the backup label.
  echo "  [4/4] Local kustomize build sanity"
  if ! kustomize build "$BASE_DIR" >/tmp/render-$NS.yaml 2>&1; then
    echo "ERROR: kustomize build $BASE_DIR failed; head:" >&2
    head -30 "/tmp/render-$NS.yaml" >&2
    exit 8
  fi
  if ! awk '/^kind: PersistentVolumeClaim$/,/^---$/' "/tmp/render-$NS.yaml" | grep -q "backup: $BACKUP_LABEL"; then
    echo "ERROR: rendered PVC lacks backup: $BACKUP_LABEL label" >&2
    exit 9
  fi
  rm -f "/tmp/render-$NS.yaml"
  echo "  5b PR sources prepared."
}

# ─── PR branch + open + CI + merge ──────────────────────────────────
open_5b_pr() {
  local branch="feat/volsync-phase5b-$NS"
  echo ""
  echo "─── PR ─────────────────────────────────────────────────────"
  cd "$REPO_ROOT"
  git fetch origin master --quiet
  git checkout -b "$branch" origin/master >/dev/null 2>&1 || git checkout "$branch" >/dev/null

  git add "$BASE_DIR/$PVC-pvc.yaml" "$BASE_DIR/kustomization.yaml" "$PROD_FILE"
  if git diff --cached --quiet; then
    echo "  No changes staged; either already merged or run was a no-op."
    return 0
  fi
  git commit -m "feat($NS): Phase 5b — drop volsync-v2 Components, static PVC manifest

Driven by scripts/migrate-stage-bc.sh. Stage B already ran (Flux
suspended for $NS, legacy $RS RS deleted, final restic copy
completed).

  - apps/base/$NS/$PVC-pvc.yaml — backup: $BACKUP_LABEL label$([ "$LIVE_REF" != "null" ] && echo " + dataSourceRef mirror")
  - apps/base/$NS/kustomization.yaml — PVC referenced
  - apps/production/$NS/kustomization.yaml — volsync Components + VOLSYNC_* substitutes pruned

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
" >/dev/null

  git push -u origin "$branch" >/dev/null 2>&1

  local pr_url
  pr_url="$(gh pr create --base master \
    --title "feat($NS): Phase 5b — drop volsync-v2 Components" \
    --body "Per-app cutover for $NS. Stage B already ran; this is the GitOps half.

| | |
|---|---|
| PVC | \`$NS/$PVC\` |
| Storage | $SC, $ACCESS, $CAP |
| Backup label | \`backup: $BACKUP_LABEL\` |
| dataSourceRef mirror | $([ "$LIVE_REF" != "null" ] && echo "\`$(echo $LIVE_REF | jq -r '.name')\`" || echo "none (backup-only origin)") |
| Legacy RS | \`$RS\` (deleted by Stage B) |
| New trio | \`volsync-$PVC\` Secret + \`$PVC-backup\` RS + RD |

After merge, run \`flux resume kustomization $NS\` (or let the driver
do Stage C if invoked end-to-end).

🤖 Generated with [Claude Code](https://claude.com/claude-code)")"
  echo "  PR opened: $pr_url"

  # Watch CI
  echo "  Waiting for CI..."
  local run_id
  for i in $(seq 1 12); do
    run_id="$(gh run list --branch "$branch" --limit 1 --json databaseId --jq '.[0].databaseId' 2>/dev/null || true)"
    [ -n "$run_id" ] && [ "$run_id" != "null" ] && break
    sleep 3
  done
  [ -n "$run_id" ] || { echo "ERROR: no CI run for $branch" >&2; exit 10; }
  gh run watch "$run_id" --exit-status >/dev/null 2>&1 || {
    echo "ERROR: CI failed for PR $pr_url" >&2; exit 11; }
  echo "  CI green."

  # Merge: auto or manual prompt.
  if [ "$AUTO_MERGE" -eq 1 ]; then
    echo "  Auto-merging (squash)..."
    gh pr merge "$pr_url" --squash --delete-branch >/dev/null
  else
    echo "  PR is ready: $pr_url"
    printf "  Merge it via the GitHub UI (or 'gh pr merge --squash --delete-branch'), then press Enter to continue with Stage C... "
    read -r _
  fi

  # Pull merged state to local master.
  git checkout master >/dev/null
  git pull --ff-only origin master >/dev/null 2>&1
}

# ─── Stage C ────────────────────────────────────────────────────────
run_stage_c() {
  echo ""
  echo "─── STAGE C ────────────────────────────────────────────────"

  # 1. Positive + negative render guards from local repo (master).
  echo "  [1/3] Render guards"
  local render; render="$(kustomize build "$BASE_DIR" 2>/dev/null)"
  echo "$render" | awk '/^kind: PersistentVolumeClaim$/,/^---$/' | grep -q "backup: $BACKUP_LABEL" || \
    { echo "ERROR: positive guard — rendered PVC has no backup label" >&2; exit 12; }
  # Negative guard: no ReplicationSource in the base render (Kyverno
  # generates that at runtime).
  if echo "$render" | grep -qE "^kind: ReplicationSource$"; then
    echo "ERROR: negative guard — base render contains a ReplicationSource (should be Kyverno-only)" >&2
    exit 13
  fi

  # 2. Resume Flux.
  echo "  [2/3] Resuming Flux"
  flux resume kustomization "$NS" -n flux-system >/dev/null

  # 3. Wait for Ready=True, max ~3min.
  echo "  [3/3] Waiting for Ready=True"
  kubectl -n flux-system wait --for=condition=Ready --timeout=180s \
    kustomization "$NS" >/dev/null || \
    { echo "ERROR: Flux $NS did not reach Ready=True" >&2; exit 14; }
  echo "  STAGE C OK — $NS cut over."
}

# ─── orchestration ──────────────────────────────────────────────────
preflight

if [ "$NO_CONFIRM" -ne 1 ]; then
  printf "Proceed with Stage B for %s/%s? [y/N] " "$NS" "$RS"
  read -r ans
  case "$ans" in [yY]|[yY][eE][sS]) ;; *) echo "Aborted."; exit 0 ;; esac
fi

run_stage_b
generate_5b

if [ "$SKIP_PR" -eq 1 ]; then
  echo ""
  echo "--skip-pr: stopping after 5b generation. Inspect:"
  echo "  git status"
  echo "  kustomize build $BASE_DIR"
  exit 0
fi

open_5b_pr
run_stage_c

echo ""
echo "================================================================"
echo "✓ $NS/$PVC cut over end-to-end"
echo "================================================================"
