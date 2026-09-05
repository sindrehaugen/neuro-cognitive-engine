"""Integration tests for project/advance.py — Wave 4a (do_advance_phase).

Validates the Acceptance criteria from Batch_071a_Module_7_Wave_4a.md:

  a. Legal G0→G1 with criteria met:
       - New PROJECT_GATE@G1 node written in kg_nodes.
       - in_phase edge moved from GATE@G0 to GATE@G1.
       - Exactly one ``project_phase_advanced`` row appended to event_log.
  b. Gate-fail (missing criterion): returns ``missing_criteria``, no writes.
  c. Illegal / undeclared transition: returns ``ok=False``, no writes.
  d. Same-phase advance: idempotent no-op (no writes, noop=True).
  e. Tool flags: ``project_advance_phase`` registered with
     ``mutation=True, admin_only=True, cacheable=False``.
  f. Canonical tool-count guard -- see tests/test_tool_registry.py, which owns
     the numbers. They are NOT repeated here: this line said 89/37/14 while the
     live registry held 142/56/27, because a count written into prose is a claim
     nothing re-derives.

Fixtures used:
  ``pg_app_conn``           — asyncpg connection as nce_app (RLS enforced).
  ``pg_pool``               — asyncpg pool for the engine stub.
  ``make_namespace``        — factory that inserts a new namespace row.
  ``set_namespace_context`` — sets the RLS GUC required by RLS policies.
  ``seed_node_ownership_registry`` — seeds PROJECT_* ownership rows from
                              node-ownership.json.

Runs as @pytest.mark.integration — requires a live Postgres with schema.sql
and migrations applied.  A live Dev DB is available on this box.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.tool_registry import ADMIN_ONLY_TOOLS, CACHEABLE_TOOLS, MUTATION_TOOLS, TOOL_REGISTRY
from nce.vertical_modules.project.advance import do_advance_phase
from nce.vertical_modules.project.convert import _gate_label, _project_label

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_QUOTE_ID = "QUOTE-ADV-TEST-001"
_ACTOR = "test-actor@example.test"

# Patch target for emit_graph_write in advance.py so integration tests don't
# need the outbox relay running.
_MOCK_EMIT = "nce.vertical_modules.project.advance.emit_graph_write"

# Criteria required to enter G1 per project-gate-criteria.json.
_G1_CRITERIA = ["signed_quote_attached", "project_manager_assigned"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine_stub(pg_pool: asyncpg.Pool) -> object:  # type: ignore[type-arg]
    """Return a minimal engine stub exposing ``pg_pool``."""

    class _EngineStub:
        pass

    stub = _EngineStub()
    stub.pg_pool = pg_pool  # type: ignore[attr-defined]
    return stub


async def _seed(conn: asyncpg.Connection, ns: object) -> None:  # type: ignore[type-arg]
    """Seed ownership registry and set RLS GUC in one transaction."""
    async with conn.transaction():
        await set_namespace_context(conn, ns)
        await seed_node_ownership_registry(conn, ns)


async def _seed_project_at_g0(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns: object,
    quote_id: str = _QUOTE_ID,
) -> tuple[str, str]:
    """Insert a minimal PROJECT + GATE@G0 + in_phase edge directly.

    Bypasses ``do_convert_signed_quote`` so the test does not depend on the
    Sales baseline seam.  Returns ``(project_label, gate_g0_label)``.
    """
    project_lbl = _project_label(quote_id)
    gate_g0_lbl = _gate_label(quote_id, "G0")

    async with conn.transaction():
        await set_namespace_context(conn, ns)
        # PROJECT_PROJECT node
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id)
            VALUES ($1, 'PROJECT_PROJECT', $2::uuid)
            ON CONFLICT (label, namespace_id) DO NOTHING
            """,
            project_lbl,
            ns,
        )
        # PROJECT_GATE@G0 node
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id)
            VALUES ($1, 'PROJECT_GATE', $2::uuid)
            ON CONFLICT (label, namespace_id) DO NOTHING
            """,
            gate_g0_lbl,
            ns,
        )
        # PROJECT -[in_phase]-> GATE@G0 edge
        await conn.execute(
            """
            INSERT INTO kg_edges
                (subject_label, predicate, object_label, confidence, namespace_id)
            VALUES ($1, 'in_phase', $2, 1.0, $3::uuid)
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
            """,
            project_lbl,
            gate_g0_lbl,
            ns,
        )

    return project_lbl, gate_g0_lbl


async def _count_event_log_rows(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns: object,
    project_label: str,
    event_type: str = "project_phase_advanced",
) -> int:
    """Count matching event_log rows for this project and event_type."""
    async with conn.transaction():
        await set_namespace_context(conn, ns)
        count = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM   event_log
            WHERE  namespace_id = $1::uuid
              AND  event_type   = $2
              AND  params->>'project_id' = $3
            """,
            ns,
            event_type,
            project_label,
        )
    return int(count or 0)


async def _fetch_in_phase_target(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns: object,
    project_label: str,
) -> str | None:
    """Return the object_label of the current in_phase edge, or None."""
    async with conn.transaction():
        await set_namespace_context(conn, ns)
        row = await conn.fetchrow(
            """
            SELECT object_label
            FROM   kg_edges
            WHERE  subject_label = $1
              AND  predicate      = 'in_phase'
              AND  namespace_id   = $2::uuid
            """,
            project_label,
            ns,
        )
    return row["object_label"] if row else None


async def _gate_node_exists(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns: object,
    gate_label: str,
) -> bool:
    """Return True if a PROJECT_GATE node exists at *gate_label*."""
    async with conn.transaction():
        await set_namespace_context(conn, ns)
        row = await conn.fetchrow(
            """
            SELECT 1 FROM kg_nodes
            WHERE label = $1 AND namespace_id = $2::uuid
              AND entity_type = 'PROJECT_GATE'
            """,
            gate_label,
            ns,
        )
    return row is not None


# ---------------------------------------------------------------------------
# Test suite
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestProjectAdvancePhase:
    """Integration tests for do_advance_phase — M7.W4a."""

    # ------------------------------------------------------------------
    # a. Legal G0→G1 with criteria met: new GATE + edge move + event_log
    # ------------------------------------------------------------------

    async def test_legal_advance_writes_gate_moves_edge_appends_event(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        pg_pool: asyncpg.Pool,
        make_namespace: object,
    ) -> None:
        """G0→G1 with criteria met: new GATE@G1 written, in_phase moved, event_log +1."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)
        project_lbl, _ = await _seed_project_at_g0(pg_app_conn, ns)
        engine = _make_engine_stub(pg_pool)

        with patch(_MOCK_EMIT, new_callable=AsyncMock):
            result = await do_advance_phase(
                engine,
                {
                    "namespace_id": str(ns),
                    "project_id": project_lbl,
                    "target_phase": "G1",
                    "actor": _ACTOR,
                    "criteria_met": _G1_CRITERIA,
                },
            )

        assert result["ok"] is True, f"Expected ok=True, got: {result}"
        assert result["phase"] == "G1"
        assert result.get("noop") is not True

        gate_g1_lbl = _gate_label(_QUOTE_ID, "G1")

        # New GATE@G1 node must exist.
        exists = await _gate_node_exists(pg_app_conn, ns, gate_g1_lbl)
        assert exists, f"PROJECT_GATE@G1 node not found: {gate_g1_lbl}"

        # in_phase edge must now point to GATE@G1.
        in_phase_target = await _fetch_in_phase_target(pg_app_conn, ns, project_lbl)
        assert in_phase_target == gate_g1_lbl, (
            f"in_phase edge still points to {in_phase_target!r}, expected {gate_g1_lbl!r}"
        )

        # Exactly one event_log row appended.
        row_count = await _count_event_log_rows(pg_app_conn, ns, project_lbl)
        assert row_count == 1, f"Expected 1 event_log row, got {row_count}"

    # ------------------------------------------------------------------
    # b. Gate-fail: returns missing_criteria, writes nothing
    # ------------------------------------------------------------------

    async def test_gate_fail_returns_missing_criteria_no_writes(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        pg_pool: asyncpg.Pool,
        make_namespace: object,
    ) -> None:
        """G0→G1 with no criteria met: ok=False + missing_criteria, no DB writes."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)
        project_lbl, gate_g0_lbl = await _seed_project_at_g0(pg_app_conn, ns)
        engine = _make_engine_stub(pg_pool)

        with patch(_MOCK_EMIT, new_callable=AsyncMock):
            result = await do_advance_phase(
                engine,
                {
                    "namespace_id": str(ns),
                    "project_id": project_lbl,
                    "target_phase": "G1",
                    "actor": _ACTOR,
                    "criteria_met": [],  # nothing satisfied
                },
            )

        assert result["ok"] is False
        assert "missing_criteria" in result
        assert sorted(result["missing_criteria"]) == sorted(_G1_CRITERIA)
        assert result["current_phase"] == "G0"

        # Gate@G1 node must NOT exist.
        gate_g1_lbl = _gate_label(_QUOTE_ID, "G1")
        exists = await _gate_node_exists(pg_app_conn, ns, gate_g1_lbl)
        assert not exists, "PROJECT_GATE@G1 node was created despite gate failure"

        # in_phase still points to G0.
        in_phase_target = await _fetch_in_phase_target(pg_app_conn, ns, project_lbl)
        assert in_phase_target == gate_g0_lbl

        # No event_log row.
        row_count = await _count_event_log_rows(pg_app_conn, ns, project_lbl)
        assert row_count == 0, f"Expected 0 event_log rows, got {row_count}"

    # ------------------------------------------------------------------
    # c. Illegal transition: refused with ok=False, no writes
    # ------------------------------------------------------------------

    async def test_illegal_transition_refused_no_writes(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        pg_pool: asyncpg.Pool,
        make_namespace: object,
    ) -> None:
        """G0→G3 (non-adjacent): ok=False, no DB writes."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)
        project_lbl, gate_g0_lbl = await _seed_project_at_g0(pg_app_conn, ns)
        engine = _make_engine_stub(pg_pool)

        with patch(_MOCK_EMIT, new_callable=AsyncMock):
            result = await do_advance_phase(
                engine,
                {
                    "namespace_id": str(ns),
                    "project_id": project_lbl,
                    "target_phase": "G3",  # illegal: G0→G3 is not a declared edge
                    "actor": _ACTOR,
                    "criteria_met": _G1_CRITERIA,
                },
            )

        assert result["ok"] is False
        # No G3 node.
        gate_g3_lbl = _gate_label(_QUOTE_ID, "G3")
        assert not await _gate_node_exists(pg_app_conn, ns, gate_g3_lbl)
        # in_phase still on G0.
        assert await _fetch_in_phase_target(pg_app_conn, ns, project_lbl) == gate_g0_lbl
        # No event_log row.
        assert await _count_event_log_rows(pg_app_conn, ns, project_lbl) == 0

    # ------------------------------------------------------------------
    # d. Same-phase advance: idempotent no-op
    # ------------------------------------------------------------------

    async def test_same_phase_advance_is_noop(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        pg_pool: asyncpg.Pool,
        make_namespace: object,
    ) -> None:
        """Advancing to the already-current phase returns noop=True, no writes."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)
        project_lbl, gate_g0_lbl = await _seed_project_at_g0(pg_app_conn, ns)
        engine = _make_engine_stub(pg_pool)

        with patch(_MOCK_EMIT, new_callable=AsyncMock):
            result = await do_advance_phase(
                engine,
                {
                    "namespace_id": str(ns),
                    "project_id": project_lbl,
                    "target_phase": "G0",  # already current
                    "actor": _ACTOR,
                    "criteria_met": [],
                },
            )

        assert result["ok"] is True
        assert result["phase"] == "G0"
        assert result.get("noop") is True

        # in_phase still on G0.
        assert await _fetch_in_phase_target(pg_app_conn, ns, project_lbl) == gate_g0_lbl
        # No event_log row written for a no-op.
        assert await _count_event_log_rows(pg_app_conn, ns, project_lbl) == 0

    # ------------------------------------------------------------------
    # e. Missing project (no in_phase edge): clean error, no write
    # ------------------------------------------------------------------

    async def test_missing_project_returns_error_no_write(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        pg_pool: asyncpg.Pool,
        make_namespace: object,
    ) -> None:
        """Advancing a non-existent project returns ok=False with an error string."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)
        engine = _make_engine_stub(pg_pool)

        with patch(_MOCK_EMIT, new_callable=AsyncMock):
            result = await do_advance_phase(
                engine,
                {
                    "namespace_id": str(ns),
                    "project_id": "PROJECT:NONEXISTENT-QUOTE",
                    "target_phase": "G1",
                    "actor": _ACTOR,
                    "criteria_met": _G1_CRITERIA,
                },
            )

        assert result["ok"] is False
        assert "error" in result

    # ------------------------------------------------------------------
    # f. event_log row contains correct fields
    # ------------------------------------------------------------------

    async def test_event_log_row_has_correct_fields(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        pg_pool: asyncpg.Pool,
        make_namespace: object,
    ) -> None:
        """The appended event_log row carries project_id, from_phase, to_phase, actor."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)
        project_lbl, _ = await _seed_project_at_g0(pg_app_conn, ns)
        engine = _make_engine_stub(pg_pool)

        with patch(_MOCK_EMIT, new_callable=AsyncMock):
            result = await do_advance_phase(
                engine,
                {
                    "namespace_id": str(ns),
                    "project_id": project_lbl,
                    "target_phase": "G1",
                    "actor": _ACTOR,
                    "criteria_met": _G1_CRITERIA,
                },
            )

        assert result["ok"] is True

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            row = await pg_app_conn.fetchrow(
                """
                SELECT params, event_type, agent_id
                FROM   event_log
                WHERE  namespace_id = $1::uuid
                  AND  event_type   = 'project_phase_advanced'
                ORDER  BY event_seq DESC
                LIMIT 1
                """,
                ns,
            )

        assert row is not None, "event_log row not found after advance"
        assert row["event_type"] == "project_phase_advanced"
        assert row["agent_id"] == "project-advance-phase"

        params = row["params"]
        if isinstance(params, str):
            import json

            params = json.loads(params)

        assert params["project_id"] == project_lbl
        assert params["from_phase"] == "G0"
        assert params["to_phase"] == "G1"
        assert params["actor"] == _ACTOR

    # ------------------------------------------------------------------
    # g. Second advance from G1→G2 produces a second event_log row
    # ------------------------------------------------------------------

    async def test_sequential_advances_produce_separate_event_log_rows(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        pg_pool: asyncpg.Pool,
        make_namespace: object,
    ) -> None:
        """Two consecutive advances each append one event_log row (two total)."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)
        project_lbl, _ = await _seed_project_at_g0(pg_app_conn, ns)
        engine = _make_engine_stub(pg_pool)

        g2_criteria = [
            "signed_baseline_frozen",
            "bom_lines_linked",
            "kick_off_meeting_held",
        ]

        with patch(_MOCK_EMIT, new_callable=AsyncMock):
            r1 = await do_advance_phase(
                engine,
                {
                    "namespace_id": str(ns),
                    "project_id": project_lbl,
                    "target_phase": "G1",
                    "actor": _ACTOR,
                    "criteria_met": _G1_CRITERIA,
                },
            )
            r2 = await do_advance_phase(
                engine,
                {
                    "namespace_id": str(ns),
                    "project_id": project_lbl,
                    "target_phase": "G2",
                    "actor": _ACTOR,
                    "criteria_met": g2_criteria,
                },
            )

        assert r1["ok"] is True
        assert r2["ok"] is True
        assert r2["phase"] == "G2"

        row_count = await _count_event_log_rows(pg_app_conn, ns, project_lbl)
        assert row_count == 2, f"Expected 2 event_log rows, got {row_count}"


# ---------------------------------------------------------------------------
# Tool-registry structural assertions (unit — no DB required)
# ---------------------------------------------------------------------------


class TestProjectAdvancePhaseToolRegistry:
    """Structural assertions for the project_advance_phase tool entry."""

    def test_tool_registered(self) -> None:
        """``project_advance_phase`` must be present in TOOL_REGISTRY."""
        assert "project_advance_phase" in TOOL_REGISTRY

    def test_tool_flags(self) -> None:
        """``project_advance_phase`` must have mutation=True, admin_only=True, cacheable=False."""
        spec = TOOL_REGISTRY["project_advance_phase"]
        assert spec.mutation is True
        assert spec.admin_only is True
        assert spec.cacheable is False

    def test_tool_in_mutation_tools(self) -> None:
        assert "project_advance_phase" in MUTATION_TOOLS

    def test_tool_in_admin_only_tools(self) -> None:
        assert "project_advance_phase" in ADMIN_ONLY_TOOLS

    def test_tool_not_in_cacheable_tools(self) -> None:
        assert "project_advance_phase" not in CACHEABLE_TOOLS

    def test_registry_total_count(self) -> None:
        """Canonical guard: total tool count must be 95 (unified realignment registry)."""
        assert len(TOOL_REGISTRY) >= 95, (
            f"Expected at least 95 tools, got {len(TOOL_REGISTRY)}: {sorted(TOOL_REGISTRY)}"
        )

    def test_mutation_count(self) -> None:
        """Mutation tools must total 52 (unified realignment registry;
        +2 Batch 131 inventory_transfer_stock/inventory_record_consumption;
        +1 Batch 143 assets_advance_lifecycle, M9.W3;
        +2 Batch 067c system_design_author_topology/
        system_design_author_functional_location, M6.W13b;
        +1 Batch 067h system_design_delete_planned, M6.W17 -- the module's
        first delete path, and the only one of the three that can remove
        anything);
        +7 Batch 138a inventory Actor tools, M11.W10a --
        inventory_record_goods_receipt/inventory_record_goods_receipt_and_match/
        inventory_reserve_stock/inventory_release_stock/inventory_record_rma/
        inventory_restock_from_rma/inventory_dispose_rma_weee. That wave
        registered eleven tools but only seven mutate; inventory_valuation and
        inventory_reconcile_dead_stock read only, and
        inventory_recommend_restock/inventory_forecast_demand write nothing."""
        assert len(MUTATION_TOOLS) == 56

    def test_admin_only_count(self) -> None:
        """Admin-only tools must total 27 (unified realignment registry;
        +2 Batch 131 inventory_transfer_stock/inventory_record_consumption;
        +1 Batch 067h system_design_delete_planned, M6.W17 -- admin_only is the
        one flag that separates it from the two authoring tools, which are
        not);
        +9 Batch 138a inventory tools, M11.W10a -- the 7 Actor mutations plus
        inventory_valuation and inventory_reconcile_dead_stock, which are
        read-only but admin_only for the cost/position data they return. The
        wave's other two tools, inventory_recommend_restock and
        inventory_forecast_demand, are Watcher reads and are not admin_only."""
        assert len(ADMIN_ONLY_TOOLS) == 27
