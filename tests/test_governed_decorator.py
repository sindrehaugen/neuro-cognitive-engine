"""
Integration tests for the C2 ``@governed`` decorator (Contract B §9.5).

Acceptance criteria (wave brief):
- A decorated mutating handler called WITHOUT ``confirm=True`` returns
  ``{"status": "pending_approval", ...}`` and does NOT execute the side effect.
- A duplicate ``idempotency_key`` is a NO-OP: the side effect runs exactly once;
  the second call returns ``{"status": "already_executed", ...}``.
- Each executed act is audited to ``event_log`` via ``append_event``.

All DB-dependent tests are marked ``@pytest.mark.integration``.

The fixture ``pg_pool`` / ``namespace_id`` come from ``tests/conftest.py``
and require ``NCE_INTEGRATION_PG_DSN`` / ``PG_DSN`` / ``DATABASE_URL`` to be set.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nce.autonomy.governor import (
    GovernanceError,
    MissingIdempotencyKeyError,
    governed,
)
from nce.db_utils import scoped_pg_session

# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------


def _make_handler() -> tuple[Any, list[int]]:
    """Return a governed handler + a call-counter list for side-effect tracking."""
    call_log: list[int] = []

    @governed(action_type="test_action")
    async def mutating_handler(
        conn: Any,
        namespace_id: uuid.UUID,
        *,
        idempotency_key: str,
        confirm: bool = False,
        payload: str = "default",
    ) -> dict[str, Any]:
        call_log.append(1)
        return {"payload": payload}

    return mutating_handler, call_log


# ---------------------------------------------------------------------------
# Unit-level tests (no DB required)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_governed_no_confirm_returns_pending() -> None:
    """Without confirm=True the handler must NOT be called and must return pending."""
    handler, call_log = _make_handler()

    result = await handler(
        None,  # conn — not accessed because confirm is False
        uuid.uuid4(),
        idempotency_key="k-unit-1",
        confirm=False,
    )

    assert result["status"] == "pending_approval"
    assert result["idempotency_key"] == "k-unit-1"
    assert result["action_type"] == "test_action"
    assert call_log == [], "side effect must not run without confirm=True"


@pytest.mark.asyncio
async def test_governed_missing_idempotency_key_raises() -> None:
    """A missing idempotency_key must raise MissingIdempotencyKeyError immediately."""
    handler, call_log = _make_handler()

    with pytest.raises(MissingIdempotencyKeyError):
        await handler(None, uuid.uuid4(), idempotency_key="", confirm=False)

    assert call_log == [], "side effect must not run when key is missing"


@pytest.mark.asyncio
async def test_governed_whitespace_idempotency_key_raises() -> None:
    """A whitespace-only idempotency_key is treated as missing."""
    handler, call_log = _make_handler()

    with pytest.raises(MissingIdempotencyKeyError):
        await handler(None, uuid.uuid4(), idempotency_key="   ", confirm=False)


@pytest.mark.asyncio
async def test_governed_default_confirm_is_false() -> None:
    """confirm defaults to False so omitting it must produce pending_approval."""
    handler, call_log = _make_handler()

    result = await handler(None, uuid.uuid4(), idempotency_key="k-unit-default")

    assert result["status"] == "pending_approval"
    assert call_log == []


@pytest.mark.asyncio
async def test_governed_conn_not_in_transaction_raises_governance_error() -> None:
    """conn.is_in_transaction() == False must raise GovernanceError before any DB call.

    This guards against a caller bypassing scoped_pg_session: without an active
    transaction the idempotency INSERT would auto-commit then append_event would
    raise, leaving a poison key that suppresses the action forever with no audit.
    """
    handler, call_log = _make_handler()

    # Build a mock conn that is NOT inside a transaction.
    mock_conn: MagicMock = MagicMock()
    mock_conn.is_in_transaction.return_value = False
    # fetchrow and execute must NOT be called — assert afterwards.
    mock_conn.fetchrow = AsyncMock()
    mock_conn.execute = AsyncMock()

    with pytest.raises(GovernanceError, match="not inside an active transaction"):
        await handler(
            mock_conn,
            uuid.uuid4(),
            idempotency_key="k-unit-no-tx",
            confirm=True,
        )

    # Guard must have fired before any DB interaction.
    mock_conn.fetchrow.assert_not_called()
    mock_conn.execute.assert_not_called()
    assert call_log == [], "handler body must not run when conn has no active transaction"


# ---------------------------------------------------------------------------
# Integration tests (require live Postgres via pg_pool / namespace_id fixtures)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_governed_confirm_executes_and_audits(
    pg_pool: Any,
    namespace_id: uuid.UUID,
) -> None:
    """Confirmed call executes the handler once and writes an event_log audit entry."""
    handler, call_log = _make_handler()

    idem_key = f"idem-confirm-{uuid.uuid4().hex}"

    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        result = await handler(
            conn,
            namespace_id,
            idempotency_key=idem_key,
            confirm=True,
            payload="first",
        )

    assert result["status"] == "executed"
    assert result["idempotency_key"] == idem_key
    assert result["result"] == {"payload": "first"}
    assert call_log == [1], "side effect must run exactly once"

    # Verify audit entry in event_log
    async with pg_pool.acquire() as conn:
        await conn.execute(f"SET nce.namespace_id = '{namespace_id}'")
        row = await conn.fetchrow(
            """
            SELECT params FROM event_log
            WHERE namespace_id = $1
              AND agent_id = 'governor'
              AND event_type = 'config_changed'
            ORDER BY event_seq DESC LIMIT 1
            """,
            namespace_id,
        )

    assert row is not None, "event_log audit row must exist after governed execution"
    params = row["params"]
    if isinstance(params, str):
        import json

        params = json.loads(params)
    assert params.get("actor") == "governor"
    changes = params.get("changes", {})
    assert changes.get("governed_action") == "test_action"
    assert changes.get("idempotency_key") == idem_key


@pytest.mark.integration
@pytest.mark.asyncio
async def test_governed_replay_is_noop(
    pg_pool: Any,
    namespace_id: uuid.UUID,
) -> None:
    """Same idempotency_key called twice: side effect runs exactly once."""
    handler, call_log = _make_handler()

    idem_key = f"idem-replay-{uuid.uuid4().hex}"

    # First call — executes
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        result_1 = await handler(
            conn,
            namespace_id,
            idempotency_key=idem_key,
            confirm=True,
            payload="first",
        )

    assert result_1["status"] == "executed"
    assert call_log == [1]

    # Second call with same key — NO-OP
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        result_2 = await handler(
            conn,
            namespace_id,
            idempotency_key=idem_key,
            confirm=True,
            payload="second",  # different payload, must not re-run
        )

    assert result_2["status"] == "already_executed"
    assert result_2["idempotency_key"] == idem_key
    assert call_log == [1], "side effect must not run a second time"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_governed_different_keys_both_execute(
    pg_pool: Any,
    namespace_id: uuid.UUID,
) -> None:
    """Different idempotency keys in the same namespace are independent executions."""
    handler, call_log = _make_handler()

    idem_key_a = f"idem-multi-a-{uuid.uuid4().hex}"
    idem_key_b = f"idem-multi-b-{uuid.uuid4().hex}"

    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        result_a = await handler(conn, namespace_id, idempotency_key=idem_key_a, confirm=True)

    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        result_b = await handler(conn, namespace_id, idempotency_key=idem_key_b, confirm=True)

    assert result_a["status"] == "executed"
    assert result_b["status"] == "executed"
    assert len(call_log) == 2, "both distinct keys must trigger execution"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_governed_pending_does_not_touch_db(
    pg_pool: Any,
    namespace_id: uuid.UUID,
) -> None:
    """A pending_approval call must write nothing to action_idempotency or event_log."""
    handler, call_log = _make_handler()

    idem_key = f"idem-pending-{uuid.uuid4().hex}"

    result = await handler(
        None,
        namespace_id,
        idempotency_key=idem_key,
        confirm=False,
    )

    assert result["status"] == "pending_approval"

    # Nothing written to action_idempotency
    async with pg_pool.acquire() as conn:
        await conn.execute(f"SET nce.namespace_id = '{namespace_id}'")
        exists = await conn.fetchval(
            """
            SELECT 1 FROM action_idempotency
            WHERE namespace_id = $1 AND idempotency_key = $2
            """,
            namespace_id,
            idem_key,
        )
    assert exists is None, "pending_approval must not write the idempotency key to DB"
    assert call_log == []
