# CloudNativePG operand image: PostgreSQL 16 (Debian bookworm) + TimescaleDB.
#
# Why this image exists: CNPG needs an operand image that already contains the
# timescaledb extension .so files; the stock ghcr.io/cloudnative-pg/postgresql
# image does not. The maintained community CNPG+TimescaleDB images (clevyr,
# imusmanmalik) had stalled at ts 2.19.x — too old to restore a dump from our
# live tracearr DB (timescaledb 2.25.0). A TimescaleDB pg_dump/restore requires
# the destination extension version to be >= the source, so we pin EQUAL here.
#
# Build-once-and-forget: tagged ghcr.io/mainertoo/cnpg-timescaledb:16-2.25.0,
# built via .github/workflows/build-cnpg-timescaledb.yml (workflow_dispatch only
# — no upstream-tracking cron). Renovate is told to leave the pinned CNPG_IMAGE
# alone. Rebuild only when you deliberately bump TS_VERSION / PG major.
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
    # Resolve the exact repo version string (e.g. "2.25.0~debian12") for the
    # requested upstream version so the pin survives the Debian build suffix.
    TS_PKG_VER="$(apt-cache madison "timescaledb-2-postgresql-${PG_MAJOR}" \
      | awk -v v="${TS_VERSION}" '$3 ~ ("^" v "~") || $3 == v {print $3; exit}')"; \
    test -n "${TS_PKG_VER}"; \
    echo "Installing timescaledb-2-postgresql-${PG_MAJOR}=${TS_PKG_VER}"; \
    # The main package Depends on the matching -loader package, so apt pulls in
    # timescaledb-2-loader-postgresql-${PG_MAJOR} at the same version.
    apt-get install -y --no-install-recommends \
      "timescaledb-2-postgresql-${PG_MAJOR}=${TS_PKG_VER}"; \
    # Confirm the extension control file landed in the operand's sharedir.
    test -f "/usr/share/postgresql/${PG_MAJOR}/extension/timescaledb.control"; \
    rm -rf /var/lib/apt/lists/* /etc/apt/sources.list.d/timescaledb.list

# Restore the CloudNativePG operand's non-root postgres user (uid 26).
USER 26
