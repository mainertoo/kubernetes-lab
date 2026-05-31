#!/usr/bin/env bash
# pocket-manual-ingest.sh — DR fallback per docs/plans/pocket-to-open-notebook-pipeline.md §10.
#
# When the pocket-bridge is unavailable, this script manually ingests a Pocket
# recording into Open Notebook using the SAME title-marker convention (D16) so
# that when the bridge comes back up, its pre-create lookup will find and claim
# these objects rather than duplicating them.
#
# Usage:
#   ./scripts/pocket-manual-ingest.sh \
#       --pocket-export ~/Downloads/recording-export.json \
#       --notebook-id  notebook:abcdef \
#       --open-notebook https://notebook.lab.mainertoo.com \
#       [--bearer "$OPEN_NOTEBOOK_KEY"]    # optional; respects empty
#
# Expected --pocket-export JSON shape (Pocket dashboard export):
#   {
#     "id":           "<recording_id>",
#     "title":        "<recording title>",
#     "transcript":   "<full text>",
#     "summary":      "<summary text>",
#     "action_items": ["...", "..."]
#   }
# If your dashboard export uses different field names, edit the jq extractions
# below; the schema is conservative on Pocket's side and may have drifted.

set -euo pipefail

POCKET_EXPORT=""
NOTEBOOK_ID=""
ON_BASE=""
BEARER=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pocket-export)  POCKET_EXPORT="$2"; shift 2 ;;
    --notebook-id)    NOTEBOOK_ID="$2"; shift 2 ;;
    --open-notebook)  ON_BASE="${2%/}"; shift 2 ;;
    --bearer)         BEARER="$2"; shift 2 ;;
    -h|--help)
      sed -n '/^# Usage/,/^# If your/p' "$0"; exit 0 ;;
    *)
      echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$POCKET_EXPORT" ]]; then echo "FATAL: --pocket-export is required" >&2; exit 2; fi
if [[ -z "$NOTEBOOK_ID"   ]]; then echo "FATAL: --notebook-id is required"   >&2; exit 2; fi
if [[ -z "$ON_BASE"       ]]; then echo "FATAL: --open-notebook is required" >&2; exit 2; fi

if [[ ! -r "$POCKET_EXPORT" ]]; then
  echo "FATAL: cannot read $POCKET_EXPORT" >&2
  exit 2
fi

RECORDING_ID=$(jq -r '.id // .recording_id // empty' "$POCKET_EXPORT")
TITLE=$(jq -r '.title // "Pocket recording"' "$POCKET_EXPORT")
TRANSCRIPT=$(jq -r '.transcript // .text // ""' "$POCKET_EXPORT")
SUMMARY=$(jq -r '.summary // ""' "$POCKET_EXPORT")
ACTION_ITEMS=$(jq -r '.action_items // [] | map("- " + .) | join("\n")' "$POCKET_EXPORT")

if [[ -z "$RECORDING_ID" ]]; then
  echo "FATAL: export JSON missing .id or .recording_id" >&2
  exit 2
fi

# Title markers MUST match D16 exactly — otherwise the bridge won't claim these.
SOURCE_TITLE="${TITLE} [pocket-id:${RECORDING_ID}]"
SUMMARY_TITLE="Summary [pocket-id:${RECORDING_ID} kind:summary]"
ACTIONS_TITLE="Action items [pocket-id:${RECORDING_ID} kind:action_items]"

HEADERS=(-H "Content-Type: application/json")
if [[ -n "$BEARER" ]]; then
  HEADERS+=(-H "Authorization: Bearer ${BEARER}")
fi

# --- POST source --------------------------------------------------------------
echo "==> POST /api/sources/json (title='${SOURCE_TITLE}', notebook=${NOTEBOOK_ID})"
SOURCE_RESP=$(jq -nc \
  --arg title "$SOURCE_TITLE" \
  --arg content "$TRANSCRIPT" \
  --arg nb "$NOTEBOOK_ID" \
  '{type:"text", notebooks:[$nb], title:$title, content:$content,
    transformations:[], embed:true, async_processing:true}' \
  | curl -sS -X POST "${ON_BASE}/api/sources/json" "${HEADERS[@]}" -d @-)
SOURCE_ID=$(jq -r '.id // empty' <<<"$SOURCE_RESP")
if [[ -z "$SOURCE_ID" ]]; then
  echo "FATAL: source creation failed: $SOURCE_RESP" >&2
  exit 3
fi
echo "    source_id=$SOURCE_ID"

# --- POST summary note --------------------------------------------------------
if [[ -n "$SUMMARY" ]]; then
  echo "==> POST /api/notes (title='${SUMMARY_TITLE}')"
  jq -nc \
    --arg title "$SUMMARY_TITLE" \
    --arg content "$SUMMARY" \
    --arg nb "$NOTEBOOK_ID" \
    '{title:$title, content:$content, note_type:"ai", notebook_id:$nb}' \
    | curl -sS -X POST "${ON_BASE}/api/notes" "${HEADERS[@]}" -d @- | jq -r '"    note_id=" + .id'
else
  echo "==> SKIP summary note (empty in export)"
fi

# --- POST action-items note ---------------------------------------------------
if [[ -n "$ACTION_ITEMS" ]]; then
  echo "==> POST /api/notes (title='${ACTIONS_TITLE}')"
  jq -nc \
    --arg title "$ACTIONS_TITLE" \
    --arg content "$ACTION_ITEMS" \
    --arg nb "$NOTEBOOK_ID" \
    '{title:$title, content:$content, note_type:"ai", notebook_id:$nb}' \
    | curl -sS -X POST "${ON_BASE}/api/notes" "${HEADERS[@]}" -d @- | jq -r '"    note_id=" + .id'
else
  echo "==> SKIP action-items note (empty in export)"
fi

echo
echo "Done. The source + notes are tagged with pocket-id:${RECORDING_ID}."
echo "If/when the pocket-bridge processes this recording later, its D16 marker"
echo "lookup will find these objects and claim them rather than duplicating."
