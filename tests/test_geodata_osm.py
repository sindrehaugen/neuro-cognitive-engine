"""Tests for the geodata OSM local store (MLV16 charter, Lane F, Wave F-11):
``nce/vertical_modules/geodata/osm.py``, migration 085's
``geodata_osm_elements`` table.

Unit tier (no DB) covers validation and the pure geometry math
(``_bbox_for_element`` / ``_points_from_geometry`` / ``_bbox_literal``).
Integration tier (``@pytest.mark.integration``) proves the SQL against a
real connection: the upsert, the box ``&&`` bbox read, and idempotency —
none of which a mock can catch, since asyncpg's own type coercion for
``::box`` and jsonb is exactly what would silently break.
"""

from __future__ import annotations

from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.db_utils import unmanaged_pg_connection
from nce.vertical_modules.geodata.osm import (
    _bbox_for_element,
    _bbox_literal,
    _points_from_geometry,
    do_import_osm_elements,
    do_query_osm_elements,
)

# ---------------------------------------------------------------------------
# Pure-logic tests (no DB)
# ---------------------------------------------------------------------------


class _DummyEngine:
    pg_pool = None


def test_bbox_literal_shape() -> None:
    assert _bbox_literal(5.0, 58.0, 6.0, 59.0) == "((5.0,58.0),(6.0,59.0))"


def test_points_from_geometry_flattens_a_way() -> None:
    geometry = [{"lat": 58.9, "lon": 5.7}, {"lat": 58.95, "lon": 5.75}]
    assert _points_from_geometry(geometry) == [(5.7, 58.9), (5.75, 58.95)]


def test_points_from_geometry_walks_relation_members() -> None:
    geometry = {"members": [{"geometry": [{"lat": 1.0, "lon": 2.0}]}]}
    assert _points_from_geometry(geometry) == [(2.0, 1.0)]


def test_points_from_geometry_ignores_fields_with_no_coordinate() -> None:
    assert _points_from_geometry({"tags": {"building": "yes"}}) == []


def test_bbox_for_element_from_a_node_lat_lon() -> None:
    element = {"type": "node", "id": 1, "lat": 58.9, "lon": 5.7}
    assert _bbox_for_element(element) == (5.7, 58.9, 5.7, 58.9)


def test_bbox_for_element_from_a_way_geometry() -> None:
    element = {
        "type": "way",
        "id": 2,
        "geometry": [{"lat": 58.9, "lon": 5.7}, {"lat": 59.0, "lon": 5.8}],
    }
    assert _bbox_for_element(element) == (5.7, 58.9, 5.8, 59.0)


def test_bbox_for_element_with_no_coordinate_is_none_not_a_placeholder() -> None:
    assert _bbox_for_element({"type": "way", "id": 3, "tags": {"building": "yes"}}) is None


@pytest.mark.asyncio
async def test_import_rejects_missing_source_file() -> None:
    with pytest.raises(ValueError, match="'source_file' is required"):
        await do_import_osm_elements(_DummyEngine(), {"elements": []})


@pytest.mark.asyncio
async def test_import_rejects_missing_elements() -> None:
    with pytest.raises(ValueError, match="'elements' must be a list"):
        await do_import_osm_elements(_DummyEngine(), {"source_file": "norway-latest.osm.pbf"})


@pytest.mark.asyncio
async def test_import_rejects_an_element_with_an_invalid_type() -> None:
    with pytest.raises(ValueError, match="invalid type/id"):
        await do_import_osm_elements(
            _DummyEngine(),
            {"source_file": "x.pbf", "elements": [{"type": "county", "id": 1}]},
        )


@pytest.mark.asyncio
async def test_import_skips_elements_with_no_geometry_rather_than_writing_a_placeholder() -> None:
    """No DB call at all here: every element lacks a coordinate, so
    ``rows`` never becomes non-empty and the connection is never opened —
    this asserts that by using an engine whose pg_pool would blow up if
    touched.
    """
    result = await do_import_osm_elements(
        _DummyEngine(),
        {
            "source_file": "x.pbf",
            "elements": [
                {"type": "way", "id": 1, "tags": {"building": "yes"}},
                {"type": "node", "id": 2},
            ],
        },
    )
    assert result == {
        "ok": True,
        "source_file": "x.pbf",
        "received": 2,
        "written": 0,
        "skipped_no_geometry": 2,
    }


@pytest.mark.asyncio
async def test_query_rejects_missing_bbox_fields() -> None:
    with pytest.raises(ValueError, match="'min_lat' is required"):
        await do_query_osm_elements(
            _DummyEngine(), {"min_lon": 5.0, "max_lon": 6.0, "max_lat": 59.0}
        )


@pytest.mark.asyncio
async def test_query_rejects_min_greater_than_max() -> None:
    with pytest.raises(ValueError, match="min must not exceed max"):
        await do_query_osm_elements(
            _DummyEngine(),
            {"min_lon": 6.0, "min_lat": 58.0, "max_lon": 5.0, "max_lat": 59.0},
        )


@pytest.mark.asyncio
async def test_query_rejects_an_unknown_osm_type() -> None:
    with pytest.raises(ValueError, match="unknown osm_type"):
        await do_query_osm_elements(
            _DummyEngine(),
            {
                "min_lon": 5.0,
                "min_lat": 58.0,
                "max_lon": 6.0,
                "max_lat": 59.0,
                "osm_type": "county",
            },
        )


# ---------------------------------------------------------------------------
# Integration tests — real Postgres, the global table, no namespace at all.
# ---------------------------------------------------------------------------


class _PoolEngine:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pg_pool = pool


async def _clear_test_rows(pool: asyncpg.Pool, ids: list[int]) -> None:
    async with unmanaged_pg_connection(pool, site="geodata.osm.import") as conn:
        await conn.execute("DELETE FROM geodata_osm_elements WHERE osm_id = ANY($1::bigint[])", ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_then_query_roundtrips_through_real_postgres(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
) -> None:
    """Proves the SQL, not the Python: the box literal actually casts,
    the GiST `&&` operator actually matches, and jsonb round-trips."""
    engine = _PoolEngine(pg_pool)
    test_ids = [900000001, 900000002]
    await _clear_test_rows(pg_pool, test_ids)
    try:
        result = await do_import_osm_elements(
            engine,
            {
                "source_file": "test-fixture.osm.pbf",
                "elements": [
                    {
                        "type": "way",
                        "id": 900000001,
                        "tags": {"building": "yes"},
                        "geometry": [
                            {"lat": 58.90, "lon": 5.70},
                            {"lat": 58.91, "lon": 5.71},
                        ],
                    },
                    {
                        "type": "node",
                        "id": 900000002,
                        "tags": {"amenity": "bench"},
                        "lat": 10.0,
                        "lon": 10.0,
                    },
                ],
            },
        )
        assert result["written"] == 2
        assert result["skipped_no_geometry"] == 0

        # A viewport around the way, nowhere near the far-away node.
        query_result = await do_query_osm_elements(
            engine,
            {"min_lon": 5.5, "min_lat": 58.8, "max_lon": 5.9, "max_lat": 59.0},
        )
        found_ids = {e["id"] for e in query_result["elements"]}
        assert 900000001 in found_ids
        assert 900000002 not in found_ids
        matched = next(e for e in query_result["elements"] if e["id"] == 900000001)
        assert matched["type"] == "way"
        assert matched["tags"] == {"building": "yes"}
    finally:
        await _clear_test_rows(pg_pool, test_ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reimporting_the_same_element_updates_in_place_not_duplicates(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
) -> None:
    engine = _PoolEngine(pg_pool)
    test_ids = [900000003]
    await _clear_test_rows(pg_pool, test_ids)
    try:
        base_element: dict[str, Any] = {
            "type": "node",
            "id": 900000003,
            "tags": {"amenity": "bench"},
            "lat": 58.9,
            "lon": 5.7,
        }
        await do_import_osm_elements(
            engine, {"source_file": "run-1.osm.pbf", "elements": [base_element]}
        )
        updated_element = {**base_element, "tags": {"amenity": "bin"}}
        await do_import_osm_elements(
            engine, {"source_file": "run-2.osm.pbf", "elements": [updated_element]}
        )

        async with unmanaged_pg_connection(pg_pool, site="geodata.osm.bbox_query") as conn:
            rows = await conn.fetch(
                "SELECT tags, source_file FROM geodata_osm_elements WHERE osm_id = $1",
                900000003,
            )
        assert len(rows) == 1, "a re-import must update the row, never duplicate it"
        import json

        assert json.loads(rows[0]["tags"]) == {"amenity": "bin"}
        assert rows[0]["source_file"] == "run-2.osm.pbf"
    finally:
        await _clear_test_rows(pg_pool, test_ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_query_result_is_truncated_and_flagged_past_the_limit(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
) -> None:
    engine = _PoolEngine(pg_pool)
    test_ids = [900001000 + i for i in range(3)]
    await _clear_test_rows(pg_pool, test_ids)
    try:
        elements = [
            {"type": "node", "id": test_id, "lat": 58.9, "lon": 5.7} for test_id in test_ids
        ]
        await do_import_osm_elements(
            engine, {"source_file": "limit-test.osm.pbf", "elements": elements}
        )
        result = await do_query_osm_elements(
            engine,
            {"min_lon": 5.6, "min_lat": 58.8, "max_lon": 5.8, "max_lat": 59.0, "limit": 2},
        )
        assert result["count"] == 2
        assert result["truncated"] is True
    finally:
        await _clear_test_rows(pg_pool, test_ids)
