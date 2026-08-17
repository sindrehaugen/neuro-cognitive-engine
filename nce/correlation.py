"""
Request-scoped correlation ID tracking via ContextVar.

Follows the same pattern as ``embeddings.degraded_embedding_flag`` —
a single module-level ContextVar, set at the request boundary and
propagated automatically through async call chains.

Usage — setting at the MCP stdio boundary (server.py)::

    from nce.correlation import correlation_id_var
    import uuid

    token = correlation_id_var.set(uuid.uuid4())
    try:
        result = await dispatch(request)
    finally:
        correlation_id_var.reset(token)

Usage — reading anywhere downstream::

    from nce.correlation import get_correlation_id

    cid = get_correlation_id()   # uuid.UUID | None — None if not set

Usage — reading in code that requires a correlation ID::

    from nce.correlation import require_correlation_id

    cid = require_correlation_id()   # raises RuntimeError if not set
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

correlation_id_var: ContextVar[uuid.UUID] = ContextVar("correlation_id")


def get_correlation_id() -> uuid.UUID | None:
    """Return the active correlation ID, or None if no request context is active."""
    return correlation_id_var.get(None)


def require_correlation_id() -> uuid.UUID:
    """Return the active correlation ID.

    Raises
    ------
    RuntimeError
        If ``correlation_id_var`` has not been set for this async context.
        Callers that can tolerate a missing ID should use ``get_correlation_id()`` instead.
    """
    cid = correlation_id_var.get(None)
    if cid is None:
        raise RuntimeError(
            "correlation_id_var is not set for this request context. "
            "Ensure the MCP call_tool handler wraps each invocation with "
            "correlation_id_var.set(uuid.uuid4()) before dispatch."
        )
    return cid
