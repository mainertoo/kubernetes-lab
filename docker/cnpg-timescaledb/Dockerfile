# CloudNativePG operand image: PostgreSQL 16 (Debian bookworm) + TimescaleDB.
#
# Why this image exists: CNPG needs an operand image that already contains the
# timescaledb extension .so files; the stock ghcr.io/cloudnative-pg/postgresql
# image does not. The maintained community CNPG+TimescaleDB images (clevyr,
# imusmanmalik) had stalled at ts 2.19.x — too old to restore a dump from our
# live tracearr DB (timescaledb 2.25.0). A TimescaleDB pg_dump/restore requires
# the destination extension version to EQUAL the source (cross-version restore
# is unsupported — Timescale's docs say restore at the same version, then
# upgrade), so we pin EQUAL here. Post-migration we bump to 2.27.2 + run
# ALTER EXTENSION timescaledb UPDATE to land current (the 2.27.2 package ships
# the 2.25→2.27 upgrade scripts).
#
# Build-once-and-forget: tagged ghcr.io/mainertoo/cnpg-timescaledb:16-2.25.0-1,
# built via .github/workflows/build-cnpg-timescaledb.yml (workflow_dispatch only
# — no upstream-tracking cron). Renovate is told to leave the pinned CNPG_IMAGE
# alone. Rebuild only when you deliberately bump TS_VERSION / PG major (or the
# REVISION input, to republish a packaging fix at the same upstream version).
#
# Consumed by the tracearr-cnpg-db Cluster — apps/base/media/tracearr/db-cnpg/.
# shared_preload_libraries: [timescaledb] is set on the CNPG Cluster spec, not
# here; this image only ships the extension binaries.

ARG BASE_TAG=16-bookworm
FROM ghcr.io/cloudnative-pg/postgresql:${BASE_TAG}

# Pinned to match the live source DB's timescaledb version (verified 2026-06-08).
ARG TS_VERSION=2.25.0
ARG PG_MAJOR=16

USER root

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl gnupg ca-certificates; \
    install -d /usr/share/keyrings; \
    curl -fsSL https://packagecloud.io/timescale/timescaledb/gpgkey \
      | gpg --dearmor -o /usr/share/keyrings/timescaledb.gpg; \
    echo "deb [signed-by=/usr/share/keyrings/timescaledb.gpg] https://packagecloud.io/timescale/timescaledb/debian/ bookworm main" \
      > /etc/apt/sources.list.d/timescaledb.list; \
    apt-get update; \
    # Resolve the exact repo version strings (e.g. "2.25.0~debian12-1613") for
    # the requested upstream version so the pin survives the Debian build suffix.
    #
    # BOTH packages MUST be pinned to the SAME version. The -loader package is
    # the shared_preload_libraries .so that drives the extension's
    # default_version; the main package ships the matching SQL install script.
    # apt's dependency between them is loose, so pinning only the main package
    # lets the loader float to the newest version — yielding a default_version
    # with no matching install script ("extension timescaledb has no
    # installation script ... for version X"), which is exactly what broke the
    # first build (loader 2.27.2 vs extension 2.25.0).
    TS_EXT_VER="$(apt-cache madison "timescaledb-2-postgresql-${PG_MAJOR}" \
      | awk -v v="${TS_VERSION}" '$3 ~ ("^" v "~") || $3 == v {print $3; exit}')"; \
    TS_LOADER_VER="$(apt-cache madison "timescaledb-2-loader-postgresql-${PG_MAJOR}" \
      | awk -v v="${TS_VERSION}" '$3 ~ ("^" v "~") || $3 == v {print $3; exit}')"; \
    test -n "${TS_EXT_VER}"; \
    test -n "${TS_LOADER_VER}"; \
    echo "Installing loader=${TS_LOADER_VER} extension=${TS_EXT_VER}"; \
    apt-get install -y --no-install-recommends \
      "timescaledb-2-loader-postgresql-${PG_MAJOR}=${TS_LOADER_VER}" \
      "timescaledb-2-postgresql-${PG_MAJOR}=${TS_EXT_VER}"; \
    # Fail the build (instead of CREATE EXTENSION at runtime) if the loader and
    # extension still disagree: the control file's default_version must equal
    # TS_VERSION.
    test -f "/usr/share/postgresql/${PG_MAJOR}/extension/timescaledb.control"; \
    grep -q "default_version = '${TS_VERSION}'" \
      "/usr/share/postgresql/${PG_MAJOR}/extension/timescaledb.control"; \
    rm -rf /var/lib/apt/lists/* /etc/apt/sources.list.d/timescaledb.list

# Restore the CloudNativePG operand's non-root postgres user (uid 26).
USER 26
