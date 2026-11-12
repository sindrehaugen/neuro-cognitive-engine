"""Semantic search with pgvector cosine + FTS hybrid ranking.

Extracted from ``MemoryOrchestrator`` (Item #22) so the query-building logic
can be tested and maintained independently of the orchestrator's lifecycle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import asyncpg
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient
from pypika import Field, Order, Parameter, Query, Table
from pypika.enums import JoinType
from pypika.terms import Term

from nce.config import cfg
from nce.db_utils import assert_namespace_filter, scoped_pg_session
from nce.embeddings import VECTOR_DIM
from nce.models import _MAX_TOP_K, NamespaceCognitiveConfig

log = logging.getLogger("nce-semantic-search")

_EMBED_TIMEOUT_SECONDS: float = 10.0
_MAX_RAW_DATA_CHARS: int = 16_000

_SKU_RE = re.compile(r"\b[A-Za-z]{2,6}-\d{2,8}(?:-[A-Za-z0-9]+)*\b")
_NOR_ORGNR_RE = re.compile(r"\b\d{9}\b")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6_RE = re.compile(
    r"\b(?:[0-9a-fA-F]{1,4}:)+(?::[0-9a-fA-F]{1,4})+\b|\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b"
)


def extract_exact_identifiers(query: str) -> list[str]:
    """Extract structured identifiers (SKU codes, Norwegian orgnr, IPv4/IPv6)."""
    identifiers: list[str] = []
    for match in _SKU_RE.finditer(query):
        identifiers.append(match.group(0))
    for match in _NOR_ORGNR_RE.finditer(query):
        identifiers.append(match.group(0))
    for match in _IPV4_RE.finditer(query):
        parts = match.group(0).split(".")
        if all(0 <= int(p) <= 255 for p in parts if p.isdigit()):
            identifiers.append(match.group(0))
    for match in _IPV6_RE.finditer(query):
        identifiers.append(match.group(0))

    seen: set[str] = set()
    deduped: list[str] = []
    for ident in identifiers:
        lower = ident.lower()
        if lower not in seen:
            seen.add(lower)
            deduped.append(ident)
    return deduped


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
    *,
    as_of: Any = None,
    is_replay: bool = False,
    redis_client: Any = None,
) -> None:
    if as_of is not None or is_replay:
        log.debug("Salience reinforcement skipped: as_of=%s is_replay=%s", as_of, is_replay)
        return

    import os

    buffer_in_redis = redis_client is not None and os.environ.get(
        "NCE_BUFFER_SALIENCE_IN_REDIS", ""
    ).lower() in ("1", "true", "yes")

    if buffer_in_redis:
        try:
            from nce.salience_buffer import buffer_salience_boost

            for res in results:
                mem_id = res.get("memory_id")
                if mem_id:
                    await buffer_salience_boost(
                        redis_client=redis_client,
                        namespace_id=namespace_id,
                        memory_id=mem_id,
                        agent_id=agent_id,
                        delta=delta,
                        as_of=as_of,
                        is_replay=is_replay,
                    )
            return
        except Exception:
            log.warning(
                "Redis salience buffering failed; falling back to direct DB reinforcement (non-fatal)"
            )

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
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
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


async def check_nli_relevance_batch(pairs: list[tuple[str, str]]) -> list[float]:
    """Batch scoring for NLI relevance predictions using CrossEncoder or sidecar.

    Preserves (premise, hypothesis) pairing and returns entailment probabilities in order.
    """
    if not pairs:
        return []

    try:
        from nce.embeddings import validated_cognitive_base_url

        base_url = validated_cognitive_base_url()
    except Exception:
        base_url = ""

    if base_url:
        return list(await asyncio.gather(*(check_nli_relevance(p, h) for p, h in pairs)))

    from nce.contradictions import NLIUnavailableError, _executor, _load_nli_model

    model = _load_nli_model()
    if model is None:
        raise NLIUnavailableError("NLI model not loaded")

    import math

    import numpy as np
    import torch

    loop = asyncio.get_running_loop()

    def _predict_batch() -> list[float]:
        try:
            raw_scores = model.predict(pairs)
            if not isinstance(raw_scores, np.ndarray):
                raw_scores = np.array(raw_scores, dtype=np.float32)
            if len(raw_scores) != len(pairs):
                raw_scores = np.array([model.predict([p])[0] for p in pairs], dtype=np.float32)
        except Exception:
            raw_scores = np.array([model.predict([p])[0] for p in pairs], dtype=np.float32)

        probs = torch.nn.functional.softmax(torch.from_numpy(raw_scores), dim=-1).numpy()
        if probs.ndim == 1:
            probs = probs.reshape(1, -1)
        scores: list[float] = []
        for row in probs:
            entail_score = float(row[0])
            if math.isnan(entail_score) or not (0.0 <= entail_score <= 1.0):
                raise NLIUnavailableError(f"NLI score out of bounds: {entail_score}")
            scores.append(entail_score)
        return scores

    return await loop.run_in_executor(_executor, _predict_batch)


_original_check_nli_relevance = check_nli_relevance


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
    at_timestamp=None,
    rerank: bool = False,
    is_replay: bool = False,
    redis_client: Any = None,
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
        at_timestamp: Alias for as_of.
        rerank: Whether to run CrossEncoder reranking.

    Returns:
        List of result dicts with ``memory_id``, ``payload_ref``, ``score``, ``raw_data``.
    """
    if as_of is None and at_timestamp is not None:
        as_of = at_timestamp
    if isinstance(as_of, str):
        as_of = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    if isinstance(as_of, datetime) and as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
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
            # No jsonb codec is registered on the pool, so asyncpg hands the
            # column back as text. me_app, tasks and contradictions decode it
            # themselves for the same reason; without this, "cognitive" in meta
            # is a substring test and the lookup below raises "string indices
            # must be integers" on every search in the namespace.
            meta = ns_row["metadata"]
            if isinstance(meta, str):
                meta = json.loads(meta or "{}")
            if not isinstance(meta, dict):
                meta = {}
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
        p_as_of = builder.param(as_of) if as_of is not None else None

        m = Table("memories").as_("m")
        me = Table("memory_embeddings").as_("me")
        s = Table("memory_salience").as_("s")
        s_sys = Table("memory_salience").as_("s_sys")

        if p_as_of is not None:
            raw_salience_sql = (
                f"LEAST("
                f"CASE WHEN s.updated_at IS NOT NULL AND s.updated_at <= {p_as_of} "
                f"AND s.salience_score > 0.0 THEN s.salience_score ELSE 1.0 END, "
                f"CASE WHEN s_sys.updated_at IS NOT NULL AND s_sys.updated_at <= {p_as_of} "
                f"AND s_sys.salience_score > 0.0 THEN s_sys.salience_score ELSE 1.0 END"
                f")"
            )
            last_updated_sql = (
                f"COALESCE("
                f"CASE WHEN s.updated_at IS NOT NULL AND s.updated_at <= {p_as_of} "
                f"AND s.salience_score > 0.0 THEN s.updated_at END, "
                f"CASE WHEN s_sys.updated_at IS NOT NULL AND s_sys.updated_at <= {p_as_of} "
                f"AND s_sys.salience_score > 0.0 THEN s_sys.updated_at END, "
                f"m.created_at"
                f")"
            )
        else:
            raw_salience_sql = (
                "LEAST(COALESCE(s.salience_score, 1.0), COALESCE(s_sys.salience_score, 1.0))"
            )
            last_updated_sql = "COALESCE(s.updated_at, s_sys.updated_at, m.created_at)"

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
            .left_join(s_sys)  # type: ignore[arg-type]
            .on((m.id == s_sys.memory_id) & RawExpression("s_sys.agent_id = 'system'"))
            .select(
                m.payload_ref,
                m.id.as_("memory_id"),
                distance_expr.as_("distance"),
                RawExpression(raw_salience_sql).as_("raw_salience"),
                RawExpression(last_updated_sql).as_("last_updated"),
            )
            .where(m.namespace_id == p_namespace_id)
            .where(RawExpression("m.memory_type IN ('episodic', 'consolidated', 'semantic')"))
        )
        if p_as_of is None:
            v_cand_query = v_cand_query.where(
                RawExpression(
                    "LEAST(COALESCE(s.salience_score, 1.0), COALESCE(s_sys.salience_score, 1.0)) > 0.0"
                )
            )

        exact_identifiers = extract_exact_identifiers(query)

        # Build extra lexical matching conditions for structured identifiers
        ident_predicates = []
        for ident in exact_identifiers:
            p_ident = builder.param(ident)
            ident_predicates.append(
                f"m.metadata::text ILIKE '%' || {p_ident} || '%' "
                f"OR COALESCE(m.search_tsv, m.content_fts) @@ plainto_tsquery('simple', {p_ident})"
            )
        ident_sql_cond = ""
        ident_boost_sql = ""
        if ident_predicates:
            ident_or_expr = " OR ".join(f"({pred})" for pred in ident_predicates)
            ident_sql_cond = f" OR ({ident_or_expr})"
            ident_boost_sql = f" + CASE WHEN ({ident_or_expr}) THEN 1.0 ELSE 0.0 END"

        fts_cand_query = (
            Query.from_(m)
            .left_join(s)  # type: ignore[arg-type]
            .on((m.id == s.memory_id) & (s.agent_id == p_agent_id))
            .left_join(s_sys)  # type: ignore[arg-type]
            .on((m.id == s_sys.memory_id) & RawExpression("s_sys.agent_id = 'system'"))
            .join(
                RawTable(
                    f"LATERAL (SELECT plainto_tsquery('english', {p_query}) AS q_eng, "
                    f"plainto_tsquery('simple', {p_query}) AS q_simple) AS query"
                )
            )
            .on(RawExpression("true"))  # type: ignore[arg-type]
            .select(
                m.payload_ref,
                m.id.as_("memory_id"),
                RawExpression(
                    "COALESCE(ts_rank_cd(COALESCE(m.search_tsv, m.content_fts), query.q_eng), 0.0) + "
                    f"COALESCE(ts_rank_cd(COALESCE(m.search_tsv, m.content_fts), query.q_simple), 0.0){ident_boost_sql}"
                ).as_("ts_score"),
                RawExpression(raw_salience_sql).as_("raw_salience"),
                RawExpression(last_updated_sql).as_("last_updated"),
            )
            .where(m.namespace_id == p_namespace_id)
            .where(
                RawExpression(
                    f"(COALESCE(m.search_tsv, m.content_fts) @@ query.q_eng "
                    f"OR COALESCE(m.search_tsv, m.content_fts) @@ query.q_simple{ident_sql_cond})"
                )
            )
            .where(RawExpression("m.memory_type IN ('episodic', 'consolidated', 'semantic')"))
        )
        if p_as_of is None:
            fts_cand_query = fts_cand_query.where(
                RawExpression(
                    "LEAST(COALESCE(s.salience_score, 1.0), COALESCE(s_sys.salience_score, 1.0)) > 0.0"
                )
            )

        kg_cand_query = (
            Query.from_(m)
            .left_join(s)  # type: ignore[arg-type]
            .on((m.id == s.memory_id) & (s.agent_id == p_agent_id))
            .left_join(s_sys)  # type: ignore[arg-type]
            .on((m.id == s_sys.memory_id) & RawExpression("s_sys.agent_id = 'system'"))
            .join(
                RawTable(
                    f"""(
                        SELECT payload_ref, 1.0 AS kg_score
                        FROM kg_nodes
                        WHERE namespace_id = {p_namespace_id} AND payload_ref IS NOT NULL
                          AND label ILIKE '%' || {p_query} || '%'
                        UNION
                        SELECT payload_ref, 0.9 AS kg_score
                        FROM kg_edges
                        WHERE namespace_id = {p_namespace_id} AND payload_ref IS NOT NULL
                          AND (subject_label ILIKE '%' || {p_query} || '%' OR object_label ILIKE '%' || {p_query} || '%')
                    ) AS kg"""
                )
            )
            .on(m.payload_ref == RawExpression("kg.payload_ref"))
            .select(
                m.payload_ref,
                m.id.as_("memory_id"),
                RawExpression("kg.kg_score").as_("kg_score"),
                RawExpression(raw_salience_sql).as_("raw_salience"),
                RawExpression(last_updated_sql).as_("last_updated"),
            )
            .where(m.namespace_id == p_namespace_id)
            .where(RawExpression("m.memory_type IN ('episodic', 'consolidated', 'semantic')"))
        )
        if p_as_of is None:
            kg_cand_query = kg_cand_query.where(
                RawExpression(
                    "LEAST(COALESCE(s.salience_score, 1.0), COALESCE(s_sys.salience_score, 1.0)) > 0.0"
                )
            )

        if temporal_retention_days is not None:
            p_days = builder.param(int(temporal_retention_days))
            if p_as_of is not None:
                retention_expr = RawExpression(
                    f"m.created_at >= {p_as_of}::timestamptz - ({p_days}::int * INTERVAL '1 day')"
                )
            else:
                retention_expr = RawExpression(
                    f"m.created_at >= NOW() - ({p_days}::int * INTERVAL '1 day')"
                )
            v_cand_query = v_cand_query.where(retention_expr)
            fts_cand_query = fts_cand_query.where(retention_expr)
            kg_cand_query = kg_cand_query.where(retention_expr)

        if p_as_of is not None:
            as_of_expr = RawExpression(
                f"m.created_at <= {p_as_of} AND (m.valid_to IS NULL OR m.valid_to > {p_as_of})"
            )
            v_cand_query = v_cand_query.where(as_of_expr)
            fts_cand_query = fts_cand_query.where(as_of_expr)
            kg_cand_query = kg_cand_query.where(as_of_expr)
        else:
            valid_expr = RawExpression("m.valid_to IS NULL")
            v_cand_query = v_cand_query.where(valid_expr)
            fts_cand_query = fts_cand_query.where(valid_expr)
            kg_cand_query = kg_cand_query.where(valid_expr)

        v_cand_query = v_cand_query.orderby(Field("distance")).limit(p_candidate_k)  # type: ignore[arg-type]
        fts_cand_query = fts_cand_query.orderby(Field("ts_score"), order=Order.desc).limit(
            p_candidate_k
        )  # type: ignore[arg-type]
        kg_cand_query = kg_cand_query.orderby(Field("kg_score"), order=Order.desc).limit(
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

        kg_candidates = kg_cand_query.as_("kg_candidates")
        kg_ranked = (
            Query.from_(Table("kg_candidates")).select(
                RawExpression("*"),
                RawExpression("ROW_NUMBER() OVER (ORDER BY kg_score DESC)").as_("rank"),
            )
        ).as_("kg_ranked")

        v_tbl = Table("vector_ranked").as_("v")
        f_tbl = Table("fts_ranked").as_("f")
        k_tbl = Table("kg_ranked").as_("k")

        p_alpha = builder.param(float(cognitive_config.alpha))
        p_half_life = builder.param(float(cognitive_config.half_life_days))
        p_need = builder.param(need)

        if p_as_of is not None:
            decay_score_sql = (
                f"CASE WHEN {p_half_life}::double precision <= 0.0 "
                f"THEN COALESCE(v.raw_salience, f.raw_salience, k.raw_salience) "
                f"ELSE COALESCE(v.raw_salience, f.raw_salience, k.raw_salience) * EXP("
                f"-LEAST((LN(2.0) / {p_half_life}::double precision) * GREATEST(0.0, "
                f"EXTRACT(EPOCH FROM ({p_as_of}::timestamptz - COALESCE(v.last_updated, f.last_updated, k.last_updated))) / 86400.0), 20.0)"
                f") END"
            )
        else:
            decay_score_sql = (
                f"nce_decayed_score(COALESCE(v.raw_salience, f.raw_salience, k.raw_salience), "
                f"COALESCE(v.last_updated, f.last_updated, k.last_updated), {p_half_life}::double precision)"
            )

        final_query = (
            Query.with_(vector_candidates, "vector_candidates")
            .with_(vector_ranked, "vector_ranked")
            .with_(fts_candidates, "fts_candidates")
            .with_(fts_ranked, "fts_ranked")
            .with_(kg_candidates, "kg_candidates")
            .with_(kg_ranked, "kg_ranked")
            .from_(v_tbl)
            .join(f_tbl, JoinType.full_outer)
            .on(v_tbl.memory_id == f_tbl.memory_id)
            .join(k_tbl, JoinType.full_outer)
            .on(RawExpression("COALESCE(v.memory_id, f.memory_id) = k.memory_id"))
            .select(
                RawExpression("COALESCE(v.payload_ref, f.payload_ref, k.payload_ref)").as_(
                    "payload_ref"
                ),
                RawExpression("COALESCE(v.memory_id, f.memory_id, k.memory_id)").as_("memory_id"),
                RawExpression(
                    f"(COALESCE(1.0 / (60 + v.rank), 0.0) + COALESCE(1.0 / (60 + f.rank), 0.0) + COALESCE(1.0 / (60 + k.rank), 0.0))"
                    f" * ({p_alpha}::double precision + (1.0::double precision - {p_alpha}::double precision) "
                    f"* {decay_score_sql})"
                ).as_("final_score"),
                RawExpression("COALESCE(v.raw_salience, f.raw_salience, k.raw_salience)").as_(
                    "raw_salience"
                ),
                RawExpression("COALESCE(v.last_updated, f.last_updated, k.last_updated)").as_(
                    "last_updated"
                ),
            )
            .orderby(Field("final_score"), order=Order.desc)
            .orderby(RawExpression("COALESCE(v.memory_id, f.memory_id, k.memory_id)"))
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
        memory_meta_by_id: dict[UUID, dict[str, Any]] = {}
        unresolved_contradiction_ids: set[UUID] = set()

        if memory_ids:
            try:
                mem_rows = await conn.fetch(
                    "SELECT id, payload_ref, wrapped_dek, valid_to, assertion_type, derivation_depth, metadata FROM memories WHERE id = ANY($1::uuid[])",
                    memory_ids,
                )
                for mem_row in mem_rows:
                    if hasattr(mem_row, "get"):
                        m_id = mem_row.get("id")
                        if m_id:
                            memory_meta_by_id[m_id] = dict(mem_row)
                        pref = str(mem_row.get("payload_ref") or "")
                        wd = mem_row.get("wrapped_dek")
                        if wd is not None:
                            wrapped_by_ref[pref] = bytes(wd)
            except Exception:
                wrapped_by_ref = {}
                memory_meta_by_id = {}

            try:
                c_rows = await conn.fetch(
                    "SELECT memory_a_id, memory_b_id FROM contradictions WHERE namespace_id = $1 AND (memory_a_id = ANY($2::uuid[]) OR memory_b_id = ANY($2::uuid[])) AND resolved_at IS NULL",
                    UUID(namespace_id),
                    memory_ids,
                )
                for c_row in c_rows:
                    if hasattr(c_row, "get"):
                        a_id = c_row.get("memory_a_id")
                        b_id = c_row.get("memory_b_id")
                        if a_id:
                            unresolved_contradiction_ids.add(a_id)
                        if b_id:
                            unresolved_contradiction_ids.add(b_id)
            except Exception:
                unresolved_contradiction_ids = set()

    if as_of is None and not is_replay:
        asyncio.create_task(
            _fire_reinforcement(
                pg_pool,
                top_results,
                agent_id,
                namespace_id,
                reinforcement_delta,
                as_of=as_of,
                is_replay=is_replay,
                redis_client=redis_client,
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

                retention_result = retention(
                    ts,
                    "episodic",
                    _now=as_of if isinstance(as_of, datetime) else None,
                )
                stale = retention_result.prune_eligible
                confidence = min(1.0, max(0.0, salience_score * retention_result.retention))
            except Exception:
                pass

        mid = res["memory_id"]
        meta_row = memory_meta_by_id.get(mid, {})
        meta_dict = meta_row.get("metadata")
        if isinstance(meta_dict, str):
            try:
                meta_dict = json.loads(meta_dict)
            except Exception:
                meta_dict = {}
        if not isinstance(meta_dict, dict):
            meta_dict = {}

        raw_str = (raw if isinstance(raw, str) else json.dumps(raw or "", default=str)).lower()
        meta_str = json.dumps(meta_dict, default=str).lower()

        # Exact identifier boost
        has_exact_match = False
        if exact_identifiers:
            for ident in exact_identifiers:
                ident_lower = ident.lower()
                if ident_lower in raw_str or ident_lower in meta_str:
                    has_exact_match = True
                    break

        score = float(res["score"])
        if has_exact_match:
            score += 100.0

        # Epistemic calculations
        epistemic_warnings: list[str] = []
        valid_to = meta_row.get("valid_to")
        now_dt = datetime.now(timezone.utc)
        ref_time = as_of if as_of is not None else now_dt
        if hasattr(ref_time, "tzinfo") and ref_time.tzinfo is None:
            ref_time = ref_time.replace(tzinfo=timezone.utc)

        if valid_to is not None:
            if hasattr(valid_to, "tzinfo") and valid_to.tzinfo is None:
                valid_to = valid_to.replace(tzinfo=timezone.utc)
            if valid_to <= ref_time:
                epistemic_warnings.append("atms_invalidated")

        if mid in unresolved_contradiction_ids:
            epistemic_warnings.append("unresolved_contradiction")

        derivation_depth = int(meta_row.get("derivation_depth") or 0)
        if derivation_depth >= 2:
            epistemic_warnings.append(f"derived_inference_depth_{derivation_depth}")

        assertion_type = str(meta_row.get("assertion_type") or "fact").lower()
        prov_tier = str(meta_dict.get("provenance_tier") or "").lower()
        if assertion_type == "opinion" or prov_tier == "unverified":
            epistemic_warnings.append("low_provenance_tier")

        if stale is True:
            epistemic_warnings.append("stale_memory_retention")

        # Synthesize confidence_score strictly in [0.0, 1.0]
        c_score = float(confidence)
        if "atms_invalidated" in epistemic_warnings:
            c_score = min(c_score * 0.25, 0.25)
        if "unresolved_contradiction" in epistemic_warnings:
            c_score *= 0.5
        if assertion_type == "opinion" or prov_tier == "unverified":
            c_score *= 0.6
        elif assertion_type == "preference":
            c_score *= 0.8
        elif assertion_type == "observation":
            c_score *= 0.9

        decay_rate = getattr(cfg, "NCE_DERIVATION_CONFIDENCE_DECAY", 0.85)
        if derivation_depth > 0:
            c_score *= decay_rate**derivation_depth

        if stale is True:
            c_score *= 0.8

        confidence_score = float(min(1.0, max(0.0, round(c_score, 4))))

        results.append(
            {
                "memory_id": res["memory_id"],
                "payload_ref": ref,
                "score": score,
                "raw_data": (raw[:_MAX_RAW_DATA_CHARS] if isinstance(raw, str) else raw)
                if doc
                else None,
                "salience_score": salience_score,
                "last_reinforced_at": last_reinforced_at.isoformat()
                if last_reinforced_at and hasattr(last_reinforced_at, "isoformat")
                else last_reinforced_at,
                "confidence": confidence,
                "stale": stale,
                "reranker_score": None,
                "confidence_score": confidence_score,
                "epistemic_warnings": epistemic_warnings,
            }
        )

    results.sort(key=lambda x: (-x["score"], str(x["memory_id"])))

    if rerank and results:
        from nce.contradictions import NLIUnavailableError

        items_to_score = [(idx, r["raw_data"]) for idx, r in enumerate(results) if r["raw_data"]]
        for r in results:
            if not r["raw_data"]:
                r["reranker_score"] = 0.0

        reranked = True
        if items_to_score:
            pairs = [(raw_text, query) for _, raw_text in items_to_score]
            try:
                if check_nli_relevance is not _original_check_nli_relevance:
                    scores = list(
                        await asyncio.gather(
                            *(
                                check_nli_relevance(raw_text, query)
                                for _, raw_text in items_to_score
                            )
                        )
                    )
                else:
                    scores = await check_nli_relevance_batch(pairs)
                for (idx, _), score in zip(items_to_score, scores):
                    results[idx]["reranker_score"] = score
            except (NLIUnavailableError, Exception) as exc:
                log.warning("NLI reranking failed (falling back to database sorting): %s", exc)
                reranked = False

        if reranked:
            results.sort(key=lambda x: (-x["reranker_score"], -x["score"], str(x["memory_id"])))
            for r in results:
                r["confidence"] = r["reranker_score"]
                c_score = float(r["confidence"])
                if "atms_invalidated" in r["epistemic_warnings"]:
                    c_score = min(c_score * 0.25, 0.25)
                if "unresolved_contradiction" in r["epistemic_warnings"]:
                    c_score *= 0.5
                if "low_provenance_tier" in r["epistemic_warnings"]:
                    c_score *= 0.6
                if "stale_memory_retention" in r["epistemic_warnings"]:
                    c_score *= 0.8
                r["confidence_score"] = float(min(1.0, max(0.0, round(c_score, 4))))
    if offset > 0:
        return results[offset : offset + limit]
    return results[:limit]
