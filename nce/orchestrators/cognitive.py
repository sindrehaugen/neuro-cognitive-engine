"""
CognitiveOrchestrator — domain orchestrator for salience boosts, forgetting, and contradictions.

Extracted from NCEEngine (Prompt 54, Step 4).
"""

from __future__ import annotations

import logging
import uuid as _uuid_mod
from uuid import UUID

import asyncpg

from nce.orchestrators._base import OrchestratorBase

log = logging.getLogger("nce-orchestrator.cognitive")


def _is_valid_uuid_str(val: str) -> bool:
    """Return True if *val* is a parseable UUID string."""
    try:
        _uuid_mod.UUID(val)
        return True
    except (ValueError, AttributeError):
        return False


class CognitiveOrchestrator(OrchestratorBase):
    """Domain orchestrator for salience management and contradiction resolution."""

    def __init__(self, pg_pool: asyncpg.Pool):
        super().__init__(pg_pool)

    # ------------------------------------------------------------------
    # Salience — boost_memory
    # ------------------------------------------------------------------

    async def boost_memory(
        self,
        memory_id: str,
        agent_id: str,
        namespace_id: str,
        factor: float = 0.2,
    ) -> dict:
        """[Phase 1.1] Boost the salience of a memory for the calling agent.

        Uses scoped_session to enforce RLS — the caller can only boost
        memories within their own namespace (defense-in-depth on top of
        the namespace_isolation_policy).  Fixes P0 RLS bypass (Item 3,
        Phase 3).
        """
        factor = max(0.0, min(1.0, factor))
        from nce.salience import reinforce

        async with self.scoped_session(namespace_id) as conn:
            async with conn.transaction():
                await reinforce(conn, memory_id, agent_id, namespace_id, delta=factor)

                from nce.event_log import append_event

                await append_event(
                    conn=conn,
                    namespace_id=self._ensure_uuid(namespace_id),
                    agent_id=agent_id,
                    event_type="boost_memory",
                    params={"memory_id": memory_id, "factor": factor},
                    result_summary={"status": "success"},
                )
        return {"status": "success", "boosted_by": factor}

    # ------------------------------------------------------------------
    # Salience — forget_memory
    # ------------------------------------------------------------------

    async def forget_memory(
        self,
        memory_id: str,
        agent_id: str,
        namespace_id: str,
    ) -> dict:
        """[Phase 1.1] Set salience to 0.0 for the calling agent.

        Uses scoped_session to enforce RLS — the caller can only forget
        memories within their own namespace (defense-in-depth on top of
        the namespace_isolation_policy).
        """
        async with self.scoped_session(namespace_id) as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO memory_salience
                        (memory_id, agent_id, namespace_id, salience_score,
                         updated_at, access_count)
                    VALUES ($1::uuid, $2, $3::uuid, 0.0, NOW(), 1)
                    ON CONFLICT (memory_id, agent_id) DO UPDATE
                        SET salience_score = 0.0,
                            updated_at = NOW(),
                            access_count = memory_salience.access_count + 1
                    """,
                    memory_id,
                    agent_id,
                    namespace_id,
                )

                await conn.execute(
                    """
                    UPDATE memories
                    SET valid_to = NOW()
                    WHERE id = $1::uuid
                      AND namespace_id = $2::uuid
                      AND valid_to IS NULL
                    """,
                    memory_id,
                    namespace_id,
                )

                from nce.event_log import append_event

                await append_event(
                    conn=conn,
                    namespace_id=UUID(namespace_id),
                    agent_id=agent_id,
                    event_type="forget_memory",
                    params={"memory_id": memory_id},
                    result_summary={"status": "success"},
                )
        return {"status": "success", "forgotten": True}

    # ------------------------------------------------------------------
    # Contradictions
    # ------------------------------------------------------------------

    async def list_contradictions(
        self,
        namespace_id: str,
        resolution: str | None = None,
        agent_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """[Phase 1.3] List contradictions with pagination.

        Args:
            limit:  Max rows to return (capped at 200).
            offset: Rows to skip for pagination.
        """
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        async with self.scoped_session(namespace_id) as conn:
            query = "SELECT * FROM contradictions WHERE namespace_id = $1"
            params: list = [UUID(namespace_id)]
            idx = 2
            if resolution:
                query += f" AND resolution = ${idx}"
                params.append(resolution)
                idx += 1
            if agent_id:
                query += f" AND agent_id = ${idx}"
                params.append(agent_id)
                idx += 1

            query += f" ORDER BY detected_at DESC LIMIT ${idx} OFFSET ${idx + 1}"
            params.extend([limit, offset])
            rows = await conn.fetch(query, *params)
            return [dict(r) for r in rows]

    async def resolve_contradiction(
        self,
        contradiction_id: str,
        namespace_id: str,
        resolution: str,
        resolved_by: str,
        note: str | None = None,
    ) -> dict:
        """[Phase 1.3] Resolve a contradiction — RLS-enforced via scoped_session.

        Uses a namespace-scoped PG session so the RLS policy on ``contradictions``
        automatically rejects cross-tenant mutations.  The UPDATE includes an
        explicit ``namespace_id = $2::uuid`` filter as defense-in-depth on top
        of RLS.  A caller from namespace A cannot resolve a contradiction in
        namespace B — the UPDATE returns zero rows and ``PermissionError`` is raised.

        The resolution event is cryptographically signed à la WORM contract via
        ``append_event``.
        """
        ns_uuid = UUID(str(namespace_id))

        async with self.scoped_session(ns_uuid) as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    UPDATE contradictions
                    SET resolution = $3, resolved_at = now(), resolved_by = $4,
                        note = COALESCE($5, note)
                    WHERE id = $1
                      AND namespace_id = $2::uuid
                    RETURNING *
                    """,
                    UUID(contradiction_id),
                    ns_uuid,
                    resolution,
                    resolved_by,
                    note,
                )
                if not row:
                    raise PermissionError(
                        f"Contradiction {contradiction_id} not accessible in your namespace"
                    )

                from nce.event_log import append_event

                await append_event(
                    conn=conn,
                    namespace_id=ns_uuid,
                    agent_id=resolved_by,
                    event_type="resolve_contradiction",
                    params={
                        "contradiction_id": contradiction_id,
                        "resolution": resolution,
                        "note": (note or "")[:256],
                    },
                    result_summary={"status": "success"},
                )

                # Nested SAVEPOINT block to prevent ATMS failure from aborting the resolution
                try:
                    async with conn.transaction():
                        loser_id = None
                        if resolution == "accepted_a":
                            loser_id = str(row["memory_b_id"])
                        elif resolution == "accepted_b":
                            loser_id = str(row["memory_a_id"])
                        elif resolution in ("superseded", "rejected", "merged"):
                            # Fetch memories to compare creation times
                            mems = await conn.fetch(
                                """
                                SELECT id, created_at FROM memories
                                WHERE id IN ($1::uuid, $2::uuid)
                                """,
                                row["memory_a_id"],
                                row["memory_b_id"],
                            )
                            if len(mems) == 2:
                                sorted_mems = sorted(mems, key=lambda m: m["created_at"])
                                older_id = str(sorted_mems[0]["id"])
                                newer_id = str(sorted_mems[1]["id"])
                            else:
                                older_id = str(row["memory_a_id"])
                                newer_id = str(row["memory_b_id"])

                            if resolution in ("superseded", "merged"):
                                loser_id = older_id
                            else:
                                loser_id = newer_id

                        if loser_id:
                            from nce.atms import (
                                evaluate_atms_intervention,
                                floor_retracted_kg_edges,
                                persist_atms_invalidation,
                            )

                            cascade_set = {loser_id}

                            # 1. Topology/infrastructure cascade
                            topo_cascade = await evaluate_atms_intervention(conn, ns_uuid, loser_id)
                            cascade_set.update(topo_cascade)

                            # 2. Memory dependents recursively via derived_from
                            max_cascade = 100
                            todo = [loser_id]
                            visited = {loser_id}

                            while todo and len(visited) < max_cascade:
                                current = todo.pop()
                                dep_rows = await conn.fetch(
                                    """
                                    SELECT id FROM memories
                                    WHERE namespace_id = $1::uuid
                                      AND (derived_from @> jsonb_build_array($2::text)
                                           OR derived_from @> jsonb_build_array($2::uuid))
                                      AND valid_to IS NULL
                                    """,
                                    ns_uuid,
                                    current,
                                )
                                for r in dep_rows:
                                    dep_id = str(r["id"])
                                    if dep_id not in visited:
                                        visited.add(dep_id)
                                        todo.append(dep_id)
                                        if len(visited) >= max_cascade:
                                            break

                            cascade_set.update(visited)

                            # 3. Persist soft-deletions
                            await persist_atms_invalidation(conn, ns_uuid, cascade_set)

                            # 4. Log the atms_cascade event
                            await append_event(
                                conn=conn,
                                namespace_id=ns_uuid,
                                agent_id=resolved_by,
                                event_type="atms_cascade",
                                params={
                                    "contradiction_id": contradiction_id,
                                    "invalidated_memory_id": loser_id,
                                    "invalidated_ids": sorted(list(cascade_set)),
                                },
                                result_summary={
                                    "status": "success",
                                    "cascade_count": len(cascade_set),
                                },
                            )

                            # 5. Batch 111 — gap (a): floor kg_edges traced to the
                            #    retracted memories.  Runs inside the same SAVEPOINT
                            #    so a failure here does not abort the resolution row.
                            await floor_retracted_kg_edges(
                                conn,
                                ns_uuid,
                                cascade_set,
                                contradiction_id,
                                resolved_by,
                            )

                            # 6. Batch 111 — gap (b): for superseded/merged resolutions
                            #    delete consolidated memories whose ONLY sources were
                            #    retracted, restore source salience, re-queue via event.
                            if resolution in ("superseded", "merged"):
                                await _reopen_superseded_consolidations(
                                    conn=conn,
                                    ns_uuid=ns_uuid,
                                    cascade_set=cascade_set,
                                    contradiction_id=contradiction_id,
                                    resolved_by=resolved_by,
                                    pool=self.pg_pool,
                                )

                except Exception:
                    log.exception("ATMS cascade failed during contradiction resolution")

                return dict(row)


# ---------------------------------------------------------------------------
# Batch 111 — module-level helper (not a method; avoids `self` threading)
# ---------------------------------------------------------------------------


async def _reopen_superseded_consolidations(
    *,
    conn: object,
    ns_uuid: UUID,
    cascade_set: set[str],
    contradiction_id: str,
    resolved_by: str,
    pool: asyncpg.Pool,
) -> None:
    """Delete consolidated memories whose source set is fully inside *cascade_set*,
    restore source salience to 0.5, and emit a ``consolidation_requeue`` event.

    A consolidated memory is "stale" when ALL memories in its ``derived_from``
    JSON array have been retracted (i.e., are in *cascade_set*).  Only those rows
    are deleted; partial-overlap rows are left — they will be re-consolidated
    naturally on the next consolidation run once their remaining sources decay.

    Salience restore reuses the existing ``ConsolidationWorker.restore_source_salience``
    path (same INSERT … ON CONFLICT DO UPDATE pattern as ``_update_kg`` decay).
    """
    from nce.consolidation import ConsolidationWorker
    from nce.event_log import append_event

    # Find consolidated memories whose every derived_from ID is in cascade_set.
    # These may have already been soft-deleted by persist_atms_invalidation above,
    # so we do NOT filter on valid_to IS NULL here — we look for rows that are
    # members of the retracted set and were consolidated.
    consol_rows = await conn.fetch(  # type: ignore[attr-defined]
        """
        SELECT id, derived_from
        FROM memories
        WHERE namespace_id = $1::uuid
          AND memory_type = 'consolidated'
          AND id = ANY($3::uuid[])
          AND derived_from IS NOT NULL
          AND derived_from != 'null'::jsonb
          AND (
              SELECT count(*)
              FROM jsonb_array_elements_text(derived_from) AS src_id
              WHERE src_id <> ALL($2::text[])
          ) = 0
        """,
        ns_uuid,
        list(cascade_set),
        [mid for mid in cascade_set if _is_valid_uuid_str(mid)],
    )

    if not consol_rows:
        return

    worker = ConsolidationWorker(pool=pool, provider=None)  # type: ignore[arg-type]

    for crow in consol_rows:
        consol_id = str(crow["id"])
        derived_raw = crow["derived_from"]
        if isinstance(derived_raw, str):
            import json as _json

            source_ids: list[str] = _json.loads(derived_raw)
        else:
            source_ids = [str(s) for s in (derived_raw or [])]

        # Soft-delete the stale consolidated memory.
        await conn.execute(  # type: ignore[attr-defined]
            """
            UPDATE memories
            SET valid_to = now()
            WHERE id = $1::uuid
              AND namespace_id = $2::uuid
              AND valid_to IS NULL
            """,
            crow["id"],
            ns_uuid,
        )

        # Restore salience for source memories (winner side may already be valid).
        surviving_sources = [sid for sid in source_ids if sid not in cascade_set]
        await worker.restore_source_salience(
            conn,
            namespace_id=ns_uuid,
            source_memory_ids=surviving_sources or source_ids,
        )

        # Emit consolidation_requeue event (WORM audit trail).
        await append_event(
            conn=conn,  # type: ignore[arg-type]
            namespace_id=ns_uuid,
            agent_id=resolved_by,
            event_type="consolidation_requeue",
            params={
                "contradiction_id": contradiction_id,
                "deleted_consolidated_id": consol_id,
                "requeued_source_ids": sorted(surviving_sources or source_ids),
            },
            result_summary={
                "status": "success",
                "deleted_consolidated_id": consol_id,
            },
        )
