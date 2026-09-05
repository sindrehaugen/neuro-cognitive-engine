"""
Centralised MCP error primitives — standard JSON-RPC 2.0 error codes and the
``@mcp_handler`` decorator for consistent error formatting across all handlers.

Usage
-----
Apply ``@mcp_handler`` to every MCP tool handler::

    from nce.mcp_errors import McpError, mcp_handler

    @mcp_handler
    async def handle_my_tool(engine, arguments) -> str:
        ...

On success the return value passes through unchanged.  On failure the decorator
catches all exceptions and re-raises an ``McpError`` with the appropriate
JSON-RPC error code.  ``server.py:call_tool()`` catches ``McpError`` and
formats it as a standard ``{"code": ..., "message": ..., "data": ...}`` response.

Mapping
-------
====================  =======  ===========================
Exception             Code     Message
====================  =======  ===========================
``McpError``          (as-is)  (as-is)
``ScopeError``        -32005   Scope forbidden
``OwnershipError``    -32005   Not permitted to write this node type
``DeploymentConfigurationError`` -32603  Internal error (deployment not configured)
``RateLimitError``    -32029   Rate limit exceeded
``ValidationError``   -32602   Invalid parameters
``QuotaExceededError``  -32013   Resource quota exceeded
``ValueError``          -32602   Invalid parameters
``TypeError``           -32602   Invalid parameters
``KeyError``            -32602   Invalid parameters (missing field, name not exposed)
``UnknownToolError``  -32601   Method not found
Everything else       -32603   Internal error
====================  =======  ===========================
"""

from __future__ import annotations

import functools
import inspect
import logging
import uuid
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import ValidationError

from nce.a2a import A2AAuthorizationError, A2AScopeViolationError
from nce.auth import RateLimitError, ScopeError
from nce.config import DeploymentConfigurationError, cfg
from nce.entity_resolution.ownership import OwnershipError
from nce.quotas import QuotaExceededError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JSON-RPC 2.0 standard error codes
# ---------------------------------------------------------------------------
MCP_PARSE_ERROR: int = -32700
MCP_INVALID_REQUEST: int = -32600
MCP_METHOD_NOT_FOUND: int = -32601
MCP_INVALID_PARAMS: int = -32602
MCP_INTERNAL_ERROR: int = -32603

# ---------------------------------------------------------------------------
# MCP extended error codes (server-defined range -32000 to -32099)
# ---------------------------------------------------------------------------
MCP_AUTH_FAILED: int = -32001
MCP_REPLAY_DETECTED: int = -32002
MCP_SCOPE_FORBIDDEN: int = -32005
MCP_A2A_AUTH_FAILED: int = -32010
MCP_A2A_SCOPE_VIOLATION: int = -32011
MCP_QUOTA_EXCEEDED: int = -32013
MCP_RATE_LIMITED: int = -32029


class McpError(Exception):
    """Exception carrying a JSON-RPC 2.0 error code and structured data.

    Raise this inside an ``@mcp_handler``-decorated handler to return a
    specific error code and message.  ``call_tool()`` catches it and formats
    it as a standard ``{"jsonrpc": "2.0", "error": {"code": ..., "message": ...}}``
    response.

    Attributes:
        code:    Standard JSON-RPC 2.0 or MCP extended error code.
        message: Short human-readable error summary.
        data:    Optional dict merged into the ``error.data`` field.
    """

    def __init__(
        self,
        code: int,
        message: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"[MCP {code}] {message}")


class UnknownToolError(McpError):
    """Raised when ``call_tool()`` receives a tool name with no registered handler."""

    def __init__(self, tool_name: str) -> None:
        super().__init__(MCP_METHOD_NOT_FOUND, f"Unknown tool: {tool_name}")


def client_visible_detail(message: str | None) -> str | None:
    """Return *message* for MCP clients only when ``cfg.IS_DEV`` is true."""
    if not message or not cfg.IS_DEV:
        return None
    return message


def internal_error_data(exc: Exception, *, request_id: str | None = None) -> dict[str, Any]:
    """Build a production-safe ``error.data`` payload for uncaught handler failures."""
    rid = request_id or str(uuid.uuid4())
    exc_type = type(exc).__name__
    if not cfg.IS_DEV:
        module = exc.__class__.__module__
        if not (module == "builtins" or module.startswith("nce.")):
            if "asyncpg" in module or "mongo" in module or "redis" in module:
                exc_type = "DatabaseError"
            else:
                exc_type = "InternalException"

    data: dict[str, Any] = {
        "reason": "internal_error",
        "type": exc_type,
        "request_id": rid,
    }
    detail = client_visible_detail(str(exc))
    if detail is not None:
        data["detail"] = detail
    return data


def invalid_arguments_data(exc: Exception) -> dict[str, Any]:
    """Build ``error.data`` for ``ValueError`` / ``TypeError`` without leaking in prod."""
    data: dict[str, Any] = {"reason": "invalid_arguments"}
    detail = client_visible_detail(str(exc))
    if detail is not None:
        data["detail"] = detail
    return data


def ownership_denied_data(exc: OwnershipError) -> dict[str, Any]:
    """Build ``error.data`` for an ``assert_owner`` refusal (**D49a**).

    JSON primitives only. ``nce/mcp_stdio_rpc.py`` emits ``error.data`` through
    a plain ``json.dumps`` with no ``default=str``, so a non-primitive left here
    would raise ``TypeError`` *inside the error-reporting path* — turning a
    refusal into the very crash this mapping exists to remove.
    ``inventory/refusals.py::refusal_payload`` already solves this, but it is
    keyed on that module's own ``_REFUSALS`` table and importing it here would
    close a cycle (``refusals.py`` imports ``nce.mcp_errors``), so the coercion
    is explicit instead.

    Carries only what the caller can act on: the contested ``node_type``, the
    ``writer_engine`` that was refused, and the ``transition`` that was checked.
    ``owner_engine`` and the namespace id are deliberately **omitted** — the
    first is registry row content and the second identifies a tenant, and a
    refusal payload is returned to a caller who may not be entitled to either.
    """
    return {
        "reason": "ownership_denied",
        "node_type": str(exc.node_type),
        "writer_engine": str(exc.writer_engine),
        "transition": None if exc.transition is None else str(exc.transition),
    }


def deployment_not_configured_data(exc: DeploymentConfigurationError) -> dict[str, Any]:
    """Build ``error.data`` for an unset/malformed deployment key (**D49b**).

    JSON primitives only, for the same reason ``ownership_denied_data`` is:
    ``nce/mcp_stdio_rpc.py`` emits ``error.data`` through a plain ``json.dumps``
    with no ``default=`` hook, so a non-primitive here would raise ``TypeError``
    *inside the error-reporting path*.

    Carries the config key's **name** only — never its value, which may be a
    secret and which the caller has no business seeing. ``reason`` is what lets
    a client tell a misconfiguration apart from a crash while the code stays
    ``-32603``: the caller's action is identical either way (escalate, never
    retry), so a new error code would cost every client a branch it cannot act
    on differently.
    """
    return {
        "reason": "deployment_not_configured",
        "config_key": str(exc.config_key),
    }


def merge_client_error_data(
    base: dict[str, Any] | None,
    *,
    detail: str | None = None,
) -> dict[str, Any]:
    """Merge optional client ``detail`` into JSON-RPC error data (dev-only for strings)."""
    merged: dict[str, Any] = dict(base or {})
    visible = client_visible_detail(detail)
    if visible is not None:
        merged["detail"] = visible
    return merged


# ---------------------------------------------------------------------------
# @mcp_handler decorator
# ---------------------------------------------------------------------------

F = TypeVar("F", bound=Callable[..., Any])


def mcp_handler(handler_fn: F) -> F:
    """Decorator: catch exceptions in an MCP handler and raise ``McpError``.

    On success the return value is passed through unchanged.
    On failure a typed ``McpError`` is raised so ``call_tool()`` can format it
    as a consistent JSON-RPC 2.0 error response.

    The decorator respects the existing exception class hierarchy so:
    - ``ScopeError`` / ``RateLimitError`` from earlier decorators (``@require_scope``)
      are propagated untouched.
    - ``McpError`` from explicit raises is propagated as-is.
    - All other exceptions are categorised by type.

    The wrapper handles both async and sync handlers via ``inspect.iscoroutine``,
    though all production MCP handlers are expected to be async.
    """

    @functools.wraps(handler_fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            result = handler_fn(*args, **kwargs)
            if inspect.iscoroutine(result):
                return await result
            return result
        except (ScopeError, RateLimitError, McpError):
            # Already typed — propagate to call_tool unchanged.
            raise
        except OwnershipError as e:
            # D49a: a stated refusal, not an opaque internal error. Clause order
            # in this decorator is load-bearing, so note explicitly why this
            # position is safe: ``OwnershipError`` derives from ``Exception``
            # only and nothing derives from it, so it is disjoint from every
            # clause below and shadows none of them. In particular
            # ``QuotaExceededError`` still precedes ``ValueError`` (it is a
            # ``ValueError`` subclass) exactly as before.
            raise McpError(
                MCP_SCOPE_FORBIDDEN,
                "Not permitted to write this node type",
                data=ownership_denied_data(e),
            )
        except DeploymentConfigurationError as e:
            # D49b: an unset/malformed deployment key is the OPERATOR's fault,
            # never the caller's — no argument any client can send will fix it.
            # Falling through to ``except (ValueError, TypeError)`` would report
            # it as ``-32602 Invalid parameters`` and invite an infinite retry.
            # Clause order (see the D49a note above) is load-bearing, and this
            # position is safe for the same reason: ``DeploymentConfigurationError``
            # derives from ``Exception`` only and nothing derives from it, so it
            # is disjoint from every clause below and shadows none of them.
            # ``QuotaExceededError`` still precedes ``ValueError`` (it is a
            # ``ValueError`` subclass) exactly as before. The code stays
            # ``-32603``: this genuinely is a server-side problem.
            raise McpError(
                MCP_INTERNAL_ERROR,
                "Internal error",
                data=deployment_not_configured_data(e),
            )
        except ValidationError as e:
            sanitized_errors = []
            for err in e.errors(include_url=False):
                err_copy = dict(err)
                if "ctx" in err_copy and isinstance(err_copy["ctx"], dict):
                    ctx_copy = dict(err_copy["ctx"])
                    if "error" in ctx_copy:
                        ctx_copy["error"] = str(ctx_copy["error"])
                    err_copy["ctx"] = ctx_copy
                sanitized_errors.append(err_copy)
            raise McpError(
                MCP_INVALID_PARAMS,
                "Invalid parameters",
                data={
                    "reason": "validation_error",
                    "errors": sanitized_errors,
                },
            )
        except QuotaExceededError:
            # Must precede ValueError — QuotaExceededError is a ValueError subclass.
            raise McpError(
                MCP_QUOTA_EXCEEDED,
                "Resource quota exceeded",
                data={"reason": "quota_exceeded"},
            )
        except KeyError:
            # Separate handler: str(KeyError) echoes field names ('secret_key').
            raise McpError(
                MCP_INVALID_PARAMS,
                "Invalid parameters",
                data={"reason": "missing_field"},
            )
        except (ValueError, TypeError) as e:
            raise McpError(
                MCP_INVALID_PARAMS,
                "Invalid parameters",
                data=invalid_arguments_data(e),
            )
        except A2AAuthorizationError as e:
            raise McpError(
                MCP_A2A_AUTH_FAILED,
                "A2A authorization failure",
                data={"reason": str(e)},
            )
        except A2AScopeViolationError as e:
            raise McpError(
                MCP_A2A_SCOPE_VIOLATION,
                "Scope violation",
                data={"reason": str(e)},
            )
        except Exception as e:
            request_id = str(uuid.uuid4())
            log.exception(
                "Handler %s failed request_id=%s",
                handler_fn.__name__,
                request_id,
            )
            raise McpError(
                MCP_INTERNAL_ERROR,
                "Internal error",
                data=internal_error_data(e, request_id=request_id),
            )

    return wrapper  # type: ignore[return-value]
