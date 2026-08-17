"""
nce/a2a_server.py

Phase 3.1 — A2A (Agent-to-Agent) Protocol Surface

Starlette ASGI application exposing the public A2A bridge endpoints:

  GET  /.well-known/agent-card     — public agent capability descriptor
  POST /tasks/send                 — submit a skill task (JWT-protected)
  GET  /tasks/{task_id}            — poll task status (JWT-protected)
  POST /tasks/{task_id}/cancel     — cancel a running task (JWT-protected)

Authentication
--------------
  - /.well-known/agent-card  → unauthenticated (public discovery)
  - /tasks/*                 → JWTAuthMiddleware (Bearer token, NamespaceContext)

Task state is held in an in-memory dict (Phase 3.1).  Tasks are short-lived
(seconds to low-minutes); use the poll endpoint for async clients.

JSON-RPC 2.0 error codes
-------------------------
  -32005  JWT validation failure
  -32006  JWT missing namespace_id claim
  -32007  JWT invalid claim value
  -32010  A2A authorization failure (bad/expired sharing token)
  -32011  A2A scope violation (resource not in granted scopes)
  -32012  Bad skill or missing parameters

Run standalone:
  uvicorn nce.a2a_server:app --host 0.0.0.0 --port 8004
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import signal
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

try:
    import psutil  # type: ignore[import-untyped]
except ImportError:
    psutil = None

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine
from uuid import uuid4

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from nce.a2a import (
    A2A_CODE_BAD_REQUEST,
    A2A_CODE_SCOPE_VIOLATION,
    A2A_CODE_UNAUTHORIZED,
    A2AAuthorizationError,
    A2AScopeViolationError,
    enforce_scope,
    verify_token,
)
from nce.auth import NamespaceContext
from nce.config import cfg
from nce.correlation import correlation_id_var
from nce.db_utils import set_external_scope
from nce.jwt_auth import JWTAuthMiddleware
from nce.models import GraphSearchRequest
from nce.providers import LLMCircuitOpenError
from nce.tool_governance import GOVERNANCE, GovernanceUnavailable

log = logging.getLogger("nce.a2a_server")

# JSON-RPC 2.0 error code for mTLS failures (same range as A2A_UNAUTHORIZED)
A2A_CODE_MTLS = -32015  # mTLS client certificate validation failed

# ---------------------------------------------------------------------------
# C3 principal-session wiring (Wave 23 / IDOR fix)
# ---------------------------------------------------------------------------
# The external scope for a request is derived EXCLUSIVELY from the verified
# NamespaceContext attached by JWTAuthMiddleware (request.state.namespace_ctx).
# That context is populated from the signed JWT payload — never from raw headers.
#
# The raw headers X-NCE-Principal-Kind / X-NCE-External-Scope-Id that existed
# in the original Wave 23 implementation were the source of the IDOR: a holder
# of a valid JWT for namespace A could forge those headers to set an arbitrary
# external scope on the DB session.  They are no longer read anywhere.
#
# Employee sessions never set nce.external_scope_id; the GUC is left unset so
# the deny-when-unset guarantee from Wave 22 applies to any
# external_isolation_policy table the session touches.


def _external_scope_from_context(ctx: NamespaceContext) -> str | None:
    """Return the authenticated external scope UUID string for a principal, or None.

    Source: the verified NamespaceContext built by JWTAuthMiddleware from the
    signed JWT payload.  Raw request headers are NOT consulted.

    Returns:
        The UUID string for contractor / external-customer principals whose JWT
        carried a valid, non-nil ``external_scope_id`` claim.
        None for employee principals or when no valid scope claim was present
        (deny-by-default — the GUC is left unset, so the external_isolation_policy
        denies all rows via the nil-UUID sentinel).

    Never raises.
    """
    if ctx.principal_kind not in ("contractor", "external-customer"):
        return None
    if ctx.external_scope_id is None:
        return None
    return str(ctx.external_scope_id)


def _get_process_memory_mb() -> float | None:
    if psutil is not None:
        try:
            return psutil.Process(os.getpid()).memory_info().rss / (1024.0 * 1024.0)
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# mTLS Client Certificate Middleware (imported from shared module)
# ---------------------------------------------------------------------------

from nce.mtls import MTLSAuthMiddleware  # noqa: E402

# ---------------------------------------------------------------------------
# Graceful shutdown machinery
# ---------------------------------------------------------------------------
_SHUTDOWN_EVENT: asyncio.Event | None = None
_ACTIVE_REQUESTS: int = 0
_ACTIVE_REQUESTS_LOCK: asyncio.Lock = asyncio.Lock()
_GRACE_PERIOD_S: int = 30  # max seconds to wait for active requests to drain


def _init_shutdown() -> None:
    """Initialise the shutdown event if not already set."""
    global _SHUTDOWN_EVENT
    if _SHUTDOWN_EVENT is None:
        _SHUTDOWN_EVENT = asyncio.Event()


def _is_shutting_down() -> bool:
    """Return True if the server is in graceful-shutdown mode."""
    return _SHUTDOWN_EVENT is not None and _SHUTDOWN_EVENT.is_set()


async def _reject_if_shutting_down(request: Request) -> JSONResponse | None:
    """Return a 503 JSON response if the server is shutting down, else None."""
    if _is_shutting_down():
        return JSONResponse(
            {
                "error": "Server shutting down",
                "detail": "No new requests accepted during graceful shutdown",
            },
            status_code=503,
        )
    return None


@asynccontextmanager
async def _track_active_request():
    """Context manager that tracks the active-request counter for graceful drain."""
    global _ACTIVE_REQUESTS
    async with _ACTIVE_REQUESTS_LOCK:
        _ACTIVE_REQUESTS += 1
    try:
        yield
    finally:
        async with _ACTIVE_REQUESTS_LOCK:
            _ACTIVE_REQUESTS -= 1


async def _drain_active_requests(timeout: int = _GRACE_PERIOD_S) -> int:
    """Wait up to *timeout* seconds for active requests to finish.

    Returns the number of requests still in-flight after the deadline.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        async with _ACTIVE_REQUESTS_LOCK:
            remaining = _ACTIVE_REQUESTS
        if remaining == 0:
            return 0
        wait = deadline - asyncio.get_running_loop().time()
        if wait <= 0:
            return remaining
        await asyncio.sleep(0.1)


# ---------------------------------------------------------------------------
class BoundedDict(collections.OrderedDict):
    """Subclass of OrderedDict that limits total keys to prevent memory leaks."""

    def __init__(self, maxlen: int = 10000, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.maxlen = maxlen

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if len(self) > self.maxlen:
            self.popitem(last=False)


_tasks: BoundedDict = BoundedDict(maxlen=10000)


async def _store_task(task_id: str, task: dict[str, Any]) -> None:
    _tasks[task_id] = task
    if _engine is not None and _engine.redis_client is not None:
        try:
            key = f"nce:a2a:tasks:{task_id}"
            await _engine.redis_client.set(key, json.dumps(task), ex=3600)
        except Exception as exc:
            log.warning("Failed to store task in Redis: %s", exc)


async def _get_task(task_id: str) -> dict[str, Any] | None:
    if _engine is not None and _engine.redis_client is not None:
        try:
            key = f"nce:a2a:tasks:{task_id}"
            raw = await _engine.redis_client.get(key)
            if raw:
                return json.loads(raw)
        except Exception as exc:
            log.warning("Failed to get task from Redis: %s", exc)
    return _tasks.get(task_id)


# ---------------------------------------------------------------------------
# Engine reference (injected via lifespan)
# ---------------------------------------------------------------------------
_engine: NCEEngine | None = None


# ---------------------------------------------------------------------------
# Agent card (A2A protocol discovery document)
# ---------------------------------------------------------------------------
_AGENT_CARD: dict[str, Any] = {
    "schema_version": "0.2",
    "name": "NCE Memory Engine",
    "description": (
        "Persistent, verifiable, temporal AI memory engine. "
        "Provides cognitive memory services including semantic recall, "
        "knowledge graph traversal, session archival, and memory integrity verification. "
        "Memories decay, consolidate, and strengthen over time via bio-inspired algorithms."
    ),
    "url": cfg.NCE_A2A_URL,
    "version": "1.0",
    "capabilities": {
        "streaming": False,
        "stateTransitionHistory": False,
        "pushNotifications": False,
    },
    "skills": [
        {
            "id": "recall_relevant_context",
            "name": "Recall Relevant Context",
            "description": (
                "Semantic search combined with knowledge graph traversal to retrieve "
                "the most relevant memories and associated context."
            ),
            "inputModes": ["text"],
            "outputModes": ["text"],
            "parameters": {
                "query": {"type": "string", "required": True},
                "namespace_id": {"type": "string", "format": "uuid", "required": True},
                "agent_id": {"type": "string", "required": False},
                "user_id": {"type": "string", "required": False},
                "top_k": {"type": "integer", "default": 5, "required": False},
            },
        },
        {
            "id": "archive_session",
            "name": "Archive Session",
            "description": "Batch-store a list of memory payloads from a completed session.",
            "inputModes": ["text"],
            "outputModes": ["text"],
            "parameters": {
                "namespace_id": {"type": "string", "format": "uuid", "required": True},
                "agent_id": {"type": "string", "required": True},
                "user_id": {"type": "string", "required": False},
                "memories": {
                    "type": "array",
                    "required": True,
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "summary": {"type": "string"},
                        },
                    },
                },
            },
        },
        {
            "id": "find_related_decisions",
            "name": "Find Related Decisions",
            "description": "Knowledge graph search to surface related decisions and context nodes.",
            "inputModes": ["text"],
            "outputModes": ["text"],
            "parameters": {
                "query": {"type": "string", "required": True},
                "namespace_id": {"type": "string", "format": "uuid", "required": True},
                "agent_id": {"type": "string", "required": False},
                "user_id": {"type": "string", "required": False},
                "max_depth": {"type": "integer", "default": 2, "required": False},
            },
        },
        {
            "id": "verify_memory_integrity",
            "name": "Verify Memory Integrity",
            "description": "Verify the HMAC signature of a stored memory to confirm it was not tampered with.",
            "inputModes": ["text"],
            "outputModes": ["text"],
            "parameters": {
                "memory_id": {"type": "string", "format": "uuid", "required": True},
                "namespace_id": {"type": "string", "format": "uuid", "required": True},
                "user_id": {"type": "string", "required": False},
            },
        },
        {
            "id": "get_cognitive_state",
            "name": "Get Cognitive State",
            "description": (
                "Retrieve the N most recent memories representing an agent's current "
                "cognitive state (fast Redis-backed recall)."
            ),
            "inputModes": ["text"],
            "outputModes": ["text"],
            "parameters": {
                "namespace_id": {"type": "string", "format": "uuid", "required": True},
                "agent_id": {"type": "string", "required": True},
                "user_id": {"type": "string", "required": False},
                "n": {"type": "integer", "default": 10, "required": False},
            },
        },
        {
            "id": "vendors_partner_view",
            "name": "Vendors Partner View",
            "description": "Retrieve redacted target node fields for external contractors.",
            "inputModes": ["text"],
            "outputModes": ["text"],
            "parameters": {
                "namespace_id": {"type": "string", "format": "uuid", "required": True},
                "node_id": {"type": "string", "required": True},
            },
        },
    ],
}


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 helpers
# ---------------------------------------------------------------------------


def _jsonrpc_err(code: int, message: str, reason: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "error": {
            "code": code,
            "message": message,
            "data": {"reason": reason},
        },
        "id": None,
    }


# ---------------------------------------------------------------------------
# Task state helpers
# ---------------------------------------------------------------------------


def _make_task(
    task_id: str, state: str, artifacts: list | None = None, message: str | None = None
) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": task_id,
        "status": {
            "state": state,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        "artifacts": artifacts or [],
    }
    if message:
        task["status"]["message"] = {
            "role": "agent",
            "parts": [{"type": "text", "text": message}],
        }
    return task


def _require_param(params: dict[str, Any], key: str) -> Any:
    val = params.get(key)
    if val is None:
        raise ValueError(f"Missing required parameter: {key!r}")
    return val


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def get_agent_card(request: Request) -> JSONResponse:
    """GET /.well-known/agent-card — public, unauthenticated."""
    rejected = await _reject_if_shutting_down(request)
    if rejected:
        return rejected
    async with _track_active_request():
        return JSONResponse(_AGENT_CARD)


async def tasks_send(request: Request) -> JSONResponse:
    """POST /tasks/send — Submit a skill invocation task."""
    rejected = await _reject_if_shutting_down(request)
    if rejected:
        return rejected

    if _engine is None:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    # Sliding-window rate limit check
    client_ip = request.client.host if request.client else "unknown"
    key = f"nce:ratelimit:a2a:tasks_send:{client_ip}"
    redis_client = _engine.redis_client if _engine else None
    from nce.auth import _check_admin_http_rate_limit

    allowed = await _check_admin_http_rate_limit(
        redis_client,
        key,
        cfg.NCE_A2A_HTTP_RATE_LIMIT,
        cfg.NCE_A2A_HTTP_RATE_PERIOD,
    )
    if not allowed:
        log.warning("A2A tasks/send rate limit exceeded for IP %s", client_ip)
        return JSONResponse(
            _jsonrpc_err(-32013, "Rate limit exceeded", "too_many_requests"),
            status_code=429,
        )

    # Check Uvicorn process memory usage to prevent OOM degradation
    mem_mb = _get_process_memory_mb()
    mem_limit = getattr(cfg, "NCE_A2A_MEMORY_LIMIT_MB", 2048.0)
    if mem_mb is not None and mem_mb > mem_limit:
        log.warning("Uvicorn memory threshold exceeded: %.1f MB > %.1f MB", mem_mb, mem_limit)
        return JSONResponse(
            _jsonrpc_err(
                -32017,
                "Resource exhaustion: memory threshold exceeded",
                f"Memory usage: {mem_mb:.1f} MB",
            ),
            status_code=503,
        )

    caller_ctx: NamespaceContext | None = getattr(request.state, "namespace_ctx", None)
    if caller_ctx is None:
        return JSONResponse(
            _jsonrpc_err(
                A2A_CODE_UNAUTHORIZED,
                "Authentication failed",
                "missing_namespace_context",
            ),
            status_code=401,
        )

    # C3 principal-session wiring (Wave 23 / IDOR fix).
    # Scope is derived from the VERIFIED NamespaceContext (JWT-backed) — never
    # from raw request headers.  None for employee principals (GUC left unset →
    # deny-when-unset on external_isolation_policy tables preserved).
    external_scope_id: str | None = _external_scope_from_context(caller_ctx)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            _jsonrpc_err(A2A_CODE_BAD_REQUEST, "Bad request", "invalid_json"),
            status_code=400,
        )

    task_id: str = str(body.get("id") or uuid4())
    skill: str = str(body.get("skill") or "").strip()
    params: dict[str, Any] = body.get("params") or {}
    sharing_token: str | None = body.get("sharing_token")  # optional cross-agent token

    # Propagate correlation_id from the caller's _meta block.
    # Guards against str(None) — only inject a valid UUID string.
    _incoming_cid: uuid.UUID | None = None
    _meta: dict[str, Any] = body.get("_meta") or {}
    _raw_cid: str | None = _meta.get("correlation_id")
    if _raw_cid:
        try:
            _incoming_cid = uuid.UUID(str(_raw_cid))
        except (ValueError, AttributeError):
            log.debug("a2a tasks_send: ignoring malformed correlation_id %r", _raw_cid)
    _cid_token = correlation_id_var.set(_incoming_cid or uuid4())

    if not skill:
        return JSONResponse(
            _jsonrpc_err(A2A_CODE_BAD_REQUEST, "Bad request", "missing_skill"),
            status_code=400,
        )

    await _store_task(task_id, _make_task(task_id, "submitted"))

    async with _track_active_request():
        try:
            # --- Cross-agent scope validation (A2A sharing token path) ---
            # correlation_id_var is already set above; reset in finally.
            if sharing_token is not None:
                async with _engine.pg_pool.acquire(timeout=10.0) as conn:
                    async with conn.transaction():
                        # C3 wiring: set external scope GUC for contractor /
                        # external-customer principals before any DB work.
                        if external_scope_id is not None:
                            await set_external_scope(conn, external_scope_id)
                        verified = await verify_token(conn, sharing_token, caller_ctx)

                # Enforce the namespace scope covers the requested namespace_id
                enforce_scope(
                    verified.scopes,
                    "namespace",
                    str(verified.owner_namespace_id),
                    str(verified.owner_namespace_id),
                )

                # Override the params to operate in the owner's namespace
                params = dict(params)
                params["namespace_id"] = str(verified.owner_namespace_id)
                params["_owner_agent_id"] = verified.owner_agent_id

                log.info(
                    "A2A cross-agent access granted: grant=%s consumer_ns=%s owner_ns=%s skill=%s",
                    verified.grant_id,
                    caller_ctx.namespace_id,
                    verified.owner_namespace_id,
                    skill,
                )

            result = await _dispatch_skill(skill, params, caller_ctx)
            task = _make_task(
                task_id,
                "completed",
                artifacts=[{"type": "text", "text": json.dumps(result)}],
            )
            await _store_task(task_id, task)
            return JSONResponse(task, status_code=200)

        except A2AAuthorizationError as exc:
            task = _make_task(task_id, "failed", message=str(exc))
            await _store_task(task_id, task)
            return JSONResponse(
                _jsonrpc_err(A2A_CODE_UNAUTHORIZED, "A2A authorization failure", str(exc)),
                status_code=403,
            )
        except A2AScopeViolationError as exc:
            task = _make_task(task_id, "failed", message=str(exc))
            await _store_task(task_id, task)
            return JSONResponse(
                _jsonrpc_err(A2A_CODE_SCOPE_VIOLATION, "Scope violation", str(exc)),
                status_code=403,
            )
        except LLMCircuitOpenError as exc:
            task = _make_task(task_id, "failed", message=str(exc))
            await _store_task(task_id, task)
            return JSONResponse(
                _jsonrpc_err(
                    -32016, "Service temporarily degraded (circuit breaker open)", str(exc)
                ),
                status_code=503,
            )
        except ValueError as exc:
            task = _make_task(task_id, "failed", message=str(exc))
            await _store_task(task_id, task)
            return JSONResponse(
                _jsonrpc_err(A2A_CODE_BAD_REQUEST, "Invalid skill parameters", str(exc)),
                status_code=400,
            )
        except Exception as exc:
            log.exception("tasks_send failed task_id=%s skill=%s", task_id, skill)
            task = _make_task(task_id, "failed", message=f"Internal error: {type(exc).__name__}")
            await _store_task(task_id, task)
            return JSONResponse({"error": "Internal error"}, status_code=500)
        except BaseException as exc:
            import asyncio

            state = "canceled" if isinstance(exc, asyncio.CancelledError) else "failed"
            msg = (
                "Task cancelled (client disconnected or timed out)"
                if state == "canceled"
                else f"Task failed: {type(exc).__name__}"
            )
            task = _make_task(task_id, state, message=msg)
            await asyncio.shield(_store_task(task_id, task))
            raise
        finally:
            correlation_id_var.reset(_cid_token)


async def tasks_get(request: Request) -> JSONResponse:
    """GET /tasks/{task_id} — Poll task status."""
    rejected = await _reject_if_shutting_down(request)
    if rejected:
        return rejected
    async with _track_active_request():
        task_id = request.path_params.get("task_id", "")
        task = await _get_task(task_id)
        if task is None:
            return JSONResponse({"error": "Task not found", "task_id": task_id}, status_code=404)
        return JSONResponse(task)


async def tasks_cancel(request: Request) -> JSONResponse:
    """POST /tasks/{task_id}/cancel — Cancel a task."""
    rejected = await _reject_if_shutting_down(request)
    if rejected:
        return rejected
    async with _track_active_request():
        task_id = request.path_params.get("task_id", "")
        task = await _get_task(task_id)
        if task is None:
            return JSONResponse({"error": "Task not found", "task_id": task_id}, status_code=404)

        current_state = task["status"]["state"]
        if current_state in ("completed", "failed", "canceled"):
            return JSONResponse(
                {
                    "error": f"Task already in terminal state: {current_state!r}",
                    "task_id": task_id,
                },
                status_code=409,
            )

        task = _make_task(task_id, "canceled", message="Cancelled by user")
        await _store_task(task_id, task)
        return JSONResponse(task)


# ---------------------------------------------------------------------------
# Skill dispatch
# ---------------------------------------------------------------------------


async def _dispatch_skill(
    skill: str,
    params: dict[str, Any],
    caller_ctx: NamespaceContext,
) -> Any:
    if _engine is None:
        raise RuntimeError("engine not initialized")

    if caller_ctx.principal_kind == "contractor":
        allowed_partner_skills = {"vendors_partner_view"}
        if skill not in allowed_partner_skills:
            raise A2AScopeViolationError(
                f"A2A skill '{skill}' is not authorized for contractor sessions."
            )

    # Governance gate (audit Domain 1, CWE-636/1188): last-known-good interceptor.
    # A positive hit raises the strict scope error (→ -32011); an unavailable
    # registry also raises it (fail CLOSED), never silently un-revoking a skill.
    try:
        if await GOVERNANCE.is_disabled(_engine.redis_client, skill):
            raise A2AScopeViolationError(
                f"A2A skill '{skill}' has been disabled by the administrator."
            )
    except GovernanceUnavailable as exc:
        raise A2AScopeViolationError(
            "A2A skill governance registry unavailable; dispatch blocked."
        ) from exc

    if skill == "recall_relevant_context":
        query = _require_param(params, "query")
        ns_id = _require_param(params, "namespace_id")
        agent_id = params.get("agent_id", caller_ctx.agent_id or "default")
        top_k = max(1, min(int(params.get("limit", params.get("top_k", 5))), 20))
        offset = max(0, int(params.get("offset", 0)))

        semantic = await _engine.semantic_search(
            namespace_id=ns_id,
            agent_id=agent_id,
            query=query,
            limit=top_k,
            offset=offset,
        )
        try:
            graph: dict = await _engine.graph_search(
                GraphSearchRequest(query=query, namespace_id=uuid.UUID(ns_id), max_depth=1)
            )
        except Exception:
            graph = {}

        return {"semantic": semantic, "graph": graph}

    if skill == "archive_session":
        from nce.models import MemoryType, StoreMemoryRequest

        memories = _require_param(params, "memories")
        ns_id = _require_param(params, "namespace_id")
        agent_id = _require_param(params, "agent_id")

        if not isinstance(memories, list):
            raise ValueError("'memories' must be a list of memory objects")

        sem = asyncio.Semaphore(4)

        async def store_one(m):
            async with sem:
                req = StoreMemoryRequest(
                    namespace_id=uuid.UUID(ns_id),
                    agent_id=agent_id,
                    content=m.get("content", ""),
                    summary=m.get("summary") or m.get("content", "")[:200],
                    heavy_payload=m.get("content", ""),
                    memory_type=MemoryType.episodic,
                )
                result = await _engine.store_memory(req)
                return str(result.get("payload_ref", ""))

        refs = await asyncio.gather(*(store_one(m) for m in memories))

        return {"archived": len(refs), "refs": refs}

    if skill == "find_related_decisions":
        query = _require_param(params, "query")
        ns_id = _require_param(params, "namespace_id")
        max_depth = max(1, min(int(params.get("max_depth", 2)), 3))
        agent_id = params.get("agent_id")

        result = await _engine.graph_search(
            GraphSearchRequest(
                query=query,
                namespace_id=uuid.UUID(ns_id),
                max_depth=max_depth,
                agent_id=agent_id,
            )
        )
        return result

    if skill == "verify_memory_integrity":
        memory_id_str = _require_param(params, "memory_id")
        return await _engine.verify_memory(memory_id=memory_id_str)

    if skill == "get_cognitive_state":
        ns_id = _require_param(params, "namespace_id")
        agent_id = _require_param(params, "agent_id")
        user_id = params.get("user_id", "default")
        n = max(1, min(int(params.get("n", 10)), 50))

        context = await _engine.recall_recent(
            namespace_id=ns_id,
            agent_id=agent_id,
            limit=n,
            user_id=user_id,
            session_id=agent_id,
        )
        return {"context": context, "n_requested": n}

    if skill == "verify_grant_status":
        sharing_token = params.get("sharing_token")
        grant_id_str = params.get("grant_id")
        grant_id = None
        if grant_id_str is not None:
            grant_id = uuid.UUID(str(grant_id_str).strip())

        from nce.a2a import verify_grant_status as a2a_verify_grant_status

        async with _engine.pg_pool.acquire(timeout=10.0) as conn:
            return await a2a_verify_grant_status(
                conn=conn,
                ctx=caller_ctx,
                sharing_token=sharing_token,
                grant_id=grant_id,
            )

    if skill == "vendors_partner_view":
        ns_id = _require_param(params, "namespace_id")
        node_id = params.get("node_id") or params.get("contractor_id") or params.get("vendor_id")
        if not node_id:
            raise ValueError("node_id is required")

        from nce.vertical_modules.vendors.partner_view import do_partner_view

        view_params = {
            "namespace_id": ns_id,
            "node_id": node_id,
        }
        if caller_ctx.external_scope_id:
            view_params["partner_scope_id"] = str(caller_ctx.external_scope_id)

        return await do_partner_view(_engine, view_params)

    if skill == "vendors_match_contractor":
        ns_id = _require_param(params, "namespace_id")
        job = _require_param(params, "job")
        if not isinstance(job, dict):
            raise ValueError("job parameter must be a dictionary")

        from nce.vertical_modules.vendors.matching import do_match_contractor

        return await do_match_contractor(_engine, {"namespace_id": ns_id, "job": job})

    if skill == "vendors_compute_performance":
        ns_id = _require_param(params, "namespace_id")
        contractor_id = _require_param(params, "contractor_id")
        window = params.get("window")

        from nce.vertical_modules.vendors.performance import do_compute_performance

        return await do_compute_performance(
            _engine,
            {
                "namespace_id": ns_id,
                "contractor_id": contractor_id,
                "window": window,
            },
        )

    if skill == "vendors_recall_similar_jobs":
        ns_id = _require_param(params, "namespace_id")
        query = _require_param(params, "query")
        contractor_id = params.get("contractor_id")
        limit = params.get("limit") or params.get("top_k", 5)

        from nce.vertical_modules.vendors.performance import do_recall_similar_jobs

        return await do_recall_similar_jobs(
            _engine,
            {
                "namespace_id": ns_id,
                "query": query,
                "contractor_id": contractor_id,
                "top_k": limit,
            },
        )

    if skill == "vendors_reliability_radar":
        ns_id = _require_param(params, "namespace_id")

        from nce.vertical_modules.vendors.frontier import do_reliability_radar

        return await do_reliability_radar(_engine, {"namespace_id": ns_id})

    if skill == "vendors_calibrate_weights":
        ns_id = _require_param(params, "namespace_id")

        from nce.vertical_modules.vendors.frontier import do_calibrate_weights

        return await do_calibrate_weights(_engine, {"namespace_id": ns_id})

    raise ValueError(f"Unknown A2A skill: {skill!r}")


# ---------------------------------------------------------------------------
# Health route
# ---------------------------------------------------------------------------


async def get_health(request: Request) -> JSONResponse:
    rejected = await _reject_if_shutting_down(request)
    if rejected:
        return rejected
    if _engine is None:
        return JSONResponse({"status": "down"}, status_code=503)
    async with _track_active_request():
        res = await _engine.check_health()
        return JSONResponse(res)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: Starlette):
    global _engine
    from nce.orchestrator import NCEEngine

    _init_shutdown()

    _engine = NCEEngine()
    await _engine.connect()
    log.info("A2A server: NCEEngine connected.")

    # Zero-trust transport boot guard (Batch 115): refuse to start a prod A2A
    # surface with mTLS disabled unless explicitly acknowledged. Runs after the
    # engine is connected so the acknowledged path can write its WORM audit event.
    from nce.mtls import assert_server_mtls_or_acknowledged

    await assert_server_mtls_or_acknowledged(
        service="a2a",
        mtls_enabled=cfg.NCE_A2A_MTLS_ENABLED,
        pg_pool=_engine.pg_pool,
    )

    # Warm the governance cache as early as possible so production serves the
    # admin-disabled snapshot from the first request. On failure we log and rely
    # on the never-initialized prod-block (next-call retry still attempts a fetch).
    if await GOVERNANCE.warm(_engine.redis_client):
        log.info("A2A server: tool governance cache warmed.")
    else:
        log.warning("A2A server: tool governance warm failed; will retry on first dispatch.")
    yield
    # --- Graceful shutdown phase ---
    log.info(
        "A2A server: initiating graceful shutdown (draining active requests, max %ds)...",
        _GRACE_PERIOD_S,
    )
    if _SHUTDOWN_EVENT is not None:
        _SHUTDOWN_EVENT.set()

    remaining = await _drain_active_requests()
    if remaining > 0:
        log.warning(
            "A2A server: %d request(s) still in-flight after grace period — disconnecting anyway",
            remaining,
        )
    else:
        log.info("A2A server: all active requests completed.")
    await _engine.disconnect()
    _engine = None
    log.info("A2A server: shutdown complete.")


# ---------------------------------------------------------------------------
# ASGI application
# ---------------------------------------------------------------------------

app = Starlette(
    debug=False,
    lifespan=lifespan,
    middleware=[
        Middleware(
            MTLSAuthMiddleware,
            protected_prefix="/tasks",
            enabled=cfg.NCE_A2A_MTLS_ENABLED,
            strict=cfg.NCE_A2A_MTLS_STRICT,
            trusted_proxy_hops=cfg.NCE_A2A_MTLS_TRUSTED_PROXY_HOP,
            allowed_sans=cfg.NCE_A2A_MTLS_ALLOWED_SANS,
            allowed_fingerprints=cfg.NCE_A2A_MTLS_ALLOWED_FINGERPRINTS,
            error_code=A2A_CODE_MTLS,
        ),
        Middleware(
            JWTAuthMiddleware,
            protected_prefix="/tasks",
            # Require a dedicated audience so tokens issued for other
            # services (web frontend, admin UI) are rejected here.
            expected_audience=cfg.NCE_A2A_JWT_AUDIENCE,
        ),
    ],
    routes=[
        Route("/health", endpoint=get_health, methods=["GET"]),
        Route("/.well-known/agent-card", endpoint=get_agent_card, methods=["GET"]),
        Route("/tasks/send", endpoint=tasks_send, methods=["POST"]),
        Route("/tasks/{task_id}", endpoint=tasks_get, methods=["GET"]),
        Route("/tasks/{task_id}/cancel", endpoint=tasks_cancel, methods=["POST"]),
    ],
)


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)

    _init_shutdown()

    def _handle_sigterm(signum: int, frame: object | None) -> None:
        log.info("Received SIGTERM — initiating graceful shutdown.")
        if _SHUTDOWN_EVENT is not None:
            _SHUTDOWN_EVENT.set()

    signal.signal(signal.SIGTERM, _handle_sigterm)
    log.info(
        "SIGTERM handler registered for graceful shutdown (grace period: %ds).",
        _GRACE_PERIOD_S,
    )

    uvicorn.run(app, host="0.0.0.0", port=8004)
