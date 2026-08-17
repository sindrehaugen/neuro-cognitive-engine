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

COPY requirements.txt .
RUN pip wheel --no-cache-dir -r requirements.txt -w /wheels


FROM python:3.12-slim-bookworm AS runtime

WORKDIR /app

# Install dependencies from wheels
COPY --from=builder /wheels /wheels
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt --no-index --find-links=/wheels \
    && rm -rf /wheels \
    # Download spaCy model required for Graphify (Phase 2) with pinned version
    && python -m spacy download en_core_web_sm \
    # Create non-root user
    && useradd --system --uid 10001 --create-home nce

# Copy application code
COPY nce ./nce
COPY start_worker.py health_probe.py ./
COPY admin_server.py server.py ./
COPY admin ./admin

# Ensure the app directory is owned by our non-root user
RUN chown -R nce:nce /app

USER nce

ENV XDG_CACHE_HOME=/tmp
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Robust healthcheck verifying backend connectivity
HEALTHCHECK --interval=60s --timeout=15s --start-period=90s --retries=3 \
    CMD python health_probe.py

CMD ["python", "start_worker.py"]
