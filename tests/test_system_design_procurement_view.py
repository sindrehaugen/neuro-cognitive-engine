"""
tests/test_system_design_procurement_view.py
============================================
Comprehensive test suite for Wave SD-6: ``system_design_procurement_view``
(domain core, MCP tool handler, REST route, ADR-0017 leak checks, and tenant isolation).
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.datastructures import QueryParams
from starlette.requests import Request

from nce.admin_handlers.system_design import (
    api_system_design_procurement_view,
)
from nce.vertical_modules.system_design.mcp_handlers import (
    handle_system_design_procurement_view,
)
from nce.vertical_modules.system_design.procurement_view import (
    _ADR0017_FORBIDDEN_KEYS,
    do_get_procurement_view,
)

# ---------------------------------------------------------------------------
# Helpers & Mocks
# ---------------------------------------------------------------------------


class _FakePoolContext:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    async def __aenter__(self) -> Any:
        return self.conn

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass


def _make_mock_engine(conn: Any) -> MagicMock:
    engine = MagicMock()
    engine.pg_pool = MagicMock()
    return engine


def _collect_all_keys(data: Any) -> set[str]:
    keys = set()
    if isinstance(data, dict):
        for k, v in data.items():
            keys.add(k)
            keys.update(_collect_all_keys(v))
    elif isinstance(data, list):
        for item in data:
            keys.update(_collect_all_keys(item))
    return keys


# ---------------------------------------------------------------------------
# 1. Validation & Missing Argument Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_procurement_view_missing_namespace():
    mock_engine = MagicMock()
    with pytest.raises(ValueError, match="'namespace_id' is required"):
        await do_get_procurement_view(mock_engine, {"design_id": "D-01"})


@pytest.mark.asyncio
async def test_procurement_view_missing_design_id():
    mock_engine = MagicMock()
    with pytest.raises(ValueError, match="'design_id' is required"):
        await do_get_procurement_view(mock_engine, {"namespace_id": str(uuid.uuid4())})


@pytest.mark.asyncio
async def test_procurement_view_design_not_found():
    ns_id = uuid.uuid4()
    mock_conn = AsyncMock()
    # meta_row query returns None
    mock_conn.fetchrow.return_value = None

    mock_engine = _make_mock_engine(mock_conn)

    with patch(
        "nce.vertical_modules.system_design.procurement_view.scoped_pg_session",
        return_value=_FakePoolContext(mock_conn),
    ):
        with pytest.raises(ValueError, match="DESIGN node not found"):
            await do_get_procurement_view(
                mock_engine, {"namespace_id": ns_id, "design_id": "NON-EXISTENT"}
            )


# ---------------------------------------------------------------------------
# 2. Frozen Design Verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_procurement_view_require_frozen_fails_when_unfrozen():
    ns_id = uuid.uuid4()
    mock_conn = AsyncMock()
    # 1st fetchrow: meta_row
    # 2nd fetchrow: quote_row (None -> no becomes edge)
    mock_conn.fetchrow.side_effect = [
        {"label": "DESIGN:D-01", "entity_type": "DESIGN", "updated_at": "2026-09-17T00:00:00Z"},
        None,
    ]
    mock_conn.fetchval.return_value = 0
    mock_conn.fetch.return_value = []

    mock_engine = _make_mock_engine(mock_conn)

    with patch(
        "nce.vertical_modules.system_design.procurement_view.scoped_pg_session",
        return_value=_FakePoolContext(mock_conn),
    ):
        with pytest.raises(ValueError, match="is not frozen"):
            await do_get_procurement_view(
                mock_engine,
                {"namespace_id": ns_id, "design_id": "D-01", "require_frozen": True},
            )


@pytest.mark.asyncio
async def test_procurement_view_frozen_via_becomes_edge():
    ns_id = uuid.uuid4()
    mock_conn = AsyncMock()
    mock_conn.fetchrow.side_effect = [
        {"label": "DESIGN:D-01", "entity_type": "DESIGN", "updated_at": "2026-09-17T00:00:00Z"},
        {"object_label": "QUOTE:Q-TEST-99"},
    ]
    mock_conn.fetchval.return_value = 5
    # fetch: bom_line_content rows, then DESIGN_LINE rows, then DEVICE rows, then candidates
    mock_conn.fetch.side_effect = [
        [
            {
                "bom_line_label": "BOM_LINE:Q-TEST-99:DL-01",
                "quote_id": "Q-TEST-99",
                "line_ref": "DL-01",
                "qty": 2,
                "unit_price": 250.0,
                "line_total": 500.0,
                "origin_ref": "DESIGN_LINE:D-01:DL-01",
                "priced": True,
            }
        ],
        [],  # DL rows
        [],  # DEV rows
        [],  # bid_rows
        [],  # price_rows
    ]

    mock_engine = _make_mock_engine(mock_conn)

    with patch(
        "nce.vertical_modules.system_design.procurement_view.scoped_pg_session",
        return_value=_FakePoolContext(mock_conn),
    ):
        res = await do_get_procurement_view(
            mock_engine,
            {"namespace_id": ns_id, "design_id": "D-01", "require_frozen": True},
        )

    assert res["is_frozen"] is True
    assert res["quote_id"] == "Q-TEST-99"
    assert res["quote_label"] == "QUOTE:Q-TEST-99"
    assert res["total_line_items"] == 1
    assert res["total_quantity"] == 2
    assert res["supplier_count"] >= 1


# ---------------------------------------------------------------------------
# 3. Multi-Item Ranking, Grouping, and PR-1 Payload Generation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_procurement_view_grouping_and_pr1_payload():
    ns_id = uuid.uuid4()
    mock_conn = AsyncMock()

    meta_row = {
        "label": "DESIGN:D-MULTI",
        "entity_type": "DESIGN",
        "updated_at": "2026-09-17T00:00:00Z",
    }
    quote_row = {"object_label": "QUOTE:Q-MULTI-01"}

    mock_conn.fetchrow.side_effect = [meta_row, quote_row]
    mock_conn.fetchval.return_value = 1

    # BOM lines: 2 lines
    bom_rows = [
        {
            "bom_line_label": "BOM_LINE:Q-MULTI-01:DL-01",
            "quote_id": "Q-MULTI-01",
            "line_ref": "DL-01",
            "qty": 4,
            "unit_price": 100.0,
            "line_total": 400.0,
            "origin_ref": "DESIGN_LINE:D-MULTI:DL-01",
            "priced": True,
        },
        {
            "bom_line_label": "BOM_LINE:Q-MULTI-01:DL-02",
            "quote_id": "Q-MULTI-01",
            "line_ref": "DL-02",
            "qty": 2,
            "unit_price": 500.0,
            "line_total": 1000.0,
            "origin_ref": "DESIGN_LINE:D-MULTI:DL-02",
            "priced": True,
        },
    ]

    dl_rows = [
        {"design_line_label": "DESIGN_LINE:D-MULTI:DL-01", "product_label": "PRODUCT:BIAMP:TESIRA"},
        {"design_line_label": "DESIGN_LINE:D-MULTI:DL-02", "product_label": "PRODUCT:SHURE:MXA910"},
    ]

    dev_rows = [
        {
            "device_label": "DEVICE:D-MULTI:DEV-01",
            "manufacturer": "Crestron",
            "model_number": "DM-NVX-350",
            "device_category": "video",
        }
    ]

    # Explicit candidates map to test multi-supplier routing
    candidates_override = {
        "DL-01": [
            {
                "supplier_id": "DISTRIBUTOR_A",
                "supplier_name": "Distributor Alpha",
                "unit_price": 95.0,
                "own_stock": True,
                "delivery_reliability": 0.95,
                "supplier_tier": 1,
            },
            {
                "supplier_id": "DISTRIBUTOR_B",
                "supplier_name": "Distributor Beta",
                "unit_price": 110.0,
                "own_stock": False,
                "delivery_reliability": 0.80,
                "supplier_tier": 3,
            },
        ],
        "DL-02": [
            {
                "supplier_id": "DISTRIBUTOR_B",
                "supplier_name": "Distributor Beta",
                "unit_price": 480.0,
                "own_stock": True,
                "delivery_reliability": 0.90,
                "supplier_tier": 1,
            },
        ],
        "DEVICE:D-MULTI:DEV-01": [
            {
                "supplier_id": "DISTRIBUTOR_A",
                "supplier_name": "Distributor Alpha",
                "unit_price": 1200.0,
                "own_stock": True,
                "delivery_reliability": 0.95,
                "supplier_tier": 1,
            },
        ],
    }

    mock_conn.fetch.side_effect = [
        bom_rows,
        dl_rows,
        dev_rows,
    ]

    mock_engine = _make_mock_engine(mock_conn)

    with patch(
        "nce.vertical_modules.system_design.procurement_view.scoped_pg_session",
        return_value=_FakePoolContext(mock_conn),
    ):
        res = await do_get_procurement_view(
            mock_engine,
            {
                "namespace_id": ns_id,
                "design_id": "D-MULTI",
                "candidates": candidates_override,
            },
        )

    assert res["supplier_count"] == 2
    assert "DISTRIBUTOR_A" in res["by_supplier"]
    assert "DISTRIBUTOR_B" in res["by_supplier"]

    grp_a = res["by_supplier"]["DISTRIBUTOR_A"]
    assert grp_a["suggested_po_number"] == "PO-D-MULTI-DISTRIBUTOR_A"
    assert len(grp_a["line_items"]) == 2  # DL-01 and DEV-01
    assert "pr1_payload" in grp_a
    payload_a = grp_a["pr1_payload"]
    assert payload_a["po_number"] == "PO-D-MULTI-DISTRIBUTOR_A"
    assert len(payload_a["line_items"]) == 2

    grp_b = res["by_supplier"]["DISTRIBUTOR_B"]
    assert grp_b["suggested_po_number"] == "PO-D-MULTI-DISTRIBUTOR_B"
    assert len(grp_b["line_items"]) == 1  # DL-02
    payload_b = grp_b["pr1_payload"]
    assert payload_b["po_number"] == "PO-D-MULTI-DISTRIBUTOR_B"
    assert len(payload_b["line_items"]) == 1


# ---------------------------------------------------------------------------
# 4. ADR-0017 Leak Invariant Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_procurement_view_adr0017_zero_financial_leaks():
    """Verify that forbidden ADR-0017 keys never appear in any response dictionary."""
    ns_id = uuid.uuid4()
    mock_conn = AsyncMock()

    meta_row = {
        "label": "DESIGN:D-LEAKTEST",
        "entity_type": "DESIGN",
        "updated_at": "2026-09-17T00:00:00Z",
    }
    quote_row = {"object_label": "QUOTE:Q-LEAK-01"}

    mock_conn.fetchrow.side_effect = [meta_row, quote_row]
    mock_conn.fetchval.return_value = 1

    # Candidate with internal keys that must be scrubbed
    candidates_with_internal = [
        {
            "supplier_id": "LEAKY_SUPPLIER",
            "unit_price": 100.0,
            "cost": 50.0,
            "cost_price": 50.0,
            "bid_id": "BID-12345",
            "bid_price": 50.0,
            "margin": 0.5,
            "unit_cost": 50.0,
            "own_stock": True,
            "delivery_reliability": 0.9,
            "supplier_tier": 1,
        }
    ]

    mock_conn.fetch.side_effect = [
        [
            {
                "bom_line_label": "BOM_LINE:Q-LEAK-01:L1",
                "quote_id": "Q-LEAK-01",
                "line_ref": "L1",
                "qty": 1,
                "unit_price": 100.0,
                "line_total": 100.0,
                "origin_ref": "",
                "priced": True,
            }
        ],
        [],
        [],
    ]

    mock_engine = _make_mock_engine(mock_conn)

    with patch(
        "nce.vertical_modules.system_design.procurement_view.scoped_pg_session",
        return_value=_FakePoolContext(mock_conn),
    ):
        res = await do_get_procurement_view(
            mock_engine,
            {
                "namespace_id": ns_id,
                "design_id": "D-LEAKTEST",
                "candidates": candidates_with_internal,
            },
        )

    all_keys = _collect_all_keys(res)
    leaks = all_keys.intersection(_ADR0017_FORBIDDEN_KEYS)
    assert not leaks, f"ADR-0017 leaks detected in procurement view: {leaks}"


# ---------------------------------------------------------------------------
# 5. MCP Handler Surface Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_handle_system_design_procurement_view():
    ns_id = str(uuid.uuid4())
    mock_engine = MagicMock()

    mock_result = {
        "namespace_id": ns_id,
        "design_id": "D-01",
        "is_frozen": True,
        "supplier_count": 1,
        "total_estimated_spend": 500.0,
        "suppliers": [],
    }

    with patch(
        "nce.vertical_modules.system_design.mcp_handlers.do_get_procurement_view",
        new_callable=AsyncMock,
        return_value=mock_result,
    ) as mock_core:
        args = {"namespace_id": ns_id, "design_id": "D-01"}
        raw_json = await handle_system_design_procurement_view(mock_engine, args)
        parsed = json.loads(raw_json)

        assert parsed["design_id"] == "D-01"
        assert parsed["is_frozen"] is True
        mock_core.assert_awaited_once_with(mock_engine, args)


# ---------------------------------------------------------------------------
# 6. REST Route Surface Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_api_system_design_procurement_view():
    ns_id = str(uuid.uuid4())
    mock_engine = MagicMock()

    mock_result = {
        "namespace_id": ns_id,
        "design_id": "D-01",
        "is_frozen": True,
        "supplier_count": 1,
        "total_estimated_spend": 250.0,
        "suppliers": [],
    }

    req = MagicMock(spec=Request)
    req.query_params = QueryParams(
        {
            "namespace_id": ns_id,
            "design_id": "D-01",
            "require_frozen": "true",
            "design_version": "2",
        }
    )

    with (
        patch("nce.admin_handlers.system_design.admin_state.engine", mock_engine),
        patch(
            "nce.admin_handlers.system_design.do_get_procurement_view",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_core,
    ):
        resp = await api_system_design_procurement_view(req)
        assert resp.status_code == 200
        body = json.loads(resp.body.decode())
        assert body["status"] == "ok"
        assert body["procurement_view"]["design_id"] == "D-01"
        assert body["procurement_view"]["is_frozen"] is True
        mock_core.assert_awaited_once_with(
            mock_engine,
            {
                "namespace_id": ns_id,
                "design_id": "D-01",
                "require_frozen": True,
                "design_version": 2,
            },
        )


# ---------------------------------------------------------------------------
# 7. Live PostgreSQL Integration Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestSystemDesignProcurementViewIntegration:
    """Live database integration test with real pg_pool, schema, and tables."""

    async def test_live_procurement_view_end_to_end(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        test_ns = await make_namespace()
        from nce.db_utils import scoped_pg_session
        from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
        from nce.vertical_modules.system_design.devices import do_author_device_topology
        from nce.vertical_modules.system_design.graph import do_author_functional_location
        from nce.vertical_modules.system_design.to_quote import do_design_to_quote

        design_id = f"DSGN-INT-{uuid.uuid4().hex[:6].upper()}"

        class FakeEngine:
            def __init__(self, p: Any) -> None:
                self.pg_pool = p

        engine = FakeEngine(pg_pool)

        # 1. Author DESIGN + DESIGN_LINE nodes
        async with scoped_pg_session(pg_pool, test_ns) as conn:
            await seed_node_ownership_registry(conn, test_ns)
            await do_author_functional_location(
                conn,
                test_ns,
                namespace_slug="procviewns",
                design_id=design_id,
                site_name="Site-Beta",
                buildings=[
                    {
                        "name": "Bldg-1",
                        "floors": [
                            {"name": "F1", "rooms": [{"name": "R101", "positions": ["Pos-1"]}]}
                        ],
                    }
                ],
                design_lines=[
                    {
                        "line_ref": "L-BIAMP",
                        "manufacturer": "Biamp",
                        "mfr_part_no": "Tesira-Server",
                        "confidence": 0.95,
                    },
                    {
                        "line_ref": "L-SHURE",
                        "manufacturer": "Shure",
                        "mfr_part_no": "MXA920",
                        "confidence": 0.90,
                    },
                ],
            )
            # Also author a device
            await do_author_device_topology(
                conn,
                test_ns,
                design_id=design_id,
                devices=[
                    {
                        "device_ref": "DEV-SW01",
                        "capability": {
                            "manufacturer": "Cisco",
                            "model_number": "CBS350-24P",
                            "device_category": "switch",
                        },
                        "ports": [{"port_ref": "G1"}],
                    }
                ],
            )

        # 2. Freeze the design via do_design_to_quote (mocking Sales A2A)
        with patch(
            "nce.vertical_modules.system_design.to_quote._propose_quote_to_sales",
            new_callable=AsyncMock,
            return_value={"accepted": True, "quote_id": design_id},
        ):
            freeze_res = await do_design_to_quote(
                engine,
                {"namespace_id": test_ns, "design_id": design_id},
            )
            assert freeze_res["bom_lines_written"] >= 2

        # 3. Call do_get_procurement_view
        view_res = await do_get_procurement_view(
            engine,
            {
                "namespace_id": test_ns,
                "design_id": design_id,
                "require_frozen": True,
            },
        )

        assert view_res["is_frozen"] is True
        assert view_res["design_id"] == design_id
        assert view_res["quote_id"] == design_id
        assert view_res["supplier_count"] >= 1
        assert view_res["total_line_items"] >= 3  # 2 design lines + 1 device
        assert view_res["total_estimated_spend"] >= 0.0

        # Check PR-1 payload
        for sup in view_res["suppliers"]:
            assert "pr1_payload" in sup
            p = sup["pr1_payload"]
            assert p["po_number"].startswith(f"PO-{design_id}-")
            assert len(p["line_items"]) >= 1
            assert len(p["candidates"]) >= 1

        # Assert zero ADR-0017 financial leaks
        all_keys = _collect_all_keys(view_res)
        leaks = all_keys.intersection(_ADR0017_FORBIDDEN_KEYS)
        assert not leaks, f"ADR-0017 leak in live DB test: {leaks}"

    async def test_live_procurement_view_tenant_isolation(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        from nce.db_utils import scoped_pg_session
        from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
        from nce.vertical_modules.system_design.devices import do_author_device_topology

        ns_a = await make_namespace()
        ns_b = await make_namespace()
        shared_design_id = f"DSGN-ISO-{uuid.uuid4().hex[:6].upper()}"

        class FakeEngine:
            def __init__(self, p: Any) -> None:
                self.pg_pool = p

        engine = FakeEngine(pg_pool)

        # Namespace A: 1 device
        async with scoped_pg_session(pg_pool, ns_a) as conn:
            await seed_node_ownership_registry(conn, ns_a)
            await conn.execute(
                "INSERT INTO kg_nodes (label, entity_type, namespace_id) VALUES ($1, 'DESIGN', $2)",
                f"DESIGN:{shared_design_id}",
                ns_a,
            )
            await do_author_device_topology(
                conn,
                ns_a,
                design_id=shared_design_id,
                devices=[
                    {
                        "device_ref": "DEV-TENANT-A",
                        "capability": {"manufacturer": "TenantAMfg", "model_number": "A100"},
                    }
                ],
            )

        # Namespace B: 2 devices
        async with scoped_pg_session(pg_pool, ns_b) as conn:
            await seed_node_ownership_registry(conn, ns_b)
            await conn.execute(
                "INSERT INTO kg_nodes (label, entity_type, namespace_id) VALUES ($1, 'DESIGN', $2)",
                f"DESIGN:{shared_design_id}",
                ns_b,
            )
            await do_author_device_topology(
                conn,
                ns_b,
                design_id=shared_design_id,
                devices=[
                    {
                        "device_ref": "DEV-TENANT-B1",
                        "capability": {"manufacturer": "TenantBMfg", "model_number": "B100"},
                    },
                    {
                        "device_ref": "DEV-TENANT-B2",
                        "capability": {"manufacturer": "TenantBMfg", "model_number": "B200"},
                    },
                ],
            )

        # Inspect Tenant A
        res_a = await do_get_procurement_view(
            engine,
            {"namespace_id": ns_a, "design_id": shared_design_id},
        )
        assert res_a["total_line_items"] == 1
        assert res_a["suppliers"][0]["supplier_id"] == "TenantAMfg"

        # Inspect Tenant B
        res_b = await do_get_procurement_view(
            engine,
            {"namespace_id": ns_b, "design_id": shared_design_id},
        )
        assert res_b["total_line_items"] == 2
        assert res_b["suppliers"][0]["supplier_id"] == "TenantBMfg"
