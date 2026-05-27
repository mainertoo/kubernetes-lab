Jellyfin VolSync Restore Playbook

Applies to VolSync + Restic + Ceph (CephFS source → RBD restore PVC)

🔁 Overview

Restore flow:

Scale app down

Patch ReplicationDestination with restoreAsOf

Delete restore PVC (if present)

Reconcile restore kustomization

Verify restored PVC contents

Rsync restore → live PVC

Scale app up

Cleanup restore artifacts

0. Pre-Checks
List snapshots
kubectl -n media logs -f job/jellyfin-restic-list


Or manually:

restic snapshots


Identify the desired timestamp (example):

2026-02-15T13:00:10Z

1. Scale Application Down
kubectl -n media scale deploy/jellyfin --replicas=0


Confirm pod terminated:

kubectl -n media get pods | grep jellyfin

2. Patch Restore Target
kubectl -n media patch replicationdestination jellyfin-restore --type=merge -p '{
  "spec": {
    "trigger": { "manual": "restore-asof-2026-02-15T13:00:10Z" },
    "restic":  { "restoreAsOf": "2026-02-15T13:00:10Z" }
  }
}'

3. Remove Existing Restore PVC (if present)
kubectl -n media delete pvc jellyfin-restore --ignore-not-found


If stuck terminating:

kubectl -n media patch pvc jellyfin-restore --type=merge -p '{"metadata":{"finalizers":[]}}'

4. Reconcile Restore Kustomization (Flux)
flux -n flux-system reconcile kustomization jellyfin-restore --with-source


Watch for restore PVC:

kubectl -n media get pvc jellyfin-restore -w


Wait until:

STATUS: Bound

5. Verify Restored Data Before Overwriting Live PVC

Create inspection pod:

kubectl -n media apply -f jf-restore-peek.yaml
kubectl -n media logs jf-restore-peek


Confirm:

jellyfin.db

jellyfin.db-wal

reasonable disk usage (not 600K empty restore)

Delete inspection pod:

kubectl -n media delete pod jf-restore-peek

6. Rsync Restore → Live PVC

Apply rsync job:

kubectl -n media apply -f jellyfin-restore-rsync.yaml


Wait for completion:

kubectl -n media wait --for=condition=complete job/jellyfin-restore-rsync --timeout=2h


Confirm:

kubectl -n media logs job/jellyfin-restore-rsync


Look for:

Done.

7. Scale App Back Up
kubectl -n media scale deploy/jellyfin --replicas=1


Watch logs:

kubectl -n media logs -f deploy/jellyfin


Verify:

Libraries visible

Media plays

No DB migration errors

8. Cleanup Restore Artifacts

Delete rsync job:

kubectl -n media delete job jellyfin-restore-rsync --ignore-not-found


Delete restore objects:

kubectl -n media delete replicationdestination jellyfin-restore --ignore-not-found
kubectl -n media delete pvc jellyfin-restore --ignore-not-found


Remove restore kustomization from Flux parent (optional best practice).

🧹 If Restic Locks Occur

Symptoms:

forget fails

snapshot count balloons

circuit breaker open

Fix:

kubectl -n media patch replicationsource jellyfin --type=merge -p '{"spec":{"paused":true}}'


Run unlock:

kubectl -n media apply -f jellyfin-restic-unlock.yaml
kubectl -n media logs -f job/jellyfin-restic-unlock


Then resume:

kubectl -n media patch replicationsource jellyfin --type=merge -p '{"spec":{"paused":false}}'


Force test sync:

kubectl -n media patch replicationsource jellyfin --type=merge -p '{"spec":{"trigger":{"manual":"post-unlock-test"}}}'

🔒 Recommended Operational Pattern

Keep restore kustomization out of Flux by default.

Only include it when needed:

clusters/<cluster>/apps/media/kustomization.yaml


Temporarily add:

- ../../../apps/base/media/jellyfin/restore


Then remove after successful restore.

This prevents:

restore PVC churn

stuck populator pods

Rancher noise

accidental overwrites

📌 Snapshot Retention (Expected Behavior)

Current policy:

Hourly: 24

Daily: 7

Weekly: 5

Monthly: 3

If snapshot count balloons:
→ almost always restic lock failure during forget

✅ End State Verification

Healthy ReplicationSource should show:

Result: Successful
Waiting for next scheduled synchronization


And no:

volsync-src-jellyfin pods in Error
