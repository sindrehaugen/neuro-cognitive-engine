"""Tests for C17 Site Master Data's address registry feed (MLV16 charter,
Lane F, Wave F-9): ``nce/vertical_modules/sites/address_registry.py``.

All HTTP calls go through an injected ``httpx.MockTransport`` — no test
reaches the network. The allow-list refusal test uses a transport that
raises if it is ever invoked, proving the refusal happens before any HTTP
call.
"""

from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from nce.vertical_modules.sites.address_registry import (
    _ALLOWED_READS,
    _BASE_PATH,
    AddressRegistryAllowListRefusal,
    _gated_request,
    _query_from_site_address,
    address_key,
    do_enrich_site_from_address_registry,
    lookup_address,
    structure_hit,
)

NS_ID = uuid.uuid4()
SITE_ID = uuid.uuid4()

_HIT = {
    "adressetekst": "Storgata 1",
    "postnummer": "0155",
    "poststed": "OSLO",
    "kommunenavn": "OSLO",
    "kommunenummer": "0301",
    "adressekode": 12345,
    "nummer": 1,
    "bokstav": "",
    "representasjonspunkt": {"lat": 59.913, "lon": 10.752, "epsg": "4326"},
}


def _never_reached(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"transport reached: {request.method} {request.url}")


def _responder(status_code: int = 200, hits: list | None = None):
    def _handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code, json={"adresser": hits if hits is not None else [_HIT]}, request=request
        )

    return _handle


def _row_for_site(address: dict | None = None) -> dict:
    import datetime

    return {
        "id": SITE_ID,
        "namespace_id": NS_ID,
        "name": "Test Site",
        "cadastre_id": None,
        "site_type": "building",
        "address": address or {},
        "latitude": None,
        "longitude": None,
        "altitude": None,
        "height": None,
        "footprint_geometry": {},
        "telemetry_stream": None,
        "metadata": {},
        "archived": False,
        "created_at": datetime.datetime.now(datetime.timezone.utc),
        "updated_at": datetime.datetime.now(datetime.timezone.utc),
    }


@pytest.fixture
def mock_pg_pool():
    pool = AsyncMock()
    conn = AsyncMock()
    conn.__aenter__.return_value = conn
    conn.__aexit__ = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=_row_for_site())
    conn.execute = AsyncMock()
    conn.transaction = MagicMock()
    conn.transaction.return_value.__aenter__ = AsyncMock(return_value=conn)
    conn.transaction.return_value.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    return pool, conn


def _engine(pool):
    return types.SimpleNamespace(pg_pool=pool)


# ---------------------------------------------------------------------------
# Allow-list
# ---------------------------------------------------------------------------


class TestAllowList:
    def test_allow_list_is_exactly_one_get(self):
        assert _ALLOWED_READS == {("GET", _BASE_PATH)}

    @pytest.mark.asyncio
    async def test_refuses_a_method_outside_its_allow_list(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(_never_reached))
        try:
            with pytest.raises(AddressRegistryAllowListRefusal):
                await _gated_request(client, "POST", "Storgata 1")
        finally:
            await client.aclose()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestAddressKey:
    def test_road_address_uses_address_code(self):
        assert address_key(_HIT) == "0301-12345-1"

    def test_road_address_keeps_letter_when_present(self):
        hit = {**_HIT, "bokstav": "B"}
        assert address_key(hit) == "0301-12345-1-B"

    def test_cadastre_only_address_falls_back_to_plot(self):
        hit = {"kommunenummer": "0301", "gardsnummer": "10", "bruksnummer": "20"}
        assert address_key(hit) == "0301-g10-b20"

    def test_empty_hit_returns_empty_string(self):
        assert address_key({}) == ""


class TestStructureHit:
    def test_maps_geonorge_fields(self):
        result = structure_hit(_HIT)
        assert result == {
            "address_key": "0301-12345-1",
            "formatted_address": "Storgata 1",
            "postal_code": "0155",
            "city": "OSLO",
            "municipality": "OSLO",
            "municipality_number": "0301",
            "lat": 59.913,
            "lon": 10.752,
        }


class TestQueryFromSiteAddress:
    def test_prefers_formatted_address(self):
        address = {"formatted_address": "Storgata 1, 0155 OSLO", "street": "ignored"}
        assert _query_from_site_address(address) == "Storgata 1, 0155 OSLO"

    def test_assembles_from_parts_when_no_formatted_address(self):
        address = {"street": "Storgata 1", "postal_code": "0155", "city": "OSLO"}
        assert _query_from_site_address(address) == "Storgata 1, 0155 OSLO"

    def test_empty_address_returns_empty_string(self):
        assert _query_from_site_address({}) == ""


# ---------------------------------------------------------------------------
# lookup_address -- mocked transport, fail-soft contract
# ---------------------------------------------------------------------------


class TestLookupAddress:
    @pytest.mark.asyncio
    async def test_happy_path(self):
        result = await lookup_address("Storgata 1", transport=httpx.MockTransport(_responder()))
        assert result["formatted_address"] == "Storgata 1"

    @pytest.mark.asyncio
    async def test_empty_query_never_reaches_transport(self):
        result = await lookup_address("   ", transport=httpx.MockTransport(_never_reached))
        assert result is None

    @pytest.mark.asyncio
    async def test_zero_hits_returns_none(self):
        result = await lookup_address("Nowhere", transport=httpx.MockTransport(_responder(hits=[])))
        assert result is None

    @pytest.mark.asyncio
    async def test_non_200_degrades_to_none(self):
        result = await lookup_address(
            "Storgata 1", transport=httpx.MockTransport(_responder(status_code=500, hits=[]))
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_transport_exception_degrades_to_none(self):
        def _raise(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("boom", request=request)

        result = await lookup_address("Storgata 1", transport=httpx.MockTransport(_raise))
        assert result is None


# ---------------------------------------------------------------------------
# do_enrich_site_from_address_registry
# ---------------------------------------------------------------------------


class TestDoEnrichValidation:
    @pytest.mark.asyncio
    async def test_missing_namespace_id_raises(self, mock_pg_pool):
        pool, _ = mock_pg_pool
        with pytest.raises(ValueError, match="namespace_id"):
            await do_enrich_site_from_address_registry(_engine(pool), {"site_id": str(SITE_ID)})

    @pytest.mark.asyncio
    async def test_missing_site_id_raises(self, mock_pg_pool):
        pool, _ = mock_pg_pool
        with pytest.raises(ValueError, match="site_id"):
            await do_enrich_site_from_address_registry(_engine(pool), {"namespace_id": str(NS_ID)})

    @pytest.mark.asyncio
    async def test_site_not_in_namespace_raises_before_any_http_call(self, mock_pg_pool):
        pool, conn = mock_pg_pool
        conn.fetchrow = AsyncMock(return_value=None)
        with pytest.raises(ValueError, match="not an active site"):
            await do_enrich_site_from_address_registry(
                _engine(pool),
                {"namespace_id": str(NS_ID), "site_id": str(SITE_ID)},
                transport=httpx.MockTransport(_never_reached),
            )

    @pytest.mark.asyncio
    async def test_no_address_and_no_query_override_raises_before_any_http_call(self, mock_pg_pool):
        pool, conn = mock_pg_pool
        conn.fetchrow = AsyncMock(return_value=_row_for_site(address={}))
        with pytest.raises(ValueError, match="no address text"):
            await do_enrich_site_from_address_registry(
                _engine(pool),
                {"namespace_id": str(NS_ID), "site_id": str(SITE_ID)},
                transport=httpx.MockTransport(_never_reached),
            )


class TestDoEnrichHappyPath:
    @pytest.mark.asyncio
    async def test_matched_address_writes_site_fields(self, mock_pg_pool, monkeypatch):
        pool, conn = mock_pg_pool
        conn.fetchrow = AsyncMock(
            return_value=_row_for_site(address={"street": "Storgata 1", "city": "OSLO"})
        )

        update_mock = AsyncMock()
        monkeypatch.setattr("nce.vertical_modules.sites.address_registry.update_site", update_mock)

        result = await do_enrich_site_from_address_registry(
            _engine(pool),
            {"namespace_id": str(NS_ID), "site_id": str(SITE_ID)},
            transport=httpx.MockTransport(_responder()),
        )

        assert result["matched"] is True
        assert result["address"]["formatted_address"] == "Storgata 1"
        update_mock.assert_awaited_once()
        _, args, _ = update_mock.mock_calls[0]
        updates = args[3]
        assert updates["address"]["is_validated"] is True
        assert updates["address"]["postal_code"] == "0155"
        assert updates["latitude"] == 59.913
        assert updates["longitude"] == 10.752

    @pytest.mark.asyncio
    async def test_query_override_is_used_instead_of_stored_address(
        self, mock_pg_pool, monkeypatch
    ):
        pool, conn = mock_pg_pool
        conn.fetchrow = AsyncMock(return_value=_row_for_site(address={}))
        update_mock = AsyncMock()
        monkeypatch.setattr("nce.vertical_modules.sites.address_registry.update_site", update_mock)

        seen_queries = []

        def _responder_capturing(request: httpx.Request) -> httpx.Response:
            seen_queries.append(request.url.params.get("sok"))
            return httpx.Response(200, json={"adresser": [_HIT]}, request=request)

        result = await do_enrich_site_from_address_registry(
            _engine(pool),
            {"namespace_id": str(NS_ID), "site_id": str(SITE_ID), "query": "Storgata 1, Oslo"},
            transport=httpx.MockTransport(_responder_capturing),
        )

        assert result["matched"] is True
        assert seen_queries == ["Storgata 1, Oslo"]


class TestDoEnrichFailSoft:
    @pytest.mark.asyncio
    async def test_not_found_in_registry_is_fail_soft_not_an_error(self, mock_pg_pool, monkeypatch):
        pool, conn = mock_pg_pool
        conn.fetchrow = AsyncMock(return_value=_row_for_site(address={"street": "Nowhere"}))
        update_mock = AsyncMock()
        monkeypatch.setattr("nce.vertical_modules.sites.address_registry.update_site", update_mock)

        result = await do_enrich_site_from_address_registry(
            _engine(pool),
            {"namespace_id": str(NS_ID), "site_id": str(SITE_ID)},
            transport=httpx.MockTransport(_responder(hits=[])),
        )

        assert result["ok"] is True
        assert result["matched"] is False
        assert result["reason"] == "not_found_in_registry"
        update_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_transport_error_is_fail_soft_not_an_error(self, mock_pg_pool, monkeypatch):
        pool, conn = mock_pg_pool
        conn.fetchrow = AsyncMock(return_value=_row_for_site(address={"street": "Storgata 1"}))
        update_mock = AsyncMock()
        monkeypatch.setattr("nce.vertical_modules.sites.address_registry.update_site", update_mock)

        def _raise(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("boom", request=request)

        result = await do_enrich_site_from_address_registry(
            _engine(pool),
            {"namespace_id": str(NS_ID), "site_id": str(SITE_ID)},
            transport=httpx.MockTransport(_raise),
        )

        assert result["ok"] is True
        assert result["matched"] is False
        update_mock.assert_not_awaited()
