"""Integration tests for Sales graph upserts (Wave 6 — pipeline-nodes).

Validates:
  a. do_create_deal creates CUSTOMER, DEAL, QUOTE, and linking edges with confidence.
  b. do_create_deal also optionally handles LEAD and OPPORTUNITY nodes and edges.
  c. do_edit_deal updates DEAL node and updates edge confidence.
  d. RLS isolates data across namespaces.
  e. OwnershipError is raised for an unseeded namespace.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.entity_resolution.ownership import OwnershipError
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.sales.graph import (
    _customer_label,
    _deal_label,
    _lead_label,
    _opportunity_label,
    _quote_label,
    do_create_deal,
    do_edit_deal,
)

_MOCK_EMIT = "nce.vertical_modules.sales.graph.emit_graph_write"

_DEAL_ID = "DEAL-TEST-001"
_QUOTE_ID = "QUOTE-TEST-001"
_CUST_ID = "CUST-TEST-001"
_OPP_ID = "OPP-TEST-001"
_LEAD_ID = "LEAD-TEST-001"


async def _seed(conn: asyncpg.Connection, ns: Any) -> None:  # type: ignore[type-arg]
    """Seed ownership registry + set namespace GUC inside one transaction."""
    async with conn.transaction():
        await set_namespace_context(conn, ns)
        await seed_node_ownership_registry(conn, ns)


# ---------------------------------------------------------------------------
# Test suite
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestSalesGraphUpserts:
    """Integration tests for sales/graph.py Wave 6."""

    async def test_create_deal_minimum_nodes_and_edges(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """do_create_deal creates CUSTOMER, DEAL, and QUOTE nodes, plus the priced_as edge."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)

        with patch(_MOCK_EMIT, new_callable=AsyncMock):
            async with pg_app_conn.transaction():
                await set_namespace_context(pg_app_conn, ns)
                res = await do_create_deal(
                    pg_app_conn,
                    ns,
                    deal_id=_DEAL_ID,
                    customer_id=_CUST_ID,
                    quote_id=_QUOTE_ID,
                    confidence=0.85,
                    source_id="d365-deal-001",
                )
                assert res["ok"] is True

        # Verify nodes exist in kg_nodes
        expected_nodes = [
            (_customer_label(_CUST_ID), "CUSTOMER"),
            (_deal_label(_DEAL_ID), "DEAL"),
            (_quote_label(_QUOTE_ID), "QUOTE"),
        ]

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            for lbl, entity_type in expected_nodes:
                row = await pg_app_conn.fetchrow(
                    "SELECT entity_type, d365_source_id FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                    lbl,
                    ns,
                )
                assert row is not None, f"Node missing: {lbl}"
                assert row["entity_type"] == entity_type
                assert row["d365_source_id"] == "d365-deal-001"

            # Verify edges exist in kg_edges
            edge = await pg_app_conn.fetchrow(
                """
                SELECT confidence, change_origin, d365_source_id FROM kg_edges
                WHERE subject_label = $1 AND predicate = 'priced_as' AND object_label = $2 AND namespace_id = $3
                """,
                _deal_label(_DEAL_ID),
                _quote_label(_QUOTE_ID),
                ns,
            )
            assert edge is not None
            assert edge["confidence"] == 0.85
            assert edge["change_origin"] == "agent"
            assert edge["d365_source_id"] == "d365-deal-001"

    async def test_create_deal_with_lead_and_opp(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """do_create_deal creates LEAD/OPPORTUNITY and all linking edges when provided."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)

        with patch(_MOCK_EMIT, new_callable=AsyncMock):
            async with pg_app_conn.transaction():
                await set_namespace_context(pg_app_conn, ns)
                await do_create_deal(
                    pg_app_conn,
                    ns,
                    deal_id=_DEAL_ID,
                    customer_id=_CUST_ID,
                    quote_id=_QUOTE_ID,
                    opportunity_id=_OPP_ID,
                    lead_id=_LEAD_ID,
                    confidence=0.95,
                )

        expected_nodes = [
            (_customer_label(_CUST_ID), "CUSTOMER"),
            (_lead_label(_LEAD_ID), "LEAD"),
            (_opportunity_label(_OPP_ID), "OPPORTUNITY"),
            (_deal_label(_DEAL_ID), "DEAL"),
            (_quote_label(_QUOTE_ID), "QUOTE"),
        ]

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            for lbl, entity_type in expected_nodes:
                row = await pg_app_conn.fetchrow(
                    "SELECT entity_type FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                    lbl,
                    ns,
                )
                assert row is not None, f"Node missing: {lbl}"
                assert row["entity_type"] == entity_type

            # Check edges:
            # CUSTOMER -[has]-> LEAD
            has_edge = await pg_app_conn.fetchrow(
                "SELECT confidence FROM kg_edges WHERE subject_label = $1 AND predicate = 'has' AND object_label = $2 AND namespace_id = $3",
                _customer_label(_CUST_ID),
                _lead_label(_LEAD_ID),
                ns,
            )
            assert has_edge is not None
            assert has_edge["confidence"] == 0.95

            # LEAD -[qualifies_into]-> OPPORTUNITY
            qual_edge = await pg_app_conn.fetchrow(
                "SELECT confidence FROM kg_edges WHERE subject_label = $1 AND predicate = 'qualifies_into' AND object_label = $2 AND namespace_id = $3",
                _lead_label(_LEAD_ID),
                _opportunity_label(_OPP_ID),
                ns,
            )
            assert qual_edge is not None
            assert qual_edge["confidence"] == 0.95

            # OPPORTUNITY -[becomes]-> DEAL
            becomes_edge = await pg_app_conn.fetchrow(
                "SELECT confidence FROM kg_edges WHERE subject_label = $1 AND predicate = 'becomes' AND object_label = $2 AND namespace_id = $3",
                _opportunity_label(_OPP_ID),
                _deal_label(_DEAL_ID),
                ns,
            )
            assert becomes_edge is not None
            assert becomes_edge["confidence"] == 0.95

            # DEAL -[priced_as]-> QUOTE
            priced_edge = await pg_app_conn.fetchrow(
                "SELECT confidence FROM kg_edges WHERE subject_label = $1 AND predicate = 'priced_as' AND object_label = $2 AND namespace_id = $3",
                _deal_label(_DEAL_ID),
                _quote_label(_QUOTE_ID),
                ns,
            )
            assert priced_edge is not None
            assert priced_edge["confidence"] == 0.95

    async def test_edit_deal(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """do_edit_deal updates the DEAL node and alters edge confidence."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)

        with patch(_MOCK_EMIT, new_callable=AsyncMock):
            async with pg_app_conn.transaction():
                await set_namespace_context(pg_app_conn, ns)
                await do_create_deal(
                    pg_app_conn,
                    ns,
                    deal_id=_DEAL_ID,
                    customer_id=_CUST_ID,
                    quote_id=_QUOTE_ID,
                    confidence=0.5,
                )

                # Edit the deal confidence
                res = await do_edit_deal(
                    pg_app_conn,
                    ns,
                    deal_id=_DEAL_ID,
                    confidence=0.99,
                    source_id="d365-edited-001",
                )
                assert res["ok"] is True

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            row = await pg_app_conn.fetchrow(
                "SELECT d365_source_id FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                _deal_label(_DEAL_ID),
                ns,
            )
            assert row["d365_source_id"] == "d365-edited-001"

            edge = await pg_app_conn.fetchrow(
                "SELECT confidence, d365_source_id FROM kg_edges WHERE subject_label = $1 AND object_label = $2 AND namespace_id = $3",
                _deal_label(_DEAL_ID),
                _quote_label(_QUOTE_ID),
                ns,
            )
            assert edge["confidence"] == 0.99
            assert edge["d365_source_id"] == "d365-edited-001"

    async def test_rls_namespace_isolation(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """RLS isolates graph data between distinct namespaces."""
        ns_a = await make_namespace()  # type: ignore[operator]
        ns_b = await make_namespace()  # type: ignore[operator]

        await _seed(pg_app_conn, ns_a)
        await _seed(pg_app_conn, ns_b)

        with patch(_MOCK_EMIT, new_callable=AsyncMock):
            # Create in namespace A
            async with pg_app_conn.transaction():
                await set_namespace_context(pg_app_conn, ns_a)
                await do_create_deal(
                    pg_app_conn,
                    ns_a,
                    deal_id=_DEAL_ID,
                    customer_id=_CUST_ID,
                    quote_id=_QUOTE_ID,
                )

        # Retrieve in namespace B (should be empty/invisible)
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns_b)
            node = await pg_app_conn.fetchrow(
                "SELECT 1 FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                _deal_label(_DEAL_ID),
                ns_a,  # Explicitly query A's namespace but under B's context
            )
            assert node is None

            edge = await pg_app_conn.fetchrow(
                "SELECT 1 FROM kg_edges WHERE subject_label = $1 AND namespace_id = $2",
                _deal_label(_DEAL_ID),
                ns_a,
            )
            assert edge is None

    async def test_unseeded_namespace_ownership_error(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """do_create_deal raises OwnershipError if node ownership has not been seeded."""
        ns = await make_namespace()  # type: ignore[operator]
        # Skip seeding ownership registry, just set context GUC
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)

        with patch(_MOCK_EMIT, new_callable=AsyncMock):
            async with pg_app_conn.transaction():
                await set_namespace_context(pg_app_conn, ns)
                with pytest.raises(OwnershipError) as exc:
                    await do_create_deal(
                        pg_app_conn,
                        ns,
                        deal_id=_DEAL_ID,
                        customer_id=_CUST_ID,
                        quote_id=_QUOTE_ID,
                    )
                assert "no ownership row registered" in str(exc.value)
