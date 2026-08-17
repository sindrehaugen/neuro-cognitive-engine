"""
Integration tests for the C3 external-principal RLS primitive.

Proves three invariants from Wave 22 (029_c3_external_scope_rls.sql):
  (a) deny-when-unset: nce.external_scope_id not set → zero rows visible.
  (b) scoped visibility: GUC set to scope X → only scope-X rows visible.
  (c) ANDs the namespace: row in namespace B never leaks even when
      nce.external_scope_id matches and nce.namespace_id is set to namespace A.

Each test creates a temporary scratch table that carries both namespace_id and
external_scope_id, applies external_isolation_policy, and tears down afterward.

All tests are @pytest.mark.integration (require a live DB).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import asyncpg  # type: ignore[import-untyped]
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NIL_UUID = "00000000-0000-0000-0000-000000000000"

_APP_TABLE = "c3_rls_test_scratch"


def _get_primary_dsn() -> str:
    return (
        os.getenv("NCE_INTEGRATION_PG_DSN")
        or os.getenv("PG_DSN")
        or os.getenv("DATABASE_URL")
        or ""
    ).strip()


def _app_dsn(primary_dsn: str) -> str:
    """Rewrite the primary DSN to connect as nce_app."""
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(primary_dsn)
    host = parsed.hostname or "localhost"
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    netloc = f"nce_app:nce_app_secret@{netloc}"
    return urlunparse(parsed._replace(netloc=netloc))


@asynccontextmanager
async def _admin_conn(dsn: str) -> AsyncGenerator[asyncpg.Connection, None]:  # type: ignore[type-arg]
    """Superuser / primary-DSN connection for DDL setup/teardown."""
    conn: asyncpg.Connection = await asyncpg.connect(dsn, timeout=10.0)  # type: ignore[type-arg]
    try:
        yield conn
    finally:
        await conn.close()


@asynccontextmanager
async def _app_conn(dsn: str) -> AsyncGenerator[asyncpg.Connection, None]:  # type: ignore[type-arg]
    """nce_app connection used to exercise RLS policies."""
    conn: asyncpg.Connection = await asyncpg.connect(dsn, timeout=10.0)  # type: ignore[type-arg]
    try:
        yield conn
    finally:
        await conn.close()


async def _setup_scratch(admin: asyncpg.Connection, ns_a: uuid.UUID, ns_b: uuid.UUID) -> None:  # type: ignore[type-arg]
    """Create scratch table, apply external_isolation_policy, seed rows."""
    await admin.execute(f"DROP TABLE IF EXISTS {_APP_TABLE}")
    await admin.execute(
        f"""
        CREATE TABLE {_APP_TABLE} (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            namespace_id    UUID NOT NULL,
            external_scope_id UUID NOT NULL,
            label           TEXT NOT NULL
        )
        """
    )
    await admin.execute(f"ALTER TABLE {_APP_TABLE} ENABLE ROW LEVEL SECURITY")
    await admin.execute(f"ALTER TABLE {_APP_TABLE} FORCE ROW LEVEL SECURITY")
    await admin.execute(f"DROP POLICY IF EXISTS external_isolation_policy ON {_APP_TABLE}")
    await admin.execute(
        f"""
        CREATE POLICY external_isolation_policy ON {_APP_TABLE}
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
    await admin.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_APP_TABLE} TO nce_app")

    scope_x = str(uuid.uuid4())
    scope_y = str(uuid.uuid4())

    # Rows in namespace A
    await admin.execute(
        f"INSERT INTO {_APP_TABLE}(namespace_id, external_scope_id, label) VALUES($1,$2,'A-X')",
        ns_a,
        scope_x,
    )
    await admin.execute(
        f"INSERT INTO {_APP_TABLE}(namespace_id, external_scope_id, label) VALUES($1,$2,'A-Y')",
        ns_a,
        scope_y,
    )
    # Row in namespace B — same scope_x, to probe cross-namespace leak
    await admin.execute(
        f"INSERT INTO {_APP_TABLE}(namespace_id, external_scope_id, label) VALUES($1,$2,'B-X')",
        ns_b,
        scope_x,
    )

    return scope_x, scope_y  # type: ignore[return-value]


async def _teardown_scratch(admin: asyncpg.Connection) -> None:  # type: ignore[type-arg]
    await admin.execute(f"DROP TABLE IF EXISTS {_APP_TABLE}")


async def _ensure_namespaces(
    admin: asyncpg.Connection,  # type: ignore[type-arg]
) -> tuple[uuid.UUID, uuid.UUID]:
    """Return two distinct namespace UUIDs, creating them when absent."""
    ns_a: uuid.UUID = uuid.uuid4()
    ns_b: uuid.UUID = uuid.uuid4()
    await admin.execute(
        "INSERT INTO namespaces(id, slug) VALUES($1,$2) ON CONFLICT DO NOTHING",
        ns_a,
        f"_c3_test_a_{ns_a}",
    )
    await admin.execute(
        "INSERT INTO namespaces(id, slug) VALUES($1,$2) ON CONFLICT DO NOTHING",
        ns_b,
        f"_c3_test_b_{ns_b}",
    )
    return ns_a, ns_b


async def _cleanup_namespaces(
    admin: asyncpg.Connection,  # type: ignore[type-arg]
    ns_a: uuid.UUID,
    ns_b: uuid.UUID,
) -> None:
    await admin.execute("DELETE FROM namespaces WHERE id IN ($1,$2)", ns_a, ns_b)


# ---------------------------------------------------------------------------
# Fixtures
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
async def test_deny_when_guc_unset(primary_dsn: str) -> None:
    """(a) nce.external_scope_id not set → zero rows returned via nce_app role."""
    app_dsn = _app_dsn(primary_dsn)

    try:
        app: asyncpg.Connection = await asyncpg.connect(app_dsn, timeout=10.0)  # type: ignore[type-arg]
    except Exception as exc:
        pytest.skip(f"Could not connect as nce_app: {exc}")

    async with _admin_conn(primary_dsn) as admin:
        ns_a, ns_b = await _ensure_namespaces(admin)
        scope_x, _scope_y = await _setup_scratch(admin, ns_a, ns_b)

        try:
            async with app.transaction():
                # Set namespace but do NOT set external_scope_id GUC.
                await app.execute("SELECT set_config('nce.namespace_id', $1, true)", str(ns_a))
                rows = await app.fetch(f"SELECT label FROM {_APP_TABLE}")
                assert rows == [], (
                    "deny-when-unset FAILED: rows visible even without external_scope_id set. "
                    f"Got: {[r['label'] for r in rows]}"
                )
        finally:
            await app.close()
            await _teardown_scratch(admin)
            await _cleanup_namespaces(admin, ns_a, ns_b)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scoped_visibility(primary_dsn: str) -> None:
    """(b) GUC set to scope X → only scope-X rows in the same namespace visible."""
    app_dsn = _app_dsn(primary_dsn)

    try:
        app: asyncpg.Connection = await asyncpg.connect(app_dsn, timeout=10.0)  # type: ignore[type-arg]
    except Exception as exc:
        pytest.skip(f"Could not connect as nce_app: {exc}")

    async with _admin_conn(primary_dsn) as admin:
        ns_a, ns_b = await _ensure_namespaces(admin)
        scope_x, scope_y = await _setup_scratch(admin, ns_a, ns_b)

        try:
            async with app.transaction():
                await app.execute("SELECT set_config('nce.namespace_id', $1, true)", str(ns_a))
                await app.execute("SELECT set_config('nce.external_scope_id', $1, true)", scope_x)
                rows = await app.fetch(f"SELECT label FROM {_APP_TABLE} ORDER BY label")
                labels = [r["label"] for r in rows]
                # Only A-X should be visible (scope X, namespace A).
                # A-Y (scope Y) and B-X (namespace B) must be hidden.
                assert labels == ["A-X"], (
                    f"scoped-visibility FAILED: expected ['A-X'], got {labels}"
                )
        finally:
            await app.close()
            await _teardown_scratch(admin)
            await _cleanup_namespaces(admin, ns_a, ns_b)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_cross_namespace_leak(primary_dsn: str) -> None:
    """(c) Setting scope X but namespace A → row B-X (same scope, other namespace) not visible."""
    app_dsn = _app_dsn(primary_dsn)

    try:
        app: asyncpg.Connection = await asyncpg.connect(app_dsn, timeout=10.0)  # type: ignore[type-arg]
    except Exception as exc:
        pytest.skip(f"Could not connect as nce_app: {exc}")

    async with _admin_conn(primary_dsn) as admin:
        ns_a, ns_b = await _ensure_namespaces(admin)
        scope_x, _scope_y = await _setup_scratch(admin, ns_a, ns_b)

        try:
            async with app.transaction():
                # Session is for namespace A with scope X.
                await app.execute("SELECT set_config('nce.namespace_id', $1, true)", str(ns_a))
                await app.execute("SELECT set_config('nce.external_scope_id', $1, true)", scope_x)
                rows = await app.fetch(f"SELECT label FROM {_APP_TABLE} ORDER BY label")
                labels = [r["label"] for r in rows]
                # B-X shares the same scope_x but lives in ns_b — must NOT appear.
                assert "B-X" not in labels, (
                    f"cross-namespace leak DETECTED: B-X appeared in ns_a session. labels={labels}"
                )
                # A-X must be present (sanity check).
                assert "A-X" in labels, f"cross-namespace test broken: A-X missing. labels={labels}"
        finally:
            await app.close()
            await _teardown_scratch(admin)
            await _cleanup_namespaces(admin, ns_a, ns_b)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_nil_uuid_sentinel_get_nce_external_scope(primary_dsn: str) -> None:
    """get_nce_external_scope() returns nil UUID when GUC is unset."""
    async with _admin_conn(primary_dsn) as admin:
        # Unset GUC context (do not set nce.external_scope_id).
        result: str = await admin.fetchval("SELECT get_nce_external_scope()::text")
        assert result == _NIL_UUID, f"sentinel FAILED: expected nil UUID, got {result!r}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_nce_external_scope_returns_set_value(primary_dsn: str) -> None:
    """get_nce_external_scope() returns the set UUID when GUC is properly set."""
    expected = str(uuid.uuid4())
    async with _admin_conn(primary_dsn) as admin:
        async with admin.transaction():
            await admin.execute("SELECT set_config('nce.external_scope_id', $1, true)", expected)
            result: str = await admin.fetchval("SELECT get_nce_external_scope()::text")
        assert result == expected, (
            f"get_nce_external_scope() FAILED: expected {expected!r}, got {result!r}"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_malformed_guc_returns_sentinel(primary_dsn: str) -> None:
    """get_nce_external_scope() returns nil UUID for malformed (non-UUID) GUC value."""
    async with _admin_conn(primary_dsn) as admin:
        async with admin.transaction():
            await admin.execute("SELECT set_config('nce.external_scope_id', 'not-a-uuid', true)")
            result: str = await admin.fetchval("SELECT get_nce_external_scope()::text")
        assert result == _NIL_UUID, (
            f"malformed-GUC sentinel FAILED: expected nil UUID, got {result!r}"
        )
