"""Integration tests for Sales DealRoom (Batch 089)."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import asyncpg  # type: ignore[import-untyped]
import pytest
from bson import ObjectId

from nce.auth import set_namespace_context
from nce.orchestrator import NCEEngine
from nce.vertical_modules.sales.dealroom import do_open_dealroom


async def _insert_sales_record(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: UUID,
    entity: str,
    source_id: str,
    name: str,
    source_json: dict[str, Any],
) -> None:
    """Helper to insert sales read model records directly for testing."""
    await conn.execute(
        """
        INSERT INTO sales_read_model
            (namespace_id, entity, source_id, name, source_json, manual, is_deleted, modifiedon, synced_at)
        VALUES
            ($1, $2, $3, $4, $5::jsonb, '{}'::jsonb, false, now(), now())
        ON CONFLICT (namespace_id, entity, source_id)
        DO UPDATE SET
            name = EXCLUDED.name,
            source_json = EXCLUDED.source_json,
            updated_at = now()
        """,
        str(namespace_id),
        entity,
        source_id,
        name,
        json.dumps(source_json),
    )


async def _insert_kg_node(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: UUID,
    label: str,
    entity_type: str,
    payload_ref: str | None = None,
) -> None:
    """Helper to insert knowledge graph nodes."""
    await conn.execute(
        """
        INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin, payload_ref)
        VALUES ($1, $2, $3::uuid, 'agent', $4)
        ON CONFLICT (label, namespace_id) DO NOTHING
        """,
        label,
        entity_type,
        str(namespace_id),
        payload_ref,
    )


def _make_dealroom_engine_stub(pg_pool: asyncpg.Pool) -> Any:  # type: ignore[type-arg]
    """Minimal engine stub exposing ``pg_pool`` and ``mongo_client`` (unused
    when seeded BOM_LINE nodes carry no ``payload_ref``)."""

    class _EngineStub:
        pass

    stub = _EngineStub()
    stub.pg_pool = pg_pool  # type: ignore[attr-defined]
    stub.mongo_client = None  # type: ignore[attr-defined]
    return stub


@pytest.mark.integration
@pytest.mark.asyncio
class TestSalesDealRoom:
    """Integration tests for Sales DealRoom do_open_dealroom function."""

    async def test_open_dealroom_prices_correctly_and_handles_toggles(
        self,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """Verify that do_open_dealroom fetches nodes, prices via C6, and handles options toggles."""
        ns = await make_namespace()
        quote_id = f"q-{uuid4().hex[:8]}"

        # 1. Setup NCEEngine and database connection
        engine = NCEEngine()
        await engine.connect()

        try:
            # Generate 2 MongoDB ObjectIDs for BOM lines
            oid1 = ObjectId()
            oid2 = ObjectId()

            # Insert payloads in MongoDB
            assert engine.mongo_client is not None
            mongo_db = engine.mongo_client.memory_archive
            await mongo_db.episodes.insert_many(
                [
                    {
                        "_id": oid1,
                        "manufacturer": "Sony",
                        "model": "VPL-XW5000ES",
                        "quantity": 1,
                        "dg_pct": 0.3,
                        "is_optional": False,
                        "toggled": True,
                        "product": {
                            "base_price": 50000.0,
                        },
                    },
                    {
                        "_id": oid2,
                        "manufacturer": "Chief",
                        "model": "Mount-X",
                        "quantity": 2,
                        "dg_pct": 0.4,
                        "is_optional": True,
                        "toggled": True,
                        "product": {
                            "base_price": 1000.0,
                        },
                    },
                ]
            )

            # Seed PostgreSQL
            async with pg_pool.acquire() as conn:
                async with conn.transaction():
                    await set_namespace_context(conn, ns)

                    # Seed quote in read model
                    await _insert_sales_record(
                        conn,
                        ns,
                        "quotes",
                        quote_id,
                        "Interactive Design Quote",
                        {
                            "quoteid": quote_id,
                            "name": "Interactive Design Quote",
                            "description": "AV DealRoom quote description",
                        },
                    )

                    # Seed BOM_LINE nodes in graph
                    await _insert_kg_node(
                        conn,
                        ns,
                        f"BOM_LINE:{quote_id.upper()}:LINE-1",
                        "BOM_LINE",
                        payload_ref=str(oid1),
                    )
                    await _insert_kg_node(
                        conn,
                        ns,
                        f"BOM_LINE:{quote_id.upper()}:LINE-2",
                        "BOM_LINE",
                        payload_ref=str(oid2),
                    )

            # 2. Call do_open_dealroom with all toggled on (default)
            res = await do_open_dealroom(
                engine,
                {
                    "namespace_id": str(ns),
                    "quote_id": quote_id,
                },
            )

            assert res["quote_id"] == quote_id
            assert res["name"] == "Interactive Design Quote"
            assert res["description"] == "AV DealRoom quote description"

            # Check lines
            lines = res["lines"]
            assert len(lines) == 2

            # Line 1: Sony Projector
            # base cost = 50000.0, dg_pct = 0.3
            # unit price = 50000.0 / (1 - 0.3) = 71428.5714...
            # total price = unit_price * 1 = 71428.5714...
            assert lines[0]["manufacturer"] == "Sony"
            assert lines[0]["model"] == "VPL-XW5000ES"
            assert lines[0]["quantity"] == 1
            assert abs(lines[0]["unit_price"] - (50000.0 / 0.7)) < 1e-2
            assert lines[0]["toggled"] is True

            # Line 2: Chief Mount
            # base cost = 1000.0, dg_pct = 0.4
            # unit price = 1000.0 / (1 - 0.4) = 1666.6666...
            # total price = unit_price * 2 = 3333.3333...
            assert lines[1]["manufacturer"] == "Chief"
            assert lines[1]["model"] == "Mount-X"
            assert lines[1]["quantity"] == 2
            assert abs(lines[1]["unit_price"] - (1000.0 / 0.6)) < 1e-2
            assert lines[1]["toggled"] is True

            # Total quote price: (50000 / 0.7) + 2 * (1000 / 0.6) = 71428.57 + 3333.33 = 74761.90
            expected_total = (50000.0 / 0.7) + (2000.0 / 0.6)
            assert abs(res["total_price_nok"] - expected_total) < 1e-2

            # 3. Toggle off Line 2 (Chief Mount) and verify recalculation
            res_toggled = await do_open_dealroom(
                engine,
                {
                    "namespace_id": str(ns),
                    "quote_id": quote_id,
                    "toggled_options": {
                        f"BOM_LINE:{quote_id.upper()}:LINE-2": False,
                    },
                },
            )

            # Recomputed price should only include Sony projector (Line 1)
            expected_toggled_total = 50000.0 / 0.7
            assert abs(res_toggled["total_price_nok"] - expected_toggled_total) < 1e-2
            assert res_toggled["lines"][1]["toggled"] is False

            # Verify toggled state is persisted in MongoDB
            doc2 = await mongo_db.episodes.find_one({"_id": oid2})
            assert doc2 is not None
            assert doc2["toggled"] is False

        finally:
            await engine.disconnect()


# ---------------------------------------------------------------------------
# Regression tests: the BOM_LINE fetch used to build a raw SQL LIKE pattern
# from a caller-supplied quote_id. `_` and `%` are LIKE metacharacters -- a
# quote id containing either would silently widen the match to a DIFFERENT
# quote's BOM lines, showing one customer another quote's line items on this
# customer-facing DealRoom surface (confirmed live:
# 'BOM_LINE:QA1:AMP01' LIKE 'BOM_LINE:Q_1:%' is true). Fixed via a literal
# starts_with() prefix test -- mirrors economy/cascade.py's
# _read_actual_cost_total (Batch 120).
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestSalesDealRoomBomLineLabelMatching:
    async def test_underscore_in_quote_id_does_not_leak_another_quotes_bom_lines(
        self,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """Reproduces the exact live scenario:
        'BOM_LINE:QA1:AMP01' LIKE 'BOM_LINE:Q_1:%' is true under raw LIKE
        because `_` matches any single character. Seeds two quotes whose ids
        differ only at the position an unescaped `_` would wildcard-match,
        and asserts the DealRoom for the underscore quote shows ONLY its own
        BOM line -- never the victim's."""
        ns = await make_namespace()
        suffix = uuid4().hex[:8]
        quote_with_underscore = f"QU_{suffix}"  # contains a literal '_'
        quote_collision_victim = f"QUZ{suffix}"  # same length, 'Z' where '_' falls
        label_own = f"BOM_LINE:{quote_with_underscore.upper()}:AMP01"
        label_victim = f"BOM_LINE:{quote_collision_victim.upper()}:AMP01"

        engine = _make_dealroom_engine_stub(pg_pool)

        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns)
                await _insert_kg_node(conn, ns, label_own, "BOM_LINE")
                await _insert_kg_node(conn, ns, label_victim, "BOM_LINE")

        result = await do_open_dealroom(
            engine,
            {"namespace_id": str(ns), "quote_id": quote_with_underscore},
        )

        labels = [line["label"] for line in result["lines"]]
        assert labels == [label_own], (
            f"quote {quote_with_underscore!r} leaked another quote's BOM line "
            f"via an unescaped LIKE '_' wildcard: {labels}"
        )

    async def test_percent_in_quote_id_does_not_leak_another_quotes_bom_lines(
        self,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """Same defect class as the underscore case, but for `%` (matches any
        sequence of characters, including zero)."""
        ns = await make_namespace()
        suffix = uuid4().hex[:8]
        quote_with_percent = f"QP%{suffix}"  # contains a literal '%'
        quote_collision_victim = f"QPZZZZ{suffix}"  # extra chars where '%' would match
        label_own = f"BOM_LINE:{quote_with_percent.upper()}:AMP01"
        label_victim = f"BOM_LINE:{quote_collision_victim.upper()}:AMP01"

        engine = _make_dealroom_engine_stub(pg_pool)

        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns)
                await _insert_kg_node(conn, ns, label_own, "BOM_LINE")
                await _insert_kg_node(conn, ns, label_victim, "BOM_LINE")

        result = await do_open_dealroom(
            engine,
            {"namespace_id": str(ns), "quote_id": quote_with_percent},
        )

        labels = [line["label"] for line in result["lines"]]
        assert labels == [label_own], (
            f"quote {quote_with_percent!r} leaked another quote's BOM line "
            f"via an unescaped LIKE '%' wildcard: {labels}"
        )

    async def test_ordinary_quote_id_still_fetches_its_own_lines_only(
        self,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """No wildcard characters at all -- the common case must be
        completely unaffected by the switch from LIKE to starts_with()."""
        ns = await make_namespace()
        quote_id = f"Q-ORD-{uuid4().hex[:8]}"
        other_quote_id = f"Q-OTHER-{uuid4().hex[:8]}"
        label_a = f"BOM_LINE:{quote_id.upper()}:AMP01"
        label_b = f"BOM_LINE:{quote_id.upper()}:CABLE01"
        label_other = f"BOM_LINE:{other_quote_id.upper()}:AMP01"

        engine = _make_dealroom_engine_stub(pg_pool)

        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns)
                await _insert_kg_node(conn, ns, label_a, "BOM_LINE")
                await _insert_kg_node(conn, ns, label_b, "BOM_LINE")
                await _insert_kg_node(conn, ns, label_other, "BOM_LINE")

        result = await do_open_dealroom(
            engine,
            {"namespace_id": str(ns), "quote_id": quote_id},
        )

        labels = sorted(line["label"] for line in result["lines"])
        assert labels == sorted([label_a, label_b])
