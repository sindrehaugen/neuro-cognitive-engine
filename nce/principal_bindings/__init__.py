"""
nce/principal_bindings package

C16 Principal Mapping (Wave A-6)
Multi-tenant principal mapping and context resolution.
"""

from __future__ import annotations

from nce.principal_bindings.models import PrincipalBinding, PrincipalContext
from nce.principal_bindings.service import (
    clear_test_bindings,
    current_principal,
    delete_principal_binding,
    get_principal_binding,
    register_test_binding,
    resolve_principal_context_async,
    resolve_principal_context_sync,
    upsert_principal_binding,
)

__all__ = [
    "PrincipalBinding",
    "PrincipalContext",
    "clear_test_bindings",
    "current_principal",
    "delete_principal_binding",
    "get_principal_binding",
    "register_test_binding",
    "resolve_principal_context_async",
    "resolve_principal_context_sync",
    "upsert_principal_binding",
]
