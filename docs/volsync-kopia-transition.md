# VolSync Kopia Transition — Plan

**Status:** DRAFT v8 — Codex's sixth adversarial pass (against v7) confirmed two v7 sections were CLEAN, surfaced 1 HIGH gate-timing bug (the §3a "BEFORE Phase 3 step 3e" gate should be "DURING"), and 3 MED polish items (kind prereq check, kustomize-build verification needs `yq` not anchored grep, stale references survive from earlier revs). v8 closes them all. This is the final pre-execution rev.
**Goal:** Replace the Restic-based data path of the label-driven backup system with Kopia, matching the **mitchross/talos-argocd-proxmox** reference design as it is *actually implemented* (not as v1–v4 imagined it).
**Trigger:** 2026-05-17 cluster incident — see [`docs/volsync-storage-recovery.md`](./volsync-storage-recovery.md) §10 and the project memory `project_volsync_label_driven_restore.md`.

> **What changed v7 → v8 (Codex v6 review):** Two findings confirmed CLEAN (kubeconform script path exists; Restic/Kopia webhook timeout split is fine — separate Services). Four actionable: (a) **HIGH #5** — §3a gate-timing said "BEFORE Phase 3 step 3e scratch validation", but step 3e IS the scratch test where these functions should be exercised. v8 flips to "DURING Phase 3 step 3e: include deliberate wrong-password + existing-repo test cases". (b) Phase 1a kind validation needed prerequisite check that `kind`/`k3d` AND container runtime are available. (c) Phase 1a `kustomize build | grep -E "^(kind|name|sourceRef)" -A1` check doesn't work — kustomize indents `metadata.name` and `sourceRef` under YAML hierarchy; anchored `^` misses them. Replaced with `yq` filter. (d) Stale references cleaned: Phase 0 step 3 activities flipped from 🔜 to ✅; "CNPG namespaces" wording (3 places) → "CNPG data PVCs by label" matching v6's actual policy approach; obsolete `migrating` risk register entry removed.

> **What changed v6 → v7 (Codex v5 review):** Six findings, two of them HIGH. (a) The Phase 1a CRD diff gate was a **false-pass** — `kubectl apply --dry-run=server` validates against CRDs INSTALLED in the live cluster (backube v0.15), NOT against the v0.18 CRDs we pulled to `/tmp`. v7 replaces the gate with a scratch-cluster validation (kind/k3d) or offline `kubeconform`. (b) The §3a `connect_repository` / `create_repository` audit items were scheduled for "before Phase 5 batch 1" — meaning we'd discover password-mismatch or retry-loop bugs against the high-value apps (authentik-media, vaultwarden, home-assistant). v7 moves them BEFORE Phase 3 step 3e scratch validation so discovery is on a throwaway PVC. Plus: (c) HTTP_TIMEOUT bumped 7s → 15s (we hit Garage through Traefik, not in-cluster RustFS); Kyverno webhook timeout 10s → 20s to coordinate. (d) `KOPIA_S3_DISABLE_TLS=false` wording tightened (correct value, but reasoning clarified — Garage is external HTTPS, no in-cluster Service). (e) Pre-merge `rg` check added that no cluster-wide cosign policy exists (which would deny the unsigned perfectra1n HelmRepository). (f) Phase 1a swap explicitly requires atomic single-PR commit and HelmRepository name `perfectra1n-volsync` (not `volsync`) to avoid transient SourceNotReady.

> **What changed v5 → v6:** Codex's fourth pass found 6 v5 bugs introduced by the reframe. Most important: (a) v5 Phase 1a's HelmRelease snippet used `HelmRepository.spec.url`, but our actual `volsync-release.yaml` uses `OCIRepository` + `chartRef` (verified 2026-05-18). v6 rewrites Phase 1a to switch from OCIRepository → HelmRepository, since perfectra1n publishes to `https://perfectra1n.github.io/volsync/charts` (gh-pages), not OCI. (b) v5 Phase 2 said "adapt ExternalSecret to SOPS" but didn't enumerate the keys. v6 lists the exact 3-key Secret schema `pvc-plumber-kopia` expects, with the deployment's env vars + file-mount layout. (c) v5 Phase 1a chart swap (`backube/volsync@0.15.0` → `perfectra1n/volsync@0.18.5`) could invalidate the 41 live Kyverno-managed RSes/RDs from yesterday's regen if CRD shape differs. v6 adds an explicit `kubectl diff` gate before the chart-swap commit lands. (d) v5 Phase 6 step 4 wording was ambiguous after the no-rename reframe. v6 makes the deletion target explicit. (e) v5 didn't pin `mitchross/pvc-plumber:3.1.0` source repo + commit. v6 pins. (f) Phase 0 audit step 3 only covered BLOCKER criteria; v6 adds a §3a deeper-audit checklist for `do_restore`, repo connect/create, retry/timeout, and maintenance ownership paths.

> **Phase 0 step 3 audit verdict (2026-05-18 AM):** **NO BLOCKERS.** Perfectra1n's mover-kopia/entry.sh (2473 LOC) properly checks exit codes (`do_backup`, `do_maintenance` both fail-fast), has NO destructive default retention (controller `mover.go:898` returns empty env vars when `retainPolicy == nil` — the OPPOSITE of restic's `--keep-last 1` trap), uses standard S3 env vars compatible with our Kyverno secret schema, and Kopia's concurrent-multi-writer model means no exclusive-lock contention. The split between per-backup work (light: `do_backup; do_retention`) and maintenance (heavy: `do_maintenance` invoked by KopiaMaintenance CRD CronJob) is exactly the architecture we wanted. Detailed findings in §10.

> **What changed v4 → v5 (the big one):** The Phase 0 audit revealed `backube/volsync` does NOT ship a Kopia mover — not in v0.15.0, not on `main`, and only as an unmerged 56k-line PR (#1723 "Implement Kopia") that has been open since 2025-08-06. **The upstream Kopia mover does not exist.** Mitchross's actual setup uses the **`perfectra1n/volsync` community fork** (`ghcr.io/perfectra1n/volsync:v0.17.11` + chart `perfectra1n/volsync 0.18.5`). Mitchross's own internal research doc (`docs/research/volsync-fork-vs-upstream-2026-05-08.md`) concludes "stay on the fork, delete the JobMutator". v1–v4 of THIS doc described a transition to a non-existent thing. v5 redirects to what actually works.

> **Smaller v4 → v5 changes:** (a) Phase 1 now includes swapping the volsync HelmRelease to perfectra1n's chart, alongside Kopia repo creation; (b) Phase 2 pvc-plumber is now mitchross's `ghcr.io/mitchross/pvc-plumber:3.1.0` v3 operator (not "upstream pvc-plumber" which doesn't exist as a Kopia-aware service), and we **delete** our `mainertoo/pvc-plumber-restic` fork in Phase 6; (c) new §7 risk entry for fork CVE-lag and the upstream-watch discipline that mitigates it; (d) new "fork governance" subsection in §6 covering what happens if the fork stagnates further or if PR #1723 finally merges.

> **What stays from v4:** all six v3 findings (CNPG label-not-namespace, concurrency cap 3/1, Stage B/5b/C ordering, same-PVC preflight, Phase 0 BLOCKER specifics, §9.1 scratch test). All v1+v2+v3 findings remain applied. The Kyverno policy split, the parallel-probe-RS Phase 5 handoff, the cold-start-with-gate strategy — unchanged.

---

## 1. Why we're transitioning (the short version)

The original design ([`volsync-storage-recovery.md`](./volsync-storage-recovery.md)) §2.1 considered Kopia and **explicitly deferred it as a separate project**:

> *"Switch from restic to Kopia — Kopia's CDC dedup is strictly better than restic's, but compounds two big migrations (mover + admission). Deferred to a separate project."*

This is that separate project, executed earlier than originally hoped because **Restic's locking model is fundamentally incompatible with the shared-repo + multi-writer pattern the design depends on**.

**Correction from v4 (read the Phase 0 audit findings before going further):** v1–v4 assumed the upstream `backube/volsync` operator supports Kopia. It does not. There is no `mover-kopia/entry.sh` in any released volsync version. Adopting Kopia means adopting **`perfectra1n/volsync` — a community fork maintained by the same person whose PR #1723 has been pending upstream merge since 2025-08-06**. This is what `mitchross/talos-argocd-proxmox` actually uses. Their own published research doc (`docs/research/volsync-fork-vs-upstream-2026-05-08.md`) is the canonical justification:

> *"The fork is the only viable Kopia option — upstream has no Kopia mover and won't have one on any timeline you can plan against. Whatever you do, you stay on the fork. So 'fork vs upstream' is a non-question."* — mitchross internal research, 2026-05-08

The same doc traces the `CreateOrUpdateDeleteOnImmutableErr` controller behavior (which broke mitchross's cluster once) to upstream PR #302 from 2022 — i.e. **switching to upstream wouldn't help even if upstream had Kopia, because the controller logic is identical between fork and upstream**.

We adopt the fork because that's where the Kopia mover lives. We accept the maintenance burden (CVE-watch discipline, see §7) because it's the same burden every Kopia-on-volsync operator has.



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
| Operator image | `quay.io/backube/volsync:0.15.0` (upstream) | `ghcr.io/perfectra1n/volsync:v0.17.11` (community fork) |
| Operator Helm chart | `backube/volsync@0.15.0` | `perfectra1n/volsync@0.18.5` (chart/image version mismatch is intentional per mitchross's notes) |
| Mover engines | restic, rclone, rsync, etc — all `backube/volsync:0.15.0` | **Same set + `kopia`** — all from `ghcr.io/perfectra1n/volsync:v0.17.11` |
| pvc-plumber | Our fork `mainertoo/pvc-plumber-restic` (~600 LOC restic CLI shell-out, lives in volsync-system) | **`ghcr.io/mitchross/pvc-plumber:3.1.0`** — mitchross's v3 operator with `KopiaMaintenance` CRD. We DELETE our restic fork in Phase 6. |
| Per-PVC Secret | `volsync-<pvc>` with `RESTIC_*` env vars | `volsync-<pvc>` with `KOPIA_PASSWORD`, `KOPIA_S3_ENDPOINT`, `KOPIA_S3_BUCKET`, `KOPIA_S3_DISABLE_TLS` (if needed), `AWS_*` creds (Kopia uses standard AWS env vars for S3 auth) |
| Shared repo | `s3://garage.lab.mainertoo.com/volsync-shared/restic` | `s3://garage.lab.mainertoo.com/volsync-kopia` (new dedicated bucket) |
| Maintenance | `volsync-restic-forget` CronJob (deployed PR #429) | **`kopia-maintenance` CronJob** — runs `kopia maintenance run`, manages identity ownership via stable synthetic hostname `maintenance@cluster` (pattern from mitchross's `kopia-maintenance-cronjob.yaml`) |
| Snapshot identity | `<ns>/<pvc>` restic tag + `RESTIC_HOSTNAME` env | Kopia `--source-path-override` + `--host` + `--username`, set per-RS by the perfectra1n mover from `spec.kopia.{hostname,username,sourcePathOverride}` |
| Snapshot format | Restic packfile format | Kopia content-addressable format — **NOT cross-readable** |
| JobMutator pattern | N/A (restic mover uses env-var creds) | **EXPLICITLY AVOIDED.** Mitchross's research doc warns against admission-time mutation of mover Jobs (cf. their cluster-wide outage, attributed to upstream PR #302's drift-correction). Our Kyverno policy generates the per-PVC Secret + RS + RD as before — no Job-spec mutation, no admission injection. |

### Stays in place during transition (then retired)

- The Restic shared repo (`volsync-shared/restic`) — kept read-only until Phase 6
- The `pvc-plumber-restic` deployment — kept running until Phase 5 cutover completes; queries serve the apps still on Restic
- The `volsync-restic-forget` CronJob — suspended at start of Phase 6, deleted at end

---

## 3. Phase plan

Each phase leaves the cluster in a working state. No phase combines unrelated changes. Time estimates assume single-operator focused sessions.

### Phase 0 — Plan + audit + sign-off (2026-05-17 evening through 2026-05-18 AM, ~5 hr total)

**Output:** this document at v5, with the perfectra1n fork's Kopia mover audited and the BLOCKER/WORKAROUND criteria evaluated against ACTUAL code (not an imagined upstream).

**Activities:**
1. ✅ Codex adversarial reviews v1/v2/v3 — done (see §10)
2. ✅ v1 → v2 → v3 → v4 applied iteratively — done
3. ✅ **Phase 0 audit step 1 (upstream Kopia check)** — done 2026-05-18 AM. Result: **upstream volsync has NO Kopia mover.** Triggered v5 reframe.
4. ✅ **Phase 0 audit step 2 (perfectra1n fork existence + activity)** — done. Result: fork is active (last commit 2026-03-22), used by mitchross in production, image `ghcr.io/perfectra1n/volsync:v0.17.11` available.
5. ✅ **Phase 0 audit step 3: read perfectra1n's `mover-kopia/entry.sh`** at `v0.17.11` — done 2026-05-18 AM. NO BLOCKERS. See "Phase 0 mandatory audit" below + full findings in §10.
6. **🔜 User sign-off** on the v8 plan
7. **🔜 Out of band:** stabilize the live cluster by re-adding the original `retain:` to live rule 6 via a one-line PR (prevents accidental `--keep-last 1` destruction if volsync is ever re-enabled before Phase 5 starts). Lower priority — volsync controller is at 0 replicas; the mitigation is a safety net not a fix.

**Exit criteria:** Phase 0 audit step 3 complete. No BLOCKER hit, OR if a BLOCKER hits, decision logged on whether to (a) pause project, (b) carry workaround into Phase 3, or (c) escalate to a deeper fork analysis.

**Phase 0 mandatory audit step 3 — perfectra1n fork's Kopia mover:**
```bash
git clone https://github.com/perfectra1n/volsync /tmp/volsync-fork
git -C /tmp/volsync-fork checkout v0.17.11
ls /tmp/volsync-fork/mover-kopia/
cat /tmp/volsync-fork/mover-kopia/entry.sh
grep -rn "kopia\|maintenance\|timeout\|retry\|server\|connect\|snapshot\|policy" \
  /tmp/volsync-fork/mover-kopia/ \
  /tmp/volsync-fork/internal/controller/mover/kopia/ | head -80
```

**BLOCKER (stop, do not proceed to Phase 1):**
- Script ignores non-zero exit codes from `kopia snapshot create`, `restore`, or `maintenance` — data-path failures would be silent.
- Maintenance/retention takes exclusive locks without a bounded retry or `--no-lock`-equivalent — the lock-leak pattern from Restic recurs.
- **Destructive retention by default.** Specifically: entry.sh or the controller invokes `kopia snapshot expire --delete`, `kopia maintenance run --full`, OR `kopia policy set` after each snapshot with synthesized retention when `spec.kopia.retain` is nil — especially `latestSnapshots/keepLatest=1` or any broad/global policy not scoped to the current `<namespace>/<pvc>` source. If retention is applied only from explicit `spec.kopia.retain` (i.e., nil → no expiration runs), this is NOT a blocker. (Codex v3 finding [4] — the v3 phrasing "analogous to `--keep-last 1`" was too vague to test against; Kopia's destructive equivalent is specifically the wrong policy scope, not a single flag.)
- S3 credential injection format is incompatible with the generated Secret schema (different env-var names, different config-file expectations).
- `copyMethod: Snapshot` is not exercised end-to-end — restore path may diverge from VolSync RD populator semantics.

**WORKAROUND (note, handle in Phase 3, do NOT block Phase 1):**
- Hardcoded cache path or size — addressable via `cacheCapacity` override in the generated RS.
- Direct repo access instead of server mode — acceptable if concurrent smoke tests pass in Phase 1.
- Noisy but bounded timeouts — acceptable if covered by Prometheus alerts.
- Missing `--no-lock` on read-only calls — document it; add to Phase 3 tracking issue.

**If a BLOCKER appears:** STOP. The mover-image-override hook (`RELATED_IMAGE_KOPIA_CONTAINER` per `feedback_volsync_mover_image_override`) is available as a fallback. But if Kopia also needs forking, the strategic argument for transitioning gets weaker — pause the project and reconsider Restic-with-mover-fork as the lesser evil.

---

### Phase 1 — Kopia repo + perfectra1n operator + creds (1 day, ~5 hr)

**Output:** (a) Operational Kopia repo on Garage; (b) perfectra1n volsync operator + chart deployed cluster-wide; (c) SOPS Secret `volsync-kopia-shared-base` in `flux-system`. Existing Restic-side volsync infra continues to work in parallel (perfectra1n's image is a superset — restic mover image still works alongside the new kopia mover).

**Activities:**

0. **Phase 1a — swap volsync from backube OCIRepository to perfectra1n HelmRepository.** Its own PR before any Kopia infra is touched. **Codex v4 finding [1] caught this:** v5 said "edit HelmRepository.spec.url" but our actual `infrastructure/controllers/volsync/app/volsync-release.yaml` uses `chartRef → OCIRepository` (verified 2026-05-18), not `HelmRepository.spec.chart.spec.version`. Plus perfectra1n publishes via gh-pages chart repo, NOT OCI. So the migration is OCIRepository → HelmRepository.
   
   **Edits:**
   - **DELETE** `infrastructure/controllers/volsync/app/volsync-repository.yaml` (the existing `OCIRepository` pointing at `oci://ghcr.io/home-operations/charts-mirror/volsync` tag `0.15.0` — perfectra1n is not mirrored there).
   - **CREATE** `infrastructure/controllers/volsync/app/volsync-helmrepo.yaml`:
     ```yaml
     apiVersion: source.toolkit.fluxcd.io/v1
     kind: HelmRepository
     metadata:
       name: perfectra1n-volsync
       namespace: volsync-system
     spec:
       interval: 1h
       url: https://perfectra1n.github.io/volsync/charts
     ```
     (No `verify.provider: cosign` here — perfectra1n's chart is not cosign-signed. We lose the verification step we had with the home-operations OCI mirror. Risk noted in §7.)
   - **EDIT** `infrastructure/controllers/volsync/app/volsync-release.yaml`:
     - Replace the `spec.chartRef` block with `spec.chart.spec` pattern:
       ```yaml
       spec:
         chart:
           spec:
             chart: volsync
             version: 0.18.5
             sourceRef:
               kind: HelmRepository
               name: perfectra1n-volsync
               namespace: volsync-system
             interval: 24h
       ```
     - In `spec.values`, add the image overrides (pattern from mitchross's `values.yaml`):
       ```yaml
       image: &volsyncImage
         repository: ghcr.io/perfectra1n/volsync
         tag: v0.17.11
       kopia: *volsyncImage
       rclone: *volsyncImage
       restic: *volsyncImage
       rsync: *volsyncImage
       rsync-tls: *volsyncImage
       syncthing: *volsyncImage
       ```
     - Keep existing `manageCRDs: true`, `metrics.disableAuth: true`, `replicaCount: 1`, `targetNamespace: volsync-system` from current values.
   - **EDIT** `infrastructure/controllers/volsync/app/kustomization.yaml`: change `volsync-repository.yaml` reference to `volsync-helmrepo.yaml`.
   
   **CRD compatibility check (Codex v4 finding [3] + v5 finding [2] correction, required pre-merge):**
   
   ⚠ **v6's `kubectl apply --dry-run=server` was a FALSE-PASS gate** — server-side dry-run validates against the CRD ALREADY INSTALLED in the live cluster (backube v0.15), not against the chart we pulled to `/tmp`. The live cluster's CRD doesn't change just because we downloaded a chart. To actually catch schema breakage, we need to install perfectra1n's CRD into a DIFFERENT API server (scratch kind/k3d), or do offline validation against the chart's CRD definitions.
   
   **Correct gate — scratch cluster validation:**
   ```bash
   # 0. Prerequisite checks (Codex v6 finding [1])
   command -v kind >/dev/null || command -v k3d >/dev/null || \
     { echo "ERROR: neither kind nor k3d on PATH — install one OR use the kubeconform alternative below"; exit 1; }
   docker info >/dev/null 2>&1 || podman info >/dev/null 2>&1 || \
     { echo "ERROR: no container runtime running — Docker Desktop / Colima / Podman must be up before kind can create a cluster"; exit 1; }
   command -v helm >/dev/null || { echo "ERROR: helm not on PATH"; exit 1; }
   command -v yq >/dev/null || { echo "ERROR: yq not on PATH"; exit 1; }
   
   # 1. Pull both charts' CRDs
   helm pull volsync --version 0.18.5 --repo https://perfectra1n.github.io/volsync/charts --untar -d /tmp/perfectra1n-chart
   
   # 2. Spin up a disposable kind cluster (or k3d)
   kind create cluster --name volsync-crd-validate
   
   # 3. Install perfectra1n's CRDs into the scratch cluster
   kubectl --context kind-volsync-crd-validate apply -f /tmp/perfectra1n-chart/volsync/crds/
   
   # 4. Export the 41 live RSes/RDs (clean of cluster-managed fields)
   kubectl --context default get replicationsource.volsync.backube,replicationdestination.volsync.backube -A \
     -l "app.kubernetes.io/managed-by=kyverno" -o yaml \
     | yq 'del(.items[].metadata.resourceVersion, .items[].metadata.uid, .items[].metadata.creationTimestamp, .items[].metadata.generation, .items[].metadata.managedFields, .items[].status)' \
     > /tmp/live-rses-rds.yaml
   
   # 5. Dry-run apply them against the scratch cluster (now has perfectra1n CRDs)
   kubectl --context kind-volsync-crd-validate apply --dry-run=server -f /tmp/live-rses-rds.yaml 2>&1 \
     | tee /tmp/crd-validate.log
   
   # 6. Teardown
   kind delete cluster --name volsync-crd-validate
   ```
   **Pass criterion:** zero `unknown field` errors, zero `validation failed` lines in `/tmp/crd-validate.log`. If ANY error: abort Phase 1a, identify the offending field(s), evaluate hand-patching the live RSes (or filing an issue against perfectra1n for backward-compatibility).
   
   **Alternative (no scratch cluster needed):** use [`kubeconform`](https://github.com/yannh/kubeconform) with schemas generated from the perfectra1n CRDs:
   ```bash
   # Generate kubeconform schemas from perfectra1n CRDs
   git clone --depth 1 https://github.com/yannh/kubeconform /tmp/kubeconform
   for crd in /tmp/perfectra1n-chart/volsync/crds/*.yaml; do
     /tmp/kubeconform/scripts/openapi2jsonschema.py "$crd"
   done
   # Then run kubeconform with the generated schemas against the live RS export
   kubeconform -schema-location ./*.json /tmp/live-rses-rds.yaml
   ```
   Use whichever is faster for our environment.

   **Pre-merge cluster-wide cosign-policy check (Codex v5 finding [1]):**
   ```bash
   rg -n "verifyImages|attestors|ImageValidatingPolicy|ValidatingAdmissionPolicy|provider: cosign|ImageUpdateAutomation" infrastructure/
   ```
   Expected output: matches ONLY on the existing per-source `verify.provider: cosign` blocks (e.g., `volsync-repository.yaml`, `snapshot-controller-repository.yaml`, `app-template.yaml`), NOT on a cluster-wide Kyverno/ValidatingAdmissionPolicy that would deny unsigned HelmRepositories. If the latter exists, adding `perfectra1n-volsync` HelmRepository would be denied at admission — we'd need to either exempt the new repo or sign perfectra1n's chart ourselves first. Current state (verified 2026-05-18): per-source only, no cluster-wide policy → safe to add unsigned HelmRepository.
   
   **Smoke test post-merge:** after Flux reconciles, `kubectl -n volsync-system get deploy volsync-system-volsync -o jsonpath='{.spec.template.spec.containers[0].image}'` returns `ghcr.io/perfectra1n/volsync:v0.17.11`. Existing Restic ReplicationSources continue to function — perfectra1n's `mover-restic` is the same upstream code.
   
   **Atomic-PR requirement (Codex v5 finding [6]):** all four file changes (DELETE `volsync-repository.yaml`, CREATE `volsync-helmrepo.yaml`, EDIT `volsync-release.yaml`, EDIT `kustomization.yaml`) must be in **one PR / one commit**. Splitting them across PRs creates a window where the HelmRelease still references the old `OCIRepository` name while the new `HelmRepository` is added (or vice versa), leading to either Flux `SourceNotReady` errors OR continued reconciliation on the old chart longer than expected.
   
   **Pre-merge verification (Codex v6 finding [4] — `yq` filter, NOT anchored grep):**
   ```bash
   kustomize build infrastructure/controllers/volsync | yq '
     select(.kind == "HelmRepository" or .kind == "OCIRepository" or .kind == "HelmRelease")
     | { kind, name: .metadata.name, chartRef: .spec.chartRef, sourceRef: .spec.chart.spec.sourceRef }
   '
   ```
   Expected output (exact shape):
   - One `kind: HelmRepository` with `name: perfectra1n-volsync` (no `chartRef`, no `sourceRef` — repositories don't have those).
   - One `kind: HelmRelease` with `sourceRef.{kind: HelmRepository, name: perfectra1n-volsync}` and `chartRef: null` (we removed the OCI chartRef).
   - Zero `kind: OCIRepository`.
   
   Why the v7 `grep -E "^(kind|name|sourceRef)" -A1` recipe was wrong: kustomize emits standard YAML where `metadata.name` and `spec.chart.spec.sourceRef` are indented under their parent keys. An anchored-at-column-zero regex misses them entirely and silently passes — same false-pass class of bug as v6's CRD dry-run.
   
   Note the deliberately distinct HelmRepository name `perfectra1n-volsync` (not `volsync` as the OCIRepository was) — different GVK + different name keeps the diff unambiguous in Flux's event log.
   
   **Rollback:** revert the single PR (all four file changes). Flux re-installs backube/volsync 0.15.0 via the OCI mirror. Existing Kyverno-managed RSes continue to operate.

1. **Phase 1b — Kopia repo on Garage.** Decide bucket scope:
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

### Phase 2 — Deploy mitchross's pvc-plumber v3 (Kopia-aware) (1 day, ~5 hr)

**Output:** `pvc-plumber` deployment in `volsync-system` running `ghcr.io/mitchross/pvc-plumber:3.1.0` (mitchross's v3 operator with `KopiaMaintenance` CRD). Our `mainertoo/pvc-plumber-restic` fork stays running in parallel during Phase 5 — Restic-backed apps still consult it. Both deployments serve their own Kyverno policy.

**Source pin (Codex v4 finding [5]):**
- Image: `ghcr.io/mitchross/pvc-plumber:3.1.0`
- Source repo: `https://github.com/mitchross/pvc-plumber` (tag `v3.1.0`)
- `KopiaMaintenance` CRD: shipped from the same repo (CRD manifest is bundled in mitchross's `infrastructure/controllers/pvc-plumber/` reference layout — not a separate Helm chart).
- Pin by digest at Phase 2 implementation time: `crane digest ghcr.io/mitchross/pvc-plumber:3.1.0` → copy the sha256 → use `image: ghcr.io/mitchross/pvc-plumber@sha256:...` in our deployment so Renovate updates are explicit not silent.

**Required Secret schema (Codex v4 finding [2] — enumerated from mitchross's deployment+externalsecret):**

The pvc-plumber:3.1.0 deployment expects a Secret named `pvc-plumber-kopia` in namespace `volsync-system`, mounted as a directory at `/var/secret/pvc-plumber-kopia` (mode 0440, NOT consumed via `secretKeyRef` env vars — see v3.1.0's lazy-credentials pattern below). Three keys:

| Secret key | Value source for us | Mitchross uses |
|---|---|---|
| `KOPIA_PASSWORD` | New password generated for the Kopia repo, stored in 1Password personal vault (item: "volsync-kopia repository master password") | 1Password property `rustfs/kopia_password` |
| `AWS_ACCESS_KEY_ID` | Garage IAM key scoped to `volsync-kopia` bucket | 1Password `rustfs/k8s-admin-access-key` |
| `AWS_SECRET_ACCESS_KEY` | Same Garage IAM secret | 1Password `rustfs/k8s-admin-secret-key` |

**v3.1.0 lazy-credentials pattern (important — explains the mount-as-directory choice):** The operator reads creds files on each `kopia` subprocess invocation and retries with backoff if any file is missing/empty. This was added in v3.1.0 specifically to avoid the pod-startup `secretKeyRef` race that crashed v3.0.0 when ExternalSecrets hadn't finished rendering yet. For us (SOPS), the race doesn't apply (Flux renders the Secret before reconciling the Deployment), but the file-mount pattern is what the operator expects — don't try to convert to env vars.

**Deployment env vars (plain values, NOT from secret):**

| Env var | Mitchross value | Our value |
|---|---|---|
| `BACKEND_TYPE` | `kopia-s3` | `kopia-s3` |
| `KOPIA_S3_ENDPOINT` | `192.168.10.133:30293` (RustFS) | `garage.lab.mainertoo.com` (bare host, no scheme — see comment in mitchross's deployment.yaml: "kopia's S3 backend rejects fully-qualified URLs") |
| `KOPIA_S3_BUCKET` | `volsync-kopia` | `volsync-kopia` |
| `KOPIA_S3_DISABLE_TLS` | `true` (RustFS in-cluster, plain HTTP) | **`false`** — Garage is at `garage.lab.mainertoo.com` (external HTTPS, no in-cluster Service). Verified 2026-05-18 against `docs/backup-architecture.md` + existing smoke tests (`apps/archive/03b-awscli-garage-endpoint-test.yaml`, `apps/archive/04-volsync-shared-init-smoketest.yaml`) which all use HTTPS to `garage.lab.mainertoo.com`. Phase 1b in-cluster smoke test must confirm before chart-swap merge. Only set `true` if a direct in-cluster HTTP endpoint is intentionally introduced (not the case today). |
| `LOG_LEVEL` | `info` | `info` |
| `CACHE_TTL` | `5m` | `5m` |
| `HTTP_TIMEOUT` | `7s` (low-latency in-cluster RustFS) | **`15s`** — our Garage path goes through HTTPS + Traefik (extra TLS handshake + ingress hop). Codex v5 finding [5] flagged 7s as too tight for this path. Bumped to 15s initially; Phase 1b records p50/p95 latency over 50 calls from an in-cluster pod; if p95 < 2s, can lower toward 7–10s before Phase 5; otherwise hold at 15s. |
| `RE_WARM_INTERVAL` | `90s` | `90s` |

**Kyverno webhook timeout (v7 addition, Codex v5 finding [5]):** The existing `volsync-pvc-backup-restore.yaml` has `webhookTimeoutSeconds: 10` (matching our restic pvc-plumber's old `HTTP_TIMEOUT=7s` + buffer). With the new kopia pvc-plumber's `HTTP_TIMEOUT=15s`, the Kyverno webhook calling it needs more headroom. Update the new Kopia ClusterPolicy `volsync-pvc-backup-restore-kopia` to set `webhookTimeoutSeconds: 20` to keep coordinated with the HTTP timeout. The restic policy can stay at 10s (its oracle hasn't changed).

**Activities:**
1. Read mitchross's `infrastructure/controllers/pvc-plumber/` reference layout (already cloned at `/tmp/mitchross` during Phase 0). Files: `certificate.yaml` (cert-manager for webhook TLS), `deployment.yaml`, `externalsecret.yaml` (for Kopia creds), `kustomization.yaml`, `rbac.yaml`, `webhooks.yaml`.
2. Replace mitchross's `externalsecret.yaml` with a SOPS-encrypted `Secret` at `infrastructure/secrets-prod/pvc-plumber-kopia.sops.yaml` carrying the 3 keys from the schema above. SOPS auto-encrypts on save per CLAUDE.md.
3. Adapt webhook TLS: mitchross uses `cert-manager` Certificate for the webhook cert. We already have cert-manager. Same pattern works as-is.
4. New manifests under `infrastructure/controllers/pvc-plumber/` (DIFFERENT directory from existing `pvc-plumber-restic/`). Add this directory to `infrastructure/controllers/kustomization.yaml`.
5. **`KopiaMaintenance` CRD:** mitchross's v3 operator installs and reconciles a `KopiaMaintenance` resource that drives the `kopia-maintenance` CronJob. CRD definition ships from the operator repo. We use the same CRD — no separate volsync-restic-forget-style CronJob needed.
6. Smoke test:
   - `kubectl -n volsync-system get deploy pvc-plumber` Ready=1/1
   - Webhook reachable: `kubectl get validatingwebhookconfiguration` shows the pvc-plumber webhook with `serviceName: pvc-plumber` and `service.namespace: volsync-system`
   - Oracle responds: `kubectl -n volsync-system port-forward svc/pvc-plumber 18080:8080 &` then `curl -s http://localhost:18080/exists/foo/nonexistent | jq` returns `decision:fresh, authoritative:true, backend:kopia-s3`
   - `KopiaMaintenance` CRD reconciles a CronJob without error
   - Creds-file mount visible inside the pod: `kubectl -n volsync-system exec -it deploy/pvc-plumber -- ls -la /var/secret/pvc-plumber-kopia` shows 3 files (KOPIA_PASSWORD, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY) at mode 0440

**During Phase 5:** both pvc-plumbers run concurrently. Each engine-specific Kyverno policy points at its own pvc-plumber Service. No cross-talk.

**Exit criteria:** mitchross pvc-plumber Ready, oracle responding, `KopiaMaintenance` CRD healthy. Our `pvc-plumber-restic` fork untouched and still serving Restic apps.

**Rollback:** comment out the new component in `infrastructure/controllers/kustomization.yaml`; everything reverts. No data side-effects — pvc-plumber is read-mostly.

---

### Phase 3 — Kyverno policy split with engine-gating (1 day, ~5 hr)

**Decision (closes §6 Open Question #5, per Codex v1 finding [8]):** **Two separate ClusterPolicies, both explicitly engine-gated**, with a deny-on-missing-engine validate policy installed FIRST. Single-policy approach was rejected because edits to one would trigger a full UpdateRequest burst across all 70 PVCs (cf. 2026-05-17 incident).

**Output:** Three ClusterPolicies live in Audit mode by end of Phase 3:
1. `volsync-pvc-engine-required` — validate-only policy, enforces `backup-engine ∈ {restic, kopia}` on every backup-labeled PVC (Audit until Phase 6, then can flip to Enforce)
2. `volsync-pvc-backup-restore` — the EXISTING policy, NOT RENAMED, with `backup-engine: restic` selector added in-place
3. `volsync-pvc-backup-restore-kopia` — NEW policy, matches `backup-engine: kopia`, generates Kopia-flavored Secret/RS/RD

> **v2→v3 change (Codex v2 finding [2]):** v2 said to rename the existing policy to `…-restic`. A rename is a delete+create under the hood, which fires `synchronize:true` GC for every generated child during the gap between old-name deletion and new-name reconcile. **v3 keeps the original policy name** through the entire transition. The rename is deferred to Phase 6 where the Restic policy is being deleted anyway — no GC hazard because by then no PVCs depend on it.

**Activities (strict order):**

1. **Step 3a — backfill `backup-engine: restic` on every currently labeled PVC.** Single PR before any policy change. All 42 currently Kyverno-managed PVCs get the label by hand-editing each app's PVC manifest. **Verification gate:** `kubectl get pvc -A -l backup --no-headers | wc -l` must equal `kubectl get pvc -A -l backup-engine=restic --no-headers | wc -l` after Flux reconcile. Step 3c is BLOCKED until parity is verified.

2. **Step 3b — deploy `volsync-pvc-engine-required`** (validate-only, Audit mode). Allowed set: **`backup-engine ∈ {restic, kopia, migrating}`** — `migrating` is enumerated explicitly because Phase 5 used to need it; v3 doesn't use it for the cutover anymore but enumerating it costs nothing and makes the schema future-proof. Closes Codex v1 finding [3]/[6] AND v2 finding [3].

3. **Step 3c — add `backup-engine: restic` selector to every rule of the existing `volsync-pvc-backup-restore` policy IN PLACE (no rename).** This is a `match.any.resources.selector.matchExpressions` addition only — no `generate.data` mutation, no rename, no force-recreate annotation. **Pre-merge verification:**
   - Step 3a verification gate (above) passes.
   - `kubectl diff -f infrastructure/controllers/kyverno/policies/volsync-pvc-backup-restore.yaml` shows ONLY the selector additions, no `metadata.name` change, no `generate.data` change.
   - flux-local CI green.
   
   **Risk if 3a parity is off (Codex v2 finding [2]):** PVCs without the engine label silently lose their generated Secret/RS/RD when the selector tightens. The 3a gate above prevents this.

4. **Step 3d — deploy `volsync-pvc-backup-restore-kopia`** as a NEW ClusterPolicy file. Identical 7-rule structure to the Restic policy, with:
   - Engine selector: `backup-engine: kopia`
   - Oracle URL: routes to the upstream `pvc-plumber` Service (deployed in Phase 2)
   - Rule 5 generates Secret with `KOPIA_PASSWORD` + S3 creds (not `RESTIC_*`)
   - Rule 6 generates RS with `spec.kopia:` block
   - Rule 7 generates RD with `spec.kopia:` block
   - **EXCLUSION FOR CNPG DATA PVCs** — by **label**, NOT namespace (Codex v3 finding [3]). v3 erroneously said "exclude CNPG namespaces"; that would also block the app data PVCs in `authentik`, `dawarich`, `joplin`, `wiki-js` which we DO want to migrate. The CNPG operator labels its managed PVCs with `cnpg.io/cluster=<cluster-name>`, so excluding-by-label is precise. Preflight to verify the label exists on live CNPG PVCs:
     ```bash
     kubectl get pvc -A --show-labels | grep -E "cnpg.io/cluster|app.kubernetes.io/managed-by=(cnpg|cloudnative-pg)"
     ```
     Then both engine policies use this exclude shape:
     ```yaml
     exclude:
       any:
         - resources:
             selector:
               matchExpressions:
                 - key: cnpg.io/cluster
                   operator: Exists
         - resources:
             selector:
               matchExpressions:
                 - key: app.kubernetes.io/managed-by
                   operator: In
                   values:
                     - cnpg
                     - cloudnative-pg
     ```
     CNPG-managed databases are out of scope for this label-driven backup system — they use `Cluster.bootstrap.recovery` per [`backup-recovery.md`](./backup-recovery.md).
   - **`synchronize: true` and `generateExisting: true` retained** — same drift-correction as the Restic policy.

5. **Step 3e — validate on a scratch PVC** in a one-off test namespace. Label it `backup: daily, backup-engine: kopia`. Verify Kyverno generates the Kopia Secret/RS/RD trio. Manually trigger a backup via `manual:` trigger to confirm Kopia mover writes to the shared repo. **Do NOT validate on a real Phase 5 candidate yet — Phase 5 handles cutover protocol separately.**

**Exit criteria:** Three policies live in Audit mode. Existing 42 Restic-backed apps continue to operate unchanged (Restic policy still gates them via their `backup-engine: restic` label from step 3a). A scratch test PVC successfully generates a Kopia trio. CNPG data PVCs (label-selected via `cnpg.io/cluster Exists`) are excluded from both engine policies.

**Rollback:** Three independent policies, three independent rollbacks. Delete the Kopia policy alone, or delete the deny-validate policy alone, or revert the Restic-policy selector PR. No big-bang rollback required.

---

### Phase 4 — History migration strategy (decision locked, ~1 hr — execution folded into Phase 5)

**Decision (closes §6 Open Question #2, per Codex v1 findings [4] / [9] + v2 finding [7]):** **Cold start, with a parallel-Kopia-RS verification gate per app.** Each app's Stage B in Phase 5 spawns a Kopia RS ALONGSIDE the still-active Restic RS and waits for it to commit a snapshot BEFORE the engine label is flipped. The Restic RS stays the authoritative path until the moment of flip, eliminating the "neither engine owns this PVC" interval that v2 had.

> **v2→v3 change (Codex v2 finding [7]):** v2 used a transient `backup-engine: migrating` label that caused the Restic policy to GC its Secret/RS/RD via `synchronize:true` BEFORE the Kopia probe ran. If the probe failed, the app was left with zero backup objects from either engine. v3 fixes this by spawning the Kopia probe RS DIRECTLY (not via the policy), keeping the Restic RS active until the probe commits a snapshot, and only then flipping the engine label.

Alternatives considered and rejected:
- **Pure cold start (rejected):** Daily-schedule apps cut over after 02:MM UTC have a full-day window with no Kopia restore point. Unacceptable for high-value apps.
- **Bridge migration (deferred):** 2-3 days of bespoke tooling against ~9000 snapshots across 41 apps. Old Restic repo serves as historical archive instead (see emergency runbook §9).

**Implementation (in Phase 5 driver):**

For each app, the cutover order is:
1. **Suspend Flux** for the app's Kustomization (existing Stage B pattern).
2. **Spawn a parallel Kopia RS** directly via `kubectl apply` (NOT via Kyverno) at the name `<pvc>-cutover-probe`. The Restic-side `<pvc>-backup` RS remains active throughout — both engines are writing to the source PVC's snapshot, which is fine because they target different repos.
3. **Wait** for the probe RS to commit a snapshot (`status.lastSyncTime` advances). 30-min timeout. On timeout: abort, leave Restic in place, surface error to operator.
4. **Verify** the snapshot exists in the Kopia shared repo via `kopia snapshot list <ns>/<pvc>`.
5. **Flip the engine label** on the PVC manifest: `backup-engine: restic` → `backup-engine: kopia`. This single edit causes:
   - Restic policy's `synchronize:true` GCs the Restic Secret/RS/RD (this is now safe — Kopia has a committed snapshot)
   - Kopia policy emits the durable Kopia Secret/RS/RD trio
6. **Delete the probe RS** (the durable Kopia RS supersedes it).
7. **Stage C** verifies + resumes Flux.

The driver probe-RS template (sketch — refined in Phase 5 prep):
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
kubectl -n "$NS" wait --for=jsonpath='{.status.lastSyncTime}' --timeout=30m \
  replicationsource.volsync.backube "${PVC}-cutover-probe"
# If wait fails: abort cutover, leave Restic RS active, do NOT flip engine label.
```

**Recovery-depth guarantee per app post-cutover:** ≥1 Kopia snapshot exists at the moment the engine label flips. The Restic RS is still active up to that moment, so any data written between the last scheduled Restic backup and the flip is captured in the Kopia probe snapshot. Zero-gap handoff.

**High-value app cutover-window discipline:** Cut over in the window IMMEDIATELY after the last successful Restic backup. This minimizes data drift between final Restic snapshot and first Kopia snapshot — both for restore depth and for the emergency runbook fallback.

**CNPG data PVCs are out of scope (Codex v2 finding [5] + [6] Q5/CNPG):** PVCs owned by CloudNativePG `Cluster` resources are NOT migrated through this Phase 5 protocol. CNPG handles its own application-consistent backup/restore via WAL streaming + base backups + `Cluster.bootstrap.recovery` (see [`backup-recovery.md`](./backup-recovery.md) §"CNPG Recovery"). Phase 3 step 3d adds an explicit **label-based** exclude (`cnpg.io/cluster Exists` OR `app.kubernetes.io/managed-by ∈ {cnpg, cloudnative-pg}`) for CNPG data PVCs — NOT a namespace-based exclude, since CNPG dbs share namespaces with app PVCs we DO want to migrate. The current Phase 5 candidates list below has CNPG db PVCs scrubbed.

**Phase 5 candidates (CNPG-scrubbed):** Apps in the high-value bucket are now their non-db PVCs only:
- `authentik/authentik-media` (the auth metadata PVC — `authentik-db` is CNPG, excluded)
- `vaultwarden/vaultwarden` (passwords — non-CNPG)
- `home-assistant/home-assistant` (state)
- `joplin/joplin` (notes — `joplin-db` is CNPG, excluded)
- `dawarich/dawarich` (data — `dawarich-db` is CNPG, excluded)
- `wiki-js/wiki-js-data` (content — `wiki-js-cnpg-db` is CNPG, excluded)

**Exit criteria:** decision locked at "cold start with parallel-probe-RS handoff". Probe-RS template + driver flow drafted. No execution-time work in Phase 4 — all inside each Phase 5 cutover.

---

### Phase 5 — Per-app cutover Restic → Kopia (~15 min/app, **3-4 days** for all 70)

**Output:** Every (non-CNPG) backup-labeled PVC has `backup-engine: kopia` and is generating snapshots into the Kopia shared repo. Restic-side RSes deleted per-app. Each app has ≥1 verified Kopia snapshot committed BEFORE its Restic objects are GC'd.

**The cutover protocol — parallel-probe-RS handoff (replaces v2's `migrating` flow):**

For each app, the sequence is:

1. **Stage B Job — driver `scripts/migrate-stage-bc.sh --engine kopia` (extended in Phase 5 prep).** The driver runs while `backup-engine: restic` is STILL set on the PVC. Steps:
   1. `flux suspend kustomization` for the app's namespace.
   2. Preflight: confirm app's Restic Secret/RS/RD exist (this app IS currently backed up on Restic).
   3. **Same-PVC CSI snapshot preflight gate (Codex v3 finding [1]):** wait for any active Restic backup Job on this PVC to finish before spawning the Kopia probe. Two concurrent CSI VolumeSnapshots on the same source PVC are not a data hazard (each is independent crash-consistent), but they pressure the snapshotter queue, Ceph metadata, and worker memory. Gate:
      ```bash
      while kubectl -n "$NS" get jobs \
            -l volsync.backube/source-name="${PVC}-backup" \
            -o jsonpath='{.items[?(@.status.active>0)].metadata.name}' \
            | grep -q .; do
        echo "Restic backup active for ${NS}/${PVC}; waiting…"
        sleep 30
      done
      ```
      Implementation note: probe is best timed IMMEDIATELY AFTER the last successful Restic backup, not during a scheduled run. Driver should check the Restic RS's `status.nextSyncTime` and abort if the next run is within 5 minutes.
   4. **Apply the parallel Kopia probe RS** at `<pvc>-cutover-probe` (NOT via Kyverno — direct `kubectl apply`). The probe RS targets the same source PVC as the Restic RS. Both engines now have RSes against the same PVC, but only the Kopia one will run (Restic was preflighted in step 1.3). Each engine writes to a different repo, each takes its own CSI VolumeSnapshot for `copyMethod: Snapshot`, and Kopia's locking model permits concurrent writers.
   5. **Wait** for the probe RS's `status.lastSyncTime` to be set (30-min timeout). Verify the snapshot exists in the Kopia shared repo via `kopia snapshot list <ns>/<pvc>`.
   6. **If probe fails (timeout, error, repo unreachable):** abort cutover via `set -e` trap. The Restic RS is still active and untouched. The probe RS gets deleted on abort. App remains on Restic. Operator picks up next session.
   7. **Flip the engine label on the LIVE PVC ONLY:** `backup-engine: restic` → `backup-engine: kopia`. Apply via `kubectl patch`. **This is a LIVE-cluster edit, NOT a git-manifest edit.** The PVC manifest in git still says `backup-engine: restic` at this moment. Flux MUST remain suspended. This patch triggers:
      - Restic policy's `synchronize:true` GC of the Restic Secret/RS/RD (now safe — Kopia has a committed snapshot).
      - Kopia policy emits the durable Kopia Secret/RS/RD trio.
   8. **Wait** for Kopia's durable RS to appear and Restic's RS to GC. Typically <60s.
   9. **Delete the probe RS** (the durable Kopia RS supersedes it).
   10. **Exit Stage B with Flux still suspended. Do NOT resume Flux in Stage B.**

2. **5b PR — confirm static PVC manifest** in git has `backup-engine: kopia` (the live cluster does — the manifest must match for next Flux reconcile to be a no-op). Merge while Flux is suspended.

3. **Stage C Job — driver resumes Flux ONLY after explicit verification.** The race v3 risked (Codex v3 finding [2]): if anyone resumes Flux before 5b PR merges, Flux reverts the live PVC label to `restic`; Kyverno regenerates Restic children and GCs Kopia children, rolling the app back while the operator believes it migrated. Strict Stage C order:
   ```text
   1. Run: flux reconcile source git flux-system
   2. Verify: flux build kustomization <app-kustomization> | grep -q "backup-engine: kopia"
      → if missing: ABORT, do not resume Flux, operator investigates
   3. Verify: flux build kustomization <app-kustomization> | grep -v "<pvc>-backup" | not match Restic-RS-shape
      → if a Restic RS is rendered: ABORT, do not resume Flux
   4. Only after 1+2+3 pass: flux resume kustomization <app-kustomization>
   ```
   If verification fails, the live cluster keeps the Kopia label (Stage B already flipped it). The migration is functionally complete; only the git/reconcile alignment is pending. Operator merges 5b, re-runs Stage C.

**Why the parallel-probe-RS approach is safe (Codex v2 finding [7]):**

The previous `migrating` design created a window where neither engine had a Secret/RS/RD for the PVC. If anything failed in that window, the app was unprotected. v3's parallel-probe approach keeps the Restic RS active right up to the engine-label flip — there is no point in the protocol where the PVC has zero backups in flight.

**Concurrent-RS safety:** During step 1.3–1.6, two RSes target the same source PVC. VolSync handles this — each RS takes its own CSI VolumeSnapshot. There is no shared lock at the VolSync level. Restic's mover holds Restic-repo locks; Kopia's mover holds Kopia-repo locks (or none, if Phase 0 audit confirms concurrent-multi-writer). No interaction.

**Order (4 batches) + concurrency cap (Codex v3 finding [6]):**

The 2026-05-17 incident showed that even 10–12 concurrent volsync movers can OOM a worker (51 RSes → worker-2 SystemOOM; 42 restore movers at 1.5–2 GiB each → worker-1 saturated). Batch SIZE in the plan is one thing; CONCURRENT IN-FLIGHT probe RSes is the actual control variable. v4 caps in-flight concurrency with a driver semaphore:

```text
# In scripts/migrate-stage-bc.sh (Phase 5 prep)
MAX_CONCURRENT_PROBES=3                  # normal ceph-rbd PVCs
BIG_DATA_MAX_CONCURRENT_PROBES=1         # cephfs OR PVC > 50Gi
NODE_MEM_HEADROOM_PCT=70                 # don't start a new probe if any node > 70% mem
```

Start the next probe only after the prior probe RS has committed, its durable Kopia RS exists, AND `kubectl top nodes` shows every worker below the headroom threshold. Operator can override via `--max-concurrent 1` flag if cluster pressure mounts.

Batch order:
1. **High-value first** (5 apps, 1 session, serial — concurrency=1 to validate the protocol): authentik-media, vaultwarden, home-assistant, dawarich (data PVC only, not db), wiki-js-data (not the CNPG db). Cut over immediately after each app's last successful Restic backup.
2. **Already-on-Restic medium** (~35 apps, ~3 sessions): concurrency capped at 3 per the semaphore. Batch LIST size of 12 alphabetically is fine — only 3 run at any moment.
3. **Never-on-Restic** (~25 apps, ~3 sessions): direct-to-Kopia. Different protocol — these never had a Restic RS, so the Phase 5 driver becomes: set `backup-engine: kopia` label, let Kopia policy emit, verify first scheduled snapshot lands. No probe-RS needed. Concurrency still capped at 3.
4. **Big-data deferrals last** (5 apps: plex, jellyfin, calibre-library-cephfs, shared-media-pvc, notifiarr-shared, dumb): concurrency **1** because these are cephfs RWX or 100Gi+ source PVCs. Each runs serially with appropriate `cacheCapacity` annotations confirmed via Phase 2 smoke tests.

**Pace target:** 5–7 apps per session × 6–8 sessions across 3–4 days. Each Restic→Kopia cutover ~15 min wall time; with concurrency 3, that's ~6–9 apps per active hour. Never-on-Restic apps ~10 min each.

**Exit criteria:** Every non-CNPG backup-labeled PVC has a Kopia-backed RS. Zero Kyverno-managed Restic RSes remain. Every app has ≥1 verified Kopia snapshot in the shared repo.

**Rollback per app (within 30-day Restic-bucket retention window):** Revert the 5b PR (`backup-engine: kopia` → `restic`). Kopia policy GCs its trio; Restic policy regenerates its trio. The Restic repo still has the historical snapshots — restore via the Restic populator on a fresh PVC, OR via the §9 emergency runbook for an in-place restore.

---

### Phase 6 — Decommission the Restic stack (0.5 day, ~3 hr)

**Output:** Restic infrastructure removed from the cluster. `volsync-shared/restic` Garage bucket retained in cold storage for 30 days as emergency historical archive.

**Activities:**
1. Verify §9.1 scratch-namespace runbook validation passes (gate from Codex v3 finding [5]).
2. Suspend `volsync-restic-forget` CronJob (don't delete yet).
3. Suspend `pvc-plumber-restic` Deployment (replicas → 0).
4. Delete the Restic Kyverno policy `volsync-pvc-backup-restore`. **Pre-condition (Codex v4 finding [4]):** confirm zero PVCs carry `backup-engine: restic` first via `kubectl get pvc -A -l backup-engine=restic --no-headers | wc -l` → must be 0. **DO NOT** delete `volsync-pvc-backup-restore-kopia` (the active Kopia policy that all 70 PVCs depend on) and **DO NOT** delete `volsync-pvc-engine-required` (the validate-only policy that enforces the engine label). Only the Restic-specific policy goes.
5. Wait 24h. Verify no app is broken — every backup-labeled PVC should have `backup-engine: kopia` and a working Kopia RS at this point.
6. PR: delete `infrastructure/controllers/pvc-plumber-restic/` directory entirely.
7. PR: delete `infrastructure/controllers/volsync/app/volsync-restic-forget-cronjob.yaml`.
8. PR: delete `infrastructure/secrets-prod/volsync-shared-base.sops.yaml` (the old Restic-creds Secret).
9. Mark `mainertoo/pvc-plumber-restic` GitHub repo archived (settings → archive). Keep tags; just freeze.
10. Garage: keep `volsync-shared` bucket read-only for 30 days, mark for deletion 2026-06-17 (calendar reminder).
11. Update `docs/label-driven-backups.md` to remove any restic-specific examples in favor of kopia-specific ones (PVC label + annotations are identical from user perspective).

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

## 3a. Phase 0 deeper-audit checklist (Codex v4 finding [6])

Phase 0 audit step 3 covered the BLOCKER criteria and verified `do_backup`, `do_retention`, `do_maintenance`, controller `retainPolicy == nil` handling, and S3 env-var compatibility. With 2473 LOC in `entry.sh`, four more areas need review BEFORE Phase 5 starts (not before Phase 1 — these are mover-side concerns that only matter when actual backups run):

| Function / path | What to verify | Gate (corrected v7 per Codex v5 finding [4]) |
|---|---|---|
| `connect_repository` (line 1398) | Password-mismatch behavior. If the per-PVC Secret has a stale `KOPIA_PASSWORD` (e.g., during password rotation), does the script error out clearly, or retry indefinitely? | **DURING Phase 3 step 3e scratch validation** — include a deliberate wrong-password test case (apply a scratch PVC with a Secret containing a garbage `KOPIA_PASSWORD`, confirm the mover Job fails with a clear error within 1 minute, not infinite retry). |
| `create_repository` (line 1705) | First-time repo creation. We init the Kopia repo manually in Phase 1b — confirm the mover script doesn't try to re-init or overwrite the existing repo on first backup against it. | **DURING Phase 3 step 3e scratch validation** — the scratch test IS first-backup-against-existing-repo. Confirm the mover's `connect_repository` succeeds (existing repo found) AND `create_repository` is skipped (not invoked because repo already exists). Inspect Job logs to verify no "creating new repository" output. |
| Garage S3 retry/timeout | Network-transient handling. Garage occasionally returns 503s under load (we saw this with restic). Does the Kopia mover retry with backoff, or fail-fast? Look for `--retries`, `--retry-interval`, timeout flags. | **DURING Phase 1b** smoke test (50 calls, record p50/p95). **RE-CHECK before Phase 5 batch 2** under load. |
| `do_restore` (line ~2291) | Restore mode error handling. Does a failed restore leave the target PVC half-populated? Does the script abort cleanly so the volsync controller marks the RD failed? | Before Phase 7 DR drill (lower urgency — failed restores are recoverable from S3 historical archive). |
| `ensure_maintenance_ownership` (line 2000) | Already audited 2026-05-18 — uses `kopia maintenance set --owner=` with `KOPIA_OVERRIDE_MAINTENANCE_USERNAME` env var (default `maintenance@volsync`). Confirmed safe. | ✅ done |

**v6 → v7 → v8 evolution:** v6 had `connect_repository` and `create_repository` gated at "Phase 5 batch 1" which would have meant discovering password-mismatch or retry-loop bugs against the high-value apps. v7 moved them to "BEFORE Phase 3 step 3e scratch validation" — but Codex v6 review pointed out step 3e IS the scratch test, so we'd be auditing a function we couldn't actually exercise without running the test we're gating. **v8 fix: gate is now "DURING Phase 3 step 3e"** — the scratch test deliberately includes the wrong-password + existing-repo-reconnect test cases. Discovery on throwaway PVC, AND we use the scratch test itself to exercise the functions instead of trying to audit them in isolation.

Each checklist item logs findings as comments in this section. Items don't gate Phase 1, but each gates the specific phase noted.

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

## 6. Open questions

Closed by v2:
- ~~#2: history migration strategy~~ → Phase 4 locked at "cold start with parallel-probe-RS handoff" (v3 refined the handoff mechanism)
- ~~#5: two policies vs. one~~ → Phase 3 locked at "two engine-gated policies + one deny-on-missing-engine validate policy"
- ~~#6: `backup-engine` discriminator~~ → Phase 3 locked at "mandatory label, never inferred by webhook"

Closed by v3 (Codex v2 finding [6] answers + restructuring):
- ~~v2 #3 — Big-data cache sizing on Kopia~~ → **Default `cacheCapacity: 10Gi` for any PVC over 100Gi.** Phase 2 smoke-tests a scratch cephfs PVC at full scale before any big-data app cutover. Smoke test is a Phase 2 explicit gate.
- ~~v2 #5 — CNPG live-snapshot consistency~~ → **CNPG data PVCs are EXCLUDED from the label-driven backup system entirely.** Restore via `Cluster.bootstrap.recovery` per [`backup-recovery.md`](./backup-recovery.md). Phase 3 step 3d adds explicit excludes; §9 runbook has a CNPG callout pointing at the right path.
- ~~v2 #7 — Restic policy rename hazard~~ → **The policy is no longer renamed in Phase 3.** v3 keeps the original name through the transition; rename happens in Phase 6 alongside deletion, where no PVC depends on it.
- ~~v2 #4 — Phase 0 trap severity rubric~~ → Decision tree now lives in Phase 0 itself (BLOCKER vs. WORKAROUND lists).

### 6.1 Fork governance — what happens if/when (v5 addition)

Adopting `perfectra1n/volsync` means a long-running dependency on a community fork. Define the exit criteria now, BEFORE the dependency becomes load-bearing:

| Event | Response |
|---|---|
| Upstream PR #1723 merges into `backube/volsync` | Watch for the release that ships Kopia upstream. Plan a "Phase 8" project to migrate `perfectra1n/volsync` → `backube/volsync@vX.Y.Z`. Same CRD shape, same image-override pattern. Should be a low-risk 1-day PR. |
| `perfectra1n/volsync` stops getting commits for >6 months | Snapshot current behavior (image digest pinned), watch for upstream alternatives. If none, consider forking-the-fork at the last-good commit, applying our own upstream-CVE backports. |
| Critical CVE in upstream's restic mover or controller, fork hasn't merged it | Decision tree: Critical → cherry-pick into local fork-of-the-fork; High → wait 1 week for `perfectra1n/volsync` to rebase; Medium → patch on next monthly window. |
| mitchross stops using the fork | They're the north-star reference. Subscribe to their commits or check quarterly. If they pivot, understand why and consider following. |

Still open, for the user to decide before Phase 1:

1. **Dual-pvc-plumber operation during Phase 5 — DECIDED IN V5.** Run two oracles, two Kubernetes Services. One is our existing `mainertoo/pvc-plumber-restic` fork (serving the Restic-backed apps until they cut over); one is `mitchross/pvc-plumber:3.1.0` (serving the Kopia-backed apps). Each engine-specific Kyverno policy points at its own pvc-plumber. Phase 6 deletes our restic fork; mitchross's pvc-plumber becomes the sole oracle.

2. **Bucket reuse vs. new bucket.** Option A (new bucket `volsync-kopia-shared`) cleaner; Option B (reuse `volsync-shared` with prefix) less Garage admin work. **Garage capacity check is the deciding factor.** Run in Phase 1 first task. Lean: **Option A** if capacity allows.

3. **30-day Restic-bucket retention window.** Is 30 days the right post-Phase-6 horizon, or 60-90? Lean: **30 days** — the Restic-side daily snapshots already roll off at that horizon. No marginal value keeping the bucket longer than its own snapshots' retention.

4. **Validate-policy enforcement timing.** Phase 3 step 3b deploys the deny-on-missing-engine validate policy in Audit mode. When to flip to Enforce? Lean: **after Phase 5 exit, before Phase 6 starts.** Codex v2 finding [6] said "require Enforce before Phase 5" — but that would block any Phase 5 admission if the label is mis-set, with no safety net. Better: Audit during Phase 5 (catches mistakes via reports), Enforce during Phase 6 (locks the contract once everything is on Kopia).

5. **Rollback at Phase 6 boundary.** Phase 5 exit criteria require every app to have ≥1 verified Kopia snapshot — so Phase 6 only starts when fully cut over. The 30-day Restic-bucket window IS the rollback safety net. No further "Phase 5.9" pause needed.

---

## 7. Risk register (transition-specific, v5 with all Codex findings folded in)

| Risk | Severity | Mitigation |
|---|---|---|
| **Adopting `perfectra1n/volsync` fork = ongoing dependency on a single-maintainer community fork** (v5 finding) | **HIGH** | (a) Watch upstream `backube/volsync` releases manually — Renovate or weekly script; (b) compare upstream Restic mover changes against fork's; (c) plan to re-evaluate at each minor-version boundary; (d) keep this transition's PRs annotated so a re-platform later is reproducible. mitchross's research doc explicitly flags this as the cost. |
| **CVE in upstream `backube/volsync` not yet pulled into the fork** (v5 finding) | **HIGH** | Subscribe to GitHub Security Advisories for `backube/volsync`. If a critical CVE lands, options: (a) cherry-pick the fix into a local fork-of-the-fork; (b) revert to upstream Restic temporarily; (c) wait for `perfectra1n/volsync` to rebase. Document the decision criteria BEFORE the first CVE happens. |
| **`perfectra1n/volsync` fork stagnates further** (v5 finding) | MED | Three exits: (a) PR #1723 finally merges upstream — we move to upstream; (b) another community member forks the fork — we follow; (c) we maintain our own fork-of-the-fork. The pattern from mitchross's research is "stay on the most-maintained Kopia-capable fork". |
| **Mitchross's pvc-plumber v3 has its own bugs we'll discover** (v5 finding) | MED | The image is in production at mitchross's cluster with 4 documented backup-restore drills. Smoke-test in Phase 2 before relying on it. If we hit a bug, file upstream against `mitchross/pvc-plumber`; meanwhile, fall back to running both old (`pvc-plumber-restic`) and new (`pvc-plumber`) until the issue is resolved. |
| **JobMutator pattern (admission-time mover-Job mutation) bites us** (v5 finding from mitchross's research) | **HIGH (avoided by design)** | We DO NOT replicate mitchross's old `JobMutator` pattern. Per their own research doc, JobMutator + volsync's `CreateOrUpdateDeleteOnImmutableErr` causes cluster-wide outages. Our Kyverno policy generates Secret/RS/RD — it never mutates the mover Job spec. The mover gets all creds via env vars from the per-PVC Secret (S3 pattern). |
| **`mover-kopia/entry.sh` has an analog of Restic's `--retry-lock=0s` trap** (Codex v1 finding [10], v5 audit redirected to perfectra1n) | **HIGH** | Phase 0 mandatory audit step 3 — Phase 1 BLOCKED until completed against PERFECTRA1N's mover (not upstream). v5 BLOCKER/WORKAROUND criteria apply. |
| Kopia + Garage S3 incompatibility | **HIGH** | Smoke test in Phase 1 BEFORE Phase 3 starts. If incompatible: fall back to S3-compatible MinIO or NFS-backed Kopia. |
| **Dual-policy collision** during Phase 5 — both Restic + Kopia rules attempt to generate same-named Secret/RS/RD (Codex [1]) | **HIGH** | Phase 3 step 3c gates the Restic policy with `backup-engine: restic` selector BEFORE the Kopia policy is deployed. Verified via `kubectl diff` before merge. |
| **Force-recreate UR herd** if Phase 3 Restic policy edit is done as delete+recreate (Codex [2]) | **HIGH** | Phase 3 step 3c is a label-selector tightening, NOT a `generate.data` mutation — should patch in-place. Pre-merge `kubectl diff` confirms no immutable-field error. If immutable-field error appears, abort the merge and reconsider. |
| **Cold-start 0–24h restore gap** for an app post-cutover (Codex [4]) | **HIGH** | Phase 5 Stage B includes a mandatory cutover-probe Kopia snapshot, verified before Restic authority is released. No app is "migrated" until probe succeeds. |
| **`synchronize:true` makes mid-flight engine switching destructive** (Codex v2 [7]) | **HIGH (mitigated)** | v3 onward: parallel-probe-RS handoff. Kopia probe RS spawned DIRECTLY via `kubectl apply` (not Kyverno) while the Restic RS stays active. Engine label flips only after probe commits. The earlier `backup-engine: migrating` intermediate state was abandoned in v3 — see Phase 5 cutover protocol. |
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

## 9. Emergency restore runbook — Restic-side, valid until Phase 6 + 30 days

> **⚠ DO NOT USE THIS RUNBOOK FOR CNPG-MANAGED DATABASE PVCs** (Codex v2 finding [5]). CNPG owns its data PVCs' lifecycle and reconciles them from the `Cluster` resource. Annotating a CNPG-owned PVC with `volsync.backup/skip-restore: "true"` will get overwritten on the next operator reconcile, and `rsync`ing data into a live Postgres data directory violates CNPG's recovery semantics (WAL replay, base backup restore, etc.). For CNPG apps, restore via `Cluster.bootstrap.recovery` — patch the app's db `Cluster` manifest to add a `bootstrap.recovery:` block, push to git, let Flux reconcile, wait for `Cluster Ready`, reconnect the app. Full procedure in [`docs/backup-recovery.md`](./backup-recovery.md) §"CNPG Recovery".
>
> **This runbook applies to NON-CNPG PVCs only.** Examples in scope: vaultwarden, home-assistant, joplin (the notes PVC, not the CNPG db), wiki-js-data (the content PVC, not the CNPG db), media app config PVCs, etc.

If a non-CNPG PVC needs restore between cutover (Phase 5) and Restic-bucket retirement (Phase 6 + 30 days):

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

### 9.1 Pre-Phase-6 runbook validation test (Codex v3 finding [5])

This runbook is **theory until proven on this cluster**. The referenced smoke-test template (`apps/archive/04-volsync-shared-init-smoketest.yaml`) only validates restic backup/restore/diff in `/tmp`, not `rsync` into a mounted PVC. Before Phase 6 begins (after Phase 5 cutovers complete), run this scratch validation on the live cluster to prove the recipe works end-to-end:

```bash
# 1. Create scratch namespace
kubectl create ns volsync-restore-runbook-test

# 2. Create source PVC + target PVC, both 1Gi ceph-rbd
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: runbook-source, namespace: volsync-restore-runbook-test }
spec: { accessModes: [ReadWriteOnce], storageClassName: ceph-rbd, resources: { requests: { storage: 1Gi } } }
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: runbook-target, namespace: volsync-restore-runbook-test }
spec: { accessModes: [ReadWriteOnce], storageClassName: ceph-rbd, resources: { requests: { storage: 1Gi } } }
EOF

# 3. Write known content + sha256 manifest to runbook-source via a writer Pod
# 4. Run restic backup against the source, tagged volsync-restore-runbook-test/runbook-source
# 5. Run the §9 restore Job: mount runbook-target at /data and restic restore output at /restore
# 6. rsync -aHAX /restore/.../runbook-source/ /data/ inside the Job
# 7. Verify sha256sum on runbook-target matches the manifest from step 3
# 8. Tear down: kubectl delete ns volsync-restore-runbook-test
```

**Pass criteria:** sha256 manifest from step 7 byte-for-byte equal to step 3's manifest. **Until this test passes, do NOT proceed with Phase 6 retiring the Restic stack.** Test against scratch PVCs only — do NOT use production app PVCs as the runbook's first real exercise.

---

## 10. Audits + Codex adversarial reviews — addressed findings

### v8 — Codex v6 review of v7 (2026-05-18 late-morning)

6 findings: 1 HIGH, 3 MED, 2 confirmed CLEAN. Diminishing-returns signal — first review with explicit CLEAN findings on previous fixes. v8 is the final pre-execution rev.

| # | Severity | Finding | Resolution in v8 |
|---|---|---|---|
| 1 | MED | Phase 1a kind-validation script had no prerequisite check that `kind`/`k3d` AND a container runtime are available | Added prerequisite block at top of the validation script: `command -v kind`, `docker info` (or podman fallback), `helm`, `yq` checks all gate execution. Falls through to the kubeconform alternative path if missing. |
| 2 | LOW | **CLEAN** | Confirmed kubeconform `scripts/openapi2jsonschema.py` exists at the cited path. No issue. |
| 3 | LOW | **CLEAN** | Confirmed Restic 10s / Kopia 20s webhook timeout split is safe — separate pvc-plumber Services, no shared admission. No issue. |
| 4 | MED | Phase 1a `kustomize build | grep -E "^(kind|name|sourceRef)" -A1` check doesn't catch what it claims — kustomize indents `metadata.name` and `sourceRef` under YAML parent keys; the anchored `^` regex never matches them. Same false-pass class as v6's CRD dry-run. | Replaced with `yq` filter that explicitly selects HelmRepository/OCIRepository/HelmRelease kinds and projects `{kind, name, chartRef, sourceRef}`. Verifies exact shape, no anchored-regex false-pass. |
| 5 | **HIGH** | §3a `connect_repository` / `create_repository` audit gated "BEFORE Phase 3 step 3e scratch validation" — but step 3e IS the scratch test. Gate "BEFORE" the test means we can't actually exercise the functions. | Gate moved to "**DURING** Phase 3 step 3e": the scratch test deliberately includes a wrong-password test case (Secret with garbage password, expect clear failure in <1 min) AND an existing-repo-reconnect test case (verify mover's connect succeeds, create is skipped). |
| 6 | MED | Stale references survived earlier revs: Phase 0 step 3 still marked 🔜 despite audit verdict at line 11; "CNPG namespaces" wording in 3 places vs. v6's label-based exclude; obsolete risk-register entry referenced `migrating` as the live mitigation | Phase 0 step 3 flipped ✅; "CNPG namespaces" → "CNPG data PVCs by label" in §3 step 3d exit criteria + §4 Phase 4 OOS section; risk-register `synchronize:true mid-flight switching` row rewritten to reflect v3+ parallel-probe-RS approach. |

### v7 — Codex v5 review of v6 (2026-05-18 mid-morning)

6 findings, 2 HIGH, 1 MED, 3 LOW. The HIGH ones were real bugs (v6's CRD diff gate was a false-pass; the connect/create_repository audit gate was scheduled too late).

| # | Severity | Finding | Resolution in v7 |
|---|---|---|---|
| 1 | LOW | Cosign removal noted in §7 but no cluster-wide policy check confirmed | Added `rg` pre-merge check to Phase 1a: verify no cluster-wide `verifyImages`/`ImageValidatingPolicy` enforces signatures on HelmRepositories. Confirmed 2026-05-18 our cluster uses per-source cosign only. |
| 2 | **HIGH** | **CRD diff gate was a false-pass.** `kubectl apply --dry-run=server` validates against CRDs INSTALLED in the cluster (backube v0.15), not the v0.18 CRDs we pulled to `/tmp`. v6's gate could not detect schema breakage. | Phase 1a gate rewritten: spin up scratch `kind` cluster, install perfectra1n CRDs there, dry-run apply live RS/RD export against scratch cluster's API server. Alternative offline path via `kubeconform`. |
| 3 | LOW | `KOPIA_S3_DISABLE_TLS=false` correct, but v6 wording was unclear about WHY | Phase 2 table reworded with explicit 2026-05-18 verification trail (`docs/backup-architecture.md` + existing smoke tests confirm Garage is HTTPS external). |
| 4 | **HIGH** | `connect_repository` / `create_repository` audit gated at "before Phase 5 batch 1" = discover bugs against authentik/vaultwarden | §3a gate moved to **before Phase 3 step 3e** (scratch validation). Discovery on throwaway PVC. |
| 5 | MED | `HTTP_TIMEOUT=7s` copied verbatim from mitchross (in-cluster RustFS); our path goes HTTPS through Traefik | Bumped to 15s initially. Kyverno webhook for kopia policy bumped to 20s to coordinate. Phase 1b records p50/p95 over 50 calls; tune down if p95 < 2s. |
| 6 | LOW | Phase 1a swap not explicitly required to be atomic | Phase 1a now mandates **single PR / single commit** for all 4 file changes (delete OCIRepository, create HelmRepository, edit release, edit kustomization). Pre-merge `kustomize build` check enforces exactly one HelmRepository named `perfectra1n-volsync`. |

### v6 — Codex v4 review of v5 + Phase 0 audit step 3 (2026-05-18 AM)

**Codex v4 review against v5:** 6 findings, all from the perfectra1n-fork reframe. Codex was hampered by a sandbox/network read-only constraint mid-review (couldn't reach mitchross's repo or patch files), but the issues were enumerable from what it COULD read.

| # | Severity | Finding | Resolution in v6 |
|---|---|---|---|
| 1 | HIGH | v5 Phase 1a HelmRelease snippet used `HelmRepository.spec.url`, but our `volsync-release.yaml` uses `OCIRepository` + `chartRef` (verified 2026-05-18). Perfectra1n publishes via gh-pages, not OCI. | Phase 1a rewritten: DELETE OCIRepository, CREATE HelmRepository for `https://perfectra1n.github.io/volsync/charts`, EDIT release to use `spec.chart.spec.sourceRef`. Cosign verification dropped (perfectra1n unsigned). |
| 2 | HIGH | v5 said "adapt ExternalSecret to SOPS" without enumerating keys | Phase 2 now lists the 3 keys (`KOPIA_PASSWORD`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`), mount path (`/var/secret/pvc-plumber-kopia`, mode 0440), AND the 8 plain-value env vars on the deployment with mitchross-vs-us mapping table. |
| 3 | HIGH | Chart-swap v0.15→v0.18 may invalidate the 41 live Kyverno-managed RSes/RDs | Phase 1a now has explicit pre-merge `helm pull` + `kubectl apply --dry-run=server` CRD validation gate. Pass criterion: zero `unknown field` errors. |
| 4 | LOW | Phase 6 step 4 wording ambiguous after no-rename reframe | Step 4 now explicit: delete `volsync-pvc-backup-restore` only after confirming zero `backup-engine: restic` PVCs; do NOT delete the kopia or engine-required policies. |
| 5 | MED | `mitchross/pvc-plumber:3.1.0` source not pinned | Phase 2 source-pin block added: repo `github.com/mitchross/pvc-plumber` tag `v3.1.0`, image `ghcr.io/mitchross/pvc-plumber:3.1.0`, pin-by-digest instruction (`crane digest`). |
| 6 | MED | Phase 0 step 3 only covered BLOCKERs | New §3a checklist for `do_restore`, `connect_repository`, `create_repository`, Garage S3 retry/timeout, with phase-gating per item. |

**Phase 0 audit step 3 findings (read perfectra1n/volsync@v0.17.11/mover-kopia/entry.sh, 2473 LOC):**

| BLOCKER check | Result | Evidence |
|---|---|---|
| Silent exit-code handling | ✅ PASS | `do_backup` line 1979: `if ! run_with_progress_output...; then error 1 "Failed to create snapshot"`. `do_maintenance` line 2113: `|| maint_exit_code=$?` + explicit return on non-zero. `set -e -o pipefail` line 55. |
| Exclusive lock without retry | ✅ PASS | No `kopia repository lock` calls. Concurrent-multi-writer by Kopia design. Maintenance uses ownership model (`maintenance set --owner=`) not locks. |
| Destructive default retention | ✅ PASS | Controller `mover.go:898`: `if m.retainPolicy == nil { return envVars }` — zero `KOPIA_RETAIN_*` env vars passed when retain unset. Mover's `do_retention` then runs `policy set DATA_DIR` with no flags = no-op. The OPPOSITE of restic's `--keep-last 1` trap. |
| S3 cred incompat | ✅ PASS | Standard env vars: `KOPIA_PASSWORD`, `KOPIA_S3_ENDPOINT`, `KOPIA_S3_BUCKET`, `KOPIA_S3_DISABLE_TLS`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`. |
| `copyMethod: Snapshot` | ⚠ Deferred to Phase 2/3 | volsync CRD shape identical between movers; verify in Phase 3 step 3e scratch test. |

**Bonus positive finding:** entry.sh has `# Function removed: do_retention_global is no longer needed / Global retention should be configured through the KopiaMaintenance CRD if needed`. Perfectra1n actively separated per-backup work (light) from maintenance (CRD-driven heavy). Exactly the architecture we wanted.

### v5 reframe — Phase 0 audit step 1+2 findings (2026-05-18 AM)

Executing Phase 0's first audit step revealed v1–v4's central premise was wrong. v5 corrects.

| # | Severity | Finding | Resolution in v5 |
|---|---|---|---|
| A | **CRITICAL** | `backube/volsync` has **no Kopia mover** — not in v0.15.0, not on main, only an open 56k-line PR #1723 stalled since 2025-08-06 | Adopt `perfectra1n/volsync` fork (image `ghcr.io/perfectra1n/volsync:v0.17.11`, chart `perfectra1n/volsync 0.18.5`). Same fork mitchross uses. |
| B | HIGH | v1–v4 specified deploying "upstream pvc-plumber" with Kopia backend; pvc-plumber upstream is `mitchross/pvc-plumber`, not a multi-backend thing | Deploy `ghcr.io/mitchross/pvc-plumber:3.1.0` (mitchross's v3 operator with `KopiaMaintenance` CRD). Our `mainertoo/pvc-plumber-restic` stays for restic apps until Phase 6. |
| C | HIGH | Mitchross's research doc traces a previous cluster-wide outage to admission-time mutation of mover Jobs (JobMutator) racing volsync's drift-correct loop | EXPLICITLY AVOID JobMutator pattern. Our Kyverno policy generates Secret/RS/RD; never mutates mover Job spec. Mover gets creds via env vars. |
| D | HIGH | Adopting a community fork = long-term dependency on a single maintainer | §6.1 "Fork governance" subsection added with explicit exit criteria. §7 risk register has fork-specific entries. |
| E | MED | The Codex v1 finding [10] "audit `mover-kopia/entry.sh`" applied to a path that doesn't exist in upstream | Phase 0 audit step 3 redirected to `perfectra1n/volsync@v0.17.11/mover-kopia/entry.sh`. BLOCKER/WORKAROUND criteria evaluated against the fork. |

### v3 review (against v3 draft, 2026-05-17 ~05:30 local)

6 findings against v3 (2 HIGH, 4 MED). v3's biggest weakness was namespace-based CNPG exclusion — would have blocked migration of legitimate app PVCs in shared namespaces. v4 also tightens batch concurrency (3 in-flight max, 1 for big-data) and rewrites Stage B/5b/Stage C ordering explicitly to prevent a race that only matters if a driver-script implementor cuts corners.

| # | Severity | Finding | Resolution in v4 |
|---|---|---|---|
| 1 | MED | Same-PVC concurrent CSI snapshot pressure | Phase 5 step 1.3 adds a preflight gate that waits for active Restic backup Jobs before applying the Kopia probe RS. |
| 2 | HIGH | Stage B/5b/Stage C ordering race | Phase 5 step 1.10 + step 3 rewritten explicitly: Stage B exits with Flux suspended, 5b PR merges, source git reconciles, then Stage C verifies before resume. |
| 3 | HIGH | CNPG exclusion by namespace would block app PVCs in shared namespaces | Phase 3 step 3d switches to label-based exclude (`cnpg.io/cluster Exists` OR `app.kubernetes.io/managed-by ∈ {cnpg, cloudnative-pg}`). |
| 4 | MED | Phase 0 "destructive retention" BLOCKER too vague | Replaced with concrete Kopia commands (`snapshot expire --delete`, `maintenance run --full`, `policy set` with synthesized retention scope) — testable not vibes. |
| 5 | MED | §9 emergency runbook untested theory | §9.1 added: scratch-namespace validation test, sha256 byte-equal pass criterion, required to pass before Phase 6. |
| 6 | HIGH | "Batches of 12" reproduces today's herd | Phase 5 batch ordering caps in-flight concurrency at 3 normal / 1 big-data via a driver semaphore. Batch LIST size of 12 fine, but only 3 run at a time. |

### v2 review (against v2 draft, 2026-05-17 ~05:00 local)

7 findings against v2. v2's biggest weakness was the `backup-engine: migrating` Phase 5 cutover state, which Codex correctly identified as creating a "neither engine owns this PVC" window — fixed in v3 by the parallel-probe-RS approach.

| # | Severity | Finding | Resolution in v3 |
|---|---|---|---|
| 1 | HIGH | v2 only partially closed v1 findings 2, 4, 7, 9, 10 | Findings 2, 4, 7, 9 fully closed in v3 by Phase 3/5 restructure. Finding 10 closed by Phase 0 decision tree. |
| 2 | HIGH | Step 3c renamed the policy = force-recreate hazard | v3 drops the rename. Pure in-place selector patch. Rename deferred to Phase 6 where deletion makes it safe. |
| 3 | HIGH | `backup-engine: migrating` not enumerated in validate policy = direct contradiction | v3 abandons the `migrating` state entirely. Parallel-probe-RS approach has no transient label state. |
| 4 | HIGH | Phase 0 audit lacked BLOCKER/WORKAROUND criteria | v3 Phase 0 now has explicit BLOCKER and WORKAROUND lists with concrete examples. |
| 5 | MED | §9 runbook unsafe for CNPG PVCs | v3 §9 leads with a CNPG callout box pointing at `Cluster.bootstrap.recovery` per `backup-recovery.md`. |
| 6 | HIGH | Open Qs not closed: cache sizing + CNPG snapshot consistency | v3 closes both — `cacheCapacity: 10Gi` default for >100Gi PVCs (Phase 2 smoke test gate); CNPG data PVCs excluded from policy entirely. |
| 7 | HIGH | Phase 5 created no-backup interval before Kopia probe | v3 parallel-probe-RS: Kopia RS spawned ALONGSIDE the active Restic RS, not after. Engine label flips only after probe commits. |

### v1 review (against v1 draft, 2026-05-17 ~04:30 local)

10 findings against v1. All applied to v2.

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
| 2026-05-17 | Codex adversarial review v2 (7 new findings against v2) | Codex |
| 2026-05-17 | v3 applied — Phase 3 no-rename, Phase 5 parallel-probe-RS handoff (drops `migrating`), CNPG excludes, Phase 0 BLOCKER decision tree, §9 CNPG callout, cache-sizing locked at 10Gi default | Claude |
| 2026-05-17 | Codex adversarial review v3 (6 new findings against v3) | Codex |
| 2026-05-17 | v4 applied — CNPG exclude by label not namespace, concurrency cap (3 normal / 1 big-data), explicit Stage B/5b/C ordering, Phase 5 same-PVC preflight, Phase 0 BLOCKER specifics, §9.1 scratch validation test | Claude |
| 2026-05-18 | Phase 0 audit step 1+2 executed — discovered upstream volsync has NO Kopia mover; reframe required | Claude |
| 2026-05-18 | v5 applied — adopt `perfectra1n/volsync` fork + `mitchross/pvc-plumber:3.1.0`, §6.1 fork governance, §7 fork-CVE risk entries, JobMutator anti-pattern documented, Phase 0 audit step 3 redirected to perfectra1n's mover | Claude |
| 2026-05-18 | Phase 0 audit step 3 executed against perfectra1n@v0.17.11/mover-kopia/entry.sh — NO BLOCKERS, full findings in §10 | Claude |
| 2026-05-18 | Codex v4 adversarial review of v5 — 6 findings (1 HIGH HelmRepo shape, 2 HIGH Secret schema, 3 HIGH CRD compat, 4 LOW Phase 6 wording, 5 MED source pin, 6 MED audit checklist) | Codex |
| 2026-05-18 | v6 applied — Phase 1a rewritten for HelmRepository, Phase 2 enumerates Secret schema + 8 env vars + source pin, Phase 1a CRD diff gate added, Phase 6 deletion target made explicit, §3a deeper-audit checklist for Phase 5/7 gating items | Claude |
| 2026-05-18 | Codex v5 adversarial review of v6 — 6 findings (2 HIGH: CRD gate was false-pass, connect/create audit gate misplaced; 1 MED HTTP_TIMEOUT; 3 LOW cosign-policy check, TLS wording, atomic PR) | Codex |
| 2026-05-18 | v7 applied — CRD gate switched to scratch-cluster validation (no false-pass), §3a connect/create audit moved before Phase 3 step 3e, HTTP_TIMEOUT 7s→15s + Kyverno webhook 10s→20s, cosign-policy pre-merge check, atomic-PR requirement for Phase 1a | Claude |
| 2026-05-18 | Codex v6 adversarial review of v7 — 6 findings (1 HIGH §3a gate timing should be DURING not BEFORE, 3 MED kind prereq + grep→yq + stale references, 2 CLEAN findings — first diminishing-returns signal) | Codex |
| 2026-05-18 | v8 applied — §3a gate flipped BEFORE→DURING Phase 3 step 3e (with wrong-password + existing-repo test cases), kind/docker prereq block, `kustomize build` verification rewritten with `yq`, stale Phase 0 🔜→✅, CNPG namespace wording → label, risk register `migrating` row rewritten for parallel-probe-RS reality | Claude |
| TBD | Phase 0 sign-off (user reads v8, confirms commit to perfectra1n fork dependency) | User |
| TBD | Phase 1 start (HelmRelease swap + Kopia bucket) | User |
