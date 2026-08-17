"""Integration and unit tests for project/pl.py — Wave 8 (my-day-capacity).

Validates:
  1. do_my_day(engine, params) ranking open tasks by priority (gate-blocking * deadline * value).
  2. do_capacity(engine, params) aggregating open task load per PL/team over a given window.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.project.pl import (
    calculate_priority,
    do_capacity,
    do_my_day,
    parse_date,
    parse_float,
)

# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_parse_date() -> None:
    assert parse_date("2026-06-25") == datetime.date(2026, 6, 25)
    assert parse_date("DEADLINE:2026-06-25") == datetime.date(2026, 6, 25)
    assert parse_date(datetime.date(2026, 6, 25)) == datetime.date(2026, 6, 25)
    assert parse_date(None) is None
    assert parse_date("invalid-date") is None


def test_parse_float() -> None:
    assert parse_float(1500.5) == 1500.5
    assert parse_float("1500.5") == 1500.5
    assert parse_float("VALUE:1500.5") == 1500.5
    assert parse_float("LOAD:2.5") == 2.5
    assert parse_float("EFFORT:1.5") == 1.5
    assert parse_float(None, 1.0) == 1.0
    assert parse_float("invalid-float", 1.0) == 1.0


def test_calculate_priority() -> None:
    ref = datetime.date(2026, 6, 23)
    # No deadline, not blocking, value = 1.0
    assert calculate_priority(False, None, 1.0, ref) == 1.0
    # Gate blocking factor = 1.5
    assert calculate_priority(True, None, 1.0, ref) == 1.5
    # Overdue/today deadline factor = 10.0
    assert calculate_priority(False, datetime.date(2026, 6, 23), 1.0, ref) == 10.0
    assert calculate_priority(False, datetime.date(2026, 6, 22), 1.0, ref) == 10.0
    # Future deadline: diff = 1 day, factor = 1 / (1 + 1) = 0.5
    assert calculate_priority(False, datetime.date(2026, 6, 24), 1.0, ref) == 0.5
    # Combination
    # Blocking (1.5) * Deadline (0.5) * Value (2.0) = 1.5
    assert calculate_priority(True, datetime.date(2026, 6, 24), 2.0, ref) == 1.5


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


def _make_engine_stub(pg_pool: asyncpg.Pool) -> Any:  # type: ignore[type-arg]
    class _EngineStub:
        pass

    stub = _EngineStub()
    stub.pg_pool = pg_pool  # type: ignore[attr-defined]
    return stub


async def _seed_data(
    pg_pool: asyncpg.Pool,
    ns_uuid: uuid.UUID,
    tasks: list[dict[str, Any]],
    employee_teams: dict[str, str],
) -> None:
    """Seed the database with graph nodes and edges for testing."""
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns_uuid)
            await seed_node_ownership_registry(conn, ns_uuid)

            # Insert BOM line node for generates edges
            bom_lbl = "BOM_LINE:TEST_BOM"
            await conn.execute(
                """
                INSERT INTO kg_nodes (label, entity_type, namespace_id)
                VALUES ($1, 'BOM_LINE', $2::uuid)
                ON CONFLICT (label, namespace_id) DO NOTHING
                """,
                bom_lbl,
                str(ns_uuid),
            )

            # Insert employee nodes and team nodes, and membership edges
            for emp, team in employee_teams.items():
                await conn.execute(
                    """
                    INSERT INTO kg_nodes (label, entity_type, namespace_id)
                    VALUES ($1, 'EMPLOYEE', $2::uuid)
                    ON CONFLICT (label, namespace_id) DO NOTHING
                    """,
                    emp,
                    str(ns_uuid),
                )
                await conn.execute(
                    """
                    INSERT INTO kg_nodes (label, entity_type, namespace_id)
                    VALUES ($1, 'TEAM', $2::uuid)
                    ON CONFLICT (label, namespace_id) DO NOTHING
                    """,
                    team,
                    str(ns_uuid),
                )
                await conn.execute(
                    """
                    INSERT INTO kg_edges
                        (subject_label, predicate, object_label, confidence, namespace_id)
                    VALUES ($1, 'member_of', $2, 1.0, $3::uuid)
                    ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
                    """,
                    emp,
                    team,
                    str(ns_uuid),
                )

            # Insert tasks and properties
            for t in tasks:
                label = t["label"]
                # 1. Insert TASK node
                await conn.execute(
                    """
                    INSERT INTO kg_nodes (label, entity_type, namespace_id)
                    VALUES ($1, 'PROJECT_TASK', $2::uuid)
                    ON CONFLICT (label, namespace_id) DO NOTHING
                    """,
                    label,
                    str(ns_uuid),
                )

                # 2. Open it with a generates edge from BOM
                await conn.execute(
                    """
                    INSERT INTO kg_edges
                        (subject_label, predicate, object_label, confidence, namespace_id)
                    VALUES ($1, 'generates', $2, 1.0, $3::uuid)
                    ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
                    """,
                    bom_lbl,
                    label,
                    str(ns_uuid),
                )

                # 3. Add attributes as edges
                if t.get("gate_blocking") is not None:
                    obj = "true" if t["gate_blocking"] else "false"
                    await conn.execute(
                        """
                        INSERT INTO kg_edges
                            (subject_label, predicate, object_label, confidence, namespace_id)
                        VALUES ($1, 'is_gate_blocking', $2, 1.0, $3::uuid)
                        ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
                        """,
                        label,
                        obj,
                        str(ns_uuid),
                    )

                if t.get("deadline") is not None:
                    await conn.execute(
                        """
                        INSERT INTO kg_edges
                            (subject_label, predicate, object_label, confidence, namespace_id)
                        VALUES ($1, 'has_deadline', $2, 1.0, $3::uuid)
                        ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
                        """,
                        label,
                        f"DEADLINE:{t['deadline']}",
                        str(ns_uuid),
                    )

                if t.get("value") is not None:
                    await conn.execute(
                        """
                        INSERT INTO kg_edges
                            (subject_label, predicate, object_label, confidence, namespace_id)
                        VALUES ($1, 'has_value', $2, 1.0, $3::uuid)
                        ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
                        """,
                        label,
                        f"VALUE:{t['value']}",
                        str(ns_uuid),
                    )

                if t.get("load") is not None:
                    await conn.execute(
                        """
                        INSERT INTO kg_edges
                            (subject_label, predicate, object_label, confidence, namespace_id)
                        VALUES ($1, 'has_load', $2, 1.0, $3::uuid)
                        ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
                        """,
                        label,
                        f"LOAD:{t['load']}",
                        str(ns_uuid),
                    )

                if t.get("assigned_to") is not None:
                    await conn.execute(
                        """
                        INSERT INTO kg_edges
                            (subject_label, predicate, object_label, confidence, namespace_id)
                        VALUES ($1, 'assigned_to', $2, 1.0, $3::uuid)
                        ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
                        """,
                        label,
                        t["assigned_to"],
                        str(ns_uuid),
                    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_do_my_day_ranks_correctly(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    engine = _make_engine_stub(pg_pool)
    ref = datetime.date(2026, 6, 23)

    tasks_seed = [
        # Priority = 1.5 (blocking) * 10.0 (overdue) * 2.0 (value) = 30.0
        {
            "label": "TASK:T1",
            "gate_blocking": True,
            "deadline": "2026-06-22",
            "value": 2.0,
        },
        # Priority = 1.0 (non-blocking) * 0.5 (future) * 10.0 (value) = 5.0
        {
            "label": "TASK:T2",
            "gate_blocking": False,
            "deadline": "2026-06-24",
            "value": 10.0,
        },
        # Priority = 1.0 * 1.0 (no deadline) * 1.0 (default value) = 1.0
        {
            "label": "TASK:T3",
            "gate_blocking": False,
        },
    ]

    await _seed_data(pg_pool, namespace_id, tasks_seed, {})

    res = await do_my_day(
        engine,
        {
            "namespace_id": str(namespace_id),
            "reference_date": ref.isoformat(),
        },
    )

    assert res["ok"] is True
    ranked = res["tasks"]
    assert len(ranked) == 3

    assert ranked[0]["task_label"] == "TASK:T1"
    assert abs(ranked[0]["priority"] - 30.0) < 1e-6

    assert ranked[1]["task_label"] == "TASK:T2"
    assert abs(ranked[1]["priority"] - 5.0) < 1e-6

    assert ranked[2]["task_label"] == "TASK:T3"
    assert abs(ranked[2]["priority"] - 1.0) < 1e-6


@pytest.mark.integration
@pytest.mark.asyncio
async def test_do_capacity_aggregates_per_team_over_window(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    engine = _make_engine_stub(pg_pool)

    employee_teams = {
        "EMPLOYEE:E1": "TEAM:Alpha",
        "EMPLOYEE:E2": "TEAM:Alpha",
        "EMPLOYEE:E3": "TEAM:Beta",
    }

    tasks_seed = [
        # In-window, Alpha: load = 1.5
        {
            "label": "TASK:T1",
            "deadline": "2026-06-25",
            "load": 1.5,
            "assigned_to": "EMPLOYEE:E1",
        },
        # In-window, Alpha: load = 2.0
        {
            "label": "TASK:T2",
            "deadline": "2026-06-26",
            "load": 2.0,
            "assigned_to": "EMPLOYEE:E2",
        },
        # In-window, Beta: load = 3.0
        {
            "label": "TASK:T3",
            "deadline": "2026-06-25",
            "load": 3.0,
            "assigned_to": "EMPLOYEE:E3",
        },
        # Out-of-window (before start): should be excluded
        {
            "label": "TASK:T4",
            "deadline": "2026-06-20",
            "load": 5.0,
            "assigned_to": "EMPLOYEE:E1",
        },
        # Out-of-window (after end): should be excluded
        {
            "label": "TASK:T5",
            "deadline": "2026-06-30",
            "load": 5.0,
            "assigned_to": "EMPLOYEE:E3",
        },
        # In-window, Unassigned employee: load = 1.0
        {
            "label": "TASK:T6",
            "deadline": "2026-06-26",
            "load": 1.0,
        },
        # No deadline (backlog): should be excluded when window is specified
        {
            "label": "TASK:T7",
            "load": 4.0,
            "assigned_to": "EMPLOYEE:E1",
        },
    ]

    await _seed_data(pg_pool, namespace_id, tasks_seed, employee_teams)

    # Date window: 2026-06-24 to 2026-06-28
    res = await do_capacity(
        engine,
        {
            "namespace_id": str(namespace_id),
            "start_date": "2026-06-24",
            "end_date": "2026-06-28",
        },
    )

    assert res["ok"] is True
    teams = res["teams"]

    # TEAM:Alpha should have T1 and T2: load = 1.5 + 2.0 = 3.5
    assert "TEAM:Alpha" in teams
    assert abs(teams["TEAM:Alpha"]["total_load"] - 3.5) < 1e-6
    assert len(teams["TEAM:Alpha"]["tasks"]) == 2
    assert teams["TEAM:Alpha"]["tasks"][0]["task_label"] == "TASK:T1"
    assert teams["TEAM:Alpha"]["tasks"][1]["task_label"] == "TASK:T2"

    # TEAM:Beta should have T3: load = 3.0
    assert "TEAM:Beta" in teams
    assert abs(teams["TEAM:Beta"]["total_load"] - 3.0) < 1e-6
    assert len(teams["TEAM:Beta"]["tasks"]) == 1
    assert teams["TEAM:Beta"]["tasks"][0]["task_label"] == "TASK:T3"

    # Unassigned should have T6: load = 1.0
    assert "Unassigned" in teams
    assert abs(teams["Unassigned"]["total_load"] - 1.0) < 1e-6
    assert len(teams["Unassigned"]["tasks"]) == 1
    assert teams["Unassigned"]["tasks"][0]["task_label"] == "TASK:T6"
