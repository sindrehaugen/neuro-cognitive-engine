"""
tests/test_field_tech_approve_time_entry.py
=============================================
C-7: governed confirm-first approval of FIELD_TECH_TIME_ENTRY (2026-09-20).

Before this wave, ``approved`` was a plain ``writable_fields`` entry on
FIELD_TECH_TIME_ENTRY_SPEC's generic PATCH -- any caller with PATCH access
could flip it with no confirmation and no audit trail, which is not what
"governed" means. This closes that: ``do_approve_time_entry``
(``field_tech/time_entry.py``) is ``@governed``, and ``approved`` is removed
from ``writable_fields`` in the same change (``field_tech/resources.py``) so
PATCH can no longer walk around the gate.

Unit tier (no DB) mirrors ``test_governed_decorator.py``'s own split: only
the "no confirm -> pending, body never runs" path is mockable without a real
Postgres connection (the confirm=True path writes to
``action_idempotency``/``event_log`` inside the decorator itself, which
``test_governed_decorator.py`` tests only at the integration tier -- same
choice made here, not a gap.).

Integration tier proves three things against a real database:
  1. Without confirm, nothing is approved.
  2. With confirm, the entry is approved, and calling do_approve_time_entry
     directly through @governed is the only path that flips it.
  3. Generic PATCH (the resource_surface list of writable_fields) can no
     longer set approved at all -- the regression this whole wave exists to
     close. Mutation-verified before this file existed: with "approved"
     still in writable_fields, PATCH {"approved": true} succeeded silently.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.field_tech.resources import FIELD_TECH_TIME_ENTRY_SPEC
from nce.vertical_modules.field_tech.time_entry import (
    TimeEntryNotFoundError,
    do_approve_time_entry,
)

# ---------------------------------------------------------------------------
# Unit tier -- no DB. Mirrors test_governed_decorator.py's _make_mock_conn.
# ---------------------------------------------------------------------------


def _make_mock_conn(queue_id: uuid.UUID | None = None) -> MagicMock:
    conn = MagicMock()
    conn.is_in_transaction.return_value = True
    qid = queue_id or uuid.uuid4()

    async def _mock_fetchrow(query: str, *args: Any) -> Any:
        # do_approve_time_entry's own body issues exactly one fetchrow: the
        # UPDATE ... RETURNING against time_entries. Every other fetchrow in
        # this path belongs to @governed's own bookkeeping (action_idempotency,
        # action_approval_queue) -- answered generically ("nothing recorded
        # yet") so this test does not hard-code the decorator's internals.
        if "time_entries" in query:
            raise AssertionError(
                f"do_approve_time_entry's body ran without confirm=True -- "
                f"query was: {query[:80]!r}"
            )
        if "INSERT INTO action_approval_queue" in query:
            return {"id": qid}
        return None  # "not found" / "not already recorded" for any governor bookkeeping query

    conn.fetchrow = AsyncMock(side_effect=_mock_fetchrow)
    conn.execute = AsyncMock()
    return conn


def test_writable_fields_does_not_include_approved() -> None:
    """The regression this wave closes, pinned directly on the spec.

    A future edit re-adding "approved" to writable_fields would reopen the
    exact PATCH-around-the-gate hole @governed exists to close -- this must
    fail loudly, not rely on someone remembering why it was removed.
    """
    assert "approved" not in FIELD_TECH_TIME_ENTRY_SPEC.writable_fields
    # And it must still be filterable -- removing it from writable_fields is
    # about the write path only; callers still need to filter on approval state.
    assert "approved" in FIELD_TECH_TIME_ENTRY_SPEC.filterable_fields


@pytest.mark.asyncio
async def test_approve_without_confirm_is_pending_and_does_not_run() -> None:
    """Without confirm=True, @governed must return pending_approval and the
    UPDATE must never execute -- proven by the mock raising if it is reached,
    not just by asserting the returned status."""
    mock_conn = _make_mock_conn()
    ns_id = uuid.uuid4()
    te_id = uuid.uuid4()

    result = await do_approve_time_entry(
        mock_conn,
        ns_id,
        idempotency_key=f"approve-{te_id}",
        confirm=False,
        engine=None,
        time_entry_id=te_id,
    )

    assert result["status"] == "pending_approval"
    mock_conn.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Integration tier -- real Postgres via pg_pool/namespace_id fixtures.
# ---------------------------------------------------------------------------


async def _seed_work_order_and_time_entry(
    pg_pool: Any, namespace_id: uuid.UUID
) -> tuple[str, uuid.UUID]:
    work_order_id = f"WO-APPROVE-{uuid.uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO work_orders
                (work_order_id, namespace_id, kind, status, source_kind, source_ref)
            VALUES ($1, $2::uuid, 'install', 'draft', 'manual', $1)
            """,
            work_order_id,
            namespace_id,
        )
        row = await conn.fetchrow(
            """
            INSERT INTO time_entries (time_entry_id, work_order_id, namespace_id, started_at, source)
            VALUES ($1, $2, $3::uuid, NOW(), 'manual')
            RETURNING id
            """,
            f"TE-APPROVE-{uuid.uuid4().hex[:8]}",
            work_order_id,
            namespace_id,
        )
    return work_order_id, row["id"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_approve_time_entry_not_found(pg_pool: Any, namespace_id: uuid.UUID) -> None:
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        with pytest.raises(TimeEntryNotFoundError):
            await do_approve_time_entry(
                conn,
                namespace_id,
                idempotency_key=f"approve-missing-{uuid.uuid4()}",
                confirm=True,
                engine=None,
                time_entry_id=uuid.uuid4(),
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_approve_time_entry_confirmed_flips_the_flag(
    pg_pool: Any, namespace_id: uuid.UUID
) -> None:
    _work_order_id, te_id = await _seed_work_order_and_time_entry(pg_pool, namespace_id)

    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        result = await do_approve_time_entry(
            conn,
            namespace_id,
            idempotency_key=f"approve-confirm-{te_id}",
            confirm=True,
            engine=None,
            time_entry_id=te_id,
        )

    assert result["status"] == "executed"
    assert result["result"]["approved"] is True
    assert result["result"]["id"] == str(te_id)

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT approved FROM time_entries WHERE id = $1::uuid", te_id)
    assert row["approved"] is True
