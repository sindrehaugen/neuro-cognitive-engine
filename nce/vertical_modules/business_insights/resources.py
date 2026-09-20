"""nce.vertical_modules.business_insights.resources — Resource definitions for Business Insights Engine.

Lane E Wave E-16 (Q-32 unblocked hr/marketing/customer_portal/business_insights):
Registers C12 ResourceSpec for the ONE of Business Insights' four documented node
types that has a real, tenant-scoped, single-table DDL:
  - BUSINESS_INSIGHTS_KPI_SNAPSHOT (business_insights_kpi_snapshots table)

BUSINESS_INSIGHTS_BRIEFING, BUSINESS_INSIGHTS_FINDING, and
BUSINESS_INSIGHTS_SCENARIO stay exempted -- worse than a kg_nodes-only stub.
provenance.py documents all four as this engine's "graph contract" node types,
but grep -rn "INSERT INTO kg_nodes" nce/vertical_modules/business_insights/
has zero matches: brief.py/radar.py/scenario.py each build their node's dict
shape in memory and return it directly in the HTTP response payload, never
persisting it anywhere, not even as a kg_nodes row. No ResourceSpec is
possible for these three today; a future wave that adds real persistence
would need to declare them then.

Guarded engine: business_insights has an unusually involved guard
(business_insights/_guard.py) enforcing BI-1..BI-4 (EU AI Act Article 5
person-ranking barrier, confidence/coverage verification, third-party AI
egress boundary, and namespace opt-in via metadata.business_insights.enabled)
-- and the namespace opt-in half of that IS now enforced: #294 added
``ResourceSpec.enabled_guard``, called at the top of every generated REST route
and MCP handler (reads included) before any DB access, and #296 makes a missing
one a hard CI failure for any engine carrying a ``_guard.py``. This spec wires
``require_business_insights_enabled`` accordingly.

An earlier revision of this docstring said resource_surface had "no hook for any
of it" -- true when written, false since #294; corrected rather than left, since
the next reader would act on it. What genuinely has no hook is the rest of
BI-1..BI-3: the EU AI Act Article 5 person-ranking barrier, confidence/coverage
verification, and the third-party AI egress boundary are enforced only at this
engine's hand-written boundaries, NOT on the generated C12 surface. This wave's
single spec is read-mostly (a cached point-in-time snapshot), which narrows that
residue but does not eliminate it.

No REST route collision: the existing business-insights routes
(/morning-brief, /risk-radar, /run-scenario, /board-pack, /kpi-dashboard,
/ask) are all different paths from what BUSINESS_INSIGHTS_KPI_SNAPSHOT's
C12 surface generates (kpi-snapshots).
"""

from __future__ import annotations

from nce.resource_surface import register_resource
from nce.resource_surface.spec import ResourceSpec
from nce.vertical_modules.business_insights._guard import (
    require_business_insights_enabled,
)

# ---------------------------------------------------------------------------
# 1. BUSINESS_INSIGHTS_KPI_SNAPSHOT
# ---------------------------------------------------------------------------
BUSINESS_INSIGHTS_KPI_SNAPSHOT_SPEC = ResourceSpec(
    engine="business_insights",
    entity="kpi_snapshots",
    node_type="BUSINESS_INSIGHTS_KPI_SNAPSHOT",
    table_name="business_insights_kpi_snapshots",
    id_field="id",
    version_field=None,
    soft_delete_field=None,
    # C12 opt-in enforcement (#294/#296): the generated surface calls this at
    # every route and handler boundary before touching the DB. Must be the
    # namespace opt-in check -- NOT require_insights_role, which is an
    # authorisation decision the generated surface has no principal to make.
    enabled_guard=require_business_insights_enabled,
    filterable_fields=("kpi_key", "period", "source_engine"),
    searchable_fields=("kpi_key",),
    writable_fields=(
        "kpi_key",
        "value",
        "period",
        "captured_at",
        "source_engine",
        "business_insights_source_id",
        "raw",
    ),
    # archive wave (2026-09-20): soft_delete_field=None falls back to a
    # literal "is_archived" column (rest.py:524/1174/1270) that
    # business_insights_kpi_snapshots does not have -- no alternate
    # soft-delete-shaped column and no hand-written archive path in this
    # engine. Point-in-time snapshots (this spec's own description) are not
    # a thing that gets "archived" in place anyway. See
    # _internal/work-docs/mlv16-orchestration/ARCHIVE_COLUMN_SWEEP.md.
    excluded_verbs=frozenset({"archive"}),
    description=(
        "C12 cached point-in-time KPI roll-ups and trend history; no updated_at column, "
        "so no version_field -- rows are point-in-time snapshots, not mutated in place."
    ),
)
register_resource(BUSINESS_INSIGHTS_KPI_SNAPSHOT_SPEC)

__all__ = [
    "BUSINESS_INSIGHTS_KPI_SNAPSHOT_SPEC",
]
