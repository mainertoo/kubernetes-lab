# CNPG Restore Ergonomics Refactor (planning doc)

> **✅ SHIPPED — implemented in PR #518 (merged 2026-05-20):** `scripts/dr-flip.sh` (+ `scripts/dr-flip.sh.bats`), the `v0..v11` lineage `externalClusters[]` entries in `components/cnpg-cluster/recovery/bootstrap-patch.yaml`, and the CI gates (`.github/workflows/cnpg-overlay-guard.yml`, `cnpg-cleanup-attestation.yml`). This doc is retained as the cited design + Codex-review reference (see `dr-flip.sh`, the cnpg CI workflows, and the `cnpg-v0-cleanup` PR template); the version history below is preserved for that purpose.
>
> **Status (historical):** v7 — Codex review pass 6 incorporated 2026-05-20. Pending pass 7 for convergence verdict.
>
> **v7 changes** (Codex pass-6: 2 High; both are v6 internal inconsistencies, not new design):
> - §6: High — "escape hatch is automatic" claim WAS WRONG. The `--restore-from-lineage v0` flag is mandatory during migration window; v7 corrects the contradiction with §4 script spec and adds an explicit invocation example.
> - §12: High — checklist line claimed "CI enforces this" which contradicts §6's "automation aid, not enforcement" framing. v7 aligns the language; both sections now say the same thing.
>
> **v6 changes** (Codex pass-5: 1 High + 1 Medium + 1 Low; Codex explicitly noted "v6 needs only one substantive fix... after which the plan should be SAFE TO IMPLEMENT"):
> - §6 attestation-freshness: High — corrected the false claim that CI enforces merge-time freshness. GitHub required checks don't re-run on merge; a check that passed at T+23.5h satisfies branch protection at T+25h. v6 honestly scopes this as a human gate with an explicit reviewer re-trigger instruction immediately before merge.
> - §4 MAX_LINEAGE: Medium — derive at runtime from `components/cnpg-cluster/recovery/bootstrap-patch.yaml` (yq + sed parse) instead of hardcoded `10`. Prevents two-file drift when the overlay is extended.
> - Appendix C arithmetic: Low — finding counts corrected. "Verbatim" claim retitled to "summary" since the appendices are synthesized rather than raw paste.
>
> **v5 changes** (Codex pass-4 Highs were all new additions introduced by v4's own fixes — Codex's trajectory note confirms convergence):
> - §4 `--restore-from-lineage` validation: High — adds upper-bound check against MAX_LINEAGE (= 10, matching pre-created overlay entries). `v11` now rejected explicitly.
> - §6 merge-time evidence gate: High — replaced fictional self-hosted runner CI job with documented manual checklist + branch-protection-required approval checkbox in PR template. Honesty about repo capability; no in-cluster kubectl from GitHub Actions exists.
> - §4 transaction dir: High — switched from `mktemp -d -p` (GNU coreutils only, FAILS on macOS) to portable template form `mktemp -d "$REPO_ROOT/.dr-flip-txn.XXXXXX"`.
> - §4 `--no-settle-warning`: Medium — gated on `CI=true` OR `BATS_TEST=1` env var; rejected with hint when human runs it manually.
> - §4 `disable` banner sleep: Medium — TTY detection (`test -t 0`); skip sleep when stdin non-interactive.
> - §6 attestation grep: Low — case-insensitive regex with lineage capture (`grep -Eiq 'side-by-side restore from v[0-9]+ verified'`). Moot if §6 evidence-gate is now manual but kept for the optional CI variant.
> - Appendix B + C: Low — pass-2 and pass-3 Codex findings appended as summaries (was: dangling references). v6 corrects "verbatim" → "summary" wording for accuracy.
>
> **v4 changes** (Codex pass-3 findings; see Appendix C):
> - §4: Critical — `dr-flip.sh enable --restore-from-lineage <vN>` override added so the v0 escape hatch is actually reachable during the migration window (was: enable auto-computed restore-from = current lineage, bypassing v0).
> - §6, §12: High — evidence-based cleanup re-verification is now required at MERGE time, not just PR-open time. Implemented as a blocking CI job that re-runs the three evidence checks against live cluster state.
> - §8 step 11: High — CI gate awk fixed for R-classified renames (`$3` not `$2`); concrete command updated to honor the stated `--diff-filter=AMR` policy.
> - §8 step 12: Medium — admission test uses full v0..v10 entry shape (was: only 3 entries).
> - §8 NEW step 13: Medium — render assertion that all 11 entries (v0..v10) appear in each rendered Cluster with correct serverName.
> - §4 txn dir: Medium — moved from `.git/dr-flip-txn.XXXXXX` to `$REPO_ROOT/.dr-flip-txn.XXXXXX` (linked-worktree safe) + `.gitignore` entry.
> - §4 banner: Medium — emits once per invocation (not per-app); usage block lists `--no-settle-warning` (CI-only) AND `--i-verified-post-recovery-base-backup` (human affirmation alternative).
> - §4 banner reference pinned: Medium — `runbook §X` resolved to `docs/cnpg-disaster-recovery.md#post-recovery-settle-checklist`.
>
> **v3 changes** (Codex pass-2 findings; see Appendix B — to be appended):
> - §3, §4, §11: Critical — recovery overlay now pre-creates v0..v10 externalClusters[] entries (was: hardcoded v0/v1/v2, would silently break at v3+). Avoids dynamic-list editing complexity.
> - §4, §6, §12: High — `dr-flip.sh disable` warns about post-recovery settle gate; runbook documents the manual "create + verify base backup at new lineage BEFORE disable" requirement.
> - §6, §12: High — T+30d v0 cleanup converted from time-based to evidence-based (verify all 8 have v1+ base backups + WAL continuity + side-by-side restore test from v1+ before removing v0).
> - §4 vs §5: regression — BATS contradiction resolved. BATS is required.
> - §8 step 5: regression — `status --all` flag dropped (script defaults to showing all).
> - §7 rollback duplication removed.
> - §8: CI gate implementation tightened — `github.base_ref` usage, `--diff-filter=AMR`, per-file yq runs.
> - §4: transaction dir moved into repo tree (`.git/dr-flip-txn.XXXX`) for cross-filesystem mv atomicity.
> - §8: pre-merge admission test added — multi-entry externalClusters[] with unreferenced entries pointing at non-existent Secrets, confirm no CNPG validation error.
>
> **v2 changes** (Codex pass-1 findings; see Appendix A):
> - §3, §6: default v0 emergency restore is now Option α (single ObjectStore + secondary `externalClusters[]` with serverName=${APP}). Codex Finding 11 corrected my wrong premise about ObjectStore↔serverName encoding.
> - §4: script switches from bash + sed to bash + yq. Adds `--force-dr-during-dr` guard, atomic temp-file write path, `status` label clarity. Codex Findings 4, 5, 6, 12.
> - §5: bumps `cnpg-disaster-recovery.md` `${APP%-db}` path-expansion fix into THIS PR (was a pre-existing bug). Codex Finding 8.
> - §7, §8: render full Flux Kustomizations per-app in pre-merge dyff diff, not just the shared Component. Add sparky-fitness as canonical app-specific-patch test case. Codex Findings 1, 2, 9.
> - §8: CI gates added — base-without-overlay rejection, recovery-mode-on-new-app guard, per-app render assertions. Codex Findings 3, 7.
> - §10: rationale documented for per-DB flag over global DR switch. Codex Finding 10.
> - §12 (new): future-Kyverno-secret-timing risk documented as a constraint. Codex Finding 11 self-check.
>
> **Context:** PRs #510/#511/#512/#513/#514 shipped CNPG plugin-spec recovery,
> validated against joplin-db (vanilla pg16), PITR (joplin-db), and dawarich-db
> (postgis 17 / 5 GiB). Cluster-nuke promise on the volsync/Kopia side is
> "paste age key from 1Password → flux bootstrap → ~10 min to data-restored,
> zero git edits during recovery." CNPG path does NOT match — operator must
> edit 8 `apps/production/<app>/db-cnpg.yaml` files to swap the `components:`
> reference from `cnpg-cluster` → `cnpg-cluster/recovery`.
>
> **What this plan addresses:** make the CNPG path operationally symmetric
> (one operator action, one PR, ~minutes) by adopting the
> [mitchross/talos-argocd-proxmox](https://github.com/mitchross/talos-argocd-proxmox)
> per-DB overlay structure + a thin `scripts/dr-flip.sh` helper.

---

## 1. Goal and non-goals

### Goal

Cluster-nuke CNPG recovery becomes:

```
1. Restore SOPS-age key from 1Password
2. ./scripts/dr-flip.sh enable --all
3. git commit && git push          # ONE PR, auto-touches 8 files
4. flux bootstrap
5. ~10 min later: 8/8 healthy on restored data
6. ./scripts/dr-flip.sh disable --all
7. git commit && git push          # settle PR
```

Symmetric to volsync/Kopia operational shape.

### Non-goals (explicit)

- **Auto-detection via Kyverno admission mutate.** Mitchross deliberately
  punted on this; rationale captured in his
  [`cnpg-explained.md`](https://github.com/mitchross/talos-argocd-proxmox/blob/1f6ab9146431a137965de0b853b3a7526a25bf57/docs/cnpg-explained.md#L137):
  manual flag forces operator to verify backup health + pick the right
  lineage. We agree.
- **Changing the volsync/Kopia side.** PVC restore via label-driven admission
  is already as good as it gets.
- **CNPG management UI/wizard.** A 5-line bash script is the right tool for
  ~1× / year cluster nuke. Anything heavier is overkill.
- ~~Migration to lineage-versioned `serverName: <app>-vN`.~~ **BUNDLED**
  per 2026-05-19 design review. See §6 for the full plan + migration risk window.

---

## 2. Current state vs target state

### Current (post-PR #512)

```
components/cnpg-cluster/
├── README.md
├── kustomization.yaml          (Component: cluster.yaml + objectstore.yaml + s3-secret.yaml + scheduledbackup.yaml)
├── cluster.yaml                ← FULL Cluster spec WITH spec.bootstrap.initdb baked in
├── objectstore.yaml            (ObjectStore CR)
├── s3-secret.yaml              (Secret)
├── scheduledbackup.yaml        (ScheduledBackup with method: plugin)
└── recovery/
    ├── kustomization.yaml      (Component: cluster.yaml + ../objectstore.yaml + ../s3-secret.yaml + ../scheduledbackup.yaml)
    └── cluster.yaml            ← FULL Cluster spec WITH spec.bootstrap.recovery + externalClusters baked in

apps/production/<app>/db-cnpg.yaml      (Flux Kustomization)
  spec.components: [../../../components/cnpg-cluster]              ← swap this path for DR
                OR [../../../components/cnpg-cluster/recovery]
```

Two complete Cluster manifests. Recovery operator edits the `components:` list
in each of 8 files.

### Target (after this refactor)

```
components/cnpg-cluster/
├── README.md
├── kustomization.yaml          (NO LONGER a Component — drops to just a pointer; see below)
├── base/
│   ├── kustomization.yaml      (Component: cluster.yaml + objectstore.yaml + s3-secret.yaml + scheduledbackup.yaml)
│   ├── cluster.yaml            ← Cluster spec WITHOUT spec.bootstrap (single source of truth for image, storage, plugins, resources)
│   ├── objectstore.yaml        (unchanged)
│   ├── s3-secret.yaml          (unchanged)
│   └── scheduledbackup.yaml    (unchanged)
├── initdb/
│   ├── kustomization.yaml      (Component: just bootstrap-patch.yaml)
│   └── bootstrap-patch.yaml    ← strategic-merge patch ADDING spec.bootstrap.initdb
└── recovery/
    ├── kustomization.yaml      (Component: just bootstrap-patch.yaml)
    └── bootstrap-patch.yaml    ← strategic-merge patch ADDING spec.bootstrap.recovery + externalClusters

apps/production/<app>/db-cnpg.yaml
  spec.components:
    - ../../../components/cnpg-cluster/base
    - ../../../components/cnpg-cluster/initdb       ← THIS LINE is the feature flag
                                                       (swap "initdb" → "recovery" for DR)
```

One Cluster spec (in `base/`), two thin overlay patches that ADD the appropriate
`spec.bootstrap` block. Each app picks `initdb` OR `recovery` via the second
`components:` entry. The flag is one word per file.

---

## 3. File-by-file delta — example: joplin

### `components/cnpg-cluster/base/cluster.yaml` (NEW — moved from cluster.yaml)

Same as today's `components/cnpg-cluster/cluster.yaml` MINUS the `bootstrap`
block. Everything else (image, storage, plugins, resources, postgresql params)
unchanged.

### `components/cnpg-cluster/base/kustomization.yaml` (NEW — moved)

```yaml
---
apiVersion: kustomize.config.k8s.io/v1alpha1
kind: Component
resources:
  - cluster.yaml
  - objectstore.yaml
  - s3-secret.yaml
  - scheduledbackup.yaml
```

### `components/cnpg-cluster/initdb/bootstrap-patch.yaml` (NEW)

```yaml
---
# Strategic-merge patch — adds spec.bootstrap.initdb to the base Cluster.
# Consumed via the initdb Component. Required substitutions:
#   CNPG_DB_NAME, CNPG_DB_OWNER (same as today's base Component).
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: ${APP}
spec:
  bootstrap:
    initdb:
      database: ${CNPG_DB_NAME}
      owner: ${CNPG_DB_OWNER}
```

### `components/cnpg-cluster/base/cluster.yaml` — lineage substitution

The base Cluster's plugin parameters become lineage-aware:

```yaml
spec:
  plugins:
    - name: barman-cloud.cloudnative-pg.io
      isWALArchiver: true
      parameters:
        barmanObjectName: ${APP}-store
        # New: lineage-suffixed serverName. Default v1. dr-flip.sh bumps this on
        # every recovery; new WAL writes go to a fresh S3 prefix.
        serverName: ${APP}-${CNPG_LINEAGE:=v1}
```

S3 path becomes `s3://volsync/cnpg/${APP}/${APP}-v1/{base,wals}/...` (per-app
prefix unchanged; lineage appended inside).

### `components/cnpg-cluster/base/objectstore.yaml` — UNCHANGED

Critical clarification from Codex Finding 11: the `ObjectStore.spec.configuration.destinationPath`
is the per-app BUCKET PREFIX only — `s3://volsync/cnpg/${APP}`. The `serverName`
segment in the actual S3 path (`<destinationPath>/<serverName>/{base,wals}/...`)
is appended by the Barman plugin from the externalCluster's
`plugin.parameters.serverName` field, NOT from the ObjectStore resource.

**Consequence:** ONE ObjectStore per app is sufficient. Different `serverName`
values resolve to different sub-paths under the same parent prefix. The v0
emergency-restore path uses the SAME `${APP}-store` ObjectStore with a different
`serverName` value (no `-vN` suffix). No second ObjectStore needed.

The existing `${APP}-store` ObjectStore (created by PR #512) is reused verbatim.

### `components/cnpg-cluster/initdb/kustomization.yaml` (NEW)

```yaml
---
apiVersion: kustomize.config.k8s.io/v1alpha1
kind: Component
patches:
  - path: bootstrap-patch.yaml
    target:
      group: postgresql.cnpg.io
      kind: Cluster
      name: ${APP}
```

### `components/cnpg-cluster/recovery/bootstrap-patch.yaml` (REWRITTEN — was cluster.yaml)

```yaml
---
# Strategic-merge patch — adds spec.bootstrap.recovery + externalClusters to the
# base Cluster. Consumed via the recovery Component for DR.
# Substitutions:
#   APP                       cluster name (same as base)
#   APP_RESTORE_FROM          source app (defaults to ${APP}; override for side-by-side)
#   CNPG_RESTORE_FROM_LINEAGE source lineage to restore FROM. v0 = pre-refactor
#                             unversioned barman prefix; v1, v2, ... = post-refactor
#                             lineages. dr-flip.sh keeps this aligned with the
#                             current CNPG_LINEAGE in base.
#
# NOTE: bootstrap.recovery.source NAMES one of the externalClusters[] entries
# below. The name is a logical identifier WITHIN the Cluster spec — it does NOT
# encode the barman serverName. The actual S3 lookup uses each entry's
# plugin.parameters.serverName, which is what differentiates v0 from vN.
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: ${APP}
spec:
  bootstrap:
    recovery:
      source: ${APP_RESTORE_FROM:=${APP}}-restore-${CNPG_RESTORE_FROM_LINEAGE:=v0}

  externalClusters:
    # ---- v0 = pre-refactor unversioned prefix (emergency only) ----
    # Plugin appends serverName WITHOUT the -vN suffix:
    #   s3://volsync/cnpg/${APP_RESTORE_FROM}/${APP_RESTORE_FROM}/{base,wals}/
    # This is where ScheduledBackups landed BEFORE this refactor merged.
    # Removed in evidence-gated cleanup PR (see §12) once all clusters have ≥1
    # base backup at v1+ and a successful v1+ restore test.
    - name: ${APP_RESTORE_FROM:=${APP}}-restore-v0
      plugin:
        name: barman-cloud.cloudnative-pg.io
        parameters:
          barmanObjectName: ${APP_RESTORE_FROM:=${APP}}-store
          serverName: ${APP_RESTORE_FROM:=${APP}}

    # ---- vN lineage restore — PRE-CREATED v1..v10 ----
    # Per Codex pass-2 Critical finding: dynamic-list editing per DR would
    # require dr-flip.sh to mutate this shared file on every enable. Easier
    # and safer to pre-create N entries up front; CNPG ignores unreferenced
    # entries (verified by §8 admission test). At v11+ extend the list in a
    # small follow-up PR. For a homelab with ~1× / year DR cadence per
    # cluster, v10 = ~10 years of safety.
    #
    # Each entry templates with the per-app ${APP_RESTORE_FROM} at render
    # time, so joplin-db sees joplin-db-restore-v1 etc. and dawarich-db
    # sees dawarich-db-restore-v1 etc. The unreferenced ones are inert.
    - name: ${APP_RESTORE_FROM:=${APP}}-restore-v1
      plugin:
        name: barman-cloud.cloudnative-pg.io
        parameters:
          barmanObjectName: ${APP_RESTORE_FROM:=${APP}}-store
          serverName: ${APP_RESTORE_FROM:=${APP}}-v1
    - name: ${APP_RESTORE_FROM:=${APP}}-restore-v2
      plugin:
        name: barman-cloud.cloudnative-pg.io
        parameters:
          barmanObjectName: ${APP_RESTORE_FROM:=${APP}}-store
          serverName: ${APP_RESTORE_FROM:=${APP}}-v2
    - name: ${APP_RESTORE_FROM:=${APP}}-restore-v3
      plugin:
        name: barman-cloud.cloudnative-pg.io
        parameters:
          barmanObjectName: ${APP_RESTORE_FROM:=${APP}}-store
          serverName: ${APP_RESTORE_FROM:=${APP}}-v3
    - name: ${APP_RESTORE_FROM:=${APP}}-restore-v4
      plugin:
        name: barman-cloud.cloudnative-pg.io
        parameters:
          barmanObjectName: ${APP_RESTORE_FROM:=${APP}}-store
          serverName: ${APP_RESTORE_FROM:=${APP}}-v4
    - name: ${APP_RESTORE_FROM:=${APP}}-restore-v5
      plugin:
        name: barman-cloud.cloudnative-pg.io
        parameters:
          barmanObjectName: ${APP_RESTORE_FROM:=${APP}}-store
          serverName: ${APP_RESTORE_FROM:=${APP}}-v5
    # v6..v10 follow the same pattern; omitted here for brevity. Full text
    # lands in components/cnpg-cluster/recovery/bootstrap-patch.yaml.
```

**Single ObjectStore is reused across all lineages.** Codex Finding 11
clarified that `serverName` is appended by the plugin from the externalCluster's
plugin parameters, not from the ObjectStore. Different serverName values yield
different `<destinationPath>/<serverName>/...` sub-paths.

**Pre-create v0..v10 entries (Codex pass-2 Critical fix).** dr-flip.sh
selects which entry to use via the `CNPG_RESTORE_FROM_LINEAGE` substitution.
Unreferenced entries are inert (verified pre-merge in §8). At v11+ the
operator extends the recovery overlay file in a small PR; rare enough for a
homelab that pre-emptive automation isn't worth it.

`CNPG_RESTORE_FROM_LINEAGE: v0` (the default after initial migration) selects
the pre-refactor backup path. After the evidence-gated cleanup (§12) lands,
the default becomes `v1` and the `-restore-v0` entry is removed.

### `components/cnpg-cluster/recovery/kustomization.yaml` (REWRITTEN)

```yaml
---
apiVersion: kustomize.config.k8s.io/v1alpha1
kind: Component
patches:
  - path: bootstrap-patch.yaml
    target:
      group: postgresql.cnpg.io
      kind: Cluster
      name: ${APP}
```

### `apps/production/joplin/db-cnpg.yaml` (UPDATED — two blocks changed)

**`components:` list:**
```yaml
  components:
    - ../../../components/cnpg-cluster/base
    - ../../../components/cnpg-cluster/initdb     # ← swap "initdb" → "recovery" for DR
```

**`postBuild.substitute:` adds 2 lineage values:**
```yaml
  postBuild:
    substitute:
      APP: joplin-db
      # ... (all existing substitutions unchanged) ...

      # NEW: lineage values. dr-flip.sh edits these in place.
      CNPG_LINEAGE: v1                         # ← current write target — bumped on every DR
      CNPG_RESTORE_FROM_LINEAGE: v0            # ← lineage to restore FROM — set on every DR
```

Same change to the other 7 `db-cnpg.yaml` files. Initial state for all: `CNPG_LINEAGE=v1`, `CNPG_RESTORE_FROM_LINEAGE=v0`.

### `components/cnpg-cluster/cluster.yaml`, `objectstore.yaml`, `s3-secret.yaml`, `scheduledbackup.yaml` (DELETED — moved into base/)

The top-level Component is removed entirely. There's no longer a "default"
Component at `components/cnpg-cluster` — consumers must pick `base/` + an
overlay explicitly. The README spells out the contract.

---

## 4. `scripts/dr-flip.sh` spec

### Usage

```
dr-flip.sh enable  <db>...                          # flip + bump lineage to recovery mode
dr-flip.sh enable  --all                            # flip + bump every CNPG DB to recovery mode
dr-flip.sh enable  --force-dr-during-dr <db>...     # required when current mode is already recovery
dr-flip.sh enable  --restore-from-lineage <vN> <db>... # override auto-computed restore-from (e.g. v0 escape hatch)
dr-flip.sh disable <db>...                          # flip back to initdb mode (no lineage change)
dr-flip.sh disable --all                            # flip every CNPG DB to initdb
dr-flip.sh disable --i-verified-post-recovery-base-backup <db>...  # skip banner via human affirmation
dr-flip.sh disable --no-settle-warning <db>...      # CI-only banner skip
dr-flip.sh status                                   # show working-tree mode + lineage
dr-flip.sh -h | --help
```

### Implementation language: bash + yq

**Changed from bash + sed in v1** based on Codex Finding 4. Two of the eight
`db-cnpg.yaml` files (`wiki-js`, `sparky-fitness`) use a different relative
path depth (`../../../../components/cnpg-cluster` vs the others'
`../../../components/cnpg-cluster`). A naive sed expression targeting
`../../../components/cnpg-cluster/...` silently misses 2 of 8 files.

yq handles this by editing structured fields, ignoring path prefixes. The
script edits:

- `.spec.components[] | select(. | test("/cnpg-cluster/(initdb|recovery)$"))` → swap terminal segment
- `.spec.postBuild.substitute.CNPG_LINEAGE` → bump
- `.spec.postBuild.substitute.CNPG_RESTORE_FROM_LINEAGE` → set to pre-bump value

Adds `yq` as a script runtime dependency. Already used in
[`scripts/cluster-thermals.sh`](../../scripts/cluster-thermals.sh) and elsewhere; in the repo's idiom.

### Behavior

- **Discovery:** finds all CNPG DBs by `rg --files apps/production | rg
  'db-cnpg.yaml$'` and filtering to files whose `.spec.components[]` contains
  a `cnpg-cluster/` path. Path depth is irrelevant (Codex Finding 4).
- **Identification:** `yq '.spec.postBuild.substitute.APP'` per file gives
  the canonical DB name. Script accepts these names as `<db>` positional
  args.
- **Edit on `enable` — three coordinated yq writes per file:**
  1. Swap the matching `components[]` entry's terminal segment (`initdb` → `recovery`)
  2. Set `CNPG_RESTORE_FROM_LINEAGE` to the current `CNPG_LINEAGE` value
     - **UNLESS** `--restore-from-lineage <vN>` is passed, in which case use that explicit value
  3. Bump `CNPG_LINEAGE` to `v(N+1)`

  **The `--restore-from-lineage <vN>` override addresses Codex pass-3 Critical:**
  the default auto-compute would bypass the v0 escape hatch during the migration
  window. To restore from the pre-refactor unversioned backups, the operator runs:
  `dr-flip.sh enable --restore-from-lineage v0 joplin-db`.

  **Validation (Codex pass-4 High-1 + pass-5 Medium-1 fix):** value must match
  `^v(0|[1-9][0-9]?)$` AND the numeric portion must be ≤ `MAX_LINEAGE`.
  `MAX_LINEAGE` is **derived at runtime** from the overlay file (NOT hardcoded —
  prevents future two-file drift if the overlay is extended to v11+):

  ```bash
  RECOVERY_PATCH="$REPO_ROOT/components/cnpg-cluster/recovery/bootstrap-patch.yaml"
  MAX_LINEAGE=$(
    yq -r '.spec.externalClusters[].name // ""' "$RECOVERY_PATCH" \
      | sed -nE 's/.*-restore-v([0-9]+)$/\1/p' \
      | sort -n | tail -1
  )
  : "${MAX_LINEAGE:?no restore lineage entries found in $RECOVERY_PATCH}"
  ```

  `v(MAX_LINEAGE+1)` is rejected with:
  ```text
  ERROR: --restore-from-lineage v11 is out of range. The recovery overlay
         pre-creates v0..v10. To use v11+, extend the overlay file first
         (see docs/cnpg-disaster-recovery.md#extending-the-lineage-list).
  ```
  BATS coverage parameterized on MAX_LINEAGE: `v0` ✓, `v(MAX_LINEAGE)` ✓,
  `v(MAX_LINEAGE+1)` rejected with exit 1, `v1.5` rejected, empty value rejected.
  Tests don't break when the overlay is extended.
- **Edit on `disable` — one yq write per file, plus a settle-gate reminder banner:**
  1. Swap the matching `components[]` entry (`recovery` → `initdb`)
  Lineage values stay at their bumped state (the live cluster is on that
  lineage; spec MUST reflect that).

  Banner emitted ONCE per invocation (not per-app; Codex pass-3 Medium), to stderr, before any edit:

  ```text
  IMPORTANT POST-RECOVERY SETTLE GATE
  Before disabling DR mode, verify each target DB's new lineage has a base backup:
    kubectl -n <ns> get backup -l cnpg.io/cluster=<app> --sort-by=.status.startedAt
    kubectl -n <ns> exec <app>-1 -c postgres -- barman-cloud-backup-list \
      --endpoint-url https://garage.lab.mainertoo.com \
      s3://volsync/cnpg/<app> <app>-v<current-lineage>
  If the current lineage has no base backup, a future DR will read from a
  hollow source. Trigger an immediate Backup CR first.

  Full checklist: docs/cnpg-disaster-recovery.md#post-recovery-settle-checklist

  Continuing in 3s — Ctrl+C to abort.
  ```

  **Skip options:**
  - `--i-verified-post-recovery-base-backup` — human affirmation. Operator typed
    the explicit flag, banner skipped, no delay. Preferred for normal use.
  - `--no-settle-warning` — CI-only mechanical bypass. **Gated on `CI=true` OR
    `BATS_TEST=1` env var** (Codex pass-4 Medium-1 fix). When invoked by a
    human (env vars unset), the script rejects with:
    ```
    ERROR: --no-settle-warning is restricted to CI/BATS execution. Humans
           should use --i-verified-post-recovery-base-backup instead.
    ```

  **TTY-aware sleep (Codex pass-4 Medium-2 fix):** the 3-second delay only fires
  when stdin is a TTY (`test -t 0`). For piped/cron/non-interactive invocations
  without an explicit skip flag, the banner still prints (audit trail) but no
  sleep — Ctrl+C wouldn't be reachable anyway. Behavior matrix:

  | stdin | skip flag | Banner | Sleep |
  |---|---|---|---|
  | TTY | none | yes | 3s |
  | TTY | `--i-verified-...` | no | 0s |
  | non-TTY | none | yes (audit) | 0s |
  | non-TTY | `--no-settle-warning` + CI=true | no | 0s |
- **Idempotent (local-file scope):** running `enable joplin-db` when the
  working-tree file already shows `recovery` mode and a higher-than-baseline
  lineage exits 0 with `"already in recovery mode at v2"`. The script does
  NOT consult the live cluster (see Codex Finding 12 + Status banner below).

### DR-during-DR guard (Codex Finding 6)

If `enable <db>` is invoked while the working-tree file already shows
`recovery` mode, the script exits 1 with:

```
ERROR: joplin-db is already in recovery mode (lineage v2, restore-from v1).
       Bumping again would create lineage v3 with restore-from v2, but v2 may
       not have a base backup yet. The current recovery's settle step (immediate
       base backup + verify) must complete first.

If you UNDERSTAND the risk and the previous recovery's base backup IS
verified present in S3 for the current lineage, re-run with:
  dr-flip.sh enable --force-dr-during-dr joplin-db
```

The `--force-dr-during-dr` flag is per-DB; cannot combine with `--all`.

### Atomicity — actually designed (Codex Finding 5)

The atomicity guarantee was asserted in v1 but not designed. v2 implementation
uses a transaction directory:

```bash
# Create txn dir INSIDE the repo working tree (not /var/folders, not .git)
# so the final `mv` is always within the same filesystem AND is safe under
# linked-worktree layouts where .git is a file. Codex pass-2 + pass-3 + pass-4
# Medium/High findings. Path is git-ignored.
#
# IMPORTANT: portable template form. `mktemp -d -p <dir>` is GNU coreutils
# only and FAILS on macOS BSD mktemp (the operator's dev platform).
REPO_ROOT=$(git rev-parse --show-toplevel)
TXN_DIR=$(mktemp -d "$REPO_ROOT/.dr-flip-txn.XXXXXX")
trap 'rm -rf "$TXN_DIR"' EXIT INT TERM ERR

# 1. Copy every target file to $TXN_DIR/originals/<sha>.yaml
# 2. Write proposed new content to $TXN_DIR/staged/<sha>.yaml
# 3. yq-validate every staged file (re-parse to confirm well-formed YAML + presence of expected fields)
# 4. ONLY IF all validations pass: atomic mv from staged/ into the repo working tree
# 5. If any step in 1-3 fails: trap fires, $TXN_DIR cleaned up, repo working tree untouched
```

Test scenarios for this in §8:
- chmod -w on one of the 8 target files → script aborts before any edit lands
- SIGINT mid-write → trap cleans up, no half-flipped state
- Inject `set -e` failure into the yq validator → trap cleans up

### Status output (Codex Finding 12 — label clarity)

```
$ ./scripts/dr-flip.sh status
NOTE: Reflects Git working-tree state ONLY. Run `kubectl get cluster.postgresql.cnpg.io -A`
      to confirm against live cluster.

APP                          MODE       LINEAGE  RESTORE_FROM  FILE
authentik-db                 initdb     v1       v0            apps/production/authentik/db-cnpg.yaml
dawarich-db                  initdb     v1       v0            apps/production/dawarich/db-cnpg.yaml
joplin-db                    initdb     v1       v0            apps/production/joplin/db-cnpg.yaml
opencut-cnpg-db              initdb     v1       v0            apps/production/opencut/db-cnpg.yaml
riven-db                     initdb     v1       v0            apps/production/media/riven/db-cnpg.yaml
sparky-fitness-cnpg-db       initdb     v1       v0            apps/production/sparky-fitness/db-cnpg.yaml
wiki-js-cnpg-db              initdb     v1       v0            apps/production/wiki-js/db-cnpg.yaml
zilean-db                    initdb     v1       v0            apps/production/media/zilean/db-cnpg.yaml
```

After `enable joplin-db`:
```
NOTE: Reflects Git working-tree state ONLY. Run `kubectl get cluster.postgresql.cnpg.io -A`
      to confirm against live cluster.

joplin-db                    recovery   v2       v1            apps/production/joplin/db-cnpg.yaml   ← flipped
```

The leading NOTE banner is mandatory and prepended to EVERY status invocation; it answers Codex Finding 12 directly.

### Explicitly out of scope

- **No git operations.** Script edits files; user does `git diff && git commit && git push`.
- **No kubectl calls.** The post-flip "delete Cluster + PVCs" dance lives in
  the runbook as a separate operator step (mitchross pattern;
  declarative-vs-imperative separation).
- **No `status --live` mode.** Possible future enhancement; not in this PR.
- **No `bump-lineage` standalone command.** Lineage is always coupled to an
  `enable` action so the script can guarantee `restore-from` is set correctly.

### Test surface (BATS — Codex pass-2 regression fix)

**BATS test suite is required pre-merge.** v2 had a contradiction: §5 listed it
as required while §4 said "NOT shipped with tests beyond bash -n." v3 resolves
to: required.

Coverage:
- `status` output format + banner presence
- `enable joplin-db` (single)
- `enable --all`
- `enable bogus-name` → exit 1
- `enable` on already-recovery DB → exit 1 with DR-during-DR message
- `enable --force-dr-during-dr` on already-recovery DB → success
- `disable` warning banner + 3s delay (testable via `--no-settle-warning`)
- atomicity: chmod -w one target file → no other files modified
- atomicity: SIGINT injection mid-write → trap fires, no files modified
- idempotence: `enable` then `enable` → no-op second invocation

Lives at `scripts/dr-flip.sh.bats`. CI runs on every PR touching `scripts/` or `apps/production/**/db-cnpg.yaml`.

### `status` output

```
$ ./scripts/dr-flip.sh status
APP                          MODE       LINEAGE  RESTORE_FROM  FILE
authentik-db                 initdb     v1       v0            apps/production/authentik/db-cnpg.yaml
dawarich-db                  initdb     v1       v0            apps/production/dawarich/db-cnpg.yaml
joplin-db                    initdb     v1       v0            apps/production/joplin/db-cnpg.yaml
opencut-cnpg-db              initdb     v1       v0            apps/production/opencut/db-cnpg.yaml
riven-db                     initdb     v1       v0            apps/production/media/riven/db-cnpg.yaml
sparky-fitness-cnpg-db       initdb     v1       v0            apps/production/sparky-fitness/db-cnpg.yaml
wiki-js-cnpg-db              initdb     v1       v0            apps/production/wiki-js/db-cnpg.yaml
zilean-db                    initdb     v1       v0            apps/production/media/zilean/db-cnpg.yaml
```

After `enable joplin-db`:
```
joplin-db                    recovery   v2       v1            apps/production/joplin/db-cnpg.yaml   ← flipped
```

### Error modes

| Condition | Behavior |
|---|---|
| Working tree dirty (other unrelated changes) | Warn + ask `--force` to proceed |
| Unknown DB name passed | Exit 1, list known DBs |
| File doesn't match expected format (no clean components: list with base + overlay) | Exit 1, do not edit that file (atomic rollback for the batch) |
| `--all` matches zero DBs | Exit 1 — almost certainly a path bug |
| Script invoked outside the repo root | Exit 1 with clear "run from kubernetes-lab/" message |

### What the script is NOT

- NOT a kubectl driver. Doesn't talk to the cluster. Pure file edit + git diff
  staging.
- NOT a DR wizard. Doesn't manage lineage versioning (§6), doesn't trigger
  Flux reconciles, doesn't restart consumer apps. Those steps stay in the
  runbook.
- ~~NOT shipped with tests~~ → BATS test suite IS required; see Test surface section above (v3 corrected the v2 contradiction).

---

## 5. Documentation deliverables

| File | Change |
|---|---|
| `components/cnpg-cluster/README.md` | Rewrite "What it renders" section: list the three Components (`base`, `initdb`, `recovery`) and the consumer pattern. Add a one-line example of `components:` list for both modes. Drop the "TL;DR — pick a variant" table (replaced by the new structure). Add a "Per-app customization" subsection documenting the patch contract (Codex Finding 9) — central overlays select bootstrap MODE only; per-app Flux `spec.patches` blocks remain supported for custom `postInitApplicationSQL`, `managed.roles`, etc. Use `sparky-fitness` as the canonical example. |
| `docs/cnpg-disaster-recovery.md` | Rewrite §1 (single-cluster restore) and §2 (cluster-nuke) procedures. Replace "manually edit db-cnpg.yaml" with `./scripts/dr-flip.sh enable <db>`. Keep PITR patch example. Update tested-on dates. **NEW (Codex Finding 8):** fix the brittle `${APP%-db}` shell-expansion path lookup at `docs/cnpg-disaster-recovery.md:28`. Current code produces wrong paths for `opencut-cnpg-db` (becomes `opencut-cnpg`, real dir is `opencut`) and any media-namespaced DB. Replace with an `rg + yq` lookup (pipe-character escaping omitted here for table parse). **NEW (Codex pass-2 High):** add an explicit Post-recovery settle checklist between recovery completion and `dr-flip.sh disable` — create Backup CR, wait completed, verify base-backup at the new lineage via barman-cloud-backup-list, only then `disable`. Mandatory step in every recovery flow. |
| `docs/backup-system-wiki.md` | Existing Layer 10 section (line 262+) currently describes the components/cnpg-cluster pattern. Update to describe `base + initdb/recovery` overlay structure. Add a "Cluster-nuke recovery recipe" subsection that's a 4-line cheat sheet pointing at the script + runbook. |
| `scripts/dr-flip.sh` | Self-documenting via `--help` + commented preamble. |
| `scripts/dr-flip.sh.bats` (NEW) | bats test suite covering: status output, enable joplin-db, enable --all, enable bogus-name, enable while already-recovery (DR-during-DR guard), enable with --force-dr-during-dr, enable with --restore-from-lineage v0, disable, atomicity (chmod -w + SIGINT injection), idempotence, banner-once-per-invocation. Required pre-merge gate. |
| `.gitignore` | Add `/.dr-flip-txn.*` entry so the script's transaction dir under repo root is git-ignored. |
| `.github/PULL_REQUEST_TEMPLATE/cnpg-v0-cleanup.md` (NEW, Codex pass-4 High-2 + pass-5 High fix) | PR template with 3 evidence-check boxes + commands + attestation timestamp + **explicit reviewer obligations** ("re-run workflow before merge; re-run local evidence commands within 1h of merge"). Used as the human merge gate for the v0 escape-hatch removal PR. Selected via `?template=cnpg-v0-cleanup.md`. |
| `.github/workflows/cnpg-cleanup-attestation.yml` (NEW) | Lightweight ubuntu-latest workflow that runs on cleanup PRs; regex-extracts `evidence-window-attested-at: <ts>` from the PR body, parses the timestamp, exits 1 if older than 24h. **Does NOT enforce merge-time freshness** — GitHub required checks don't re-run on merge. The workflow is an automation aid for the human gate (reviewer must manually re-trigger before merge per the PR template). |

The wiki update is the load-bearing piece for "easy to find" — anyone landing
on `backup-system-wiki.md` looking for "how do I recover CNPG" should see the
script + runbook in the first scroll.

---

## 6. Migration concern: existing live clusters + lineage versioning

### Two things change in-cluster on merge

1. **Kustomize render path:** consumers reference `components: [cnpg-cluster/base, cnpg-cluster/initdb]` instead of `components: [cnpg-cluster]`. The rendered Cluster manifest is byte-identical EXCEPT for the `spec.plugins[].parameters.serverName` field (see #2) AND any app-specific patches that already overlay on top (Codex Finding 2 — see §8 validation gate).
2. **`serverName` value:** the plugin's serverName changes from `${APP}` (e.g. `joplin-db`) to `${APP}-v1` (e.g. `joplin-db-v1`). Garage S3 layout shifts from `s3://volsync/cnpg/joplin-db/joplin-db/{base,wals}/` to `s3://volsync/cnpg/joplin-db/joplin-db-v1/{base,wals}/`.

The `spec.plugins[].parameters.serverName` field IS mutable on a live Cluster — CNPG re-evaluates it on next reconcile and the barman-cloud plugin starts writing to the new path. Existing data under the old prefix is NOT moved (S3 doesn't have move semantics; you'd have to copy). It's **orphaned but preserved**, AND accessible via the v0 emergency restore path (below).

### Migration risk window

Between merge and the first successful base backup of the new `-v1` lineage, recovery TO LATEST WAL on v1 isn't possible — only WAL since the lineage started exists, and barman needs at least one base backup as a starting point.

Default ScheduledBackup cadence is daily at staggered times (04:30–08:00 UTC). Worst case: cluster goes down ~1 minute after merge, before any base backup has populated `-v1/base/`, v1 recovery is unavailable until either (a) the scheduled backup window arrives, or (b) the operator triggers an immediate one.

### Mitigation 1: trigger 8 immediate base backups post-merge (blocking validation gate)

Per §7 rollout, this is a **mandatory blocking gate** — not optional cleanup. Loop:

```bash
for ns_app in 'joplin joplin-db' 'authentik authentik-db' 'dawarich dawarich-db' \
              'media zilean-db' 'media riven-db' 'opencut opencut-cnpg-db' \
              'sparky-fitness sparky-fitness-cnpg-db' 'wiki-js wiki-js-cnpg-db'; do
  set -- $ns_app
  kubectl -n $1 create -f - <<EOF
apiVersion: postgresql.cnpg.io/v1
kind: Backup
metadata:
  name: post-refactor-base-$(date +%s)
  namespace: $1
spec:
  cluster: { name: $2 }
  method: plugin
  pluginConfiguration: { name: barman-cloud.cloudnative-pg.io }
EOF
done
```

Then `kubectl wait --for=condition=Completed backup/<name>` per cluster.
Time-to-coverage drops from "up to 24h" to ~5 min for all 8.

### Mitigation 2: v0 emergency-restore is ALWAYS available (Codex Finding 11 — Option α)

The recovery overlay (§3) defaults `CNPG_RESTORE_FROM_LINEAGE: v0`. The `v0`
externalCluster entry has `plugin.parameters.serverName: ${APP}` (the
pre-refactor unversioned name). The barman plugin appends this to the
ObjectStore's `destinationPath: s3://volsync/cnpg/${APP}`, yielding
`s3://volsync/cnpg/${APP}/${APP}/{base,wals}/` — exactly where pre-refactor
ScheduledBackups landed.

**ONE ObjectStore is sufficient.** Codex Finding 11 corrected my v1 misreading:
the ObjectStore is just the per-app bucket prefix; serverName comes from the
externalCluster's plugin parameters. So the existing `${APP}-store` is reused
for both v0 (pre-refactor backups) and v1+ (post-refactor lineages).

**No S3 copy needed for v0 recovery.** No transitional second ObjectStore
needed.

**The escape hatch is NOT automatic** (Codex pass-6 High fix). `dr-flip.sh
enable` auto-computes `restore-from` from the current lineage (post-refactor:
`v1`, so default restore-from would be `v0`... actually no, see §4 — the
script bumps lineage to `v2` and sets restore-from to the prior lineage `v1`).
During the migration window when no v1 base backup exists yet, the operator
MUST explicitly invoke v0 recovery:

```bash
./scripts/dr-flip.sh enable --restore-from-lineage v0 joplin-db
```

The explicit `--restore-from-lineage v0` is mandatory; the default behavior
restores from the prior-numbered lineage which may not have a base backup
during the migration window. Without the flag, the recovery cluster fails
with "no target backup found" against the empty v1 prefix.

This is fully documented in §4 (script spec) + the runbook's migration-window
section, but worth calling out here as a v0-escape-hatch usability gotcha.

### Cleanup of the v0 escape hatch — evidence-based, not time-based (Codex pass-2 High fix)

v2 said "T+30d." Codex correctly flagged that 30 calendar days is the wrong
gate — if ScheduledBackups have been failing silently for some apps, removing
v0 deletes the only known restore source for them. v3 changes to an
evidence-based cleanup gate.

Cleanup PR removes the v0 escape hatch ONLY AFTER ALL of:

1. **All 8 clusters have ≥3 base backups at v1+** (verifiable per cluster via
   `barman-cloud-backup-list ... <app>-v1 | grep -c '^[0-9]'` ≥ 3). Three so
   the operator has confidence base backups are reliable, not just "one
   happened to land."
2. **WAL continuity verified** on all 8: `kubectl cnpg status <cluster> -n
   cnpg-system` shows zero WAL archive errors over the prior 7 days.
3. **At least one successful side-by-side restore test from v1+** on a
   non-trivial cluster (joplin or dawarich). Validates that the v1 lineage
   is restorable end-to-end, not just "looks healthy."

Once those gates pass:

1. Remove the `${APP_RESTORE_FROM}-restore-v0` entry from the recovery overlay's `externalClusters[]` list
2. Change default `CNPG_RESTORE_FROM_LINEAGE: v0` → `CNPG_RESTORE_FROM_LINEAGE: v1`
3. Update the runbook to document v1 as the new "default restore lineage"

Calendar timeline: gates achievable within ~7-14 days post-merge under nominal
conditions. Wall-clock is secondary; evidence is primary. Tracked in §12
definition of done as an evidence-gated checklist, not a date.

### Merge-time re-verification (Codex pass-3 High, pass-4 High-2 revision)

PR-open verification ≠ merge-time correctness. ScheduledBackups can fail
overnight; a PR opened Monday with passing evidence can degrade by Wednesday.

**Codex pass-4 High-2 correction:** v4 proposed a `.github/workflows/cnpg-v0-cleanup-gate.yml` with `runs-on: self-hosted`. This repo has NO self-hosted runner registered and no in-cluster kubectl access from GitHub Actions. The job would queue indefinitely and provide no real blocking semantics. v5 abandons the auto-CI gate framing and uses the actually-enforceable mechanism for this homelab: a mandatory PR checklist + branch-protection-required reviewer approval.

**Mechanism:**

1. **New PR template** at `.github/PULL_REQUEST_TEMPLATE/cnpg-v0-cleanup.md` (selected via the `?template=` query string when opening the cleanup PR). Contains the three evidence checks as `- [ ]` checkboxes with the exact commands to run locally:

   ```markdown
   # CNPG v0 escape-hatch cleanup PR

   This PR removes the v0 emergency-restore externalClusters[] entry from
   components/cnpg-cluster/recovery/bootstrap-patch.yaml and flips the
   default CNPG_RESTORE_FROM_LINEAGE to v1.

   ## Required evidence (run within 24h of intended merge)

   - [ ] **≥3 base backups per cluster at v1+:**
     ```bash
     for ns_app in 'joplin joplin-db' 'authentik authentik-db' \
                   'dawarich dawarich-db' 'media zilean-db' 'media riven-db' \
                   'opencut opencut-cnpg-db' 'sparky-fitness sparky-fitness-cnpg-db' \
                   'wiki-js wiki-js-cnpg-db'; do
       set -- $ns_app
       count=$(kubectl exec -n $1 $2-1 -c postgres -- \
         barman-cloud-backup-list --endpoint-url https://garage.lab.mainertoo.com \
         s3://volsync/cnpg/$2 $2-v1 2>/dev/null | grep -c '^[0-9]')
       [ "$count" -ge 3 ] && echo "✓ $2-v1: $count" || echo "✗ $2-v1: $count (need ≥3)"
     done
     ```
   - [ ] **7 days of WAL continuity (no archive errors per cluster):**
     ```bash
     for cluster in joplin-db authentik-db dawarich-db zilean-db riven-db \
                    opencut-cnpg-db sparky-fitness-cnpg-db wiki-js-cnpg-db; do
       errs=$(kubectl cnpg status "$cluster" --output json \
         | jq '.archiverStats.errors // 0')
       [ "$errs" = "0" ] && echo "✓ $cluster" || echo "✗ $cluster has $errs WAL errors"
     done
     ```
   - [ ] **Side-by-side restore from v1+ verified** on at least one non-trivial cluster (joplin or dawarich). Paste row-count comparison evidence below.

   ## Attestation

   By checking the boxes above and merging, I attest the evidence was
   re-verified within the past 24 hours.

   _evidence-window-attested-at: YYYY-MM-DD HH:MM UTC_
   ```

2. **Attestation-freshness CI workflow.** A lightweight ubuntu-latest job
   (no cluster access needed) regex-extracts `evidence-window-attested-at: <ts>`
   from the PR body, parses it, and exits 1 if `(now - ts) > 24h`:

   ```bash
   ts=$(gh pr view ${{ github.event.pull_request.number }} --json body \
     | jq -r '.body' | grep -oE 'evidence-window-attested-at: \S+ \S+ UTC' \
     | cut -d' ' -f2-)
   now_epoch=$(date -u +%s)
   ts_epoch=$(date -u -d "$ts" +%s)
   [ $((now_epoch - ts_epoch)) -le 86400 ] || {
     echo "ERROR: evidence-window-attested-at is $((now_epoch - ts_epoch))s old (>24h). Re-verify evidence + update timestamp."
     exit 1
   }
   ```

3. **Important correction (Codex pass-5 High):** This CI check passing **does
   NOT mean merge-time freshness is guaranteed.** GitHub's required-check
   model satisfies branch protection with the LAST PASSED check, not a
   re-run at merge. A check that passed at hour 23.5 will satisfy branch
   protection at hour 25+ without re-running. Plan v4/v5 incorrectly framed
   this as "blocking semantics" — v6 is honest: the CI check is an
   automation aid, not a merge-time enforcement.

   **Therefore the merge-time freshness is a HUMAN GATE** with these
   explicit reviewer obligations (documented in the PR template):

   - Before clicking "Squash and merge," the reviewer MUST manually
     re-trigger the `cnpg-cleanup-attestation` workflow via the GitHub UI
     ("Re-run all jobs" on the latest commit) and wait for it to pass green.
   - The reviewer MUST update the `evidence-window-attested-at: <ts>`
     timestamp in the PR body if the prior value is approaching 24h.
   - The reviewer MUST manually re-run the 3 evidence commands locally
     within 1h of merge and confirm output matches.

4. **Manual reviewer approval required** (standard branch protection).

This is the honest scope for a homelab without merge queue + self-hosted
runner infrastructure: CI helps detect stale evidence at PR-update time, but
the actual merge-time enforcement is human diligence guided by the PR template
checklist. If a self-hosted runner OR GitHub merge queue is set up later, the
auto-re-verification design from v4 (live cluster checks on merge) becomes
implementable — strict improvement, not different design.

### Why bundle lineage versioning now (rather than defer)

- Mitchross's full pattern provides defense-in-depth that costs ~1 day extra here vs ~3 days bolted on later
- New WAL writes IMMEDIATELY use the lineage suffix, so we don't have to retrofit a serverName migration later (which would also have a risk window)
- Operator's mental model is consistent from day one: every DR bumps a version, lineages are first-class concepts
- The script bears the complexity (3 substitution edits) so operator interface stays simple (`dr-flip.sh enable`)
- v0 escape hatch makes the migration window risk-free, not "5 min of accepted risk"

---

## 7. Rollout plan

Single PR, atomic. Rolling this in pieces would leave the Component tree
half-migrated and brittle.

**Pre-merge validation (all blocking gates):**

1. **Per-app full Flux Kustomization render diff** (Codex Finding 2). For each of the 8 `db-cnpg.yaml` files, render the FULL Flux Kustomization (i.e., `flux build kustomization <name> --kustomization-file ./apps/production/<path>/db-cnpg.yaml --path ./apps/base/empty`), NOT just the shared Component. Compare against pre-refactor render with `dyff`. Expected diff:
   - Cluster: `spec.plugins[0].parameters.serverName` only (e.g. `joplin-db` → `joplin-db-v1`)
   - No other meaningful differences

   **Critical app-specific test case:** `sparky-fitness` final manifest MUST contain ALL of `bootstrap.initdb.database`, `bootstrap.initdb.owner`, `bootstrap.initdb.postInitApplicationSQL`, AND `spec.managed.roles`. The strategic merge of the initdb overlay + sparky-fitness's local patches must combine map keys correctly (Codex Finding 1).
2. `flux-local diff` in CI shows ONLY the `serverName` field change per Cluster.
3. **Side-by-side recovery test against joplin-db using v0 escape hatch.** Render the new overlay locally with `APP=joplin-db-overlay-test`, `APP_RESTORE_FROM=joplin-db`, `CNPG_RESTORE_FROM_LINEAGE=v0`. Apply, confirm recovery completes against the existing pre-refactor backups. Confirms the v0 path works BEFORE we depend on it post-merge. Tear down.
4. **Side-by-side recovery test against joplin-db using v1 path.** Same test but with `CNPG_RESTORE_FROM_LINEAGE=v1` after manually creating one base backup at `s3://volsync/cnpg/joplin-db/joplin-db-v1/`. Confirms the v1 path works. Tear down.
5. `./scripts/dr-flip.sh status` shows `initdb / v1 / v0` for all 8.
6. `./scripts/dr-flip.sh enable joplin-db && git diff` shows expected 3 yq writes (mode swap + lineage bump + restore-from set). Revert.
7. `./scripts/dr-flip.sh enable --all && git diff --stat` shows 8 files changed. Revert.
8. **DR-during-DR guard test:** with joplin-db's working-tree file in `recovery` mode, run `./scripts/dr-flip.sh enable joplin-db`. Expect exit 1 + the multi-line error message from §4. With `--force-dr-during-dr`, expect success.
9. **Atomicity tests** (Codex Finding 5):
   - `chmod -w apps/production/wiki-js/db-cnpg.yaml; ./scripts/dr-flip.sh enable --all` — expect exit 1, no other 7 files modified
   - `./scripts/dr-flip.sh enable --all` with SIGINT injection mid-loop — expect trap to fire, no files modified
   - Inject yq-validator failure — expect trap to fire, no files modified
10. **CI gate: base-without-overlay rejection** (Codex pass-1 Finding 3). A workflow that fails the PR if any `db-cnpg.yaml` references `cnpg-cluster/base` without exactly one of `cnpg-cluster/initdb` or `cnpg-cluster/recovery`. Implementation per file via yq: `for f in $(rg --files apps/production | rg 'db-cnpg\.yaml$'); do mode=$(yq '.spec.components[] | select(. == "*/cnpg-cluster/initdb" or . == "*/cnpg-cluster/recovery")' "$f"); [ -n "$mode" ] || exit 1; done`. Run per-file to avoid yq's multi-doc aggregation issues (Codex pass-2 Medium fix).
11. **CI gate: new-app recovery-mode guard** (Codex pass-1 Finding 7). A workflow that fails the PR if a NEW `db-cnpg.yaml` (one not present on the base branch) is committed with `recovery` mode in its components list. Implementation per Codex pass-3 fix:
    - Use `${{ github.base_ref }}` from the workflow env (not hard-coded `origin/master`).
    - Use `git diff --name-status --diff-filter=AMR origin/${{ github.base_ref }}...HEAD` to capture Added/Modified/Renamed paths.
    - **Rename rows have 3 columns** (`R100  old/path  new/path`); awk must select column 3 for renames, column 2 otherwise:
      ```bash
      git diff --name-status --diff-filter=AMR "origin/${BASE_REF}...HEAD" \
        | awk '$1 ~ /^R/ {print $3} $1 !~ /^R/ {print $2}' \
        | grep 'db-cnpg\.yaml$' \
        | while read path; do
            mode=$(yq '.spec.components[] | select(. == "*/cnpg-cluster/recovery")' "$path")
            override=$(yq '.spec.postBuild.substitute.DR_MODE_ALLOWED' "$path")
            [ -z "$mode" ] || [ "$override" = "true" ] || { echo "ERROR: $path uses recovery mode without DR_MODE_ALLOWED override"; exit 1; }
          done
      ```
    Allow override via `DR_MODE_ALLOWED: "true"` substitution if intentional.
12. **CNPG admission test for multi-entry externalClusters[]** (Codex pass-2 Low; pass-3 Medium tightening). Apply a test Cluster manifest with the **FULL v0..v10 entry shape** (matching what the recovery overlay actually renders). Reference v0 from bootstrap.recovery.source. v1..v10 unreferenced. Use a bogus ObjectStore name in 2 of the unreferenced entries to confirm CNPG doesn't validate references on unreferenced externalClusters[]. Confirm:
    - CNPG admission accepts the Cluster
    - Recovery pod starts cleanly with v0
    - No webhook validation error for the unreferenced bogus entries
    - Plugin sidecar logs no errors about the unreferenced entries
    Test artifact lives in `apps/archive/` for repeatability. Required pre-merge gate.
13. **Render assertion: all v0..v10 entries present per app** (Codex pass-3 Medium). For each of the 8 `db-cnpg.yaml` files, render the FULL Flux Kustomization and assert exactly 11 `externalClusters[]` entries with names matching `{${APP}-restore-v0, ${APP}-restore-v1, ..., ${APP}-restore-v10}` and serverName values matching `{${APP}, ${APP}-v1, ..., ${APP}-v10}`. Catches the failure mode where the implementation accidentally copies only v0..v5 because the plan snippet showed brevity-omitted entries.

**Post-merge — blocking validation gates (must all pass before declaring rollout complete):**

1. Watch Flux reconcile (~2 min). All 8 Clusters should re-render with the new `serverName: <app>-v1`. No app errors.
2. Plugin restarts each cluster's barman archiver (rolling primary restart, ~30s each, staggered). Brief WAL archive gap is acceptable.
3. **BLOCKING:** Trigger 8 immediate base backups (loop in §6). Wait for each `Backup` CR to report `phase: completed`. Loop must include `kubectl wait` — operator does not move on until all 8 are green.
4. **BLOCKING:** Verify each cluster's `-v1` prefix has a base backup via barman-cloud-backup-list:
   ```bash
   for ns_app in 'joplin joplin-db' 'authentik authentik-db' 'dawarich dawarich-db' \
                 'media zilean-db' 'media riven-db' 'opencut opencut-cnpg-db' \
                 'sparky-fitness sparky-fitness-cnpg-db' 'wiki-js wiki-js-cnpg-db'; do
     set -- $ns_app
     count=$(kubectl exec -n $1 $2-1 -c postgres -- \
       barman-cloud-backup-list --endpoint-url https://garage.lab.mainertoo.com \
       s3://volsync/cnpg/$2 $2-v1 2>/dev/null | grep -c '^[0-9]')
     [ "$count" -ge 1 ] && echo "✓ $2-v1 has $count base backup(s)" || { echo "✗ $2-v1 EMPTY — rollback!"; exit 1; }
   done
   ```
5. **BLOCKING:** `./scripts/dr-flip.sh status` confirms all 8 still in `initdb / v1 / v0`.

If ANY of these gates fails: revert the PR before attempting a fix. The v0 backups are still intact; reverting restores the pre-refactor state cleanly.

**Settle (24h after merge):**
- One full daily ScheduledBackup cycle has completed under the new lineage.
- All 8 clusters have ≥2 base backups in `-v1/`.
- Recovery testable end-to-end against v1.
- Consider a cleanup PR to remove the v0-emergency-restore plumbing if the post-merge window passed without incident (or keep for one full month as transition-period safety).

**Rollback if it breaks:**
- Revert the PR. The old `components/cnpg-cluster` Component + recovery sub-Component come back. The 8 `db-cnpg.yaml` files revert to the single-Component reference. **`serverName` reverts from `${APP}-v1` to `${APP}`** — backups land back at the pre-refactor prefix.
- Any data written to `-v1/` during the (likely short) test window is orphaned but preserved on S3.
- Cluster specs reconcile cleanly back. No app downtime.

---

## 8. Test plan

| Phase | Test | Pass criterion |
|---|---|---|
| Pre-merge | `kustomize build apps/production/joplin/db-cnpg.yaml` before + after refactor, `dyff between` | Diff limited to: `serverName: joplin-db` → `joplin-db-v1` (expected). No other meaningful changes. |
| Pre-merge | Same for all 8 `db-cnpg.yaml` files | Same pattern |
| Pre-merge | `flux-local diff` in CI | Diff list shows only `serverName` field per Cluster |
| Pre-merge | Side-by-side recovery test against joplin-db using the new recovery overlay (APP=joplin-db-overlay-test, APP_RESTORE_FROM=joplin-db, EMERGENCY_RESTORE_FROM_UNVERSIONED=true) | Cluster healthy in <3 min, row counts identical to source |
| Pre-merge | `./scripts/dr-flip.sh status` shows `initdb / v1 / v0` for all 8 | ✓ |
| Pre-merge | `./scripts/dr-flip.sh enable joplin-db` → `git diff --stat` | Exactly 1 file, 3 substitution edits |
| Pre-merge | After enable: `status` shows `recovery / v2 / v1` for joplin-db | ✓ |
| Pre-merge | `./scripts/dr-flip.sh enable --all` → `git diff --stat` | Exactly 8 files |
| Pre-merge | `./scripts/dr-flip.sh disable --all` → `git status` | Mode flipped back to initdb; lineage values stay at v2/v1 (deliberate) |
| Pre-merge | `./scripts/dr-flip.sh enable bogus-name` | Exit 1 with clear error |
| Pre-merge | `bash -n scripts/dr-flip.sh` + shellcheck | Pass |
| Post-merge (5 min) | All 8 clusters reconcile to `serverName: <app>-v1` | ✓ |
| Post-merge (10 min) | All 8 manual `Backup` CRs report `phase: completed` | ✓ |
| Post-merge (10 min) | Each `-v1` prefix has ≥1 base backup verifiable via `barman-cloud-backup-list` | ✓ |
| Post-merge (24h) | Daily ScheduledBackup cycle completes; all 8 have ≥2 base backups under v1 | ✓ |
| Post-merge (24h) | Cluster state stable; no app errors | ✓ |

---

## 9. Hand-off prompt for Codex adversarial review

After this plan passes user review, hand to `codex:codex-rescue` agent with
the prompt below. Codex's job: find what we missed.

### Prompt

> Adversarial design review of a planning doc for refactoring CloudNativePG
> (CNPG) cluster bootstrap/recovery management in a homelab Kubernetes mono-repo
> (Flux-managed, mitchross/talos-argocd-proxmox-inspired).
>
> **Read first:**
> - `docs/plans/cnpg-overlay-refactor.md` (this doc, the plan)
> - `components/cnpg-cluster/cluster.yaml` + `recovery/cluster.yaml` (current state)
> - `apps/production/joplin/db-cnpg.yaml` (one example consumer)
> - `docs/cnpg-disaster-recovery.md` (existing runbook)
> - The two mitchross reference docs:
>   - https://github.com/mitchross/talos-argocd-proxmox/blob/1f6ab9146431a137965de0b853b3a7526a25bf57/docs/cnpg-explained.md
>   - https://github.com/mitchross/talos-argocd-proxmox/blob/1f6ab9146431a137965de0b853b3a7526a25bf57/docs/cnpg-disaster-recovery.md
>
> **Cluster context (live, real workloads):**
> - 8 CNPG clusters across 7 namespaces, all freshly migrated to the
>   plugin-barman-cloud spec (PR #512, 2026-05-19)
> - barman-cloud plugin operator v0.12.0 deployed in `cnpg-system`
> - Backups land in Garage S3 bucket `volsync` under `cnpg/<app>/<serverName>/{base,wals}/`
> - No DR events have happened yet against any of these backups
> - cnpg-cluster Component was just rewritten 8h ago (PRs #510, #512)
>
> **Attack the plan along these axes:**
>
> 1. **Kustomize semantics.** Strategic-merge patches via Components — does
>    the patch target syntax actually merge correctly when the base Cluster
>    has no `spec.bootstrap` at all? Are there ordering issues if Flux
>    postBuild substitutions are applied AFTER patch resolution (they are)?
>    Edge case: what if `${CNPG_DB_NAME}` substitution fails — does the
>    initdb patch render a half-valid Cluster?
>
> 2. **Live-cluster spec drift.** Will the in-place re-render REALLY produce
>    a byte-identical Cluster manifest for already-running clusters? Walk
>    through the rendering for joplin-db specifically (postgres 16.10,
>    bootstrap.initdb with database/owner=joplin, etc.) and verify.
>
> 3. **CNPG webhook validation.** CNPG validates Cluster manifests at admission.
>    Are there cases where the base (bootstrap-less) Cluster would be rejected
>    BEFORE the overlay patch applies? (kustomize patches inside a Component
>    are applied during render, not via webhook chain — so this should be safe,
>    but verify.)
>
> 4. **Script failure modes.** scripts/dr-flip.sh sed-based editing — what
>    happens if a future contributor reformats a `db-cnpg.yaml` (e.g. yq
>    -P --indent 4) such that the regex doesn't match? What's the failure
>    mode? Should the script use yq instead of sed?
>
> 5. **Partial-flip recovery.** Operator runs `enable --all`, the script
>    succeeds on 6 of 8 files then crashes on file 7 (disk full, signal,
>    whatever). The atomicity guarantee says all-or-nothing; verify the
>    rollback path actually works (test: chmod -w on a file, run enable
>    --all, expect clean revert).
>
> 6. **DR-during-DR scenarios with lineage.** Operator flips joplin-db to
>    recovery (lineage: v1→v2, restore-from: v0→v1), commits, pushes. Flux
>    reconciles. Cluster comes back healthy on restored data at v2. BEFORE
>    the operator flips back to initdb, ANOTHER DR event happens. They run
>    `dr-flip.sh enable joplin-db` AGAIN. Script bumps to v3 (restore-from
>    v2). But v2's `-v2/base/` doesn't have a backup yet — the only data
>    in v2 is the new WAL since v2 was created. Recovery may have no base
>    to restore from. Walk through what CNPG does in this case and whether
>    the script should detect+warn.
>
> 7. **The "fresh deploy" case.** New CNPG app being added to the repo:
>    operator copies the joplin pattern, adds `db-cnpg.yaml`, no S3 backups
>    exist yet. The initdb overlay runs, Cluster bootstraps fresh. Confirm
>    this works. Then: operator forgets to flip from `recovery` (e.g.
>    cloned a recovery-mode db-cnpg.yaml from another app). What does
>    CNPG do? Does the Cluster fail to bootstrap because the externalCluster's
>    S3 path has no base backup? Is the error message clear?
>
> 8. **Documentation gap test.** Imagine you're on-call at 02:00, the
>    cluster just nuked, you have to recover CNPG. Read ONLY
>    `docs/backup-system-wiki.md` + `docs/cnpg-disaster-recovery.md`
>    (post-refactor versions). Could you actually do the recovery without
>    additional context? Identify gaps.
>
> 9. **Why is this better than what mitchross has?** Mitchross's pattern
>    keeps per-DB autonomy by giving each DB its own directory with overlays
>    inside. Our pattern centralizes overlays in `components/cnpg-cluster/`
>    and references them from per-app `db-cnpg.yaml`. Argue whether our
>    pattern degrades anything mitchross gets — e.g., per-DB bootstrap-patch
>    customization (custom postInitApplicationSQL, different image per DB).
>    If yes, propose a mitigation.
>
> 10. **"Single global override" alternative.** §1 mentions Option A.3 (a
>     single top-level "DR mode" file). Argue concretely why A.2 (per-DB
>     flag + helper script) is better than A.3 for THIS codebase. If A.3
>     is actually better, say so.
>
> 11. **Migration risk window + v0 emergency restore.** §6 proposes Option β
>     (no built-in v0 alias; manual S3-copy procedure if DR happens in the
>     5-min post-merge window). Is this acceptable? Walk through the actual
>     procedure for emergency-restoring from the unversioned `${APP}/`
>     prefix during the window. Could the v0 alias be made automatic instead
>     (Option α with a secondary ObjectStore) and would it be worth it?
>
> 12. **Lineage drift between live cluster and git.** Operator runs
>     `dr-flip.sh enable joplin-db` but forgets to commit. Cluster still
>     shows the OLD lineage in the live state. Then they run the script
>     AGAIN — does it idempotently detect the local file's state, or does
>     it double-bump (v1 → v2 → v3) without realizing? Walk through.
>
> **Deliverables:**
> - A markdown response with numbered findings (Critical / High / Medium / Low)
> - For each finding: file:line citation if applicable, specific repro or thought-experiment, recommended fix
> - Explicitly call out anything in the plan that's just *wrong* (not just suboptimal)

After codex review: incorporate findings into the plan, re-validate, then
implement.

---

## 10. User design decisions (locked 2026-05-19)

These were answered before handing the plan to Codex. Recorded here so the
reviewer doesn't relitigate them. Items marked **(REVISED v2)** were
overturned by Codex review and updated.

1. **Lineage versioning:** BUNDLED into this refactor (not deferred). §6 expanded.
2. **PR shape:** Single atomic PR.
3. **Script language:** ~~bash + sed~~ **(REVISED v2)** → **bash + yq** per Codex Finding 4. The two path depths (`../../../` vs `../../../../`) made a sed-based pattern silently miss 2 of 8 files. yq edits structured fields and is depth-agnostic.
4. **Naming:** Flatter — `components/cnpg-cluster/{base,initdb,recovery}/`. Matches volsync-v2 convention.

### Per-DB flag (A.2) vs global DR switch (A.3) — rationale documented (Codex Finding 10)

We chose A.2 (per-DB `kustomization.yaml` flag + `dr-flip.sh --all` for global ergonomics) over A.3 (single top-level DR-mode toggle) because:

- **Single-cluster restore is the common case.** Most real DR events are "one DB has bad data, restore that one." A global toggle is wrong-tool for this — it would flip all 8 simultaneously, including 7 healthy clusters that don't need recovery.
- **Staged recovery is supportable.** During cluster-nuke, the operator might want to bring up critical apps (auth, joplin) first and verify them before flipping the media stack. Per-DB flags allow this; a global toggle doesn't.
- **Heterogeneous path depths exist in the consumer files** (Codex Finding 4). A global toggle would still need per-app edits to resolve the right Component path — defeating the "single file" claim.
- **`dr-flip.sh enable --all` gives the global ergonomics when needed.** Operator types one command, gets all 8 flipped in a single PR. The "single operator action" goal is met without the structural downsides of a global toggle.

A.3 stays a non-goal.

---

## 11. Future-design constraint (Codex Finding 11 self-check)

The current design has the per-app `${APP}-cnpg-s3` Secret rendered in the
SAME Flux Kustomization as the Cluster (via the base Component). Flux applies
all resources from that Kustomization in a single transaction, so the Secret
exists before the Cluster's recovery pod attempts to mount it. No race.

**If we ever move Secret generation to Kyverno mutate** (analogous to how
volsync per-PVC Secrets are generated via `volsync-pvc-backup-restore-kopia`
ClusterPolicy), a timing window opens:

1. Cluster admitted by API server
2. Recovery pod scheduled, tries to mount `${APP}-cnpg-s3` Secret
3. Kyverno generate rule has NOT yet fired (Kyverno is async)
4. Recovery pod fails to start: `MountVolume.SetUp failed for volume "..." : secret "..." not found`

**Mitigations if this future change happens:**

- Add `dependsOn` ordering on the per-app Flux Kustomization to wait for the Kyverno-generated Secret to exist before applying the Cluster
- OR: use Kyverno's `synchronize: true` on the generate rule so the Secret is reconciled to existence before the policy returns success
- OR: don't migrate the CNPG Secrets to Kyverno; keep them in-Kustomization for this exact reason

Documented here so a future refactorer reading this plan knows the constraint
exists before they reach for Kyverno on instinct.

---

## 12. Definition of done

- [ ] Plan reviewed by user, open questions in §10 answered
- [ ] Plan reviewed by codex (codex:codex-rescue), findings incorporated
- [ ] Implementation PR opened with all changes in §3
- [ ] Pre-merge validation per §8 completed (all rows green)
- [ ] `scripts/dr-flip.sh` lands with `--help`, `status`, `enable`, `disable`
- [ ] Three doc files updated per §5 (`README.md`, `cnpg-disaster-recovery.md`, `backup-system-wiki.md`)
- [ ] PR merged, post-merge 24h soak confirms zero spec drift
- [ ] Memory updated: `project_cnpg_recovery_component.md` notes the refactor +
  re-validates the cluster-nuke promise
- [ ] **Evidence-based v0 cleanup PR (no calendar gate).** Open when ALL three
  evidence gates from §6 pass: (a) ≥3 base backups per cluster at v1+, (b) 7
  days of WAL continuity with zero archive errors per cluster, (c) at least one
  successful side-by-side restore test from v1+.
  - [ ] PR opened with the `?template=cnpg-v0-cleanup.md` query string so the body has the 3-checkbox evidence form + attestation timestamp
  - [ ] All 3 evidence checkboxes ticked, with command output pasted as evidence
  - [ ] `evidence-window-attested-at: <ts>` updated within 24h of intended merge. CI `cnpg-cleanup-attestation.yml` is an automation aid that DETECTS stale timestamps, but per §6 does NOT enforce merge-time freshness — GitHub required checks don't re-run on merge. Reviewer obligation: manually re-trigger the workflow + re-run the 3 evidence commands locally within 1h of merging.
  - [ ] Manual reviewer approval (standard branch protection)
  - [ ] The PR removes the v0 entry + flips default `CNPG_RESTORE_FROM_LINEAGE` to v1 + updates the runbook

---

## Appendix A — Codex review pass 1 (2026-05-19) — summary

Adversarial review by `codex:codex-rescue`. Mitchross URLs were unreachable in
Codex's sandbox so axis 9 is hypothesis-flagged. 12 findings: 1 Critical,
4 High, 5 Medium, 1 Low.

### Findings that change the plan (must address before implementation)

**[Critical] Finding 11 — Option α is achievable without a second ObjectStore.** My
recommendation of Option β was based on a wrong premise. The existing ObjectStore's
`destinationPath` is just `s3://volsync/cnpg/${APP}` — the `serverName` segment
in the actual S3 path is appended by the Barman plugin from the **externalCluster's**
`plugin.parameters.serverName` field, NOT from the ObjectStore resource. So Option α
just needs a second `externalClusters[]` entry named `${APP}-v0-restore` with
`barmanObjectName: ${APP}-store` (reusing the existing ObjectStore) and
`serverName: ${APP}` (the pre-refactor unversioned name). No S3 copy needed,
no second ObjectStore needed. **Update §6 to make Option α the default.**

**[High] Finding 2 — "byte-identical except serverName" is wrong.** `sparky-fitness`
already has app-specific Cluster patches (`spec.managed.roles` +
`spec.bootstrap.initdb.postInitApplicationSQL`) at
`apps/production/sparky-fitness/db-cnpg.yaml:87-113`. My pre-merge dyff test needs
to render the FULL Flux Kustomization per app, not just the shared Component. **Update
§7 + §8 validation steps.**

**[High] Finding 4 — sed-based script can't handle the two path depths.** `joplin`,
`authentik`, `dawarich`, `opencut` use `../../../components/cnpg-cluster`.
`wiki-js`, `sparky-fitness`, `media/riven`, `media/zilean` use
`../../../../components/cnpg-cluster` (one extra `../`). A naive sed expression
that targets `../../../components/cnpg-cluster/...` misses 4 of 8 files silently.
**My "bash + sed" decision needs to change to yq, OR sed must use a depth-agnostic
regex.** Either way, fixture tests for both path depths are mandatory.

**[High] Finding 6 — DR-during-DR can produce unrecoverable `restore-from vN`.**
If `joplin-db` is freshly recovered to v2 and a second DR happens before v2 has a
base backup, `dr-flip.sh enable` bumps to v3 and sets `restore-from=v2` —
CNPG then fails with "no target backup found." **Update §4 script spec:**
require `--force-dr-during-dr` flag when current mode is already `recovery`;
add a blocking "create + verify immediate base backup" settle step after every
recovery before `disable` is allowed.

**[High] Finding 8 — Existing docs are stale and have brittle path expansion.**
`docs/cnpg-disaster-recovery.md:28` uses `${APP%-db}` which produces wrong paths
for `opencut-cnpg-db` (→ `opencut-cnpg`, not `opencut`) and media-namespaced DBs
(`apps/production/media/riven/...`, not `apps/production/riven/...`). **Fix
in this PR, not as follow-up.** Replace string-slicing with `rg --files | yq`.

### Findings that add validation gates (incorporate into §8 test plan)

**[Medium] Finding 1 — strategic merge with sparky-fitness's existing patches.**
Add a pre-merge render assertion: `sparky-fitness` final manifest must contain
`database`, `owner`, `postInitApplicationSQL`, AND `managed.roles`. JSON6902
patches have historically failed silently in this repo; strategic merge SHOULD
combine map keys but verify, don't assume.

**[Medium] Finding 3 — fail-closed admission gate.** CNPG defaults bootstrap
method to `initdb`, so a base-only Cluster (no overlay) is admitted with wrong
defaults instead of being rejected. Add CI: any `db-cnpg.yaml` referencing
`cnpg-cluster/base` must reference exactly one of `initdb` or `recovery`.

**[Medium] Finding 5 — atomicity is asserted but not designed.** Plan promises
all-or-nothing edits, but the script spec doesn't describe temp-file +
trap-cleanup. Implement with: copy originals to temp dir, write new files to
temp, validate all output, atomic move into place. Trap `INT`/`TERM`/`ERR`.
Test with unwritable file + injected signal.

**[Medium] Finding 7 — fresh deploy in recovery mode fails late.** A new app
mistakenly cloned with the recovery overlay will fail inside the recovery pod
(no source backup exists). CI gate: new `db-cnpg.yaml` in `recovery` mode
requires `DR_MODE_ALLOWED: true` annotation or explicit substitution.

**[Medium] Finding 9 — central overlays vs per-DB autonomy.** The plan must
explicitly document the contract: central overlays select bootstrap MODE; per-app
Flux `spec.patches` blocks remain supported for custom postInitApplicationSQL,
managed.roles, etc. Add `sparky-fitness` as the canonical test case (currently
the only app exercising this).

**[Medium] Finding 12 — script idempotence is local-file, not live-cluster.**
`dr-flip.sh status` shows the working-tree state, not what Flux has actually
applied. Either rename columns to make this obvious or add a banner: "MODE and
LINEAGE reflect Git working-tree state only, not confirmed live cluster state."
Keep no-kubectl behavior as the default.

### Findings to document, not act on

**[Low] Finding 10 — A.2 vs A.3 rationale.** §10 records the decision but doesn't
state WHY. Add: per-DB flags support single-cluster restore, staged recovery, and
heterogeneous path depths; `--all` gives global ergonomics when needed; a global
DR switch would be dangerous for single-DB PITR.

### Hypothesis (Codex couldn't fetch mitchross URLs)

**[Medium] Finding 9 (hypothesis)** — Codex couldn't validate against the
mitchross reference docs because the raw GitHub URLs were unreachable in its
sandbox. The recommendation (document the per-app patch contract; add
sparky-fitness as canonical test case) stands regardless; we already verified
the mitchross pattern independently in the parent session.

### Codex correctness self-check (deep on Finding 11)

Codex went deeper on axis 11 than I expected and the analysis was sharp:

- Pointed out that bootstrap methods are MUTUALLY EXCLUSIVE in CNPG (no
  "recovery wins" semantics) — confirms Finding 3's CI gate is mandatory.
- Distinguished the plugin's TWO roles (bootstrap-time external cluster
  reading vs steady-state archive writing) cleanly.
- Caught my factual error: ObjectStore's destinationPath does NOT encode serverName.
- Flagged a future risk: if we ever move S3 Secret generation to Kyverno mutate
  (instead of Flux-rendered same-Kustomization), there's a timing window where
  the recovery pod can't mount the Secret. Document this as a constraint on
  future Secret management changes.

### Net effect on the plan

Roughly 4 hours of additional design work folded back into the plan before
implementation can start:

1. Rewrite §6 to default Option α with the second `externalClusters[]` entry
   pattern Codex described.
2. Rewrite §4 script spec to use yq (not sed) + add `--force-dr-during-dr` flag
   + design atomic temp-file write path.
3. Rewrite §7 + §8 to render full Flux Kustomizations (not just shared Component)
   in the diff test; add the 8 immediate base-backup post-merge step to a
   blocking validation gate not just a "should do."
4. Add §5 entry to fix the brittle `${APP%-db}` path expansion in the existing
   `cnpg-disaster-recovery.md` as part of THIS PR.
5. Add CI gates: base-without-overlay rejection (Finding 3), recovery-mode for
   new apps rejection (Finding 7), per-app render assertions (Finding 1).
6. Document the per-app patch contract + add sparky-fitness as canonical test
   case (Finding 9).
7. Document the future Kyverno-secret-timing risk (Finding 11 self-check).

After plan v2 incorporates these, second Codex pass before implementation PR.

---

## Appendix B — Codex review pass 2 (2026-05-19) — summary

Adversarial review by `codex:codex-rescue` on plan v2. 9 findings: 1 Critical, 3 High, 4 Medium, 2 Regressions. Verdict: NOT YET SAFE TO IMPLEMENT.

### Pass-1 finding verification

| Finding | Status |
|---|---|
| F1 (Kustomize semantics + sparky-fitness merge) | ADDRESSED (§3 + §8 sparky-fitness assertion) |
| F2 (live-cluster spec drift wrong "byte-identical") | ADDRESSED (full Flux Kustomization render diff) |
| F3 (CNPG webhook validation — base-only is initdb default) | ADDRESSED (CI gate base-without-overlay rejection) |
| F4 (sed unable to handle path depths) | ADDRESSED (switched to yq) |
| F5 (atomicity asserted not designed) | ADDRESSED (transaction-dir staging + trap cleanup); portability flagged separately |
| F6 (DR-during-DR) | PARTIALLY ADDRESSED (script guard exists; per-recovery base-backup gate missing) |
| F7 (fresh deploy in recovery mode fails late) | ADDRESSED (CI gate) |
| F8 (existing runbook brittle path expansion) | ADDRESSED IN PLAN (must apply edits during implementation) |
| F9 (central overlays vs per-DB autonomy) | ADDRESSED (per-app patch contract documented) |
| F10 (per-DB vs global flag rationale) | ADDRESSED (§10 rationale) |
| F11 (Option α/β) | ADDRESSED (Option α adopted) |
| F12 (script idempotence local-file only) | PARTIALLY ADDRESSED (banner present but second status block repeated without banner) |

### New issues (v2-introduced)

**[Critical] Recovery lineage entries not maintained by the script.** v2 said "dr-flip.sh appends a fresh entry on each lineage bump" — but never specified the mechanism. Hardcoded v0/v1/v2 silently break at v3+. → v3 fix: pre-create v0..v10 entries.

**[High] DR-after-settle reaches a lineage with no verified base backup.** `--force-dr-during-dr` only fires when current mode is already recovery. Operator who disables after recovery without verifying skips the guard on the next normal enable. → v3 fix: settle-gate banner on disable + runbook checklist.

**[High] T+30d v0 cleanup is time-based not evidence-based.** Calendar date is the wrong gate; if backups have been failing silently, removing v0 deletes the only restore source. → v3 fix: evidence-gated cleanup with 3 explicit verification gates.

**[Medium] New-app CI gate brittle.** `origin/master` vs `origin/main` confusion, `--name-only` loses A/M/R distinction, yq aggregation on multi-doc input. → v3 fix: `github.base_ref`, `--diff-filter=AMR`, per-file yq.

**[Medium] Transaction dir cross-filesystem mv non-atomic.** macOS happens to be safe because temp + repo are on same APFS volume, but CI runners or different setups break atomicity. → v3 fix: move txn dir under `.git/`.

**[Low] Multiple externalClusters[] entries unproven behavior.** CNPG should ignore unreferenced entries but admission webhook may complain about non-existent Secrets. → v3 fix: pre-merge admission test.

### v2 regressions

**[Medium] BATS contradiction.** §5 said "required pre-merge gate"; §4 said "NOT shipped with tests beyond bash -n." → v3 fix: BATS required, §4 corrected.

**[Low] `status --all` flag undefined.** §8 step 5 called it; §4 usage didn't define it. → v3 fix: dropped `--all`.

**[Low] Duplicate rollback section.** Two "Rollback if it breaks" sections in §7. → v3 fix: deleted duplicate.

---

## Appendix C — Codex review pass 3 (2026-05-19) — summary

Adversarial review by `codex:codex-rescue` on plan v3. 8 new findings: 1 Critical, 2 High, 5 Medium, 0 Low. (Pass-5 caught my v5 miscounting; v6 corrects.) Verdict: NOT SAFE TO IMPLEMENT.

### Pass-2 finding verification

| Finding | Status |
|---|---|
| Critical: hardcoded recovery entries | ADDRESSED in design (pre-create v0..v10); §8 brevity-omitted entries flagged |
| High: disable before post-recovery base backup | PARTIALLY ADDRESSED (banner + runbook checklist; no hard gate) |
| High: runbook lacks settle checklist | ADDRESSED |
| High: T+30d cleanup unsafe | PARTIALLY ADDRESSED (PR-open evidence; no merge-time re-verification) |
| Medium: CI new-app guard tightening | PARTIALLY ADDRESSED (concrete command said AMR but used A; awk wrong for R rows) |
| Medium: multi-entry externalClusters admission test | PARTIALLY ADDRESSED (3 entries tested, not v0..v10 shape) |
| Medium: txn dir cross-filesystem | PARTIALLY ADDRESSED (moved to .git, but .git is private + breaks in linked worktrees) |
| Medium: duplicate rollback | ADDRESSED |
| Regression: BATS contradiction | ADDRESSED |
| Regression: status --all | ADDRESSED |

### New issues (v3-introduced)

**[Critical] v0 escape hatch unreachable through dr-flip.sh enable.** Default behavior auto-computes restore-from from current lineage; bypasses v0 during the migration window when v0 is exactly what the operator needs. → v4 fix: `--restore-from-lineage <vN>` flag override.

**[High] v0 cleanup evidence goes stale between PR-open and merge.** ScheduledBackups can fail overnight. → v4 fix: merge-time evidence-gate CI job that re-runs the 3 checks against live cluster state. → v5 fix: CI job replaced with manual checklist + 24h attestation timestamp (no self-hosted runner exists).

**[High] CI gate awk wrong for R-classified rename rows + concrete command contradicts stated AMR filter.** → v4 fix: correct awk pattern + AMR honored in command.

**[Medium] Pre-created v0..v10 entries: §8 only tests 3, doesn't render-assert all 11.** → v4 fix: §8 step 13 explicit render assertion.

**[Medium] Banner per-app vs per-invocation.** Operator running `disable --all` hits 8× 3s delay. → v4 fix: emit once per invocation.

**[Medium] `--no-settle-warning` named flag for both CI bypass and human use.** Less safe than a "yes I verified" affirmation flag. → v4 fix: `--i-verified-post-recovery-base-backup` for humans; `--no-settle-warning` for CI only.

**[Medium] `.git/dr-flip-txn.XXXXXX` breaks in linked-worktree layouts where .git is a file.** → v4 fix: use `$REPO_ROOT/.dr-flip-txn.XXXXXX` + .gitignore entry. → v5 fix: switched to portable `mktemp -d` template form (no `-p`).

**[Medium] Banner reference "runbook §X" unresolved.** → v4 fix: pinned to `docs/cnpg-disaster-recovery.md#post-recovery-settle-checklist`.


---

## Appendix D — Codex review pass 4 (2026-05-20) — summary

Adversarial review by `codex:codex-rescue` on plan v4. 7 findings: 0 Critical, 3 High, 2 Medium, 2 Low. Verdict: NOT SAFE — but Codex explicitly noted convergence ("Pass 1: 12 → Pass 2: 9 → Pass 3: 8 → Pass 4: 3 High... We are converging").

### Pass-3 finding verification

All addressed or addressed-with-callout. See top-level v4 status banner.

### New issues (v4-introduced)

**[High] `--restore-from-lineage v11` silently passes regex `^v[0-9]+$` despite no v11 entry in overlay.** → v5 fix: bounded validation against MAX_LINEAGE=10.

**[High] Merge-time evidence gate uses fictional self-hosted runner.** This repo has no self-hosted runner registered; the job would queue indefinitely. → v5 fix: replaced auto-CI with manual PR-template + 24h attestation + branch protection.

**[High] `mktemp -d -p` fails on macOS.** Plan was GNU-only. → v5 fix: portable template form.

**[Medium] `--no-settle-warning` available to humans (intended CI-only).** → v5 fix: gated on `CI=true` OR `BATS_TEST=1`.

**[Medium] `disable` sleeps with unreachable Ctrl+C in non-interactive shells.** → v5 fix: TTY detection skips sleep.

**[Low] Attestation grep exact-case.** → v5 fix: `grep -Eiq` (only relevant if §6 evidence-gate becomes CI; manual checklist replaces it in v5).

**[Low] Appendix B and C referenced but not present.** → v5 fix: appended (this Appendix D + Appendices B/C above).
