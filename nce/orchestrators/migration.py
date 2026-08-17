"""
MigrationOrchestrator — domain orchestrator for embedding migration lifecycle and code indexing.

Extracted from NCEEngine (Prompt 54, Step 5).
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

import asyncpg

from nce.cache_keys import get_code_index_cache_key
from nce.observability import enqueue_traced
from nce.orchestrators._base import OrchestratorBase
from nce.orchestrators._utils import _validate_path

log = logging.getLogger("nce-orchestrator.migration")

VALID_TRANSITIONS: dict[str, list[str]] = {
    "running": ["validating", "aborted"],
    "validating": ["committed", "aborted"],
}


class MigrationOrchestrator(OrchestratorBase):
    """Domain orchestrator for embedding migrations and code indexing."""

    def __init__(
        self,
        pg_pool: asyncpg.Pool,
        redis_client,
        redis_sync_client,
    ):
        super().__init__(pg_pool, redis_client=redis_client)
        self.redis_sync_client = redis_sync_client

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _redis_cache_key(
        self, namespace_id: str | UUID | None, user_id: str | None, filepath: str
    ) -> str:
        """Build a deterministic Redis cache key for code indexing."""
        return get_code_index_cache_key(
            str(namespace_id) if namespace_id else None, user_id, filepath
        )

    # ------------------------------------------------------------------
    # Code indexing & RQ job status
    # ------------------------------------------------------------------

    async def index_code_file(self, payload, *, priority: int = 0) -> dict:
        """[Phase 3.2] Offloads indexing to a background worker via RQ.

        *priority* routes the job to a queue lane (§5.4):
          - ``> 0`` → ``high_priority`` (user-facing API extractions)
          - ``0``  → ``batch_processing`` (bulk / webhook indexing)
        """
        _validate_path(payload.filepath)

        import hashlib

        from nce.config import cfg

        max_size = cfg.NCE_MAX_CODE_INDEX_BYTES

        if len(payload.raw_code) > max_size:
            raise ValueError(
                f"Code payload too large: {len(payload.raw_code)} bytes (max {max_size})"
            )

        file_hash = hashlib.sha256(payload.raw_code.encode()).hexdigest()

        scope_user = payload.user_id if payload.private else None
        cache_key = self._redis_cache_key(payload.namespace_id, scope_user, payload.filepath)

        try:
            cached_hash = await asyncio.wait_for(self.redis_client.get(cache_key), timeout=2.0)
        except asyncio.TimeoutError:
            cached_hash = None
            log.warning("[Code] Redis cache read timed out for key=%s, proceeding", cache_key)

        if cached_hash and cached_hash.decode() == file_hash:
            return {
                "status": "skipped",
                "reason": "unchanged",
                "filepath": payload.filepath,
            }

        import re

        from nce.extractors.dispatch import get_priority_queue
        from nce.tasks import process_code_indexing

        raw_job_id = f"index:{cache_key}"
        job_id = re.sub(r"[^a-zA-Z0-9_-]", "-", raw_job_id)

        q = get_priority_queue(priority, self.redis_sync_client)
        job = enqueue_traced(
            q,
            process_code_indexing,
            args=(
                payload.filepath,
                payload.raw_code,
                payload.language,
                scope_user,
                str(payload.namespace_id) if payload.namespace_id else None,
            ),
            job_timeout="10m",
            job_id=job_id,
        )

        queue_name = q.name
        log.info(
            "[Code] Enqueued indexing job %s for %s (queue=%s)",
            job.id,
            payload.filepath,
            queue_name,
        )
        status = "indexed" if job.is_finished else "enqueued"
        return {"status": status, "job_id": job.id, "filepath": payload.filepath}

    async def get_job_status(self, job_id: str) -> dict:
        """Check the status of an RQ job."""
        from rq.job import Job

        try:
            job = await asyncio.to_thread(Job.fetch, job_id, connection=self.redis_sync_client)
            return {
                "job_id": job_id,
                "status": job.get_status(),
                "result": job.result if job.is_finished else None,
                "error": "job failed" if job.is_failed else None,
            }
        except Exception as e:
            log.warning("[Job] Status fetch failed for job_id=%s: %s", job_id, e)
            return {"job_id": job_id, "status": "not_found", "error": "job not found"}

    # ------------------------------------------------------------------
    # Embedding migration lifecycle
    # ------------------------------------------------------------------

    async def start_migration(self, target_model_id: str) -> dict:
        """Atomically create a new migration only if none is currently running.

        Returns dict with migration_id, or a conflict dict if a migration is already active.
        This is a single SQL statement — no TOCTOU race.
        """
        target_model_id = str(self._ensure_uuid(target_model_id))

        async with self.pg_pool.acquire(timeout=10.0) as conn:
            async with conn.transaction():
                model = await conn.fetchrow(
                    "SELECT id FROM embedding_models WHERE id = $1::uuid", target_model_id
                )
                if not model:
                    raise ValueError("Target model not found")

                mig_id = await conn.fetchval(
                    """
                    INSERT INTO embedding_migrations (target_model_id, namespace_id)
                    SELECT
                        $1::uuid,
                        COALESCE(
                            NULLIF(trim(current_setting('nce.namespace_id', true)), '')::uuid,
                            (SELECT id FROM namespaces WHERE slug = '_global_legacy' LIMIT 1)
                        )
                    WHERE NOT EXISTS (
                        SELECT 1 FROM embedding_migrations
                        WHERE status IN ('running', 'validating')
                    )
                    RETURNING id
                    """,
                    target_model_id,
                )

                if not mig_id:
                    return {"status": "conflict", "reason": "already_active"}

                await conn.execute(
                    "UPDATE embedding_models SET status = 'migrating' WHERE id = $1::uuid",
                    target_model_id,
                )

                return {"migration_id": str(mig_id), "status": "running"}

    async def migration_status(self, migration_id: str) -> dict:
        migration_id = str(self._ensure_uuid(migration_id))

        async with self.pg_pool.acquire(timeout=10.0) as conn:
            row = await conn.fetchrow(
                "SELECT id, target_model_id, status, last_memory_id, last_node_id, "
                "started_at, completed_at FROM embedding_migrations WHERE id = $1::uuid",
                migration_id,
            )
            if not row:
                raise ValueError("Migration not found")
            return dict(row)

    async def validate_migration(self, migration_id: str) -> dict:
        """Quality gate: compare embedded-row counts vs target model.

        Returns ``{"status": "success", "message": ...}`` or
        ``{"status": "failed", "reason": ...}`` — API vocabulary only; the
        ``embedding_migrations.status`` column remains the DB state machine.
        """
        migration_id = str(self._ensure_uuid(migration_id))

        async with self.pg_pool.acquire(timeout=10.0) as conn:
            row = await conn.fetchrow(
                "SELECT status, target_model_id FROM embedding_migrations WHERE id = $1::uuid",
                migration_id,
            )
            if not row or row["status"] != "validating":
                raise ValueError("Migration not found or not in validating state")

            target_model_id = row["target_model_id"]

            # P0 fix: scope mem_count to only memories that possess a
            # corresponding embedding for the target model.  The old unscoped
            # count(*) FROM memories included every namespace on the cluster,
            # guaranteeing emb_count < mem_count and deadlocking every
            # multi-tenant migration at the quality gate.
            mem_count = await conn.fetchval(
                """
                SELECT count(*) FROM memories m
                WHERE EXISTS (
                    SELECT 1 FROM memory_embeddings me
                    WHERE me.memory_id = m.id
                      AND me.model_id = $1::uuid
                      AND me.embedding IS NOT NULL
                )
                """,
                target_model_id,
            )
            emb_count = await conn.fetchval(
                "SELECT count(*) FROM memory_embeddings "
                "WHERE model_id = $1::uuid AND embedding IS NOT NULL",
                target_model_id,
            )

            if emb_count < mem_count:
                return {
                    "status": "failed",
                    "reason": f"Missing memory embeddings: {mem_count} memories, {emb_count} embeddings",
                }

            return {
                "status": "success",
                "message": "All memories and nodes have been embedded",
            }

    async def commit_migration(self, migration_id: str) -> dict:
        migration_id = str(self._ensure_uuid(migration_id))

        async with self.pg_pool.acquire(timeout=10.0) as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT status, target_model_id FROM embedding_migrations WHERE id = $1::uuid",
                    migration_id,
                )
                if not row:
                    raise ValueError("Migration not found")

                current_status = row["status"]
                if "committed" not in VALID_TRANSITIONS.get(current_status, []):
                    raise ValueError(f"Migration not ready to commit: state is '{current_status}'")

                target_model_id = row["target_model_id"]

                await conn.execute(
                    "SELECT id FROM embedding_models WHERE status = 'active' FOR UPDATE"
                )
                await conn.execute(
                    "UPDATE embedding_models SET status = 'retired', retired_at = now() "
                    "WHERE status = 'active'"
                )
                await conn.execute(
                    "UPDATE embedding_models SET status = 'active' WHERE id = $1::uuid",
                    target_model_id,
                )
                await conn.execute(
                    "UPDATE embedding_migrations SET status = 'committed', completed_at = now() WHERE id = $1::uuid",
                    migration_id,
                )
                return {"status": "committed", "active_model_id": str(target_model_id)}

    async def abort_migration(self, migration_id: str) -> dict:
        migration_id = str(self._ensure_uuid(migration_id))

        async with self.pg_pool.acquire(timeout=10.0) as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT status, target_model_id FROM embedding_migrations WHERE id = $1::uuid",
                    migration_id,
                )
                if not row:
                    raise ValueError("Migration not found")

                current_status = row["status"]
                if "aborted" not in VALID_TRANSITIONS.get(current_status, []):
                    raise ValueError(f"Cannot abort migration in state '{current_status}'")

                target_model_id = row["target_model_id"]

                await conn.execute(
                    "UPDATE embedding_models SET status = 'active' "
                    "WHERE id = $1::uuid AND status = 'migrating'",
                    target_model_id,
                )
                await conn.execute(
                    "UPDATE embedding_migrations SET status = 'aborted', completed_at = now() "
                    "WHERE id = $1::uuid",
                    migration_id,
                )
                return {"status": "aborted"}
