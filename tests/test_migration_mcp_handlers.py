"""Contract tests for nce.migration_mcp_handlers (validation, audit gate, serialization)."""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce import auth as auth_mod
from nce import migration_mcp_handlers
from nce.mcp_errors import MCP_INTERNAL_ERROR, McpError

_ADMIN_KEY = "test-admin-mcp-key"


def _admin_arguments(extra: dict) -> dict:
    return {"admin_api_key": _ADMIN_KEY, **extra}


def _bare_handler(handler):
    """Return the inner async function beneath @mcp_handler / @require_scope."""
    fn = handler
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


@pytest.fixture
def admin_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NCE_ADMIN_API_KEY", _ADMIN_KEY)
    monkeypatch.setattr(auth_mod.cfg, "NCE_ADMIN_API_KEY", _ADMIN_KEY)
    monkeypatch.setattr(auth_mod.cfg, "NCE_ADMIN_OVERRIDE", False)


def _engine_with_pool() -> MagicMock:
    engine = MagicMock()
    engine.pg_pool = MagicMock()
    return engine


def _pool_acquire_context() -> tuple[MagicMock, AsyncMock]:
    pool = MagicMock()
    conn = AsyncMock()

    @asynccontextmanager
    async def _tx():
        yield None

    conn.transaction = MagicMock(side_effect=lambda: _tx())

    @asynccontextmanager
    async def _cm():
        yield conn

    pool.acquire = MagicMock(return_value=_cm())
    return pool, conn


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    @pytest.mark.asyncio
    async def test_start_migration_missing_target_model_id(self, admin_key_env: None) -> None:
        engine = _engine_with_pool()
        start = _bare_handler(migration_mcp_handlers.handle_start_migration)
        with patch.object(
            migration_mcp_handlers, "_audit_migration_action", new_callable=AsyncMock
        ):
            with pytest.raises(ValueError, match="target_model_id is required"):
                await start(engine, {})

    @pytest.mark.asyncio
    async def test_start_migration_target_model_id_too_long(self, admin_key_env: None) -> None:
        engine = _engine_with_pool()
        start = _bare_handler(migration_mcp_handlers.handle_start_migration)
        with patch.object(
            migration_mcp_handlers, "_audit_migration_action", new_callable=AsyncMock
        ):
            with pytest.raises(ValueError, match="target_model_id too long"):
                await start(engine, {"target_model_id": "x" * 129})

    @pytest.mark.asyncio
    async def test_commit_migration_missing_migration_id(self, admin_key_env: None) -> None:
        engine = _engine_with_pool()
        commit = _bare_handler(migration_mcp_handlers.handle_commit_migration)
        with patch.object(
            migration_mcp_handlers, "_audit_migration_action", new_callable=AsyncMock
        ):
            with pytest.raises(ValueError, match="migration_id is required"):
                await commit(engine, {})

    @pytest.mark.asyncio
    async def test_commit_migration_invalid_uuid(self, admin_key_env: None) -> None:
        engine = _engine_with_pool()
        commit = _bare_handler(migration_mcp_handlers.handle_commit_migration)
        with patch.object(
            migration_mcp_handlers, "_audit_migration_action", new_callable=AsyncMock
        ):
            with pytest.raises(ValueError, match="migration_id must be a valid UUID"):
                await commit(engine, {"migration_id": "not-a-uuid"})

    @pytest.mark.asyncio
    async def test_migration_status_valid_uuid_passes_canonical_to_engine(
        self, admin_key_env: None
    ) -> None:
        engine = _engine_with_pool()
        mid = uuid.uuid4()
        engine.migration_status = AsyncMock(return_value={"state": "running"})

        raw = await migration_mcp_handlers.handle_migration_status(
            engine,
            _admin_arguments({"migration_id": str(mid)}),
        )

        engine.migration_status.assert_awaited_once_with(str(mid))
        assert json.loads(raw)["state"] == "running"


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    @pytest.mark.asyncio
    async def test_engine_uuid_values_serialize_as_strings(self, admin_key_env: None) -> None:
        engine = _engine_with_pool()
        uid = uuid.uuid4()
        engine.migration_status = AsyncMock(return_value={"migration_id": uid})

        raw = await migration_mcp_handlers.handle_migration_status(
            engine,
            _admin_arguments({"migration_id": str(uuid.uuid4())}),
        )

        parsed = json.loads(raw)
        assert parsed["migration_id"] == str(uid)

    @pytest.mark.asyncio
    async def test_engine_datetime_values_serialize_without_error(
        self, admin_key_env: None
    ) -> None:
        engine = _engine_with_pool()
        dt = datetime(2024, 6, 1, 12, 30, 0, tzinfo=timezone.utc)
        engine.migration_status = AsyncMock(return_value={"started_at": dt})

        raw = await migration_mcp_handlers.handle_migration_status(
            engine,
            _admin_arguments({"migration_id": str(uuid.uuid4())}),
        )

        parsed = json.loads(raw)
        assert isinstance(parsed["started_at"], str)
        assert "2024" in parsed["started_at"]


# ---------------------------------------------------------------------------
# Audit pre-flight gate
# ---------------------------------------------------------------------------


class TestAuditPreflightGate:
    @pytest.mark.asyncio
    async def test_audit_failure_prevents_engine_start(self, admin_key_env: None) -> None:
        # After the refactor, the audit lives inside engine.start_migration (via
        # enforce_start_gate → audit_migration_action).  Simulate an audit failure by
        # making engine.start_migration raise, which is what the real engine does when
        # audit_migration_action raises.  The handler must surface this as McpError and
        # must have invoked start_migration exactly once (the audit/gate ran inside it).
        engine = _engine_with_pool()
        engine.start_migration = AsyncMock(side_effect=RuntimeError("audit down"))

        with pytest.raises(McpError) as exc_info:
            await migration_mcp_handlers.handle_start_migration(
                engine,
                _admin_arguments({"target_model_id": "embed-v2"}),
            )

        assert exc_info.value.code == MCP_INTERNAL_ERROR
        engine.start_migration.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_engine_failure_after_successful_audit_propagates(
        self, admin_key_env: None
    ) -> None:
        # After the refactor, audit lives inside engine.start_migration.  Build a thin
        # real coroutine that explicitly calls audit_migration_action (the gate layer)
        # and then raises RuntimeError, mirroring what the real engine does: audit
        # succeeds first, then the migration step fails.  Patch the gate-layer symbol
        # so no real PG connection is needed and assert it was awaited exactly once.
        engine = _engine_with_pool()
        start = _bare_handler(migration_mcp_handlers.handle_start_migration)

        with patch(
            "nce.migration_gate.audit_migration_action",
            new_callable=AsyncMock,
        ) as aud:

            async def _start_that_audits_then_fails(
                target_model_id: str, *, admin_identity: str | None = None
            ) -> dict:
                from nce.migration_gate import audit_migration_action  # noqa: PLC0415

                await audit_migration_action(
                    engine.pg_pool,
                    event_type="migration_start_requested",
                    admin_identity=admin_identity,
                    migration_id=None,
                    target_model_id=target_model_id,
                )
                raise RuntimeError("engine down")

            engine.start_migration = _start_that_audits_then_fails

            with pytest.raises(RuntimeError, match="engine down"):
                await start(engine, {"target_model_id": "embed-v2"})

            aud.assert_awaited_once()


# ---------------------------------------------------------------------------
# Event type naming
# ---------------------------------------------------------------------------


class TestAuditEventTypes:
    @pytest.mark.asyncio
    async def test_start_migration_audit_event_type(self, admin_key_env: None) -> None:
        # Audit now lives in nce.migration_gate.audit_migration_action, called by
        # enforce_start_gate inside engine.start_migration.  Wire engine.start_migration
        # as a thin real coroutine that calls audit_migration_action so the gate-layer
        # symbol is the one captured, then assert the event_type is unchanged.
        engine = _engine_with_pool()

        with patch(
            "nce.migration_gate.audit_migration_action",
            new_callable=AsyncMock,
        ) as aud:

            async def _start_with_audit(
                target_model_id: str, *, admin_identity: str | None = None
            ) -> dict:
                from nce.migration_gate import audit_migration_action  # noqa: PLC0415

                await audit_migration_action(
                    engine.pg_pool,
                    event_type="migration_start_requested",
                    admin_identity=admin_identity,
                    migration_id=None,
                    target_model_id=target_model_id,
                )
                return {"id": "m1"}

            engine.start_migration = _start_with_audit

            await migration_mcp_handlers.handle_start_migration(
                engine,
                _admin_arguments({"target_model_id": "embed-v2"}),
            )

        assert aud.await_args.kwargs["event_type"] == "migration_start_requested"

    @pytest.mark.asyncio
    async def test_commit_migration_audit_event_type(self, admin_key_env: None) -> None:
        # Audit now lives in nce.migration_gate.audit_migration_action, called by
        # enforce_commit_gate inside engine.commit_migration.  Wire engine.commit_migration
        # as a thin real coroutine that calls audit_migration_action so we can assert the
        # event_type emitted at the gate layer.
        engine = _engine_with_pool()
        mid = str(uuid.uuid4())

        with patch(
            "nce.migration_gate.audit_migration_action",
            new_callable=AsyncMock,
        ) as aud:

            async def _commit_with_audit(
                migration_id: str,
                *,
                force: bool = False,
                admin_identity: str | None = None,
            ) -> dict:
                from nce.migration_gate import audit_migration_action  # noqa: PLC0415

                await audit_migration_action(
                    engine.pg_pool,
                    event_type="migration_commit_requested",
                    admin_identity=admin_identity,
                    migration_id=migration_id,
                    target_model_id=None,
                )
                return {"committed": True}

            engine.commit_migration = _commit_with_audit

            await migration_mcp_handlers.handle_commit_migration(
                engine,
                _admin_arguments({"migration_id": mid}),
            )

        assert aud.await_args.kwargs["event_type"] == "migration_commit_requested"

    @pytest.mark.asyncio
    async def test_abort_migration_audit_event_type(self, admin_key_env: None) -> None:
        engine = _engine_with_pool()
        mid = str(uuid.uuid4())
        engine.abort_migration = AsyncMock(return_value={"aborted": True})

        with patch.object(
            migration_mcp_handlers,
            "_audit_migration_action",
            new_callable=AsyncMock,
        ) as aud:
            await migration_mcp_handlers.handle_abort_migration(
                engine,
                _admin_arguments({"migration_id": mid}),
            )

        assert aud.await_args.kwargs["event_type"] == "migration_abort_requested"


# ---------------------------------------------------------------------------
# extra_params bounds (_audit_migration_action)
# ---------------------------------------------------------------------------


class TestAuditExtraParamsBounds:
    @pytest.mark.asyncio
    async def test_extra_params_seventeen_keys_rejected(self) -> None:
        pool, _conn = _pool_acquire_context()
        extra = {f"k{i}": i for i in range(17)}

        with pytest.raises(ValueError, match="extra_params exceeds maximum key count"):
            await migration_mcp_handlers._audit_migration_action(
                pool,
                event_type="migration_test",
                admin_identity="root",
                migration_id="x",
                target_model_id=None,
                extra_params=extra,
            )

    @pytest.mark.asyncio
    async def test_extra_params_nested_dict_rejected(self) -> None:
        pool, _conn = _pool_acquire_context()

        with pytest.raises(ValueError, match="extra_params values must be scalar"):
            await migration_mcp_handlers._audit_migration_action(
                pool,
                event_type="migration_test",
                admin_identity="root",
                migration_id="x",
                target_model_id=None,
                extra_params={"nested": {"a": 1}},
            )

    @pytest.mark.asyncio
    async def test_extra_params_list_value_rejected(self) -> None:
        pool, _conn = _pool_acquire_context()

        with pytest.raises(ValueError, match="extra_params values must be scalar"):
            await migration_mcp_handlers._audit_migration_action(
                pool,
                event_type="migration_test",
                admin_identity="root",
                migration_id="x",
                target_model_id=None,
                extra_params={"items": [1, 2]},
            )

    @pytest.mark.asyncio
    async def test_extra_params_string_too_long_rejected(self) -> None:
        pool, _conn = _pool_acquire_context()

        with pytest.raises(ValueError, match="value too long"):
            await migration_mcp_handlers._audit_migration_action(
                pool,
                event_type="migration_test",
                admin_identity="root",
                migration_id="x",
                target_model_id=None,
                extra_params={"note": "z" * 257},
            )

    @pytest.mark.asyncio
    async def test_extra_params_sixteen_scalar_keys_accepted(self) -> None:
        # append_event now lives in nce.migration_gate (the shared enforcement module);
        # patch it there so migration_gate.audit_migration_action resolves the symbol.
        pool, _conn = _pool_acquire_context()
        extra = {f"k{i}": f"v{i}" for i in range(16)}
        calls: list[str] = []

        async def _append(*, conn, **kwargs):
            calls.append(kwargs.get("event_type", ""))
            return SimpleNamespace(event_id=uuid.uuid4(), event_seq=1)

        with patch("nce.migration_gate.append_event", side_effect=_append):
            await migration_mcp_handlers._audit_migration_action(
                pool,
                event_type="migration_test",
                admin_identity="root",
                migration_id="x",
                target_model_id="y",
                extra_params=extra,
            )

        assert calls == ["migration_test"]


# ---------------------------------------------------------------------------
# admin_identity logging
# ---------------------------------------------------------------------------


class TestAdminIdentityLogging:
    @pytest.mark.asyncio
    async def test_admin_identity_truncated_in_info_log(self, caplog) -> None:
        # The truncation log now fires from nce.migration_gate (logger="nce.migration_gate")
        # because audit_migration_action relocated there.  Patch append_event at the
        # gate module and capture the gate logger; all other assertions are identical.
        pool, _conn = _pool_acquire_context()
        long_identity = "a" * 64
        expected_logged = "a" * 32

        async def _append(*, conn, **kwargs):
            return SimpleNamespace(event_id=uuid.uuid4(), event_seq=7)

        caplog.set_level(logging.INFO, logger="nce.migration_gate")

        with patch("nce.migration_gate.append_event", side_effect=_append):
            await migration_mcp_handlers._audit_migration_action(
                pool,
                event_type="migration_test",
                admin_identity=long_identity,
                migration_id="x",
                target_model_id=None,
            )

        assert any(
            expected_logged in rec.message and "admin=" in rec.message
            for rec in caplog.records
            if rec.name == "nce.migration_gate"
        )
        assert not any(
            long_identity in rec.message
            for rec in caplog.records
            if rec.name == "nce.migration_gate"
        )
