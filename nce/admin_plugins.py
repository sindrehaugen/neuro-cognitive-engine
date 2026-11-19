"""Admin plugin catalog, runtime enable/disable toggles, and dual-axis filtering."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import sys
import time
import uuid
from base64 import b64decode
from binascii import Error as BinasciiError
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from nce import admin_state
from nce.auth import (
    _PBKDF2_HASH_FORMAT_PREFIX,
    _TIMESTAMP_DRIFT_SECONDS,
    hmac_principal_scope,
    validate_hmac_nonce,
    verify_admin_password,
    verify_hmac,
)
from nce.config import cfg, live_admin_api_key
from nce.connectors.guards import AllowListRedactor, BridgeURLValidationError
from nce.connectors.vault import TenantConnectionVault
from nce.plugins import list_plugin_engine_descriptors
from nce.tool_governance import GovernanceUnavailable

logger = logging.getLogger("nce-admin-plugins")

REDIS_DISABLED_ENGINES_KEY = "nce:engines:disabled"
_IN_MEMORY_DISABLED_ENGINES: set[str] = set()
_PLUGINS_HTML_PATH = Path(__file__).resolve().parent.parent / "admin" / "plugins.html"
MINTED_MCP_TOKENS: OrderedDict[str, dict[str, Any]] = OrderedDict()
_MAX_MINTED_MCP_TOKENS = 512


async def get_disabled_engines(
    redis_client: Any = None,
    *,
    is_prod: bool | None = None,
) -> set[str]:
    """Retrieve the set of disabled engine slugs from Redis or in-memory fallback.

    In production (cfg.IS_PROD or NCE_ENV=production), if Redis is unavailable
    or raises an error, raises GovernanceUnavailable (fail-closed).
    In dev/test, falls back to _IN_MEMORY_DISABLED_ENGINES.
    """
    if is_prod is None:
        is_prod = bool(
            cfg.IS_PROD or os.environ.get("NCE_ENV", "").lower() in ("production", "prod")
        )

    if redis_client is not None:
        try:
            keys_raw = await redis_client.hkeys(REDIS_DISABLED_ENGINES_KEY)
            redis_disabled = {
                k.decode("utf-8") if isinstance(k, bytes) else str(k) for k in keys_raw
            }
            _IN_MEMORY_DISABLED_ENGINES.clear()
            _IN_MEMORY_DISABLED_ENGINES.update(redis_disabled)
            return set(redis_disabled)
        except Exception as exc:
            if is_prod:
                raise GovernanceUnavailable(
                    "Redis unavailable in production (fail-closed)"
                ) from exc
            logger.warning(
                "Redis read failed for %s, falling back to memory: %s",
                REDIS_DISABLED_ENGINES_KEY,
                exc,
            )
            return set(_IN_MEMORY_DISABLED_ENGINES)

    if is_prod:
        raise GovernanceUnavailable("Redis unavailable in production (fail-closed)")

    return set(_IN_MEMORY_DISABLED_ENGINES)


async def is_engine_disabled(
    engine_slug: str | None,
    redis_client: Any = None,
    *,
    is_prod: bool | None = None,
) -> bool:
    """Check if a specific engine slug is currently admin-disabled."""
    if not engine_slug:
        return False
    disabled = await get_disabled_engines(redis_client=redis_client, is_prod=is_prod)
    return engine_slug in disabled


is_engine_disabled_sync_or_async = is_engine_disabled


def _check_admin_auth(request: Request) -> JSONResponse | None:
    """Validate admin authentication across plugin endpoints.

    Checks:
      - X-Admin-Api-Key, X-NCE-Admin-Key, X-NCE-Api-Key
      - Authorization: Bearer <key>
      - Authorization: Basic <base64(user:pass)>
      - Authorization: HMAC-SHA256 <sig>
      - NCE_ADMIN_OVERRIDE (allowed only in dev/test when no admin keys are configured)
    """
    # 1. API key in custom headers
    headers = getattr(request, "headers", None)
    if headers is None:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    api_key = (
        headers.get("x-admin-api-key")
        or headers.get("x-nce-admin-key")
        or headers.get("x-nce-api-key")
    )
    if api_key:
        api_key = api_key.strip()

    # 2. Authorization header
    auth = headers.get("authorization", "").strip()
    if auth:
        auth_lower = auth.lower()
        if auth_lower.startswith("bearer "):
            api_key = auth[7:].strip()
        elif auth_lower.startswith("basic "):
            token = auth[6:].strip()
            try:
                decoded = b64decode(token).decode("utf-8")
                username, _, password = decoded.partition(":")
            except (ValueError, BinasciiError, UnicodeDecodeError):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)

            expected_user = cfg.NCE_ADMIN_USERNAME or "admin"
            expected_pass = cfg.NCE_ADMIN_PASSWORD or ""

            user_matches = secrets.compare_digest(
                username.encode("utf-8"), expected_user.encode("utf-8")
            )
            pass_matches = False
            if expected_pass:
                if expected_pass.startswith(_PBKDF2_HASH_FORMAT_PREFIX):
                    valid_pw, _ = verify_admin_password(password, expected_pass, auto_upgrade=False)
                    pass_matches = valid_pw
                else:
                    pass_matches = secrets.compare_digest(
                        password.encode("utf-8"), expected_pass.encode("utf-8")
                    )
            elif password == "" and expected_pass == "":
                pass_matches = True

            if user_matches and pass_matches and expected_pass:
                return None
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        elif auth.startswith("HMAC-SHA256 ") or auth_lower.startswith("hmac-sha256 "):
            if (
                getattr(request.state, "hmac_authenticated", False)
                or getattr(request.state, "namespace_ctx", None) is not None
            ):
                return None
            ts_raw = headers.get("x-nce-timestamp")
            nonce_val = (headers.get("x-nce-nonce") or "").strip()
            sig_val = auth.split(" ", 1)[1].strip()
            if ts_raw and validate_hmac_nonce(nonce_val):
                try:
                    ts = int(ts_raw)
                    if abs(int(time.time()) - ts) > _TIMESTAMP_DRIFT_SECONDS:
                        return JSONResponse({"error": "Unauthorized"}, status_code=401)
                    body_bytes = getattr(request, "_body", b"")
                    expected_hmac_keys = [k for k in (cfg.NCE_API_KEY, live_admin_api_key()) if k]
                    for candidate_key in expected_hmac_keys:
                        if verify_hmac(
                            candidate_key,
                            request.method,
                            request.url.path,
                            ts,
                            body_bytes,
                            sig_val,
                            nonce=nonce_val,
                        ):
                            state = getattr(request, "state", None)
                            if state is not None:
                                state._matched_hmac_scope = hmac_principal_scope(candidate_key)
                            return None
                except Exception:
                    pass
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

    expected_keys = [
        k.strip()
        for k in (
            live_admin_api_key(),
            cfg.NCE_API_KEY,
            os.environ.get("NCE_ADMIN_API_KEY", ""),
            os.environ.get("NCE_API_KEY", ""),
        )
        if k and k.strip()
    ]

    if api_key is not None:
        if expected_keys and any(
            secrets.compare_digest(api_key.encode("utf-8"), k.encode("utf-8"))
            for k in expected_keys
        ):
            return None
        return JSONResponse({"error": "Invalid admin API key"}, status_code=401)

    # No auth header provided
    is_prod = bool(cfg.IS_PROD or os.environ.get("NCE_ENV", "").lower() in ("production", "prod"))
    admin_override_env = os.environ.get("NCE_ADMIN_OVERRIDE", "").lower()
    override_disabled_in_env = admin_override_env in ("0", "false", "no")
    has_admin_key_in_env = bool(os.environ.get("NCE_ADMIN_API_KEY"))

    if override_disabled_in_env or not cfg.NCE_ADMIN_OVERRIDE or is_prod or has_admin_key_in_env:
        return JSONResponse({"error": "Authentication required"}, status_code=401)

    return None


def _get_admin_hmac_nonce_store() -> Any:
    """Resolve the admin HMAC NonceStore from nce.admin_app or optional_hmac_nonce_store()."""
    admin_app_mod = sys.modules.get("nce.admin_app")
    if admin_app_mod is not None:
        store = getattr(admin_app_mod, "_hmac_nonce_store", None)
        if store is not None:
            return store
    from nce.auth import optional_hmac_nonce_store

    return optional_hmac_nonce_store()


async def _check_admin_auth_async(request: Request) -> JSONResponse | None:
    """Async wrapper over _check_admin_auth adding distributed HMAC replay protection."""
    headers = getattr(request, "headers", None)
    auth = ((headers.get("authorization") if headers else None) or "").strip()
    if auth.lower().startswith("hmac-sha256 ") and callable(getattr(request, "body", None)):
        try:
            await request.body()
        except Exception:
            pass

    err = _check_admin_auth(request)
    if err is not None:
        return err

    if headers is None or not auth.lower().startswith("hmac-sha256 "):
        return None

    state = getattr(request, "state", None)
    if state is not None and (
        getattr(state, "hmac_authenticated", False)
        or getattr(state, "namespace_ctx", None) is not None
    ):
        return None

    nonce_val = (headers.get("x-nce-nonce") or "").strip()
    if not validate_hmac_nonce(nonce_val):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    principal_scope = getattr(state, "_matched_hmac_scope", None) if state is not None else None
    if not principal_scope:
        fallback_key = cfg.NCE_API_KEY or live_admin_api_key() or ""
        principal_scope = hmac_principal_scope(fallback_key)
    scoped_nonce = f"{principal_scope}:{nonce_val}"

    nonce_store = _get_admin_hmac_nonce_store()
    is_prod = bool(cfg.IS_PROD or os.environ.get("NCE_ENV", "").lower() in ("production", "prod"))

    if nonce_store is None:
        if is_prod:
            logger.error("Admin plugin HMAC requires NonceStore in production (fail-closed)")
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return None

    ping_fn = getattr(nonce_store, "ping", None)
    redis_up = await ping_fn() if callable(ping_fn) else True
    if not redis_up:
        if is_prod:
            logger.error("Admin plugin HMAC NonceStore unreachable in production (fail-closed)")
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        logger.warning("Admin plugin HMAC NonceStore unreachable — allowing in dev/test")
        return None

    if not await nonce_store.check_and_store(scoped_nonce):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    return None


def _matches_status(d: dict[str, Any], sf: str) -> bool:
    if sf == "active":
        return bool(d.get("enabled", False))
    if sf == "installed":
        return bool(d.get("installed", False))
    if sf in ("uninstalled", "not-installed"):
        return not bool(d.get("installed", False))
    return True


def _matches_q(d: dict[str, Any], query: str) -> bool:
    if not query:
        return True
    slug = str(d.get("slug", "")).lower()
    name = str(d.get("name", "")).lower()
    desc = str(d.get("description", "")).lower()
    types = [str(t).lower() for t in d.get("types", ())]
    categories = [str(c).lower() for c in d.get("categories", ())]
    return (
        query in slug
        or query in name
        or query in desc
        or any(query in t for t in types)
        or any(query in c for c in categories)
    )


_VALID_MATURITIES: frozenset[str] = frozenset({"stable", "beta", "experimental"})
_VALID_AUTH_KINDS: frozenset[str] = frozenset(
    {"oauth2", "api_key", "entra_oidc", "webhook", "local_file", "none"}
)
_VALID_CONNECTION_STATUSES: frozenset[str] = frozenset(
    {"connected", "disconnected", "error", "pending"}
)
_DEFAULT_NAMESPACE_ID = "00000000-0000-0000-0000-000000000001"
_APP_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_META_REDACTOR = AllowListRedactor(mode="strip")


def _matches_types(d: dict[str, Any], st: list[str]) -> bool:
    if not st:
        return True
    d_types = {str(t).lower() for t in d.get("types", ())}
    return bool(d_types & set(st))


def _matches_categories(d: dict[str, Any], sc: list[str]) -> bool:
    if not sc:
        return True
    d_cats = {str(c).lower() for c in d.get("categories", ())}
    return bool(d_cats & set(sc))


def _matches_maturities(d: dict[str, Any], sm: list[str]) -> bool:
    if not sm:
        return True
    return str(d.get("maturity", "stable")).strip().lower() in set(sm)


def build_plugins_catalog_payload(
    descriptors: list[dict[str, Any]],
    disabled_slugs: set[str],
    *,
    type_param: str | None = None,
    category_param: str | None = None,
    maturity_param: str | None = None,
    status_param: str | None = None,
    q_param: str | None = None,
) -> dict[str, Any]:
    """Compute dual-axis + maturity filtered engines and cross-faceted badge counts."""
    catalog = [dict(d) for d in descriptors]
    for d in catalog:
        slug = d["slug"]
        installed = bool(d.get("installed", True))
        enabled_by_env = bool(d.get("enabled_by_env", d.get("enabled", True)))
        runtime_disabled = slug in disabled_slugs
        active = installed and enabled_by_env and not runtime_disabled
        raw_maturity = str(d.get("maturity") or "stable").strip().lower()
        d["enabled"] = active
        d["active"] = active
        d["runtime_disabled"] = runtime_disabled
        d["maturity"] = raw_maturity if raw_maturity in _VALID_MATURITIES else "stable"
        d["vendor_specific"] = bool(d.get("vendor_specific", False))

    selected_types = [t.strip().lower() for t in (type_param or "").split(",") if t.strip()]
    selected_categories = [
        c.strip().lower() for c in (category_param or "").split(",") if c.strip()
    ]
    selected_maturities = [
        m.strip().lower()
        for m in (maturity_param or "").split(",")
        if m.strip() and m.strip().lower() != "all"
    ]
    status_filter = (status_param or "all").strip().lower()
    q = (q_param or "").strip().lower()

    matched = [
        d
        for d in catalog
        if _matches_types(d, selected_types)
        and _matches_categories(d, selected_categories)
        and _matches_maturities(d, selected_maturities)
        and _matches_status(d, status_filter)
        and _matches_q(d, q)
    ]

    all_types = sorted({str(t) for d in catalog for t in d.get("types", ()) if str(t).strip()})
    type_counts: dict[str, int] = {}
    for t in all_types:
        t_lower = t.lower()
        type_counts[t] = sum(
            1
            for d in catalog
            if _matches_categories(d, selected_categories)
            and _matches_maturities(d, selected_maturities)
            and _matches_status(d, status_filter)
            and _matches_q(d, q)
            and t_lower in {str(x).lower() for x in d.get("types", ())}
        )

    all_categories = sorted(
        {str(c) for d in catalog for c in d.get("categories", ()) if str(c).strip()}
    )
    cat_counts: dict[str, int] = {}
    for c in all_categories:
        c_lower = c.lower()
        cat_counts[c] = sum(
            1
            for d in catalog
            if _matches_types(d, selected_types)
            and _matches_maturities(d, selected_maturities)
            and _matches_status(d, status_filter)
            and _matches_q(d, q)
            and c_lower in {str(x).lower() for x in d.get("categories", ())}
        )

    maturity_counts = {
        stage: sum(1 for d in catalog if d["maturity"] == stage)
        for stage in ("stable", "beta", "experimental")
    }

    base_pkg = "nce-vertical-engines"
    for d in catalog:
        extra = str(d.get("install_extra", ""))
        if "[" in extra:
            candidate = extra.split("[")[0].strip()
            if candidate:
                base_pkg = candidate
                break

    if (
        not selected_types
        and not selected_categories
        and not selected_maturities
        and not q
        and status_filter == "all"
    ):
        pip_snippet = f'pip install "{base_pkg}[all]"'
    elif selected_types and not selected_categories and not selected_maturities and not q:
        extras = [f"type-{t}" for t in selected_types]
        pip_snippet = f'pip install "{base_pkg}[{",".join(extras)}]"'
    elif selected_categories and not selected_types and not selected_maturities and not q:
        extras = [f"cat-{c}" for c in selected_categories]
        pip_snippet = f'pip install "{base_pkg}[{",".join(extras)}]"'
    else:
        matched_slugs = [d["slug"] for d in matched]
        if matched_slugs:
            pip_snippet = f'pip install "{base_pkg}[{",".join(matched_slugs)}]"'
        else:
            pip_snippet = "# No matching engine extras for current filter"

    if selected_types and selected_categories:
        env_snippet = (
            f"NCE_ENGINE_TYPE_FILTER={','.join(selected_types)}\n"
            f"NCE_ENGINE_CATEGORY_FILTER={','.join(selected_categories)}"
        )
    elif selected_types:
        env_snippet = f"NCE_ENGINE_TYPE_FILTER={','.join(selected_types)}"
    elif selected_categories:
        env_snippet = f"NCE_ENGINE_CATEGORY_FILTER={','.join(selected_categories)}"
    elif selected_maturities or q or status_filter != "all":
        matched_slugs = [d["slug"] for d in matched]
        env_snippet = f"NCE_ENABLED_ENGINES={','.join(matched_slugs)}"
    else:
        env_snippet = "NCE_ENABLED_ENGINES=all"

    return {
        "summary": {
            "catalog": len(catalog),
            "installed": sum(1 for d in catalog if d["installed"]),
            "active": sum(1 for d in catalog if d["enabled"]),
            "matched": len(matched),
            "maturity": maturity_counts,
        },
        "maturity_counts": maturity_counts,
        "filters": {
            "types": selected_types,
            "categories": selected_categories,
            "maturity": selected_maturities,
            "status": status_filter,
            "q": q_param or "",
        },
        "facets": {
            "types": [
                {
                    "name": t,
                    "count": type_counts.get(t, 0),
                    "active": t.lower() in selected_types,
                }
                for t in all_types
            ],
            "categories": [
                {
                    "name": c,
                    "count": cat_counts.get(c, 0),
                    "active": c.lower() in selected_categories,
                }
                for c in all_categories
            ],
        },
        "snippets": {
            "pip_install": pip_snippet,
            "env_vars": env_snippet,
        },
        "engines": matched,
        "plugins": matched,
        "total": len(matched),
    }


async def admin_plugins_console(request: Request, ctx: Any = None) -> Response:
    """GET /admin/plugins

    Serve admin/plugins.html console UI, or return catalog JSON if requested.
    """
    auth_err = await _check_admin_auth_async(request)
    if auth_err is not None:
        return auth_err

    accept = request.headers.get("accept", "").lower()
    format_param = request.query_params.get("format", "").lower()
    if "application/json" in accept or format_param == "json":
        return await admin_plugins_api(request, ctx)

    if await asyncio.to_thread(os.path.isfile, str(_PLUGINS_HTML_PATH)):
        return FileResponse(str(_PLUGINS_HTML_PATH), media_type="text/html")
    return HTMLResponse("Admin Plugins Console not found", status_code=404)


async def admin_plugins_api(request: Request, ctx: Any = None) -> JSONResponse:
    """GET /admin/plugins/api and GET /api/admin/plugins

    Dual-axis + maturity faceted catalog query API.
    """
    auth_err = await _check_admin_auth_async(request)
    if auth_err is not None:
        return auth_err

    redis_client = getattr(ctx, "redis_client", None)
    if redis_client is None and getattr(admin_state, "engine", None):
        redis_client = getattr(admin_state.engine, "redis_client", None)

    disabled_slugs = await get_disabled_engines(redis_client=redis_client)
    catalog = list_plugin_engine_descriptors()

    payload = build_plugins_catalog_payload(
        catalog,
        disabled_slugs,
        type_param=request.query_params.get("type"),
        category_param=request.query_params.get("category"),
        maturity_param=request.query_params.get("maturity"),
        status_param=request.query_params.get("status"),
        q_param=request.query_params.get("q"),
    )
    return JSONResponse(payload)


async def admin_plugins_toggle(request: Request, ctx: Any = None) -> JSONResponse:
    """POST /admin/plugins/api/toggle and POST /api/admin/plugins/toggle

    Dynamically enable or disable an engine runtime state in Redis.
    """
    await request.body()
    auth_err = await _check_admin_auth_async(request)
    if auth_err is not None:
        return auth_err

    try:
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": f"Invalid JSON body: {exc}"}, status_code=400)

    raw_slug = body.get("slug") or body.get("id") or body.get("plugin_id")
    if not raw_slug or not isinstance(raw_slug, str):
        return JSONResponse({"error": "Missing or invalid 'slug'"}, status_code=400)
    slug = raw_slug.strip()
    if not slug:
        return JSONResponse({"error": "Missing or invalid 'slug'"}, status_code=400)

    if "enabled" not in body or not isinstance(body["enabled"], bool):
        return JSONResponse({"error": "Missing or invalid 'enabled' boolean"}, status_code=400)
    enabled = body["enabled"]

    catalog = list_plugin_engine_descriptors()
    target_descriptor = next((d for d in catalog if d["slug"] == slug or d["id"] == slug), None)
    if target_descriptor is None:
        return JSONResponse({"error": f"Unknown engine: {slug}"}, status_code=404)

    if enabled is True and not target_descriptor.get("installed", True):
        install_extra = target_descriptor.get("install_extra") or slug
        install_cmd = (
            f'pip install "{install_extra}"'
            if "[" in install_extra
            else f'pip install "nce-vertical-engines[{install_extra}]"'
        )
        return JSONResponse(
            {
                "error": f"Engine '{slug}' is not installed",
                "slug": slug,
                "installed": False,
                "install_hint": install_cmd,
                "pip_install": install_cmd,
            },
            status_code=409,
        )

    is_prod = bool(cfg.IS_PROD or os.environ.get("NCE_ENV", "").lower() in ("production", "prod"))
    redis_client = getattr(ctx, "redis_client", None)
    if redis_client is None and getattr(admin_state, "engine", None):
        redis_client = getattr(admin_state.engine, "redis_client", None)

    if redis_client is None:
        if is_prod:
            return JSONResponse(
                {"error": "Redis unavailable in production (fail-closed)"},
                status_code=503,
            )
        if enabled:
            _IN_MEMORY_DISABLED_ENGINES.discard(slug)
        else:
            _IN_MEMORY_DISABLED_ENGINES.add(slug)
    else:
        try:
            if enabled:
                await redis_client.hdel(REDIS_DISABLED_ENGINES_KEY, slug)
                _IN_MEMORY_DISABLED_ENGINES.discard(slug)
            else:
                await redis_client.hset(REDIS_DISABLED_ENGINES_KEY, slug, "1")
                _IN_MEMORY_DISABLED_ENGINES.add(slug)
        except Exception as exc:
            if is_prod:
                return JSONResponse(
                    {"error": "Redis unavailable in production (fail-closed)"},
                    status_code=503,
                )
            logger.warning("Redis toggle write failed; falling back to memory: %s", exc)
            if enabled:
                _IN_MEMORY_DISABLED_ENGINES.discard(slug)
            else:
                _IN_MEMORY_DISABLED_ENGINES.add(slug)

    updated = dict(target_descriptor)
    installed = bool(updated.get("installed", True))
    enabled_by_env = bool(updated.get("enabled_by_env", updated.get("enabled", True)))
    runtime_disabled = not enabled
    active = installed and enabled_by_env and enabled
    updated["enabled"] = active
    updated["active"] = active
    updated["runtime_disabled"] = runtime_disabled

    return JSONResponse(
        {
            "status": "ok",
            "slug": slug,
            "enabled": enabled,
            "engine": updated,
        }
    )


ALLOWED_MCP_TOKEN_SCOPES: frozenset[str] = frozenset(
    {"mcp:tools:discover", "mcp:tools:call", "admin"}
)


async def admin_plugins_mint_token(request: Request, ctx: Any = None) -> JSONResponse:
    """POST /admin/plugins/api/mint-token and POST /api/admin/plugins/mint-token

    Mint a scoped bearer token for Streamable HTTP /mcp gateway access and return
    zero-secret desktop/IDE mcp_config.json snippet.
    """
    await request.body()
    auth_err = await _check_admin_auth_async(request)
    if auth_err is not None:
        return auth_err

    body: dict[str, Any] = {}
    try:
        raw_body = await request.json()
        if isinstance(raw_body, dict):
            body = raw_body
    except Exception:
        body = {}

    raw_scopes = body.get("scopes")
    if raw_scopes is None:
        scopes_list = ["mcp:tools:call", "mcp:tools:discover"]
    else:
        if not isinstance(raw_scopes, list) or len(raw_scopes) == 0:
            return JSONResponse(
                {"error": "'scopes' must be a non-empty list of allowed scope strings"},
                status_code=400,
            )
        scopes_list = []
        for scope_item in raw_scopes:
            if not isinstance(scope_item, str) or scope_item not in ALLOWED_MCP_TOKEN_SCOPES:
                return JSONResponse(
                    {
                        "error": (
                            f"Invalid scope {scope_item!r}. "
                            f"Allowed scopes: {sorted(ALLOWED_MCP_TOKEN_SCOPES)}"
                        )
                    },
                    status_code=400,
                )
            if scope_item not in scopes_list:
                scopes_list.append(scope_item)

    raw_ns = body.get("namespace_id")
    if raw_ns is not None and raw_ns != "":
        try:
            namespace_id = str(uuid.UUID(str(raw_ns).strip()))
        except (ValueError, TypeError, AttributeError):
            return JSONResponse(
                {"error": "Invalid 'namespace_id': must be a valid UUID"},
                status_code=400,
            )
    else:
        namespace_id = "00000000-0000-0000-0000-000000000001"

    principal_id = str(body.get("principal_id") or "mcp-operator").strip() or "mcp-operator"

    types_list = [str(t).strip() for t in body.get("types", []) if str(t).strip()]
    cats_list = [str(c).strip() for c in body.get("categories", []) if str(c).strip()]
    explicit_engines = [str(e).strip() for e in body.get("engines", []) if str(e).strip()]

    allowed_engines: list[str] = []
    for t in types_list:
        allowed_engines.append(f"type:{t}")
    for c in cats_list:
        allowed_engines.append(f"cat:{c}")
    for e in explicit_engines:
        if e not in allowed_engines:
            allowed_engines.append(e)

    if not allowed_engines:
        allowed_engines = ["*"]

    raw_mode = str(body.get("tool_exposure_mode", "compact")).strip().lower()
    tool_exposure_mode = raw_mode if raw_mode in ("compact", "full") else "compact"

    if "rate_limit_rpm" in body and body["rate_limit_rpm"] is not None:
        raw_rpm = body["rate_limit_rpm"]
        if isinstance(raw_rpm, bool):
            return JSONResponse(
                {"error": "'rate_limit_rpm' must be an integer between 1 and 10000"},
                status_code=400,
            )
        try:
            rate_limit_rpm = int(raw_rpm)
        except (ValueError, TypeError):
            return JSONResponse(
                {"error": "'rate_limit_rpm' must be an integer between 1 and 10000"},
                status_code=400,
            )
        if not (1 <= rate_limit_rpm <= 10000):
            return JSONResponse(
                {"error": "'rate_limit_rpm' must be between 1 and 10000"},
                status_code=400,
            )
    else:
        rate_limit_rpm = 120

    if "expires_in_days" in body and body["expires_in_days"] is not None:
        raw_exp = body["expires_in_days"]
        if isinstance(raw_exp, bool):
            return JSONResponse(
                {"error": "'expires_in_days' must be an integer between 1 and 365"},
                status_code=400,
            )
        try:
            expires_in_days = int(raw_exp)
        except (ValueError, TypeError):
            return JSONResponse(
                {"error": "'expires_in_days' must be an integer between 1 and 365"},
                status_code=400,
            )
        if not (1 <= expires_in_days <= 365):
            return JSONResponse(
                {"error": "'expires_in_days' must be between 1 and 365"},
                status_code=400,
            )
    else:
        expires_in_days = 30

    now_utc = datetime.now(timezone.utc)
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
        "tool_exposure_mode": tool_exposure_mode,
        "rate_limit_rpm": rate_limit_rpm,
        "expires_at": expires_at.isoformat(),
        "created_at": now_utc.isoformat(),
    }
    MINTED_MCP_TOKENS[token_sha256] = token_meta
    MINTED_MCP_TOKENS.move_to_end(token_sha256)
    while len(MINTED_MCP_TOKENS) > _MAX_MINTED_MCP_TOKENS:
        MINTED_MCP_TOKENS.popitem(last=False)

    pg_pool = (
        getattr(request.app.state, "pg_pool", None)
        if hasattr(request, "app") and hasattr(request.app, "state")
        else None
    )
    if pg_pool is None and getattr(admin_state, "engine", None):
        pg_pool = getattr(admin_state.engine, "pg_pool", None)

    if pg_pool is not None:
        try:
            from nce.db_utils import scoped_pg_session

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
                    json.dumps(token_meta["scopes"]),
                    allowed_engines,
                    tool_exposure_mode,
                    rate_limit_rpm,
                    expires_at,
                    now_utc,
                )
        except Exception as exc:
            MINTED_MCP_TOKENS.pop(token_sha256, None)
            logger.error("Failed to persist mcp_client_token to DB: %s", exc)
            return JSONResponse(
                {"error": "Failed to persist minted token"},
                status_code=500,
            )

    mcp_config = {
        "mcpServers": {
            "nce-http": {
                "url": "https://localhost:8443/mcp",
                "headers": {"Authorization": f"Bearer {raw_token}"},
            },
            "nce-proxy": {
                "command": "python",
                "args": ["-m", "nce.mcp_client_proxy"],
                "env": {
                    "NCE_MCP_HTTP_URL": "https://localhost:8443/mcp",
                    "NCE_MCP_CLIENT_TOKEN": raw_token,
                },
            },
        }
    }

    return JSONResponse(
        {
            "status": "ok",
            "token_id": token_id,
            "token": raw_token,
            "token_sha256": token_sha256,
            "namespace_id": namespace_id,
            "principal_id": principal_id,
            "scopes": scopes_list,
            "allowed_engines": allowed_engines,
            "tool_exposure_mode": tool_exposure_mode,
            "rate_limit_rpm": rate_limit_rpm,
            "expires_at": expires_at.isoformat(),
            "mcp_config_json": json.dumps(mcp_config, indent=2),
        },
        status_code=201,
    )


def _resolve_connection_vault(request: Request, ctx: Any = None) -> Any:
    """Resolve an injected TenantConnectionVault or build one from admin PG pool."""
    vault = getattr(ctx, "connection_vault", None) or getattr(ctx, "vault", None)
    app = getattr(request, "app", None)
    app_state = getattr(app, "state", None) if app is not None else None
    if vault is None:
        vault = getattr(app_state, "connection_vault", None)
    engine = getattr(admin_state, "engine", None)
    if vault is None:
        vault = getattr(engine, "connection_vault", None)
    if vault is not None:
        return vault

    pg_pool = getattr(ctx, "pg_pool", None)
    if pg_pool is None:
        pg_pool = getattr(app_state, "pg_pool", None)
    if pg_pool is None:
        pg_pool = getattr(engine, "pg_pool", None)
    if pg_pool is None:
        return None
    return TenantConnectionVault(pool=pg_pool)


def _parse_uuid_or_default(raw: Any, default_val: str | None = None) -> str:
    if raw is None or str(raw).strip() == "":
        if default_val is None:
            raise ValueError("Missing required UUID")
        return default_val
    return str(uuid.UUID(str(raw).strip()))


def _serialize_connection_metadata(item: Any) -> dict[str, Any]:
    """Serialize AppConnectionMetadata strictly without encrypted_config or secret fields."""
    raw = item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
    raw_meta = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    safe_meta = _SAFE_META_REDACTOR.redact(raw_meta)
    auth_kind = str(raw.get("auth_kind") or "api_key")
    dek_id = str(raw["dek_id"]) if raw.get("dek_id") is not None else None
    return {
        "connection_id": str(raw["connection_id"]),
        "namespace_id": str(raw["namespace_id"]),
        "app_slug": str(raw["app_slug"]),
        "connection_name": str(raw.get("connection_name") or "default"),
        "auth_kind": auth_kind,
        "endpoint_url": raw.get("endpoint_url"),
        "status": str(raw.get("status") or "connected"),
        "scopes": [str(s) for s in (raw.get("scopes") or ())],
        "metadata": safe_meta if isinstance(safe_meta, dict) else {},
        "dek_id": dek_id,
        "kek_version": str(raw["kek_version"]) if raw.get("kek_version") is not None else None,
        "has_credentials": bool(dek_id is not None or auth_kind != "none"),
        "credentials_masked": "••••••••" if auth_kind != "none" else None,
        "last_verified_at": raw.get("last_verified_at"),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
    }


async def admin_plugins_connections(request: Request, ctx: Any = None) -> JSONResponse:
    """GET/POST/DELETE /admin/plugins/api/connections and /api/admin/plugins/connections.

    Manage encrypted tenant connector credentials via TenantConnectionVault without
    ever returning plaintext secrets or encrypted_config envelopes.
    """
    method = str(getattr(request, "method", "GET")).upper()
    if method in ("POST", "DELETE") and callable(getattr(request, "body", None)):
        try:
            await request.body()
        except Exception:
            pass

    auth_err = await _check_admin_auth_async(request)
    if auth_err is not None:
        return auth_err

    vault = _resolve_connection_vault(request, ctx)
    if vault is None:
        return JSONResponse(
            {"error": "Connection vault unavailable: PostgreSQL pool is not initialized"},
            status_code=503,
        )

    if method == "GET":
        try:
            ns_id = _parse_uuid_or_default(
                request.query_params.get("namespace_id"),
                _DEFAULT_NAMESPACE_ID,
            )
        except (ValueError, TypeError, AttributeError):
            return JSONResponse(
                {"error": "Invalid 'namespace_id': must be a valid UUID"},
                status_code=400,
            )

        raw_slug = request.query_params.get("app_slug")
        app_slug = raw_slug.strip() if isinstance(raw_slug, str) and raw_slug.strip() else None

        raw_status = request.query_params.get("status")
        status_val: str | None = None
        if isinstance(raw_status, str) and raw_status.strip():
            status_val = raw_status.strip().lower()
            if status_val not in _VALID_CONNECTION_STATUSES:
                return JSONResponse(
                    {
                        "error": f"Invalid 'status'. Allowed: {sorted(_VALID_CONNECTION_STATUSES)}",
                    },
                    status_code=400,
                )

        try:
            items = await vault.list_connections(ns_id, app_slug=app_slug, status=status_val)
        except Exception as exc:
            logger.error("Failed to list app connections: %s", type(exc).__name__)
            return JSONResponse({"error": "Failed to list app connections"}, status_code=500)

        serialized = [_serialize_connection_metadata(item) for item in items]
        return JSONResponse(
            {
                "status": "ok",
                "namespace_id": ns_id,
                "connections": serialized,
                "total": len(serialized),
            }
        )

    if method == "POST":
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        except Exception as exc:
            return JSONResponse({"error": f"Invalid JSON body: {exc}"}, status_code=400)

        try:
            ns_id = _parse_uuid_or_default(body.get("namespace_id"), _DEFAULT_NAMESPACE_ID)
        except (ValueError, TypeError, AttributeError):
            return JSONResponse(
                {"error": "Invalid 'namespace_id': must be a valid UUID"},
                status_code=400,
            )

        raw_slug = body.get("app_slug")
        if not isinstance(raw_slug, str) or not _APP_SLUG_RE.fullmatch(raw_slug.strip()):
            return JSONResponse(
                {"error": "Missing or invalid 'app_slug' (must match ^[a-z][a-z0-9_]*$)"},
                status_code=400,
            )
        app_slug = raw_slug.strip()

        raw_conn_name = body.get("connection_name")
        if raw_conn_name is not None and not isinstance(raw_conn_name, str):
            return JSONResponse({"error": "Invalid 'connection_name'"}, status_code=400)
        connection_name = (raw_conn_name or "default").strip() or "default"
        if len(connection_name) > 128:
            return JSONResponse(
                {"error": "'connection_name' exceeds 128 characters"},
                status_code=400,
            )

        auth_kind = str(body.get("auth_kind") or "api_key").strip().lower()
        if auth_kind not in _VALID_AUTH_KINDS:
            return JSONResponse(
                {"error": f"Invalid 'auth_kind'. Allowed: {sorted(_VALID_AUTH_KINDS)}"},
                status_code=400,
            )

        status_val = str(body.get("status") or "connected").strip().lower()
        if status_val not in _VALID_CONNECTION_STATUSES:
            return JSONResponse(
                {"error": f"Invalid 'status'. Allowed: {sorted(_VALID_CONNECTION_STATUSES)}"},
                status_code=400,
            )

        raw_endpoint = body.get("endpoint_url")
        if raw_endpoint is not None and not isinstance(raw_endpoint, str):
            return JSONResponse({"error": "Invalid 'endpoint_url'"}, status_code=400)
        endpoint_url = raw_endpoint.strip() if isinstance(raw_endpoint, str) else None
        if endpoint_url == "":
            endpoint_url = None

        raw_scopes = body.get("scopes")
        if raw_scopes is None:
            scopes_list: list[str] = []
        elif isinstance(raw_scopes, list):
            scopes_list = [str(s).strip() for s in raw_scopes if str(s).strip()]
        else:
            return JSONResponse({"error": "'scopes' must be a list of strings"}, status_code=400)

        raw_meta = body.get("metadata")
        if raw_meta is None:
            meta_dict: dict[str, Any] = {}
        elif isinstance(raw_meta, dict):
            meta_dict = dict(raw_meta)
        else:
            return JSONResponse({"error": "'metadata' must be a JSON object"}, status_code=400)

        secret_config = body.get("secret_config")
        if secret_config is None and "credentials" in body:
            secret_config = body.get("credentials")
        if secret_config is None:
            if auth_kind == "none":
                secret_config = {}
            else:
                return JSONResponse(
                    {"error": "Missing 'secret_config' dictionary for this auth_kind"},
                    status_code=400,
                )
        elif not isinstance(secret_config, dict):
            return JSONResponse(
                {"error": "'secret_config' must be a JSON object"},
                status_code=400,
            )
        elif auth_kind != "none" and len(secret_config) == 0:
            return JSONResponse(
                {"error": "'secret_config' must be non-empty for this auth_kind"},
                status_code=400,
            )

        try:
            meta = await vault.upsert_connection(
                ns_id,
                app_slug,
                secret_config,
                connection_name=connection_name,
                auth_kind=auth_kind,
                endpoint_url=endpoint_url,
                scopes=scopes_list,
                metadata=meta_dict,
                status=status_val,
            )
        except BridgeURLValidationError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:
            logger.error("Failed to upsert app connection: %s", type(exc).__name__)
            return JSONResponse({"error": "Failed to save app connection"}, status_code=500)

        return JSONResponse(
            {
                "status": "ok",
                "connection": _serialize_connection_metadata(meta),
            },
            status_code=201,
        )

    if method == "DELETE":
        body_data: dict[str, Any] = {}
        try:
            raw_bytes = getattr(request, "_body", b"")
            if raw_bytes:
                parsed = json.loads(raw_bytes.decode("utf-8"))
                if isinstance(parsed, dict):
                    body_data = parsed
        except Exception:
            body_data = {}

        raw_ns = request.query_params.get("namespace_id") or body_data.get("namespace_id")
        try:
            ns_id = _parse_uuid_or_default(raw_ns, _DEFAULT_NAMESPACE_ID)
        except (ValueError, TypeError, AttributeError):
            return JSONResponse(
                {"error": "Invalid 'namespace_id': must be a valid UUID"},
                status_code=400,
            )

        raw_cid = request.query_params.get("connection_id") or body_data.get("connection_id")
        try:
            conn_id = _parse_uuid_or_default(raw_cid, None)
        except (ValueError, TypeError, AttributeError):
            return JSONResponse(
                {"error": "Missing or invalid 'connection_id': must be a valid UUID"},
                status_code=400,
            )

        try:
            deleted = await vault.delete_connection(ns_id, conn_id)
        except Exception as exc:
            logger.error("Failed to delete app connection: %s", type(exc).__name__)
            return JSONResponse({"error": "Failed to delete app connection"}, status_code=500)

        if not deleted:
            return JSONResponse({"error": "Connection not found"}, status_code=404)

        return JSONResponse(
            {
                "status": "ok",
                "deleted": True,
                "connection_id": conn_id,
                "namespace_id": ns_id,
            }
        )

    return JSONResponse({"error": "Method not allowed"}, status_code=405)


def register_admin_plugin_routes(routes: list[Route] | None = None, ctx: Any = None) -> list[Route]:
    """Register all admin plugin endpoints including token minter and connections vault."""

    def _bind_local(hnd: Any) -> Any:
        async def _endpoint(req: Any) -> Any:
            try:
                return await hnd(req, ctx)
            except TypeError:
                return await hnd(req)

        _endpoint.__name__ = getattr(hnd, "__name__", "_endpoint")
        _endpoint.__doc__ = getattr(hnd, "__doc__", None)
        return _endpoint

    plugin_routes = [
        Route("/admin/plugins", _bind_local(admin_plugins_console), methods=["GET"]),
        Route("/admin/plugins/api", _bind_local(admin_plugins_api), methods=["GET"]),
        Route("/admin/plugins/api/toggle", _bind_local(admin_plugins_toggle), methods=["POST"]),
        Route(
            "/admin/plugins/api/mint-token", _bind_local(admin_plugins_mint_token), methods=["POST"]
        ),
        Route(
            "/admin/plugins/api/connections",
            _bind_local(admin_plugins_connections),
            methods=["GET", "POST", "DELETE"],
        ),
        Route("/api/admin/plugins", _bind_local(admin_plugins_api), methods=["GET"]),
        Route("/api/admin/plugins/toggle", _bind_local(admin_plugins_toggle), methods=["POST"]),
        Route(
            "/api/admin/plugins/mint-token", _bind_local(admin_plugins_mint_token), methods=["POST"]
        ),
        Route(
            "/api/admin/plugins/connections",
            _bind_local(admin_plugins_connections),
            methods=["GET", "POST", "DELETE"],
        ),
    ]
    if routes is not None:
        routes.extend(plugin_routes)
    return plugin_routes
