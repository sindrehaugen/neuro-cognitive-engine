"""
tests/test_c2_donewhen.py

C2 done-when proof (§9.5 / Contract B).

Asserts end-to-end against a live DB that:
  1. A @governed tool without confirm=True → pending_approval (no side effect).
  2. A @governed tool without an idempotency_key → MissingIdempotencyKeyError.
  3. A @governed tool with confirm=True → executes once; side effect runs.
  4. Replaying the same idempotency_key → already_executed (side effect stays 1).
  5. The executed act appears in event_log (audit ledger — queried, not mutated).

This is an integration test (touches DB: action_idempotency + event_log).
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.autonomy.governor import MissingIdempotencyKeyError, governed
from nce.db_utils import scoped_pg_session

# ---------------------------------------------------------------------------
# Dummy mutating tool — the smallest handler that exercises the gate (SRP).
# Records a side effect in ``_calls`` so assertions can count executions
# without additional DB rows.  The handler itself is DB-free; the governor
# owns the DB writes (idempotency + event_log).
# ---------------------------------------------------------------------------

_calls: list[str] = []


@governed(action_type="dummy_mutate")
async def _dummy_tool(
    conn: asyncpg.Connection,
    namespace_id: uuid.UUID,
    *,
    idempotency_key: str,
    confirm: bool = False,
    marker: str = "default",
) -> dict[str, Any]:
    """Dummy world-writing tool — appends *marker* to _calls as its side effect."""
    _calls.append(marker)
    return {"marker": marker}


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_confirm_returns_pending_no_side_effect(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Without confirm=True the governor must block execution (pending_approval).

    The side effect (append to _calls) must NOT run.
    """
    _calls.clear()
    ikey = f"c2-pending-{uuid.uuid4().hex}"

    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        result = await _dummy_tool(
            conn,
            namespace_id,
            idempotency_key=ikey,
            confirm=False,
            marker="should_not_appear",
        )

    assert result["status"] == "pending_approval"
    assert result["action_type"] == "dummy_mutate"
    assert result["idempotency_key"] == ikey
    assert _calls == [], "Side effect must NOT run when confirm=False"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_missing_idempotency_key_raises(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """A call without a non-empty idempotency_key must raise MissingIdempotencyKeyError.

    The check fires before any DB write — the transaction guard is irrelevant here.
    """
    _calls.clear()

    with pytest.raises(MissingIdempotencyKeyError):
        async with scoped_pg_session(pg_pool, namespace_id) as conn:
            await _dummy_tool(
                conn,
                namespace_id,
                idempotency_key="",  # empty → must raise
                confirm=True,
                marker="should_not_appear",
            )

    assert _calls == [], "Side effect must NOT run when idempotency_key is missing"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_confirmed_executes_once_and_replay_is_noop(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Confirmed act executes the side effect exactly once; replay is a no-op.

    Also verifies:
    - First call → status "executed".
    - Second call (same key, same session) → status "already_executed".
    - _calls length stays 1 after both calls (side effect not repeated).
    """
    _calls.clear()
    ikey = f"c2-exec-{uuid.uuid4().hex}"

    # --- First execution ---
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        first = await _dummy_tool(
            conn,
            namespace_id,
            idempotency_key=ikey,
            confirm=True,
            marker="run-once",
        )

    assert first["status"] == "executed"
    assert first["idempotency_key"] == ikey
    assert first["result"] == {"marker": "run-once"}
    assert _calls == ["run-once"], "Side effect must run exactly once on first confirm"

    # --- Replay with the same key (new scoped session = new transaction) ---
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        replay = await _dummy_tool(
            conn,
            namespace_id,
            idempotency_key=ikey,
            confirm=True,
            marker="should_not_appear_again",
        )

    assert replay["status"] == "already_executed"
    assert replay["idempotency_key"] == ikey
    assert _calls == ["run-once"], "Side effect must NOT re-run on replay (no-op)"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_executed_act_appears_in_event_log(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """The governor must append an audit entry to event_log on confirmed execution.

    Queries the live event_log table; does NOT mutate it (WORM invariant).
    The audit entry uses event_type='config_changed' and stores the
    governed_action and idempotency_key in params->changes (see governor.py
    _audit_execution).
    """
    _calls.clear()
    ikey = f"c2-audit-{uuid.uuid4().hex}"

    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        result = await _dummy_tool(
            conn,
            namespace_id,
            idempotency_key=ikey,
            confirm=True,
            marker="audit-check",
        )

    assert result["status"] == "executed"

    # Query the audit ledger — read-only (WORM: no UPDATE/DELETE).
    # The governor writes event_type='config_changed' with
    # params->'changes'->>'governed_action' = 'dummy_mutate'
    # and params->'changes'->>'idempotency_key' = ikey.
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT event_type, params
            FROM   event_log
            WHERE  namespace_id = $1
              AND  event_type   = 'config_changed'
              AND  params -> 'changes' ->> 'idempotency_key' = $2
            ORDER BY occurred_at DESC
            LIMIT 1
            """,
            namespace_id,
            ikey,
        )

    assert row is not None, (
        f"No event_log entry found for idempotency_key={ikey!r}. "
        "The governor must append an audit entry on every confirmed execution."
    )
    assert row["event_type"] == "config_changed"
    import json

    changes = (
        row["params"]["changes"]
        if isinstance(row["params"], dict)
        else json.loads(row["params"])["changes"]
    )
    assert changes["governed_action"] == "dummy_mutate"
    assert changes["idempotency_key"] == ikey
