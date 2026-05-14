#!/bin/bash
# Phase 3 Stage A driver — runs the volsync label-driven restore project's
# per-app snapshot migration into the shared restic repo on Garage.
#
# Usage:
#   scripts/migrate-stage-a.sh <namespace> <replication-source-name>
#
# Example:
#   scripts/migrate-stage-a.sh homepage homepage
#   scripts/migrate-stage-a.sh home-assistant esphome
#
# What it does:
#   1. Looks up the ReplicationSource to discover sourcePVC + legacy Secret name
#   2. Mirrors the legacy <app>-volsync Secret from the app's namespace into
#      volsync-system as migration-src-<ns>-<rs>
#   3. Renders apps/archive/volsync-stage-a-template.yaml with placeholders
#      substituted, applies it to volsync-system as a one-shot Job
#   4. Streams logs until Job exits, checks exit status
#   5. On success: cleans up Job + mirror Secret, prints the line to append
#      to apps/archive/PHASE3-STAGE-A-LOG.md
#   6. On failure: leaves Job + mirror Secret intact for inspection, exits non-zero
#
# After STAGE A OK, verify with:
#   kubectl -n volsync-system port-forward svc/pvc-plumber-restic 18080:8080 &
#   curl -s http://localhost:18080/exists/<ns>/<pvc> | jq .   # should show exists=true

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="${REPO_ROOT}/apps/archive/volsync-stage-a-template.yaml"

usage() {
  cat >&2 <<EOF
Usage: $0 <namespace> <replication-source-name>

  namespace                   Kubernetes namespace of the app being migrated.
  replication-source-name     Name of the volsync ReplicationSource in that ns.
                              Usually the same as the app name; check with
                              kubectl get replicationsource -n <namespace>.

The script discovers spec.sourcePVC and spec.restic.repository from the RS.
EOF
  exit 64
}

[ $# -eq 2 ] || usage
NS="$1"
RS="$2"

# Discover details from the live RS
PVC="$(kubectl -n "$NS" get replicationsource "$RS" -o jsonpath='{.spec.sourcePVC}' 2>/dev/null || true)"
LEGACY_SECRET="$(kubectl -n "$NS" get replicationsource "$RS" -o jsonpath='{.spec.restic.repository}' 2>/dev/null || true)"

if [ -z "$PVC" ] || [ -z "$LEGACY_SECRET" ]; then
  echo "ERROR: could not find ReplicationSource $NS/$RS or its spec.restic.* fields." >&2
  echo "       Run: kubectl -n $NS get replicationsource $RS -o yaml" >&2
  exit 1
fi

# Kubernetes-safe name: lowercase alphanumeric + dash only, max 63 chars
SAFE="$(printf '%s-%s' "$NS" "$RS" | tr -c 'a-z0-9-' '-' | cut -c1-40)"
JOB_NAME="volsync-stage-a-${SAFE}"
MIRROR_NAME="migration-src-${SAFE}"
TAG="${NS}/${PVC}"

echo "=== Phase 3 Stage A — $NS/$RS ==="
echo "  source PVC:    $PVC"
echo "  legacy Secret: $LEGACY_SECRET (in $NS)"
echo "  tag:           $TAG"
echo "  mirror Secret: $MIRROR_NAME (in volsync-system)"
echo "  Job:           $JOB_NAME (in volsync-system)"
echo ""

# 1. Mirror legacy Secret into volsync-system
echo "Mirroring legacy Secret..."
kubectl -n "$NS" get secret "$LEGACY_SECRET" -o json \
  | jq --arg name "$MIRROR_NAME" \
       '.metadata = {name: $name, namespace: "volsync-system"} | del(.metadata.resourceVersion, .metadata.uid, .metadata.creationTimestamp)' \
  | kubectl apply -f -

# 2. Render template + apply Job
echo "Applying Job..."
sed -e "s|__JOB_NAME__|$JOB_NAME|g" \
    -e "s|__NS__|$NS|g" \
    -e "s|__PVC__|$PVC|g" \
    -e "s|__MIRROR_SECRET__|$MIRROR_NAME|g" \
  "$TEMPLATE" \
  | kubectl apply -f -

# 3. Wait + stream logs
echo "Waiting for Pod to start..."
kubectl -n volsync-system wait --for=condition=PodScheduled --timeout=60s \
  pod -l job-name="$JOB_NAME" >/dev/null
sleep 2
kubectl -n volsync-system logs -f job/"$JOB_NAME" || true

# 4. Wait for the Job controller to register the terminal condition.
#    `kubectl logs -f` exits as soon as the container terminates, but
#    `.status.conditions[Complete|Failed]` may not be populated for a
#    second or two after that — so we explicitly wait. --condition=jsonpath
#    isn't portable across kubectl versions; instead we race --for=condition
#    against the two terminal states.
echo ""
STATUS="Unknown"
if kubectl -n volsync-system wait --for=condition=complete --timeout=60s job/"$JOB_NAME" >/dev/null 2>&1; then
  STATUS="True"
elif kubectl -n volsync-system wait --for=condition=failed --timeout=5s job/"$JOB_NAME" >/dev/null 2>&1; then
  STATUS="False"
fi
SNAPSHOT_COUNT="$(kubectl -n volsync-system logs job/"$JOB_NAME" 2>/dev/null | grep -oE 'STAGE A OK: [0-9]+' | grep -oE '[0-9]+' || echo '?')"

if [ "$STATUS" = "True" ]; then
  echo "✓ Stage A SUCCESS for $TAG (${SNAPSHOT_COUNT} snapshots)"
  echo ""
  echo "Cleaning up..."
  kubectl -n volsync-system delete job "$JOB_NAME" --wait=false >/dev/null
  kubectl -n volsync-system delete secret "$MIRROR_NAME" --wait=false >/dev/null
  echo ""
  echo "Append this line to apps/archive/PHASE3-STAGE-A-LOG.md:"
  printf "  - [x] **%s** | %s | %s snapshots\n" "$TAG" "$(date -u +%Y-%m-%d)" "$SNAPSHOT_COUNT"
  echo ""
  echo "Confirm via oracle:"
  echo "  kubectl -n volsync-system port-forward svc/pvc-plumber-restic 18080:8080 &"
  echo "  curl -s http://localhost:18080/exists/$TAG | jq ."
  exit 0
else
  echo "✗ Stage A FAILED for $TAG"
  echo ""
  echo "Inspect:"
  echo "  kubectl -n volsync-system describe job $JOB_NAME"
  echo "  kubectl -n volsync-system logs job/$JOB_NAME"
  echo ""
  echo "Mirror Secret '$MIRROR_NAME' left intact for retry."
  echo "Delete artifacts after debugging with:"
  echo "  kubectl -n volsync-system delete job $JOB_NAME"
  echo "  kubectl -n volsync-system delete secret $MIRROR_NAME"
  exit 1
fi
