"""
nce/vertical_modules/diagnostics/mcp_handlers.py
================================================
Async MCP tool handlers for the Diagnostic Log Digestion Engine.

These follow the standard ``async (engine, arguments) -> str`` signature
(see ``nce/vertical_modules/dynamics365/mcp_handlers.py``).  Registry wiring is
**Batch 77** — do NOT register these here.

Handlers
--------
* ``handle_diag_ingest_bundle``   — mutation: mint a tenant-prefixed presigned
  PUT URL + register a ``PENDING`` ``diag_ingestions`` row.
* ``handle_diag_commit_bundle``   — mutation: enqueue ``process_diag_bundle`` on
  the ``diag_ingest`` RQ lane (by string task path).
* ``handle_diag_digest_status``   — read-only: ingestion status for an ingest_id.
* ``handle_diag_device_health``   — read-only: latest device-health rollup.
* ``handle_diag_list_anomalies``  — read-only: anomalies for an ingestion.

Security
--------
* All read-only handlers run inside ``scoped_pg_session`` (RLS enforced).
* Presigned URLs are tenant-prefixed (``{namespace_id}/...``) and bounded to a
  PUT method by ``generate_secure_presigned_url``.
* No secrets, raw bundle bytes, or PII are logged or returned.
* Every handler refuses cleanly (error JSON, no exception) when the engine
  feature flag ``cfg.NCE_DIAG_ENABLED`` is false.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.diagnostics.mcp_handlers")

# Task lives in Batch 75 (nce/tasks.py); enqueue by string path so we don't import it here.
_PROCESS_DIAG_BUNDLE_TASK = "nce.tasks.process_diag_bundle"

# Allowed ingestion sources (mirrors the CHECK constraint in migration 025).
_ALLOWED_SOURCES = ("upload", "api", "ticketing")


def _disabled_error() -> str:
    """Uniform clean-rejection payload when the diagnostics engine is off."""
    return json.dumps(
        {"error": "Diagnostic Log Digestion Engine is disabled (NCE_DIAG_ENABLED=false)."}
    )


def _diag_enabled() -> bool:
    """Return whether the diagnostics feature flag is enabled."""
    from nce.config import cfg

    return bool(cfg.NCE_DIAG_ENABLED)


def _derive_ingest_id(landing_uri: str, etag_or_uuid: str) -> str:
    """Deterministic ingest_id = sha256(landing_uri + etag-or-uuid) hexdigest."""
    return hashlib.sha256(f"{landing_uri}{etag_or_uuid}".encode()).hexdigest()


async def handle_diag_ingest_bundle(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """
    Begin a diagnostic-bundle ingestion: mint a presigned PUT URL and register
    a ``PENDING`` ``diag_ingestions`` row.

    Arguments
    ---------
    namespace_id   : str  — required
    vendor_profile : str  — required (validated against the profile registry)
    device_slug    : str  — required
    object_name    : str  — bundle file name (extension validated by storage layer)
    source         : str  — 'upload' | 'api' | 'ticketing' (default 'upload')
    etag           : str  — optional client-supplied etag for deterministic id

    Returns a JSON string with the presigned PUT URL, ``ingest_id`` and the
    tenant-prefixed ``landing_uri``.
    """
    if not _diag_enabled():
        return _disabled_error()

    namespace_id = str(arguments.get("namespace_id", "")).strip()
    vendor_profile = str(arguments.get("vendor_profile", "")).strip()
    device_slug = str(arguments.get("device_slug", "")).strip()
    object_name = str(arguments.get("object_name", "")).strip()
    source = str(arguments.get("source", "upload")).strip().lower()
    etag = str(arguments.get("etag", "")).strip()

    if not namespace_id or not vendor_profile or not device_slug or not object_name:
        return json.dumps(
            {"error": ("namespace_id, vendor_profile, device_slug and object_name are required")}
        )
    if source not in _ALLOWED_SOURCES:
        return json.dumps({"error": f"source must be one of {list(_ALLOWED_SOURCES)}"})

    try:
        from nce.config import cfg
        from nce.db_utils import scoped_pg_session
        from nce.storage import generate_secure_presigned_url
        from nce.vertical_modules.diagnostics.profiles import get_profile

        # Validate vendor (registry falls back to 'generic' — reject silent fallback
        # so an unknown vendor does not get mis-profiled).
        resolved = get_profile(vendor_profile)
        if resolved.name != vendor_profile and vendor_profile != "generic":
            return json.dumps({"error": f"unknown vendor_profile: {vendor_profile}"})

        # Tenant-prefixed object path: "{namespace_id}/diag/{device_slug}/{object_name}".
        ns_lower = namespace_id.lower()
        safe_object = object_name.lstrip("/")
        landing_object = f"{ns_lower}/diag/{device_slug}/{safe_object}"

        bucket = cfg.NCE_DIAG_LANDING_BUCKET

        # Ensure the landing bucket + lifecycle policy exist (idempotent).
        ensure_landing = _ensure_landing_bucket
        await _run_blocking(ensure_landing, engine.minio_client, bucket)

        presigned_put = await _run_blocking(
            generate_secure_presigned_url,
            engine.minio_client,
            bucket,
            landing_object,
            namespace_id,
            "PUT",
        )

        landing_uri = f"s3://{bucket}/{landing_object}"
        # Deterministic id from landing_uri + etag (or a fresh uuid when no etag).
        ingest_id = _derive_ingest_id(landing_uri, etag or str(uuid.uuid4()))

        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            await conn.execute(
                """
                INSERT INTO diag_ingestions (
                    namespace_id, ingest_id, source, vendor_profile,
                    device_slug, landing_uri, status
                )
                VALUES ($1::uuid, $2, $3, $4, $5, $6, 'PENDING')
                ON CONFLICT (namespace_id, ingest_id) DO NOTHING
                """,
                namespace_id,
                ingest_id,
                source,
                vendor_profile,
                device_slug,
                landing_uri,
            )

        return json.dumps(
            {
                "ingest_id": ingest_id,
                "status": "PENDING",
                "upload_url": presigned_put,
                "landing_uri": landing_uri,
                "vendor_profile": vendor_profile,
                "device_slug": device_slug,
            }
        )

    except Exception as exc:
        # Never leak raw paths/credentials — log namespace + ingest intent only.
        log.exception(
            "handle_diag_ingest_bundle failed namespace=%s device=%s",
            namespace_id,
            device_slug,
        )
        return json.dumps({"error": str(exc)})


async def handle_diag_commit_bundle(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """
    Commit an uploaded bundle: enqueue ``process_diag_bundle`` on the
    ``diag_ingest`` RQ lane.

    The task itself is Batch 75; it is enqueued by string path so we don't import
    it here.  Heavy bundle work lands on the isolated ``diag_ingest`` lane so it
    never starves ``high_priority`` / ``batch_processing``.

    The ``landing_uri`` / ``vendor_profile`` / ``device_slug`` the worker needs
    are looked up from the ``diag_ingestions`` row registered at ingest time
    (keyed by ``(namespace_id, ingest_id)``) so the enqueue kwargs match the
    ``process_diag_bundle`` task signature exactly.

    Arguments
    ---------
    namespace_id : str  — required
    ingest_id    : str  — required (deterministic id from ingest_bundle)
    """
    if not _diag_enabled():
        return _disabled_error()

    namespace_id = str(arguments.get("namespace_id", "")).strip()
    ingest_id = str(arguments.get("ingest_id", "")).strip()

    if not namespace_id or not ingest_id:
        return json.dumps({"error": "namespace_id and ingest_id are required"})

    try:
        from nce.config import cfg
        from nce.db_utils import scoped_pg_session
        from nce.extractors.dispatch import get_diag_queue
        from nce.observability import enqueue_traced

        # The worker task needs landing_uri / vendor_profile / device_slug, which
        # were persisted on the diag_ingestions row at ingest time.  Look them up
        # (RLS-scoped) so the enqueue kwargs match the process_diag_bundle signature.
        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            row = await conn.fetchrow(
                """
                SELECT landing_uri, vendor_profile, device_slug, status
                FROM diag_ingestions
                WHERE namespace_id = $1::uuid AND ingest_id = $2
                """,
                namespace_id,
                ingest_id,
            )

        if row is None:
            return json.dumps({"error": f"unknown ingest_id {ingest_id}"})

        if str(row["status"]).upper() == "DIGESTED":
            return json.dumps(
                {
                    "ingest_id": ingest_id,
                    "status": "DIGESTED",
                    "note": "already digested; not re-enqueued",
                }
            )

        queue = get_diag_queue(engine.redis_sync_client)
        job = enqueue_traced(
            queue,
            _PROCESS_DIAG_BUNDLE_TASK,
            kwargs={
                "ingest_id": ingest_id,
                "namespace_id": namespace_id,
                "landing_uri": row["landing_uri"],
                "vendor_profile": row["vendor_profile"],
                "device_slug": row["device_slug"],
            },
            job_timeout=cfg.NCE_DIAG_JOB_TIMEOUT_MIN * 60,
        )

        job_id = getattr(job, "id", None)
        return json.dumps(
            {
                "ingest_id": ingest_id,
                "status": "PROCESSING",
                "job_id": job_id,
                "lane": "diag_ingest",
            }
        )

    except Exception as exc:
        log.exception(
            "handle_diag_commit_bundle failed namespace=%s ingest_id=%s",
            namespace_id,
            ingest_id,
        )
        return json.dumps({"error": str(exc)})


async def handle_diag_digest_status(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """
    Read-only: return the ingestion status row(s) for a namespace.

    Arguments
    ---------
    namespace_id : str  — required
    ingest_id    : str  — optional; when omitted returns the most recent rows
    limit        : int  — max rows (default 20, capped at 200)
    """
    if not _diag_enabled():
        return _disabled_error()

    namespace_id = str(arguments.get("namespace_id", "")).strip()
    ingest_id = str(arguments.get("ingest_id", "")).strip()
    if not namespace_id:
        return json.dumps({"error": "namespace_id is required"})

    try:
        limit = int(arguments.get("limit", 20) or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 200))

    try:
        from nce.db_utils import scoped_pg_session

        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            if ingest_id:
                rows = await conn.fetch(
                    """
                    SELECT ingest_id, source, vendor_profile, device_slug,
                           status, bytes, processed_lines, anomaly_count,
                           created_at, updated_at
                    FROM diag_ingestions
                    WHERE namespace_id = $1::uuid AND ingest_id = $2
                    ORDER BY created_at DESC
                    LIMIT $3
                    """,
                    namespace_id,
                    ingest_id,
                    limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT ingest_id, source, vendor_profile, device_slug,
                           status, bytes, processed_lines, anomaly_count,
                           created_at, updated_at
                    FROM diag_ingestions
                    WHERE namespace_id = $1::uuid
                    ORDER BY created_at DESC
                    LIMIT $2
                    """,
                    namespace_id,
                    limit,
                )

        ingestions = [dict(r) for r in rows]
        return json.dumps(
            {
                "namespace_id": namespace_id,
                "count": len(ingestions),
                "ingestions": ingestions,
            },
            default=str,
        )

    except Exception as exc:
        log.exception("handle_diag_digest_status failed namespace=%s", namespace_id)
        return json.dumps({"error": str(exc)})


async def handle_diag_device_health(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """
    Read-only: latest per-device health rollup for a namespace.

    Arguments
    ---------
    namespace_id : str  — required
    device_slug  : str  — optional; filter to a single device
    limit        : int  — max rows (default 50, capped at 500)
    """
    if not _diag_enabled():
        return _disabled_error()

    namespace_id = str(arguments.get("namespace_id", "")).strip()
    device_slug = str(arguments.get("device_slug", "")).strip()
    if not namespace_id:
        return json.dumps({"error": "namespace_id is required"})

    try:
        limit = int(arguments.get("limit", 50) or 50)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 500))

    try:
        from nce.db_utils import scoped_pg_session

        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            if device_slug:
                rows = await conn.fetch(
                    """
                    SELECT device_slug, health_state, top_anomaly_type,
                           anomaly_score, last_ingestion_id, last_seen_at
                    FROM device_health_rollup
                    WHERE namespace_id = $1::uuid AND device_slug = $2
                    ORDER BY last_seen_at DESC NULLS LAST
                    LIMIT $3
                    """,
                    namespace_id,
                    device_slug,
                    limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT device_slug, health_state, top_anomaly_type,
                           anomaly_score, last_ingestion_id, last_seen_at
                    FROM device_health_rollup
                    WHERE namespace_id = $1::uuid
                    ORDER BY last_seen_at DESC NULLS LAST
                    LIMIT $2
                    """,
                    namespace_id,
                    limit,
                )

        devices = [dict(r) for r in rows]
        return json.dumps(
            {
                "namespace_id": namespace_id,
                "count": len(devices),
                "devices": devices,
            },
            default=str,
        )

    except Exception as exc:
        log.exception("handle_diag_device_health failed namespace=%s", namespace_id)
        return json.dumps({"error": str(exc)})


async def handle_diag_list_anomalies(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """
    Read-only: list anomalies, optionally scoped to one ingestion or device.

    Arguments
    ---------
    namespace_id : str  — required
    ingest_id    : str  — optional; filter to one ingestion (joined via id)
    device_slug  : str  — optional; filter to one device
    limit        : int  — max rows (default 50, capped at 500)
    """
    if not _diag_enabled():
        return _disabled_error()

    namespace_id = str(arguments.get("namespace_id", "")).strip()
    ingest_id = str(arguments.get("ingest_id", "")).strip()
    device_slug = str(arguments.get("device_slug", "")).strip()
    if not namespace_id:
        return json.dumps({"error": "namespace_id is required"})

    try:
        limit = int(arguments.get("limit", 50) or 50)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 500))

    try:
        from nce.db_utils import scoped_pg_session

        where = ["a.namespace_id = $1::uuid"]
        args: list[Any] = [namespace_id]
        i = 2
        if ingest_id:
            where.append(f"i.ingest_id = ${i}")
            args.append(ingest_id)
            i += 1
        if device_slug:
            where.append(f"a.device_slug = ${i}")
            args.append(device_slug)
            i += 1

        where_sql = " AND ".join(where)
        query = f"""
            SELECT a.anomaly_type, a.device_slug, a.severity, a.first_line,
                   a.occurrences, a.sample, a.window_start, a.window_end,
                   a.created_at
            FROM diag_anomalies a
            JOIN diag_ingestions i
              ON i.id = a.ingestion_id AND i.namespace_id = a.namespace_id
            WHERE {where_sql}
            ORDER BY a.severity ASC, a.created_at DESC
            LIMIT ${i}
        """  # noqa: S608 — placeholders are positional; only param-count is interpolated

        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            rows = await conn.fetch(query, *args, limit)

        anomalies = [dict(r) for r in rows]
        return json.dumps(
            {
                "namespace_id": namespace_id,
                "count": len(anomalies),
                "anomalies": anomalies,
            },
            default=str,
        )

    except Exception as exc:
        log.exception("handle_diag_list_anomalies failed namespace=%s", namespace_id)
        return json.dumps({"error": str(exc)})


# ── Internal helpers ────────────────────────────────────────────────────────


async def _run_blocking(func: Any, *args: Any) -> Any:
    """Run a blocking MinIO call off the event loop."""
    import asyncio

    return await asyncio.to_thread(func, *args)


def _ensure_landing_bucket(minio_client: Any, bucket: str) -> None:
    """Thin wrapper so ``ensure_landing_bucket`` can be patched in unit tests."""
    from nce.storage import ensure_landing_bucket

    ensure_landing_bucket(minio_client, bucket)
