"""
Tri-Stack Garbage Collector
Runs every hour as an async background task.
Finds MongoDB documents older than GC_ORPHAN_AGE_SECONDS with no matching payload_ref
in either PG table (memories or memories), then deletes them.
Guarantees data purity even if the Python process is hard-killed mid-transaction.

Hardening:
- PG scan is paginated (PAGE_SIZE rows at a time) — safe on million-row tables.
- Startup uses exponential backoff so a slow Docker start never crashes the GC.
- Pool size is bounded; command timeout prevents hung queries.

Batch 125 — Event Retention
Adds three bounded-retention sweeps (all tenant-scoped / WORM-safe):
  1. Partition retention: archive+DROP aged event_log partitions to MinIO — ONLY when
     fully anchored (Batch 124).  Never row-level-deletes.
  2. Contradiction purge: DELETE resolved rows older than NCE_CONTRADICTION_RETENTION_DAYS.
  3. Edge prune: DELETE kg_edges with confidence<0.15 AND age>NCE_EDGE_PRUNE_AGE_DAYS,
     but NEVER edges with change_origin='sync' (Batch 106 deterministic ground truth).
"""

import asyncio
import io
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

from nce.auth import set_namespace_context
from nce.config import cfg, redact_secrets_in_text
from nce.db_utils import resolve_worker_dsn, scoped_pg_session
from nce.redis_lock import acquire_lock as _acquire_redis_lock
from nce.redis_lock import release_lock as _release_redis_lock

log = logging.getLogger("nce-gc")

_GC_LOCK_KEY: str = "nce:gc:lock"
_GC_LOCK_TTL_SECONDS: int = cfg.GC_INTERVAL_SECONDS + 60


async def _acquire_gc_lock(redis_client: Any) -> str | None:
    """Try to acquire the GC distributed lock.

    Parameters
    ----------
    redis_client:
        An open ``redis.asyncio.Redis`` instance shared by the GC loop.
        The caller owns its lifetime — this function never closes it.

    Returns
    -------
    str
        The lock token (pass to :func:`_release_gc_lock`).
    None
        Lock held by another instance or Redis unavailable — skip this pass.
    """
    token = await _acquire_redis_lock(redis_client, _GC_LOCK_KEY, _GC_LOCK_TTL_SECONDS)
    if token is None:
        log.debug("GC lock held by another instance — skipping this pass.")
    return token


async def _release_gc_lock(redis_client: Any, token: str) -> None:
    """Release the GC distributed lock (compare-and-delete)."""
    await _release_redis_lock(redis_client, _GC_LOCK_KEY, token)


PAGE_SIZE = cfg.GC_PAGE_SIZE  # rows fetched per PG cursor page
MAX_CONNECT_ATTEMPTS = cfg.GC_MAX_CONNECT_ATTEMPTS
CONNECT_BASE_DELAY = cfg.GC_CONNECT_BASE_DELAY  # seconds; doubles each retry
CHUNK_DELETE_SIZE = 1000  # rows deleted per chunk to prevent table locks

# R-B reverse sweep: hard ceiling on dangling refs repaired per namespace per
# pass, so a corrupt namespace can never turn one GC tick into an unbounded
# scan/repair storm. Excess refs are surfaced via an alert and picked up next
# pass.
REVERSE_SWEEP_MAX_PER_NS = 5000


# --- Connection helpers with retry ---


async def _connect_with_retry() -> tuple[AsyncIOMotorClient, asyncpg.Pool]:
    """
    Attempt to connect to Mongo and PG with exponential backoff.
    Raises RuntimeError after MAX_CONNECT_ATTEMPTS failures.
    """
    delay = CONNECT_BASE_DELAY
    last_exc: Exception | None = None

    for attempt in range(1, MAX_CONNECT_ATTEMPTS + 1):
        mongo_client: AsyncIOMotorClient | None = None
        try:
            mongo_client = AsyncIOMotorClient(
                cfg.MONGO_URI,
                serverSelectionTimeoutMS=5_000,
            )
            # Force a real connection check
            await mongo_client.admin.command("ping")

            # R4 / VI.4: connect as the least-privilege worker principal
            # (``nce_gc`` via NCE_GC_DSN) when provisioned; falls back to the
            # app DSN (``nce_app``) when NCE_GC_DSN is unset.
            pg_pool = await asyncpg.create_pool(
                resolve_worker_dsn(),
                min_size=1,
                max_size=3,  # GC needs very few connections
                command_timeout=30,
            )
            log.info("GC connected on attempt %d.", attempt)
            return mongo_client, pg_pool

        except Exception as exc:
            last_exc = exc
            if mongo_client is not None:
                try:
                    mongo_client.close()
                except Exception:
                    pass
            log.warning(
                "GC connect attempt %d/%d failed: %s. Retrying in %.0fs.",
                attempt,
                MAX_CONNECT_ATTEMPTS,
                exc,
                delay,
            )
            if attempt < MAX_CONNECT_ATTEMPTS:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    raise RuntimeError(f"GC could not connect after {MAX_CONNECT_ATTEMPTS} attempts: {last_exc}")


# --- Paginated PG reference set builder ---


async def _fetch_pg_ref_batch(
    conn: asyncpg.Connection, table: str, last_seen_id: UUID, limit: int
) -> list[asyncpg.Record]:
    """Fetch a batch of payload_refs using keyset pagination."""
    # Keyset pagination — table name is from a hardcoded tuple, not user input
    return await conn.fetch(
        f"SELECT id, payload_ref FROM {table} "  # noqa: S608 — table is not user-controlled
        f"WHERE payload_ref IS NOT NULL AND id > $1 "
        f"ORDER BY id LIMIT $2",
        last_seen_id,
        limit,
    )


async def _fetch_pg_refs(pg_pool: asyncpg.Pool, namespaces: list[UUID]) -> set[str]:
    """
    Build the set of all known mongo_ref_ids in PG using keyset-based pagination.
    Iterates over all namespaces and sets RLS context for each so FORCE ROW LEVEL
    SECURITY does not silently return zero rows.
    """
    pg_refs: set[str] = set()
    ZERO_UUID = UUID(int=0)

    for ns_id in namespaces:
        try:
            async with pg_pool.acquire(timeout=30.0) as conn:
                async with conn.transaction():
                    await set_namespace_context(conn, ns_id)
                    for table in ("memories",):
                        last_seen_id = ZERO_UUID
                        while True:
                            rows = await _fetch_pg_ref_batch(conn, table, last_seen_id, PAGE_SIZE)
                            if not rows:
                                break
                            pg_refs.update(row["payload_ref"] for row in rows)
                            last_seen_id = rows[-1]["id"]

                            if len(rows) < PAGE_SIZE:
                                break  # last page
        except Exception as e:
            log.error("GC: Failed to fetch PG refs for namespace=%s: %s", ns_id, e)
            raise

    return pg_refs


async def _fetch_minio_refs(pg_pool: asyncpg.Pool, namespaces: list[UUID]) -> set[str]:
    """Build the set of all known MinIO object_names in PG using keyset-based pagination."""
    minio_refs: set[str] = set()
    ZERO_UUID = UUID(int=0)

    for ns_id in namespaces:
        try:
            async with pg_pool.acquire(timeout=30.0) as conn:
                async with conn.transaction():
                    await set_namespace_context(conn, ns_id)
                    for table in ("memories",):
                        last_seen_id = ZERO_UUID
                        while True:
                            rows = await conn.fetch(
                                f"SELECT id, metadata FROM {table} "
                                f"WHERE id > $1 "
                                f"ORDER BY id LIMIT $2",
                                last_seen_id,
                                PAGE_SIZE,
                            )
                            if not rows:
                                break
                            for row in rows:
                                meta = row["metadata"]
                                if meta:
                                    meta_dict = meta if isinstance(meta, dict) else json.loads(meta)
                                    obj_name = meta_dict.get("object_name")
                                    if obj_name:
                                        minio_refs.add(obj_name)
                            last_seen_id = rows[-1]["id"]

                            if len(rows) < PAGE_SIZE:
                                break  # last page
        except Exception as e:
            log.error("GC: Failed to fetch MinIO refs for namespace=%s: %s", ns_id, e)
            raise

    return minio_refs


# --- Namespace-aware maintenance helpers ---
# These helpers must set namespace context so RLS policies allow the
# cross-table orphan detection queries to see each namespace's data.


async def _fetch_all_namespaces(pg_pool: asyncpg.Pool) -> list[UUID]:
    """Fetch all namespace UUIDs from the namespaces table."""
    async with pg_pool.acquire(timeout=10.0) as conn:
        rows = await conn.fetch("SELECT id FROM namespaces")
        return [row["id"] for row in rows]


async def _clean_orphaned_cascade(
    pg_pool: asyncpg.Pool,
    namespace_id: UUID,
) -> dict[str, int]:
    """
    Chunked cascade that identifies orphaned memory_ids and deletes them in
    batches of CHUNK_DELETE_SIZE to prevent long-held table locks.

    Every subquery and DELETE includes an explicit ``namespace_id = $1::uuid``
    filter as defense-in-depth.  RLS policies provide the primary isolation;
    the explicit clause guarantees tenant boundaries even if RLS is misconfigured
    or bypassed (e.g. a role with ``row_security = off``).

    Iterates with ``await asyncio.sleep(0.1)`` between chunks to yield the
    event loop so other connections can process during a large GC sweep.

    Only orphaned dependents whose own timestamp is older than the grace window
    (``GC_ORPHAN_AGE_SECONDS``) are reaped — mirroring the Mongo/MinIO sweeps.
    This prevents immediate reaping of still-fresh rows that a concurrent or
    late writer may still be referencing.

    Returns cumulative counts for ``salience`` and ``contradictions`` across all chunks.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=cfg.GC_ORPHAN_AGE_SECONDS)
    totals: dict[str, int] = {
        "salience": 0,
        "contradictions": 0,
    }

    try:
        async with pg_pool.acquire(timeout=30.0) as conn:
            while True:
                try:
                    async with conn.transaction():
                        await set_namespace_context(conn, namespace_id)
                        row = await conn.fetchrow(
                            """
                    WITH existing_memories AS (
                        SELECT id FROM memories
                        WHERE namespace_id = $1::uuid
                    ),
                    orphan_memory_ids AS (
                        SELECT memory_id FROM (
                            SELECT ms.memory_id
                            FROM memory_salience ms
                            LEFT JOIN existing_memories em ON ms.memory_id = em.id
                            WHERE em.id IS NULL
                              AND ms.namespace_id = $1::uuid
                              AND ms.updated_at < $3
                            UNION
                            SELECT c.memory_a_id
                            FROM contradictions c
                            LEFT JOIN existing_memories em ON c.memory_a_id = em.id
                            WHERE em.id IS NULL
                              AND c.namespace_id = $1::uuid
                              AND c.detected_at < $3
                            UNION
                            SELECT c.memory_b_id
                            FROM contradictions c
                            LEFT JOIN existing_memories em ON c.memory_a_id = em.id
                            WHERE em.id IS NULL
                              AND c.namespace_id = $1::uuid
                              AND c.detected_at < $3
                            UNION
                            SELECT (el.params->>'memory_id')::uuid
                            FROM event_log el
                            LEFT JOIN existing_memories em
                                   ON (el.params->>'memory_id')::uuid = em.id
                            WHERE el.params->>'memory_id' IS NOT NULL
                              AND em.id IS NULL
                              AND el.namespace_id = $1::uuid
                        ) sub
                        LIMIT $2
                    ),
                    deleted_salience AS (
                        DELETE FROM memory_salience
                        WHERE memory_id IN (SELECT memory_id FROM orphan_memory_ids)
                          AND namespace_id = $1::uuid
                          AND updated_at < $3
                        RETURNING 1 AS dummy
                    ),
                    deleted_contradictions AS (
                        DELETE FROM contradictions
                        WHERE (   memory_a_id IN (SELECT memory_id FROM orphan_memory_ids)
                               OR memory_b_id IN (SELECT memory_id FROM orphan_memory_ids))
                          AND namespace_id = $1::uuid
                          AND detected_at < $3
                        RETURNING 1 AS dummy
                    )
                    SELECT
                        (SELECT count(*) FROM deleted_salience)      AS salience_count,
                        (SELECT count(*) FROM deleted_contradictions) AS contradictions_count
                    """,
                            namespace_id,
                            CHUNK_DELETE_SIZE,
                            cutoff,
                        )
                        if row is None:
                            break
                        chunk_salience = int(row["salience_count"])
                        chunk_contradictions = int(row["contradictions_count"])
                        if chunk_salience == 0 and chunk_contradictions == 0:
                            break
                        totals["salience"] += chunk_salience
                        totals["contradictions"] += chunk_contradictions
                except Exception as exc:
                    log.error(
                        "GC: cascade cleanup chunk failed for ns=%s: %s — stopping cascade for "
                        "this namespace (partial cleanup may have occurred)",
                        namespace_id,
                        type(exc).__name__,
                    )
                    break

                await asyncio.sleep(0.1)
    except Exception as exc:
        log.error(
            "GC: cascade cleanup connection check out failed for ns=%s: %s",
            namespace_id,
            exc,
        )

    return totals


# --- Reverse integrity sweep (R-B): PG ref → missing Mongo doc ---


async def _dispatch_reverse_alert(title: str, message: str) -> None:
    """Fail-safe operator alert. A notification failure must never break the sweep."""
    try:
        from nce.notifications import dispatcher

        await dispatcher.dispatch_alert(title, message)
    except Exception as exc:  # pragma: no cover - defensive, alerting is best-effort
        log.error("GC reverse sweep: alert dispatch failed: %s", type(exc).__name__)


async def _fetch_reverse_candidates(
    pg_pool: asyncpg.Pool,
    namespace_id: UUID,
    cutoff: datetime,
) -> list[tuple[UUID, str]]:
    """Collect live ``memories`` (id, payload_ref) for one namespace, RLS-scoped.

    Bounded by ``REVERSE_SWEEP_MAX_PER_NS`` and keyset-paginated.  Only rows
    older than ``cutoff`` and not already soft-retired (``valid_to IS NULL``)
    are considered, mirroring the forward GC's orphan-age guard so a payload
    written mid-saga is never mistaken for a dangling reference.

    Mongo existence is NOT checked here — that slow I/O is deliberately kept
    outside the scoped transaction (see ``scoped_pg_session`` warning).
    """
    candidates: list[tuple[UUID, str]] = []
    last_seen_id = UUID(int=0)

    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        while len(candidates) < REVERSE_SWEEP_MAX_PER_NS:
            rows = await conn.fetch(
                """
                SELECT id, payload_ref
                FROM   memories
                WHERE  namespace_id = $1::uuid
                  AND  payload_ref IS NOT NULL
                  AND  valid_to IS NULL
                  AND  created_at < $2
                  AND  id > $3
                ORDER BY id
                LIMIT  $4
                """,
                namespace_id,
                cutoff,
                last_seen_id,
                PAGE_SIZE,
            )
            if not rows:
                break
            for row in rows:
                candidates.append((row["id"], row["payload_ref"]))
            last_seen_id = rows[-1]["id"]
            if len(rows) < PAGE_SIZE:
                break  # last page

    return candidates


async def _soft_retire_dangling(
    pg_pool: asyncpg.Pool,
    namespace_id: UUID,
    memory_id: UUID,
) -> bool:
    """Soft-retire one dangling memory (``valid_to = now()``), RLS-scoped.

    Returns True when a live row was retired.  Uses ``UPDATE … SET valid_to``
    (never DELETE) so the WORM ``event_log`` and the row itself are preserved
    for forensic audit / replay-based rebuild.  An explicit ``namespace_id``
    filter backs up RLS as defence-in-depth.
    """
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        result = await conn.execute(
            """
            UPDATE memories
            SET    valid_to = now()
            WHERE  id = $1::uuid
              AND  namespace_id = $2::uuid
              AND  valid_to IS NULL
            """,
            memory_id,
            namespace_id,
        )
    # asyncpg returns e.g. "UPDATE 1"; treat any non-zero count as retired.
    return result.rsplit(" ", 1)[-1] != "0"


async def _collect_reverse_orphans(
    mongo_client: AsyncIOMotorClient,
    pg_pool: asyncpg.Pool,
    namespaces: list[UUID],
) -> int:
    """Mirror of the forward GC: detect+repair PG memories with a missing Mongo doc.

    For each namespace (RLS-scoped) scan live ``memories.payload_ref`` values,
    look up the matching ``episodes`` document in MongoDB, and for every
    dangling reference soft-retire the memory (``valid_to = now()``), dispatch
    a fail-safe operator alert, and log it auditably.  Today the read path only
    raises ``ValueError("MongoDB payload missing.")`` reactively; this proactively
    converges the R-A dangling-ref state.

    Returns the number of memories soft-retired across all namespaces.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=cfg.GC_ORPHAN_AGE_SECONDS)
    retired = 0
    from nce.db_utils import scoped_mongo_session

    for ns_id in namespaces:
        try:
            candidates = await _fetch_reverse_candidates(pg_pool, ns_id, cutoff)
        except Exception as exc:
            log.error(
                "GC reverse sweep: failed to fetch candidates for ns=%s: %s",
                ns_id,
                type(exc).__name__,
            )
            continue

        if len(candidates) >= REVERSE_SWEEP_MAX_PER_NS:
            await _dispatch_reverse_alert(
                "GC reverse sweep bounded",
                f"Namespace {ns_id} hit the reverse-sweep cap "
                f"({REVERSE_SWEEP_MAX_PER_NS}); remaining refs deferred to the next pass.",
            )

        async with scoped_mongo_session(mongo_client, ns_id) as s_db:
            for memory_id, payload_ref in candidates:
                try:
                    doc = await s_db.episodes.find_one({"_id": ObjectId(payload_ref)}, {"_id": 1})
                except Exception as exc:
                    # Malformed ObjectId or a transient Mongo error: skip — never
                    # soft-retire on uncertainty.
                    log.error(
                        "GC reverse sweep: Mongo lookup failed for memory=%s ns=%s: %s",
                        memory_id,
                        ns_id,
                        type(exc).__name__,
                    )
                    continue

                if doc is not None:
                    continue  # healthy — Mongo doc present, leave untouched

                try:
                    did_retire = await _soft_retire_dangling(pg_pool, ns_id, memory_id)
                except Exception as exc:
                    log.error(
                        "GC reverse sweep: soft-retire failed for memory=%s ns=%s: %s",
                        memory_id,
                        ns_id,
                        type(exc).__name__,
                    )
                    continue

                if did_retire:
                    retired += 1
                    log.warning(
                        "GC reverse sweep: soft-retired dangling memory=%s ns=%s "
                        "(payload_ref=%s missing in Mongo episodes).",
                        memory_id,
                        ns_id,
                        payload_ref,
                    )
                    await _dispatch_reverse_alert(
                        "Dangling memory payload",
                        f"Memory {memory_id} (namespace {ns_id}) referenced a missing "
                        f"MongoDB episodes document and was soft-retired (valid_to set).",
                    )

    if retired:
        log.warning("GC reverse sweep: soft-retired %d dangling memory(ies).", retired)
    return retired


# --- Core GC pass ---


async def _collect_minio_orphans(
    minio_client: Any,
    minio_refs: set[str],
) -> int:
    """List all mcp-* buckets and remove objects that are not in minio_refs and older than GC_ORPHAN_AGE_SECONDS."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=cfg.GC_ORPHAN_AGE_SECONDS)
    deleted_count = 0

    def _sweep():
        nonlocal deleted_count
        try:
            buckets = minio_client.list_buckets()
        except Exception as e:
            log.error("GC: Failed to list MinIO buckets: %s", e)
            return

        for bucket in buckets:
            if not bucket.name.startswith("mcp-"):
                continue
            try:
                objects = minio_client.list_objects(bucket.name, recursive=True)
                for obj in objects:
                    if obj.is_dir:
                        continue
                    if obj.last_modified and obj.last_modified < cutoff:
                        if obj.object_name not in minio_refs:
                            log.warning(
                                "GC: deleting orphaned MinIO object %s/%s",
                                bucket.name,
                                obj.object_name,
                            )
                            try:
                                minio_client.remove_object(bucket.name, obj.object_name)
                                deleted_count += 1
                            except Exception as ex:
                                log.error(
                                    "GC: failed to remove MinIO object %s/%s: %s",
                                    bucket.name,
                                    obj.object_name,
                                    ex,
                                )
            except Exception as e:
                log.error("GC: Failed to scan MinIO bucket %s: %s", bucket.name, e)

            # Sweep incomplete multipart uploads
            try:
                key_marker = None
                upload_id_marker = None
                while True:
                    res = minio_client._list_multipart_uploads(
                        bucket.name,
                        key_marker=key_marker,
                        upload_id_marker=upload_id_marker,
                    )
                    uploads = getattr(res, "uploads", None) or []
                    for upload in uploads:
                        initiated = upload.initiated_time
                        if initiated:
                            if initiated.tzinfo is None:
                                initiated = initiated.replace(tzinfo=timezone.utc)
                            if initiated < cutoff:
                                log.warning(
                                    "GC: deleting incomplete MinIO upload %s/%s initiated=%s",
                                    bucket.name,
                                    upload.object_name,
                                    initiated,
                                )
                                try:
                                    minio_client._abort_multipart_upload(
                                        bucket.name,
                                        upload.object_name,
                                        upload.upload_id,
                                    )
                                    deleted_count += 1
                                except Exception as ex:
                                    log.error(
                                        "GC: failed to abort incomplete MinIO upload %s/%s: %s",
                                        bucket.name,
                                        upload.object_name,
                                        ex,
                                    )
                    is_trunc = getattr(res, "is_truncated", False)
                    if not isinstance(is_trunc, bool) or not is_trunc:
                        break
                    key_marker = getattr(res, "next_key_marker", None)
                    upload_id_marker = getattr(res, "next_upload_id_marker", None)
                    if not key_marker and uploads:
                        key_marker = uploads[-1].object_name
                        upload_id_marker = uploads[-1].upload_id
                    if not key_marker or not isinstance(key_marker, str):
                        break
            except Exception as e:
                log.error(
                    "GC: Failed to scan incomplete uploads for MinIO bucket %s: %s", bucket.name, e
                )

    await asyncio.to_thread(_sweep)
    return deleted_count


async def _collect_orphans(
    mongo_client: AsyncIOMotorClient,
    pg_pool: asyncpg.Pool,
    minio_client: Any | None = None,
) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=cfg.GC_ORPHAN_AGE_SECONDS)
    db = mongo_client.memory_archive

    candidates: list[tuple[str, str]] = []

    for col_name in ("episodes", "code_files"):
        cursor = (
            db[col_name]
            .find(
                {"ingested_at": {"$lt": cutoff}},
                {"_id": 1},
            )
            .max_time_ms(30_000)
        )
        async for doc in cursor:
            candidates.append((col_name, str(doc["_id"])))

    if not candidates:
        log.info("GC: no candidates — Tri-Stack is clean.")
        deleted_minio = 0
        if minio_client:
            try:
                # Still run MinIO check even if no MongoDB candidates to keep MinIO aligned
                minio_refs = await _fetch_minio_refs(pg_pool, await _fetch_all_namespaces(pg_pool))
                deleted_minio = await _collect_minio_orphans(minio_client, minio_refs)
            except Exception as exc:
                log.error("GC: Failed to collect MinIO orphans: %s", exc)
        ret = {
            "deleted_docs": 0,
            "deleted_salience": 0,
            "deleted_contradictions": 0,
        }
        if minio_client is not None:
            ret["deleted_minio"] = deleted_minio
        return ret

    log.info(
        "GC: %d candidate(s) older than %ds. Cross-referencing PG (page=%d)...",
        len(candidates),
        cfg.GC_ORPHAN_AGE_SECONDS,
        PAGE_SIZE,
    )

    # Fetch all namespaces once — used by both _fetch_pg_refs (for RLS-scoped
    # reference collection) and the per-namespace cascade loop below.
    namespaces = await _fetch_all_namespaces(pg_pool)

    if not namespaces:
        log.warning(
            "GC: no namespaces found in PG — aborting orphan deletion to prevent "
            "data loss. This may indicate an empty or misconfigured database."
        )
        ret = {
            "deleted_docs": 0,
            "deleted_salience": 0,
            "deleted_contradictions": 0,
        }
        if minio_client is not None:
            ret["deleted_minio"] = 0
        return ret

    pg_refs = await _fetch_pg_refs(pg_pool, namespaces)
    orphans = [(col, oid) for col, oid in candidates if oid not in pg_refs]

    deleted = 0
    if orphans:
        log.warning("GC: %d orphan(s) detected. Purging...", len(orphans))
        for col_name, str_id in orphans:
            try:
                result = await db[col_name].delete_one({"_id": ObjectId(str_id)})
                if result.deleted_count:
                    log.info("GC: deleted orphan [%s] %s", col_name, str_id)
                    deleted += 1
            except Exception as exc:
                log.error("GC: failed to delete %s from [%s]: %s", str_id, col_name, exc)
    else:
        log.info("GC: all %d candidates referenced in PG — no orphans.", len(candidates))

    # Run MinIO cleanup if client is available
    deleted_minio = 0
    if minio_client:
        try:
            minio_refs = await _fetch_minio_refs(pg_pool, namespaces)
            deleted_minio = await _collect_minio_orphans(minio_client, minio_refs)
        except Exception as exc:
            log.error("GC: Failed to collect MinIO orphans: %s", exc)

    # --- Namespace-aware PG maintenance passes ---
    # These operations hit RLS-protected tables (memory_salience,
    # contradictions).  We iterate over all namespaces and set the
    # session variable so RLS allows the cross-table orphan detection to work.
    #
    # Unified single-pass CTE: one query identifies orphaned memory_ids via
    # LEFT JOIN against memories, then cascades DELETEs to all dependent
    # tables in a single round-trip.  Every subquery and DELETE includes an
    # explicit namespace_id filter (defense-in-depth on top of RLS).

    log.info(
        "GC: running per-namespace cascade maintenance across %d namespace(s).",
        len(namespaces),
    )

    total_salience = 0
    total_contradictions = 0

    for ns_id in namespaces:
        counts = await _clean_orphaned_cascade(pg_pool, ns_id)
        total_salience += counts["salience"]
        total_contradictions += counts["contradictions"]

    if total_salience > 0:
        log.info(
            "GC: purged %d orphaned memory_salience rows across all namespaces.",
            total_salience,
        )
    if total_contradictions > 0:
        log.info(
            "GC: purged %d orphaned contradictions across all namespaces.",
            total_contradictions,
        )

    # --- Reverse integrity sweep (R-B) ---
    # Mirror of the forward sweep above: scan PG memories for payload_refs whose
    # MongoDB episodes document is missing, soft-retire them, and alert.  Runs on
    # the same GC cadence and over the same namespace set.
    reverse_retired = await _collect_reverse_orphans(mongo_client, pg_pool, namespaces)

    log.info(
        "GC: pass complete — %d Mongo orphan(s), %d MinIO orphan(s) removed, "
        "%d dangling memory(ies) soft-retired.",
        deleted,
        deleted_minio,
        reverse_retired,
    )
    ret = {
        "deleted_docs": deleted,
        "deleted_salience": total_salience,
        "deleted_contradictions": total_contradictions,
        "reverse_retired": reverse_retired,
    }
    if minio_client is not None:
        ret["deleted_minio"] = deleted_minio
    return ret


# ---------------------------------------------------------------------------
# Batch 125 — Event Retention helpers
# ---------------------------------------------------------------------------

# Archive object prefix inside cfg.NCE_ANCHOR_BUCKET (reuse existing WORM bucket).
# Archived partition blobs land at: event_log_archive/{partition_name}.parquet.json
# (content is a JSON manifest; full row export is out-of-scope for this batch).
_ARCHIVE_PREFIX = "event_log_archive"


async def _get_latest_anchor_for_namespace(
    minio_client: Any,
    namespace_id: UUID,
) -> dict[str, Any] | None:
    """Fetch the latest Batch-124 anchor blob for a namespace from MinIO.

    Returns the parsed anchor dict or None when no anchor exists.
    The anchor lives at ``<namespace_id>/<max_seq>.json`` inside
    ``cfg.NCE_ANCHOR_BUCKET``; the latest one has the highest numeric name.
    """

    def _fetch() -> dict[str, Any] | None:
        bucket = cfg.NCE_ANCHOR_BUCKET
        prefix = f"{namespace_id}/"
        try:
            objects = list(minio_client.list_objects(bucket, prefix=prefix, recursive=True))
        except Exception as exc:
            log.warning(
                "retention: could not list anchor objects for ns=%s: %s",
                namespace_id,
                exc,
            )
            return None

        # Filter to seq-named blobs and pick the highest seq.
        candidates: list[tuple[int, str]] = []
        for obj in objects:
            name = obj.object_name or ""
            basename = name.split("/")[-1]
            if basename.endswith(".json"):
                try:
                    seq = int(basename[: -len(".json")])
                    candidates.append((seq, name))
                except ValueError:
                    pass

        if not candidates:
            return None

        _seq, latest_name = max(candidates, key=lambda t: t[0])
        try:
            resp = minio_client.get_object(bucket, latest_name)
            data = resp.read()
            return json.loads(data.decode("utf-8"))
        except Exception as exc:
            log.warning(
                "retention: could not read anchor blob %s for ns=%s: %s",
                latest_name,
                namespace_id,
                exc,
            )
            return None

    return await asyncio.to_thread(_fetch)


async def _archive_partition_to_minio(
    minio_client: Any,
    pg_pool: asyncpg.Pool,
    partition_name: str,
    namespace_id: UUID,
) -> bool:
    """Write a JSON manifest of the partition to MinIO before dropping it.

    Stores a lightweight manifest (row count + event_seq range + occurred_at range)
    rather than a full row export — full serialisation is deferred to a future
    export pipeline.  Returns True on success.
    """
    # Gather partition stats without RLS (superuser read is required here).
    # We use an unmanaged connection so there is no SET LOCAL namespace binding.
    try:
        async with pg_pool.acquire(timeout=30.0) as conn:
            row = await conn.fetchrow(
                f"""
                SELECT
                    count(*)                            AS row_count,
                    min(event_seq)                      AS min_seq,
                    max(event_seq)                      AS max_seq,
                    min(occurred_at)                    AS min_at,
                    max(occurred_at)                    AS max_at
                FROM {partition_name}
                WHERE namespace_id = $1
                """,  # noqa: S608 — partition_name from pg_class, not user input
                namespace_id,
            )
    except Exception as exc:
        log.error(
            "retention: failed to gather stats for partition=%s ns=%s: %s",
            partition_name,
            namespace_id,
            exc,
        )
        return False

    if row is None:
        return True  # empty partition — nothing to archive

    manifest = {
        "partition": partition_name,
        "namespace_id": str(namespace_id),
        "row_count": int(row["row_count"]) if row["row_count"] else 0,
        "min_seq": int(row["min_seq"]) if row["min_seq"] else None,
        "max_seq": int(row["max_seq"]) if row["max_seq"] else None,
        "min_occurred_at": row["min_at"].isoformat() if row["min_at"] else None,
        "max_occurred_at": row["max_at"].isoformat() if row["max_at"] else None,
        "archived_at": datetime.now(timezone.utc).isoformat(),
    }
    blob = json.dumps(manifest, sort_keys=True).encode("utf-8")
    object_name = f"{_ARCHIVE_PREFIX}/{namespace_id}/{partition_name}.json"

    def _put() -> None:
        minio_client.put_object(
            cfg.NCE_ANCHOR_BUCKET,
            object_name,
            io.BytesIO(blob),
            len(blob),
            content_type="application/json",
        )

    try:
        await asyncio.to_thread(_put)
        log.info(
            "retention: archived manifest for partition=%s ns=%s -> %s",
            partition_name,
            namespace_id,
            object_name,
        )
        return True
    except Exception as exc:
        log.error(
            "retention: failed to upload archive manifest for partition=%s ns=%s: %s",
            partition_name,
            namespace_id,
            exc,
        )
        return False


async def run_partition_retention(
    pg_pool: asyncpg.Pool,
    minio_client: Any,
    *,
    retention_months: int | None = None,
) -> dict[str, int]:
    """Archive and DROP aged event_log partitions — ONLY when fully anchored.

    A partition ``event_log_YYYY_MM`` is eligible for retention when:
      1. Its range end is older than ``retention_months`` months ago.
      2. Every namespace whose events live in that partition has an anchor
         (Batch 124) whose ``max_seq`` >= the partition's per-namespace
         ``max(event_seq)``.  A single un-anchored namespace blocks the drop.

    Partitions are NEVER row-level-deleted; only whole partitions are dropped.
    Before dropping, a JSON manifest is archived to MinIO.

    Returns
    -------
    dict with keys ``checked``, ``archived``, ``dropped``, ``skipped_unanchored``.
    """
    if retention_months is None:
        retention_months = cfg.NCE_EVENT_RETENTION_MONTHS

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_months * 30)
    result: dict[str, int] = {
        "checked": 0,
        "archived": 0,
        "dropped": 0,
        "skipped_unanchored": 0,
    }

    # Discover monthly event_log partitions from pg_class.
    async with pg_pool.acquire(timeout=30.0) as conn:
        rows = await conn.fetch(
            """
            SELECT
                c.relname           AS partition_name,
                pg_get_expr(c.relpartbound, c.oid) AS bound_expr
            FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            WHERE i.inhparent = 'event_log'::regclass
              AND c.relname ~ '^event_log_[0-9]{4}_[0-9]{2}$'
            ORDER BY c.relname
            """
        )

    for row in rows:
        partition_name: str = row["partition_name"]
        # Parse partition start from name: event_log_YYYY_MM
        parts = partition_name.split("_")
        try:
            year = int(parts[-2])
            month = int(parts[-1])
        except (IndexError, ValueError):
            log.debug("retention: skipping unrecognised partition name %s", partition_name)
            continue

        # Partition range: [YYYY_MM, YYYY_MM+1 month) — end of range is used for cutoff.
        # The partition covers [first of month, first of next month).
        # A partition is "aged" when its END (first of next month) <= cutoff.
        if month == 12:
            partition_end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            partition_end = datetime(year, month + 1, 1, tzinfo=timezone.utc)

        if partition_end > cutoff:
            log.debug(
                "retention: partition %s is within retention window — skipping",
                partition_name,
            )
            continue

        result["checked"] += 1

        # Determine if this partition is fully anchored for ALL namespaces that
        # have events in it.  We read the per-namespace max_seq directly.
        async with pg_pool.acquire(timeout=30.0) as conn:
            ns_seq_rows = await conn.fetch(
                f"""
                SELECT namespace_id, max(event_seq) AS max_seq
                FROM   {partition_name}
                GROUP  BY namespace_id
                """,  # noqa: S608 — partition_name from pg_class, not user input
            )

        if not ns_seq_rows:
            # Empty partition — safe to drop without archiving.
            log.info("retention: dropping empty aged partition %s", partition_name)
            try:
                async with pg_pool.acquire(timeout=30.0) as conn:
                    await conn.execute(f"DROP TABLE IF EXISTS {partition_name}")
                result["dropped"] += 1
            except Exception as exc:
                log.error("retention: failed to drop empty partition %s: %s", partition_name, exc)
            continue

        # Check anchor coverage for each namespace.
        all_anchored = True
        for ns_row in ns_seq_rows:
            ns_id: UUID = ns_row["namespace_id"]
            partition_max_seq: int = int(ns_row["max_seq"])

            anchor = await _get_latest_anchor_for_namespace(minio_client, ns_id)
            if anchor is None:
                log.warning(
                    "retention: no anchor found for ns=%s — partition %s is UNANCHORED, keeping",
                    ns_id,
                    partition_name,
                )
                all_anchored = False
                break

            anchor_max_seq: int = int(anchor.get("max_seq", 0))
            if anchor_max_seq < partition_max_seq:
                log.warning(
                    "retention: anchor max_seq=%d < partition max_seq=%d for ns=%s "
                    "— partition %s is UNANCHORED, keeping",
                    anchor_max_seq,
                    partition_max_seq,
                    ns_id,
                    partition_name,
                )
                all_anchored = False
                break

        if not all_anchored:
            result["skipped_unanchored"] += 1
            continue

        # All namespaces are fully anchored — archive then drop.
        # Archive manifest once per namespace.
        archive_ok = True
        for ns_row in ns_seq_rows:
            ns_id = ns_row["namespace_id"]
            if not await _archive_partition_to_minio(minio_client, pg_pool, partition_name, ns_id):
                archive_ok = False
                log.error(
                    "retention: archive failed for partition=%s ns=%s — skipping drop",
                    partition_name,
                    ns_id,
                )
                break

        if not archive_ok:
            result["skipped_unanchored"] += 1
            continue

        result["archived"] += 1

        # DROP the partition (detaches it from the parent + drops the table).
        try:
            async with pg_pool.acquire(timeout=60.0) as conn:
                await conn.execute(f"DROP TABLE IF EXISTS {partition_name}")
            log.info(
                "retention: dropped anchored aged partition %s (cutoff=%s)",
                partition_name,
                cutoff.date(),
            )
            result["dropped"] += 1
        except Exception as exc:
            log.error("retention: failed to drop partition %s: %s", partition_name, exc)

    return result


async def run_contradiction_retention(
    pg_pool: asyncpg.Pool,
    namespaces: list[UUID],
    *,
    retention_days: int | None = None,
) -> int:
    """Delete resolved contradictions older than ``retention_days``.

    Only rows with a non-null ``resolution`` (i.e. the contradiction has been
    adjudicated) older than the TTL are deleted.  Active/open contradictions are
    never touched.  Scoped via ``scoped_pg_session`` so RLS + the explicit
    ``namespace_id`` predicate provide defense-in-depth.

    Returns total deleted rows across all namespaces.
    """
    if retention_days is None:
        retention_days = cfg.NCE_CONTRADICTION_RETENTION_DAYS

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    total_deleted = 0

    for ns_id in namespaces:
        try:
            async with scoped_pg_session(pg_pool, ns_id) as conn:
                result = await conn.execute(
                    """
                    DELETE FROM contradictions
                    WHERE namespace_id = $1::uuid
                      AND resolution IS NOT NULL
                      AND resolved_at < $2
                    """,
                    ns_id,
                    cutoff,
                )
            count = int(result.rsplit(" ", 1)[-1])
            if count:
                log.info(
                    "retention: purged %d resolved contradiction(s) for ns=%s (cutoff=%s)",
                    count,
                    ns_id,
                    cutoff.date(),
                )
            total_deleted += count
        except Exception as exc:
            log.error(
                "retention: contradiction purge failed for ns=%s: %s",
                ns_id,
                type(exc).__name__,
            )

    return total_deleted


async def run_edge_prune(
    pg_pool: asyncpg.Pool,
    namespaces: list[UUID],
    *,
    prune_age_days: int | None = None,
    confidence_threshold: float = 0.15,
) -> int:
    """Reap low-confidence, aged kg_edges — except change_origin='sync' edges.

    Deletes edges that satisfy ALL of:
      - ``confidence < confidence_threshold`` (default 0.15)
      - ``updated_at < now() - prune_age_days``
      - ``change_origin != 'sync'``   ← Batch 106 invariant

    A sync-origin edge whose confidence dips below 0.15 reflects deterministic
    ground-truth data and MUST survive; only non-deterministic, stale, low-weight
    inferences are reaped.

    Chunked by CHUNK_DELETE_SIZE to avoid long-held table locks.
    Returns total deleted rows across all namespaces.
    """
    if prune_age_days is None:
        prune_age_days = cfg.NCE_EDGE_PRUNE_AGE_DAYS

    cutoff = datetime.now(timezone.utc) - timedelta(days=prune_age_days)
    total_deleted = 0

    for ns_id in namespaces:
        deleted_ns = 0
        while True:
            try:
                async with scoped_pg_session(pg_pool, ns_id) as conn:
                    result = await conn.execute(
                        """
                        DELETE FROM kg_edges
                        WHERE id IN (
                            SELECT id FROM kg_edges
                            WHERE namespace_id = $1::uuid
                              AND confidence < $2
                              AND updated_at < $3
                              AND change_origin != 'sync'
                            LIMIT $4
                        )
                          AND namespace_id = $1::uuid
                          AND change_origin != 'sync'
                        """,
                        ns_id,
                        confidence_threshold,
                        cutoff,
                        CHUNK_DELETE_SIZE,
                    )
                count = int(result.rsplit(" ", 1)[-1])
                deleted_ns += count
                if count < CHUNK_DELETE_SIZE:
                    break  # last chunk
                await asyncio.sleep(0.05)
            except Exception as exc:
                log.error(
                    "retention: edge prune failed for ns=%s: %s",
                    ns_id,
                    type(exc).__name__,
                )
                break

        if deleted_ns:
            log.info(
                "retention: pruned %d low-confidence non-sync kg_edge(s) for ns=%s",
                deleted_ns,
                ns_id,
            )
        total_deleted += deleted_ns

    return total_deleted


async def run_retention_pass(
    pg_pool: asyncpg.Pool,
    minio_client: Any,
) -> dict[str, Any]:
    """Execute all three retention sweeps in a single maintenance pass.

    Called by the cron tick.  Failures in one sweep do not block others.
    Returns a summary dict for logging.
    """
    namespaces = await _fetch_all_namespaces(pg_pool)

    partition_stats: dict[str, int] = {}
    contradiction_deleted = 0
    edge_deleted = 0

    # 1. Partition retention (event_log WORM archive+drop)
    try:
        partition_stats = await run_partition_retention(pg_pool, minio_client)
        log.info("retention: partition sweep complete: %s", partition_stats)
    except Exception as exc:
        log.error("retention: partition sweep raised unexpected error: %s", exc)

    # 2. Resolved-contradiction purge
    try:
        contradiction_deleted = await run_contradiction_retention(pg_pool, namespaces)
        if contradiction_deleted:
            log.info("retention: purged %d resolved contradiction(s) total", contradiction_deleted)
    except Exception as exc:
        log.error("retention: contradiction purge raised unexpected error: %s", exc)

    # 3. Low-confidence edge prune (skip sync edges)
    try:
        edge_deleted = await run_edge_prune(pg_pool, namespaces)
        if edge_deleted:
            log.info("retention: pruned %d low-confidence kg_edge(s) total", edge_deleted)
    except Exception as exc:
        log.error("retention: edge prune raised unexpected error: %s", exc)

    return {
        "partition_stats": partition_stats,
        "contradictions_purged": contradiction_deleted,
        "edges_pruned": edge_deleted,
    }


# --- Long-running loop ---


async def run_gc_loop():
    """
    Background loop. Connects once with retry, then runs a GC pass every hour.
    Designed to be launched as asyncio.create_task() alongside the MCP server.

    A single Redis client is created here and reused for every lock acquire/
    release cycle — one persistent connection instead of two ephemeral ones
    per GC pass.
    """
    log.info(
        "GC starting up (interval=%ds, orphan_age=%ds).",
        cfg.GC_INTERVAL_SECONDS,
        cfg.GC_ORPHAN_AGE_SECONDS,
    )

    try:
        mongo_client, pg_pool = await _connect_with_retry()
    except RuntimeError as exc:
        log.critical(
            "GC failed to connect — background task will exit: %s",
            redact_secrets_in_text(str(exc)),
        )
        return

    minio_client: Any | None = None
    if cfg.MINIO_ENDPOINT:
        try:
            from minio import Minio

            minio_client = Minio(
                cfg.MINIO_ENDPOINT,
                access_key=cfg.MINIO_ACCESS_KEY,
                secret_key=cfg.MINIO_SECRET_KEY,
                secure=cfg.MINIO_SECURE,
            )
            log.info("GC connected to MinIO endpoint: %s", cfg.MINIO_ENDPOINT)
        except Exception as exc:
            log.error("GC could not create MinIO client: %s", exc)

    # Create a single shared Redis client for the lock lifecycle.
    redis_client: Any | None = None
    if cfg.REDIS_URL:
        try:
            from redis.asyncio import Redis as AsyncRedis

            redis_client = AsyncRedis.from_url(cfg.REDIS_URL)
        except Exception as exc:
            log.error("GC could not create Redis client — distributed lock disabled: %s", exc)
    else:
        log.warning("REDIS_URL not set — GC distributed lock disabled.")

    try:
        while True:
            if redis_client is None:
                # No Redis — run without locking (single-instance deployments / dev).
                lock_token: str | None = "no-lock"
            else:
                lock_token = await _acquire_gc_lock(redis_client)

            if lock_token is None:
                await asyncio.sleep(cfg.GC_INTERVAL_SECONDS)
                continue

            try:
                await _collect_orphans(mongo_client, pg_pool, minio_client)
            except Exception as exc:
                log.error("GC pass raised unexpected error: %s", exc)
            finally:
                if redis_client is not None and lock_token != "no-lock":
                    await _release_gc_lock(redis_client, lock_token)

            await asyncio.sleep(cfg.GC_INTERVAL_SECONDS)
    finally:
        mongo_client.close()
        await pg_pool.close()
        if redis_client is not None:
            try:
                await redis_client.aclose()
            except Exception:
                pass
        log.info("GC connections closed.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [GC] %(levelname)s %(message)s")
    asyncio.run(run_gc_loop())
