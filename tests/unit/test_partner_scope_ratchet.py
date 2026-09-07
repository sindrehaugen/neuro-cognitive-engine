"""Unit tests and shrink-only ratchet for Wave B-FT5: Partner Scope Resolution & Impersonation Audit.

Charter B-FT5 Design Contract:
1. Make it explicit: resolve_partner_scope(request, params) returns verified principal context
   when present, and otherwise treats a parameter-supplied scope as declared internal impersonation.
2. Audit every assertion: Emits WORM audit event 'partner_scope_impersonated' with caller identity
   (mTLS SAN / fingerprint or HMAC agent), namespace, and asserted partner_scope_id.
3. Shrink-only ratchet: No handler under nce/admin_handlers/ may extract *_scope_id from
   request.query_params or request body except through resolve_partner_scope.
4. Positive control: Proves RED when a synthetic fifth unmediated site is introduced.
5. Anti-enumeration: Does not probe DB for scope existence.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nce.auth import (
    NamespaceContext,
    extract_caller_identity,
    resolve_partner_scope,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ADMIN_HANDLERS_DIR = _REPO_ROOT / "nce" / "admin_handlers"


# ---------------------------------------------------------------------------
# Test Helpers / Dummy Requests
# ---------------------------------------------------------------------------


class DummyRequest:
    def __init__(
        self,
        headers: dict[str, str] | None = None,
        query_params: dict[str, str] | None = None,
        scope: dict[str, Any] | None = None,
        state: Any = None,
    ):
        self.headers = headers or {}
        self.query_params = query_params or {}
        self.scope = scope or {"type": "http"}
        self.state = state or MagicMock()


# ---------------------------------------------------------------------------
# 1. Verified Principal Context Passthrough
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_partner_scope_from_verified_caller_context() -> None:
    """When caller_ctx has an external_scope_id, it is returned unconditionally."""
    verified_scope = uuid4()
    spoofed_scope = uuid4()

    req = DummyRequest()
    req.state.caller_ctx = NamespaceContext(external_scope_id=verified_scope)

    # Even if parameter provides a different scope, verified context wins
    result = await resolve_partner_scope(req, str(spoofed_scope))
    assert result == str(verified_scope)


@pytest.mark.asyncio
async def test_resolve_partner_scope_from_request_state() -> None:
    """When request.state has external_scope_id, it is returned without impersonation audit."""
    verified_scope = uuid4()
    req = DummyRequest()
    req.state.caller_ctx = None
    req.state.external_scope_id = verified_scope

    result = await resolve_partner_scope(req, None)
    assert result == str(verified_scope)


@pytest.mark.asyncio
async def test_resolve_partner_scope_none_when_unset() -> None:
    """When no scope is passed or present on context, returns None."""
    req = DummyRequest()
    req.state.caller_ctx = None
    req.state.external_scope_id = None

    assert await resolve_partner_scope(req, None) is None
    assert await resolve_partner_scope(req, "") is None
    assert await resolve_partner_scope(req, {}) is None


# ---------------------------------------------------------------------------
# 2. Validation & Invariants (Anti-Enumeration + Deny Sentinels)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_partner_scope_rejects_invalid_uuid() -> None:
    """Malformed UUID strings raise ValueError."""
    req = DummyRequest()
    req.state.caller_ctx = None
    req.state.external_scope_id = None

    with pytest.raises(ValueError, match="Invalid partner_scope_id"):
        await resolve_partner_scope(req, "not-a-valid-uuid")


@pytest.mark.asyncio
async def test_resolve_partner_scope_rejects_nil_uuid_sentinel() -> None:
    """Nil-UUID deny sentinel is forbidden from being asserted as a partner scope."""
    req = DummyRequest()
    req.state.caller_ctx = None
    req.state.external_scope_id = None

    with pytest.raises(ValueError, match="nil UUID sentinel"):
        await resolve_partner_scope(req, "00000000-0000-0000-0000-000000000000")


@pytest.mark.asyncio
async def test_anti_enumeration_rule_does_not_probe_database() -> None:
    """Anti-enumeration: Random uncreated UUID is accepted without database existence checks."""
    req = DummyRequest()
    req.state.caller_ctx = None
    req.state.external_scope_id = None

    uncreated_scope = uuid4()
    # No engine / pool passed — returns scope without DB error or entity-not-found check
    result = await resolve_partner_scope(req, str(uncreated_scope))
    assert result == str(uncreated_scope)


# ---------------------------------------------------------------------------
# 3. Caller Identity Resolution
# ---------------------------------------------------------------------------


def test_extract_caller_identity_from_mtls_san_header() -> None:
    req = DummyRequest(headers={"x-client-cert-san": "partner-gateway.example.com"})
    assert extract_caller_identity(req) == "san:partner-gateway.example.com"


def test_extract_caller_identity_from_mtls_fp_header() -> None:
    req = DummyRequest(headers={"x-client-cert-fingerprint": "12:34:56:78:ab:cd"})
    assert extract_caller_identity(req) == "fp:12:34:56:78:ab:cd"


def test_extract_caller_identity_from_forwarded_client_cert() -> None:
    req = DummyRequest(
        headers={"x-forwarded-client-cert": "Hash=abcdef012345;SAN=tech-subcontractor.av"}
    )
    assert extract_caller_identity(req) == "fp:abcdef012345"


def test_extract_caller_identity_from_hmac_agent() -> None:
    req = DummyRequest(headers={"x-nce-agent-id": "field-dispatch-admin"})
    req.state.caller_ctx = None
    req.state.agent_id = None
    assert extract_caller_identity(req) == "hmac_agent:field-dispatch-admin"


def test_extract_caller_identity_fallback_client() -> None:
    req = DummyRequest(scope={"type": "http", "client": ("10.0.1.25", 49152)})
    req.state.caller_ctx = None
    req.state.agent_id = None
    assert extract_caller_identity(req) == "client:10.0.1.25"


# ---------------------------------------------------------------------------
# 4. Impersonation Audit Event Emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_partner_scope_emits_audit_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """Asserting a partner scope emits a WORM audit event with caller identity & scope."""
    target_scope = uuid4()
    ns_id = uuid4()

    req = DummyRequest(
        headers={
            "x-client-cert-san": "admin-internal.infra",
            "x-nce-namespace-id": str(ns_id),
        }
    )
    req.state.caller_ctx = None
    req.state.external_scope_id = None

    fake_conn = AsyncMock()
    tx_ctx = AsyncMock()
    tx_ctx.__aenter__.return_value = fake_conn
    tx_ctx.__aexit__.return_value = False
    fake_conn.transaction = MagicMock(return_value=tx_ctx)

    acquire_ctx = AsyncMock()
    acquire_ctx.__aenter__.return_value = fake_conn
    acquire_ctx.__aexit__.return_value = False

    fake_pool = MagicMock()
    fake_pool.acquire = MagicMock(return_value=acquire_ctx)
    fake_engine = MagicMock()
    fake_engine.pg_pool = fake_pool

    appended_events: list[dict[str, Any]] = []

    async def _mock_append_event(conn: Any, **kwargs: Any) -> Any:
        appended_events.append(kwargs)
        mock_result = MagicMock()
        mock_result.event_id = uuid4()
        return mock_result

    from nce import event_log

    monkeypatch.setattr(event_log, "append_event", _mock_append_event)

    result = await resolve_partner_scope(
        req,
        {"partner_scope_id": str(target_scope)},
        namespace_id=ns_id,
        engine=fake_engine,
    )

    assert result == str(target_scope)
    assert len(appended_events) == 1
    ev = appended_events[0]
    assert ev["event_type"] == "partner_scope_impersonated"
    assert ev["agent_id"] == "san:admin-internal.infra"
    assert ev["namespace_id"] == ns_id
    assert ev["params"]["partner_scope_id"] == str(target_scope)
    assert ev["params"]["caller_identity"] == "san:admin-internal.infra"


# ---------------------------------------------------------------------------
# 5. Shrink-Only Ratchet & Positive Control
# ---------------------------------------------------------------------------


def _find_unmediated_scope_sites(tree: ast.AST, filename: str) -> list[tuple[int, str]]:
    """Find any raw extraction of *_scope_id from query_params or body not wrapped in resolve_partner_scope."""
    violations: list[tuple[int, str]] = []

    class ScopeVisitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            # If the call is already to resolve_partner_scope, its arguments are mediated
            func_name = ""
            if isinstance(node.func, ast.Name):
                func_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                func_name = node.func.attr

            if func_name == "resolve_partner_scope":
                # Mediated call — don't inspect inner parameters as raw unmediated extractions
                return

            # Check for unmediated extractions: e.g. .get("partner_scope_id")
            if isinstance(node.func, ast.Attribute) and node.func.attr == "get":
                if node.args and isinstance(node.args[0], ast.Constant):
                    val = str(node.args[0].value)
                    if val.endswith("_scope_id") and val != "namespace_id":
                        violations.append(
                            (
                                node.lineno,
                                f"Unmediated '{val}' access via .get() at line {node.lineno}",
                            )
                        )

            # Check subscript access: e.g. query_params["partner_scope_id"]
            self.generic_visit(node)

        def visit_Subscript(self, node: ast.Subscript) -> None:
            if not isinstance(node.ctx, ast.Load):
                self.generic_visit(node)
                return

            slice_node = node.slice
            if isinstance(slice_node, ast.Constant):
                val = str(slice_node.value)
                if val.endswith("_scope_id") and val != "namespace_id":
                    violations.append(
                        (
                            node.lineno,
                            f"Unmediated '{val}' access via subscript at line {node.lineno}",
                        )
                    )
            self.generic_visit(node)

    ScopeVisitor().visit(tree)
    return violations


def test_admin_handlers_have_zero_unmediated_scope_extractions() -> None:
    """Shrink-only ratchet: All *_scope_id extractions in nce/admin_handlers must route through resolve_partner_scope."""
    all_violations: dict[str, list[tuple[int, str]]] = {}

    for file_path in _ADMIN_HANDLERS_DIR.glob("*.py"):
        code = file_path.read_text(encoding="utf-8")
        tree = ast.parse(code, filename=str(file_path))
        violations = _find_unmediated_scope_sites(tree, file_path.name)
        if violations:
            all_violations[file_path.name] = violations

    assert not all_violations, (
        "Found unmediated *_scope_id parameter extractions in admin_handlers:\n"
        + "\n".join(
            f"  {fname}:{lineno} - {msg}"
            for fname, viols in all_violations.items()
            for lineno, msg in viols
        )
        + "\nEvery *_scope_id extraction MUST route through resolve_partner_scope(request, ...)."
    )


def test_positive_control_fails_on_unmediated_fifth_site() -> None:
    """Standing positive control (U18): Proves the ratchet fails RED when an unmediated extraction is present."""
    bad_code = """
async def api_unmediated_handler(request):
    scope = request.query_params.get("partner_scope_id")
    return await do_something(engine, {"partner_scope_id": scope})
"""
    tree = ast.parse(bad_code, filename="synthetic_fifth_site.py")
    violations = _find_unmediated_scope_sites(tree, "synthetic_fifth_site.py")

    assert len(violations) == 1
    lineno, msg = violations[0]
    assert "Unmediated 'partner_scope_id' access" in msg
