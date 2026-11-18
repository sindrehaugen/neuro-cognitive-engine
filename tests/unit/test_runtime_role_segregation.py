"""Unit tests for three-role runtime PostgreSQL privilege separation."""

from __future__ import annotations

import importlib.util
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.admin_handlers._shared import fleet_pg_pool
from nce.config import cfg
from nce.orchestrator import NCEEngine

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "bootstrap-compose-secrets.py"


class _DummyPool:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.closed = False
        self.terminated = False

    @asynccontextmanager
    async def acquire(self, timeout: float = 10.0):
        yield MagicMock()

    async def close(self) -> None:
        self.closed = True

    def terminate(self) -> None:
        self.terminated = True


@pytest.mark.asyncio
async def test_connect_uses_transient_migration_pool_and_closes_before_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_dsn = "postgresql://nce_app:app_secret@postgres:5432/memory_meta"
    mig_dsn = "postgresql://mcp_user:mcp_password@postgres:5432/memory_meta"

    monkeypatch.setattr(cfg, "PG_DSN", app_dsn)
    monkeypatch.setattr(cfg, "DB_READ_URL", app_dsn)
    monkeypatch.setattr(cfg, "NCE_MIGRATION_PG_DSN", mig_dsn)
    monkeypatch.setattr(cfg, "NCE_ENABLE_FLEET_POOL", False)

    created_pools: list[tuple[str, _DummyPool]] = []
    seen_ddl_pools: list[str] = []
    seen_runtime_pools: list[str] = []

    async def _fake_create_pool(dsn: str, **kwargs):
        pool = _DummyPool(dsn)
        created_pools.append((dsn, pool))
        return pool

    engine = NCEEngine()

    async def _rec_init() -> None:
        seen_ddl_pools.append(engine._ddl_pool.dsn)

    async def _rec_migs() -> None:
        seen_ddl_pools.append(engine._ddl_pool.dsn)

    async def _rec_plugin_migs() -> None:
        seen_ddl_pools.append(engine._ddl_pool.dsn)

    async def _rec_seed() -> None:
        seen_ddl_pools.append(engine._ddl_pool.dsn)

    async def _rec_schema_ver() -> None:
        seen_runtime_pools.append(engine.pg_pool.dsn)

    async def _noop() -> None:
        return None

    monkeypatch.setattr(engine, "_init_pg_schema", _rec_init)
    monkeypatch.setattr(engine, "_apply_pg_migrations", _rec_migs)
    monkeypatch.setattr(engine, "_apply_plugin_pg_migrations", _rec_plugin_migs)
    monkeypatch.setattr(engine, "_verify_schema_version", _rec_schema_ver)
    monkeypatch.setattr(engine, "_ensure_initial_signing_key", _noop)
    monkeypatch.setattr(engine, "_verify_master_key_matches_data", _noop)
    monkeypatch.setattr(engine, "_verify_worm_enforcement", _noop)
    monkeypatch.setattr(engine, "_verify_rls_enforcement", _noop)
    monkeypatch.setattr(engine, "_seed_node_ownership_all", _rec_seed)
    monkeypatch.setattr(engine, "_check_global_legacy_warning", _noop)
    monkeypatch.setattr(engine, "_init_mongo_indexes", _noop)
    monkeypatch.setattr(engine, "_init_minio_buckets", lambda: None)

    fake_redis_async = MagicMock()
    fake_redis_async.aclose = AsyncMock()
    fake_redis_sync = MagicMock()

    with (
        patch.object(cfg, "validate", return_value=None),
        patch("nce.orchestrator.AsyncIOMotorClient", return_value=MagicMock()),
        patch("nce.orchestrator.asyncpg.create_pool", side_effect=_fake_create_pool),
        patch("nce._client_factory.make_async_redis", return_value=fake_redis_async),
        patch("nce._client_factory.make_sync_redis", return_value=fake_redis_sync),
        patch("nce._client_factory.make_minio", return_value=MagicMock()),
    ):
        await engine.connect(run_startup_checks=True)

    assert [d for d, _ in created_pools] == [mig_dsn, app_dsn]
    mig_pool = created_pools[0][1]
    app_pool = created_pools[1][1]
    assert seen_ddl_pools == [mig_dsn, mig_dsn, mig_dsn, mig_dsn]
    assert seen_runtime_pools == [app_dsn]
    assert mig_pool.closed is True
    assert app_pool.closed is False
    assert engine._startup_migration_pool is None
    assert engine.pg_pool is app_pool
    assert engine.pg_fleet_pool is None


@pytest.mark.asyncio
async def test_connect_opens_and_disconnects_fleet_pool_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_dsn = "postgresql://nce_app:app_secret@postgres:5432/memory_meta"
    gc_dsn = "postgresql://nce_gc:gc_secret@postgres:5432/memory_meta"

    monkeypatch.setattr(cfg, "PG_DSN", app_dsn)
    monkeypatch.setattr(cfg, "DB_READ_URL", app_dsn)
    monkeypatch.setattr(cfg, "NCE_GC_DSN", gc_dsn)
    monkeypatch.setattr(cfg, "NCE_MIGRATION_PG_DSN", "")
    monkeypatch.setattr(cfg, "NCE_ENABLE_FLEET_POOL", True)

    created_pools: list[tuple[str, _DummyPool]] = []

    async def _fake_create_pool(dsn: str, **kwargs):
        pool = _DummyPool(dsn)
        created_pools.append((dsn, pool))
        return pool

    fake_redis_async = MagicMock()
    fake_redis_async.aclose = AsyncMock()
    fake_redis_sync = MagicMock()

    engine = NCEEngine()
    with (
        patch.object(cfg, "validate", return_value=None),
        patch("nce.orchestrator.AsyncIOMotorClient", return_value=MagicMock()),
        patch("nce.orchestrator.asyncpg.create_pool", side_effect=_fake_create_pool),
        patch("nce._client_factory.make_async_redis", return_value=fake_redis_async),
        patch("nce._client_factory.make_sync_redis", return_value=fake_redis_sync),
        patch("nce._client_factory.make_minio", return_value=MagicMock()),
    ):
        await engine.connect(run_startup_checks=False)

    assert [d for d, _ in created_pools] == [app_dsn, gc_dsn]
    app_pool, fleet_pool = created_pools[0][1], created_pools[1][1]
    assert engine.pg_pool is app_pool
    assert engine.pg_fleet_pool is fleet_pool
    assert fleet_pg_pool(engine) is fleet_pool

    await engine.disconnect()
    assert app_pool.closed is True
    assert fleet_pool.closed is True
    assert engine.pg_fleet_pool is None


def test_fleet_pg_pool_helper_fallback_and_mock_safety() -> None:
    primary = MagicMock(name="primary_pool")
    fleet = MagicMock(name="fleet_pool")

    assert fleet_pg_pool(SimpleNamespace(pg_pool=primary)) is primary
    assert fleet_pg_pool(SimpleNamespace(pg_pool=primary, pg_fleet_pool=None)) is primary
    assert fleet_pg_pool(SimpleNamespace(pg_pool=primary, pg_fleet_pool=fleet)) is fleet

    mock_engine = MagicMock()
    mock_engine.pg_pool = primary
    assert fleet_pg_pool(mock_engine) is primary


@pytest.mark.asyncio
async def test_cron_connects_with_resolved_worker_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    import nce.cron as cron

    gc_dsn = "postgresql://nce_gc:gc_secret@postgres:5432/memory_meta"
    captured: dict[str, object] = {}

    class _StopAfterPool(RuntimeError):
        pass

    async def _fake_create_pool(dsn: str, **kwargs):
        captured["dsn"] = dsn
        raise _StopAfterPool("stop after pool connect")

    monkeypatch.setattr(cron, "resolve_worker_dsn", lambda: gc_dsn)
    monkeypatch.setattr(cron.cfg, "validate", lambda: None)
    monkeypatch.setattr(cron.cfg, "CRON_STARTUP_JITTER_MAX_SECONDS", 0.0)
    monkeypatch.setattr(cron.asyncpg, "create_pool", _fake_create_pool)

    with pytest.raises(_StopAfterPool):
        await cron.async_main()

    assert captured["dsn"] == gc_dsn


def test_bootstrap_secrets_generates_gc_password_and_syncs_dotenv(tmp_path: Path) -> None:
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "compose.stack.env").write_text(
        "\n".join(
            [
                "NCE_MASTER_KEY=dev-master-key",
                "NCE_API_KEY=dev-api-key",
                "NCE_ADMIN_API_KEY=dev-admin-key",
                "NCE_MCP_API_KEY=dev-mcp-key",
                "NCE_JWT_SECRET=dev",
                "NCE_ADMIN_PASSWORD=dev",
                "NCE_APP_PASSWORD=nce_app_secret",
                "NCE_GC_PASSWORD=nce_gc_secret",
                "DROPBOX_APP_SECRET=dev-x",
                "GRAPH_CLIENT_STATE=dev-x",
                "DRIVE_CHANNEL_TOKEN=dev-x",
                "",
            ]
        ),
        encoding="utf-8",
    )

    spec = importlib.util.spec_from_file_location(f"_bootstrap_rt_{tmp_path.name}", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    mod.ROOT = tmp_path
    mod.BASE_ENV = tmp_path / "deploy" / "compose.stack.env"
    mod.EXAMPLE_ENV = tmp_path / "deploy" / "compose.stack.env.example"
    mod.GENERATED = tmp_path / "deploy" / "compose.stack.env.generated"
    mod.SECRETS_DIR = tmp_path / "deploy" / "secrets"

    mod.main()
    gen_vals = mod._parse_env_text(mod.GENERATED.read_text(encoding="utf-8"))
    dotenv_vals = mod._parse_env_text((tmp_path / ".env").read_text(encoding="utf-8"))

    assert gen_vals["NCE_APP_PASSWORD"] != "nce_app_secret"
    assert gen_vals["NCE_GC_PASSWORD"] != "nce_gc_secret"
    assert dotenv_vals["NCE_APP_PASSWORD"] == gen_vals["NCE_APP_PASSWORD"]
    assert dotenv_vals["NCE_GC_PASSWORD"] == gen_vals["NCE_GC_PASSWORD"]

    before_gen = mod.GENERATED.read_text(encoding="utf-8")
    before_env = (tmp_path / ".env").read_text(encoding="utf-8")
    mod.main()
    assert mod.GENERATED.read_text(encoding="utf-8") == before_gen
    assert (tmp_path / ".env").read_text(encoding="utf-8") == before_env


@pytest.mark.asyncio
async def test_init_pg_schema_refreshes_nce_gc_special_character_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify customized NCE_GC_PASSWORD is bound via set_config in _init_pg_schema."""

    class _FakeConn:
        def __init__(self) -> None:
            self.execute = AsyncMock()
            self.fetchval = AsyncMock(return_value=None)

        @asynccontextmanager
        async def transaction(self):
            yield self

    class _FakePool:
        def __init__(self, conn: _FakeConn) -> None:
            self._conn = conn

        @asynccontextmanager
        async def acquire(self, timeout: float = 10.0):
            yield self._conn

    mock_conn = _FakeConn()
    engine = NCEEngine()
    engine.pg_pool = _FakePool(mock_conn)

    special_gc_password = r"gc$'special\'pass''$$\\"
    monkeypatch.setattr(cfg, "NCE_APP_PASSWORD", "")
    monkeypatch.setattr(cfg, "NCE_GC_PASSWORD", special_gc_password)

    with patch("pathlib.Path.read_text", return_value="-- mock schema ddl"):
        await engine._init_pg_schema()

    calls = mock_conn.execute.call_args_list
    assert len(calls) == 6, [c[0][0] for c in calls]

    locks = [c for c in calls if "pg_advisory_xact_lock" in c[0][0]]
    assert len(locks) == 2

    (set_config_call,) = [c for c in calls if "set_config" in c[0][0]]
    assert set_config_call[0] == (
        "SELECT set_config('nce.temp_gc_password', $1, true)",
        special_gc_password,
    )

    (do_block_call,) = [c for c in calls if "DO $$" in c[0][0]]
    assert (
        "ALTER ROLE nce_gc WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS PASSWORD %L"
        in do_block_call[0][0]
    )
    assert "current_setting('nce.temp_gc_password')" in do_block_call[0][0]
