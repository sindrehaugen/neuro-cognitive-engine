"""nce.vertical_modules.project.resources — C12 resource declaration for PROJECT_PROJECT.

Wave C-6 (2026-09-20): closes the PROJECT_PROJECT exemption
(nce/resource_surface/exemptions.py).

Deliberately thin, not a limitation to apologise for
------------------------------------------------------
kg_nodes has no attribute storage of its own -- confirmed empty for every
PROJECT_* node type (case_study.py inserts only (label, entity_type,
namespace_id), ever; tasks.py's own comment: "the graph has no free-text
status column on kg_nodes") -- filed as Q-47, still open with Sindre. So
this spec exposes exactly what a kg_nodes-primary spec with no secondary
tables can: identity (label, entity_type, change_origin, timestamps),
list/get/create/patch, and events via the generic C12 surface. It does
NOT expose phase gates, capacity, my-day, or reports -- those remain
served by their own hand-written, already-working routes
(nce/admin_handlers/project.py) and are not replaced or duplicated here.
The exemption this closes previously described "phase gates and capacity
metadata" as if a generic ResourceSpec would expose them; it never could,
since none of that is queryable via kg_nodes -- the same shape as the
E-unblock miscalculation from earlier tonight, corrected here rather than
carried forward silently.
"""

from __future__ import annotations

from nce.resource_surface import register_resource
from nce.resource_surface.spec import ResourceSpec

PROJECT_SPEC = ResourceSpec(
    engine="project",
    entity="projects",
    node_type="PROJECT_PROJECT",
    table_name=None,
    id_field="label",
    version_field="updated_at",
    soft_delete_field=None,
    filterable_fields=("change_origin",),
    writable_fields=("change_origin",),
    description=(
        "Identity only: label, entity_type, change_origin, timestamps. "
        "kg_nodes has no attribute storage of its own and none exists for "
        "PROJECT_PROJECT elsewhere either (Q-47, open) -- no project data "
        "(name, value, dates, status) is queryable through this surface. "
        "Phase gates, capacity, my-day, and reports are served by "
        "nce/admin_handlers/project.py's own routes, not by this generic "
        "surface, and are not replaced or duplicated by registering this spec."
    ),
)

register_resource(PROJECT_SPEC)
