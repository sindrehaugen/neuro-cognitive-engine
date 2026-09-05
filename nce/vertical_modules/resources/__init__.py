"""
Module 15: Staff & Resources Engine (nce/vertical_modules/resources).
The capacity + scheduling brain coordinating people, contractors, vehicles, and tools.
"""

from __future__ import annotations

from nce.vertical_modules.resources._guard import (
    ResourceConcurrencyError,
    ResourceNotFoundError,
    ResourcesDisabledError,
    ResourcesError,
    ResourceValidationError,
    require_resources_enabled,
)
from nce.vertical_modules.resources.allocations import (
    CONTRACTOR_ALLOWED_ALLOCATION_FIELDS,
    VALID_ALLOCATION_STATUSES,
    do_detect_conflicts,
    do_release,
    do_reserve,
    redact_contractor_view,
)
from nce.vertical_modules.resources.capacity import do_resolve_capacity
from nce.vertical_modules.resources.material_flow import do_plan_material_flow
from nce.vertical_modules.resources.planner import (
    do_plan_allocation,
    do_record_allocation_outcome,
    load_allocation_weights,
)
from nce.vertical_modules.resources.registry import (
    VALID_RESOURCE_KINDS,
    do_create_resource,
    do_get_resource,
    do_list_resources,
    do_update_resource,
)

__all__ = [
    "CONTRACTOR_ALLOWED_ALLOCATION_FIELDS",
    "ResourceConcurrencyError",
    "ResourceNotFoundError",
    "ResourceValidationError",
    "ResourcesDisabledError",
    "ResourcesError",
    "VALID_ALLOCATION_STATUSES",
    "VALID_RESOURCE_KINDS",
    "do_create_resource",
    "do_detect_conflicts",
    "do_get_resource",
    "do_list_resources",
    "do_plan_allocation",
    "do_plan_material_flow",
    "do_record_allocation_outcome",
    "do_release",
    "do_reserve",
    "do_resolve_capacity",
    "do_update_resource",
    "load_allocation_weights",
    "redact_contractor_view",
    "require_resources_enabled",
]
