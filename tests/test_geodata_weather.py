"""Tests for the geodata module's weather live-read (MLV16 charter, Lane
F, Wave F-15): ``nce/vertical_modules/geodata/weather.py``.

All HTTP calls go through an injected ``httpx.MockTransport`` — no test
reaches the network. The allow-list refusal test uses a transport that
raises if it is ever invoked, proving the refusal happens before any HTTP
call. This module has no local table, so unlike its three siblings these
tests need no mocked pg_pool at all.
"""

from __future__ import annotations

import httpx
import pytest

from nce.vertical_modules.geodata.weather import (
    _ALLOWED_READS,
    _BASE_PATH,
    WeatherAllowListRefusal,
    _cache,
    _gated_request,
    do_get_weather,
    fetch_forecast,
    parse_forecast,
    valid_location,
)

OSLO = {"lat": 59.9139, "lon": 10.7522}


def _never_reached(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"transport reached: {request.method} {request.url}")


def _forecast_payload(
    cloud: float = 62.5, fog: float = 0.0, precip: float = 0.1, symbol: str = "cloudy"
) -> dict:
    return {
        "properties": {
            "timeseries": [
                {
                    "time": "2026-09-19T00:00:00Z",
                    "data": {
                        "instant": {
                            "details": {"cloud_area_fraction": cloud, "fog_area_fraction": fog}
                        },
                        "next_1_hours": {
                            "summary": {"symbol_code": symbol},
                            "details": {"precipitation_amount": precip},
                        },
                    },
                },
                {
                    "time": "2026-09-19T01:00:00Z",
                    "data": {"instant": {"details": {"cloud_area_fraction": 70.0}}},
                },
            ]
        }
    }


def _responder(status_code: int = 200, payload: dict | None = None):
    def _handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json=payload if payload is not None else _forecast_payload(),
            request=request,
        )

    return _handle


@pytest.fixture(autouse=True)
def _reset_cache():
    _cache.clear()
    yield
    _cache.clear()


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
            with pytest.raises(WeatherAllowListRefusal):
                await _gated_request(client, "POST", 59.91, 10.75)
        finally:
            await client.aclose()


# ---------------------------------------------------------------------------
# valid_location -- pure function
# ---------------------------------------------------------------------------


class TestValidLocation:
    def test_rounds_to_two_decimals(self):
        assert valid_location(59.913912, 10.752245) == (59.91, 10.75)

    @pytest.mark.parametrize(
        "lat,lon", [(91.0, 10.0), (-91.0, 10.0), (10.0, 181.0), (10.0, -181.0)]
    )
    def test_out_of_bounds_returns_none(self, lat, lon):
        assert valid_location(lat, lon) is None

    def test_null_island_returns_none(self):
        # A common default-unset-coordinate footgun: MET answers (0, 0)
        # happily, so the bounds check alone would not catch it.
        assert valid_location(0.0, 0.0) is None
        assert valid_location("0", "0.0") is None

    @pytest.mark.parametrize(
        "lat,lon", [(None, 10.0), ("not-a-number", 10.0), (float("nan"), 10.0)]
    )
    def test_non_numeric_or_nan_returns_none(self, lat, lon):
        assert valid_location(lat, lon) is None


# ---------------------------------------------------------------------------
# parse_forecast -- pure function
# ---------------------------------------------------------------------------


class TestParseForecast:
    def test_happy_path_reads_first_timeseries_point(self):
        result = parse_forecast(_forecast_payload())
        assert result == {
            "cloud_cover_pct": 62.5,
            "fog_pct": 0.0,
            "precipitation_mm": 0.1,
            "symbol": "cloudy",
            "time": "2026-09-19T00:00:00Z",
        }

    def test_missing_next_1_hours_still_returns_cloud_cover(self):
        payload = {
            "properties": {
                "timeseries": [
                    {"time": "t", "data": {"instant": {"details": {"cloud_area_fraction": 70.0}}}}
                ]
            }
        }
        result = parse_forecast(payload)
        assert result["cloud_cover_pct"] == 70.0
        assert result["precipitation_mm"] is None
        assert result["symbol"] is None

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"properties": {}},
            {"properties": {"timeseries": []}},
            {"properties": {"timeseries": "not-a-list"}},
            {"properties": {"timeseries": [{"data": {}}]}},
            "not-a-dict",
        ],
    )
    def test_no_usable_cloud_cover_returns_empty_dict(self, payload):
        assert parse_forecast(payload) == {}


# ---------------------------------------------------------------------------
# fetch_forecast -- mocked transport, fail-soft contract
# ---------------------------------------------------------------------------


class TestFetchForecast:
    @pytest.mark.asyncio
    async def test_happy_path(self):
        result = await fetch_forecast(59.91, 10.75, transport=httpx.MockTransport(_responder()))
        assert result["cloud_cover_pct"] == 62.5

    @pytest.mark.asyncio
    async def test_non_200_degrades_to_empty_dict(self):
        result = await fetch_forecast(
            59.91, 10.75, transport=httpx.MockTransport(_responder(status_code=403, payload={}))
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_transport_exception_degrades_to_empty_dict(self):
        def _raise(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("boom", request=request)

        result = await fetch_forecast(59.91, 10.75, transport=httpx.MockTransport(_raise))
        assert result == {}


# ---------------------------------------------------------------------------
# do_get_weather -- in-process TTL cache keyed by rounded coordinate
# ---------------------------------------------------------------------------


class TestDoGetWeather:
    @pytest.mark.asyncio
    async def test_happy_path_returns_ok_envelope(self):
        result = await do_get_weather(None, OSLO, transport=httpx.MockTransport(_responder()))
        assert result["ok"] is True
        assert result["cloud_cover_pct"] == 62.5
        assert result["stale"] is False
        assert result["location"] == {"lat": 59.91, "lon": 10.75}

    @pytest.mark.asyncio
    async def test_invalid_location_never_reaches_transport(self):
        result = await do_get_weather(
            None, {"lat": 0.0, "lon": 0.0}, transport=httpx.MockTransport(_never_reached)
        )
        assert result["ok"] is True
        assert result["cloud_cover_pct"] is None
        assert result["stale"] is True

    @pytest.mark.asyncio
    async def test_second_call_within_ttl_never_reaches_transport(self):
        await do_get_weather(None, OSLO, transport=httpx.MockTransport(_responder()))
        result = await do_get_weather(None, OSLO, transport=httpx.MockTransport(_never_reached))
        assert result["stale"] is False
        assert result["cloud_cover_pct"] == 62.5

    @pytest.mark.asyncio
    async def test_force_bypasses_the_cache(self):
        await do_get_weather(
            None,
            OSLO,
            transport=httpx.MockTransport(_responder(payload=_forecast_payload(cloud=10.0))),
        )
        result = await do_get_weather(
            None,
            {**OSLO, "force": True},
            transport=httpx.MockTransport(_responder(payload=_forecast_payload(cloud=99.0))),
        )
        assert result["cloud_cover_pct"] == 99.0

    @pytest.mark.asyncio
    async def test_source_failure_with_warm_cache_degrades_to_stale_cached(self):
        await do_get_weather(None, OSLO, transport=httpx.MockTransport(_responder()))
        result = await do_get_weather(
            None,
            {**OSLO, "force": True},
            transport=httpx.MockTransport(_responder(status_code=500, payload={})),
        )
        assert result["stale"] is True
        assert result["cloud_cover_pct"] == 62.5

    @pytest.mark.asyncio
    async def test_cold_start_source_failure_returns_empty_stale_contract(self):
        result = await do_get_weather(
            None, OSLO, transport=httpx.MockTransport(_responder(status_code=500, payload={}))
        )
        assert result["stale"] is True
        assert result["cloud_cover_pct"] is None

    @pytest.mark.asyncio
    async def test_cache_is_keyed_per_rounded_coordinate(self):
        await do_get_weather(
            None,
            OSLO,
            transport=httpx.MockTransport(_responder(payload=_forecast_payload(cloud=1.0))),
        )
        bergen = {"lat": 60.39, "lon": 5.32}
        result = await do_get_weather(
            None,
            bergen,
            transport=httpx.MockTransport(_responder(payload=_forecast_payload(cloud=2.0))),
        )
        assert result["cloud_cover_pct"] == 2.0

    @pytest.mark.asyncio
    async def test_cache_eviction_caps_growth(self):
        import nce.vertical_modules.geodata.weather as weather_mod

        for i in range(weather_mod._CACHE_MAX + 5):
            lat = 1.0 + i * 0.01
            await do_get_weather(
                None, {"lat": lat, "lon": 1.0}, transport=httpx.MockTransport(_responder())
            )
        assert len(_cache) == weather_mod._CACHE_MAX
