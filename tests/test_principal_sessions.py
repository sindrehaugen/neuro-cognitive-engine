"""
Integration tests for C3 principal-session wiring (Wave 23).

Proves that the three principal tiers are correctly scoped via the
nce.external_scope_id GUC wired by set_external_scope():

  (a) employee session — external scope GUC not set; has no visibility on
      external_isolation_policy tables (deny-when-unset preserved).
  (b) contractor session — set_external_scope() sets the GUC; only rows
      belonging to the contractor's scope_id are visible.
  (c) external-customer session — identical mechanism to contractor, distinct
      scope UUID; only that customer's rows are visible.
  (d) no-scope / unresolvable scope — session with no scope set sees nothing.

All four tiers must be isolated from each other: no cross-scope leak.

Requires a live database with migration 028 applied.
All tests are @pytest.mark.integration.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import NamespaceContext
from nce.db_utils import POOL_ACQUIRE_TIMEOUT, set_external_scope

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NIL_UUID: str = "00000000-0000-0000-0000-000000000000"
_SCRATCH_TABLE: str = "c3_principal_sessions_test_scratch"

# ---------------------------------------------------------------------------
# DSN helpers (mirrors test_external_scope_rls.py pattern)
# ---------------------------------------------------------------------------


def _get_primary_dsn() -> str:
    return (
        os.getenv("NCE_INTEGRATION_PG_DSN")
        or os.getenv("PG_DSN")
        or os.getenv("DATABASE_URL")
        or ""
    ).strip()


def _app_dsn(primary_dsn: str) -> str:
    """Return a DSN that connects as nce_app (the RLS-enforced role)."""
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(primary_dsn)
    host = parsed.hostname or "localhost"
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    netloc = f"nce_app:nce_app_secret@{netloc}"
    return urlunparse(parsed._replace(netloc=netloc))


# ---------------------------------------------------------------------------
# Low-level connection helpers
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _admin_conn(dsn: str) -> AsyncGenerator[asyncpg.Connection, None]:  # type: ignore[type-arg]
    conn: asyncpg.Connection = await asyncpg.connect(dsn, timeout=POOL_ACQUIRE_TIMEOUT)  # type: ignore[type-arg]
    try:
        yield conn
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Scratch table setup / teardown
# ---------------------------------------------------------------------------


async def _setup_scratch(
    admin: asyncpg.Connection,  # type: ignore[type-arg]
    ns_id: uuid.UUID,
    contractor_scope: uuid.UUID,
    customer_scope: uuid.UUID,
) -> None:
    """Create scratch table with external_isolation_policy; seed one row per scope."""
    await admin.execute(f"DROP TABLE IF EXISTS {_SCRATCH_TABLE}")
    await admin.execute(
        f"""
        CREATE TABLE {_SCRATCH_TABLE} (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            namespace_id      UUID NOT NULL,
            external_scope_id UUID NOT NULL,
            principal_tier    TEXT NOT NULL
        )
        """
    )
    await admin.execute(f"ALTER TABLE {_SCRATCH_TABLE} ENABLE ROW LEVEL SECURITY")
    await admin.execute(f"ALTER TABLE {_SCRATCH_TABLE} FORCE ROW LEVEL SECURITY")
    await admin.execute(f"DROP POLICY IF EXISTS external_isolation_policy ON {_SCRATCH_TABLE}")
    await admin.execute(
        f"""
        CREATE POLICY external_isolation_policy ON {_SCRATCH_TABLE}
            FOR ALL TO nce_app
            USING (
                namespace_id IS NOT NULL
                AND namespace_id = get_nce_namespace()
                AND external_scope_id IS NOT NULL
                AND external_scope_id = get_nce_external_scope()
            )
            WITH CHECK (
                namespace_id IS NOT NULL
                AND namespace_id = get_nce_namespace()
                AND external_scope_id IS NOT NULL
                AND external_scope_id = get_nce_external_scope()
            )
        """
    )
    await admin.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_SCRATCH_TABLE} TO nce_app")

    # Seed rows — one per external principal tier.
    await admin.execute(
        f"INSERT INTO {_SCRATCH_TABLE}(namespace_id, external_scope_id, principal_tier)"
        " VALUES($1, $2, 'contractor')",
        ns_id,
        contractor_scope,
    )
    await admin.execute(
        f"INSERT INTO {_SCRATCH_TABLE}(namespace_id, external_scope_id, principal_tier)"
        " VALUES($1, $2, 'external-customer')",
        ns_id,
        customer_scope,
    )


async def _teardown_scratch(admin: asyncpg.Connection) -> None:  # type: ignore[type-arg]
    await admin.execute(f"DROP TABLE IF EXISTS {_SCRATCH_TABLE}")


async def _ensure_namespace(
    admin: asyncpg.Connection,  # type: ignore[type-arg]
) -> uuid.UUID:
    ns_id = uuid.uuid4()
    await admin.execute(
        "INSERT INTO namespaces(id, slug) VALUES($1, $2) ON CONFLICT DO NOTHING",
        ns_id,
        f"_c3_ps_test_{ns_id}",
    )
    return ns_id


async def _cleanup_namespace(
    admin: asyncpg.Connection,  # type: ignore[type-arg]
    ns_id: uuid.UUID,
) -> None:
    await admin.execute("DELETE FROM namespaces WHERE id = $1", ns_id)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def primary_dsn() -> str:
    dsn = _get_primary_dsn()
    if not dsn:
        pytest.skip(
            "Integration database DSN not configured — set PG_DSN or NCE_INTEGRATION_PG_DSN."
        )
    return dsn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_employee_session_has_no_external_scope_visibility(primary_dsn: str) -> None:
    """Employee session — external scope GUC not set → zero rows on external_isolation_policy.

    Proves: employee sessions never call set_external_scope(); the deny-when-unset
    guarantee from Wave 22 means they see nothing on external-facing tables.
    This is the correct behaviour: employees reach external data through service-layer
    logic, not direct RLS paths.
    """
    app_dsn = _app_dsn(primary_dsn)
    try:
        app: asyncpg.Connection = await asyncpg.connect(app_dsn, timeout=POOL_ACQUIRE_TIMEOUT)  # type: ignore[type-arg]
    except Exception as exc:
        pytest.skip(f"Could not connect as nce_app: {exc}")

    contractor_scope = uuid.uuid4()
    customer_scope = uuid.uuid4()

    async with _admin_conn(primary_dsn) as admin:
        ns_id = await _ensure_namespace(admin)
        await _setup_scratch(admin, ns_id, contractor_scope, customer_scope)

        try:
            async with app.transaction():
                # Employee: set namespace only; external scope GUC NOT set.
                await app.execute("SELECT set_config('nce.namespace_id', $1, true)", str(ns_id))
                rows = await app.fetch(f"SELECT principal_tier FROM {_SCRATCH_TABLE}")
                assert rows == [], (
                    "employee deny-when-unset FAILED: external rows visible without "
                    f"set_external_scope(). Got tiers: {[r['principal_tier'] for r in rows]}"
                )
        finally:
            await app.close()
            await _teardown_scratch(admin)
            await _cleanup_namespace(admin, ns_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_contractor_session_sees_only_its_scope(primary_dsn: str) -> None:
    """Contractor session — set_external_scope() sets GUC → only contractor rows visible.

    Proves: set_external_scope() correctly issues SET LOCAL nce.external_scope_id;
    RLS returns only the contractor's rows; the external-customer row is hidden.
    """
    app_dsn = _app_dsn(primary_dsn)
    try:
        app: asyncpg.Connection = await asyncpg.connect(app_dsn, timeout=POOL_ACQUIRE_TIMEOUT)  # type: ignore[type-arg]
    except Exception as exc:
        pytest.skip(f"Could not connect as nce_app: {exc}")

    contractor_scope = uuid.uuid4()
    customer_scope = uuid.uuid4()

    async with _admin_conn(primary_dsn) as admin:
        ns_id = await _ensure_namespace(admin)
        await _setup_scratch(admin, ns_id, contractor_scope, customer_scope)

        try:
            async with app.transaction():
                # Contractor: set namespace + external scope.
                await app.execute("SELECT set_config('nce.namespace_id', $1, true)", str(ns_id))
                await set_external_scope(app, contractor_scope)

                rows = await app.fetch(
                    f"SELECT principal_tier FROM {_SCRATCH_TABLE} ORDER BY principal_tier"
                )
                tiers = [r["principal_tier"] for r in rows]
                assert tiers == ["contractor"], (
                    f"contractor scope FAILED: expected ['contractor'], got {tiers}"
                )
        finally:
            await app.close()
            await _teardown_scratch(admin)
            await _cleanup_namespace(admin, ns_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_external_customer_session_sees_only_its_scope(primary_dsn: str) -> None:
    """External-customer session — set_external_scope() sets GUC → only customer rows visible.

    Proves: the same mechanism works for external-customer principals; the contractor
    row is hidden even though both are in the same namespace.
    """
    app_dsn = _app_dsn(primary_dsn)
    try:
        app: asyncpg.Connection = await asyncpg.connect(app_dsn, timeout=POOL_ACQUIRE_TIMEOUT)  # type: ignore[type-arg]
    except Exception as exc:
        pytest.skip(f"Could not connect as nce_app: {exc}")

    contractor_scope = uuid.uuid4()
    customer_scope = uuid.uuid4()

    async with _admin_conn(primary_dsn) as admin:
        ns_id = await _ensure_namespace(admin)
        await _setup_scratch(admin, ns_id, contractor_scope, customer_scope)

        try:
            async with app.transaction():
                # External-customer: set namespace + customer scope.
                await app.execute("SELECT set_config('nce.namespace_id', $1, true)", str(ns_id))
                await set_external_scope(app, customer_scope)

                rows = await app.fetch(
                    f"SELECT principal_tier FROM {_SCRATCH_TABLE} ORDER BY principal_tier"
                )
                tiers = [r["principal_tier"] for r in rows]
                assert tiers == ["external-customer"], (
                    f"external-customer scope FAILED: expected ['external-customer'], got {tiers}"
                )
        finally:
            await app.close()
            await _teardown_scratch(admin)
            await _cleanup_namespace(admin, ns_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_cross_scope_leak_between_tiers(primary_dsn: str) -> None:
    """Contractor and external-customer scopes are isolated from each other.

    Proves: setting scope X never reveals scope Y rows even within the same namespace.
    """
    app_dsn = _app_dsn(primary_dsn)
    try:
        app: asyncpg.Connection = await asyncpg.connect(app_dsn, timeout=POOL_ACQUIRE_TIMEOUT)  # type: ignore[type-arg]
    except Exception as exc:
        pytest.skip(f"Could not connect as nce_app: {exc}")

    contractor_scope = uuid.uuid4()
    customer_scope = uuid.uuid4()

    async with _admin_conn(primary_dsn) as admin:
        ns_id = await _ensure_namespace(admin)
        await _setup_scratch(admin, ns_id, contractor_scope, customer_scope)

        try:
            # --- Contractor view: must not see customer rows ---
            async with app.transaction():
                await app.execute("SELECT set_config('nce.namespace_id', $1, true)", str(ns_id))
                await set_external_scope(app, contractor_scope)
                rows = await app.fetch(f"SELECT principal_tier FROM {_SCRATCH_TABLE}")
                tiers = [r["principal_tier"] for r in rows]
                assert "external-customer" not in tiers, (
                    f"cross-scope leak: contractor session saw external-customer row. tiers={tiers}"
                )
                assert "contractor" in tiers, (
                    f"contractor session missing its own row. tiers={tiers}"
                )

            # --- Customer view: must not see contractor rows ---
            async with app.transaction():
                await app.execute("SELECT set_config('nce.namespace_id', $1, true)", str(ns_id))
                await set_external_scope(app, customer_scope)
                rows = await app.fetch(f"SELECT principal_tier FROM {_SCRATCH_TABLE}")
                tiers = [r["principal_tier"] for r in rows]
                assert "contractor" not in tiers, (
                    f"cross-scope leak: customer session saw contractor row. tiers={tiers}"
                )
                assert "external-customer" in tiers, (
                    f"customer session missing its own row. tiers={tiers}"
                )
        finally:
            await app.close()
            await _teardown_scratch(admin)
            await _cleanup_namespace(admin, ns_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unset_scope_denies_all_external_rows(primary_dsn: str) -> None:
    """A session with no scope set sees nothing on external_isolation_policy tables.

    Proves: the deny-when-unset invariant from Wave 22 is preserved end-to-end —
    unresolvable / missing scope → zero rows, never a partial or full open.
    """
    app_dsn = _app_dsn(primary_dsn)
    try:
        app: asyncpg.Connection = await asyncpg.connect(app_dsn, timeout=POOL_ACQUIRE_TIMEOUT)  # type: ignore[type-arg]
    except Exception as exc:
        pytest.skip(f"Could not connect as nce_app: {exc}")

    contractor_scope = uuid.uuid4()
    customer_scope = uuid.uuid4()

    async with _admin_conn(primary_dsn) as admin:
        ns_id = await _ensure_namespace(admin)
        await _setup_scratch(admin, ns_id, contractor_scope, customer_scope)

        try:
            async with app.transaction():
                # Namespace set, but no external scope → deny.
                await app.execute("SELECT set_config('nce.namespace_id', $1, true)", str(ns_id))
                rows = await app.fetch(f"SELECT principal_tier FROM {_SCRATCH_TABLE}")
                assert rows == [], (
                    "deny-when-unset FAILED: rows visible without any external scope. "
                    f"Got: {[r['principal_tier'] for r in rows]}"
                )
        finally:
            await app.close()
            await _teardown_scratch(admin)
            await _cleanup_namespace(admin, ns_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_set_external_scope_is_transaction_local(primary_dsn: str) -> None:
    """set_external_scope() is SET LOCAL: GUC is cleared after transaction ends.

    Proves the no-cross-session-leak guarantee: a subsequent transaction on the
    same connection does NOT inherit the external scope from the previous one.
    """
    app_dsn = _app_dsn(primary_dsn)
    try:
        app: asyncpg.Connection = await asyncpg.connect(app_dsn, timeout=POOL_ACQUIRE_TIMEOUT)  # type: ignore[type-arg]
    except Exception as exc:
        pytest.skip(f"Could not connect as nce_app: {exc}")

    contractor_scope = uuid.uuid4()

    try:
        # Transaction 1: set the scope inside a transaction, verify it is set.
        async with app.transaction():
            await set_external_scope(app, contractor_scope)
            scope_in_tx: str = await app.fetchval(
                "SELECT current_setting('nce.external_scope_id', true)"
            )
            assert scope_in_tx.strip() == str(contractor_scope), (
                f"set_external_scope() did not set GUC. Got: {scope_in_tx!r}"
            )
        # Transaction 1 committed — SET LOCAL must have been cleared.

        # Transaction 2: same connection, scope must be gone.
        async with app.transaction():
            scope_after_commit: str = await app.fetchval(
                "SELECT current_setting('nce.external_scope_id', true)"
            )
            assert (scope_after_commit or "").strip() == "", (
                "set_external_scope() leaked across transactions (not SET LOCAL). "
                f"Got: {scope_after_commit!r}"
            )
    finally:
        await app.close()


# ---------------------------------------------------------------------------
# Unit tests — IDOR proof (no live DB required)
# ---------------------------------------------------------------------------
# These tests prove that external_scope_id is sourced from the verified
# NamespaceContext (JWT-backed), NOT from raw request headers.


def test_external_scope_from_context_employee_returns_none() -> None:
    """Employee NamespaceContext → _external_scope_from_context returns None.

    Proves: employee principals never set the external scope, regardless of
    what any request header might contain.
    """
    from nce.a2a_server import _external_scope_from_context

    ctx = NamespaceContext(
        namespace_id=UUID("11111111-1111-1111-1111-111111111111"),
        agent_id="test-agent",
        principal_kind="employee",
        external_scope_id=None,
    )
    assert _external_scope_from_context(ctx) is None, (
        "employee principal must return None (no external scope)"
    )


def test_external_scope_from_context_contractor_returns_verified_scope() -> None:
    """Contractor NamespaceContext with a scope UUID → returns that UUID string.

    Proves: the scope comes from the verified JWT context, not from any header.
    """
    from nce.a2a_server import _external_scope_from_context

    scope_id = uuid.uuid4()
    ctx = NamespaceContext(
        namespace_id=UUID("22222222-2222-2222-2222-222222222222"),
        agent_id="contractor-agent",
        principal_kind="contractor",
        external_scope_id=scope_id,
    )
    result = _external_scope_from_context(ctx)
    assert result == str(scope_id), f"contractor must return verified scope UUID; got {result!r}"


def test_external_scope_from_context_contractor_missing_scope_returns_none() -> None:
    """Contractor NamespaceContext without a scope → returns None (deny).

    Proves: a contractor JWT that lacks an external_scope_id claim is denied,
    not defaulted-open.
    """
    from nce.a2a_server import _external_scope_from_context

    ctx = NamespaceContext(
        namespace_id=UUID("33333333-3333-3333-3333-333333333333"),
        agent_id="contractor-no-scope",
        principal_kind="contractor",
        external_scope_id=None,
    )
    assert _external_scope_from_context(ctx) is None, (
        "contractor without a scope claim must return None (deny-by-default)"
    )


def test_forged_header_cannot_override_authenticated_scope() -> None:
    """Forged X-NCE-External-Scope-Id header CANNOT set a foreign scope.

    Proves the IDOR fix: _external_scope_from_context reads only from the
    verified NamespaceContext.  A foreign scope UUID placed only in a request
    header has no path to set_external_scope.

    Scenario: authenticated contractor for scope A; attacker forges header
    with scope B (a different contractor's scope UUID).  The function must
    return scope A (from the JWT), not scope B (from the forged header).
    """
    from nce.a2a_server import _external_scope_from_context

    authenticated_scope = uuid.uuid4()
    foreign_scope = uuid.uuid4()
    assert authenticated_scope != foreign_scope, "test setup: scopes must differ"

    # Build the authenticated context (from the JWT — what JWTAuthMiddleware sets)
    ctx = NamespaceContext(
        namespace_id=UUID("44444444-4444-4444-4444-444444444444"),
        agent_id="contractor-agent",
        principal_kind="contractor",
        external_scope_id=authenticated_scope,
    )

    # The forged header value is NOT passed to _external_scope_from_context —
    # the function signature accepts only the verified context.  The foreign scope
    # has no entry point.  This is the structural proof: there is no parameter for
    # a raw header value.
    result = _external_scope_from_context(ctx)

    assert result == str(authenticated_scope), (
        f"IDOR: scope mismatch. Expected authenticated_scope={authenticated_scope!s}, "
        f"got {result!r}. Foreign scope was {foreign_scope!s}."
    )
    assert result != str(foreign_scope), (
        "IDOR: forged foreign_scope leaked into the result — this must not happen."
    )


def test_namespace_context_rejects_unknown_principal_kind() -> None:
    """NamespaceContext normalises unknown principal_kind to 'employee'.

    Proves: an attacker who injects an unrecognised principal_kind claim in a
    JWT (e.g. 'super-admin') is silently demoted to the safest tier ('employee'),
    which carries no external scope.
    """
    ctx = NamespaceContext(
        namespace_id=UUID("55555555-5555-5555-5555-555555555555"),
        agent_id="sneaky-agent",
        principal_kind="super-admin",  # unknown tier
        external_scope_id=None,
    )
    assert ctx.principal_kind == "employee", (
        f"Unknown principal_kind must normalise to 'employee'; got {ctx.principal_kind!r}"
    )
