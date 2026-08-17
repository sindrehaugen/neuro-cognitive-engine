"""
Phase 2.1 — Re-embedding Worker
=================================
Finds ``memories`` rows whose ``embedding_model_id`` differs from the current
embedding model version (or is NULL) and re-embeds them in bounded,
rate-limited batches.  Optionally re-embeds ``kg_nodes`` (which have no model
version column).

Design
------
Model versioning
    The running model is identified by a deterministic UUIDv5 derived from
    ``nce.embeddings.MODEL_ID``.  Each updated memory row gets that UUID
    stamped into ``embedding_model_id``, so the next run can skip it cheaply
    via a single index scan.

Keyset pagination
    Memories are fetched via ``(created_at ASC, id ASC)`` cursor so the query
    planner can use the partitioned index and the cursor stays stable even if
    new rows are inserted during a run.

Rate limiting
    A configurable ``REEMBED_BATCHES_PER_MINUTE`` cap (default: 20 → one batch
    every 3 s) is implemented as a post-batch ``asyncio.sleep``.  A single
    ``asyncio.Semaphore`` guards the embedding call to prevent concurrent embed
    fan-out.

Text source
    - ``episodic`` memories  → Mongo ``episodes.raw_data`` (the heavy payload;
      best available approximation of the original summary text).
    - ``code_chunk`` memories → Mongo ``code_files.raw_code`` (truncated).
    - Fallback                → ``name + filepath`` columns on the memories row.
    - ``kg_nodes``            → ``label`` column (no Mongo lookup needed).

Resumability
    Each run is recorded in the ``reembedding_runs`` audit table (created by the
    worker on first run — no schema.sql changes required).  The cursor position
    is checkpointed after every batch.  If the process is killed mid-run, the
    next invocation continues from where the cursor left off because rows already
    updated are excluded by the ``embedding_model_id != current`` WHERE clause.

Entry points
------------
``ReembeddingWorker.run_once(pool, mongo_client)``
    One full sweep; callable from APScheduler (see ``nce/cron.py``).
``async_main()``
    Connects, runs one sweep, disconnects — suitable for ``python -m nce.reembedding_worker``.
``main()``
    Sync wrapper for ``__main__``.

Env vars
--------
REEMBED_BATCH_SIZE            Rows per embed batch (default: 32).
REEMBED_BATCHES_PER_MINUTE    Rate cap (default: 20 → 3 s sleep between batches).
REEMBED_MAX_ROWS_PER_RUN      0 = unlimited; positive = stop after N memories (default: 0).
REEMBED_INCLUDE_KG_NODES      "true" to also refresh kg_nodes embeddings (default: false).
REEMBED_MAX_TEXT_CHARS        Clip text before embedding (default: 4096).
REEMBED_CRON_INTERVAL_MINUTES APScheduler interval when running via cron (default: 60).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import Any

import asyncpg  # type: ignore[import-untyped]

from nce import embeddings as _embeddings
from nce.config import cfg
from nce.db_utils import resolve_worker_dsn
from nce.embeddings import MODEL_ID, VECTOR_DIM  # noqa: F401
from nce.observability import (
    ENVELOPE_DECRYPT_FAILURES,
    REEMBEDDER_VRAM_ALLOCATED,
    REEMBEDDER_VRAM_PEAK,
    REEMBEDDER_VRAM_RESERVED,
)
from nce.redis_lock import acquire_lock as _acquire_redis_lock
from nce.redis_lock import release_lock as _release_redis_lock

log = logging.getLogger("nce.reembedding")

# --------------------------------------------------------------------------- #
# Config — sourced from nce.config.cfg (env vars documented in .env.example)
# --------------------------------------------------------------------------- #

BATCH_SIZE: int = cfg.REEMBED_BATCH_SIZE
BATCHES_PER_MINUTE: int = cfg.REEMBED_BATCHES_PER_MINUTE
MAX_ROWS_PER_RUN: int = cfg.REEMBED_MAX_ROWS_PER_RUN
INCLUDE_KG_NODES: bool = cfg.REEMBED_INCLUDE_KG_NODES
MAX_TEXT_CHARS: int = cfg.REEMBED_MAX_TEXT_CHARS
CRON_INTERVAL_MINUTES: int = cfg.REEMBED_CRON_INTERVAL_MINUTES

_UUID_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # uuid.NAMESPACE_URL

# Sleep duration (seconds) between VRAM pressure re-checks.
_VRAM_PRESSURE_SLEEP: float = 5.0


class VRAMPressureError(RuntimeError):
    """Raised when CUDA VRAM remains above the high-watermark after all wait cycles.

    ``run_once`` catches this and exits the tick cleanly — no DLQ routing.
    """


def current_model_uuid() -> uuid.UUID:
    """Return a stable UUID that uniquely identifies the active embedding model."""
    return uuid.uuid5(_UUID_NS, MODEL_ID)


# --------------------------------------------------------------------------- #
# Audit table DDL — created by the worker on first run (idempotent).
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Pagination helpers — pure SQL, raw asyncpg
# --------------------------------------------------------------------------- #


async def _fetch_memories_batch(
    conn: asyncpg.Connection,
    model_uuid: uuid.UUID,
    batch_size: int,
    cursor_created_at: datetime | None,
    cursor_id: uuid.UUID | None,
) -> list[asyncpg.Record]:
    """
    Keyset-paginated SELECT of memories that need re-embedding.

    Includes rows where:
    - ``embedding IS NOT NULL``  (skip blanks that were never embedded)
    - ``embedding_model_id``  is NULL or does not match the current model UUID
    """
    model_str = str(model_uuid)

    if cursor_created_at is None:
        return await conn.fetch(
            """
            SELECT id, created_at, memory_type, payload_ref, name, filepath, namespace_id
            FROM   memories
            WHERE  embedding IS NOT NULL
              AND  (embedding_model_id IS NULL
                    OR embedding_model_id::text <> $1)
            ORDER  BY created_at ASC, id ASC
            LIMIT  $2
            """,
            model_str,
            batch_size,
        )

    # Composite keyset: advance past (created_at, id) of the last processed row.
    return await conn.fetch(
        """
        SELECT id, created_at, memory_type, payload_ref, name, filepath, namespace_id
        FROM   memories
        WHERE  embedding IS NOT NULL
          AND  (embedding_model_id IS NULL
                OR embedding_model_id::text <> $1)
          AND  (created_at, id) > ($2, $3)
        ORDER  BY created_at ASC, id ASC
        LIMIT  $4
        """,
        model_str,
        cursor_created_at,
        cursor_id,
        batch_size,
    )


async def _fetch_kg_nodes_batch(
    conn: asyncpg.Connection,
    model_uuid: uuid.UUID,
    batch_size: int,
    cursor_id: uuid.UUID | None,
) -> list[asyncpg.Record]:
    """Ordered by id ASC; works across HASH partitions."""
    model_str = str(model_uuid)
    if cursor_id is None:
        return await conn.fetch(
            """
            SELECT id, label FROM kg_nodes
            WHERE embedding IS NOT NULL
              AND (embedding_model_id IS NULL OR embedding_model_id::text <> $1)
            ORDER BY id ASC LIMIT $2
            """,
            model_str,
            batch_size,
        )
    return await conn.fetch(
        """
        SELECT id, label FROM kg_nodes
        WHERE embedding IS NOT NULL
          AND (embedding_model_id IS NULL OR embedding_model_id::text <> $1)
          AND id > $2
        ORDER BY id ASC LIMIT $3
        """,
        model_str,
        cursor_id,
        batch_size,
    )


# --------------------------------------------------------------------------- #
# Mongo text resolution — batch by collection
# --------------------------------------------------------------------------- #


async def _resolve_texts_from_mongo(
    mongo_client: Any,
    rows: list[asyncpg.Record],
    max_text_chars: int,
    wrapped_by_ref: dict[str, bytes | None] | None = None,
) -> tuple[dict[str, str], set[str]]:
    """
    Returns ``({payload_ref: text_to_embed}, decrypt_failed_refs)`` for rows
    that have a valid MongoDB payload_ref.

    Episodic memories → ``episodes.raw_data``.
    Code chunks       → ``code_files.raw_code`` (truncated to max_text_chars).

    When *wrapped_by_ref* is provided, every raw payload is routed through
    :func:`nce.envelope.maybe_decrypt_raw_data`.  Refs whose DEK cannot be
    unwrapped (zeroed/destroyed — provable forgetting) are collected in
    ``decrypt_failed_refs`` so the caller can flag them ``dek_unreadable`` and
    skip embedding — the batch continues normally.
    """
    from collections import defaultdict

    from bson import ObjectId  # defer so tests that mock Mongo don't need bson

    from nce.db_utils import scoped_mongo_session
    from nce.envelope import maybe_decrypt_raw_data

    ns_episodic_refs = defaultdict(list)
    ns_code_refs = defaultdict(list)

    for row in rows:
        ref = row.get("payload_ref") or ""
        ns_id = row.get("namespace_id")
        if len(ref) != 24 or not ns_id:  # MongoDB ObjectId hex is always 24 chars
            continue
        if row.get("memory_type") == "code_chunk":
            ns_code_refs[ns_id].append(ObjectId(ref))
        else:
            ns_episodic_refs[ns_id].append(ObjectId(ref))

    result: dict[str, str] = {}
    decrypt_failed: set[str] = set()
    _wbr = wrapped_by_ref or {}

    for ns_id, oids in ns_episodic_refs.items():
        try:
            async with scoped_mongo_session(mongo_client, ns_id) as s_db:
                async for doc in s_db.episodes.find({"_id": {"$in": oids}}, {"raw_data": 1}):
                    ref = str(doc["_id"])
                    raw = doc.get("raw_data", "")
                    try:
                        text = maybe_decrypt_raw_data(raw, _wbr.get(ref))
                    except Exception as dec_exc:
                        log.warning(
                            "reembedding_worker: decrypt failure for episodic "
                            "payload_ref %s (dek_unreadable=true); skipping. %s",
                            ref,
                            dec_exc,
                        )
                        decrypt_failed.add(ref)
                        continue
                    result[ref] = text[:max_text_chars]
        except Exception as exc:
            log.warning("Re-embed: Mongo episodic fetch error for ns %s: %s", ns_id, exc)

    for ns_id, oids in ns_code_refs.items():
        try:
            async with scoped_mongo_session(mongo_client, ns_id) as s_db:
                async for doc in s_db.code_files.find({"_id": {"$in": oids}}, {"raw_code": 1}):
                    ref = str(doc["_id"])
                    raw = doc.get("raw_code", "")
                    try:
                        text = maybe_decrypt_raw_data(raw, _wbr.get(ref))
                    except Exception as dec_exc:
                        log.warning(
                            "reembedding_worker: decrypt failure for code_chunk "
                            "payload_ref %s (dek_unreadable=true); skipping. %s",
                            ref,
                            dec_exc,
                        )
                        decrypt_failed.add(ref)
                        continue
                    result[ref] = text[:max_text_chars]
        except Exception as exc:
            log.warning("Re-embed: Mongo code fetch error for ns %s: %s", ns_id, exc)

    return result, decrypt_failed


def _fallback_text(row: asyncpg.Record, max_chars: int) -> str:
    """Best-effort text from the memories row itself when Mongo is unavailable."""
    parts = [p for p in (row.get("name"), row.get("filepath")) if p]
    return (" ".join(parts))[:max_chars]


# --------------------------------------------------------------------------- #
# Batch update helpers
# --------------------------------------------------------------------------- #


async def _update_memories_batch(
    conn: asyncpg.Connection,
    batch: list[tuple[uuid.UUID, datetime, list[float]]],
    model_uuid: uuid.UUID,
) -> None:
    """
    Stamps updated embedding + embedding_model_id for a batch of memories.
    Includes ``created_at`` in the WHERE clause so Postgres can prune to the
    correct range partition without a full-table scan.
    """
    model_str = str(model_uuid)
    async with conn.transaction():
        await conn.executemany(
            """
            UPDATE memories
            SET    embedding          = $1::vector,
                   embedding_model_id = $2::uuid
            WHERE  id         = $3
              AND  created_at = $4
            """,
            [(json.dumps(vec), model_str, mem_id, created_at) for mem_id, created_at, vec in batch],
        )


async def _update_kg_nodes_batch(
    conn: asyncpg.Connection,
    batch: list[tuple[uuid.UUID, list[float]]],
    model_uuid: uuid.UUID,
) -> None:
    model_str = str(model_uuid)
    async with conn.transaction():
        await conn.executemany(
            "UPDATE kg_nodes SET embedding = $1::vector, embedding_model_id = $2::uuid, updated_at = now() WHERE id = $3",
            [(json.dumps(vec), model_str, node_id) for node_id, vec in batch],
        )


# --------------------------------------------------------------------------- #
# Progress checkpoint
# --------------------------------------------------------------------------- #


async def _checkpoint(
    conn: asyncpg.Connection,
    run_id: uuid.UUID,
    memories_done: int,
    kg_nodes_done: int,
    cursor_created_at: datetime | None,
    cursor_id: uuid.UUID | None,
) -> None:
    await conn.execute(
        """
        UPDATE reembedding_runs
        SET    memories_done     = $1,
               kg_nodes_done    = $2,
               cursor_created_at = $3,
               cursor_id         = $4
        WHERE  id = $5
        """,
        memories_done,
        kg_nodes_done,
        cursor_created_at,
        cursor_id,
        run_id,
    )


# --------------------------------------------------------------------------- #
# Worker class
# --------------------------------------------------------------------------- #


_EMBED_LOCK_KEY: str = "nce:reembed:embed_lock"
# TTL covers the maximum expected time for one embed_batch call.
# Chosen conservatively at 5 minutes; the lock is released immediately on
# success, so the TTL is only relevant if the process dies mid-embedding.
_EMBED_LOCK_TTL: int = 300


class ReembeddingWorker:
    """
    Stateless background worker — instantiate once per process; call
    ``run_once`` as many times as needed (APScheduler or manual).
    """

    def __init__(
        self,
        *,
        batch_size: int = BATCH_SIZE,
        batches_per_minute: int = BATCHES_PER_MINUTE,
        max_rows_per_run: int = MAX_ROWS_PER_RUN,
        include_kg_nodes: bool = INCLUDE_KG_NODES,
        max_text_chars: int = MAX_TEXT_CHARS,
        redis_client: Any | None = None,
    ) -> None:
        self.batch_size = max(1, batch_size)
        # Inter-batch sleep enforces the token-rate cap.
        self._sleep = 60.0 / max(1, batches_per_minute)
        self.max_rows_per_run = max_rows_per_run  # 0 = unlimited
        self.include_kg_nodes = include_kg_nodes
        self.max_text_chars = max_text_chars
        # Optional shared Redis client for the embedding lock.  When None a
        # short-lived client is created per embed batch (acceptable overhead
        # given embed_batch itself takes seconds).
        self._redis_client = redis_client

    # ---------------------------------------------------------------------- #
    # Internal helpers
    # ---------------------------------------------------------------------- #

    @property
    def _worker_id(self) -> str:
        """Stable per-instance label for Prometheus gauges."""
        return f"reembedder-{id(self)}"

    async def _vram_pressure_gate(self) -> None:
        """Pause-don't-poison VRAM back-pressure guard (Domain 3 / Batch 104).

        If CUDA is unavailable this is a no-op.  When CUDA is present:
        1. Read allocated / reserved / peak bytes and emit the three gauges.
        2. If ``allocated / total < NCE_REEMBED_VRAM_HIGH_WATERMARK``, return.
        3. Otherwise call ``torch.cuda.empty_cache()`` and sleep, retrying up
           to ``NCE_REEMBED_VRAM_MAX_PRESSURE_WAITS`` times.
        4. If still saturated after all waits, raise ``VRAMPressureError``.
        """
        try:
            import torch  # type: ignore[import-untyped]
        except ImportError:
            return  # torch not installed — no CUDA, gate is a no-op

        if not torch.cuda.is_available():
            return  # CPU-only deployment — gate is a no-op

        worker_id = self._worker_id
        high_watermark: float = cfg.NCE_REEMBED_VRAM_HIGH_WATERMARK
        max_waits: int = cfg.NCE_REEMBED_VRAM_MAX_PRESSURE_WAITS

        total_memory: int = torch.cuda.get_device_properties(0).total_memory

        for attempt in range(max_waits + 1):
            allocated: int = torch.cuda.memory_allocated()
            reserved: int = torch.cuda.memory_reserved()
            peak: int = torch.cuda.max_memory_allocated()

            # Emit gauges on every check so operators see live pressure.
            REEMBEDDER_VRAM_ALLOCATED.labels(worker_id=worker_id).set(allocated)
            REEMBEDDER_VRAM_RESERVED.labels(worker_id=worker_id).set(reserved)
            REEMBEDDER_VRAM_PEAK.labels(worker_id=worker_id).set(peak)

            ratio: float = allocated / total_memory if total_memory > 0 else 0.0

            if ratio < high_watermark:
                # Pressure is acceptable — proceed with embedding.
                return

            if attempt < max_waits:
                log.warning(
                    "Re-embed: VRAM pressure %.1f%% >= watermark %.1f%% "
                    "(attempt %d/%d) — flushing cache and waiting %ds.",
                    ratio * 100,
                    high_watermark * 100,
                    attempt + 1,
                    max_waits,
                    _VRAM_PRESSURE_SLEEP,
                )
                torch.cuda.empty_cache()
                await asyncio.sleep(_VRAM_PRESSURE_SLEEP)
            else:
                raise VRAMPressureError(
                    f"VRAM still at {ratio:.1%} after {max_waits} wait(s); "
                    "skipping tick to avoid OOM."
                )

    async def _embed(self, pool: asyncpg.Pool, texts: list[str]) -> list[list[float]]:
        """Run batch embedding, holding a Redis distributed lock for the duration.

        Replaces the old ``pg_advisory_lock`` approach which kept a pool
        connection parked while the embedding model ran (potentially minutes).
        A Redis key lock achieves the same cross-worker exclusion without
        tying up a PG connection.

        If Redis is not configured or unavailable the embedding proceeds
        without a distributed lock — functionally identical to the original
        behaviour when no second instance is running.

        Raises ``VRAMPressureError`` when CUDA memory stays above the
        high-watermark after all configured wait cycles (see
        ``_vram_pressure_gate``).
        """
        await self._vram_pressure_gate()

        if not cfg.REDIS_URL:
            return await _embeddings.embed_batch(texts)

        owned = self._redis_client is None
        client = self._redis_client
        try:
            if owned:
                from redis.asyncio import Redis as AsyncRedis

                client = AsyncRedis.from_url(cfg.REDIS_URL)

            token = await _acquire_redis_lock(client, _EMBED_LOCK_KEY, _EMBED_LOCK_TTL)
            if token is None:
                log.warning(
                    "Re-embed: embedding lock held by another worker — proceeding without lock"
                )
            try:
                return await _embeddings.embed_batch(texts)
            finally:
                if token is not None:
                    await _release_redis_lock(client, _EMBED_LOCK_KEY, token)
        finally:
            if owned and client is not None:
                try:
                    await client.aclose()
                except Exception:
                    pass

    async def _create_run(
        self,
        pool: asyncpg.Pool,
        model_uuid: uuid.UUID,
    ) -> uuid.UUID:
        async with pool.acquire(timeout=10.0) as conn:
            run_id: uuid.UUID = await conn.fetchval(
                """
                INSERT INTO reembedding_runs (model_version, model_name)
                VALUES ($1, $2)
                RETURNING id
                """,
                model_uuid,
                MODEL_ID,
            )
        return run_id

    async def _close_run(
        self,
        pool: asyncpg.Pool,
        run_id: uuid.UUID,
        status: str,
        memories_done: int,
        kg_nodes_done: int,
        error: str | None = None,
    ) -> None:
        async with pool.acquire(timeout=10.0) as conn:
            await conn.execute(
                """
                UPDATE reembedding_runs
                SET    status        = $1,
                       completed_at  = now(),
                       memories_done = $2,
                       kg_nodes_done = $3,
                       error_message = $4
                WHERE  id = $5
                """,
                status,
                memories_done,
                kg_nodes_done,
                error,
                run_id,
            )

    # ---------------------------------------------------------------------- #
    # Phase A — memories
    # ---------------------------------------------------------------------- #

    async def _run_memories_phase(
        self,
        pool: asyncpg.Pool,
        mongo_client: Any,
        model_uuid: uuid.UUID,
        run_id: uuid.UUID,
    ) -> int:
        """Returns total memories re-embedded during this phase."""
        cursor_created_at: datetime | None = None
        cursor_id: uuid.UUID | None = None
        memories_done = 0

        while True:
            # Stop early if the operator set a per-run ceiling.
            if self.max_rows_per_run and memories_done >= self.max_rows_per_run:
                log.info(
                    "Re-embed: max_rows_per_run=%d reached, stopping memories phase.",
                    self.max_rows_per_run,
                )
                break

            async with pool.acquire(timeout=10.0) as conn:
                async with conn.transaction():
                    rows = await _fetch_memories_batch(
                        conn,
                        model_uuid,
                        self.batch_size,
                        cursor_created_at,
                        cursor_id,
                    )

            if not rows:
                log.debug("Re-embed: no more stale memories found.")
                break

            # Fetch wrapped_dek for all payload_refs in this batch so the
            # decrypt path has access to the per-memory DEK (Part II.4).
            all_payload_refs = [
                row["payload_ref"]
                for row in rows
                if row.get("payload_ref") and len(row["payload_ref"]) == 24
            ]
            wrapped_by_ref: dict[str, bytes | None] = {}
            if all_payload_refs:
                try:
                    async with pool.acquire(timeout=10.0) as dek_conn:
                        dek_rows = await dek_conn.fetch(
                            "SELECT payload_ref, wrapped_dek "
                            "FROM memories WHERE payload_ref = ANY($1::text[])",
                            all_payload_refs,
                        )
                    for dek_row in dek_rows:
                        wd = dek_row["wrapped_dek"]
                        wrapped_by_ref[str(dek_row["payload_ref"])] = (
                            bytes(wd) if wd is not None else None
                        )
                except Exception as _dek_exc:
                    # If the wrapped_dek fetch fails for any reason (e.g. schema
                    # mismatch or test mock), fall back to treating all rows as
                    # legacy plaintext (wrapped_dek=None → back-compat).
                    log.debug(
                        "Re-embed: wrapped_dek batch fetch failed, "
                        "treating batch as legacy plaintext: %s",
                        _dek_exc,
                    )
                    wrapped_by_ref = {}

            # Resolve text for each row —————————————————————————————————————
            mongo_texts: dict[str, str] = {}
            decrypt_failed: set[str] = set()
            if mongo_client is not None:
                mongo_texts, decrypt_failed = await _resolve_texts_from_mongo(
                    mongo_client, rows, self.max_text_chars, wrapped_by_ref
                )

            # Flag dek_unreadable for any refs that failed decryption.
            if decrypt_failed:
                async with pool.acquire(timeout=10.0) as flag_conn:
                    for failed_ref in decrypt_failed:
                        ENVELOPE_DECRYPT_FAILURES.labels(consumer="reembedding_worker").inc()
                        await flag_conn.execute(
                            "UPDATE memories "
                            "SET metadata = jsonb_set("
                            "    COALESCE(metadata, '{}'::jsonb),"
                            "    '{dek_unreadable}', 'true'::jsonb, true"
                            ") WHERE payload_ref = $1",
                            failed_ref,
                        )

            texts: list[str] = []
            selected: list[tuple[uuid.UUID, datetime]] = []

            for row in rows:
                ref = row.get("payload_ref") or ""
                # Skip refs that failed decryption — already flagged above.
                if ref in decrypt_failed:
                    continue
                text = mongo_texts.get(ref) or _fallback_text(row, self.max_text_chars)
                if not text:
                    log.debug("Re-embed: skipping memory %s — no text available.", row["id"])
                    continue
                texts.append(text)
                selected.append((row["id"], row["created_at"]))

            if texts:
                vectors = await self._embed(pool, texts)

                update_batch = [
                    (mem_id, created_at, vec)
                    for (mem_id, created_at), vec in zip(selected, vectors)
                ]

                async with pool.acquire(timeout=10.0) as conn:
                    await _update_memories_batch(conn, update_batch, model_uuid)

                memories_done += len(update_batch)

            # Advance cursor ————————————————————————————————————————————————
            last = rows[-1]
            cursor_created_at = last["created_at"]
            cursor_id = last["id"]

            async with pool.acquire(timeout=10.0) as conn:
                await _checkpoint(
                    conn,
                    run_id,
                    memories_done,
                    0,
                    cursor_created_at,
                    cursor_id,
                )

            log.info(
                "Re-embed: %d memories updated this run (batch=%d).",
                memories_done,
                len(texts),
            )

            # Rate-limit: honour REEMBED_BATCHES_PER_MINUTE ————————————————
            await asyncio.sleep(self._sleep)

        return memories_done

    # ---------------------------------------------------------------------- #
    # Phase B — kg_nodes (optional)
    # ---------------------------------------------------------------------- #

    async def _run_kg_nodes_phase(
        self,
        pool: asyncpg.Pool,
        run_id: uuid.UUID,
        memories_done: int,
        model_uuid: uuid.UUID,
    ) -> int:
        kg_cursor_id: uuid.UUID | None = None
        kg_nodes_done = 0

        while True:
            async with pool.acquire(timeout=10.0) as conn:
                async with conn.transaction():
                    rows = await _fetch_kg_nodes_batch(
                        conn, model_uuid, self.batch_size, kg_cursor_id
                    )

            if not rows:
                break

            texts = [row["label"][: self.max_text_chars] for row in rows]
            vectors = await self._embed(pool, texts)

            batch = [(row["id"], vec) for row, vec in zip(rows, vectors)]

            async with pool.acquire(timeout=10.0) as conn:
                await _update_kg_nodes_batch(conn, batch, model_uuid)

            kg_nodes_done += len(batch)
            kg_cursor_id = rows[-1]["id"]

            async with pool.acquire(timeout=10.0) as conn:
                await _checkpoint(
                    conn,
                    run_id,
                    memories_done,
                    kg_nodes_done,
                    None,
                    None,
                )

            log.info("Re-embed: %d kg_nodes updated this run.", kg_nodes_done)
            await asyncio.sleep(self._sleep)

        return kg_nodes_done

    # ---------------------------------------------------------------------- #
    # Public entry point
    # ---------------------------------------------------------------------- #

    async def run_once(
        self,
        pool: asyncpg.Pool,
        mongo_client: Any = None,
    ) -> dict[str, Any]:
        """
        Run one full re-embedding sweep.

        Parameters
        ----------
        pool:
            asyncpg connection pool (already connected).
        mongo_client:
            Motor ``AsyncIOMotorClient`` (or compatible).  Pass ``None`` in
            tests; the worker falls back to ``name + filepath`` text.

        Returns
        -------
        dict with keys: run_id, status, memories_done, kg_nodes_done.
        """
        model_uuid = current_model_uuid()
        run_id = await self._create_run(pool, model_uuid)

        log.info(
            "Re-embedding run %s started | model=%s | batch=%d | rate=%d/min",
            run_id,
            MODEL_ID,
            self.batch_size,
            int(60.0 / self._sleep),
        )

        memories_done = 0
        kg_nodes_done = 0

        try:
            memories_done = await self._run_memories_phase(pool, mongo_client, model_uuid, run_id)

            if self.include_kg_nodes:
                kg_nodes_done = await self._run_kg_nodes_phase(
                    pool, run_id, memories_done, model_uuid
                )

            await self._close_run(pool, run_id, "completed", memories_done, kg_nodes_done)
            log.info(
                "Re-embedding run %s completed | memories=%d kg_nodes=%d",
                run_id,
                memories_done,
                kg_nodes_done,
            )
            return {
                "run_id": str(run_id),
                "status": "completed",
                "memories_done": memories_done,
                "kg_nodes_done": kg_nodes_done,
            }

        except VRAMPressureError as exc:
            # Pause-don't-poison: VRAM was saturated for the full wait window.
            # Close the run as 'vram_paused' so operators can see it in the
            # audit table, but do NOT re-raise — the keyset cursor is already
            # checkpointed and the next cron tick will resume where it left off.
            await self._close_run(
                pool,
                run_id,
                "vram_paused",
                memories_done,
                kg_nodes_done,
                error=str(exc)[:2048],
            )
            log.warning(
                "Re-embedding run %s paused due to VRAM pressure — will resume next tick: %s",
                run_id,
                exc,
            )
            return {
                "run_id": str(run_id),
                "status": "vram_paused",
                "memories_done": memories_done,
                "kg_nodes_done": kg_nodes_done,
            }

        except Exception as exc:
            await self._close_run(
                pool,
                run_id,
                "failed",
                memories_done,
                kg_nodes_done,
                error=str(exc)[:2048],
            )
            log.exception("Re-embedding run %s failed", run_id)
            raise


# --------------------------------------------------------------------------- #
# Standalone entry point (``python -m nce.reembedding_worker``)
# --------------------------------------------------------------------------- #


async def async_main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [nce.reembedding] %(levelname)s %(message)s",
    )
    from nce.config import cfg

    cfg.validate()

    # R4 / VI.4: connect as the least-privilege worker principal
    # (``nce_gc`` via NCE_GC_DSN) when provisioned; falls back to the app DSN
    # (``nce_app``) when NCE_GC_DSN is unset.  Only the standalone entry point
    # owns its pool — when driven by ``nce/cron.py`` the worker reuses the
    # pool the caller passes to ``run_once`` (see module docstring / cron).
    pool = await asyncpg.create_pool(
        resolve_worker_dsn(),
        min_size=1,
        max_size=4,
        command_timeout=120,
    )

    mongo_client: Any = None
    try:
        from motor.motor_asyncio import AsyncIOMotorClient

        mongo_client = AsyncIOMotorClient(cfg.MONGO_URI, serverSelectionTimeoutMS=5_000)
    except ImportError:
        log.warning("motor not available — re-embedding will use fallback text only.")

    worker = ReembeddingWorker()
    try:
        stats = await worker.run_once(pool, mongo_client)
        log.info("Done: %s", stats)
    finally:
        await pool.close()
        if mongo_client:
            mongo_client.close()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
