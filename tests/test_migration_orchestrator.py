"""
Comprehensive contract tests for MigrationOrchestrator hardening.

Covers input validation, Redis cache atomicity, migration state machine,
commit/abort consistency, validation quality gate, and error sanitization.
All external I/O (asyncpg, Redis async/sync) is mocked.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nce.models import IndexCodeFileRequest
from nce.orchestrators.migration import MigrationOrchestrator

# ── Helpers ────────────────────────────────────────────────────────────────


def _make_acquire_ctx(conn: AsyncMock) -> MagicMock:
    """Simulate ``async with pool.acquire() as conn:``."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _code_payload(
    *,
    filepath: str | None = None,
    raw_code: str = "def hello(): pass",
    **kwargs,
) -> IndexCodeFileRequest:
    return IndexCodeFileRequest(
        filepath=filepath or str(Path.cwd() / "sample.py"),
        raw_code=raw_code,
        language="python",
        **kwargs,
    )


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_pool() -> MagicMock:
    return MagicMock()


@pytest.fixture
def conn() -> AsyncMock:
    c = AsyncMock()
    tx = AsyncMock()
    tx.__aenter__.return_value = tx
    tx.__aexit__.return_value = False
    c.transaction = MagicMock(return_value=tx)
    return c


@pytest.fixture
def redis_client() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def redis_sync_client() -> MagicMock:
    return MagicMock()


@pytest.fixture
def orch(
    mock_pool: MagicMock, redis_client: AsyncMock, redis_sync_client: MagicMock
) -> MigrationOrchestrator:
    return MigrationOrchestrator(
        pg_pool=mock_pool,
        redis_client=redis_client,
        redis_sync_client=redis_sync_client,
    )


# ── GROUP A — Input hardening ───────────────────────────────────────────────


class TestInputHardening:
    @pytest.mark.asyncio
    async def test_a1_index_code_file_rejects_oversized_payload(
        self, orch: MigrationOrchestrator
    ) -> None:
        from nce.config import cfg

        payload = _code_payload(raw_code="x" * (cfg.NCE_MAX_CODE_INDEX_BYTES + 1))

        with pytest.raises(ValueError, match="Code payload too large"):
            await orch.index_code_file(payload)

    def test_a2_validate_path_rejects_traversal(self, orch: MigrationOrchestrator) -> None:
        with pytest.raises(ValueError, match="Path traversal detected"):
            orch._validate_path("../../../etc/passwd")

    def test_a3_validate_path_accepts_path_within_cwd(
        self, orch: MigrationOrchestrator, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        valid = str(tmp_path / "src" / "module.py")
        orch._validate_path(valid)

    @pytest.mark.asyncio
    async def test_a4_start_migration_rejects_non_uuid_target(
        self, orch: MigrationOrchestrator, mock_pool: MagicMock, conn: AsyncMock
    ) -> None:
        mock_pool.acquire.return_value = _make_acquire_ctx(conn)

        with pytest.raises(ValueError):
            await orch.start_migration("not-a-valid-uuid")

    @pytest.mark.asyncio
    async def test_a5_index_code_file_uses_sha256_hex_hash(
        self,
        orch: MigrationOrchestrator,
        redis_client: AsyncMock,
    ) -> None:
        import hashlib

        raw = "print('hash me')"
        payload = _code_payload(raw_code=raw)

        # Calculate correct SHA256 hex digest
        expected_hash = hashlib.sha256(raw.encode()).hexdigest()

        # If Redis returns this hash, it must skip
        redis_client.get.return_value = expected_hash.encode()
        result = await orch.index_code_file(payload)
        assert result["status"] == "skipped"
        redis_client.get.assert_called_once()


# ── GROUP B — Redis atomicity ───────────────────────────────────────────────


class TestRedisAtomicity:
    @pytest.mark.asyncio
    async def test_b1_cached_hash_match_returns_skipped(
        self,
        orch: MigrationOrchestrator,
        redis_client: AsyncMock,
    ) -> None:
        import hashlib

        raw = "unchanged code"
        payload = _code_payload(raw_code=raw)
        file_hash = hashlib.sha256(raw.encode()).hexdigest()
        redis_client.get.return_value = file_hash.encode()

        result = await orch.index_code_file(payload)

        assert result == {
            "status": "skipped",
            "reason": "unchanged",
            "filepath": payload.filepath,
        }

    @pytest.mark.asyncio
    async def test_b2_redis_get_timeout_proceeds_with_enqueue(
        self,
        orch: MigrationOrchestrator,
        redis_client: AsyncMock,
    ) -> None:
        payload = _code_payload()
        redis_client.set.return_value = True

        mock_queue = MagicMock()
        mock_queue.name = "batch_processing"
        mock_queue.enqueue.return_value = SimpleNamespace(id="index:job", is_finished=False)

        async def slow_get(*_args, **_kwargs):
            await asyncio.sleep(5)
            return None

        redis_client.get.side_effect = slow_get

        with patch(
            "nce.extractors.dispatch.get_priority_queue",
            return_value=mock_queue,
        ):
            result = await orch.index_code_file(payload)

        assert result["status"] == "enqueued"
        mock_queue.enqueue.assert_called_once()

    @pytest.mark.asyncio
    async def test_b3_enqueued_job_id_matches_cache_key(
        self,
        orch: MigrationOrchestrator,
        redis_client: AsyncMock,
    ) -> None:
        payload = _code_payload()
        redis_client.get.return_value = None
        redis_client.set.return_value = True

        cache_key = orch._redis_cache_key(payload.namespace_id, None, payload.filepath)
        import re

        raw_job_id = f"index:{cache_key}"
        expected_job_id = re.sub(r"[^a-zA-Z0-9_-]", "-", raw_job_id)

        mock_queue = MagicMock()
        mock_queue.name = "batch_processing"
        mock_queue.enqueue.return_value = SimpleNamespace(id=expected_job_id, is_finished=False)

        with patch(
            "nce.extractors.dispatch.get_priority_queue",
            return_value=mock_queue,
        ):
            result = await orch.index_code_file(payload)

        enqueue_kwargs = mock_queue.enqueue.call_args.kwargs
        assert enqueue_kwargs["job_id"] == expected_job_id
        assert result["job_id"] == expected_job_id

    @pytest.mark.asyncio
    async def test_b4_redis_set_not_called_during_enqueue(
        self,
        orch: MigrationOrchestrator,
        redis_client: AsyncMock,
    ) -> None:
        payload = _code_payload()
        redis_client.get.return_value = None

        mock_queue = MagicMock()
        mock_queue.name = "batch_processing"
        mock_queue.enqueue.return_value = SimpleNamespace(id="index:job", is_finished=False)

        with patch(
            "nce.extractors.dispatch.get_priority_queue",
            return_value=mock_queue,
        ):
            await orch.index_code_file(payload)

        mock_queue.enqueue.assert_called_once()
        redis_client.set.assert_not_called()


# ── GROUP C — State machine ─────────────────────────────────────────────────


class TestStateMachine:
    @pytest.mark.asyncio
    async def test_c1_abort_raises_when_committed(
        self, orch: MigrationOrchestrator, mock_pool: MagicMock, conn: AsyncMock
    ) -> None:
        migration_id = str(uuid4())
        conn.fetchrow.return_value = {
            "status": "committed",
            "target_model_id": uuid4(),
        }
        mock_pool.acquire.return_value = _make_acquire_ctx(conn)

        with pytest.raises(ValueError, match="Cannot abort migration"):
            await orch.abort_migration(migration_id)

    @pytest.mark.asyncio
    async def test_c2_abort_succeeds_when_running(
        self, orch: MigrationOrchestrator, mock_pool: MagicMock, conn: AsyncMock
    ) -> None:
        migration_id = str(uuid4())
        target_model_id = uuid4()
        conn.fetchrow.return_value = {
            "status": "running",
            "target_model_id": target_model_id,
        }
        mock_pool.acquire.return_value = _make_acquire_ctx(conn)

        result = await orch.abort_migration(migration_id)

        assert result == {"status": "aborted"}

    @pytest.mark.asyncio
    async def test_c3_abort_succeeds_when_validating(
        self, orch: MigrationOrchestrator, mock_pool: MagicMock, conn: AsyncMock
    ) -> None:
        migration_id = str(uuid4())
        target_model_id = uuid4()
        conn.fetchrow.return_value = {
            "status": "validating",
            "target_model_id": target_model_id,
        }
        mock_pool.acquire.return_value = _make_acquire_ctx(conn)

        result = await orch.abort_migration(migration_id)

        assert result == {"status": "aborted"}


# ── GROUP D — Commit/abort consistency ──────────────────────────────────────


class TestCommitAbortConsistency:
    @pytest.mark.asyncio
    async def test_d1_commit_uses_select_for_update_before_retire(
        self, orch: MigrationOrchestrator, mock_pool: MagicMock, conn: AsyncMock
    ) -> None:
        migration_id = str(uuid4())
        target_model_id = uuid4()
        conn.fetchrow.return_value = {
            "status": "validating",
            "target_model_id": target_model_id,
        }
        mock_pool.acquire.return_value = _make_acquire_ctx(conn)

        await orch.commit_migration(migration_id)

        execute_sql = [call.args[0] for call in conn.execute.call_args_list]
        for_update_idx = next(i for i, sql in enumerate(execute_sql) if "FOR UPDATE" in sql)
        retire_idx = next(i for i, sql in enumerate(execute_sql) if "retired" in sql.lower())
        assert for_update_idx < retire_idx

    @pytest.mark.asyncio
    async def test_d2_abort_sets_target_model_active_not_retired(
        self, orch: MigrationOrchestrator, mock_pool: MagicMock, conn: AsyncMock
    ) -> None:
        migration_id = str(uuid4())
        target_model_id = uuid4()
        conn.fetchrow.return_value = {
            "status": "running",
            "target_model_id": target_model_id,
        }
        mock_pool.acquire.return_value = _make_acquire_ctx(conn)

        await orch.abort_migration(migration_id)

        model_update_sql = conn.execute.call_args_list[0].args[0]
        assert "status = 'active'" in model_update_sql
        assert "retired" not in model_update_sql.lower()

    @pytest.mark.asyncio
    async def test_d3_commit_raises_when_not_validating(
        self, orch: MigrationOrchestrator, mock_pool: MagicMock, conn: AsyncMock
    ) -> None:
        migration_id = str(uuid4())
        conn.fetchrow.return_value = {
            "status": "running",
            "target_model_id": uuid4(),
        }
        mock_pool.acquire.return_value = _make_acquire_ctx(conn)

        with pytest.raises(ValueError, match="not ready to commit"):
            await orch.commit_migration(migration_id)


# ── GROUP E — Validation ──────────────────────────────────────────────────────


class TestValidation:
    @pytest.mark.asyncio
    async def test_e1_validate_failed_when_emb_count_lt_mem_count(
        self, orch: MigrationOrchestrator, mock_pool: MagicMock, conn: AsyncMock
    ) -> None:
        migration_id = str(uuid4())
        target_model = str(uuid4())
        conn.fetchrow.return_value = {
            "status": "validating",
            "target_model_id": target_model,
        }
        conn.fetchval.side_effect = [200, 150]
        mock_pool.acquire.return_value = _make_acquire_ctx(conn)

        result = await orch.validate_migration(migration_id)

        assert result["status"] == "failed"
        assert "Missing memory embeddings" in result["reason"]

    @pytest.mark.asyncio
    async def test_e2_validate_success_when_counts_match(
        self, orch: MigrationOrchestrator, mock_pool: MagicMock, conn: AsyncMock
    ) -> None:
        migration_id = str(uuid4())
        target_model = str(uuid4())
        conn.fetchrow.return_value = {
            "status": "validating",
            "target_model_id": target_model,
        }
        conn.fetchval.side_effect = [100, 100]
        mock_pool.acquire.return_value = _make_acquire_ctx(conn)

        result = await orch.validate_migration(migration_id)

        assert result["status"] == "success"
        assert result["message"] == "All memories and nodes have been embedded"

    @pytest.mark.asyncio
    async def test_e3_validate_queries_filter_non_null_embeddings(
        self, orch: MigrationOrchestrator, mock_pool: MagicMock, conn: AsyncMock
    ) -> None:
        migration_id = str(uuid4())
        target_model = str(uuid4())
        conn.fetchrow.return_value = {
            "status": "validating",
            "target_model_id": target_model,
        }
        conn.fetchval.side_effect = [10, 10]
        mock_pool.acquire.return_value = _make_acquire_ctx(conn)

        await orch.validate_migration(migration_id)

        mem_sql = conn.fetchval.call_args_list[0].args[0]
        emb_sql = conn.fetchval.call_args_list[1].args[0]
        assert "IS NOT NULL" in mem_sql
        assert "IS NOT NULL" in emb_sql


# ── GROUP F — Error sanitization ────────────────────────────────────────────


class TestErrorSanitization:
    @pytest.mark.asyncio
    async def test_f1_get_job_status_sanitizes_failed_job_error(
        self, orch: MigrationOrchestrator
    ) -> None:
        failed_job = MagicMock()
        failed_job.is_finished = False
        failed_job.is_failed = True
        failed_job.get_status.return_value = "failed"
        failed_job.result = None

        with patch(
            "nce.orchestrators.migration.asyncio.to_thread",
            new=AsyncMock(return_value=failed_job),
        ):
            with patch("rq.job.Job.fetch", return_value=failed_job):
                result = await orch.get_job_status("job-123")

        assert result["status"] == "failed"
        assert result["error"] == "job failed"
        assert "Traceback" not in str(result.get("error", ""))

    @pytest.mark.asyncio
    async def test_f2_get_job_status_missing_job_returns_not_found(
        self, orch: MigrationOrchestrator
    ) -> None:
        with patch(
            "nce.orchestrators.migration.asyncio.to_thread",
            new=AsyncMock(side_effect=Exception("No such job RedisKey")),
        ):
            result = await orch.get_job_status("missing-job")

        assert result["status"] == "not_found"
        assert result["error"] == "job not found"
        assert "RedisKey" not in result["error"]
