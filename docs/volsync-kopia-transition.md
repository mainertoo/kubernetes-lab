# VolSync Kopia Transition — Plan

**Status:** DRAFT v2 — incorporates Codex adversarial review v1 findings (see §10). Ready for user sign-off before Phase 1 starts.
**Goal:** Replace the Restic-based data path of the label-driven backup system with Kopia, matching the upstream `mitchross/talos-argocd-proxmox` reference design that this project was originally adapted from.
**Trigger:** 2026-05-17 cluster incident — see [`docs/volsync-storage-recovery.md`](./volsync-storage-recovery.md) §10 and the project memory `project_volsync_label_driven_restore.md`.

> **What changed v1 → v2:** Codex flagged 6 HIGH and 4 MED issues. Most consequential: (a) Restic policy MUST be engine-gated before the Kopia policy is deployed or the two will fight over the same Secret/RS/RD names; (b) Phase 5 Stage B MUST verify a successful manual Kopia snapshot before declaring an app migrated, otherwise cold-start leaves a 0–24h restore gap; (c) `mover-kopia/entry.sh` MUST be audited in Phase 0 before any commitment — we don't actually know yet whether Kopia's volsync wrapper has its own Restic-`--retry-lock=0s`-equivalent trap. All findings applied below.

---

## 1. Why we're transitioning (the short version)

The original design ([`volsync-storage-recovery.md`](./volsync-storage-recovery.md)) §2.1 considered Kopia and **explicitly deferred it as a separate project**:

> *"Switch from restic to Kopia — Kopia's CDC dedup is strictly better than restic's, but compounds two big migrations (mover + admission). Deferred to a separate project."*

This is that separate project, executed earlier than originally hoped because **Restic's locking model is fundamentally incompatible with the shared-repo + multi-writer pattern the design depends on**:

| Property | Kopia | Restic |
|---|---|---|
| Concurrent writers | Built-in | Exclusive lock per write op |
| `forget`/`prune` | Concurrent-safe | Requires exclusive lock |
| Per-RS retain at scale | Works fine | Cascading lock contention |
| Mover image fork required | No | Yes (to skip the always-on forget step — see `feedback_volsync_default_forget_trap`) |

Restic-specific incidents this project hit (all chronicled in `project_volsync_label_driven_restore.md`):

| Date | Symptom | Underlying cause |
|---|---|---|
| 2026-05-15 | pvc-plumber 503 outages | Restic's per-call lock acquisition + HTTP timeouts |
| 2026-05-16 | pvc-plumber lock leak (167 stale locks) | Restic acquires lock even for reads |
| 2026-05-17 AM | Mover-vs-mover storm after PR #419 force-recreate | `restic forget --retry-lock=0s` exclusive-lock contention |
| 2026-05-17 PM | `--keep-last 1` destructive default after PR #429 | Volsync's controller falls back to a destructive forget policy when `retain:` is unset |

The mover-image fork is technically workable (~2 hr) but signs us up for indefinite maintenance against an upstream tool whose design intent we're working around. Kopia just works the way the design assumes.

---

## 2. What stays the same vs. what changes

### Stays the same — ~70% of the work to date is preserved

- **The whole label-driven UX.** `backup: hourly | daily` on a PVC. The user-facing contract in [`docs/label-driven-backups.md`](./label-driven-backups.md) is unchanged.
- **The Kyverno admission policy structure** — 7 rules, same matches/excludes, same skip-restore semantics. Only rules 5/6/7's `generate.data` swaps from `spec.restic:` to `spec.kopia:`.
- **The bootstrap chain** — `volsync-bootstrap` Flux Kustomization, dependsOn ordering, probe Job pattern.
- **The migration driver** — `scripts/migrate-stage-bc.sh` already exists, will be reused with Kopia secret names.
- **All Phase 5 cutover protocol** — Stage A / Stage B / Stage C, the per-app sequence, the trap-based rollback paths.
- **The 10-min cluster-rebuild target.** Restore path is RD → CSI populator regardless of mover type.
- **Garage S3 as the storage backend** — Kopia supports S3 natively.

### Changes — the data-path layer

| Layer | Was (Restic) | Becomes (Kopia) |
|---|---|---|
| Mover engine | volsync 0.15.0 + `mover-restic/entry.sh` | volsync 0.15.0 + `mover-kopia/entry.sh` |
| pvc-plumber | Our fork `mainertoo/pvc-plumber-restic` (~600 LOC restic CLI shell-out) | **Upstream `mitchross/pvc-plumber`** — pin a digest, retire our fork |
| Per-PVC Secret | `volsync-<pvc>` with `RESTIC_*` env vars | `volsync-<pvc>` with `KOPIA_*` env vars + S3 creds |
| Shared repo | `s3://garage.lab.mainertoo.com/volsync-shared/restic` | `s3://garage.lab.mainertoo.com/volsync-shared/kopia` (new bucket OR new prefix) |
| Per-mover forget | Hardcoded in entry.sh, always runs | Kopia's `gc`/maintenance is concurrent-safe; no contention |
| Central forget CronJob | `volsync-restic-forget` (deployed PR #429) | **DELETE** — Kopia doesn't need it |
| Snapshot tagging | `<ns>/<pvc>` restic tag + `RESTIC_HOSTNAME=<ns>/<pvc>` | Kopia source path identifier (TBD — verify upstream pvc-plumber convention) |
| Snapshot format | Restic packfile format | Kopia content-addressable format — **NOT cross-readable** |

### Stays in place during transition (then retired)

- The Restic shared repo (`volsync-shared/restic`) — kept read-only until Phase 6
- The `pvc-plumber-restic` deployment — kept running until Phase 5 cutover completes; queries serve the apps still on Restic
- The `volsync-restic-forget` CronJob — suspended at start of Phase 6, deleted at end

---

## 3. Phase plan

Each phase leaves the cluster in a working state. No phase combines unrelated changes. Time estimates assume single-operator focused sessions.

### Phase 0 — Plan + audit + sign-off (tonight + tomorrow AM, ~3 hr)

**Output:** this document at v2+, with verified-by-source-reading answers to "does Kopia have an equivalent of Restic's locking trap?" question before any execution begins.

**Activities:**
1. ✅ Codex adversarial review of v1 — done (see §10)
2. ✅ Apply findings → v2 — done (this document)
3. **🔜 Audit `mover-kopia/entry.sh` at the volsync release tag we run** — see "Phase 0 mandatory audit" below
4. **🔜 Decide remaining open questions** — only 3 left after v2 closed #2 and #5 (see §6)
5. **🔜 User sign-off** on the v2 plan
6. **Out of band:** stabilize the live cluster by re-adding the original `retain:` to live rule 6 via a one-line PR (prevents accidental `--keep-last 1` destruction if volsync is ever re-enabled before Phase 5 starts)

**Exit criteria:** all four "🔜" items complete. Kopia mover audit confirms there is no analog of Restic's `--retry-lock=0s` trap, OR if one exists, mitigation is folded into Phase 3.

**Phase 0 mandatory audit (Codex finding [10]):**
```bash
git clone https://github.com/backube/volsync /tmp/volsync
git -C /tmp/volsync checkout v0.15.0
grep -n "kopia\|maintenance\|timeout\|retry\|server\|connect\|snapshot\|policy" \
  /tmp/volsync/mover-kopia/entry.sh
```
Specifically confirm:
- Does Kopia maintenance run **inline** during snapshots, or in a separate CronJob?
- Do concurrent snapshotters share a **server connection**, or each open a direct repo connection?
- Are there hardcoded **connection timeouts** or **retry knobs** with destructive defaults (cf. Restic's `--keep-last 1` from `feedback_volsync_default_forget_trap`)?
- Does the volsync controller pass any **always-on flag** to the mover that locks behavior we'll regret?

**If the audit surfaces a trap:** STOP. Re-evaluate. The mover-image-override hook (`RELATED_IMAGE_KOPIA_CONTAINER` per `feedback_volsync_mover_image_override`) is available as a fallback, but if Kopia needs forking too, the strategic argument for transitioning gets weaker. Pause the project and reconsider.

---

### Phase 1 — Kopia repo + creds (0.5 day, ~3 hr)

**Output:** Operational Kopia repo on Garage, SOPS Secret `volsync-kopia-shared-base` in `flux-system`.

**Activities:**
1. Decide bucket scope:
   - **Option A:** new bucket `volsync-kopia-shared` (clean separation, easier to nuke later)
   - **Option B:** reuse `volsync-shared` bucket, prefix `/kopia` (less Garage admin work)
   - *Lean: A. Cleaner Phase 6 decom.*
2. Generate Kopia master password (`openssl rand -base64 32`) — 1Password personal vault, item "volsync-kopia repository master password"
3. Garage IAM key scoped to new bucket (`garage bucket allow --read --write`)
4. One-shot Job: `kopia repository create s3 ...` against the new bucket (mirrors the Phase 1 restic init pattern)
5. SOPS-encrypt `infrastructure/secrets-prod/volsync-kopia-shared-base.sops.yaml` with: `KOPIA_PASSWORD`, `S3_ENDPOINT`, `S3_BUCKET`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`
6. Smoke test: hand-run a `kopia snapshot create` against a scratch volume, verify with `kopia snapshot list`

**Exit criteria:** `kopia snapshot list` against the shared repo returns the smoke-test snapshot. Secret reconciled by Flux at `infra-secrets-prod` Kustomization.

**Rollback:** delete the Garage bucket; revert the SOPS file.

---

### Phase 2 — pvc-plumber retreat to upstream (0.5–1 day, ~4 hr)

**Output:** `pvc-plumber` deployment in `volsync-system` running upstream `mitchross/pvc-plumber` image with Kopia backend. Our `pvc-plumber-restic` fork is parked (kept running for the duration of Phase 5 coexistence; deleted in Phase 6).

**Activities:**
1. Read the latest `mitchross/pvc-plumber` upstream — confirm Kopia client lifecycle still works
2. Build/pin an upstream image digest reference at `ghcr.io/mitchross/pvc-plumber:<digest>`
3. New manifests under `infrastructure/controllers/pvc-plumber/` (next to existing `pvc-plumber-restic/`)
4. Deployment uses the new `volsync-kopia-shared-base` Secret
5. Smoke test: `kubectl port-forward` + `curl /readyz` and `curl /exists/foo/nonexistent` returns `decision: fresh, authoritative: true, backend: kopia-*`
6. Add `pvc-plumber` to `infrastructure/controllers/kustomization.yaml`
7. **Both pvc-plumbers run concurrently** during Phase 5. Each app's Kyverno-generated Secret points at the right backend.

**Exit criteria:** upstream pvc-plumber Ready=True in cluster; oracle responds. Existing `pvc-plumber-restic` continues to serve restic-backed apps unchanged.

**Rollback:** comment the new component out of the controllers kustomization; everything reverts.

---

### Phase 3 — Kyverno policy split with engine-gating (1 day, ~5 hr)

**Decision (closes §6 Open Question #5, per Codex finding [8]):** **Two separate ClusterPolicies, both explicitly engine-gated**, with a deny-on-missing-engine validate policy installed FIRST. Single-policy approach was rejected because edits to one would trigger a full UpdateRequest burst across all 70 PVCs (cf. today's incident).

**Output:** Three ClusterPolicies live in Audit mode in this order:
1. `volsync-pvc-engine-required` — validate-only deny rule, ensures every backup-labeled PVC carries a valid `backup-engine` label
2. `volsync-pvc-backup-restore-restic` — RENAMED + engine-gated copy of today's policy, matches only `backup-engine: restic`
3. `volsync-pvc-backup-restore-kopia` — new policy, matches only `backup-engine: kopia`, generates Kopia-flavored Secret/RS/RD

**Activities (strict order):**

1. **Step 3a — backfill `backup-engine: restic` on every currently labeled PVC.** Done as a single PR before any policy change. All 42 currently Kyverno-managed PVCs get the label added by hand-editing the per-app PVC manifests. Verify with: `kubectl get pvc -A -l backup --no-headers | wc -l` matches the count of `kubectl get pvc -A -l backup-engine=restic --no-headers | wc -l` after reconcile.

2. **Step 3b — deploy the deny-on-missing-engine validate policy** (`volsync-pvc-engine-required`), validationFailureAction: **Audit** for now. This policy denies (audit-mode = audit-logs) any PVC with `backup ∈ {hourly, daily}` that lacks `backup-engine ∈ {restic, kopia}`. Closes Codex finding [3] / [6]: makes the engine label MANDATORY, never inferred by webhook.

3. **Step 3c — engine-gate the existing Restic policy in place** by adding the selector `backup-engine: restic` to every rule's `match.any.resources.selector.matchExpressions`. This is a label-selector tightening, NOT a `generate.data` mutation — it should NOT be one of Kyverno's immutable-fields per `feedback_kyverno_generate_rules_immutable`. **Verify with `kubectl diff`** before merging. **Rename** the policy at the same time to `volsync-pvc-backup-restore-restic`. *Codex finding [1] / [2]: this step prevents collision and avoids force-delete churn.*

4. **Step 3d — deploy `volsync-pvc-backup-restore-kopia`** as a new ClusterPolicy file. Identical structure to the Restic policy (rules 1–7), with:
   - Engine selector: `backup-engine: kopia`
   - Oracle URL: routes to the upstream `pvc-plumber` Service (deployed in Phase 2)
   - Rule 5 generates Secret with `KOPIA_PASSWORD` + S3 creds (not `RESTIC_*`)
   - Rule 6 generates RS with `spec.kopia:` block
   - Rule 7 generates RD with `spec.kopia:` block
   - **`synchronize: true` and `generateExisting: true` retained** — same drift-correction semantics as the Restic policy

5. **Step 3e — validate on a scratch PVC** in a non-production namespace (e.g., `default` with policy exclude tweaked, OR a one-off test namespace). Label it `backup: daily, backup-engine: kopia`. Verify Kyverno generates the Kopia Secret/RS/RD trio. Manually trigger a backup via `manual:` trigger to confirm Kopia mover writes to the shared repo. **Do NOT validate on a real Phase 5 candidate yet — Phase 5 handles cutover protocol separately.**

**Exit criteria:** Three policies live in Audit mode. Existing 42 Restic-backed apps continue to operate unchanged (Restic policy still gates them via their newly-added `backup-engine: restic` label). A scratch test PVC successfully generates a Kopia trio.

**Codex finding [7] — mid-flight engine switching hazard:** Because both Restic and Kopia generate rules have `synchronize: true`, changing `backup-engine: restic` → `kopia` on a live PVC would cause Kyverno's Restic policy to GC its children AT THE SAME TIME as the Kopia policy emits new ones — a window where the PVC's `dataSourceRef` points at a deleted RD. This is the same hazard the original Phase 5 (Restic) addressed with Flux suspension + driver-managed cutover. The Phase 5 protocol below introduces a transient `backup-engine: migrating` state (excluded from both policies) so the switch is never atomic.

**Rollback:** Three independent policies, three independent rollbacks. Delete the Kopia policy alone, or delete the deny-validate policy alone, or revert the Restic-policy engine-selector PR. No big-bang rollback required.

---

### Phase 4 — History migration strategy (decision locked, ~1 hr — execution folded into Phase 5)

**Decision (closes §6 Open Question #2, per Codex findings [4] / [9]):** **Cold start, with a per-app gating discipline.** Each app's Stage B in Phase 5 MUST verify a successful manual Kopia snapshot BEFORE Restic authority is suspended/deleted. No app is "migrated" until its first Kopia snapshot is on disk in the shared repo.

Alternatives were:
- **Pure cold start (rejected as bare):** Codex flagged that daily-schedule apps cut over after 02:MM UTC have a full-day window with no Kopia restore point. Unacceptable for high-value apps (authentik db, vaultwarden, joplin, home-assistant).
- **Bridge migration (deferred):** `restic restore` each retention-relevant snapshot to a tmpdir, then `kopia snapshot create`. 2-3 days of bespoke tooling against ~9000 snapshots across 41 apps. High effort, low marginal value — old Restic repo serves as the historical archive instead (see emergency runbook §9).

**Implementation (in Phase 5 driver):**

For each app, the cutover order is:
1. Add `backup-engine: migrating` label (transient state, matched by neither engine policy — Codex finding [7])
2. **Trigger a one-shot manual Kopia snapshot** on a temporary Kopia RS pointed at the same PVC. Wait for completion. Verify the snapshot exists in the shared repo via `kopia snapshot list`.
3. Only then: delete the Restic-side RS, set `backup-engine: kopia` label, let the Kopia policy generate the durable RS/RD/Secret.

The driver gate script (sketch — refined in Phase 5):
```bash
kubectl -n "$NS" apply -f - <<EOF
apiVersion: volsync.backube/v1alpha1
kind: ReplicationSource
metadata: { name: "${PVC}-cutover-probe", namespace: "${NS}" }
spec:
  sourcePVC: "${PVC}"
  trigger: { manual: "cutover-$(date +%s)" }
  kopia:
    repository: "volsync-kopia-shared-base"
    copyMethod: Snapshot
    # … storage class etc. matched to source PVC
EOF
kubectl -n "$NS" wait --for=condition=Synchronized --timeout=30m \
  replicationsource.volsync.backube "${PVC}-cutover-probe"
# If wait fails: abort cutover, leave the Restic RS in place.
kubectl -n "$NS" delete replicationsource.volsync.backube "${PVC}-cutover-probe"
```

**Recovery-depth guarantee per app post-cutover:** ≥1 Kopia snapshot exists at the moment Stage B completes. Subsequent scheduled backups grow the chain normally. Emergency historical restore from the read-only Restic repo remains available for 30 days (see emergency runbook).

**High-value app cutover-window discipline:** Apps in this list should be cut over in the window IMMEDIATELY after their last successful Restic backup, minimizing data drift between final Restic snapshot and first Kopia snapshot:
- `authentik/authentik-db` (CNPG, but the auth metadata PVC matters)
- `vaultwarden/vaultwarden` (passwords)
- `home-assistant/home-assistant` (Z-Wave/Zigbee state, automations)
- `joplin/joplin` (notes)
- `dawarich/dawarich` (location history)
- `wiki-js/wiki-js-data` (content)

**Exit criteria:** decision locked at "cold start with manual-snapshot gate". Probe-RS template added to the driver design. No execution-time work in Phase 4 — it's all inside each Phase 5 cutover.

---

### Phase 5 — Per-app cutover Restic → Kopia (~15 min/app, **3-4 days** for all 70)

**Output:** Every app's PVC carrying `backup: hourly|daily` has `backup-engine: kopia` and is generating snapshots into the Kopia shared repo. Restic-side RSes deleted per-app. Each app has ≥1 verified Kopia snapshot before its cutover is marked complete.

**The cutover protocol — with the transient `migrating` state (Codex finding [7]):**

For each app, the sequence is:

1. **5a PR — set transient label.** Edit the app's PVC manifest: `backup-engine: restic` → `backup-engine: migrating`. Merge.
   - The Restic policy no longer matches → `synchronize:true` GCs the app's Restic Secret/RS/RD on its own schedule. **Wait for this GC to settle** (poll `kubectl get rs,rd,secret -n <ns> -l app.kubernetes.io/managed-by=kyverno` until empty for this app's resources).
   - The Kopia policy doesn't match either → no new Kopia resources yet.
   - The deny-on-missing-engine validate policy does NOT trip — `migrating` is in the allow set we add to it in Phase 3.

2. **Stage B Job — driver `scripts/migrate-stage-bc.sh --engine kopia` (extended in Phase 5 prep).** The driver:
   - `flux suspend kustomization` for the app's namespace
   - Confirm Restic-side resources are gone (preflight check)
   - **Apply the cutover-probe Kopia RS** (template from Phase 4 above), wait for first Kopia snapshot to land. If wait fails: abort with `set -e` trap, leave PVC in `migrating` state — operator picks up next session.
   - Delete the cutover-probe RS
   - Set PVC label `backup-engine: kopia`
   - Exit with Flux still suspended

3. **5b PR — confirm static PVC manifest.** Verify the app's PVC manifest in git has `backup-engine: kopia` (the live cluster does, but the manifest needs to match for next Flux reconcile to be a no-op). Merge while Flux is suspended.

4. **Stage C Job — driver resumes Flux.** Standard Stage-C verification (positive: PVC label is `kopia` in `flux build kustomization`; negative: no Restic RS for this PVC in rendered manifests). Resumes only on success.

**Why each step is necessary (vs. atomic label swap):** Codex finding [7]. With `synchronize: true` on both engine policies, a single-edit swap `restic → kopia` would cause Kyverno's Restic policy to GC its children and Kyverno's Kopia policy to emit new children **at the same time**. The PVC's `dataSourceRef` would briefly point at a deleted RD. The `migrating` intermediate state, plus Flux suspension during Stage B, plus the cutover-probe pre-flight, eliminates the window.

**Order (3 batches):**
1. **High-value first** (6 apps, ~1 session): authentik, vaultwarden, home-assistant, joplin, dawarich, wiki-js. Each cut over immediately after its last successful Restic backup window. Per-app verification on the cutover-probe Kopia snapshot.
2. **Already-on-Restic medium** (36 remaining of the 42, ~3 sessions): alphabetical batches of 12.
3. **Never-on-Restic** (29 apps, ~3 sessions): direct-to-Kopia cutover, batched alphabetically. These never get a Restic RS — Phase 5a label is set to `backup-engine: kopia` immediately, Stage B still runs the cutover-probe.
4. **Big-data deferrals last** (5 apps: plex, jellyfin, calibre-library-cephfs, shared-media-pvc, notifiarr-shared, dumb): cut over last with appropriate cache-capacity annotations.

**Pace target:** 5-7 apps per session × 6-8 sessions across 3-4 days. Each cutover ~15 min (was ~10 min for Restic Phase 5 — adding the probe-wait costs ~3-5 min per app, sometimes more for large PVCs).

**Exit criteria:** Every backup-labeled PVC has a Kopia-backed RS. Zero Kyverno-managed Restic RSes remain. Every app has ≥1 verified Kopia snapshot in the shared repo (verifiable via `kopia snapshot list --tags ns:* --tags pvc:*` or whatever Kopia's listing convention is — confirmed in Phase 0 audit).

**Rollback per app (within 30-day Restic-bucket retention window):** Revert the 5a PR (`backup-engine: kopia` → `restic`); the cutover-probe Kopia snapshot remains in the Kopia repo but unreferenced. The Restic policy regenerates the Restic Secret/RS/RD when the label flips back. Re-runs Restic's `dataSourceRef` populator if the PVC is deleted+recreated.

---

### Phase 6 — Decommission the Restic stack (0.5 day, ~3 hr)

**Output:** Restic infrastructure removed from the cluster. `volsync-shared/restic` Garage bucket retained in cold storage for 30 days as emergency historical archive.

**Activities:**
1. Suspend `volsync-restic-forget` CronJob (don't delete yet)
2. Suspend `pvc-plumber-restic` Deployment (replicas → 0)
3. Delete the Restic Kyverno policy (`volsync-pvc-backup-restore-restic` per Phase 3 option 6b)
4. Wait 24h. Verify no app is broken.
5. PR: delete `infrastructure/controllers/pvc-plumber-restic/` directory entirely
6. PR: delete `infrastructure/controllers/volsync/app/volsync-restic-forget-cronjob.yaml`
7. PR: delete `infrastructure/secrets-prod/volsync-shared-base.sops.yaml` (the old Restic creds)
8. Mark `mainertoo/pvc-plumber-restic` GitHub repo archived
9. Garage: keep `volsync-shared` bucket read-only for 30 days, mark for deletion 2026-06-17

**Exit criteria:** No references to restic in `infrastructure/` (excluding archived docs). Cluster fully on Kopia.

**Rollback:** within 30 days, restore from the archived bucket by reverting the deletion PRs. After 30 days, no rollback (bucket gone).

---

### Phase 7 — DR drill + design doc update (0.5 day, ~3 hr)

**Output:** Verified 10-min cluster-rebuild on the staging cluster. `docs/volsync-storage-recovery.md` updated to reflect Kopia reality.

**Activities:**
1. Staging cluster: bootstrap from scratch (`flux bootstrap` + sops-age paste)
2. Apply one labeled PVC manifest
3. Time: PVC create → snapshot restore → pod Running
4. **Target: ≤10 min total, including the manual sops-age paste**
5. If target missed: analyze where time is spent, address before declaring done
6. Update `docs/volsync-storage-recovery.md` §2.1: Kopia is now the implementation
7. Update `docs/label-driven-backups.md` if any user-facing semantics changed
8. Memory: mark `project_volsync_label_driven_restore.md` as COMPLETE 2026-05-XX

**Exit criteria:** 10-min DR drill passes on staging. Docs reflect reality.

---

## 4. Total time + calendar estimate

| Phase | Effort | Calendar |
|---|---|---|
| 0 — Plan + review | 2 hr | Tonight + tomorrow AM |
| 1 — Kopia repo + creds | 3 hr | Day 1 PM |
| 2 — pvc-plumber retreat | 4 hr | Day 2 |
| 3 — Kyverno policy v3 | 3 hr | Day 2 |
| 4 — History decision | 1 hr decision; 0 days (Option A) | Day 2 (combined with above) |
| 5 — Per-app cutover (70 apps) | ~12 hr | Days 3–5 |
| 6 — Decom Restic | 3 hr (+24h soak) | Day 6 |
| 7 — DR drill + docs | 3 hr | Day 7 |
| **Total focused work** | **~30 hr** | |
| **Calendar (split-time sessions)** | | **~1.5 weeks** |

For reference, the original Restic build was ~6 weeks calendar (Phases 0–5 from 2026-04-05 → 2026-05-16). Re-using the structural work makes this much faster.

---

## 5. What we do tonight before sleeping

1. **Save volsync state.** Volsync controller stays at 0. Don't re-enable.
2. **Restore destructive `retain:` default mitigation.** One-line PR to add `retain: { hourly: 24, daily: 7, weekly: 4, monthly: 3 }` back into rule 6 of the live Kyverno policy. This way, if anything triggers a re-enable accidentally, the destructive `--keep-last 1` default doesn't run. Backups would resume with the same lock contention as before — that's a known bad state but not data-destroying. **Lower priority than this plan; can also do tomorrow.**
3. **Memory updates** — done in this session: `feedback_volsync_default_forget_trap`, `feedback_volsync_mover_image_override`, MEMORY.md index entries.
4. **Codex adversarial review** — kick off as the last action before sleeping; review the response in the morning.

---

## 6. Open questions still open (after v2)

Closed by v2:
- ~~#2: history migration strategy~~ → Phase 4 locked at "cold start with manual-snapshot gate per app"
- ~~#5: two policies vs. one~~ → Phase 3 locked at "two engine-gated policies + one deny-on-missing-engine validate policy"
- ~~#6: `backup-engine` discriminator~~ → Phase 3 locked at "mandatory label, never inferred by webhook"

Still open, for the user to decide before Phase 1:

1. **Dual-pvc-plumber operation during Phase 5.** Run two oracles (one restic, one kopia) routed by separate Kubernetes Services, OR build a single oracle that supports both backends behind one API? Lean: two services. The upstream pvc-plumber (Kopia) and our `mainertoo/pvc-plumber-restic` fork already exist; pointing two ClusterPolicies at two separate Services costs zero extra code.

2. **Bucket reuse vs. new bucket.** Option A (new bucket `volsync-kopia-shared`) cleaner; Option B (reuse `volsync-shared` with prefix) less Garage admin work. **Garage capacity check is the deciding factor.** Run in Phase 1 first task.

3. **Big-data app cache sizing on Kopia.** Restic needed `volsync.backup/cache-capacity: 10Gi` for dumb (100Gi cephfs). Kopia's caching is different. Phase 0 audit should answer: does Kopia even use a cache PVC, or is its content-addressable store fully S3-backed? If yes-cache: sizing TBD via Phase 1 smoke test.

4. **30-day Restic-bucket retention window.** Is 30 days the right post-Phase-6 horizon, or should it be 60-90? Tradeoff: Garage storage cost (we know the current footprint, ~XXX GiB) vs. emergency-restore depth. **My lean: 30 days; the Restic-side daily snapshots already roll off at the same horizon, so there's no marginal value in keeping the bucket longer than the snapshots' own retention.**

5. **CNPG apps.** WAL backup is independent (covered by `project_cnpg_migration.md`). But the `<app>-db-N` data PVC is still volsync-labeled. Will Kopia's snapshot of a live postgres PVC be crash-consistent? Restic+volsync was using `copyMethod: Snapshot` (CSI VolumeSnapshot → restore PVC → Kopia reads from the consistent snapshot). Phase 0 audit confirms: Kopia mover MUST use the same `copyMethod: Snapshot` flow.

6. **Rollback at Phase 6 boundary.** Once we delete the Restic Kyverno policy in Phase 6, per-app rollback is constrained to "restore from the read-only Restic bucket via the emergency runbook" (§9). Is there a "Phase 5.9" safety-pause we should formalize? Lean: yes — Phase 5 exit criteria already require "every app has ≥1 verified Kopia snapshot", so Phase 6 starts only when the system is fully cut over. The 30-day Restic-bucket window IS the rollback safety net.

7. **Restic policy rename hazard.** Phase 3 step 3c renames the policy to `volsync-pvc-backup-restore-restic` and adds a label selector. Renaming a `ClusterPolicy` triggers Kyverno to GC the old policy and emit the new one — at which point `generateExisting: true` would re-emit children for matching PVCs. With the engine-gate selector added simultaneously, only `backup-engine: restic`-labeled PVCs match, which (after step 3a backfill) is all of them. **Risk: if step 3a backfill is incomplete, some PVCs lose their Restic Secret/RS/RD when the rename happens.** Mitigation: step 3a's verification gate (label count parity) MUST pass before step 3c is merged.

---

## 7. Risk register (transition-specific, v2 with Codex findings folded in)

| Risk | Severity | Mitigation |
|---|---|---|
| **`mover-kopia/entry.sh` has its own analog of Restic's `--retry-lock=0s` trap** (Codex [10]) | **HIGH** | Phase 0 mandatory audit — Phase 1 BLOCKED until completed. If audit surfaces a trap and only image-fork can fix it, pause the project and reconsider. |
| Kopia + Garage S3 incompatibility | **HIGH** | Smoke test in Phase 1 BEFORE Phase 3 starts. If incompatible: fall back to S3-compatible MinIO or NFS-backed Kopia. |
| **Dual-policy collision** during Phase 5 — both Restic + Kopia rules attempt to generate same-named Secret/RS/RD (Codex [1]) | **HIGH** | Phase 3 step 3c gates the Restic policy with `backup-engine: restic` selector BEFORE the Kopia policy is deployed. Verified via `kubectl diff` before merge. |
| **Force-recreate UR herd** if Phase 3 Restic policy edit is done as delete+recreate (Codex [2]) | **HIGH** | Phase 3 step 3c is a label-selector tightening, NOT a `generate.data` mutation — should patch in-place. Pre-merge `kubectl diff` confirms no immutable-field error. If immutable-field error appears, abort the merge and reconsider. |
| **Cold-start 0–24h restore gap** for an app post-cutover (Codex [4]) | **HIGH** | Phase 5 Stage B includes a mandatory cutover-probe Kopia snapshot, verified before Restic authority is released. No app is "migrated" until probe succeeds. |
| **`synchronize:true` makes mid-flight engine switching destructive** (Codex [7]) | **HIGH** | Phase 5 uses transient `backup-engine: migrating` label, excluded from both engine policies. Engine swap is never atomic. |
| **Admission ordering fires wrong engine** if `backup-engine` label is missing or arrives late (Codex [3], [6]) | **HIGH** | Phase 3 step 3b deploys a deny-on-missing-engine validate policy FIRST (Audit mode initially, flippable to Enforce later). Label is required on the PVC manifest — never set by webhook. |
| Volsync mover restore path uses different PVC populator semantics than Restic | **MED** | Phase 7 DR drill catches this. Worst case: the 10-min target slips slightly. |
| Per-app cutover strands a snapshot mid-window | LOW | Same hazard as Restic Phase 5 had; same mitigation (Stage B/C protocol, Flux suspend during cutover). |
| Two oracles diverging in `/exists` semantics (Phase 5 coexistence) | MED | Phase 3 step 3e end-to-end smoke test before Phase 5 kicks off. Per-engine oracle URLs are distinct, no shared state. |
| User loses interest mid-Phase 5 → stuck in coexistence | MED | Phase 5 is sized for 6-8 sessions across 3-4 days. If life happens, the coexistence is stable indefinitely (each engine generates its own children for its own labeled set); can pick back up. |
| `pvc-plumber` upstream has stagnated and won't run on current K8s | MED | Phase 2 first task: validate upstream image against current cluster. If broken: contribute upstream fix, OR temporarily resurrect our `pvc-plumber-restic` fork retargeted at Kopia. |
| Garage capacity exhaustion during Phase 5 (Restic + Kopia both live) | MED | Garage capacity check in Phase 1 first task. Reserve ~50% headroom. Cold-start means Kopia grows linearly app-by-app, not all-at-once. |
| Renaming `backup-engine: kopia` label out at Phase 6 breaks GC | LOW | Final state has only `backup: hourly|daily`. The rename is done by removing the engine selector from the Kopia policy in the same PR that deletes the Restic policy (Phase 6). |
| Phase 3 step 3a backfill incomplete → some PVCs lose Secret/RS/RD when Restic policy rename fires `synchronize`-driven GC (§6 Open Q7) | MED | Step 3a verification gate: `kubectl get pvc -A -l backup` count must equal `kubectl get pvc -A -l backup-engine=restic` count. Step 3c blocked until parity. |

---

## 8. References

- **Original design:** [`docs/volsync-storage-recovery.md`](./volsync-storage-recovery.md) — read §2.1 (rejected Kopia), §2.4 (schedule), §4.1 (cutover protocol)
- **User-facing UX:** [`docs/label-driven-backups.md`](./label-driven-backups.md) — what stays the same for app authors
- **Driver script:** [`scripts/migrate-stage-bc.sh`](../scripts/migrate-stage-bc.sh) — extended in Phase 5 with `--engine kopia`
- **Upstream reference:** [`mitchross/talos-argocd-proxmox/docs/volsync-storage-recovery.md`](https://github.com/mitchross/talos-argocd-proxmox/blob/main/docs/volsync-storage-recovery.md) — the design we're now matching
- **Memory entries** (in `/Users/darcymainville/.claude/projects/-Users-darcymainville-kubernetes-lab/memory/`):
  - `feedback_kopia_vs_restic_shared_repo.md` — locking-model differences
  - `feedback_volsync_default_forget_trap.md` — why dropping `retain:` doesn't work on Restic
  - `feedback_volsync_mover_image_override.md` — operator env var hook (relevant if we ever revisit the Restic-fork path)
  - `project_volsync_label_driven_restore.md` — current state (paused 2026-05-17)

---

## 9. Emergency restore runbook (Codex finding [5]) — Restic-side, valid until Phase 6 + 30 days

If a PVC needs restore between cutover (Phase 5) and Restic-bucket retirement (Phase 6 + 30 days):

1. **Confirm the snapshot exists in the read-only Restic repo:**
   ```bash
   kubectl run restic-list --rm -i --restart=Never --image=restic/restic:0.18.0 \
     -n flux-system \
     --overrides='{"spec":{"securityContext":{"runAsNonRoot":true,"runAsUser":1000,"seccompProfile":{"type":"RuntimeDefault"}},"containers":[{"name":"restic","image":"restic/restic:0.18.0","stdin":true,"securityContext":{"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]},"runAsNonRoot":true,"runAsUser":1000,"seccompProfile":{"type":"RuntimeDefault"}},"command":["sh","-c","restic snapshots --tag $NS_PVC --json | jq ."],"env":[{"name":"NS_PVC","value":"<ns>/<pvc>"},{"name":"RESTIC_REPOSITORY","value":"s3:https://garage.lab.mainertoo.com/volsync-shared/restic"},{"name":"AWS_EC2_METADATA_DISABLED","value":"true"},{"name":"HOME","value":"/tmp"},{"name":"RESTIC_PASSWORD","valueFrom":{"secretKeyRef":{"name":"volsync-shared-base","key":"RESTIC_PASSWORD"}}},{"name":"AWS_ACCESS_KEY_ID","valueFrom":{"secretKeyRef":{"name":"volsync-shared-base","key":"AWS_ACCESS_KEY_ID"}}},{"name":"AWS_SECRET_ACCESS_KEY","valueFrom":{"secretKeyRef":{"name":"volsync-shared-base","key":"AWS_SECRET_ACCESS_KEY"}}}]}]}}'
   ```
2. **Annotate the target PVC to skip the Kopia-driven restore:** `kubectl annotate pvc <name> -n <ns> volsync.backup/skip-restore=true volsync.backup/skip-restore-reason="emergency restic restore from cold archive"`. This prevents the Kopia policy from regenerating a `dataSourceRef` mid-recovery.
3. **Create a scratch PVC of the same size/class** in the same namespace.
4. **Run a one-shot restore Job** (template lives at `apps/archive/04-volsync-shared-init-smoketest.yaml`, swap command to `restic restore --tag <ns>/<pvc> latest --target /restore`). Mount the scratch PVC + the data PVC.
5. **`rsync -aHAX /restore/ /data/`** inside the Job.
6. **Remove the skip-restore annotations.** Kopia policy regenerates the Secret/RS/RD trio. Next scheduled backup is now from the restored state.

**Garage retention:** Restic bucket `volsync-shared` is retained read-only for **30 days after Phase 6 completion**. After that, the bucket is deleted and this runbook is no longer applicable. After Phase 6 + 30 days, only Kopia-side recovery exists — same as the steady-state cluster.

---

## 10. Codex adversarial review v1 — addressed findings

10 findings raised against draft v1 (`2026-05-17 ~04:30 local`). All applied to v2.

| # | Severity | Area | Finding | Resolution in v2 |
|---|---|---|---|---|
| 1 | HIGH | Phase 3 / 5 | Dual policies will collide unless Restic policy is engine-gated first | Phase 3 step 3c gates Restic policy with `backup-engine: restic` selector BEFORE Kopia policy deployed |
| 2 | HIGH | Phase 3 | Force-recreating Restic policy would re-churn 70 PVC trios | Phase 3 step 3c is a label-selector tightening, not a `generate.data` mutation — should patch in-place |
| 3 | MED | Phase 3 | "Both labels" and "neither label" PVC behavior undefined | Phase 3 step 3b adds deny-on-missing-engine validate policy FIRST |
| 4 | HIGH | Phase 4 / 5 | Cold start gives zero Kopia recovery for ~24h after cutover | Phase 5 Stage B mandatory cutover-probe Kopia snapshot; no app marked migrated until probe succeeds |
| 5 | MED | Phase 4 | Emergency restore is Restic-manual but not documented | §9 runbook added (this section) |
| 6 | HIGH | Phase 3 | Admission can fire wrong engine if label arrives late | `backup-engine` is MANDATORY in PVC manifest; never inferred by webhook |
| 7 | MED | Phase 5 | `synchronize:true` makes mid-flight engine switch destructive | Transient `backup-engine: migrating` label introduced; engine swap never atomic |
| 8 | HIGH | §6 OQ | Open Question #5 (two policies vs. one) unanswered | Decision locked: two engine-gated policies + one deny-validate policy |
| 9 | HIGH | §6 OQ | Open Question #2 (cold start vs. history) unanswered | Decision locked: cold start, with per-app manual-snapshot gate |
| 10 | MED | Phase 0 | `mover-kopia/entry.sh` not audited; lock/retry traps unknown | Phase 0 audit added as MANDATORY blocker before Phase 1 starts |

What v2 did NOT change (acknowledged but accepted):
- The 1.5-week calendar estimate is unchanged; the per-app cutover time grew (~10 → ~15 min) but the herd-size discipline keeps total roughly fixed.
- Bridge-migration of Restic history remains rejected. Emergency restore from the read-only Restic bucket is the chosen tradeoff.
- Big-data app cache sizing on Kopia (§6 OQ #3) stays open — gets answered in Phase 1 smoke test.

---

## 11. Decision log

| Date | Decision | By |
|---|---|---|
| 2026-05-06 | Original design committed to Restic (`docs/volsync-storage-recovery.md` §2.1) | User |
| 2026-05-17 | Restic path abandoned; transition to Kopia committed | User |
| 2026-05-17 | This transition plan drafted v1 | Claude |
| 2026-05-17 | Codex adversarial review v1 | Codex |
| 2026-05-17 | v2 applied — Phase 0 audit gate, Phase 3 policy split, Phase 4 cold-start-with-gate, Phase 5 transient-migrating-state, §9 emergency runbook | Claude |
| TBD | Phase 0 sign-off (user reads v2, audits `mover-kopia/entry.sh`, confirms commit) | User |
| TBD | Phase 1 start | User |
