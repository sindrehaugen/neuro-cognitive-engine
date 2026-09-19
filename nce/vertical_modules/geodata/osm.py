"""
nce/vertical_modules/geodata/osm.py
======================================
OSM local store: import + bounding-box read (MLV16 charter, Lane F,
Wave F-11 — the first of the geodata FEED module's waves).

Same shape decision as the host's own ADR 0046 ("Norway's OSM data local
in Postgres, not live against Overpass"): Overpass is a shared-quota
service, one backend is one sender for every tenant, and a live call per
read does not scale — so the data lives in ``geodata_osm_elements``
(migration 086) and is refreshed by a batch import, never fetched live
per request. Read for shape only (Q-25); this module's schema, functions
and tests are its own, not a port of the host's ``osm_lokal.py``.

What this wave does NOT do, stated rather than discovered
--------------------------------------------------------------
The host's own import script parses a
country-scale ``.osm.pbf`` extract (Norway alone: 8.6M elements, hours of
runtime, tens of GB) using ``osmium``, a dependency this codebase does
not carry. Adding it, and a binary PBF parser, is a bigger decision than
one wave should make unilaterally. ``do_import_osm_elements`` therefore
accepts ALREADY-PARSED elements in Overpass's own shape
(``{"type", "id", "tags", "geometry"}``) — the boundary the host's own
ADR draws in the opposite direction for a different reason (there, so one
parser serves two transports; here, so this module's contract does not
depend on which extraction tool produces the input). Turning a raw
``.osm.pbf`` into that shape is a separate, out-of-band step — the host's
own script is one example of how, not something this module re-executes.

Q-25 (AGPL / proprietary boundary)
--------------------------------------
The underlying OpenStreetMap data is public and ODbL-licensed regardless
of transport; nothing about it is the host's proprietary work. What was
read from the host for shape only was the ARCHITECTURAL DECISION (local
store instead of live Overpass, box+GiST instead of PostGIS) and the
storage shape it justifies — re-derived and re-implemented here with an
independent schema, never the host's SQL or code.

Attribution (ODbL, carried over from the host's own ADR): a caller
displaying this data must show OpenStreetMap attribution; this module
does not enforce that on a display it does not own.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from nce.db_utils import unmanaged_pg_connection

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.geodata.osm")

_VALID_OSM_TYPES = frozenset({"node", "way", "relation"})
_MAX_QUERY_LIMIT = 500


def _bbox_literal(min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> str:
    """Postgres ``box`` literal, ``((x1,y1),(x2,y2))`` — corners in either
    order; the type itself normalises to (lower-left, upper-right)."""
    return f"(({min_lon},{min_lat}),({max_lon},{max_lat}))"


def _points_from_geometry(geometry: Any) -> list[tuple[float, float]]:
    """Every (lon, lat) pair reachable from an Overpass-shaped ``geometry``
    value: a node's own ``{"lat", "lon"}``, or a way/relation's list of
    such points (a relation's members nest one level further; walked the
    same way rather than assumed absent).
    """
    points: list[tuple[float, float]] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            lat, lon = node.get("lat"), node.get("lon")
            if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                points.append((float(lon), float(lat)))
            for value in node.values():
                if isinstance(value, (dict, list)):
                    _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(geometry)
    return points


def _bbox_for_element(element: dict[str, Any]) -> tuple[float, float, float, float] | None:
    """The element's own bounding box, from its geometry — or ``None`` if
    it carries no coordinate at all (refused by the caller, never guessed
    at with a placeholder box)."""
    lat, lon = element.get("lat"), element.get("lon")
    points = _points_from_geometry(element.get("geometry"))
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        points.append((float(lon), float(lat)))
    if not points:
        return None
    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    return (min(lons), min(lats), max(lons), max(lats))


async def do_import_osm_elements(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Upsert a batch of already-parsed OSM elements into the local store.

    Parameters
    ----------
    params:
        ``{"source_file": str, "elements": [{"type", "id", "tags"?,
        "geometry"?, "lat"?, "lon"?}, ...]}``

    Global table (migration 086) — no ``namespace_id``, read via
    ``unmanaged_pg_connection`` rather than a tenant-scoped session.

    Returns
    -------
    dict
        ``{"ok": True, "source_file": str, "received": int, "written": int,
        "skipped_no_geometry": int}``. ``written`` counts rows genuinely
        inserted or updated; an element with no coordinate anywhere is
        counted in ``skipped_no_geometry`` and never written — a made-up
        box would be worse than a missing row, since it would answer a
        bbox query with an element that is not really there.
    """
    source_file = str(params.get("source_file") or "").strip()
    if not source_file:
        raise ValueError("do_import_osm_elements: 'source_file' is required")
    elements = params.get("elements")
    if not isinstance(elements, list):
        raise ValueError("do_import_osm_elements: 'elements' must be a list")

    rows: list[tuple[str, int, str, str, str]] = []
    skipped = 0
    for raw in elements:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        osm_type = str(raw.get("type") or "").strip()
        osm_id = raw.get("id")
        if osm_type not in _VALID_OSM_TYPES or not isinstance(osm_id, int):
            raise ValueError(
                f"do_import_osm_elements: element has an invalid type/id: {raw.get('type')!r}/"
                f"{raw.get('id')!r}"
            )
        bbox = _bbox_for_element(raw)
        if bbox is None:
            skipped += 1
            continue

        tags = raw.get("tags") if isinstance(raw.get("tags"), dict) else {}
        rows.append(
            (
                osm_type,
                osm_id,
                json.dumps(tags, sort_keys=True, default=str),
                json.dumps(
                    raw.get("geometry")
                    if raw.get("geometry") is not None
                    else {"lat": raw.get("lat"), "lon": raw.get("lon")},
                    sort_keys=True,
                    default=str,
                ),
                _bbox_literal(*bbox),
            )
        )

    written = 0
    if rows:
        async with unmanaged_pg_connection(engine.pg_pool, site="geodata.osm.import") as conn:
            # executemany's return isn't a trustworthy row count for an
            # upsert (asyncpg gives none worth relying on here), so
            # `written` counts what we asked it to write, not what it says
            # it did.
            #
            # $5::text::box, not $5::box: a bare ::box cast makes asyncpg infer
            # $5's type as box and reach for its BINARY box codec, which wants
            # a real Python box value, not the "((x1,y1),(x2,y2))" string
            # _bbox_literal builds -- caught only by the integration test
            # against real Postgres, never by a mock. The ::text cast first
            # keeps asyncpg on its trivial text codec; Postgres parses the
            # text as a box literal server-side, which is exactly the input
            # format box literals accept.
            await conn.executemany(
                """
                INSERT INTO geodata_osm_elements
                    (osm_type, osm_id, tags, geometry, bbox, source_file)
                VALUES ($1, $2, $3::jsonb, $4::jsonb, $5::text::box, $6)
                ON CONFLICT ON CONSTRAINT geodata_osm_elements_osm_uq DO UPDATE SET
                    tags = EXCLUDED.tags,
                    geometry = EXCLUDED.geometry,
                    bbox = EXCLUDED.bbox,
                    source_file = EXCLUDED.source_file,
                    imported_at = now()
                """,
                [(*row, source_file) for row in rows],
            )
            written = len(rows)

    log.info(
        "do_import_osm_elements source=%s received=%d written=%d skipped_no_geometry=%d",
        source_file,
        len(elements),
        written,
        skipped,
    )
    return {
        "ok": True,
        "source_file": source_file,
        "received": len(elements),
        "written": written,
        "skipped_no_geometry": skipped,
    }


async def do_query_osm_elements(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Bounding-box read: every element whose stored bbox intersects the
    requested viewport.

    Parameters
    ----------
    params:
        ``{"min_lon", "min_lat", "max_lon", "max_lat"}`` (all required
        floats), plus optional ``"osm_type"`` (one of node/way/relation)
        and ``"limit"`` (default/ceiling 500 — a bbox query with no cap
        is a way to ask for the whole country).

    Returns
    -------
    dict
        ``{"ok": True, "elements": [...], "count": int, "truncated": bool}``
        — Overpass-shaped elements (``type``/``id``/``tags``/``geometry``),
        the same envelope the stored rows were written in, so a caller
        already speaking that shape needs no translation.
    """
    for field_name in ("min_lon", "min_lat", "max_lon", "max_lat"):
        if not isinstance(params.get(field_name), (int, float)):
            raise ValueError(
                f"do_query_osm_elements: '{field_name}' is required and must be numeric"
            )
    min_lon, min_lat = float(params["min_lon"]), float(params["min_lat"])
    max_lon, max_lat = float(params["max_lon"]), float(params["max_lat"])
    if min_lon > max_lon or min_lat > max_lat:
        raise ValueError("do_query_osm_elements: min must not exceed max on either axis")

    osm_type = params.get("osm_type")
    if osm_type is not None and osm_type not in _VALID_OSM_TYPES:
        raise ValueError(f"do_query_osm_elements: unknown osm_type {osm_type!r}")

    limit = min(int(params.get("limit") or _MAX_QUERY_LIMIT), _MAX_QUERY_LIMIT)
    bbox = _bbox_literal(min_lon, min_lat, max_lon, max_lat)

    # $1::text::box, not $1::box -- same asyncpg box-codec trap as the INSERT
    # above in do_import_osm_elements.
    query = """
        SELECT osm_type, osm_id, tags, geometry
        FROM geodata_osm_elements
        WHERE bbox && $1::text::box
    """
    args: list[Any] = [bbox]
    if osm_type is not None:
        query += " AND osm_type = $2"
        args.append(osm_type)
    query += f" ORDER BY osm_type, osm_id LIMIT {limit + 1}"

    async with unmanaged_pg_connection(engine.pg_pool, site="geodata.osm.bbox_query") as conn:
        rows = await conn.fetch(query, *args)

    elements = [
        {
            "type": row["osm_type"],
            "id": row["osm_id"],
            "tags": json.loads(row["tags"]) if isinstance(row["tags"], str) else row["tags"],
            "geometry": json.loads(row["geometry"])
            if isinstance(row["geometry"], str)
            else row["geometry"],
        }
        for row in rows[:limit]
    ]
    return {
        "ok": True,
        "elements": elements,
        "count": len(elements),
        "truncated": len(rows) > limit,
    }
