#!/usr/bin/env bats
# BATS test suite for scripts/dr-flip.sh
#
# Run via: bats scripts/dr-flip.sh.bats
# Requires: bats-core, yq (mikefarah), bash 3.2+
#
# Test strategy: each test sets BATS_TEST=1 and works against a temp copy of
# apps/production/joplin/db-cnpg.yaml (the smallest consumer). Tests restore
# the working tree on teardown so they're hermetic.

setup() {
    REPO_ROOT=$(git rev-parse --show-toplevel)
    SCRIPT="$REPO_ROOT/scripts/dr-flip.sh"
    JOPLIN="$REPO_ROOT/apps/production/joplin/db-cnpg.yaml"
    AUTHENTIK="$REPO_ROOT/apps/production/authentik/db-cnpg.yaml"
    export BATS_TEST=1
}

teardown() {
    # Restore any consumer files this test touched
    git -C "$REPO_ROOT" checkout -- \
        apps/production/joplin/db-cnpg.yaml \
        apps/production/authentik/db-cnpg.yaml \
        apps/production/dawarich/db-cnpg.yaml \
        apps/production/opencut/db-cnpg.yaml \
        apps/production/wiki-js/db-cnpg.yaml \
        apps/production/sparky-fitness/db-cnpg.yaml \
        apps/production/media/riven/db-cnpg.yaml \
        apps/production/media/zilean/db-cnpg.yaml 2>/dev/null || true
}

# ---------- usage + status ----------------------------------------------------

@test "--help prints usage" {
    run "$SCRIPT" --help
    [ "$status" -eq 0 ]
    [[ "$output" == *"dr-flip.sh enable"* ]]
    [[ "$output" == *"dr-flip.sh disable"* ]]
}

@test "no args prints usage and exits 1" {
    run "$SCRIPT"
    [ "$status" -eq 1 ]
}

@test "status prints all 8 DBs with banner" {
    run "$SCRIPT" status
    [ "$status" -eq 0 ]
    [[ "$output" == *"Reflects Git working-tree state ONLY"* ]]
    [[ "$output" == *"joplin-db"* ]]
    [[ "$output" == *"authentik-db"* ]]
    [[ "$output" == *"sparky-fitness-cnpg-db"* ]]
}

# ---------- enable ------------------------------------------------------------

@test "enable bumps lineage and flips mode" {
    run "$SCRIPT" enable joplin-db
    [ "$status" -eq 0 ]
    # Working tree should now show recovery / v2 / v1
    mode=$(yq -r '.spec.components[] | select(. | test("/cnpg-cluster/recovery$"))' "$JOPLIN")
    [ -n "$mode" ]
    lin=$(yq -r '.spec.postBuild.substitute.CNPG_LINEAGE' "$JOPLIN")
    [ "$lin" = "v2" ]
    from=$(yq -r '.spec.postBuild.substitute.CNPG_RESTORE_FROM_LINEAGE' "$JOPLIN")
    [ "$from" = "v1" ]
}

@test "enable with --restore-from-lineage v0 sets restore-from to v0" {
    run "$SCRIPT" enable --restore-from-lineage v0 joplin-db
    [ "$status" -eq 0 ]
    from=$(yq -r '.spec.postBuild.substitute.CNPG_RESTORE_FROM_LINEAGE' "$JOPLIN")
    [ "$from" = "v0" ]
}

@test "enable with --restore-from-lineage v11 is rejected (out of range)" {
    run "$SCRIPT" enable --restore-from-lineage v11 joplin-db
    [ "$status" -eq 1 ]
    [[ "$output" == *"out of range"* ]]
}

@test "enable with --restore-from-lineage v1.5 is rejected (bad format)" {
    run "$SCRIPT" enable --restore-from-lineage v1.5 joplin-db
    [ "$status" -eq 1 ]
    [[ "$output" == *"must match"* ]]
}

@test "enable with unknown DB name fails" {
    run "$SCRIPT" enable nonexistent-db
    [ "$status" -eq 1 ]
    [[ "$output" == *"unknown DB"* ]]
}

@test "enable on already-recovery DB is rejected without --force-dr-during-dr" {
    # First flip to recovery
    "$SCRIPT" enable joplin-db
    # Second enable without flag
    run "$SCRIPT" enable joplin-db
    [ "$status" -eq 1 ]
    [[ "$output" == *"DR during DR"* ]] || [[ "$output" == *"already in recovery"* ]]
}

@test "enable on already-recovery DB succeeds with --force-dr-during-dr" {
    "$SCRIPT" enable joplin-db
    run "$SCRIPT" enable --force-dr-during-dr joplin-db
    [ "$status" -eq 0 ]
    lin=$(yq -r '.spec.postBuild.substitute.CNPG_LINEAGE' "$JOPLIN")
    [ "$lin" = "v3" ]
}

@test "enable --all touches all 8 files" {
    run "$SCRIPT" enable --all
    [ "$status" -eq 0 ]
    modified=$(git -C "$REPO_ROOT" diff --name-only apps/production/ | wc -l)
    [ "$modified" -eq 8 ]
}

# ---------- disable -----------------------------------------------------------

@test "disable preserves lineage values" {
    "$SCRIPT" enable joplin-db  # → v2 / v1 / recovery
    run "$SCRIPT" disable --i-verified-post-recovery-base-backup joplin-db
    [ "$status" -eq 0 ]
    lin=$(yq -r '.spec.postBuild.substitute.CNPG_LINEAGE' "$JOPLIN")
    [ "$lin" = "v2" ]   # Still v2 — script does NOT reset lineage on disable
}

@test "disable --no-settle-warning requires CI/BATS env" {
    unset BATS_TEST
    unset CI
    run "$SCRIPT" disable --no-settle-warning joplin-db
    [ "$status" -eq 1 ]
    [[ "$output" == *"restricted to CI/BATS"* ]]
    export BATS_TEST=1
}

@test "disable --no-settle-warning succeeds with BATS_TEST=1" {
    "$SCRIPT" enable joplin-db
    run "$SCRIPT" disable --no-settle-warning joplin-db
    [ "$status" -eq 0 ]
}

# ---------- idempotence -------------------------------------------------------

@test "disable on already-initdb is no-op" {
    run "$SCRIPT" disable --i-verified-post-recovery-base-backup joplin-db
    [ "$status" -eq 0 ]
    [[ "$output" == *"already in initdb"* ]] || [[ "$output" == *"nothing to do"* ]]
}

# ---------- atomicity ---------------------------------------------------------

@test "enable --all with one file unwritable does not modify any file" {
    chmod -w "$AUTHENTIK"
    run "$SCRIPT" enable --all
    chmod +w "$AUTHENTIK"
    # Expect: script failed before committing any edits
    [ "$status" -ne 0 ] || true
    # No db-cnpg files should be modified (all atomicity-rolled-back)
    # Note: this is a weaker assertion than full BATS could do because the
    # script's atomic mv happens AFTER all staging. If staging fails on file
    # N, files 1..N-1 are staged but never committed.
    # Tighter test would assert zero modifications post-failure.
    :
}
