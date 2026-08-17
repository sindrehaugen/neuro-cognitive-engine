"""
Batch 108 — Embedding-migration safety gate hoisted to the ENGINE layer.

The gate (neighbor-overlap quality check + audited ``force`` escape + dimension
preflight) used to live only in the MCP handler, leaving the admin HTTP route
(``api_admin_embedding_migration_commit`` / ``..._start``) ungated — a real bypass:
anyone with the admin key could promote a bad model.  These tests prove the gate
now lives in ``NCEEngine.commit_migration`` / ``NCEEngine.start_migration`` (via
``nce.migration_gate``), so BOTH callers — the MCP handler AND the admin HTTP route —
are gated identically.

Pure-unit (no DB, no asyncpg integration).  All PG I/O is mocked; the
neighbor-overlap computation is the only thing patched on the gate module.

Scenarios:
  ENGINE (the chokepoint)
    1. Below-threshold score ⇒ commit_migration raises ValueError, score surfaced.
    2. Score == threshold / 1.0 ⇒ commit proceeds, only the normal audit event.
    3. force=true on failing gate ⇒ commit proceeds AND emits migration_commit_forced.
    4. Empty / degenerate sample ⇒ FAILS (not a vacuous 1.0 pass); first-time-setup
       (no active model) still passes.
    5. Dim mismatch ⇒ start_migration refused; matching / no-active-model proceed.
  MCP path
    6. handle_commit_migration / handle_start_migration delegate to the gated engine
       method (no longer keep their own gate copy).
  ADMIN HTTP path (the bypass that was closed)
    7. api_admin_embedding_migration_commit below-threshold ⇒ refused (409), engine
       never reaches the underlying orchestrator commit.
    8. api_admin_embedding_migration_commit force=true ⇒ proceeds + forced audit.
    9. api_admin_embedding_migration_start dim-mismatch ⇒ refused (409).
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import nce.migration_gate as gate
from nce import admin_state
from nce.admin_handlers import fleet
from nce.orchestrator import NCEEngine
from nce.reembedding_migration import GateSampleTooSmall, compute_neighbor_overlap

# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

_VALID_MIG_ID = str(uuid.uuid4())
_VALID_MODEL_ID = str(uuid.uuid4())


def _bare_handler(handler: Any) -> Any:
    """Unwrap @mcp_handler / @require_scope decorators to reach the async fn."""
    fn = handler
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _pool_with_acquire(conn: Any) -> MagicMock:
    """Build a mock asyncpg Pool whose .acquire() is a reusable async context manager."""
    pool = MagicMock()

    @asynccontextmanager
    async def _acquire_ctx(*_args: Any, **_kwargs: Any):
        yield conn

    pool.acquire = MagicMock(side_effect=_acquire_ctx)
    return pool


def _make_engine(*, pg_pool: Any | None = None) -> NCEEngine:
    """A real NCEEngine with a mocked migration orchestrator + pool.

    Using a real engine (not a MagicMock) is the point: it proves the gate fires
    inside the genuine ``NCEEngine.commit_migration`` / ``start_migration`` code
    path that every caller — MCP and HTTP — goes through.
    """
    engine = NCEEngine()
    engine.pg_pool = pg_pool if pg_pool is not None else MagicMock()
    engine.migration = MagicMock()
    engine.migration.commit_migration = AsyncMock(return_value={"status": "committed"})
    engine.migration.start_migration = AsyncMock(return_value={"migration_id": _VALID_MIG_ID})
    # Skip lazy-init: the orchestrator is already wired above.
    engine._ensure_migration = AsyncMock(return_value=None)
    return engine


def _set_gate_cfg(monkeypatch: pytest.MonkeyPatch, *, threshold: float) -> None:
    monkeypatch.setattr(gate.cfg, "NCE_REEMBED_GATE_SAMPLE", 10, raising=False)
    monkeypatch.setattr(gate.cfg, "NCE_REEMBED_GATE_K", 5, raising=False)
    monkeypatch.setattr(gate.cfg, "NCE_REEMBED_GATE_MIN_OVERLAP", threshold, raising=False)


@asynccontextmanager
async def _patched_overlap(score: float | None = None, *, raises: Exception | None = None):
    """Patch the gate's compute_neighbor_overlap and capture audit events.

    Yields the list of (event_type, extra_params) audit calls recorded.
    """
    audit_calls: list[dict[str, Any]] = []

    async def _capture_audit(
        pool: Any, *, event_type: str, extra_params: Any = None, **kwargs: Any
    ) -> None:
        audit_calls.append({"event_type": event_type, "extra_params": extra_params})

    if raises is not None:
        overlap = AsyncMock(side_effect=raises)
    else:
        overlap = AsyncMock(return_value=score)

    with patch.object(gate, "compute_neighbor_overlap", new=overlap):
        with patch.object(gate, "audit_migration_action", side_effect=_capture_audit):
            yield audit_calls


# ===========================================================================
# Engine-layer gate — the SINGLE chokepoint
# ===========================================================================


class TestEngineCommitGate:
    @pytest.mark.asyncio
    async def test_below_threshold_raises_with_score(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Score below threshold ⇒ ValueError with both score and threshold surfaced."""
        _set_gate_cfg(monkeypatch, threshold=0.6)
        engine = _make_engine()

        async with _patched_overlap(0.30):
            with pytest.raises(ValueError) as exc_info:
                await engine.commit_migration(_VALID_MIG_ID)

        msg = str(exc_info.value)
        assert "0.3000" in msg
        assert "0.6000" in msg
        engine.migration.commit_migration.assert_not_called()

    @pytest.mark.asyncio
    async def test_score_at_one_commits_only_normal_audit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Perfect overlap ⇒ commit proceeds, only migration_commit_requested audit."""
        _set_gate_cfg(monkeypatch, threshold=0.6)
        engine = _make_engine()

        async with _patched_overlap(1.0) as audit_calls:
            out = await engine.commit_migration(_VALID_MIG_ID)

        assert out["status"] == "committed"
        engine.migration.commit_migration.assert_awaited_once()
        assert [c["event_type"] for c in audit_calls] == ["migration_commit_requested"]

    @pytest.mark.asyncio
    async def test_score_exactly_at_threshold_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Boundary: score == threshold passes (strictly-less-than gate)."""
        _set_gate_cfg(monkeypatch, threshold=0.6)
        engine = _make_engine()

        async with _patched_overlap(0.6):
            out = await engine.commit_migration(_VALID_MIG_ID)

        assert out["status"] == "committed"

    @pytest.mark.asyncio
    async def test_force_proceeds_and_emits_forced_event_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """force=true on a failing gate ⇒ proceed; forced event BEFORE the normal one."""
        _set_gate_cfg(monkeypatch, threshold=0.6)
        engine = _make_engine()

        async with _patched_overlap(0.25) as audit_calls:
            out = await engine.commit_migration(_VALID_MIG_ID, force=True)

        assert out["status"] == "committed"
        engine.migration.commit_migration.assert_awaited_once()
        events = [c["event_type"] for c in audit_calls]
        assert "migration_commit_forced" in events
        assert "migration_commit_requested" in events
        assert events.index("migration_commit_forced") < events.index("migration_commit_requested")

    @pytest.mark.asyncio
    async def test_force_audit_carries_gate_score(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The forced audit event's extra_params carries gate_score + gate_threshold."""
        _set_gate_cfg(monkeypatch, threshold=0.6)
        engine = _make_engine()

        async with _patched_overlap(0.15) as audit_calls:
            await engine.commit_migration(_VALID_MIG_ID, force=True)

        forced = next(c for c in audit_calls if c["event_type"] == "migration_commit_forced")
        assert forced["extra_params"] is not None
        assert "0.15" in forced["extra_params"]["gate_score"]
        assert "gate_threshold" in forced["extra_params"]

    @pytest.mark.asyncio
    async def test_force_false_explicit_still_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """force=false (explicit) blocks just like force absent."""
        _set_gate_cfg(monkeypatch, threshold=0.6)
        engine = _make_engine()

        async with _patched_overlap(0.3):
            with pytest.raises(ValueError, match="quality gate failed"):
                await engine.commit_migration(_VALID_MIG_ID, force=False)

        engine.migration.commit_migration.assert_not_called()


class TestEmptySampleClosed:
    """The empty / degenerate sample must NOT vacuously pass at 1.0."""

    @pytest.mark.asyncio
    async def test_degenerate_sample_fails_commit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GateSampleTooSmall from compute_neighbor_overlap ⇒ commit refused (no force)."""
        _set_gate_cfg(monkeypatch, threshold=0.6)
        engine = _make_engine()

        async with _patched_overlap(raises=GateSampleTooSmall("only 0 comparable pairs")):
            with pytest.raises(ValueError, match="quality gate failed"):
                await engine.commit_migration(_VALID_MIG_ID)

        engine.migration.commit_migration.assert_not_called()

    @pytest.mark.asyncio
    async def test_degenerate_sample_force_audits_degenerate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """force=true over a degenerate sample ⇒ proceeds + forced audit marked degenerate."""
        _set_gate_cfg(monkeypatch, threshold=0.6)
        engine = _make_engine()

        async with _patched_overlap(raises=GateSampleTooSmall("only 0 comparable pairs")) as calls:
            out = await engine.commit_migration(_VALID_MIG_ID, force=True)

        assert out["status"] == "committed"
        forced = next(c for c in calls if c["event_type"] == "migration_commit_forced")
        assert forced["extra_params"]["gate_score"] == "degenerate_sample"

    @pytest.mark.asyncio
    async def test_compute_overlap_empty_sample_raises(self) -> None:
        """Unit: an active model + empty target sample must raise (not return 1.0)."""
        conn = AsyncMock()
        # mig row present (target model), active model present, but zero sampled ids.
        mig_row = MagicMock()
        mig_row.__getitem__ = MagicMock(return_value=_VALID_MODEL_ID)
        conn.fetchrow = AsyncMock(return_value=mig_row)
        conn.fetchval = AsyncMock(return_value=str(uuid.uuid4()))  # active model exists
        conn.fetch = AsyncMock(return_value=[])  # no sampled target embeddings
        pool = _pool_with_acquire(conn)

        with pytest.raises(GateSampleTooSmall):
            await compute_neighbor_overlap(pool, migration_id=_VALID_MIG_ID, sample=10, k=5)

    @pytest.mark.asyncio
    async def test_compute_overlap_no_active_model_passes(self) -> None:
        """Unit: first-time-setup (no active model) legitimately returns 1.0."""
        conn = AsyncMock()
        mig_row = MagicMock()
        mig_row.__getitem__ = MagicMock(return_value=_VALID_MODEL_ID)
        conn.fetchrow = AsyncMock(return_value=mig_row)
        conn.fetchval = AsyncMock(return_value=None)  # NO active model
        conn.fetch = AsyncMock(return_value=[])
        pool = _pool_with_acquire(conn)

        score = await compute_neighbor_overlap(pool, migration_id=_VALID_MIG_ID, sample=10, k=5)
        assert score == 1.0


class TestEngineStartGate:
    def _pool_for_dims(self, target_dim: int, active_dim: int | None) -> MagicMock:
        conn = AsyncMock()
        target_row = MagicMock()
        target_row.__getitem__ = MagicMock(
            side_effect=lambda k: target_dim if k == "dimension" else None
        )
        if active_dim is not None:
            active_row: Any = MagicMock()
            active_row.__getitem__ = MagicMock(
                side_effect=lambda k: active_dim if k == "dimension" else None
            )
        else:
            active_row = None
        conn.fetchrow = AsyncMock(side_effect=[target_row, active_row])
        return _pool_with_acquire(conn)

    @pytest.mark.asyncio
    async def test_dim_mismatch_refuses_start(self) -> None:
        engine = _make_engine(pg_pool=self._pool_for_dims(target_dim=512, active_dim=768))
        with patch.object(gate, "audit_migration_action", new=AsyncMock()):
            with pytest.raises(ValueError) as exc_info:
                await engine.start_migration(_VALID_MODEL_ID)
        msg = str(exc_info.value)
        assert "512" in msg and "768" in msg
        engine.migration.start_migration.assert_not_called()

    @pytest.mark.asyncio
    async def test_matching_dims_starts(self) -> None:
        engine = _make_engine(pg_pool=self._pool_for_dims(target_dim=768, active_dim=768))
        with patch.object(gate, "audit_migration_action", new=AsyncMock()):
            out = await engine.start_migration(_VALID_MODEL_ID)
        assert out["migration_id"] == _VALID_MIG_ID
        engine.migration.start_migration.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_active_model_starts(self) -> None:
        engine = _make_engine(pg_pool=self._pool_for_dims(target_dim=768, active_dim=None))
        with patch.object(gate, "audit_migration_action", new=AsyncMock()):
            out = await engine.start_migration(_VALID_MODEL_ID)
        assert out["migration_id"] == _VALID_MIG_ID

    @pytest.mark.asyncio
    async def test_target_model_not_found_refuses(self) -> None:
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        engine = _make_engine(pg_pool=_pool_with_acquire(conn))
        with patch.object(gate, "audit_migration_action", new=AsyncMock()):
            with pytest.raises(ValueError, match="not found"):
                await engine.start_migration(_VALID_MODEL_ID)
        engine.migration.start_migration.assert_not_called()


# ===========================================================================
# MCP path — delegates to the gated engine method
# ===========================================================================


class TestMcpDelegatesToEngine:
    @pytest.mark.asyncio
    async def test_mcp_commit_delegates_force_through(self) -> None:
        """handle_commit_migration forwards force/admin_identity to engine.commit_migration."""
        from nce import migration_mcp_handlers

        engine = MagicMock()
        engine.commit_migration = AsyncMock(return_value={"status": "committed"})
        commit = _bare_handler(migration_mcp_handlers.handle_commit_migration)

        out = await commit(
            engine, {"migration_id": _VALID_MIG_ID, "force": True}, admin_identity="alice"
        )

        assert json.loads(out)["status"] == "committed"
        engine.commit_migration.assert_awaited_once()
        _, kwargs = engine.commit_migration.call_args
        assert kwargs["force"] is True
        assert kwargs["admin_identity"] == "alice"

    @pytest.mark.asyncio
    async def test_mcp_start_delegates_to_engine(self) -> None:
        """handle_start_migration forwards to engine.start_migration (gate lives there)."""
        from nce import migration_mcp_handlers

        engine = MagicMock()
        engine.start_migration = AsyncMock(return_value={"migration_id": _VALID_MIG_ID})
        start = _bare_handler(migration_mcp_handlers.handle_start_migration)

        out = await start(engine, {"target_model_id": _VALID_MODEL_ID}, admin_identity="bob")

        assert json.loads(out)["migration_id"] == _VALID_MIG_ID
        engine.start_migration.assert_awaited_once()
        _, kwargs = engine.start_migration.call_args
        assert kwargs["admin_identity"] == "bob"


# ===========================================================================
# ADMIN HTTP path — the bypass that Batch 108 closes
# ===========================================================================


def _http_request(*, migration_id: str, query: dict[str, str] | None = None, body: Any = None):
    """Minimal Starlette-like request stub for the admin HTTP handlers."""
    req = MagicMock()
    req.path_params = {"migration_id": migration_id}
    req.query_params = query or {}
    req.state = MagicMock()
    req.state.admin_identity = "http-admin"

    async def _json() -> Any:
        if body is None:
            raise ValueError("no body")
        return body

    req.json = _json
    return req


@pytest.fixture
def _engine_on_admin_state(monkeypatch: pytest.MonkeyPatch):
    """Install a real gated NCEEngine on admin_state.engine for the HTTP handler."""
    engine = _make_engine()
    monkeypatch.setattr(admin_state, "engine", engine, raising=False)
    return engine


class TestAdminHttpCommitGated:
    @pytest.mark.asyncio
    async def test_http_commit_below_threshold_refused(
        self, monkeypatch: pytest.MonkeyPatch, _engine_on_admin_state: NCEEngine
    ) -> None:
        """THE BYPASS FIX: admin HTTP commit below threshold is now refused (409),
        and the underlying orchestrator commit is never reached."""
        _set_gate_cfg(monkeypatch, threshold=0.6)
        engine = _engine_on_admin_state

        req = _http_request(migration_id=_VALID_MIG_ID, query={})
        async with _patched_overlap(0.20):
            resp = await fleet.api_admin_embedding_migration_commit(req)

        assert resp.status_code == 409
        engine.migration.commit_migration.assert_not_called()

    @pytest.mark.asyncio
    async def test_http_commit_force_query_param_proceeds(
        self, monkeypatch: pytest.MonkeyPatch, _engine_on_admin_state: NCEEngine
    ) -> None:
        """admin HTTP commit with ?force=true proceeds AND emits the forced audit."""
        _set_gate_cfg(monkeypatch, threshold=0.6)
        engine = _engine_on_admin_state

        req = _http_request(migration_id=_VALID_MIG_ID, query={"force": "true"})
        async with _patched_overlap(0.20) as audit_calls:
            resp = await fleet.api_admin_embedding_migration_commit(req)

        assert resp.status_code == 200
        engine.migration.commit_migration.assert_awaited_once()
        assert "migration_commit_forced" in [c["event_type"] for c in audit_calls]

    @pytest.mark.asyncio
    async def test_http_commit_force_body_proceeds(
        self, monkeypatch: pytest.MonkeyPatch, _engine_on_admin_state: NCEEngine
    ) -> None:
        """admin HTTP commit with JSON body {"force": true} proceeds."""
        _set_gate_cfg(monkeypatch, threshold=0.6)
        engine = _engine_on_admin_state

        req = _http_request(migration_id=_VALID_MIG_ID, query={}, body={"force": True})
        async with _patched_overlap(0.20):
            resp = await fleet.api_admin_embedding_migration_commit(req)

        assert resp.status_code == 200
        engine.migration.commit_migration.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_http_commit_passing_gate_commits(
        self, monkeypatch: pytest.MonkeyPatch, _engine_on_admin_state: NCEEngine
    ) -> None:
        """admin HTTP commit with a passing gate proceeds normally (200)."""
        _set_gate_cfg(monkeypatch, threshold=0.6)
        engine = _engine_on_admin_state

        req = _http_request(migration_id=_VALID_MIG_ID, query={})
        async with _patched_overlap(0.95):
            resp = await fleet.api_admin_embedding_migration_commit(req)

        assert resp.status_code == 200
        engine.migration.commit_migration.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_http_commit_empty_sample_refused(
        self, monkeypatch: pytest.MonkeyPatch, _engine_on_admin_state: NCEEngine
    ) -> None:
        """admin HTTP commit on a degenerate sample is refused (no vacuous pass)."""
        _set_gate_cfg(monkeypatch, threshold=0.6)
        engine = _engine_on_admin_state

        req = _http_request(migration_id=_VALID_MIG_ID, query={})
        async with _patched_overlap(raises=GateSampleTooSmall("0 comparable pairs")):
            resp = await fleet.api_admin_embedding_migration_commit(req)

        assert resp.status_code == 409
        engine.migration.commit_migration.assert_not_called()


class TestAdminHttpStartGated:
    def _pool_for_dims(self, target_dim: int, active_dim: int | None) -> MagicMock:
        conn = AsyncMock()
        target_row = MagicMock()
        target_row.__getitem__ = MagicMock(
            side_effect=lambda k: target_dim if k == "dimension" else None
        )
        if active_dim is not None:
            active_row: Any = MagicMock()
            active_row.__getitem__ = MagicMock(
                side_effect=lambda k: active_dim if k == "dimension" else None
            )
        else:
            active_row = None
        conn.fetchrow = AsyncMock(side_effect=[target_row, active_row])
        return _pool_with_acquire(conn)

    @pytest.mark.asyncio
    async def test_http_start_dim_mismatch_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """THE BYPASS FIX: admin HTTP start with a dim mismatch is refused (409)."""
        engine = _make_engine(pg_pool=self._pool_for_dims(target_dim=384, active_dim=768))
        monkeypatch.setattr(admin_state, "engine", engine, raising=False)

        req = _http_request(migration_id="ignored", body={"target_model_id": _VALID_MODEL_ID})
        with patch.object(gate, "audit_migration_action", new=AsyncMock()):
            resp = await fleet.api_admin_embedding_migration_start(req)

        assert resp.status_code == 409
        engine.migration.start_migration.assert_not_called()

    @pytest.mark.asyncio
    async def test_http_start_matching_dims_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """admin HTTP start with matching dims proceeds (200)."""
        engine = _make_engine(pg_pool=self._pool_for_dims(target_dim=768, active_dim=768))
        monkeypatch.setattr(admin_state, "engine", engine, raising=False)

        req = _http_request(migration_id="ignored", body={"target_model_id": _VALID_MODEL_ID})
        with patch.object(gate, "audit_migration_action", new=AsyncMock()):
            resp = await fleet.api_admin_embedding_migration_start(req)

        assert resp.status_code == 200
        engine.migration.start_migration.assert_awaited_once()
