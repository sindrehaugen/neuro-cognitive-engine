"""Batch 75 — Diagnostics ingestion worker (Stream-and-Reduce, bounded memory).

This is the async body behind the ``process_diag_bundle`` RQ task (registered in
:mod:`nce.tasks`).  It glues the previously-built diagnostics seams into a single
bounded-memory, idempotent, poison-safe pipeline:

* Batch 70 :func:`get_profile`            — vendor → :class:`LogProfile`;
* Batch 71 :func:`stream_entries` / :func:`digest_stream` / :class:`PoisonBundleError`
                                            — flat-memory archive walk + fold;
* Batch 72 :func:`resolve_device_context` — best-effort NetBox enrichment;
* Batch 74 :class:`CentralSink`           — RLS-scoped cognitive-layer landing.

Memory contract
---------------
The landing object is **streamed to a temp file on disk** (never read whole into
RAM), then walked member-by-member by :func:`stream_entries`.  Peak resident
memory is therefore independent of bundle size.  The temp directory is ALWAYS
removed via ``try/finally`` even on failure.

Idempotency
-----------
``diag_ingestions`` carries a status lifecycle ``PENDING → PROCESSING →
DIGESTED | FAILED``.  A row already at ``DIGESTED`` short-circuits to a no-op so
an at-least-once RQ redelivery (or a manual re-enqueue of the same ``ingest_id``)
never double-writes the cognitive layers.

Error policy (see :func:`_diag_async`)
--------------------------------------
* **Non-retryable** — :class:`PoisonBundleError` (zip bomb / corrupt archive) or
  an unknown vendor profile.  These breach identically on every retry, so the row
  is marked ``FAILED``, the payload is dead-lettered (ids + reason only, never raw
  bundle bytes / PII), the Redis attempt counter is cleared, and the function
  RETURNS (does NOT re-raise) — breaking the infinite-retry spin-loop.
* **Transient** — download / Postgres / Mongo / object-store hiccups.  These are
  re-raised so the ``process_diag_bundle`` wrapper's ``_check_poison_pill`` governs
  retry-vs-DLQ via the shared attempt counter.

WORM / RLS / secrets
--------------------
All tenant writes go through :class:`CentralSink` (already RLS-scoped) and the
``diag_ingestions`` status updates run inside ``scoped_pg_session``.  No raw bundle
bytes or PII are ever logged, emitted, or placed in the DLQ payload.
``NCE_MASTER_KEY`` is never handled here.
"""

from __future__ import annotations

import gzip
import logging
import os
import shutil
import tarfile
import tempfile
import zipfile
from typing import TYPE_CHECKING, Any

from nce.config import cfg
from nce.db_utils import scoped_pg_session
from nce.dead_letter_queue import _clear_attempt, store_dead_letter
from nce.vertical_modules.diagnostics.digest_writer import CentralSink
from nce.vertical_modules.diagnostics.enrichment import resolve_device_context
from nce.vertical_modules.diagnostics.profiles import get_profile
from nce.vertical_modules.diagnostics.streaming import (
    PoisonBundleError,
    digest_stream,
    stream_entries,
)

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.diagnostics.worker")

_BYTES_PER_MB = 1024 * 1024

# Free-disk headroom we demand before downloading: the bundle is bounded at
# ``NCE_DIAG_MAX_BUNDLE_MB``; require that much free plus a small fixed margin so a
# nearly-full volume fails the pre-flight cleanly rather than mid-write.
_DISK_SAFETY_MARGIN_BYTES = 64 * _BYTES_PER_MB

# Task name shared with nce.tasks (DLQ rows + attempt-counter convention).
_TASK_NAME = "process_diag_bundle"


class _NonRetryableBundleError(Exception):
    """Internal marker: a permanent defect that must be dead-lettered, not retried.

    Wraps the human-readable reason for the DLQ payload.  Distinct from
    :class:`PoisonBundleError` (raised by the streaming core) so the worker can
    funnel BOTH plus "unknown profile" through one non-retryable arm.
    """


async def _diag_async(
    *,
    ingest_id: str,
    namespace_id: str,
    landing_uri: str,
    vendor_profile: str,
    device_slug: str | None = None,
) -> dict[str, Any]:
    """Run the Stream-and-Reduce pipeline for one diagnostic bundle.

    See the module docstring for the full contract.  Returns a small status dict
    describing the outcome (``digested`` / ``noop`` / ``dead_lettered``); never
    leaks raw content.

    Raises:
        Exception: Re-raised for *transient* failures so the RQ wrapper's
            poison-pill governor can decide retry-vs-DLQ.  Non-retryable defects
            are dead-lettered internally and reported via the return value.
    """
    job_id = _current_job_id()

    engine = _build_engine()
    await engine.connect()
    try:
        # (a) IDEMPOTENCY short-circuit — already DIGESTED ⇒ no-op.
        status = await _fetch_status(engine, namespace_id, ingest_id)
        if status == "DIGESTED":
            log.info(
                "[DiagWorker] ingest_id already DIGESTED — no-op (namespace=%s)",
                namespace_id,
            )
            return {"status": "noop", "ingest_id": ingest_id, "reason": "already_digested"}

        # Mark PROCESSING so concurrent/duplicate dequeues observe in-flight state.
        await _set_status(engine, namespace_id, ingest_id, "PROCESSING")

        try:
            result = await _run_pipeline(
                engine,
                ingest_id=ingest_id,
                namespace_id=namespace_id,
                landing_uri=landing_uri,
                vendor_profile=vendor_profile,
                device_slug=device_slug,
            )
        except (PoisonBundleError, _NonRetryableBundleError) as exc:
            # ── NON-retryable arm: mark FAILED, dead-letter (ids+reason only),
            #    clear the attempt counter, and RETURN (do NOT re-raise).
            reason = f"{type(exc).__name__}: {exc!s}"
            log.warning(
                "[DiagWorker] non-retryable defect for ingest_id (namespace=%s): %s",
                namespace_id,
                reason,
            )
            await _set_status(engine, namespace_id, ingest_id, "FAILED")
            await _dead_letter(
                engine,
                job_id=job_id,
                ingest_id=ingest_id,
                namespace_id=namespace_id,
                vendor_profile=vendor_profile,
                device_slug=device_slug,
                reason=reason,
            )
            _clear_attempt_counter(job_id)
            return {
                "status": "dead_lettered",
                "ingest_id": ingest_id,
                "reason": "non_retryable",
            }

        # ── Success: clear the attempt counter (mirrors nce.tasks success path).
        _clear_attempt_counter(job_id)
        return {"status": "digested", "ingest_id": ingest_id, **result}

    finally:
        await engine.disconnect()


async def _run_pipeline(
    engine: NCEEngine,
    *,
    ingest_id: str,
    namespace_id: str,
    landing_uri: str,
    vendor_profile: str,
    device_slug: str | None,
) -> dict[str, Any]:
    """Download → stream → digest → enrich → land → mark DIGESTED.

    Raises :class:`PoisonBundleError` / :class:`_NonRetryableBundleError` for
    permanent defects (caller dead-letters); re-raises every other exception as a
    transient failure (caller lets the poison-pill governor decide).
    """
    # Reject an unknown vendor profile up-front (non-retryable): the registry
    # silently falls back to "generic", so detect the mismatch explicitly.
    profile = get_profile(vendor_profile)
    if profile.name != vendor_profile and vendor_profile != "generic":
        raise _NonRetryableBundleError(f"unknown vendor_profile: {vendor_profile!r}")

    bucket, object_name = _parse_landing_uri(landing_uri)

    # (b) PRE-FLIGHT size guard: stat the object; reject oversize before download.
    max_bytes = cfg.NCE_DIAG_MAX_BUNDLE_MB * _BYTES_PER_MB
    object_size = await _stat_object_size(engine, bucket, object_name)
    if object_size is not None and object_size > max_bytes:
        raise _NonRetryableBundleError(
            f"bundle object exceeds NCE_DIAG_MAX_BUNDLE_MB ({object_size} > {max_bytes} bytes)"
        )

    tmpdir = tempfile.mkdtemp(dir=cfg.NCE_DIAG_TMPDIR or None, prefix="nce-diag-")
    try:
        # (b cont.) PRE-FLIGHT disk guard: ensure enough free space for the bundle.
        free = shutil.disk_usage(tmpdir).free
        needed = (object_size or max_bytes) + _DISK_SAFETY_MARGIN_BYTES
        if free < needed:
            # Low disk is environmental (transient): re-raise so a healthier worker
            # / a later retry can succeed rather than permanently dead-lettering.
            raise OSError(
                f"insufficient free disk for bundle download: free={free} needed={needed}"
            )

        # (c) STREAM the landing object to disk (NEVER whole-file into memory).
        local_path = os.path.join(tmpdir, "bundle.bin")
        downloaded_bytes = await _download_to_disk(engine, bucket, object_name, local_path)

        # (d) STREAM-and-REDUCE with bounded memory.
        try:
            digest = digest_stream(
                profile,
                stream_entries(
                    local_path,
                    max_uncompressed_bytes=max_bytes,
                    max_entries=cfg.NCE_DIAG_MAX_ANOMALIES * 10_000 or 1_000_000,
                ),
            )
        except PoisonBundleError:
            raise  # non-retryable — handled by the caller's arm
        except (
            zipfile.BadZipFile,
            tarfile.TarError,
            gzip.BadGzipFile,
            ValueError,
            EOFError,
        ) as exc:
            # A corrupt/unreadable archive is a permanent defect for THIS bundle:
            # it will fail identically on every retry, so dead-letter rather than
            # spin. (OSError is deliberately NOT swallowed here — a read error on
            # the temp file is environmental/transient and must re-raise.)
            raise _NonRetryableBundleError(f"corrupt or unreadable archive: {exc}") from exc

        # (e) Best-effort NetBox enrichment (a missing device is non-fatal).
        device_ctx = await _resolve_context(device_slug)

        # (f) Land the digest across Mongo + the RLS-scoped PG cognitive layers.
        #     engine.memory is always set after a successful connect().
        assert engine.memory is not None, "engine.memory unset after connect()"
        sink = CentralSink(engine.memory)
        digest_payload_ref = await sink.write(digest, device_ctx, ingest_id, namespace_id)

        # (g) Mark the row DIGESTED with the bounded stats (attempt counter is
        #     cleared by the caller on the success path).
        await _mark_digested(
            engine,
            namespace_id=namespace_id,
            ingest_id=ingest_id,
            object_bytes=downloaded_bytes,
            processed_lines=digest.processed_lines,
            anomaly_count=len(digest.anomalies),
            digest_payload_ref=digest_payload_ref,
        )

        return {
            "processed_lines": digest.processed_lines,
            "anomaly_count": len(digest.anomalies),
            "digest_payload_ref": digest_payload_ref,
        }
    finally:
        # Temp files ALWAYS cleaned, even on failure (no raw content left behind).
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Object-store helpers (blocking MinIO calls run off the event loop)
# ---------------------------------------------------------------------------


def _parse_landing_uri(landing_uri: str) -> tuple[str, str]:
    """Split a ``s3://bucket/object/key`` (or ``bucket/object/key``) landing URI.

    Returns ``(bucket, object_name)``.  Raises a non-retryable error for a
    malformed URI (it will never become valid on retry).
    """
    raw = landing_uri.strip()
    if raw.startswith("s3://"):
        raw = raw[len("s3://") :]
    if "/" not in raw:
        raise _NonRetryableBundleError(f"malformed landing_uri: {landing_uri!r}")
    bucket, object_name = raw.split("/", 1)
    if not bucket or not object_name:
        raise _NonRetryableBundleError(f"malformed landing_uri: {landing_uri!r}")
    return bucket, object_name


async def _stat_object_size(engine: NCEEngine, bucket: str, object_name: str) -> int | None:
    """Return the object's size in bytes via ``stat_object``; ``None`` if unknown.

    Network/stat failures are transient (re-raised to the caller).
    """
    import asyncio

    def _stat() -> int | None:
        st = engine.minio_client.stat_object(bucket, object_name)
        size = getattr(st, "size", None)
        return int(size) if size is not None else None

    return await asyncio.to_thread(_stat)


async def _download_to_disk(
    engine: NCEEngine, bucket: str, object_name: str, local_path: str
) -> int:
    """Stream a MinIO object to *local_path* in fixed-size chunks; return bytes written.

    Uses ``get_object`` + chunked ``stream`` so the whole object is never held in
    memory.  Runs the blocking MinIO I/O on a worker thread.  Errors here are
    transient (re-raised) so a retry can recover from a flaky object store.
    """
    import asyncio

    chunk_size = 1024 * 1024  # 1 MiB

    def _download() -> int:
        written = 0
        response = engine.minio_client.get_object(bucket, object_name)
        try:
            with open(local_path, "wb") as fh:
                for chunk in response.stream(chunk_size):
                    fh.write(chunk)
                    written += len(chunk)
        finally:
            response.close()
            response.release_conn()
        return written

    return await asyncio.to_thread(_download)


async def _resolve_context(device_slug: str | None) -> dict[str, Any]:
    """Best-effort NetBox context for *device_slug*.

    When NetBox is not configured (no URL/token) or the lookup fails, return the
    non-resolved echo shape so the pipeline keeps landing for un-inventoried
    devices — enrichment is additive, never a hard dependency.
    """
    if not cfg.NCE_NETBOX_URL or not cfg.NCE_NETBOX_TOKEN:
        return {
            "device_slug": device_slug,
            "site": None,
            "location": None,
            "room": None,
            "tenant": None,
            "resolved": False,
        }

    try:
        from nce.vertical_modules.netbox.graphql_activation import NetBoxGraphQLClient

        client = NetBoxGraphQLClient(cfg.NCE_NETBOX_URL, cfg.NCE_NETBOX_TOKEN)
        return await resolve_device_context(client, slug=device_slug)
    except Exception as exc:  # noqa: BLE001 — enrichment is strictly best-effort
        log.warning(
            "[DiagWorker] NetBox enrichment failed (continuing un-enriched): %s",
            exc,
        )
        return {
            "device_slug": device_slug,
            "site": None,
            "location": None,
            "room": None,
            "tenant": None,
            "resolved": False,
        }


# ---------------------------------------------------------------------------
# diag_ingestions status helpers (RLS-scoped)
# ---------------------------------------------------------------------------


async def _fetch_status(engine: NCEEngine, namespace_id: str, ingest_id: str) -> str | None:
    """Return the current ``status`` of the ``diag_ingestions`` row, or ``None``."""
    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        return await conn.fetchval(
            "SELECT status FROM diag_ingestions WHERE namespace_id = $1::uuid AND ingest_id = $2",
            namespace_id,
            ingest_id,
        )


async def _set_status(engine: NCEEngine, namespace_id: str, ingest_id: str, status: str) -> None:
    """Set the ``diag_ingestions`` row status + ``updated_at`` (RLS-scoped)."""
    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        await conn.execute(
            "UPDATE diag_ingestions SET status = $3, updated_at = now() "
            "WHERE namespace_id = $1::uuid AND ingest_id = $2",
            namespace_id,
            ingest_id,
            status,
        )


async def _mark_digested(
    engine: NCEEngine,
    *,
    namespace_id: str,
    ingest_id: str,
    object_bytes: int,
    processed_lines: int,
    anomaly_count: int,
    digest_payload_ref: str,
) -> None:
    """Mark the row DIGESTED and record the bounded ingestion stats (RLS-scoped)."""
    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        await conn.execute(
            """
            UPDATE diag_ingestions
               SET status = 'DIGESTED',
                   bytes = $3,
                   processed_lines = $4,
                   anomaly_count = $5,
                   digest_payload_ref = $6,
                   updated_at = now()
             WHERE namespace_id = $1::uuid AND ingest_id = $2
            """,
            namespace_id,
            ingest_id,
            object_bytes,
            processed_lines,
            anomaly_count,
            digest_payload_ref,
        )


# ---------------------------------------------------------------------------
# DLQ + attempt-counter helpers
# ---------------------------------------------------------------------------


async def _dead_letter(
    engine: NCEEngine,
    *,
    job_id: str,
    ingest_id: str,
    namespace_id: str,
    vendor_profile: str,
    device_slug: str | None,
    reason: str,
) -> None:
    """Persist a non-retryable defect to the DLQ — IDs + reason only, no raw content.

    Best-effort: a DLQ-store failure is logged but never masks the original defect
    or escalates it into a retry.
    """
    try:
        await store_dead_letter(
            engine.pg_pool,
            _TASK_NAME,
            job_id,
            {
                "ingest_id": ingest_id,
                "namespace_id": namespace_id,
                "vendor_profile": vendor_profile,
                "device_slug": device_slug,
                "reason": reason,
            },
            reason,
            # Non-retryable defects are dead-lettered on first detection.
            1,
            namespace_id=namespace_id,
        )
    except Exception:  # noqa: BLE001 — DLQ persistence must not raise into the worker
        log.critical(
            "[DiagWorker] CRITICAL — could not persist DLQ entry for ingest_id "
            "(namespace=%s, job=%s)",
            namespace_id,
            job_id,
        )


def _clear_attempt_counter(job_id: str) -> None:
    """Clear the Redis attempt counter for *job_id* (mirrors nce.tasks)."""
    try:
        from nce.tasks import _get_redis

        _clear_attempt(_get_redis(), job_id)
    except Exception:  # noqa: BLE001 — counter cleanup is best-effort
        log.debug("[DiagWorker] could not clear attempt counter for job %s", job_id)


def _current_job_id() -> str:
    """Return the current RQ job id (reuses nce.tasks' resolver), or 'unknown'."""
    try:
        from nce.tasks import _get_job_id

        return _get_job_id()
    except Exception:  # noqa: BLE001
        return "unknown"


def _build_engine() -> NCEEngine:
    """Construct an NCEEngine (own pools/clients), mirroring nce.tasks workers."""
    from nce.orchestrator import NCEEngine

    return NCEEngine()
