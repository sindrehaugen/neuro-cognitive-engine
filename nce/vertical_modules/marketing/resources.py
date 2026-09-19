"""nce.vertical_modules.marketing.resources — Resource definitions for Marketing Engine.

Lane E Wave E-15 (Q-32 unblocked hr/marketing/customer_portal/business_insights):
Registers C12 ResourceSpec instances for Marketing's three owned node types, all
real, tenant-scoped, single-table DDL:
  - CASE_STUDY (case_studies table)
  - TESTIMONIAL (testimonials table)
  - CONTENT_ASSET (content_assets table)

None of the three node types had any prior kg_nodes/entity_type usage anywhere
in the codebase (grep -rn "CASE_STUDY\|TESTIMONIAL\|CONTENT_ASSET"
nce/vertical_modules/marketing/ -> no matches); names chosen to match their
tables, not invented beyond that.

REST route shadowing, documented not fixed (same class as hr's, PR #287):
admin_handlers/marketing.py already registers GET /api/marketing/testimonials
(api_marketing_testimonials -- filterable list with pagination, gated by
require_marketing_enabled, an opt-in-per-namespace check the generic C12
list/write paths have no hook for). That single (path, method) pair collides
with C12's generated TESTIMONIAL list route; Starlette's registration-order
matching keeps the hand-written handler authoritative there, so the C12
route is inert for that one pair, not broken. No other marketing route
collides: /api/marketing/draft, /testimonials/capture, /testimonials/retract,
/assets, /suggest-content, /audit-seo, /approve, /publish are all different
paths from what CASE_STUDY/TESTIMONIAL/CONTENT_ASSET's C12 surface generates
(case-studies, testimonials, content-assets).

enabled_guard=require_marketing_enabled (#294/#296): every generated
route/tool for these three specs now enforces the same per-namespace
opt-in check the hand-written routes already did, at the same boundary
(before any DB access), matching MarketingDisabledError's existing
translation path (already a subclass of EngineDisabledError, so #294's
generic handling applies with zero changes to _guard.py). #296 makes
this a hard CI failure (tests/unit/test_engine_guard_ratchet.py) for
any spec on an engine with a _guard.py, not just a reminder.
"""

from __future__ import annotations

from nce.resource_surface import register_resource
from nce.resource_surface.spec import ResourceSpec
from nce.vertical_modules.marketing._guard import require_marketing_enabled

# ---------------------------------------------------------------------------
# 1. CASE_STUDY
# ---------------------------------------------------------------------------
CASE_STUDY_SPEC = ResourceSpec(
    engine="marketing",
    entity="case_studies",
    node_type="CASE_STUDY",
    table_name="case_studies",
    id_field="id",
    version_field="updated_at",
    enabled_guard=require_marketing_enabled,
    soft_delete_field=None,
    filterable_fields=("status", "project_id", "anonymized"),
    searchable_fields=("title", "body"),
    writable_fields=(
        "project_id",
        "title",
        "body",
        "status",
        "anonymized",
        "approver",
        "approved_at",
        "marketing_source_id",
        "raw",
    ),
    description="C12 drafted, approved, and published customer success stories.",
)
register_resource(CASE_STUDY_SPEC)

# ---------------------------------------------------------------------------
# 2. TESTIMONIAL
# ---------------------------------------------------------------------------
TESTIMONIAL_SPEC = ResourceSpec(
    engine="marketing",
    entity="testimonials",
    node_type="TESTIMONIAL",
    table_name="testimonials",
    id_field="id",
    version_field="updated_at",
    enabled_guard=require_marketing_enabled,
    soft_delete_field=None,
    filterable_fields=("status", "customer_id", "project_id", "consent_tier"),
    searchable_fields=("quote",),
    writable_fields=(
        "customer_id",
        "project_id",
        "quote",
        "status",
        "consent",
        "consent_tier",
        "consent_scope",
        "consent_recorded_at",
        "nps_at_capture",
        "marketing_source_id",
    ),
    description="C12 customer testimonial quotes with structured consent tiers and NPS capture.",
)
register_resource(TESTIMONIAL_SPEC)

# ---------------------------------------------------------------------------
# 3. CONTENT_ASSET
# ---------------------------------------------------------------------------
CONTENT_ASSET_SPEC = ResourceSpec(
    engine="marketing",
    entity="content_assets",
    node_type="CONTENT_ASSET",
    table_name="content_assets",
    id_field="id",
    version_field="updated_at",
    enabled_guard=require_marketing_enabled,
    soft_delete_field=None,
    filterable_fields=("kind", "status"),
    searchable_fields=("title",),
    writable_fields=(
        "kind",
        "ref_id",
        "title",
        "seo",
        "storage_uri",
        "status",
        "marketing_source_id",
    ),
    description="C12 marketing content assets with AEO/GEO metadata and MinIO storage references.",
)
register_resource(CONTENT_ASSET_SPEC)

__all__ = [
    "CASE_STUDY_SPEC",
    "TESTIMONIAL_SPEC",
    "CONTENT_ASSET_SPEC",
]
