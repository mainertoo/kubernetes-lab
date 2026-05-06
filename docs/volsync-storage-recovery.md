# VolSync Storage & Recovery — Design Doc

**Status:** DRAFT v9 — incorporates Codex adversarial reviews v1–v8 findings (see §10).
**Goal:** Cluster nuke → fully restored, all data, in ~10 min, no manual per-app restore steps.
**Pattern source:** Adapted from [`mitchross/talos-argocd-proxmox`](https://github.com/mitchross/talos-argocd-proxmox/blob/main/docs/volsync-storage-recovery.md), retargeted from Kopia/NFS to restic/Garage-S3.

> **Reviewers:** this doc commits to architectural decisions that drive 3–4 days of implementation plus per-app migration. The point of the review is to find decisions we'll regret, not nitpicks. Focus on assumptions that would break under cluster failure, partial outages, secret rotation, or operator error.

---

## 1. Stack at a glance — what we have vs. what's required

| Layer | Production cluster (this repo) | Required for the pattern | Notes |
|---|---|---|---|
| OS / K8s | k3s on Proxmox VMs | Any K8s ≥ 1.27 | ✓ |
| GitOps | Flux CD | Doesn't matter; ArgoCD/Flux/raw kubectl all work | ✓ retry-on-deny: Flux Kustomization `retryInterval: 2m` |
| CSI w/ snapshots | ceph-rbd, cephfs, both with VolumeSnapshotClass | CSI with VolumeSnapshot support | ✓ |
| VolSync | installed (`infrastructure/controllers/volsync/`) | required | ✓ |
| Backup mover | restic to Garage S3 | any VolSync mover | mitchross uses Kopia/NFS |
| Backup destination | Garage (in-cluster S3) | any S3 or filesystem reachable from cluster | ✓ |
| Admission engine | Kyverno 3.8.0 | required | ✓ today only has one mutate policy (jitter) with `failurePolicy: Ignore` |
| Backup-existence oracle | **none today** | small HTTP service answering `does ns/pvc have a backup?` | **fork pvc-plumber → restic backend** |
| Secret store | SOPS-encrypted secrets in git, age key bootstrapped manually | anything that produces a Secret | ✓ no external dependency, accepts a single manual paste at bootstrap |

---

## 2. Core architectural decisions

### 2.1 Shared restic repo with `<ns>/<pvc>` tags (NOT per-app repos)

**Decision:** A single restic repository at `s3://volsync-shared/restic` holds backups for every backup-labeled PVC in the cluster. Snapshots are tagged `<namespace>/<pvc-name>` for lookup.

**Rationale:**
- **Oracle simplicity.** `restic snapshots --tag <ns>/<pvc> --json --latest 1` is the entire query. No per-PVC credential lookup, no `RESTIC_REPOSITORY` map.
- **Cross-PVC dedup.** Restic chunks are content-addressed inside a repo. Per-app repos = zero dedup; shared repo dedupes runtime libs, fonts, ICU tables, timezone data across apps. Estimated 30–50% storage reduction for typical homelab app mix.
- **Single-credential bootstrap.** pvc-plumber holds one Secret with one set of restic creds; on cluster rebuild, that Secret is the only one needed for the oracle to function.

**Tradeoffs:**
- **Single password = full blast radius.** Leak the master password and every backup is decryptable. Accepted: this is a homelab, the password lives in the user's password manager.
- **No per-app retention tuning.** One global retention policy applies to all snapshots.
- **Migration cost.** Existing per-app repos must be merged into the shared repo (covered in §4).

**Considered and rejected:**
- *Per-PVC repos with deterministic paths derived from `<ns>/<pvc>`* — keeps credential isolation but loses dedup, complicates oracle by 2–3×, doesn't make bootstrap appreciably safer.
- *Switch from restic to Kopia* — Kopia's CDC dedup is strictly better than restic's, but compounds two big migrations (mover + admission). Deferred to a separate project.

### 2.2 Fork pvc-plumber for restic backend

**Decision:** Fork [`mitchross/pvc-plumber`](https://github.com/mitchross/pvc-plumber) → `mainertoo/pvc-plumber-restic`. Replace the Kopia client with a restic CLI shell-out. Keep cache layer, HTTP API contract, and Kubernetes manifests (minus the NFS volume mount).

**API contract (unchanged from upstream):**
- `GET /exists/{ns}/{pvc}` → `{"decision":"restore|fresh|unknown","authoritative":bool,"exists":bool,"error":""}`
- `GET /healthz`, `GET /readyz`

**Backend behavior:**
```
exists := (restic snapshots --tag <ns>/<pvc> --json --latest 1 returns non-empty array)
on success:        decision=restore|fresh, authoritative=true
on timeout/error:  decision=unknown, authoritative=false  (fail-closed)
```

**Caching:** 5-minute TTL, 90-second re-warm, singleflight on identical concurrent lookups. Ported verbatim from upstream — mover-agnostic.

**Deployment:** 2 replicas + podAntiAffinity + PDB minAvailable=1. **No NFS volume mount** (the key diff from upstream). Restic creds via `envFrom` of a SOPS-encrypted Secret.

**Build & publish:** image to `ghcr.io/mainertoo/pvc-plumber-restic:<version>`, pinned by digest in the deployment manifest.

### 2.3 Kyverno admission policy — adapted from upstream

**Decision:** Add ClusterPolicy `volsync-pvc-backup-restore` (~200 lines yaml). Port the seven rules from mitchross's policy with restic-specific changes:

1. `require-authoritative-backup-decision` (validate, deny) — calls `/exists`, denies if `authoritative=false` or error present
2. `add-datasource-if-backup-exists` (mutate) — calls `/exists`, patches `spec.dataSourceRef → <pvc>-backup/ReplicationDestination/volsync.backube` when restore
3. `require-datasource-when-restore` (validate, deny, post-mutate) — closes the validate/mutate race
4. `require-skip-restore-reason` (validate, deny) — escape-hatch annotation must carry a non-empty reason
5. `generate-restic-secret` (generate) — creates per-PVC `volsync-<pvc>` Secret with shared `RESTIC_REPOSITORY` + `RESTIC_PASSWORD` + AWS creds, plus `RESTIC_HOSTNAME=<ns>/<pvc>` for tag enforcement on backup
6. `generate-replication-source` (generate) — `<pvc>-backup` RS, schedule from label, retain 24h/7d/4w/3m
7. `generate-replication-destination` (generate) — `<pvc>-backup` RD for restore-on-create

**`failurePolicy: Fail`** on validate webhooks. Required for fail-closed semantics. Risk acknowledged in §6.

**Skip the upstream `volsync-nfs-inject` policy entirely** — restic mover gets credentials via env vars from the per-PVC Secret. No mount injection needed.

**Generate-rule ordering and durability (addresses Codex v1 finding 1):**

The PVC's `dataSourceRef` is patched at admission time to point at `<pvc>-backup` ReplicationDestination, but the RD itself is created by Kyverno generate rule 7 *after* admission completes. The CSI populator polls until the RD exists, so the timing usually works — but two failure modes exist:

1. **Generate rule fires once with `synchronize: false` and silently fails** (transient API-server hiccup, RBAC drift). PVC stays Pending forever with `dataSourceRef` pointing at a never-created RD.
2. **Generate rule fires successfully but the RD references a Secret that doesn't exist yet** (rule 5 raced rule 7).

Mitigations applied:

- **`synchronize: true` on rules 5–7** for per-PVC RS/RD/Secret. Costs background-controller load, but Kyverno reconciles missing objects on parent-PVC update events. mitchross uses `synchronize: false` with an orphan-reaper for the inverse direction; we add forward-reconciliation by flipping the flag.
- **Generate rule 5 (Secret) ordered before rules 6–7 in the policy file.** Kyverno applies generate rules in declaration order within a policy.
- **`volsync-bootstrap` Flux Kustomization (§2.8) reconciles before app Kustomizations.** Apps with backup-labeled PVCs declare `dependsOn: volsync-bootstrap`. On cluster rebuild, no PVC admission happens until pvc-plumber is `Ready`, the master Secret is decryptable, and the shared repo is reachable.
- **Reconciliation watchdog CronJob** (every 15 min): finds backup-labeled PVCs without their generated RS/RD/Secret and patches the PVC's label off-and-on to re-trigger admission. Belt-and-suspenders for the synchronize-true gap.

### 2.4 Schedule scheme

| Label | Cron | Retention |
|---|---|---|
| `backup: "hourly"` | `<minute> * * * *` | 24h, 7d, 4w, 3m |
| `backup: "daily"`  | `<minute> 2 * * *` | 24h, 7d, 4w, 3m |

`<minute>` derived as `length(namespace-name) modulo 60`. Acknowledged temporary spread; revisit at ~50 backup-labeled PVCs.

### 2.5 Excluded namespaces (hardcoded in policy `match.exclude`)

`flux-system`, `kube-system`, `kyverno`, `volsync-system`, `cert-manager`, `traefik`, `ceph-csi-rbd`, `ceph-csi-cephfs`, `monitoring`, `tailscale`, `cloudflared`, `newt`, `intel-gpu`. Adding a `backup:` label to a PVC in any of these namespaces is a no-op.

### 2.6 Skip-restore escape hatch (revised after Codex v1 finding 3)

Annotations on the PVC:
```yaml
volsync.backup/skip-restore: "true"
volsync.backup/skip-restore-reason: "<non-empty text>"
```

**Behavior (diverges from upstream mitchross):**

- Bypasses rules 1–3: no `dataSourceRef` injected, empty PVC binds.
- **Also bypasses rules 5–7: no Secret/RS/RD generated.** PVC has no backups while the annotation is set.
- Two-tier alerting:
  - `ProtectedPVCSkipRestoreFresh` fires at **T+1h** — informational, "your bypass is in effect, no backups are being taken"
  - `ProtectedPVCSkipRestoreStale` fires at T+24h — warning, "remove the annotation or accept that this PVC has been unprotected for a day"

**Rationale for diverging from mitchross:**

The upstream design lets the bypassed-empty PVC continue taking backups into the same `<ns>/<pvc>` lineage. With a 24h alert window, the operator who hits skip-restore as an emergency bypass and forgets can lose recoverable historical snapshots: the new empty-PVC backups age out the good snapshots within `pruneIntervalDays * keep-hourly`. This converts an emergency override into silent data loss.

By suppressing backup generation entirely, the operator must consciously decide when to resume protection. The cost is "no backups during skip period" — but that's *correct* behavior for a deliberate bypass. To resume, the operator either removes the annotation (validation re-runs, oracle re-queried) or runs a manual one-shot RS to start a new lineage.

**Operator workflow to resolve a skip-restore:**
1. Remove `skip-restore: "true"` annotation
2. Kyverno re-evaluates on next admission (only fires on PVC create — see workaround below)
3. Patch the PVC's `backup` label off-and-back-on to re-trigger generation
4. Verify `kubectl get rs,rd,secret -n <ns> -l app.kubernetes.io/managed-by=kyverno` shows the generated trio

### 2.7 Bootstrap secret: SOPS-age stays manual

**Decision:** No external secret store. The age key remains the irreducible cluster-rebuild secret, pasted from the user's password manager.

**Rationale:** Adding an external store (Vault on a VPS, 1Password Connect, etc.) just moves the bootstrap secret one level — ESO's credentials become the new manual paste. Trades one paste for an external dependency.

**Rebuild path:**
```
empty cluster → kubectl create secret -n flux-system sops-age <paste> → flux bootstrap → everything else
```
Adds ~30s to the rebuild. The plan's "10-min restore" budget assumes this paste happens.

### 2.8 Bootstrap readiness gate (addresses Codex v1 finding 4)

**Decision:** A dedicated Flux Kustomization `volsync-bootstrap` reconciles the entire backup/restore admission chain to a verified-healthy state before any backup-labeled app PVC is allowed to admit.

**Composition (`clusters/production/volsync-bootstrap.yaml`):**

```yaml
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: volsync-bootstrap
  namespace: flux-system
spec:
  interval: 10m
  retryInterval: 1m
  timeout: 10m
  wait: true                            # block dependents until healthChecks pass
  prune: false                          # never auto-delete this layer
  path: ./infrastructure/volsync-bootstrap
  dependsOn:
    - { name: infra-controllers }       # Kyverno + VolSync operators must be up first
  healthChecks:
    - { kind: Deployment, name: pvc-plumber, namespace: volsync-system }
    - { kind: Deployment, name: volsync, namespace: volsync-system }
    - { kind: Job, name: volsync-bootstrap-probe, namespace: volsync-system }
```

**`volsync-bootstrap-probe` Job (runs once per Kustomization reconcile):**
1. Decrypts `volsync-shared-base` Secret (proves SOPS chain works)
2. `restic snapshots --no-lock --json --latest 1` against shared repo (proves Garage reachable + repo unlocked)
3. `curl -fsS http://pvc-plumber.volsync-system/readyz` (proves oracle is ready)
4. Exits 0 only on all three; otherwise non-zero, Kustomization fails its health check

**App Kustomizations declare `dependsOn: volsync-bootstrap`** if they have any backup-labeled PVC. On cluster rebuild, Flux serializes the order:
```
infra-controllers (Kyverno, VolSync, snapshot-controller, ceph-csi-*)
   ↓ healthy
volsync-bootstrap (pvc-plumber + probe Job)
   ↓ healthy
apps (each app's own Kustomization, with backup-labeled PVCs)
```

**What this prevents:**

- App PVC admitted before pvc-plumber is reachable → `apiCall.default` fires → admission denied → app stuck Pending until next retry — *but* if the apiCall default isn't well-formed, admission could mistakenly admit
- Master Secret missing or undecryptable when generate rule 5 fires → per-PVC Secret created with empty values → mover Job fails — caught by probe before any PVC admits
- Garage reachable but shared repo locked (e.g. previous restic process didn't release) → all backups fail silently for the schedule interval — caught by probe

**Cost:** ~20–60 sec on cluster rebuild before any app reconciles. Inside the "10-min restore" budget.

---

## 3. Architecture diagram

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Git (source of truth)                                                    │
└─┬────────────────────────────────────────────────────────────────────────┘
  │ flux reconcile
  ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ Cluster                                                                  │
│                                                                          │
│  app PVC.yaml  ← carries label `backup: hourly|daily`                    │
│         │                                                                │
│         ▼                                                                │
│  ┌──────────────────────┐  GET /exists/<ns>/<pvc>  ┌───────────────────┐ │
│  │ Kyverno admission    │ ────────────────────────▶│ pvc-plumber       │ │
│  │ webhook              │ ◀────────────────────────│ (forked, restic)  │ │
│  └─────────┬────────────┘  {decision, authoritative} └─────────┬───────┘ │
│            │                                                  │         │
│            │ generate RS+RD+Secret                            │ restic  │
│            │ inject dataSourceRef if "restore"                │snapshots│
│            ▼                                                  ▼         │
│  ┌──────────────────────┐                          ┌────────────────┐   │
│  │ VolSync (RS+RD)      │ ─── restic Job ────────▶ │ Garage S3      │   │
│  │                      │ ◀── restic restore ───── │ (shared repo,  │   │
│  └──────────────────────┘                          │  tagged        │   │
│                                                    │  <ns>/<pvc>)   │   │
│                                                    └────────────────┘   │
└──────────────────────────────────────────────────────────────────────────┘
```

## 4. Migration plan — moving existing snapshots to the shared repo

Existing per-app restic repos at `s3://volsync/<app>/<path>` carry the only copies of historical backups. They must be merged into `s3://volsync-shared/restic` with `<ns>/<pvc>` tags before the new oracle can find them.

**Mechanism:** `restic copy --from-repo` with pre-applied tags. Preserves history, dedupes during copy, restartable.

### 4.1 Per-app cutover protocol (revised v3 after Codex v2 finding)

**Two-stage migration.** Stage A pre-populates the shared repo while the old repo continues normal backups (no pause). Stage B is the actual cutover: it uses VolSync's native `spec.paused: true` for real quiescence (not an impossible-date schedule), bundles the verify and the authority flip into one transactional operation, and uses a `trap` to ensure rollback runs on every abort path.

```
  ── Stage A (run anytime, idempotent) ───────────────────────────────────
              t0           t1            t2            t3
              │             │             │             │
              ▼             ▼             ▼             ▼
  ┌──────────────┐ ┌──────────┐ ┌──────────┐ ┌────────────────┐
  │ Tag old      │ │ Copy 1   │ │ Verify   │ │ Stage A passes:│
  │ snapshots    │ │ (initial)│ │ ID-set   │ │ shared repo    │
  │ <ns>/<pvc>   │ │          │ │          │ │ caught up      │
  └──────────────┘ └──────────┘ └──────────┘ └────────────────┘

  ── Stage B (atomic; runs as part of Phase-5 PR merge) ──────────────────
              t4              t5             t6             t7
              │               │              │              │
              ▼               ▼              ▼              ▼
  ┌─────────────────┐ ┌──────────┐ ┌─────────────┐ ┌──────────────┐
  │ PAUSE old RS    │ │ Final    │ │ Final       │ │ DELETE old   │
  │ (spec.paused)   │ │ copy 2   │ │ verify      │ │ RS atomically│
  │ + drain         │ │ (catch-  │ │ (ID set)    │ │ + apply      │
  │                 │ │ up)      │ │             │ │ Phase-5 PR   │
  └─────────────────┘ └──────────┘ └─────────────┘ └──────────────┘
                                                          │
                                                          ▼
                                               Kyverno generates new RS
                                               against shared repo
```

#### Stage A: pre-populate shared repo (background-safe, run anytime)

```bash
#!/bin/bash
set -euo pipefail
NS="$1"; PVC="$2"; OLD="$3"; NEW="$4"

# No pause. Old RS continues normal backups. Stage A is idempotent —
# re-run any time to bring the shared repo up to date with the old.

restic -r "$OLD" --password-file /tmp/old-pw \
  tag --add "$NS/$PVC" --tag ""

restic -r "$NEW" --password-file /tmp/new-pw \
  copy --from-repo "$OLD" --from-password-file /tmp/old-pw

OLD_IDS=$(restic -r "$OLD" --password-file /tmp/old-pw \
  snapshots --json | jq -r '.[].short_id' | sort)
NEW_IDS=$(restic -r "$NEW" --password-file /tmp/new-pw \
  snapshots --tag "$NS/$PVC" --json | jq -r '.[].short_id' | sort)
MISSING=$(comm -23 <(echo "$OLD_IDS") <(echo "$NEW_IDS"))
[ -z "$MISSING" ] || { echo "Stage A FAIL: missing in shared repo: $MISSING"; exit 1; }

echo "Stage A OK: $(echo "$OLD_IDS" | wc -l) snapshots present in shared repo"
```

#### Phase 5 split into 5a and 5b with Flux suspension during Stage B (revised v5 after Codex v4 finding)

The original Phase 5 PR did two things at once: added the `backup:` label AND removed the old volsync Component reference. The Component removal causes Flux to prune the old RS, which races Stage B's pause/drain/copy steps. Equally serious: while Stage B operates on the old RS at runtime (pause, drain, copy), Flux could reconcile the still-referenced manifest and revert `spec.paused: true` back to false (since the manifest doesn't specify it), unpausing the old RS mid-drain. Both races are closed by splitting Phase 5 and by suspending Flux reconciliation of the app's Kustomization for the duration of Stage B.

Phase 5 is now two ordered PRs with Stage B between them:

| Step | Action | Effect |
|---|---|---|
| **Phase 5a PR** | Add `backup:` label to PVC. **Keep** old Component reference. | New RS generated by Kyverno (points at shared repo). Old RS continues running (points at per-app repo). Both RS coexist; both back up the same PVC source on their respective schedules. PVC has dual-redundant backups for the migration window. |
| **Stage B** | Run as a one-shot Job after Phase 5a is reconciled and observed healthy. **Suspends Flux reconciliation of the app's Kustomization and leaves it suspended on success.** | Suspend → preflight → pause → drain → final-copy → verify new RS via probe → delete old RS → exit (Flux remains suspended). Old RS cannot be recreated because Flux cannot reconcile while suspended. |
| **Phase 5b PR** | Remove old volsync Component reference. | Merged while Flux is still suspended for this app. No reconcile happens yet. |
| **Stage C** | Run after Phase 5b merge has propagated to flux-source-controller's git state. | Verify 5b is in flux-source-controller's working tree → resume Flux Kustomization → wait for Ready=True → confirm old RS is absent and stays absent. Resume only happens once 5b's removal is what Flux will apply. |

**Why Flux stays suspended through Stage B AND until Stage C:** Three failure modes exist if Flux is allowed to reconcile during the migration window:

1. **Mid-Stage-B unpause.** Stage B sets `spec.paused: true` at runtime. The Phase 5a manifest doesn't specify `spec.paused`. Flux's strong-reconciliation default would patch `paused` back to nil, unpausing the old RS while Stage B is still draining. A backup Job fires against the old repo mid-drain, snapshot lands only there.
2. **Post-delete recreation (Codex v6).** After Stage B `kubectl delete`s the old RS but before Phase 5b is reflected in flux-source-controller's git state, Flux reconciles 5a's manifest (which still references the old Component → old RS) and recreates the old RS. The recreated RS is unpaused, on its original schedule, and fires backups to the old repo until 5b is reconciled. If the cluster fails in this window, the freshest backup exists only in the old repo where the new oracle cannot see it.
3. **Resume-too-early race window.** Even if Stage B resumes Flux at exit, there's a window of seconds-to-minutes where Flux reconciles against 5a's manifest (because 5b isn't merged yet). Same recreation race as #2.

Flux stays suspended from the moment Stage B starts until Stage C confirms Phase 5b is in flux-source-controller's git state and resumes safely. While suspended, *no path* exists for Flux to recreate the old RS or revert any of Stage B's runtime changes. The cost is that other unrelated changes to the app (Renovate image bumps, manual config edits) are also blocked during the suspend window — typically minutes-to-hours, bounded by how fast the operator merges Phase 5b.

**Invocation rule for Stage B:** Triggered after Phase 5a is reconciled. Three preflight gates:
- `require_pvc_has_backup_label` — confirms 5a manifest reconciled
- `require_old_rs_present` — confirms Flux hasn't already pruned old RS via some unrelated path; required before pause/drain has any effect
- Flux Kustomization is `Ready` — confirms suspend will succeed

Manual pre-merge or post-Phase-5b invocation is rejected. Stage B exits with Flux suspended on both success and failure paths (failure path additionally restores `spec.paused: false` via trap).

**Stage C is required to complete migration.** A Prometheus alert `FluxKustomizationSuspendedTooLong` fires after 30 min of suspend to ensure operator runs Stage C and doesn't leave the app in suspended limbo.

```bash
#!/bin/bash
set -euo pipefail
NS="$1"; PVC="$2"; OLD="$3"; NEW="$4"
APP_KUSTOMIZATION="${5:-$PVC}"   # Flux Kustomization name; defaults to PVC name

# State tracking for trap-based rollback. The trap restores spec.paused=false
# AND resumes Flux reconciliation on ANY non-success exit.
STAGE_B_DONE=0
PAUSED=0
FLUX_SUSPENDED=0

cleanup() {
  local rc=$?
  if [ "$STAGE_B_DONE" -eq 1 ]; then return $rc; fi

  # Order matters: un-pause RS first (so when Flux resumes, it sees a
  # running RS, not a paused one being unpaused-by-Flux during its own
  # reconcile). Resume Flux last.
  if [ "$PAUSED" -eq 1 ]; then
    echo "ROLLBACK: un-pausing old RS due to abort (rc=$rc)"
    kubectl -n "$NS" patch rs "$PVC" --type merge \
      -p '{"spec":{"paused":false}}' || \
      echo "ROLLBACK FAILED (paused=false): manual: kubectl -n $NS patch rs $PVC --type merge -p '{\"spec\":{\"paused\":false}}'"
  fi
  if [ "$FLUX_SUSPENDED" -eq 1 ]; then
    echo "ROLLBACK: resuming Flux reconciliation of $APP_KUSTOMIZATION"
    flux resume kustomization "$APP_KUSTOMIZATION" -n flux-system || \
      echo "ROLLBACK FAILED (flux resume): manual: flux resume kustomization $APP_KUSTOMIZATION -n flux-system"
  fi
  return $rc
}
trap cleanup EXIT

# ─── Preflight gates ────────────────────────────────────────────────────

require_zero_active_jobs() {
  local active
  active=$(kubectl -n "$NS" get jobs \
    -l volsync.backube/source-name="$PVC" \
    -o jsonpath='{.items[?(@.status.active>0)].metadata.name}')
  [ -z "$active" ] || { echo "FAIL: active Jobs: $active"; return 2; }
}

require_pvc_has_backup_label() {
  local label
  label=$(kubectl -n "$NS" get pvc "$PVC" \
    -o jsonpath='{.metadata.labels.backup}')
  [[ "$label" == "hourly" || "$label" == "daily" ]] || {
    echo "FAIL: PVC $NS/$PVC does not have backup label (got: '$label')"
    echo "      Phase 5a PR was not merged or not yet reconciled."
    return 3
  }
}

# Codex v5 fix: explicitly verify old RS still exists before we try to
# pause/drain/copy it. Catches the case where Phase 5b merged ahead of
# Stage B, or some unrelated process already deleted the old RS.
require_old_rs_present() {
  if ! kubectl -n "$NS" get rs "$PVC" -o name >/dev/null 2>&1; then
    echo "FAIL: old RS $NS/$PVC does not exist."
    echo "      Either Phase 5b was merged before Stage B (forbidden by protocol),"
    echo "      or another process deleted the RS. Stage B cannot guarantee no-data-loss."
    echo "      Manually inspect the old per-app repo for any snapshots that may have"
    echo "      been created after Stage A's last copy, and run the catch-up copy"
    echo "      manually before continuing."
    return 8
  fi
}

require_flux_kustomization_ready() {
  local ready
  ready=$(kubectl -n flux-system get kustomization "$APP_KUSTOMIZATION" \
    -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
  [[ "$ready" == "True" ]] || {
    echo "FAIL: Flux Kustomization $APP_KUSTOMIZATION is not Ready (got: '$ready')"
    return 9
  }
}

require_pvc_has_backup_label
require_old_rs_present
require_flux_kustomization_ready

# ─── Suspend Flux reconciliation of this app ────────────────────────────
#
# While suspended, Flux will not attempt to reconcile this Kustomization,
# meaning it cannot revert spec.paused or recreate a deleted RS. This is
# the lock that closes Codex v5's "Flux pruning races Stage B" finding.
# The trap restores reconciliation on any exit path.
echo "Suspending Flux reconciliation of $APP_KUSTOMIZATION for Stage B duration"
flux suspend kustomization "$APP_KUSTOMIZATION" -n flux-system
FLUX_SUSPENDED=1

# t4: Use VolSync's native pause. Real quiescence — controller stops creating
# Jobs entirely. spec.paused is a documented VolSync API field
# (kubectl explain replicationsource.spec.paused).
kubectl -n "$NS" patch rs "$PVC" --type merge -p '{"spec":{"paused":true}}'
PAUSED=1

# Drain in-flight Jobs. NO `|| true` — wait failures abort migration,
# trap restores paused=false.
if kubectl -n "$NS" get jobs -l volsync.backube/source-name="$PVC" \
     -o jsonpath='{.items[?(@.status.active>0)].metadata.name}' | grep -q .; then
  kubectl -n "$NS" wait --for=condition=complete --timeout=30m \
    job -l volsync.backube/source-name="$PVC"
fi
require_zero_active_jobs

# t5: Final catch-up copy. Captures any snapshots that landed between Stage A
# and the pause.
restic -r "$NEW" --password-file /tmp/new-pw \
  copy --from-repo "$OLD" --from-password-file /tmp/old-pw

# t6: Final verify by snapshot ID set intersection.
OLD_IDS=$(restic -r "$OLD" --password-file /tmp/old-pw \
  snapshots --json | jq -r '.[].short_id' | sort)
NEW_IDS=$(restic -r "$NEW" --password-file /tmp/new-pw \
  snapshots --tag "$NS/$PVC" --json | jq -r '.[].short_id' | sort)
MISSING=$(comm -23 <(echo "$OLD_IDS") <(echo "$NEW_IDS"))
[ -z "$MISSING" ] || { echo "Stage B FAIL: missing snapshots: $MISSING"; exit 1; }

# ─────────────────────────────────────────────────────────────────────────
# t7: GATE — wait for the NEW RS to exist and prove it works.
#
# This must happen BEFORE deleting the old RS. The old RS is the only
# remaining backup mechanism for this PVC; deleting it before confirming
# the new one is operational creates a "no backups at all" window that
# Codex v4 finding 8 flagged.
#
# At this point Phase 5a is reconciled (verified by preflight), so Kyverno
# admission has fired and generated the new RS/RD/Secret. We confirm all
# three exist and prove the new RS works end-to-end via a probe backup
# before proceeding.
# ─────────────────────────────────────────────────────────────────────────

require_new_rs_healthy() {
  local rs_name="${PVC}-backup"
  local timeout_secs=600
  local start
  start=$(date +%s)

  while true; do
    # Generated trio must exist.
    local rs rd secret
    rs=$(kubectl -n "$NS" get rs "$rs_name" -o jsonpath='{.metadata.name}' 2>/dev/null || true)
    rd=$(kubectl -n "$NS" get replicationdestination "$rs_name" -o jsonpath='{.metadata.name}' 2>/dev/null || true)
    secret=$(kubectl -n "$NS" get secret "volsync-${PVC}" -o jsonpath='{.metadata.name}' 2>/dev/null || true)

    if [ -n "$rs" ] && [ -n "$rd" ] && [ -n "$secret" ]; then
      # New RS must reference the shared repo, not the old per-app repo.
      local repo
      repo=$(kubectl -n "$NS" get rs "$rs_name" \
        -o jsonpath='{.spec.restic.repository}')
      if [ "$repo" != "volsync-${PVC}" ]; then
        echo "FAIL: new RS exists but references unexpected repo Secret: $repo"
        return 4
      fi

      # Trigger an immediate one-shot backup to prove the new RS works
      # end-to-end (mover Job runs, restic auths against shared repo,
      # snapshot lands with correct tag).
      kubectl -n "$NS" patch rs "$rs_name" --type merge \
        -p "{\"spec\":{\"trigger\":{\"manual\":\"cutover-probe-$(date +%s)\"}}}"

      # Wait for the probe Job to succeed (5 min budget).
      kubectl -n "$NS" wait --for=condition=complete --timeout=5m \
        job -l volsync.backube/source-name="$rs_name" || {
          echo "FAIL: new RS probe backup did not complete within 5min"
          return 5
        }

      # Confirm the probe snapshot landed in the shared repo with our tag.
      local probe_count
      probe_count=$(restic -r "$NEW" --password-file /tmp/new-pw \
        snapshots --tag "$NS/$PVC" --host "$NS/$PVC" --json \
        | jq '[.[] | select(.time > "'$(date -u -v-5M +%Y-%m-%dT%H:%M:%SZ)'")] | length')
      if [ "${probe_count:-0}" -lt 1 ]; then
        echo "FAIL: probe backup completed but no recent snapshot found in shared repo"
        return 6
      fi

      echo "Gate passed: new RS healthy, probe snapshot landed in shared repo"
      return 0
    fi

    # Timeout check.
    local now elapsed
    now=$(date +%s)
    elapsed=$((now - start))
    if [ "$elapsed" -gt "$timeout_secs" ]; then
      echo "FAIL: new RS/RD/Secret did not all appear within ${timeout_secs}s"
      echo "  rs=$rs rd=$rd secret=$secret"
      return 7
    fi

    sleep 5
  done
}

require_new_rs_healthy

# t8: NOW delete the old RS. New RS has been observed healthy AND has
# successfully backed up to the shared repo; if delete fails or the new
# RS subsequently degrades, at worst we have one redundant RS pointing
# at an unused repo until manual cleanup. Never "no backups."
#
# Note: we do NOT set STAGE_B_DONE=1 yet. If the delete fails or the
# new RS regresses before the delete completes, the trap should still
# fire to un-pause the old RS as a fail-safe. (Flux stays suspended
# on the success path, so the trap's flux-resume branch only fires on
# real abort, not normal completion.)
kubectl -n "$NS" delete rs "$PVC" --wait=true

# Success — disable the rollback trap WITHOUT resuming Flux.
# Stage B intentionally exits with Flux suspended; Stage C is required
# to resume after Phase 5b is merged. This closes Codex v6's
# "post-delete recreation race" because Flux cannot reconcile while
# suspended — no path for the old RS to be recreated and start
# writing snapshots to the old repo only.
STAGE_B_DONE=1
echo "Stage B OK: $NS/$PVC migrated."
echo "  Old RS deleted. New RS verified backing up to shared repo."
echo "  Flux Kustomization $APP_KUSTOMIZATION REMAINS SUSPENDED."
echo ""
echo "NEXT STEPS (must complete to finalize migration):"
echo "  1. Merge Phase 5b PR (removes old Component reference)"
echo "  2. Wait for flux-source-controller to fetch the merged commit:"
echo "       flux reconcile source git flux-system"
echo "  3. Run Stage C to resume Flux:"
echo "       stage-c-cutover.sh $NS $PVC $APP_KUSTOMIZATION"
echo ""
echo "Prometheus alert FluxKustomizationSuspendedTooLong will fire if"
echo "this Kustomization stays suspended >30min."
```

#### Stage C: post-merge resume (run after Phase 5b is merged AND fetched)

```bash
#!/bin/bash
set -euo pipefail
NS="$1"; PVC="$2"; APP_KUSTOMIZATION="${3:-$PVC}"

# Stage C verifies that Phase 5b is in flux-source-controller's view of
# git, then resumes Flux. Resuming with 5b in place means Flux's first
# reconcile sees: old Component absent from manifest, old RS absent from
# cluster — no diff for the old RS, no recreation. The recreate race is
# eliminated.

# Verify Phase 5b is reflected in flux-source-controller's git state.
#
# Codex v7 finding 11 fix: grep for the Component path string was wrong
# because that string lives in kustomization.yaml's `components:` list,
# not in rendered manifest output. Instead, check the rendered output
# for the OLD RS resource itself — that resource IS in rendered output
# when 5a (with old Component) is the source, and is NOT when 5b is.
#
# The old RS pattern: ReplicationSource named "$PVC" (no -backup suffix,
# the volsync-v2 Component naming).
# The new RS pattern: ReplicationSource named "$PVC-backup" (Kyverno
# generate rule output, mitchross naming).

LATEST_COMMIT=$(kubectl -n flux-system get gitrepository flux-system \
  -o jsonpath='{.status.artifact.revision}')
echo "flux-source-controller has commit: $LATEST_COMMIT"

# Codex v8 finding 13 fix: do NOT pass --path. flux build uses the
# Kustomization's configured spec.path (e.g. ./apps/production/<app>)
# when --path is omitted. Passing --path . would render the repo root,
# which would not contain the app's RS regardless of 5a vs 5b state —
# the gate would pass for unrelated reasons.
KUSTOMIZATION_PATH=$(kubectl -n flux-system get kustomization "$APP_KUSTOMIZATION" \
  -o jsonpath='{.spec.path}')
echo "Building Kustomization $APP_KUSTOMIZATION at its configured path: $KUSTOMIZATION_PATH"

RENDERED=$(flux build kustomization "$APP_KUSTOMIZATION" -n flux-system)

# Codex v9 finding 14 fix: positive guard must be tied to the specific
# PVC being migrated, not a generic "anything in $NS" check. Apps in
# shared namespaces (e.g. multiple apps in `media`) would render
# different HelmReleases — the wrong-app HelmRelease would satisfy a
# generic guard, then the negative-old-RS check would pass for
# unrelated reasons.
#
# Tighter guard: require the PVC named $PVC in $NS to be in rendered
# output AND carry the backup: label (proves Phase 5a label is in
# source, which is a precondition for Phase 5b being valid).
PVC_RENDERED=$(echo "$RENDERED" \
  | yq eval-all '. | select(.kind == "PersistentVolumeClaim" and .metadata.name == "'"$PVC"'" and .metadata.namespace == "'"$NS"'")' -)

if [ -z "$PVC_RENDERED" ]; then
  echo "FAIL: rendered manifest contains no PVC $NS/$PVC."
  echo "      The render is empty, points at the wrong source, or APP_KUSTOMIZATION is wrong."
  echo "      Refusing to resume Flux. Investigate:"
  echo "        flux build kustomization $APP_KUSTOMIZATION -n flux-system"
  echo "        (configured spec.path: $KUSTOMIZATION_PATH)"
  exit 13
fi

PVC_BACKUP_LABEL=$(echo "$PVC_RENDERED" | yq '.metadata.labels.backup // ""' -)
if [[ "$PVC_BACKUP_LABEL" != "hourly" && "$PVC_BACKUP_LABEL" != "daily" ]]; then
  echo "FAIL: rendered PVC $NS/$PVC does not have backup label (got: '$PVC_BACKUP_LABEL')."
  echo "      Phase 5a may not have been correctly applied to git, or APP_KUSTOMIZATION"
  echo "      is rendering the wrong app. Refusing to resume Flux."
  exit 14
fi
echo "Positive guard passed: rendered PVC $NS/$PVC present with backup=$PVC_BACKUP_LABEL"

# Negative guard: check for the OLD RS resource in rendered output.
# If present, 5b is not in source yet; the volsync-v2 Component is
# still rendering its RS.
if echo "$RENDERED" \
   | yq eval-all '. | select(.kind == "ReplicationSource" and .metadata.name == "'"$PVC"'" and .metadata.namespace == "'"$NS"'") | .metadata.name' - \
   | grep -q .; then
  echo "FAIL: Phase 5b not yet in flux-source-controller's git state."
  echo "      Rendered manifest still contains old ReplicationSource $NS/$PVC."
  echo "      Wait for flux-source-controller to fetch the merged commit:"
  echo "        flux reconcile source git flux-system"
  echo "      then retry Stage C."
  exit 10
fi

echo "Phase 5b confirmed: rendered manifest has no ReplicationSource $NS/$PVC."

# Verify old RS is still absent from cluster (sanity check — Stage B
# should have deleted it, and no path can have recreated it while
# suspended).
if kubectl -n "$NS" get rs "$PVC" -o name >/dev/null 2>&1; then
  echo "FAIL: old RS $NS/$PVC unexpectedly exists. Investigate before resuming Flux."
  echo "      Stage B may not have completed cleanly, or someone manually recreated it."
  exit 11
fi

# Resume Flux. Flux will reconcile the now-current 5b manifest. With 5b
# in place, the manifest doesn't reference the old Component, so the old
# RS is not expected and not recreated. New RS continues running unaffected.
flux resume kustomization "$APP_KUSTOMIZATION" -n flux-system

# Wait for the first post-resume reconcile to complete and confirm Ready.
flux reconcile kustomization "$APP_KUSTOMIZATION" -n flux-system --with-source

# Final sanity: old RS still absent after Flux's first reconcile against 5b.
if kubectl -n "$NS" get rs "$PVC" -o name >/dev/null 2>&1; then
  echo "FAIL: old RS reappeared after Flux resume. This should be impossible."
  echo "      5b may not have been correctly merged. Investigate the manifest."
  exit 12
fi

echo "Stage C OK: $NS/$PVC migration complete."
echo "  Flux resumed, Phase 5b reconciled, old RS confirmed absent."
echo "  New RS in shared repo is now the only authority for $NS/$PVC backups."
```

#### What this fixes vs. v2

1. **No more impossible-date pause.** `spec.paused: true` is a documented VolSync field that prevents the controller from creating Jobs at all. A manual `--type=merge` patch to the trigger or a separate operator action cannot bypass it the way an impossible-date schedule can be unpaused by a one-line patch.

2. **`trap cleanup EXIT`** runs the un-pause rollback on every abort path, not just verify-failure. Any `set -e` exit, a `return 2` from a helper, an external signal — all trigger the trap.

3. **Old RS is *deleted* in Stage B's final step**, not just paused. After deletion, no Job can be created against the old repo by any mechanism. The "point-in-time check vs. real quiescence" gap that Codex v2 flagged is closed because the resource that would create future Jobs doesn't exist anymore.

4. **Stage A is no-op-on-old-repo.** No pause, no schedule change, just tag + copy + verify. Old backups continue undisturbed during Stage A. This means Stage A can run far in advance (days before merging the Phase-5 PR), reducing the Stage B window to the minimum.

5. **Stage B is bundled with the Phase-5 cutover.** The Phase-5 PR's CI/post-merge hook runs Stage B. Authority flip is one operation: old RS deleted, new RS generated by Kyverno on next admission of the relabeled PVC. No "verified consistent now, but maybe not later" gap — the moment verification passes is the moment the old RS goes away.

#### Failure modes after v6

| Failure | Result |
|---|---|
| Stage A fails (verify mismatch) | Old repo unmodified. Investigate, re-run Stage A. No data loss. |
| Stage B invoked before Phase 5a merge | `require_pvc_has_backup_label` fails (return 3) at preflight, before suspend. No state change. |
| Stage B invoked after Phase 5b already merged | `require_old_rs_present` fails (return 8) at preflight, before suspend. No state change; manual recovery instructions printed. |
| Flux Kustomization not Ready at preflight | `require_flux_kustomization_ready` fails (return 9) at preflight, before suspend. No state change. |
| Stage B fails after suspend, before pause | trap fires, Flux reconciliation resumed. No data loss. |
| Stage B fails after pause, before new-RS gate | trap fires, paused=false restored, Flux resumed. Old RS continues normal backups on next reconcile. No data loss. |
| Stage B fails at new-RS gate | trap fires, paused=false restored, Flux resumed. Old RS still authoritative; operator investigates, retries Stage B. No data loss. |
| Stage B fails AT delete (gate passed, kubectl delete errored) | trap fires, paused=false attempted, Flux resumed. Two RS may briefly coexist. Redundant, harmless. Manual cleanup. |
| Stage B succeeds; operator forgets Phase 5b merge | Flux remains suspended for this app. New RS continues backing up to shared repo. After 30 min, `FluxKustomizationSuspendedTooLong` Prometheus alert fires. Other unrelated changes to the app (Renovate, manual edits) blocked until operator merges 5b and runs Stage C. **No data-loss window** — old RS is gone, can't be recreated while suspended. |
| Stage B succeeds; cluster fails before Phase 5b merge | **NOT REBUILD-SAFE — see §4.2 runbook.** Stage B's suspend is runtime-only state, not durable in git. On rebuild, Flux reconciles git at Phase 5a, which still references the old Component → recreates old RS. New RS also gets generated (Kyverno). Both RS run, both back up; backups duplicate, NOT lost. But snapshots taken by recreated old RS land in old per-app repo only — invisible to oracle on a *subsequent* restore until 5b is merged and reconciled. Operator runbook: merge 5b promptly post-rebuild, run Stage C. |
| Stage C run before Phase 5b is in flux-source-controller's git | Negative gate: `flux build` shows old `ReplicationSource $PVC` still rendered; return 10. No state change; operator waits for source fetch and retries. |
| Stage C: rendered manifest empty or wrong path | Positive guard: no PVC `$NS/$PVC` found in render; return 13 BEFORE any gate or resume. Flux stays suspended. No state change. |
| Stage C: APP_KUSTOMIZATION points at wrong app in shared namespace | Positive guard fails because target PVC isn't in render (different app's PVCs are); return 13. Flux stays suspended. No state change. |
| Stage C: rendered PVC missing backup label | Phase 5a was not in git or rendered output is for wrong source; return 14. Flux stays suspended. No state change. |
| Stage C run, but old RS unexpectedly exists in cluster | Return 11 before resume; operator investigates. Flux stays suspended. No state change. |
| Stage C: Flux reconcile after resume recreates old RS | Should be impossible if 5b is in source AND positive guard passed. Return 12; operator inspects manifest. |
| Trap fails during Stage B abort | Explicit recovery commands printed. Self-healing on next operator action. |
| Probe backup tag check passes but snapshot is corrupt | Out of scope for cutover — caught by Phase 6 DR drill. |

### 4.2 Rebuild-during-migration hazard (Codex v7 finding 12)

**Problem.** Stage B's `flux suspend` is runtime cluster state, not durable in git. If the cluster fails between Stage B success and Phase 5b being committed to git, the rebuilt cluster reconciles Phase 5a (with the old Component reference still present), recreates the old RS, and starts taking backups to the old per-app repo again. The new RS also runs, so backups are duplicated rather than lost — but the old-repo snapshots are invisible to the new oracle until Phase 5b is merged and reconciled.

This is the **only window in the migration where a cluster rebuild is not single-step GitOps-safe.** The window is bounded by how fast the operator merges Phase 5b after Stage B completes.

**Mitigations:**

1. **Minimize the window.** Merge Phase 5b within minutes of Stage B success. The Phase 5b PR is a tiny change (remove one Component reference); pre-author it, get pre-approval, merge immediately after Stage B.

2. **Per-app migration, not bulk.** Migrate one app at a time. The hazard window only applies to the single app currently in flight. Other apps are either pre-migration (still on per-app repos, fully GitOps-safe) or post-migration (label-driven, fully GitOps-safe).

3. **Don't migrate during planned cluster maintenance.** If you have any reason to expect a cluster restart in the next hour (kernel update, hardware swap, disk pressure), don't start Stage B. Wait for a quiet window.

**Runbook: cluster rebuild detected during migration window**

If the cluster fails between Stage B success and Phase 5b merge, after the rebuild reconciles git at Phase 5a:

```
# 1. Inspect cluster state — both RS should now exist (recreated old + new).
kubectl -n "$NS" get rs

# 2. Pause the recreated old RS to stop further old-repo-only backups.
kubectl -n "$NS" patch rs "$PVC" --type merge -p '{"spec":{"paused":true}}'

# 3. Merge Phase 5b PR immediately.

# 4. Wait for source-controller to fetch:
flux reconcile source git flux-system

# 5. Run Stage C (which now sees no recreate-window because rebuild forced
#    Flux's reconcile state to be derived from git anyway — runtime suspension
#    irrelevant after rebuild).
./stage-c-cutover.sh "$NS" "$PVC" "$APP_KUSTOMIZATION"

# 6. If the recreated old RS managed to take any backups before step 2:
#    run a final restic copy from old per-app repo to shared, repeat
#    Stage A's verify step.
restic -r "$NEW" --password-file /tmp/new-pw \
  copy --from-repo "$OLD" --from-password-file /tmp/old-pw

# 7. Confirm shared repo has all old-repo snapshots tagged correctly:
restic -r "$NEW" --password-file /tmp/new-pw \
  snapshots --tag "$NS/$PVC" --json | jq 'length'
```

**Acceptance.** This hazard is acknowledged but accepted as a transitional cost of the migration. Post-migration steady state has no equivalent risk: every PVC is purely label-driven, no Component references, full single-PR GitOps recovery (§3 architecture).

### 4.3 Driver script

A bash wrapper iterates apps from `apps/production/*/kustomization.yaml`, generates one Job per app from a template, waits for completion. Apps are processed sequentially (not parallel) to avoid concurrent restic repo locks on the new shared repo.

**Old per-app repos retained as cold archive** until after Phase 6 (Enforce mode + DR drill verifies new shared repo restores cleanly). Then aged out per their existing retention.

---

## 5. Phase ordering

| Phase | Output | Effort | Reversible? |
|---|---|---|---|
| 0 | This doc, after adversarial review | done | yes |
| 1 | New Garage bucket, `restic init`, `volsync-shared-base` SOPS Secret | 0.5 day | yes (delete bucket) |
| 2 | Forked pvc-plumber image + deployment in `volsync-system` | 1 day | yes (no other component depends yet) |
| 3 | Snapshot migration via `restic copy --tag` Jobs | 1 day per ~10 apps | yes (old repos untouched) |
| 4 | Kyverno policies in **Audit** mode | 0.5 day | yes (no enforcement yet) |
| 5 | Per-app PRs migrating to label-driven pattern | ~30 min/app | yes per app |
| 6 | Flip to **Enforce** + DR drill on staging cluster | 0.5 day | yes (revert PR) |

Each phase leaves the cluster in a working state. No phase combines unrelated changes.

---

## 6. Risk register

| Risk | Severity | Mitigation |
|---|---|---|
| Kyverno crash with `failurePolicy: Fail` deadlocks all backup-labeled PVC creates cluster-wide | **High** | Pre-stage `scripts/emergency-webhook-cleanup.sh` (deletes ValidatingWebhookConfiguration); webhook namespaceSelector excludes infra namespaces; 2 replicas of admission controller |
| pvc-plumber unavailable → admission denies all backup-labeled PVC creates | Medium | 2 replicas + podAntiAffinity + PDB minAvailable=1. Flux retries every `retryInterval: 2m`. Same fail-closed semantics as mitchross. |
| Single restic password leaked = full blast radius | Medium | Password lives in password manager only; SOPS-encrypted in repo; rotation requires re-encrypting the repo (`restic key passwd`) — accepted homelab tradeoff |
| Migration Job tags wrong PVC due to typo | Low | Per-app verify step; restic tag --add is non-destructive (appends, doesn't replace) |
| `RESTIC_HOSTNAME` change creates parallel snapshot lineage on existing apps | Medium | Acceptable — old lineage ages out by retention, new lineage takes over within 30 days |
| Cluster cleanup work in flight collides with backup work | Medium | App migrations bundled into cleanup PRs (every cleanup PR also onboards the app to label-driven pattern) |
| `restic copy` from N old repos fails partway, leaves shared repo half-populated | Low | restic copy is restartable; verify step catches partial state; old repos not deleted until Phase 6 |
| Kyverno generate rule fires once and silently fails (transient API error, RBAC drift) → PVC stuck Pending forever | **High → Medium** | `synchronize: true` on rules 5–7 (§2.3); reconciliation watchdog CronJob every 15 min; `volsync-bootstrap` health check ensures Kyverno itself is healthy before apps reconcile |
| Per-app Secret generated AFTER RS/RD references it, mover Job runs against missing Secret | Medium | Generate rule 5 (Secret) ordered before rules 6–7 in policy file declaration order; bootstrap probe Job verifies decryption chain works before apps reconcile |
| `length(ns) % 60` schedule clusters → many PVCs back up at the same minute | Low | Acknowledged; revisit at ~50 backup-labeled PVCs with sha256-derived minute or controller-driven schedule |
| pvc-plumber 5-min cache returns stale `fresh` for a PVC that just got its first backup | Low | First-backup race has zero impact: PVC was already created with `fresh`, has no `dataSourceRef`, an empty volume is correct |
| Re-creating a PVC during the 5-min cache window after a snapshot is pruned could miss the prune and inject a stale `dataSourceRef` | Medium | RD references `<pvc>-backup` RD which queries restic at population time; if no snapshots exist when populator runs, RD fails and PVC stays Pending — not silently wrong |
| Skip-restore bypass + continued backups age out recoverable historical snapshots before operator notices | **Was High, now Low** | §2.6 revised: skip-restore now suppresses Secret/RS/RD generation entirely. Bypassed PVC takes no backups. T+1h informational alert + T+24h warning |
| Migration window: backup runs on old repo mid-copy, snapshot stranded outside shared repo | **Was High, now Low** | §4.1 revised: pause old RS first, two-pass copy, verify by snapshot-ID set intersection (not count), explicit rollback path on failure |
| Bootstrap order: app PVC admits before pvc-plumber/oracle is ready, hits `apiCall.default` and denies cluster-wide | **Was High, now Low** | §2.8: `volsync-bootstrap` Flux Kustomization with health checks gates apps via `dependsOn`. Probe Job verifies decryption + Garage + oracle before any app reconciles |

---

## 7. Out of scope (deliberately)

- Continuous data protection / RPO < 1h
- Application-consistent backups for stateful apps needing quiescing (databases — not handled here; CNPG/Postgres separate)
- Multi-cluster federation
- Backup verification automation (no scheduled "restore-and-diff" job; relies on operator drill on staging)
- Encryption-at-rest on Garage backend (relies on restic encryption alone)
- Off-site copy (3-2-1 compliance) — single Garage instance, no remote replication. Acknowledged limitation. Future: rclone to B2 or second Garage cluster.

---

## 8. Open questions for review

These are the design points still least confident about after Codex v1. The v2 review should attack these specifically:

1. **Is shared-repo + tags actually simpler than per-PVC repos with deterministic paths?** Codex didn't push back on this in v1; still want a second opinion on whether there's a third option (e.g. shared repo with per-namespace sub-keys for partial blast-radius isolation).
2. **`synchronize: true` on Kyverno generate rules at this scale** — what's the actual API-server load cost in a homelab with ~30 backup-labeled PVCs? The mitchross design avoided it for a reason; is the watchdog CronJob a sufficient alternative?
3. **`volsync-bootstrap` probe Job runs at every Flux reconcile** (every 10 min) — does the restic snapshot list call against the shared repo create operational noise (Garage logs, restic lock contention)? Should the probe be lazier?
4. **Skip-restore now suppresses backups** — does this surface a race during the operator's "remove annotation, label off-and-on" workflow? If admission re-fires while the master Secret is rotating, does the PVC end up in a worse state than before?
5. **Migration cutover protocol's "pause old RS"** uses an impossible-date schedule. Is there a cleaner pause mechanism (e.g. `spec.paused: true` on the RS — does VolSync support this)?
6. **Migration verify-by-snapshot-ID-set** — does `restic copy` ever create a new snapshot ID for a copied snapshot (e.g. due to re-encryption with the new repo's master key)? If yes, the ID-set comparison would fail spuriously.
7. **Cache TTL of 5 minutes** — under "PVC deleted, recreated, restored, deleted again", could the oracle return stale `restore` after a snapshot is pruned? §6 claims the populator catches this; is that actually true?
8. **The age key is the irreducible bootstrap secret** — paper backup in a safe vs. password-manager-only? Codex didn't push on this in v1.

---

## 9. What this doc is asking for

A second opinion on §2 (decisions), §4 (migration), §6 (risks), and the §8 open questions. Specifically:

- Are any §2 decisions still wrong after the v2 revisions?
- Does the §4.1 cutover protocol still have a data-loss window I haven't identified?
- Did the §10 changes introduce new risks not yet in §6?
- Are there §6 risks I should treat as showstoppers that I'm still treating as accepted?

Style/naming/grammar feedback explicitly out of scope.

---

## 10. Codex adversarial review v1 — addressed findings

The v1 review (run via `/codex:adversarial-review --wait --scope working-tree` with focus "fail-closed admission, secret bootstrap, snapshot migration races") returned `needs-attention` with four findings. All four are addressed in v2:

| # | Finding | Where addressed |
|---|---|---|
| 1 (high) | Fail-closed admission can deadlock initial restore before generated RD exists | §2.3 (generate-rule ordering, `synchronize: true`, watchdog CronJob); §2.8 (bootstrap readiness gate) |
| 2 (high) | Migration not quiesced; backups during copy can be lost | §4.1 (pause old RS, two-pass copy, verify by snapshot-ID set intersection, explicit rollback) |
| 3 (medium) | Skip-restore + fresh backups can age out recoverable snapshots | §2.6 revised: skip-restore suppresses Secret/RS/RD generation; T+1h alert |
| 4 (medium) | Bootstrap restores cluster but not necessarily oracle-readiness for fail-closed admission | §2.8: `volsync-bootstrap` Flux Kustomization with health-checked probe Job, app Kustomizations declare `dependsOn` |

### v2 review (after applying v1 fixes)

The v2 review returned `needs-attention` with one finding:

| # | Finding | Initial fix | Held up to v3? |
|---|---|---|---|
| 5 (high) | Cutover script `kubectl wait \|\| true` masks timeouts; old-repo backup completing after copy can be lost | Removed `\|\| true`, added hard gate, `set -e` on errors | Partially — see v3 finding 6 |

### v3 review (after applying v2 fix)

The v3 review returned `needs-attention` with two new high findings. Both addressed by restructuring §4.1 from a one-stage script into two stages:

| # | Finding | Where addressed |
|---|---|---|
| 6 (high) | `set -euo pipefail` + `exit` in helper bypasses rollback block; old RS stays paused on most abort paths | §4.1 Stage B: `trap cleanup EXIT` registered immediately after pause succeeds. Trap fires on any non-success exit (set -e, helper return, signal). Rollback runs uniformly. |
| 7 (high) | Final point-in-time `require_zero_active_jobs` cannot prevent new Jobs after the check; the impossible-date schedule pause can be unpaused by a one-line patch | §4.1 Stage B: replaced impossible-date hack with `spec.paused: true` (documented VolSync API); old RS is **deleted** in Stage B's final step rather than left paused — eliminates the gap entirely because no resource exists to create future Jobs. Stage A and Stage B split so the deletion is bundled with verify in one transactional operation. |

### v4 review (after applying v3 fix)

The v4 review returned `needs-attention` with one finding:

| # | Finding | Where addressed |
|---|---|---|
| 8 (high) | Stage B deletes old RS before proving the new RS exists; "manual pre-merge invocation" path explicitly accepted a no-backups gap | §4.1 Stage B: invocation rule narrowed to "post-merge hook ONLY"; manual pre-merge path removed. New gate `require_new_rs_healthy` runs before delete: confirms RS/RD/Secret exist, verifies new RS references shared repo, triggers a manual probe backup, waits for it to complete, confirms snapshot landed in shared repo with correct tag. Old RS deletion only happens after gate passes. Failure-mode table updated to reflect: even mid-cutover failure leaves at least one RS authoritative for backups. |

### v5 review (after applying v4 fix)

The v5 review returned `needs-attention` with one finding:

| # | Finding | Where addressed |
|---|---|---|
| 9 (high) | "Post-merge hook" invocation races Flux pruning of the old RS: when Phase 5 merge removes the Component reference, Flux can prune old RS *before* Stage B reaches its first kubectl patch — bypassing pause/drain/copy entirely | §4.1: Phase 5 split into 5a (label add, keep old Component) and 5b (Component removal). Stage B now `flux suspend kustomization`s the app for its entire duration, blocking both Flux's mid-Stage-B unpause-revert and post-delete recreation. New `require_old_rs_present` preflight catches the case where 5b somehow merged ahead. Trap restores both paused=false and Flux reconciliation on every abort path. |

### v6 review (after applying v5 fix)

The v6 review returned `needs-attention` with one finding:

| # | Finding | Where addressed |
|---|---|---|
| 10 (high) | Resuming Flux at Stage B exit while Phase 5a still references old Component lets Flux recreate the old RS in a window before Phase 5b merges; recreated RS could write snapshots to the old repo only, bypassing the new oracle | §4.1: Stage B no longer resumes Flux at exit on the success path. Suspend persists through Phase 5b merge. New **Stage C** runs after Phase 5b is reflected in flux-source-controller's git state: verifies the old Component reference is gone from the rendered manifest, verifies old RS is absent, then resumes Flux. With 5b in place at resume time, Flux's first reconcile sees no old-RS expectation and no recreation occurs. **No window exists where Flux can recreate the old RS** *during normal operation*. Cost: app's Kustomization stays suspended between Stage B and Stage C — bounded by `FluxKustomizationSuspendedTooLong` alert at 30 min. (See v7 finding 12 for the cluster-rebuild edge case.) |

### v7 review (after applying v6 fix)

The v7 review returned `needs-attention` with two findings:

| # | Finding | Where addressed |
|---|---|---|
| 11 (high) | Stage C's gate grepped rendered output for the Component path string `components/volsync-v2`, but Component paths live in kustomization.yaml's `components:` list — they don't appear in rendered manifests. The grep would never match and Stage C would always pass the gate. | §4.1 Stage C: replaced grep with `flux build` + yq query for the **OLD `ReplicationSource` resource** named `$PVC` in rendered output. That resource IS present in 5a's rendered manifest (volsync-v2 Component produces it) and IS NOT in 5b's. Deterministic gate. |
| 12 (medium) | Stage B's `flux suspend` is runtime-only cluster state, not durable in git. Cluster rebuild between Stage B and Phase 5b merge would reconcile git at Phase 5a, recreate the old RS, and write backups to the old per-app repo only — invisible to the new oracle on subsequent restores. | New §4.2 runbook acknowledges this is the **only non-rebuild-safe window** in the migration. Mitigations: minimize the window (merge 5b promptly), migrate per-app not bulk, don't migrate during planned maintenance. Recovery runbook documents the manual catch-up if a rebuild happens mid-window. Failure-mode table updated to reflect honest characterization of the hazard. |

### v8 review (after applying v7 fix)

The v8 review returned `needs-attention` with one finding:

| # | Finding | Where addressed |
|---|---|---|
| 13 (high) | Stage C ran `flux build kustomization ... --path .` which overrides the Kustomization's actual `spec.path`. Rendered output was the repo root, not the app's manifest. Old-RS-absent check would pass for unrelated reasons. | §4.1 Stage C: removed the `--path .` override; flux build now uses the Kustomization's configured `spec.path`. Added explicit lookup of `spec.path` for logging. Added a positive guard for an app-owned resource. |

### v9 review (after applying v8 fix)

The v9 review returned `needs-attention` with one finding:

| # | Finding | Where addressed |
|---|---|---|
| 14 (high) | Positive guard ("HelmRelease in `$NS`") is too generic. Apps sharing a namespace (e.g. `media` namespace contains plex, jellyfin, tracearr, etc.) all render HelmReleases that satisfy the guard. Wrong `APP_KUSTOMIZATION` argument or shared-namespace render would pass the positive guard, then pass the negative old-RS check for unrelated reasons (different app's render naturally lacks `ReplicationSource $PVC`). | §4.1 Stage C: replaced HelmRelease guard with a **PVC-specific** invariant: rendered output must contain `PersistentVolumeClaim` named exactly `$PVC` in `$NS` AND that PVC must carry the `backup:` label. The PVC name is unique per migration target; the label proves Phase 5a is reflected in source. Two new return codes (13, 14) for the failure paths. Failure-mode table updated. |

### What v3 did NOT change (acknowledged but accepted):

- Single shared restic password — homelab tradeoff, password lives in password manager, rotation cost accepted
- `length(ns) % 60` schedule clustering — flagged as future work at ~50 PVCs
- No off-site copy / 3-2-1 compliance — out of scope, single Garage instance accepted
- 2-replica pvc-plumber on a small cluster — both pods could land on same node briefly, accepted "best effort, will eventually succeed"
