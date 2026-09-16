"""
tests/unit/test_vendors_seed.py
===============================
Unit tests for Wave V-3: Seed VENDOR identities from sales_read_model
and Nettailer sources through C1 entity resolution.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.admin_handlers.vendors import api_vendors_seed
from nce.entity_resolution.resolver import Match
from nce.vertical_modules.vendors.registry import do_seed_vendors

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"


def _make_engine() -> MagicMock:
    engine = MagicMock()
    engine.pg_pool = MagicMock()
    engine.mongo_client = MagicMock()
    return engine


def _make_request(
    qp: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> MagicMock:
    req = MagicMock()
    req.query_params = qp or {}
    if body is not None:
        req.json = AsyncMock(return_value=body)
    else:
        req.json = AsyncMock(side_effect=Exception("No JSON body"))
    return req


# ---------------------------------------------------------------------------
# Core: do_seed_vendors validation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_seed_vendors_missing_namespace_id() -> None:
    engine = _make_engine()
    with pytest.raises(ValueError, match="namespace_id is required"):
        await do_seed_vendors(engine, {})


@pytest.mark.asyncio
async def test_do_seed_vendors_invalid_namespace_id() -> None:
    engine = _make_engine()
    with pytest.raises(ValueError, match="Invalid namespace_id"):
        await do_seed_vendors(engine, {"namespace_id": "not-a-valid-uuid"})


# ---------------------------------------------------------------------------
# Core: do_seed_vendors sales_read_model source
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_seed_vendors_sales_read_model() -> None:
    engine = _make_engine()
    ns_uuid = uuid.UUID(_NAMESPACE_ID)

    srm_rows = [
        {
            "id": 101,
            "entity": "accounts",
            "source_id": "acc-001",
            "name": "Supplier One AS",
            "source_json": {
                "organizationnumber": "987654321",
                "customertypecode": "11",
                "email": "contact@supp1.no",
            },
        },
        {
            "id": 102,
            "entity": "suppliers",
            "source_id": "supp-002",
            "name": "",
            "source_json": {
                "accountname": "Supplier Two AS",
                "phone": "12345678",
            },
        },
    ]

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = srm_rows
    mock_conn.fetchrow.return_value = None

    class MockPgContext:
        async def __aenter__(self) -> AsyncMock:
            return mock_conn

        async def __aexit__(self, *args: Any) -> None:
            pass

    with (
        patch(
            "nce.vertical_modules.vendors.registry.scoped_pg_session",
            return_value=MockPgContext(),
        ),
        patch(
            "nce.vertical_modules.vendors.registry.resolve",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "nce.vertical_modules.vendors.registry.do_upsert_vendor",
            new_callable=AsyncMock,
        ) as mock_upsert,
    ):
        mock_upsert.side_effect = [
            {"ok": True, "label": "VENDOR:987654321", "payload_ref": "ref1"},
            {"ok": True, "label": "VENDOR:SRM_SUPP002", "payload_ref": "ref2"},
        ]

        result = await do_seed_vendors(
            engine,
            {
                "namespace_id": ns_uuid,
                "sources": ["sales_read_model"],
            },
        )

        assert result["ok"] is True
        assert result["dry_run"] is False
        assert result["sales_read_model_candidates"] == 2
        assert result["sales_read_model_seeded"] == 2
        assert result["nettailer_candidates"] == 0
        assert result["total_seeded"] == 2
        assert len(result["vendors"]) == 2

        # Check first candidate call
        mock_upsert.assert_any_call(
            engine,
            {
                "namespace_id": ns_uuid,
                "orgnr": "987654321",
                "name": "Supplier One AS",
                "source_id": "sales_read_model:acc-001",
                "feed_fields": {
                    "source": "sales_read_model",
                    "entity": "accounts",
                    "source_id": "acc-001",
                    "customertypecode": "11",
                    "email": "contact@supp1.no",
                },
                "source_type": "feed",
            },
        )


# ---------------------------------------------------------------------------
# Core: do_seed_vendors nettailer source
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_seed_vendors_nettailer() -> None:
    engine = _make_engine()
    ns_uuid = uuid.UUID(_NAMESPACE_ID)

    mfr_rows = [{"manufacturer": "Cisco Systems"}]
    supp_rows = [{"supplier": "Also AS"}]

    mock_conn = AsyncMock()

    async def mock_fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        if "product_catalog" in query:
            return mfr_rows
        if "product_prices" in query:
            return supp_rows
        return []

    mock_conn.fetch.side_effect = mock_fetch
    mock_conn.fetchrow.return_value = None

    class MockPgContext:
        async def __aenter__(self) -> AsyncMock:
            return mock_conn

        async def __aexit__(self, *args: Any) -> None:
            pass

    with (
        patch(
            "nce.vertical_modules.vendors.registry.scoped_pg_session",
            return_value=MockPgContext(),
        ),
        patch(
            "nce.vertical_modules.vendors.registry.resolve",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "nce.vertical_modules.vendors.registry.do_upsert_vendor",
            new_callable=AsyncMock,
        ) as mock_upsert,
    ):
        mock_upsert.side_effect = [
            {"ok": True, "label": "VENDOR:NET_CISCOSYSTEMS", "payload_ref": "ref-cisco"},
            {"ok": True, "label": "VENDOR:NET_ALSOAS", "payload_ref": "ref-also"},
        ]

        result = await do_seed_vendors(
            engine,
            {
                "namespace_id": ns_uuid,
                "sources": ["nettailer"],
            },
        )

        assert result["ok"] is True
        assert result["nettailer_candidates"] == 2
        assert result["nettailer_seeded"] == 2
        assert result["total_seeded"] == 2

        # Check Nettailer candidate arguments
        mock_upsert.assert_any_call(
            engine,
            {
                "namespace_id": ns_uuid,
                "orgnr": "NET_CISCOSYSTEMS",
                "name": "Cisco Systems",
                "source_id": "nettailer:Cisco Systems",
                "feed_fields": {
                    "source": "nettailer",
                    "is_manufacturer": True,
                    "is_supplier": False,
                },
                "source_type": "feed",
            },
        )


# ---------------------------------------------------------------------------
# Core: do_seed_vendors C1 resolution match & deduplication
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_seed_vendors_c1_dedup_match() -> None:
    engine = _make_engine()
    ns_uuid = uuid.UUID(_NAMESPACE_ID)
    matched_node_uuid = uuid.uuid4()

    mfr_rows = [{"manufacturer": "HP Inc"}]

    mock_conn = AsyncMock()

    async def mock_fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        if "product_catalog" in query:
            return mfr_rows
        return []

    mock_conn.fetch.side_effect = mock_fetch

    async def mock_fetchrow(query: str, *args: Any) -> dict[str, Any] | None:
        if "WHERE id = $1" in query and args[0] == matched_node_uuid:
            return {"label": "VENDOR:984567890"}
        return None

    mock_conn.fetchrow.side_effect = mock_fetchrow

    class MockPgContext:
        async def __aenter__(self) -> AsyncMock:
            return mock_conn

        async def __aexit__(self, *args: Any) -> None:
            pass

    # C1 resolver returns high-confidence match
    match = Match(node_id=matched_node_uuid, score=0.92, matched_on=["name"])

    with (
        patch(
            "nce.vertical_modules.vendors.registry.scoped_pg_session",
            return_value=MockPgContext(),
        ),
        patch(
            "nce.vertical_modules.vendors.registry.resolve",
            new_callable=AsyncMock,
            return_value=[match],
        ),
        patch(
            "nce.vertical_modules.vendors.registry.do_upsert_vendor",
            new_callable=AsyncMock,
            return_value={"ok": True, "label": "VENDOR:984567890", "payload_ref": "ref-hp"},
        ) as mock_upsert,
    ):
        result = await do_seed_vendors(
            engine,
            {
                "namespace_id": ns_uuid,
                "sources": ["nettailer"],
            },
        )

        assert result["ok"] is True
        assert result["nettailer_candidates"] == 1
        assert result["vendors"][0]["action"] == "update"
        assert result["vendors"][0]["orgnr"] == "984567890"

        # Verify do_upsert_vendor was invoked with the resolved orgnr, NOT fallback NET_HPINC
        mock_upsert.assert_called_once_with(
            engine,
            {
                "namespace_id": ns_uuid,
                "orgnr": "984567890",
                "name": "HP Inc",
                "source_id": "nettailer:HP Inc",
                "feed_fields": {
                    "source": "nettailer",
                    "is_manufacturer": True,
                    "is_supplier": False,
                },
                "source_type": "feed",
            },
        )


# ---------------------------------------------------------------------------
# Core: do_seed_vendors dry_run mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_seed_vendors_dry_run() -> None:
    engine = _make_engine()
    ns_uuid = uuid.UUID(_NAMESPACE_ID)

    mfr_rows = [{"manufacturer": "Barco"}]

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = mfr_rows
    mock_conn.fetchrow.return_value = None

    class MockPgContext:
        async def __aenter__(self) -> AsyncMock:
            return mock_conn

        async def __aexit__(self, *args: Any) -> None:
            pass

    with (
        patch(
            "nce.vertical_modules.vendors.registry.scoped_pg_session",
            return_value=MockPgContext(),
        ),
        patch(
            "nce.vertical_modules.vendors.registry.resolve",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "nce.vertical_modules.vendors.registry.do_upsert_vendor",
            new_callable=AsyncMock,
        ) as mock_upsert,
    ):
        result = await do_seed_vendors(
            engine,
            {
                "namespace_id": ns_uuid,
                "sources": ["nettailer"],
                "dry_run": True,
            },
        )

        assert result["ok"] is True
        assert result["dry_run"] is True
        assert result["total_seeded"] == 0
        assert len(result["vendors"]) == 1
        assert result["vendors"][0]["dry_run"] is True
        assert result["vendors"][0]["name"] == "Barco"
        assert result["vendors"][0]["action"] == "create"

        # Ensure do_upsert_vendor was never called
        mock_upsert.assert_not_called()


# ---------------------------------------------------------------------------
# Core: do_seed_vendors limit parameter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_seed_vendors_limit() -> None:
    engine = _make_engine()
    ns_uuid = uuid.UUID(_NAMESPACE_ID)

    srm_rows = [
        {
            "id": 1,
            "entity": "suppliers",
            "source_id": "s1",
            "name": "Supplier 1",
            "source_json": {},
        },
        {
            "id": 2,
            "entity": "suppliers",
            "source_id": "s2",
            "name": "Supplier 2",
            "source_json": {},
        },
    ]

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = srm_rows
    mock_conn.fetchrow.return_value = None

    class MockPgContext:
        async def __aenter__(self) -> AsyncMock:
            return mock_conn

        async def __aexit__(self, *args: Any) -> None:
            pass

    with (
        patch(
            "nce.vertical_modules.vendors.registry.scoped_pg_session",
            return_value=MockPgContext(),
        ),
        patch(
            "nce.vertical_modules.vendors.registry.resolve",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "nce.vertical_modules.vendors.registry.do_upsert_vendor",
            new_callable=AsyncMock,
            return_value={"ok": True, "label": "VENDOR:SRM_S1", "payload_ref": "p1"},
        ) as mock_upsert,
    ):
        result = await do_seed_vendors(
            engine,
            {
                "namespace_id": ns_uuid,
                "sources": ["sales_read_model"],
                "limit": 1,
            },
        )

        assert result["ok"] is True
        assert result["total_candidates"] == 1
        assert result["total_seeded"] == 1
        assert mock_upsert.call_count == 1


# ---------------------------------------------------------------------------
# REST Handler: api_vendors_seed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_vendors_seed_success() -> None:
    from nce import admin_state

    engine = _make_engine()
    mock_result = {
        "ok": True,
        "namespace_id": _NAMESPACE_ID,
        "dry_run": False,
        "total_seeded": 3,
    }

    with (
        patch.object(admin_state, "engine", engine),
        patch(
            "nce.admin_handlers.vendors.do_seed_vendors",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_core,
        patch(
            "nce.admin_handlers.vendors.bump_mcp_cache_generation",
            new_callable=AsyncMock,
        ) as mock_bump,
    ):
        req = _make_request(body={"namespace_id": _NAMESPACE_ID})
        resp = await api_vendors_seed(req)
        assert resp.status_code == 200
        mock_core.assert_called_once()
        mock_bump.assert_called_once_with(engine, route="api_vendors_seed")


@pytest.mark.asyncio
async def test_api_vendors_seed_dry_run_skips_bump() -> None:
    from nce import admin_state

    engine = _make_engine()
    mock_result = {
        "ok": True,
        "namespace_id": _NAMESPACE_ID,
        "dry_run": True,
        "total_seeded": 0,
    }

    with (
        patch.object(admin_state, "engine", engine),
        patch(
            "nce.admin_handlers.vendors.do_seed_vendors",
            new_callable=AsyncMock,
            return_value=mock_result,
        ),
        patch(
            "nce.admin_handlers.vendors.bump_mcp_cache_generation",
            new_callable=AsyncMock,
        ) as mock_bump,
    ):
        req = _make_request(body={"namespace_id": _NAMESPACE_ID, "dry_run": True})
        resp = await api_vendors_seed(req)
        assert resp.status_code == 200
        mock_bump.assert_not_called()


@pytest.mark.asyncio
async def test_api_vendors_seed_validation_errors() -> None:
    from nce import admin_state

    engine = _make_engine()

    with patch.object(admin_state, "engine", engine):
        # Missing namespace_id
        resp = await api_vendors_seed(_make_request(body={}))
        assert resp.status_code == 422

        # Invalid namespace_id
        resp = await api_vendors_seed(_make_request(body={"namespace_id": "not-uuid"}))
        assert resp.status_code == 422
