"""
GraphOrchestrator — domain orchestrator for GraphRAG traversal and codebase search.

Extracted from NCEEngine (Prompt 54, Step 1) following the same lazy-init
delegate pattern used by MemoryOrchestrator.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING
from uuid import UUID

import asyncpg
from motor.motor_asyncio import AsyncIOMotorClient

if TYPE_CHECKING:
    from nce.graph_query import GraphRAGTraverser

from nce.constants import (
    ALLOWED_LANGUAGES as _ALLOWED_LANGUAGES,
)
from nce.constants import (
    MAX_TOP_K as _MAX_TOP_K,
)
from nce.constants import (
    SAFE_ID_RE as _SAFE_ID_RE,
)
from nce.db_utils import scoped_pg_session  # noqa: F401
from nce.mongo_bulk import fetch_code_files_raw_by_ref, normalize_payload_ref
from nce.orchestrators._base import OrchestratorBase

log = logging.getLogger("nce-orchestrator.graph")


class GraphOrchestrator(OrchestratorBase):
    """Domain orchestrator for graph search and codebase semantic search."""

    def __init__(
        self,
        pg_pool: asyncpg.Pool,
        mongo_client: AsyncIOMotorClient,
        graph_traverser: GraphRAGTraverser,  # GraphRAGTraverser
        embed_fn,  # async callable: (str) -> list[float]
    ):
        super().__init__(pg_pool, mongo_client)
        self._graph_traverser = graph_traverser
        self._embed = embed_fn

    # ------------------------------------------------------------------
    # GraphRAG traversal
    # ------------------------------------------------------------------

    async def graph_search(self, payload) -> dict:
        """
        [Phase 2.2] GraphRAG traversal with temporal and user scoping.
        """
        if self._graph_traverser is None:
            raise RuntimeError("Engine not connected — call connect() first")

        if not payload.query or not str(payload.query).strip():
            raise ValueError("graph_search requires a non-empty query")

        subgraph = await self._graph_traverser.search(
            payload.query,
            namespace_id=str(payload.namespace_id),
            max_depth=payload.max_depth,
            anchor_top_k=payload.anchor_top_k,
            user_id=payload.agent_id,
            private=bool(payload.agent_id),
            as_of=payload.as_of,
            max_edges_per_node=payload.max_edges_per_node,
            edge_limit=payload.edge_limit,
            edge_offset=payload.edge_offset,
        )
        return subgraph.to_dict()

    # ------------------------------------------------------------------
    # Codebase search (hybrid vector + FTS with RRF)
    # ------------------------------------------------------------------

    async def search_codebase(
        self,
        query: str,
        namespace_id: str | None = None,
        language_filter: str | None = None,
        top_k: int = 5,
        *,
        user_id: str | None = None,
        private: bool = False,
        aspect: str | None = None,
    ) -> list[dict]:
        top_k = max(1, min(top_k, _MAX_TOP_K))
        if language_filter and language_filter not in _ALLOWED_LANGUAGES:
            raise ValueError(f"Invalid language_filter '{language_filter}'")
        if private:
            if not user_id or not _SAFE_ID_RE.match(user_id):
                raise ValueError("private codebase search requires valid user_id")
        elif user_id is not None and not _SAFE_ID_RE.match(user_id):
            raise ValueError("Invalid user_id format")
        if aspect and aspect not in ("code_intent", "nl_intent"):
            raise ValueError(f"Invalid aspect '{aspect}'")

        query = query.strip()
        if not query:
            return []
        if len(query) > 1000:
            raise ValueError("query too long (max 1000 characters)")

        try:
            vector = await asyncio.wait_for(self._embed(query), timeout=10.0)
        except asyncio.TimeoutError:
            log.error("Embedding call timed out for query prefix=%r", query[:80])
            raise RuntimeError("Embedding service timed out") from None
        if not isinstance(vector, list) or not vector:
            raise ValueError("Embedding function returned invalid output (empty or non-list)")

        candidate_k = min(top_k * 4, 500)

        async with self.scoped_session(namespace_id or "default") as conn:
            if private:
                scope_clause = "AND user_id = $5"
                query_params: list = [
                    json.dumps(vector),
                    candidate_k,
                    query,
                    top_k,
                    user_id,
                ]
                next_i = 6
            else:
                scope_clause = "AND user_id IS NULL"
                query_params = [json.dumps(vector), candidate_k, query, top_k]
                next_i = 5

            lang_clause = f"AND language = ${next_i}" if language_filter else ""
            if language_filter:
                query_params.append(language_filter)
                next_i += 1

            embedding_col = "ea.embedding" if aspect else "m.embedding"
            aspect_join = (
                f"JOIN embedding_aspects ea ON m.id = ea.memory_id AND ea.aspect = ${next_i}"
                if aspect
                else ""
            )
            if aspect:
                query_params.append(aspect)
                next_i += 1

            # NOTE: scope_clause and lang_clause inject ONLY hardcoded string literals
            # or parameterized placeholders ($N). No user-controlled values are
            # ever interpolated directly into the SQL string. All user values are
            # passed as asyncpg positional parameters ($1..$N) in query_params.
            # Explicit namespace_id filter added as defense-in-depth (Fix 2B).
            sql = f"""
                WITH vector_candidates AS (
                    SELECT m.id, {embedding_col} <=> $1::vector AS distance
                    FROM memories m
                    {aspect_join}
                    WHERE m.memory_type = 'code_chunk'
                      AND m.namespace_id = current_setting('nce.namespace_id')::uuid
                      {scope_clause} {lang_clause}
                    ORDER BY distance ASC
                    LIMIT $2
                ),
                vector_ranked AS (
                    SELECT id, ROW_NUMBER() OVER (ORDER BY distance ASC) as rank
                    FROM vector_candidates
                ),
                fts_candidates AS (
                    SELECT id, ts_rank_cd(content_fts, query) AS ts_score
                    FROM memories,
                    LATERAL websearch_to_tsquery('english', $3) AS query
                    WHERE content_fts @@ query
                      AND memory_type = 'code_chunk'
                      AND namespace_id = current_setting('nce.namespace_id')::uuid
                      {scope_clause} {lang_clause}
                    ORDER BY ts_score DESC
                    LIMIT $2
                ),
                fts_ranked AS (
                    SELECT id, ROW_NUMBER() OVER (ORDER BY ts_score DESC) as rank
                    FROM fts_candidates
                )
                SELECT
                    COALESCE(v.id, f.id) AS id,
                    (COALESCE(1.0 / (60 + v.rank), 0.0) +
                     COALESCE(1.0 / (60 + f.rank), 0.0)) AS score
                FROM vector_ranked v
                FULL OUTER JOIN fts_ranked f ON v.id = f.id
                ORDER BY score DESC, id ASC
                LIMIT $4
            """

            fused_rows = await conn.fetch(sql, *query_params)

            if not fused_rows:
                return []

            memory_ids = [UUID(str(r["id"])) for r in fused_rows]
            score_map = {str(r["id"]): float(r["score"]) for r in fused_rows}

            rows = await conn.fetch(
                """
                SELECT m.id, m.payload_ref, m.language, m.filepath, m.assertion_type,
                       m.metadata, m.content_fts, m.name, m.node_type, m.start_line, m.end_line
                FROM memories m
                WHERE m.id = ANY($1::uuid[])
                """,
                memory_ids,
            )

            try:
                code_docs = await fetch_code_files_raw_by_ref(
                    self._mongo_db,
                    [normalize_payload_ref(r["payload_ref"]) for r in rows],
                )
            except Exception as exc:
                log.warning(
                    "Code file hydrate failed for %d refs: %s",
                    len(rows),
                    type(exc).__name__,
                )
                code_docs = {}

            row_by_id = {str(r["id"]): r for r in rows}
            results = []

            for fused in fused_rows:
                mid = str(fused["id"])
                row = row_by_id.get(mid)
                if row is None:
                    continue
                meta = {}
                raw = row.get("metadata")
                if raw is not None:
                    if isinstance(raw, str):
                        try:
                            meta = json.loads(raw)
                        except json.JSONDecodeError:
                            log.warning(
                                "Invalid metadata JSON for memory_id=%s — treating as empty dict",
                                mid,
                            )
                    elif isinstance(raw, dict):
                        meta = dict(raw)
                name = row["name"] or meta.get("name") or row["filepath"]
                node_type = row["node_type"] or meta.get("node_type") or "chunk"
                start_line = (
                    row["start_line"]
                    if row["start_line"] is not None
                    else meta.get("start_line", 0)
                )
                end_line = (
                    row["end_line"] if row["end_line"] is not None else meta.get("end_line", 0)
                )

                ref_key = normalize_payload_ref(row["payload_ref"])
                raw_code = code_docs.get(ref_key, "") if ref_key else ""
                excerpt = str(raw_code)[:600] if raw_code else ""

                results.append(
                    {
                        "memory_id": mid,
                        "score": score_map.get(mid, 0.0),
                        "filepath": row["filepath"],
                        "language": row["language"],
                        "node_type": node_type,
                        "name": name,
                        "start_line": start_line,
                        "end_line": end_line,
                        "excerpt": excerpt,
                    }
                )

            return results
