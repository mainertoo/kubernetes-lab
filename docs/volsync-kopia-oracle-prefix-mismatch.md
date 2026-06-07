# VolSync Kopia — Restore-Oracle / Mover S3-Prefix Mismatch

> **Investigation + fix plan.** Captured 2026-06-04 so the audit below does not
> have to be re-derived. Companion to `docs/volsync-kopia-transition.md` (the
> original transition plan) and `docs/label-driven-backups.md` (operator guide).
> Related memory: `project_pvc_plumber_oracle_prefix_mismatch`.

---

## ✅ RESOLVED — 2026-06-07 (Option B, fresh start at root)

The fix landed via the **realign-movers-to-root** path (doc §7 Option B, chosen
over the 182 GB migration in Option A and the rejected oracle-fork in Option C —
which §4/§2 prove impossible: pvc-plumber v3.1.0 re-runs `kopia repository
connect` with no `--prefix` on every startup, so a seeded/forked prefix can't
survive).

**What changed:**

- `KOPIA_REPOSITORY` dropped its path segment so the mover's `entry.sh` extracts
  **no prefix** → movers now write to the **bucket root** where the oracle reads:
  - `infrastructure/controllers/kyverno/policies/volsync-pvc-backup-restore-kopia.yaml`
    (per-PVC generated mover Secret): `s3://garage.lab.mainertoo.com/volsync-kopia`
    → `s3://garage.lab.mainertoo.com`.
  - `infrastructure/controllers/volsync/app/volsync-kopia-maintenance-secret.sops.yaml`
    (`KOPIA_REPOSITORY`): same drop, so `KopiaMaintenance` GC-runs the root repo.
  - Bucket (`volsync-kopia`) + endpoint are unchanged — they come from the
    explicit `KOPIA_S3_BUCKET` / `KOPIA_S3_ENDPOINT` vars, not the URI path.
- The **shared-base** secret (`infrastructure/secrets-prod/volsync-kopia-shared-base.sops.yaml`)
  needed no change — it carries only `KOPIA_PASSWORD` + AWS creds, never
  `KOPIA_REPOSITORY`.

**Consequences / state after the fix:**

- The existing **182 GB prefixed repo is a frozen, manual-restore-only archive**
  (Option B keeps it rather than moving objects). Restore from it manually by
  pointing a `ReplicationDestination` at prefix `volsync-kopia/` (§6 reproducer).
- Each app re-seeds the root repo on its next backup; backups were triggered
  post-cutover to close the auto-restore gap quickly rather than waiting for the
  scheduled run.
- Validated end-to-end with the §8 scratch harness: backup lands at root, the
  oracle returns `exists:true / decision:restore`, and a recreated PVC
  auto-restores. Real-app oracle checks (`/exists/<ns>/<pvc>`) now return
  `exists:true`.

The investigation/audit below is retained as the historical record of how the
break was found and why this path was chosen.

---

## TL;DR / Status

- **Label-driven auto-restore is broken fleet-wide.** Backups are **fine**; data
  is **safe**. Only *auto*-restore-on-PVC-recreate is affected.
- **This defeats the system's headline design goal — the 10-minute
  cluster-rebuild restore** (`docs/volsync-kopia-transition.md` line 70: *"The
  10-min cluster-rebuild target. Restore path is RD → CSI populator"*). The
  entire label-backup + oracle + populator chain exists so a bare-metal rebuild
  re-hydrates every app's PVC automatically. **Today that chain is severed at the
  oracle** — a rebuild would bring every labeled app up **empty**, and each PVC
  would need a manual `ReplicationDestination` against the prefix instead. The
  data survives; the *automatic* 10-minute DR does not.
- **Root cause:** the VolSync movers write the real repo under S3 **prefix
  `volsync-kopia/`**, while the restore oracle (`pvc-plumber`) reads the **bucket
  root**. Two separate Kopia repos in the same bucket; they never meet.
- **This is drift from the documented design, not a design flaw.** The Kopia
  transition deliberately chose a *dedicated bucket, data at the root, no prefix*.
  The oracle is configured correctly for that intent. The **movers** drifted onto
  a prefix because `KOPIA_REPOSITORY`'s path segment gets parsed into a prefix by
  perfectra1n's `entry.sh`.
- **Correct fix direction:** realign the **movers** back to the bucket root (the
  documented layout) and migrate the existing ~180 GB up from the prefix —
  **NOT** teach the oracle a prefix (that would cement the drift against the
  design, and mitchross `pvc-plumber:3.1.0` has no prefix support anyway).
- **Next action:** run the **scratch end-to-end restore test** in §8 first (the
  validation the transition plan deferred — "theory until proven", line 737). It
  confirms the break non-destructively and becomes the harness that proves any
  fix works. **Do not change production until that test runs.**
- **Not urgent.** Manual restore works today (point a `ReplicationDestination` at
  the prefix). Take the time to do this carefully.

---

## 1. The two repos — verified live state (2026-06-04)

Bucket `volsync-kopia` on Garage (`garage.lab.mainertoo.com`, HTTPS via Traefik):

| Repo location | Contents | Size | Sources | Who reads/writes it |
|---|---|---|---|---|
| **Bucket root** (no prefix) | ~10 | 18.6 KB | `kopia-init@cluster-bootstrap`, `scratch-pvc-backup@kopia-validation` (Phase 1b manual-init + scratch-validation artifacts) | **Oracle** (`pvc-plumber`) + the Phase 1b manual init |
| **Prefix `volsync-kopia/`** | 991,293 | **180.8 GB** | ~70 real app sources `<pvc>-backup@<ns>:/data` (cinesync→zilean, all live apps) | **All VolSync movers** + `KopiaMaintenance` |

Both the movers *and* maintenance operate on the prefixed repo (maintenance GC
reports "991237 in-use contents (180.3 GB)"), so backups and repo upkeep are
healthy. The oracle is simply looking somewhere else.

---

## 2. Root cause

**Movers** (`ghcr.io/perfectra1n/volsync`, `entry.sh`): the per-PVC Secret (and
the shared `volsync-kopia-shared-base` / `volsync-kopia-maintenance` secrets)
carry `KOPIA_REPOSITORY=s3://garage.lab.mainertoo.com/volsync-kopia`. `entry.sh`
(~lines 1476–1490) **extracts the path component as an S3 prefix**:

```
# Extract prefix from KOPIA_REPOSITORY (e.g., s3://bucket/prefix -> prefix)
if [[ "${KOPIA_REPOSITORY}" =~ s3://[^/]+/(.+) ]]; then S3_PREFIX="${BASH_REMATCH[1]}"
# ...adds a trailing slash...
S3_CONNECT_CMD+=(--prefix="${S3_PREFIX}")     # --> --prefix="volsync-kopia/"
```

Combined with the separately-set `KOPIA_S3_BUCKET=volsync-kopia`, the mover
connects to **bucket `volsync-kopia`, prefix `volsync-kopia/`**.

**Oracle** (`ghcr.io/mitchross/pvc-plumber:3.1.0`,
`infrastructure/controllers/pvc-plumber/deployment.yaml`): env sets
`KOPIA_S3_BUCKET=volsync-kopia` but has **no `KOPIA_REPOSITORY` and no prefix
var**; its secret `pvc-plumber-kopia` holds only `KOPIA_PASSWORD` + AWS keys.
mitchross 3.1.0 shells out `kopia repository connect s3 --endpoint --bucket
--access-key --secret-access-key --password [--disable-tls]` — **no `--prefix`
flag exists, and no prefix env var is read** (verified against its README; its
reference env is in-cluster RustFS with the repo at bucket root). So the oracle
connects to **bucket `volsync-kopia` at the root**.

→ Oracle and movers are on **different repos**.

---

## 3. Evidence it is actually broken (not theoretical)

1. **Oracle returns "no backup" for apps that demonstrably have backups.**
   `/exists/<ns>/<pvc>` returns `{"exists":false,"decision":"fresh",
   "authoritative":true}` for **every** app tested — including known-good
   `home-assistant/home-assistant`, `actual-budget/actual-budget`, `media/plex`,
   `dumb/dumb` — all of which have real snapshots in the prefixed repo.
2. **The oracle's own logs:** `"snapshot scan complete","unique_sources":1` — it
   sees only the single root scratch source.
3. **Kyverno restore is gated on that answer.** Rule 2 of
   `volsync-pvc-backup-restore-kopia` only injects `dataSourceRef` when
   `decision == restore`. The oracle never returns `restore`, so a recreated PVC
   **binds empty** — no auto-restore.
4. **Backups themselves are fine** — `ReplicationSource` status shows recent
   successful syncs; 180 GB lives in the prefixed repo.

---

## 4. Why the fix is "realign movers", not "teach the oracle a prefix"

The documented **design intent was a dedicated bucket with data at the root, no
prefix** — the oracle matches that intent; the movers drifted away from it:

- `docs/volsync-kopia-transition.md` **line 82**: the repo moved from Restic's
  `s3://…/volsync-shared/restic` (bucket **+ prefix**) to
  `s3://garage.lab.mainertoo.com/volsync-kopia` described as a **"new dedicated
  bucket."** A dedicated bucket was chosen precisely to *avoid* a prefix.
- **Phase 1b** explicitly weighed **Option A** (new bucket — chosen, "cleaner
  Phase 6 decom") vs **Option B** (reuse a bucket *with prefix `/kopia`* —
  rejected).
- The oracle's **Phase 2 secret schema** is bucket-only (no repository path) —
  i.e. **designed for the root**, on purpose.
- The plan **anticipated this exact failure class**: §3a audit item (line 595)
  said to "confirm the mover doesn't create a new repo instead of connecting to
  the existing one… verify no 'creating new repository' in the logs," and the
  risk register (line 690) flagged "two oracles diverging in `/exists`
  semantics."
- The plan **acknowledged restore was never proven**: line 737 — *"This runbook
  is theory until proven on this cluster"* — and the Phase 7 end-to-end DR drill
  was deferred. The §595 check would have passed against the Phase 1b/3e
  artifacts (which are at the **root**), and the production movers landing on the
  **prefix** was never caught.

Teaching the oracle a prefix would (a) require a fork of `pvc-plumber:3.1.0`
(no prefix support) or the bigger v4 migration (v4 removes the `/exists` oracle
our Kyverno policy depends on — see `project_pvc_plumber_v4_planned_upgrade`),
and (b) **enshrine the drift** against the documented root layout. So the
design-aligned fix is to move the movers + data back to the root.

---

## 5. Data safety (no emergency — but a real DR gap, not "low priority")

The data is safe and nothing is actively degrading, so there is no need to make a
rushed change. But the **disaster-recovery posture is currently compromised** —
the 10-minute automated rebuild does not work — so this is a real gap to close
deliberately, not an optional nicety.

- **180 GB is intact** under the prefix and fully restorable manually (create a
  `ReplicationDestination` pointed at prefix `volsync-kopia/`).
- **Retention will not age it out:** per-RS `retain` is intentionally unset; the
  repo's global policy is `keepLatest 10 / hourly 48 / daily 7 / weekly 4 /
  monthly 24 / annual 3`. `keepLatest=10` pins a dead source's last snapshots
  indefinitely; nothing auto-prunes (`docs/label-driven-backups.md`: snapshots
  "stay until manually pruned").
- **Only auto-restore-on-recreate is affected.** Live backups and manual restore
  are unaffected.

---

## 6. How to re-inspect the repos (reproducer)

Read-only; spins up a transient pod with the maintenance creds. **The trailing
slash on `--prefix` is mandatory** — without it kopia looks for
`volsync-kopiakopia.repository` and reports "repository not initialized."

```bash
# 1) transient inspect pod (perfectra1n image has the kopia CLI + entry env)
kubectl --context production -n volsync-system run kopia-inspect \
  --image=ghcr.io/perfectra1n/volsync:v0.17.11 --restart=Never \
  --overrides='{"spec":{"containers":[{"name":"kopia-inspect","image":"ghcr.io/perfectra1n/volsync:v0.17.11","command":["sleep","1800"],"envFrom":[{"secretRef":{"name":"volsync-kopia-maintenance"}}],"volumeMounts":[{"name":"cache","mountPath":"/cache"}]}],"volumes":[{"name":"cache","emptyDir":{}}],"restartPolicy":"Never"}}'
kubectl --context production -n volsync-system wait --for=condition=Ready pod/kopia-inspect --timeout=90s

# 2) connect to the REAL (prefixed) repo and list sources  --> 180 GB, ~70 sources
kubectl --context production -n volsync-system exec kopia-inspect -- sh -c '
  kopia repository connect s3 --bucket="$KOPIA_S3_BUCKET" --endpoint="$KOPIA_S3_ENDPOINT" \
    --prefix="volsync-kopia/" --access-key="$AWS_ACCESS_KEY_ID" \
    --secret-access-key="$AWS_SECRET_ACCESS_KEY" --region="$AWS_REGION" \
    --password="$KOPIA_PASSWORD" --override-username=maintenance --override-hostname=cluster
  kopia content stats
  kopia snapshot list --all | grep -oE "[a-zA-Z0-9._-]+@[a-zA-Z0-9._-]+:/[^ ]*" | sort -u'

# 3) connect WITHOUT --prefix to see the (near-empty) ROOT repo the oracle reads
#    (same command, drop the --prefix line)  --> ~10 contents, only scratch sources

# 4) ask the oracle directly (curl image pod), expect exists:false for real apps
kubectl --context production -n volsync-system run oracle-check --image=curlimages/curl:8.11.1 --restart=Never --command -- sleep 300
kubectl --context production -n volsync-system exec oracle-check -- \
  curl -s http://pvc-plumber.volsync-system.svc.cluster.local:80/exists/media/plex

# cleanup
kubectl --context production -n volsync-system delete pod kopia-inspect oracle-check --ignore-not-found
```

---

## 7. Fix options

| Option | Touches | Verdict |
|---|---|---|
| **A. Realign movers to root + migrate 180 GB** — change the mover-side `KOPIA_REPOSITORY` so no prefix is extracted (e.g. drop the path so `entry.sh` adds no `--prefix`), and server-side-move the existing objects from `…/volsync-kopia/volsync-kopia/*` up to `…/volsync-kopia/*`. | Garage data + `volsync-kopia-shared-base` secret + **Kyverno generate rule** | **Recommended (design-aligned).** Preserves history, ends with one repo at the root matching the oracle + docs. Costs: backup/maintenance quiesce window for the move; **and** changing the Kyverno-generated `KOPIA_REPOSITORY` is write-once-immutable → needs force delete+recreate of the generate rule → **fleet thundering-herd risk** (cold-starts every child RS at once — widen jitter first; see `feedback_kyverno_generate_rules_immutable`, `feedback_force_recreate_fleet_blast_radius`). |
| **B. Fresh start at root** — point movers at the root now; leave the 180 GB prefixed repo as a manual-only archive. | secret + Kyverno rule | Cheapest data-wise (no move), but ~24 h with no auto-restorable snapshot at root, and pre-cutover history becomes manual-only. Still hits the Kyverno force-recreate. |
| **C. Teach the oracle the prefix** (init-container config-seed, or fork 3.1.0 to add `--prefix`). | pvc-plumber Deployment only | **Rejected.** No data move and avoids the Kyverno churn, but cements the drift against the documented root layout, and 3.1.0 can't take a prefix natively (fork burden until the v4 migration). Keep only as a stopgap if A/B are deferred. |

Decision still open. Recommendation: **A**, gated behind the §8 test.

---

## 8. Test plan — run this FIRST (non-destructive, scratch data)

This is the deferred line-737 end-to-end restore validation. It proves the break
on throwaway data and becomes the harness that proves the fix later.

1. **Scratch namespace** `kopia-restore-test` (NOT one of the policy-excluded
   namespaces).
2. **Scratch PVC** `restore-probe`: labels `backup: daily` + `backup-engine:
   kopia`, `storageClassName: ceph-rbd`, small (e.g. 1Gi).
3. Confirm Kyverno generated the trio: `Secret/volsync-restore-probe`,
   `ReplicationSource/restore-probe-backup`, `ReplicationDestination/restore-probe-backup`.
4. **Write known data** into the PVC via a throwaway pod (e.g. a sentinel file
   with a timestamp/UUID).
5. **Trigger a manual backup** (patch the RS `trigger.manual`); confirm the
   snapshot lands in the prefixed repo (§6 inspect → source
   `restore-probe-backup@kopia-restore-test:/data`).
6. **Ask the oracle:** `curl …/exists/kopia-restore-test/restore-probe`.
   - **Expected today (broken):** `exists:false, decision:fresh` — even though the
     snapshot demonstrably exists. This is the break, shown end-to-end.
7. **Delete** the pod + PVC. **Recreate** the PVC (same name + labels).
8. **Observe:** does Kyverno inject `dataSourceRef` and repopulate the sentinel?
   - **Expected today (broken):** no `dataSourceRef`, PVC binds **empty** → sentinel
     gone.
   - **After the fix:** oracle returns `exists:true`, recreate auto-restores the
     sentinel → fix validated.
9. **Cleanup:** delete the `kopia-restore-test` namespace (prunes the trio); then
   prune the scratch source from the repo if desired
   (`kopia snapshot delete restore-probe-backup@kopia-restore-test … --delete`).

---

## 9. Follow-up doc correction

`docs/label-driven-backups.md` troubleshooting ("Backup runs but the oracle says
exists=false") attributes `authoritative:true, exists:false` to *"no snapshots
for that source identity yet."* That guidance **masks this bug** — it sends you
to check the RS (which is healthy) and never to the prefix/root mismatch. Update
it to list the prefix/root mismatch as a cause when the fix lands.

---

## 10. Key references

- This investigation: **2026-06-04**. Verified live against context `production`.
- `docs/volsync-kopia-transition.md` — line 82 (dedicated bucket), Phase 1b
  (Option A vs B), §595 / line 690 (anticipated failure), line 737 ("theory
  until proven").
- `infrastructure/controllers/pvc-plumber/deployment.yaml` — oracle env (bucket,
  no prefix).
- `infrastructure/controllers/kyverno/policies/volsync-pvc-backup-restore-kopia.yaml`
  — generates the per-PVC Secret/RS/RD; rule 2 gates restore on the oracle.
- `infrastructure/controllers/volsync/app/volsync-kopia-maintenance.yaml` — the
  `KopiaMaintenance` CR (operates on the prefixed repo).
- Memory: `project_pvc_plumber_oracle_prefix_mismatch`.
