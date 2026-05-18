#!/bin/bash
# Phase 5 per-app cutover driver for the **Kopia** transition variant.
# Forked from migrate-stage-bc.sh on 2026-05-18; targets the legacy
# bucket-B apps that never went through the original Restic migration
# (the "~29 remaining" from project_volsync_label_driven_restore).
#
# Key differences from the Restic driver:
#   - Writes BOTH labels: `backup: <hourly|daily>` AND
#     `backup-engine: kopia` (engine-required policy needs both)
#   - Drops the "final catch-up restic copy" step — Kopia repo is fresh,
#     cold-start, first backup is full
#   - Drops the pvc-plumber-restic oracle check — replaced with a
#     direct "new Kopia RS + Secret exist" check
#
#   1. Label PVC `backup: <hourly|daily>` + `backup-engine: kopia`
#      (triggers Kyverno-kopia to generate volsync-<pvc> Secret +
#      <pvc>-backup RS + RD with spec.kopia)
#   2. Stage B — suspend Flux, pause legacy RS, drain in-flight Jobs,
#      verify Kopia trio exists, delete legacy RS. Exits with Flux
#      suspended.
#   3. Generate the 5b PR — static PVC manifest with both labels +
#      dataSourceRef mirror (if non-null), uncomment base kustomization,
#      prune volsync-v2 + volsync/remote Components + VOLSYNC_*
#      substitutes from the production overlay.
#   4. Stage C — verify 5b is in flux-source-controller's git state,
#      resume Flux, wait for Ready=True.
#
# Usage:
#   scripts/migrate-stage-bc-kopia.sh <namespace> <rs-name> [options]
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

# App's git layout — Flux Kustomization name doesn't always match the
# target namespace. Audiobookshelf lives at apps/base/audiobookshelf/
# but its RS is in the `media` ns. Bazarr lives at apps/base/media/
# bazarr/ (nested). Discover the Flux Kustomization owner from the
# PVC's own label, then read spec.path from the live Kustomization.
APP="$(echo "$PVC_JSON" | jq -r '.metadata.labels["kustomize.toolkit.fluxcd.io/name"] // empty')"
[ -n "$APP" ] || { echo "ERROR: PVC $NS/$PVC has no kustomize.toolkit.fluxcd.io/name label" >&2; exit 1; }
SPEC_PATH="$(kubectl -n flux-system get kustomization "$APP" -o jsonpath='{.spec.path}' 2>/dev/null || true)"
[ -n "$SPEC_PATH" ] || { echo "ERROR: Flux Kustomization $APP not found or has no spec.path" >&2; exit 1; }
# Normalize leading "./"
SPEC_PATH="${SPEC_PATH#./}"
BASE_DIR="$REPO_ROOT/$SPEC_PATH"
[ -d "$BASE_DIR" ] || { echo "ERROR: $BASE_DIR not a directory (spec.path=$SPEC_PATH)" >&2; exit 1; }

# Production overlay file — grep for the manifest declaring this APP
# name. Handles both plain `name: foo` and anchored `name: &app foo`.
PROD_FILE="$(grep -rlE "^[[:space:]]*name:[[:space:]]+(&[A-Za-z0-9_-]+[[:space:]]+)?${APP}[[:space:]]*\$" \
  "$REPO_ROOT/apps/production/" 2>/dev/null | head -1)"
[ -n "$PROD_FILE" ] || { echo "ERROR: no production overlay found for app=$APP under apps/production/" >&2; exit 1; }

echo "  PVC:           $PVC ($NS/$PVC)"
echo "  Flux Kust:     $APP"
echo "  Base dir:      $BASE_DIR"
echo "  Prod file:     ${PROD_FILE#$REPO_ROOT/}"
echo "  Storage:       $SC, $ACCESS, $CAP"
echo "  Legacy Secret: $LEGACY_SECRET"
echo "  Live ref:      $LIVE_REF"
echo "  Backup label:  $BACKUP_LABEL"
echo "  Branch:        feat/volsync-phase5b-$APP"
echo "  Tag (Kyverno): $NS/$PVC"

# ─── preflight checks ───────────────────────────────────────────────
preflight() {
  local already
  already="$(kubectl -n flux-system get kustomization "$APP" -o jsonpath='{.spec.suspend}' 2>/dev/null || true)"
  if [ "$already" = "true" ]; then
    echo "ERROR: Flux Kustomization $APP is already suspended. Previous cutover stuck?" >&2
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
    echo "  resuming Flux $APP"
    flux resume kustomization "$APP" -n flux-system || true
  fi
}

# ─── Stage B ────────────────────────────────────────────────────────
run_stage_b() {
  echo ""
  echo "─── STAGE B ────────────────────────────────────────────────"

  STAGE_B_DONE=0; PAUSED=0; FLUX_SUSPENDED=0
  trap trap_state EXIT

  # 1. Label PVC with BOTH backup + backup-engine. Triggers Kyverno-
  #    kopia generate; mutate is scoped to CREATE-only so this is safe
  #    on bound PVCs.
  echo "  [1/5] Labeling PVC backup=$BACKUP_LABEL, backup-engine=kopia"
  kubectl -n "$NS" label pvc "$PVC" \
    "backup=$BACKUP_LABEL" "backup-engine=kopia" --overwrite >/dev/null

  # 2. Wait for Kyverno to generate the trio (≤30s in practice). If it
  #    doesn't fire within 30s, re-annotate to retrigger admission —
  #    batches 2-4 of the Kopia label-flip showed webhookTimeout under
  #    burst can leave PVCs without generated children.
  echo "  [2/5] Waiting for Kyverno-kopia to generate Secret + RS + RD..."
  local i=0
  local retriggered=0
  while ! kubectl -n "$NS" get replicationsource.volsync.backube "${PVC}-backup" >/dev/null 2>&1; do
    sleep 2; i=$((i+1))
    if [ $i -eq 15 ] && [ $retriggered -eq 0 ]; then
      echo "        no RS yet at 30s; re-annotating to retrigger admission"
      kubectl -n "$NS" annotate pvc "$PVC" \
        "kopia-trigger.driver/ts=$(date +%s)" --overwrite >/dev/null
      retriggered=1
    fi
    [ $i -gt 45 ] && { echo "ERROR: Kyverno-kopia did not produce $NS/${PVC}-backup within 90s" >&2; exit 4; }
  done
  kubectl -n "$NS" get secret "volsync-$PVC" >/dev/null 2>&1 || \
    { echo "ERROR: volsync-$PVC Secret missing after RS generate" >&2; exit 4; }
  # Verify it's actually a Kopia RS (not a stale Restic one from before).
  local kopia_repo
  kopia_repo="$(kubectl -n "$NS" get replicationsource.volsync.backube "${PVC}-backup" \
    -o jsonpath='{.spec.kopia.repository}' 2>/dev/null)"
  [ -n "$kopia_repo" ] || \
    { echo "ERROR: $NS/${PVC}-backup exists but has no spec.kopia.repository" >&2; exit 4; }

  # 3. Suspend Flux. Stage B's runtime changes (pause, delete RS)
  #    must not be reverted by a reconcile from the still-old manifest.
  echo "  [3/5] Suspending Flux Kustomization $APP"
  flux suspend kustomization "$APP" -n flux-system >/dev/null
  FLUX_SUSPENDED=1

  # 4. Pause the legacy RS and drain any in-flight mover Jobs.
  echo "  [4/5] Pausing legacy RS $RS and draining in-flight Jobs"
  kubectl -n "$NS" patch replicationsource.volsync.backube "$RS" \
    --type merge -p '{"spec":{"paused":true}}' >/dev/null
  PAUSED=1
  if kubectl -n "$NS" get jobs -l volsync.backube/source-name="$PVC" \
       -o jsonpath='{.items[?(@.status.active>0)].metadata.name}' 2>/dev/null | grep -q .; then
    kubectl -n "$NS" wait --for=condition=complete --timeout=10m \
      job -l volsync.backube/source-name="$PVC" || \
      { echo "ERROR: legacy mover Jobs did not drain in 10m" >&2; exit 5; }
  fi

  # 5. Delete the legacy RS. No catch-up copy and no oracle check —
  #    Kopia repo is brand new; the new Kopia RS will do a full first
  #    backup at the next scheduled run (or Phase 6 enable).
  echo "  [5/5] Deleting legacy RS $RS (cold-start on Kopia; no history transfer)"
  kubectl -n "$NS" delete replicationsource.volsync.backube "$RS" >/dev/null
  STAGE_B_DONE=1

  echo "  STAGE B OK — Flux $APP remains suspended; legacy RS deleted; new Kopia RS ready."
}

# ─── 5b PR generation ───────────────────────────────────────────────
# Rewrites the production overlay (drops volsync-v2 Components +
# VOLSYNC_* substitutes via Ruby) and the base manifests (writes a
# static PVC YAML with backup label + dataSourceRef mirror, ensures
# the base kustomization references it).
generate_5b() {
  echo ""
  echo "─── 5b PR ──────────────────────────────────────────────────"

  # Empty-base sentinel handling. Some apps' production overlays
  # point at apps/base/empty/ because the volsync-v2 Components used
  # to render everything inline. Phase 5b needs real manifests, so we
  # create a per-app dedicated dir named after the Flux Kustomization
  # name and rewrite spec.path. apps/base/empty/kustomization.yaml
  # MUST be left as `resources: []` (still consumed by other
  # empty-base apps that haven't cut over yet).
  local empty_base=0
  if [ "$SPEC_PATH" = "apps/base/empty" ]; then
    empty_base=1
    local new_dir="apps/base/$APP"
    echo "  [0/4] Empty-base sentinel detected; creating $new_dir/"
    mkdir -p "$REPO_ROOT/$new_dir"
    BASE_DIR="$REPO_ROOT/$new_dir"
    SPEC_PATH="$new_dir"
    cat > "$BASE_DIR/kustomization.yaml" <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: $NS
resources:
  - $PVC-pvc.yaml
EOF
    # Rewrite production overlay spec.path. Handles both quoted and
    # unquoted forms.
    sed -i.bak -E "s|(^[[:space:]]+path:[[:space:]]+)\"?\\./apps/base/empty\"?[[:space:]]*\$|\\1\"./$new_dir\"|" "$PROD_FILE" \
      && rm "$PROD_FILE.bak"
  fi

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
    # Phase 5 of the Kopia transition (docs/volsync-kopia-transition.md).
    # ClusterPolicy volsync-pvc-backup-restore-kopia matches both labels
    # and generates volsync-$PVC Secret + $PVC-backup RS + RD with
    # spec.kopia in this ns.
    backup: $BACKUP_LABEL
    backup-engine: kopia
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
  if [ "$empty_base" -eq 1 ]; then
    echo "        kustomization.yaml created with reference (empty-base path)"
  elif grep -qE "^[[:space:]]*-[[:space:]]+$PVC-pvc\.yaml[[:space:]]*\$" "$base_kust"; then
    echo "        already referenced (idempotent)"
  elif grep -qE "^[[:space:]]*#[[:space:]]*-[[:space:]]+$PVC-pvc\.yaml[[:space:]]*\$" "$base_kust"; then
    # Uncomment the existing line.
    sed -i.bak -E "s|^([[:space:]]*)#[[:space:]]*-[[:space:]]+($PVC-pvc\.yaml[[:space:]]*)\$|\1- \2|" "$base_kust" && rm "$base_kust.bak"
    echo "        uncommented existing reference"
  else
    # Insert under the `resources:` block via awk. Naive end-of-file
    # append put new entries under whichever top-level field was last
    # (configMapGenerator, configurations, …) — broke mosquitto.
    awk -v entry="$PVC-pvc.yaml" '
      function ins() { if (state==1 && !done) { print "  - " entry; done=1; state=2 } }
      /^resources:[[:space:]]*$/ { state=1; print; next }
      state==1 && (/^[^ ]/ || /^$/) { ins() }
      { print }
      END { ins() }
    ' "$base_kust" > "$base_kust.tmp" && mv "$base_kust.tmp" "$base_kust"
    echo "        inserted into resources: block"
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
# Phase 5 of Kopia transition — $NS on production with label-driven
# Kopia backup. components/volsync-v2 + volsync/remote removed; PVC
# owned by apps/base/$APP/$PVC-pvc.yaml with \`backup: $BACKUP_LABEL\` +
# \`backup-engine: kopia\`.
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
  local branch="feat/kopia-cutover-$APP"
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
  git commit -m "feat($APP): Kopia cutover — drop volsync-v2 Components, static PVC manifest

Driven by scripts/migrate-stage-bc-kopia.sh (Phase 5 of the Kopia
transition). Stage B already ran (Flux Kustomization $APP suspended,
legacy $RS RS in ns $NS deleted; cold-start on Kopia repo, no history
transfer).

  - $SPEC_PATH/$PVC-pvc.yaml — backup: $BACKUP_LABEL + backup-engine: kopia$([ "$LIVE_REF" != "null" ] && echo " + dataSourceRef mirror")
  - $SPEC_PATH/kustomization.yaml — PVC referenced
  - ${PROD_FILE#$REPO_ROOT/} — volsync Components + VOLSYNC_* substitutes pruned

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
" >/dev/null

  git push -u origin "$branch" >/dev/null 2>&1

  local pr_url
  pr_url="$(gh pr create --base master \
    --title "feat($APP): Kopia cutover — drop volsync-v2 Components" \
    --body "Per-app Kopia cutover for Flux Kustomization \`$APP\` (target ns: \`$NS\`). Stage B already ran; this is the GitOps half.

| | |
|---|---|
| PVC | \`$NS/$PVC\` |
| Storage | $SC, $ACCESS, $CAP |
| Backup label | \`backup: $BACKUP_LABEL\` + \`backup-engine: kopia\` |
| dataSourceRef mirror | $([ "$LIVE_REF" != "null" ] && echo "\`$(echo $LIVE_REF | jq -r '.name')\`" || echo "none (backup-only origin)") |
| Legacy RS | \`$RS\` (deleted by Stage B) |
| New trio | \`volsync-$PVC\` Secret + \`$PVC-backup\` RS + RD (spec.kopia) |

After merge, run \`flux resume kustomization $APP\` (or let the driver
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
  local pvc_block; pvc_block="$(echo "$render" | awk '/^kind: PersistentVolumeClaim$/,/^---$/')"
  echo "$pvc_block" | grep -q "backup: $BACKUP_LABEL" || \
    { echo "ERROR: positive guard — rendered PVC has no backup label" >&2; exit 12; }
  echo "$pvc_block" | grep -q "backup-engine: kopia" || \
    { echo "ERROR: positive guard — rendered PVC has no backup-engine: kopia label" >&2; exit 12; }
  # Negative guard: no ReplicationSource in the base render (Kyverno
  # generates that at runtime).
  if echo "$render" | grep -qE "^kind: ReplicationSource$"; then
    echo "ERROR: negative guard — base render contains a ReplicationSource (should be Kyverno-only)" >&2
    exit 13
  fi

  # 2. Resume Flux.
  echo "  [2/3] Resuming Flux"
  flux resume kustomization "$APP" -n flux-system >/dev/null

  # 3. Wait for Ready=True, max ~3min.
  echo "  [3/3] Waiting for Ready=True"
  kubectl -n flux-system wait --for=condition=Ready --timeout=180s \
    kustomization "$APP" >/dev/null || \
    { echo "ERROR: Flux $APP did not reach Ready=True" >&2; exit 14; }
  echo "  STAGE C OK — $APP cut over."
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
