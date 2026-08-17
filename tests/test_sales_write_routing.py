"""Integration tests for Sales write routing (Wave 7 — write-routing)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.orchestrator import NCEEngine
from nce.vertical_modules.sales.write_routing import do_create_deal, do_edit_deal


async def _seed(conn: asyncpg.Connection, ns: UUID) -> None:
    """Seed ownership registry + set namespace GUC inside one transaction."""
    async with conn.transaction():
        await set_namespace_context(conn, ns)
        await seed_node_ownership_registry(conn, ns)


async def _seed_mode(
    conn: asyncpg.Connection,
    *,
    namespace_id: UUID,
    engine: str,
    function: str,
    mode: str,
) -> None:
    """Insert a source_mode_config row."""
    await set_namespace_context(conn, namespace_id)
    await conn.execute(
        """
        INSERT INTO source_mode_config (namespace_id, engine, function, mode)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (namespace_id, engine, function)
        DO UPDATE SET mode = EXCLUDED.mode, updated_at = now()
        """,
        namespace_id,
        engine,
        function,
        mode,
    )


def _make_engine_stub(pg_pool: asyncpg.Pool) -> NCEEngine:
    class _EngineStub:
        pass

    stub = _EngineStub()
    stub.pg_pool = pg_pool  # type: ignore[attr-defined]
    stub.redis_client = None  # type: ignore[attr-defined]
    return stub  # type: ignore[return-value]


@pytest.mark.integration
@pytest.mark.asyncio
class TestSalesWriteRouting:
    """Integration tests for Sales write routing."""

    async def test_create_deal_d365_mode(
        self,
        pg_app_conn: asyncpg.Connection,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """d365 mode writes only to external system."""
        ns = await make_namespace()
        await _seed(pg_app_conn, ns)

        # Seed source_mode_config for create_deal -> d365
        async with pg_app_conn.transaction():
            await _seed_mode(
                pg_app_conn,
                namespace_id=ns,
                engine="sales",
                function="create_deal",
                mode="d365",
            )

        engine = _make_engine_stub(pg_pool)
        params = {
            "namespace_id": ns,
            "deal_id": "D365-DEAL-001",
            "customer_id": "D365-CUST-001",
            "quote_id": "D365-QUOTE-001",
            "source_id": "ext-d365-id-001",
        }

        res = await do_create_deal(engine, params)
        assert res["ok"] is True
        assert res["mode"] == "d365"
        assert res["native"] is None
        assert res["external"] is not None
        assert res["external"]["d365_id"] == "ext-d365-id-001"

        # Verify NCE graph does not contain the native deal
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            row = await pg_app_conn.fetchrow(
                "SELECT * FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                "DEAL:D365-DEAL-001",
                ns,
            )
            assert row is None

    async def test_create_deal_both_mode(
        self,
        pg_app_conn: asyncpg.Connection,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """both mode writes to both external system and native graph."""
        ns = await make_namespace()
        await _seed(pg_app_conn, ns)

        # Seed source_mode_config for create_deal -> both
        async with pg_app_conn.transaction():
            await _seed_mode(
                pg_app_conn,
                namespace_id=ns,
                engine="sales",
                function="create_deal",
                mode="both",
            )

        engine = _make_engine_stub(pg_pool)
        deal_id = "BOTH-DEAL-001"
        params = {
            "namespace_id": ns,
            "deal_id": deal_id,
            "customer_id": "BOTH-CUST-001",
            "quote_id": "BOTH-QUOTE-001",
            "source_id": "ext-both-id-001",
        }

        res = await do_create_deal(engine, params)
        assert res["ok"] is True
        assert res["mode"] == "both"
        assert res["native"] is not None
        assert res["native"]["ok"] is True
        assert res["external"] is not None
        assert res["external"]["d365_id"] == "ext-both-id-001"

        # Verify native NCE graph nodes exist
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            row = await pg_app_conn.fetchrow(
                "SELECT d365_source_id FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                f"DEAL:{deal_id.upper()}",
                ns,
            )
            assert row is not None
            assert row["d365_source_id"] == "ext-both-id-001"

    async def test_create_deal_nce_mode_with_prefixing(
        self,
        pg_app_conn: asyncpg.Connection,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """nce mode writes only to native graph and automatically prefixes IDs."""
        ns = await make_namespace()
        await _seed(pg_app_conn, ns)

        # Seed source_mode_config for create_deal -> nce
        async with pg_app_conn.transaction():
            await _seed_mode(
                pg_app_conn,
                namespace_id=ns,
                engine="sales",
                function="create_deal",
                mode="nce",
            )

        engine = _make_engine_stub(pg_pool)
        params = {
            "namespace_id": ns,
            "deal_id": "native-deal-001",
            "customer_id": "native-cust-001",
            "quote_id": "native-quote-001",
        }

        res = await do_create_deal(engine, params)
        assert res["ok"] is True
        assert res["mode"] == "nce"
        assert res["native"] is not None
        assert res["native"]["ok"] is True
        assert res["native"]["deal_id"] == "nce:native-deal-001"
        assert res["external"] is None

        # Verify nodes are created with nce: prefix
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            row = await pg_app_conn.fetchrow(
                "SELECT * FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                "DEAL:NCE:NATIVE-DEAL-001",
                ns,
            )
            assert row is not None

    async def test_edit_deal_d365_only_no_mapping_required(
        self,
        pg_app_conn: asyncpg.Connection,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """In d365 mode, editing a deal does not require a native mapping/existence check."""
        ns = await make_namespace()
        await _seed(pg_app_conn, ns)

        # Seed source_mode_config for edit_deal -> d365
        async with pg_app_conn.transaction():
            await _seed_mode(
                pg_app_conn,
                namespace_id=ns,
                engine="sales",
                function="edit_deal",
                mode="d365",
            )

        engine = _make_engine_stub(pg_pool)
        # DEAL-D365-EDIT does not exist natively in NCE
        params = {
            "namespace_id": ns,
            "deal_id": "DEAL-D365-EDIT",
            "confidence": 0.9,
        }

        res = await do_edit_deal(engine, params)
        assert res["ok"] is True
        assert res["mode"] == "d365"
        assert res["native"] is None
        assert res["external"] is not None

    async def test_edit_deal_nce_mode_native_record(
        self,
        pg_app_conn: asyncpg.Connection,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """In nce mode, a native record (with nce: prefix) can be edited directly."""
        ns = await make_namespace()
        await _seed(pg_app_conn, ns)

        # 1. Create a native deal first (seeded via NCE mode)
        async with pg_app_conn.transaction():
            await _seed_mode(
                pg_app_conn,
                namespace_id=ns,
                engine="sales",
                function="create_deal",
                mode="nce",
            )
            await _seed_mode(
                pg_app_conn,
                namespace_id=ns,
                engine="sales",
                function="edit_deal",
                mode="nce",
            )

        engine = _make_engine_stub(pg_pool)
        create_res = await do_create_deal(
            engine,
            {
                "namespace_id": ns,
                "deal_id": "nce:native-edit-001",
                "customer_id": "nce:native-cust-001",
                "quote_id": "nce:native-quote-001",
            },
        )
        assert create_res["native"]["ok"] is True

        # 2. Edit the deal natively
        params = {
            "namespace_id": ns,
            "deal_id": "nce:native-edit-001",
            "confidence": 0.99,
        }
        res = await do_edit_deal(engine, params)
        assert res["ok"] is True
        assert res["mode"] == "nce"
        assert res["native"]["ok"] is True
        assert res["external"] is None

        # Verify edge confidence is updated in NCE graph
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            edge = await pg_app_conn.fetchrow(
                """
                SELECT confidence FROM kg_edges
                WHERE subject_label = 'DEAL:NCE:NATIVE-EDIT-001'
                  AND predicate = 'priced_as'
                  AND namespace_id = $1
                """,
                ns,
            )
            assert edge is not None
            assert edge["confidence"] == 0.99

    async def test_edit_deal_d365_record_fails_without_mapping(
        self,
        pg_app_conn: asyncpg.Connection,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """In nce or both mode, editing a D365 record natively raises ValueError if no mapping exists."""
        ns = await make_namespace()
        await _seed(pg_app_conn, ns)

        # Seed source_mode_config for edit_deal -> nce
        async with pg_app_conn.transaction():
            await _seed_mode(
                pg_app_conn,
                namespace_id=ns,
                engine="sales",
                function="edit_deal",
                mode="nce",
            )

        engine = _make_engine_stub(pg_pool)
        # "d365-unmapped-deal" has no prefix, implying it's a D365 ID.
        # It does not exist in NCE graph, so native edit must be prevented.
        params = {
            "namespace_id": ns,
            "deal_id": "d365-unmapped-deal",
            "confidence": 0.8,
        }

        with pytest.raises(ValueError) as excinfo:
            await do_edit_deal(engine, params)
        assert "no mapping exists" in str(excinfo.value)

    async def test_edit_deal_d365_record_succeeds_with_mapping(
        self,
        pg_app_conn: asyncpg.Connection,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """In nce or both mode, editing a D365 record natively succeeds if a mapping exists in NCE graph."""
        ns = await make_namespace()
        await _seed(pg_app_conn, ns)

        # 1. Seed the config modes
        async with pg_app_conn.transaction():
            await _seed_mode(
                pg_app_conn,
                namespace_id=ns,
                engine="sales",
                function="create_deal",
                mode="both",
            )
            await _seed_mode(
                pg_app_conn,
                namespace_id=ns,
                engine="sales",
                function="edit_deal",
                mode="both",
            )

        engine = _make_engine_stub(pg_pool)

        # 2. Write-through to NCE to establish a mapping/record
        deal_id = "mapped-d365-deal"
        create_res = await do_create_deal(
            engine,
            {
                "namespace_id": ns,
                "deal_id": deal_id,
                "customer_id": "cust-001",
                "quote_id": "quote-001",
                "source_id": "d365-guid-mapped-001",
            },
        )
        assert create_res["native"]["ok"] is True

        # 3. Edit the deal natively, which should succeed because a mapping (the deal node) exists
        params = {
            "namespace_id": ns,
            "deal_id": deal_id,
            "confidence": 0.95,
            "source_id": "d365-guid-mapped-001",
        }
        res = await do_edit_deal(engine, params)
        assert res["ok"] is True
        assert res["mode"] == "both"
        assert res["native"]["ok"] is True
        assert res["external"] is not None

        # Verify update has taken place in native graph
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            edge = await pg_app_conn.fetchrow(
                """
                SELECT confidence FROM kg_edges
                WHERE subject_label = $1
                  AND predicate = 'priced_as'
                  AND namespace_id = $2
                """,
                f"DEAL:{deal_id.upper()}",
                ns,
            )
            assert edge is not None
            assert edge["confidence"] == 0.95
