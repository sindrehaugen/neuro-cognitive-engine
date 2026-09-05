"""
nce.vertical_modules.resources._guard
======================================
Guardrails, permission checks, and domain exceptions for Module 15 (Staff & Resources Engine).
"""

from __future__ import annotations

import logging
from typing import Any

from nce.config import cfg

log = logging.getLogger("nce.vertical_modules.resources.guard")


class ResourcesError(Exception):
    """Base exception for all Resources Engine operations."""


class ResourcesDisabledError(ResourcesError):
    """Raised when Resources Engine is disabled globally or for a tenant."""


class ResourceNotFoundError(ResourcesError):
    """Raised when a requested resource does not exist in the tenant scope."""


class ResourceValidationError(ResourcesError):
    """Raised when input parameters fail domain validation (e.g. invalid kind, RS-2)."""


class ResourceConcurrencyError(ResourcesError):
    """Raised on concurrency/double-booking constraint violations (RS-3)."""


def require_resources_enabled(namespace_metadata: dict[str, Any] | None = None) -> None:
    """Verify that Resources Engine is enabled globally and for the tenant."""
    if not cfg.NCE_RESOURCES_ENABLED:
        raise ResourcesDisabledError(
            "Resources Engine is globally disabled via NCE_RESOURCES_ENABLED."
        )
    if namespace_metadata:
        res_meta = namespace_metadata.get("resources") or {}
        if isinstance(res_meta, dict) and res_meta.get("enabled") is False:
            raise ResourcesDisabledError("Resources Engine is disabled for this tenant.")
