#!/usr/bin/env python3
"""Deterministic 100k-Row Diagnostic & Hot-Path Latency Profiler (v3.5.1).

Seeds a realistic 100k-row tenant (`Tenant-Perf`) plus a 500-row canary tenant
(`Tenant-Canary`) against an isolated local stack (`nce_v351_perf`), captures
authentic `EXPLAIN (ANALYZE, BUFFERS, VERBOSE)` plans inside `scoped_pg_session`,
records sub-stage wall-clock latency breakdowns (`time.perf_counter_ns`), and
verifies zero cross-tenant leakage.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Ensure test master key is set if not already present
if not os.environ.get("NCE_MASTER_KEY"):
    import base64

    os.environ["NCE_MASTER_KEY"] = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()
os.environ.setdefault("PG_DSN", "postgresql://mcp_user:mcp_password@127.0.0.1:5432/memory_meta")
os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:27017")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/0")
os.environ.setdefault("MINIO_ENDPOINT", "127.0.0.1:9002")
os.environ.setdefault("MINIO_ACCESS_KEY", "mcp_admin")
os.environ.setdefault("MINIO_SECRET_KEY", "super_secure_minio_password")
os.environ.setdefault("NCE_COGNITIVE_BASE_URL", "http://127.0.0.1:11435")
os.environ.setdefault("NCE_ENVELOPE_ENCRYPTION_ENABLED", "true")

import asyncpg  # noqa: E402
from bson import ObjectId  # noqa: E402
from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

from nce import embeddings as _embeddings  # noqa: E402
from nce.db_utils import scoped_pg_session  # noqa: E402
from nce.envelope import encrypt_raw_data, maybe_decrypt_raw_data  # noqa: E402
from nce.event_log import append_event  # noqa: E402
from nce.models import (  # noqa: E402
    AssertionType,
    GraphSearchRequest,
    MemoryType,
    StoreMemoryRequest,
)
from nce.mongo_bulk import fetch_episodes_raw_by_ref, normalize_payload_ref  # noqa: E402
from nce.orchestrator import NCEEngine  # noqa: E402
from nce.working_memory import compile_working_memory  # noqa: E402

TENANT_PERF_ID = UUID("11111111-2222-4333-8444-555555555555")
TENANT_CANARY_ID = UUID("99999999-8888-4777-8666-555555555555")

PROSE_QUERY = "quarterly cloud infrastructure architecture review"
IDENT_QUERY = "SKU-9942-X 987654321 10.42.0.15"


def _pct(vals: list[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    idx = min(len(s) - 1, max(0, int(round((len(s) - 1) * q))))
    return round(s[idx], 3)


def _stats(vals: list[float]) -> dict[str, float]:
    if not vals:
        return {"p50": 0.0, "p95": 0.0, "mean": 0.0, "min": 0.0, "max": 0.0}
    return {
        "p50": _pct(vals, 0.50),
        "p95": _pct(vals, 0.95),
        "mean": round(statistics.mean(vals), 3),
        "min": round(min(vals), 3),
        "max": round(max(vals), 3),
    }


async def seed_100k_if_needed(
    pg_pool: asyncpg.Pool, mongo_client: AsyncIOMotorClient, engine: NCEEngine
) -> dict[str, Any]:
    """Idempotently seed 100k memories, 5k kg_nodes, 15k kg_edges, 10k event_log, + 500 canary rows."""
    t0 = time.perf_counter()
    await engine._ensure_initial_signing_key()
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO namespaces (id, slug, metadata)
            VALUES ($1, 'tenant-perf', '{}'::jsonb),
                   ($2, 'tenant-canary', '{}'::jsonb)
            ON CONFLICT (id) DO UPDATE SET metadata = '{}'::jsonb
            """,
            TENANT_PERF_ID,
            TENANT_CANARY_ID,
        )

    async with scoped_pg_session(pg_pool, TENANT_PERF_ID) as conn:
        mem_count = await conn.fetchval(
            "SELECT count(*) FROM memories WHERE namespace_id = $1", TENANT_PERF_ID
        )
    if mem_count and mem_count >= 100_000:
        print(f"[seed] Tenant-Perf already has {mem_count} memories — skipping bulk seed.")
        return {"seeded": False, "existing_memories": mem_count}

    print(
        "[seed] Seeding Tenant-Perf (100k memories, 5k nodes, 15k edges, 10k events) + Tenant-Canary (500)..."
    )

    # Pre-compute 8 deterministic non-zero unit 768-d vectors so HNSW cosine distances are realistic
    import math

    vecs = []
    for k in range(8):
        raw_v = [math.sin((i + 1) * (k + 1) * 0.13) for i in range(768)]
        norm = math.sqrt(sum(x * x for x in raw_v))
        vecs.append([round(x / norm, 6) for x in raw_v])
    vec_strs = [json.dumps(v) for v in vecs]

    # Encrypt 500 representative Mongo episodes for hydration realism (including top hits)
    sample_raw_enc, sample_dek, sample_kid = encrypt_raw_data(
        f" Detailed architectural review notes for {PROSE_QUERY} and {IDENT_QUERY} in production."
    )

    db = mongo_client.memory_archive
    hot_oids = [ObjectId() for _ in range(200)]
    hot_refs = [str(oid) for oid in hot_oids]
    mongo_docs = [
        {
            "_id": oid,
            "namespace_id": str(TENANT_PERF_ID),
            "user_id": "bench-user",
            "session_id": "bench-session",
            "type": "episodic",
            "raw_data": sample_raw_enc,
            "metadata": {"idx": i, "sku": "SKU-9942-X" if i < 25 else f"SKU-{i:04d}-A"},
            "ingested_at": datetime.now(timezone.utc),
        }
        for i, oid in enumerate(hot_oids)
    ]
    await db.episodes.insert_many(mongo_docs, ordered=False)

    async with scoped_pg_session(pg_pool, TENANT_PERF_ID) as conn:
        await conn.execute("DELETE FROM memory_salience WHERE namespace_id = $1", TENANT_PERF_ID)
        await conn.execute("DELETE FROM memories WHERE namespace_id = $1", TENANT_PERF_ID)
        await conn.execute("DELETE FROM kg_edges WHERE namespace_id = $1", TENANT_PERF_ID)
        await conn.execute("DELETE FROM kg_nodes WHERE namespace_id = $1", TENANT_PERF_ID)

        # Seed 100,000 memories in 4 batches of 25,000 using set-based generate_series
        for batch_idx in range(4):
            start_i = batch_idx * 25_000 + 1
            end_i = (batch_idx + 1) * 25_000
            await conn.execute(
                """
                INSERT INTO memories (
                    id, namespace_id, agent_id, user_id, session_id,
                    embedding, content_fts, search_tsv, payload_ref,
                    assertion_type, memory_type, metadata, wrapped_dek, dek_key_id,
                    valid_from, created_at
                )
                SELECT
                    gen_random_uuid(),
                    $1::uuid,
                    'default',
                    'bench-user',
                    'bench-session',
                    (CASE (g % 7)
                        WHEN 0 THEN $2::halfvec(768)
                        WHEN 1 THEN $3::halfvec(768)
                        WHEN 2 THEN $4::halfvec(768)
                        WHEN 3 THEN $5::halfvec(768)
                        WHEN 4 THEN $6::halfvec(768)
                        WHEN 5 THEN $7::halfvec(768)
                        ELSE $8::halfvec(768)
                    END),
                    to_tsvector('english',
                        CASE
                            WHEN g <= 50 THEN 'quarterly cloud infrastructure architecture review SKU-9942-X 987654321 10.42.0.15 doc ' || g::text
                            WHEN g % 200 = 0 THEN 'cloud infrastructure architecture review notes ' || g::text
                            ELSE 'synthetic operational telemetry record number ' || g::text || ' cluster node ' || (g % 500)::text
                        END
                    ),
                    to_tsvector('english',
                        CASE
                            WHEN g <= 50 THEN 'quarterly cloud infrastructure architecture review SKU-9942-X 987654321 10.42.0.15 doc ' || g::text
                            WHEN g % 200 = 0 THEN 'cloud infrastructure architecture review notes ' || g::text
                            ELSE 'synthetic operational telemetry record number ' || g::text || ' cluster node ' || (g % 500)::text
                        END
                    ) || to_tsvector('simple',
                        CASE
                            WHEN g <= 50 THEN 'quarterly cloud infrastructure architecture review SKU-9942-X 987654321 10.42.0.15 doc ' || g::text
                            ELSE 'record ' || g::text
                        END
                    ),
                    CASE WHEN g <= 200 THEN ($9::text[])[g] ELSE lpad(to_hex(g + 1000000), 24, '0') END,
                    (CASE WHEN g % 3 = 0 THEN 'fact' ELSE 'observation' END),
                    'episodic',
                    jsonb_build_object('seq', g, 'tier', CASE WHEN g <= 200 THEN 'hot' ELSE 'warm' END),
                    $10::bytea,
                    $11::text,
                    NOW() - (g || ' seconds')::interval,
                    NOW() - (g || ' seconds')::interval
                FROM generate_series($12::int, $13::int) AS g
                """,
                TENANT_PERF_ID,
                vec_strs[0],
                vec_strs[1],
                vec_strs[2],
                vec_strs[3],
                vec_strs[4],
                vec_strs[5],
                vec_strs[6],
                hot_refs,
                sample_dek,
                sample_kid,
                start_i,
                end_i,
            )
            print(f"  [seed] Inserted memories {start_i}..{end_i}")

        # Seed salience rows for a subset of memories (20,000 rows) to exercise salience joins
        await conn.execute(
            """
            INSERT INTO memory_salience (memory_id, agent_id, namespace_id, salience_score, updated_at)
            SELECT id, 'default', $1::uuid, 0.85, NOW()
            FROM (
                SELECT id FROM memories WHERE namespace_id = $1::uuid LIMIT 20000
            ) sub
            ON CONFLICT DO NOTHING
            """,
            TENANT_PERF_ID,
        )

        # Seed 5,000 kg_nodes
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, embedding, payload_ref, namespace_id, created_at, updated_at)
            SELECT
                CASE
                    WHEN g = 1 THEN 'cloud_infrastructure'
                    WHEN g = 2 THEN 'SKU-9942-X'
                    WHEN g = 3 THEN 'architecture_review'
                    ELSE 'entity_node_' || g::text
                END,
                CASE WHEN g % 4 = 0 THEN 'TOOL' WHEN g % 4 = 1 THEN 'ORG' ELSE 'CONCEPT' END,
                (CASE (g % 4)
                    WHEN 0 THEN $2::vector
                    WHEN 1 THEN $3::vector
                    WHEN 2 THEN $4::vector
                    ELSE $5::vector
                END),
                ($6::text[])[(g % 200) + 1],
                $1::uuid,
                NOW() - (g || ' seconds')::interval,
                NOW()
            FROM generate_series(1, 5000) AS g
            ON CONFLICT (label, namespace_id) DO NOTHING
            """,
            TENANT_PERF_ID,
            vec_strs[0],
            vec_strs[1],
            vec_strs[2],
            vec_strs[4],
            hot_refs,
        )

        # Seed 15,000 kg_edges
        await conn.execute(
            """
            INSERT INTO kg_edges (subject_label, predicate, object_label, confidence, payload_ref, namespace_id, created_at, updated_at)
            SELECT
                CASE
                    WHEN g <= 25 THEN 'cloud_infrastructure'
                    WHEN g <= 50 THEN 'SKU-9942-X'
                    ELSE 'entity_node_' || ((g % 4900) + 1)::text
                END,
                CASE (g % 5)
                    WHEN 0 THEN 'depends_on_' || (g / 5000)::text
                    WHEN 1 THEN 'uses_' || (g / 5000)::text
                    WHEN 2 THEN 'connects_to_' || (g / 5000)::text
                    WHEN 3 THEN 'hosts_' || (g / 5000)::text
                    ELSE 'monitors_' || (g / 5000)::text
                END,
                CASE
                    WHEN g = 1 THEN 'architecture_review'
                    WHEN g = 2 THEN 'SKU-9942-X'
                    ELSE 'entity_node_' || (((g + (g / 4900) + 17) % 4900) + 1)::text
                END,
                0.75 + ((g % 20) * 0.01),
                ($2::text[])[(g % 200) + 1],
                $1::uuid,
                NOW() - (g || ' seconds')::interval,
                NOW()
            FROM generate_series(1, 15000) AS g
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
            """,
            TENANT_PERF_ID,
            hot_refs,
        )

        # Seed procedural_heuristics (50 rows) and contradictions (25 rows) for compile_working_memory
        await conn.execute(
            """
            INSERT INTO procedural_heuristics (
                namespace_id, rule_text, trigger_condition, action_guidance,
                source_type, source_refs, confidence_weight, support_count
            )
            SELECT
                $1::uuid,
                'ALLOW: Verified cloud infrastructure architecture review step ' || g::text,
                'goal contains cloud infrastructure',
                'Proceed with architecture verification gate ' || g::text,
                'governed_decision',
                '[]'::jsonb,
                0.88,
                g
            FROM generate_series(1, 50) AS g
            """,
            TENANT_PERF_ID,
        )

        # Seed 10,000 event_log rows (including authentic signed store_memory events for top memories)
        sample_mems = await conn.fetch(
            "SELECT id, payload_ref FROM memories WHERE namespace_id = $1 ORDER BY created_at DESC LIMIT 20",
            TENANT_PERF_ID,
        )
        async with conn.transaction():
            for idx, mrow in enumerate(sample_mems):
                await append_event(
                    conn=conn,
                    namespace_id=TENANT_PERF_ID,
                    agent_id="default",
                    event_type="store_memory",
                    params={
                        "saga_id": str(uuid.uuid4()),
                        "memory_id": str(mrow["id"]),
                        "payload_ref": mrow["payload_ref"],
                        "assertion_type": "fact",
                        "entities": [
                            {"label": "cloud_infrastructure", "entity_type": "CONCEPT"},
                            {"label": f"entity_node_{idx + 1}", "entity_type": "TOOL"},
                        ],
                        "triplets": [
                            {
                                "subject_label": "cloud_infrastructure",
                                "predicate": "depends_on",
                                "object_label": f"entity_node_{idx + 1}",
                                "confidence": 0.92,
                            }
                        ],
                    },
                )

        existing_events = await conn.fetchval(
            "SELECT count(*) FROM event_log WHERE namespace_id = $1", TENANT_PERF_ID
        )
        remaining_events = max(0, 10_000 - int(existing_events or 0))
        if remaining_events > 0:
            await conn.execute(
                """
                INSERT INTO event_log (
                    id, namespace_id, agent_id, event_type, event_seq, occurred_at,
                    params, signature, signature_key_id, signature_version, chain_hash
                )
                SELECT
                    gen_random_uuid(),
                    $1::uuid,
                    'default',
                    'boost_memory',
                    1000 + g,
                    NOW() - ((10000 - g) || ' seconds')::interval,
                    jsonb_build_object('memory_id', gen_random_uuid()::text, 'factor', 1.1),
                    decode('00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff', 'hex'),
                    'seed-key',
                    2,
                    decode('00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff', 'hex')
                FROM generate_series(1, $2::int) AS g
                """,
                TENANT_PERF_ID,
                remaining_events,
            )
            await conn.execute(
                """
                INSERT INTO event_sequences (namespace_id, seq)
                VALUES ($1::uuid, 20000)
                ON CONFLICT (namespace_id) DO UPDATE SET seq = GREATEST(event_sequences.seq, 20000)
                """,
                TENANT_PERF_ID,
            )

    # Seed Tenant-Canary with 500 rows matching the exact same queries & vectors
    canary_oid = ObjectId()
    await db.episodes.insert_one(
        {
            "_id": canary_oid,
            "namespace_id": str(TENANT_CANARY_ID),
            "user_id": "canary-user",
            "session_id": "canary-session",
            "type": "episodic",
            "raw_data": "CANARY_SECRET_DO_NOT_LEAK quarterly cloud infrastructure architecture review SKU-9942-X",
            "ingested_at": datetime.now(timezone.utc),
        }
    )
    async with scoped_pg_session(pg_pool, TENANT_CANARY_ID) as conn:
        await conn.execute("DELETE FROM memories WHERE namespace_id = $1", TENANT_CANARY_ID)
        await conn.execute(
            """
            INSERT INTO memories (
                id, namespace_id, agent_id, user_id, session_id,
                embedding, content_fts, search_tsv, payload_ref,
                assertion_type, memory_type, metadata
            )
            SELECT
                gen_random_uuid(),
                $1::uuid,
                'default',
                'canary-user',
                'canary-session',
                $2::halfvec(768),
                to_tsvector('english', 'CANARY_SECRET_DO_NOT_LEAK quarterly cloud infrastructure architecture review SKU-9942-X ' || g::text),
                to_tsvector('english', 'CANARY_SECRET_DO_NOT_LEAK quarterly cloud infrastructure architecture review SKU-9942-X ' || g::text),
                $3::text,
                'fact',
                'episodic',
                jsonb_build_object('canary', true, 'seq', g)
            FROM generate_series(1, 500) AS g
            """,
            TENANT_CANARY_ID,
            vec_strs[0],
            str(canary_oid),
        )
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, embedding, payload_ref, namespace_id)
            VALUES ('cloud_infrastructure', 'CONCEPT', $2::vector, $3::text, $1::uuid)
            ON CONFLICT (label, namespace_id) DO NOTHING
            """,
            TENANT_CANARY_ID,
            vec_strs[0],
            str(canary_oid),
        )

    # Run VACUUM ANALYZE on hot tables
    async with pg_pool.acquire() as conn:
        for tbl in (
            "memories",
            "memory_salience",
            "kg_nodes",
            "kg_edges",
            "event_log",
            "procedural_heuristics",
        ):
            await conn.execute(f"VACUUM ANALYZE {tbl}")

    elapsed = round(time.perf_counter() - t0, 2)
    print(f"[seed] Completed 100k seed + VACUUM ANALYZE in {elapsed}s.")
    return {"seeded": True, "elapsed_s": elapsed}


async def capture_explain_plans(pg_pool: asyncpg.Pool) -> dict[str, Any]:
    """Capture authentic EXPLAIN (ANALYZE, BUFFERS) plans inside scoped_pg_session(TENANT_PERF_ID)."""
    import math

    raw_v = [math.sin((i + 1) * 1 * 0.13) for i in range(768)]
    norm = math.sqrt(sum(x * x for x in raw_v))
    vec = [round(x / norm, 6) for x in raw_v]
    vec_json = json.dumps(vec)
    plans: dict[str, str] = {}

    async with scoped_pg_session(pg_pool, TENANT_PERF_ID) as conn:
        # Check current_user and RLS settings
        role_info = await conn.fetchrow(
            "SELECT current_user, current_setting('nce.namespace_id', true) AS ns"
        )
        plans["_session_info"] = f"user={role_info['current_user']} ns={role_info['ns']}"

        # 1. Legacy vector leg (with ::vector cast) vs halfvec(768) cast
        for name, sql, params in [
            (
                "vector_leg_cast_vector",
                """
                EXPLAIN (ANALYZE, BUFFERS)
                SELECT m.id, (m.embedding <=> $1::vector) AS distance
                FROM memories m
                LEFT JOIN memory_salience s ON m.id = s.memory_id AND s.agent_id = $2
                LEFT JOIN memory_salience s_sys ON m.id = s_sys.memory_id AND s_sys.agent_id = 'system'
                WHERE m.namespace_id = $3::uuid AND m.valid_to IS NULL
                  AND LEAST(COALESCE(s.salience_score, 1.0), COALESCE(s_sys.salience_score, 1.0)) > 0.0
                ORDER BY distance ASC LIMIT 30
                """,
                [vec_json, "default", str(TENANT_PERF_ID)],
            ),
            (
                "vector_leg_inner_halfvec",
                """
                EXPLAIN (ANALYZE, BUFFERS)
                SELECT cand.id, cand.distance
                FROM (
                    SELECT m.id, (m.embedding <=> $1::halfvec(768)) AS distance
                    FROM memories m
                    WHERE m.namespace_id = $3::uuid AND m.valid_to IS NULL
                    ORDER BY m.embedding <=> $1::halfvec(768) ASC
                    LIMIT 60
                ) cand
                LEFT JOIN memory_salience s ON cand.id = s.memory_id AND s.agent_id = $2
                LEFT JOIN memory_salience s_sys ON cand.id = s_sys.memory_id AND s_sys.agent_id = 'system'
                WHERE LEAST(COALESCE(s.salience_score, 1.0), COALESCE(s_sys.salience_score, 1.0)) > 0.0
                ORDER BY cand.distance ASC LIMIT 30
                """,
                [vec_json, "default", str(TENANT_PERF_ID)],
            ),
            (
                "lexical_leg_coalesce",
                """
                EXPLAIN (ANALYZE, BUFFERS)
                SELECT m.id,
                       (COALESCE(ts_rank_cd(COALESCE(m.search_tsv, m.content_fts), query.q_eng), 0.0) +
                        COALESCE(ts_rank_cd(COALESCE(m.search_tsv, m.content_fts), query.q_simple), 0.0)) AS fts_score
                FROM memories m
                JOIN LATERAL (
                    SELECT websearch_to_tsquery('english', $1) AS q_eng,
                           plainto_tsquery('simple', $1) AS q_simple
                ) query ON true
                WHERE m.namespace_id = $2::uuid AND m.valid_to IS NULL
                  AND (COALESCE(m.search_tsv, m.content_fts) @@ query.q_eng
                       OR COALESCE(m.search_tsv, m.content_fts) @@ query.q_simple)
                ORDER BY fts_score DESC LIMIT 30
                """,
                [PROSE_QUERY, str(TENANT_PERF_ID)],
            ),
            (
                "lexical_leg_search_tsv_direct",
                """
                EXPLAIN (ANALYZE, BUFFERS)
                SELECT m.id,
                       (COALESCE(ts_rank_cd(m.search_tsv, query.q_eng), 0.0) +
                        COALESCE(ts_rank_cd(m.search_tsv, query.q_simple), 0.0)) AS fts_score
                FROM memories m
                JOIN LATERAL (
                    SELECT websearch_to_tsquery('english', $1) AS q_eng,
                           plainto_tsquery('simple', $1) AS q_simple
                ) query ON true
                WHERE m.namespace_id = $2::uuid AND m.valid_to IS NULL
                  AND m.search_tsv @@ (query.q_eng || query.q_simple)
                ORDER BY fts_score DESC LIMIT 30
                """,
                [PROSE_QUERY, str(TENANT_PERF_ID)],
            ),
            (
                "kg_edges_trgm_or_ilike",
                """
                EXPLAIN (ANALYZE, BUFFERS)
                SELECT payload_ref, confidence AS kg_sim
                FROM kg_edges
                WHERE namespace_id = $1::uuid
                  AND (subject_label ILIKE '%' || $2 || '%' OR object_label ILIKE '%' || $2 || '%')
                  AND payload_ref IS NOT NULL
                """,
                [str(TENANT_PERF_ID), "SKU-9942-X"],
            ),
            (
                "kg_edges_trgm_combined_ilike",
                """
                EXPLAIN (ANALYZE, BUFFERS)
                SELECT payload_ref, confidence AS kg_sim
                FROM kg_edges
                WHERE namespace_id = $1::uuid
                  AND ((subject_label || ' ' || object_label) ILIKE '%' || $2 || '%')
                  AND payload_ref IS NOT NULL
                """,
                [str(TENANT_PERF_ID), "SKU-9942-X"],
            ),
        ]:
            rows = await conn.fetch(sql, *params)
            plans[name] = "\n".join(r[0] for r in rows)

        # Also capture RLS InitPlan evaluation under non-bypassrls nce_app role
        try:
            async with conn.transaction():
                await conn.execute("SET LOCAL ROLE nce_app")
                rls_rows = await conn.fetch(
                    """
                    EXPLAIN (ANALYZE, BUFFERS)
                    SELECT id, payload_ref FROM memories
                    WHERE valid_to IS NULL
                    ORDER BY embedding <=> $1::halfvec(768) ASC
                    LIMIT 10
                    """,
                    vec_json,
                )
                plans["rls_initplan_nce_app"] = "\n".join(r[0] for r in rls_rows)
        except Exception as exc:
            plans["rls_initplan_nce_app"] = f"skipped: {exc}"

    return plans


async def verify_zero_canary_leak(engine: NCEEngine) -> dict[str, Any]:
    """Verify Tenant-Perf never sees Tenant-Canary rows across search, graph, and working memory."""
    hits_prose = await engine.semantic_search(
        query=PROSE_QUERY,
        namespace_id=str(TENANT_PERF_ID),
        agent_id="default",
        limit=20,
    )
    hits_ident = await engine.semantic_search(
        query=IDENT_QUERY,
        namespace_id=str(TENANT_PERF_ID),
        agent_id="default",
        limit=20,
    )
    wm = await compile_working_memory(
        engine,
        goal=PROSE_QUERY,
        namespace_id=TENANT_PERF_ID,
        agent_id="default",
        max_tokens=4000,
    )
    serialized = json.dumps({"prose": hits_prose, "ident": hits_ident, "wm": wm}, default=str)
    leaked = "CANARY_SECRET_DO_NOT_LEAK" in serialized or str(TENANT_CANARY_ID) in serialized
    if leaked:
        raise RuntimeError("CRITICAL SECURITY FAILURE: Cross-tenant canary leak detected!")
    return {
        "canary_leak_count": 0,
        "prose_hits": len(hits_prose),
        "ident_hits": len(hits_ident),
    }


async def profile_substage_latencies(engine: NCEEngine, iterations: int = 30) -> dict[str, Any]:
    """Measure p50/p95/mean ms across 30 warm calls with sub-stage timers."""
    # Warm up caches / pools once
    await engine.semantic_search(
        query=PROSE_QUERY, namespace_id=str(TENANT_PERF_ID), agent_id="default", limit=5
    )
    await engine.graph_search(
        GraphSearchRequest(
            query="cloud_infrastructure",
            namespace_id=TENANT_PERF_ID,
            agent_id="default",
            max_depth=2,
            as_of=datetime.now(timezone.utc),
        )
    )
    await compile_working_memory(
        engine, goal=PROSE_QUERY, namespace_id=TENANT_PERF_ID, agent_id="default", max_tokens=2000
    )

    # 1. semantic_search (prose + identifier + as_of)
    ss_prose_totals: list[float] = []
    ss_ident_totals: list[float] = []
    ss_asof_totals: list[float] = []
    ss_stages: dict[str, list[float]] = {
        "t_redis_gov": [],
        "t_embed_http": [],
        "t_pg_checkout_guc": [],
        "t_sql_exec": [],
        "t_mongo_hydrate": [],
        "t_dek_decrypt": [],
        "t_postprocess_serialize": [],
        "t_total": [],
    }

    from nce.admin_plugins import is_engine_disabled
    from nce.tool_governance import GOVERNANCE

    as_of_ts = datetime.now(timezone.utc)
    for _ in range(iterations):
        # Stage 1: Redis governance
        t0 = time.perf_counter_ns()
        await GOVERNANCE.is_disabled(engine.redis_client, "semantic_search")
        await is_engine_disabled(None, redis_client=engine.redis_client, is_prod=False)
        t_redis = (time.perf_counter_ns() - t0) / 1e6

        # Measure full engine.semantic_search on prose query
        t_e2e_0 = time.perf_counter_ns()
        res = await engine.semantic_search(
            query=PROSE_QUERY,
            namespace_id=str(TENANT_PERF_ID),
            agent_id="default",
            limit=5,
        )
        t_post_0 = time.perf_counter_ns()
        _ = json.dumps(res, default=str)
        t_post = (time.perf_counter_ns() - t_post_0) / 1e6
        t_e2e = (time.perf_counter_ns() - t_e2e_0) / 1e6
        ss_prose_totals.append(t_e2e)

        # Also measure sub-stages explicitly
        t0 = time.perf_counter_ns()
        vec = await _embeddings.embed(PROSE_QUERY)
        t_emb = (time.perf_counter_ns() - t0) / 1e6

        t0 = time.perf_counter_ns()
        async with scoped_pg_session(engine.pg_pool, TENANT_PERF_ID) as conn:
            t_guc = (time.perf_counter_ns() - t0) / 1e6
            t_sql0 = time.perf_counter_ns()
            rows = await conn.fetch(
                """
                SELECT m.id, m.payload_ref, m.wrapped_dek, m.metadata
                FROM memories m
                WHERE m.namespace_id = $1::uuid AND m.valid_to IS NULL
                ORDER BY m.embedding <=> $2::halfvec(768) ASC
                LIMIT 5
                """,
                TENANT_PERF_ID,
                json.dumps(vec),
            )
            t_sql = (time.perf_counter_ns() - t_sql0) / 1e6

        t0 = time.perf_counter_ns()
        refs = [normalize_payload_ref(r["payload_ref"]) for r in rows]
        raw_map = await fetch_episodes_raw_by_ref(
            engine.mongo_client.memory_archive, refs, decode_bytes=False
        )
        t_mongo = (time.perf_counter_ns() - t0) / 1e6

        t0 = time.perf_counter_ns()
        for r in rows:
            ref = normalize_payload_ref(r["payload_ref"])
            w = r["wrapped_dek"]
            maybe_decrypt_raw_data(raw_map.get(ref, ""), bytes(w) if w else None)
        t_dek = (time.perf_counter_ns() - t0) / 1e6

        ss_stages["t_redis_gov"].append(t_redis)
        ss_stages["t_embed_http"].append(t_emb)
        ss_stages["t_pg_checkout_guc"].append(t_guc)
        ss_stages["t_sql_exec"].append(t_sql)
        ss_stages["t_mongo_hydrate"].append(t_mongo)
        ss_stages["t_dek_decrypt"].append(t_dek)
        ss_stages["t_postprocess_serialize"].append(t_post)
        ss_stages["t_total"].append(t_e2e)

        # Identifier query
        t_id0 = time.perf_counter_ns()
        await engine.semantic_search(
            query=IDENT_QUERY,
            namespace_id=str(TENANT_PERF_ID),
            agent_id="default",
            limit=5,
        )
        ss_ident_totals.append((time.perf_counter_ns() - t_id0) / 1e6)

        # as_of query
        t_ao0 = time.perf_counter_ns()
        await engine.semantic_search(
            query=PROSE_QUERY,
            namespace_id=str(TENANT_PERF_ID),
            agent_id="default",
            limit=5,
            as_of=as_of_ts,
        )
        ss_asof_totals.append((time.perf_counter_ns() - t_ao0) / 1e6)

    # 2. graph_search (live + as_of)
    gs_live_totals: list[float] = []
    gs_asof_totals: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter_ns()
        await engine.graph_search(
            GraphSearchRequest(
                query="cloud_infrastructure",
                namespace_id=TENANT_PERF_ID,
                agent_id="default",
                max_depth=2,
            )
        )
        gs_live_totals.append((time.perf_counter_ns() - t0) / 1e6)

        t0 = time.perf_counter_ns()
        await engine.graph_search(
            GraphSearchRequest(
                query="cloud_infrastructure",
                namespace_id=TENANT_PERF_ID,
                agent_id="default",
                max_depth=2,
                as_of=as_of_ts,
            )
        )
        gs_asof_totals.append((time.perf_counter_ns() - t0) / 1e6)

    # 3. compile_working_memory
    cwm_totals: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter_ns()
        await compile_working_memory(
            engine,
            goal=PROSE_QUERY,
            namespace_id=TENANT_PERF_ID,
            agent_id="default",
            max_tokens=2000,
        )
        cwm_totals.append((time.perf_counter_ns() - t0) / 1e6)

    # 4. store_memory (15 warm calls to avoid bloating table count excessively)
    sm_totals: list[float] = []
    for i in range(15):
        t0 = time.perf_counter_ns()
        await engine.store_memory(
            StoreMemoryRequest(
                namespace_id=TENANT_PERF_ID,
                agent_id="default",
                content=f"Hotpath benchmark episodic note {i} for latency profiling.",
                summary=f"Hotpath benchmark episodic note {i}",
                heavy_payload=f"Hotpath benchmark episodic note {i} heavy payload.",
                memory_type=MemoryType.episodic,
                assertion_type=AssertionType.observation,
                check_contradictions=False,
                metadata={"bypass_quarantine": True, "bench": i},
            )
        )
        sm_totals.append((time.perf_counter_ns() - t0) / 1e6)

    return {
        "semantic_search_prose_ms": _stats(ss_prose_totals),
        "semantic_search_ident_ms": _stats(ss_ident_totals),
        "semantic_search_as_of_ms": _stats(ss_asof_totals),
        "semantic_search_substages_ms": {k: _stats(v) for k, v in ss_stages.items()},
        "graph_search_live_ms": _stats(gs_live_totals),
        "graph_search_as_of_ms": _stats(gs_asof_totals),
        "compile_working_memory_ms": _stats(cwm_totals),
        "store_memory_ms": _stats(sm_totals),
    }


async def measure_mongo_pause_failfast(engine: NCEEngine) -> dict[str, Any]:
    """Pause nce-mongo-local, time how fast store_memory fails over to state='recovery_needed', then unpause."""
    await asyncio.to_thread(
        subprocess.run,
        ["docker", "pause", "nce-mongo-local"],
        check=True,
        capture_output=True,
    )
    t0 = time.perf_counter()
    err_type = None
    try:
        await engine.store_memory(
            StoreMemoryRequest(
                namespace_id=TENANT_PERF_ID,
                agent_id="default",
                content="Mongo pause fail-fast probe",
                summary="Mongo pause fail-fast probe",
                heavy_payload="Mongo pause fail-fast probe",
                memory_type=MemoryType.episodic,
                assertion_type=AssertionType.observation,
                check_contradictions=False,
                metadata={"bypass_quarantine": True},
            )
        )
    except Exception as exc:
        err_type = type(exc).__name__
    failover_s = round(time.perf_counter() - t0, 3)
    await asyncio.to_thread(
        subprocess.run,
        ["docker", "unpause", "nce-mongo-local"],
        check=True,
        capture_output=True,
    )

    # Wait briefly for deferred cleanup or run _saga_recovery_tick
    await asyncio.sleep(0.5)
    from nce.cron import _saga_recovery_tick

    await _saga_recovery_tick(engine.pg_pool, mongo_client=engine.mongo_client)
    return {"mongo_pause_failover_s": failover_s, "exception": err_type}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["baseline", "optimized"], required=True)
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--skip-mongo-chaos", action="store_true")
    parser.add_argument("--out", type=str, default="")
    args = parser.parse_args()

    engine = NCEEngine()
    await engine.connect(run_startup_checks=False)
    try:
        from nce.master_key_registry import rls_visibility_problem

        async with engine.pg_pool.acquire() as conn:
            await rls_visibility_problem(conn)
        seed_info = await seed_100k_if_needed(engine.pg_pool, engine.mongo_client, engine)
        canary_info = await verify_zero_canary_leak(engine)
        plans = await capture_explain_plans(engine.pg_pool)
        latencies = await profile_substage_latencies(engine, iterations=args.iterations)
        chaos_info = {}
        if not args.skip_mongo_chaos:
            chaos_info = await measure_mongo_pause_failfast(engine)

        report = {
            "phase": args.phase,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "seed": seed_info,
            "canary": canary_info,
            "latencies": latencies,
            "mongo_chaos": chaos_info,
            "explain_plans": plans,
        }
        out_path = args.out or str(_REPO_ROOT / f".profile_{args.phase}.json")
        await asyncio.to_thread(
            Path(out_path).write_text, json.dumps(report, indent=2), encoding="utf-8"
        )
        print(json.dumps({k: v for k, v in report.items() if k != "explain_plans"}, indent=2))
        print("\n--- EXPLAIN SUMMARY ---")
        for k, v in plans.items():
            lines = [
                ln
                for ln in v.splitlines()
                if "Execution Time" in ln or "Scan" in ln or "InitPlan" in ln
            ]
            print(f"[{k}]")
            for ln in lines[:8]:
                print(f"  {ln}")
    finally:
        await engine.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
