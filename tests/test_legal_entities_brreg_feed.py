"""Tests for the C15 Legal-Entity Register's national business registry
feed (MLV16 charter, Lane F, Wave F-8):
``nce/vertical_modules/legal_entities/brreg_feed.py``.

All HTTP calls go through an injected ``httpx.MockTransport`` — no test
reaches the network. The allow-list refusal and malformed-org_nr checks
use a transport that raises if it is ever invoked, proving the refusal
happens before any HTTP call.
"""

from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from nce.vertical_modules.legal_entities.brreg_feed import (
    _ALLOWED_READS,
    BrregAllowListRefusal,
    _fields_from_enhet,
    _gated_request,
    do_enrich_legal_entity_from_registry,
)

NS_ID = uuid.uuid4()
ENTITY_ID = uuid.uuid4()
ORG_NR = "923609016"


def _never_reached(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"transport reached: {request.method} {request.url}")


class TestAllowList:
    def test_allow_list_is_exactly_one_get(self):
        assert _ALLOWED_READS == {("GET", "/enhetsregisteret/api/enheter/{org_nr}")}

    @pytest.mark.asyncio
    async def test_refuses_a_method_outside_its_allow_list(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(_never_reached))
        try:
            with pytest.raises(BrregAllowListRefusal):
                await _gated_request(client, "POST", ORG_NR)
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_refuses_a_malformed_org_nr_before_any_http_call(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(_never_reached))
        try:
            with pytest.raises(ValueError, match="9-digit"):
                await _gated_request(client, "GET", "12345")
        finally:
            await client.aclose()


class TestFieldsFromEnhet:
    def test_extracts_company_level_fields(self):
        enhet = {
            "navn": "Eksempel AS",
            "naeringskode1": {"kode": "62.010", "beskrivelse": "Programmeringstjenester"},
            "organisasjonsform": {"kode": "AS"},
            "forretningsadresse": {
                "adresse": ["Eksempelveien 1"],
                "postnummer": "0001",
                "poststed": "OSLO",
                "kommunenummer": "0301",
                "landkode": "NO",
            },
            "slettedato": None,
            "konkurs": False,
            "underAvvikling": False,
        }
        fields = _fields_from_enhet(enhet)
        assert fields["brreg_navn"] == "Eksempel AS"
        assert fields["brreg_naeringskode"] == "62.010"
        assert fields["brreg_naeringsbeskrivelse"] == "Programmeringstjenester"
        assert fields["brreg_organisasjonsform"] == "AS"
        assert fields["brreg_adresse"] == "Eksempelveien 1"
        assert fields["brreg_postnummer"] == "0001"
        assert fields["brreg_poststed"] == "OSLO"
        assert fields["brreg_konkurs"] is False
        assert "brreg_synced_at" in fields

    def test_never_writes_a_role_or_person_field_even_if_present_upstream(self):
        # A real BRREG response never carries "rollegrupper" on the /enheter
        # endpoint used here, but this is a documentation-as-test guard: even
        # if the upstream shape changed to include it, this function must
        # never surface it. See the module docstring's decision (2).
        enhet = {
            "navn": "Eksempel AS",
            "rollegrupper": [{"type": {"kode": "DAGL"}, "roller": [{"person": {"navn": "..."}}]}],
        }
        fields = _fields_from_enhet(enhet)
        assert "roles" not in fields
        assert "roller" not in fields
        assert "rollegrupper" not in fields
        assert not any("person" in k or "navn_person" in k for k in fields)

    def test_missing_address_and_nace_are_simply_absent(self):
        fields = _fields_from_enhet({"navn": "Bare Navn AS"})
        assert fields["brreg_navn"] == "Bare Navn AS"
        assert "brreg_naeringskode" not in fields
        assert "brreg_adresse" not in fields

    def test_adresse_as_plain_string_is_accepted(self):
        enhet = {"navn": "X", "forretningsadresse": {"adresse": "Gate 1"}}
        assert _fields_from_enhet(enhet)["brreg_adresse"] == "Gate 1"


# ---------------------------------------------------------------------------
# do_enrich_legal_entity_from_registry
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_pg_pool():
    pool = AsyncMock()
    conn = AsyncMock()
    conn.__aenter__.return_value = conn
    conn.__aexit__ = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value={"org_nr": ORG_NR})
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    conn.executemany = AsyncMock()
    conn.transaction = MagicMock()
    conn.transaction.return_value.__aenter__ = AsyncMock(return_value=conn)
    conn.transaction.return_value.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    return pool, conn


def _engine(pool):
    return types.SimpleNamespace(pg_pool=pool)


class TestDoEnrichValidation:
    @pytest.mark.asyncio
    async def test_missing_namespace_id_raises(self, mock_pg_pool):
        pool, _ = mock_pg_pool
        with pytest.raises(ValueError, match="namespace_id"):
            await do_enrich_legal_entity_from_registry(_engine(pool), {"entity_id": str(ENTITY_ID)})

    @pytest.mark.asyncio
    async def test_missing_entity_id_raises(self, mock_pg_pool):
        pool, _ = mock_pg_pool
        with pytest.raises(ValueError, match="entity_id"):
            await do_enrich_legal_entity_from_registry(_engine(pool), {"namespace_id": str(NS_ID)})

    @pytest.mark.asyncio
    async def test_entity_not_in_namespace_raises_before_any_http_call(self, mock_pg_pool):
        pool, conn = mock_pg_pool
        conn.fetchrow = AsyncMock(return_value=None)
        with pytest.raises(ValueError, match="not an active legal entity"):
            await do_enrich_legal_entity_from_registry(
                _engine(pool),
                {"namespace_id": str(NS_ID), "entity_id": str(ENTITY_ID)},
                transport=httpx.MockTransport(_never_reached),
            )


class TestDoEnrichHappyPath:
    @pytest.mark.asyncio
    async def test_matched_entity_writes_metadata(self, mock_pg_pool, monkeypatch):
        pool, conn = mock_pg_pool

        def _responder(request: httpx.Request) -> httpx.Response:
            assert request.url.path == f"/enhetsregisteret/api/enheter/{ORG_NR}"
            return httpx.Response(
                200,
                json={
                    "navn": "Eksempel AS",
                    "naeringskode1": {"kode": "62.010", "beskrivelse": "Programmering"},
                    "organisasjonsform": {"kode": "AS"},
                    "forretningsadresse": {
                        "adresse": ["Gate 1"],
                        "postnummer": "0001",
                        "poststed": "OSLO",
                    },
                },
            )

        update_mock = AsyncMock()
        monkeypatch.setattr(
            "nce.vertical_modules.legal_entities.brreg_feed.update_legal_entity", update_mock
        )

        result = await do_enrich_legal_entity_from_registry(
            _engine(pool),
            {"namespace_id": str(NS_ID), "entity_id": str(ENTITY_ID)},
            transport=httpx.MockTransport(_responder),
        )

        assert result["matched"] is True
        assert result["org_nr"] == ORG_NR
        assert "brreg_navn" in result["fields_written"]
        update_mock.assert_awaited_once()
        _, args, kwargs = update_mock.mock_calls[0]
        assert kwargs["metadata"]["brreg_navn"] == "Eksempel AS"


class TestDoEnrichFailSoft:
    @pytest.mark.asyncio
    async def test_not_found_in_registry_is_fail_soft_not_an_error(self, mock_pg_pool, monkeypatch):
        pool, conn = mock_pg_pool

        def _responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={})

        update_mock = AsyncMock()
        monkeypatch.setattr(
            "nce.vertical_modules.legal_entities.brreg_feed.update_legal_entity", update_mock
        )

        result = await do_enrich_legal_entity_from_registry(
            _engine(pool),
            {"namespace_id": str(NS_ID), "entity_id": str(ENTITY_ID)},
            transport=httpx.MockTransport(_responder),
        )

        assert result["ok"] is True
        assert result["matched"] is False
        assert result["reason"] == "not_found_in_registry"
        assert result["fields_written"] == []
        update_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_transport_error_is_fail_soft_not_an_error(self, mock_pg_pool, monkeypatch):
        pool, conn = mock_pg_pool

        def _responder(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("boom")

        update_mock = AsyncMock()
        monkeypatch.setattr(
            "nce.vertical_modules.legal_entities.brreg_feed.update_legal_entity", update_mock
        )

        result = await do_enrich_legal_entity_from_registry(
            _engine(pool),
            {"namespace_id": str(NS_ID), "entity_id": str(ENTITY_ID)},
            transport=httpx.MockTransport(_responder),
        )

        assert result["matched"] is False
        assert result["reason"] == "registry_unreachable"
        update_mock.assert_not_awaited()
