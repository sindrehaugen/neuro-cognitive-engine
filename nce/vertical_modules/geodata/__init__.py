"""
nce.vertical_modules.geodata — local, offline-imported public geodata,
plus one live-read weather feed.

Not a tenant vertical engine: geodata (OpenStreetMap elements, N50 land
cover, place names, and weather conditions) describes the physical world,
not a tenant's data. Lives under ``vertical_modules`` per the MLV16
charter's own path for this module (§9 "Lane F"), but is never registered
in ``nce.engine_registry`` and carries no ``namespace_id`` anywhere — see
``osm.py``'s module docstring and migration 086 for the RLS reasoning.

``weather.py`` (Wave F-15) differs from its three siblings in one way
worth stating: it has no local store and no import step. OSM/N50/place
names are batch-imported snapshots read back by bbox/point; weather is a
live per-request read-through to MET Norway with an in-process TTL
cache, the same "vendor telemetry adapter" shape as Waves F-1..F-7, not
the local-store shape of this package's other three modules.

The charter's original wave list also named "coastline" and "roads" as
siblings here — filed as Q-42 instead of built: the host's own ADR 0048
and its road-network module show both are map-rendering-pipeline concerns (a
Douglas-Peucker LOD tile pyramid, and a third vendor NVDB chosen for a
zoom-band rendering rule) with no NCE-side consumer, not additional
FEED-shaped local stores like N50/OSM. See Q-42 and this lane's ledger
for the full reasoning.
"""

from __future__ import annotations
