"""Pytest bootstrap — per-test signing cache isolation for parallel-safe execution."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator, Generator

# `nce.config` fails fast on import if unset; tests often import the package
# without a local .env — provide deterministic dev keys for collection only.
for _key, _default in {
    "NCE_MASTER_KEY": "x" * 32,
    # NCE_API_KEY is the HMAC secret for HMACAuthMiddleware (admin HTTP API).
    # Must be set before nce.config is imported so that _Config.NCE_API_KEY and
    # the middleware captured at admin_app.app construction time both see the
    # same non-empty value; otherwise all /api/* requests return 401.
    "NCE_API_KEY": "test-nce-api-key-for-unit-tests",
    "NCE_ADMIN_API_KEY": "test-admin-api-key-for-unit-tests",
    "NCE_MCP_API_KEY": "test-mcp-api-key-for-unit-tests",
    "DROPBOX_APP_SECRET": "test-dropbox-secret",
    "GRAPH_CLIENT_STATE": "test-graph-state",
    "DRIVE_CHANNEL_TOKEN": "test-drive-token",
    # mTLS strict mode is disabled in unit tests — bridges don't have certs.
    # Production deployments must set NCE_MTLS_STRICT=true (default).
    "NCE_MTLS_STRICT": "false",
}.items():
    os.environ.setdefault(_key, _default)

import asyncpg
import pytest
import pytest_asyncio


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Run ``test_init_public_api`` last — it purges ``nce`` from ``sys.modules``."""
    purge_last: list[pytest.Item] = []
    rest: list[pytest.Item] = []
    for item in items:
        if "test_init_public_api" in item.nodeid:
            purge_last.append(item)
        else:
            rest.append(item)
    items[:] = rest + purge_last


@pytest.fixture(autouse=True)
def _inject_mcp_tenant_api_key_for_tool_calls(request, monkeypatch):
    """Supplies MCP admin/tenant API credentials on every test's behalf, suite-wide.

    That means missing-credential rejections are unobservable to any test that
    goes through this fixture: the real auth boundary is patched away before the
    test body runs. A test that needs to exercise that boundary for real must be
    marked `@pytest.mark.real_mcp_auth` to opt out of this patching.
    """
    if request.node.get_closest_marker("real_mcp_auth"):
        return
    from nce.auth import MCP_ADMIN_TOOL_NAMES, enforce_mcp_tool_auth
    from nce.tool_registry import TOOL_REGISTRY

    _real = enforce_mcp_tool_auth

    def _enforce_with_test_keys(tool_name: str, arguments: dict) -> None:
        args = dict(arguments)
        spec = TOOL_REGISTRY.get(tool_name)
        if tool_name in MCP_ADMIN_TOOL_NAMES or (spec is not None and spec.admin_only):
            args.setdefault("admin_api_key", os.environ.get("NCE_ADMIN_API_KEY", ""))
        elif not args.get("admin_api_key"):
            args.setdefault("mcp_api_key", os.environ.get("NCE_MCP_API_KEY", ""))
        return _real(tool_name, args)

    monkeypatch.setattr("nce.auth.enforce_mcp_tool_auth", _enforce_with_test_keys)
    monkeypatch.setattr("nce.mcp_stdio_dispatch.enforce_mcp_tool_auth", _enforce_with_test_keys)


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name, "true" if default else "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def first_recorded_contradiction(out: dict | None) -> dict | None:
    """First row from ``detect_contradictions`` (``{"contradictions": [...]}`` or legacy flat dict)."""
    if out is None:
        return None
    items = out.get("contradictions")
    if items:
        return items[0]
    return out


_TEST_ENV_DEFAULTS: dict[str, str] = {
    "NCE_MASTER_KEY": "x" * 32,
    "NCE_API_KEY": "test-nce-api-key-for-unit-tests",
    "NCE_ADMIN_API_KEY": "test-admin-api-key-for-unit-tests",
    "NCE_MCP_API_KEY": "test-mcp-api-key-for-unit-tests",
    "DROPBOX_APP_SECRET": "test-dropbox-secret",
    "GRAPH_CLIENT_STATE": "test-graph-state",
    "DRIVE_CHANNEL_TOKEN": "test-drive-token",
}


def _restore_mcp_env_api_keys() -> None:
    """Some tests clear env keys (e.g. admin hardening); restore blanks for isolation."""
    for key, default in _TEST_ENV_DEFAULTS.items():
        if not os.environ.get(key, "").strip():
            os.environ[key] = default


def _restore_nce_cfg_from_env() -> None:
    """Reset module-level ``cfg`` fields tests often mutate on the shared singleton."""
    from nce.config import cfg

    _restore_mcp_env_api_keys()

    env = os.environ.get("NCE_ENV", "dev").strip().lower()
    cfg.ENVIRONMENT = env
    cfg.IS_PROD = env in {"prod", "production"}
    cfg.IS_TEST = env in {"test", "testing", "ci"}
    cfg.IS_DEV = not cfg.IS_PROD and not cfg.IS_TEST
    cfg.REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    cfg.NCE_API_KEY = os.environ.get("NCE_API_KEY", getattr(cfg, "NCE_API_KEY", ""))
    cfg.NCE_MCP_API_KEY = os.environ.get("NCE_MCP_API_KEY", "test-mcp-api-key-for-unit-tests")
    cfg.NCE_MCP_NAMESPACE_ID = os.environ.get("NCE_MCP_NAMESPACE_ID", "")
    cfg.NCE_ADMIN_API_KEY = os.environ.get("NCE_ADMIN_API_KEY", "test-admin-api-key-for-unit-tests")
    cfg.NCE_ADMIN_OVERRIDE = _env_bool("NCE_ADMIN_OVERRIDE", default=False)
    cfg.NCE_QUOTAS_ENABLED = _env_bool("NCE_QUOTAS_ENABLED", default=True)
    cfg.NCE_QUOTA_REDIS_COUNTERS = _env_bool("NCE_QUOTA_REDIS_COUNTERS", default=True)
    cfg.NCE_OBSERVABILITY_ENABLED = _env_bool("NCE_OBSERVABILITY_ENABLED", default=True)
    cfg.NCE_MAX_TEMPORAL_LOOKBACK_DAYS = int(os.environ.get("NCE_MAX_TEMPORAL_LOOKBACK_DAYS", "90"))
    cfg.NCE_JWT_SECRET = os.environ.get("NCE_JWT_SECRET", "")
    cfg.NCE_JWT_PUBLIC_KEY = os.environ.get("NCE_JWT_PUBLIC_KEY", "")
    cfg.NCE_JWT_ALGORITHM = (os.environ.get("NCE_JWT_ALGORITHM") or "HS256").upper().strip()
    cfg.NCE_JWT_ISSUER = os.environ.get("NCE_JWT_ISSUER", "")
    cfg.NCE_JWT_AUDIENCE = os.environ.get("NCE_JWT_AUDIENCE", "")
    cfg.NCE_JWT_LEEWAY_SECONDS = int(os.environ.get("NCE_JWT_LEEWAY_SECONDS", "30"))
    cfg.NCE_DISABLE_MIGRATION_MCP = _env_bool("NCE_DISABLE_MIGRATION_MCP", default=cfg.IS_PROD)
    cfg.NCE_MINIO_REQUIRED = _env_bool("NCE_MINIO_REQUIRED", default=True)
    cfg.NCE_EMBEDDING_MODEL_REVISION = os.environ.get("NCE_EMBEDDING_MODEL_REVISION", "")
    cfg.AZURE_CLIENT_ID = os.environ.get("AZURE_CLIENT_ID", "")
    cfg.AZURE_CLIENT_SECRET = os.environ.get("AZURE_CLIENT_SECRET", "")
    cfg.GDRIVE_OAUTH_CLIENT_ID = os.environ.get("GDRIVE_OAUTH_CLIENT_ID", "")
    cfg.GDRIVE_OAUTH_CLIENT_SECRET = os.environ.get("GDRIVE_OAUTH_CLIENT_SECRET", "")
    cfg.DROPBOX_OAUTH_CLIENT_ID = os.environ.get("DROPBOX_OAUTH_CLIENT_ID", "")
    cfg.WEBHOOK_MAX_BODY_BYTES = max(
        1, int(os.environ.get("WEBHOOK_MAX_BODY_BYTES", str(cfg.WEBHOOK_MAX_BODY_BYTES)))
    )
    cfg.WEBHOOK_RATE_LIMIT = max(
        1, int(os.environ.get("WEBHOOK_RATE_LIMIT", str(cfg.WEBHOOK_RATE_LIMIT)))
    )
    cfg.WEBHOOK_RATE_PERIOD_SECONDS = max(
        1,
        int(os.environ.get("WEBHOOK_RATE_PERIOD_SECONDS", str(cfg.WEBHOOK_RATE_PERIOD_SECONDS))),
    )
    cfg.WEBHOOK_DEDUP_TTL_SECONDS = max(
        60, int(os.environ.get("WEBHOOK_DEDUP_TTL_SECONDS", str(cfg.WEBHOOK_DEDUP_TTL_SECONDS)))
    )
    cfg.WEBHOOK_DEDUP_FAIL_OPEN = _env_bool("WEBHOOK_DEDUP_FAIL_OPEN", default=False)
    cfg.DROPBOX_APP_SECRET = os.environ.get(
        "DROPBOX_APP_SECRET", _TEST_ENV_DEFAULTS["DROPBOX_APP_SECRET"]
    )
    cfg.GRAPH_CLIENT_STATE = os.environ.get(
        "GRAPH_CLIENT_STATE", _TEST_ENV_DEFAULTS["GRAPH_CLIENT_STATE"]
    )
    cfg.DRIVE_CHANNEL_TOKEN = os.environ.get(
        "DRIVE_CHANNEL_TOKEN", _TEST_ENV_DEFAULTS["DRIVE_CHANNEL_TOKEN"]
    )
    cfg.NCE_WEBHOOK_TRUST_PROXY = _env_bool("NCE_WEBHOOK_TRUST_PROXY", default=False)


def _ensure_nce_package_loaded() -> None:
    """Re-import ``nce`` after ``test_init_public_api`` purges ``sys.modules``."""
    import importlib
    import sys

    if "nce" in sys.modules:
        return
    importlib.import_module("nce")


def _restore_nce_temporal_datetime() -> None:
    """Undo tests that monkeypatch ``nce.temporal.datetime`` with a fixed clock."""
    import datetime as std_datetime

    import nce.temporal as temporal_mod

    temporal_mod.datetime = std_datetime.datetime


def _reset_governance_cache_initialized_empty() -> None:
    """Reset the governance singleton to an INITIALIZED-EMPTY snapshot.

    Batch 100: ``GOVERNANCE`` is a module singleton shared across the suite.
    Unrelated dispatch tests must run in the deterministic ALLOW path — that
    means an INITIALIZED (fetched-at=now) but EMPTY (nothing disabled) snapshot,
    NOT the never-initialized state (which fails closed in prod). Tests that
    exercise governance directly override this in-test.
    """
    import time

    try:
        from nce.tool_governance import GOVERNANCE
    except Exception:
        return
    GOVERNANCE._snapshot = frozenset()
    GOVERNANCE._fetched_at = time.monotonic()


@pytest.fixture(autouse=True)
def _reset_nce_cfg_singleton_after_test() -> None:
    """Prevent order-dependent failures when tests patch ``nce.config.cfg``."""
    _restore_nce_cfg_from_env()
    _restore_nce_temporal_datetime()
    _reset_governance_cache_initialized_empty()
    yield
    _restore_nce_cfg_from_env()
    _restore_nce_temporal_datetime()
    _reset_governance_cache_initialized_empty()


def pytest_runtest_teardown(item: pytest.Item) -> None:
    """``test_init_public_api`` purges ``nce`` from ``sys.modules`` — restore for teardown hooks."""
    if "test_init_public_api" in item.nodeid:
        _ensure_nce_package_loaded()
        _restore_nce_cfg_from_env()
        _restore_nce_temporal_datetime()


@pytest.fixture(autouse=True)
def _reset_admin_state_engine_after_test() -> None:
    """Handlers read ``nce.admin_state.engine``; do not leak mocks across tests."""
    import nce.admin_state as admin_state

    admin_state.engine = None
    try:
        import admin_server as adm

        adm.engine = None
    except Exception:
        pass
    yield
    admin_state.engine = None
    try:
        import admin_server as adm

        adm.engine = None
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _reset_server_engine_after_test() -> None:
    """``server.call_tool`` uses module-level ``server.engine``."""
    try:
        import server as srv
    except Exception:
        yield
        return

    original = srv.engine
    yield
    srv.engine = original


def _integration_pool_dsn() -> str | None:
    """DSN used by ``pg_pool`` (mutations + ``append_event`` integration tests).

    Operators may point CI at an isolated database via ``NCE_INTEGRATION_PG_DSN``.
    Defaults to twelve-factor aliases so ``PG_DSN`` / ``DATABASE_URL`` work.
    """

    raw = (
        os.getenv("NCE_INTEGRATION_PG_DSN")
        or os.getenv("PG_DSN")
        or os.getenv("DATABASE_URL")
        or ""
    ).strip()
    return raw or None


@pytest.fixture(autouse=True)
def _reset_signing_key_cache_after_test(request: pytest.FixtureRequest) -> None:
    """Reset the signing key module-level cache after each test if isolated.

    Prevents test-order dependencies by clearing ``_key_cache`` so each
    test starts with a fresh signing state.  Uses ``yield`` to run after
    the test body (teardown semantics).  Safe under ``pytest-xdist``
    because each worker has its own module namespace.
    """
    yield
    if request.node.get_closest_marker("signing_isolation") is not None:
        try:
            import nce.signing as signing_mod

            # _key_cache is a _SigningKeyCache(TTLCache) — clear() removes all
            # entries and __delitem__ zeros their MutableKeyBuffer.
            signing_mod._key_cache.clear()
        except Exception:
            return


# ---------------------------------------------------------------------------
# Integration Postgres (asyncpg pool + namespaces)
# Used by pytest.mark.integration tests; skips when Postgres is unreachable.
# ---------------------------------------------------------------------------


def _refresh_signing_when_decrypt_fails() -> bool:
    """When true, rotate signing keys if ``NCE_MASTER_KEY`` cannot decrypt the active blob."""

    return os.getenv("NCE_INTEGRATION_REFRESH_SIGNING_ON_DECRYPT_FAIL", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


async def _require_append_event_schema(pool: asyncpg.Pool) -> None:
    """``append_event`` / Merkle integration requires current ``event_log`` columns."""

    async with pool.acquire() as conn:
        ok = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM   information_schema.columns
                WHERE  table_schema = 'public'
                  AND  table_name = 'event_log'
                  AND  column_name = 'chain_hash'
            )
            """
        )
    if not ok:
        pytest.skip(
            "Postgres schema is missing public.event_log.chain_hash — "
            "apply the current nce/schema.sql before integration tests.",
        )


async def _ensure_active_signing_key(pool: asyncpg.Pool) -> None:
    """Ensure ``get_active_key`` succeeds (rotate when empty / optionally on decrypt mismatch)."""

    from nce.signing import (
        NoActiveSigningKeyError,
        SigningKeyDecryptionError,
        get_active_key,
        rotate_key,
    )

    async with pool.acquire() as conn:
        try:
            await get_active_key(conn)
            return
        except NoActiveSigningKeyError:
            await rotate_key(conn)
            return
        except SigningKeyDecryptionError:
            if _refresh_signing_when_decrypt_fails():
                await rotate_key(conn)
                return
            pytest.skip(
                "NCE_MASTER_KEY does not decrypt signing_keys in this database. "
                "Use the deployment master key or set "
                "NCE_INTEGRATION_REFRESH_SIGNING_ON_DECRYPT_FAIL=1 "
                "(rotates active signing keys — use only on disposable databases).",
            )


@pytest_asyncio.fixture
async def pg_pool() -> AsyncGenerator[asyncpg.Pool, None]:
    dsn = _integration_pool_dsn()
    if not dsn:
        pytest.skip(
            "Integration tests need NCE_INTEGRATION_PG_DSN, PG_DSN, or DATABASE_URL",
        )
    try:
        pool = await asyncpg.create_pool(
            dsn,
            min_size=1,
            max_size=6,
            command_timeout=60,
        )
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"Postgres not reachable for integration tests: {exc}")

    try:
        await _require_append_event_schema(pool)
        await _ensure_active_signing_key(pool)
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def pg_admin_conn(pg_pool: asyncpg.Pool) -> AsyncGenerator[asyncpg.Connection, None]:
    """Single connection with the same role as ``pg_pool`` (compose default: ``mcp_user``)."""

    async with pg_pool.acquire() as conn:
        yield conn


@pytest_asyncio.fixture
async def pg_app_conn(
    pg_pool: asyncpg.Pool,
) -> AsyncGenerator[asyncpg.Connection, None]:
    """Connection for catalog / WORM privilege probes.

    When ``PG_DSN_APP`` is set to a different DSN than the integration pool,
    checkout uses that role only. Otherwise reuses ``pg_pool`` — owner roles
    may pass ``UPDATE … WHERE FALSE``; those tests skip.
    """

    app_dsn = os.getenv("PG_DSN_APP", "").strip()
    primary = _integration_pool_dsn() or ""

    if not app_dsn or app_dsn == primary:
        from urllib.parse import urlparse, urlunparse

        from nce.config import cfg

        try:
            parsed = urlparse(primary)
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            app_pass = cfg.NCE_APP_PASSWORD or "nce_app_secret"
            netloc = f"nce_app:{app_pass}@{netloc}"
            app_dsn = urlunparse(parsed._replace(netloc=netloc))
        except Exception:
            async with pg_pool.acquire() as conn:
                yield conn
            return

    try:
        app_pool = await asyncpg.create_pool(
            app_dsn,
            min_size=1,
            max_size=2,
            command_timeout=60,
        )
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"PG_DSN_APP not reachable: {exc}")
    try:
        async with app_pool.acquire() as conn:
            yield conn
    finally:
        await app_pool.close()


async def _insert_namespace(pool: asyncpg.Pool) -> uuid.UUID:
    slug = f"pytest-ns-{uuid.uuid4().hex}"
    async with pool.acquire() as conn:
        ns = await conn.fetchval(
            "INSERT INTO namespaces (slug) VALUES ($1) RETURNING id",
            slug,
        )
    assert ns is not None
    return ns


async def _drop_namespaces(pool: asyncpg.Pool, ids: list[uuid.UUID]) -> None:
    """Delete test namespaces, tolerating the ones that cannot go.

    Migration 055 made every tenant-scoped child FK ON DELETE CASCADE, so this
    removes the namespace and its rows in one statement. ``event_log`` and
    ``event_parents`` are deliberately still NO ACTION (they are WORM), so a
    namespace that recorded events raises ForeignKeyViolationError -- that is
    expected, not a teardown bug, and must never fail a passing test.
    """

    for ns in ids:
        try:
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM namespaces WHERE id = $1", ns)
        except (asyncpg.PostgresError, OSError):
            # Best-effort: a WORM-pinned or already-gone namespace is fine.
            pass


@pytest_asyncio.fixture
async def namespace_id(pg_pool: asyncpg.Pool) -> AsyncGenerator[uuid.UUID, None]:
    """Fresh namespace row for integration tests that need RLS / event_log scope.

    Yields rather than returns so the row is removed afterwards. Before this had
    teardown, every test run leaked a namespace permanently: the live database
    had accumulated 4,613 ``pytest-ns-*`` rows against 7 real tenants, and the
    boot-time ownership backfill was fanning out over all of them.
    """

    ns = await _insert_namespace(pg_pool)
    try:
        yield ns
    finally:
        await _drop_namespaces(pg_pool, [ns])


@pytest_asyncio.fixture
async def make_namespace(pg_pool: asyncpg.Pool):
    """Factory that inserts a new namespace and returns its id.

    Every namespace it hands out is removed when the test ends -- see
    :func:`_drop_namespaces` for why some legitimately survive.
    """

    created: list[uuid.UUID] = []

    async def _make() -> uuid.UUID:
        ns = await _insert_namespace(pg_pool)
        created.append(ns)
        return ns

    try:
        yield _make
    finally:
        await _drop_namespaces(pg_pool, created)


@pytest.fixture(scope="session", autouse=True)
def _sweep_leaked_test_namespaces() -> Generator[None, None, None]:
    """Remove ``pytest-%`` namespaces this session created but did not tear down.

    Per-test teardown handles the normal path; this catches what it cannot --
    a hard kill, a crashed worker, a test that inserts a namespace without going
    through the fixtures. Without it the leak is unbounded: the live database
    had 4,613 ``pytest-ns-*`` rows against 7 real tenants, and boot-time seeding
    fanned out over every one of them.

    Only namespaces absent at session start are swept, so a concurrent session's
    rows are never touched. Failures are swallowed: a namespace holding WORM
    ``event_log`` rows cannot be deleted, and that must not fail a green run.
    """

    dsn = _integration_pool_dsn()
    if not dsn:
        yield
        return

    async def _slugs() -> set[str]:
        conn = None
        try:
            conn = await asyncpg.connect(dsn, timeout=10)
            rows = await conn.fetch("SELECT slug FROM namespaces WHERE slug LIKE 'pytest-%'")
            return {r["slug"] for r in rows}
        except (asyncpg.PostgresError, OSError):
            return set()
        finally:
            if conn is not None:
                await conn.close()

    async def _sweep(preexisting: set[str]) -> None:
        conn = None
        try:
            conn = await asyncpg.connect(dsn, timeout=10)
            rows = await conn.fetch("SELECT id, slug FROM namespaces WHERE slug LIKE 'pytest-%'")
            for row in rows:
                if row["slug"] in preexisting:
                    continue
                try:
                    await conn.execute("DELETE FROM namespaces WHERE id = $1", row["id"])
                except (asyncpg.PostgresError, OSError):
                    continue
        except (asyncpg.PostgresError, OSError):
            return
        finally:
            if conn is not None:
                await conn.close()

    try:
        before = asyncio.run(_slugs())
    except RuntimeError:
        before = set()

    try:
        yield
    finally:
        try:
            asyncio.run(_sweep(before))
        except RuntimeError:
            pass


@pytest.fixture(autouse=True)
def _mock_embeddings_globally():
    """Mock the embedding model load to return None, forcing fallback vectors and avoiding slow imports/downloads."""
    from unittest.mock import patch

    with patch("nce.embeddings._load_sentence_transformer", return_value=None):
        yield
