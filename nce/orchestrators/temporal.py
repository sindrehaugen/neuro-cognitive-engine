"""
TemporalOrchestrator — domain orchestrator for consolidation, snapshots, and state diffing.

Extracted from NCEEngine (Prompt 54, Step 2).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, TypeVar
from uuid import UUID

import asyncpg
from motor.motor_asyncio import AsyncIOMotorClient

from nce.background_task_manager import create_tracked_task
from nce.mongo_bulk import fetch_episode_previews_by_ref, normalize_payload_ref
from nce.orchestrators._base import OrchestratorBase
from nce.orchestrators._utils import (
    _build_lineage_modified,
    _lineage_source_id,
    _metadata_as_dict,
)

log = logging.getLogger("nce-orchestrator.temporal")

MAX_DIFF_ITEMS: int = 1000
_MAX_COMPARE_QUERY_LEN: int = 2048

_TDiffItem = TypeVar("_TDiffItem")


def _validate_compare_window(as_of_a: datetime, as_of_b: datetime) -> None:
    if as_of_a >= as_of_b:
        raise ValueError(
            f"as_of_a must be strictly before as_of_b "
            f"(got as_of_a={as_of_a.isoformat()}, as_of_b={as_of_b.isoformat()})"
        )


def _normalize_compare_query(raw: str | None) -> str | None:
    if raw is None:
        return None
    query = raw.strip()
    if not query:
        raise ValueError("query must not be empty or whitespace-only")
    if len(query) > _MAX_COMPARE_QUERY_LEN:
        raise ValueError(f"query exceeds maximum length of {_MAX_COMPARE_QUERY_LEN} characters")
    return query


def _cap_diff_list(kind: str, items: list[_TDiffItem]) -> list[_TDiffItem]:
    if len(items) <= MAX_DIFF_ITEMS:
        return items
    log.warning(
        "compare_states %s list truncated from %d to %d items",
        kind,
        len(items),
        MAX_DIFF_ITEMS,
    )
    return items[:MAX_DIFF_ITEMS]


class TemporalOrchestrator(OrchestratorBase):
    """Domain orchestrator for consolidation, snapshots, and state diffing."""

    def __init__(
        self,
        pg_pool: asyncpg.Pool,
        mongo_client: AsyncIOMotorClient,
        semantic_search_fn: Callable[..., Awaitable[list[dict[str, Any]]]],
    ):
        super().__init__(pg_pool, mongo_client=mongo_client)
        self._semantic_search_fn = semantic_search_fn

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _fetch_memories_valid_at(
        self,
        conn: asyncpg.Connection,
        namespace_id: UUID,
        memory_ids: list[UUID],
        as_of: datetime,
    ) -> dict[str, Any]:
        if not memory_ids:
            return {}
        rows = await conn.fetch(
            """
            SELECT m.id AS memory_id, m.namespace_id, m.agent_id, m.payload_ref,
                   m.assertion_type, m.memory_type, m.valid_from, m.pii_redacted,
                   m.derived_from, COALESCE(m.metadata, '{}'::jsonb) AS metadata,
                   (SELECT ms.salience_score FROM memory_salience ms
                    WHERE ms.memory_id = m.id AND ms.namespace_id = m.namespace_id
                    ORDER BY ms.updated_at DESC NULLS LAST LIMIT 1) AS salience
            FROM memories m
            WHERE m.namespace_id = $1
              AND m.id = ANY($2::uuid[])
              AND m.valid_from <= $3
              AND (m.valid_to IS NULL OR m.valid_to > $3)
            """,
            namespace_id,
            memory_ids,
            as_of,
        )
        return {str(r["memory_id"]): r for r in rows}

    async def _hydrate_semantic_results(self, outs: list) -> None:
        if not outs:
            return
        refs = [normalize_payload_ref(getattr(res, "payload_ref", None)) for res in outs]
        previews = await fetch_episode_previews_by_ref(self._mongo_db, refs)

        # Part II.4: previews prefer the (plaintext) summary, but fall back to
        # raw_data which may now be DEK-encrypted.  Fetch the wrapped DEK per
        # memory and decrypt those refs so the preview is plaintext; legacy rows
        # (wrapped_dek NULL) are untouched.
        from nce.envelope import maybe_decrypt_raw_data
        from nce.mongo_bulk import fetch_episodes_raw_by_ref

        memory_ids = [getattr(res, "memory_id", None) for res in outs]
        memory_ids = [m for m in memory_ids if m]
        wrapped_by_ref: dict[str, bytes] = {}
        if memory_ids:
            try:
                async with self.pg_pool.acquire(timeout=10.0) as conn:
                    dek_rows = await conn.fetch(
                        "SELECT payload_ref, wrapped_dek FROM memories "
                        "WHERE id = ANY($1::uuid[]) AND wrapped_dek IS NOT NULL",
                        [UUID(str(m)) for m in memory_ids],
                    )
                for dek_row in dek_rows:
                    wrapped_by_ref[str(dek_row["payload_ref"])] = bytes(dek_row["wrapped_dek"])
            except Exception:
                wrapped_by_ref = {}

        decrypted_raw: dict[str, str] = {}
        if wrapped_by_ref:
            raw_by_ref = await fetch_episodes_raw_by_ref(
                self._mongo_db, list(wrapped_by_ref.keys()), decode_bytes=False
            )
            for ref, blob in raw_by_ref.items():
                decrypted_raw[ref] = maybe_decrypt_raw_data(blob, wrapped_by_ref.get(ref))

        for res in outs:
            key = normalize_payload_ref(getattr(res, "payload_ref", None))
            if not key:
                continue
            if key in decrypted_raw:
                res.content_preview = decrypted_raw[key][:200]
            elif key in previews:
                res.content_preview = previews[key]

    # ------------------------------------------------------------------
    # Consolidation
    # ------------------------------------------------------------------

    async def trigger_consolidation(
        self, namespace_id: str, since_timestamp: datetime | None = None
    ):
        """[Phase 1.2] Trigger a sleep-consolidation run."""
        from nce.consolidation import ConsolidationWorker
        from nce.providers import get_provider

        async with self.pg_pool.acquire(timeout=10.0) as conn:
            ns_row = await conn.fetchrow(
                "SELECT metadata FROM namespaces WHERE id = $1", UUID(namespace_id)
            )
            if not ns_row:
                raise ValueError(f"Namespace {namespace_id} not found")

        metadata = json.loads(ns_row["metadata"])
        provider = get_provider(metadata)
        worker = ConsolidationWorker(
            self.pg_pool,
            provider,
            mongo_client=self.mongo_client,
        )

        create_tracked_task(
            worker.run_consolidation(UUID(namespace_id), since_timestamp=since_timestamp),
            name=f"consolidation-{namespace_id}",
        )
        return {
            "status": "triggered",
            "namespace_id": namespace_id,
            "since": since_timestamp.isoformat() if since_timestamp else "all",
        }

    async def consolidation_status(self, run_id: str) -> dict:
        """[Phase 1.2] Check status of a consolidation run. Admin bypass by design."""
        async with self.pg_pool.acquire(timeout=10.0) as conn:
            row = await conn.fetchrow(
                "SELECT * FROM consolidation_runs WHERE id = $1", UUID(run_id)
            )
            if not row:
                return {"error": "run_not_found"}

            res = dict(row)
            for k, v in res.items():
                if isinstance(v, datetime):
                    res[k] = v.isoformat()
                elif isinstance(v, UUID):
                    res[k] = str(v)
            return res

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    async def create_snapshot(self, payload) -> Any:
        """[Phase 2.2] Create a named PIT reference."""
        from nce.event_log import append_event

        snapshot_at = payload.snapshot_at or datetime.now(timezone.utc)

        async with self.scoped_session(payload.namespace_id) as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO snapshots (namespace_id, agent_id, name, snapshot_at, metadata)
                    VALUES ($1, $2, $3, $4, $5)
                    RETURNING id, namespace_id, agent_id, name, snapshot_at, created_at, metadata
                    """,
                    self._ensure_uuid(payload.namespace_id),
                    payload.agent_id,
                    payload.name,
                    snapshot_at,
                    json.dumps(payload.metadata),
                )

                await append_event(
                    conn=conn,
                    namespace_id=self._ensure_uuid(payload.namespace_id),  # type: ignore[arg-type]
                    agent_id=payload.agent_id,
                    event_type="snapshot_created",
                    params={
                        "snapshot_id": str(row["id"]),
                        "name": payload.name,
                        "snapshot_at": snapshot_at.isoformat(),
                    },
                )

        from nce.models import SnapshotRecord

        return SnapshotRecord(**row)

    async def list_snapshots(self, namespace_id: str) -> list:
        """[Phase 2.2] List snapshots for a namespace."""
        from nce.models import SnapshotRecord

        async with self.scoped_session(namespace_id) as conn:
            rows = await conn.fetch(
                "SELECT * FROM snapshots WHERE namespace_id = $1 ORDER BY created_at DESC",
                UUID(namespace_id),
            )
            return [SnapshotRecord(**r) for r in rows]

    async def delete_snapshot(self, snapshot_id: str, namespace_id: str) -> DeleteSnapshotResult:  # type: ignore[name-defined]  # noqa: F821
        """[Phase 2.2] Delete a point-in-time reference."""
        from nce.models import DeleteSnapshotResult

        async with self.scoped_session(namespace_id) as conn:
            res = await conn.execute(
                "DELETE FROM snapshots WHERE id = $1 AND namespace_id = $2",
                UUID(snapshot_id),
                UUID(namespace_id),
            )
            if res == "DELETE 0":
                raise ValueError(f"Snapshot {snapshot_id} not found in namespace {namespace_id}")

        return DeleteSnapshotResult(status="ok", message=f"Snapshot {snapshot_id} deleted")

    # ------------------------------------------------------------------
    # State diffing (compare_states)
    # ------------------------------------------------------------------

    async def compare_states(self, payload) -> Any:
        """[Phase 2.2] Diff two temporal views."""
        ns_uuid = self._ensure_uuid(payload.namespace_id)
        if ns_uuid is None:
            raise ValueError("namespace_id is required")

        _validate_compare_window(payload.as_of_a, payload.as_of_b)
        query = _normalize_compare_query(payload.query)

        if query is not None:
            return await self._semantic_compare(payload, ns_uuid, query)
        else:
            return await self._full_namespace_compare(payload, ns_uuid)

    async def _semantic_compare(self, payload, ns_uuid: UUID, query: str) -> Any:
        from nce.models import SemanticSearchResult, StateDiffResult

        agent_id = "default"

        def _preview_from_hit(hit: dict[str, Any]) -> str | None:
            rd = hit.get("raw_data")
            if isinstance(rd, str):
                return rd[:200]
            if rd is not None:
                return str(rd)[:200]
            return None

        def _row_to_sem(row: Any, *, score: float, hit: dict[str, Any] | None) -> Any:
            meta = _metadata_as_dict(row.get("metadata"))
            pv = _preview_from_hit(hit) if hit else None
            return SemanticSearchResult(
                memory_id=row["memory_id"],
                namespace_id=row["namespace_id"],
                agent_id=row["agent_id"],
                score=score,
                payload_ref=row["payload_ref"],
                assertion_type=row["assertion_type"],
                memory_type=row["memory_type"],
                valid_from=row["valid_from"],
                pii_redacted=row["pii_redacted"],
                content_preview=pv,
                metadata=meta if meta else None,
            )

        res_a = await self._semantic_search_fn(
            query,
            str(payload.namespace_id),
            agent_id,
            limit=payload.top_k,
            offset=0,
            as_of=payload.as_of_a,
        )
        res_b = await self._semantic_search_fn(
            query,
            str(payload.namespace_id),
            agent_id,
            limit=payload.top_k,
            offset=0,
            as_of=payload.as_of_b,
        )

        def _mid(r: dict[str, Any]) -> str:
            return str(r["memory_id"])

        ids_a = {_mid(r) for r in res_a}
        ids_b = {_mid(r) for r in res_b}
        all_uuid = [UUID(x) for x in sorted(ids_a | ids_b)]

        score_a = {_mid(r): float(r.get("score", 1.0)) for r in res_a}
        score_b = {_mid(r): float(r.get("score", 1.0)) for r in res_b}
        hit_a = {_mid(r): r for r in res_a}
        hit_b = {_mid(r): r for r in res_b}

        async with self.scoped_session(payload.namespace_id) as conn:
            map_a = await self._fetch_memories_valid_at(conn, ns_uuid, all_uuid, payload.as_of_a)
            map_b = await self._fetch_memories_valid_at(conn, ns_uuid, all_uuid, payload.as_of_b)

        added_ids = ids_b - ids_a
        removed_ids = ids_a - ids_b

        modified: list[dict[str, Any]] = []
        consumed_a: set[str] = set()
        consumed_b: set[str] = set()

        for yid in sorted(added_ids):
            row_b = map_b.get(yid)
            if row_b is None:
                continue
            src = _lineage_source_id(row_b)
            if src and src in removed_ids and src in map_a:
                row_a = map_a[src]
                modified.append(_build_lineage_modified(row_a, row_b))
                consumed_b.add(yid)
                consumed_a.add(src)

        added = sorted(
            [
                _row_to_sem(map_b[i], score=score_b.get(i, 1.0), hit=hit_b.get(i))
                for i in sorted(added_ids)
                if i not in consumed_b and i in map_b
            ],
            key=lambda r: str(r.memory_id),
        )
        removed = sorted(
            [
                _row_to_sem(map_a[i], score=score_a.get(i, 1.0), hit=hit_a.get(i))
                for i in sorted(removed_ids)
                if i not in consumed_a and i in map_a
            ],
            key=lambda r: str(r.memory_id),
        )

        added = _cap_diff_list("added", added)
        removed = _cap_diff_list("removed", removed)

        await self._hydrate_semantic_results(added + removed)

        return StateDiffResult(
            as_of_a=payload.as_of_a,
            as_of_b=payload.as_of_b,
            added=added,
            removed=removed,
            modified=modified,
        )

    async def _full_namespace_compare(self, payload, ns_uuid: UUID) -> Any:
        from nce.models import SemanticSearchResult, StateDiffResult

        def _preview_from_hit(hit: dict[str, Any] | None) -> str | None:
            if not hit:
                return None
            rd = hit.get("raw_data")
            if isinstance(rd, str):
                return rd[:200]
            if rd is not None:
                return str(rd)[:200]
            return None

        def _row_to_sem(row: Any, *, score: float, hit: dict[str, Any] | None) -> Any:
            meta = _metadata_as_dict(row.get("metadata"))
            pv = _preview_from_hit(hit) if hit else None
            return SemanticSearchResult(
                memory_id=row["memory_id"],
                namespace_id=row["namespace_id"],
                agent_id=row["agent_id"],
                score=score,
                payload_ref=row["payload_ref"],
                assertion_type=row["assertion_type"],
                memory_type=row["memory_type"],
                valid_from=row["valid_from"],
                pii_redacted=row["pii_redacted"],
                content_preview=pv,
                metadata=meta if meta else None,
            )

        # Full namespace diff: UNION ALL query
        async with self.scoped_session(payload.namespace_id) as conn:
            all_rows = await conn.fetch(
                """
                SELECT * FROM (
                    SELECT m.id AS memory_id, m.namespace_id, m.agent_id, m.payload_ref,
                           m.assertion_type, m.memory_type, m.valid_from, m.pii_redacted,
                           m.derived_from, COALESCE(m.metadata, '{}'::jsonb) AS metadata,
                           (SELECT ms.salience_score FROM memory_salience ms
                            WHERE ms.memory_id = m.id AND ms.namespace_id = m.namespace_id
                            ORDER BY ms.updated_at DESC NULLS LAST LIMIT 1) AS salience,
                           'added' AS change_type
                    FROM memories m
                    WHERE m.namespace_id = $1
                      AND m.valid_from > $2 AND m.valid_from <= $3
                      AND (m.valid_to IS NULL OR m.valid_to > $3)
                    UNION ALL
                    SELECT m.id AS memory_id, m.namespace_id, m.agent_id, m.payload_ref,
                           m.assertion_type, m.memory_type, m.valid_from, m.pii_redacted,
                           m.derived_from, COALESCE(m.metadata, '{}'::jsonb) AS metadata,
                           (SELECT ms.salience_score FROM memory_salience ms
                            WHERE ms.memory_id = m.id AND ms.namespace_id = m.namespace_id
                            ORDER BY ms.updated_at DESC NULLS LAST LIMIT 1) AS salience,
                           'removed' AS change_type
                    FROM memories m
                    WHERE m.namespace_id = $1
                      AND m.valid_to > $2 AND m.valid_to <= $3
                ) diff_rows
                ORDER BY valid_from, memory_id, change_type
                """,
                ns_uuid,
                payload.as_of_a,
                payload.as_of_b,
            )

        added_rows = [r for r in all_rows if r["change_type"] == "added"]
        removed_rows = [r for r in all_rows if r["change_type"] == "removed"]

        added_by_id = {str(r["memory_id"]): r for r in added_rows}
        removed_by_id = {str(r["memory_id"]): r for r in removed_rows}

        modified_ns: list[dict[str, Any]] = []
        consumed_added: set[str] = set()
        consumed_removed: set[str] = set()

        for yid in sorted(added_by_id.keys()):
            row_b = added_by_id[yid]
            src = _lineage_source_id(row_b)
            if src and src in removed_by_id:
                row_a = removed_by_id[src]
                modified_ns.append(_build_lineage_modified(row_a, row_b))
                consumed_added.add(yid)
                consumed_removed.add(src)

        added = sorted(
            [
                _row_to_sem(added_by_id[mid], score=1.0, hit=None)
                for mid in sorted(added_by_id.keys())
                if mid not in consumed_added
            ],
            key=lambda r: str(r.memory_id),
        )
        removed = sorted(
            [
                _row_to_sem(removed_by_id[mid], score=1.0, hit=None)
                for mid in sorted(removed_by_id.keys())
                if mid not in consumed_removed
            ],
            key=lambda r: str(r.memory_id),
        )

        added = _cap_diff_list("added", added)
        removed = _cap_diff_list("removed", removed)

        await self._hydrate_semantic_results(added + removed)

        return StateDiffResult(
            as_of_a=payload.as_of_a,
            as_of_b=payload.as_of_b,
            added=added,
            removed=removed,
            modified=modified_ns,
        )
