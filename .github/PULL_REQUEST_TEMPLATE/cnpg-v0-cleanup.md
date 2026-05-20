<!-- cnpg-v0-cleanup-template -->
# CNPG v0 escape-hatch cleanup PR

This PR removes the v0 emergency-restore `externalClusters[]` entry from
`components/cnpg-cluster/recovery/bootstrap-patch.yaml` and flips the default
`CNPG_RESTORE_FROM_LINEAGE` to v1. See
[`docs/plans/cnpg-overlay-refactor.md`](../docs/plans/cnpg-overlay-refactor.md)
§6 for the full design + Codex review history.

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
  _Paste output:_

- [ ] **7 days of WAL continuity (no archive errors per cluster):**
  ```bash
  for cluster in joplin-db authentik-db dawarich-db zilean-db riven-db \
                 opencut-cnpg-db sparky-fitness-cnpg-db wiki-js-cnpg-db; do
    errs=$(kubectl cnpg status "$cluster" --output json | jq '.archiverStats.errors // 0')
    [ "$errs" = "0" ] && echo "✓ $cluster" || echo "✗ $cluster has $errs WAL errors"
  done
  ```
  _Paste output:_

- [ ] **Side-by-side restore from v1+ verified** on at least one non-trivial cluster (joplin or dawarich).
  Paste row-count comparison evidence (source vs restored):

## Attestation

By checking the boxes above and merging, I attest the evidence was
re-verified within the past 24 hours.

`evidence-window-attested-at: YYYY-MM-DD HH:MM UTC`

(Update this timestamp every time you re-verify. The
`cnpg-cleanup-attestation` CI check enforces ≤24h freshness AT CI RUN TIME
only — GitHub required checks don't re-run on merge.)

## Reviewer obligations (Codex pass-5 High fix; v6 honest scope)

Before clicking "Squash and merge":

- [ ] **Manually re-trigger** the `cnpg-cleanup-attestation` workflow via the GitHub UI ("Re-run all jobs" on the latest commit) and wait for it to pass green.
- [ ] **Update** the `evidence-window-attested-at` timestamp in the PR body if the prior value is approaching 24h.
- [ ] **Re-run the 3 evidence commands locally** within 1h of merge and confirm output matches.

These manual gates exist because GitHub required checks don't re-run on merge — a check that passed at hour 23.5 satisfies branch protection at hour 25+. Until this repo has a self-hosted runner OR merge queue, evidence freshness is a human gate.

## Changes in this PR

- [ ] Remove `${APP_RESTORE_FROM}-restore-v0` entry from `components/cnpg-cluster/recovery/bootstrap-patch.yaml`
- [ ] All 8 `apps/production/**/db-cnpg.yaml` set `CNPG_RESTORE_FROM_LINEAGE: v1` (was v0)
- [ ] Update `docs/cnpg-disaster-recovery.md` to mark v1 as the default restore lineage
- [ ] Update `components/cnpg-cluster/README.md` if v0-related content needs trimming
