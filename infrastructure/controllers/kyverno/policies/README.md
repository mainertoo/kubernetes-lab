# Kyverno policies

ClusterPolicies reconciled by the `kyverno-policies` Flux Kustomization. Today
they all serve one job: **label-driven VolSync (Kopia) backups** for app PVCs.

For the app-author's how-to ("I just want my PVC backed up"), see
[`docs/label-driven-backups.md`](../../../../docs/label-driven-backups.md).
This README is the operator's view of how the policies fit together.

## Files

| File | Kind | Purpose |
|---|---|---|
| `volsync-pvc-backup-restore-kopia.yaml` | ClusterPolicy | The core policy — validates, mutates, and **generates** the per-PVC VolSync trio |
| `volsync-pvc-engine-required.yaml` | ClusterPolicy | Deny a PVC that has `backup:` but no `backup-engine: kopia` |
| `kyverno-volsync-jitter.yaml` | ClusterPolicy | Mutate: add a random `sleep` initContainer to mover Jobs so backups don't thunder |
| `volsync-policy-rbac.yaml` | RBAC | Lets Kyverno's background controller create VolSync resources + Secrets |
| `kustomization.yaml` | — | Lists the above |

## How the core policy works

A PVC labelled `backup: daily|hourly` + `backup-engine: kopia` triggers
`volsync-pvc-backup-restore-kopia`, which has three kinds of rule:

- **mutate** — on a fresh PVC, injects `dataSourceRef` pointing at the app's
  `ReplicationDestination` so the PVC is auto-restored from the last backup
  (unless `volsync.backup/skip-restore: "true"`).
- **generate** (3 rules, `synchronize: true`, `generateExisting: true`) — emits
  the per-PVC `Secret/volsync-<pvc>`, `ReplicationSource/<pvc>-backup`, and
  `ReplicationDestination/<pvc>-backup`. Removing the labels GCs them.
- **validate** — `validationFailureAction: Audit` (reports only, never blocks).

Generated values come from `context` jmesPath variables, e.g.:

| Variable | Drives |
|---|---|
| `scheduleMinute` / `scheduleHour` | RS cron — `length(ns) % 60` minute, hour `2` (daily) or `*` (hourly) |
| `snapClass` | `cephfs-snapclass` vs `ceph-rbd-snapclass` |
| `pitStorageClass` / `pitAccessMode` | The point-in-time copy: cephfs → `cephfs-backingsnapshot` + `ReadOnlyMany` (shallow, zero-copy); rbd → its own class + `ReadWriteOnce` |
| `cacheCap` | Mover cache size, `volsync.backup/cache-capacity` annotation or `5Gi` |

All apps share one Kopia repo (Garage bucket `volsync-kopia`); per-app identity
is `<pvc>-backup@<ns>:/data`. Shared creds: `Secret/flux-system/volsync-kopia-shared-base`.

## ⚠️ Editing the core policy — the generate-rule trap

Kyverno **rejects in-place edits to a generate rule's `data` block**. To change
anything under `generate.data`, Flux must delete+recreate the whole policy —
add `kustomize.toolkit.fluxcd.io/force: "Enabled"` to the policy's annotations
for that change, then remove it afterward.

A force-recreate **cold-starts every generated child** — all ~70 RS + RD
regenerate, firing a fleet-wide wave of backup/restore movers. Before doing it:

1. Widen `kyverno-volsync-jitter.yaml` (e.g. `RANDOM % 1800`) so the wave spreads
   over ~30 min; revert afterward.
2. Keep the circuit breaker ready: `kubectl -n volsync-system scale deploy/volsync-system-volsync --replicas=0`.
3. Watch for a known race — the policy-delete cleanup can fire *after*
   `generateExisting` recreates the children and delete them again. If the RS
   count doesn't return to normal, re-trigger generation by annotating the
   backup PVCs (`kubectl annotate pvc … regen=$(date +%s) --overwrite`).

History: the cephfs `backingSnapshot` rollout (Phase 2, 2026-05-22) is the
worked example of all of the above.

## Related

- [`docs/label-driven-backups.md`](../../../../docs/label-driven-backups.md) — app-author how-to
- [`docs/backup-system-wiki.md`](../../../../docs/backup-system-wiki.md) — full backup-system reference (Layer 2)
- `infrastructure/controllers/storage/cephfs/storageclass-cephfs-backingsnapshot.yaml` — the shallow-snapshot StorageClass
