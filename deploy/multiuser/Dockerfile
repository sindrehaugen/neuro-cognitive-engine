# NCE application image (RQ worker + optional webhook command overrides).
# Build from repo root:
#   docker compose -f deploy/multiuser/docker-compose.yml build
#
# Multi-stage: compile wheels without dev headers in final layer; non-root runtime;
# pinned base tag (not :latest). HEALTHCHECK is process-local (config sanity only).

FROM python:3.12-slim-bookworm AS builder

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential git \
    && rm -rf /var/lib/apt/lists/*

# requirements.lock, NOT requirements.txt. requirements.txt is 68 `>=` floors with zero
# exact pins, so building from it re-resolves every dependency against PyPI at build time:
# the same commit produces a different image tomorrow, and a bad or compromised upstream
# release ships with no PR, no diff and no review. The lock is exact pins, held in step by
# ci.yml's blocking "Verify requirements.lock is current" gate.
#
# The lock MUST be compiled under this image's Python (3.12). A 3.11-compiled lock cannot
# represent this runtime: numpy 2.5.3 and scipy 1.18.1 declare Requires-Python >=3.12, so a
# 3.11 resolve silently caps them at 2.4.6 / 1.17.1. See ci.yml's verify step and
# .github/workflows/refresh-lock.yml -- those two and this base image move together.
COPY requirements.lock deploy/requirements-verticals.lock ./
RUN pip install --no-cache-dir "setuptools>=84.2.1" wheel \
    && pip wheel --no-cache-dir -r requirements.lock -w /wheels \
    && pip wheel --no-cache-dir --no-deps -r requirements-verticals.lock -w /wheels

COPY pyproject.toml README.md ./
COPY nce ./nce
RUN pip wheel --no-cache-dir --no-deps --no-build-isolation . -w /core-wheel


FROM python:3.12-slim-bookworm AS runtime

WORKDIR /app

# Install OCR/PDF runtime binaries + matching PostgreSQL 16 client tools (zstd pg_dump/pg_restore)
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr poppler-utils ca-certificates curl gnupg \
    && install -d /etc/apt/keyrings \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /etc/apt/keyrings/postgresql.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/postgresql.gpg] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-16 \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies from wheels
COPY --from=builder /wheels /wheels
COPY --from=builder /core-wheel /core-wheel
COPY requirements.lock deploy/requirements-verticals.lock ./

RUN pip install --no-cache-dir -r requirements.lock --no-index --find-links=/wheels \
    && pip install --no-cache-dir --no-deps --no-index --find-links=/wheels -r requirements-verticals.lock \
    && pip install --no-cache-dir --no-deps --no-index /core-wheel/*.whl \
    # Download spaCy model required for Graphify (Phase 2) with exact pinned 3.8.0 wheel matching spacy==3.8.18
    && pip install --no-cache-dir --no-deps https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl \
    # Create fixed non-root user/group 10001:10001
    && groupadd --system --gid 10001 nce \
    && useradd --system --uid 10001 --gid 10001 --create-home nce

# Copy application code
COPY nce ./nce
COPY start_worker.py health_probe.py ./
COPY admin_server.py mcp_gateway_server.py server.py ./
COPY admin ./admin
COPY deploy/multiuser/entrypoint.sh ./entrypoint.sh
COPY deploy/plugins /tmp/nce-plugin-wheels

# Install any optional pre-built plugin wheels staged in deploy/plugins/;
# keep /app code root:root owned (read-only to the non-root runtime user).
RUN set -e; \
    if ls /tmp/nce-plugin-wheels/*.whl >/dev/null 2>&1; then \
        pip install --no-cache-dir --no-index --find-links=/wheels --find-links=/core-wheel --find-links=/tmp/nce-plugin-wheels /tmp/nce-plugin-wheels/*.whl; \
    fi \
    && rm -rf /wheels /core-wheel /tmp/nce-plugin-wheels \
    && mkdir -p /var/lib/nce/backups \
    && chown -R 10001:10001 /var/lib/nce/backups \
    && chmod 0755 /app/entrypoint.sh

USER 10001:10001

ENV XDG_CACHE_HOME=/tmp
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Build stamp (fix recipe #2). Read back by nce/build_info.py so a version-skew
# failure can name the image instead of leaving the operator to guess which
# checkout is running. Both default to unset -> "unknown"; never load-bearing.
ARG NCE_GIT_SHA=
ARG NCE_BUILD_TIME=
ENV NCE_GIT_SHA=${NCE_GIT_SHA}
ENV NCE_BUILD_TIME=${NCE_BUILD_TIME}

# Robust healthcheck verifying backend connectivity
HEALTHCHECK --interval=60s --timeout=15s --start-period=90s --retries=3 \
    CMD python health_probe.py

# Boot pre-flight before the real command (fix recipe #2). `uvicorn --workers N`
# respawns a worker that dies during startup immediately and forever without
# backoff, and its supervisor never exits - so the container stays `Up`, Docker's
# restart backoff never engages, and an environmental startup failure presents as
# saturated CPU rather than an error. The entrypoint validates once and lets the
# container *exit* non-zero on failure. HEALTHCHECK bypasses ENTRYPOINT, so the
# probe above is unaffected. Set NCE_PREFLIGHT=0 to skip.
ENTRYPOINT ["/app/entrypoint.sh"]

CMD ["python", "start_worker.py"]
