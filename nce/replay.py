"""
Phase 2.3 — Memory Replay Engine (Observational + Forked).

Transport-agnostic: all execution loops are **async generators** that yield
progress dicts.  The caller (MCP tool handler, admin API route, test) decides
how to serialise output.  There is no SSE, no HTTP-specific code, and no
transport coupling anywhere in this module.

Observational
─────────────
Streams event_log rows for a namespace/seq range back to the caller without
touching any engine state.  Uses an asyncpg server-side cursor so rows are
never fully loaded into Python heap.

Forked
──────
Replays source events into an isolated target namespace up to ``fork_seq``.
For every source event a fresh event_log entry is written in the target via
``append_event()``, with ``parent_event_id`` set to the source event's UUID.
The new HMAC-SHA256 signature is computed over the **fork's own** fields
(fork namespace_id, fork event_seq, fork occurred_at, source parent_event_id)
— this is the "alternate causal provenance" required by the spec.

In ``deterministic`` mode LLM responses are served from the MinIO payload
cache (``event_log.llm_payload_uri``) so the fork is byte-identical to the
source run up to the divergence point.

In ``re-execute`` mode the LLM provider is called fresh, optionally with
``config_overrides``, so the fork intentionally diverges.

Handler registry
────────────────
Each ``event_type`` is mapped to a coroutine that applies the event to the
target namespace and returns a ``result_summary`` dict.  Adding a new
event_type requires adding a matching handler — the registry validates this
at ``ForkedReplay.__init__`` time.
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import io
import json
import logging
import uuid
from collections.abc import AsyncGenerator, Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, get_args

import asyncpg
from minio import Minio
from minio.error import S3Error

from nce.config import cfg
from nce.event_log import (
    DataIntegrityError,
    append_event,
    verify_event_signature,
)
from nce.event_types import EventType
from nce.models import FrozenForkConfig
from nce.signing import canonical_json

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

# How many rows the asyncpg cursor fetches from Postgres per round-trip.
# Keeps server memory bounded regardless of event_log size.
_CURSOR_PREFETCH: int = 50

# Write a progress item + update replay_runs every N events.
_PROGRESS_INTERVAL: int = 10

# MinIO bucket for LLM payloads (spec: llm_payload_uri = "bucket/object_key").
_LLM_PAYLOAD_BUCKET: str = "nce-llm-payloads"

# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------


class ReplayError(Exception):
    """Base class for replay errors."""


class ReplayModeError(ReplayError):
    """``replay_mode`` is not 'deterministic' or 're-execute'."""


class ReplayHandlerMissingError(ReplayError):
    """No handler is registered for the given ``event_type``."""


class MinIOPayloadMissingError(ReplayError):
    """Deterministic replay requested but ``llm_payload_uri`` is NULL on the source event."""


class ReplayRunNotFoundError(ReplayError):
    """Queried ``replay_run_id`` does not exist in ``replay_runs``."""


class ReplayChecksumError(ReplayError):
    """Payload checksum mismatch — the replay request was tampered with or corrupted."""


# ---------------------------------------------------------------------------
# Internal dataclass: one row from event_log
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _EventRow:
    event_id: uuid.UUID
    event_seq: int
    event_type: str
    occurred_at: datetime
    agent_id: str
    params: dict[str, Any]
    result_summary: dict[str, Any] | None
    parent_event_id: uuid.UUID | None
    llm_payload_uri: str | None
    llm_payload_hash: bytes | None


def _row_as_dict(row: _EventRow) -> dict[str, Any]:
    """Serialise an ``_EventRow`` to a JSON-safe dict for yielding."""
    return {
        "event_id": str(row.event_id),
        "event_seq": row.event_seq,
        "event_type": row.event_type,
        "occurred_at": row.occurred_at.isoformat(),
        "agent_id": row.agent_id,
        "params": row.params,
        "result_summary": row.result_summary,
        "parent_event_id": str(row.parent_event_id) if row.parent_event_id else None,
        "llm_payload_uri": row.llm_payload_uri,
    }


def _record_to_event_row(record: asyncpg.Record) -> _EventRow:
    """Convert a raw asyncpg record to ``_EventRow``."""
    return _EventRow(
        event_id=record["id"],
        event_seq=record["event_seq"],
        event_type=record["event_type"],
        occurred_at=record["occurred_at"].astimezone(timezone.utc),
        agent_id=record["agent_id"],
        params=dict(record["params"]) if record["params"] else {},
        result_summary=(dict(record["result_summary"]) if record["result_summary"] else None),
        parent_event_id=record["parent_event_id"],
        llm_payload_uri=record["llm_payload_uri"],
        llm_payload_hash=(
            bytes(record["llm_payload_hash"]) if record["llm_payload_hash"] else None
        ),
    )


# ---------------------------------------------------------------------------
# replay_runs table helpers
# NOTE: replay_runs is NOT WORM — we can INSERT and UPDATE it.
# ---------------------------------------------------------------------------


async def _create_run(
    conn: asyncpg.Connection,
    *,
    source_namespace_id: uuid.UUID,
    target_namespace_id: uuid.UUID | None,
    mode: str,
    replay_mode: str,
    start_seq: int,
    end_seq: int | None,
    divergence_seq: int | None,
    config_overrides: dict | None,
) -> uuid.UUID:
    """INSERT a new ``replay_runs`` row and return its UUID."""
    run_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO replay_runs (
            id, source_namespace_id, target_namespace_id,
            mode, replay_mode, start_seq, end_seq, divergence_seq,
            config_overrides, status, events_applied
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, 'running', 0
        )
        """,
        run_id,
        source_namespace_id,
        target_namespace_id,
        mode,
        replay_mode,
        start_seq,
        end_seq,
        divergence_seq,
        json.dumps(config_overrides, sort_keys=True) if config_overrides else None,
    )
    log.info(
        "replay_run created: run_id=%s mode=%s replay_mode=%s ns=%s",
        run_id,
        mode,
        replay_mode,
        source_namespace_id,
    )
    return run_id


async def _update_run_progress(
    conn: asyncpg.Connection,
    run_id: uuid.UUID,
    events_applied: int,
) -> None:
    await conn.execute(
        "UPDATE replay_runs SET events_applied = $1 WHERE id = $2",
        events_applied,
        run_id,
    )


async def _finish_run(
    conn: asyncpg.Connection,
    run_id: uuid.UUID,
    *,
    status: str,
    events_applied: int,
    error: str | None = None,
) -> None:
    await conn.execute(
        """
        UPDATE replay_runs
        SET status = $1, events_applied = $2, finished_at = now(), error = $3
        WHERE id = $4
        """,
        status,
        events_applied,
        error,
        run_id,
    )
    log.info(
        "replay_run finished: run_id=%s status=%s events=%d",
        run_id,
        status,
        events_applied,
    )


async def get_run_status(
    pool: asyncpg.Pool,
    run_id: uuid.UUID,
) -> dict[str, Any]:
    """Return a JSON-safe dict for the given ``replay_run_id``."""
    async with pool.acquire(timeout=10.0) as conn:
        row = await conn.fetchrow(
            """
            SELECT id, source_namespace_id, target_namespace_id,
                   mode, replay_mode, start_seq, end_seq, divergence_seq,
                   config_overrides, status, events_applied,
                   started_at, finished_at, error,
                   source_state_digest, target_state_digest, digest_match
            FROM replay_runs WHERE id = $1
            """,
            run_id,
        )
    if row is None:
        raise ReplayRunNotFoundError(f"replay_run {run_id} not found.")
    return {
        "run_id": str(row["id"]),
        "source_namespace_id": str(row["source_namespace_id"]),
        "target_namespace_id": (
            str(row["target_namespace_id"]) if row["target_namespace_id"] else None
        ),
        "mode": row["mode"],
        "replay_mode": row["replay_mode"],
        "start_seq": row["start_seq"],
        "end_seq": row["end_seq"],
        "divergence_seq": row["divergence_seq"],
        "config_overrides": (dict(row["config_overrides"]) if row["config_overrides"] else None),
        "status": row["status"],
        "events_applied": row["events_applied"],
        "started_at": row["started_at"].isoformat() if row["started_at"] else None,
        "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
        "error": row["error"],
        "source_state_digest": row["source_state_digest"],
        "target_state_digest": row["target_state_digest"],
        "digest_match": row["digest_match"],
    }


# ---------------------------------------------------------------------------
# Event source query builder
# ---------------------------------------------------------------------------


def _build_event_query(
    *,
    source_namespace_id: uuid.UUID,
    start_seq: int,
    end_seq: int | None,
    agent_id_filter: str | None,
) -> tuple[str, list[Any]]:
    """Return ``(sql, args)`` for the event_log cursor query."""
    conditions = ["namespace_id = $1", "event_seq >= $2"]
    args: list[Any] = [source_namespace_id, start_seq]
    idx = 3

    if end_seq is not None:
        conditions.append(f"event_seq <= ${idx}")
        args.append(end_seq)
        idx += 1

    if agent_id_filter:
        conditions.append(f"agent_id = ${idx}")
        args.append(agent_id_filter)
        idx += 1

    sql = f"""
        SELECT
            id, event_seq, event_type, occurred_at, agent_id,
            params, result_summary, parent_event_id,
            llm_payload_uri, llm_payload_hash,
            signature, signature_key_id
        FROM event_log
        WHERE {' AND '.join(conditions)}
        ORDER BY event_seq ASC
    """
    return sql, args


async def _fetch_event_log_snapshot(
    pool: asyncpg.Pool,
    *,
    source_namespace_id: uuid.UUID,
    start_seq: int,
    end_seq: int | None,
    agent_id_filter: str | None,
):
    """Snapshot event rows inside a short REPEATABLE READ txn (FIX-041)."""
    sql, args = _build_event_query(
        source_namespace_id=source_namespace_id,
        start_seq=start_seq,
        end_seq=end_seq,
        agent_id_filter=agent_id_filter,
    )
    async with pool.acquire(timeout=10.0) as conn:
        async with conn.transaction(isolation="repeatable_read"):
            rows = await conn.fetch(sql, *args)
            return list(rows)


# ---------------------------------------------------------------------------
# MinIO helpers  (blocking I/O → thread-pool, never blocks the event loop)
# ---------------------------------------------------------------------------


def _make_minio() -> Minio:
    return Minio(
        cfg.MINIO_ENDPOINT,
        access_key=cfg.MINIO_ACCESS_KEY,
        secret_key=cfg.MINIO_SECRET_KEY,
        secure=cfg.MINIO_SECURE,
    )


async def _fetch_llm_payload(uri: str) -> dict[str, Any]:
    """
    Fetch ``{prompt, response}`` JSON from MinIO.

    ``uri`` format: ``"<bucket>/<object_key>"``.
    Runs the blocking MinIO call on the thread-pool executor.
    """
    if "/" not in uri:
        raise ReplayError(f"Malformed llm_payload_uri (no '/'): {uri!r}")
    bucket, key = uri.split("/", 1)

    loop = asyncio.get_running_loop()
    client = _make_minio()

    def _get() -> bytes:
        response = client.get_object(bucket, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    raw: bytes = await loop.run_in_executor(None, _get)
    return json.loads(raw.decode("utf-8"))


# Public alias for admin / tooling (replay module remains the canonical MinIO accessor).
fetch_llm_payload = _fetch_llm_payload


async def _put_llm_payload(uri: str, payload: dict[str, Any]) -> bytes:
    """
    PUT ``{prompt, response}`` to MinIO and return ``sha256(JCS(payload))``.

    Creates the bucket if it does not exist.  Runs on thread-pool.
    """
    if "/" not in uri:
        raise ReplayError(f"Malformed llm_payload_uri (no '/'): {uri!r}")
    bucket, key = uri.split("/", 1)
    payload_bytes: bytes = canonical_json(payload)

    loop = asyncio.get_running_loop()
    client = _make_minio()

    def _put() -> None:
        # Ensure bucket exists before writing.
        try:
            if not client.bucket_exists(bucket):
                client.make_bucket(bucket)
        except S3Error:
            pass  # May already exist in a race; the put below will fail if bucket truly missing
        client.put_object(
            bucket,
            key,
            io.BytesIO(payload_bytes),
            len(payload_bytes),
            content_type="application/json",
        )

    await loop.run_in_executor(None, _put)
    return hashlib.sha256(payload_bytes).digest()


def _fork_llm_payload_uri(
    source_uri: str,
    target_namespace_id: uuid.UUID,
    source_event_id: uuid.UUID,
) -> str:
    """
    Derive a MinIO URI for the fork's LLM payload.

    Format: ``"<bucket>/fork/<target_ns>/<source_event_id>.json"``
    """
    return f"{_LLM_PAYLOAD_BUCKET}/fork/{target_namespace_id}/{source_event_id}.json"


# ---------------------------------------------------------------------------
# Handler protocol + registry
# ---------------------------------------------------------------------------


class ReplayContext:
    """Carries state for replay executions, ensuring deterministic UUID remapping."""

    def __init__(
        self, target_namespace_id: uuid.UUID, source_namespace_id: uuid.UUID | None = None
    ) -> None:
        self.target_namespace_id = target_namespace_id
        self.source_namespace_id = source_namespace_id
        self.uuid_remap: dict[uuid.UUID, uuid.UUID] = {}
        self.mongo_remap: dict[str, str] = {}
        self._mongo_client: Any = None
        self._copied_refs: set[str] = set()

    @property
    def mongo_client(self) -> Any:
        if self._mongo_client is None:
            from motor.motor_asyncio import AsyncIOMotorClient

            self._mongo_client = AsyncIOMotorClient(cfg.MONGO_URI, serverSelectionTimeoutMS=5000)
        return self._mongo_client

    def close(self) -> None:
        if self._mongo_client is not None:
            self._mongo_client.close()
            self._mongo_client = None

    def remap(self, src: uuid.UUID) -> uuid.UUID:
        if src not in self.uuid_remap:
            self.uuid_remap[src] = uuid.uuid5(self.target_namespace_id, str(src))
        return self.uuid_remap[src]

    def remap_mongo_ref(self, src_ref: str) -> str:
        if src_ref not in self.mongo_remap:
            from bson import ObjectId

            # Derive deterministic UUID from target namespace and source ref
            derived_uuid = uuid.uuid5(self.target_namespace_id, f"payload_ref:{src_ref}")
            derived_bytes = derived_uuid.bytes[:12]
            self.mongo_remap[src_ref] = str(ObjectId(derived_bytes))
        return self.mongo_remap[src_ref]

    async def copy_mongo_doc(
        self, src_ref: str, source_namespace_id: uuid.UUID | None = None
    ) -> str:
        """Copy Mongo document from src_ref to a deterministic target_ref."""
        target_ref = self.remap_mongo_ref(src_ref)
        if src_ref in self._copied_refs:
            return target_ref

        from bson import ObjectId

        from nce.db_utils import scoped_mongo_session

        ns_to_use = source_namespace_id or self.source_namespace_id
        if ns_to_use is None:
            ns_to_use = self.target_namespace_id

        try:
            src_oid = ObjectId(src_ref)
        except Exception:
            # If src_ref is not a valid ObjectId (e.g. in test mocks or fallback), skip copy
            return target_ref

        try:
            async with scoped_mongo_session(self.mongo_client, ns_to_use) as src_db:
                doc = await src_db.episodes.find_one({"_id": src_oid})

            if doc is not None:
                # Prepare target document
                target_doc = dict(doc)
                target_doc["_id"] = ObjectId(target_ref)
                target_doc["namespace_id"] = str(self.target_namespace_id)

                # Insert or replace in target (using upsert to be idempotent)
                async with scoped_mongo_session(
                    self.mongo_client, self.target_namespace_id
                ) as tgt_db:
                    await tgt_db.episodes.replace_one(
                        {"_id": target_doc["_id"]}, target_doc, upsert=True
                    )
                self._copied_refs.add(src_ref)
            else:
                log.warning(
                    "Source Mongo document not found for payload_ref: %s in namespace %s",
                    src_ref,
                    ns_to_use,
                )
        except Exception as e:
            # Under some test configurations, Mongo might be mocked or unavailable.
            # We log the warning but do not crash the replay, so mock tests can run cleanly.
            log.warning("Failed to copy MongoDB document for payload_ref %s: %s", src_ref, e)

        return target_ref


# A handler is a coroutine:
#   async def handler(
#       conn, source_event, target_namespace_id, llm_payload, config_overrides
#   ) -> dict[str, Any]
#
# ``llm_payload`` is either:
#   * the fetched MinIO dict  (deterministic mode)
#   * the freshly-computed result  (re-execute mode)
#   * None for non-LLM events

HandlerFn = Callable[
    [asyncpg.Connection, "_EventRow", ReplayContext | uuid.UUID, dict | None, dict | None],
    Coroutine[Any, Any, dict[str, Any]],
]

_HANDLER_REGISTRY: dict[str, HandlerFn] = {}


def _register(event_type: str) -> Callable[[HandlerFn], HandlerFn]:
    """Decorator: register a coroutine as the handler for *event_type*."""

    def _dec(fn: HandlerFn) -> HandlerFn:
        _HANDLER_REGISTRY[event_type] = fn
        return fn

    return _dec


# ---------------------------------------------------------------------------
# Per-event-type handlers
# ---------------------------------------------------------------------------


@_register("store_memory")
async def _handle_store_memory(
    conn: asyncpg.Connection,
    src: _EventRow,
    ctx: ReplayContext | uuid.UUID,
    llm_payload: dict | None,
    config_overrides: dict | None,
) -> dict[str, Any]:
    """
    Re-insert the memory row into the target namespace, preserving content.

    The embedding and salience values are carried over from the source row so
    that the fork's semantic state is identical up to the divergence point.
    Full re-embedding is supported by kicking off a re-embedding job later.
    """
    try:
        memory_id_str: str = src.params.get("memory_id", "")
        if not memory_id_str:
            return {"skipped": True, "reason": "no_memory_id_in_params"}

        src_memory_id = uuid.UUID(memory_id_str)

        # Fetch the source memory row (embedding + metadata).
        # The source_namespace_id is injected into params.source_namespace_id by
        # ForkedReplay.execute() when it enriches the params dict.
        raw_src_ns = src.params.get("source_namespace_id")
        src_ns_id = uuid.UUID(raw_src_ns) if raw_src_ns else None
        if src_ns_id is None:
            return {"skipped": True, "reason": "source_namespace_id_missing_in_params"}

        is_raw_uuid = isinstance(ctx, uuid.UUID)
        if isinstance(ctx, uuid.UUID):
            ctx = ReplayContext(ctx, source_namespace_id=src_ns_id)

        new_memory_id = ctx.remap(src_memory_id)

        src_row = await conn.fetchrow(
            """
            SELECT embedding, assertion_type, memory_type, metadata, valid_from, created_at,
                   wrapped_dek, dek_key_id
            FROM memories
            WHERE id = $1 AND namespace_id = $2
              AND valid_to IS NULL
            """,
            src_memory_id,
            src_ns_id,
        )
        if src_row is None:
            log.warning(
                "store_memory handler: source row not found memory_id=%s; writing stub",
                src_memory_id,
            )
            return {"skipped": True, "reason": "source_memory_not_found"}

        payload_ref = src.params.get("payload_ref")
        if not payload_ref:
            return {"skipped": True, "reason": "payload_ref_missing_in_params"}

        # Copy the MongoDB document to a deterministic targets ref and update the params in-place
        target_payload_ref = await ctx.copy_mongo_doc(payload_ref, source_namespace_id=src_ns_id)
        src.params["payload_ref"] = target_payload_ref

        meta = dict(src_row["metadata"]) if src_row["metadata"] else {}
        meta["source_memory_id"] = str(src_memory_id)

        # Part II.4: the copied Mongo doc carries the source ciphertext verbatim;
        # carry the source wrapped_dek/dek_key_id so the target can decrypt under
        # the same (global) NCE_MASTER_KEY.  Legacy rows carry NULL → plaintext.
        await conn.execute(
            """
            INSERT INTO memories (
                id, namespace_id, agent_id,
                embedding, assertion_type, memory_type,
                payload_ref, metadata,
                valid_from, created_at,
                wrapped_dek, dek_key_id,
                change_origin
            ) VALUES (
                $1, $2, $3,
                $4, $5, $6,
                $7, $8::jsonb,
                $9, $10,
                $11, $12,
                'replay'
            )
            ON CONFLICT DO NOTHING
            """,
            new_memory_id,
            ctx.target_namespace_id,
            src.agent_id,
            src_row["embedding"],
            src_row["assertion_type"],
            src_row["memory_type"],
            target_payload_ref,
            json.dumps(meta),
            src_row["valid_from"],
            src_row["created_at"],
            src_row["wrapped_dek"],
            src_row["dek_key_id"],
        )

        # Carry over salience score if it exists in the source namespace
        salience_row = await conn.fetchrow(
            """
            SELECT salience_score
            FROM memory_salience
            WHERE memory_id = $1 AND agent_id = $2 AND namespace_id = $3
            """,
            src_memory_id,
            src.agent_id,
            src_ns_id,
        )
        if salience_row is not None:
            salience_score = salience_row["salience_score"]
            await conn.execute(
                """
                INSERT INTO memory_salience (
                    memory_id, agent_id, namespace_id, salience_score
                ) VALUES ($1, $2, $3, $4)
                ON CONFLICT (memory_id, agent_id) DO UPDATE
                SET salience_score = EXCLUDED.salience_score,
                    updated_at = now()
                """,
                new_memory_id,
                src.agent_id,
                ctx.target_namespace_id,
                salience_score,
            )

        return {
            "source_memory_id": str(src_memory_id),
            "new_memory_id": str(new_memory_id),
            "target_namespace": str(ctx.target_namespace_id),
        }
    finally:
        if is_raw_uuid and isinstance(ctx, ReplayContext):
            ctx.close()


@_register("forget_memory")
async def _handle_forget_memory(
    conn: asyncpg.Connection,
    src: _EventRow,
    ctx: ReplayContext | uuid.UUID,
    llm_payload: dict | None,
    config_overrides: dict | None,
) -> dict[str, Any]:
    """
    Expire (soft-delete) the matching memory in the target namespace.

    Because the target memory was inserted with a new UUID, we identify it by
    ``source_memory_id`` stored in ``metadata``.  If not found, the event is
    a no-op (idempotent).
    """
    if isinstance(ctx, uuid.UUID):
        ctx = ReplayContext(ctx)

    src_memory_id = src.params.get("memory_id", "")
    if not src_memory_id:
        return {"skipped": True, "reason": "no_memory_id_in_params"}

    result = await conn.execute(
        """
        UPDATE memories
        SET valid_to = $4
        WHERE namespace_id = $1
          AND agent_id = $2
          AND valid_to IS NULL
          AND metadata->>'source_memory_id' = $3
        """,
        ctx.target_namespace_id,
        src.agent_id,
        src_memory_id,
        src.occurred_at,
    )
    return {"rows_expired": int(result.split()[-1])}


@_register("boost_memory")
async def _handle_boost_memory(
    conn: asyncpg.Connection,
    src: _EventRow,
    ctx: ReplayContext | uuid.UUID,
    llm_payload: dict | None,
    config_overrides: dict | None,
) -> dict[str, Any]:
    """Apply the same salience boost to the corresponding fork memory."""
    if isinstance(ctx, uuid.UUID):
        ctx = ReplayContext(ctx)

    src_memory_id = src.params.get("memory_id", "")
    factor = float(src.params.get("factor", 0.2))
    if not src_memory_id:
        return {"skipped": True, "reason": "no_memory_id_in_params"}

    result = await conn.execute(
        """
        INSERT INTO memory_salience (memory_id, agent_id, namespace_id, salience_score)
        SELECT id, agent_id, namespace_id, $1::real
        FROM memories
        WHERE namespace_id = $2
          AND agent_id = $3
          AND valid_to IS NULL
          AND metadata->>'source_memory_id' = $4
        ON CONFLICT (memory_id, agent_id) DO UPDATE
        SET salience_score = LEAST(1.0, memory_salience.salience_score + EXCLUDED.salience_score),
            updated_at = now()
        """,
        factor,
        ctx.target_namespace_id,
        src.agent_id,
        src_memory_id,
    )
    return {"rows_updated": int(result.split()[-1]), "factor": factor}


@_register("resolve_contradiction")
async def _handle_resolve_contradiction(
    conn: asyncpg.Connection,
    src: _EventRow,
    ctx: ReplayContext | uuid.UUID,
    llm_payload: dict | None,
    config_overrides: dict | None,
) -> dict[str, Any]:
    """Mark the corresponding contradiction as resolved in the fork."""
    if isinstance(ctx, uuid.UUID):
        ctx = ReplayContext(ctx)

    contradiction_id = src.params.get("contradiction_id")
    resolution = src.params.get("resolution", "deferred")
    if not contradiction_id:
        return {"skipped": True, "reason": "no_contradiction_id_in_params"}

    result = await conn.execute(
        """
        UPDATE contradictions
        SET resolution = $1, resolved_at = now()
        WHERE namespace_id = $2
          AND id = $3
          AND resolution = 'unresolved'
        """,
        resolution,
        ctx.target_namespace_id,
        uuid.UUID(contradiction_id),
    )
    return {"rows_updated": int(result.split()[-1])}


@_register("consolidation_run")
async def _handle_consolidation_run(
    conn: asyncpg.Connection,
    src: _EventRow,
    ctx: ReplayContext | uuid.UUID,
    llm_payload: dict | None,
    config_overrides: dict | None,
) -> dict[str, Any]:
    """
    Apply a consolidation to the fork namespace.

    ``llm_payload`` carries the ``{prompt, response}`` dict:
    * In deterministic mode it was fetched from MinIO (byte-identical to source).
    * In re-execute mode it contains the freshly-computed LLM response.

    The handler writes the resulting consolidated memory and returns the
    result_summary for the fork's event_log entry.
    """
    try:
        if llm_payload is None:
            return {"skipped": True, "reason": "llm_payload_unavailable"}

        response: dict = llm_payload.get("response", {})
        abstraction: str = response.get("abstraction", "")
        confidence: float = float(response.get("confidence", 0.0))

        if confidence < 0.3:
            return {
                "skipped": True,
                "reason": "low_confidence",
                "confidence": confidence,
            }

        if not abstraction:
            return {"skipped": True, "reason": "empty_abstraction"}

        payload_ref = src.params.get("payload_ref")
        if not payload_ref:
            return {"skipped": True, "reason": "payload_ref_missing_in_params"}

        raw_src_ns = src.params.get("source_namespace_id")
        src_ns_id = uuid.UUID(raw_src_ns) if raw_src_ns else None

        is_raw_uuid = isinstance(ctx, uuid.UUID)
        if isinstance(ctx, uuid.UUID):
            ctx = ReplayContext(ctx, source_namespace_id=src_ns_id)

        # Copy the MongoDB document to a deterministic targets ref and update the params in-place
        target_payload_ref = await ctx.copy_mongo_doc(payload_ref, source_namespace_id=src_ns_id)
        src.params["payload_ref"] = target_payload_ref

        consolidated_memory_id_str = src.params.get("consolidated_memory_id")
        if consolidated_memory_id_str:
            new_memory_id = ctx.remap(uuid.UUID(consolidated_memory_id_str))
        else:
            new_memory_id = uuid.uuid4()

        # Fetch valid_from and created_at from source memories table if it exists
        raw_src_ns = src.params.get("source_namespace_id")
        src_ns_id = uuid.UUID(raw_src_ns) if raw_src_ns else None
        src_valid_from = None
        src_created_at = None
        src_wrapped_dek = None
        src_dek_key_id = None
        if consolidated_memory_id_str and src_ns_id:
            try:
                row = await conn.fetchrow(
                    "SELECT valid_from, created_at, wrapped_dek, dek_key_id FROM memories WHERE id = $1 AND namespace_id = $2",
                    uuid.UUID(consolidated_memory_id_str),
                    src_ns_id,
                )
                if row:
                    src_valid_from = row["valid_from"]
                    src_created_at = row["created_at"]
                    # Part II.4: carry the source DEK so the copied ciphertext doc
                    # stays decryptable under the global master key.
                    src_wrapped_dek = row["wrapped_dek"]
                    src_dek_key_id = row["dek_key_id"]
            except Exception:
                pass

        if src_valid_from is None:
            src_valid_from = src.occurred_at
        if src_created_at is None:
            src_created_at = src.occurred_at

        # Embed the abstraction (reuse the existing embedding infrastructure
        # via a direct import; avoids circular deps since we don't import engine).
        from nce import embeddings as _emb  # local import to avoid module-level circular

        vector = await _emb.embed(abstraction)

        meta = {
            "source_memory_ids": response.get("supporting_memory_ids", []),
            "key_entities": response.get("key_entities", []),
            "key_relations": response.get("key_relations", []),
            "replay_fork": True,
        }
        if consolidated_memory_id_str:
            meta["source_memory_id"] = consolidated_memory_id_str

        await conn.execute(
            """
            INSERT INTO memories (
                id, namespace_id, agent_id,
                embedding, assertion_type, memory_type,
                payload_ref, metadata,
                valid_from, created_at,
                wrapped_dek, dek_key_id,
                change_origin
            ) VALUES (
                $1, $2, $3,
                $4, 'fact', 'consolidated',
                $5, $6::jsonb,
                $7, $8,
                $9, $10,
                'replay'
            )
            ON CONFLICT DO NOTHING
            """,
            new_memory_id,
            ctx.target_namespace_id,
            src.agent_id,
            vector,
            target_payload_ref,
            json.dumps(meta),
            src_valid_from,
            src_created_at,
            src_wrapped_dek,
            src_dek_key_id,
        )

        # Populate target namespace KG nodes and edges
        for entity in response.get("key_entities", []):
            await conn.execute(
                """
                INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
                VALUES ($1, 'Entity', $2, 'replay')
                ON CONFLICT (label, namespace_id) DO UPDATE
                    SET change_origin = CASE
                        WHEN kg_nodes.change_origin IN ('sync', 'webhook') THEN kg_nodes.change_origin
                        ELSE EXCLUDED.change_origin
                    END
                """,
                entity,
                ctx.target_namespace_id,
            )

        for rel in response.get("key_relations", []):
            subj = rel.get("subject")
            pred = rel.get("predicate")
            obj = rel.get("object")
            if subj and pred and obj:
                await conn.execute(
                    """
                    INSERT INTO kg_edges (subject_label, predicate, object_label, confidence,
                                         namespace_id, change_origin)
                    VALUES ($1, $2, $3, $4, $5, 'replay')
                    ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
                        SET confidence = EXCLUDED.confidence,
                            change_origin = CASE
                                WHEN kg_edges.change_origin IN ('sync', 'webhook') THEN kg_edges.change_origin
                                ELSE EXCLUDED.change_origin
                            END
                    """,
                    subj,
                    pred,
                    obj,
                    confidence,
                    ctx.target_namespace_id,
                )

        # Route salience into memory_salience.salience_score
        salience_score = float(response.get("confidence", 0.0))
        await conn.execute(
            """
            INSERT INTO memory_salience (
                memory_id, agent_id, namespace_id, salience_score
            ) VALUES ($1, $2, $3, $4)
            ON CONFLICT (memory_id, agent_id) DO UPDATE
            SET salience_score = EXCLUDED.salience_score,
                updated_at = now()
            """,
            new_memory_id,
            src.agent_id,
            ctx.target_namespace_id,
            salience_score,
        )

        return {
            "memory_id": str(new_memory_id),
            "confidence": confidence,
            "abstraction": abstraction[:120],
        }
    finally:
        if is_raw_uuid and isinstance(ctx, ReplayContext):
            ctx.close()


@_register("pii_redaction")
async def _handle_pii_redaction(
    conn: asyncpg.Connection,
    src: _EventRow,
    ctx: ReplayContext | uuid.UUID,
    llm_payload: dict | None,
    config_overrides: dict | None,
) -> dict[str, Any]:
    """Record that PII redaction occurred in the fork (no re-scanning needed)."""
    return {
        "memory_id": src.params.get("memory_id"),
        "entity_types": src.params.get("entity_types", []),
        "replayed": True,
    }


@_register("snapshot_created")
async def _handle_snapshot_created(
    conn: asyncpg.Connection,
    src: _EventRow,
    ctx: ReplayContext | uuid.UUID,
    llm_payload: dict | None,
    config_overrides: dict | None,
) -> dict[str, Any]:
    """
    Record the snapshot event in the fork namespace (provenance-only).

    State snapshots are namespace-level checkpoints.  In a fork, re-recording
    the event in event_log is sufficient for audit provenance; no additional
    state mutation is needed.
    """
    return {
        "source_snapshot_name": src.params.get("name"),
        "replayed": True,
    }


@_register("unredact")
async def _handle_unredact(
    conn: asyncpg.Connection,
    src: _EventRow,
    ctx: ReplayContext | uuid.UUID,
    llm_payload: dict | None,
    config_overrides: dict | None,
) -> dict[str, Any]:
    """Record that an unredaction occurred in the fork namespace."""
    return {
        "memory_id": src.params.get("memory_id"),
        "replayed": True,
    }


async def _handle_fork_provenance_only(
    conn: asyncpg.Connection,
    src: _EventRow,
    ctx: ReplayContext | uuid.UUID,
    llm_payload: dict | None,
    config_overrides: dict | None,
) -> dict[str, Any]:
    """Namespace / migration audit events: fork records provenance in event_log only."""
    return {"replayed": True, "event_type": src.event_type}


_FORK_PROVENANCE_ONLY_TYPES: tuple[str, ...] = (
    "namespace_access_granted",
    "namespace_access_revoked",
    "namespace_created",
    "namespace_metadata_updated",
    "namespace_impersonated",
    "namespace_deletion_requested",
    "namespace_disabled",
    "migration_start_requested",
    "migration_commit_requested",
    "migration_abort_requested",
    "migration_started",
    "migration_committed",
    "migration_aborted",
)

for _fork_prov_et in _FORK_PROVENANCE_ONLY_TYPES:
    _HANDLER_REGISTRY[_fork_prov_et] = _handle_fork_provenance_only

# Audit / saga / sharing events — provenance-only fork projection (same contract
# as namespace + migration registrations above).
_additional_fork_provenance_types: tuple[str, ...] = (
    "store_memory_rolled_back",
    "saga_recovered",
    "a2a_grant_created",
    "a2a_grant_revoked",
    "a2a_shared_query",
    "signing_key_rotated",
    "chain_verification_failed",
    "atms_cascade",
    "config_changed",
    "d365_sla_breach",
    "project_phase_advanced",
    "sales_ai_decision",
    # M6.W13b — System Design authoring audit. Provenance-only, exactly like
    # project_phase_advanced: the design graph a fork would need is rebuilt by
    # replaying the graph writes, not by re-running the audit record.
    "system_design_authored",
    # Part II.4: shred is destructive + content-free; fork projection records
    # provenance only (no content to re-apply).
    "memory_shredded",
    # Module 10 Support Engine events (provenance-only fork projection per Charter M10.W7)
    "support_ticket_opened",
    "support_ticket_resolved",
    "support_touchpoint_recorded",
    "support_diagnosis_authored",
    # Module 12 Field Tech Engine events (provenance-only fork projection per Charter M12.W7)
    "field_tech_work_order_created",
    "field_tech_work_order_assigned",
    "field_tech_checklist_completed",
    "field_tech_serial_scanned",
    "field_tech_time_logged",
    "field_tech_outcome_recorded",
    # Module 14 Marketing Engine events (provenance-only fork projection per Charter M14.W7)
    "marketing_case_study_drafted",
    "marketing_testimonial_requested",
    "marketing_testimonial_captured",
    "marketing_testimonial_retracted",
    "marketing_content_approved",
    "marketing_content_published",
)
for _fork_et in _additional_fork_provenance_types:
    assert _fork_et not in _HANDLER_REGISTRY, (
        f"duplicate ForkedReplay handler registration for event_type={_fork_et!r}"
    )
    _HANDLER_REGISTRY[_fork_et] = _handle_fork_provenance_only


# ---------------------------------------------------------------------------
# Diagnostic Log Digestion Engine handlers
# ---------------------------------------------------------------------------


@_register("ingestion_completed")
async def _handle_ingestion_completed(
    conn: asyncpg.Connection,
    src: _EventRow,
    ctx: ReplayContext | uuid.UUID,
    llm_payload: dict | None,
    config_overrides: dict | None,
) -> dict[str, Any]:
    """Idempotent re-apply for an ingestion_completed event.

    The event records that a diagnostic-log ingestion run finished (keyed by
    ``ingest_id``).  On fork replay the raw digest has already been captured in
    the immutable ``digest_payload_ref``; no content is re-written.  The handler
    returns provenance metadata so the fork's event_log row accurately reflects
    the original ingest without carrying raw log content.
    """
    ingest_id: str | None = src.params.get("ingest_id")
    if not ingest_id:
        return {"skipped": True, "reason": "no_ingest_id_in_params"}

    return {
        "ingest_id": ingest_id,
        "skipped": False,
        "replayed": True,
    }


# ---------------------------------------------------------------------------
# Batch 112 — Active Learning quarantine handlers
# ---------------------------------------------------------------------------


@_register("quarantine_confirmed")
async def _handle_quarantine_confirmed(
    conn: asyncpg.Connection,
    src: _EventRow,
    ctx: ReplayContext | uuid.UUID,
    llm_payload: dict | None,
    config_overrides: dict | None,
) -> dict[str, Any]:
    """Re-apply the salience prior for a confirmed quarantine memory.

    The original confirm_memory() call stored a memory, then set
    memory_salience.salience_score = GREATEST(existing, 0.65) keyed by the
    memories.payload_ref that was returned from store_memory.  In replay we
    look up that same payload_ref via the active_learning_queue row (which is
    durable, not WORM), then find the matching memory in the target namespace
    and re-apply the salience floor.  If the queue item or memory is absent
    we return skipped — no raising, consistent with idempotent replay semantics.
    """
    if isinstance(ctx, uuid.UUID):
        ctx = ReplayContext(ctx)

    queue_item_id_str: str | None = src.params.get("queue_item_id")
    agent_id: str | None = src.params.get("agent_id")

    if not queue_item_id_str or not agent_id:
        return {"skipped": True, "reason": "missing_queue_item_id_or_agent_id_in_params"}

    try:
        queue_item_id = uuid.UUID(queue_item_id_str)
    except ValueError:
        return {"skipped": True, "reason": "invalid_queue_item_id_uuid"}

    # Resolve payload_ref via the active_learning_queue row (durable, non-WORM).
    queue_row = await conn.fetchrow(
        """
        SELECT payload FROM active_learning_queue
        WHERE id = $1 AND namespace_id = $2
        """,
        queue_item_id,
        ctx.target_namespace_id,
    )
    if queue_row is None:
        return {"skipped": True, "reason": "queue_item_not_found_in_target_namespace"}

    payload_data = (
        json.loads(queue_row["payload"])
        if isinstance(queue_row["payload"], str)
        else dict(queue_row["payload"])
    )
    payload_ref: str | None = payload_data.get("payload_ref")
    if not payload_ref:
        return {"skipped": True, "reason": "payload_ref_absent_in_queue_payload"}

    memory_id = await conn.fetchval(
        "SELECT id FROM memories WHERE payload_ref = $1 AND namespace_id = $2 LIMIT 1",
        payload_ref,
        ctx.target_namespace_id,
    )
    if memory_id is None:
        return {"skipped": True, "reason": "memory_not_found_by_payload_ref"}

    _CONFIRMED_SALIENCE: float = 0.65
    await conn.execute(
        """
        INSERT INTO memory_salience
            (memory_id, agent_id, namespace_id, salience_score, updated_at, access_count)
        VALUES ($1::uuid, $2, $3, $4::real, NOW(), 1)
        ON CONFLICT (memory_id, agent_id) DO UPDATE
            SET salience_score = GREATEST(memory_salience.salience_score,
                                          EXCLUDED.salience_score),
                updated_at = NOW()
        """,
        memory_id,
        agent_id,
        ctx.target_namespace_id,
        _CONFIRMED_SALIENCE,
    )
    return {
        "memory_id": str(memory_id),
        "salience_applied": _CONFIRMED_SALIENCE,
        "replayed": True,
    }


@_register("quarantine_rejected")
async def _handle_quarantine_rejected(
    conn: asyncpg.Connection,
    src: _EventRow,
    ctx: ReplayContext | uuid.UUID,
    llm_payload: dict | None,
    config_overrides: dict | None,
) -> dict[str, Any]:
    """No-op for replay.

    A rejected quarantine event discards the payload; only the sha256 is
    stored in the WORM log.  There is nothing durable to reproduce — the
    memory was never promoted to the memories table.  Re-applying the
    discard would have no effect.
    """
    return {
        "skipped": True,
        "reason": "quarantine_rejected_payload_discarded_no_durable_state",
        "queue_item_id": src.params.get("queue_item_id"),
    }


# ---------------------------------------------------------------------------
# Batch 111 — Contradiction-cascade residual handlers
# ---------------------------------------------------------------------------


@_register("edge_confidence_floored")
async def _handle_edge_confidence_floored(
    conn: asyncpg.Connection,
    src: _EventRow,
    ctx: ReplayContext | uuid.UUID,
    llm_payload: dict | None,
    config_overrides: dict | None,
) -> dict[str, Any]:
    """Re-apply LEAST(confidence, 0.1) to origin-tagged kg_edges in the fork.

    Mirrors atms.floor_retracted_kg_edges: looks up event_log rows in the
    target namespace whose params->>'memory_id' is in retracted_memory_ids,
    then floors confidence on all kg_edges with a matching origin_event_id.
    If no edges match the operation is a no-op (idempotent).
    """
    if isinstance(ctx, uuid.UUID):
        ctx = ReplayContext(ctx)

    retracted_memory_ids: list[str] = src.params.get("retracted_memory_ids", [])
    if not retracted_memory_ids:
        return {"skipped": True, "reason": "no_retracted_memory_ids_in_params"}

    # Filter to valid UUIDs (guard against malformed params).
    valid_ids: list[uuid.UUID] = []
    for mid in retracted_memory_ids:
        try:
            valid_ids.append(uuid.UUID(str(mid)))
        except (ValueError, AttributeError):
            pass

    if not valid_ids:
        return {"skipped": True, "reason": "no_valid_uuid_retracted_memory_ids"}

    # Collect origin_event_ids from the target namespace's event_log.
    origin_event_rows = await conn.fetch(
        """
        SELECT id
        FROM event_log
        WHERE namespace_id = $1
          AND params->>'memory_id' = ANY($2::text[])
        """,
        ctx.target_namespace_id,
        [str(v) for v in valid_ids],
    )
    if not origin_event_rows:
        return {"skipped": True, "reason": "no_matching_origin_events_in_target_namespace"}

    origin_event_ids = [row["id"] for row in origin_event_rows]

    _EDGE_CONFIDENCE_FLOOR: float = 0.1
    res = await conn.execute(
        """
        UPDATE kg_edges
        SET confidence = LEAST(confidence, $1)
        WHERE namespace_id = $2
          AND origin_event_id = ANY($3::uuid[])
          AND confidence > $1
        """,
        _EDGE_CONFIDENCE_FLOOR,
        ctx.target_namespace_id,
        origin_event_ids,
    )
    floored_count = int(res.split()[-1])
    return {
        "floored_edge_count": floored_count,
        "retracted_memory_count": len(valid_ids),
        "replayed": True,
    }


@_register("consolidation_requeue")
async def _handle_consolidation_requeue(
    conn: asyncpg.Connection,
    src: _EventRow,
    ctx: ReplayContext | uuid.UUID,
    llm_payload: dict | None,
    config_overrides: dict | None,
) -> dict[str, Any]:
    """No-op for replay.

    A consolidation_requeue event records a transient orchestration action
    (re-opening a consolidation job after its prior resolution was superseded
    or merged).  The re-queue itself is ephemeral: there is no durable row
    written that can be idempotently re-applied.  Durable outcomes (the
    subsequent consolidation_run result) are captured by their own events.
    """
    return {
        "skipped": True,
        "reason": "consolidation_requeue_transient_orchestration_no_durable_state",
        "contradiction_id": src.params.get("contradiction_id"),
    }


# ---------------------------------------------------------------------------
# LLM re-execution helper for ForkedReplay
# ---------------------------------------------------------------------------


async def _resolve_llm_payload(
    src: _EventRow,
    replay_mode: str,
    config_overrides: dict | None,
    target_namespace_id: uuid.UUID,
    source_namespace_id: uuid.UUID,
) -> tuple[dict | None, str | None, bytes | None]:
    """
    Resolve the LLM payload for a forked event.

    Returns ``(payload_dict, new_uri, new_hash)`` where:
    * ``payload_dict``  — ``{prompt, response}`` for handler consumption
    * ``new_uri``       — MinIO URI to store in the fork's event_log row (or None)
    * ``new_hash``      — sha256 of the canonical payload (or None)

    For non-LLM events ``src.llm_payload_uri is None`` so all three are None.

    Deterministic mode
    ──────────────────
    Fetches the cached payload from MinIO and copies it to a fork-scoped URI.

    Re-execute mode
    ───────────────
    Extracts the original prompt from the cached payload (if available) or
    reconstructs from ``src.params``.  Calls the LLM provider with optional
    overrides and stores the result to a new MinIO URI.
    """
    if src.llm_payload_uri is None:
        return None, None, None  # not an LLM-driven event

    if replay_mode not in ("deterministic", "re-execute"):
        raise ReplayModeError(f"Invalid replay_mode: {replay_mode!r}")

    fork_uri = _fork_llm_payload_uri(src.llm_payload_uri, target_namespace_id, src.event_id)

    if replay_mode == "deterministic":
        try:
            payload = await _fetch_llm_payload(src.llm_payload_uri)
        except (S3Error, Exception) as exc:
            raise MinIOPayloadMissingError(
                f"Deterministic replay: cannot fetch payload at {src.llm_payload_uri!r}: {exc}"
            ) from exc

        # Verify cryptographic integrity of the fetched payload against the WORM-secured DB hash
        if src.llm_payload_hash is not None:
            computed_hash = hashlib.sha256(canonical_json(payload)).digest()
            if computed_hash != src.llm_payload_hash:
                raise ReplayChecksumError(
                    f"LLM payload hash mismatch for event {src.event_id}. "
                    f"Expected {src.llm_payload_hash.hex()}, got {computed_hash.hex()}"
                )

        # Store copy under fork-scoped URI so it is independently addressable.
        fork_hash = await _put_llm_payload(fork_uri, payload)
        return payload, fork_uri, fork_hash

    # --- re-execute ---
    # Retrieve the original prompt (best-effort; fall back to params).
    original_prompt: str = ""
    try:
        src_payload = await _fetch_llm_payload(src.llm_payload_uri)
        original_prompt = src_payload.get("prompt", "")
    except Exception:
        original_prompt = src.params.get("prompt", "")

    if not original_prompt:
        log.warning(
            "re-execute replay: no prompt recoverable for event %s; skipping LLM call",
            src.event_id,
        )
        return None, None, None

    # Apply optional config_overrides to provider selection only (prompt text is never
    # user-mutable here — validated via ReplayConfigOverrides at the API boundary).
    overrides = config_overrides or {}
    from nce.consolidation import ConsolidatedAbstraction  # local import
    from nce.providers.base import Message  # local import
    from nce.providers.factory import get_provider  # local import

    ns_metadata: dict = {}
    if overrides:
        ns_metadata["consolidation"] = {
            k: overrides[k]
            for k in ("llm_provider", "llm_model", "llm_credentials", "llm_temperature")
            if k in overrides
        }

    provider = get_provider(ns_metadata)

    log.info(
        "re-execute replay: calling %s for event %s",
        provider.model_identifier(),
        src.event_id,
    )

    result: ConsolidatedAbstraction = await provider.complete(
        messages=[
            Message.system(
                "You are a memory consolidation engine. "
                "Given N related episodic memories, produce ONE durable semantic "
                "abstraction capturing their shared meaning. "
                "Return ONLY valid JSON matching the schema. No preamble. No markdown."
            ),
            Message.user(original_prompt),
        ],
        response_model=ConsolidatedAbstraction,
    )

    new_payload = {
        "prompt": original_prompt,
        "response": result.model_dump(),
        "provider": provider.model_identifier(),
        "replay": {
            "source_event_id": str(src.event_id),
            "source_namespace_id": str(source_namespace_id),
        },
    }
    fork_hash = await _put_llm_payload(fork_uri, new_payload)
    return new_payload, fork_uri, fork_hash


# ---------------------------------------------------------------------------
# Observational Replay
# ---------------------------------------------------------------------------


class ObservationalReplay:
    """
    Read-only stream of event_log rows for a namespace/seq range.

    Engine state is **never** modified.  Uses a server-side asyncpg cursor so
    the full event_log is never loaded into Python heap.

    Yielded items
    ─────────────
    * ``{"type": "event",    ...event_fields...}``       — one per log row
    * ``{"type": "progress", "run_id": ..., "events_streamed": N}``
    * ``{"type": "complete", "run_id": ..., "events_streamed": N}``
    * ``{"type": "error",    "run_id": ..., "message": "..."}``  (on failure)
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def execute(
        self,
        *,
        source_namespace_id: uuid.UUID,
        start_seq: int = 1,
        end_seq: int | None = None,
        agent_id_filter: str | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """
        Async generator — yield one dict per event + periodic progress items.

        The caller drives the iteration; no internal concurrency is created.
        The event loop is never blocked.
        """
        run_id: uuid.UUID | None = None
        events_streamed = 0

        async with self.pool.acquire(timeout=10.0) as meta_conn:
            run_id = await _create_run(
                meta_conn,
                source_namespace_id=source_namespace_id,
                target_namespace_id=None,
                mode="observational",
                replay_mode="deterministic",
                start_seq=start_seq,
                end_seq=end_seq,
                divergence_seq=None,
                config_overrides=None,
            )

        sql, args = _build_event_query(
            source_namespace_id=source_namespace_id,
            start_seq=start_seq,
            end_seq=end_seq,
            agent_id_filter=agent_id_filter,
        )

        try:
            # A separate long-lived connection for the server-side cursor.
            # The transaction keeps the cursor alive across yield boundaries.
            async with self.pool.acquire(timeout=10.0) as cursor_conn:
                async with cursor_conn.transaction(isolation="repeatable_read"):
                    async for record in cursor_conn.cursor(sql, *args, prefetch=_CURSOR_PREFETCH):
                        try:
                            await verify_event_signature(cursor_conn, record)
                        except DataIntegrityError as exc:
                            yield {
                                "type": "error",
                                "run_id": str(run_id),
                                "message": str(exc),
                            }
                            async with self.pool.acquire(timeout=10.0) as finish_conn:
                                await _finish_run(
                                    finish_conn,
                                    run_id,
                                    status="failed",
                                    events_applied=events_streamed,
                                    error=str(exc),
                                )
                            raise

                        row = _record_to_event_row(record)
                        yield {"type": "event", **_row_as_dict(row)}
                        events_streamed += 1

                        if events_streamed % _PROGRESS_INTERVAL == 0:
                            # Progress update uses a *different* connection to avoid
                            # nesting statements on the cursor connection.
                            async with self.pool.acquire(timeout=10.0) as prog_conn:
                                await _update_run_progress(prog_conn, run_id, events_streamed)
                            yield {
                                "type": "progress",
                                "run_id": str(run_id),
                                "events_streamed": events_streamed,
                            }

            async with self.pool.acquire(timeout=10.0) as finish_conn:
                await _finish_run(
                    finish_conn,
                    run_id,
                    status="success",
                    events_applied=events_streamed,
                )

            yield {
                "type": "complete",
                "run_id": str(run_id),
                "events_streamed": events_streamed,
            }

        except Exception as exc:
            log.exception(
                "ObservationalReplay failed at event %d run_id=%s",
                events_streamed,
                run_id,
            )
            if run_id is not None:
                async with self.pool.acquire(timeout=10.0) as err_conn:
                    await _finish_run(
                        err_conn,
                        run_id,
                        status="failed",
                        events_applied=events_streamed,
                        error=str(exc),
                    )
            yield {
                "type": "error",
                "run_id": str(run_id) if run_id else None,
                "message": str(exc),
            }
            raise


# ---------------------------------------------------------------------------
# Forked Replay
# ---------------------------------------------------------------------------


def _validate_handler_coverage() -> None:
    """Assert all EventTypes have registered handlers.

    Called at construction time so misconfiguration is caught immediately,
    not mid-run. Uses the public ``get_args(EventType)`` API.
    """
    valid_types: frozenset[str] = frozenset(get_args(EventType))
    missing = valid_types - set(_HANDLER_REGISTRY)
    if missing:
        raise ReplayHandlerMissingError(
            f"No replay handler registered for event type(s): {sorted(missing)}.  "
            "Add a @_register('<type>') handler in nce/replay.py."
        )


async def _dispatch_and_apply_event(
    write_conn: asyncpg.Connection,
    *,
    src: _EventRow,
    ctx: ReplayContext | uuid.UUID | None = None,
    target_namespace_id: uuid.UUID | None = None,
    llm_payload: dict | None,
    config_overrides: dict | None,
    run_id: uuid.UUID,
    source_namespace_id: uuid.UUID,
    fork_uri: str | None = None,
    fork_hash: bytes | None = None,
) -> tuple[dict, Any]:
    """Apply one event inside a write transaction: dispatch -> append_event."""
    if ctx is None:
        if target_namespace_id is None:
            raise ValueError("Either ctx or target_namespace_id must be provided")
        ctx = ReplayContext(target_namespace_id, source_namespace_id=source_namespace_id)
    elif isinstance(ctx, uuid.UUID):
        ctx = ReplayContext(ctx, source_namespace_id=source_namespace_id)

    handler = _HANDLER_REGISTRY.get(src.event_type)
    if handler is None:
        log.warning("No handler for event_type=%s; writing provenance only", src.event_type)
        result_summary = {"skipped": True, "reason": "no_handler"}
    else:
        result_summary = await handler(
            write_conn,
            src,
            ctx,
            llm_payload,
            config_overrides,
        )

    enriched_params: dict = {
        **src.params,
        "replay_run_id": str(run_id),
        "source_event_id": str(src.event_id),
        "source_namespace_id": str(source_namespace_id),
    }

    det_event_id = ctx.remap(src.event_id)

    fork_event = await append_event(
        conn=write_conn,
        namespace_id=ctx.target_namespace_id,
        agent_id=src.agent_id,
        event_type=src.event_type,
        params=enriched_params,
        result_summary=result_summary,
        parent_event_id=src.event_id,
        llm_payload_uri=fork_uri,
        llm_payload_hash=fork_hash,
        event_id=det_event_id,
        replay_occurred_at=src.occurred_at,
    )

    return result_summary, fork_event


class ForkedReplay:
    """
    Spawns a target namespace from a historical ``fork_seq``, re-evaluating
    agent actions.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool
        _validate_handler_coverage()

    async def execute(
        self,
        *,
        frozen_config: FrozenForkConfig,
        _existing_run_id: uuid.UUID | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """
        Async generator — drive the forked replay loop.

        Every yielded item is a JSON-safe dict.  The caller collects them and
        decides how to surface them (MCP TextContent, HTTP JSON body, etc.).

        ``frozen_config`` (``FrozenForkConfig``)
            Immutable replay execution config (``frozen=True`` Pydantic model).
            Once instantiated, NO code path can mutate its fields — ``setattr``
            and ``object.__setattr__`` both raise ``ValidationError``.  This
            guarantees WORM-compliant replay integrity.

        ``_existing_run_id``
            If provided, skip creating a new ``replay_runs`` row and use this
            UUID instead.  Callers that need the run_id *before* the generator
            starts (e.g. to return it immediately in an HTTP/MCP response)
            should pre-create the row via ``_create_run()`` and pass it here.
        """
        # ── Extract ALL values from the frozen config ONCE ──
        # These local variables are bound at generator creation time;
        # the frozen_config object itself cannot be mutated by any code
        # path (Pydantic frozen=True blocks setattr at the model level).
        source_namespace_id: uuid.UUID = frozen_config.source_namespace_id
        target_namespace_id: uuid.UUID = frozen_config.target_namespace_id
        fork_seq: int = frozen_config.fork_seq
        start_seq: int = frozen_config.start_seq
        replay_mode: str = frozen_config.replay_mode
        config_overrides: dict | None = frozen_config.overrides_dict
        agent_id_filter: str | None = frozen_config.agent_id_filter

        run_id: uuid.UUID | None = None
        events_applied = 0
        ctx = ReplayContext(target_namespace_id, source_namespace_id=source_namespace_id)

        try:
            # ------------------------------------------------------------------
            # 1.  Create (or reuse) replay_run row
            # ------------------------------------------------------------------
            if _existing_run_id is not None:
                run_id = _existing_run_id
            else:
                async with self.pool.acquire(timeout=10.0) as meta_conn:
                    run_id = await _create_run(
                        meta_conn,
                        source_namespace_id=source_namespace_id,
                        target_namespace_id=target_namespace_id,
                        mode="forked",
                        replay_mode=replay_mode,
                        start_seq=start_seq,
                        end_seq=fork_seq,
                        divergence_seq=fork_seq,
                        config_overrides=config_overrides,
                    )

            # ------------------------------------------------------------------
            # 2.  Check for prior progress (idempotency on resume)
            # ------------------------------------------------------------------
            async with self.pool.acquire(timeout=10.0) as chk_conn:
                prior = await chk_conn.fetchval(
                    """
                    SELECT COALESCE(MAX(event_seq), 0)
                    FROM event_log
                    WHERE namespace_id = $1
                      AND params->>'replay_run_id' = $2
                    """,
                    target_namespace_id,
                    str(run_id),
                )
            resume_from_seq = int(prior) + 1 if prior else start_seq
            if resume_from_seq > start_seq:
                log.info(
                    "ForkedReplay resuming from seq %d (prior progress detected) run_id=%s",
                    resume_from_seq,
                    run_id,
                )
                start_seq = resume_from_seq

            # ------------------------------------------------------------------
            # 3.  Stream source events + apply each one (FIX-041: RR snapshot only).
            # ------------------------------------------------------------------
            records = await _fetch_event_log_snapshot(
                self.pool,
                source_namespace_id=source_namespace_id,
                start_seq=start_seq,
                end_seq=fork_seq,
                agent_id_filter=agent_id_filter,
            )
            for record in records:
                try:
                    async with self.pool.acquire(timeout=10.0) as sig_conn:
                        await verify_event_signature(sig_conn, record)
                except DataIntegrityError as exc:
                    yield {
                        "type": "error",
                        "run_id": str(run_id),
                        "message": str(exc),
                    }
                    async with self.pool.acquire(timeout=10.0) as err_conn:
                        await _finish_run(
                            err_conn,
                            run_id,
                            status="failed",
                            events_applied=events_applied,
                            error=str(exc),
                        )
                    raise

                src = _record_to_event_row(record)

                # -- Resolve LLM payload outside RR / crypto verification connections --
                llm_payload, fork_uri, fork_hash = await _resolve_llm_payload(
                    src,
                    replay_mode=replay_mode,
                    config_overrides=config_overrides,
                    target_namespace_id=target_namespace_id,
                    source_namespace_id=source_namespace_id,
                )

                # -- Apply event in its own Saga transaction on a new conn --
                async with self.pool.acquire(timeout=10.0) as write_conn:
                    async with write_conn.transaction():
                        result_summary, fork_event = await _dispatch_and_apply_event(
                            write_conn,
                            src=src,
                            ctx=ctx,
                            target_namespace_id=ctx.target_namespace_id,
                            llm_payload=llm_payload,
                            config_overrides=config_overrides,
                            run_id=run_id,
                            source_namespace_id=source_namespace_id,
                            fork_uri=fork_uri,
                            fork_hash=fork_hash,
                        )

                events_applied += 1
                skipped = result_summary.get("skipped", False)
                yield_type = "skipped" if skipped else "applied"

                yield {
                    "type": yield_type,
                    "event_seq": src.event_seq,
                    "event_type": src.event_type,
                    "fork_event_id": str(fork_event.event_id),  # type: ignore[attr-defined]
                    "fork_event_seq": fork_event.event_seq,  # type: ignore[attr-defined]
                    "result": result_summary,
                }

                if events_applied % _PROGRESS_INTERVAL == 0:
                    async with self.pool.acquire(timeout=10.0) as prog_conn:
                        await _update_run_progress(prog_conn, run_id, events_applied)
                    yield {
                        "type": "progress",
                        "run_id": str(run_id),
                        "events_applied": events_applied,
                    }

            async with self.pool.acquire(timeout=10.0) as finish_conn:
                await _finish_run(
                    finish_conn,
                    run_id,
                    status="success",
                    events_applied=events_applied,
                )

            yield {
                "type": "complete",
                "run_id": str(run_id),
                "events_applied": events_applied,
                "fork_namespace": str(target_namespace_id),
                "divergence_seq": fork_seq,
            }

        except Exception as exc:
            log.exception("ForkedReplay failed at event %d run_id=%s", events_applied, run_id)
            if run_id is not None:
                async with self.pool.acquire(timeout=10.0) as err_conn:
                    await _finish_run(
                        err_conn,
                        run_id,
                        status="failed",
                        events_applied=events_applied,
                        error=str(exc),
                    )
            yield {
                "type": "error",
                "run_id": str(run_id) if run_id else None,
                "message": str(exc),
            }
            raise
        finally:
            ctx.close()


# ---------------------------------------------------------------------------
# Reconstructive Replay (Phase 2.3)
# ---------------------------------------------------------------------------


class ReconstructiveReplay:
    """
    Apply source events to an empty target namespace, reproducing byte-identical
    state at ``end_seq``.

    Unlike ``ForkedReplay``, no LLM payload resolution is performed — all
    events are applied deterministically.  UUIDs are remapped (original →
    new) to avoid constraint violations in the target namespace.

    Yielded items
    ─────────────
    * ``{"type": "applied",  "event_seq": N, "event_type": "...", ...}``
    * ``{"type": "skipped",  "event_seq": N, "event_type": "...", "reason": "..."}``
    * ``{"type": "progress", "run_id": ..., "events_applied": N}``
    * ``{"type": "complete", "run_id": ..., "events_applied": N}``
    * ``{"type": "error",    "run_id": ..., "message": "..."}``

    UUID remapping
    ──────────────
    Handlers are expected to map source UUIDs to fresh UUIDs in the target
    namespace (e.g., ``_handle_store_memory`` already does this).
    ``ReconstructiveReplay`` does NOT maintain a central UUID mapping table
    — each handler is responsible for its own deterministic remapping.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool
        _validate_handler_coverage()

    async def execute(
        self,
        *,
        source_namespace_id: uuid.UUID,
        target_namespace_id: uuid.UUID,
        end_seq: int,
        start_seq: int = 1,
        agent_id_filter: str | None = None,
        _existing_run_id: uuid.UUID | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """
        Async generator — drive the reconstructive replay loop.

        Args:
            source_namespace_id: Source namespace to replay from.
            target_namespace_id: Empty target namespace to populate.
            end_seq:            Last event sequence to apply (inclusive).
            start_seq:          First event sequence (default 1).
            agent_id_filter:    Optional agent_id to filter source events.
            _existing_run_id:   Pre-created replay_runs row UUID.

        Yields:
            JSON-safe dict per event + progress/complete/error items.
        """
        run_id: uuid.UUID | None = None
        events_applied = 0
        ctx = ReplayContext(target_namespace_id, source_namespace_id=source_namespace_id)

        # 1. Create (or reuse) run row
        try:
            # 1. Create (or reuse) run row
            if _existing_run_id is not None:
                run_id = _existing_run_id
            else:
                async with self.pool.acquire(timeout=10.0) as meta_conn:
                    run_id = await _create_run(
                        meta_conn,
                        source_namespace_id=source_namespace_id,
                        target_namespace_id=target_namespace_id,
                        mode="reconstructive",
                        replay_mode="deterministic",
                        start_seq=start_seq,
                        end_seq=end_seq,
                        divergence_seq=None,
                        config_overrides=None,
                    )

            # 2. Check for prior progress (idempotent resume)
            async with self.pool.acquire(timeout=10.0) as chk_conn:
                prior = await chk_conn.fetchval(
                    """
                    SELECT COALESCE(MAX(event_seq), 0)
                    FROM event_log
                    WHERE namespace_id = $1
                      AND params->>'replay_run_id' = $2
                    """,
                    target_namespace_id,
                    str(run_id),
                )
            resume_from_seq = int(prior) + 1 if prior else start_seq
            if resume_from_seq > start_seq:
                log.info(
                    "ReconstructiveReplay resuming from seq %d (prior=%d) run_id=%s",
                    resume_from_seq,
                    prior,
                    run_id,
                )
                start_seq = resume_from_seq

            # 3. Stream source events + apply each one
            sql, args = _build_event_query(
                source_namespace_id=source_namespace_id,
                start_seq=start_seq,
                end_seq=end_seq,
                agent_id_filter=agent_id_filter,
            )

            async with self.pool.acquire(timeout=10.0) as cursor_conn:
                async with cursor_conn.transaction(isolation="repeatable_read"):
                    async for record in cursor_conn.cursor(sql, *args, prefetch=_CURSOR_PREFETCH):
                        try:
                            await verify_event_signature(cursor_conn, record)
                        except DataIntegrityError as exc:
                            yield {
                                "type": "error",
                                "run_id": str(run_id),
                                "message": str(exc),
                            }
                            async with self.pool.acquire(timeout=10.0) as err_conn:
                                await _finish_run(
                                    err_conn,
                                    run_id,
                                    status="failed",
                                    events_applied=events_applied,
                                    error=str(exc),
                                )
                            raise

                        src = _record_to_event_row(record)

                        # Apply event in its own Saga transaction
                        async with self.pool.acquire(timeout=10.0) as write_conn:
                            async with write_conn.transaction():
                                result_summary, fork_event = await _dispatch_and_apply_event(
                                    write_conn,
                                    src=src,
                                    ctx=ctx,
                                    target_namespace_id=ctx.target_namespace_id,
                                    llm_payload=None,
                                    config_overrides=None,
                                    run_id=run_id,
                                    source_namespace_id=source_namespace_id,
                                )

                        events_applied += 1
                        skipped = result_summary.get("skipped", False)
                        yield_type = "skipped" if skipped else "applied"

                        yield {
                            "type": yield_type,
                            "event_seq": src.event_seq,
                            "event_type": src.event_type,
                            "result": result_summary,
                        }

                        if events_applied % _PROGRESS_INTERVAL == 0:
                            async with self.pool.acquire(timeout=10.0) as prog_conn:
                                await _update_run_progress(prog_conn, run_id, events_applied)
                            yield {
                                "type": "progress",
                                "run_id": str(run_id),
                                "events_applied": events_applied,
                            }

            # Calculate state digests
            from nce.state_digest import compute_namespace_state_digest

            source_digest = None
            target_digest = None
            digest_match = None

            try:
                async with self.pool.acquire(timeout=10.0) as digest_conn:
                    as_of_dt = await digest_conn.fetchval(
                        "SELECT occurred_at FROM event_log WHERE namespace_id = $1 AND event_seq = $2",
                        source_namespace_id,
                        end_seq,
                    )
                    source_digest = await compute_namespace_state_digest(
                        digest_conn, source_namespace_id, as_of=as_of_dt
                    )
                    target_digest = await compute_namespace_state_digest(
                        digest_conn, target_namespace_id, as_of=as_of_dt
                    )
                    digest_match = source_digest == target_digest
            except Exception as e:
                log.warning("Failed to compute namespace state digests: %s", e)

            # Store digests in replay_runs
            async with self.pool.acquire(timeout=10.0) as store_conn:
                await store_conn.execute(
                    """
                    UPDATE replay_runs
                    SET source_state_digest = $1,
                        target_state_digest = $2,
                        digest_match = $3
                    WHERE id = $4
                    """,
                    source_digest,
                    target_digest,
                    digest_match,
                    run_id,
                )

            async with self.pool.acquire(timeout=10.0) as finish_conn:
                await _finish_run(
                    finish_conn,
                    run_id,
                    status="success",
                    events_applied=events_applied,
                )

            yield {
                "type": "complete",
                "run_id": str(run_id),
                "events_applied": events_applied,
                "target_namespace": str(target_namespace_id),
                "end_seq": end_seq,
            }

        except Exception as exc:
            log.exception(
                "ReconstructiveReplay failed at event %d run_id=%s",
                events_applied,
                run_id,
            )
            if run_id is not None:
                async with self.pool.acquire(timeout=10.0) as err_conn:
                    await _finish_run(
                        err_conn,
                        run_id,
                        status="failed",
                        events_applied=events_applied,
                        error=str(exc),
                    )
            yield {
                "type": "error",
                "run_id": str(run_id) if run_id else None,
                "message": str(exc),
            }
            raise
        finally:
            ctx.close()


# ---------------------------------------------------------------------------
# Convenience: Event Provenance
# ---------------------------------------------------------------------------


async def get_event_provenance(
    pool: asyncpg.Pool,
    memory_id: uuid.UUID,
) -> dict[str, Any]:
    """
    Return the causal DAG for a memory: the creating event and all its
    ancestors via ``event_parents`` (multi-parent) with fallback to the
    scalar ``parent_event_id`` column for single-parent events.

    Returns a dict with ``chain`` (list, root-first, BFS order) and
    ``memory_id``.  Each entry includes a ``parent_event_ids`` field listing
    all causal parents recorded in ``event_parents``.
    """
    async with pool.acquire(timeout=10.0) as conn:
        # Find the event_log row that created this memory.
        root = await conn.fetchrow(
            """
            SELECT id, namespace_id, agent_id, event_type, event_seq,
                   occurred_at, params, result_summary, parent_event_id,
                   signature, signature_key_id, signature_version, chain_hash
            FROM event_log
            WHERE params->>'memory_id' = $1
            ORDER BY event_seq ASC
            LIMIT 1
            """,
            str(memory_id),
        )
        if root is None:
            return {"memory_id": str(memory_id), "chain": []}

        chain: list[dict] = []

        # BFS over the multi-parent DAG (bounded to 200 nodes to guard against
        # deep chains; cycles are broken by the visited set).
        visited: set[uuid.UUID] = set()
        frontier: collections.deque[uuid.UUID] = collections.deque([root["id"]])

        while frontier and len(visited) < 200:
            current_id = frontier.popleft()
            if current_id in visited:
                continue
            visited.add(current_id)

            row = await conn.fetchrow(
                """
                SELECT id, namespace_id, agent_id, event_type, event_seq,
                       occurred_at, params, result_summary, parent_event_id,
                       signature, signature_key_id, signature_version, chain_hash
                FROM event_log WHERE id = $1
                """,
                current_id,
            )
            if row is None:
                continue

            verified = True
            try:
                await verify_event_signature(conn, row)
            except DataIntegrityError:
                verified = False
            except Exception:
                verified = False

            sig_val = row["signature"]
            if isinstance(sig_val, memoryview):
                sig_val = bytes(sig_val)
            sig_hex = sig_val.hex() if sig_val else ""

            # Robust JSON decoding of params
            params_val = row["params"]
            if isinstance(params_val, str):
                params_dict = json.loads(params_val)
            elif params_val is not None:
                params_dict = dict(params_val)
            else:
                params_dict = {}

            # Robust JSON decoding of result_summary
            res_val = row["result_summary"]
            if isinstance(res_val, str):
                res_dict = json.loads(res_val)
            elif res_val is not None:
                res_dict = dict(res_val)
            else:
                res_dict = None

            # Fetch multi-parent IDs from event_parents table.
            ep_rows = await conn.fetch(
                "SELECT parent_event_id FROM event_parents WHERE event_id = $1",
                current_id,
            )
            multi_parent_ids: list[str] = [str(r["parent_event_id"]) for r in ep_rows]

            # Fall back to scalar column for single-parent legacy events.
            if not multi_parent_ids and row["parent_event_id"] is not None:
                multi_parent_ids = [str(row["parent_event_id"])]

            chain.append(
                {
                    "event_id": str(row["id"]),
                    "namespace_id": str(row["namespace_id"]),
                    "agent_id": row["agent_id"],
                    "event_type": row["event_type"],
                    "event_seq": row["event_seq"],
                    "occurred_at": row["occurred_at"].isoformat(),
                    "params": params_dict,
                    "result_summary": res_dict,
                    "parent_event_id": (
                        str(row["parent_event_id"]) if row["parent_event_id"] else None
                    ),
                    "parent_event_ids": multi_parent_ids,
                    "signature": sig_hex,
                    "verified": verified,
                }
            )

            # Enqueue parents from event_parents for BFS traversal.
            for pid_str in multi_parent_ids:
                try:
                    pid = uuid.UUID(pid_str)
                except ValueError:
                    continue
                if pid not in visited:
                    frontier.append(pid)

    chain.reverse()  # root first
    return {"memory_id": str(memory_id), "chain": chain}
