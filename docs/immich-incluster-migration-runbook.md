# Immich In-Cluster Migration Runbook (Strategy A′) — Codex-hardened

**Supersedes Phase 1 of `docs/immich-cutover-runbook.md`** (QNAS in-place migration — dead; QNAP is ARM64, can't run VectorChord/jemalloc; plan §2). Incorporates the 2026-06-03 Codex adversarial review (blockers B1–B3, highs H1–H7, mediums M1–M8).

## Premise
- **QNAP stays on pgvecto.rs forever** and keeps serving photos throughout.
- `vectors`→`vchord` conversion happens **in-cluster** (x86/4 KB). **Immich performs the conversion** at startup — we run it against a dual-extension scratch DB, then hand the converted DB to CNPG.
- CNPG `immich-cnpg-db` (`cloudnative-vectorchord:17.5-0.4.2`, **PG17**, vchord 0.4.2) is deployed, healthy, empty. It ships **vchord only** (no `vectors.so`) — hence the scratch step.
- pgvecto.rs dump on the NAS at `/QNAS/data/photos/library/backups/`, reachable via NFS (cluster RW to that tree already **proven** by the 2026-06-03 write test → M6/Q8 closed).
- **Scratch DB image: `ghcr.io/immich-app/postgres:17-vectorchord0.4.2-pgvectors0.3.0`** (verified on ghcr) — **PG17** (same major as CNPG, no cross-major final restore), vchord **0.4.2** (matches CNPG), pgvecto.rs **0.3.0** (loads the 0.2.0 dump; within Immich's `VECTORS_VERSION_RANGE >=0.2 <0.4`).

> **GitOps note (Codex M1):** the scratch resources below are **deliberate, user-approved one-off throwaway objects** in a `immich-migrate` namespace, created via `kubectl apply` and deleted at the end. They are NOT desired state and are intentionally kept out of Flux. The repo's "never kubectl apply" rule is waived **only** for this throwaway namespace, with user sign-off. `kubectl diff` first where practical.

---

## ✅ Rehearsal PASSED — 2026-06-03
Full M1–M3 run on the real 2026-06-03 dump (32,575 assets / 31,593 faces / 1,346 people / 261 albums / 11 users):
- **M1** restore pgvecto.rs dump → scratch PG17: ~3 min, clean.
- **M2** Immich (API-worker only) converted `vectors`→`vchord`: **~15 s** (`Reindexed clip_index`+`face_index`, `Dropping pgvecto.rs extension`), **row counts identical before/after** (zero destructive deltas — Codex B1 refuted), **no ML service needed** (H1 refuted). Note: the emptyDir upload makes Immich abort on its folder-integrity check (`ENOENT .../encoded-video/.immich`) **after** the conversion finishes — harmless (DB already converted); the real cutover mounts the real NFS (which has the `.immich` markers) so it won't hit this.
- **M3** vchord-native dump → CNPG restore: counts matched exactly, indexes `USING vchordrq`, tables owned by `immich`, app role can read.
- **Bugs caught & fixed:** CNPG missing `pg_trgm`/`unaccent`/`uuid-ossp` (now in `db-cnpg.yaml`); CNPG `/tmp` read-only (use `/var/lib/postgresql/data`); TOC filter must also drop `COMMENT ON EXTENSION`.

The end-to-end path is proven. The real cutover repeats M1–M3 with a fresh dump.

## Phase M0 — Rehearse, then do it for real
Run **M1–M3 as a rehearsal first** with the existing 2026-06-03 dump, restoring the converted DB into a **disposable** CNPG/PG17 target (NOT the real `immich-cnpg-db`), and prove a throwaway Immich boots clean + **row counts are unchanged** by the conversion (B1). **Record wall-clock per phase** (H6) to size the cutover window. Only then do the **real** run with a **fresh** dump at cutover.

---

## Phase M1 — Scratch postgres + restore the pgvecto.rs dump

```bash
kubectl create namespace immich-migrate
# Parameterize the dump name (Codex M4) — set to the file you'll use:
DUMP=immich-prevchord-20260603.dump
```

```yaml
# /tmp/immich-scratch.yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: scratch-pg, namespace: immich-migrate}
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: ceph-rbd
  resources: {requests: {storage: 5Gi}}
---
apiVersion: v1
kind: Pod
metadata: {name: scratch-pg, namespace: immich-migrate, labels: {app: scratch-pg}}
spec:
  containers:
    - name: postgres
      image: ghcr.io/immich-app/postgres:17-vectorchord0.4.2-pgvectors0.3.0
      env:
        - {name: POSTGRES_PASSWORD, value: migrate}
        - {name: POSTGRES_USER, value: postgres}
        - {name: POSTGRES_DB, value: immich}
        - {name: PGDATA, value: /var/lib/postgresql/data/pgdata}
      volumeMounts:
        - {name: data, mountPath: /var/lib/postgresql/data}
        - {name: dump, mountPath: /dump, readOnly: true}
  volumes:
    - {name: data, persistentVolumeClaim: {claimName: scratch-pg}}
    - {name: dump, nfs: {server: 192.168.1.252, path: /QNAS/data/photos/library/backups}}
---
apiVersion: v1
kind: Service
metadata: {name: scratch-pg, namespace: immich-migrate}
spec: {selector: {app: scratch-pg}, ports: [{port: 5432, targetPort: 5432}]}
```
```bash
kubectl apply -f /tmp/immich-scratch.yaml
kubectl -n immich-migrate wait --for=condition=ready pod/scratch-pg --timeout=180s

# Verify the scratch image preloads BOTH extensions (Codex M5)
kubectl -n immich-migrate exec scratch-pg -- psql -U postgres -d immich -tAc \
  "show shared_preload_libraries;"     # expect vchord.so (+ vectors during migration)
kubectl -n immich-migrate exec scratch-pg -- psql -U postgres -d immich -tAc \
  "select name, default_version from pg_available_extensions where name in ('vectors','vchord','vector') order by 1;"

# Restore the pgvecto.rs dump (atomic; Codex B3)
kubectl -n immich-migrate exec scratch-pg -- bash -lc "
  pg_restore -U postgres -d immich --single-transaction --exit-on-error \
    --clean --if-exists --no-owner /dump/$DUMP 2>&1 | tail -20
  psql -U postgres -d immich -tAc \"select extname,extversion from pg_extension order by 1;\""
```
**Expect:** `vectors` present, restore clean. **Discover the real table names now** (Codex M3 — Immich v2 uses singular names) for the invariant check:
```bash
kubectl -n immich-migrate exec scratch-pg -- psql -U postgres -d immich -c "\dt" | grep -iE 'asset|user|person|face|smart|album'
# capture BEFORE counts (adjust names to match \dt output):
kubectl -n immich-migrate exec scratch-pg -- psql -U postgres -d immich -Atc "
  select 'asset',count(*) from asset
  union all select 'person',count(*) from person
  union all select 'face_search',count(*) from face_search
  union all select 'smart_search',count(*) from smart_search;"
```

## Phase M2 — Throwaway Immich converts vectors → vchord (API-worker only)

Redis as its **own** Deployment (Codex M2); Immich runs **API worker only** so no destructive microservices/jobs fire against the empty upload dir (Codex **B1/H1**):
```yaml
# /tmp/immich-scratch-app.yaml
apiVersion: apps/v1
kind: Deployment
metadata: {name: scratch-redis, namespace: immich-migrate}
spec:
  replicas: 1
  selector: {matchLabels: {app: scratch-redis}}
  template:
    metadata: {labels: {app: scratch-redis}}
    spec:
      containers: [{name: redis, image: docker.io/valkey/valkey:8-bookworm, ports: [{containerPort: 6379}]}]
---
apiVersion: v1
kind: Service
metadata: {name: scratch-redis, namespace: immich-migrate}
spec: {selector: {app: scratch-redis}, ports: [{port: 6379, targetPort: 6379}]}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: scratch-immich, namespace: immich-migrate}
spec:
  replicas: 1
  selector: {matchLabels: {app: scratch-immich}}
  template:
    metadata: {labels: {app: scratch-immich}}
    spec:
      containers:
        - name: server
          image: ghcr.io/immich-app/immich-server:v2.7.5
          env:
            - {name: IMMICH_WORKERS_INCLUDE, value: api}   # <-- ONLY the api worker (Codex B1)
            - {name: DB_HOSTNAME, value: scratch-pg}
            - {name: DB_USERNAME, value: postgres}
            - {name: DB_PASSWORD, value: migrate}
            - {name: DB_DATABASE_NAME, value: immich}
            - {name: DB_VECTOR_EXTENSION, value: vectorchord}
            - {name: REDIS_HOSTNAME, value: scratch-redis}
          volumeMounts: [{name: upload, mountPath: /usr/src/app/upload}]
      volumes: [{name: upload, emptyDir: {}}]
```
```bash
kubectl apply -f /tmp/immich-scratch-app.yaml
kubectl -n immich-migrate logs -f deploy/scratch-immich -c server
```
**Completion gate (Codex B2) — ALL must pass before dumping:**
```bash
# 1) both reindexes finished (not just started)
kubectl -n immich-migrate logs deploy/scratch-immich -c server --since=24h \
  | grep -E 'Reindexed (face_index|clip_index)'
# 2) indexes are vchord, vectors extension gone
kubectl -n immich-migrate exec scratch-pg -- psql -U postgres -d immich -x -c \
  "select indexname,indexdef from pg_indexes where indexname in ('clip_index','face_index');" # expect vchordrq
kubectl -n immich-migrate exec scratch-pg -- psql -U postgres -d immich -tAc \
  "select extname,extversion from pg_extension order by 1;"   # vchord present, NO vectors
# 3) row counts UNCHANGED vs M1 BEFORE (Codex B1 — proves no destructive jobs ran)
kubectl -n immich-migrate exec scratch-pg -- psql -U postgres -d immich -Atc "
  select 'asset',count(*) from asset
  union all select 'person',count(*) from person
  union all select 'face_search',count(*) from face_search
  union all select 'smart_search',count(*) from smart_search;"
```
Then freeze the DB:
```bash
kubectl -n immich-migrate scale deploy/scratch-immich --replicas=0
```

## Phase M3 — Dump vchord-native, restore into CNPG
> **Rehearsal-corrected (2026-06-03).** Three things the rehearsal fixed vs the original draft: (a) CNPG pods have a **read-only `/tmp`** → use the writable PVC root `/var/lib/postgresql/data`; (b) the TOC filter must drop **both** `EXTENSION` creates **and** `COMMENT ON EXTENSION` (non-owner can't run the comment); (c) **all 8 extensions** must already exist in CNPG (now handled by the fixed `db-cnpg.yaml postInitApplicationSQL` — vchord/cube/earthdistance/pg_trgm/unaccent/uuid-ossp, plus `vector` via vchord CASCADE).

```bash
W=/var/lib/postgresql/data   # CNPG writable path (NOT /tmp — read-only)
kubectl -n immich-migrate exec scratch-pg -- bash -lc \
  'pg_dump -Fc -U postgres -d immich --no-owner --no-privileges -f /tmp/immich-vchord.dump'
kubectl -n immich-migrate cp scratch-pg:/tmp/immich-vchord.dump /tmp/immich-vchord.dump
kubectl -n immich cp /tmp/immich-vchord.dump immich-cnpg-db-1:$W/immich-vchord.dump -c postgres

kubectl -n immich exec immich-cnpg-db-1 -c postgres -- bash -lc '
  set -e; W=/var/lib/postgresql/data
  # Drop EXTENSION creates AND comments-on-extension from the TOC (Codex H4 + rehearsal):
  pg_restore -l $W/immich-vchord.dump \
    | awk "!(\$4==\"EXTENSION\" || (\$4==\"COMMENT\" && \$6==\"EXTENSION\"))" > $W/r.list
  # Atomic restore as superuser, objects owned by immich (Codex B3/H5):
  pg_restore -U postgres -d immich --single-transaction --exit-on-error \
    --no-owner --role=immich --clean --if-exists -L $W/r.list $W/immich-vchord.dump
  echo "pg_restore rc=$?"
  psql -U postgres -d immich -Atc "select (select count(*) from asset) asset, (select count(*) from \"user\") usr;"
  rm -f $W/immich-vchord.dump $W/r.list'
```
**Expect:** `pg_restore rc=0`, `asset`/`usr` counts matching M2. Verify ownership + app-role access:
```bash
kubectl -n immich exec immich-cnpg-db-1 -c postgres -- psql -U postgres -d immich -tAc \
  "select tableowner from pg_tables where tablename='asset';"          # expect: immich
kubectl -n immich exec immich-cnpg-db-1 -c postgres -- psql -U postgres -d immich -tAc \
  "set role immich; select count(*) from asset;"                       # app role can read
```
`--single-transaction` makes a failed restore roll back fully (the rehearsal hit this twice — clean re-run each time). If CNPG ever needs a hard reset before re-restore (only if the cluster carries stale data and isn't being recreated):
```sql
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='immich' AND pid<>pg_backend_pid();
DROP DATABASE immich WITH (FORCE);
CREATE DATABASE immich OWNER immich;  -- then reconnect; postInitApplicationSQL only runs on a fresh CNPG bootstrap, so re-create extensions manually if you went this route.
```

## Phase M4 — Cutover (real run only)
1. **Keep ingress OFF until validated** (Codex H7): don't merge the `app.yaml` enable until M3 is green; or enable controllers but hold the IngressRoute. QNAS Immich app+ML stay **stopped**; QNAS DB/data untouched (rollback anchor).
2. Stop QNAS `immich-server` + `immich-machine-learning`, take a **fresh** pgvecto.rs dump, re-run **M1–M3** with `DUMP=<fresh>`.
3. Enable the cluster app: uncomment `app.yaml` in `apps/production/immich/kustomization.yaml` (PR → merge).
4. Verify: timeline + **thumbnails** (NFS read), **search + People** (vchord restore), upload one disposable test asset.
5. **Tailscale ingress/funnel handoff.** Today the QNAS exposes Immich via a Tailscale **sidecar** (`tailscale-immich`, hostname `immich`, `TS_SERVE_CONFIG=/config/immich.json` = serve/funnel) on `:2283` — some clients connect through that funnel URL, NOT through `immich.lab.mainertoo.com`. At cutover this must move to the cluster:
   - Expose the cluster `immich` Service (`:2283`) via the **tailscale-operator** — a Tailscale `Ingress` (or a `tailscale`-class Service) with `tailscale.com/funnel: "true"` for public funnel parity. Add this manifest to `apps/base/immich/` (enabled with `app.yaml` at cutover).
   - **Hostname collision:** the MagicDNS name `immich` is held by the QNAS tailscale node. Either (a) free it first — stop/remove the QNAS `tailscale-immich` container at Phase M4-step-2 (it's already stopping the app) so the operator can claim `immich`, or (b) give the cluster ingress a distinct hostname (e.g. `immich-k8s`) and cut clients over, then rename later. Option (a) keeps existing funnel client URLs working.
   - **Order:** bring up the cluster Tailscale ingress only AFTER the QNAS tailscale node releases the name, to avoid a split-brain on the `immich` MagicDNS record. Re-point the mobile app server URL to the funnel/ingress endpoint last.
   - TODO before cutover: capture the current `immich.json` serve/funnel config from the QNAS (`/share/appdata/immich/ts/config/immich.json`) so the cluster funnel reproduces the same public path/port.

## Phase M5 — Teardown
```bash
kubectl delete namespace immich-migrate
```

---

## Codex review dispositions
- **B1 destructive-jobs / emptyDir** → `IMMICH_WORKERS_INCLUDE=api` + before/after row-count invariant (M1↔M2). Rehearsal must prove zero deltas before trusting it.
- **B2 completion gate** → require both `Reindexed face_index`+`clip_index`, `vchordrq` index DDL, and `vectors` gone, before dumping.
- **B3 atomic restore** → `--single-transaction --exit-on-error` on both restores + documented DROP/recreate fallback.
- **H3 PG major** → scratch is now **PG17** (`17-vectorchord0.4.2-pgvectors0.3.0`), same major as CNPG; the PG14→17 jump happens only at the dual-extension scratch restore (forgiving).
- **H4 TOC filter** → `awk '$4 != "EXTENSION"'` + inspect generated SQL, instead of `grep -vi EXTENSION`.
- **H5 role** → restore as `-U postgres … --role=immich`; verify `SET ROLE immich` create/drop probe first.
- **H6 window** → record per-phase wall-clock in rehearsal; pre-staging gives no clean incremental (no logical-replication path), so the cutover re-runs M1–M3 fully.
- **H7 rollback** → ingress off until validated; QNAS stays the anchor.
- **M2 redis** → own Deployment. **M3 tables** → singular `asset`/`"user"`. **M4 dump name** → `$DUMP` var. **M5 preload** → verified at M1. **M6 NFS** → already proven by the write test. **M7 storage** → DB is small (183 MB dump); 10Gi CNPG stands, re-confirm with `pg_database_size` at rehearsal.
- **H2 DB_VECTOR_EXTENSION** → set `vectorchord` (Immich honors configured backend; auto-detect order is vchord→vectors→vector). Confirm via logs in M2.

## Still genuinely unproven until rehearsal (do NOT skip M0)
- B1: that API-only Immich runs the reindex AND fires no destructive job (row counts hold).
- B2/H1: that the reindex completes from stored embeddings without the ML service.
- H3: that the PG14→PG17 dual-scratch restore + vchord conversion + PG17→PG17 CNPG restore all succeed end-to-end.
