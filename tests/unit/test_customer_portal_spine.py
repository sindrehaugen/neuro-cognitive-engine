"""Unit tests for Module 17 Customer Portal Engine: Security Spine and Principal Isolation.

Charter Phase 1 Gates:
  1. Registration in EXPECTED_TENANT_RLS_TABLES (82 -> 85).
  2. Migration and schema.sql definitions (ENABLE + FORCE RLS, external_isolation_policy).
  3. L1 Customer-scope RLS & IDOR: deny-when-unset proven, cross-tenant IDOR refused.
  4. L2 Explicit field allow-list: margin/cost/internal status stripped, fails closed.
  5. L3 Separate rate-limited app: customer-principal session, no internal tool surface.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from starlette.testclient import TestClient

from nce.event_log import EXPECTED_TENANT_RLS_TABLES

REPO_ROOT = Path(__file__).resolve().parents[2]

_NAMESPACE_ID = UUID("00000000-0000-4000-8000-000000000001")
_CUSTOMER_A_SCOPE = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_CUSTOMER_B_SCOPE = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_ROOM_ID = "11111111-1111-4111-8111-111111111111"


def test_customer_portal_tables_registered_in_expected_tenant_rls_tables():
    """All 3 Customer Portal tables must be registered in EXPECTED_TENANT_RLS_TABLES."""
    expected = {
        "portal_users": "namespace_id",
        "portal_document_shares": "namespace_id",
        "portal_service_requests": "namespace_id",
    }
    for table, col in expected.items():
        assert table in EXPECTED_TENANT_RLS_TABLES, (
            f"Table {table!r} missing from EXPECTED_TENANT_RLS_TABLES"
        )
        assert EXPECTED_TENANT_RLS_TABLES[table] == col, (
            f"Table {table!r} has namespace column {EXPECTED_TENANT_RLS_TABLES[table]!r}, expected {col!r}"
        )


def test_expected_tenant_rls_tables_total_count():
    """Total count of tenant RLS tables after Customer Portal additions must be 85."""
    assert len(EXPECTED_TENANT_RLS_TABLES) == 85, (
        f"Expected 85 tenant RLS tables, got {len(EXPECTED_TENANT_RLS_TABLES)}"
    )


def test_migration_and_schema_sql_contain_portal_tables():
    """Both migration 073 and schema.sql must define portal tables with RLS and external policy."""
    migration_file = REPO_ROOT / "nce" / "migrations" / "073_customer_portal_engine.sql"
    schema_file = REPO_ROOT / "nce" / "schema.sql"

    assert migration_file.exists(), f"Migration file missing: {migration_file}"

    mig_text = migration_file.read_text(encoding="utf-8")
    schema_text = schema_file.read_text(encoding="utf-8")

    for table in ("portal_users", "portal_document_shares", "portal_service_requests"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in mig_text
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in mig_text
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in mig_text
        assert f"CREATE TABLE IF NOT EXISTS {table}" in schema_text
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in schema_text
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in schema_text

    for text in (mig_text, schema_text):
        assert "get_nce_external_scope()" in text
        assert "customer_isolation_policy" in text or "external_isolation_policy" in text


def test_allowlist_redaction_harness():
    """Layer 2: Allow-list projection must strip sensitive internal fields and fail closed."""
    from nce.vertical_modules.customer_portal.redaction import (
        load_customer_redaction_rules,
        project_customer_safe,
    )

    rules = load_customer_redaction_rules()
    assert "room_tracker" in rules
    assert "asset_register" in rules
    assert "service_request" in rules

    raw_room_data = {
        "room_id": "room-101",
        "room_name": "Boardroom Alpha",
        "stage": "Installed",
        "percent_ready": 80,
        "margin": 0.35,
        "cost": 120000.0,
        "our_cost": 78000.0,
        "supplier_terms": "Net 30 with 5% rebate",
        "internal_status": "Delayed by distributor backorder",
        "internal_slip": "3 weeks behind schedule",
        "churn_risk": 0.85,
        "health_score": 42,
        "unregistered_secret_field": "top_secret",
    }

    projected = project_customer_safe("room_tracker", raw_room_data)

    assert projected["room_id"] == "room-101"
    assert projected["room_name"] == "Boardroom Alpha"
    assert projected["stage"] == "Installed"
    assert projected["percent_ready"] == 80

    forbidden_keys = {
        "margin",
        "cost",
        "our_cost",
        "supplier_terms",
        "internal_status",
        "internal_slip",
        "churn_risk",
        "health_score",
        "unregistered_secret_field",
    }
    for key in forbidden_keys:
        assert key not in projected, f"Sensitive key {key!r} leaked in customer projection!"


def test_customer_isolation_policy_logic_and_idor_refusal():
    """Layer 1: Policy evaluation must strictly deny when unset and refuse IDOR across scopes."""
    from nce.vertical_modules.customer_portal.auth import evaluate_customer_scope_access

    nil_uuid = UUID("00000000-0000-0000-0000-000000000000")
    assert not evaluate_customer_scope_access(
        session_scope_id=None,
        record_scope_id=_CUSTOMER_A_SCOPE,
    )
    assert not evaluate_customer_scope_access(
        session_scope_id=nil_uuid,
        record_scope_id=_CUSTOMER_A_SCOPE,
    )

    assert evaluate_customer_scope_access(
        session_scope_id=_CUSTOMER_A_SCOPE,
        record_scope_id=_CUSTOMER_A_SCOPE,
    )

    assert not evaluate_customer_scope_access(
        session_scope_id=_CUSTOMER_A_SCOPE,
        record_scope_id=_CUSTOMER_B_SCOPE,
    )


def test_separate_customer_portal_app_surface():
    """Layer 3: Separate customer portal app has dedicated rate limiting and NO internal tools."""
    from nce.vertical_modules.customer_portal.app import build_customer_portal_app

    app = build_customer_portal_app()
    client = TestClient(app)

    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["surface"] == "customer_portal"

    forbidden_routes = [
        "/api/admin/system",
        "/api/procurement/spend",
        "/api/economy/margin",
        "/api/sales/targets",
    ]
    for route in forbidden_routes:
        r = client.get(route)
        assert r.status_code == 404, f"Privileged route {route!r} exposed on customer portal app!"

    login_resp = client.post(
        "/api/portal/login",
        json={
            "email": "alex@example.test",
            "auth_provider": "magic_link",
            "token": "test_magic_token_alpha",
        },
    )
    assert login_resp.status_code == 200
    data = login_resp.json()
    assert "token" in data
    assert data["customer_scope_id"] is not None


# ---------------------------------------------------------------------------
# L1 hardening — a client must NOT be able to set its own customer scope.
#
# Every route built `params` with the client-controlled mapping spread LAST:
#
#     params = {"namespace_id": ns, "customer_scope_id": cust_scope, **body}
#
# so `?customer_scope_id=<other>` (GET) or `{"customer_scope_id": "<other>"}`
# (POST) OVERWROTE the authoritative value taken from the X-Customer-Scope-ID
# header. That value flows to rooms.py -> scoped_customer_pg_session(), which
# sets the `nce.external_scope_id` GUC -- so RLS would then return the OTHER
# customer's rows. A cross-customer IDOR on all ten routes.
#
# The header check `if not cust_scope: 401` did not catch it: the attacker
# supplies a valid header for their OWN scope, and overrides only the params.
# ---------------------------------------------------------------------------


def _resolved_scope_for(monkeypatch, path, *, headers, params=None, json_body=None):
    """Call a portal route and capture the customer_scope_id it actually resolved."""
    from nce.vertical_modules.customer_portal.app import build_customer_portal_app

    captured: dict = {}

    async def _capture(engine, p):  # noqa: ANN001
        captured.update(p)
        return {"ok": True}

    import nce.vertical_modules.customer_portal.actions as actions_mod
    import nce.vertical_modules.customer_portal.rooms as rooms_mod

    for mod, name in (
        (rooms_mod, "do_room_tracker"),
        (rooms_mod, "do_room_overview"),
        (rooms_mod, "do_asset_register"),
        (actions_mod, "do_raise_service_request"),
    ):
        if hasattr(mod, name):
            monkeypatch.setattr(mod, name, _capture, raising=False)

    client = TestClient(build_customer_portal_app())
    if json_body is not None:
        client.post(path, headers=headers, json=json_body)
    else:
        client.get(path, headers=headers, params=params or {})
    return captured.get("customer_scope_id")


def test_get_route_query_param_cannot_override_header_scope(monkeypatch):
    """A GET query parameter MUST NOT override the authoritative header scope."""
    resolved = _resolved_scope_for(
        monkeypatch,
        f"/api/portal/rooms/{_ROOM_ID}/tracker",
        headers={"X-Customer-Scope-ID": str(_CUSTOMER_A_SCOPE)},
        params={"customer_scope_id": str(_CUSTOMER_B_SCOPE)},
    )
    assert resolved is not None, "route did not resolve a customer scope at all"
    assert str(resolved) == str(_CUSTOMER_A_SCOPE), (
        "IDOR: a client-supplied ?customer_scope_id overrode the header scope "
        f"(resolved {resolved!r}, expected customer A {_CUSTOMER_A_SCOPE!r})"
    )
    assert str(resolved) != str(_CUSTOMER_B_SCOPE)


def test_post_route_body_cannot_override_header_scope(monkeypatch):
    """A POST body field MUST NOT override the authoritative header scope."""
    resolved = _resolved_scope_for(
        monkeypatch,
        "/api/portal/service-requests",
        headers={"X-Customer-Scope-ID": str(_CUSTOMER_A_SCOPE)},
        json_body={
            "customer_scope_id": str(_CUSTOMER_B_SCOPE),
            "subject": "microphone not working",
        },
    )
    assert resolved is not None, "route did not resolve a customer scope at all"
    assert str(resolved) == str(_CUSTOMER_A_SCOPE), (
        "IDOR: a client-supplied body customer_scope_id overrode the header scope "
        f"(resolved {resolved!r}, expected customer A {_CUSTOMER_A_SCOPE!r})"
    )


def test_every_route_assigns_the_authoritative_scope_last():
    """Structural guard: no params dict may end with a client-controlled spread.

    This is the shape that caused the IDOR. Asserting the SHAPE rather than one
    route's behaviour means a newly-added route inherits the check for free --
    the two tests above only cover the two routes they call.
    """
    import re

    src = (REPO_ROOT / "nce" / "vertical_modules" / "customer_portal" / "app.py").read_text(
        encoding="utf-8"
    )

    offenders = re.findall(r"\*\*(?:body|dict\(request\.query_params\)),\s*\n\s*\}", src)
    assert not offenders, (
        f"{len(offenders)} route(s) still spread client-controlled input LAST in their params "
        "dict, so a client can override customer_scope_id. Put the spread FIRST and assign "
        "namespace_id / customer_scope_id after it."
    )

    # ...and the authoritative keys must actually be assigned somewhere
    assert src.count('"customer_scope_id": cust_scope') >= 10
