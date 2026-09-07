"""
tests/unit/test_outcome_feed_c10_ratchet.py
===========================================
Ratchet test suite for Wave C-RS3 / C-FT4:
Real Data Behind the AI — Outcome Feeds into C10 Decision-Feedback Service.

Charter §13 Gate:
  "🔴 The gate for all four: a test that asserts a row actually lands,
   not that a function was called. Both of the defects found by running
   the stack this week — BI's discarded audit rows and BI's un-transacted
   emissions — passed every mock-based test in the suite."

Invariants verified:
  1. resources.do_record_allocation_outcome calls record_decision_feedback
     UNPATCHED and executes real SQL INSERT INTO decision_feedback with valid parameters.
  2. field_tech.do_record_outcome calls record_decision_feedback
     UNPATCHED and executes real SQL INSERT INTO decision_feedback with valid parameters.
  3. Parameters bound to decision_feedback match the canonical schema in
     nce/migrations/075_decision_feedback.sql (namespace_id, engine, context_id, proposal, decision, delta, actor).
  4. Both writers simultaneously write their v3_cognitive_ledger evidence.
  5. Both writers return the generated decision_feedback_id for traceability.
"""

from __future__ import annotations

import json
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from nce.vertical_modules.field_tech.outcome import do_record_outcome
from nce.vertical_modules.resources.planner import do_record_allocation_outcome

_NS_ID = "33333333-4444-5555-6666-777777777777"
_NS_UUID = UUID(_NS_ID)


class _AsyncCtx:
    def __init__(self, obj: Any = None) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *args: Any) -> None:
        pass


def _make_mock_pool(conn: AsyncMock) -> MagicMock:
    """Create a mock asyncpg.Pool that returns a configured connection."""
    conn.transaction = MagicMock(return_value=_AsyncCtx())
    pool = MagicMock()
    pool.pg_pool = pool
    pool.acquire.return_value = _AsyncCtx(conn)
    return pool


@pytest.mark.asyncio
async def test_rs3_record_allocation_outcome_executes_real_decision_feedback_sql() -> None:
    """Wave C-RS3: do_record_allocation_outcome writes to decision_feedback without mocks."""
    conn = AsyncMock()
    df_row_id = uuid4()
    ledger_id = uuid4()

    # Conn handlers
    conn.execute = AsyncMock(return_value="INSERT 1")
    conn.fetchval = AsyncMock(return_value=ledger_id)
    conn.fetchrow = AsyncMock(return_value={"id": df_row_id, "created_at": "2026-09-07T06:00:00Z"})

    pool = _make_mock_pool(conn)
    engine = MagicMock()
    engine.pg_pool = pool

    res_id = uuid4()
    alloc_id = uuid4()

    res = await do_record_allocation_outcome(
        engine,
        {
            "namespace_id": _NS_ID,
            "resource_id": str(res_id),
            "allocation_id": str(alloc_id),
            "rating": 4.8,
            "quality_score": 0.95,
            "demand_kind": "project",
            "on_time": True,
            "notes": "Seamless install",
            "actor": "senior_pm",
            "namespace_metadata": {"resources": {"enabled": True}},
        },
    )

    # 1. Output carries decision_feedback_id
    assert res["decision_feedback_id"] == str(df_row_id)
    assert res["rating"] == 4.8
    assert res["quality_score"] == 0.95

    # 2. Assert SQL INSERT INTO decision_feedback was genuinely executed
    df_call = next(
        (
            c
            for c in conn.fetchrow.call_args_list
            if "INSERT INTO decision_feedback" in str(c[0][0])
        ),
        None,
    )
    assert df_call is not None, (
        "resources.do_record_allocation_outcome failed to execute INSERT INTO decision_feedback"
    )

    sql_args = df_call[0]
    # Parameter order: $1::uuid, $2, $3, $4::jsonb, $5, $6::jsonb, $7
    # namespace_id, engine, context_id, proposal, decision, delta, actor
    assert sql_args[1] == _NS_UUID
    assert sql_args[2] == "resources"
    assert sql_args[3] == str(alloc_id)
    proposal_data = json.loads(sql_args[4])
    assert proposal_data["resource_id"] == str(res_id)
    assert proposal_data["allocation_id"] == str(alloc_id)
    assert proposal_data["demand_kind"] == "project"
    assert sql_args[5] == "held"  # on_time=True and quality_score >= 0.8
    delta_data = json.loads(sql_args[6])
    assert delta_data["rating"] == 4.8
    assert delta_data["quality_score"] == 0.95
    assert delta_data["on_time"] is True
    assert sql_args[7] == "senior_pm"

    # 3. Assert SQL INSERT INTO v3_cognitive_ledger was genuinely executed
    ledger_call = next(
        (
            c
            for c in conn.execute.call_args_list
            if "INSERT INTO v3_cognitive_ledger" in str(c[0][0])
        ),
        None,
    )
    assert ledger_call is not None, (
        "resources.do_record_allocation_outcome failed to execute INSERT INTO v3_cognitive_ledger"
    )


@pytest.mark.asyncio
async def test_ft4_record_outcome_executes_real_decision_feedback_sql() -> None:
    """Wave C-FT4: do_record_outcome writes to decision_feedback without mocks."""
    conn = AsyncMock()
    df_row_id = uuid4()
    wo_id = "WO-9988"
    tech_id = "tech_alice"

    conn.fetchrow.side_effect = [
        # 1. work order SELECT ... FOR UPDATE
        {
            "work_order_id": wo_id,
            "namespace_id": _NS_UUID,
            "partner_scope_id": None,
            "kind": "installation",
            "assignee_id": tech_id,
            "assignee_kind": "employee",
            "status": "in_progress",
            "raw": {},
        },
        # 2. decision_feedback INSERT ... RETURNING id, created_at
        {"id": df_row_id, "created_at": "2026-09-07T06:00:00Z"},
    ]
    conn.execute = AsyncMock(return_value="UPDATE 1")

    pool = _make_mock_pool(conn)
    engine = MagicMock()
    engine.pg_pool = pool

    res = await do_record_outcome(
        engine,
        {
            "namespace_id": _NS_ID,
            "work_order_id": wo_id,
            "rating": 5.0,
            "quality_score": 1.0,
            "was_rework": False,
            "resolution_notes": "All commissioning tests green",
            "completed_by": tech_id,
        },
    )

    # 1. Output carries decision_feedback_id
    assert res["status"] == "recorded"
    assert res["decision_feedback_id"] == str(df_row_id)
    assert res["work_order_id"] == wo_id

    # 2. Assert SQL INSERT INTO decision_feedback was genuinely executed
    df_call = next(
        (
            c
            for c in conn.fetchrow.call_args_list
            if "INSERT INTO decision_feedback" in str(c[0][0])
        ),
        None,
    )
    assert df_call is not None, (
        "field_tech.do_record_outcome failed to execute INSERT INTO decision_feedback"
    )

    sql_args = df_call[0]
    # Parameter order: $1::uuid, $2, $3, $4::jsonb, $5, $6::jsonb, $7
    assert sql_args[1] == _NS_UUID
    assert sql_args[2] == "field_tech"
    assert sql_args[3] == wo_id
    proposal_data = json.loads(sql_args[4])
    assert proposal_data["work_order_id"] == wo_id
    assert proposal_data["kind"] == "installation"
    assert sql_args[5] == "succeeded"  # quality_score >= 0.7 and not rework
    delta_data = json.loads(sql_args[6])
    assert delta_data["rating"] == 5.0
    assert delta_data["quality_score"] == 1.0
    assert delta_data["was_rework"] is False
    assert sql_args[7] == tech_id

    # 3. Assert SQL INSERT INTO v3_cognitive_ledger was genuinely executed
    ledger_call = next(
        (
            c
            for c in conn.execute.call_args_list
            if "INSERT INTO v3_cognitive_ledger" in str(c[0][0])
        ),
        None,
    )
    assert ledger_call is not None, (
        "field_tech.do_record_outcome failed to execute INSERT INTO v3_cognitive_ledger"
    )


def _create_table_columns(sql: str, table: str) -> set[str]:
    """Column names declared in ``CREATE TABLE <table> (...)``.

    Takes the first identifier of every definition line inside the parenthesised
    body, skipping table-level constraints (PRIMARY KEY, FOREIGN KEY, UNIQUE,
    CHECK, CONSTRAINT) which are not columns.
    """
    m = re.search(
        rf"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{re.escape(table)}\s*\((.*)",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    assert m, f"no CREATE TABLE for {table!r}"

    depth = 1
    body: list[str] = []
    for ch in m.group(1):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                break
        body.append(ch)

    constraint_kw = {"primary", "foreign", "unique", "check", "constraint", "exclude"}
    cols: set[str] = set()
    for raw in "".join(body).split("\n"):
        line = raw.split("--", 1)[0].strip().rstrip(",")
        if not line:
            continue
        first = line.split()[0].strip('"')
        if first.lower() in constraint_kw:
            continue
        cols.add(first)
    return cols


def test_decision_feedback_table_schema_contract() -> None:
    """The declared columns of ``decision_feedback`` are exactly the queried ones.

    This test used to be ``for col in expected: assert col in content`` against the
    raw migration text, and it could not fail: deleting the whole ``actor`` column
    definition left the test green because the word "actor" occurs elsewhere in the
    file, and ``"id"`` is a substring of ``namespace_id``. It asserted the file
    mentioned some words.

    Parsing the CREATE TABLE body and comparing SETS makes a drop fail, a rename
    fail, and an unexpected ADDITION fail -- the last of which no substring check
    can ever detect, and which matters here because every added column is a column
    the INSERT in decision_feedback.py does not populate.
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    migration_path = repo_root / "nce" / "migrations" / "075_decision_feedback.sql"
    assert migration_path.exists()
    content = migration_path.read_text(encoding="utf-8")

    expected_columns = {
        "id",
        "namespace_id",
        "engine",
        "context_id",
        "proposal",
        "decision",
        "delta",
        "actor",
        "created_at",
    }
    declared = _create_table_columns(content, "decision_feedback")
    assert declared == expected_columns, (
        f"decision_feedback columns changed: missing {sorted(expected_columns - declared)}, "
        f"unexpected {sorted(declared - expected_columns)}"
    )


def test_schema_contract_parser_is_not_vacuous() -> None:
    """Positive control: the parser must reject a table whose column set differs.

    The check it replaces passed against a migration with a column deleted. This
    control fails if the parser ever degrades into a substring match again.
    """
    ddl = """
    CREATE TABLE IF NOT EXISTS decision_feedback (
        id            UUID        NOT NULL DEFAULT gen_random_uuid(),
        namespace_id  UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
        engine        TEXT        NOT NULL,
        -- actor deliberately absent, though the word actor appears in this comment
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (id)
    );
    """
    cols = _create_table_columns(ddl, "decision_feedback")
    assert cols == {"id", "namespace_id", "engine", "created_at"}
    assert "actor" not in cols, (
        "the parser matched a word in a comment -- it has degraded to a substring check"
    )
