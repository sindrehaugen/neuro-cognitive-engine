"""Tests for the geodata place-name nearest-point lookup (MLV16 charter,
Lane F, Wave F-13): ``nce/vertical_modules/geodata/place_names.py``,
migration 090's ``geodata_place_names`` table.

Unit tier (no DB) covers validation, the pure haversine math, and —
the load-bearing property this module exists for — that the two-stage
lookup actually RE-RANKS candidates by real distance rather than
trusting the GiST `<->` degree-order the mocked connection returns them
in. Integration tier (``@pytest.mark.integration``) proves the SQL
against a real connection: the point upsert and the `<->` KNN read,
neither of which a mock can catch, since asyncpg's own type coercion
for ``::point`` is exactly what would silently break (the same trap
Waves F-11/F-12 hit for ``::box``, avoided here from the start via
``::text::point``).
"""

from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from nce.vertical_modules.geodata.place_names import (
    _haversine_m,
    _point_literal,
    do_import_place_names,
    do_query_nearest_place_name,
)

# ---------------------------------------------------------------------------
# Pure-logic tests (no DB)
# ---------------------------------------------------------------------------


class TestPointLiteral:
    def test_formats_as_postgres_point_literal(self):
        assert _point_literal(10.75, 59.91) == "(10.75,59.91)"


class TestHaversine:
    def test_zero_distance_for_identical_points(self):
        assert _haversine_m(10.0, 59.0, 10.0, 59.0) == pytest.approx(0.0, abs=1e-6)

    def test_oslo_to_bergen_is_roughly_correct(self):
        # Oslo (10.7522, 59.9139) -> Bergen (5.3221, 60.3913): ~305 km great-circle.
        d = _haversine_m(10.7522, 59.9139, 5.3221, 60.3913)
        assert 295_000 < d < 315_000

    def test_one_degree_of_longitude_shrinks_at_high_latitude(self):
        # Same 1-degree longitude step is a shorter real distance further north --
        # this is exactly why GiST degree-order cannot be trusted directly.
        d_low_lat = _haversine_m(10.0, 10.0, 11.0, 10.0)
        d_high_lat = _haversine_m(10.0, 70.0, 11.0, 70.0)
        assert d_high_lat < d_low_lat


# ---------------------------------------------------------------------------
# do_import_place_names -- validation (raises before any DB call)
# ---------------------------------------------------------------------------


class TestDoImportValidation:
    @pytest.mark.asyncio
    async def test_missing_source_file_raises(self):
        with pytest.raises(ValueError, match="source_file"):
            await do_import_place_names(None, {"places": []})

    @pytest.mark.asyncio
    async def test_places_not_a_list_raises(self):
        with pytest.raises(ValueError, match="places"):
            await do_import_place_names(None, {"source_file": "ssr.gml", "places": {}})


class TestDoImportAllInvalid:
    @pytest.mark.asyncio
    async def test_invalid_places_are_skipped_without_a_db_call(self):
        # engine has no usable pg_pool -- a clean return proves the DB path
        # was never entered (rows stayed empty, `if rows:` short-circuits).
        engine = types.SimpleNamespace(pg_pool="not-a-real-pool")
        result = await do_import_place_names(
            engine,
            {
                "source_file": "ssr.gml",
                "places": [
                    {"navn": "Missing external_id", "lon": 10.0, "lat": 59.0},
                    {"external_id": "1", "lon": 10.0, "lat": 59.0},  # missing navn
                    {"external_id": "2", "navn": "No coords"},
                    "not-a-dict",
                ],
            },
        )
        assert result == {
            "ok": True,
            "source_file": "ssr.gml",
            "received": 4,
            "written": 0,
            "skipped_invalid": 4,
        }


# ---------------------------------------------------------------------------
# do_import_place_names -- mocked pg_pool
# ---------------------------------------------------------------------------


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


class TestDoImportWithMockedPool:
    @pytest.mark.asyncio
    async def test_writes_valid_places_and_reports_mixed_batch(self, mock_pg_pool):
        pool, conn = mock_pg_pool
        engine = types.SimpleNamespace(pg_pool=pool)
        result = await do_import_place_names(
            engine,
            {
                "source_file": "ssr.gml",
                "places": [
                    {
                        "external_id": "ssr-123",
                        "navn": "Svolvær",
                        "kategori": "bebyggelse",
                        "sprak": "norsk",
                        "lon": 14.5683,
                        "lat": 68.2341,
                    },
                    {"external_id": "bad", "navn": "No coords"},
                ],
            },
        )
        assert result == {
            "ok": True,
            "source_file": "ssr.gml",
            "received": 2,
            "written": 1,
            "skipped_invalid": 1,
        }
        conn.executemany.assert_awaited_once()
        _, args, _ = conn.executemany.mock_calls[0]
        rows = args[1]
        assert len(rows) == 1
        external_id, navn, kategori, sprak, point, source_file = rows[0]
        assert (external_id, navn, kategori, sprak) == ("ssr-123", "Svolvær", "bebyggelse", "norsk")
        assert point == "(14.5683,68.2341)"
        assert source_file == "ssr.gml"

    @pytest.mark.asyncio
    async def test_no_rows_never_calls_executemany(self, mock_pg_pool):
        pool, conn = mock_pg_pool
        engine = types.SimpleNamespace(pg_pool=pool)
        await do_import_place_names(engine, {"source_file": "ssr.gml", "places": []})
        conn.executemany.assert_not_awaited()


# ---------------------------------------------------------------------------
# do_query_nearest_place_name -- validation
# ---------------------------------------------------------------------------


class TestDoQueryValidation:
    @pytest.mark.asyncio
    async def test_missing_lon_raises(self):
        with pytest.raises(ValueError, match="lon"):
            await do_query_nearest_place_name(None, {"lat": 59.0})

    @pytest.mark.asyncio
    async def test_non_numeric_lat_raises(self):
        with pytest.raises(ValueError, match="lon"):
            await do_query_nearest_place_name(None, {"lon": 10.0, "lat": "north"})


# ---------------------------------------------------------------------------
# do_query_nearest_place_name -- mocked pg_pool, the re-ranking property
# ---------------------------------------------------------------------------


def _row(external_id: str, navn: str, lon: float, lat: float) -> dict:
    return {
        "external_id": external_id,
        "navn": navn,
        "kategori": None,
        "sprak": None,
        "p_lon": lon,
        "p_lat": lat,
    }


class TestDoQueryReranking:
    @pytest.mark.asyncio
    async def test_reranks_by_real_distance_not_mock_return_order(self, mock_pg_pool):
        pool, conn = mock_pg_pool
        query_lon, query_lat = 10.7522, 59.9139  # Oslo

        # Mocked GiST candidate order is DELIBERATELY wrong (farthest first) --
        # if do_query_nearest_place_name trusted it, the test would fail.
        conn.fetch = AsyncMock(
            return_value=[
                _row("far", "Tromsø", 18.9553, 69.6492),
                _row("mid", "Bergen", 5.3221, 60.3913),
                _row("near", "Sandvika", 10.5231, 59.8938),
            ]
        )
        engine = types.SimpleNamespace(pg_pool=pool)

        result = await do_query_nearest_place_name(
            engine, {"lon": query_lon, "lat": query_lat, "limit": 3}
        )

        assert [p["external_id"] for p in result["places"]] == ["near", "mid", "far"]
        assert result["places"][0]["distance_m"] < result["places"][1]["distance_m"]
        assert result["places"][1]["distance_m"] < result["places"][2]["distance_m"]

    @pytest.mark.asyncio
    async def test_limit_defaults_to_one(self, mock_pg_pool):
        pool, conn = mock_pg_pool
        conn.fetch = AsyncMock(
            return_value=[_row("a", "A", 10.0, 59.0), _row("b", "B", 10.1, 59.1)]
        )
        engine = types.SimpleNamespace(pg_pool=pool)
        result = await do_query_nearest_place_name(engine, {"lon": 10.0, "lat": 59.0})
        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_kategori_filter_is_added_to_the_query_when_present(self, mock_pg_pool):
        pool, conn = mock_pg_pool
        conn.fetch = AsyncMock(return_value=[])
        engine = types.SimpleNamespace(pg_pool=pool)
        await do_query_nearest_place_name(engine, {"lon": 10.0, "lat": 59.0, "kategori": "sjø"})
        query, point_arg, kategori_arg = conn.fetch.call_args.args
        assert "WHERE kategori = $2" in query
        assert kategori_arg == "sjø"


# ---------------------------------------------------------------------------
# Integration -- real Postgres, real point/GiST behaviour
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestGeodataPlaceNamesIntegration:
    @pytest.mark.asyncio
    async def test_import_then_nearest_lookup_roundtrip(self, pg_pool):
        engine = types.SimpleNamespace(pg_pool=pg_pool)
        source = f"ssr-test-{uuid.uuid4().hex}.gml"
        eid_near = f"near-{uuid.uuid4().hex}"
        eid_far = f"far-{uuid.uuid4().hex}"
        await do_import_place_names(
            engine,
            {
                "source_file": source,
                "places": [
                    {"external_id": eid_near, "navn": "Nær", "lon": 10.75, "lat": 59.91},
                    {"external_id": eid_far, "navn": "Fjern", "lon": 30.0, "lat": 70.0},
                ],
            },
        )
        result = await do_query_nearest_place_name(
            engine, {"lon": 10.751, "lat": 59.911, "limit": 5}
        )
        matched = [p for p in result["places"] if p["external_id"] in (eid_near, eid_far)]
        assert matched[0]["external_id"] == eid_near

    @pytest.mark.asyncio
    async def test_reimporting_the_same_external_id_updates_not_duplicates(self, pg_pool):
        engine = types.SimpleNamespace(pg_pool=pg_pool)
        source = f"ssr-test-{uuid.uuid4().hex}.gml"
        eid = f"dup-{uuid.uuid4().hex}"
        first = await do_import_place_names(
            engine,
            {
                "source_file": source,
                "places": [{"external_id": eid, "navn": "Old", "lon": 10.0, "lat": 59.0}],
            },
        )
        second = await do_import_place_names(
            engine,
            {
                "source_file": source,
                "places": [{"external_id": eid, "navn": "New", "lon": 10.0, "lat": 59.0}],
            },
        )
        assert first["written"] == 1
        assert second["written"] == 1
        result = await do_query_nearest_place_name(engine, {"lon": 10.0, "lat": 59.0, "limit": 20})
        matches = [p for p in result["places"] if p["external_id"] == eid]
        assert len(matches) == 1
        assert matches[0]["navn"] == "New"
