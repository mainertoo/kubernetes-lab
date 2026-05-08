# Database migrations

One-shot migration manifests for moving each app's existing postgres data into
its new CNPG-managed cluster. **Flux does not reconcile this directory** — these
are run manually with `kubectl apply` during a controlled migration window.

Each file is named `<app>-cnpg-YYYY-MM-DD.yaml` for traceability.

## Pattern (per app)

```
1. PR #A: deploy CNPG Cluster <APP>-db alongside existing postgres   ← additive, zero risk
   ─ wait for cluster Healthy + WAL streaming + first base backup ──
2. Stop the app:    kubectl -n <ns> scale deploy <app> --replicas=0
3. Run migration:   kubectl apply -f docs/migrations/<app>-cnpg-DATE.yaml
4. Watch:           kubectl -n <ns> logs -f job/<app>-cnpg-migration
5. Verify counts in the Job log (last [5/5] step matches earlier [3/5])
6. PR #B (cutover): update <app>-release.yaml so DATABASE_URL/POSTGRES_HOST etc.
                    point at <APP>-db-rw and credentials read from <APP>-db-app
7. Flux applies, app pod restarts, scales back up
8. Verify in app UI that data is intact
9. ── 24 h soak ──
10. PR #C (decommission): remove old postgres Deployment + raw-PVC volsync RS
```

## Why two-step (cluster, then migration)

- Cluster bootstrapping touches CNPG operator + S3 + storage — verify in isolation
- Data migration touches user data — separate to make rollback easy
- The Job is intentionally NOT in GitOps because it's a one-shot operation;
  re-applying it after success would no-op (the `psql --single-transaction` would
  fail on existing tables) and that's fine

## Before running each Job

- Confirm the destination CNPG cluster is healthy:
  `kubectl -n <ns> get cluster.postgresql.cnpg.io <APP>-db -o jsonpath='{.status.phase}'`
  → `Cluster in healthy state`
- Confirm the auto-generated Secret exists:
  `kubectl -n <ns> get secret <APP>-db-app -o jsonpath='{.data.username}' | base64 -d`
- Quiesce writes by scaling the app to 0:
  `kubectl -n <ns> scale deploy <app> --replicas=0`
  (or `statefulset` / sub-deployments if app has multiple components writing to DB)

## After successful Job

The Job auto-deletes after 1 hour (`ttlSecondsAfterFinished: 3600`). To delete sooner:
```
kubectl delete job/<app>-cnpg-migration -n <ns>
```

Logs are kept until the Job is deleted. To check counts after auto-delete, query the
new DB directly:
```
kubectl -n <ns> exec <APP>-db-1 -c postgres -- \
  psql -U postgres -d <db> -c "SELECT count(*) FROM <some_table>"
```

## Migration order (project-wide)

Per `project_cnpg_migration` memory: zilean → riven → opencut → sparky-fitness →
**joplin (test)** → wiki-js → dawarich → authentik (last). Joplin tests the
pattern; once it soaks 24 h cleanly, the rest follow with minor per-app variation.
