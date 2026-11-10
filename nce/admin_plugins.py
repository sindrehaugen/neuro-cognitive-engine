"""Admin plugin catalog, runtime enable/disable toggles, and dual-axis filtering."""

from __future__ import annotations

import logging
import os
import secrets
from base64 import b64decode
from binascii import Error as BinasciiError
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response

from nce import admin_state
from nce.auth import _PBKDF2_HASH_FORMAT_PREFIX, verify_admin_password, verify_hmac
from nce.config import cfg, live_admin_api_key
from nce.plugins import list_plugin_engine_descriptors
from nce.tool_governance import GovernanceUnavailable

logger = logging.getLogger("nce-admin-plugins")

REDIS_DISABLED_ENGINES_KEY = "nce:engines:disabled"
_IN_MEMORY_DISABLED_ENGINES: set[str] = set()


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
        return None
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
            sig_val = auth.split(" ", 1)[1].strip()
            if ts_raw:
                try:
                    ts = int(ts_raw)
                    body_bytes = getattr(request, "_body", b"")
                    expected_hmac_keys = [k for k in (cfg.NCE_API_KEY, live_admin_api_key()) if k]
                    if any(
                        verify_hmac(k, request.method, request.url.path, ts, body_bytes, sig_val)
                        for k in expected_hmac_keys
                    ):
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


def build_plugins_catalog_payload(
    descriptors: list[dict[str, Any]],
    disabled_slugs: set[str],
    *,
    type_param: str | None = None,
    category_param: str | None = None,
    status_param: str | None = None,
    q_param: str | None = None,
) -> dict[str, Any]:
    """Compute dual-axis filtered engines and cross-faceted badge counts."""
    catalog = [dict(d) for d in descriptors]
    for d in catalog:
        slug = d["slug"]
        installed = bool(d.get("installed", True))
        enabled_by_env = bool(d.get("enabled_by_env", d.get("enabled", True)))
        runtime_disabled = slug in disabled_slugs
        active = installed and enabled_by_env and not runtime_disabled
        d["enabled"] = active
        d["active"] = active
        d["runtime_disabled"] = runtime_disabled

    selected_types = [t.strip().lower() for t in (type_param or "").split(",") if t.strip()]
    selected_categories = [
        c.strip().lower() for c in (category_param or "").split(",") if c.strip()
    ]
    status_filter = (status_param or "all").strip().lower()
    q = (q_param or "").strip().lower()

    matched = [
        d
        for d in catalog
        if _matches_types(d, selected_types)
        and _matches_categories(d, selected_categories)
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
            and _matches_status(d, status_filter)
            and _matches_q(d, q)
            and c_lower in {str(x).lower() for x in d.get("categories", ())}
        )

    base_pkg = "nce-vertical-engines"
    for d in catalog:
        extra = str(d.get("install_extra", ""))
        if "[" in extra:
            candidate = extra.split("[")[0].strip()
            if candidate:
                base_pkg = candidate
                break

    if not selected_types and not selected_categories and not q and status_filter == "all":
        pip_snippet = f'pip install "{base_pkg}[all]"'
    elif selected_types and not selected_categories and not q:
        extras = [f"type-{t}" for t in selected_types]
        pip_snippet = f'pip install "{base_pkg}[{",".join(extras)}]"'
    elif selected_categories and not selected_types and not q:
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
    elif q or status_filter != "all":
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
        },
        "filters": {
            "types": selected_types,
            "categories": selected_categories,
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
    auth_err = _check_admin_auth(request)
    if auth_err is not None:
        return auth_err

    accept = request.headers.get("accept", "").lower()
    format_param = request.query_params.get("format", "").lower()
    if "application/json" in accept or format_param == "json":
        return await admin_plugins_api(request, ctx)

    html_path = Path(__file__).resolve().parent.parent / "admin" / "plugins.html"
    if html_path.is_file():
        return FileResponse(str(html_path), media_type="text/html")
    return HTMLResponse("Admin Plugins Console not found", status_code=404)


async def admin_plugins_api(request: Request, ctx: Any = None) -> JSONResponse:
    """GET /admin/plugins/api and GET /api/admin/plugins

    Dual-axis faceted catalog query API.
    """
    auth_err = _check_admin_auth(request)
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
        status_param=request.query_params.get("status"),
        q_param=request.query_params.get("q"),
    )
    return JSONResponse(payload)


async def admin_plugins_toggle(request: Request, ctx: Any = None) -> JSONResponse:
    """POST /admin/plugins/api/toggle and POST /api/admin/plugins/toggle

    Dynamically enable or disable an engine runtime state in Redis.
    """
    auth_err = _check_admin_auth(request)
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
