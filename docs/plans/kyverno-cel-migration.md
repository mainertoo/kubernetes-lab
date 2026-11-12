# Kyverno: migrate the VolSync ClusterPolicies to CEL policy types

> **Status 2026-11-12: DONE in production.** Kyverno 1.20.0 (chart 3.10.0) runs five CEL
> policies (`policies/` + `policies-cel/`); no legacy `kyverno.io` policies remain.
> - Cutover via PRs #2197 (A), #2198 (B) and #2203 (C).
> - Adoption script: 219/219 objects across 73 PVCs adopted, UIDs unchanged. Against the pre-flight
>   baseline: 0 missing, 0 UID changes, 0 ReplicationDestination restore re-runs, 0 spec changes.
> - Drift on a production RS reverted in 1s.
> - Verified live: a fresh scratch PVC gets its 3 objects and no `dataSourceRef`. Restore drill:
>   recreating the retired `media/riven-data-pvc` got `dataSourceRef`, restored in 3 min, and the
>   data was Riven's real files. Obico, deployed mid-window, got its objects from the new
>   GeneratingPolicy.
>
> ⚠️ **Known Kyverno 1.20 issue, deleting a backup PVC that is still in use:** if the PVC sits in
> Terminating (a pod still holds it), the GeneratingPolicy can **recreate** its Secret/RS/RD for the
> deleted PVC, re-run the RD's restore Job, and keep recreating them when deleted by hand. Seen on
> the drill PVC. Fix:
> 1. `kubectl -n kyverno delete pod -l app.kubernetes.io/component=background-controller`
> 2. Delete the orphaned RS/RD/Secret.
> 3. Restart VolSync (`kubectl -n volsync-system delete pod -l app.kubernetes.io/name=volsync`) to
>   clear stale `volsync_kopia_repository_connectivity` series.
>
> Prevent it by stopping the pods before deleting a backup-labelled PVC. After any app retirement,
> check `kubectl get replicationsource -A` for leftovers.

## Problem

Kyverno **1.20** (chart 3.10.0) no longer executes legacy `kyverno.io` policies
(`ClusterPolicy`, `Policy`, `CleanupPolicy`, legacy `PolicyException`), and its
chart refuses to install while any exist. The Renovate bump (#2172) failed that
guard on 2026-11-12 without changing the cluster; #2174 rolled git back to chart
3.9.2 and holds Renovate below 3.10.

All three of our policies are legacy ClusterPolicies:

| Policy | Rules | Job |
|---|---|---|
| `volsync-pvc-backup-restore-kopia` | 7 (3 validate, 1 mutate, 3 generate) | Asks the pvc-plumber oracle whether a PVC has a backup; injects `dataSourceRef` so a rebuilt PVC restores; generates the per-PVC Kopia `Secret`, `ReplicationSource` and `ReplicationDestination` |
| `volsync-pvc-engine-required` | 1 validate (Enforce) | Denies backup-labelled PVCs without `backup-engine: kopia` |
| `volsync-mover-jitter` | 1 mutate (`failurePolicy: Ignore`) | Adds a random-sleep initContainer to VolSync mover Jobs |

The first one carries the whole label-driven backup system: today it owns **73
ReplicationSources, 73 ReplicationDestinations and 73 Secrets**. Never use the
chart's `upgrade.acknowledgeLegacyPoliciesNotEnforced=true`: it lets 1.20 install
and silently stops backup generation and restore-on-rebuild.

**Kyverno 1.19.2 (running now) already serves every replacement type** at
`policies.kyverno.io/v1` (`ValidatingPolicy`, `MutatingPolicy`,
`GeneratingPolicy`, …). So the migration happens entirely on 1.19, and the 1.20
upgrade comes afterwards as an ordinary bump. There's no deadline: 1.19 is a
supported release.

**Alternative considered:** pvc-plumber v4, whose operator replaces the Kyverno
backup policy. Still not viable: as of v4.0.2 its restore path is unreachable
code, so it would drop auto-restore (see memory
`project_pvc_plumber_v4_planned_upgrade`).

## The hard part: 73 objects of each kind that must not be recreated

Two existing behaviours turn a naive "delete old policy, add new policy" into an
outage plus a storm:

1. **Deleting a `synchronize: true` ClusterPolicy deletes everything it
   generated** (orphaning is off by default). Recreating them then:
   - starts **73 ReplicationSources at once** with no `lastSyncTime`: a
     fleet-wide cold-start herd, the PR #419 incident (memory
     `feedback_force_recreate_fleet_blast_radius`);
   - recreates **73 ReplicationDestinations**, each with `trigger.manual:
     restore-once`, so every one runs a fresh **restore of its full PVC from
     Garage** into its dest PVC: hundreds of GB of reads (Plex and Jellyfin alone
     are 150 GiB each). RD deletion also cascades their dest and cache PVCs.
2. **Generate rules are write-once.** Kyverno rejects edits to an existing generate
   rule (memory `feedback_kyverno_generate_rules_immutable`), so
   `orphanDownstreamOnPolicyDelete: true` may not be addable to the live
   ClusterPolicy. `generateExisting` was in-place mutable on 1.18, so this has to
   be tested, not assumed.

So the cutover must hand the **existing objects, same UIDs, same status** to the
new GeneratingPolicy. Whether and how a GeneratingPolicy adopts same-named
objects created by a legacy rule (they carry `generate.kyverno.io/policy-name`,
`rule-name` and `trigger-*` labels) is the main unknown, and gets settled in a
rehearsal before production.

## Target design

| New policy | Kind | Replaces | Notes |
|---|---|---|---|
| `volsync-pvc-engine-required` | `ValidatingPolicy` | engine-required rule | `validationActions: [Deny]`; same namespace excludes via `matchConstraints.excludeResourceRules`/`namespaceSelector` |
| `volsync-pvc-restore-datasource` | `MutatingPolicy` | rule 2 | CREATE only; `failurePolicy: Fail`; `webhookConfiguration.timeoutSeconds: 20` (pvc-plumber's Kopia timeout is 15s); oracle call via `http.Get("http://pvc-plumber.volsync-system.svc.cluster.local:80/exists/<ns>/<pvc>")` in `variables`; `ApplyConfiguration` patch sets `spec.dataSourceRef` only when `decision == "restore"`, authoritative, no `skip-restore`, no existing dataSourceRef |
| `volsync-pvc-restore-checks` | `ValidatingPolicy` | rules 1, 3, 4 | `validationActions: [Audit]` to match today (rule 1 must stay non-blocking); three `validations` with the deny logic inverted |
| `volsync-pvc-backup-objects` | `GeneratingPolicy` | rules 5, 6, 7 | `evaluation.synchronize.enabled`, `generateExisting.enabled`, and **`orphanDownstreamOnPolicyDelete.enabled: true` from day one**; one `generator.Apply` per object; YAML-template mode keeps RS/RD readable |
| `volsync-mover-jitter` | `MutatingPolicy` | jitter rule | Jobs `volsync-*` with `app.kubernetes.io/created-by: volsync`; `failurePolicy: Ignore`; adds the `jitter` initContainer (pin `busybox` to a tag while at it) |

Mapping notes:
- **Preconditions that call the oracle can't be `matchConditions`.** The API
  server evaluates those without Kyverno's libraries. They become `variables` and
  guards inside the patch and validation expressions. Label and namespace filters
  do go in `matchConstraints`/`matchConditions`.
- **The shared Kopia secret:** `resource.Get("v1", "secrets", "flux-system",
  "volsync-kopia-shared-base")`. Copy its base64 values straight into the
  generated Secret's `data` (no decode needed) and put the constants
  (`KOPIA_REPOSITORY`, `KOPIA_S3_*`, regions) in `stringData`. The generated
  Secret must stay byte-identical to today's, so diff it.
- **Schedule math:** `size(object.metadata.namespace) % 60` for the minute,
  `"*"` for hourly or `"2"` for daily for the hour. Storage-class branches
  (cephfs → `cephfs-backingsnapshot` + `ReadOnlyMany`, `cephfs-snapclass`; rbd →
  own class + `ReadWriteOnce`, `ceph-rbd-snapclass`); the
  `volsync.backup/cache-capacity` annotation defaults to `5Gi`.
- **Make the next change cheap:** put any value we might tune later (cache size,
  mover UID/GID, schedule hour) behind a PVC annotation with a default. Generated
  shape changes on a GeneratingPolicy should be editable (to be confirmed in
  rehearsal; that was the main pain with the legacy rule).
- **RBAC:** `volsync-policy-rbac.yaml` already grants the admission and
  background controllers what they need for RS/RD/Secrets. Check the CEL
  controllers use the same service accounts.

## Plan

> **Revised 2026-11-12 (option 1): the backup policy switches over AT the Kyverno 1.20 upgrade.**
> Phase 1 found that Kyverno 1.19.2's GeneratingPolicy can't safely sync (upstream
> [kyverno#16747](https://github.com/kyverno/kyverno/issues/16747), fixed by PR #16748 on `main`
> only, so in 1.20 and not in 1.19.2):
> 1. edits to generated **custom resources** (our RS/RD) are never reverted (`metadata.resourceVersion:
>    Invalid value: 0`); `useServerSideApply` doesn't help;
> 2. after a background-controller restart, a namespace whose generated set is incomplete makes
>    Kyverno **delete that kind's downstreams in every other namespace**. For us that could mean
>    fleet-wide RD deletion, dest-PVC cascade and a restore storm.
>
> So the GeneratingPolicy must not run on 1.19.2 in production. New order:
> - **Phase 3 (on 1.19):** only the engine gate and the mover jitter move to CEL. They have no
>   generate rules, so deleting their legacy versions removes nothing.
> - **The legacy `volsync-pvc-backup-restore-kopia` stays whole on 1.19**: restore-on-rebuild
>   (rule 2), the audit checks (1, 3, 4) and generation (5–7). Splitting it would mean editing a
>   generate ClusterPolicy (write-once).
> - **Phase 4 = the 1.20 upgrade itself:** in one change, upgrade the chart to 3.10 and add
>   `../policies-cel` (restore-datasource, restore-checks, backup-objects). On 1.20 the legacy
>   ClusterPolicy is inert, so it can no longer delete what it generated when it is removed. That
>   sidesteps the hardest phase 2 question (orphaning). The GeneratingPolicy's `generateExisting`
>   then has to **adopt** the 73 existing objects in place: same UIDs, no RD recreation. That is
>   what the staging rehearsal must prove on 1.20.
> - **Phase 2 is now a rehearsal of that exact change on staging:** staging runs the same policies
>   as production (legacy backup ClusterPolicy + CEL gate + CEL jitter), scratch PVCs get
>   legacy-generated objects, then staging takes the 1.20 + `policies-cel` change and we check
>   adoption.

### Phase 0: rehearsal environment (staging)
> **Status:** manifests in `infrastructure/controllers-staging/{volsync-crds,kyverno-rehearsal}/`
> plus `../controllers/kyverno` (patched to 1 replica, no PDBs/ServiceMonitors).
> The stub picks its answer from the PVC name: `*restore*`, `*nonauth*`,
> `*error*`, `*slow*` (25s, past the 20s webhook timeout), otherwise `fresh`.
Staging runs no Kyverno and no backup stack (memory notes this blocked the
pvc-plumber v4 soak too). The rehearsal doesn't need a working backup stack,
only realistic objects:
- Kyverno **1.19.2** (same chart and values as production), under
  `infrastructure/controllers-staging/`.
- VolSync **CRDs only, with no controller**, so generated RS/RD sit inert and
  nothing restores.
- A **pvc-plumber stub**: a tiny HTTP service answering `/exists/<ns>/<pvc>` with
  canned `restore`/`fresh`/error/non-authoritative responses, chosen per PVC name.
- A copy of `volsync-kopia-shared-base` with dummy values.

### Phase 1: write and unit-test the five policies
> **Status (2026-11-12):** policies in `infrastructure/controllers/kyverno/policies-cel/`,
> offline suite in `tests/` (`kyverno test infrastructure/controllers/kyverno/policies-cel/tests`, 10/10).
> - **Golden diff vs production:** the GeneratingPolicy was run over all 73 production backup PVCs
>   and compared with the live objects: **146/146 RS/RD specs identical, 73/73 Secrets identical**
>   (keys + every non-credential value). The legacy RD rule's `kopia.sourcePathOverride` is not in
>   the RD CRD, so the API server drops it; the new policy omits it to match what is stored.
> - **Excludes:** skip-restore, both CNPG labels, infra namespaces and missing/wrong engine labels
>   generate nothing.
> - **Jitter:** byte-identical to the legacy mutation (including a Job with an existing initContainer;
>   jitter goes first). ApplyConfiguration can't set the atomic `command` list, so it's a JSONPatch
>   with a no-duplicate guard.
> - **Engine gate:** same pass/fail set as the legacy policy.
> - **`http.Get` failure errors the policy**, so with `failurePolicy: Fail` an unreachable oracle
>   blocks the PVC, fail-closed like today (CLI). Live confirmation on staging pending.
> - A server-side dry run of a policy does **not** compile its CEL; only the CLI (or a real
>   admission) does.
> - **Live tests on staging (2026-11-12), scratch PVCs in `kyverno-rehearsal`, all cleaned up:**
>   - `restore` answer: `dataSourceRef` injected (`<pvc>-backup` RD); `fresh`/`nonauth`/`error`: none.
>   - Generated objects correct for rbd/daily and cephfs/hourly/10Gi cache (backingSnapshot + ROX,
>     minute = len(namespace) mod 60).
>   - Restore checks (Audit): `nonauth` and `error` flagged by check 1, `skip-restore` without a
>     reason flagged by check 4, everything else passes, no PVC blocked.
>   - Engine gate: a PVC without `backup-engine` is denied.
>   - **Fail-closed confirmed live:** the `slow` oracle (25s) times out the 20s webhook and the PVC
>     is rejected.
>   - skip-restore PVCs get no Secret/RS/RD.
>   - **PVC label/annotation updates do not recreate or even touch the generated objects** (UIDs
>     and resourceVersions unchanged); answers open question 5.
>   - A deleted generated RS is recreated within seconds; deleting the PVC removes all three objects.
>   - New objects are labelled `generate.kyverno.io/policy-name: volsync-pvc-backup-objects` with
>     no `rule-name` (legacy objects carry the legacy policy and rule names). Relevant to phase 2.
>   - ⚠️ **Drift is NOT reverted.** Editing a generated RS's schedule stayed edited; the background
>     controller logs `failed to revert downstream resource ... metadata.resourceVersion: Invalid
>     value: 0: must be specified for an update` (Kyverno 1.19.2). The legacy policy reverted such
>     edits in seconds. Next: test `spec.useServerSideApply: true` (server-side apply needs no
>     resourceVersion) and check upstream for a fix. Not a blocker for backups (nothing edits the
>     RS/RD specs), but it is a regression in self-healing.
>   - Not tested live: the jitter MutatingPolicy (needs a test Job; its offline output is
>     byte-identical to legacy) and editing the policy in place (needs a policy change via git).
- `kyverno test` suites with fixtures for every branch:
  - restore / fresh / oracle error / non-authoritative
  - `skip-restore` with and without a reason
  - CNPG label excludes and namespace excludes
  - hourly vs daily; cephfs vs rbd; the cache-capacity annotation
  - Jobs that are and aren't VolSync movers
- **Golden diff:** for the same set of PVCs, render the legacy rules' output and
  the new policies' output and diff them. RS/RD/Secret specs must match exactly.

### Phase 2: rehearse the cutover with legacy objects present
On staging, install the legacy ClusterPolicies, create ~10 scratch PVCs so the
legacy rules generate their objects, and record every UID and `status`. Then
find the cutover technique that **keeps those exact objects**, trying in order:
1. `kubectl apply --dry-run=server` adding `orphanDownstreamOnPolicyDelete: true`
   to the legacy generate rules. If accepted, apply it, add the GeneratingPolicy,
   then delete the legacy policy.
2. Otherwise: scale `kyverno-background-controller` to 0, strip the
   `generate.kyverno.io/*` labels from the downstream objects, delete the legacy
   policy, add the GeneratingPolicy, and scale the controller back up.
3. Whatever adoption behaviour the GeneratingPolicy shows (adopt in place,
   conflict, or delete and recreate) decides which of the above works.

**Pass criteria, all required:**
- Every RS/RD/Secret keeps its UID.
- RS `lastSyncTime` and RD `lastManualSync` are unchanged.
- No new `restore-once` run.
- New PVCs still generate all three objects.
- Deleting a scratch PVC still cleans up its objects.
- Editing a value in the GeneratingPolicy updates the generated objects in place.

Also rehearse rollback: re-apply the legacy policies over the new ones without
recreating objects.

> **Rehearsal findings (staging, 2026-11-12):**
> - **Step 1 (1.20 upgrade with the acknowledge flag) is safe:** all 9 legacy-generated objects kept
>   their UID, resourceVersion and labels. The legacy ClusterPolicy reports `policy is not enforced`,
>   and a new restore-named PVC got no `dataSourceRef` and no objects (inert). The CEL engine gate
>   still denies on 1.20.
> - ⚠️ **On 1.20 Kyverno denies create *and update* of legacy ClusterPolicies**, so Flux's dry-run of
>   the unchanged legacy file fails and the whole Kustomization containing it stalls (staging's
>   `infra-controllers` did). In production that is `kyverno-policies`. So the production cutover is
>   **two PRs**:
>   1. Chart 3.10 + `upgrade.acknowledgeLegacyPoliciesNotEnforced: true` (in `infra-controllers`).
>      `kyverno-policies` then stalls, without deleting anything.
>   2. Minutes later: delete `policies/volsync-pvc-backup-restore-kopia.yaml` **and** add
>      `../policies-cel` in the same commit.
>
>   Never one PR: if Flux pruned the legacy policy while Kyverno was still 1.19, it would delete
>   all 73 generated object sets.
> - Between the two PRs, new PVCs get no backup objects and no restore-on-rebuild. Keep the gap to
>   minutes, with no app deploys.

### Phase 3: production, low-risk policies (on 1.19)
> **Status:** one PR moves the CEL `volsync-mover-jitter` (MutatingPolicy) and
> `volsync-pvc-engine-required` (ValidatingPolicy) into `policies/`, replacing the legacy files of the
> same names. Flux applies the new ones before pruning the old, so there is no gap. Staging now
> includes the same `policies/` folder.
- Verify the engine gate with a server dry-run of a PVC missing `backup-engine` (must be denied).
- Verify jitter on the next mover Job (hourly backups: grafana, vaultwarden): one `jitter`
  initContainer, first in the list.

### Phase 4: production, the backup policy at the 1.20 upgrade (quiet window)
> **Rehearsed end to end on staging (2026-11-12).** Adoption findings:
> - On 1.20 the GeneratingPolicy **ignores** objects carrying the legacy policy's generate labels.
>   Touching their PVC doesn't change that.
> - The `protect-generation-labels` webhook blocks relabelling them. Chart 3.10's
>   `features.protectGenerationLabels.enabled: false` (kyverno#18222) removes it.
> - Relabel (`policy-name=volsync-pvc-backup-objects`, add `trigger-name=<pvc>`, drop `rule-name` and
>   `policy-namespace`), then **one PVC event**. The GeneratingPolicy then adopts the objects
>   **in place**: Secret/RS/RD UIDs unchanged, edits reverted in about 2s from then on, PVC deletion
>   removes all three. Relabelling without the PVC event is not picked up.
> - `scripts/kyverno/adopt-legacy-generated.py` does that in bulk. Rehearsed on staging: 6/6
>   relabelled with unchanged UIDs, then drift on an adopted RS and RD reverted in 2s, and a rerun
>   finds nothing (idempotent). Its production dry run (read-only) finds **219 objects across 73 PVCs,
>   0 orphans**, and its pre-flight refuses to apply while production is still on 1.19.

**Runbook (one quiet window, no app deploys; pick a time away from the 02:xx backup wave):**
0. **Pre-flight:**
   - Last nightly backups OK.
   - `python3 scripts/kyverno/adopt-legacy-generated.py --context production` (dry run) shows 219 / 73 / 0.
   - Circuit breaker ready: `kubectl -n volsync-system scale deploy volsync-system-volsync --replicas=0`.
1. **PR A:** in `infrastructure/controllers/kyverno/kyverno-release.yaml`, set chart `3.10.0` plus
   `upgrade.acknowledgeLegacyPoliciesNotEnforced: true` and
   `features.protectGenerationLabels.enabled: false`. Remove the Renovate `<3.10.0` hold.
   - Wait for the HelmRelease to be Ready on 1.20.0.
   - Check the 73 RS / 73 RD / 73 Secret counts are unchanged.
   - `kyverno-policies` now stalls on the legacy policy. That is expected and deletes nothing.
2. **PR B (immediately after):** delete `policies/volsync-pvc-backup-restore-kopia.yaml` and add
   `../policies-cel` to the production kyverno-policies path.
   - Reconcile; `kyverno-policies` Ready.
   - The legacy ClusterPolicy is pruned; the counts are still 73/73/73.
3. **Adopt:** `python3 scripts/kyverno/adopt-legacy-generated.py --context production --apply`.
   It must report 219/219 with unchanged UIDs. Drift-test one RS, which must revert within seconds.
4. **PR C:** `features.protectGenerationLabels.enabled` back to the default (true).
5. **Verify:**
   - No ReplicationDestination restore Jobs started.
   - A scratch PVC gets Secret/RS/RD and is cleaned up on delete.
   - **Restore drill:** create a PVC whose name has an existing backup and confirm `dataSourceRef` and the data.
   - The next backup wave runs normally.

**Gaps to keep short:**
- Between A and B, new PVCs get no backup objects and no restore-on-rebuild.
- Between B and step 3, existing objects are unmanaged (backups still run).
- **Never** remove the legacy file before PR A is live: on 1.19 that would delete all 219 objects.

### Phase 5: finish
- `sum(kyverno_legacy_policies_total) == 0` (the metric 1.19 exposes); no
  `LegacyPolicyPresent` events.
- Remove the Renovate `<3.10.0` hold and take the 1.20 bump as a normal PR. The
  chart guard then passes.
- Update `infrastructure/controllers/kyverno/policies/README.md`,
  `docs/label-driven-backups.md`, the backup docs and the related memory notes.

## Rollback
- **Phases 3–4:** revert the PR. Re-adding the legacy policies is safe *only if*
  the Phase 2 rehearsal showed they re-adopt the existing objects. If they don't,
  the rollback is to keep the new policies and fix forward.
- **Mid-cutover herd or restore storm:** scale the VolSync controller to 0, then
  sort out the objects before scaling back up.

## Open questions (answered in Phase 2)
- Does a GeneratingPolicy adopt existing same-named objects created by a legacy
  rule, or conflict with them?
- Can `orphanDownstreamOnPolicyDelete` be added to the live legacy rules in place?
- Is a GeneratingPolicy's generated shape editable later, unlike legacy generate
  rules?
- `http.Get` failure semantics: does an unreachable oracle error the webhook (so
  `failurePolicy: Fail` blocks the PVC), matching today's fail-closed behaviour?
- Trigger UPDATE: the docs list "Modify Trigger → Downstream deleted" for a
  synchronized data source. Confirm that ordinary PVC updates (labels,
  annotations, resize) do **not** delete and recreate RS/RD.
