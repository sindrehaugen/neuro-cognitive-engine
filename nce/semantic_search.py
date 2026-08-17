"""Semantic search with pgvector cosine + FTS hybrid ranking.

Extracted from ``MemoryOrchestrator`` (Item #22) so the query-building logic
can be tested and maintained independently of the orchestrator's lifecycle.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import UUID

import asyncpg
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient
from pypika import Field, Order, Parameter, Query, Table
from pypika.enums import JoinType
from pypika.terms import Term

from nce.db_utils import assert_namespace_filter, scoped_pg_session
from nce.embeddings import VECTOR_DIM
from nce.models import _MAX_TOP_K, NamespaceCognitiveConfig

log = logging.getLogger("nce-semantic-search")

_EMBED_TIMEOUT_SECONDS: float = 10.0
_MAX_RAW_DATA_CHARS: int = 16_000


class RawExpression(Term):
    """Custom term class for injecting raw SQL fragments into PyPika queries."""

    def __init__(self, sql: str):
        super().__init__()
        self.sql = sql

    def get_sql(self, **kwargs) -> str:
        alias = getattr(self, "alias", None)
        if alias and kwargs.get("with_alias"):
            return f'{self.sql} "{alias}"'
        return self.sql


# SAFETY: RawExpression interpolates only Parameter($N) objects — never raw
# user-controlled strings. All values reach Postgres via bind parameters in
# conn.fetch(sql, *builder.get_params()). Adding raw string interpolation here
# would introduce SQL injection.


class RawTable(Table):
    """Custom table class for injecting unquoted raw SQL/LATERAL in join clauses."""

    def __init__(self, sql: str):
        super().__init__("")
        self.sql = sql

    def get_table_name(self) -> str:
        return self.sql

    def get_sql(self, **kwargs) -> str:
        return self.sql


class AsyncpgQueryBuilder:
    """Stateful query builder wrapper to manage sequential $N placeholders for asyncpg."""

    def __init__(self):
        self._params = []

    def param(self, value: Any) -> Parameter:
        self._params.append(value)
        return Parameter(f"${len(self._params)}")

    def get_params(self) -> list:
        return self._params


async def _fire_reinforcement(
    pool: asyncpg.Pool,
    results: list[dict],
    agent_id: str,
    namespace_id: str,
    delta: float,
) -> None:
    from nce.salience import reinforce

    try:
        async with scoped_pg_session(pool, namespace_id) as _conn:
            for res in results:
                await reinforce(
                    _conn,
                    str(res["memory_id"]),
                    agent_id,
                    namespace_id,
                    delta=delta,
                )
    except Exception:
        log.warning("Reinforcement background task failed (non-fatal)")


async def check_nli_relevance(premise: str, hypothesis: str) -> float:
    """Async wrapper for NLI relevance prediction.

    If NCE_COGNITIVE_BASE_URL is configured, the NLI calculation is offloaded
    out-of-process to the cognitive sidecar to prevent memory usage spikes.
    Otherwise, it runs locally in-process using the CrossEncoder.
    """
    try:
        from nce.embeddings import validated_cognitive_base_url

        base_url = validated_cognitive_base_url()
    except Exception:
        base_url = ""

    if base_url:
        import math

        import httpx

        from nce.contradictions import NLIUnavailableError
        from nce.http_resilience import request_with_retry

        url = f"{base_url}/v1/nlp/nli"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await request_with_retry(
                client,
                "POST",
                url,
                json={"premise": premise, "hypothesis": hypothesis},
                operation_name="nlp_sidecar:nli",
            )
            data = resp.json()
            score = float(data["score"])
            if math.isnan(score) or not (0.0 <= score <= 1.0):
                raise NLIUnavailableError(f"Remote NLI score out of bounds: {score}")
            return 1.0 - score

    from nce.contradictions import NLIUnavailableError, _executor, _load_nli_model

    model = _load_nli_model()
    if model is None:
        raise NLIUnavailableError("NLI model not loaded")

    import math

    import torch

    loop = asyncio.get_running_loop()

    def _predict() -> float:
        scores = model.predict([(premise, hypothesis)])
        probs = torch.nn.functional.softmax(torch.from_numpy(scores), dim=1).numpy()[0]
        # DeBERTa NLI: 0=entail, 1=neutral, 2=contradiction
        entail_score = float(probs[0])
        if math.isnan(entail_score) or not (0.0 <= entail_score <= 1.0):
            raise NLIUnavailableError(f"NLI score out of bounds: {entail_score}")
        return entail_score

    return await loop.run_in_executor(_executor, _predict)


async def semantic_search(
    *,
    pg_pool: asyncpg.Pool,
    mongo_client: AsyncIOMotorClient,
    embedding_fn,
    query: str,
    namespace_id: str,
    agent_id: str,
    limit: int = 5,
    offset: int = 0,
    as_of=None,
    rerank: bool = False,
) -> list[dict]:
    """Semantic search with pgvector cosine + FTS hybrid ranking.

    Args:
        pg_pool: PostgreSQL connection pool.
        mongo_client: MongoDB client (used to hydrate episode results).
        embedding_fn: Async callable ``(str) -> list[float]``.
        query: Free-text query string.
        namespace_id: Target namespace UUID (string).
        agent_id: Agent identifier.
        limit: Maximum results to return.
        offset: Pagination offset.
        as_of: Optional time-travel timestamp.

    Returns:
        List of result dicts with ``memory_id``, ``payload_ref``, ``score``, ``raw_data``.
    """
    limit = max(1, min(int(limit), _MAX_TOP_K))
    offset = max(0, int(offset))
    need = offset + limit
    candidate_k = min(max(need * 4, 20), 2000)

    cognitive_config = NamespaceCognitiveConfig()
    temporal_retention_days = 90
    active_model_id = None

    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        ns_row = await conn.fetchrow(
            "SELECT metadata FROM namespaces WHERE id = $1", UUID(namespace_id)
        )
        if ns_row:
            meta = ns_row["metadata"]
            if "cognitive" in meta:
                cognitive_config = NamespaceCognitiveConfig(**meta["cognitive"])
            if "temporal_retention_days" in meta:
                temporal_retention_days = meta["temporal_retention_days"]

        active_model_id = await conn.fetchval(
            "SELECT id FROM embedding_models WHERE status = 'active' LIMIT 1"
        )

    vector = await asyncio.wait_for(embedding_fn(query), timeout=_EMBED_TIMEOUT_SECONDS)
    if len(vector) != VECTOR_DIM:
        raise ValueError(f"embedding_fn returned dim {len(vector)}, expected {VECTOR_DIM}")

    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        builder = AsyncpgQueryBuilder()
        p_vector = builder.param(json.dumps(vector))
        p_namespace_id = builder.param(UUID(namespace_id))
        p_agent_id = builder.param(agent_id)
        p_candidate_k = builder.param(candidate_k)
        p_query = builder.param(query)

        m = Table("memories").as_("m")
        me = Table("memory_embeddings").as_("me")
        s = Table("memory_salience").as_("s")

        v_cand_query = Query.from_(m)
        if active_model_id:
            distance_expr = RawExpression(f"me.embedding <=> {p_vector}::vector")
            v_cand_query = v_cand_query.join(me).on(
                (m.id == me.memory_id) & (me.model_id == active_model_id)
            )
        else:
            distance_expr = RawExpression(f"m.embedding <=> {p_vector}::vector")

        v_cand_query = (
            v_cand_query.left_join(s)  # type: ignore[arg-type]
            .on((m.id == s.memory_id) & (s.agent_id == p_agent_id))
            .select(
                m.payload_ref,
                m.id.as_("memory_id"),
                distance_expr.as_("distance"),
                RawExpression("COALESCE(s.salience_score, 1.0)").as_("raw_salience"),
                RawExpression("COALESCE(s.updated_at, m.created_at)").as_("last_updated"),
            )
            .where(m.namespace_id == p_namespace_id)
            .where(m.memory_type == "episodic")
            .where(RawExpression("COALESCE(s.salience_score, 1.0) > 0.0"))
        )

        fts_cand_query = (
            Query.from_(m)
            .left_join(s)  # type: ignore[arg-type]
            .on((m.id == s.memory_id) & (s.agent_id == p_agent_id))
            .join(RawTable(f"LATERAL websearch_to_tsquery('english', {p_query}) AS query"))
            .on(RawExpression("true"))  # type: ignore[arg-type]
            .select(
                m.payload_ref,
                m.id.as_("memory_id"),
                RawExpression("ts_rank_cd(m.content_fts, query)").as_("ts_score"),
                RawExpression("COALESCE(s.salience_score, 1.0)").as_("raw_salience"),
                RawExpression("COALESCE(s.updated_at, m.created_at)").as_("last_updated"),
            )
            .where(m.namespace_id == p_namespace_id)
            .where(RawExpression("m.content_fts @@ query"))
            .where(m.memory_type == "episodic")
            .where(RawExpression("COALESCE(s.salience_score, 1.0) > 0.0"))
        )

        if temporal_retention_days is not None:
            p_days = builder.param(int(temporal_retention_days))
            retention_expr = RawExpression(
                f"m.created_at >= NOW() - ({p_days}::int * INTERVAL '1 day')"
            )
            v_cand_query = v_cand_query.where(retention_expr)
            fts_cand_query = fts_cand_query.where(retention_expr)

        if as_of:
            p_as_of = builder.param(as_of)
            as_of_expr = RawExpression(f"m.created_at <= {p_as_of}")
            v_cand_query = v_cand_query.where(as_of_expr)
            fts_cand_query = fts_cand_query.where(as_of_expr)

        v_cand_query = v_cand_query.orderby(Field("distance")).limit(p_candidate_k)  # type: ignore[arg-type]
        fts_cand_query = fts_cand_query.orderby(Field("ts_score"), order=Order.desc).limit(
            p_candidate_k
        )  # type: ignore[arg-type]

        vector_candidates = v_cand_query.as_("vector_candidates")
        vector_ranked = (
            Query.from_(Table("vector_candidates")).select(
                RawExpression("*"),
                RawExpression("ROW_NUMBER() OVER (ORDER BY distance ASC)").as_("rank"),
            )
        ).as_("vector_ranked")

        fts_candidates = fts_cand_query.as_("fts_candidates")
        fts_ranked = (
            Query.from_(Table("fts_candidates")).select(
                RawExpression("*"),
                RawExpression("ROW_NUMBER() OVER (ORDER BY ts_score DESC)").as_("rank"),
            )
        ).as_("fts_ranked")

        v_tbl = Table("vector_ranked").as_("v")
        f_tbl = Table("fts_ranked").as_("f")

        p_alpha = builder.param(float(cognitive_config.alpha))
        p_half_life = builder.param(float(cognitive_config.half_life_days))
        p_need = builder.param(need)

        final_query = (
            Query.with_(vector_candidates, "vector_candidates")
            .with_(vector_ranked, "vector_ranked")
            .with_(fts_candidates, "fts_candidates")
            .with_(fts_ranked, "fts_ranked")
            .from_(v_tbl)
            .join(f_tbl, JoinType.full_outer)
            .on(v_tbl.memory_id == f_tbl.memory_id)
            .select(
                RawExpression("COALESCE(v.payload_ref, f.payload_ref)").as_("payload_ref"),
                RawExpression("COALESCE(v.memory_id, f.memory_id)").as_("memory_id"),
                RawExpression(
                    f"(COALESCE(1.0 / (60 + v.rank), 0.0) + COALESCE(1.0 / (60 + f.rank), 0.0))"
                    f" * ({p_alpha}::double precision + (1.0::double precision - {p_alpha}::double precision) "
                    f"* nce_decayed_score(COALESCE(v.raw_salience, f.raw_salience), "
                    f"COALESCE(v.last_updated, f.last_updated), {p_half_life}::double precision))"
                ).as_("final_score"),
                RawExpression("COALESCE(v.raw_salience, f.raw_salience)").as_("raw_salience"),
                RawExpression("COALESCE(v.last_updated, f.last_updated)").as_("last_updated"),
            )
            .orderby(Field("final_score"), order=Order.desc)
            .orderby(RawExpression("COALESCE(v.memory_id, f.memory_id)"))
            .limit(p_need)  # type: ignore[arg-type]
        )

        # Defense-in-depth: verify the rendered SQL carries a namespace_id
        # predicate and the namespace UUID is bound as a parameter before we
        # hit Postgres.  memory_embeddings has no DB-level RLS; the guard is
        # the query-layer tenant-isolation contract (see assert_namespace_filter
        # in nce/db_utils.py for the kg_node_embeddings asymmetry explanation).
        assert_namespace_filter(final_query.get_sql(), builder.get_params(), namespace_id)
        rows = await conn.fetch(final_query.get_sql(), *builder.get_params())

        top_results = [
            {
                "payload_ref": row["payload_ref"],
                "memory_id": row["memory_id"],
                "score": row["final_score"],
                "salience_score": float(row.get("raw_salience", 1.0)),
                "last_reinforced_at": row.get("last_updated"),
            }
            for row in rows
        ]
        reinforcement_delta = cognitive_config.reinforcement_delta

        # Part II.4: fetch the wrapped DEK for each result so encrypted raw_data
        # can be decrypted on hydration; legacy rows return NULL → plaintext.
        memory_ids = [r["memory_id"] for r in top_results if r.get("memory_id")]
        wrapped_by_ref: dict[str, bytes | None] = {}
        if memory_ids:
            try:
                dek_rows = await conn.fetch(
                    "SELECT payload_ref, wrapped_dek FROM memories WHERE id = ANY($1::uuid[])",
                    memory_ids,
                )
                for dek_row in dek_rows:
                    wd = dek_row.get("wrapped_dek") if hasattr(dek_row, "get") else None
                    if wd is None:
                        continue
                    wrapped_by_ref[str(dek_row["payload_ref"] or "")] = bytes(wd)
            except Exception:
                # Defensive: a DEK lookup failure must not break search; rows
                # then read as plaintext (only encrypted rows would be affected).
                wrapped_by_ref = {}

    asyncio.create_task(
        _fire_reinforcement(
            pg_pool,
            top_results,
            agent_id,
            namespace_id,
            reinforcement_delta,
        )
    )

    from nce.db_utils import scoped_mongo_session

    oid_map: dict[str, ObjectId] = {}
    for res in top_results:
        ref = str(res.get("payload_ref") or "")
        try:
            oid_map[ref] = ObjectId(ref)
        except Exception:
            pass

    docs: dict[str, Any] = {}
    if oid_map:
        async with scoped_mongo_session(mongo_client, namespace_id) as s_db:
            async for doc in s_db.episodes.find(
                {"_id": {"$in": list(oid_map.values())}},
                {"raw_data": 1},
            ):
                docs[str(doc["_id"])] = doc

    from datetime import datetime, timezone

    from nce.envelope import maybe_decrypt_raw_data
    from nce.temporal_decay import retention

    results = []
    for res in top_results:
        ref = str(res.get("payload_ref") or "")
        doc = docs.get(ref)
        # Part II.4: transparently decrypt encrypted raw_data; legacy rows
        # (wrapped_dek NULL) pass through as plaintext.
        raw = maybe_decrypt_raw_data(doc.get("raw_data"), wrapped_by_ref.get(ref)) if doc else ""

        salience_score = res.get("salience_score", 1.0)
        last_reinforced_at = res.get("last_reinforced_at")

        confidence = min(1.0, max(0.0, salience_score))
        stale = False
        if last_reinforced_at is not None:
            try:
                if isinstance(last_reinforced_at, str):
                    ts = datetime.fromisoformat(last_reinforced_at.replace("Z", "+00:00"))
                else:
                    ts = last_reinforced_at

                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)

                retention_result = retention(ts, "episodic")
                stale = retention_result.prune_eligible
                confidence = min(1.0, max(0.0, salience_score * retention_result.retention))
            except Exception:
                pass

        results.append(
            {
                "memory_id": res["memory_id"],
                "payload_ref": ref,
                "score": res["score"],
                "raw_data": (raw[:_MAX_RAW_DATA_CHARS] if isinstance(raw, str) else raw)
                if doc
                else None,
                "salience_score": salience_score,
                "last_reinforced_at": last_reinforced_at.isoformat()
                if last_reinforced_at and hasattr(last_reinforced_at, 'isoformat')
                else last_reinforced_at,
                "confidence": confidence,
                "stale": stale,
                "reranker_score": None,
            }
        )

    if rerank and results:
        from nce.contradictions import NLIUnavailableError

        reranked = True
        for r in results:
            raw_text = r["raw_data"] or ""
            if not raw_text:
                r["reranker_score"] = 0.0
            else:
                try:
                    nli_score = await check_nli_relevance(raw_text, query)
                    r["reranker_score"] = nli_score
                except (NLIUnavailableError, Exception) as exc:
                    log.warning("NLI reranking failed (falling back to database sorting): %s", exc)
                    reranked = False
                    break

        if reranked:
            results.sort(key=lambda x: (-x["reranker_score"], -x["score"], str(x["memory_id"])))
            for r in results:
                r["confidence"] = r["reranker_score"]
    return results
