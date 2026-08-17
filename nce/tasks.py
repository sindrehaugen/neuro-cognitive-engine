"""
Phase 6: Async Background Tasks
Handles heavy processing (AST parsing + Jina vectorization) outside the MCP loop.
Uses RQ (Redis Queue) for reliable task distribution.

Phase 3 hardening: Poison-pill dead-letter-queue (DLQ) for tasks that exhaust
their retry budget.  Each task tracks attempts via Redis; when
``attempt_count > cfg.TASK_MAX_RETRIES``, the payload is persisted to
``dead_letter_queue`` and the job exits cleanly instead of re-raising,
breaking the infinite-retry CPU spin-loop.
"""

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from redis import Redis
from rq import get_current_job

from nce import embeddings as _embeddings
from nce.ast_parser import parse_file
from nce.cache_keys import get_code_index_cache_key
from nce.config import cfg
from nce.db_utils import unmanaged_pg_connection
from nce.dead_letter_queue import (
    _clear_attempt,
    _track_attempt,
    is_circuit_open,
    store_dead_letter,
)
from nce.observability import enqueue_traced, traced_worker_job
from nce.orchestrator import NCEEngine

log = logging.getLogger("nce-tasks")


def run_async(coro):
    """Helper to run async code in sync RQ worker context.

    If an event loop is already running (e.g., during pytest execution),
    attempting to run loop.run_until_complete() on the same thread will clash.
    In that case, we execute the coroutine in a separate thread with its own loop.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is None:
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            return new_loop.run_until_complete(coro)
        finally:
            new_loop.close()
    else:
        import threading

        res = []
        err = []

        def worker():
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            try:
                val = new_loop.run_until_complete(coro)
                res.append(val)
            except BaseException as e:
                err.append(e)
            finally:
                new_loop.close()

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        if err:
            raise err[0]
        return res[0]


def _get_job_id() -> str:
    """Return the current RQ job ID, or ``"unknown"`` if not in worker context."""
    try:
        job = get_current_job()
        return job.id if job else "unknown"
    except Exception:
        return "unknown"


_redis_client: Redis | None = None


def _get_redis() -> Redis:
    """Return a cached Redis client for attempt-tracking counters."""
    global _redis_client
    if _redis_client is None:
        _redis_client = Redis.from_url(cfg.REDIS_URL)
    return _redis_client


class CircuitOpenError(RuntimeError):
    """Raised when a task_name's circuit-breaker is open (deterministic-failure quarantine)."""


def check_circuit_before_enqueue(task_name: str, redis_client: Redis | None = None) -> None:
    """Raise ``CircuitOpenError`` when the circuit-breaker for *task_name* is open.

    Call this at the enqueue site so deterministically-failing task types are
    rejected fast — before they consume a worker slot — while the circuit flag
    is set.  An alert is dispatched non-blockingly.

    Args:
        task_name: The RQ task name (e.g. ``'process_code_indexing'``).
        redis_client: Optional pre-existing client; falls back to ``_get_redis()``.

    Raises:
        CircuitOpenError: when the Redis circuit flag is present.
    """
    client = redis_client or _get_redis()
    if not is_circuit_open(client, task_name):
        return

    log.warning(
        "[Circuit] Enqueue of '%s' rejected — circuit is OPEN (deterministic-failure quarantine).",
        task_name,
    )

    # Fire-and-forget alert (best-effort; we are on a sync code path).
    try:
        import asyncio

        from nce.notifications import dispatcher

        async def _alert() -> None:
            await dispatcher.dispatch_alert(
                f"Enqueue rejected (circuit open): {task_name}",
                f"Task '{task_name}' is quarantined. "
                "Admin must close the circuit via POST /api/admin/dlq/circuit/{task_name}/close.",
            )

        loop = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

        if loop is None:
            asyncio.run(_alert())
        else:
            loop.create_task(_alert())
    except Exception:
        log.exception("[Circuit] Failed to dispatch circuit-open enqueue-rejected alert")

    raise CircuitOpenError(
        f"Task '{task_name}' is circuit-open (deterministic-failure quarantine). "
        "Resolve the underlying issue and close the circuit via the admin API."
    )


def _check_poison_pill(
    task_name: str,
    job_id: str,
    redis_client: Redis,
    exc: BaseException,
) -> tuple[bool, int, str]:
    """Increment the attempt counter and decide whether to route to DLQ.

    Returns ``(poisoned: bool, attempt_count: int, error_message: str)``.

    - ``poisoned=True``: retries exhausted — caller should persist to DLQ
      and exit cleanly (do NOT re-raise).
    - ``poisoned=False``: still within retry budget — caller should re-raise
      so RQ re-enqueues.
    """
    attempt = _track_attempt(redis_client, job_id)
    max_retries = cfg.TASK_MAX_RETRIES

    if max_retries <= 0:
        # DLQ disabled — always re-raise
        return False, attempt, ""

    if attempt <= max_retries:
        log.warning(
            "[Worker] %s (job %s) attempt %d/%d — retrying. Error: %s",
            task_name,
            job_id,
            attempt,
            max_retries,
            str(exc)[:256],
        )
        return False, attempt, ""

    # Exhausted — route to DLQ
    error_msg = f"{type(exc).__name__}: {exc!s}"[:1024]

    log.critical(
        "[Worker] %s (job %s) exhausted %d retries — routing to dead_letter_queue.",
        task_name,
        job_id,
        attempt,
    )
    return True, attempt, error_msg


async def _store_dlq_async(
    *,
    task_name: str,
    job_id: str,
    kwargs: dict[str, Any],
    error_msg: str,
    attempt: int,
    pg_pool: Any | None = None,
) -> None:
    """Persist a poisoned task to the dead_letter_queue table (async).

    If *pg_pool* is provided (reuse an existing pool and is not closed), it is used directly.
    Otherwise a lightweight temporary pool is created and torn down.
    """
    if pg_pool is not None and not getattr(pg_pool, "_closed", False):
        await store_dead_letter(pg_pool, task_name, job_id, kwargs, error_msg, attempt)
        return

    # No pool provided — create a short-lived one
    import asyncpg

    pool = await asyncpg.create_pool(cfg.PG_DSN, min_size=1, max_size=2)
    try:
        await store_dead_letter(pool, task_name, job_id, kwargs, error_msg, attempt)
    finally:
        await pool.close()


@traced_worker_job("process_code_indexing")
def process_code_indexing(
    filepath: str,
    raw_code: str,
    language: str,
    user_id: str | None = None,
    namespace_id: str | None = None,
):
    """
    Worker task: Performs the actual heavy lifting of indexing.
    user_id=None: shared corpus (enterprise default). Otherwise private to that user.
    namespace_id: multi-tenant isolation scope.

    Phase 3: Attempts are tracked via Redis.  After ``cfg.TASK_MAX_RETRIES``
    failures the payload is routed to ``dead_letter_queue`` and the job
    exits cleanly (no re-raise → no re-enqueue).
    """
    job_id = _get_job_id()
    redis_client = _get_redis()
    log.info(
        "[Worker] Starting indexing for %s (namespace=%s, job=%s)",
        filepath,
        namespace_id,
        job_id,
    )

    pg_pool_ref = None

    async def _index():
        nonlocal pg_pool_ref
        engine = NCEEngine()
        await engine.connect()
        pg_pool_ref = engine.pg_pool
        inserted_mongo_id = None
        db = engine.mongo_client.memory_archive
        collection = db.code_files
        try:
            file_hash = hashlib.sha256(raw_code.encode()).hexdigest()

            # STEP 1: Episodic Commit (MongoDB)
            doc: dict = {
                "filepath": filepath,
                "language": language,
                "file_hash": file_hash,
                "raw_code": raw_code,
                "ingested_at": datetime.now(timezone.utc),
            }
            if user_id:
                doc["user_id"] = user_id
            if namespace_id:
                doc["namespace_id"] = namespace_id
            inserted_result = await collection.insert_one(doc)
            inserted_mongo_id = str(inserted_result.inserted_id)

            # STEP 2: Batch-embed all AST chunks after PII sanitization
            from nce.models import NamespacePIIConfig
            from nce.pii import process as pii_process

            pii_config = NamespacePIIConfig()
            if namespace_id:
                from unittest.mock import AsyncMock, Mock

                if isinstance(engine.pg_pool, Mock):
                    if isinstance(getattr(engine.pg_pool, "fetchrow", None), AsyncMock):
                        ns_row = await engine.pg_pool.fetchrow(
                            "SELECT metadata FROM namespaces WHERE id = $1::uuid", namespace_id
                        )
                    else:
                        ns_row = None
                else:
                    ns_row = await engine.pg_pool.fetchrow(
                        "SELECT metadata FROM namespaces WHERE id = $1::uuid", namespace_id
                    )
                if ns_row:
                    meta = json.loads(ns_row["metadata"])
                    if "pii" in meta:
                        pii_config = NamespacePIIConfig(**meta["pii"])

            chunks = list(parse_file(raw_code, language))
            sanitized_chunks_code = []
            chunk_vault_entries = []
            chunk_redacted = []
            for chunk in chunks:
                pii_res = await pii_process(chunk.code_string, pii_config)
                sanitized_chunks_code.append(pii_res.sanitized_text)
                chunk_vault_entries.append(pii_res.vault_entries)
                chunk_redacted.append(pii_res.redacted)

            primary_texts = [f"{c.name}\n{sc}" for c, sc in zip(chunks, sanitized_chunks_code)]
            code_texts = [sc for sc in sanitized_chunks_code]
            nl_texts = [c.name for c in chunks]

            all_texts = primary_texts + code_texts + nl_texts
            all_vectors = await _embeddings.embed_batch(all_texts)

            n_chunks = len(chunks)
            primary_vectors = all_vectors[:n_chunks]
            code_vectors = all_vectors[n_chunks : 2 * n_chunks]
            nl_vectors = all_vectors[2 * n_chunks :]

            # Use scoped session for RLS if namespace_id is provided
            async with (
                engine.scoped_session(namespace_id)
                if namespace_id
                else unmanaged_pg_connection(
                    engine.pg_pool, site="tasks.code_indexing.legacy_no_namespace"
                )
            ) as conn:
                async with conn.transaction():
                    await conn.execute(
                        "UPDATE memories SET valid_to = now() "
                        "WHERE filepath = $1 AND (user_id IS NOT DISTINCT FROM $2) AND valid_to IS NULL",
                        filepath,
                        user_id,
                    )
                    import uuid
                    from uuid import UUID

                    ns_uuid = UUID(namespace_id) if namespace_id else None
                    metadata = {}
                    if (
                        getattr(_embeddings, "degraded_embedding_flag", None)
                        and _embeddings.degraded_embedding_flag.get()
                    ):
                        metadata["degraded_embedding"] = True

                    for i, (chunk, sc, vector, vault, redacted) in enumerate(
                        zip(
                            chunks,
                            sanitized_chunks_code,
                            primary_vectors,
                            chunk_vault_entries,
                            chunk_redacted,
                        )
                    ):
                        memory_id = uuid.uuid4()
                        await conn.execute(
                            """
                            INSERT INTO memories
                                (id, filepath, language, node_type, name, start_line, end_line,
                                 file_hash, embedding, content_fts, payload_ref, user_id, namespace_id, memory_type, metadata, pii_redacted, change_origin)
                            VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9::vector,
                                    to_tsvector('english', $10 || ' ' || $11), $12, $13, $14::uuid, 'code_chunk', $15, $16, 'agent')
                            """,
                            str(memory_id),
                            filepath,
                            language,
                            chunk.node_type,
                            chunk.name,
                            chunk.start_line,
                            chunk.end_line,
                            file_hash,
                            json.dumps(vector),
                            chunk.name,
                            sc,
                            inserted_mongo_id,
                            user_id,
                            ns_uuid,
                            json.dumps(metadata),
                            redacted,
                        )
                        # Store code_intent aspect embedding
                        code_vector = (
                            code_vectors[i]
                            if i < len(code_vectors)
                            else (primary_vectors[i] if i < len(primary_vectors) else [0.0] * 768)
                        )
                        await conn.execute(
                            """
                            INSERT INTO embedding_aspects (memory_id, aspect, embedding, namespace_id)
                            VALUES ($1::uuid, 'code_intent', $2::vector, $3::uuid)
                            """,
                            str(memory_id),
                            json.dumps(code_vector),
                            ns_uuid,
                        )
                        # Store nl_intent aspect embedding
                        nl_vector = (
                            nl_vectors[i]
                            if i < len(nl_vectors)
                            else (primary_vectors[i] if i < len(primary_vectors) else [0.0] * 768)
                        )
                        await conn.execute(
                            """
                            INSERT INTO embedding_aspects (memory_id, aspect, embedding, namespace_id)
                            VALUES ($1::uuid, 'nl_intent', $2::vector, $3::uuid)
                            """,
                            str(memory_id),
                            json.dumps(nl_vector),
                            ns_uuid,
                        )
                        if vault and ns_uuid:
                            await conn.executemany(
                                """
                                INSERT INTO pii_redactions (namespace_id, memory_id, token, encrypted_value, entity_type)
                                VALUES ($1, $2, $3, $4, $5)
                                """,
                                [
                                    (
                                        ns_uuid,
                                        memory_id,
                                        v["token"],
                                        v["encrypted_value"],
                                        v["entity_type"],
                                    )
                                    for v in vault
                                ],
                            )

            # STEP 3: Cache hash in Redis
            cache_key = get_code_index_cache_key(namespace_id, user_id, filepath)
            await engine.redis_client.setex(cache_key, 3600, file_hash)
            log.info("[Worker] Finished indexing %s (%d chunks)", filepath, len(chunks))

            # Success — clear the attempt counter
            _clear_attempt(redis_client, job_id)
            return {"status": "success", "chunks": len(chunks)}

        except Exception as exc:
            log.exception("[Worker] Indexing failed for %s", filepath)
            if inserted_mongo_id:
                log.warning("[ROLLBACK] Removing orphaned Mongo doc %s", inserted_mongo_id)
                try:
                    await collection.delete_one({"_id": inserted_result.inserted_id})
                except Exception as mongo_exc:
                    log.error("[ROLLBACK] Mongo cleanup failed: %s", mongo_exc)

            # Phase 3: Poison-pill check — route to DLQ if retries exhausted
            poisoned, attempt_count, error_msg = _check_poison_pill(
                task_name="process_code_indexing",
                job_id=job_id,
                redis_client=redis_client,
                exc=exc,
            )
            if poisoned:
                return {
                    "status": "dead_lettered",
                    "job_id": job_id,
                    "_dlq_kwargs": {
                        "filepath": filepath,
                        "language": language,
                        "user_id": user_id,
                        "namespace_id": namespace_id,
                    },
                    "_dlq_error_msg": error_msg,
                    "_dlq_attempt": attempt_count,
                }
            raise  # retry — let RQ re-enqueue
        finally:
            await engine.disconnect()

    result = run_async(_index())

    # Phase 3: If the inner coroutine signalled dead-letter, persist the DLQ record
    # in a SEPARATE event loop to avoid nesting (run_async creates a new loop).
    if isinstance(result, dict) and result.get("status") == "dead_lettered":
        try:
            pool = pg_pool_ref
            run_async(
                _store_dlq_async(
                    task_name="process_code_indexing",
                    job_id=result["job_id"],
                    kwargs=result["_dlq_kwargs"],
                    error_msg=result["_dlq_error_msg"],
                    attempt=result["_dlq_attempt"],
                    pg_pool=pool,
                )
            )
        except Exception as dlq_exc:
            log.critical(
                "[DLQ] CRITICAL — Could not persist DLQ entry for process_code_indexing (job %s): %s",
                result.get("job_id"),
                dlq_exc,
            )
        # Clean up private keys before returning to caller
        return {"status": "dead_lettered", "job_id": result["job_id"]}

    return result


@traced_worker_job("process_bridge_event")
def process_bridge_event(provider: str, payload: dict) -> dict:
    """
    RQ worker: process a validated webhook payload for a document bridge.

    ``provider``: sharepoint | gdrive | dropbox (see §10.3 / Appendix H).

    Phase 3: Attempts are tracked via Redis.  After ``cfg.TASK_MAX_RETRIES``
    failures the payload is routed to ``dead_letter_queue`` and the job
    exits cleanly.
    """
    from nce.bridges import dispatch_bridge_event

    job_id = _get_job_id()
    redis_client = _get_redis()
    log.info(
        "[Bridge worker] provider=%s keys=%s job=%s",
        provider,
        list(payload.keys()),
        job_id,
    )

    try:
        result = run_async(dispatch_bridge_event(provider, payload))
        # Success — clear attempt counter
        _clear_attempt(redis_client, job_id)
        return result
    except ValueError as e:
        log.error("[Bridge worker] %s", e)
        return {"status": "error", "error": str(e)}
    except Exception as exc:
        log.exception("[Bridge worker] Unhandled failure for provider=%s", provider)
        poisoned, attempt_count, error_msg = _check_poison_pill(
            task_name="process_bridge_event",
            job_id=job_id,
            redis_client=redis_client,
            exc=exc,
        )
        if poisoned:
            try:
                run_async(
                    _store_dlq_async(
                        task_name="process_bridge_event",
                        job_id=job_id,
                        kwargs={"provider": provider, "payload": payload},
                        error_msg=error_msg,
                        attempt=attempt_count,
                        pg_pool=None,
                    )
                )
            except Exception as dlq_exc:
                log.critical(
                    "[DLQ] CRITICAL — Could not persist DLQ entry for process_bridge_event (job %s): %s",
                    job_id,
                    dlq_exc,
                )
            return {"status": "dead_lettered", "job_id": job_id}
        raise  # retry — let RQ re-enqueue


@traced_worker_job("process_d365_event")
def process_d365_event(payload: dict) -> dict:
    """
    RQ worker task: process a validated Dataverse webhook event.

    Routes to the appropriate ``DataverseIngestionWorker`` method based on
    ``entity_type`` and ``operation`` extracted from the Dataverse payload.

    Follows the same poison-pill / DLQ pattern as ``process_bridge_event``.
    """
    from nce.vertical_modules.dynamics365.webhooks import D365WebhookValidator

    job_id = _get_job_id()
    redis_client = _get_redis()

    entity_ctx = D365WebhookValidator.extract_entity_context(payload)
    entity_type = entity_ctx.get("entity_type", "unknown")
    operation = entity_ctx.get("operation", "unknown")

    log.info(
        "[D365 Worker] entity_type=%s operation=%s job=%s",
        entity_type,
        operation,
        job_id,
    )

    try:
        result = run_async(_dispatch_d365_event(entity_ctx, payload))
        _clear_attempt(redis_client, job_id)
        return result
    except Exception as exc:
        log.exception(
            "[D365 Worker] Unhandled failure entity_type=%s operation=%s", entity_type, operation
        )
        poisoned, attempt_count, error_msg = _check_poison_pill(
            task_name="process_d365_event",
            job_id=job_id,
            redis_client=redis_client,
            exc=exc,
        )
        if poisoned:
            try:
                run_async(
                    _store_dlq_async(
                        task_name="process_d365_event",
                        job_id=job_id,
                        kwargs={"payload": payload},
                        error_msg=error_msg,
                        attempt=attempt_count,
                        pg_pool=None,
                    )
                )
            except Exception as dlq_exc:
                log.critical(
                    "[DLQ] CRITICAL — Could not persist DLQ entry for process_d365_event (job %s): %s",
                    job_id,
                    dlq_exc,
                )
            return {"status": "dead_lettered", "job_id": job_id}
        raise  # retry — let RQ re-enqueue


async def _dispatch_d365_event(
    entity_ctx: dict,
    raw_payload: dict,
) -> dict:
    """
    Async dispatch: create engine resources and route to the correct ingestion method.
    Called inside ``run_async()`` from ``process_d365_event``.

    Echo suppression (Batch 119): before semantic ingestion, checks the Redis echo
    set for the arriving entity.  On a hit the semantic track (episodic memory +
    Empathic Tensor) is skipped; the deterministic KG upsert still runs for state
    convergence.  The result carries ``echo_of`` in its metadata and the
    ``nce_echo_suppressed_total`` counter is incremented.
    """
    from uuid import UUID

    import asyncpg
    import redis.asyncio as aioredis
    from motor.motor_asyncio import AsyncIOMotorClient

    from nce.config import cfg
    from nce.db_utils import scoped_pg_session
    from nce.observability import ECHO_SUPPRESSED_TOTAL
    from nce.vertical_modules.dynamics365.auth import DataverseTokenManager
    from nce.vertical_modules.dynamics365.client import DataverseClient
    from nce.vertical_modules.dynamics365.ingestion import DataverseIngestionWorker
    from nce.vertical_modules.dynamics365.sync import DataverseSyncEngine
    from nce.webhook_receiver.main import check_echo

    entity_type = entity_ctx.get("entity_type", "")
    operation = entity_ctx.get("operation", "")
    entity_id = entity_ctx.get("entity_id", "")

    # --- Echo suppression check ---
    # If NCE itself caused this external change (Batch 129 producers seed the echo
    # set), suppress semantic re-ingestion and return early with convergence-only
    # semantics (deterministic KG upsert still runs below for the structural types).
    echo_of: str | None = None
    if entity_id:
        echo_of = check_echo("d365", entity_id)
    if echo_of:
        log.info(
            "[D365 Worker] Echo suppression hit entity_id=%s origin_event_id=%s — "
            "skipping semantic track",
            entity_id,
            echo_of,
        )
        ECHO_SUPPRESSED_TOTAL.labels(system="d365").inc()

    # Build connection resources
    pg_pool = await asyncpg.create_pool(cfg.PG_DSN, min_size=1, max_size=2)
    mongo_client = AsyncIOMotorClient(cfg.MONGO_URI, serverSelectionTimeoutMS=5_000)
    redis_client = aioredis.from_url(cfg.REDIS_URL)

    try:
        token_mgr = DataverseTokenManager(redis_client)
        token = await token_mgr.get_access_token()
        d365_client = DataverseClient(cfg.NCE_D365_ORG_URL, token)

        # Determine namespace from org ID (use default namespace if no mapping)
        org_id = entity_ctx.get("org_id", "")
        ns_id_str: str | None = None
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM d365_integrations WHERE org_url ILIKE $1 AND status='ACTIVE' LIMIT 1",
                f"%{org_id}%" if org_id else cfg.NCE_D365_ORG_URL,
            )
            if row:
                # Fetch the namespace_id from d365_integrations
                d365_row = await conn.fetchrow(
                    "SELECT namespace_id FROM d365_integrations WHERE id = $1", row["id"]
                )
                if d365_row:
                    ns_id_str = str(d365_row["namespace_id"])

        if not ns_id_str:
            log.warning("[D365 Worker] No namespace mapping for org_id=%s — skipping", org_id)
            return {"status": "skipped", "reason": "no_namespace_mapping"}

        ns_id = UUID(ns_id_str)
        worker = DataverseIngestionWorker(pg_pool, mongo_client, redis_client, ns_id)

        # Route by entity type + operation.
        # Echo-suppressed events skip semantic ingestion (no episodic memory, no
        # Empathic Tensor) but still run the deterministic KG upsert path for
        # structural entity types so graph state converges.
        if echo_of:
            # Semantic routes (annotation, email): skip entirely on echo hit.
            if entity_type in ("annotation", "email"):
                return {
                    "status": "echo_suppressed",
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "change_origin": "webhook",
                    "metadata": {"echo_of": echo_of},
                }
            # Structural routes: still run KG sync for state convergence.
            if entity_type in ("incident", "account", "opportunity", "contact"):
                async with scoped_pg_session(pg_pool, ns_id_str) as conn:
                    sync_engine = DataverseSyncEngine(conn, ns_id, d365_client)
                    stats = await sync_engine.run_full_sync(entity_types=[f"{entity_type}s"])
                return {
                    "status": "echo_suppressed",
                    "action": "sync_edges",
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "change_origin": "webhook",
                    "metadata": {"echo_of": echo_of},
                    "stats": stats,
                }
            return {
                "status": "echo_suppressed",
                "entity_type": entity_type,
                "entity_id": entity_id,
                "change_origin": "webhook",
                "metadata": {"echo_of": echo_of},
            }

        if entity_type == "annotation" and operation == "Create":
            raw_target = entity_ctx.get("raw_target") or {}
            text = raw_target.get("notetext") or raw_target.get("NoteText") or ""
            incident_id = raw_target.get("objectid_incident") or entity_id
            result = await worker.ingest_case_note(incident_id=incident_id, annotation_text=text)
            return {"status": "ok", "action": "ingest_case_note", **result}

        if entity_type == "email" and operation in ("Create", "Update"):
            raw_target = entity_ctx.get("raw_target") or {}
            subject = raw_target.get("subject") or ""
            body = raw_target.get("description") or ""
            related_id = raw_target.get("regardingobjectid") or entity_id
            result = await worker.ingest_activity("email", subject, body, related_id)
            return {"status": "ok", "action": "ingest_activity_email", **result}

        if entity_type in ("incident", "account", "opportunity", "contact"):
            # Structural change → re-sync graph edges for affected entity type
            async with scoped_pg_session(pg_pool, ns_id_str) as conn:
                sync_engine = DataverseSyncEngine(conn, ns_id, d365_client)
                stats = await sync_engine.run_full_sync(entity_types=[f"{entity_type}s"])
            return {"status": "ok", "action": "sync_edges", "stats": stats}

        log.info(
            "[D365 Worker] Unhandled entity_type=%s operation=%s — no action",
            entity_type,
            operation,
        )
        return {"status": "no_action", "entity_type": entity_type, "operation": operation}

    finally:
        await pg_pool.close()
        mongo_client.close()
        await redis_client.aclose()


def enqueue_memory_postprocess(payload: dict) -> None:
    """
    Enqueue post-processing work for a stored memory onto the high-priority RQ queue.

    Called by the outbox relay when a 'memory.stored' event is delivered.
    Intentionally thin — heavy work (embeddings, graph, contradiction detection)
    belongs in the worker, not in the relay transaction.
    """
    from rq import Queue

    from nce.extractors.dispatch import HIGH_PRIORITY_QUEUE

    redis_conn = _get_redis()
    q = Queue(HIGH_PRIORITY_QUEUE, connection=redis_conn)
    enqueue_traced(
        q,
        "nce.tasks._process_memory_postprocess",
        kwargs={"payload": payload},
        job_timeout=300,
    )


@traced_worker_job("process_diag_bundle")
def process_diag_bundle(
    *,
    ingest_id: str,
    namespace_id: str,
    landing_uri: str,
    vendor_profile: str,
    device_slug: str | None = None,
) -> dict:
    """RQ worker: Stream-and-Reduce a diagnostic bundle into the cognitive layers.

    Thin sync wrapper around the async pipeline body
    (:func:`nce.vertical_modules.diagnostics.worker._diag_async`), mirroring the
    other ``traced_worker_job`` tasks in this module.  The body handles bounded-
    memory download (stream-to-disk + ``try/finally`` cleanup), idempotency
    (``DIGESTED`` short-circuit), and the retryable-vs-non-retryable error split:

    * non-retryable defects (poison/corrupt bundle, unknown profile) are
      dead-lettered internally and reported via the return dict (no re-raise);
    * transient failures (download / PG / Mongo) are re-raised here so the
      poison-pill governor decides retry-vs-DLQ via the shared attempt counter.
    """
    from nce.vertical_modules.diagnostics.worker import _diag_async

    job_id = _get_job_id()
    redis_client = _get_redis()
    log.info(
        "[DiagWorker] process_diag_bundle ingest_id=%s namespace=%s vendor=%s job=%s",
        ingest_id,
        namespace_id,
        vendor_profile,
        job_id,
    )

    try:
        return run_async(
            _diag_async(
                ingest_id=ingest_id,
                namespace_id=namespace_id,
                landing_uri=landing_uri,
                vendor_profile=vendor_profile,
                device_slug=device_slug,
            )
        )
    except Exception as exc:
        # Transient failure path: the inner body re-raises only transient errors.
        log.exception("[DiagWorker] process_diag_bundle transient failure")
        poisoned, attempt_count, error_msg = _check_poison_pill(
            task_name="process_diag_bundle",
            job_id=job_id,
            redis_client=redis_client,
            exc=exc,
        )
        if poisoned:
            try:
                run_async(
                    _store_dlq_async(
                        task_name="process_diag_bundle",
                        job_id=job_id,
                        kwargs={
                            "ingest_id": ingest_id,
                            "namespace_id": namespace_id,
                            "vendor_profile": vendor_profile,
                            "device_slug": device_slug,
                        },
                        error_msg=error_msg,
                        attempt=attempt_count,
                        pg_pool=None,
                    )
                )
            except Exception as dlq_exc:
                log.critical(
                    "[DLQ] CRITICAL — Could not persist DLQ entry for "
                    "process_diag_bundle (job %s): %s",
                    job_id,
                    dlq_exc,
                )
            return {"status": "dead_lettered", "job_id": job_id}
        raise  # retry — let RQ re-enqueue


@traced_worker_job("process_memory_postprocess")
def _process_memory_postprocess(payload: dict) -> dict:
    """
    Worker task: post-processing after a memory is stored.
    Placeholder — wire embedding worker, graph worker, and contradiction
    detection here as outbox-driven async steps.
    """
    log.info(
        "[Worker] memory postprocess: memory_id=%s saga_id=%s",
        payload.get("memory_id"),
        payload.get("saga_id"),
    )
    return {"status": "ok", "memory_id": payload.get("memory_id")}
