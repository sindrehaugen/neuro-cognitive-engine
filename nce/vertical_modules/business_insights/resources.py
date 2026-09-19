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
-- resource_surface has no hook for any of it (the systemic gap ML-orch
quantified: 18 of 30 declared C12 specs are on guarded engines, none
enforced; routed to H as a follow-on to #284). This wave's single spec is
read-mostly (a cached snapshot; no PATCH-worthy business logic sits behind
it), which narrows but does not eliminate the exposure once #284 opens the
generic write path.

No REST route collision: the existing business-insights routes
(/morning-brief, /risk-radar, /run-scenario, /board-pack, /kpi-dashboard,
/ask) are all different paths from what BUSINESS_INSIGHTS_KPI_SNAPSHOT's
C12 surface generates (kpi-snapshots).
"""

from __future__ import annotations

from nce.resource_surface import register_resource
from nce.resource_surface.spec import ResourceSpec

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
    description=(
        "C12 cached point-in-time KPI roll-ups and trend history; no updated_at column, "
        "so no version_field -- rows are point-in-time snapshots, not mutated in place."
    ),
)
register_resource(BUSINESS_INSIGHTS_KPI_SNAPSHOT_SPEC)

__all__ = [
    "BUSINESS_INSIGHTS_KPI_SNAPSHOT_SPEC",
]
