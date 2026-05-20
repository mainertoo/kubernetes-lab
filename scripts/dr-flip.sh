#!/usr/bin/env bash
# dr-flip.sh — flip CNPG db-cnpg.yaml consumers between initdb and recovery
# overlay modes (with lineage versioning). One operator action replaces 8
# per-file git edits during a cluster-nuke recovery.
#
# Companion to docs/plans/cnpg-overlay-refactor.md and the runbook at
# docs/cnpg-disaster-recovery.md. See those for the operational flow.
#
# Designed by mitchross/talos-argocd-proxmox-inspired overlay pattern + 7 rounds
# of Codex adversarial review.

set -Eeuo pipefail

# ---------- locate repo + helpers ---------------------------------------------

REPO_ROOT=$(git -C "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")" rev-parse --show-toplevel 2>/dev/null) || {
    echo "ERROR: must run inside the kubernetes-lab git repository" >&2
    exit 1
}

RECOVERY_PATCH="$REPO_ROOT/components/cnpg-cluster/recovery/bootstrap-patch.yaml"
[ -r "$RECOVERY_PATCH" ] || { echo "ERROR: $RECOVERY_PATCH not found" >&2; exit 1; }

# MAX_LINEAGE derived at runtime from the overlay file (Codex pass-5 Medium fix).
# Prevents two-file drift when the overlay is extended to v11+.
MAX_LINEAGE=$(
    yq -r '.spec.externalClusters[].name // ""' "$RECOVERY_PATCH" \
        | sed -nE 's/.*-restore-v([0-9]+)$/\1/p' \
        | sort -n | tail -1
)
: "${MAX_LINEAGE:?ERROR: no restore lineage entries (e.g. *-restore-v0) found in $RECOVERY_PATCH}"

command -v yq >/dev/null 2>&1 || { echo "ERROR: yq is required (https://github.com/mikefarah/yq)" >&2; exit 1; }

# Bash 3.2-compatible globals for the txn (declare -g is bash 4.2+).
STAGED_SRCS=()
STAGED_DSTS=()

# ---------- discovery ---------------------------------------------------------

# Find all db-cnpg.yaml files under apps/production that reference cnpg-cluster
# base + initdb/recovery overlay. Returns absolute paths, one per line.
discover_files() {
    local f
    while IFS= read -r f; do
        if yq -e '.spec.components[] | select(. | test("/cnpg-cluster/(base|initdb|recovery)$"))' "$f" >/dev/null 2>&1; then
            printf "%s\n" "$f"
        fi
    done < <(find "$REPO_ROOT/apps/production" -name 'db-cnpg.yaml' -type f 2>/dev/null)
}

# Map a file → APP name (via .spec.postBuild.substitute.APP).
file_to_app() {
    yq -r '.spec.postBuild.substitute.APP // ""' "$1"
}

# Read current mode from a file: returns "initdb" or "recovery".
file_mode() {
    if yq -e '.spec.components[] | select(. | test("/cnpg-cluster/recovery$"))' "$1" >/dev/null 2>&1; then
        echo "recovery"
    elif yq -e '.spec.components[] | select(. | test("/cnpg-cluster/initdb$"))' "$1" >/dev/null 2>&1; then
        echo "initdb"
    else
        echo "unknown"
    fi
}

file_lineage() {
    yq -r '.spec.postBuild.substitute.CNPG_LINEAGE // "v?"' "$1"
}

file_restore_from() {
    yq -r '.spec.postBuild.substitute.CNPG_RESTORE_FROM_LINEAGE // "v?"' "$1"
}

# Resolve <db>... or --all positional args against discovered files.
# Echoes one absolute path per requested DB, in deterministic order.
resolve_targets() {
    local arg
    local files=()
    local matched=()
    while IFS= read -r line; do files+=("$line"); done < <(discover_files)
    [ "${#files[@]}" -gt 0 ] || { echo "ERROR: no db-cnpg.yaml files discovered under apps/production/" >&2; exit 1; }

    for arg in "$@"; do
        if [ "$arg" = "--all" ]; then
            matched+=("${files[@]}")
            continue
        fi
        local found=0 f
        for f in "${files[@]}"; do
            if [ "$(file_to_app "$f")" = "$arg" ]; then
                matched+=("$f"); found=1; break
            fi
        done
        if [ "$found" -eq 0 ]; then
            echo "ERROR: unknown DB '$arg'. Known DBs:" >&2
            for f in "${files[@]}"; do echo "  $(file_to_app "$f")" >&2; done
            exit 1
        fi
    done
    # set -u guard: if matched is empty, don't expand
    if [ "${#matched[@]}" -gt 0 ]; then
        printf "%s\n" "${matched[@]}"
    fi
}

# ---------- atomic txn dir ----------------------------------------------------

setup_txn_dir() {
    # Codex pass-3 + pass-4: inside repo (linked-worktree safe), portable mktemp
    # (no -p flag — that's GNU-only and fails on macOS BSD mktemp).
    TXN_DIR=$(mktemp -d "$REPO_ROOT/.dr-flip-txn.XXXXXX")
    trap 'rm -rf "$TXN_DIR" 2>/dev/null || true' EXIT INT TERM ERR
}

# Stage a target file: copy original + write proposed new content. Returns the
# staged file path. Caller validates + atomic-moves into place later.
stage_edit() {
    local src="$1" mode="$2" lineage="$3" restore_from="$4"
    local sha
    # Portable sha256 (BSD shasum on macOS, GNU sha256sum on Linux).
    if command -v sha256sum >/dev/null 2>&1; then
        sha=$(printf "%s" "$src" | sha256sum | cut -d' ' -f1)
    else
        sha=$(printf "%s" "$src" | shasum -a 256 | cut -d' ' -f1)
    fi
    local staged="$TXN_DIR/staged-$sha.yaml"

    # Copy → edit on staged file, never the source.
    cp "$src" "$staged"

    # Edit 1: components[] entry whose value ends with /initdb or /recovery → swap.
    # Path depth-agnostic (Codex pass-1 Finding 4).
    if [ "$mode" = "recovery" ]; then
        yq -i '(.spec.components[] | select(. | test("/cnpg-cluster/initdb$"))) |= sub("/cnpg-cluster/initdb$"; "/cnpg-cluster/recovery")' "$staged"
    else
        yq -i '(.spec.components[] | select(. | test("/cnpg-cluster/recovery$"))) |= sub("/cnpg-cluster/recovery$"; "/cnpg-cluster/initdb")' "$staged"
    fi

    # Edit 2 + 3: CNPG_LINEAGE and CNPG_RESTORE_FROM_LINEAGE.
    yq -i ".spec.postBuild.substitute.CNPG_LINEAGE = \"$lineage\"" "$staged"
    yq -i ".spec.postBuild.substitute.CNPG_RESTORE_FROM_LINEAGE = \"$restore_from\"" "$staged"

    # Validate: re-parse the staged file to confirm it's still well-formed YAML
    # and the expected fields exist.
    yq -e '.spec.components | length > 0' "$staged" >/dev/null || {
        echo "ERROR: staged file lost .spec.components: $src" >&2; return 1
    }
    yq -e '.spec.postBuild.substitute.APP' "$staged" >/dev/null || {
        echo "ERROR: staged file lost .spec.postBuild.substitute.APP: $src" >&2; return 1
    }

    printf "%s\n" "$staged"
}

# Atomic move: rename each staged file over its source. Same-filesystem mv is
# atomic on POSIX.
commit_staging() {
    local i src staged
    for i in "${!STAGED_SRCS[@]}"; do
        src="${STAGED_SRCS[$i]}"
        staged="${STAGED_DSTS[$i]}"
        mv "$staged" "$src"
    done
}

# ---------- subcommands -------------------------------------------------------

cmd_status() {
    cat >&2 <<'EOF'
NOTE: Reflects Git working-tree state ONLY. Run `kubectl get cluster.postgresql.cnpg.io -A`
      to confirm against live cluster.

EOF
    printf "%-30s %-10s %-8s %-13s %s\n" "APP" "MODE" "LINEAGE" "RESTORE_FROM" "FILE"
    local f app mode lin rest rel
    while IFS= read -r f; do
        app=$(file_to_app "$f")
        mode=$(file_mode "$f")
        lin=$(file_lineage "$f")
        rest=$(file_restore_from "$f")
        rel=${f#"$REPO_ROOT"/}
        printf "%-30s %-10s %-8s %-13s %s\n" "$app" "$mode" "$lin" "$rest" "$rel"
    done < <(discover_files | sort)
}

cmd_enable() {
    local restore_from_override="" force_during_dr=0 args=()
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --restore-from-lineage) restore_from_override="$2"; shift 2 ;;
            --force-dr-during-dr)   force_during_dr=1; shift ;;
            --)                     shift; args+=("$@"); break ;;
            --all)                  args+=("$1"); shift ;;
            -*)                     echo "ERROR: unknown flag for enable: $1" >&2; exit 1 ;;
            *)                      args+=("$1"); shift ;;
        esac
    done
    [ "${#args[@]}" -gt 0 ] || { echo "ERROR: enable requires <db>... or --all" >&2; exit 1; }

    # Validate override against MAX_LINEAGE if given.
    if [ -n "$restore_from_override" ]; then
        if ! [[ "$restore_from_override" =~ ^v(0|[1-9][0-9]?)$ ]]; then
            echo "ERROR: --restore-from-lineage must match ^v[0-9]+\$, got '$restore_from_override'" >&2
            exit 1
        fi
        local n=${restore_from_override#v}
        if [ "$n" -gt "$MAX_LINEAGE" ]; then
            cat >&2 <<EOF
ERROR: --restore-from-lineage $restore_from_override is out of range. The recovery overlay
       pre-creates v0..v$MAX_LINEAGE. To use v$((n))+, extend the overlay file first
       (see docs/cnpg-disaster-recovery.md#extending-the-lineage-list).
EOF
            exit 1
        fi
    fi

    local targets
    targets=()
    while IFS= read -r line; do targets+=("$line"); done < <(resolve_targets "${args[@]}")

    setup_txn_dir
    STAGED_SRCS=(); STAGED_DSTS=()
    local f app cur_mode cur_lin new_lin from_lin staged

    for f in "${targets[@]}"; do
        app=$(file_to_app "$f")
        cur_mode=$(file_mode "$f")
        cur_lin=$(file_lineage "$f")

        # DR-during-DR guard (Codex pass-2 High).
        if [ "$cur_mode" = "recovery" ] && [ "$force_during_dr" -ne 1 ]; then
            cat >&2 <<EOF
ERROR: $app is already in recovery mode (lineage $cur_lin, restore-from $(file_restore_from "$f")).
       Bumping again would create the next lineage with restore-from $cur_lin,
       but $cur_lin may not have a base backup yet. The current recovery's settle step
       (immediate base backup + verify) must complete first.

If you UNDERSTAND the risk and the previous recovery's base backup IS verified present
in S3 for the current lineage, re-run with:
  dr-flip.sh enable --force-dr-during-dr $app
EOF
            exit 1
        fi

        # Compute new lineage (bump by 1).
        local n=${cur_lin#v}
        new_lin="v$((n + 1))"

        # Compute restore-from: override OR auto-compute (previous lineage).
        if [ -n "$restore_from_override" ]; then
            from_lin="$restore_from_override"
        else
            from_lin="$cur_lin"
        fi

        # Idempotence: if already in target state, skip (no-op).
        if [ "$cur_mode" = "recovery" ] && [ "$cur_lin" = "$new_lin" ]; then
            echo "already in recovery mode at $cur_lin: $app (no-op)" >&2
            continue
        fi

        staged=$(stage_edit "$f" "recovery" "$new_lin" "$from_lin")
        STAGED_SRCS+=("$f")
        STAGED_DSTS+=("$staged")
        echo "staged: $app → recovery / $new_lin / restore-from $from_lin" >&2
    done

    [ "${#STAGED_SRCS[@]}" -gt 0 ] || { echo "nothing to do" >&2; exit 0; }

    commit_staging
    echo "Flipped ${#STAGED_SRCS[@]} file(s). Review with \`git diff\` then commit + push." >&2
}

cmd_disable() {
    local skip_warning=0 skip_via_affirm=0 args=()
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --no-settle-warning)
                # Codex pass-4 Medium: gate on CI/BATS env (humans use --i-verified instead).
                if [ "${CI:-}" != "true" ] && [ "${BATS_TEST:-}" != "1" ]; then
                    echo "ERROR: --no-settle-warning is restricted to CI/BATS execution. Humans should use --i-verified-post-recovery-base-backup instead." >&2
                    exit 1
                fi
                skip_warning=1; shift ;;
            --i-verified-post-recovery-base-backup)
                skip_via_affirm=1; shift ;;
            --) shift; args+=("$@"); break ;;
            --all) args+=("$1"); shift ;;
            -*) echo "ERROR: unknown flag for disable: $1" >&2; exit 1 ;;
            *)  args+=("$1"); shift ;;
        esac
    done
    [ "${#args[@]}" -gt 0 ] || { echo "ERROR: disable requires <db>... or --all" >&2; exit 1; }

    # Banner once per invocation (Codex pass-3 Medium).
    if [ "$skip_warning" -eq 0 ] && [ "$skip_via_affirm" -eq 0 ]; then
        cat >&2 <<'EOF'
IMPORTANT POST-RECOVERY SETTLE GATE
Before disabling DR mode, verify each target DB's new lineage has a base backup:
  kubectl -n <ns> get backup -l cnpg.io/cluster=<app> --sort-by=.status.startedAt
  kubectl -n <ns> exec <app>-1 -c postgres -- barman-cloud-backup-list \
    --endpoint-url https://garage.lab.mainertoo.com \
    s3://volsync/cnpg/<app> <app>-v<current-lineage>
If the current lineage has no base backup, a future DR will read from a
hollow source. Trigger an immediate Backup CR first.

Full checklist: docs/cnpg-disaster-recovery.md#post-recovery-settle-checklist
EOF
        if test -t 0; then
            echo "Continuing in 3s — Ctrl+C to abort." >&2
            sleep 3
        else
            echo "(non-interactive shell; sleep skipped)" >&2
        fi
    fi

    local targets
    targets=()
    while IFS= read -r line; do targets+=("$line"); done < <(resolve_targets "${args[@]}")

    setup_txn_dir
    STAGED_SRCS=(); STAGED_DSTS=()
    local f app cur_mode cur_lin cur_rest staged

    for f in "${targets[@]}"; do
        app=$(file_to_app "$f")
        cur_mode=$(file_mode "$f")
        if [ "$cur_mode" = "initdb" ]; then
            echo "already in initdb mode: $app (no-op)" >&2
            continue
        fi
        cur_lin=$(file_lineage "$f")
        cur_rest=$(file_restore_from "$f")
        # Disable preserves lineage values (live cluster IS on that lineage).
        staged=$(stage_edit "$f" "initdb" "$cur_lin" "$cur_rest")
        STAGED_SRCS+=("$f")
        STAGED_DSTS+=("$staged")
        echo "staged: $app → initdb (lineage $cur_lin preserved)" >&2
    done

    [ "${#STAGED_SRCS[@]}" -gt 0 ] || { echo "nothing to do" >&2; exit 0; }

    commit_staging
    echo "Flipped ${#STAGED_SRCS[@]} file(s) back to initdb. Review with \`git diff\` then commit + push." >&2
}

usage() {
    cat <<EOF
Usage:
  dr-flip.sh enable  <db>...                          # flip + bump lineage to recovery mode
  dr-flip.sh enable  --all                            # flip + bump every CNPG DB to recovery
  dr-flip.sh enable  --force-dr-during-dr <db>...     # required when current mode is already recovery
  dr-flip.sh enable  --restore-from-lineage <vN> <db>... # override auto-computed restore-from (e.g. v0 escape hatch)
  dr-flip.sh disable <db>...                          # flip back to initdb mode (no lineage change)
  dr-flip.sh disable --all                            # flip every CNPG DB to initdb
  dr-flip.sh disable --i-verified-post-recovery-base-backup <db>...
                                                      # human affirmation; skip banner + sleep
  dr-flip.sh disable --no-settle-warning <db>...      # CI-only banner skip (gated on CI=true/BATS_TEST=1)
  dr-flip.sh status                                   # show working-tree mode + lineage
  dr-flip.sh -h | --help

Environment:
  MAX_LINEAGE is derived from the recovery overlay at runtime (currently $MAX_LINEAGE).

See docs/plans/cnpg-overlay-refactor.md for the full design + Codex review history.
See docs/cnpg-disaster-recovery.md for operational runbook.
EOF
}

# ---------- main dispatcher ---------------------------------------------------

[ "$#" -gt 0 ] || { usage; exit 1; }
case "$1" in
    -h|--help) usage; exit 0 ;;
    status)    shift; cmd_status "$@" ;;
    enable)    shift; cmd_enable "$@" ;;
    disable)   shift; cmd_disable "$@" ;;
    *) echo "ERROR: unknown subcommand '$1'" >&2; usage >&2; exit 1 ;;
esac
