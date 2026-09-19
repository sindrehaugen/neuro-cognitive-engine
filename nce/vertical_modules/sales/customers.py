"""nce.vertical_modules.sales.customers — Customer domain interfaces and resource bindings.

Lane B Wave B-1:
Surfaces customer resource bindings, satisfying downstream predicate P9 for Lane G
(accounts+contacts sync).
"""

from __future__ import annotations

from typing import Any

from nce.vertical_modules.sales.resources import CUSTOMER_SPEC

__all__ = [
    "CUSTOMER_SPEC",
    "get_customer_resource_spec",
]


def get_customer_resource_spec() -> Any:
    """Return the C12 ResourceSpec for CUSTOMER."""
    return CUSTOMER_SPEC
