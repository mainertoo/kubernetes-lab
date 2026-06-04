# Immich → Cluster Migration Plan

**Status:** DRAFT v2 — Codex review folded in, tags pinned, **manifests scaffolded** on branch `feat/immich-cluster-migration` (uncommitted; pending user approval + remaining Phase-0/1 gates)

> **Scaffold (2026-06-03):** Manifests written under `apps/base/immich/` + `apps/production/immich/`. Validated with `kubectl kustomize` (base, db-cnpg components+vchord patch, whole apps/production all render clean; the `\${APP}`-targeted strategic-merge patch correctly injects `shared_preload_libraries: [vchord.so]` + `postInitApplicationSQL`). The writing app (`app.yaml`) is committed but **left out of `apps/production/immich/kustomization.yaml`** (commented) so Flux deploys only the CNPG DB until cutover — the single-writer invariant. DB creds come from CNPG's auto-generated `immich-cnpg-db-app` secret (no custom SOPS secret, no superuser). Still unvalidated locally: HelmRelease chart templating + full postBuild substitution — the repo's flux-local CI covers that on PR.
**Author:** session 2026-06-03
**Goal:** Move the Immich *application* off the QNAS Docker host into the production K3s cluster, keep the 187 GB photo library on the QNAS (NFS), gain GPU-accelerated ML/transcode + GitOps lifecycle + CNPG/Garage-S3 backups of the database. Decommission the Docker stack only after the cluster instance is validated.

> **Review note:** This v2 incorporates a Codex adversarial review (14 findings). Corrections folded in: `postInitApplicationSQL` (not `postInitSQL`), `pg_dump -Fc` + `pg_restore` (not `pg_dumpall`+`psql`), superuser only at first-boot then dropped, explicit single-writer gate, NFS *write* preflight, throwaway-restore rehearsal, probes, ML mounts, sizing, rollback invariant. See §8 for the finding-by-finding disposition.

---

## 1. Current state (verified 2026-06-03)

Source: `~/home_server/docker-qnas/immich/{docker-compose.yml,.env}` + live QNAS inspection.

| Component | Current (QNAS Docker) | Notes |
|---|---|---|
| immich-server | `ghcr.io/immich-app/immich-server:release` | PUID=1000 PGID=100, network via Tailscale sidecar `:2283` |
| immich-machine-learning | `:release` (CPU, no hwaccel) | model-cache = local docker volume |
| redis | `redis:8.8-alpine` | ephemeral cache/queue |
| database | `tensorchord/pgvecto-rs:pg14-v0.2.0` | **PG14 + pgvecto.rs `vectors` v0.2.0 (DEPRECATED)**; `shared_preload_libraries=vectors.so`, `search_path="$user", public, vectors` |
| UPLOAD_LOCATION | `/share/QNAS/data/photos/library` | **187 GB**: library 132G / encoded-video 44G / thumbs 8.7G / backups 2.5G / upload 238M |
| DB_DATA_LOCATION | `/share/appdata/immich/postgres` | local QNAS appdata |
| Ingress | Tailscale sidecar, host `immich`, `:2283` | |

**NFS reality:** QNAP exports `rw` to `192.168.1.0/24`. The existing cluster `qnap-media-pv` already mounts `192.168.1.252:/QNAS/data`. Immich's photos are at `/QNAS/data/photos/library` — a subdirectory of an already-working export. File ownership is `admin:administrators`; the container writes as `1000:100`.

**Live Immich version: v2.7.5** (confirmed 2026-06-03 via `curl http://localhost:2283/api/server/version` on the QNAS host → `{"major":2,"minor":7,"patch":5}` — the current latest). It is running successfully against the pgvecto.rs-only image, which confirms v2.7.5 still tolerates pgvecto.rs (its `VECTORS_VERSION_RANGE` is `>=0.2 <0.4`). The in-place migration (§2) therefore works with the current app version — no app bump needed first.

---

## 2. The vector-extension migration (the crux) — pgvecto.rs → VectorChord

**Why we can't avoid it:** Immich needs a vector-search extension for smart search + face recognition. Vanilla Postgres can't do efficient nearest-neighbor search, so a vector extension is mandatory. You currently run **pgvecto.rs** (`vectors`), which is **deprecated**. Its successor (same authors, TensorChord) is **VectorChord** (`vchord`) — Immich's recommended path, with a built-in pgvecto.rs→VectorChord migration. We migrate to VectorChord rather than sideways to pgvector. (Decision confirmed by user 2026-06-03.)

**Bonus — this also solves PostgreSQL 14 EOL.** PG14 reaches end-of-life **2026-11-13** (~5 months out). Immich users were historically pinned to PG14 because the old pgvecto.rs image only existed for PG14. The new VectorChord-based images are published for PG14–18, but bumping the major requires a real dump/restore (not an in-place tag swap). Since we're doing a dump/restore into CNPG anyway, we land on **PG17** — killing the deprecated extension *and* the EOL major in one move. PG17 EOL is 2029.

> **⛔ STRATEGY A IS DEAD — the QNAP cannot run VectorChord (2026-06-03).** Attempted in PR #48 (home_server): swapping the QNAS DB to `immich-app/postgres:14-vectorchord0.4.2-pgvectors0.2.0` crash-loops immediately:
> ```
> <jemalloc>: Unsupported system page size
> memory allocation of 16 bytes failed
> ```
> The QNAP is **`aarch64` (ARM64, 64 KB kernel page size)**; the VectorChord image's jemalloc is built for 4 KB pages. This is a hardware incompatibility, not a config fix — **VectorChord/jemalloc images will never run on this QNAP.** Reverted in PR #49; QNAS Immich restored to pgvecto.rs (data dir untouched — the crash was at allocator init, pre-DB-open). Lesson: the Proxmox cluster nodes are x86/4 KB (why the CNPG VectorChord DB is healthy there), so **all VectorChord work must happen in the cluster, never on the QNAP.**
>
> **→ Revised approach (Strategy A′): the pgvecto.rs→VectorChord migration moves into the cluster.** The QNAP stays on pgvecto.rs permanently. We already have the pgvecto.rs dump (183 MB, `immich-prevchord-20260603.dump`). Migrate it in-cluster (x86), then load into CNPG. Two candidate designs under evaluation (pending redesign + Codex pass):
> - **X (keep CNPG):** restore the pgvecto.rs dump into a temporary `immich-app/postgres` Deployment in-cluster → point a throwaway Immich v2.7.5 at it to auto-migrate `vectors`→`vchord` → dump vchord-native → restore into the existing CNPG `cloudnative-vectorchord` cluster → cutover. Preserves CNPG barman/PITR backups; more steps.
> - **Y (simpler, weaker backups):** run the cluster Immich directly against an `immich-app/postgres` Deployment (it auto-migrates in place); back its PVC up via label-driven Kopia instead of CNPG barman. Drops the scratch-migration dance; loses CNPG-grade PITR. The already-deployed CNPG cluster would be torn down.
>
> The original Strategy A text below is retained for history.

### Strategy A (chosen): migrate the extension IN PLACE on QNAS first, then move

The current QNAS image has only `vectors.so` — a dump from it carries `CREATE EXTENSION vectors` + `vectors.vector`-typed columns that won't restore into a vchord-only image. So:

1. **On QNAS**, swap the DB image to a **dual-extension** image that bundles *both* pgvecto.rs (matching v0.2.0) *and* vchord, restart Immich → Immich's built-in migration converts `vectors` → `vchord` on the system you already trust, with the 2.5 GB `backups/` dumps as rollback.
2. **Validate** Immich healthy on VectorChord (search + faces work, no pgvecto.rs deprecation warning).
3. The DB is now vchord-native → a logical dump restores cleanly into the CNPG `cloudnative-vectorchord` image.

*Why:* decouples the fragile extension conversion from the infra move; the cluster restore becomes a same-extension PG14→PG17 logical restore.

**Strategy B (rejected):** restore the old pgvecto.rs dump straight into CNPG and migrate in-cluster. Requires a CNPG image bundling *both* `vectors.so` and `vchord.so`. **Confirmed unavailable** — `tensorchord/cloudnative-vectorchord` ships vchord only (verify: `docker run --rm --entrypoint sh ghcr.io/tensorchord/cloudnative-vectorchord:17.5-0.4.2 -lc 'ls /usr/lib/postgresql/*/lib/*vectors* 2>/dev/null; ls /usr/share/postgresql/*/extension/*vectors* 2>/dev/null'` → expect empty). Strategy B is dead; A is the only viable path.

### Dump / restore mechanics (corrected per Codex #7)

Use `pg_dump` of the single app DB (not `pg_dumpall`) — CNPG already creates the DB, owner role, and managed-role passwords at bootstrap, so a globals-level dump would collide.

```bash
# DUMP from QNAS AFTER the in-place vchord migration (stop immich-server first)
pg_dump -Fc -d immich --no-owner --no-privileges -f immich.dump
# Inspect before restoring:
pg_restore --list immich.dump | rg -n "EXTENSION|VECTOR|DATABASE|OWNER|ACL"

# RESTORE into the CNPG app DB, AFTER extensions exist (see §3 postInitApplicationSQL),
# connecting as the CNPG superuser, remapping ownership to the app role:
pg_restore --clean --if-exists --no-owner --role=immich \
  -h <immich-rw.immich.svc> -U postgres -d immich immich.dump
```
Ordering trap: the `vchord`/`cube`/`earthdistance` extensions must exist in the target **before** restoring vector-typed columns — CNPG bootstrap creates them (§3), so restore is a *data* load into a pre-extended DB.

---

## 3. Target architecture in-cluster

Namespace: `immich`. One bjw-s `app-template` HelmRelease (3 controllers) + a CNPG cluster.

```
immich (ns)
├── HelmRelease immich (app-template 5.0.1)
│   ├── controller: server   immich-server      (+ /dev/dri QSV transcode, NFS upload mount, probes)
│   ├── controller: ml        immich-machine-learning:…-openvino (+ /dev/dri, model-cache + /cache writable)
│   └── controller: redis     valkey (ephemeral; drained at cutover)
├── CNPG Cluster immich  (cloudnative-vectorchord, ceph-rbd, superuser@first-boot only, ScheduledBackup→Garage S3)
├── PV/PVC immich-media (NFS 192.168.1.252:/QNAS/data/photos/library, RWX, Retain)
├── PVC immich-model-cache (ceph-rbd, ~10Gi, NOT backed up — regenerable)
├── Secret immich-secret.sops.yaml (DB password etc.)
└── IngressRoute immich.lab.mainertoo.com (Traefik, NO Authentik mw — mobile app needs raw API)
```

### CNPG specifics (corrected per Codex #5/#6/#9)
- **Extension knobs aren't in the shared component.** `components/cnpg-cluster/base/cluster.yaml` only exposes `postgresql.parameters` substitutions — *not* `shared_preload_libraries` or post-init SQL. Add a per-app **`spec.patches` overlay** in `db-cnpg.yaml` (the sparky-fitness pattern), setting:
  - `spec.postgresql.shared_preload_libraries: ["vchord.so"]`
  - `spec.bootstrap.initdb.postInitApplicationSQL:` (runs against the **app** DB, not `postgres`):
    ```sql
    CREATE EXTENSION IF NOT EXISTS vchord CASCADE;
    CREATE EXTENSION IF NOT EXISTS cube;
    CREATE EXTENSION IF NOT EXISTS earthdistance;
    ```
- **Superuser only at first boot.** Pre-create the extensions via the CNPG superuser at bootstrap, run Immich as the plain `immich` owner role, and set `DB_VECTOR_EXTENSION=vectorchord`. Only grant the app role superuser if startup/extension-upgrade logs prove it's needed (Immich's postgres-standalone docs document a no-superuser path). Don't bake permanent superuser into the managed role.
- **Kyverno generate rules are write-once** — get the first commit of `db-cnpg.yaml` and any generated secrets right; later edits need the Flux force annotation.

### Storage
- **Photos (NFS):** dedicated `immich-media-pv` scoped to exactly `/QNAS/data/photos/library` (least-privilege vs the broad 20 Ti `qnap-media-pvc`, which also lives in the `media` namespace — PVCs are namespace-scoped). RWX, `reclaimPolicy: Retain`. Mounted at `/usr/src/app/upload` on the **server**.
- **Path stability:** Immich stores paths relative to `UPLOAD_LOCATION`; mounting the same tree at `/usr/src/app/upload` keeps every DB reference valid — no re-import.
- **DB:** CNPG on `ceph-rbd`. **Size from measurement, not guess** (Codex #10): after the QNAS vchord migration, run `psql -d immich -c "select pg_size_pretty(pg_database_size('immich'));"`, then size `CNPG_STORAGE_SIZE` with WAL + restore-temp + vchord reindex headroom (rule of thumb: ≥3× measured DB size, floor 10Gi).
- **model-cache:** `ceph-rbd` ~10Gi, no backup label (re-downloads).
- **Permissions (Codex #2 — RESOLVED via run-as-root, 2026-06-03):** The tree is owned **`0:0` (root), mode 755** — `id admin` on QNAP returns **uid=0**, and `ls -n` shows `0 0`. The compose's `PUID=1000/PGID=100` is **vestigial** — the official `immich-server` image ignores it and runs as **root**; that's why it writes into a root-owned 755 tree, and why the export's **`no_root_squash`** is load-bearing. **Decision: run the Immich server (and ml, if it mounts NFS) pod as `runAsUser: 0 / runAsGroup: 0`** (i.e. NOT the repo's usual `runAsNonRoot`). Rationale: matches current working behavior + the image's design + the root-owned tree, and **avoids a 187 GB recursive chown** entirely. Do **not** set `fsGroup` on the NFS volume (it could trigger a recursive chown); if used elsewhere, pin `fsGroupChangePolicy: OnRootMismatch`. This is a deliberate deviation from the non-root norm (Plex/Jellyfin run 1000) — documented here so a future reader doesn't "fix" it. Still prove writability with a debug pod in Phase 0. Ref [[feedback_pvc_non_root_user_needs_fsgroup]].

### GPU
- **ML (OpenVINO):** image tag `…-openvino`, `nodeSelector: intel.feature.node.kubernetes.io/gpu: "true"`, `/dev/dri` hostPath. OpenVINO on Intel is the finicky path — may silently fall back to CPU; validate GPU is actually used. Also ensure `/cache` (and any `/.config`,`/.cache`) are writable for the non-root ML container (Codex #12).
- **Transcode (QSV):** server gets `/dev/dri` too; set hwaccel=QSV in Immich admin post-deploy.

### Ingress / auth
- Immich has its own auth + mobile clients hit the API directly → expose like Plex/Jellyfin, **no Authentik forward-auth**. Internal `immich.lab.mainertoo.com`; external exposure decided separately (today it's Tailscale-only). Mobile app server URL must be re-pointed at cutover (Codex #5-adjacent).
- **Probes (Codex #11):** server liveness/readiness on `GET /api/server/ping`; startup probe generous enough for DB migrations/reindex on first boot.

---

## 4. Pinned images (Renovate-tracked)

All resolved against ghcr 2026-06-03. **vchord version is matched end-to-end at 0.4.2** so the only delta on restore is the PG14→17 major jump.

| Purpose | Image | Pin |
|---|---|---|
| QNAS in-place migration DB (PG14, bundles pgvecto.rs 0.2.0 **exact** + vchord) | `ghcr.io/immich-app/postgres` | **`14-vectorchord0.4.2-pgvectors0.2.0`** |
| CNPG target DB (PG17.5 + vchord, CNPG operand) | `ghcr.io/tensorchord/cloudnative-vectorchord` | **`17.5-0.4.2`** |
| Immich server | `ghcr.io/immich-app/immich-server` | **`v2.7.5`** *(confirm exact latest at execution)* |
| Immich ML (OpenVINO) | `ghcr.io/immich-app/immich-machine-learning` | **`v2.7.5-openvino`** |
| Redis/Valkey | `docker.io/valkey/valkey` | **`8-bookworm`** *(mirror upstream Immich compose; pin minor at execution)* |
| HelmRelease base | `oci://ghcr.io/bjw-s-labs/helm/app-template` | `5.0.1` *(existing OCIRepository)* |

**✅ BLOCKER GATE — vchord version compatibility — RESOLVED (2026-06-03).** Read directly from Immich **v2.7.5** source (`server/src/constants.ts`):
```
POSTGRES_VERSION_RANGE    = '>=14.0.0'    → PG17 OK
VECTORCHORD_VERSION_RANGE = '>=0.3 <2'    → vchord 0.4.2 OK (well inside range)
VECTORS_VERSION_RANGE     = '>=0.2 <0.4'  → pgvecto.rs 0.2.0 OK (current app runs on it)
```
The concern that Immich needed vchord ≥ 0.5.0 was wrong — `immich-app/postgres` merely *bundles* the newest vchord; the enforced floor is **0.3**. So we run **latest Immich v2.7.5** against `cloudnative-vectorchord:17.5-0.4.2` with no downgrade, no custom image, no waiting. vchord is matched end-to-end at 0.4.2, so restore has zero extension-version delta. Renovate note: the compound `17.5-0.4.2` tag is non-semver — needs a `versioning: regex:` rule, not `extractVersion` ([[feedback_renovate_extractversion_vs_versioning_regex]]).

---

## 5. Execution sequence

**Phase 0 — Verification gates (NO changes; all must pass before committing manifests)**
- [x] ~~Confirm the live running Immich version~~ → **v2.7.5**, still running on pgvecto.rs (API probe, 2026-06-03).
- [x] ~~**vchord compat gate**~~ → **RESOLVED**: v2.7.5 accepts vchord `>=0.3 <2`; `17.5-0.4.2` is compatible. App pinned at v2.7.5 (latest). See §4.
- [ ] Confirm `cloudnative-vectorchord:17.5-0.4.2` ships vchord-only (Strategy B dead — sanity check).
- [x] ~~**NFS write test (BLOCKER, Codex #2)**~~ → **PASSED 2026-06-03**: throwaway pod as `runAsUser:0/runAsGroup:0` mounted the NFS export and did `mkdir/write/rename/read/rm` in a scratch dir under UPLOAD_LOCATION; files created as `0:0`. `no_root_squash` + run-as-root confirmed. No fsGroup used (no recursive chown). uid(`admin`)=0, gid(`administrators`)=0.
- [ ] Decide PG target (PG17 baseline) and confirm Immich supports it for the chosen app version.

**Phase 1 — In-place pgvecto.rs → VectorChord on QNAS**
- [ ] Manual `pg_dump -Fc` of the current DB (belt-and-suspenders alongside the 2.5 GB `backups/`).
- [ ] Swap QNAS DB image → `ghcr.io/immich-app/postgres:14-vectorchord0.4.2-pgvectors0.2.0`; ensure app version supports the vchord migration; restart; let auto-migration run.
- [ ] Validate: search works, faces intact, no pgvecto.rs warning; `select extname,extversion from pg_extension;` shows `vchord`. **This is the rollback checkpoint.**
- [ ] Measure DB size → set `CNPG_STORAGE_SIZE` (§3).

**Phase 1.5 — Throwaway restore rehearsal (Codex #8)**
- [ ] Spin up a *disposable* CNPG `cloudnative-vectorchord:17.5-0.4.2` cluster (separate name), restore the Phase-1 dump, confirm extensions + `pg_restore` clean, and boot a throwaway Immich pod against it. Tear down. Only proceed if green.

**Phase 2 — Land cluster infra (QNAS still serving; NO cluster writes) — ✅ DONE 2026-06-03**
- [x] Repo changes merged (PR #723 scaffold + PR #724 namespace fix). Writing app gated out of `apps/production/immich/kustomization.yaml` (only `db-cnpg.yaml` active) — single-writer invariant holds; nothing mounts the NFS tree.
- [x] CNPG cluster **healthy** (`immich-cnpg-db-1` 2/2 Running). Live-verified: `vchord 0.4.2` + `cube 1.5` + `earthdistance 1.2` + `vector 0.8.0`; `shared_preload_libraries=vchord.so`. `immich-cnpg-db-app` secret + ObjectStore + daily ScheduledBackup created.
- [ ] (Optional) Trigger a manual backup to confirm Garage S3 write before relying on the 08:15 schedule: `kubectl -n immich create -f -` a `Backup` CR, or wait for the first scheduled run (Phase 4 check).

**Phase 3 — Cutover (maintenance window, single-writer invariant)**
- [ ] Drain/verify no critical queued Immich jobs (admin Jobs dashboard) (Codex #14), then stop QNAS immich-server + ml (leave DB up for the final dump).
- [ ] Final `pg_dump -Fc` → `pg_restore` into CNPG (§2).
- [ ] Enable the cluster HelmRelease (add to kustomization / scale up) pointed at NFS + CNPG.
- [ ] Verify: timeline loads, thumbnails render, **upload one disposable test asset**, ML job runs (confirm GPU), faces/search intact; re-point the mobile app server URL; update Tailscale/DNS/ingress so `immich` resolves to the cluster.

**Phase 4 — Decommission**
- [ ] `docker compose down` the QNAS stack; archive the compose dir per repo convention.
- [ ] Keep `/share/appdata/immich/postgres` + the Phase-1 dump for N days as rollback, then reclaim.
- [ ] Confirm CNPG ScheduledBackup ran green to Garage S3.

---

## 6. Backups — what's actually protected
- **DB (crown jewels):** CNPG → barman → Garage S3, daily, 30 d retention. ✅ the real win.
- **Photos (187 GB on NFS):** **NOT** in cluster backups — they stay as protected as the QNAS makes them (same as Plex media). QNAP-side snapshots/backup remain responsible for originals. Optionally keep Immich's built-in DB-dump-to-`backups/` on NFS as a second DB copy.

## 7. Rollback invariant (Codex #13)
Until Phase 3 validation passes, the cluster must perform **no real writes** beyond a single disposable test asset that is deleted before any rollback. If rollback is needed after cutover: stop cluster Immich, remove the test asset from the NFS tree, restart QNAS Immich against its preserved PG14 data dir. If real user writes (uploads/jobs) have occurred on the cluster, rollback is no longer clean — document forward-only past that point.

## 8. Codex review disposition
- **Folded in as corrections:** #5 `postInitApplicationSQL`, #7 `pg_dump -Fc`/`pg_restore`, #9 drop superuser post-boot, #6 patch-overlay for `shared_preload_libraries`.
- **Folded in as gates:** #1 single-writer `flux build` check, #2 NFS write preflight, #8 throwaway restore rehearsal, #3/#4 image-tag pinning + Strategy B confirmed dead.
- **Folded in as additions:** #10 measured sizing, #11 `/api/server/ping` probes, #12 ML `/cache` writability, #13 rollback invariant, #14 job drain.
- **New (tag-driven) blocker:** §4 vchord 0.4.x-vs-0.5.x compatibility gate.

## 9. Open questions still to close in Phase 0
1. ~~Live Immich version + required vchord range~~ → **CLOSED**: v2.7.5, vchord `>=0.3 <2`, app pinned v2.7.5 (§4).
2. Numeric uid/gid for the securityContext + proven NFS write.
3. Does `immich-machine-learning` need the NFS upload mount, or does the server stream images to it over HTTP? (affects whether ml mounts NFS at all).
4. NFS thumbnail-browsing performance acceptable (all thumbs/encoded-video on NFS)?
5. External exposure for the mobile app (Tailscale vs Traefik external) — match current Tailscale behavior or move to ingress?
