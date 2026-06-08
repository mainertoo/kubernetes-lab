# cnpg-timescaledb operand image

A CloudNativePG operand image = `ghcr.io/cloudnative-pg/postgresql:16-bookworm`
+ the **TimescaleDB** extension, pinned to the version running in the live
`tracearr` database.

Published as `ghcr.io/mainertoo/cnpg-timescaledb:<PG_MAJOR>-<TS_VERSION>`
(currently `16-2.25.0`).

## Why a custom image

CNPG runs PostgreSQL from an immutable *operand* image and the timescaledb
extension binaries must be baked in. As of 2026-06 no maintained community
CNPG+TimescaleDB image shipped a tag new enough (the live DB is on
**timescaledb 2.25.0**; clevyr/imusmanmalik had stalled at 2.19.x). A
TimescaleDB `pg_dump`/restore requires the destination extension version to be
**>= source**, so we pin **equal** here and build it ourselves.

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
override `ts_version` / `pg_major` / `base_tag`).

`shared_preload_libraries: [timescaledb]` and `CREATE EXTENSION timescaledb`
are configured on the CNPG **Cluster** spec
(`apps/base/media/tracearr/db-cnpg/`), not in this image — the image only ships
the extension binaries.
