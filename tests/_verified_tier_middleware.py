"""tests/_verified_tier_middleware.py

Test-only stand-in for the real verified-auth layer these routes actually
run behind: admin_app.py's HMACAuthMiddleware, whose own
_resolve_namespace_context (nce/auth.py) sets request.state.namespace_ctx
on every authenticated request. Production code must never trust a raw
request header for principal tier -- see resolve_principal_tier
(nce/resource_surface/rest.py) and extract_principal_identity
(nce/principal_bindings/service.py), both hardened to read only that
verified context, never a header.

Bare `Starlette(routes=...)` test apps have no auth middleware at all, so
without this, request.state.namespace_ctx is never populated and every
request resolves to the least-privileged fallback. This middleware reads
the same header name tests already use to select a tier and sets
request.state.namespace_ctx from it -- the one place in this file where
trusting a header is correct, because this IS the boundary standing in
for verification.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.middleware import Middleware

from nce.auth import NamespaceContext

_VALID_TIERS = ("employee", "contractor", "external-customer")


class VerifiedTierTestMiddleware:
    """Pure-ASGI test stand-in setting verified namespace_ctx principal kind."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        raw = headers.get("X-NCE-Principal-Tier") or headers.get("X-NCE-Principal-Kind")
        if raw:
            tier = raw.strip().lower()
            if tier in _VALID_TIERS:
                scope.setdefault("state", {})["namespace_ctx"] = NamespaceContext(
                    principal_kind=tier
                )
        await self.app(scope, receive, send)


VERIFIED_TIER_TEST_MIDDLEWARE = [Middleware(VerifiedTierTestMiddleware)]
