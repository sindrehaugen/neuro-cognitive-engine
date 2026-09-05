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
from nce.vertical_modules.resources.field_schedule import do_field_schedule
from nce.vertical_modules.resources.forecast import (
    do_forecast_demand,
    get_morning_brief_capacity_pulse,
)
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
from nce.vertical_modules.resources.travel import (
    calculate_norwegian_diett,
    do_plan_travel,
    load_travel_policy,
)
from nce.vertical_modules.resources.watcher import (
    handle_hr_cert_change,
    register_resources_event_subscribers,
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
    "calculate_norwegian_diett",
    "do_create_resource",
    "do_detect_conflicts",
    "do_field_schedule",
    "do_forecast_demand",
    "do_get_resource",
    "do_list_resources",
    "do_plan_allocation",
    "do_plan_material_flow",
    "do_plan_travel",
    "do_record_allocation_outcome",
    "do_release",
    "do_reserve",
    "do_resolve_capacity",
    "do_update_resource",
    "get_morning_brief_capacity_pulse",
    "handle_hr_cert_change",
    "load_allocation_weights",
    "load_travel_policy",
    "redact_contractor_view",
    "register_resources_event_subscribers",
    "require_resources_enabled",
]
