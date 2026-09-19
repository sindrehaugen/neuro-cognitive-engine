"""
tests/unit/test_c16_principal_mapping.py

Wave A-6 (C16 Principal Mapping):
Unit tests for principal_bindings, current_principal(request), and GET/PUT /api/me/context.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import jwt
import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

from nce.auth import NamespaceContext
from nce.config import cfg
from nce.event_log import EXPECTED_TENANT_RLS_TABLES
from nce.me_app import app
from nce.principal_bindings import (
    PrincipalBinding,
    PrincipalContext,
    clear_test_bindings,
    current_principal,
    register_test_binding,
)


@pytest.fixture(autouse=True)
def _cleanup_test_bindings():
    clear_test_bindings()
    yield
    clear_test_bindings()


def _make_mock_request(
    namespace_id: UUID | None = None,
    principal_id: str | None = None,
    tier: str = "employee",
    headers: dict[str, str] | None = None,
) -> Request:
    """Construct a mock Starlette Request with populated scope and state."""
    ns = namespace_id or uuid4()
    req_headers = dict(headers or {})
    req_headers["x-nce-namespace-id"] = str(ns)
    if principal_id:
        req_headers["x-nce-principal-id"] = principal_id
    req_headers["x-nce-principal-tier"] = tier

    raw_headers = [
        (k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in req_headers.items()
    ]

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/me/context",
        "headers": raw_headers,
        "query_string": b"",
        "app": MagicMock(),
    }
    req = Request(scope)
    req.state.namespace_ctx = NamespaceContext(
        namespace_id=ns,
        agent_id=principal_id or "default",
        principal_kind=tier,
        principal_id=principal_id,
    )
    return req


def test_predicate_p6_migration_and_rls_registration():
    """Predicate P6: principal_bindings table registered in EXPECTED_TENANT_RLS_TABLES and migration exists."""
    assert "principal_bindings" in EXPECTED_TENANT_RLS_TABLES
    assert EXPECTED_TENANT_RLS_TABLES["principal_bindings"] == "namespace_id"
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    mig = root / "nce" / "migrations" / "093_principal_bindings.sql"
    assert mig.exists(), "093_principal_bindings.sql must exist"
    content = mig.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS principal_bindings" in content
    assert "tenant_isolation_policy" in content


@pytest.mark.asyncio
async def test_current_principal_unbound_gate():
    """Gate requirement: an unbound principal receives an empty context with is_bound=False,

    never another person's rows or fallback identities.
    """
    ns_id = uuid4()
    req = _make_mock_request(namespace_id=ns_id, principal_id="unbound-user-999", tier="employee")

    # Awaitable access
    ctx = await current_principal(req)
    assert isinstance(ctx, PrincipalContext)
    assert ctx.principal_id == "unbound-user-999"
    assert ctx.tier == "employee"
    assert ctx.is_bound is False
    assert ctx.employee_id is None
    assert ctx.customer_id is None
    assert ctx.contractor_id is None
    assert ctx.roles == ()
    assert ctx.is_employee is True
    assert ctx.is_customer is False

    # Synchronous attribute access on helper return
    sync_res = current_principal(req)
    assert sync_res.principal_id == "unbound-user-999"
    assert sync_res.employee_id is None
    assert sync_res.is_bound is False


@pytest.mark.asyncio
async def test_current_principal_bound_resolution():
    """A registered binding resolves into a populated PrincipalContext."""
    ns_id = uuid4()
    binding = PrincipalBinding(
        id=uuid4(),
        namespace_id=ns_id,
        principal_id="emp.alice@example.com",
        tier="employee",
        employee_id="EMP-0042",
        customer_id=None,
        contractor_id=None,
        roles=("technician", "lead"),
        metadata={"department": "Engineering"},
    )
    register_test_binding(binding)

    req = _make_mock_request(
        namespace_id=ns_id, principal_id="emp.alice@example.com", tier="employee"
    )

    ctx = await current_principal(req)
    assert ctx.is_bound is True
    assert ctx.principal_id == "emp.alice@example.com"
    assert ctx.employee_id == "EMP-0042"
    assert ctx.customer_id is None
    assert ctx.roles == ("technician", "lead")
    assert ctx.has_role("technician") is True
    assert ctx.has_role("admin") is False
    assert ctx.metadata == {"department": "Engineering"}


@pytest.mark.asyncio
async def test_multi_tenant_isolation():
    """Bindings registered in tenant A are invisible and never leak to tenant B."""
    tenant_a = uuid4()
    tenant_b = uuid4()

    binding_a = PrincipalBinding(
        id=uuid4(),
        namespace_id=tenant_a,
        principal_id="shared-user-id",
        tier="employee",
        employee_id="EMP-A-1",
        roles=("admin",),
    )
    register_test_binding(binding_a)

    # Request from tenant B for the same principal_id
    req_b = _make_mock_request(namespace_id=tenant_b, principal_id="shared-user-id")
    ctx_b = await current_principal(req_b)

    # Must be unbound in tenant B!
    assert ctx_b.is_bound is False
    assert ctx_b.employee_id is None
    assert ctx_b.roles == ()


def test_api_me_context_endpoints(monkeypatch: pytest.MonkeyPatch):
    """Test GET and PUT /api/me/context via TestClient with JWT Bearer authentication."""
    test_secret = "x" * 32
    monkeypatch.setattr(cfg, "NCE_JWT_SECRET", test_secret)
    monkeypatch.setattr(cfg, "NCE_JWT_ALGORITHM", "HS256")

    client = TestClient(app)
    ns_id = uuid4()
    user_id = "user-me-test-1"

    # 1. Unauthenticated request -> 401
    resp = client.get("/api/me/context")
    assert resp.status_code == 401

    # 2. Authenticated request for unbound user
    token = jwt.encode(
        {
            "namespace_id": str(ns_id),
            "agent_id": user_id,
            "principal_id": user_id,
            "principal_kind": "employee",
            "exp": int(time.time()) + 3600,
        },
        test_secret,
        algorithm="HS256",
    )
    headers = {"Authorization": f"Bearer {token}"}

    resp = client.get("/api/me/context", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["principal_id"] == user_id
    assert data["tier"] == "employee"
    assert data["employee_id"] is None
    assert data["roles"] == []

    # 3. PUT /api/me/context to bind identity (BFF login flow)
    put_payload = {
        "employee_id": "EMP-9001",
        "tier": "employee",
        "roles": ["operations", "admin"],
        "metadata": {"title": "Operations Lead"},
    }
    put_resp = client.put("/api/me/context", headers=headers, json=put_payload)
    assert put_resp.status_code == 200
    put_data = put_resp.json()
    assert put_data["employee_id"] == "EMP-9001"
    assert put_data["roles"] == ["operations", "admin"]

    # 4. Subsequent GET /api/me/context returns the newly bound identity
    get_resp = client.get("/api/me/context", headers=headers)
    assert get_resp.status_code == 200
    get_data = get_resp.json()
    assert get_data["principal_id"] == user_id
    assert get_data["employee_id"] == "EMP-9001"
    assert get_data["roles"] == ["operations", "admin"]
