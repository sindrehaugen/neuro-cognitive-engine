"""
nce/vertical_modules/geodata/mcp_handlers.py
===============================================
MCP tool wrappers for the geodata local stores: the OSM element store
(Wave F-11), the N50 land-cover store (Wave F-12), and the place-name
nearest-point lookup (Wave F-13).

No ``require_namespace_id`` anywhere in this file, unlike every tenant
engine's handlers: every geodata table is GLOBAL (no ``namespace_id``
column at all) — the same reasoning as ``product_catalog``. A caller that
sends one is not refused for it; it is simply not read.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from nce.mcp_errors import mcp_handler
from nce.vertical_modules.geodata.n50 import do_import_n50_land_cover, do_query_n50_land_cover
from nce.vertical_modules.geodata.osm import do_import_osm_elements, do_query_osm_elements
from nce.vertical_modules.geodata.place_names import (
    do_import_place_names,
    do_query_nearest_place_name,
)

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine


@mcp_handler
async def handle_geodata_import_osm_elements(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: geodata_import_osm_elements — upsert a batch of
    already-parsed OSM elements into the local store (Operator/batch job).

    Requires ``source_file`` and ``elements`` (a list of Overpass-shaped
    element dicts). Thin adapter — all logic lives in
    :func:`do_import_osm_elements`.
    """
    result = await do_import_osm_elements(engine, dict(arguments))
    return json.dumps(result, default=str)


@mcp_handler
async def handle_geodata_query_osm_elements(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: geodata_query_osm_elements — read OSM elements whose
    stored bounding box intersects the requested viewport (Actor).

    Requires ``min_lon``, ``min_lat``, ``max_lon``, ``max_lat``. Thin
    adapter — all logic lives in :func:`do_query_osm_elements`.
    """
    result = await do_query_osm_elements(engine, dict(arguments))
    return json.dumps(result, default=str)


@mcp_handler
async def handle_geodata_import_n50_land_cover(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: geodata_import_n50_land_cover — upsert a batch of
    already-parsed N50 land-cover features into the local store
    (Operator/batch job).

    Requires ``source_file`` and ``features``. Thin adapter — all logic
    lives in :func:`do_import_n50_land_cover`.
    """
    result = await do_import_n50_land_cover(engine, dict(arguments))
    return json.dumps(result, default=str)


@mcp_handler
async def handle_geodata_query_n50_land_cover(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: geodata_query_n50_land_cover — read N50 land-cover
    features whose stored bounding box intersects the requested viewport
    (Actor).

    Requires ``min_lon``, ``min_lat``, ``max_lon``, ``max_lat``. Thin
    adapter — all logic lives in :func:`do_query_n50_land_cover`.
    """
    result = await do_query_n50_land_cover(engine, dict(arguments))
    return json.dumps(result, default=str)


@mcp_handler
async def handle_geodata_import_place_names(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: geodata_import_place_names — upsert a batch of
    already-resolved place names into the local store (Operator/batch job).

    Requires ``source_file`` and ``places``. Thin adapter — all logic
    lives in :func:`do_import_place_names`.
    """
    result = await do_import_place_names(engine, dict(arguments))
    return json.dumps(result, default=str)


@mcp_handler
async def handle_geodata_query_nearest_place_name(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: geodata_query_nearest_place_name — find the place
    name(s) nearest a given point (Actor).

    Requires ``lon`` and ``lat``. Thin adapter — all logic lives in
    :func:`do_query_nearest_place_name`.
    """
    result = await do_query_nearest_place_name(engine, dict(arguments))
    return json.dumps(result, default=str)
