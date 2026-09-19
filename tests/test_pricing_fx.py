"""Tests for the shared pricing service's FX rate feed (MLV16 charter,
Lane F, Wave F-15): ``nce/pricing/fx.py``.

All HTTP calls go through an injected ``httpx.MockTransport`` — no test
reaches the network. The allow-list refusal test uses a transport that
raises if it is ever invoked, proving the refusal happens before any HTTP
call.
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import httpx
import pytest

from nce.pricing.fx import (
    _ALLOWED_READS,
    _BASE_PATH,
    CURRENCIES,
    FxAllowListRefusal,
    _gated_request,
    _load_last_good,
    _persist_last_good,
    convert_to_nok,
    do_get_fx_rates,
    fetch_rates,
    get_rates,
    parse_csv,
)

_CSV = (
    "BASE_CUR;QUOTE_CUR;TIME_PERIOD;OBS_VALUE\n"
    "EUR;NOK;2026-09-18;11.834\n"
    "USD;NOK;2026-09-18;10.021\n"
)


def _never_reached(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"transport reached: {request.method} {request.url}")


def _responder(status_code: int = 200, text: str = _CSV):
    def _handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=text, request=request)

    return _handle


# ---------------------------------------------------------------------------
# Allow-list
# ---------------------------------------------------------------------------


class TestAllowList:
    def test_allow_list_is_exactly_one_get_on_the_currency_path(self):
        assert _ALLOWED_READS == {("GET", _BASE_PATH)}
        for currency in CURRENCIES:
            assert currency in _BASE_PATH

    @pytest.mark.asyncio
    async def test_refuses_a_method_outside_its_allow_list(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(_never_reached))
        try:
            with pytest.raises(FxAllowListRefusal):
                await _gated_request(client, "POST")
        finally:
            await client.aclose()


# ---------------------------------------------------------------------------
# parse_csv -- pure function, no HTTP
# ---------------------------------------------------------------------------


class TestParseCsv:
    def test_parses_rates_and_date_by_header_name(self):
        result = parse_csv(_CSV)
        assert result == {"rates": {"EUR": 11.834, "USD": 10.021}, "date": "2026-09-18"}

    def test_column_order_does_not_matter(self):
        reordered = "OBS_VALUE;TIME_PERIOD;BASE_CUR\n11.834;2026-09-18;EUR\n"
        assert parse_csv(reordered) == {"rates": {"EUR": 11.834}, "date": "2026-09-18"}

    def test_comma_decimal_separator_is_accepted(self):
        result = parse_csv("BASE_CUR;OBS_VALUE\nEUR;11,834\n")
        assert result == {"rates": {"EUR": 11.834}, "date": None}

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "BASE_CUR;OBS_VALUE\n",
            "not,a,recognised,header\nfoo;bar\n",
            "BASE_CUR;OTHER\nEUR;x\n",
        ],
    )
    def test_empty_or_unrecognisable_input_returns_empty_dict(self, text):
        assert parse_csv(text) == {}

    def test_non_positive_or_unparseable_values_are_dropped(self):
        result = parse_csv("BASE_CUR;OBS_VALUE\nEUR;0\nUSD;-1\nGBP;not-a-number\nSEK;9.5\n")
        assert result == {"rates": {"SEK": 9.5}, "date": None}


# ---------------------------------------------------------------------------
# fetch_rates -- mocked transport, fail-soft contract
# ---------------------------------------------------------------------------


class TestFetchRates:
    @pytest.mark.asyncio
    async def test_happy_path(self):
        result = await fetch_rates(transport=httpx.MockTransport(_responder()))
        assert result == {"rates": {"EUR": 11.834, "USD": 10.021}, "date": "2026-09-18"}

    @pytest.mark.asyncio
    async def test_non_200_degrades_to_empty_dict(self):
        result = await fetch_rates(
            transport=httpx.MockTransport(_responder(status_code=503, text=""))
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_transport_exception_degrades_to_empty_dict(self):
        def _raise(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("boom", request=request)

        result = await fetch_rates(transport=httpx.MockTransport(_raise))
        assert result == {}


# ---------------------------------------------------------------------------
# get_rates -- in-process TTL cache, DB persist/load, degrade chain
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_module_cache():
    import nce.pricing.fx as fx_mod

    fx_mod._cache["data"] = None
    fx_mod._cache["fetched_at"] = 0.0
    yield
    fx_mod._cache["data"] = None
    fx_mod._cache["fetched_at"] = 0.0


@pytest.fixture
def mock_pg_pool():
    pool = AsyncMock()
    conn = AsyncMock()
    conn.__aenter__.return_value = conn
    conn.__aexit__ = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.executemany = AsyncMock()
    pool.acquire = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    return pool, conn


class TestPersistAndLoadLastGoodExceptionHandling:
    """A genuine DB-connectivity error degrades to ``False``/``None``; any
    other exception (a bug in this function, not a DB outage) propagates
    rather than being silently swallowed -- the swallowed-exception census
    (``tests/unit/test_swallowed_exception_census.py``) objects to a bare
    ``except Exception`` over a money-adjacent write, not to error
    handling as such."""

    @pytest.mark.asyncio
    async def test_persist_degrades_to_false_on_postgres_error(self, mock_pg_pool):
        pool, conn = mock_pg_pool
        conn.executemany = AsyncMock(side_effect=asyncpg.PostgresError("boom"))
        engine = types.SimpleNamespace(pg_pool=pool)
        result = await _persist_last_good(engine, {"EUR": 11.5}, "2026-09-18")
        assert result is False

    @pytest.mark.asyncio
    async def test_persist_propagates_a_non_db_exception(self, mock_pg_pool):
        pool, conn = mock_pg_pool
        conn.executemany = AsyncMock(side_effect=TypeError("not a db problem"))
        engine = types.SimpleNamespace(pg_pool=pool)
        with pytest.raises(TypeError):
            await _persist_last_good(engine, {"EUR": 11.5}, "2026-09-18")

    @pytest.mark.asyncio
    async def test_load_degrades_to_none_on_postgres_error(self, mock_pg_pool):
        pool, conn = mock_pg_pool
        conn.fetch = AsyncMock(side_effect=asyncpg.PostgresError("boom"))
        engine = types.SimpleNamespace(pg_pool=pool)
        assert await _load_last_good(engine) is None

    @pytest.mark.asyncio
    async def test_load_propagates_a_non_db_exception(self, mock_pg_pool):
        pool, conn = mock_pg_pool
        conn.fetch = AsyncMock(side_effect=TypeError("not a db problem"))
        engine = types.SimpleNamespace(pg_pool=pool)
        with pytest.raises(TypeError):
            await _load_last_good(engine)


class TestGetRates:
    @pytest.mark.asyncio
    async def test_fresh_fetch_persists_and_returns_not_stale(self, mock_pg_pool):
        pool, conn = mock_pg_pool
        engine = types.SimpleNamespace(pg_pool=pool)
        data = await get_rates(engine, transport=httpx.MockTransport(_responder()))
        assert data["stale"] is False
        assert data["rates"] == {"EUR": 11.834, "USD": 10.021}
        assert data["source"] == "Norges Bank"
        conn.executemany.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_second_call_within_ttl_never_reaches_transport(self, mock_pg_pool):
        pool, conn = mock_pg_pool
        engine = types.SimpleNamespace(pg_pool=pool)
        await get_rates(engine, transport=httpx.MockTransport(_responder()))
        # A transport that raises if reached proves the second call was
        # served entirely from the in-process cache.
        data = await get_rates(engine, transport=httpx.MockTransport(_never_reached))
        assert data["stale"] is False
        assert data["rates"] == {"EUR": 11.834, "USD": 10.021}

    @pytest.mark.asyncio
    async def test_source_failure_with_warm_cache_degrades_to_stale_cached(self, mock_pg_pool):
        pool, conn = mock_pg_pool
        engine = types.SimpleNamespace(pg_pool=pool)
        await get_rates(engine, transport=httpx.MockTransport(_responder()))
        data = await get_rates(
            engine, force=True, transport=httpx.MockTransport(_responder(status_code=500, text=""))
        )
        assert data["stale"] is True
        assert data["rates"] == {"EUR": 11.834, "USD": 10.021}

    @pytest.mark.asyncio
    async def test_cold_start_source_failure_falls_back_to_persisted_last_good(self, mock_pg_pool):
        import datetime

        pool, conn = mock_pg_pool
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "currency": "EUR",
                    "rate": 11.5,
                    "rate_date": datetime.date(2026, 9, 17),
                    "fetched_at": datetime.datetime(
                        2026, 9, 17, 16, 0, tzinfo=datetime.timezone.utc
                    ),
                }
            ]
        )
        engine = types.SimpleNamespace(pg_pool=pool)
        data = await get_rates(
            engine, transport=httpx.MockTransport(_responder(status_code=500, text=""))
        )
        assert data["stale"] is True
        assert data["rates"] == {"EUR": 11.5}

    @pytest.mark.asyncio
    async def test_cold_start_no_source_no_persisted_data_returns_empty_stale_contract(
        self, mock_pg_pool
    ):
        pool, conn = mock_pg_pool
        engine = types.SimpleNamespace(pg_pool=pool)
        data = await get_rates(
            engine, transport=httpx.MockTransport(_responder(status_code=500, text=""))
        )
        assert data == {
            "base": "NOK",
            "rates": {},
            "date": None,
            "fetchedAt": data["fetchedAt"],
            "stale": True,
            "source": "Norges Bank",
        }


# ---------------------------------------------------------------------------
# convert_to_nok -- pure function
# ---------------------------------------------------------------------------


class TestConvertToNok:
    def test_converts_using_the_supplied_rate(self):
        assert convert_to_nok(100.0, "eur", {"EUR": 11.5}) == 1150.0

    def test_unknown_currency_returns_none_not_a_guessed_rate(self):
        assert convert_to_nok(100.0, "GBP", {"EUR": 11.5}) is None


# ---------------------------------------------------------------------------
# do_get_fx_rates -- MCP entry point
# ---------------------------------------------------------------------------


class TestDoGetFxRates:
    @pytest.mark.asyncio
    async def test_wraps_get_rates_with_an_ok_envelope(self, mock_pg_pool):
        pool, _conn = mock_pg_pool
        engine = types.SimpleNamespace(pg_pool=pool)
        import nce.pricing.fx as fx_mod

        original_get_rates = fx_mod.get_rates

        async def _stub(engine, *, force=False, transport=None):
            return {
                "base": "NOK",
                "rates": {"EUR": 11.5},
                "date": "2026-09-18",
                "fetchedAt": "2026-09-18T00:00:00+00:00",
                "stale": False,
                "source": "Norges Bank",
            }

        fx_mod.get_rates = _stub
        try:
            result = await do_get_fx_rates(engine, {})
        finally:
            fx_mod.get_rates = original_get_rates
        assert result["ok"] is True
        assert result["rates"] == {"EUR": 11.5}
