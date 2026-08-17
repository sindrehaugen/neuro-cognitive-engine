"""
tests/unit/test_project_case_study.py
======================================
Integration tests for project/case_study.py — Wave 12 (case-study-edge).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.project.case_study import do_generate_case_study_edge

# Patch target for emit_graph_write so integration tests don't need outbox running
_MOCK_EMIT = "nce.vertical_modules.project.case_study.emit_graph_write"


def _make_engine_stub(pg_pool: asyncpg.Pool) -> Any:  # type: ignore[type-arg]
    class _EngineStub:
        pass

    stub = _EngineStub()
    stub.pg_pool = pg_pool  # type: ignore[attr-defined]
    return stub


async def _seed(conn: asyncpg.Connection, ns: Any) -> None:  # type: ignore[type-arg]
    async with conn.transaction():
        await set_namespace_context(conn, ns)
        await seed_node_ownership_registry(conn, ns)


async def _seed_project_at_phase(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns: Any,
    project_lbl: str,
    phase: str,
) -> None:
    gate_lbl = f"GATE:{project_lbl.split(':')[-1]}:{phase}"

    async with conn.transaction():
        await set_namespace_context(conn, ns)
        # PROJECT_PROJECT node
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id)
            VALUES ($1, 'PROJECT_PROJECT', $2::uuid)
            ON CONFLICT (label, namespace_id) DO NOTHING
            """,
            project_lbl,
            ns,
        )
        # PROJECT_GATE node
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id)
            VALUES ($1, 'PROJECT_GATE', $2::uuid)
            ON CONFLICT (label, namespace_id) DO NOTHING
            """,
            gate_lbl,
            ns,
        )
        # PROJECT -[in_phase]-> GATE edge
        await conn.execute(
            """
            INSERT INTO kg_edges
                (subject_label, predicate, object_label, confidence, namespace_id)
            VALUES ($1, 'in_phase', $2, 1.0, $3::uuid)
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
            """,
            project_lbl,
            gate_lbl,
            ns,
        )


@pytest.mark.integration
@pytest.mark.asyncio
class TestProjectCaseStudyEdge:
    """Integration test suite for case-study node and edge generation."""

    async def test_terminal_state_project_yields_case_study_and_edge(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """Project in G6 handover state seeds a CASE_STUDY node and generates edge."""
        ns = await make_namespace()
        await _seed(pg_app_conn, ns)
        engine = _make_engine_stub(pg_pool)

        project_id = "PROJECT:Q_TERMINAL_001"
        await _seed_project_at_phase(pg_app_conn, ns, project_id, "G6")

        with patch(_MOCK_EMIT, new_callable=AsyncMock) as mock_emit:
            res = await do_generate_case_study_edge(
                engine,
                {
                    "namespace_id": ns,
                    "project_id": project_id,
                    "confidence": 0.85,
                },
            )

        assert res["ok"] is True
        assert res["case_study_label"] == "CASE_STUDY:Q_TERMINAL_001"
        assert mock_emit.call_count == 1

        # Verify CASE_STUDY node exists in kg_nodes
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            node_exists = await pg_app_conn.fetchval(
                """
                SELECT 1 FROM kg_nodes
                WHERE label = 'CASE_STUDY:Q_TERMINAL_001'
                  AND entity_type = 'PROJECT_CASE_STUDY'
                  AND namespace_id = $1::uuid
                """,
                ns,
            )
            assert node_exists == 1

            # Verify edge exists in kg_edges
            edge_row = await pg_app_conn.fetchrow(
                """
                SELECT confidence FROM kg_edges
                WHERE subject_label = $1
                  AND predicate = 'generates'
                  AND object_label = 'CASE_STUDY:Q_TERMINAL_001'
                  AND namespace_id = $2::uuid
                """,
                project_id,
                ns,
            )
            assert edge_row is not None
            assert float(edge_row["confidence"]) == 0.85

    async def test_in_flight_project_yields_none(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """In-flight project (e.g. G0) yields no case study node or edge."""
        ns = await make_namespace()
        await _seed(pg_app_conn, ns)
        engine = _make_engine_stub(pg_pool)

        project_id = "PROJECT:Q_INFLIGHT_002"
        await _seed_project_at_phase(pg_app_conn, ns, project_id, "G0")

        with patch(_MOCK_EMIT, new_callable=AsyncMock) as mock_emit:
            res = await do_generate_case_study_edge(
                engine,
                {
                    "namespace_id": ns,
                    "project_id": project_id,
                },
            )

        assert res["ok"] is False
        assert res["reason"] == "in_flight"
        assert mock_emit.call_count == 0

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            # Verify no case study node exists
            node_exists = await pg_app_conn.fetchval(
                """
                SELECT 1 FROM kg_nodes
                WHERE label = 'CASE_STUDY:Q_INFLIGHT_002'
                  AND namespace_id = $1::uuid
                """,
                ns,
            )
            assert node_exists is None

            # Verify no generates edge exists
            edge_exists = await pg_app_conn.fetchval(
                """
                SELECT 1 FROM kg_edges
                WHERE subject_label = $1
                  AND predicate = 'generates'
                  AND namespace_id = $2::uuid
                """,
                project_id,
                ns,
            )
            assert edge_exists is None

    async def test_missing_project_returns_error(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """Calling for non-existent project returns error dict."""
        ns = await make_namespace()
        await _seed(pg_app_conn, ns)
        engine = _make_engine_stub(pg_pool)

        res = await do_generate_case_study_edge(
            engine,
            {
                "namespace_id": ns,
                "project_id": "PROJECT:NON_EXISTENT",
            },
        )
        assert res["ok"] is False
        assert "not found" in res["error"]

    async def test_idempotent_case_study_generation(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """Running multiple times updates existing nodes/edges without duplicates."""
        ns = await make_namespace()
        await _seed(pg_app_conn, ns)
        engine = _make_engine_stub(pg_pool)

        project_id = "PROJECT:Q_IDEMP_003"
        await _seed_project_at_phase(pg_app_conn, ns, project_id, "G6")

        with patch(_MOCK_EMIT, new_callable=AsyncMock) as mock_emit:
            res1 = await do_generate_case_study_edge(
                engine,
                {
                    "namespace_id": ns,
                    "project_id": project_id,
                    "confidence": 0.9,
                },
            )
            assert res1["ok"] is True

            res2 = await do_generate_case_study_edge(
                engine,
                {
                    "namespace_id": ns,
                    "project_id": project_id,
                    "confidence": 0.95,
                },
            )
            assert res2["ok"] is True

        assert mock_emit.call_count == 2

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)

            # Node count should be 1
            node_count = await pg_app_conn.fetchval(
                """
                SELECT count(*) FROM kg_nodes
                WHERE label = 'CASE_STUDY:Q_IDEMP_003'
                  AND namespace_id = $1::uuid
                """,
                ns,
            )
            assert node_count == 1

            # Edge count should be 1, with updated confidence
            edges = await pg_app_conn.fetch(
                """
                SELECT confidence FROM kg_edges
                WHERE subject_label = $1
                  AND predicate = 'generates'
                  AND object_label = 'CASE_STUDY:Q_IDEMP_003'
                  AND namespace_id = $2::uuid
                """,
                project_id,
                ns,
            )
            assert len(edges) == 1
            assert float(edges[0]["confidence"]) == 0.95
