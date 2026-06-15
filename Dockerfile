# syntax=docker/dockerfile:1
#
# Swing-screener job image. Runs as an Azure Container Apps Job: a UTC cron
# fires the ENTRYPOINT gate (swing_screener.ops.eastern_gate), which decides at
# US-Eastern wall time whether to exec the real CLI (CMD, overridden per job).
#
# NOT built in CI -- a manual `docker build` smoke runs at deploy time. Keep it
# correct rather than clever.

# python:3.12-slim (Debian 12 "bookworm"). Digest-pinned for reproducible builds;
# bump the digest deliberately when refreshing the base image.
FROM python:3.12-slim@sha256:d764629ce0ddd8c71fd371e9901efb324a95789d2315a47db7e4d27e78f1b0e9

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ---------------------------------------------------------------------------
# One apt layer: native deps for pyodbc (unixODBC), matplotlib/mplfinance and
# reportlab (libfreetype6, libpng16-16, fontconfig), plus Microsoft ODBC Driver
# 18 for SQL Server (Azure SQL).
#
# KNOWN FRAGILITY: Microsoft periodically rotates the packages.microsoft.com
# signing key and/or the repo URL, which breaks THIS layer's `apt-get install
# msodbcsql18`. If a CD `docker build` fails here, refresh the key/repo lines
# below (see https://learn.microsoft.com/sql/connect/odbc/linux-mac/). The
# reliable fallback is server-side `az acr build`, which is less sensitive to
# local key drift.
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg2 apt-transport-https ca-certificates \
        unixodbc unixodbc-dev \
        libfreetype6 libpng16-16 fontconfig \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list -o /etc/apt/sources.list.d/mssql-release.list \
    && sed -i 's|https://packages|[signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages|' /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

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
