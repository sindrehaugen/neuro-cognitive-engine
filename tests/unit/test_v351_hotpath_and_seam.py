"""Contract tests for v3.5.1 hot-path optimizations, cross-repo migration seam, and drift coverage."""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


def test_cross_repo_migration_037_038_ledger_disjointness() -> None:
    """Core 037/038 and nce_verticals:037_bom_line_priced.sql use disjoint ledger keys."""
    from nce.orchestrator import NCEEngine

    mig_dir = _REPO_ROOT / "nce" / "migrations"
    core_037 = mig_dir / "037_perf_hot_path_indexes.sql"
    core_038 = mig_dir / "038_v351_hotpath_rls_and_indexes.sql"
    assert core_037.is_file()
    assert core_038.is_file()
    assert core_038.read_text(encoding="utf-8").splitlines()[0] == (
        "-- 038_v351_hotpath_rls_and_indexes.sql"
    )

    core_key = core_037.name
    plugin_key = "nce_verticals:037_bom_line_priced.sql"
    assert core_key != plugin_key
    assert ":" not in core_key
    assert ":" in plugin_key

    # Verify _verify_schema_version ignores plugin-prefixed rows (`':' not in k`)
    src = inspect.getsource(NCEEngine._verify_schema_version)
    assert "not in k" in src and ":" in src

    # Verify _apply_plugin_pg_migrations prefixes ledger entries with plugin_scope:
    plugin_src = inspect.getsource(NCEEngine._apply_plugin_pg_migrations)
    assert 'f"{plugin_scope}:{p_path.name}"' in plugin_src


def test_check_schema_drift_reconciles_33_plus_10_core_tables() -> None:
    """schema.sql (41 tables, incl. 041 app_connections + 042 Day-2 Ops) + Core migrations (10) = 51 SQL tables."""
    from check_schema_drift import expected_columns, expected_migration_columns

    schema_tables = set(expected_columns())
    mig_tables = set(expected_migration_columns())
    mig_only = mig_tables - schema_tables
    total_core = schema_tables | mig_tables

    assert len(schema_tables) == 41
    assert len(mig_only) == 10
    assert len(total_core) == 51


def test_dek_unwrap_cache_prewarms_and_zeroes_on_clear() -> None:
    """wrap_dek pre-warms _UNWRAPPED_DEK_CACHE and clear_unwrapped_dek_cache zeroes buffers."""
    from nce import envelope
    from nce.signing import require_master_key

    envelope.clear_unwrapped_dek_cache()
    ct, wrapped, _kid = envelope.encrypt_raw_data("hotpath secret episode payload")

    assert len(envelope._UNWRAPPED_DEK_CACHE) == 1
    cached_buf, _exp = next(iter(envelope._UNWRAPPED_DEK_CACHE.values()))
    assert isinstance(cached_buf, bytearray)
    assert any(b != 0 for b in cached_buf)

    # maybe_decrypt_raw_data hits cache without calling unwrap_dek
    with patch.object(envelope, "unwrap_dek", wraps=envelope.unwrap_dek) as spy_unwrap:
        pt = envelope.maybe_decrypt_raw_data(ct, wrapped)
        assert pt == "hotpath secret episode payload"
        spy_unwrap.assert_not_called()

    # Wrong master key fingerprint or tampered wrapped_dek misses cache and fails closed
    tampered = bytes(wrapped[:-1]) + bytes([wrapped[-1] ^ 0xFF])
    with pytest.raises(Exception):
        envelope.maybe_decrypt_raw_data(ct, tampered)

    # Clearing cache zeroes the underlying bytearray in place
    envelope.clear_unwrapped_dek_cache()
    assert len(envelope._UNWRAPPED_DEK_CACHE) == 0
    assert all(b == 0 for b in cached_buf)

    # Subsequent read unwraps via master_key and re-populates cache
    with require_master_key():
        pt2 = envelope.maybe_decrypt_raw_data(ct, wrapped)
        assert pt2 == "hotpath secret episode payload"
    envelope.clear_unwrapped_dek_cache()


@pytest.mark.asyncio
async def test_query_embedding_ttl_lru_cache_hits_on_repeat() -> None:
    """embed(text) caches normalized query embeddings and clears on reset."""
    from nce import embeddings

    embeddings.reset_backend_singleton_for_tests()
    fake_backend = MagicMock()
    fake_backend.backend_id = "test_backend"
    fake_backend.model_id = "test_model"
    fake_vec = [0.125] * embeddings.VECTOR_DIM
    fake_backend.embed = AsyncMock(return_value=[fake_vec])

    with patch.object(embeddings, "get_backend", return_value=fake_backend):
        v1 = await embeddings.embed("  quarterly cloud architecture  ")
        v2 = await embeddings.embed("quarterly cloud architecture")
        assert v1 == fake_vec
        assert v2 == fake_vec
        assert fake_backend.embed.await_count == 1

    embeddings.reset_backend_singleton_for_tests()
    assert len(embeddings._embed_query_cache) == 0


@pytest.mark.asyncio
async def test_scoped_pg_session_combines_set_config_when_hnsw_supported() -> None:
    """Once _HNSW_ITERATIVE_SCAN_SUPPORTED is True, scoped_pg_session issues 1 combined set_config."""
    from nce import db_utils

    executed: list[tuple[str, tuple[object, ...]]] = []

    class _FakeTx:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *args: object) -> None:
            return None

    class _FakeConn:
        def transaction(self) -> _FakeTx:
            return _FakeTx()

        async def execute(self, sql: str, *args: object) -> None:
            executed.append((sql, args))

    fake_conn = _FakeConn()
    fake_acq = MagicMock()
    fake_acq.__aenter__ = AsyncMock(return_value=fake_conn)
    fake_acq.__aexit__ = AsyncMock(return_value=None)

    fake_pool = MagicMock()
    fake_pool.acquire = MagicMock(return_value=fake_acq)

    prev = db_utils._HNSW_ITERATIVE_SCAN_SUPPORTED
    db_utils._HNSW_ITERATIVE_SCAN_SUPPORTED = True
    try:
        ns = uuid4()
        async with db_utils.scoped_pg_session(fake_pool, ns):
            pass
        assert len(executed) == 1
        sql_called, _ = executed[0]
        assert "set_config('nce.namespace_id', $1, true)" in sql_called
        assert "set_config('hnsw.iterative_scan', 'relaxed_order', true)" in sql_called
    finally:
        db_utils._HNSW_ITERATIVE_SCAN_SUPPORTED = prev


def test_cron_saga_recovery_uses_for_update_skip_locked() -> None:
    """_saga_recovery_tick uses FOR UPDATE SKIP LOCKED on both state queries."""
    from nce import cron

    src = inspect.getsource(cron._saga_recovery_tick)
    assert src.count("FOR UPDATE SKIP LOCKED") >= 2
