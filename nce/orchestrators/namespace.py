"""
NamespaceOrchestrator — domain orchestrator for namespace management and quotas.

Extracted from NCEEngine (Prompt 54, Step 3).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.auth import set_namespace_context
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry

log = logging.getLogger("nce-orchestrator.namespace")

_NS_DELETE_CHUNK_SIZE = 1_000

# Tables eligible for id-based chunked delete.
_CHUNKED_DELETE_TABLES: frozenset[str] = frozenset(
    {
        "memories",
        "kg_nodes",
        "kg_edges",
        "pii_redactions",
        "outbox_events",
        "saga_execution_log",
    }
)


async def _delete_namespace_rows_chunked(
    pool: asyncpg.Pool,
    table: str,
    namespace_id: UUID,
) -> None:
    """Delete all rows for namespace_id in 1000-row chunks.

    Each chunk commits its own short transaction so row locks are released
    promptly rather than accumulating across the entire namespace deletion.

    Only tables in _CHUNKED_DELETE_TABLES are accepted to prevent accidental
    misuse with arbitrary table names.
    """
    if table not in _CHUNKED_DELETE_TABLES:
        raise ValueError(f"Table '{table}' is not in the allowed chunked-delete list")
    while True:
        async with pool.acquire(timeout=10.0) as conn:
            async with conn.transaction():
                result = await conn.execute(
                    f"""
                    WITH to_delete AS (
                        SELECT id FROM {table}
                        WHERE namespace_id = $1
                        LIMIT $2
                    )
                    DELETE FROM {table}
                    WHERE id IN (SELECT id FROM to_delete)
                    """,
                    namespace_id,
                    _NS_DELETE_CHUNK_SIZE,
                )
        if result == "DELETE 0":
            break
        await asyncio.sleep(0)  # yield event loop between chunks


async def _delete_memory_salience_chunked(
    pool: asyncpg.Pool,
    namespace_id: UUID,
) -> None:
    """Chunked delete for memory_salience (composite PK: memory_id, agent_id).

    Each chunk commits its own transaction — see :func:`_delete_namespace_rows_chunked`.
    """
    while True:
        async with pool.acquire(timeout=10.0) as conn:
            async with conn.transaction():
                result = await conn.execute(
                    """
                    WITH to_delete AS (
                        SELECT memory_id, agent_id
                        FROM memory_salience
                        WHERE namespace_id = $1
                        LIMIT $2
                    )
                    DELETE FROM memory_salience
                    WHERE (memory_id, agent_id) IN (SELECT memory_id, agent_id FROM to_delete)
                    """,
                    namespace_id,
                    _NS_DELETE_CHUNK_SIZE,
                )
        if result == "DELETE 0":
            break
        await asyncio.sleep(0)


from nce.orchestrators._base import OrchestratorBase  # noqa: E402


class NamespaceOrchestrator(OrchestratorBase):
    """Domain orchestrator for namespace CRUD, grants, and quota management."""

    def __init__(self, pg_pool: asyncpg.Pool, redis_client: Any | None = None):
        super().__init__(pg_pool, redis_client=redis_client)
        self._redis = redis_client

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Namespace management
    # ------------------------------------------------------------------

    async def manage_namespace(
        self,
        payload,
        admin_identity: str | None = None,
    ) -> dict:
        """
        [Phase 0.1] Admin MCP tool for namespace CRUD and grants.

        *admin_identity* is the authenticated principal identifier (e.g. from JWT
        claims or API-key metadata).  Falls back to ``"admin"`` when not provided
        (legacy / single-user mode).

        Admin bypass note: list / create / grant / revoke operate
        cross-namespace by design and intentionally use raw
        ``pg_pool.acquire(timeout=10.0)``.  ``update_metadata`` targets a single
        namespace and uses ``scoped_session()`` for RLS consistency.
        """
        from nce.models import ManageNamespaceCommand

        _agent_id = admin_identity or "admin"

        if payload.command == ManageNamespaceCommand.create:
            return await self._create_namespace(payload, _agent_id)
        if payload.command == ManageNamespaceCommand.list:
            return await self._list_namespaces(payload)
        if payload.command == ManageNamespaceCommand.update_metadata:
            return await self._update_namespace_metadata(payload, _agent_id)
        if payload.command == ManageNamespaceCommand.grant:
            return await self._grant_namespace_access(payload, _agent_id)
        if payload.command == ManageNamespaceCommand.revoke:
            return await self._revoke_namespace_access(payload, _agent_id)
        if payload.command == ManageNamespaceCommand.delete:
            return await self._delete_namespace(payload, _agent_id)

        raise ValueError(f"Unsupported command: {payload.command}")

    async def _create_namespace(self, payload, agent_id: str) -> dict:
        async with self.pg_pool.acquire(timeout=10.0) as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO namespaces (slug, parent_id, metadata)
                    VALUES ($1, $2, $3::jsonb)
                    RETURNING id, slug, parent_id, created_at, metadata
                    """,
                    payload.create.slug,
                    payload.create.parent_id,
                    payload.create.metadata.model_dump_json(),
                )
                await set_namespace_context(conn, row["id"])
                from nce.event_log import append_event

                await append_event(
                    conn=conn,
                    namespace_id=row["id"],
                    agent_id=agent_id,
                    event_type="namespace_created",
                    params={
                        "slug": payload.create.slug,
                        "parent_id": (
                            str(payload.create.parent_id) if payload.create.parent_id else None
                        ),
                    },
                )
                await seed_node_ownership_registry(conn, row["id"])
            return dict(row)

    async def _list_namespaces(self, payload) -> dict:
        # Reserved non-tenant rows (``_system``, seeded by migration 065 to hold
        # global security events) are NOT tenants and must not be handed to a
        # client as if they were. This is the tenant-facing enumeration; other
        # namespace sweeps are unfiltered and are recorded as debt, not fixed
        # here.
        from nce.system_namespace import RESERVED_NON_TENANT_SLUGS

        async with self.pg_pool.acquire(timeout=10.0) as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM   namespaces
                WHERE  slug <> ALL($1::text[])
                ORDER BY created_at DESC
                """,
                sorted(RESERVED_NON_TENANT_SLUGS),
            )
            return {"namespaces": [dict(r) for r in rows]}

    async def _update_namespace_metadata(self, payload, agent_id: str) -> dict:
        async with self.scoped_session(payload.namespace_id) as conn:
            async with conn.transaction():
                old_meta_json = await conn.fetchval(
                    "SELECT metadata FROM namespaces WHERE id = $1",
                    payload.namespace_id,
                )
                if not old_meta_json:
                    raise ValueError(f"Namespace {payload.namespace_id} not found")

                old_meta = json.loads(old_meta_json)
                was_disabled = bool(old_meta.get("disabled", False))
                old_meta.update(payload.metadata_patch.model_dump(exclude_none=True))

                from nce.models import NamespaceMetadata

                validated = NamespaceMetadata(**old_meta)

                await conn.execute(
                    "UPDATE namespaces SET metadata = $1 WHERE id = $2",
                    validated.model_dump_json(),
                    payload.namespace_id,
                )

                from nce.event_log import append_event

                await append_event(
                    conn=conn,
                    namespace_id=payload.namespace_id,
                    agent_id=agent_id,
                    event_type="namespace_metadata_updated",
                    params={
                        "old_metadata": old_meta_json,
                        "new_metadata": validated.model_dump_json(),
                    },
                )
                # Soft-disable audit — pairs with NamespaceMetadata.disabled
                # (physical deletes cannot append here first due to FK + WORM; see ``delete``).
                if validated.disabled and not was_disabled:
                    await append_event(
                        conn=conn,
                        namespace_id=payload.namespace_id,
                        agent_id=agent_id,
                        event_type="namespace_disabled",
                        params={
                            "was_disabled": was_disabled,
                        },
                    )
            return {"status": "ok", "metadata": validated.model_dump()}

    async def _grant_namespace_access(self, payload, agent_id: str) -> dict:
        async with self.pg_pool.acquire(timeout=10.0) as conn:
            async with conn.transaction():
                result = await conn.execute(
                    "UPDATE namespaces SET parent_id = $1 WHERE id = $2",
                    payload.namespace_id,
                    payload.grantee_namespace_id,
                )
                if result == "UPDATE 0":
                    raise ValueError(f"Grantee namespace {payload.grantee_namespace_id} not found")
                await set_namespace_context(conn, payload.grantee_namespace_id)
                from nce.event_log import append_event

                await append_event(
                    conn=conn,
                    namespace_id=payload.grantee_namespace_id,
                    agent_id=agent_id,
                    event_type="namespace_access_granted",
                    params={
                        "granting_namespace_id": str(payload.namespace_id),
                        "grantee_namespace_id": str(payload.grantee_namespace_id),
                    },
                )
            return {
                "status": "ok",
                "message": f"Namespace {payload.grantee_namespace_id} granted access to {payload.namespace_id}",
            }

    async def _revoke_namespace_access(self, payload, agent_id: str) -> dict:
        async with self.pg_pool.acquire(timeout=10.0) as conn:
            async with conn.transaction():
                result = await conn.execute(
                    "UPDATE namespaces SET parent_id = NULL WHERE id = $1 AND parent_id = $2",
                    payload.grantee_namespace_id,
                    payload.namespace_id,
                )
                if result == "UPDATE 0":
                    raise ValueError(
                        f"Namespace {payload.grantee_namespace_id} not found "
                        f"or not granted by {payload.namespace_id}"
                    )
                await set_namespace_context(conn, payload.grantee_namespace_id)
                from nce.event_log import append_event

                await append_event(
                    conn=conn,
                    namespace_id=payload.grantee_namespace_id,
                    agent_id=agent_id,
                    event_type="namespace_access_revoked",
                    params={
                        "revoking_namespace_id": str(payload.namespace_id),
                        "revokee_namespace_id": str(payload.grantee_namespace_id),
                    },
                )
            return {
                "status": "ok",
                "message": f"Access revoked for {payload.grantee_namespace_id}",
            }

    async def _delete_namespace(self, payload, agent_id: str) -> dict:
        if not payload.namespace_id:
            raise ValueError("namespace_id required for delete")

        # FIX-026: event_log is WORM-immutable — use scoped_session so RLS on
        # event_log is satisfied; raw acquire() would see 0 rows under RLS.
        async with self.scoped_session(payload.namespace_id) as evt_chk:
            audit_rows = await evt_chk.fetchval(
                "SELECT COUNT(*)::bigint FROM event_log WHERE namespace_id = $1",
                payload.namespace_id,
            )
        # ``event_log.namespace_id`` is NOT NULL and references ``namespaces`` — we cannot
        # append ``namespace_deletion_requested`` *before* a hard delete without leaving
        # orphan references; WORM semantics reject purging audited rows here. Operators
        # should rely on ``metadata.disabled`` (+ ``namespace_disabled`` audit via
        # ``update_metadata``) unless a separate archival FK story exists.
        if audit_rows and audit_rows > 0:
            msg = (
                f"Cannot delete namespace {payload.namespace_id}: event_log retains "
                f"{audit_rows} immutable audit row(s) (references namespaces via FK)."
            )
            if payload.allow_audit_destruction:
                msg += (
                    " allow_audit_destruction documents operator intent only; "
                    "this server does not purge WORM partitions."
                )
            raise PermissionError(msg)

        # Purge MCP cache entries for this namespace BEFORE deletion.
        if self._redis is not None:
            from nce.mcp_args import purge_namespace_cache

            try:
                await purge_namespace_cache(self._redis, str(payload.namespace_id))
            except Exception as exc:
                log.warning(
                    "Namespace delete: cache purge failed for %s: %s",
                    payload.namespace_id,
                    exc,
                )

        # Phase 1: chunked deletes — each chunk commits its own short
        # transaction so row locks are released after every 1000 rows.
        # An outer transaction here would defeat the entire purpose.
        # NB: omit event_log (WORM + FK); audited tenants are rejected above.
        chunked_tables = [
            "memories",
            "kg_nodes",
            "kg_edges",
            "pii_redactions",
            "outbox_events",
            "saga_execution_log",
        ]
        for table in chunked_tables:
            if table not in _CHUNKED_DELETE_TABLES:
                raise ValueError(f"Table '{table}' is not in the allowed chunked-delete list")
            await _delete_namespace_rows_chunked(self.pg_pool, table, payload.namespace_id)

        # memory_salience has composite PK (memory_id, agent_id).
        await _delete_memory_salience_chunked(self.pg_pool, payload.namespace_id)

        # Phase 2: atomic final teardown — small tables + namespace row in
        # one transaction so the namespace is never left empty-but-present.
        async with self.pg_pool.acquire(timeout=10.0) as conn:
            async with conn.transaction():
                result = await conn.execute(
                    "DELETE FROM namespaces WHERE id = $1",
                    payload.namespace_id,
                )

        if result == "DELETE 0":
            raise ValueError(f"Namespace {payload.namespace_id} not found")

        # Bump global cache generation as a secondary invalidation signal.
        if self._redis is not None:
            from nce.mcp_args import bump_cache_generation

            try:
                await bump_cache_generation(self._redis)
            except Exception as exc:
                log.warning("Namespace delete: global cache bump failed: %s", exc)

        return {
            "status": "ok",
            "message": f"Namespace {payload.namespace_id} deleted",
        }

        raise ValueError(f"Unsupported command: {payload.command}")

    # ------------------------------------------------------------------
    # Quota management
    # ------------------------------------------------------------------

    async def manage_quotas(self, payload) -> dict:
        """
        [Phase 3.2] Admin MCP tool for resource quota management.
        Uses scoped_session so RLS on resource_quotas filters correctly.
        """
        from nce.models import ManageQuotasCommand

        async with self.scoped_session(payload.namespace_id) as conn:
            if payload.command == ManageQuotasCommand.set:
                if payload.agent_id is None:
                    row = await conn.fetchrow(
                        """
                        INSERT INTO resource_quotas (namespace_id, agent_id, resource_type, limit_amount)
                        VALUES ($1, NULL, $2, $3)
                        ON CONFLICT (namespace_id, resource_type) WHERE agent_id IS NULL
                        DO UPDATE SET limit_amount = EXCLUDED.limit_amount, updated_at = now()
                        RETURNING *
                        """,
                        payload.namespace_id,
                        payload.resource_type,
                        payload.limit,
                    )
                else:
                    row = await conn.fetchrow(
                        """
                        INSERT INTO resource_quotas (namespace_id, agent_id, resource_type, limit_amount)
                        VALUES ($1, $2, $3, $4)
                        ON CONFLICT (namespace_id, agent_id, resource_type) WHERE agent_id IS NOT NULL
                        DO UPDATE SET limit_amount = EXCLUDED.limit_amount, updated_at = now()
                        RETURNING *
                        """,
                        payload.namespace_id,
                        payload.agent_id,
                        payload.resource_type,
                        payload.limit,
                    )
                return dict(row)

            if payload.command == ManageQuotasCommand.list:
                rows = await conn.fetch(
                    "SELECT * FROM resource_quotas WHERE namespace_id = $1 ORDER BY created_at DESC",
                    payload.namespace_id,
                )
                return {"quotas": [dict(r) for r in rows]}

            if payload.command == ManageQuotasCommand.delete:
                await conn.execute(
                    """
                    DELETE FROM resource_quotas 
                    WHERE namespace_id = $1 AND resource_type = $2 
                      AND (agent_id IS NOT DISTINCT FROM $3)
                    """,
                    payload.namespace_id,
                    payload.resource_type,
                    payload.agent_id,
                )
                return {
                    "status": "ok",
                    "message": f"Quota for {payload.resource_type} deleted",
                }

            if payload.command == ManageQuotasCommand.reset:
                rows = await conn.fetch(
                    """
                    UPDATE resource_quotas 
                    SET used_amount = 0, updated_at = now()
                    WHERE namespace_id = $1 AND resource_type = $2 
                      AND (agent_id IS NOT DISTINCT FROM $3)
                    RETURNING id, namespace_id
                    """,
                    payload.namespace_id,
                    payload.resource_type,
                    payload.agent_id,
                )
                if self._redis is not None and rows:
                    from nce import quotas as quotas_mod

                    for r in rows:
                        await quotas_mod.delete_quota_redis_counter(
                            self._redis, r["namespace_id"], r["id"]
                        )
                return {
                    "status": "ok",
                    "message": f"Usage reset for {payload.resource_type}",
                }

        raise ValueError(f"Unsupported quota command: {payload.command}")
