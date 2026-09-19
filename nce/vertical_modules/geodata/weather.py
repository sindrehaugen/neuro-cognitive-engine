"""
nce/vertical_modules/geodata/weather.py
======================================
Weather-conditions FEED (MLV16 charter, Lane F, Wave F-15 — the second
half of the geodata module's "weather and FX" pairing, F44): a live
per-request read-through to MET Norway's public locationforecast API,
never a local store.

Same shape decision as the host's own ``vaer.py``: MET's Terms of Service
require an identifying ``User-Agent`` with a real contact, which a
browser's own ``fetch`` cannot set (a forbidden header name) — so this
must be a server-side call, not something exposed for a caller to hit
MET directly through. Read for shape only (Q-25); this module's schema,
functions and tests are its own, not a port of the host's.

Why this has no local store, unlike its three siblings
--------------------------------------------------------
OSM/N50/place names answer "what does the world look like here", a fact
that changes on a survey cycle of months or years — worth a batch import
and a persistent table. A cloud-cover reading answers "what does the sky
look like here right now" and is stale within the hour; persisting it
would only ever serve a request that arrived within the same TTL window
an in-process cache already covers for free. This module therefore has
no migration and no ``do_import_*`` — an in-process cache, keyed on a
rounded coordinate, is the whole store.

Coordinate rounding (2 decimals, ~1 km) is not a precision compromise
------------------------------------------------------------------------
MET's own guidance asks callers to round because every distinct
coordinate is a separate cache key on their side; cloud cover over one
building is not meaningfully different from cloud cover across the
street, and rounding also raises this module's own cache hit rate when
two callers ask about nearby points.

Q-25 (AGPL / proprietary boundary)
--------------------------------------
MET Norway's forecast data is public sector information (NLOD-licensed);
nothing about it is the host's proprietary work. What was read from the
host for shape only was the ARCHITECTURAL DECISION (server-side proxy
required by MET's own ToS, coordinate rounding, degrade-never-raise,
size-capped in-process cache) — re-implemented independently here, never
the host's code, User-Agent contact string, or comments.
"""

from __future__ import annotations

import datetime
import logging
import time
from typing import TYPE_CHECKING, Any

import httpx

from nce.config import cfg

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.geodata.weather")

_BASE_PATH = "/weatherapi/locationforecast/2.0/compact"
_BASE_URL = f"https://api.met.no{_BASE_PATH}"
_SOURCE = "MET Norway"
# An identifying contact is a MET ToS condition, not decoration -- an
# anonymous User-Agent is refused with 403. The project's own repository
# is the contact MET's own docs accept in place of an email address.
_HEADERS = {"User-Agent": "NCE/1.0 (+https://github.com/sindrehaugen/neuro-cognitive-engine)"}

# The allow-list literal (charter §0 rule 3): one GET, one exact path.
_ALLOWED_READS: frozenset[tuple[str, str]] = frozenset({("GET", _BASE_PATH)})

# In-memory cache keyed by rounded "lat,lon". `fetched_at` only advances
# on a fresh hit, so a degraded response keeps retrying MET on every call.
_cache: dict[str, dict[str, Any]] = {}
# Caps unbounded growth on an instance serving many distinct locations;
# oldest-fetched entries are evicted first. 512 covers every city in
# Norway several times over with room to spare.
_CACHE_MAX = 512


class WeatherAllowListRefusal(PermissionError):
    """A (method, path) pair fell outside this feed's allow-list.

    Raised BEFORE any HTTP call.
    """

    def __init__(self, method: str, path: str) -> None:
        super().__init__(f"weather feed refused {method} {path}: not in its read allow-list")
        self.method = method
        self.path = path


async def _gated_request(
    client: httpx.AsyncClient, method: str, lat: float, lon: float
) -> httpx.Response:
    if (method, _BASE_PATH) not in _ALLOWED_READS:
        raise WeatherAllowListRefusal(method, _BASE_PATH)
    return await client.request(method, _BASE_URL, params={"lat": lat, "lon": lon})


def _round_coordinate(value: Any) -> float | None:
    """Coordinate -> 2 decimals (~1 km), or ``None`` if it is not a real
    number."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return round(number, 2)


def valid_location(lat: Any, lon: Any) -> tuple[float, float] | None:
    """``(lat, lon)`` rounded, or ``None`` if the point does not exist on
    Earth (or is the (0, 0) null-island default many languages produce
    for an unset coordinate — MET answers it happily, so the bounds check
    alone would not catch this one)."""
    rounded_lat, rounded_lon = _round_coordinate(lat), _round_coordinate(lon)
    if rounded_lat is None or rounded_lon is None:
        return None
    if not (-90.0 <= rounded_lat <= 90.0) or not (-180.0 <= rounded_lon <= 180.0):
        return None
    if rounded_lat == 0.0 and rounded_lon == 0.0:
        return None
    return (rounded_lat, rounded_lon)


def parse_forecast(payload: Any) -> dict[str, Any]:
    """MET's locationforecast JSON -> ``{"cloud_cover_pct", "fog_pct",
    "precipitation_mm", "symbol", "time"}``, or ``{}`` if the shape does
    not carry a usable reading.

    Reads the FIRST point in the timeseries (MET's own current-conditions
    entry), never a "closest to now" search — the series already starts
    at the current hour and is sorted, so a timestamp-nearness search
    would only add a way to get the same answer wrong across a DST
    boundary. ``next_1_hours`` (precipitation/symbol) is absent from the
    series' last point and omitted entirely once MET steps to 6-hour
    resolution further out, so those two fields are optional; cloud cover
    under ``instant.details`` is the one field always present when the
    series has any point at all.
    """
    if not isinstance(payload, dict):
        return {}
    series = ((payload.get("properties") or {}).get("timeseries")) or []
    if not isinstance(series, list) or not series:
        return {}
    first = series[0]
    if not isinstance(first, dict):
        return {}
    point = first.get("data") or {}
    instant = ((point.get("instant") or {}).get("details")) or {}

    def _number(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number == number else None

    next_hour = point.get("next_1_hours") or {}
    result: dict[str, Any] = {
        "cloud_cover_pct": _number(instant.get("cloud_area_fraction")),
        "fog_pct": _number(instant.get("fog_area_fraction")),
        "precipitation_mm": _number((next_hour.get("details") or {}).get("precipitation_amount")),
        "symbol": ((next_hour.get("summary") or {}).get("symbol_code")) or None,
        "time": first.get("time"),
    }
    # No cloud-cover reading means nothing usable came back at all -- an
    # empty result is a cleaner "don't know" than a half-filled one.
    return result if result["cloud_cover_pct"] is not None else {}


async def fetch_forecast(
    lat: float,
    lon: float,
    *,
    client: httpx.AsyncClient | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Fetch the current-hour forecast from MET for one rounded point.
    Never raises — any failure degrades to ``{}``."""
    owns = client is None
    cl = client or httpx.AsyncClient(
        timeout=cfg.NCE_WEATHER_TIMEOUT_SECONDS,
        headers=_HEADERS,
        follow_redirects=True,
        transport=transport,
    )
    try:
        resp = await _gated_request(cl, "GET", lat, lon)
        if resp.status_code != 200:
            log.warning("weather: MET returned HTTP %s for %s,%s", resp.status_code, lat, lon)
            return {}
        return parse_forecast(resp.json())
    except WeatherAllowListRefusal:
        raise
    except Exception as exc:  # noqa: BLE001 — degrade, never raise into the caller
        log.warning("weather: fetch failed for %s,%s: %s", lat, lon, str(exc)[:200])
        return {}
    finally:
        if owns:
            await cl.aclose()


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _contract(
    reading: dict[str, Any], lat: float, lon: float, fetched_at: str, *, stale: bool
) -> dict[str, Any]:
    return {
        "cloud_cover_pct": reading.get("cloud_cover_pct"),
        "fog_pct": reading.get("fog_pct"),
        "precipitation_mm": reading.get("precipitation_mm"),
        "symbol": reading.get("symbol"),
        "location": {"lat": lat, "lon": lon},
        "time": reading.get("time"),
        "fetchedAt": fetched_at,
        "stale": bool(stale),
        "source": _SOURCE,
    }


def _evict_oldest() -> None:
    if len(_cache) <= _CACHE_MAX:
        return
    oldest_first = sorted(_cache, key=lambda key: _cache[key]["fetched_at"])
    for key in oldest_first[: len(_cache) - _CACHE_MAX]:
        _cache.pop(key, None)


async def do_get_weather(
    engine: NCEEngine, params: dict[str, Any], *, transport: httpx.BaseTransport | None = None
) -> dict[str, Any]:
    """MCP-callable entry point: current cloud cover / fog / precipitation
    for a point, from MET Norway.

    Parameters
    ----------
    params:
        ``{"lat": float, "lon": float, "force": bool}`` — ``lat``/``lon``
        required. ``force`` (default ``False``) bypasses the in-process
        TTL cache for this coordinate.

    ``transport`` is a test-only escape hatch (mirrors ``brreg_feed``'s
    own ``transport=`` kwarg): ``None`` in production uses httpx's real
    transport; tests inject an ``httpx.MockTransport`` so no test reaches
    the network.

    An invalid or null-island location is not an error: it returns
    ``cloud_cover_pct: null, stale: true`` rather than raising, the same
    "don't know" contract MET's own downstream sky rendering relies on.
    """
    del engine  # no DB access at all -- this feed is pure live-read
    location = valid_location(params.get("lat"), params.get("lon"))
    if location is None:
        return {"ok": True, **_contract({}, 0.0, 0.0, _now_iso(), stale=True)}
    lat, lon = location
    key = f"{lat},{lon}"
    force = bool(params.get("force", False))

    cached = _cache.get(key)
    if (
        not force
        and cached
        and (time.monotonic() - cached["fetched_at"] < cfg.NCE_WEATHER_TTL_SECONDS)
    ):
        return {"ok": True, **cached["data"]}

    fresh = await fetch_forecast(lat, lon, transport=transport)
    if fresh:
        data = _contract(fresh, lat, lon, _now_iso(), stale=False)
        _cache[key] = {"data": data, "fetched_at": time.monotonic()}
        _evict_oldest()
        return {"ok": True, **data}

    if cached:
        return {"ok": True, **{**cached["data"], "stale": True}}
    return {"ok": True, **_contract({}, lat, lon, _now_iso(), stale=True)}
