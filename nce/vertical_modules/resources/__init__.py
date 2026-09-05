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
from nce.vertical_modules.resources.capacity import do_resolve_capacity
from nce.vertical_modules.resources.registry import (
    VALID_RESOURCE_KINDS,
    do_create_resource,
    do_get_resource,
    do_list_resources,
    do_update_resource,
)

__all__ = [
    "ResourceConcurrencyError",
    "ResourceNotFoundError",
    "ResourceValidationError",
    "ResourcesDisabledError",
    "ResourcesError",
    "VALID_RESOURCE_KINDS",
    "do_create_resource",
    "do_get_resource",
    "do_list_resources",
    "do_resolve_capacity",
    "do_update_resource",
    "require_resources_enabled",
]
