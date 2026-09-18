"""
tests/test_geodata_n50.py
============================
Wave F-12 (Lane F, MLV16 charter): N50 land-cover local store.

Unit tests (no marker, mocked pg_pool) run in the ordinary unit job and
cover every branch that does not require real Postgres box/GiST/unique-
constraint behaviour. The three @pytest.mark.integration tests below them
prove that behaviour against a live database, and are wired into
.github/workflows/ci.yml in the same commit as this file -- an unwired
integration-marked file gates nothing, which was flagged against this
exact pattern on PR #239 (Wave F-11) and is not repeated here.
"""

from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from nce.vertical_modules.geodata.n50 import (
    _bbox_for_rings,
    _bbox_literal,
    do_import_n50_land_cover,
    do_query_n50_land_cover,
)

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestBboxLiteral:
    def test_formats_as_postgres_box_literal(self):
        assert _bbox_literal(10.0, 59.0, 10.5, 59.5) == "((10.0,59.0),(10.5,59.5))"


class TestBboxForRings:
    def test_single_ring(self):
        rings = [[10.0, 59.0], [10.5, 59.0], [10.5, 59.5], [10.0, 59.5]]
        assert _bbox_for_rings(rings) == (10.0, 59.0, 10.5, 59.5)

    def test_multi_ring_polygon_with_hole(self):
        rings = [
            [[10.0, 59.0], [11.0, 59.0], [11.0, 60.0], [10.0, 60.0]],
            [[10.2, 59.2], [10.3, 59.2], [10.3, 59.3], [10.2, 59.3]],
        ]
        assert _bbox_for_rings(rings) == (10.0, 59.0, 11.0, 60.0)

    def test_no_coordinates_returns_none(self):
        assert _bbox_for_rings([]) is None
        assert _bbox_for_rings(None) is None
        assert _bbox_for_rings("not-a-list") is None

    def test_integer_coordinates_are_accepted(self):
        rings = [[10, 59], [11, 59], [11, 60]]
        assert _bbox_for_rings(rings) == (10.0, 59.0, 11.0, 60.0)


# ---------------------------------------------------------------------------
# do_import_n50_land_cover -- validation (raises before any DB call)
# ---------------------------------------------------------------------------


class TestDoImportValidation:
    @pytest.mark.asyncio
    async def test_missing_source_file_raises(self):
        with pytest.raises(ValueError, match="source_file"):
            await do_import_n50_land_cover(None, {"features": []})

    @pytest.mark.asyncio
    async def test_blank_source_file_raises(self):
        with pytest.raises(ValueError, match="source_file"):
            await do_import_n50_land_cover(None, {"source_file": "   ", "features": []})

    @pytest.mark.asyncio
    async def test_features_not_a_list_raises(self):
        with pytest.raises(ValueError, match="features"):
            await do_import_n50_land_cover(None, {"source_file": "n50.sos", "features": {}})

    @pytest.mark.asyncio
    async def test_feature_missing_klasse_raises(self):
        with pytest.raises(ValueError, match="klasse/objid"):
            await do_import_n50_land_cover(
                None,
                {
                    "source_file": "n50.sos",
                    "features": [{"objid": 1, "rings": [[10.0, 59.0]]}],
                },
            )

    @pytest.mark.asyncio
    async def test_feature_non_int_objid_raises(self):
        with pytest.raises(ValueError, match="klasse/objid"):
            await do_import_n50_land_cover(
                None,
                {
                    "source_file": "n50.sos",
                    "features": [{"klasse": "Skog", "objid": "not-an-int", "rings": []}],
                },
            )


# ---------------------------------------------------------------------------
# do_import_n50_land_cover -- all-skipped path never touches the DB
# ---------------------------------------------------------------------------


class TestDoImportAllSkipped:
    @pytest.mark.asyncio
    async def test_features_with_no_geometry_are_skipped_without_a_db_call(self):
        # engine has no usable pg_pool at all -- if this reached
        # unmanaged_pg_connection it would blow up immediately, so a
        # clean return here proves the DB path was never entered.
        engine = types.SimpleNamespace(pg_pool="not-a-real-pool")
        result = await do_import_n50_land_cover(
            engine,
            {
                "source_file": "n50.sos",
                "features": [
                    {"klasse": "Skog", "objid": 1, "rings": []},
                    {"klasse": "Myr", "objid": 2, "rings": None},
                    "not-a-dict",
                ],
            },
        )
        assert result == {
            "ok": True,
            "source_file": "n50.sos",
            "received": 3,
            "written": 0,
            "skipped_no_geometry": 3,
        }


# ---------------------------------------------------------------------------
# do_import_n50_land_cover -- mocked pg_pool
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
    async def test_writes_rows_with_geometry_and_reports_mixed_batch(self, mock_pg_pool):
        pool, conn = mock_pg_pool
        engine = types.SimpleNamespace(pg_pool=pool)
        result = await do_import_n50_land_cover(
            engine,
            {
                "source_file": "n50.sos",
                "features": [
                    {
                        "klasse": "Skog",
                        "objid": 1,
                        "objtype": "Løvskog",
                        "rings": [[10.0, 59.0], [10.5, 59.5]],
                        "area_m2": 12345.6,
                    },
                    {"klasse": "Myr", "objid": 2, "rings": []},
                ],
            },
        )
        assert result == {
            "ok": True,
            "source_file": "n50.sos",
            "received": 2,
            "written": 1,
            "skipped_no_geometry": 1,
        }
        conn.executemany.assert_awaited_once()
        _, args, _ = conn.executemany.mock_calls[0]
        rows = args[1]
        assert len(rows) == 1
        klasse, objid, objtype, rings_json, area_m2, bbox, source_file = rows[0]
        assert (klasse, objid, objtype) == ("Skog", 1, "Løvskog")
        assert area_m2 == 12345.6
        assert bbox == "((10.0,59.0),(10.5,59.5))"
        assert source_file == "n50.sos"

    @pytest.mark.asyncio
    async def test_no_rows_never_calls_executemany(self, mock_pg_pool):
        pool, conn = mock_pg_pool
        engine = types.SimpleNamespace(pg_pool=pool)
        await do_import_n50_land_cover(engine, {"source_file": "n50.sos", "features": []})
        conn.executemany.assert_not_awaited()


# ---------------------------------------------------------------------------
# do_query_n50_land_cover -- validation
# ---------------------------------------------------------------------------


class TestDoQueryValidation:
    @pytest.mark.asyncio
    async def test_missing_field_raises(self):
        with pytest.raises(ValueError, match="min_lon"):
            await do_query_n50_land_cover(None, {"min_lat": 59.0, "max_lon": 11.0, "max_lat": 60.0})

    @pytest.mark.asyncio
    async def test_non_numeric_field_raises(self):
        with pytest.raises(ValueError, match="max_lat"):
            await do_query_n50_land_cover(
                None,
                {"min_lon": 10.0, "min_lat": 59.0, "max_lon": 11.0, "max_lat": "north"},
            )

    @pytest.mark.asyncio
    async def test_min_exceeding_max_raises(self):
        with pytest.raises(ValueError, match="min must not exceed max"):
            await do_query_n50_land_cover(
                None,
                {"min_lon": 12.0, "min_lat": 59.0, "max_lon": 11.0, "max_lat": 60.0},
            )


# ---------------------------------------------------------------------------
# do_query_n50_land_cover -- mocked pg_pool
# ---------------------------------------------------------------------------


def _row(klasse: str, objid: int, area_m2: float | None) -> dict:
    return {
        "klasse": klasse,
        "objid": objid,
        "objtype": None,
        "rings": "[[10.0, 59.0], [10.5, 59.5]]",
        "area_m2": area_m2,
    }


class TestDoQueryWithMockedPool:
    @pytest.mark.asyncio
    async def test_klasse_filter_is_added_to_the_query_when_present(self, mock_pg_pool):
        pool, conn = mock_pg_pool
        conn.fetch = AsyncMock(return_value=[_row("Skog", 1, 100.0)])
        engine = types.SimpleNamespace(pg_pool=pool)
        result = await do_query_n50_land_cover(
            engine,
            {"min_lon": 10.0, "min_lat": 59.0, "max_lon": 11.0, "max_lat": 60.0, "klasse": "Skog"},
        )
        query, bbox_arg, klasse_arg = conn.fetch.call_args.args
        assert "AND klasse = $2" in query
        assert klasse_arg == "Skog"
        assert result["count"] == 1
        assert result["features"][0]["klasse"] == "Skog"

    @pytest.mark.asyncio
    async def test_truncation_flag_set_when_more_rows_than_limit(self, mock_pg_pool):
        pool, conn = mock_pg_pool
        conn.fetch = AsyncMock(return_value=[_row("Skog", i, float(100 - i)) for i in range(3)])
        engine = types.SimpleNamespace(pg_pool=pool)
        result = await do_query_n50_land_cover(
            engine,
            {"min_lon": 10.0, "min_lat": 59.0, "max_lon": 11.0, "max_lat": 60.0, "limit": 2},
        )
        assert result["count"] == 2
        assert result["truncated"] is True

    @pytest.mark.asyncio
    async def test_limit_is_capped_at_max_query_limit(self, mock_pg_pool):
        pool, conn = mock_pg_pool
        conn.fetch = AsyncMock(return_value=[])
        engine = types.SimpleNamespace(pg_pool=pool)
        await do_query_n50_land_cover(
            engine,
            {
                "min_lon": 10.0,
                "min_lat": 59.0,
                "max_lon": 11.0,
                "max_lat": 60.0,
                "limit": 999999,
            },
        )
        query = conn.fetch.call_args.args[0]
        assert "LIMIT 501" in query


# ---------------------------------------------------------------------------
# Integration -- real Postgres, real box/GiST/unique-constraint behaviour
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestGeodataN50Integration:
    @pytest.mark.asyncio
    async def test_import_then_bbox_query_roundtrip(self, pg_pool):
        engine = types.SimpleNamespace(pg_pool=pg_pool)
        source = f"n50-test-{uuid.uuid4().hex}.sos"
        objid = int(uuid.uuid4().int % 1_000_000_000)
        await do_import_n50_land_cover(
            engine,
            {
                "source_file": source,
                "features": [
                    {
                        "klasse": "Skog",
                        "objid": objid,
                        "rings": [[10.0, 59.0], [10.5, 59.5]],
                        "area_m2": 5000.0,
                    },
                    # Well outside the query viewport below -- must not be returned.
                    {
                        "klasse": "Skog",
                        "objid": objid + 1,
                        "rings": [[80.0, 10.0], [80.5, 10.5]],
                        "area_m2": 5000.0,
                    },
                ],
            },
        )
        result = await do_query_n50_land_cover(
            engine,
            {"min_lon": 9.9, "min_lat": 58.9, "max_lon": 10.6, "max_lat": 59.6},
        )
        matched = [f for f in result["features"] if f["objid"] in (objid, objid + 1)]
        assert [f["objid"] for f in matched] == [objid]

    @pytest.mark.asyncio
    async def test_reimporting_the_same_klasse_objid_updates_not_duplicates(self, pg_pool):
        engine = types.SimpleNamespace(pg_pool=pg_pool)
        source = f"n50-test-{uuid.uuid4().hex}.sos"
        objid = int(uuid.uuid4().int % 1_000_000_000)
        base_feature = {
            "klasse": "Myr",
            "objid": objid,
            "rings": [[20.0, 69.0], [20.2, 69.2]],
            "area_m2": 1000.0,
        }
        first = await do_import_n50_land_cover(
            engine, {"source_file": source, "features": [base_feature]}
        )
        updated_feature = {**base_feature, "area_m2": 2000.0}
        second = await do_import_n50_land_cover(
            engine, {"source_file": source, "features": [updated_feature]}
        )
        assert first["written"] == 1
        assert second["written"] == 1
        result = await do_query_n50_land_cover(
            engine,
            {"min_lon": 19.9, "min_lat": 68.9, "max_lon": 20.3, "max_lat": 69.3, "klasse": "Myr"},
        )
        matches = [f for f in result["features"] if f["objid"] == objid]
        assert len(matches) == 1
        assert matches[0]["area_m2"] == 2000.0

    @pytest.mark.asyncio
    async def test_query_orders_by_area_descending_and_truncates(self, pg_pool):
        engine = types.SimpleNamespace(pg_pool=pg_pool)
        source = f"n50-test-{uuid.uuid4().hex}.sos"
        base_objid = int(uuid.uuid4().int % 1_000_000_000)
        features = [
            {
                "klasse": "Vann",
                "objid": base_objid + i,
                "rings": [[30.0, 60.0], [30.1, 60.1]],
                "area_m2": float(i),
            }
            for i in range(4)
        ]
        await do_import_n50_land_cover(engine, {"source_file": source, "features": features})
        result = await do_query_n50_land_cover(
            engine,
            {
                "min_lon": 29.9,
                "min_lat": 59.9,
                "max_lon": 30.2,
                "max_lat": 60.2,
                "klasse": "Vann",
                "limit": 2,
            },
        )
        ours = [f for f in result["features"] if base_objid <= f["objid"] < base_objid + 4]
        assert [f["objid"] for f in ours] == [base_objid + 3, base_objid + 2]
        assert result["truncated"] is True
