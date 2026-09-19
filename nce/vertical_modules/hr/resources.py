"""nce.vertical_modules.hr.resources — Resource definitions for HR Engine.

Lane E Wave E-14 (Q-32 unblocked hr/marketing/customer_portal/business_insights):
Registers C12 ResourceSpec instances for HR's four owned node types, all real,
tenant-scoped, single-table DDL, FK-linked to employees:
  - EMPLOYEE (employees table)
  - SKILL (skills table)
  - CERTIFICATION (certifications table)
  - ABSENCE (absences table)

REST route shadowing (documented, not a defect to fix here): admin_handlers/hr.py
already registers GET/POST /api/hr/employees, GET /api/hr/employees/{id},
GET/POST /api/hr/absences, and POST /api/hr/skills, each doing real work beyond
generic CRUD -- do_get_employee composes skills + active certs with role-based
field redaction. None of these hand-written routes are retired: none is
generic CRUD. Starlette matches routes in registration order and the
hand-written ones are registered before build_all_resource_routes() in
admin_app.py, so for the exact (path, method) pairs that collide --
GET/POST /api/hr/employees, GET /api/hr/employees/{id}, GET/POST /api/hr/absences,
POST /api/hr/skills -- the hand-written handler stays authoritative and the
C12-generated route is inert (never reached), not broken. Every other
C12-generated route (PATCH, archive, restore, bulk, events, comments, tags,
documents, and all MCP tool twins) is genuinely new capability with no
collision. Flagged to ML-orch as a new failure-mode class (REST-route
shadowing) distinct from the TOOL_REGISTRY name-collision class K-H4/K-H5
already guard -- no existing ratchet detects it.

enabled_guard=require_hr_enabled (Wave A-9-H / #294, landed after this wave's
initial declaration): every generated route/tool for these four specs now
enforces the same per-namespace opt-in check the hand-written routes already
did, at the same boundary (before any DB access), matching HrDisabledError's
existing translation path (already a subclass of EngineDisabledError, so
#294's generic handling applies with zero changes to _guard.py).
"""

from __future__ import annotations

from nce.resource_surface import register_resource
from nce.resource_surface.spec import ResourceSpec
from nce.vertical_modules.hr._guard import require_hr_enabled

# ---------------------------------------------------------------------------
# 1. EMPLOYEE
# ---------------------------------------------------------------------------
EMPLOYEE_SPEC = ResourceSpec(
    engine="hr",
    entity="employees",
    node_type="EMPLOYEE",
    table_name="employees",
    id_field="id",
    version_field="updated_at",
    enabled_guard=require_hr_enabled,
    soft_delete_field=None,
    filterable_fields=("department", "role", "location_id", "active"),
    searchable_fields=("name", "email", "employee_id"),
    writable_fields=(
        "employee_id",
        "name",
        "email",
        "role",
        "department",
        "location_id",
        "leave_balance",
        "active",
        "hr_source_id",
        "raw",
    ),
    description="C12 employee profile cards with department, role, and leave balance.",
)
register_resource(EMPLOYEE_SPEC)

# ---------------------------------------------------------------------------
# 2. SKILL
# ---------------------------------------------------------------------------
SKILL_SPEC = ResourceSpec(
    engine="hr",
    entity="skills",
    node_type="SKILL",
    table_name="skills",
    id_field="id",
    version_field="updated_at",
    enabled_guard=require_hr_enabled,
    soft_delete_field=None,
    filterable_fields=("employee_id", "category", "level"),
    searchable_fields=("name",),
    writable_fields=(
        "skill_id",
        "employee_id",
        "name",
        "category",
        "level",
        "assessed_at",
        "hr_source_id",
        "raw",
    ),
    description="C12 employee skill assessments with category and proficiency level.",
)
register_resource(SKILL_SPEC)

# ---------------------------------------------------------------------------
# 3. CERTIFICATION
# ---------------------------------------------------------------------------
CERTIFICATION_SPEC = ResourceSpec(
    engine="hr",
    entity="certifications",
    node_type="CERTIFICATION",
    table_name="certifications",
    id_field="id",
    version_field="updated_at",
    enabled_guard=require_hr_enabled,
    soft_delete_field=None,
    filterable_fields=("employee_id", "status", "valid_to"),
    searchable_fields=("name", "authority"),
    writable_fields=(
        "cert_id",
        "employee_id",
        "authority",
        "name",
        "issued",
        "valid_to",
        "status",
        "hr_source_id",
        "raw",
    ),
    description="C12 employee certification records with issuing authority and validity window.",
)
register_resource(CERTIFICATION_SPEC)

# ---------------------------------------------------------------------------
# 4. ABSENCE
# ---------------------------------------------------------------------------
ABSENCE_SPEC = ResourceSpec(
    engine="hr",
    entity="absences",
    node_type="ABSENCE",
    table_name="absences",
    id_field="id",
    version_field="updated_at",
    enabled_guard=require_hr_enabled,
    soft_delete_field=None,
    filterable_fields=("employee_id", "type", "status", "compliance_state"),
    searchable_fields=("reason",),
    writable_fields=(
        "absence_id",
        "employee_id",
        "type",
        "start_date",
        "end_date",
        "days",
        "reason",
        "status",
        "compliance_state",
        "hr_source_id",
        "raw",
    ),
    description="C12 employee absence/leave records with compliance state tracking.",
)
register_resource(ABSENCE_SPEC)

__all__ = [
    "EMPLOYEE_SPEC",
    "SKILL_SPEC",
    "CERTIFICATION_SPEC",
    "ABSENCE_SPEC",
]
