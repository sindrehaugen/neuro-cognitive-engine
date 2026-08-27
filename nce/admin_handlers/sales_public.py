"""
nce/admin_handlers/sales_public.py
==================================
Public customer-facing quote handlers (Batch 088).
Bypasses basic/HMAC/mTLS auth, protected by stateless tokens + rate limiting.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from uuid import UUID

from starlette.requests import Request
from starlette.responses import JSONResponse

from nce import admin_state
from nce.auth import _check_admin_http_rate_limit
from nce.config import cfg
from nce.redaction.redactor import project
from nce.vertical_modules.sales.source_mode import do_quote_detail

log = logging.getLogger("nce.admin_handlers.sales_public")


def generate_public_token(quote_id: str) -> str:
    """Generate a secure stateless token for a quote_id using NCE_MASTER_KEY.

    The key is ``.strip()``ed to match :meth:`nce.signing.MasterKey.from_env`,
    which is how every other consumer of this secret normalises it.
    ``cfg.NCE_MASTER_KEY`` holds the raw ``secret_env`` result, and ``secret_env``
    deliberately preserves surrounding whitespace (it removes only one trailing
    newline) — so HMACing the raw value made token validity depend on INVISIBLE
    padding: tidy the whitespace out of the configured secret and every
    previously issued token silently starts returning 401. It also meant one
    configured secret yielded two different key values across subsystems, which
    no decrypt/auth-tag probe can detect because this derivation is not AEAD.

    For a key with no surrounding whitespace — the normal case — this is a no-op,
    so healthy deployments keep every token they have already issued.
    """
    key = cfg.NCE_MASTER_KEY.strip().encode("utf-8")
    msg = quote_id.encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


async def api_sales_quote_public(request: Request) -> JSONResponse:
    """GET /public-api/sales/quotes/{id}

    Public endpoint to retrieve a C8-redacted quote payload.
    Requires token validation and enforces sliding window rate limiting.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    quote_id = request.path_params.get("id")
    if not quote_id:
        return JSONResponse({"error": "Missing quote ID"}, status_code=400)

    namespace_id = request.query_params.get("namespace_id")
    if not namespace_id:
        return JSONResponse({"error": "Missing namespace_id"}, status_code=400)

    try:
        ns_uuid = UUID(str(namespace_id))
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=400)

    # 1. Extract and validate token
    token = (
        request.query_params.get("token")
        or request.headers.get("authorization", "").replace("Bearer ", "").strip()
    )
    if not token:
        return JSONResponse({"error": "Unauthorized: missing token"}, status_code=401)

    expected_token = generate_public_token(quote_id)
    if not hmac.compare_digest(token, expected_token):
        return JSONResponse({"error": "Unauthorized: invalid token"}, status_code=401)

    # 2. Enforce sliding window rate limiting (5 requests per 10 seconds)
    limit = 5
    period = 10
    key = f"nce:ratelimit:public_quote:{token}"
    redis_client = getattr(request.app.state, "redis_client", None)

    allowed = await _check_admin_http_rate_limit(redis_client, key, limit, period)
    if not allowed:
        return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)

    # 3. Fetch quote details
    try:
        quote_detail = await do_quote_detail(
            admin_state.engine,
            {"namespace_id": ns_uuid, "quoteid": quote_id},
        )
        if "error" in quote_detail:
            if quote_detail["error"] == "unknown_quote":
                return JSONResponse({"error": "Quote not found"}, status_code=404)
            return JSONResponse(quote_detail, status_code=400)
    except Exception as exc:
        log.exception("Failed to fetch quote %s", quote_id)
        return JSONResponse({"error": f"Internal server error: {exc}"}, status_code=500)

    # 4. Project via C8 Redactor
    try:
        redacted_quote = project(quote_detail, "public-quote")
    except Exception as exc:
        log.exception("Redaction failed for quote %s", quote_id)
        return JSONResponse({"error": f"Redaction failed: {exc}"}, status_code=500)

    # Invariant safety: assert cost, margin, commission, internal-status never leak
    for forbidden_key in ["cost", "margin", "commission", "internal-status"]:
        if forbidden_key in redacted_quote:
            log.error(
                "Security violation: forbidden key %r present in redacted quote!", forbidden_key
            )
            del redacted_quote[forbidden_key]

    return JSONResponse(redacted_quote)
