"""Unit tests and standing ratchets for Wave A-T6: Adversarial Review of C3 External Scope.

Governance & Contracts:
1. Transaction Locality Proof:
   - set_external_scope(conn, scope) sets 'nce.external_scope_id' with is_local=true.
   - Proves that once the transaction commits or rolls back, the GUC is empty/null on the
     exact same connection, preventing pooled connection cross-request leakage.
2. Anti-Enumeration Parity:
   - resolve_partner_scope() accepts arbitrary valid UUIDs without querying the database,
     preventing oracle probing at the authentication boundary.
   - Rejects the nil-UUID sentinel and malformed UUID strings fail-closed.
3. AST Ratchet Against Unmediated Bulk Dictionary Forwarding:
   - Scans field_tech admin handlers for patterns like `params = dict(body)` where unmediated
     parameters reach underlying vertical cores without resolve_partner_scope().
   - Enforces a shrink-only allowlist with mandatory substantive reasons (>=60 chars).
4. Standing Positive Controls (U18):
   - Proves the AST ratchet flags a synthetic unmediated bulk-forwarding handler.
   - Proves transaction locality assertion fails if a simulated connection leaks GUC.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from nce.auth import NamespaceContext, resolve_partner_scope
from nce.db_utils import set_external_scope

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ADMIN_HANDLERS_DIR = _REPO_ROOT / "nce" / "admin_handlers"

# ---------------------------------------------------------------------------
# Shrink-Only Allowlist for Wholesale Dictionary Forwarding in Admin Handlers
# ---------------------------------------------------------------------------
# As discovered in Wave A-T6 adversarial review:
# These 5 field_tech mutating handlers construct params via `params = dict(body)`
# and pass them directly to `do_*` cores that read `partner_scope_id`, without
# routing `partner_scope_id` through `resolve_partner_scope()`.
# They are pinned here in a shrink-only allowlist so no new sites can be added.

KNOWN_WHOLESALE_FORWARDING_SITES: dict[str, dict[str, str]] = {
    "field_tech.py:api_field_tech_create_work_order": {
        "owner": "field_tech",
        "reason": (
            "Legacy create work order forwards raw JSON body dictionary into do_create_work_order "
            "without invoking resolve_partner_scope. Documented in A-T6 adversarial review; "
            "requires P0 remediation to strip or resolve partner_scope_id before execution."
        ),
    },
    "field_tech.py:api_field_tech_complete_checklist": {
        "owner": "field_tech",
        "reason": (
            "Legacy complete checklist forwards raw JSON body dictionary into do_complete_checklist "
            "without invoking resolve_partner_scope. Documented in A-T6 adversarial review; "
            "requires P0 remediation to strip or resolve partner_scope_id before execution."
        ),
    },
    "field_tech.py:api_field_tech_log_time": {
        "owner": "field_tech",
        "reason": (
            "Legacy log time forwards raw JSON body dictionary into do_log_time "
            "without invoking resolve_partner_scope. Documented in A-T6 adversarial review; "
            "requires P0 remediation to strip or resolve partner_scope_id before execution."
        ),
    },
    "field_tech.py:api_field_tech_attach_photo": {
        "owner": "field_tech",
        "reason": (
            "Legacy attach photo forwards raw JSON body dictionary into do_attach_photo "
            "without invoking resolve_partner_scope. Documented in A-T6 adversarial review; "
            "requires P0 remediation to strip or resolve partner_scope_id before execution."
        ),
    },
    "field_tech.py:api_field_tech_sync": {
        "owner": "field_tech",
        "reason": (
            "Legacy offline sync forwards raw JSON body dictionary into do_sync "
            "without invoking resolve_partner_scope. Documented in A-T6 adversarial review; "
            "requires P0 remediation to strip or resolve partner_scope_id before execution."
        ),
    },
}


# ---------------------------------------------------------------------------
# 1. Transaction Locality & Session Isolation Tests
# ---------------------------------------------------------------------------


class SimulatedPooledConnection:
    """Simulates a stateful pooled asyncpg Connection tracking transaction-local GUCs."""

    def __init__(self) -> None:
        self.session_gucs: dict[str, str] = {}
        self.local_gucs: dict[str, str] = {}
        self.in_transaction: bool = False

    class _TxContext:
        def __init__(self, conn: SimulatedPooledConnection) -> None:
            self.conn = conn
            self._prev_local: dict[str, str] = {}

        async def __aenter__(self) -> SimulatedPooledConnection:
            self.conn.in_transaction = True
            self._prev_local = dict(self.conn.local_gucs)
            return self.conn

        async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
            # Transaction commits or rolls back -> discard all transaction-local GUCs
            self.conn.local_gucs.clear()
            self.conn.in_transaction = False
            return False

    def transaction(self) -> _TxContext:
        return self._TxContext(self)

    async def execute(self, query: str, *args: Any) -> None:
        # Emulate SELECT set_config('nce.external_scope_id', $1, true)
        if "set_config" in query and "nce.external_scope_id" in query:
            val = str(args[0])
            if self.in_transaction:
                self.local_gucs["nce.external_scope_id"] = val
            else:
                # autocommit statement-local resets immediately
                pass

    async def fetchval(self, query: str, *args: Any) -> str | None:
        if "current_setting('nce.external_scope_id'" in query:
            if self.in_transaction:
                return self.local_gucs.get("nce.external_scope_id") or ""
            return self.session_gucs.get("nce.external_scope_id") or ""
        return ""


@pytest.mark.asyncio
async def test_set_external_scope_transaction_locality_simulated() -> None:
    """Transaction locality contract: GUC cleared upon transaction exit on same physical connection."""
    conn = SimulatedPooledConnection()
    scope_a = uuid4()
    scope_b = uuid4()

    # 1. Transaction A commits
    async with conn.transaction():
        await set_external_scope(conn, scope_a)  # type: ignore[arg-type]
        val = await conn.fetchval("SELECT current_setting('nce.external_scope_id', true)")
        assert val == str(scope_a)

    # After exit, GUC must be empty
    after_a = await conn.fetchval("SELECT current_setting('nce.external_scope_id', true)")
    assert after_a == ""

    # 2. Transaction B rolls back
    try:
        async with conn.transaction():
            await set_external_scope(conn, scope_b)  # type: ignore[arg-type]
            val_b = await conn.fetchval("SELECT current_setting('nce.external_scope_id', true)")
            assert val_b == str(scope_b)
            raise RuntimeError("simulated failure")
    except RuntimeError:
        pass

    # After rollback, GUC must still be empty
    after_b = await conn.fetchval("SELECT current_setting('nce.external_scope_id', true)")
    assert after_b == ""


@pytest.mark.asyncio
async def test_set_external_scope_live_postgres_if_available() -> None:
    """Test transaction-local GUC behavior against live PostgreSQL if reachable."""
    import asyncpg

    dsn = os.getenv(
        "NCE_INTEGRATION_PG_DSN",
        os.getenv("PG_DSN", "postgresql://mcp_user:mcp_password@127.0.0.1:5432/memory_meta"),
    )

    try:
        conn = await asyncpg.connect(dsn, timeout=2.0)
    except Exception as exc:
        pytest.skip(f"Live PostgreSQL not reachable ({exc}); simulated test covers contract")

    try:
        scope_a = uuid4()
        scope_b = uuid4()

        # Transaction 1: Commit
        async with conn.transaction():
            await set_external_scope(conn, scope_a)
            val = await conn.fetchval("SELECT current_setting('nce.external_scope_id', true)")
            assert val == str(scope_a)

        # After commit: must be cleared
        val_after = await conn.fetchval("SELECT current_setting('nce.external_scope_id', true)")
        assert not val_after

        # Transaction 2: Rollback
        try:
            async with conn.transaction():
                await set_external_scope(conn, scope_b)
                val = await conn.fetchval("SELECT current_setting('nce.external_scope_id', true)")
                assert val == str(scope_b)
                raise RuntimeError("force rollback")
        except RuntimeError:
            pass

        val_after_rb = await conn.fetchval("SELECT current_setting('nce.external_scope_id', true)")
        assert not val_after_rb
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# 2. Anti-Enumeration & Input Validation Tests
# ---------------------------------------------------------------------------


class _DummyReq:
    def __init__(self, ctx: Any = None) -> None:
        self.state = SimpleNamespace(caller_ctx=ctx, external_scope_id=None)
        self.query_params: dict[str, str] = {}
        self.headers: dict[str, str] = {}


@pytest.mark.asyncio
async def test_anti_enumeration_arbitrary_uuids_accepted_without_db() -> None:
    """Anti-enumeration: random valid UUID string accepted without DB probe."""
    req = _DummyReq(ctx=None)
    random_uuid = str(uuid4())
    result = await resolve_partner_scope(req, random_uuid)
    assert result == random_uuid


@pytest.mark.asyncio
async def test_resolve_partner_scope_rejects_nil_uuid() -> None:
    """Deny-sentinel: nil UUID must be rejected unconditionally."""
    req = _DummyReq(ctx=None)
    with pytest.raises(ValueError, match="nil UUID sentinel"):
        await resolve_partner_scope(req, "00000000-0000-0000-0000-000000000000")


@pytest.mark.asyncio
async def test_resolve_partner_scope_rejects_malformed_syntax() -> None:
    """Fail-closed on invalid UUID strings."""
    req = _DummyReq(ctx=None)
    with pytest.raises(ValueError, match="Invalid partner_scope_id"):
        await resolve_partner_scope(req, "not-a-valid-uuid-12345")


@pytest.mark.asyncio
async def test_resolve_partner_scope_prefers_verified_jwt_claim() -> None:
    """Verified context from JWT claim overrides any parameter-supplied scope."""
    jwt_scope = uuid4()
    ctx = NamespaceContext(external_scope_id=jwt_scope)
    req = _DummyReq(ctx=ctx)

    spoofed_param = str(uuid4())
    result = await resolve_partner_scope(req, spoofed_param)
    assert result == str(jwt_scope)
    assert result != spoofed_param


# ---------------------------------------------------------------------------
# 3. AST Ratchet: Wholesale Body Forwarding Detection
# ---------------------------------------------------------------------------

_CORES_CONSUMING_PARTNER_SCOPE = frozenset(
    {
        "do_create_work_order",
        "do_complete_checklist",
        "do_log_time",
        "do_attach_photo",
        "do_sync",
        "do_partner_view",
        "do_upsert_contractor",
    }
)


def _find_wholesale_dict_forwarding_sites(
    tree: ast.AST, filename: str
) -> list[tuple[int, str, str]]:
    """Detect handlers where request body dictionary is copied directly via dict(body) into scope-consuming cores."""
    findings: list[tuple[int, str, str]] = []

    class BodyForwardingVisitor(ast.NodeVisitor):
        def _check_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            # Check if this function calls resolve_partner_scope
            has_resolve_partner_scope = False
            called_cores: set[str] = set()

            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    func_name = ""
                    if isinstance(child.func, ast.Name):
                        func_name = child.func.id
                    elif isinstance(child.func, ast.Attribute):
                        func_name = child.func.attr
                    if func_name == "resolve_partner_scope":
                        has_resolve_partner_scope = True
                    if func_name in _CORES_CONSUMING_PARTNER_SCOPE:
                        called_cores.add(func_name)

            if not called_cores:
                return

            # Search for `params = dict(body)`
            for child in ast.walk(node):
                if isinstance(child, ast.Assign):
                    if isinstance(child.value, ast.Call):
                        call_node = child.value
                        is_dict_call = (
                            isinstance(call_node.func, ast.Name) and call_node.func.id == "dict"
                        )
                        if is_dict_call and call_node.args:
                            arg0 = call_node.args[0]
                            if isinstance(arg0, ast.Name) and arg0.id in ("body", "arguments"):
                                if not has_resolve_partner_scope:
                                    cores_str = ", ".join(sorted(called_cores))
                                    findings.append(
                                        (
                                            child.lineno,
                                            node.name,
                                            f"Wholesale forwarding `{arg0.id}` via `dict({arg0.id})` into scope-consuming core ({cores_str}) without resolve_partner_scope()",
                                        )
                                    )

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._check_func(node)
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._check_func(node)
            self.generic_visit(node)

    BodyForwardingVisitor().visit(tree)
    return findings


def test_wholesale_dict_forwarding_ratchet_is_shrink_only() -> None:
    """Ratchet: Wholesale dictionary forwarding sites in admin_handlers must not grow and must shrink as fixed."""
    found_sites: dict[str, str] = {}

    for file_path in sorted(_ADMIN_HANDLERS_DIR.glob("*.py")):
        code = file_path.read_text(encoding="utf-8")
        tree = ast.parse(code, filename=str(file_path))
        findings = _find_wholesale_dict_forwarding_sites(tree, file_path.name)
        for lineno, func_name, desc in findings:
            key = f"{file_path.name}:{func_name}"
            found_sites[key] = f"Line {lineno}: {desc}"

    # Verify no unallowlisted sites exist
    unallowlisted = set(found_sites.keys()) - set(KNOWN_WHOLESALE_FORWARDING_SITES.keys())
    assert not unallowlisted, (
        f"Found {len(unallowlisted)} UNALLOWLISTED wholesale dictionary forwarding sites in admin_handlers:\n"
        + "\n".join(f"  {k} -> {found_sites[k]}" for k in sorted(unallowlisted))
        + "\nAll wholesale forwarding of body/arguments must be mediated or resolved via resolve_partner_scope()."
    )

    # Verify every allowlist entry has a substantive reason (>= 60 chars)
    for key, data in KNOWN_WHOLESALE_FORWARDING_SITES.items():
        reason = data.get("reason", "")
        assert len(reason) >= 60, (
            f"Allowlist entry '{key}' must have a substantive reason (>=60 chars); got {len(reason)}"
        )
        assert data.get("owner"), f"Allowlist entry '{key}' must have an owner"

    # Baseline match verification (shrink-only, audit fix 2026-09-20): this was
    # `== 5`, which fires the moment any ONE of the 5 allowlisted sites is
    # legitimately fixed (removed from KNOWN_WHOLESALE_FORWARDING_SITES) without
    # this literal also being hand-edited in the same change -- exact-equality
    # on a shrink-only ratchet punishes remediation instead of only blocking
    # growth. KNOWN_WHOLESALE_FORWARDING_SITES is already the mechanism that
    # blocks growth (see the `unallowlisted` assertion above); this count only
    # needs to guard against silent over-shrinkage of the tracked set itself.
    assert len(found_sites) <= 5, (
        f"Expected at most 5 known wholesale-forwarding sites, found {len(found_sites)}. "
        "This ratchet is shrink-only: fixing one of the 5 allowlisted "
        "field_tech.py sites (and removing it from KNOWN_WHOLESALE_FORWARDING_SITES) "
        "should make this number go down without needing this literal updated."
    )


# ---------------------------------------------------------------------------
# 4. Standing Positive Controls (U18)
# ---------------------------------------------------------------------------


def test_positive_control_catches_synthetic_unmediated_body_forwarding() -> None:
    """U18 Positive Control: Ratchet fails RED when synthetic unmediated dict(body) into scope-consuming core is introduced."""
    import textwrap

    synthetic_bad_code = textwrap.dedent("""
    async def api_synthetic_create_work_order(request):
        body, err = await _parse_json_body(request)
        params = dict(body)
        return await do_create_work_order(engine, params)
    """)
    tree = ast.parse(synthetic_bad_code, filename="synthetic_handler.py")
    findings = _find_wholesale_dict_forwarding_sites(tree, "synthetic_handler.py")

    assert len(findings) == 1
    lineno, func_name, desc = findings[0]
    assert func_name == "api_synthetic_create_work_order"
    assert "Wholesale forwarding `body`" in desc
    assert "do_create_work_order" in desc


def test_positive_control_catches_leaking_pooled_connection() -> None:
    """U18 Positive Control: Proof fails RED if a pooled connection retains GUC across transactions."""

    class BrokenLeakingConnection:
        def __init__(self) -> None:
            self.retained_guc: str = ""

        class _BrokenTx:
            def __init__(self, parent: BrokenLeakingConnection) -> None:
                self.parent = parent

            async def __aenter__(self) -> BrokenLeakingConnection:
                return self.parent

            async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
                # Bug: Fails to clear transaction-local GUC on transaction exit
                return False

        def transaction(self) -> _BrokenTx:
            return self._BrokenTx(self)

        async def execute(self, query: str, *args: Any) -> None:
            self.retained_guc = str(args[0])

        async def fetchval(self, query: str, *args: Any) -> str:
            return self.retained_guc

    broken_conn = BrokenLeakingConnection()
    test_scope = uuid4()

    async def _run_leak_test() -> None:
        async with broken_conn.transaction():
            await set_external_scope(broken_conn, test_scope)  # type: ignore[arg-type]

        # After transaction, value leaked into session
        after_val = await broken_conn.fetchval(
            "SELECT current_setting('nce.external_scope_id', true)"
        )
        assert after_val == ""  # This assertion MUST fail on the leaking connection

    import asyncio

    with pytest.raises(AssertionError):
        asyncio.run(_run_leak_test())
