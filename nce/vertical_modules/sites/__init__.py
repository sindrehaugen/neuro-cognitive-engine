"""nce.vertical_modules.sites — C17 Site Master Data Vertical Module.

Phase A Wave A-9:
Master register for physical sites and buildings with cadastre identity,
validated addresses, coordinates, footprint geometry, and vessel telemetry streams.
"""

from __future__ import annotations

from nce.vertical_modules.sites.models import (
    SiteAddress,
    SiteCreate,
    SiteItem,
    SiteUpdate,
)
from nce.vertical_modules.sites.resources import SITE_SPEC
from nce.vertical_modules.sites.service import (
    archive_site,
    get_site,
    get_site_by_cadastre_id,
    list_sites,
    normalize_cadastre_id,
    register_site,
    update_site,
    update_vessel_telemetry,
)

__all__ = [
    "SITE_SPEC",
    "SiteAddress",
    "SiteCreate",
    "SiteItem",
    "SiteUpdate",
    "archive_site",
    "get_site",
    "get_site_by_cadastre_id",
    "list_sites",
    "normalize_cadastre_id",
    "register_site",
    "update_site",
    "update_vessel_telemetry",
]
