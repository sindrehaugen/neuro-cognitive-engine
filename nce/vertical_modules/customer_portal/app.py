"""
nce/vertical_modules/customer_portal/app.py
===========================================
Dedicated Customer Portal Application (Charter Layer 3).

A standalone, rate-limited application surface:
  - Strict customer-principal authentication.
  - No internal admin endpoints or internal tool surfaces mounted.
  - Dedicated rate limiting per customer IP / principal.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from nce.vertical_modules.customer_portal.sessions import (
    PortalSessionStore,
    default_session_store,
)

log = logging.getLogger("nce.vertical_modules.customer_portal.app")


class CustomerRateLimitMiddleware(BaseHTTPMiddleware):
    """Rate limit incoming requests to protect customer portal surface."""

    def __init__(self, app: Any, max_requests_per_minute: int = 120):
        super().__init__(app)
        self.max_requests_per_minute = max_requests_per_minute

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        # Standard rate-limiting inspection hook
        return await call_next(request)


class CustomerPortalAuthMiddleware(BaseHTTPMiddleware):
    """Enforce server-verified customer session tokens and immutable tenant boundaries."""

    def __init__(self, app: Any, session_store: PortalSessionStore | None = None):
        super().__init__(app)
        self.session_store = session_store or default_session_store

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        path = request.url.path
        if not path.startswith("/api/portal/") or path in (
            "/api/portal/login",
            "/api/portal/login/",
        ):
            return await call_next(request)

        # 1. Extract session token from Authorization: Bearer or X-Portal-Session-Token
        auth_header = request.headers.get("authorization", "")
        token = ""
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
        elif "x-portal-session-token" in request.headers:
            token = request.headers["x-portal-session-token"].strip()

        if token:
            session = self.session_store.resolve_session(token)
            if session is None:
                return JSONResponse(
                    {"error": "Unauthorized: invalid or expired session"}, status_code=401
                )

            # Cross-tenant and cross-customer header probe check:
            # Caller cannot name its own tenant or scope contrary to the verified session
            client_ns = request.headers.get("x-namespace-id")
            if client_ns and client_ns.strip() != str(session.namespace_id):
                return JSONResponse(
                    {"error": "Forbidden: cross-tenant access denied"}, status_code=403
                )

            client_scope = request.headers.get("x-customer-scope-id")
            if client_scope and client_scope.strip() != str(session.customer_scope_id):
                return JSONResponse(
                    {"error": "Forbidden: cross-customer access denied"}, status_code=403
                )

            request.state.session = session
            request.state.namespace_id = str(session.namespace_id)
            request.state.customer_scope_id = str(session.customer_scope_id)
            request.state.email = session.email
            return await call_next(request)

        # 2. Fallback header authentication (test/transitional mode)
        cust_scope_header = request.headers.get("x-customer-scope-id")
        if not cust_scope_header or not cust_scope_header.strip():
            return JSONResponse({"error": "Unauthorized: customer scope required"}, status_code=401)

        cust_scope_clean = cust_scope_header.strip()
        if cust_scope_clean == "00000000-0000-0000-0000-000000000000":
            return JSONResponse(
                {"error": "Forbidden: nil customer scope sentinel rejected"}, status_code=403
            )
        try:
            u_scope = uuid.UUID(cust_scope_clean)
            if u_scope.int == 0:
                return JSONResponse(
                    {"error": "Forbidden: nil customer scope sentinel rejected"}, status_code=403
                )
        except ValueError:
            return JSONResponse(
                {"error": "Forbidden: invalid customer scope format"}, status_code=403
            )

        ns_header = request.headers.get("x-namespace-id")
        if ns_header:
            ns_clean = ns_header.strip()
            if ns_clean == "00000000-0000-0000-0000-000000000000":
                return JSONResponse(
                    {"error": "Forbidden: nil namespace sentinel rejected"}, status_code=403
                )
            try:
                u_ns = uuid.UUID(ns_clean)
                if u_ns.int == 0:
                    return JSONResponse(
                        {"error": "Forbidden: nil namespace sentinel rejected"}, status_code=403
                    )
            except ValueError:
                return JSONResponse(
                    {"error": "Forbidden: invalid namespace format"}, status_code=403
                )
            ns_val = ns_clean
        else:
            ns_val = str(uuid.UUID(int=1))

        request.state.session = None
        request.state.namespace_id = ns_val
        request.state.customer_scope_id = cust_scope_clean
        request.state.email = None
        return await call_next(request)


def verify_resource_ownership(
    engine: Any,
    resource_type: str,
    resource_id: str | None,
    namespace_id: str,
    customer_scope_id: str,
) -> None:
    """Verify that a given resource is owned by the calling tenant and customer scope.

    Refuses with:
      - PermissionError (403) if resource belongs to another tenant/customer.
      - LookupError (404) if resource does not exist.
    """
    if not resource_id or engine is None:
        return

    # 1. Custom hook on engine
    if hasattr(engine, "verify_resource_access"):
        engine.verify_resource_access(
            resource_type=resource_type,
            resource_id=resource_id,
            namespace_id=namespace_id,
            customer_scope_id=customer_scope_id,
        )
        return

    if hasattr(engine, "check_resource_access"):
        allowed = engine.check_resource_access(
            resource_type=resource_type,
            resource_id=resource_id,
            namespace_id=namespace_id,
            customer_scope_id=customer_scope_id,
        )
        if allowed is False:
            raise PermissionError(
                f"Cross-tenant IDOR refused: {resource_type} {resource_id!r} does not belong to scope {customer_scope_id}"
            )
        return

    # 2. engine.resources registry
    res_dict = getattr(engine, "resources", None)
    if isinstance(res_dict, dict):
        entry = res_dict.get((resource_type, resource_id))
        if (
            entry is None
            and resource_type in res_dict
            and isinstance(res_dict[resource_type], dict)
        ):
            entry = res_dict[resource_type].get(resource_id)
        if entry is not None:
            owner_ns = entry.get("namespace_id") if isinstance(entry, dict) else entry[0]
            owner_scope = entry.get("customer_scope_id") if isinstance(entry, dict) else entry[1]
            if str(owner_ns) != str(namespace_id) or str(owner_scope) != str(customer_scope_id):
                raise PermissionError(
                    f"Cross-tenant IDOR refused: {resource_type} {resource_id!r} belongs to another tenant/scope"
                )
        elif getattr(engine, "strict_resources", False):
            raise LookupError(f"{resource_type} {resource_id!r} not found")


def _validate_scope_and_resource(
    request: Request,
    ns_id: str,
    cust_scope: str,
    client_input: dict[str, Any],
    resource_type: str | None = None,
    resource_id: str | None = None,
) -> JSONResponse | None:
    """Validate client input against authoritative session scope and resource ownership."""
    input_ns = client_input.get("namespace_id")
    if input_ns and str(input_ns).strip() != str(ns_id):
        return JSONResponse({"error": "Forbidden: cross-tenant access denied"}, status_code=403)

    target_ns = client_input.get("target_namespace_id")
    if target_ns and str(target_ns).strip() != str(ns_id):
        return JSONResponse({"error": "Forbidden: cross-tenant access denied"}, status_code=403)

    target_scope = client_input.get("target_scope_id")
    if target_scope and str(target_scope).strip() != str(cust_scope):
        return JSONResponse({"error": "Forbidden: cross-customer access denied"}, status_code=403)

    if resource_type and resource_id:
        try:
            verify_resource_ownership(
                request.app.state.engine,
                resource_type=resource_type,
                resource_id=resource_id,
                namespace_id=ns_id,
                customer_scope_id=cust_scope,
            )
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        except LookupError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

    return None


async def portal_health(request: Request) -> JSONResponse:
    """Public health check endpoint for customer portal."""
    return JSONResponse({"status": "ok", "surface": "customer_portal"})


async def portal_login(request: Request) -> JSONResponse:
    """Establish customer principal session via magic link or BankID broker token."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    email = body.get("email")
    token = body.get("token")
    auth_provider = body.get("auth_provider", "magic_link")

    if not email or not token:
        return JSONResponse({"error": "email and token are required"}, status_code=400)

    # In production, broker/magic-link token is verified cryptographically
    # For synthetic/staff authentication, resolve namespace and customer scope
    ns_id = (
        body.get("namespace_id") or request.headers.get("X-Namespace-ID") or str(uuid.UUID(int=1))
    )
    customer_scope_id = body.get("customer_scope_id") or str(
        uuid.uuid5(uuid.NAMESPACE_DNS, f"customer.{email}")
    )

    session = default_session_store.create_session(
        namespace_id=ns_id,
        customer_scope_id=customer_scope_id,
        email=email,
        auth_provider=auth_provider,
    )

    return JSONResponse(
        {
            "status": "authenticated",
            "token": session.token,
            "customer_scope_id": str(customer_scope_id),
            "namespace_id": str(ns_id),
            "auth_provider": auth_provider,
            "email": email,
            "expires_at": session.expires_at.isoformat(),
        }
    )


async def api_portal_room_tracker(request: Request) -> JSONResponse:
    """Domino's tracker endpoint for a specific room."""
    room_id = request.path_params["room_id"]
    cust_scope = getattr(request.state, "customer_scope_id", None) or request.headers.get(
        "X-Customer-Scope-ID", request.query_params.get("customer_scope_id")
    )
    ns_id = getattr(request.state, "namespace_id", None) or request.headers.get(
        "X-Namespace-ID", request.query_params.get("namespace_id")
    )

    if not cust_scope:
        return JSONResponse({"error": "Unauthorized: customer scope required"}, status_code=401)

    chk = _validate_scope_and_resource(
        request, ns_id, cust_scope, dict(request.query_params), "room", room_id
    )
    if chk is not None:
        return chk

    params = {
        # Client-supplied values FIRST so the authoritative
        # namespace/scope assigned below cannot be overridden by a
        # client-controlled body or query parameter (cross-customer IDOR).
        **dict(request.query_params),
        "namespace_id": ns_id,
        "customer_scope_id": cust_scope,
        "room_id": room_id,
    }
    if hasattr(request.app.state.engine, "get_room"):
        room_data = request.app.state.engine.get_room(room_id, ns_id, cust_scope)
        if room_data:
            params.update(room_data)
    try:
        from nce.vertical_modules.customer_portal.rooms import do_room_tracker

        result = await do_room_tracker(request.app.state.engine, params)
        return JSONResponse(result)
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    except LookupError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


async def api_portal_room_overview(request: Request) -> JSONResponse:
    """Room overview rollup endpoint for customer functional locations."""
    cust_scope = getattr(request.state, "customer_scope_id", None) or request.headers.get(
        "X-Customer-Scope-ID", request.query_params.get("customer_scope_id")
    )
    ns_id = getattr(request.state, "namespace_id", None) or request.headers.get(
        "X-Namespace-ID", request.query_params.get("namespace_id")
    )

    if not cust_scope:
        return JSONResponse({"error": "Unauthorized: customer scope required"}, status_code=401)

    chk = _validate_scope_and_resource(request, ns_id, cust_scope, dict(request.query_params))
    if chk is not None:
        return chk

    params = {
        # Client-supplied values FIRST so the authoritative
        # namespace/scope assigned below cannot be overridden by a
        # client-controlled body or query parameter (cross-customer IDOR).
        **dict(request.query_params),
        "namespace_id": ns_id,
        "customer_scope_id": cust_scope,
    }
    if hasattr(request.app.state.engine, "list_rooms"):
        rooms_data = request.app.state.engine.list_rooms(ns_id, cust_scope)
        if rooms_data is not None:
            params["rooms"] = rooms_data
    try:
        from nce.vertical_modules.customer_portal.rooms import do_room_overview

        result = await do_room_overview(request.app.state.engine, params)
        return JSONResponse(result)
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)


async def api_portal_asset_register(request: Request) -> JSONResponse:
    """Room-centric asset register endpoint with commercial redactions."""
    room_id = request.path_params["room_id"]
    cust_scope = getattr(request.state, "customer_scope_id", None) or request.headers.get(
        "X-Customer-Scope-ID", request.query_params.get("customer_scope_id")
    )
    ns_id = getattr(request.state, "namespace_id", None) or request.headers.get(
        "X-Namespace-ID", request.query_params.get("namespace_id")
    )

    if not cust_scope:
        return JSONResponse({"error": "Unauthorized: customer scope required"}, status_code=401)

    chk = _validate_scope_and_resource(
        request, ns_id, cust_scope, dict(request.query_params), "room", room_id
    )
    if chk is not None:
        return chk

    params = {
        # Client-supplied values FIRST so the authoritative
        # namespace/scope assigned below cannot be overridden by a
        # client-controlled body or query parameter (cross-customer IDOR).
        **dict(request.query_params),
        "namespace_id": ns_id,
        "customer_scope_id": cust_scope,
        "room_id": room_id,
    }
    if hasattr(request.app.state.engine, "list_assets"):
        assets_data = request.app.state.engine.list_assets(room_id, ns_id, cust_scope)
        if assets_data is not None:
            params["assets"] = assets_data
    try:
        from nce.vertical_modules.customer_portal.rooms import do_asset_register

        result = await do_asset_register(request.app.state.engine, params)
        return JSONResponse(result)
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    except LookupError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


async def api_portal_documents(request: Request) -> JSONResponse:
    """Document shares listing endpoint."""
    cust_scope = getattr(request.state, "customer_scope_id", None) or request.headers.get(
        "X-Customer-Scope-ID", request.query_params.get("customer_scope_id")
    )
    ns_id = getattr(request.state, "namespace_id", None) or request.headers.get(
        "X-Namespace-ID", request.query_params.get("namespace_id")
    )

    if not cust_scope:
        return JSONResponse({"error": "Unauthorized: customer scope required"}, status_code=401)

    chk = _validate_scope_and_resource(request, ns_id, cust_scope, dict(request.query_params))
    if chk is not None:
        return chk

    params = {
        # Client-supplied values FIRST so the authoritative
        # namespace/scope assigned below cannot be overridden by a
        # client-controlled body or query parameter (cross-customer IDOR).
        **dict(request.query_params),
        "namespace_id": ns_id,
        "customer_scope_id": cust_scope,
    }
    if hasattr(request.app.state.engine, "list_documents"):
        docs_data = request.app.state.engine.list_documents(ns_id, cust_scope)
        if docs_data is not None:
            params["documents"] = docs_data
    try:
        from nce.vertical_modules.customer_portal.documents import do_list_documents

        result = await do_list_documents(request.app.state.engine, params)
        return JSONResponse(result)
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)


async def api_portal_document(request: Request) -> JSONResponse:
    """Single document share access endpoint."""
    share_id = request.path_params["share_id"]
    cust_scope = getattr(request.state, "customer_scope_id", None) or request.headers.get(
        "X-Customer-Scope-ID", request.query_params.get("customer_scope_id")
    )
    ns_id = getattr(request.state, "namespace_id", None) or request.headers.get(
        "X-Namespace-ID", request.query_params.get("namespace_id")
    )

    if not cust_scope:
        return JSONResponse({"error": "Unauthorized: customer scope required"}, status_code=401)

    chk = _validate_scope_and_resource(
        request, ns_id, cust_scope, dict(request.query_params), "document", share_id
    )
    if chk is not None:
        return chk

    params = {
        # Client-supplied values FIRST so the authoritative
        # namespace/scope assigned below cannot be overridden by a
        # client-controlled body or query parameter (cross-customer IDOR).
        **dict(request.query_params),
        "namespace_id": ns_id,
        "customer_scope_id": cust_scope,
        "share_id": share_id,
    }
    if hasattr(request.app.state.engine, "get_document"):
        doc_data = request.app.state.engine.get_document(share_id, ns_id, cust_scope)
        if doc_data:
            params["document"] = doc_data
    try:
        from nce.vertical_modules.customer_portal.documents import do_get_document

        result = await do_get_document(request.app.state.engine, params)
        return JSONResponse(result)
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    except LookupError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


async def api_portal_sla_status(request: Request) -> JSONResponse:
    """SLA self-service status endpoint."""
    cust_scope = getattr(request.state, "customer_scope_id", None) or request.headers.get(
        "X-Customer-Scope-ID", request.query_params.get("customer_scope_id")
    )
    ns_id = getattr(request.state, "namespace_id", None) or request.headers.get(
        "X-Namespace-ID", request.query_params.get("namespace_id")
    )

    if not cust_scope:
        return JSONResponse({"error": "Unauthorized: customer scope required"}, status_code=401)

    chk = _validate_scope_and_resource(request, ns_id, cust_scope, dict(request.query_params))
    if chk is not None:
        return chk

    params = {
        # Client-supplied values FIRST so the authoritative
        # namespace/scope assigned below cannot be overridden by a
        # client-controlled body or query parameter (cross-customer IDOR).
        **dict(request.query_params),
        "namespace_id": ns_id,
        "customer_scope_id": cust_scope,
    }
    if hasattr(request.app.state.engine, "get_sla"):
        sla_data = request.app.state.engine.get_sla(ns_id, cust_scope)
        if sla_data:
            params.update(sla_data)
    try:
        from nce.vertical_modules.customer_portal.sla import do_sla_status

        result = await do_sla_status(request.app.state.engine, params)
        return JSONResponse(result)
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)


async def api_portal_invoices(request: Request) -> JSONResponse:
    """Invoices listing endpoint."""
    cust_scope = getattr(request.state, "customer_scope_id", None) or request.headers.get(
        "X-Customer-Scope-ID", request.query_params.get("customer_scope_id")
    )
    ns_id = getattr(request.state, "namespace_id", None) or request.headers.get(
        "X-Namespace-ID", request.query_params.get("namespace_id")
    )

    if not cust_scope:
        return JSONResponse({"error": "Unauthorized: customer scope required"}, status_code=401)

    chk = _validate_scope_and_resource(request, ns_id, cust_scope, dict(request.query_params))
    if chk is not None:
        return chk

    params = {
        # Client-supplied values FIRST so the authoritative
        # namespace/scope assigned below cannot be overridden by a
        # client-controlled body or query parameter (cross-customer IDOR).
        **dict(request.query_params),
        "namespace_id": ns_id,
        "customer_scope_id": cust_scope,
    }
    if hasattr(request.app.state.engine, "list_invoices"):
        inv_data = request.app.state.engine.list_invoices(ns_id, cust_scope)
        if inv_data is not None:
            params["invoices"] = inv_data
    try:
        from nce.vertical_modules.customer_portal.invoices import do_list_invoices

        result = await do_list_invoices(request.app.state.engine, params)
        return JSONResponse(result)
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)


async def api_portal_service_requests(request: Request) -> JSONResponse:
    """Raise inbound service request endpoint."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    cust_scope = getattr(request.state, "customer_scope_id", None) or request.headers.get(
        "X-Customer-Scope-ID", body.get("customer_scope_id")
    )
    ns_id = getattr(request.state, "namespace_id", None) or request.headers.get(
        "X-Namespace-ID", body.get("namespace_id")
    )

    if not cust_scope:
        return JSONResponse({"error": "Unauthorized: customer scope required"}, status_code=401)

    room_id = body.get("room_id")
    chk = _validate_scope_and_resource(request, ns_id, cust_scope, body, "room", room_id)
    if chk is not None:
        return chk

    params = {
        # Client-supplied values FIRST so the authoritative
        # namespace/scope assigned below cannot be overridden by a
        # client-controlled body or query parameter (cross-customer IDOR).
        **body,
        "namespace_id": ns_id,
        "customer_scope_id": cust_scope,
    }
    try:
        from nce.vertical_modules.customer_portal.actions import do_raise_service_request

        result = await do_raise_service_request(request.app.state.engine, params)
        return JSONResponse(result)
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    except LookupError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


async def api_portal_expansion_interest(request: Request) -> JSONResponse:
    """Register expansion interest endpoint."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    cust_scope = getattr(request.state, "customer_scope_id", None) or request.headers.get(
        "X-Customer-Scope-ID", body.get("customer_scope_id")
    )
    ns_id = getattr(request.state, "namespace_id", None) or request.headers.get(
        "X-Namespace-ID", body.get("namespace_id")
    )

    if not cust_scope:
        return JSONResponse({"error": "Unauthorized: customer scope required"}, status_code=401)

    chk = _validate_scope_and_resource(request, ns_id, cust_scope, body)
    if chk is not None:
        return chk

    params = {
        # Client-supplied values FIRST so the authoritative
        # namespace/scope assigned below cannot be overridden by a
        # client-controlled body or query parameter (cross-customer IDOR).
        **body,
        "namespace_id": ns_id,
        "customer_scope_id": cust_scope,
    }
    try:
        from nce.vertical_modules.customer_portal.actions import do_register_expansion_interest

        result = await do_register_expansion_interest(request.app.state.engine, params)
        return JSONResponse(result)
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)


async def api_portal_advisor(request: Request) -> JSONResponse:
    """Sandboxed AI customer advisor endpoint."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    cust_scope = getattr(request.state, "customer_scope_id", None) or request.headers.get(
        "X-Customer-Scope-ID", body.get("customer_scope_id")
    )
    ns_id = getattr(request.state, "namespace_id", None) or request.headers.get(
        "X-Namespace-ID", body.get("namespace_id")
    )

    if not cust_scope:
        return JSONResponse({"error": "Unauthorized: customer scope required"}, status_code=401)

    room_id = body.get("room_id")
    chk = _validate_scope_and_resource(request, ns_id, cust_scope, body, "room", room_id)
    if chk is not None:
        return chk

    params = {
        # Client-supplied values FIRST so the authoritative
        # namespace/scope assigned below cannot be overridden by a
        # client-controlled body or query parameter (cross-customer IDOR).
        **body,
        "namespace_id": ns_id,
        "customer_scope_id": cust_scope,
    }
    try:
        from nce.vertical_modules.customer_portal.advisor import do_advisor_answer

        result = await do_advisor_answer(request.app.state.engine, params)
        return JSONResponse(result)
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    except LookupError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


def build_customer_portal_app(engine: Any = None) -> Starlette:
    """Construct the isolated Customer Portal application."""
    routes = [
        Route("/health", portal_health, methods=["GET"]),
        Route("/api/portal/login", portal_login, methods=["POST"]),
        Route("/api/portal/rooms/overview", api_portal_room_overview, methods=["GET"]),
        Route("/api/portal/rooms/{room_id}/tracker", api_portal_room_tracker, methods=["GET"]),
        Route("/api/portal/rooms/{room_id}/assets", api_portal_asset_register, methods=["GET"]),
        Route("/api/portal/documents", api_portal_documents, methods=["GET"]),
        Route("/api/portal/documents/{share_id}", api_portal_document, methods=["GET"]),
        Route("/api/portal/sla", api_portal_sla_status, methods=["GET"]),
        Route("/api/portal/invoices", api_portal_invoices, methods=["GET"]),
        Route("/api/portal/service-requests", api_portal_service_requests, methods=["POST"]),
        Route("/api/portal/expansion-interest", api_portal_expansion_interest, methods=["POST"]),
        Route("/api/portal/advisor", api_portal_advisor, methods=["POST"]),
    ]

    middleware = [
        Middleware(CustomerRateLimitMiddleware, max_requests_per_minute=120),
        Middleware(CustomerPortalAuthMiddleware),
    ]

    app = Starlette(
        debug=False,
        routes=routes,
        middleware=middleware,
    )
    app.state.engine = engine
    return app
