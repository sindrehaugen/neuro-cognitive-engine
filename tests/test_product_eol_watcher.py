"""
tests/test_product_eol_watcher.py
==================================
Integration tests for the Product EOL/EOS Watcher (Module 2 Wave 12).

Assertions:
  1. ``do_check_eol`` writes a ``replaced_by`` edge with confidence given
     a seeded EOL entry (config-list path).
  2. The PRODUCT payload (``product_catalog`` row) is NOT mutated after
     the watcher runs (Watcher discipline).
  3. The ``product_eol_watcher`` job is registered in the APScheduler
     when ``async_main`` boots.
  4. A seeded ``failure_pattern`` edge surfaces in the Advisor output
     returned by ``do_check_eol``.

All tests are ``@pytest.mark.integration`` (live Postgres required).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from nce.auth import set_namespace_context
from nce.cron import async_main
from nce.db_utils import scoped_pg_session
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.product.graph import upsert_product_node
from nce.vertical_modules.product.watchers import do_check_eol

# ---------------------------------------------------------------------------
# Minimal engine stub (satisfies ``engine.pg_pool``)
# ---------------------------------------------------------------------------


class _EngineStub:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pg_pool = pool


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def ns(make_namespace: Any) -> uuid.UUID:
    """Fresh namespace for EOL watcher tests."""
    return await make_namespace()


@pytest_asyncio.fixture
async def seeded_nodes(
    pg_pool: asyncpg.Pool,
    pg_app_conn: asyncpg.Connection,
    ns: uuid.UUID,
) -> AsyncGenerator[dict[str, str], None]:
    """Insert ownership seed, two PRODUCT_SKU nodes and a product_catalog row.

    Yields a dict with ``subject_label``, ``object_label``, ``manufacturer``,
    ``mfr_part_no``, ``succ_mfr_part_no``.

    Mocks ``emit_graph_write`` to isolate the write path from outbox infra.
    """
    manufacturer = "TESTMFR"
    mfr_part_no = f"SKU-EOL-{uuid.uuid4().hex[:6].upper()}"
    succ_part_no = f"SKU-SUCC-{uuid.uuid4().hex[:6].upper()}"

    # Seed the node-ownership registry so assert_owner passes for 'product'
    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns)
        await seed_node_ownership_registry(pg_app_conn, ns)

    with patch(
        "nce.vertical_modules.product.graph.emit_graph_write",
        new_callable=AsyncMock,
    ):
        async with scoped_pg_session(pg_pool, ns) as conn:
            # Insert subject node
            await upsert_product_node(
                conn,
                ns,
                manufacturer=manufacturer,
                mfr_part_no=mfr_part_no,
            )
            # Insert successor node
            await upsert_product_node(
                conn,
                ns,
                manufacturer=manufacturer,
                mfr_part_no=succ_part_no,
            )
            # Insert a product_catalog row for the subject (minimal required fields)
            await conn.execute(
                """
                INSERT INTO product_catalog
                    (namespace_id, manufacturer, mfr_part_no, product_source_id,
                     lifecycle_status)
                VALUES ($1::uuid, $2, $3, $4, $5)
                ON CONFLICT (namespace_id, manufacturer, mfr_part_no) DO NOTHING
                """,
                str(ns),
                manufacturer,
                mfr_part_no,
                f"test-source-{mfr_part_no}",
                "active",  # We do NOT change this — watcher must not mutate it
            )

    subject_label = f"PRODUCT:{manufacturer.upper()}:{mfr_part_no.upper()}"
    object_label = f"PRODUCT:{manufacturer.upper()}:{succ_part_no.upper()}"

    yield {
        "subject_label": subject_label,
        "object_label": object_label,
        "manufacturer": manufacturer,
        "mfr_part_no": mfr_part_no,
        "succ_mfr_part_no": succ_part_no,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_eol_list_json(
    manufacturer: str,
    mfr_part_no: str,
    succ_manufacturer: str,
    succ_part_no: str,
    confidence: float = 0.95,
) -> str:
    return json.dumps(
        [
            {
                "manufacturer": manufacturer,
                "mfr_part_no": mfr_part_no,
                "successor_manufacturer": succ_manufacturer,
                "successor_mfr_part_no": succ_part_no,
                "confidence": confidence,
            }
        ]
    )


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestProductEolWatcher:
    """Integration test suite for the Product EOL Watcher."""

    async def test_replaced_by_edge_written_with_confidence(
        self,
        pg_pool: asyncpg.Pool,
        pg_admin_conn: asyncpg.Connection,
        ns: uuid.UUID,
        seeded_nodes: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """do_check_eol writes a replaced_by edge with the configured confidence."""
        confidence = 0.95
        eol_list = _make_eol_list_json(
            seeded_nodes["manufacturer"],
            seeded_nodes["mfr_part_no"],
            seeded_nodes["manufacturer"],
            seeded_nodes["succ_mfr_part_no"],
            confidence,
        )
        monkeypatch.setenv("NCE_PRODUCT_EOL_LIST", eol_list)

        engine = _EngineStub(pg_pool)
        result = await do_check_eol(engine, {"namespace_id": str(ns)})

        assert result["edges_written"] == 1, f"Expected 1 edge written, got {result}"
        assert result["skipped"] == 0

        # Verify edge exists in kg_edges with correct confidence
        await set_namespace_context(pg_admin_conn, ns)
        row = await pg_admin_conn.fetchrow(
            """
            SELECT predicate, confidence
            FROM kg_edges
            WHERE subject_label = $1
              AND predicate = 'replaced_by'
              AND object_label = $2
              AND namespace_id = $3::uuid
            """,
            seeded_nodes["subject_label"],
            seeded_nodes["object_label"],
            str(ns),
        )
        assert row is not None, "replaced_by edge not found in kg_edges"
        assert row["predicate"] == "replaced_by"
        assert abs(float(row["confidence"]) - confidence) < 1e-9

    async def test_product_payload_not_mutated(
        self,
        pg_pool: asyncpg.Pool,
        pg_admin_conn: asyncpg.Connection,
        ns: uuid.UUID,
        seeded_nodes: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Watcher discipline: product_catalog row is unchanged after do_check_eol."""
        eol_list = _make_eol_list_json(
            seeded_nodes["manufacturer"],
            seeded_nodes["mfr_part_no"],
            seeded_nodes["manufacturer"],
            seeded_nodes["succ_mfr_part_no"],
        )
        monkeypatch.setenv("NCE_PRODUCT_EOL_LIST", eol_list)

        # Capture original payload before watcher runs
        await set_namespace_context(pg_admin_conn, ns)
        before = await pg_admin_conn.fetchrow(
            """
            SELECT lifecycle_status, product_source_id, is_deleted
            FROM product_catalog
            WHERE manufacturer = $1
              AND mfr_part_no = $2
              AND namespace_id = $3::uuid
            """,
            seeded_nodes["manufacturer"],
            seeded_nodes["mfr_part_no"],
            str(ns),
        )
        assert before is not None, "Subject product_catalog row not found before watcher run"

        engine = _EngineStub(pg_pool)
        await do_check_eol(engine, {"namespace_id": str(ns)})

        # Verify unchanged — watcher must not touch any product_catalog column
        after = await pg_admin_conn.fetchrow(
            """
            SELECT lifecycle_status, product_source_id, is_deleted
            FROM product_catalog
            WHERE manufacturer = $1
              AND mfr_part_no = $2
              AND namespace_id = $3::uuid
            """,
            seeded_nodes["manufacturer"],
            seeded_nodes["mfr_part_no"],
            str(ns),
        )
        assert after is not None, "Subject product_catalog row disappeared after watcher run"
        assert after["lifecycle_status"] == before["lifecycle_status"], (
            "Watcher mutated lifecycle_status — Watcher discipline violated"
        )
        assert after["product_source_id"] == before["product_source_id"], (
            "Watcher mutated product_source_id — Watcher discipline violated"
        )
        assert after["is_deleted"] == before["is_deleted"], (
            "Watcher mutated is_deleted — Watcher discipline violated"
        )

    async def test_no_eol_signal_is_noop(
        self,
        pg_pool: asyncpg.Pool,
        ns: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When no EOL signal is present, do_check_eol returns 0 edges written."""
        monkeypatch.delenv("NCE_PRODUCT_EOL_LIST", raising=False)

        engine = _EngineStub(pg_pool)
        result = await do_check_eol(engine, {"namespace_id": str(ns)})

        assert result["edges_written"] == 0

    async def test_failure_pattern_edge_surfaces_in_advisor_output(
        self,
        pg_pool: asyncpg.Pool,
        pg_admin_conn: asyncpg.Connection,
        ns: uuid.UUID,
        seeded_nodes: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A seeded failure_pattern edge appears in the do_check_eol result."""
        # Seed a failure_pattern edge on the subject product (read-only from watcher's perspective)
        await set_namespace_context(pg_admin_conn, ns)
        failure_label = f"FAILURE_REPORT:{uuid.uuid4().hex[:8].upper()}"
        await pg_admin_conn.execute(
            """
            INSERT INTO kg_edges
                (subject_label, predicate, object_label, confidence, namespace_id)
            VALUES ($1, 'failure_pattern', $2, $3, $4::uuid)
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
            """,
            failure_label,
            seeded_nodes["subject_label"],
            0.8,
            str(ns),
        )

        eol_list = _make_eol_list_json(
            seeded_nodes["manufacturer"],
            seeded_nodes["mfr_part_no"],
            seeded_nodes["manufacturer"],
            seeded_nodes["succ_mfr_part_no"],
        )
        monkeypatch.setenv("NCE_PRODUCT_EOL_LIST", eol_list)

        engine = _EngineStub(pg_pool)
        result = await do_check_eol(engine, {"namespace_id": str(ns)})

        assert result["edges_written"] == 1
        fp_list = result["failure_patterns"]
        assert len(fp_list) >= 1, "Expected at least one failure_pattern edge in Advisor output"
        predicates = {fp["predicate"] for fp in fp_list}
        assert "failure_pattern" in predicates

    async def test_missing_successor_node_is_skipped(
        self,
        pg_pool: asyncpg.Pool,
        ns: uuid.UUID,
        seeded_nodes: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the successor PRODUCT_SKU node is absent, the entry is skipped (no error)."""
        # Use a successor part number that has no node in kg_nodes
        missing_succ = f"MISSING-{uuid.uuid4().hex[:8].upper()}"
        eol_list = _make_eol_list_json(
            seeded_nodes["manufacturer"],
            seeded_nodes["mfr_part_no"],
            seeded_nodes["manufacturer"],
            missing_succ,
        )
        monkeypatch.setenv("NCE_PRODUCT_EOL_LIST", eol_list)

        engine = _EngineStub(pg_pool)
        result = await do_check_eol(engine, {"namespace_id": str(ns)})

        assert result["edges_written"] == 0
        assert result["skipped"] == 1


# ---------------------------------------------------------------------------
# Unit test: cron job registration
# ---------------------------------------------------------------------------


class StopMain(Exception):
    pass


@pytest.mark.asyncio
async def test_cron_boot_registers_product_eol_watcher() -> None:
    """async_main registers product_eol_watcher with the APScheduler."""
    mock_pool = MagicMock()
    mock_pool.close = AsyncMock()

    with (
        patch("asyncpg.create_pool", new_callable=AsyncMock, return_value=mock_pool),
        patch("asyncio.Event.wait", side_effect=StopMain),
        patch("nce.cron._renewal_tick", new_callable=AsyncMock),
        patch("nce.cron._reembedding_tick", new_callable=AsyncMock),
        patch("nce.cron._consolidation_tick", new_callable=AsyncMock),
        patch("nce.cron._partition_maintenance_tick", new_callable=AsyncMock),
        patch("nce.cron._saga_recovery_tick", new_callable=AsyncMock),
        patch("nce.cron._outbox_relay_tick", new_callable=AsyncMock),
        patch("nce.cron._decay_prune_tick", new_callable=AsyncMock),
        patch("nce.cron._product_eol_watcher_tick", new_callable=AsyncMock),
        patch("nce.cron._chain_verification_tick", new_callable=AsyncMock),
        patch("nce.cron._d365_sync_tick", new_callable=AsyncMock),
        patch("nce.cron._d365_netbox_bridge_tick", new_callable=AsyncMock),
    ):
        added_job_ids: list[str] = []

        def _add_job(func: Any, trigger: Any, *args: Any, **kwargs: Any) -> None:
            added_job_ids.append(kwargs.get("id", ""))

        with patch("nce.cron.AsyncIOScheduler") as mock_scheduler_cls:
            mock_scheduler = MagicMock()
            mock_scheduler.add_job = _add_job
            mock_scheduler_cls.return_value = mock_scheduler

            try:
                await async_main()
            except StopMain:
                pass

    assert "product_eol_watcher" in added_job_ids, (
        f"product_eol_watcher not found in registered job ids: {added_job_ids}"
    )
