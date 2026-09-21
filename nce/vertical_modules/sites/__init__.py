"""nce.vertical_modules.sites — C17 Site Master Data Vertical Module.

Phase A Wave A-9:
Master register for physical sites and buildings with cadastre identity,
validated addresses, coordinates, footprint geometry, and vessel telemetry streams.

MLV16 charter, Lane F, Wave F-9:
``address_registry.py`` enriches an existing site's own ``address``/
``latitude``/``longitude`` fields from Kartverket's public Adresse API —
no new table, the same reasoning as the C15 BRREG feed (F-8) writing into
``legal_entities.metadata`` rather than a store of its own.
"""

from __future__ import annotations

from nce.vertical_modules.sites.address_registry import do_enrich_site_from_address_registry
from nce.vertical_modules.sites.models import (
    SiteAddress,
    SiteCreate,
    SiteItem,
    SiteUpdate,
)
from nce.vertical_modules.sites.resources import SITE_SPEC
from nce.vertical_modules.sites.service import (
    get_site,
    normalize_cadastre_id,
    update_site,
)

__all__ = [
    "SITE_SPEC",
    "SiteAddress",
    "SiteCreate",
    "SiteItem",
    "SiteUpdate",
    "do_enrich_site_from_address_registry",
    "get_site",
    "normalize_cadastre_id",
    "update_site",
]
