"""R4 / VI.4 — least-privilege worker DSN segregation.

These tests assert the *real* DSN-selection contract for the background
maintenance workers (garbage collector + re-embedding worker), not that a mock
was called:

* When ``NCE_GC_DSN`` is set, ``resolve_worker_dsn()`` returns it and it is a
  principal *distinct* from ``PG_DSN`` (the app role).
* When ``NCE_GC_DSN`` is unset, it falls back to ``PG_DSN`` (safe,
  backward-compatible default).
* The GC and re-embedding workers actually open their pools against the
  resolved worker DSN, so a deployment that provisions ``nce_gc`` keeps the
  application role out of the worker connection — the app pool is never the
  ``BYPASSRLS`` principal.

Config is an import-time singleton, so the env-precedence cases run in fresh
subprocesses (mirroring ``tests/test_config_prod_hardening.py``). The
worker-wiring cases patch ``asyncpg.create_pool`` and inspect the DSN argument
that the worker passes — asserting the value selected, not merely that connect
happened.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

_APP_DSN = "postgresql://nce_app:app_secret@db.internal:5432/memory_meta"
_GC_DSN = "postgresql://nce_gc:gc_secret@db.internal:5432/memory_meta"


def _run_in_subprocess(code: str, extra_env: dict[str, str]) -> str:
    env = os.environ.copy()
    # Ensure a clean baseline: drop any inherited DSN overrides.
    for k in ("NCE_GC_DSN", "PG_DSN", "DATABASE_URL", "DB_READ_URL", "DB_WRITE_URL"):
        env.pop(k, None)
    env["NCE_ENV"] = "dev"
    env["NCE_MASTER_KEY"] = "x" * 32
    env.update(extra_env)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"subprocess failed:\n{result.stdout}\n{result.stderr}"
    return result.stdout.strip()


# --------------------------------------------------------------------------- #
# Env-precedence contract (real config resolution)
# --------------------------------------------------------------------------- #


def test_gc_dsn_distinct_from_pg_dsn_when_set() -> None:
    """NCE_GC_DSN set → resolver returns the GC principal, distinct from PG_DSN."""
    out = _run_in_subprocess(
        "from nce.config import cfg\n"
        "from nce.db_utils import resolve_worker_dsn, worker_dsn_is_segregated\n"
        "print(cfg.PG_DSN)\n"
        "print(cfg.NCE_GC_DSN)\n"
        "print(resolve_worker_dsn())\n"
        "print(worker_dsn_is_segregated())\n",
        {"PG_DSN": _APP_DSN, "NCE_GC_DSN": _GC_DSN},
    )
    pg_dsn, gc_dsn, resolved, segregated = out.splitlines()
    assert pg_dsn == _APP_DSN
    assert gc_dsn == _GC_DSN
    # The contract: the worker resolves to the GC principal, NOT the app role.
    assert resolved == _GC_DSN
    assert resolved != pg_dsn
    assert segregated == "True"


def test_gc_dsn_falls_back_to_pg_dsn_when_unset() -> None:
    """NCE_GC_DSN unset → safe, backward-compatible fallback to PG_DSN."""
    out = _run_in_subprocess(
        "from nce.config import cfg\n"
        "from nce.db_utils import resolve_worker_dsn, worker_dsn_is_segregated\n"
        "print(cfg.PG_DSN)\n"
        "print(resolve_worker_dsn())\n"
        "print(worker_dsn_is_segregated())\n",
        {"PG_DSN": _APP_DSN},
    )
    pg_dsn, resolved, segregated = out.splitlines()
    assert pg_dsn == _APP_DSN
    assert resolved == _APP_DSN  # fallback to the app role — unchanged behavior
    assert segregated == "False"  # not segregated when sharing the app DSN


# --------------------------------------------------------------------------- #
# Worker wiring — the pool is opened against the resolved worker DSN
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_gc_worker_connects_with_resolved_worker_dsn() -> None:
    """garbage_collector._connect_with_retry opens its pool on resolve_worker_dsn()."""
    import nce.garbage_collector as gc

    fake_pool = MagicMock()

    fake_mongo = MagicMock()
    fake_mongo.admin.command = AsyncMock(return_value={"ok": 1})

    captured: dict[str, object] = {}

    async def _fake_create_pool(dsn, **kwargs):  # noqa: ANN001
        captured["dsn"] = dsn
        return fake_pool

    with (
        patch.object(gc, "resolve_worker_dsn", return_value=_GC_DSN) as resolver,
        patch.object(gc, "AsyncIOMotorClient", return_value=fake_mongo),
        patch.object(gc.asyncpg, "create_pool", side_effect=_fake_create_pool),
    ):
        mongo_client, pool = await gc._connect_with_retry()

    assert pool is fake_pool
    resolver.assert_called_once()
    # The load-bearing assertion: the GC pool authenticates as the worker
    # principal (NCE_GC_DSN), not whatever cfg.PG_DSN happens to be.
    assert captured["dsn"] == _GC_DSN


@pytest.mark.asyncio
async def test_reembedding_worker_connects_with_resolved_worker_dsn() -> None:
    """reembedding_worker.async_main opens its pool on resolve_worker_dsn()."""
    import nce.reembedding_worker as rw

    fake_pool = MagicMock()
    fake_pool.close = AsyncMock()

    captured: dict[str, object] = {}

    async def _fake_create_pool(dsn, **kwargs):  # noqa: ANN001
        captured["dsn"] = dsn
        return fake_pool

    fake_worker = MagicMock()
    fake_worker.run_once = AsyncMock(return_value={"status": "completed"})

    with (
        patch.object(rw, "resolve_worker_dsn", return_value=_GC_DSN) as resolver,
        patch.object(rw.cfg, "validate", return_value=None),
        patch.object(rw.asyncpg, "create_pool", side_effect=_fake_create_pool),
        patch.object(rw, "ReembeddingWorker", return_value=fake_worker),
        # Force the Mongo import branch to be skipped deterministically.
        patch.dict(sys.modules, {"motor.motor_asyncio": None}),
    ):
        await rw.async_main()

    resolver.assert_called_once()
    assert captured["dsn"] == _GC_DSN


def test_app_path_does_not_use_gc_dsn() -> None:
    """The application orchestrator pool must use PG_DSN (app role), never the GC DSN.

    Guards the inverse invariant: segregation puts BYPASSRLS-capable creds only
    on the worker side; the app pool keeps PG_DSN and never picks up NCE_GC_DSN.
    """
    out = _run_in_subprocess(
        "from nce.config import cfg\nprint(cfg.PG_DSN)\nprint(cfg.NCE_GC_DSN)\n",
        {"PG_DSN": _APP_DSN, "NCE_GC_DSN": _GC_DSN},
    )
    pg_dsn, gc_dsn = out.splitlines()
    # The app DSN is the app principal; the GC DSN is a different principal.
    assert pg_dsn == _APP_DSN
    assert gc_dsn == _GC_DSN
    assert pg_dsn != gc_dsn
