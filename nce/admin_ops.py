"""Day-2 Operations Control Plane REST API and Pure-ASGI Dual-Auth Spine.

Provides:
  - Pure-ASGI ``AdminDualAuthMiddleware`` (subclasses ``HMACAuthMiddleware`` with
    zero Starlette base-HTTP wrapper classes, keeping ``/healthz`` < 3.0 ms).
  - Browser session lifecycle (``POST /admin/session/login``,
    ``POST /admin/session/logout``, ``GET /admin/session/status``) issuing
    ``8h`` ``HttpOnly; SameSite=Strict; Path=/admin`` signed cookies and
    double-submit ``nce_admin_csrf`` tokens.
  - Day-2 operations REST endpoints under ``/admin/ops/api/*``:
      * ``GET  /admin/ops/api/overview``
      * ``GET  /admin/ops/api/backups``
      * ``POST /admin/ops/api/backups/trigger``
      * ``POST /admin/ops/api/backups/{backup_id}/drill``
      * ``GET  /admin/ops/api/incidents``
      * ``GET  /admin/ops/api/quarantine``
      * ``POST /admin/ops/api/quarantine/{quarantine_id}/requeue``
      * ``POST /admin/ops/api/quarantine/{quarantine_id}/dismiss``
      * ``POST /admin/ops/api/playbooks/{playbook_id}/preview``
      * ``POST /admin/ops/api/playbooks/{playbook_id}/execute``
      * ``GET  /admin/ops/api/tenants``
      * ``POST /admin/ops/api/tokens/mint``
      * ``POST /admin/ops/api/tokens/{token_id}/revoke``
  - PostgreSQL ``runtime_tool_governance`` write-through and Redis sentinel
    (``nce:governance:cache_hydrated``) auto-hydration helpers.
  - Air-gapped unified 5-Tab Console static asset serving under ``/admin/*``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import importlib
import inspect
import json
import logging
import os
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import httpx
from cachetools import TTLCache
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from nce import admin_state
from nce.auth import (
    _CODE_AUTH_FAILED,
    _CODE_REPLAY,
    _FAST_PROBE_PATHS,
    _NONCE_TTL_SECONDS,
    _TIMESTAMP_DRIFT_SECONDS,
    HMACAuthContext,
    HMACAuthMiddleware,
    _admin_http_client_ip,
    _jsonrpc_error,
    hmac_principal_scope,
    validate_hmac_nonce,
    verify_hmac,
)
from nce.config import cfg, live_admin_api_key, resolve_secret
from nce.connectors.entra import EntraConfig, EntraOIDCVerifier, EntraVerificationError
from nce.db_utils import (
    POOL_ACQUIRE_TIMEOUT,
    resolve_worker_dsn,
    scoped_pg_session,
    worker_dsn_is_segregated,
)
from nce.event_log import EXPECTED_TENANT_RLS_TABLES
from nce.http_body_limit import _build_413_response
from nce.system_namespace import SYSTEM_NAMESPACE_SLUG

logger = logging.getLogger("nce-admin-ops")

UTC = timezone.utc

SESSION_COOKIE_NAME = "nce_admin_session"
CSRF_COOKIE_NAME = "nce_admin_csrf"
SESSION_TTL_SECONDS = 8 * 3600

GOVERNANCE_CACHE_HYDRATED_KEY = "nce:governance:cache_hydrated"
REDIS_DISABLED_ENGINES_KEY = "nce:engines:disabled"
REDIS_DISABLED_TOOLS_KEY = "nce:tools:disabled"

_ADMIN_DIR = Path(__file__).resolve().parent.parent / "admin"
_MUTATING_METHODS: frozenset[str] = frozenset({"POST", "PUT", "DELETE", "PATCH"})
_PLAYBOOK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

OPS_ALLOWED_MCP_TOKEN_SCOPES: frozenset[str] = frozenset(
    {
        "memory:read",
        "memory:write",
        "graph:read",
        "admin",
        "mcp:tools:discover",
        "mcp:tools:call",
    }
)

DEFAULT_PLAYBOOKS: tuple[dict[str, str], ...] = (
    {
        "playbook_id": "flush_salience_buffer",
        "title": "Flush Salience Buffer",
        "description": "Drain buffered Redis salience increments into PostgreSQL.",
    },
    {
        "playbook_id": "reclaim_stale_locks",
        "title": "Reclaim Stale Leases & Locks",
        "description": "Inspect and release expired backup leases and stale advisory locks.",
    },
    {
        "playbook_id": "reconcile_orphan_blobs",
        "title": "Reconcile Orphan Blobs",
        "description": "Scan MinIO and MongoDB for unreferenced blobs beyond retention.",
    },
    {
        "playbook_id": "replay_quarantine_batch",
        "title": "Replay Quarantined RQ Jobs",
        "description": "Re-enqueue eligible quarantined background jobs after remediation.",
    },
    {
        "playbook_id": "vacuum_high_churn_tables",
        "title": "Vacuum High-Churn Tables",
        "description": "Run ANALYZE / maintenance check on memory_salience, kg_edges, and ops_probe_runs.",
    },
)

# Optional test/integration seam hooks (resolved lazily from nce.ops.* when None)
create_watermark_backup: Any = None
run_ephemeral_restore_drill: Any = None
preview_playbook: Any = None
execute_playbook: Any = None
_ENTRA_VERIFIER: EntraOIDCVerifier | None = None

_EPHEMERAL_SESSION_SECRET = secrets.token_hex(32)
_REVOKED_SESSION_JTIS: TTLCache[str, bool] = TTLCache(maxsize=4096, ttl=SESSION_TTL_SECONDS)
_MEM_NONCE_CACHE: TTLCache[str, bool] = TTLCache(maxsize=20000, ttl=_NONCE_TTL_SECONDS)
_MEM_LOGIN_RL: TTLCache[str, list[float]] = TTLCache(maxsize=4096, ttl=300)
_CACHE_LOCK = threading.Lock()


def clear_admin_ops_caches() -> None:
    """Reset module-level nonce, login rate-limit, and revoked session caches (for tests)."""
    with _CACHE_LOCK:
        _REVOKED_SESSION_JTIS.clear()
        _MEM_NONCE_CACHE.clear()
        _MEM_LOGIN_RL.clear()


def _is_prod_mode() -> bool:
    return bool(
        cfg.IS_PROD or os.environ.get("NCE_ENV", "").strip().lower() in ("production", "prod")
    )


def _session_signing_key() -> bytes:
    """Derive a process-consistent HMAC-SHA256 key for signing browser session cookies."""
    material = (
        resolve_secret("NCE_MASTER_KEY")
        or cfg.NCE_MASTER_KEY
        or cfg.NCE_API_KEY
        or live_admin_api_key()
        or _EPHEMERAL_SESSION_SECRET
    )
    return hashlib.sha256(b"nce-admin-session-v1:" + str(material).encode("utf-8")).digest()


def _admin_api_keys() -> list[str]:
    """Return deduplicated list of non-empty configured admin/HMAC API keys."""
    candidates = [
        live_admin_api_key(),
        cfg.NCE_API_KEY,
        os.environ.get("NCE_ADMIN_API_KEY", ""),
        os.environ.get("NCE_API_KEY", ""),
    ]
    out: list[str] = []
    for key in candidates:
        cleaned = (key or "").strip()
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def _hmac_candidate_keys(extra_key: str = "") -> list[str]:
    """Return deduplicated HMAC verification keys (live cfg + admin key + optional middleware key)."""
    candidates = [
        cfg.NCE_API_KEY,
        live_admin_api_key(),
        os.environ.get("NCE_API_KEY", ""),
        os.environ.get("NCE_ADMIN_API_KEY", ""),
        extra_key,
    ]
    out: list[str] = []
    for key in candidates:
        cleaned = (key or "").strip()
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def _parse_cookies(raw_cookie: str) -> dict[str, str]:
    """Parse HTTP Cookie header safely into a name -> value dict."""
    if not raw_cookie:
        return {}
    parsed: dict[str, str] = {}
    jar: SimpleCookie = SimpleCookie()
    try:
        jar.load(raw_cookie)
        for name, morsel in jar.items():
            parsed[name] = morsel.value
    except Exception:
        pass
    if parsed:
        return parsed
    for segment in raw_cookie.split(";"):
        name, sep, val = segment.strip().partition("=")
        if sep and name:
            parsed[name.strip()] = val.strip()
    return parsed


def _check_memory_nonce(scoped_nonce: str) -> bool:
    """Atomically test-and-set *scoped_nonce* in the bounded TTL in-memory cache."""
    with _CACHE_LOCK:
        if scoped_nonce in _MEM_NONCE_CACHE:
            return False
        _MEM_NONCE_CACHE[scoped_nonce] = True
        return True


def _is_session_jti_revoked_mem(jti: str) -> bool:
    with _CACHE_LOCK:
        return jti in _REVOKED_SESSION_JTIS


def _revoke_session_jti_mem(jti: str) -> None:
    if not jti:
        return
    with _CACHE_LOCK:
        _REVOKED_SESSION_JTIS[jti] = True


def mint_admin_session_cookie(
    *,
    principal: str = "admin",
    auth_mode: str = "api_key",
    ttl_seconds: int = SESSION_TTL_SECONDS,
) -> tuple[str, str, int]:
    """Create a signed ``nce_admin_session`` value, CSRF token, and expiry epoch."""
    now = int(time.time())
    exp = now + max(60, int(ttl_seconds))
    csrf_token = secrets.token_urlsafe(32)
    jti = secrets.token_urlsafe(16)
    payload = {
        "sub": (principal or "admin").strip()[:128],
        "auth_mode": auth_mode,
        "iat": now,
        "exp": exp,
        "jti": jti,
        "csrf": csrf_token,
    }
    raw_json = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body_b64 = base64.urlsafe_b64encode(raw_json).decode("ascii")
    sig_hex = hmac.new(_session_signing_key(), body_b64.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body_b64}.{sig_hex}", csrf_token, exp


def verify_admin_session_cookie(cookie_val: str | None) -> dict[str, Any] | None:
    """Verify HMAC signature, TTL, and memory revocation for ``nce_admin_session``."""
    if not cookie_val or not isinstance(cookie_val, str):
        return None
    raw = cookie_val.strip()
    body_b64, sep, sig_hex = raw.partition(".")
    if not sep or not body_b64 or not sig_hex:
        return None
    expected_sig = hmac.new(
        _session_signing_key(), body_b64.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig_hex.strip().lower(), expected_sig.lower()):
        return None
    try:
        padded = body_b64 + ("=" * ((4 - len(body_b64) % 4) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        exp = int(payload.get("exp", 0))
    except (ValueError, TypeError):
        return None
    if exp <= int(time.time()):
        return None
    csrf = payload.get("csrf")
    if not isinstance(csrf, str) or not csrf:
        return None
    jti = str(payload.get("jti") or "")
    if jti and _is_session_jti_revoked_mem(jti):
        return None
    return payload


async def _is_session_revoked_redis(jti: str, redis_client: Any = None) -> bool:
    if not jti:
        return False
    if _is_session_jti_revoked_mem(jti):
        return True
    rc = redis_client or _resolve_redis_client()
    if rc is None:
        return False
    try:
        exists_fn = getattr(rc, "exists", None)
        if callable(exists_fn):
            res = exists_fn(f"nce:admin:revoked_sess:{jti}")
            if inspect.isawaitable(res):
                res = await res
            if bool(res):
                _revoke_session_jti_mem(jti)
                return True
    except Exception:
        pass
    return False


def verify_admin_csrf(
    headers: Headers,
    cookies: dict[str, str],
    session: dict[str, Any],
) -> bool:
    """Validate double-submit ``X-NCE-CSRF-Token`` against session and ``nce_admin_csrf`` cookie."""
    header_token = (headers.get("x-nce-csrf-token") or "").strip()
    session_csrf = str(session.get("csrf") or "")
    if not header_token or not session_csrf:
        return False
    if not hmac.compare_digest(header_token, session_csrf):
        return False
    cookie_csrf = (cookies.get(CSRF_COOKIE_NAME) or "").strip()
    if cookie_csrf and not hmac.compare_digest(header_token, cookie_csrf):
        return False
    return True


def _should_use_secure_cookie(request: Request) -> bool:
    """Return True when served over HTTPS (direct or Caddy) or non-testserver prod."""
    proto = (request.headers.get("x-forwarded-proto") or "").strip().lower()
    if request.url.scheme == "https" or proto == "https":
        return True
    host = (request.headers.get("host") or "").split(":")[0].strip().lower()
    if _is_prod_mode() and host not in ("testserver", ""):
        return True
    return False


def _build_session_cookie_header(
    name: str,
    value: str,
    *,
    max_age: int,
    http_only: bool,
    secure: bool,
) -> str:
    parts = [
        f"{name}={value}",
        "Path=/admin",
        f"Max-Age={max_age}",
        "SameSite=Strict",
    ]
    if http_only:
        parts.append("HttpOnly")
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def _resolve_redis_client(request: Request | None = None, ctx: Any = None) -> Any:
    if ctx is not None and getattr(ctx, "redis_client", None) is not None:
        return getattr(ctx, "redis_client", None)
    if request is not None:
        app_obj = getattr(request, "app", None)
        app_state = getattr(app_obj, "state", None) if app_obj is not None else None
        if getattr(app_state, "redis_client", None) is not None:
            return getattr(app_state, "redis_client", None)
    eng = getattr(admin_state, "engine", None)
    if eng is not None:
        return getattr(eng, "redis_client", None)
    return None


def _resolve_nonce_store(fallback: Any = None) -> Any:
    admin_app_mod = sys.modules.get("nce.admin_app")
    if admin_app_mod is not None and hasattr(admin_app_mod, "_hmac_nonce_store"):
        return getattr(admin_app_mod, "_hmac_nonce_store")
    return fallback


async def _verify_entra_bearer(token: str) -> tuple[str, str] | None:
    """Validate a JWT Bearer token via Microsoft Entra OIDC verifier if configured."""
    verifier = _ENTRA_VERIFIER
    enforce_mode = (
        (os.getenv("NCE_ENTRA_ENFORCE_MODE", "") or cfg.NCE_ENTRA_ENFORCE_MODE or "")
        .strip()
        .lower()
    )
    tenant_id = (os.getenv("NCE_ENTRA_TENANT_ID", "") or cfg.NCE_ENTRA_TENANT_ID or "").strip()
    jwks_uri = os.getenv("NCE_ENTRA_JWKS_URI", "").strip()

    if verifier is None:
        if not (tenant_id or jwks_uri or enforce_mode == "strict"):
            return None
        verifier = EntraOIDCVerifier(EntraConfig())

    principal = await verifier.verify_token(token)
    allowlist = tuple(getattr(cfg, "NCE_OIDC_ADMIN_SCOPES_ALLOWLIST", ()) or ())
    if allowlist:
        claims_set = set(principal.roles) | set(principal.scopes)
        if not (set(allowlist) & claims_set):
            raise EntraVerificationError("Entra principal lacks required admin role/scope")
    return (principal.upn or principal.oid or principal.sub or "entra-admin"), "entra_oidc"


async def _check_login_rate_limit(redis_client: Any, client_ip: str) -> bool:
    """Rate-limit login attempts via Redis ``nce:admin:login_rl:{client_ip}`` or RAM."""
    limit = max(1, int(os.environ.get("NCE_ADMIN_LOGIN_RATE_LIMIT", "15")))
    period = max(1, int(os.environ.get("NCE_ADMIN_LOGIN_RATE_PERIOD", "60")))
    key = f"nce:admin:login_rl:{client_ip}"

    if redis_client is not None:
        try:
            incr_fn = getattr(redis_client, "incr", None)
            if callable(incr_fn):
                count_res = incr_fn(key)
                if inspect.isawaitable(count_res):
                    count_res = await count_res
                count_val = int(count_res)
                if count_val == 1:
                    expire_fn = getattr(redis_client, "expire", None)
                    if callable(expire_fn):
                        exp_res = expire_fn(key, period)
                        if inspect.isawaitable(exp_res):
                            await exp_res
                return count_val <= limit
        except Exception as exc:
            logger.warning("Redis login rate limit fallback to memory: %s", exc)

    now = time.time()
    with _CACHE_LOCK:
        stamps = [t for t in _MEM_LOGIN_RL.get(key, []) if t > now - period]
        if len(stamps) >= limit:
            _MEM_LOGIN_RL[key] = stamps
            return False
        stamps.append(now)
        _MEM_LOGIN_RL[key] = stamps
        return True


def _extract_hmac_signature(headers: Headers) -> tuple[str, bool]:
    """Extract hex signature from ``X-NCE-Signature`` or ``Authorization: HMAC-SHA256``."""
    auth_hdr = headers.get("authorization", "").strip()
    if auth_hdr:
        scheme, _, sig = auth_hdr.partition(" ")
        if scheme.upper() == "HMAC-SHA256":
            return sig.strip(), True
    x_sig = headers.get("x-nce-signature", "").strip()
    if x_sig:
        if x_sig.lower().startswith("hmac-sha256 "):
            return x_sig.split(" ", 1)[1].strip(), True
        return x_sig, True
    return "", False


async def _verify_hmac_with_replay(
    *,
    method: str,
    path: str,
    headers: Headers,
    body_bytes: bytes,
    candidate_keys: list[str],
    nonce_store: Any,
    nonce_required: bool = True,
) -> tuple[HMACAuthContext | None, JSONResponse | None]:
    """Verify 3-header HMAC-SHA256 (``Authorization`` or ``X-NCE-Signature``) + replay."""
    ts_raw = headers.get("x-nce-timestamp", "").strip()
    nonce_raw = headers.get("x-nce-nonce", "").strip()
    sig_val, has_sig_scheme = _extract_hmac_signature(headers)

    if not ts_raw or not sig_val or not has_sig_scheme:
        return None, _jsonrpc_error(
            _CODE_AUTH_FAILED, "Authentication failed", "missing_auth_headers"
        )

    try:
        ctx = HMACAuthContext(timestamp=int(ts_raw), signature=sig_val, nonce=nonce_raw)
    except Exception:
        return None, _jsonrpc_error(
            _CODE_AUTH_FAILED, "Authentication failed", "malformed_auth_headers"
        )

    now = int(time.time())
    if abs(now - ctx.timestamp) > _TIMESTAMP_DRIFT_SECONDS:
        return None, _jsonrpc_error(
            _CODE_REPLAY, "Request timestamp out of acceptable range", "replay_or_clock_skew"
        )

    if not ctx.nonce:
        return None, _jsonrpc_error(_CODE_AUTH_FAILED, "Authentication failed", "nonce_missing")
    if not validate_hmac_nonce(ctx.nonce):
        return None, _jsonrpc_error(_CODE_AUTH_FAILED, "Authentication failed", "invalid_nonce")

    matched_key: str | None = None
    for key in candidate_keys:
        if key and verify_hmac(
            key, method, path, ctx.timestamp, body_bytes, ctx.signature, nonce=ctx.nonce
        ):
            matched_key = key
            break
    if matched_key is None:
        return None, _jsonrpc_error(_CODE_AUTH_FAILED, "Authentication failed", "invalid_signature")

    scoped_nonce = f"{hmac_principal_scope(matched_key)}:{ctx.nonce}"
    is_prod = _is_prod_mode()

    if nonce_store is None:
        if is_prod:
            return None, _jsonrpc_error(
                _CODE_REPLAY, "NonceStore required in production", "nonce_store_unavailable"
            )
        if not _check_memory_nonce(scoped_nonce):
            return None, _jsonrpc_error(
                _CODE_REPLAY, "Request already processed (replay detected)", "replay_nonce_conflict"
            )
        return ctx, None

    ping_fn = getattr(nonce_store, "ping", None)
    redis_up = await ping_fn() if callable(ping_fn) else True
    if not redis_up:
        if is_prod:
            return None, _jsonrpc_error(
                _CODE_REPLAY, "Authentication failed", "nonce_store_unavailable"
            )
        if not _check_memory_nonce(scoped_nonce):
            return None, _jsonrpc_error(
                _CODE_REPLAY, "Request already processed (replay detected)", "replay_nonce_conflict"
            )
        return ctx, None

    if not await nonce_store.check_and_store(scoped_nonce):
        return None, _jsonrpc_error(
            _CODE_REPLAY, "Request already processed (replay detected)", "replay_nonce_conflict"
        )

    return ctx, None


class AdminDualAuthMiddleware(HMACAuthMiddleware):
    """Pure-ASGI Dual-Auth & CSRF middleware (zero Starlette base wrapper).

    Supports:
      - Sub-millisecond fast-probe bypass on ``/healthz``, ``/readyz``, etc.
      - Browser ``nce_admin_session`` cookie + ``X-NCE-CSRF-Token`` enforcement on
        ``/admin/ops/api/*``, ``/admin/plugins*``, and ``/api/admin/*``.
      - Backwards-compatible 3-header HMAC-SHA256 (``Authorization: HMAC-SHA256``
        and ``X-NCE-Signature``) with Redis/in-memory nonce replay rejection on
        ``/admin/ops/api/*``, ``/admin/plugins/api*``, and ``/api/*``.
      - Constant-time Admin API key & Entra ID Bearer JWT on ``/admin/ops/api/*``.
    """

    def __init__(
        self,
        app: Any,
        *,
        protected_prefix: str = "/api/",
        api_key: str = "",
        nonce_store: Any = None,
        nonce_required: bool | None = None,
        dynamic_config: bool = True,
    ) -> None:
        super().__init__(
            app,
            protected_prefix=protected_prefix,
            api_key=api_key,
            nonce_store=nonce_store,
            nonce_required=nonce_required,
        )
        self._dynamic_config = dynamic_config

    def _effective_api_key(self) -> str:
        if self._dynamic_config:
            return (cfg.NCE_API_KEY or "").strip()
        return self._api_key

    def _effective_nonce_store(self) -> Any:
        if self._dynamic_config:
            return _resolve_nonce_store(self._nonce_store)
        return self._nonce_store

    async def _handle_session_cookie(
        self,
        *,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
        headers: Headers,
        cookies: dict[str, str],
        method: str,
    ) -> bool:
        """Validate session cookie + CSRF; sends 401/403 and returns False on failure."""
        session = verify_admin_session_cookie(cookies.get(SESSION_COOKIE_NAME))
        if session is None or await _is_session_revoked_redis(str(session.get("jti") or "")):
            resp = JSONResponse({"error": "Invalid or expired session"}, status_code=401)
            await resp(scope, receive, send)
            return False
        if method in _MUTATING_METHODS and not verify_admin_csrf(headers, cookies, session):
            resp = JSONResponse({"error": "CSRF token missing or invalid"}, status_code=403)
            await resp(scope, receive, send)
            return False
        state = scope.setdefault("state", {})
        state["admin_session"] = session
        state["admin_authenticated"] = True
        state["admin_principal"] = str(session.get("sub") or "admin")
        return True

    async def _handle_ops_api_auth(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
        path: str,
    ) -> None:
        """Enforce explicit dual-auth on ``/admin/ops/api/*`` (never NCE_ADMIN_OVERRIDE)."""
        headers = Headers(scope=scope)
        method = str(scope.get("method", "GET")).upper()
        cookies = _parse_cookies(headers.get("cookie", ""))
        has_hmac_headers = bool(
            headers.get("x-nce-timestamp")
            and (
                headers.get("x-nce-signature")
                or headers.get("authorization", "").strip().lower().startswith("hmac-sha256 ")
            )
        )

        # 1. Session cookie (if present and not explicitly overridden by HMAC/API key)
        has_api_key_hdr = bool(
            headers.get("x-admin-api-key")
            or headers.get("x-nce-admin-key")
            or headers.get("x-nce-api-key")
            or headers.get("authorization", "").strip().lower().startswith("bearer ")
        )
        if SESSION_COOKIE_NAME in cookies and not has_hmac_headers and not has_api_key_hdr:
            if await self._handle_session_cookie(
                scope=scope,
                receive=receive,
                send=send,
                headers=headers,
                cookies=cookies,
                method=method,
            ):
                await self.app(scope, receive, send)
            return

        # 2. 3-header HMAC-SHA256
        if has_hmac_headers:
            body_bytes, exceeded = await self._read_bounded_body(headers, receive)
            if exceeded is not None:
                start_msg, body_msg = _build_413_response(exceeded)
                await send(start_msg)
                await send(body_msg)
                return
            ctx, err = await _verify_hmac_with_replay(
                method=method,
                path=path,
                headers=headers,
                body_bytes=body_bytes,
                candidate_keys=_hmac_candidate_keys(self._api_key),
                nonce_store=self._effective_nonce_store(),
                nonce_required=self._nonce_required,
            )
            if err is not None:
                await err(scope, receive, send)
                return
            state = scope.setdefault("state", {})
            state["hmac_auth"] = ctx
            state["hmac_authenticated"] = True
            state["admin_authenticated"] = True
            state["admin_principal"] = "hmac-admin"
            replayed = False

            async def _replay_receive() -> dict[str, Any]:
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": body_bytes, "more_body": False}
                return await receive()

            await self.app(scope, _replay_receive, send)
            return

        # 3. API Key / Bearer Token (or Entra OIDC JWT)
        api_key = (
            headers.get("x-admin-api-key")
            or headers.get("x-nce-admin-key")
            or headers.get("x-nce-api-key")
            or ""
        ).strip()
        auth_hdr = headers.get("authorization", "").strip()
        bearer_tok = auth_hdr[7:].strip() if auth_hdr.lower().startswith("bearer ") else ""
        candidate = api_key or bearer_tok

        if candidate:
            expected = _admin_api_keys()
            if expected and any(
                hmac.compare_digest(candidate.encode("utf-8"), k.encode("utf-8")) for k in expected
            ):
                state = scope.setdefault("state", {})
                state["admin_authenticated"] = True
                state["admin_principal"] = "api-key-admin"
                await self.app(scope, receive, send)
                return
            if bearer_tok and bearer_tok.count(".") == 2:
                try:
                    entra_res = await _verify_entra_bearer(bearer_tok)
                    if entra_res is not None:
                        state = scope.setdefault("state", {})
                        state["admin_authenticated"] = True
                        state["admin_principal"] = entra_res[0]
                        await self.app(scope, receive, send)
                        return
                except EntraVerificationError:
                    pass

        resp = JSONResponse({"error": "Authentication required"}, status_code=401)
        await resp(scope, receive, send)

    async def _handle_general_api_hmac(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
        path: str,
    ) -> None:
        """Protect ``/api/*`` via 3-header HMAC (Authorization or X-NCE-Signature) + replay."""
        api_key = self._effective_api_key()
        if not api_key:
            resp = _jsonrpc_error(
                _CODE_AUTH_FAILED, "Authentication failed", "server_misconfigured"
            )
            await resp(scope, receive, send)
            return

        headers = Headers(scope=scope)
        body_bytes, exceeded = await self._read_bounded_body(headers, receive)
        if exceeded is not None:
            start_msg, body_msg = _build_413_response(exceeded)
            await send(start_msg)
            await send(body_msg)
            return

        method = str(scope.get("method", "GET")).upper()
        ctx, err = await _verify_hmac_with_replay(
            method=method,
            path=path,
            headers=headers,
            body_bytes=body_bytes,
            candidate_keys=[api_key],
            nonce_store=self._effective_nonce_store(),
            nonce_required=self._nonce_required,
        )
        if err is not None:
            await err(scope, receive, send)
            return

        ns_result = self._resolve_namespace_context(headers, scope)
        if isinstance(ns_result, JSONResponse):
            await ns_result(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        state["hmac_auth"] = ctx
        state["hmac_authenticated"] = True

        replayed = False

        async def _replay_receive() -> dict[str, Any]:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body_bytes, "more_body": False}
            return await receive()

        await self.app(scope, _replay_receive, send)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        """Pure-ASGI dual-auth entry point."""
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path") or "/")
        if path in _FAST_PROBE_PATHS:
            await self.app(scope, receive, send)
            return

        if path == "/admin/ops/api" or path.startswith("/admin/ops/api/"):
            await self._handle_ops_api_auth(scope, receive, send, path)
            return

        if (
            path == "/admin/plugins"
            or path == "/admin/plugins/api"
            or path.startswith("/admin/plugins/api/")
        ):
            headers = Headers(scope=scope)
            cookies = _parse_cookies(headers.get("cookie", ""))
            has_explicit_auth = bool(
                headers.get("authorization")
                or headers.get("x-admin-api-key")
                or headers.get("x-nce-admin-key")
                or headers.get("x-nce-api-key")
                or headers.get("x-nce-signature")
            )
            if SESSION_COOKIE_NAME in cookies and not has_explicit_auth:
                method = str(scope.get("method", "GET")).upper()
                if await self._handle_session_cookie(
                    scope=scope,
                    receive=receive,
                    send=send,
                    headers=headers,
                    cookies=cookies,
                    method=method,
                ):
                    await self.app(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return

        if not path.startswith(self._protected_prefix):
            await self.app(scope, receive, send)
            return

        if path.startswith("/api/admin/"):
            headers = Headers(scope=scope)
            cookies = _parse_cookies(headers.get("cookie", ""))
            has_hmac = bool(
                headers.get("x-nce-timestamp")
                or headers.get("x-nce-signature")
                or headers.get("authorization", "").strip().lower().startswith("hmac-sha256 ")
            )
            if SESSION_COOKIE_NAME in cookies and not has_hmac:
                method = str(scope.get("method", "GET")).upper()
                if await self._handle_session_cookie(
                    scope=scope,
                    receive=receive,
                    send=send,
                    headers=headers,
                    cookies=cookies,
                    method=method,
                ):
                    await self.app(scope, receive, send)
                return

        await self._handle_general_api_hmac(scope, receive, send, path)


# ---------------------------------------------------------------------------
# Session Endpoints
# ---------------------------------------------------------------------------


async def post_admin_session_login(request: Request, ctx: Any = None) -> JSONResponse:
    """POST /admin/session/login — issue 8h HttpOnly session + CSRF cookies."""
    redis_client = _resolve_redis_client(request, ctx)
    client_ip = _admin_http_client_ip(request)
    if not await _check_login_rate_limit(redis_client, client_ip):
        return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)

    auth_hdr = request.headers.get("authorization", "").strip()
    principal: str | None = None
    auth_mode: str = "api_key"

    if auth_hdr.lower().startswith("bearer "):
        bearer_tok = auth_hdr[7:].strip()
        if not bearer_tok:
            return JSONResponse({"error": "Missing bearer token"}, status_code=401)
        if bearer_tok.count(".") == 2:
            try:
                entra_res = await _verify_entra_bearer(bearer_tok)
            except EntraVerificationError as exc:
                logger.warning("Entra login verification rejected: %s", exc)
                return JSONResponse({"error": "Bearer token rejected"}, status_code=401)
            if entra_res is None:
                return JSONResponse(
                    {"error": "Entra OIDC validator not configured"}, status_code=401
                )
            principal, auth_mode = entra_res
        else:
            expected_keys = _admin_api_keys()
            if expected_keys and any(
                hmac.compare_digest(bearer_tok.encode("utf-8"), k.encode("utf-8"))
                for k in expected_keys
            ):
                principal = "admin"
                auth_mode = "api_key"
            else:
                return JSONResponse({"error": "Invalid credentials"}, status_code=401)
    else:
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        raw_key = body.get("api_key")
        if not isinstance(raw_key, str) or not raw_key.strip():
            return JSONResponse({"error": "Missing api_key"}, status_code=401)
        api_key = raw_key.strip()
        expected_keys = _admin_api_keys()
        if not expected_keys or not any(
            hmac.compare_digest(api_key.encode("utf-8"), k.encode("utf-8")) for k in expected_keys
        ):
            return JSONResponse({"error": "Invalid credentials"}, status_code=401)
        principal = "admin"
        auth_mode = "api_key"

    sess_val, csrf_token, exp = mint_admin_session_cookie(
        principal=principal or "admin", auth_mode=auth_mode
    )
    secure = _should_use_secure_cookie(request)
    resp = JSONResponse(
        {
            "authenticated": True,
            "auth_mode": auth_mode,
            "principal": principal or "admin",
            "csrf_token": csrf_token,
            "expires_at": datetime.fromtimestamp(exp, tz=UTC).isoformat(),
        },
        status_code=200,
    )
    resp.headers.append(
        "set-cookie",
        _build_session_cookie_header(
            SESSION_COOKIE_NAME,
            sess_val,
            max_age=SESSION_TTL_SECONDS,
            http_only=True,
            secure=secure,
        ),
    )
    resp.headers.append(
        "set-cookie",
        _build_session_cookie_header(
            CSRF_COOKIE_NAME,
            csrf_token,
            max_age=SESSION_TTL_SECONDS,
            http_only=False,
            secure=secure,
        ),
    )
    return resp


async def post_admin_session_logout(request: Request, ctx: Any = None) -> JSONResponse:
    """POST /admin/session/logout — revoke session JTI and clear session/CSRF cookies."""
    cookies = _parse_cookies(request.headers.get("cookie", ""))
    session = verify_admin_session_cookie(cookies.get(SESSION_COOKIE_NAME))
    if session is not None:
        jti = str(session.get("jti") or "")
        if jti:
            _revoke_session_jti_mem(jti)
            rc = _resolve_redis_client(request, ctx)
            if rc is not None:
                try:
                    set_fn = getattr(rc, "set", None)
                    if callable(set_fn):
                        res = set_fn(
                            f"nce:admin:revoked_sess:{jti}",
                            "1",
                            ex=SESSION_TTL_SECONDS,
                        )
                        if inspect.isawaitable(res):
                            await res
                except Exception:
                    pass

    secure = _should_use_secure_cookie(request)
    resp = JSONResponse({"authenticated": False, "status": "logged_out"}, status_code=200)
    resp.headers.append(
        "set-cookie",
        _build_session_cookie_header(
            SESSION_COOKIE_NAME, "", max_age=0, http_only=True, secure=secure
        ),
    )
    resp.headers.append(
        "set-cookie",
        _build_session_cookie_header(
            CSRF_COOKIE_NAME, "", max_age=0, http_only=False, secure=secure
        ),
    )
    return resp


async def get_admin_session_status(request: Request, ctx: Any = None) -> JSONResponse:
    """GET /admin/session/status — return active session metadata and CSRF token."""
    cookies = _parse_cookies(request.headers.get("cookie", ""))
    session = verify_admin_session_cookie(cookies.get(SESSION_COOKIE_NAME))
    if session is not None:
        if not await _is_session_revoked_redis(
            str(session.get("jti") or ""), _resolve_redis_client(request, ctx)
        ):
            return JSONResponse(
                {
                    "authenticated": True,
                    "auth_mode": str(session.get("auth_mode") or "api_key"),
                    "principal": str(session.get("sub") or "admin"),
                    "csrf_token": str(session.get("csrf") or ""),
                    "expires_at": datetime.fromtimestamp(int(session["exp"]), tz=UTC).isoformat(),
                }
            )

    return JSONResponse(
        {
            "authenticated": False,
            "auth_mode": "none",
            "principal": None,
            "csrf_token": None,
        }
    )


# ---------------------------------------------------------------------------
# Static Air-Gapped Console Serving
# ---------------------------------------------------------------------------


async def serve_admin_console(request: Request, ctx: Any = None) -> Response:
    """GET /admin and GET /admin/index.html — serve unified 5-Tab Console shell."""
    index_path = _ADMIN_DIR / "index.html"
    if await asyncio.to_thread(index_path.is_file):
        return FileResponse(str(index_path), media_type="text/html")
    return HTMLResponse("Admin Console not found", status_code=404)


async def serve_admin_console_js(request: Request, ctx: Any = None) -> Response:
    """GET /admin/console.js and GET /admin/app.js — serve air-gapped console script."""
    js_path = _ADMIN_DIR / "console.js"
    if await asyncio.to_thread(js_path.is_file):
        return FileResponse(str(js_path), media_type="application/javascript")
    app_path = _ADMIN_DIR / "app.js"
    if await asyncio.to_thread(app_path.is_file):
        return FileResponse(str(app_path), media_type="application/javascript")
    return Response("Console script not found", status_code=404, media_type="text/plain")


async def serve_admin_console_css(request: Request, ctx: Any = None) -> Response:
    """GET /admin/console.css and GET /admin/styles.css — serve console stylesheets."""
    req_path = str(request.url.path or "")
    target_name = "console.css" if req_path.endswith("/console.css") else "styles.css"
    css_path = _ADMIN_DIR / target_name
    if not await asyncio.to_thread(css_path.is_file):
        css_path = _ADMIN_DIR / "styles.css"
    if await asyncio.to_thread(css_path.is_file):
        return FileResponse(str(css_path), media_type="text/css")
    return Response("Console stylesheet not found", status_code=404, media_type="text/plain")


# ---------------------------------------------------------------------------
# Durable Governance PG Write-Through & Redis Hydration
# ---------------------------------------------------------------------------


def _is_usable_async_pool(pool: Any) -> bool:
    """Return True when *pool* is a real asyncpg.Pool or an explicit non-mock async pool."""
    if pool is None:
        return False
    if isinstance(pool, asyncpg.Pool):
        return True
    if hasattr(pool, "mock_calls"):
        return False
    return callable(getattr(pool, "acquire", None))


async def _acquire_governance_conn(ctx: Any = None) -> tuple[Any, bool]:
    """Return ``(conn_or_pool, should_close_conn)`` or ``(None, False)`` when offline.

    Prefers ``engine.pg_fleet_pool``, then ``resolve_worker_dsn()`` (when segregated
    and ``pg_pool`` is live), then ``pg_pool`` (for real/mock test pools).
    """
    eng = getattr(admin_state, "engine", None)
    eng_dict = getattr(eng, "__dict__", {}) if eng is not None else {}
    fleet_pool = getattr(ctx, "pg_fleet_pool", None)
    if fleet_pool is None:
        fleet_pool = eng_dict.get("pg_fleet_pool")

    if _is_usable_async_pool(fleet_pool):
        return fleet_pool, False

    pg_pool = getattr(ctx, "pg_pool", None)
    if pg_pool is None and eng is not None:
        pg_pool = eng_dict.get("pg_pool")
        if pg_pool is None and not hasattr(eng, "mock_calls"):
            pg_pool = getattr(eng, "pg_pool", None)

    if isinstance(pg_pool, asyncpg.Pool) and worker_dsn_is_segregated():
        try:
            conn = await asyncpg.connect(resolve_worker_dsn(), timeout=POOL_ACQUIRE_TIMEOUT)
            return conn, True
        except Exception as exc:
            logger.warning(
                "Failed to connect via worker DSN for governance: %s", type(exc).__name__
            )
            return None, False

    if _is_usable_async_pool(pg_pool):
        return pg_pool, False

    return None, False


async def _run_with_governance_conn(fn: Any, *, ctx: Any = None) -> Any:
    """Run async callback ``fn(conn)`` against governance PostgreSQL when available."""
    target, close_direct = await _acquire_governance_conn(ctx=ctx)
    if target is None:
        return None
    if close_direct:
        try:
            return await fn(target)
        finally:
            await target.close()
    async with target.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
        return await fn(conn)


async def persist_governance_toggle(
    *,
    scope_key: str,
    tool_name: str,
    enabled: bool,
    reason: str | None = None,
    updated_by: str = "admin",
    ctx: Any = None,
) -> bool:
    """Persist an engine/tool toggle into ``public.runtime_tool_governance`` if PG is available."""

    async def _upsert(conn: Any) -> bool:
        await conn.execute(
            """
            INSERT INTO public.runtime_tool_governance (
                scope_key, tool_name, enabled, reason, updated_by, updated_at
            ) VALUES ($1, $2, $3, $4, $5, now())
            ON CONFLICT (scope_key, tool_name) DO UPDATE
            SET enabled = EXCLUDED.enabled,
                reason = EXCLUDED.reason,
                updated_by = EXCLUDED.updated_by,
                updated_at = now()
            """,
            scope_key,
            tool_name,
            bool(enabled),
            reason,
            (updated_by or "admin")[:128],
        )
        return True

    try:
        res = await _run_with_governance_conn(_upsert, ctx=ctx)
        return bool(res)
    except Exception as exc:
        logger.warning(
            "PostgreSQL runtime_tool_governance write failed (%s/%s): %s",
            scope_key,
            tool_name,
            exc,
        )
        return False


async def _redis_set_governance_hydrated(redis_client: Any) -> None:
    if redis_client is None:
        return
    set_fn = getattr(redis_client, "set", None)
    if not callable(set_fn):
        return
    try:
        res = set_fn(GOVERNANCE_CACHE_HYDRATED_KEY, "1")
        if inspect.isawaitable(res):
            await res
    except Exception:
        pass


async def _redis_has_governance_hydrated(redis_client: Any) -> bool | None:
    """Return True/False if Redis supports ``get``/``exists``, or None if mock lacks them."""
    if redis_client is None:
        return None
    get_fn = getattr(redis_client, "get", None)
    if callable(get_fn):
        try:
            res = get_fn(GOVERNANCE_CACHE_HYDRATED_KEY)
            if inspect.isawaitable(res):
                res = await res
            return bool(res)
        except Exception:
            return None
    exists_fn = getattr(redis_client, "exists", None)
    if callable(exists_fn):
        try:
            res = exists_fn(GOVERNANCE_CACHE_HYDRATED_KEY)
            if inspect.isawaitable(res):
                res = await res
            return bool(res)
        except Exception:
            return None
    return None


async def hydrate_governance_cache(
    redis_client: Any,
    *,
    ctx: Any = None,
    force: bool = False,
) -> dict[str, set[str]] | None:
    """Hydrate ``nce:engines:disabled`` and ``nce:tools:disabled`` from Postgres if flushed."""
    if not force:
        hydrated = await _redis_has_governance_hydrated(redis_client)
        if hydrated is True:
            return None

    async def _fetch_rows(conn: Any) -> list[Any]:
        return await conn.fetch(
            "SELECT scope_key, tool_name FROM public.runtime_tool_governance WHERE enabled = false"
        )

    try:
        rows = await _run_with_governance_conn(_fetch_rows, ctx=ctx)
    except Exception as exc:
        logger.warning("Governance auto-hydration from PostgreSQL failed: %s", exc)
        return None

    if not isinstance(rows, (list, tuple)):
        return None

    disabled_engines: set[str] = set()
    disabled_tools: set[str] = set()
    for row in rows:
        s_key = str(row["scope_key"]).strip().lower()
        t_name = str(row["tool_name"]).strip()
        if not t_name:
            continue
        if s_key == "engine":
            disabled_engines.add(t_name)
        elif s_key == "tool":
            disabled_tools.add(t_name)

    if redis_client is not None:
        try:
            del_fn = getattr(redis_client, "delete", None)
            if callable(del_fn):
                res = del_fn(REDIS_DISABLED_ENGINES_KEY, REDIS_DISABLED_TOOLS_KEY)
                if inspect.isawaitable(res):
                    await res
            else:
                for hash_key, names in (
                    (REDIS_DISABLED_ENGINES_KEY, disabled_engines),
                    (REDIS_DISABLED_TOOLS_KEY, disabled_tools),
                ):
                    existing = await redis_client.hkeys(hash_key)
                    for k in existing:
                        k_str = k.decode("utf-8") if isinstance(k, bytes) else str(k)
                        if k_str not in names:
                            await redis_client.hdel(hash_key, k_str)
            for slug in disabled_engines:
                await redis_client.hset(REDIS_DISABLED_ENGINES_KEY, slug, "1")
            for t_name in disabled_tools:
                await redis_client.hset(REDIS_DISABLED_TOOLS_KEY, t_name, "1")
            await _redis_set_governance_hydrated(redis_client)
        except Exception as exc:
            logger.warning("Failed writing hydrated governance cache to Redis: %s", exc)

    return {"engine": disabled_engines, "tool": disabled_tools}


# ---------------------------------------------------------------------------
# Day-2 Ops Helpers (Data Serialization, Lazy Ops Subsystems, Probes)
# ---------------------------------------------------------------------------


def _json_safe(val: Any) -> Any:
    if hasattr(val, "model_dump"):
        val = val.model_dump(mode="json")
    return json.loads(json.dumps(val, default=str))


def _iso(dt: Any) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    if isinstance(dt, datetime):
        return (dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)).astimezone(UTC).isoformat()
    return str(dt)


def _json_col(val: Any, default: Any) -> Any:
    if val is None:
        return default
    if isinstance(val, (dict, list)):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return default
    return default


def _resolve_backup_dir() -> Path:
    raw = (
        getattr(cfg, "NCE_BACKUP_DIR", "") or os.getenv("NCE_BACKUP_DIR", "") or "/var/backups/nce"
    ).strip()
    candidate = Path(raw)
    for probe in (candidate, *candidate.parents):
        if probe.exists():
            return probe
    return Path(tempfile.gettempdir())


def _compute_disk_headroom(required_bytes: int = 0) -> dict[str, Any]:
    probe_dir = _resolve_backup_dir()
    usage = shutil.disk_usage(probe_dir)
    gib = float(1024**3)
    free_gib = round(usage.free / gib, 2)
    total_gib = round(usage.total / gib, 2)
    used_pct = round(((usage.total - usage.free) / usage.total) * 100.0, 1) if usage.total else 0.0
    required_gib = round(max(1.0, (required_bytes * 2.5) / gib), 2) if required_bytes > 0 else 1.0
    return {
        "path": str(probe_dir),
        "free_bytes": int(usage.free),
        "total_bytes": int(usage.total),
        "free_gib": free_gib,
        "total_gib": total_gib,
        "used_pct": used_pct,
        "required_gib": required_gib,
        "preflight_ok": free_gib >= required_gib,
    }


async def _call_ops_callable(
    override_fn: Any,
    module_name: str,
    attr_name: str,
    *,
    available_kwargs: dict[str, Any],
) -> Any:
    """Resolve and invoke an async/sync ``nce.ops.*`` function matching its signature."""
    fn = override_fn
    if fn is None:
        mod = importlib.import_module(module_name)
        fn = getattr(mod, attr_name)

    try:
        sig = inspect.signature(fn)
        accepts_kwargs = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        call_kwargs = (
            dict(available_kwargs)
            if accepts_kwargs
            else {k: v for k, v in available_kwargs.items() if k in sig.parameters}
        )
    except (TypeError, ValueError):
        call_kwargs = dict(available_kwargs)

    res = fn(**call_kwargs)
    if inspect.isawaitable(res):
        return await res
    return res


def _get_ops_pg_pool(request: Request | None = None, ctx: Any = None) -> Any:
    if ctx is not None:
        p = getattr(ctx, "pg_fleet_pool", None) or getattr(ctx, "pg_pool", None)
        if p is not None:
            return p
    if request is not None:
        app_state = getattr(getattr(request, "app", None), "state", None)
        if app_state is not None:
            p = getattr(app_state, "pg_fleet_pool", None) or getattr(app_state, "pg_pool", None)
            if p is not None:
                return p
    eng = getattr(admin_state, "engine", None)
    if eng is None:
        return None
    fleet_pool = getattr(eng, "__dict__", {}).get("pg_fleet_pool")
    return fleet_pool if fleet_pool is not None else getattr(eng, "pg_pool", None)


def _list_available_playbooks() -> list[dict[str, Any]]:
    try:
        mod = importlib.import_module("nce.ops.playbooks")
        for attr in ("list_playbooks", "PLAYBOOKS", "PLAYBOOK_REGISTRY"):
            val = getattr(mod, attr, None)
            if callable(val):
                items = val()
                if isinstance(items, (list, tuple)):
                    return [_json_safe(item) for item in items]
            elif isinstance(val, dict):
                out = []
                for pid, meta in val.items():
                    if isinstance(meta, dict):
                        out.append({"playbook_id": pid, **meta})
                    else:
                        out.append(
                            {
                                "playbook_id": str(pid),
                                "title": str(getattr(meta, "title", pid)),
                                "description": str(getattr(meta, "description", "")),
                            }
                        )
                if out:
                    return out
    except Exception:
        pass
    return [dict(p) for p in DEFAULT_PLAYBOOKS]


# ---------------------------------------------------------------------------
# Day-2 Ops REST API Endpoints
# ---------------------------------------------------------------------------


async def ops_overview(request: Request, ctx: Any = None) -> JSONResponse:
    """GET /admin/ops/api/overview — 4-store health/latency, pools, RLS, queues, disk, cognitive."""
    eng = getattr(admin_state, "engine", None)
    stores: dict[str, dict[str, Any]] = {
        "postgres": {"status": "uninitialized", "latency_ms": None},
        "mongodb": {"status": "uninitialized", "latency_ms": None},
        "redis": {"status": "uninitialized", "latency_ms": None},
        "minio": {"status": "uninitialized", "latency_ms": None},
    }
    pg_stats: dict[str, Any] = {
        "pool_size": None,
        "pool_idle": None,
        "pool_max": None,
        "active": 0,
        "idle_in_tx": 0,
        "locks": 0,
        "total_connections": 0,
    }
    verified_forced_tables: int | None = None

    pg_pool = _get_ops_pg_pool(request, ctx)
    if pg_pool is not None:
        t0 = time.perf_counter()
        try:
            for attr, target in (
                ("get_size", "pool_size"),
                ("get_idle_size", "pool_idle"),
                ("get_max_size", "pool_max"),
            ):
                fn = getattr(pg_pool, attr, None)
                if callable(fn):
                    val = fn()
                    if isinstance(val, int):
                        pg_stats[target] = val

            async with pg_pool.acquire(timeout=5.0) as conn:
                await conn.fetchval("SELECT 1")
                stores["postgres"] = {
                    "status": "up",
                    "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
                }
                try:
                    act_row = await conn.fetchrow(
                        """
                        SELECT
                          count(*) FILTER (WHERE state = 'active')::int AS active,
                          count(*) FILTER (WHERE state = 'idle in transaction')::int AS idle_in_tx,
                          count(*) FILTER (WHERE wait_event_type = 'Lock')::int AS locks,
                          count(*)::int AS total_connections
                        FROM pg_stat_activity
                        WHERE datname = current_database()
                        """
                    )
                    if act_row and hasattr(act_row, "keys"):
                        for k in ("active", "idle_in_tx", "locks", "total_connections"):
                            if k in act_row and act_row[k] is not None:
                                pg_stats[k] = int(act_row[k])
                except Exception:
                    pass
                try:
                    rls_cnt = await conn.fetchval(
                        """
                        SELECT count(*)::int
                        FROM pg_class c
                        JOIN pg_namespace n ON n.oid = c.relnamespace
                        WHERE n.nspname = 'public'
                          AND c.relrowsecurity = true
                          AND c.relforcerowsecurity = true
                        """
                    )
                    if isinstance(rls_cnt, int):
                        verified_forced_tables = rls_cnt
                except Exception:
                    pass
        except Exception as exc:
            stores["postgres"] = {
                "status": "down",
                "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
                "error": type(exc).__name__,
            }

    mongo_client = getattr(eng, "mongo_client", None) if eng is not None else None
    if mongo_client is not None:
        t0 = time.perf_counter()
        try:
            await mongo_client.admin.command("ping")
            stores["mongodb"] = {
                "status": "up",
                "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
            }
        except Exception as exc:
            stores["mongodb"] = {
                "status": "down",
                "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
                "error": type(exc).__name__,
            }

    redis_info: dict[str, Any] = {
        "used_memory_human": "0B",
        "used_memory_bytes": 0,
        "salience_buffer_len": 0,
    }
    rq_queues: dict[str, int] = {}
    rq_failed_total = 0

    redis_client = _resolve_redis_client(request, ctx)
    if redis_client is not None:
        t0 = time.perf_counter()
        try:
            await redis_client.ping()
            stores["redis"] = {
                "status": "up",
                "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
            }
            try:
                mem_info = await redis_client.info("memory")
                if isinstance(mem_info, dict):
                    redis_info["used_memory_human"] = str(mem_info.get("used_memory_human", "0B"))
                    redis_info["used_memory_bytes"] = int(mem_info.get("used_memory", 0) or 0)
            except Exception:
                pass
            try:
                for q_name in (
                    "nce-critical",
                    "nce-default",
                    "nce-bulk",
                    "high_priority",
                    "batch_processing",
                    "diag_ingest",
                    "default",
                ):
                    llen_fn = getattr(redis_client, "llen", None)
                    if callable(llen_fn):
                        q_len = llen_fn(f"rq:queue:{q_name}")
                        if inspect.isawaitable(q_len):
                            q_len = await q_len
                        rq_queues[q_name] = int(q_len or 0)
                    zcard_fn = getattr(redis_client, "zcard", None)
                    if callable(zcard_fn):
                        f_len = zcard_fn(f"rq:failed:{q_name}")
                        if inspect.isawaitable(f_len):
                            f_len = await f_len
                        rq_failed_total += int(f_len or 0)
            except Exception:
                pass
            try:
                buf_total = 0
                keys_list: list[Any] = []
                scan_fn = getattr(redis_client, "scan_iter", None)
                if callable(scan_fn):
                    async for k in scan_fn(match="nce:ns:*:salience_buffer", count=100):
                        keys_list.append(k)
                        if len(keys_list) >= 200:
                            break
                elif callable(getattr(redis_client, "keys", None)):
                    raw_keys = redis_client.keys("nce:ns:*:salience_buffer")
                    if inspect.isawaitable(raw_keys):
                        raw_keys = await raw_keys
                    keys_list = list(raw_keys or [])
                hlen_fn = getattr(redis_client, "hlen", None)
                if callable(hlen_fn):
                    for k in keys_list:
                        cnt = hlen_fn(k)
                        if inspect.isawaitable(cnt):
                            cnt = await cnt
                        buf_total += int(cnt or 0)
                redis_info["salience_buffer_len"] = buf_total
            except Exception:
                pass
        except Exception as exc:
            stores["redis"] = {
                "status": "down",
                "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
                "error": type(exc).__name__,
            }

    minio_client = getattr(eng, "minio_client", None) if eng is not None else None
    if minio_client is not None:
        t0 = time.perf_counter()
        try:
            buckets = await asyncio.to_thread(minio_client.list_buckets)
            stores["minio"] = {
                "status": "up",
                "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
                "bucket_count": len(buckets or ()),
            }
        except Exception as exc:
            stores["minio"] = {
                "status": "down",
                "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
                "error": type(exc).__name__,
            }

    # Cognitive / Embedding Provider probe
    cog_backend_name = "unknown"
    embedding_provider = (os.getenv("NCE_EMBEDDING_PROVIDER", "") or "").strip().lower()
    cog_status = "unknown"
    cog_model = cfg.NCE_EMBEDDING_MODEL_ID
    try:
        from nce.embeddings import CognitiveRemoteBackend, cognitive_health_check_url, get_backend

        backend = get_backend()
        cog_backend_name = type(backend).__name__
        if "stub" in cog_backend_name.lower() or getattr(backend, "is_stub", False):
            embedding_provider = "stub"
            cog_status = "stub"
        elif isinstance(backend, CognitiveRemoteBackend) or (
            bool((cfg.NCE_COGNITIVE_BASE_URL or "").strip())
            and not bool((cfg.NCE_BACKEND or "").strip())
        ):
            async with httpx.AsyncClient(timeout=1.5) as client:
                r = await client.get(cognitive_health_check_url())
                if r.status_code == 200:
                    payload = r.json() if r.content else {}
                    reported = str(payload.get("engine") or "").strip().lower()
                    if payload.get("model"):
                        cog_model = str(payload["model"])
                    if reported == "stub":
                        embedding_provider = "stub"
                        cog_status = "stub"
                    else:
                        embedding_provider = embedding_provider or reported or "onnx"
                        cog_status = "up"
                else:
                    cog_status = f"down ({r.status_code})"
                    embedding_provider = embedding_provider or cfg.NCE_COGNITIVE_BACKEND or "remote"
        else:
            embedding_provider = embedding_provider or cfg.NCE_BACKEND or "local"
            cog_status = "up"
    except Exception:
        if not embedding_provider:
            embedding_provider = cfg.NCE_BACKEND or cfg.NCE_COGNITIVE_BACKEND or "unconfigured"
        cog_status = "unreachable"

    stub_warning = embedding_provider == "stub"

    # Optional ops-elm reachability probe
    ops_elm_url = (
        (
            os.getenv("NCE_OPS_ELM_BASE_URL", "")
            or getattr(cfg, "NCE_OPS_ELM_BASE_URL", "")
            or os.getenv("NCE_OPS_ELM_URL", "")
            or getattr(cfg, "NCE_OPS_ELM_URL", "")
            or ""
        )
        .strip()
        .rstrip("/")
    )
    ops_elm_info: dict[str, Any] = {
        "configured": bool(ops_elm_url),
        "reachable": False,
        "status": "disabled",
        "url": ops_elm_url or None,
    }
    if ops_elm_url:
        try:
            async with httpx.AsyncClient(timeout=1.0) as client:
                for health_suffix in ("/api/tags", "/healthz", "/health"):
                    r = await client.get(f"{ops_elm_url}{health_suffix}")
                    if r.status_code == 200:
                        ops_elm_info["reachable"] = True
                        ops_elm_info["status"] = "up"
                        break
                if not ops_elm_info["reachable"]:
                    ops_elm_info["status"] = "down"
        except Exception:
            ops_elm_info["status"] = "unreachable"

    disk = _compute_disk_headroom()

    return JSONResponse(
        {
            "status": "ok" if not stub_warning else "warn",
            "stores": stores,
            "postgres": pg_stats,
            "rls": {
                "expected_forced_tables": len(EXPECTED_TENANT_RLS_TABLES),
                "verified_forced_tables": verified_forced_tables,
            },
            "redis": redis_info,
            "queues": {
                "depths": rq_queues,
                "failed_total": rq_failed_total,
            },
            "disk": disk,
            "cognitive": {
                "backend": cog_backend_name,
                "embedding_provider": embedding_provider,
                "model": cog_model,
                "status": cog_status,
                "stub_warning": stub_warning,
            },
            "ops_elm": ops_elm_info,
        }
    )


async def ops_backups_list(request: Request, ctx: Any = None) -> JSONResponse:
    """GET /admin/ops/api/backups — list T_cut backup manifests + disk preflight."""
    pg_pool = _get_ops_pg_pool(request, ctx)
    if pg_pool is None:
        return JSONResponse({"error": "PostgreSQL pool unavailable"}, status_code=503)

    manifests: list[dict[str, Any]] = []
    latest_size = 0
    latest_tombstone_count = 0
    try:
        async with pg_pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
            rows = await conn.fetch(
                """
                SELECT backup_id, t_cut, t_blob_completed, archive_path, archive_sha256,
                       size_bytes, master_key_hmac_fp, schema_heads, worm_heads,
                       tombstone_count, status, last_drill_at, last_drill_status,
                       last_drill_report, created_at
                FROM public.backup_manifests
                ORDER BY t_cut DESC, created_at DESC
                LIMIT 100
                """
            )
            for r in rows:
                item = {
                    "backup_id": str(r["backup_id"]),
                    "t_cut": _iso(r["t_cut"]),
                    "t_blob_completed": _iso(r["t_blob_completed"]),
                    "archive_path": str(r["archive_path"]),
                    "archive_sha256": str(r["archive_sha256"]),
                    "size_bytes": int(r["size_bytes"] or 0),
                    "master_key_hmac_fp": str(r["master_key_hmac_fp"]),
                    "schema_heads": _json_col(r["schema_heads"], {}),
                    "worm_heads": _json_col(r["worm_heads"], {}),
                    "tombstone_count": int(r["tombstone_count"] or 0),
                    "status": str(r["status"]),
                    "last_drill_at": _iso(r["last_drill_at"]),
                    "last_drill_status": r["last_drill_status"],
                    "last_drill_report": _json_col(r["last_drill_report"], None),
                    "created_at": _iso(r["created_at"]),
                }
                manifests.append(item)
            if manifests:
                latest_size = manifests[0]["size_bytes"]
                latest_tombstone_count = manifests[0]["tombstone_count"]
    except asyncpg.UndefinedTableError:
        manifests = []
    except Exception as exc:
        logger.error("Failed to list backup manifests: %s", type(exc).__name__)
        return JSONResponse({"error": "Failed to list backup manifests"}, status_code=500)

    return JSONResponse(
        {
            "status": "ok",
            "manifests": manifests,
            "total": len(manifests),
            "latest_tombstone_count": latest_tombstone_count,
            "disk_preflight": _compute_disk_headroom(required_bytes=latest_size),
        }
    )


async def ops_backups_trigger(request: Request, ctx: Any = None) -> JSONResponse:
    """POST /admin/ops/api/backups/trigger — trigger immediate @governed T_cut backup."""
    body: dict[str, Any] = {}
    try:
        raw = await request.body()
        if raw:
            parsed = json.loads(raw.decode("utf-8"))
            if isinstance(parsed, dict):
                body = parsed
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    eng = getattr(admin_state, "engine", None)
    pg_pool = _get_ops_pg_pool(request, ctx)
    available = {
        "engine": eng,
        "pg_pool": pg_pool,
        "mongo_client": getattr(eng, "mongo_client", None) if eng is not None else None,
        "redis_client": _resolve_redis_client(request, ctx),
        "minio_client": getattr(eng, "minio_client", None) if eng is not None else None,
        "backup_dir": str(_resolve_backup_dir()),
        "reason": str(body.get("reason") or "admin_console_trigger"),
        "approved_by": str(getattr(request.state, "admin_principal", "admin")),
    }
    try:
        result = await _call_ops_callable(
            create_watermark_backup,
            "nce.ops.backup",
            "create_watermark_backup",
            available_kwargs=available,
        )
    except (ModuleNotFoundError, AttributeError):
        return JSONResponse({"error": "Backup subsystem unavailable"}, status_code=503)
    except Exception as exc:
        status_code = (
            409 if "Headroom" in type(exc).__name__ or "Preflight" in type(exc).__name__ else 500
        )
        return JSONResponse({"error": str(exc)}, status_code=status_code)

    return JSONResponse({"status": "ok", "manifest": _json_safe(result)}, status_code=200)


async def ops_backups_drill(request: Request, ctx: Any = None) -> JSONResponse:
    """POST /admin/ops/api/backups/{backup_id}/drill — run ephemeral restore drill."""
    raw_id = request.path_params.get("backup_id", "")
    try:
        backup_uuid = uuid.UUID(str(raw_id).strip())
    except (ValueError, TypeError, AttributeError):
        return JSONResponse({"error": "Invalid backup_id UUID"}, status_code=400)

    eng = getattr(admin_state, "engine", None)
    pg_pool = _get_ops_pg_pool(request, ctx)
    available = {
        "backup_id": backup_uuid,
        "engine": eng,
        "pg_pool": pg_pool,
        "mongo_client": getattr(eng, "mongo_client", None) if eng is not None else None,
        "redis_client": _resolve_redis_client(request, ctx),
        "minio_client": getattr(eng, "minio_client", None) if eng is not None else None,
        "approved_by": str(getattr(request.state, "admin_principal", "admin")),
    }
    try:
        report = await _call_ops_callable(
            run_ephemeral_restore_drill,
            "nce.ops.backup",
            "run_ephemeral_restore_drill",
            available_kwargs=available,
        )
    except (ModuleNotFoundError, AttributeError):
        return JSONResponse({"error": "Backup drill subsystem unavailable"}, status_code=503)
    except LookupError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except Exception as exc:
        status_code = (
            409 if "Headroom" in type(exc).__name__ or "Preflight" in type(exc).__name__ else 500
        )
        return JSONResponse({"error": str(exc)}, status_code=status_code)

    return JSONResponse({"status": "ok", "drill_report": _json_safe(report)}, status_code=200)


async def ops_incidents_list(request: Request, ctx: Any = None) -> JSONResponse:
    """GET /admin/ops/api/incidents — list ops_incidents + recent ops_probe_runs."""
    pg_pool = _get_ops_pg_pool(request, ctx)
    if pg_pool is None:
        return JSONResponse({"error": "PostgreSQL pool unavailable"}, status_code=503)

    incidents: list[dict[str, Any]] = []
    probe_runs: list[dict[str, Any]] = []
    try:
        async with pg_pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
            inc_rows = await conn.fetch(
                """
                SELECT incident_id, fingerprint, severity, status, title,
                       deterministic_facts, elm_brief_json, elm_mode_used,
                       recommended_playbook, occurrence_count,
                       first_seen_at, last_seen_at, resolved_at
                FROM public.ops_incidents
                ORDER BY CASE WHEN status = 'open' THEN 0 ELSE 1 END, last_seen_at DESC
                LIMIT 100
                """
            )
            for r in inc_rows:
                incidents.append(
                    {
                        "incident_id": str(r["incident_id"]),
                        "fingerprint": str(r["fingerprint"]),
                        "severity": str(r["severity"]),
                        "status": str(r["status"]),
                        "title": str(r["title"]),
                        "deterministic_facts": _json_col(r["deterministic_facts"], {}),
                        "elm_brief_json": _json_col(r["elm_brief_json"], None),
                        "elm_mode_used": str(r["elm_mode_used"] or "none"),
                        "recommended_playbook": r["recommended_playbook"],
                        "occurrence_count": int(r["occurrence_count"] or 1),
                        "first_seen_at": _iso(r["first_seen_at"]),
                        "last_seen_at": _iso(r["last_seen_at"]),
                        "resolved_at": _iso(r["resolved_at"]),
                    }
                )

            probe_rows = await conn.fetch(
                """
                SELECT run_id, probe_name, status, metrics_json, created_at
                FROM public.ops_probe_runs
                ORDER BY created_at DESC
                LIMIT 50
                """
            )
            for r in probe_rows:
                probe_runs.append(
                    {
                        "run_id": str(r["run_id"]),
                        "probe_name": str(r["probe_name"]),
                        "status": str(r["status"]),
                        "metrics_json": _json_col(r["metrics_json"], {}),
                        "created_at": _iso(r["created_at"]),
                    }
                )
    except asyncpg.UndefinedTableError:
        pass
    except Exception as exc:
        logger.error("Failed to list ops incidents: %s", type(exc).__name__)
        return JSONResponse({"error": "Failed to list ops incidents"}, status_code=500)

    return JSONResponse(
        {
            "status": "ok",
            "incidents": incidents,
            "probe_runs": probe_runs,
            "playbooks": _list_available_playbooks(),
        }
    )


async def ops_quarantine_list(request: Request, ctx: Any = None) -> JSONResponse:
    """GET /admin/ops/api/quarantine — list poison-pill jobs in ops_quarantine."""
    pg_pool = _get_ops_pg_pool(request, ctx)
    if pg_pool is None:
        return JSONResponse({"error": "PostgreSQL pool unavailable"}, status_code=503)

    items: list[dict[str, Any]] = []
    try:
        async with pg_pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
            rows = await conn.fetch(
                """
                SELECT quarantine_id, job_fingerprint, queue_name, func_name,
                       namespace_id, failure_count, exit_reason,
                       sanitized_traceback, job_args_preview, status,
                       created_at, updated_at
                FROM public.ops_quarantine
                ORDER BY created_at DESC
                LIMIT 100
                """
            )
            for r in rows:
                items.append(
                    {
                        "quarantine_id": str(r["quarantine_id"]),
                        "job_fingerprint": str(r["job_fingerprint"]),
                        "queue_name": str(r["queue_name"]),
                        "func_name": str(r["func_name"]),
                        "namespace_id": str(r["namespace_id"]) if r["namespace_id"] else None,
                        "failure_count": int(r["failure_count"] or 0),
                        "exit_reason": str(r["exit_reason"]),
                        "sanitized_traceback": str(r["sanitized_traceback"] or ""),
                        "job_args_preview": _json_col(r["job_args_preview"], {}),
                        "status": str(r["status"]),
                        "created_at": _iso(r["created_at"]),
                        "updated_at": _iso(r["updated_at"]),
                    }
                )
    except asyncpg.UndefinedTableError:
        items = []
    except Exception as exc:
        logger.error("Failed to list ops_quarantine: %s", type(exc).__name__)
        return JSONResponse({"error": "Failed to list quarantined jobs"}, status_code=500)

    return JSONResponse({"status": "ok", "items": items, "total": len(items)})


async def _update_quarantine_status(
    request: Request,
    ctx: Any,
    *,
    target_status: str,
) -> JSONResponse:
    raw_id = request.path_params.get("quarantine_id", "")
    try:
        qid = uuid.UUID(str(raw_id).strip())
    except (ValueError, TypeError, AttributeError):
        return JSONResponse({"error": "Invalid quarantine_id UUID"}, status_code=400)

    pg_pool = _get_ops_pg_pool(request, ctx)
    if pg_pool is None:
        return JSONResponse({"error": "PostgreSQL pool unavailable"}, status_code=503)

    try:
        async with pg_pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
            row = await conn.fetchrow(
                """
                UPDATE public.ops_quarantine
                SET status = $2, updated_at = now()
                WHERE quarantine_id = $1::uuid
                RETURNING quarantine_id, status, updated_at
                """,
                qid,
                target_status,
            )
            if row is None:
                return JSONResponse({"error": "Quarantine record not found"}, status_code=404)
    except Exception as exc:
        logger.error("Failed updating ops_quarantine status: %s", type(exc).__name__)
        return JSONResponse({"error": "Failed to update quarantine record"}, status_code=500)

    return JSONResponse(
        {
            "status": "ok",
            "quarantine_id": str(qid),
            "new_status": target_status,
        }
    )


async def ops_quarantine_requeue(request: Request, ctx: Any = None) -> JSONResponse:
    """POST /admin/ops/api/quarantine/{quarantine_id}/requeue — mark quarantined job requeued."""
    return await _update_quarantine_status(request, ctx, target_status="requeued")


async def ops_quarantine_dismiss(request: Request, ctx: Any = None) -> JSONResponse:
    """POST /admin/ops/api/quarantine/{quarantine_id}/dismiss — mark quarantined job dismissed."""
    return await _update_quarantine_status(request, ctx, target_status="dismissed")


async def ops_playbook_preview(request: Request, ctx: Any = None) -> JSONResponse:
    """POST /admin/ops/api/playbooks/{playbook_id}/preview — dry-run preview + confirmation token."""
    playbook_id = str(request.path_params.get("playbook_id") or "").strip().lower()
    if not _PLAYBOOK_ID_RE.fullmatch(playbook_id):
        return JSONResponse({"error": "Invalid playbook_id"}, status_code=400)

    body: dict[str, Any] = {}
    try:
        raw = await request.body()
        if raw:
            parsed = json.loads(raw.decode("utf-8"))
            if isinstance(parsed, dict):
                body = parsed
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    eng = getattr(admin_state, "engine", None)
    available = {
        "playbook_id": playbook_id,
        "dry_run": True,
        "incident_id": body.get("incident_id"),
        "params": body.get("params") if isinstance(body.get("params"), dict) else {},
        "engine": eng,
        "pg_pool": _get_ops_pg_pool(request, ctx),
        "redis_client": _resolve_redis_client(request, ctx),
        "approved_by": str(getattr(request.state, "admin_principal", "admin")),
    }
    try:
        res = await _call_ops_callable(
            preview_playbook,
            "nce.ops.playbooks",
            "preview_playbook",
            available_kwargs=available,
        )
    except (ModuleNotFoundError, AttributeError):
        return JSONResponse({"error": "Playbook subsystem unavailable"}, status_code=503)
    except LookupError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    safe_res = _json_safe(res)
    payload: dict[str, Any] = {"status": "ok", "preview": safe_res}
    if isinstance(safe_res, dict) and safe_res.get("confirmation_token"):
        payload["confirmation_token"] = safe_res["confirmation_token"]
    return JSONResponse(payload, status_code=200)


async def ops_playbook_execute(request: Request, ctx: Any = None) -> JSONResponse:
    """POST /admin/ops/api/playbooks/{playbook_id}/execute — execute enforcing confirmation_token."""
    playbook_id = str(request.path_params.get("playbook_id") or "").strip().lower()
    if not _PLAYBOOK_ID_RE.fullmatch(playbook_id):
        return JSONResponse({"error": "Invalid playbook_id"}, status_code=400)

    try:
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    confirmation_token = body.get("confirmation_token")
    if not isinstance(confirmation_token, str) or not confirmation_token.strip():
        return JSONResponse({"error": "Missing confirmation_token"}, status_code=400)

    eng = getattr(admin_state, "engine", None)
    available = {
        "playbook_id": playbook_id,
        "confirmation_token": confirmation_token.strip(),
        "dry_run": False,
        "incident_id": body.get("incident_id"),
        "params": body.get("params") if isinstance(body.get("params"), dict) else {},
        "engine": eng,
        "pg_pool": _get_ops_pg_pool(request, ctx),
        "redis_client": _resolve_redis_client(request, ctx),
        "approved_by": str(getattr(request.state, "admin_principal", "admin")),
    }
    try:
        res = await _call_ops_callable(
            execute_playbook,
            "nce.ops.playbooks",
            "execute_playbook",
            available_kwargs=available,
        )
    except (ModuleNotFoundError, AttributeError):
        return JSONResponse({"error": "Playbook subsystem unavailable"}, status_code=503)
    except LookupError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse({"status": "ok", "execution": _json_safe(res)}, status_code=200)


def _format_masked_token_prefix(token_id: Any) -> str:
    tid_str = str(token_id or "")
    tail = tid_str[-6:] if len(tid_str) >= 6 else tid_str
    return f"nce_mcp_****{tail}"


async def ops_tenants_list(request: Request, ctx: Any = None) -> JSONResponse:
    """GET /admin/ops/api/tenants — tenant namespaces (excl. _system), quotas, masked tokens."""
    pg_pool = _get_ops_pg_pool(request, ctx)
    if pg_pool is None:
        return JSONResponse({"error": "PostgreSQL pool unavailable"}, status_code=503)

    eng = getattr(admin_state, "engine", None)
    has_fleet_pool = bool(eng is not None and getattr(eng, "__dict__", {}).get("pg_fleet_pool"))
    use_scoped_rls = isinstance(pg_pool, asyncpg.Pool) and not has_fleet_pool

    namespaces: list[dict[str, Any]] = []
    quotas: list[dict[str, Any]] = []
    tokens: list[dict[str, Any]] = []

    try:
        async with pg_pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
            ns_rows = await conn.fetch(
                """
                SELECT id, slug, parent_id, created_at, embedding_provider,
                       embedding_model, embedding_dim, is_replay_sandbox
                FROM namespaces
                WHERE slug <> $1
                ORDER BY created_at DESC
                LIMIT 200
                """,
                SYSTEM_NAMESPACE_SLUG,
            )
            for r in ns_rows:
                namespaces.append(
                    {
                        "id": str(r["id"]),
                        "slug": str(r["slug"]),
                        "parent_id": str(r["parent_id"]) if r["parent_id"] else None,
                        "created_at": _iso(r["created_at"]),
                        "embedding_provider": r["embedding_provider"],
                        "embedding_model": r["embedding_model"],
                        "embedding_dim": r["embedding_dim"],
                        "is_replay_sandbox": bool(r["is_replay_sandbox"]),
                    }
                )

            if not use_scoped_rls:
                quota_rows = await conn.fetch(
                    """
                    SELECT q.id, q.namespace_id, q.agent_id, q.resource_type,
                           q.limit_amount, q.used_amount, q.reset_at, q.updated_at
                    FROM resource_quotas q
                    JOIN namespaces n ON n.id = q.namespace_id
                    WHERE n.slug <> $1
                    ORDER BY q.updated_at DESC
                    LIMIT 250
                    """,
                    SYSTEM_NAMESPACE_SLUG,
                )
                for r in quota_rows:
                    quotas.append(
                        {
                            "id": str(r["id"]),
                            "namespace_id": str(r["namespace_id"]),
                            "agent_id": r["agent_id"],
                            "resource_type": str(r["resource_type"]),
                            "limit_amount": int(r["limit_amount"] or 0),
                            "used_amount": int(r["used_amount"] or 0),
                            "reset_at": _iso(r["reset_at"]),
                            "updated_at": _iso(r["updated_at"]),
                        }
                    )

                token_rows = await conn.fetch(
                    """
                    SELECT t.token_id, t.namespace_id, t.principal_id, t.scopes,
                           t.allowed_engines, t.tool_exposure_mode, t.rate_limit_rpm,
                           t.expires_at, t.revoked_at, t.created_at
                    FROM mcp_client_tokens t
                    JOIN namespaces n ON n.id = t.namespace_id
                    WHERE n.slug <> $1
                    ORDER BY t.created_at DESC
                    LIMIT 250
                    """,
                    SYSTEM_NAMESPACE_SLUG,
                )
                for r in token_rows:
                    tokens.append(
                        {
                            "token_id": str(r["token_id"]),
                            "token_prefix": _format_masked_token_prefix(r["token_id"]),
                            "namespace_id": str(r["namespace_id"]),
                            "principal_id": str(r["principal_id"]),
                            "scopes": _json_col(r["scopes"], []),
                            "allowed_engines": list(r["allowed_engines"] or ["*"]),
                            "tool_exposure_mode": str(r["tool_exposure_mode"] or "compact"),
                            "rate_limit_rpm": int(r["rate_limit_rpm"] or 120),
                            "expires_at": _iso(r["expires_at"]),
                            "revoked_at": _iso(r["revoked_at"]),
                            "created_at": _iso(r["created_at"]),
                            "active": r["revoked_at"] is None,
                        }
                    )

        if use_scoped_rls:
            for ns in namespaces[:50]:
                async with scoped_pg_session(pg_pool, ns["id"]) as sconn:
                    q_rows = await sconn.fetch(
                        """
                        SELECT id, namespace_id, agent_id, resource_type,
                               limit_amount, used_amount, reset_at, updated_at
                        FROM resource_quotas
                        ORDER BY updated_at DESC
                        LIMIT 50
                        """
                    )
                    for r in q_rows:
                        quotas.append(
                            {
                                "id": str(r["id"]),
                                "namespace_id": str(r["namespace_id"]),
                                "agent_id": r["agent_id"],
                                "resource_type": str(r["resource_type"]),
                                "limit_amount": int(r["limit_amount"] or 0),
                                "used_amount": int(r["used_amount"] or 0),
                                "reset_at": _iso(r["reset_at"]),
                                "updated_at": _iso(r["updated_at"]),
                            }
                        )
                    t_rows = await sconn.fetch(
                        """
                        SELECT token_id, namespace_id, principal_id, scopes,
                               allowed_engines, tool_exposure_mode, rate_limit_rpm,
                               expires_at, revoked_at, created_at
                        FROM mcp_client_tokens
                        ORDER BY created_at DESC
                        LIMIT 50
                        """
                    )
                    for r in t_rows:
                        tokens.append(
                            {
                                "token_id": str(r["token_id"]),
                                "token_prefix": _format_masked_token_prefix(r["token_id"]),
                                "namespace_id": str(r["namespace_id"]),
                                "principal_id": str(r["principal_id"]),
                                "scopes": _json_col(r["scopes"], []),
                                "allowed_engines": list(r["allowed_engines"] or ["*"]),
                                "tool_exposure_mode": str(r["tool_exposure_mode"] or "compact"),
                                "rate_limit_rpm": int(r["rate_limit_rpm"] or 120),
                                "expires_at": _iso(r["expires_at"]),
                                "revoked_at": _iso(r["revoked_at"]),
                                "created_at": _iso(r["created_at"]),
                                "active": r["revoked_at"] is None,
                            }
                        )
    except Exception as exc:
        logger.error("Failed to load ops tenants view: %s", type(exc).__name__)
        return JSONResponse({"error": "Failed to query tenant metadata"}, status_code=500)

    return JSONResponse(
        {
            "status": "ok",
            "namespaces": namespaces,
            "quotas": quotas,
            "tokens": tokens,
        }
    )


def _resolve_mcp_public_url(request: Request) -> str:
    raw_host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.hostname
        or "localhost"
    ).strip()
    if raw_host.startswith("["):
        host = raw_host.split("]")[0] + "]"
    else:
        host = raw_host.split(":")[0].strip() or "localhost"
    if host == "testserver":
        host = "localhost"
    return f"https://{host}:8443/mcp"


async def ops_tokens_mint(request: Request, ctx: Any = None) -> JSONResponse:
    """POST /admin/ops/api/tokens/mint — mint scoped /mcp token with 4-scope + mode controls."""
    from nce.admin_plugins import _MAX_MINTED_MCP_TOKENS, MINTED_MCP_TOKENS

    try:
        raw = await request.body()
        body = json.loads(raw.decode("utf-8")) if raw else {}
        if not isinstance(body, dict):
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": f"Invalid JSON body: {exc}"}, status_code=400)

    raw_scopes = body.get("scopes")
    if raw_scopes is None:
        scopes_list = [
            "memory:read",
            "memory:write",
            "graph:read",
            "mcp:tools:discover",
            "mcp:tools:call",
        ]
    else:
        if not isinstance(raw_scopes, list) or not raw_scopes:
            return JSONResponse(
                {"error": "'scopes' must be a non-empty list of allowed scope strings"},
                status_code=400,
            )
        scopes_list = []
        for item in raw_scopes:
            if not isinstance(item, str) or item not in OPS_ALLOWED_MCP_TOKEN_SCOPES:
                return JSONResponse(
                    {
                        "error": (
                            f"Invalid scope {item!r}. Allowed: {sorted(OPS_ALLOWED_MCP_TOKEN_SCOPES)}"
                        )
                    },
                    status_code=400,
                )
            if item not in scopes_list:
                scopes_list.append(item)

    raw_ns = body.get("namespace_id")
    if raw_ns is not None and str(raw_ns).strip() != "":
        try:
            namespace_id = str(uuid.UUID(str(raw_ns).strip()))
        except (ValueError, TypeError, AttributeError):
            return JSONResponse(
                {"error": "Invalid 'namespace_id': must be a valid UUID"}, status_code=400
            )
    else:
        namespace_id = "00000000-0000-0000-0000-000000000001"

    principal_id = str(body.get("principal_id") or "mcp-operator").strip()[:128] or "mcp-operator"

    raw_mode = str(body.get("tool_exposure_mode", "compact")).strip().lower()
    if raw_mode not in ("compact", "full"):
        return JSONResponse(
            {"error": "'tool_exposure_mode' must be 'compact' or 'full'"}, status_code=400
        )

    allowed_engines = [
        str(e).strip()
        for e in (body.get("allowed_engines") or body.get("engines") or [])
        if str(e).strip()
    ]
    if not allowed_engines:
        allowed_engines = ["*"]

    try:
        raw_rpm = body.get("rate_limit_rpm", 120)
        if isinstance(raw_rpm, bool):
            raise ValueError("bool not allowed")
        rate_limit_rpm = int(raw_rpm)
        if not (1 <= rate_limit_rpm <= 10000):
            raise ValueError("out of range")
    except (ValueError, TypeError):
        return JSONResponse(
            {"error": "'rate_limit_rpm' must be an integer between 1 and 10000"}, status_code=400
        )

    try:
        raw_exp = body.get("expires_in_days", 30)
        if isinstance(raw_exp, bool):
            raise ValueError("bool not allowed")
        expires_in_days = int(raw_exp)
        if not (1 <= expires_in_days <= 365):
            raise ValueError("out of range")
    except (ValueError, TypeError):
        return JSONResponse(
            {"error": "'expires_in_days' must be an integer between 1 and 365"}, status_code=400
        )

    now_utc = datetime.now(UTC)
    expires_at = now_utc + timedelta(days=expires_in_days)
    token_id = str(uuid.uuid4())
    raw_token = f"nce_mcp_{secrets.token_urlsafe(32)}"
    token_sha256 = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    token_meta = {
        "token_id": token_id,
        "token_sha256": token_sha256,
        "namespace_id": namespace_id,
        "principal_id": principal_id,
        "scopes": scopes_list,
        "allowed_engines": allowed_engines,
        "tool_exposure_mode": raw_mode,
        "rate_limit_rpm": rate_limit_rpm,
        "expires_at": expires_at.isoformat(),
        "created_at": now_utc.isoformat(),
    }
    MINTED_MCP_TOKENS[token_sha256] = token_meta
    MINTED_MCP_TOKENS.move_to_end(token_sha256)
    while len(MINTED_MCP_TOKENS) > _MAX_MINTED_MCP_TOKENS:
        MINTED_MCP_TOKENS.popitem(last=False)

    pg_pool = _get_ops_pg_pool(request, ctx)
    if pg_pool is not None:
        try:
            async with scoped_pg_session(pg_pool, namespace_id) as conn:
                await conn.execute(
                    """
                    INSERT INTO mcp_client_tokens (
                        token_id, token_sha256, namespace_id, principal_id,
                        scopes, allowed_engines, tool_exposure_mode,
                        rate_limit_rpm, expires_at, created_at
                    ) VALUES (
                        $1::uuid, $2, $3::uuid, $4,
                        $5::jsonb, $6::text[], $7,
                        $8, $9, $10
                    )
                    """,
                    uuid.UUID(token_id),
                    token_sha256,
                    uuid.UUID(namespace_id),
                    principal_id,
                    json.dumps(scopes_list),
                    allowed_engines,
                    raw_mode,
                    rate_limit_rpm,
                    expires_at,
                    now_utc,
                )
        except Exception as exc:
            MINTED_MCP_TOKENS.pop(token_sha256, None)
            logger.error("Failed to persist minted token: %s", type(exc).__name__)
            return JSONResponse({"error": "Failed to persist minted token"}, status_code=500)

    mcp_url = _resolve_mcp_public_url(request)
    mcp_config = {
        "mcpServers": {
            "nce-http": {
                "url": mcp_url,
                "headers": {"Authorization": f"Bearer {raw_token}"},
            },
            "nce-proxy": {
                "command": "python",
                "args": ["-m", "nce.mcp_client_proxy"],
                "env": {
                    "NCE_MCP_HTTP_URL": mcp_url,
                    "NCE_MCP_CLIENT_TOKEN": raw_token,
                },
            },
        }
    }

    return JSONResponse(
        {
            "status": "ok",
            "token_id": token_id,
            "token_prefix": _format_masked_token_prefix(token_id),
            "token": raw_token,
            "namespace_id": namespace_id,
            "principal_id": principal_id,
            "scopes": scopes_list,
            "allowed_engines": allowed_engines,
            "tool_exposure_mode": raw_mode,
            "rate_limit_rpm": rate_limit_rpm,
            "expires_at": expires_at.isoformat(),
            "mcp_url": mcp_url,
            "mcp_config_json": json.dumps(mcp_config, indent=2),
        },
        status_code=201,
    )


async def ops_tokens_revoke(request: Request, ctx: Any = None) -> JSONResponse:
    """POST /admin/ops/api/tokens/{token_id}/revoke — set revoked_at = now() in mcp_client_tokens."""
    from nce.admin_plugins import MINTED_MCP_TOKENS
    from nce.mcp_http_gateway import _IN_MEMORY_MCP_CLIENT_TOKENS, invalidate_verified_token_cache

    raw_id = request.path_params.get("token_id", "")
    try:
        token_uuid = uuid.UUID(str(raw_id).strip())
    except (ValueError, TypeError, AttributeError):
        return JSONResponse({"error": "Invalid token_id UUID"}, status_code=400)

    token_id_str = str(token_uuid)
    now_utc = datetime.now(UTC)

    mem_matched = False
    for sha, meta in list(MINTED_MCP_TOKENS.items()):
        if str(meta.get("token_id")) == token_id_str:
            meta["revoked_at"] = now_utc.isoformat()
            invalidate_verified_token_cache(sha)
            mem_matched = True
    for sha, meta in list(_IN_MEMORY_MCP_CLIENT_TOKENS.items()):
        if str(meta.get("token_id")) == token_id_str:
            meta["revoked_at"] = now_utc.isoformat()
            invalidate_verified_token_cache(sha)
            mem_matched = True

    pg_pool = _get_ops_pg_pool(request, ctx)
    if pg_pool is None:
        if mem_matched:
            return JSONResponse(
                {
                    "status": "ok",
                    "token_id": token_id_str,
                    "revoked": True,
                    "revoked_at": now_utc.isoformat(),
                }
            )
        return JSONResponse({"error": "Token not found"}, status_code=404)

    body: dict[str, Any] = {}
    try:
        raw = await request.body()
        if raw:
            parsed = json.loads(raw.decode("utf-8"))
            if isinstance(parsed, dict):
                body = parsed
    except Exception:
        body = {}

    raw_ns = request.query_params.get("namespace_id") or body.get("namespace_id")
    eng = getattr(admin_state, "engine", None)
    has_fleet_pool = bool(eng is not None and getattr(eng, "__dict__", {}).get("pg_fleet_pool"))
    use_scoped_rls = isinstance(pg_pool, asyncpg.Pool) and not has_fleet_pool and bool(raw_ns)

    try:
        if use_scoped_rls:
            ns_uuid = str(uuid.UUID(str(raw_ns).strip()))
            async with scoped_pg_session(pg_pool, ns_uuid) as conn:
                row = await conn.fetchrow(
                    """
                    UPDATE mcp_client_tokens
                    SET revoked_at = COALESCE(revoked_at, now())
                    WHERE token_id = $1::uuid
                    RETURNING token_id, token_sha256, revoked_at
                    """,
                    token_uuid,
                )
        else:
            async with pg_pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
                row = await conn.fetchrow(
                    """
                    UPDATE mcp_client_tokens
                    SET revoked_at = COALESCE(revoked_at, now())
                    WHERE token_id = $1::uuid
                    RETURNING token_id, token_sha256, revoked_at
                    """,
                    token_uuid,
                )
    except Exception as exc:
        logger.error("Failed to revoke mcp_client_token: %s", type(exc).__name__)
        return JSONResponse({"error": "Failed to revoke token"}, status_code=500)

    if row is None and not mem_matched:
        return JSONResponse({"error": "Token not found"}, status_code=404)

    if row is not None:
        sha = str(row["token_sha256"] or "")
        if sha:
            invalidate_verified_token_cache(sha)

    return JSONResponse(
        {
            "status": "ok",
            "token_id": token_id_str,
            "revoked": True,
            "revoked_at": _iso(row["revoked_at"]) if row is not None else now_utc.isoformat(),
        }
    )


# ---------------------------------------------------------------------------
# Route Registration
# ---------------------------------------------------------------------------


def register_admin_ops_routes(
    routes: list[Route] | None = None,
    ctx: Any = None,
) -> list[Route]:
    """Register Day-2 ops, browser session, and air-gapped console shell routes."""
    from nce.admin_handlers.tools import api_admin_tools, api_admin_tools_toggle

    def _bind_local(hnd: Any) -> Any:
        async def _endpoint(req: Any) -> Any:
            try:
                return await hnd(req, ctx)
            except TypeError:
                return await hnd(req)

        _endpoint.__name__ = getattr(hnd, "__name__", "_endpoint")
        _endpoint.__doc__ = getattr(hnd, "__doc__", None)
        return _endpoint

    ops_routes = [
        Route("/admin", _bind_local(serve_admin_console), methods=["GET"]),
        Route("/admin/index.html", _bind_local(serve_admin_console), methods=["GET"]),
        Route("/admin/console.js", _bind_local(serve_admin_console_js), methods=["GET"]),
        Route("/admin/app.js", _bind_local(serve_admin_console_js), methods=["GET"]),
        Route("/admin/console.css", _bind_local(serve_admin_console_css), methods=["GET"]),
        Route("/admin/styles.css", _bind_local(serve_admin_console_css), methods=["GET"]),
        Route("/admin/session/login", _bind_local(post_admin_session_login), methods=["POST"]),
        Route("/admin/session/logout", _bind_local(post_admin_session_logout), methods=["POST"]),
        Route("/admin/session/status", _bind_local(get_admin_session_status), methods=["GET"]),
        Route("/admin/ops/api/overview", _bind_local(ops_overview), methods=["GET"]),
        Route("/admin/ops/api/backups", _bind_local(ops_backups_list), methods=["GET"]),
        Route("/admin/ops/api/backups/trigger", _bind_local(ops_backups_trigger), methods=["POST"]),
        Route(
            "/admin/ops/api/backups/{backup_id}/drill",
            _bind_local(ops_backups_drill),
            methods=["POST"],
        ),
        Route("/admin/ops/api/incidents", _bind_local(ops_incidents_list), methods=["GET"]),
        Route("/admin/ops/api/quarantine", _bind_local(ops_quarantine_list), methods=["GET"]),
        Route(
            "/admin/ops/api/quarantine/{quarantine_id}/requeue",
            _bind_local(ops_quarantine_requeue),
            methods=["POST"],
        ),
        Route(
            "/admin/ops/api/quarantine/{quarantine_id}/dismiss",
            _bind_local(ops_quarantine_dismiss),
            methods=["POST"],
        ),
        Route(
            "/admin/ops/api/playbooks/{playbook_id}/preview",
            _bind_local(ops_playbook_preview),
            methods=["POST"],
        ),
        Route(
            "/admin/ops/api/playbooks/{playbook_id}/execute",
            _bind_local(ops_playbook_execute),
            methods=["POST"],
        ),
        Route("/admin/ops/api/tenants", _bind_local(ops_tenants_list), methods=["GET"]),
        Route("/admin/ops/api/tokens/mint", _bind_local(ops_tokens_mint), methods=["POST"]),
        Route(
            "/admin/ops/api/tokens/{token_id}/revoke",
            _bind_local(ops_tokens_revoke),
            methods=["POST"],
        ),
        Route("/admin/ops/api/tools", _bind_local(api_admin_tools), methods=["GET"]),
        Route("/admin/ops/api/tools/toggle", _bind_local(api_admin_tools_toggle), methods=["POST"]),
    ]
    if routes is not None:
        routes.extend(ops_routes)
    return ops_routes


__all__ = [
    "AdminDualAuthMiddleware",
    "CSRF_COOKIE_NAME",
    "GOVERNANCE_CACHE_HYDRATED_KEY",
    "OPS_ALLOWED_MCP_TOKEN_SCOPES",
    "SESSION_COOKIE_NAME",
    "SESSION_TTL_SECONDS",
    "clear_admin_ops_caches",
    "get_admin_session_status",
    "hydrate_governance_cache",
    "mint_admin_session_cookie",
    "ops_backups_drill",
    "ops_backups_list",
    "ops_backups_trigger",
    "ops_incidents_list",
    "ops_overview",
    "ops_playbook_execute",
    "ops_playbook_preview",
    "ops_quarantine_dismiss",
    "ops_quarantine_list",
    "ops_quarantine_requeue",
    "ops_tenants_list",
    "ops_tokens_mint",
    "ops_tokens_revoke",
    "persist_governance_toggle",
    "post_admin_session_login",
    "post_admin_session_logout",
    "register_admin_ops_routes",
    "serve_admin_console",
    "serve_admin_console_css",
    "serve_admin_console_js",
    "verify_admin_csrf",
    "verify_admin_session_cookie",
]
