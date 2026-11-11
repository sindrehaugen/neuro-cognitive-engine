"""Zero-Trust Streamable HTTP /mcp Gateway & Progressive Tool Router.

Implements the MCP Streamable HTTP transport:
  - POST /mcp: JSON-RPC requests/batches, returning application/json or text/event-stream
  - GET /mcp: text/event-stream SSE stream supporting Last-Event-ID replay from bounded queue
  - DELETE /mcp: revokes active Mcp-Session-Id
  - GET /healthz: liveness and readiness probe

Features:
  - DNS-rebinding defense via strict Origin allowlist (NCE_MCP_ALLOWED_ORIGINS)
  - Scoped client bearer token verification against mcp_client_tokens (SHA-256)
  - Issuer-pinned RS256/ES256 OIDC JWT verification (HS256 strictly rejected)
  - Progressive tool discovery (compact exposure mode with discover_tools meta-tool)
  - Cross-tenant spoofing prevention (binding authenticated namespace_id into arguments)
  - Sliding-window rate limiting per token/session
  - Fail-closed Redis runtime toggles (nce:tools:disabled & nce:engines:disabled)
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import orjson
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from nce.http_body_limit import BoundedBodyMiddleware

logger = logging.getLogger("nce.mcp_http_gateway")

# Default allowed origins for DNS-rebinding defense
DEFAULT_ALLOWED_ORIGINS: frozenset[str] = frozenset(
    {
        "http://localhost",
        "https://localhost",
        "http://127.0.0.1",
        "https://127.0.0.1",
        "http://localhost:8003",
        "http://localhost:8004",
        "http://localhost:8005",
        "http://localhost:8080",
        "http://localhost:8082",
        "https://localhost:8443",
    }
)

# In-memory test registry for mcp client tokens (when database pool is mocked/absent)
_IN_MEMORY_MCP_CLIENT_TOKENS: dict[str, dict[str, Any]] = {}

# Active gateway sessions keyed by session_id
_GATEWAY_SESSIONS: dict[str, MCPGatewaySession] = {}

# Cached tools/list definition fragments keyed by (exposure_mode, allowed_engines, expanded_tools, count)
_TOOLS_LIST_CACHE: dict[
    tuple[str, tuple[str, ...], tuple[str, ...], int],
    list[dict[str, Any]],
] = {}


@dataclass
class MCPGatewaySession:
    """Session state bound to an authenticated Mcp-Session-Id."""

    session_id: str
    namespace_id: str
    principal_id: str
    scopes: list[str]
    allowed_engines: list[str]
    tool_exposure_mode: str
    expanded_tools: set[str]
    rate_limit_rpm: int
    event_queue: asyncio.Queue[Any]
    last_event_id: int = 0
    event_history: collections.deque[tuple[int, dict[str, Any]]] = field(
        default_factory=lambda: collections.deque(maxlen=256)
    )
    request_timestamps: list[float] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)


def register_test_mcp_client_token(
    token: str,
    *,
    namespace_id: str = "00000000-0000-4000-8000-000000000001",
    principal_id: str = "test-client",
    scopes: list[str] | None = None,
    allowed_engines: list[str] | None = None,
    tool_exposure_mode: str = "compact",
    rate_limit_rpm: int = 120,
    expires_at: Any = None,
    revoked_at: Any = None,
) -> str:
    """Register an in-memory client token for testing without a database."""
    token_clean = token.strip()
    if token_clean.lower().startswith("bearer "):
        token_clean = token_clean[7:].strip()
    token_sha256 = hashlib.sha256(token_clean.encode("utf-8")).hexdigest()
    _IN_MEMORY_MCP_CLIENT_TOKENS[token_sha256] = {
        "token_id": str(uuid.uuid4()),
        "token_sha256": token_sha256,
        "namespace_id": str(namespace_id),
        "principal_id": principal_id,
        "scopes": scopes or ["tools:read", "tools:call"],
        "allowed_engines": allowed_engines or ["*"],
        "tool_exposure_mode": tool_exposure_mode,
        "rate_limit_rpm": rate_limit_rpm,
        "expires_at": expires_at,
        "revoked_at": revoked_at,
    }
    return token_sha256


def get_configured_allowed_origins() -> set[str]:
    """Retrieve the set of allowed Origin values from config and environment."""
    from nce.config import cfg

    origins = set(DEFAULT_ALLOWED_ORIGINS)
    env_origins = os.getenv("NCE_MCP_ALLOWED_ORIGINS", "")
    if env_origins:
        for part in env_origins.split(","):
            part_str = part.strip()
            if part_str:
                origins.add(part_str)

    cfg_allowed = getattr(cfg, "NCE_MCP_ALLOWED_ORIGINS", None)
    if cfg_allowed:
        if isinstance(cfg_allowed, str):
            for part in cfg_allowed.split(","):
                part_str = part.strip()
                if part_str:
                    origins.add(part_str)
        elif isinstance(cfg_allowed, (list, set, tuple)):
            for item in cfg_allowed:
                origins.add(str(item).strip())
    return origins


def validate_origin(origin: str | None, allowed_origins: set[str]) -> bool:
    """Validate that Origin header is in the allowlist for DNS-rebinding defense."""
    if origin is None or origin == "":
        return True

    norm_origin = origin.strip().rstrip("/")
    norm_allowed = {o.strip().rstrip("/") for o in allowed_origins if o}
    if "*" in norm_allowed:
        return True
    return norm_origin in norm_allowed


def is_engine_allowed_for_session(
    engine_slug: str | None,
    allowed_engines: list[str],
    engine_descriptors: list[dict[str, Any]] | None = None,
) -> bool:
    """Check if an engine tool is permitted under the session's engine scope.

    Supports:
      - None (Core memory tools): always allowed
      - '*' or 'all': all engines allowed
      - Specific engine slug matching: 'odoo', 'salesforce'
      - Dual-axis type filter: 'type:erp-crm'
      - Dual-axis category filter: 'cat:commercial' or 'category:commercial'
    """
    if engine_slug is None or engine_slug == "":
        return True

    if not allowed_engines:
        return False

    slug = engine_slug.strip().lower()
    norm_allowed = [a.strip().lower() for a in allowed_engines if a]

    if "*" in norm_allowed or "all" in norm_allowed:
        return True

    if slug in norm_allowed:
        return True

    if engine_descriptors is None:
        try:
            from nce.plugins import list_plugin_engine_descriptors

            engine_descriptors = list_plugin_engine_descriptors()
        except Exception:
            engine_descriptors = []

    desc = None
    for d in engine_descriptors:
        if str(d.get("slug") or d.get("id") or "").lower() == slug:
            desc = d
            break

    if desc is not None:
        desc_types = {str(t).strip().lower() for t in desc.get("types", [])}
        desc_cats = {str(c).strip().lower() for c in desc.get("categories", [])}

        for pattern in norm_allowed:
            if pattern.startswith("type:"):
                type_name = pattern[5:].strip()
                if type_name in desc_types:
                    return True
            elif pattern.startswith("cat:"):
                cat_name = pattern[4:].strip()
                if cat_name in desc_cats:
                    return True
            elif pattern.startswith("category:"):
                cat_name = pattern[9:].strip()
                if cat_name in desc_cats:
                    return True

    return False


async def verify_mcp_token(token: str, pg_pool: Any = None) -> dict[str, Any]:
    """Verify an MCP client bearer token via SHA-256 or issuer-pinned RS256/ES256 OIDC JWT."""
    raw_token = token.strip()
    if raw_token.lower().startswith("bearer "):
        raw_token = raw_token[7:].strip()

    if not raw_token:
        raise ValueError("Empty or missing MCP client bearer token")

    token_sha256 = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    # 1. In-memory test registry & admin plugin token minter registry
    record = _IN_MEMORY_MCP_CLIENT_TOKENS.get(token_sha256)
    if record is None:
        try:
            from nce.admin_plugins import MINTED_MCP_TOKENS

            record = MINTED_MCP_TOKENS.get(token_sha256)
        except Exception:
            record = None

    if record is not None:
        if record.get("revoked_at") is not None:
            raise ValueError("MCP client token has been revoked")
        return {
            "token_id": record.get("token_id"),
            "token_sha256": token_sha256,
            "namespace_id": str(record["namespace_id"]),
            "principal_id": record["principal_id"],
            "scopes": list(record.get("scopes", ["tools:read", "tools:call"])),
            "allowed_engines": list(record.get("allowed_engines", ["*"])),
            "tool_exposure_mode": record.get("tool_exposure_mode", "compact"),
            "rate_limit_rpm": int(record.get("rate_limit_rpm", 120)),
        }

    # 2. Database lookup via lookup_mcp_client_token(p_token_sha256)
    if pg_pool is not None:
        try:
            async with pg_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM lookup_mcp_client_token($1)",
                    token_sha256,
                )
                if row is not None:
                    scopes_val = row["scopes"]
                    if isinstance(scopes_val, str):
                        try:
                            scopes_list = json.loads(scopes_val)
                        except Exception:
                            scopes_list = []
                    elif isinstance(scopes_val, (list, tuple)):
                        scopes_list = list(scopes_val)
                    else:
                        scopes_list = ["tools:read", "tools:call"]

                    engines_val = row["allowed_engines"]
                    allowed_engines = list(engines_val) if engines_val else ["*"]

                    return {
                        "token_id": str(row["token_id"]),
                        "token_sha256": token_sha256,
                        "namespace_id": str(row["namespace_id"]),
                        "principal_id": str(row["principal_id"]),
                        "scopes": scopes_list,
                        "allowed_engines": allowed_engines,
                        "tool_exposure_mode": str(row["tool_exposure_mode"] or "compact"),
                        "rate_limit_rpm": int(row["rate_limit_rpm"] or 120),
                    }
        except Exception as db_exc:
            logger.warning("Database lookup_mcp_client_token failed: %s", db_exc)

    # 3. Issuer-pinned RS256/ES256 OIDC JWT verification
    if raw_token.count(".") == 2:
        try:
            import jwt

            unverified_header = jwt.get_unverified_header(raw_token)
            alg = unverified_header.get("alg")

            # Strictly reject HS256 / symmetric algorithms on the OIDC path
            if (
                not alg
                or alg.startswith("HS")
                or alg
                not in (
                    "RS256",
                    "RS384",
                    "RS512",
                    "ES256",
                    "ES384",
                    "ES512",
                    "PS256",
                    "PS384",
                    "PS512",
                )
            ):
                raise ValueError(
                    f"OIDC JWT algorithm {alg!r} is strictly forbidden; only RS256/ES256 asymmetric algorithms are accepted"
                )

            from nce.config import cfg
            from nce.jwt_auth import _load_public_key

            if not cfg.NCE_JWT_PUBLIC_KEY:
                raise ValueError(
                    "NCE_JWT_PUBLIC_KEY is not configured for asymmetric OIDC JWT verification"
                )

            public_key = _load_public_key(cfg.NCE_JWT_PUBLIC_KEY)
            decode_kwargs: dict[str, Any] = {
                "algorithms": [alg],
                "options": {"verify_signature": True, "verify_exp": True},
            }
            if cfg.NCE_JWT_AUDIENCE:
                decode_kwargs["audience"] = cfg.NCE_JWT_AUDIENCE
            if cfg.NCE_JWT_ISSUER:
                decode_kwargs["issuer"] = cfg.NCE_JWT_ISSUER

            claims = jwt.decode(raw_token, public_key, **decode_kwargs)

            ns_id = claims.get("namespace_id") or claims.get("tid")
            if not ns_id:
                raise ValueError("JWT token missing namespace_id or tid claim")

            principal = (
                claims.get("sub") or claims.get("oid") or claims.get("upn") or "oidc-principal"
            )
            raw_scopes = claims.get("scopes") or claims.get("scp") or ["tools:read", "tools:call"]
            if isinstance(raw_scopes, str):
                scopes = raw_scopes.split()
            else:
                scopes = list(raw_scopes)

            allowed_engines = claims.get("allowed_engines")
            if not allowed_engines or not isinstance(allowed_engines, list):
                allowed_engines = ["*"]

            exposure_mode = claims.get("tool_exposure_mode", "compact")
            rate_limit = int(claims.get("rate_limit_rpm", 120))

            return {
                "token_id": str(uuid.uuid4()),
                "token_sha256": token_sha256,
                "namespace_id": str(ns_id),
                "principal_id": str(principal),
                "scopes": scopes,
                "allowed_engines": allowed_engines,
                "tool_exposure_mode": exposure_mode,
                "rate_limit_rpm": rate_limit,
            }
        except Exception as jwt_exc:
            raise ValueError(f"OIDC JWT verification failed: {jwt_exc}") from jwt_exc

    raise ValueError("Invalid, expired, or revoked MCP client token")


DISCOVER_TOOLS_TOOL: dict[str, Any] = {
    "name": "discover_tools",
    "description": "Progressively discover and activate specialized engine tools matching a query, type_filter, or category_filter.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search keyword matching engine tools, names, and descriptions",
            },
            "type_filter": {
                "type": "string",
                "description": "Filter by canonical engine type slug (core-domain, erp-crm, infra-iot, observability, portal)",
            },
            "category_filter": {
                "type": "string",
                "description": "Filter by canonical engine category slug (commercial, operations-delivery, finance-governance, service-support, av-it-integrator, enterprise-erp, field-service-ops)",
            },
        },
    },
}


async def _enqueue_session_event(session: MCPGatewaySession, event_data: dict[str, Any]) -> None:
    """Enqueue a JSON-RPC notification into the session event queue and bounded replay history."""
    session.last_event_id += 1
    session.event_history.append((session.last_event_id, event_data))

    if session.event_queue.full():
        try:
            session.event_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass

    try:
        session.event_queue.put_nowait(event_data)
    except asyncio.QueueFull:
        pass


async def discover_tools_handler(
    session: MCPGatewaySession,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Handler for discover_tools meta-tool."""
    query = str(arguments.get("query") or "").strip().lower()
    type_filter = arguments.get("type_filter")
    category_filter = arguments.get("category_filter")

    from nce.mcp_stdio_tools import TOOLS
    from nce.plugins import list_plugin_engine_descriptors
    from nce.tool_registry import TOOL_REGISTRY, infer_tool_engine

    try:
        descriptors = list_plugin_engine_descriptors()
    except Exception:
        descriptors = []
    descriptors_by_slug = {d.get("slug"): d for d in descriptors}

    matched_tools: list[dict[str, Any]] = []

    for tool in TOOLS:
        spec = TOOL_REGISTRY.get(tool.name)
        tool_engine = infer_tool_engine(spec) if spec else None

        if not is_engine_allowed_for_session(tool_engine, session.allowed_engines, descriptors):
            continue

        desc = descriptors_by_slug.get(tool_engine) if tool_engine else None

        if type_filter:
            t_wanted = type_filter.strip().lower()
            if not desc or t_wanted not in [t.lower() for t in desc.get("types", [])]:
                continue

        if category_filter:
            c_wanted = category_filter.strip().lower()
            if not desc or c_wanted not in [c.lower() for c in desc.get("categories", [])]:
                continue

        if query:
            name_match = query in tool.name.lower()
            desc_match = query in (tool.description or "").lower()
            eng_match = (query in tool_engine.lower()) if tool_engine else False
            eng_desc_match = (
                (
                    query in desc.get("name", "").lower()
                    or query in desc.get("description", "").lower()
                )
                if desc
                else False
            )
            if not (name_match or desc_match or eng_match or eng_desc_match):
                continue

        session.expanded_tools.add(tool.name)
        matched_tools.append(
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.inputSchema,
            }
        )

    notification = {
        "jsonrpc": "2.0",
        "method": "notifications/tools/list_changed",
        "params": {},
    }
    await _enqueue_session_event(session, notification)

    return {
        "tools": matched_tools,
        "matched_count": len(matched_tools),
        "total_expanded": len(session.expanded_tools),
    }


def _check_session_rate_limit(session: MCPGatewaySession) -> bool:
    """Sliding-window rate limiter checking rate_limit_rpm. Returns True if OK, False if exceeded."""
    now = time.time()
    cutoff = now - 60.0
    session.request_timestamps = [t for t in session.request_timestamps if t >= cutoff]
    if len(session.request_timestamps) >= session.rate_limit_rpm:
        return False
    session.request_timestamps.append(now)
    return True


def create_mcp_gateway_app(engine: Any = None) -> Starlette:
    """Construct and configure the Starlette ASGI application for the /mcp gateway."""

    async def healthz_endpoint(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "service": "mcp-gateway",
                "version": "3.3.0",
            }
        )

    async def _resolve_session(
        request: Request,
    ) -> tuple[MCPGatewaySession | None, JSONResponse | None]:
        session_id = request.headers.get("mcp-session-id") or request.query_params.get("session_id")
        auth_header = request.headers.get("authorization", "")
        if session_id and session_id in _GATEWAY_SESSIONS:
            session = _GATEWAY_SESSIONS[session_id]
            if auth_header.lower().startswith("bearer "):
                token = auth_header[7:].strip()
                pg_pool = getattr(engine, "pg_pool", None) if engine else None
                try:
                    token_data = await verify_mcp_token(token, pg_pool=pg_pool)
                except Exception as exc:
                    return None, JSONResponse({"error": f"Unauthorized: {exc}"}, status_code=401)
                if str(token_data.get("namespace_id")) != str(session.namespace_id):
                    return None, JSONResponse(
                        {"error": "Forbidden: session namespace mismatch"},
                        status_code=403,
                    )
            if not _check_session_rate_limit(session):
                return None, JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "error": {
                            "code": -32029,
                            "message": "Rate limit exceeded",
                            "data": {"retry_after_s": 60},
                        },
                    },
                    status_code=429,
                    headers={"Retry-After": "60"},
                )
            return session, None
        if not auth_header.lower().startswith("bearer "):
            return None, JSONResponse(
                {"error": "Unauthorized: valid Bearer token or Mcp-Session-Id required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        token = auth_header[7:].strip()
        pg_pool = getattr(engine, "pg_pool", None) if engine else None
        try:
            token_data = await verify_mcp_token(token, pg_pool=pg_pool)
        except Exception as exc:
            return None, JSONResponse({"error": f"Unauthorized: {exc}"}, status_code=401)

        new_session_id = f"mcp-session-{uuid.uuid4()}"
        session = MCPGatewaySession(
            session_id=new_session_id,
            namespace_id=token_data["namespace_id"],
            principal_id=token_data["principal_id"],
            scopes=token_data.get("scopes", ["tools:read", "tools:call"]),
            allowed_engines=token_data.get("allowed_engines", ["*"]),
            tool_exposure_mode=token_data.get("tool_exposure_mode", "compact"),
            expanded_tools=set(),
            rate_limit_rpm=token_data.get("rate_limit_rpm", 120),
            event_queue=asyncio.Queue(maxsize=256),
            last_event_id=0,
        )
        _GATEWAY_SESSIONS[new_session_id] = session

        if not _check_session_rate_limit(session):
            return None, JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32029,
                        "message": "Rate limit exceeded",
                        "data": {"retry_after_s": 60},
                    },
                },
                status_code=429,
                headers={"Retry-After": "60"},
            )

        return session, None

    async def _handle_jsonrpc_request(
        session: MCPGatewaySession, req_data: dict[str, Any]
    ) -> dict[str, Any]:
        req_id = req_data.get("id")
        method = req_data.get("method")
        params = req_data.get("params") or {}

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {"listChanged": True},
                    },
                    "serverInfo": {
                        "name": "nce-memory-gateway",
                        "version": "3.3.0",
                    },
                },
            }

        if method == "notifications/initialized":
            return {}

        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}

        if method == "tools/list":
            from nce.mcp_stdio_tools import TOOLS
            from nce.plugins import list_plugin_engine_descriptors
            from nce.tool_registry import TOOL_REGISTRY, infer_tool_engine

            cache_key = (
                session.tool_exposure_mode,
                tuple(sorted(session.allowed_engines)),
                tuple(sorted(session.expanded_tools)),
                len(TOOLS),
            )

            if cache_key in _TOOLS_LIST_CACHE:
                exposed_tools = _TOOLS_LIST_CACHE[cache_key]
            else:
                try:
                    descriptors = list_plugin_engine_descriptors()
                except Exception:
                    descriptors = []

                exposed_tools = [DISCOVER_TOOLS_TOOL]

                if session.tool_exposure_mode == "compact":
                    for tool in TOOLS:
                        spec = TOOL_REGISTRY.get(tool.name)
                        tool_engine = infer_tool_engine(spec) if spec else None
                        if tool_engine is None:
                            # Core memory tool
                            exposed_tools.append(
                                {
                                    "name": tool.name,
                                    "description": tool.description,
                                    "inputSchema": tool.inputSchema,
                                }
                            )
                        elif tool.name in session.expanded_tools:
                            if is_engine_allowed_for_session(
                                tool_engine, session.allowed_engines, descriptors
                            ):
                                exposed_tools.append(
                                    {
                                        "name": tool.name,
                                        "description": tool.description,
                                        "inputSchema": tool.inputSchema,
                                    }
                                )
                else:
                    for tool in TOOLS:
                        spec = TOOL_REGISTRY.get(tool.name)
                        tool_engine = infer_tool_engine(spec) if spec else None
                        if is_engine_allowed_for_session(
                            tool_engine, session.allowed_engines, descriptors
                        ):
                            exposed_tools.append(
                                {
                                    "name": tool.name,
                                    "description": tool.description,
                                    "inputSchema": tool.inputSchema,
                                }
                            )

                _TOOLS_LIST_CACHE[cache_key] = exposed_tools

            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": exposed_tools,
                },
            }

        if method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments") or {}

            if not tool_name:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32602, "message": "Missing tool name in params"},
                }

            if tool_name == "discover_tools":
                res = await discover_tools_handler(session, arguments)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(res)}],
                        "isError": False,
                    },
                }

            from nce.config import cfg
            from nce.plugins import list_plugin_engine_descriptors
            from nce.tool_governance import GOVERNANCE, GovernanceUnavailable
            from nce.tool_registry import TOOL_REGISTRY, infer_tool_engine

            spec = TOOL_REGISTRY.get(tool_name)
            if spec is None:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32602, "message": f"Unknown tool: '{tool_name}'"},
                }

            tool_engine = getattr(spec, "engine", None) or infer_tool_engine(spec)
            try:
                descriptors = list_plugin_engine_descriptors()
            except Exception:
                descriptors = []

            # Check engine scope before executing
            if not is_engine_allowed_for_session(tool_engine, session.allowed_engines, descriptors):
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32005,
                        "message": "Engine scope forbidden for token",
                    },
                }

            # Inoculate against cross-tenant spoofing: inject session namespace_id
            arguments["namespace_id"] = session.namespace_id

            redis_client = getattr(engine, "redis_client", None) if engine else None
            is_prod = bool(
                getattr(cfg, "IS_PROD", False)
                or os.environ.get("NCE_ENV", "").strip().lower() in ("production", "prod")
            )

            # Tool governance gate (fail-closed in production)
            try:
                if await GOVERNANCE.is_disabled(redis_client, tool_name):
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {
                            "code": -32005,
                            "message": "Scope forbidden",
                            "data": {
                                "detail": f"Tool '{tool_name}' has been disabled by administrator."
                            },
                        },
                    }
            except GovernanceUnavailable:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32005,
                        "message": "Scope forbidden",
                        "data": {
                            "detail": "Tool governance registry unavailable; dispatch blocked."
                        },
                    },
                }
            except Exception:
                if is_prod:
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {
                            "code": -32005,
                            "message": "Scope forbidden",
                            "data": {
                                "detail": "Tool governance check failed in production (fail-closed)."
                            },
                        },
                    }

            # Engine runtime disable gate (fail-closed in production)
            if tool_engine:
                try:
                    from nce.admin_plugins import is_engine_disabled

                    if await is_engine_disabled(
                        tool_engine, redis_client=redis_client, is_prod=is_prod
                    ):
                        return {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "error": {
                                "code": -32001,
                                "message": f"Engine '{tool_engine}' is currently disabled by administrator.",
                            },
                        }
                except GovernanceUnavailable:
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {
                            "code": -32005,
                            "message": "Scope forbidden",
                            "data": {
                                "detail": "Tool governance registry unavailable; dispatch blocked."
                            },
                        },
                    }
                except Exception:
                    if is_prod:
                        return {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "error": {
                                "code": -32005,
                                "message": "Scope forbidden",
                                "data": {
                                    "detail": "Engine governance unavailable in production (fail-closed)."
                                },
                            },
                        }

            # Migration gate (default NCE_DISABLE_MIGRATION_MCP=true on HTTP gateway)
            disable_migration = os.getenv("NCE_DISABLE_MIGRATION_MCP", "true").strip().lower() in (
                "1",
                "true",
                "yes",
            ) or getattr(cfg, "NCE_DISABLE_MIGRATION_MCP", False)
            if getattr(spec, "migration", False) and disable_migration:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": "Migration tools are disabled (NCE_DISABLE_MIGRATION_MCP=true).",
                            }
                        ],
                        "isError": False,
                    },
                }

            if getattr(spec, "admin_only", False):
                if "admin" not in session.scopes:
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {
                            "code": -32005,
                            "message": "Scope forbidden",
                            "data": {"detail": "Admin scope required for this tool."},
                        },
                    }

            # Execute tool handler
            try:
                raw_result = await spec.handler(engine, arguments)

                if getattr(spec, "mutation", False) and redis_client:
                    try:
                        from nce.mcp_args import bump_cache_generation

                        await bump_cache_generation(redis_client, engine=tool_engine)
                    except Exception:
                        pass

                if isinstance(raw_result, str):
                    res_text = raw_result
                elif isinstance(raw_result, list):
                    res_text = json.dumps([getattr(item, "text", str(item)) for item in raw_result])
                elif isinstance(raw_result, dict):
                    res_text = json.dumps(raw_result)
                else:
                    res_text = str(raw_result)

                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": res_text}],
                        "isError": False,
                    },
                }
            except Exception as handler_exc:
                logger.exception("Error executing tool %s: %s", tool_name, handler_exc)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32603,
                        "message": "Internal error",
                        "data": str(handler_exc),
                    },
                }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: '{method}'"},
        }

    async def mcp_post_endpoint(request: Request) -> Response:
        allowed_origins = get_configured_allowed_origins()
        origin = request.headers.get("origin")
        if not validate_origin(origin, allowed_origins):
            return JSONResponse({"error": "Forbidden: invalid origin"}, status_code=403)

        session, error_resp = await _resolve_session(request)
        if error_resp is not None:
            return error_resp

        assert session is not None

        try:
            body_bytes = await request.body()
            body_json = json.loads(body_bytes)
        except Exception as parse_exc:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Parse error", "data": str(parse_exc)},
                },
                status_code=400,
                headers={"Mcp-Session-Id": session.session_id},
            )

        accept_header = request.headers.get("accept", "")
        wants_sse = "text/event-stream" in accept_header

        if isinstance(body_json, list):
            results = []
            for item in body_json:
                res = await _handle_jsonrpc_request(session, item)
                if res:
                    results.append(res)
            response_payload = results
        else:
            req_method = body_json.get("method") if isinstance(body_json, dict) else None
            response_payload = await _handle_jsonrpc_request(session, body_json)
            if not wants_sse and req_method == "tools/list":
                content_bytes = orjson.dumps(response_payload)
                return Response(
                    content=content_bytes,
                    media_type="application/json",
                    headers={"Mcp-Session-Id": session.session_id},
                )

        if wants_sse:

            async def sse_response_generator():
                data_str = json.dumps(response_payload)
                yield f"event: message\ndata: {data_str}\n\n"

            return StreamingResponse(
                sse_response_generator(),
                media_type="text/event-stream",
                headers={
                    "Mcp-Session-Id": session.session_id,
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )

        return JSONResponse(
            response_payload,
            headers={"Mcp-Session-Id": session.session_id},
        )

    async def mcp_get_endpoint(request: Request) -> Response:
        allowed_origins = get_configured_allowed_origins()
        origin = request.headers.get("origin")
        if not validate_origin(origin, allowed_origins):
            return JSONResponse({"error": "Forbidden: invalid origin"}, status_code=403)

        session, error_resp = await _resolve_session(request)
        if error_resp is not None:
            return error_resp

        assert session is not None

        last_event_id_header = request.headers.get("last-event-id")
        last_event_id: int | None = None
        if last_event_id_header:
            try:
                last_event_id = int(last_event_id_header.strip())
            except ValueError:
                pass

        async def sse_event_stream():
            if last_event_id is not None:
                for eid, item in list(session.event_history):
                    if eid > last_event_id:
                        yield f"id: {eid}\nevent: message\ndata: {json.dumps(item)}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(session.event_queue.get(), timeout=1.0)
                    eid = session.last_event_id
                    yield f"id: {eid}\nevent: message\ndata: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                except asyncio.CancelledError:
                    break

        return StreamingResponse(
            sse_event_stream(),
            media_type="text/event-stream",
            headers={
                "Mcp-Session-Id": session.session_id,
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    async def mcp_delete_endpoint(request: Request) -> Response:
        allowed_origins = get_configured_allowed_origins()
        origin = request.headers.get("origin")
        if not validate_origin(origin, allowed_origins):
            return JSONResponse({"error": "Forbidden: invalid origin"}, status_code=403)

        session_id = request.headers.get("mcp-session-id") or request.query_params.get("session_id")
        if session_id and session_id in _GATEWAY_SESSIONS:
            del _GATEWAY_SESSIONS[session_id]
            return Response(status_code=204)
        return JSONResponse({"error": "Session not found"}, status_code=404)

    routes = [
        Route("/healthz", healthz_endpoint, methods=["GET"]),
        Route("/mcp", mcp_post_endpoint, methods=["POST"]),
        Route("/mcp", mcp_get_endpoint, methods=["GET"]),
        Route("/mcp", mcp_delete_endpoint, methods=["DELETE"]),
    ]

    return Starlette(
        debug=False,
        routes=routes,
        middleware=[Middleware(BoundedBodyMiddleware)],
    )
