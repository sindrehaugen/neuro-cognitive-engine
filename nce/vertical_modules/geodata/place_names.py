"""
nce/vertical_modules/geodata/place_names.py
===============================================
Place-name nearest-point lookup (MLV16 charter, Lane F, Wave F-13 — the
third of the geodata FEED module's waves).

Grounded in the host's own ADR ("Kartverket's place names local in
Postgres") for shape only (Q-25): a batch-imported local mirror instead of
a live per-request call to the source registry, the same reasoning as
Waves F-11/F-12. This module's schema, functions and tests are its own,
not a port of the host's ``stedsnavn.py``.

A genuinely different read shape from F-11/F-12, checked rather than
assumed
--------------------------------------------------------------------
F-11 (OSM elements) and F-12 (N50 land cover) both answer "what
intersects this viewport?" — a bounding-box read. Place names answer a
different question: "what named place is closest to this point?" — a
nearest-neighbour lookup. Reusing the box+GiST shape here would be the
wrong tool: a bounding box says nothing about what's INSIDE it (the
host's own ADR measured this directly — an unfiltered nearest-BOX
answer picked `Glåma`, the Glomma river's bounding box, for a query
point in downtown Oslo, because that box happens to cover half of
eastern Norway). The right index for "nearest point" is a GiST index on
a POINT column ordered by the `<->` operator, which is core Postgres
(no PostGIS needed here either).

Two-stage lookup, not one, and the reason is measured, not stylistic
--------------------------------------------------------------------
`<->` on a geographic (lon, lat) point orders candidates by DEGREE
distance. Degrees of longitude are not a fixed real-world distance —
they shrink toward the poles — so degree-distance and metre-distance
only agree exactly at the equator. This module therefore takes the
GiST index's cheapest ordering as a CANDIDATE set only, then re-ranks
that candidate set by actual great-circle distance (haversine) in
Python before returning the true nearest. This mirrors the shape the
host's own ADR describes taking, independently re-derived and
re-implemented here, not copied.

One anchor point per place, not the source geometry
--------------------------------------------------------------------
A place in the source registry can be recorded as a point, a point
cluster, a line, or an area. This module's contract is a single (lon,
lat) anchor per place — the caller (an out-of-band import step, not
this module) resolves whatever shape the source gives it down to one
representative point before calling this tool. Storing every recorded
shape verbatim was the host's own explicitly rejected alternative: tens
of millions of rows for a marginally more precise answer nothing here
needs.

Q-25 (AGPL / proprietary boundary)
--------------------------------------
Norway's place-name register is public sector data; nothing about it is
the host's proprietary work. What was read from the host for shape only
was the ARCHITECTURAL DECISION (local mirror, point+GiST nearest-lookup,
one-anchor-per-place, re-rank in metres) — re-derived and re-implemented
here with an independent schema, never the host's SQL or code.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any

from nce.db_utils import unmanaged_pg_connection

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.geodata.place_names")

_MAX_CANDIDATES = 50
_MAX_RESULT_LIMIT = 20
_EARTH_RADIUS_M = 6_371_000.0


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle distance in metres between two (lon, lat) points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def _point_literal(lon: float, lat: float) -> str:
    return f"({lon},{lat})"


async def do_import_place_names(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Upsert a batch of already-resolved place names into the local store.

    Parameters
    ----------
    params:
        ``{"source_file": str, "places": [{"external_id", "navn",
        "kategori"?, "sprak"?, "lon", "lat"}, ...]}`` — ``external_id`` is
        the source registry's own identifier for the place, ``lon``/``lat``
        its single resolved anchor point.

    Global table (migration 090) — no ``namespace_id``, read via
    ``unmanaged_pg_connection`` rather than a tenant-scoped session.

    Returns
    -------
    dict
        ``{"ok": True, "source_file": str, "received": int, "written": int,
        "skipped_invalid": int}``. A place missing ``external_id``,
        ``navn``, or a numeric ``lon``/``lat`` is counted in
        ``skipped_invalid`` and never written — a made-up anchor point
        would be worse than a missing row.
    """
    source_file = str(params.get("source_file") or "").strip()
    if not source_file:
        raise ValueError("do_import_place_names: 'source_file' is required")
    places = params.get("places")
    if not isinstance(places, list):
        raise ValueError("do_import_place_names: 'places' must be a list")

    rows: list[tuple[str, str, str | None, str | None, str, str]] = []
    skipped = 0
    for raw in places:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        external_id = str(raw.get("external_id") or "").strip()
        navn = str(raw.get("navn") or "").strip()
        lon, lat = raw.get("lon"), raw.get("lat")
        if (
            not external_id
            or not navn
            or not isinstance(lon, (int, float))
            or not isinstance(lat, (int, float))
        ):
            skipped += 1
            continue

        kategori = raw.get("kategori")
        sprak = raw.get("sprak")
        rows.append(
            (
                external_id,
                navn,
                str(kategori) if kategori is not None else None,
                str(sprak) if sprak is not None else None,
                _point_literal(float(lon), float(lat)),
                source_file,
            )
        )

    written = 0
    if rows:
        async with unmanaged_pg_connection(
            engine.pg_pool, site="geodata.place_names.import"
        ) as conn:
            await conn.executemany(
                """
                INSERT INTO geodata_place_names
                    (external_id, navn, kategori, sprak, point, source_file)
                VALUES ($1, $2, $3, $4, $5::text::point, $6)
                ON CONFLICT ON CONSTRAINT geodata_place_names_external_id_uq DO UPDATE SET
                    navn = EXCLUDED.navn,
                    kategori = EXCLUDED.kategori,
                    sprak = EXCLUDED.sprak,
                    point = EXCLUDED.point,
                    source_file = EXCLUDED.source_file,
                    imported_at = now()
                """,
                rows,
            )
            written = len(rows)

    log.info(
        "do_import_place_names source=%s received=%d written=%d skipped_invalid=%d",
        source_file,
        len(places),
        written,
        skipped,
    )
    return {
        "ok": True,
        "source_file": source_file,
        "received": len(places),
        "written": written,
        "skipped_invalid": skipped,
    }


async def do_query_nearest_place_name(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Find the place name(s) nearest a given point.

    Parameters
    ----------
    params:
        ``{"lon": float, "lat": float}`` (required), plus optional
        ``"kategori"`` (exact-match filter) and ``"limit"`` (default 1,
        ceiling 20).

    Two-stage lookup (see module docstring): the GiST ``<->`` operator
    orders up to 50 candidates by degree distance (cheap, approximate),
    then this function re-ranks those candidates by real great-circle
    distance in metres and returns the true nearest ``limit`` of them —
    never trusting degree-order directly, since it is not proportional to
    real distance except at the equator.

    Returns
    -------
    dict
        ``{"ok": True, "places": [{"external_id", "navn", "kategori",
        "sprak", "lon", "lat", "distance_m"}, ...], "count": int}``,
        ordered nearest first.
    """
    lon, lat = params.get("lon"), params.get("lat")
    if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
        raise ValueError("do_query_nearest_place_name: 'lon' and 'lat' are required and numeric")
    lon, lat = float(lon), float(lat)

    kategori = params.get("kategori")
    limit = min(max(int(params.get("limit") or 1), 1), _MAX_RESULT_LIMIT)
    point = _point_literal(lon, lat)

    query = """
        SELECT external_id, navn, kategori, sprak, point[0] AS p_lon, point[1] AS p_lat
        FROM geodata_place_names
    """
    args: list[Any] = [point]
    if kategori is not None:
        query += " WHERE kategori = $2"
        args.append(str(kategori))
    query += f" ORDER BY point <-> $1::text::point LIMIT {_MAX_CANDIDATES}"

    async with unmanaged_pg_connection(engine.pg_pool, site="geodata.place_names.nearest") as conn:
        rows = await conn.fetch(query, *args)

    ranked = sorted(
        (
            {
                "external_id": row["external_id"],
                "navn": row["navn"],
                "kategori": row["kategori"],
                "sprak": row["sprak"],
                "lon": row["p_lon"],
                "lat": row["p_lat"],
                "distance_m": _haversine_m(lon, lat, row["p_lon"], row["p_lat"]),
            }
            for row in rows
        ),
        key=lambda p: p["distance_m"],
    )[:limit]

    return {
        "ok": True,
        "places": ranked,
        "count": len(ranked),
    }
