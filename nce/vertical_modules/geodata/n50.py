"""
nce/vertical_modules/geodata/n50.py
======================================
N50 land-cover local store: import + bounding-box read (MLV16 charter,
Lane F, Wave F-12 — the second of the geodata FEED module's waves).

Same shape decision as the host's own ADR 0047 ("Kartverket's N50 land
cover for all of Norway, local in Postgres"), which is itself the same
decision as ADR 0046 (Wave F-11's OSM store): a live per-request call to
an external source does not scale, so the data lives in
``geodata_n50_land_cover`` (migration 089) and is refreshed by a
batch import. Read for shape only (Q-25); this module's schema,
functions and tests are its own, not a port of the host's N50
land-cover module.

No vendor inherits another's answer, checked rather than assumed
--------------------------------------------------------------------
Opened this ADR fresh rather than copying Wave F-11's assumptions, and
one thing genuinely differs: N50's source dump names each feature with
its OWN identifier (``objid``, unique within a class) — unlike Disruptive
(Wave F-4) or Neowit (Wave F-3), there is no absent-identifier honesty
flag needed here. What is the same as F-11: no PostGIS needed to READ
it (box + GiST), and the accepted input is already-parsed, not a raw
vendor extract — see below for why, which differs in its OWN reason
from F-11's.

What this wave does NOT do, stated rather than discovered
--------------------------------------------------------------
The host's own import reads the source as a ``pg_dump``-shaped text file
with polygon geometry encoded as EWKB hex, in UTM33 (EPSG:25833) —
requiring a byte-level EWKB parser (``struct``) AND a UTM-to-degrees
coordinate conversion before anything is usable. Both are format-specific
parsing concerns, not something this wave re-implements; ``rings`` here
must already be [lon, lat] DEGREES, and ``area_m2`` must already be
computed on the source's PROJECTED coordinates (an accurate square-metre
figure cannot be recovered from degrees alone — they are not equal-area).
Producing both from a raw N50 extract is a separate, out-of-band step.

Q-25 (AGPL / proprietary boundary)
--------------------------------------
Kartverket's N50 data is public and CC BY 4.0-licensed regardless of
transport; nothing about it is the host's proprietary work. What was read
from the host for shape only was the ARCHITECTURAL DECISION (local store,
box+GiST, per-class identity, largest-first truncation) — re-derived and
re-implemented here with an independent schema, never the host's SQL or
code.

Attribution (CC BY 4.0, carried over from the host's own ADR): a caller
displaying this data must show Kartverket attribution; this module does
not enforce that on a display it does not own.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from nce.db_utils import unmanaged_pg_connection

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.geodata.n50")

_MAX_QUERY_LIMIT = 500


def _bbox_literal(min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> str:
    return f"(({min_lon},{min_lat}),({max_lon},{max_lat}))"


def _bbox_for_rings(rings: Any) -> tuple[float, float, float, float] | None:
    """The feature's own bounding box, from its [lon, lat] ring(s) —
    ``None`` if no coordinate is present anywhere (refused by the caller,
    never guessed at with a placeholder box)."""
    lons: list[float] = []
    lats: list[float] = []

    def _walk(node: Any) -> None:
        if isinstance(node, list):
            if (
                len(node) == 2
                and isinstance(node[0], (int, float))
                and isinstance(node[1], (int, float))
            ):
                lons.append(float(node[0]))
                lats.append(float(node[1]))
                return
            for item in node:
                _walk(item)

    _walk(rings)
    if not lons:
        return None
    return (min(lons), min(lats), max(lons), max(lats))


async def do_import_n50_land_cover(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Upsert a batch of already-parsed N50 land-cover features into the
    local store.

    Parameters
    ----------
    params:
        ``{"source_file": str, "features": [{"klasse", "objid", "objtype"?,
        "rings", "area_m2"?}, ...]}`` — ``rings`` is a list of [lon, lat]
        ring(s) in degrees; ``area_m2`` is the source's own precomputed
        area (see the module docstring for why this module never computes
        it itself).

    Global table (migration 089) — no ``namespace_id``, read via
    ``unmanaged_pg_connection`` rather than a tenant-scoped session.

    Returns
    -------
    dict
        ``{"ok": True, "source_file": str, "received": int, "written": int,
        "skipped_no_geometry": int}``.
    """
    source_file = str(params.get("source_file") or "").strip()
    if not source_file:
        raise ValueError("do_import_n50_land_cover: 'source_file' is required")
    features = params.get("features")
    if not isinstance(features, list):
        raise ValueError("do_import_n50_land_cover: 'features' must be a list")

    rows: list[tuple[str, int, str | None, str, Any, str]] = []
    skipped = 0
    for raw in features:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        klasse = str(raw.get("klasse") or "").strip()
        objid = raw.get("objid")
        if not klasse or not isinstance(objid, int):
            raise ValueError(
                f"do_import_n50_land_cover: feature has an invalid klasse/objid: "
                f"{raw.get('klasse')!r}/{raw.get('objid')!r}"
            )
        bbox = _bbox_for_rings(raw.get("rings"))
        if bbox is None:
            skipped += 1
            continue

        objtype = raw.get("objtype")
        area_m2 = raw.get("area_m2")
        rows.append(
            (
                klasse,
                objid,
                str(objtype) if objtype is not None else None,
                json.dumps(raw.get("rings"), default=str),
                float(area_m2) if isinstance(area_m2, (int, float)) else None,
                _bbox_literal(*bbox),
            )
        )

    written = 0
    if rows:
        async with unmanaged_pg_connection(engine.pg_pool, site="geodata.n50.import") as conn:
            # $6::text::box, not $6::box: a bare ::box cast makes asyncpg infer
            # $6's type as box and reach for its binary box codec, which wants
            # a real Python box value, not the "((x1,y1),(x2,y2))" string
            # _bbox_literal builds -- caught on Wave F-11's identical pattern
            # by CI's real-Postgres integration job, never by a mock. The
            # ::text cast first keeps asyncpg on its trivial text codec;
            # Postgres parses the text as a box literal server-side, which is
            # exactly the input format box literals accept.
            await conn.executemany(
                """
                INSERT INTO geodata_n50_land_cover
                    (klasse, objid, objtype, rings, area_m2, bbox, source_file)
                VALUES ($1, $2, $3, $4::jsonb, $5, $6::text::box, $7)
                ON CONFLICT ON CONSTRAINT geodata_n50_land_cover_uq DO UPDATE SET
                    objtype = EXCLUDED.objtype,
                    rings = EXCLUDED.rings,
                    area_m2 = EXCLUDED.area_m2,
                    bbox = EXCLUDED.bbox,
                    source_file = EXCLUDED.source_file,
                    imported_at = now()
                """,
                [(*row, source_file) for row in rows],
            )
            written = len(rows)

    log.info(
        "do_import_n50_land_cover source=%s received=%d written=%d skipped_no_geometry=%d",
        source_file,
        len(features),
        written,
        skipped,
    )
    return {
        "ok": True,
        "source_file": source_file,
        "received": len(features),
        "written": written,
        "skipped_no_geometry": skipped,
    }


async def do_query_n50_land_cover(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Bounding-box read: every land-cover feature whose stored bbox
    intersects the requested viewport, largest area first.

    Parameters
    ----------
    params:
        ``{"min_lon", "min_lat", "max_lon", "max_lat"}`` (required
        floats), plus optional ``"klasse"`` (exact match) and ``"limit"``
        (default/ceiling 500).

    Returns
    -------
    dict
        ``{"ok": True, "features": [...], "count": int, "truncated": bool}``.
        Ordered by ``area_m2`` descending (ADR 0047's own truncation
        rule): when a read is capped, the large features survive — a lake
        never falls out in favour of many small bog holes.
    """
    for field_name in ("min_lon", "min_lat", "max_lon", "max_lat"):
        if not isinstance(params.get(field_name), (int, float)):
            raise ValueError(
                f"do_query_n50_land_cover: '{field_name}' is required and must be numeric"
            )
    min_lon, min_lat = float(params["min_lon"]), float(params["min_lat"])
    max_lon, max_lat = float(params["max_lon"]), float(params["max_lat"])
    if min_lon > max_lon or min_lat > max_lat:
        raise ValueError("do_query_n50_land_cover: min must not exceed max on either axis")

    klasse = params.get("klasse")
    limit = min(int(params.get("limit") or _MAX_QUERY_LIMIT), _MAX_QUERY_LIMIT)
    bbox = _bbox_literal(min_lon, min_lat, max_lon, max_lat)

    # $1::text::box, not $1::box -- same asyncpg box-codec trap as the INSERT
    # above in do_import_n50_land_cover.
    query = """
        SELECT klasse, objid, objtype, rings, area_m2
        FROM geodata_n50_land_cover
        WHERE bbox && $1::text::box
    """
    args: list[Any] = [bbox]
    if klasse is not None:
        query += " AND klasse = $2"
        args.append(str(klasse))
    query += f" ORDER BY area_m2 DESC NULLS LAST, objid LIMIT {limit + 1}"

    async with unmanaged_pg_connection(engine.pg_pool, site="geodata.n50.bbox_query") as conn:
        rows = await conn.fetch(query, *args)

    features = [
        {
            "klasse": row["klasse"],
            "objid": row["objid"],
            "objtype": row["objtype"],
            "rings": json.loads(row["rings"]) if isinstance(row["rings"], str) else row["rings"],
            "area_m2": float(row["area_m2"]) if row["area_m2"] is not None else None,
        }
        for row in rows[:limit]
    ]
    return {
        "ok": True,
        "features": features,
        "count": len(features),
        "truncated": len(rows) > limit,
    }
