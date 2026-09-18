"""
nce.vertical_modules.geodata — local, offline-imported public geodata.

Not a tenant vertical engine: geodata (OpenStreetMap elements, and their
siblings named in the charter — N50 land cover, coastline, roads, place
names) describes the physical world, not a tenant's data. Lives under
``vertical_modules`` per the MLV16 charter's own path for this module
(§9 "Lane F"), but is never registered in ``nce.engine_registry`` and
carries no ``namespace_id`` anywhere — see ``osm.py``'s module docstring
and migration 085 for the RLS reasoning.
"""

from __future__ import annotations
