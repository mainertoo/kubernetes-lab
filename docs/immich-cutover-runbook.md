# Immich Cutover Runbook (Phase 1 + Phase 3)

Companion to `docs/immich-cluster-migration-plan.md`. Phase 2 (cluster CNPG DB) is **already done and live**. This runbook is the copy/paste sequence for the parts that act on the QNAS + the cutover.

**Verified facts going in (2026-06-03):**
- QNAS Immich: **v2.7.5**, DB = `tensorchord/pgvecto-rs:pg14-v0.2.0` (pgvecto.rs `vectors` 0.2.0).
- Cluster CNPG `immich-cnpg-db` is healthy with **vchord 0.4.2** + cube + earthdistance + pgvector 0.8.0, `shared_preload_libraries=vchord.so`, owner role `immich`, creds in secret `immich-cnpg-db-app`.
- NFS write-as-root proven OK. App layer is gated out of `apps/production/immich/kustomization.yaml` (single-writer).

**vchord is matched end-to-end at 0.4.2** — keep it that way (don't use a 0.4.3 image on the QNAS, or the dump won't restore cleanly into the 0.4.2 cluster).

Shell variable used throughout (QNAP Container Station docker path):
```bash
DOCKER=/share/ALLDATA/.qpkg/container-station/bin/docker
```

---

## PHASE 1 — In-place pgvecto.rs → VectorChord on the QNAS

> Goal: migrate the *extension* on the system you trust, with rollback, so the eventual cluster restore is a boring same-extension dump/restore. The QNAS keeps serving photos throughout — this only touches the DB container.

### 1.1 Safety dump (rollback insurance, on top of the 2.5 GB `backups/`)
```bash
cd ~/home_server/docker-qnas/immich
sudo $DOCKER exec immich_postgres pg_dump -Fc -U postgres -d immich \
  -f /tmp/immich-prevchord.dump
# copy it somewhere durable on the NAS
sudo $DOCKER cp immich_postgres:/tmp/immich-prevchord.dump \
  "/share/QNAS/data/photos/library/backups/immich-prevchord-$(date +%Y%m%d).dump"
ls -lh /share/QNAS/data/photos/library/backups/ | tail -3
```

> **This stack is Portainer-managed** — there is NO compose file on the QNAS filesystem, so `docker compose ...` commands fail with `no configuration file provided`. The compose lives in the `home_server` git repo and is deployed via Portainer. The `sudo $DOCKER exec immich_postgres ...` commands (dump/validate) DO work — only compose-file commands don't.

### 1.2 Update the compose (via the `home_server` repo)
The `database:` image change is committed in `home_server` PR #48 (branch `feat/immich-vectorchord-db`). It swaps:

**Replace this image line:**
```yaml
    image: docker.io/tensorchord/pgvecto-rs:pg14-v0.2.0
```
**with:**
```yaml
    image: ghcr.io/immich-app/postgres:14-vectorchord0.4.2-pgvectors0.2.0
    shm_size: 128mb
```

…and DELETE the old `command:` line (the `shared_preload_libraries=vectors.so` override) — the new image bakes preload + tuning in itself; leaving the old override breaks startup.

### 1.3 Redeploy via Portainer (NOT `docker compose`)
1. **Merge `home_server` PR #48** so `main` carries the new image.
2. In **Portainer → Stacks → immich**:
   - **If it's a Git-backed stack:** click **Pull and redeploy** (ensure **Re-pull image** / "always pull" is enabled). Portainer pulls the updated compose from `main` *and* the new postgres image.
   - **If it's a web-editor stack:** paste the §1.2 change into the stack editor (new `image:` + `shm_size: 128mb`, delete the `command:` line), then **Update the stack** with **Re-pull image** enabled.
3. **Watch the migration** — Portainer → Containers → `immich_server` → **Logs** (or from the QNAS shell):
   ```bash
   sudo $DOCKER logs -f immich_server
   ```
**What you want to see:** the server starts, then `Reindexing clip_index` / `Reindexing face_index`. **It is normal for these to appear "stuck" for a while** — that's the vector reindex. Wait for healthy, no `error`/`FATAL`.

> Tag-change note: because the image **tag** changed (not `:latest`), Portainer only pulls the new image once the compose reference is updated (via the git pull or the editor edit) — a plain "re-pull" on the unchanged stack won't fetch it.

### 1.4 Validate the extension migrated
```bash
sudo $DOCKER exec immich_postgres psql -U postgres -d immich -tAc \
  "select extname, extversion from pg_extension order by 1;"
```
**Expect:** `vchord 0.4.2`, `cube`, `earthdistance`, `vector`, `plpgsql`. The deprecated `vectors` (pgvecto.rs) should be gone (Immich drops it after migrating). Then in the Immich web UI: run a **smart search** ("beach", a person's face) and confirm results + the **People** view still works. No pgvecto.rs deprecation banner.

### 1.5 Measure the DB size → tells us CNPG storage
```bash
sudo $DOCKER exec immich_postgres psql -U postgres -d immich -tAc \
  "select pg_size_pretty(pg_database_size('immich'));"
```
**→ Send me this number.** I'll set `CNPG_STORAGE_SIZE` (currently a `10Gi` placeholder) to ≥3× it (WAL + restore-temp + reindex headroom) via a quick PR before cutover, if needed.

> **Rollback (Phase 1):** if anything looks wrong, set the image back to `tensorchord/pgvecto-rs:pg14-v0.2.0` + restore the original `command:` line, `docker compose up -d`. The data dir is untouched by a failed start; if the migration half-ran, restore `/tmp/immich-prevchord.dump` into a fresh pgvecto.rs container. **Do not proceed to Phase 3 until 1.4 is green.**

**After Phase 1 is green, the QNAS keeps running normally. Stop here until you're ready for the maintenance window.**

---

## PHASE 3 — Cutover (maintenance window)

> Single-writer invariant: from 3.2 until the cluster app is up, **nothing writes the photo tree**. Only a disposable test asset is written during validation.

### 3.1 Pre-flight
- Immich web UI → Administration → **Jobs**: confirm nothing important is mid-run (let active jobs drain).
- Confirm the cluster DB is still healthy:
  ```bash
  kubectl -n immich get cluster immich-cnpg-db \
    -o jsonpath='{.status.phase} ready={.status.readyInstances}{"\n"}'
  ```

### 3.2 Stop the QNAS Immich app (leave its DB up for the final dump)
```bash
cd ~/home_server/docker-qnas/immich
sudo $DOCKER compose stop immich-server immich-machine-learning
```

### 3.3 Final dump (vchord-native, app-DB only)
```bash
sudo $DOCKER exec immich_postgres pg_dump -Fc -U postgres -d immich \
  --no-owner --no-privileges -f /tmp/immich-final.dump
# pull it to your workstation (where kubectl runs)
sudo $DOCKER cp immich_postgres:/tmp/immich-final.dump ./immich-final.dump
ls -lh ./immich-final.dump
```

### 3.4 Restore into the cluster CNPG
The cluster already has the extensions pre-created, so we **filter EXTENSION entries out of the restore** and load as the `postgres` superuser (in-pod socket auth) while assigning ownership to `immich`:
```bash
# copy the dump into the primary pod
kubectl -n immich cp ./immich-final.dump immich-cnpg-db-1:/tmp/immich-final.dump -c postgres

# build a restore list without the (already-present) extensions, then restore
kubectl -n immich exec immich-cnpg-db-1 -c postgres -- bash -lc '
  set -e
  pg_restore -l /tmp/immich-final.dump | grep -vi "EXTENSION" > /tmp/r.list
  pg_restore -U postgres -d immich --no-owner --role=immich \
    --clean --if-exists -L /tmp/r.list /tmp/immich-final.dump
  echo "---- restored extensions (sanity) ----"
  psql -U postgres -d immich -tAc "select extname,extversion from pg_extension order by 1;"
  echo "---- row counts (sanity) ----"
  psql -U postgres -d immich -tAc "select (select count(*) from assets) as assets, (select count(*) from users) as users;"
  rm -f /tmp/immich-final.dump /tmp/r.list
'
```
**Expect:** vchord/cube/earthdistance/vector present, and non-zero `assets`/`users` counts matching your library. (If the `assets`/`users` table names differ on v2.7.5, any non-zero core table is fine — the point is data landed.)

### 3.5 Enable the cluster app (the moment it becomes sole writer)
On a fresh branch from `master`, uncomment `app.yaml` in `apps/production/immich/kustomization.yaml`:
```yaml
resources:
  - db-cnpg.yaml
  - app.yaml        # <-- uncomment
```
Commit → PR → merge. Then force reconcile and watch:
```bash
kubectl -n flux-system annotate kustomization immich \
  reconcile.fluxcd.io/requestedAt="$(date +%s)" --overwrite 2>/dev/null || true
kubectl -n immich get pods -w
```
(Or just wait for Flux's interval.) `immich` (server), `immich-ml`, and `immich-redis` pods should come up; the server's startup probe allows up to 10 min for first boot.

### 3.6 Verify end-to-end
```bash
kubectl -n immich get pods
kubectl -n immich logs deploy/immich-server --tail=50   # name may be a different controller kind; adjust
```
- Browse `https://immich.lab.mainertoo.com`: timeline loads, **thumbnails render** (proves NFS read path), **smart search + People** work (proves vchord restore).
- Upload **one disposable test photo**, confirm it appears and an ML job runs (check an `immich-ml` pod logs for inference + GPU use).
- Re-point the **mobile app** server URL to the new endpoint.

### 3.7 Decommission (after a soak period)
- `sudo $DOCKER compose down` the QNAS immich stack; archive `~/home_server/docker-qnas/immich/` per repo convention.
- Keep `/share/appdata/immich/postgres` + the Phase-1/Phase-3 dumps for N days as rollback, then reclaim.
- Confirm the cluster's first **ScheduledBackup** ran green to Garage S3:
  ```bash
  kubectl -n immich get backup
  ```

> **Rollback (Phase 3, before real writes):** re-comment `app.yaml` (PR), delete the test asset from the NFS tree, `docker compose start immich-server immich-machine-learning` on the QNAS. Clean as long as no real user uploads hit the cluster. Past that point it's forward-only.

---

## Quick reference — what I (Claude) do vs you

| Step | Who |
|---|---|
| 1.1–1.5 QNAS in-place migration + size | **you** (QNAS shell) |
| Set `CNPG_STORAGE_SIZE` from your number | me (PR) |
| 3.2–3.4 stop + dump + restore | you run; I can drive 3.4 (kubectl) |
| 3.5 enable app | me (PR) + you merge |
| 3.6 verify | me (kubectl/curl) + you (mobile app, eyeball) |
