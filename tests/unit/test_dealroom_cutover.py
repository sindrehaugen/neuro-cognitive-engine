"""
tests/unit/test_dealroom_cutover.py
===================================
Unit tests for Sales DealRoom PostgreSQL cutover (Wave S-3).

Verifies:
  1. Literal prefix starts_with prevents wildcard leaks in quote_id.
  2. Absent catalog product yields manufacturer=None, model=None (never "Unknown").
  3. Unpriced lines report priced=False, unit_price=None, total_price=None, total_price_nok=None.
  4. Dynamic toggle options affect total_price_nok and unpriced_line_count.
  5. REST route POST /api/sales/dealroom (api_admin_sales_dealroom) handles requests and errors.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from starlette.responses import JSONResponse

from nce.admin_handlers._shared import admin_state
from nce.admin_handlers.sales import api_admin_sales_dealroom
from nce.vertical_modules.sales.dealroom import do_open_dealroom


class _AsyncCtx:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *_: Any) -> None:
        pass


def _make_engine(
    fetch_return: list[dict[str, Any]] | None = None, fetchrow_return: dict[str, Any] | None = None
) -> tuple[MagicMock, AsyncMock]:
    engine = MagicMock()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=fetch_return or [])
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)

    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    engine.pg_pool = pool
    return engine, conn


@pytest.fixture(autouse=True)
def _patch_scoped_session(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeScoped:
        def __init__(self, pool: Any, ns: Any) -> None:
            self._pool = pool
            self._ns = ns

        async def __aenter__(self) -> Any:
            return await self._pool.acquire().__aenter__()

        async def __aexit__(self, *_: Any) -> None:
            pass

    monkeypatch.setattr(
        "nce.vertical_modules.sales.dealroom.scoped_pg_session",
        _FakeScoped,
    )


@pytest.mark.asyncio
async def test_quote_id_prefix_matching_uses_literal_starts_with() -> None:
    """Wave S-3: starts_with literal prefix prevents quote ID wildcard collisions."""
    quote_id = "QU_TEST%123"
    ns = uuid4()

    engine, conn = _make_engine(
        fetch_return=[],
        fetchrow_return={
            "name": "Test Quote",
            "source_json": {},
            "manual": {},
        },
    )

    res = await do_open_dealroom(
        engine,
        {
            "namespace_id": str(ns),
            "quote_id": quote_id,
        },
    )

    assert res["quote_id"] == quote_id
    assert res["lines"] == []

    # Verify query used starts_with and passed literal prefix
    assert conn.fetch.called
    call_args = conn.fetch.call_args[0]
    sql = call_args[0]
    assert "starts_with(b.bom_line_label, $3)" in sql
    assert call_args[1] == str(ns)
    assert call_args[2] == quote_id
    assert call_args[3] == f"BOM_LINE:{quote_id.upper()}:"


@pytest.mark.asyncio
async def test_absent_catalog_product_yields_none_not_fabricated_defaults() -> None:
    """Wave S-3: absent catalog product yields manufacturer=None, model=None, never 'Unknown'."""
    quote_id = f"Q-{uuid4().hex[:8]}"
    ns = uuid4()
    label = f"BOM_LINE:{quote_id.upper()}:LINE-1"

    engine, conn = _make_engine(
        fetch_return=[
            {
                "id": uuid4(),
                "bom_line_label": label,
                "quote_id": quote_id,
                "line_ref": "LINE-1",
                "qty": 2.0,
                "unit_price": 500.0,
                "line_total": 1000.0,
                "currency": "NOK",
                "priced": True,
                "origin_kind": "manual",
                "origin_ref": None,
                "manufacturer": None,  # absent from product_catalog
                "model": None,  # absent from product_catalog
            }
        ],
        fetchrow_return={"name": "No Catalog Quote", "source_json": {}, "manual": {}},
    )

    res = await do_open_dealroom(
        engine,
        {
            "namespace_id": str(ns),
            "quote_id": quote_id,
        },
    )

    assert len(res["lines"]) == 1
    line = res["lines"][0]
    assert line["manufacturer"] is None, "Must be None (absent), never 'Unknown'"
    assert line["model"] is None, "Must be None (absent), never 'Unknown'"
    assert line["is_optional"] is None, "Must be None (absent), never True"
    assert line["unit_price"] == 500.0
    assert line["total_price"] == 1000.0
    assert line["priced"] is True
    assert res["total_price_nok"] == 1000.0
    assert res["unpriced_line_count"] == 0


@pytest.mark.asyncio
async def test_unpriced_lines_report_priced_false_and_none_prices() -> None:
    """Wave S-3: unpriced lines report priced: False, unit_price: None, total_price: None, total_price_nok: None."""
    quote_id = f"Q-{uuid4().hex[:8]}"
    ns = uuid4()
    label = f"BOM_LINE:{quote_id.upper()}:LINE-1"

    engine, conn = _make_engine(
        fetch_return=[
            {
                "id": uuid4(),
                "bom_line_label": label,
                "quote_id": quote_id,
                "line_ref": "LINE-1",
                "qty": 3.0,
                "unit_price": 0.0,
                "line_total": 0.0,
                "currency": "NOK",
                "priced": False,
                "origin_kind": "design",
                "origin_ref": "DESIGN_LINE:1",
                "manufacturer": "Sony",
                "model": "Projector-X",
            }
        ],
        fetchrow_return={"name": "Unpriced Quote", "source_json": {}, "manual": {}},
    )

    res = await do_open_dealroom(
        engine,
        {
            "namespace_id": str(ns),
            "quote_id": quote_id,
        },
    )

    assert len(res["lines"]) == 1
    line = res["lines"][0]
    assert line["priced"] is False
    assert line["unpriced_reason"] == "no_price_on_record"
    assert line["unit_price"] is None
    assert line["total_price"] is None
    assert line["base_cost"] is None
    assert res["total_price_nok"] is None
    assert res["unpriced_line_count"] == 1


@pytest.mark.asyncio
async def test_dynamic_toggles_recompute_total_and_unpriced_count() -> None:
    """Wave S-3: dynamic toggle options affect total_price_nok and unpriced_line_count."""
    quote_id = f"Q-{uuid4().hex[:8]}"
    ns = uuid4()
    label1 = f"BOM_LINE:{quote_id.upper()}:LINE-1"
    label2 = f"BOM_LINE:{quote_id.upper()}:LINE-2"

    lines_data = [
        {
            "id": uuid4(),
            "bom_line_label": label1,
            "quote_id": quote_id,
            "line_ref": "LINE-1",
            "qty": 1.0,
            "unit_price": 2500.0,
            "line_total": 2500.0,
            "currency": "NOK",
            "priced": True,
            "origin_kind": "manual",
            "origin_ref": None,
            "manufacturer": "Shure",
            "model": "Microphone",
        },
        {
            "id": uuid4(),
            "bom_line_label": label2,
            "quote_id": quote_id,
            "line_ref": "LINE-2",
            "qty": 1.0,
            "unit_price": 0.0,
            "line_total": 0.0,
            "currency": "NOK",
            "priced": False,
            "origin_kind": "manual",
            "origin_ref": None,
            "manufacturer": "K&M",
            "model": "Stand",
        },
    ]

    engine, _ = _make_engine(
        fetch_return=lines_data,
        fetchrow_return={"name": "Toggle Quote", "source_json": {}, "manual": {}},
    )

    # 1. Both active by default: line2 is unpriced, so room total is None
    res_default = await do_open_dealroom(
        engine,
        {
            "namespace_id": str(ns),
            "quote_id": quote_id,
        },
    )
    assert res_default["total_price_nok"] is None
    assert res_default["unpriced_line_count"] == 1
    assert res_default["lines"][0]["toggled"] is True
    assert res_default["lines"][1]["toggled"] is True

    # 2. Toggle off unpriced line2: total becomes honest (2500.0) and unpriced count is 0
    res_toggled_off = await do_open_dealroom(
        engine,
        {
            "namespace_id": str(ns),
            "quote_id": quote_id,
            "toggled_options": {label2: False},
        },
    )
    assert res_toggled_off["total_price_nok"] == 2500.0
    assert res_toggled_off["unpriced_line_count"] == 0
    assert res_toggled_off["lines"][1]["toggled"] is False

    # 3. Toggle off both lines: total is 0.0, unpriced count is 0
    res_all_off = await do_open_dealroom(
        engine,
        {
            "namespace_id": str(ns),
            "quote_id": quote_id,
            "toggled_options": {label1: False, label2: False},
        },
    )
    assert res_all_off["total_price_nok"] == 0.0
    assert res_all_off["unpriced_line_count"] == 0


@pytest.mark.asyncio
async def test_api_admin_sales_dealroom_rest_endpoint() -> None:
    """Wave S-3: REST route POST /api/sales/dealroom functions correctly."""
    ns = str(uuid4())
    quote_id = f"Q-{uuid4().hex[:8]}"

    # 1. Error when engine is not connected (503)
    admin_state.engine = None
    req_mock = MagicMock()
    req_mock.json = AsyncMock(return_value={"namespace_id": ns, "quote_id": quote_id})
    resp_503 = await api_admin_sales_dealroom(req_mock)
    assert isinstance(resp_503, JSONResponse)
    assert resp_503.status_code == 503

    # 2. Setup mock engine
    engine, conn = _make_engine(
        fetch_return=[],
        fetchrow_return={"name": "REST DealRoom Quote", "source_json": {}, "manual": {}},
    )
    admin_state.engine = engine

    try:
        # Error when namespace_id missing (422)
        req_bad_ns = MagicMock()
        req_bad_ns.json = AsyncMock(return_value={"quote_id": quote_id})
        req_bad_ns.query_params = {}
        resp_422 = await api_admin_sales_dealroom(req_bad_ns)
        assert resp_422.status_code == 422
        assert "Missing required field: namespace_id" in json.loads(resp_422.body)["error"]

        # Error when quote_id missing (422)
        req_bad_quote = MagicMock()
        req_bad_quote.json = AsyncMock(return_value={"namespace_id": ns})
        req_bad_quote.query_params = {}
        resp_422_q = await api_admin_sales_dealroom(req_bad_quote)
        assert resp_422_q.status_code == 422
        assert "Missing required field: quote_id" in json.loads(resp_422_q.body)["error"]

        # Success case (200)
        req_ok = MagicMock()
        req_ok.json = AsyncMock(
            return_value={
                "namespace_id": ns,
                "quote_id": quote_id,
                "toggled_options": {},
            }
        )
        req_ok.query_params = {}
        resp_200 = await api_admin_sales_dealroom(req_ok)
        assert resp_200.status_code == 200
        body = json.loads(resp_200.body)
        assert body["status"] == "ok"
        assert body["dealroom"]["quote_id"] == quote_id
        assert body["dealroom"]["name"] == "REST DealRoom Quote"
    finally:
        admin_state.engine = None
