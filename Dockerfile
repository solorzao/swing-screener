# syntax=docker/dockerfile:1
#
# Swing-screener job image. Runs as an Azure Container Apps Job: a UTC cron
# fires the ENTRYPOINT gate (swing_screener.ops.eastern_gate), which decides at
# US-Eastern wall time whether to exec the real CLI (CMD, overridden per job).
#
# NOT built in CI -- a manual `docker build` smoke runs at deploy time. Keep it
# correct rather than clever.

# Pinned to python:3.12-slim-BOOKWORM (Debian 12), NOT plain -slim which has
# rolled to Debian 13 "trixie" -- bookworm matches Microsoft's debian/12 ODBC
# repo below. Digest-pinned for reproducible builds; bump deliberately.
FROM python:3.12-slim-bookworm@sha256:76d4b7b6305788c6b4c6a19d6a22a3921bf802e9af4d5e1e5bd771208dba74bf

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ---------------------------------------------------------------------------
# One apt layer: native deps for pyodbc (unixODBC), matplotlib/mplfinance and
# reportlab (libfreetype6, libpng16-16, fontconfig), plus Microsoft ODBC Driver
# 18 for SQL Server (Azure SQL).
#
# The MS repo line is written DIRECTLY (single bracket with arch + signed-by)
# rather than sed-editing the downloaded prod.list: the modern prod.list already
# carries an [arch=...] group, so injecting a second [signed-by=...] bracket
# produces a malformed "URI parse" sources entry. KNOWN FRAGILITY: Microsoft
# rotates the packages.microsoft.com signing key periodically; if this layer
# fails, refresh the key (see https://learn.microsoft.com/sql/connect/odbc/linux-mac/).
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg2 apt-transport-https ca-certificates \
        unixodbc unixodbc-dev \
        libfreetype6 libpng16-16 fontconfig \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && echo "deb [arch=amd64,arm64,armhf signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Alembic migration files live here (copied below), NOT next to the installed
# package in site-packages -- the pipeline reads this to find alembic.ini/alembic.
ENV SWING_ALEMBIC_DIR=/app

# COPY exactly what the build + runtime need. pyproject has no `readme=`, so
# README.md is NOT required by the build (and is excluded via .dockerignore).
#   - pyproject.toml      : build metadata / dependency spec
#   - src/                : the package itself, incl. data/universe_seed.csv
#                           (declared as package-data so the install bundles it)
#   - alembic/, alembic.ini : the pipeline runs `alembic upgrade head` against
#                           Azure SQL, resolving these from the repo root (/app).
COPY pyproject.toml ./
COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini ./alembic.ini
#   - edge/               : the per-strategy playbooks (+ committed verdicts sidecars).
#                           The insight engine reads them at /app/edge (SWING_EDGE_DIR
#                           default); WITHOUT this COPY the deep path silently fell back
#                           and never recorded an analyst call (2026-07-01 audit).
COPY edge/ ./edge/

# Install with the Azure extra (pyodbc, azure-*, alembic). Editable is not used:
# a regular install bundles the package data (universe_seed.csv) into site-packages.
RUN pip install --no-cache-dir ".[azure]"

# Drop root: the job needs no write access to the image, only to /tmp + mounts.
RUN useradd -m appuser && chown -R appuser /app
USER appuser

# The gate is always the entrypoint; jobs override CMD per cadence, e.g.
#   ["-m","swing_screener.notify.run","--kind","weekly"].
# RUN_IF_ET_HOUR / RUN_IF_LAST_BUSINESS_DAY (env, set per job) steer the gate.
ENTRYPOINT ["python", "-m", "swing_screener.ops.eastern_gate"]
CMD ["-m", "swing_screener.pipeline.run"]
