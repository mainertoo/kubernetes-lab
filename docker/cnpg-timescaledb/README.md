# cnpg-timescaledb operand image

A CloudNativePG operand image = `ghcr.io/cloudnative-pg/postgresql:16-bookworm`
+ the **TimescaleDB** extension, pinned to the version running in the live
`tracearr` database.

Published as `ghcr.io/mainertoo/cnpg-timescaledb:<PG_MAJOR>-<TS_VERSION>[-<REVISION>]`
(currently `16-2.25.0-1`).

## Why a custom image

CNPG runs PostgreSQL from an immutable *operand* image and the timescaledb
extension binaries must be baked in. As of 2026-06 no maintained community
CNPG+TimescaleDB image shipped a tag new enough (the live DB is on
**timescaledb 2.25.0**; clevyr/imusmanmalik had stalled at 2.19.x). Timescale's
docs require a `pg_dump`/restore to land on the **same** extension version as
the source and then upgrade (cross-version restore is unsupported), so we pin
**equal** here and build it ourselves.

## Pin BOTH the loader and the extension

The first `16-2.25.0` build broke at `CREATE EXTENSION timescaledb`
("no installation script ... for version 2.27.2"): pinning only
`timescaledb-2-postgresql-16` to 2.25.0 let its loose dependency
`timescaledb-2-loader-postgresql-16` (the `shared_preload_libraries` .so that
sets the extension's `default_version`) float to the newest 2.27.2, so the
running loader wanted 2.27.2 while only the 2.25.0 SQL install script was
present. The Dockerfile now pins **both** packages to `TS_VERSION` and asserts
`default_version = '<TS_VERSION>'` in the control file at build time. The
`16-2.25.0-1` revision is that fix.

## Path to 2.27.2 (current)

The migration restore runs on `16-2.25.0-1` (same version as source). Once the
data is verified, bump `CNPG_IMAGE` to a freshly built `16-2.27.2` image (its
package ships the 2.25→2.27 upgrade scripts) and run
`ALTER EXTENSION timescaledb UPDATE;` so the cluster lands on the current
release. CNPG rolls the pod on the image change.

## Build-once-and-forget

The build workflow (`.github/workflows/build-cnpg-timescaledb.yml`) is
`workflow_dispatch` only — there is **no** upstream-tracking cron. It builds the
pinned tag and skips if that tag already exists in GHCR. After the one-time
build there is no recurring maintenance:

- `renovate.json` ignores `docker/cnpg-timescaledb/**` and the pinned
  `CNPG_IMAGE` in the consumer manifest, so nothing auto-bumps it.
- Rebuild only when you deliberately raise `TS_VERSION` or the PG major — run
  the workflow with the new inputs, then bump `CNPG_IMAGE` in
  `apps/production/media/tracearr/db-cnpg.yaml`.

## Build manually

GitHub → Actions → **Build cnpg-timescaledb** → *Run workflow* (optionally
override `ts_version` / `pg_major` / `base_tag` / `revision`). Bump `revision`
to republish a packaging fix at the same upstream version (the workflow skips a
tag that already exists in GHCR).

`shared_preload_libraries: [timescaledb]` and `CREATE EXTENSION timescaledb`
are configured on the CNPG **Cluster** spec
(`apps/base/media/tracearr/db-cnpg/`), not in this image — the image only ships
the extension binaries.
