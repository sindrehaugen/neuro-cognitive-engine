"""Integration tests for Project convert — Wave 4 (do_convert_signed_quote).

Validates:
  a. Conversion creates PROJECT_PROJECT node (deterministic label from quote_id).
  b. Conversion creates PROJECT_GATE node at G0.
  c. Conversion creates an initial PROJECT_TASK node.
  d. PROJECT -[in_phase]-> GATE@G0 edge is written.
  e. PROJECT -[contains]-> BOM_LINE edges are written onto existing BOM_LINE nodes.
  e2. Zero BOM_LINE nodes → result is flagged ``degraded`` with a reason code
     (see tests/unit/test_project_convert_degraded.py for the DB-free suite).
  f. The Sales-frozen baseline is referenced by id only (no SIGNED_BASELINE node
     created; no project_signed_baselines object written).
  g. Idempotent on quote_id: a second call returns the same project_id and
     produces no duplicate nodes or edges.
  h. No SIGNED_BASELINE node is ever created in kg_nodes.
  i. No project_signed_baselines table exists (§9.1 hard stop).
  j. Graceful degradation when Sales baseline is unavailable (NotImplementedError).
  k. OwnershipError raised for unseeded namespace.

Fixtures used:
  ``pg_app_conn``         — asyncpg connection as nce_app (RLS enforced).
  ``make_namespace``      — factory that inserts a new namespace row.
  ``set_namespace_context`` (nce.auth) — sets the GUC required by RLS.
  ``seed_node_ownership_registry`` — seeds PROJECT_*, product, procurement,
                          system_design ownership rows from node-ownership.json.

Runs as @pytest.mark.integration — requires a live Postgres with schema.sql
and migrations applied (run scratch/_apply_probe_b032.py first).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.entity_resolution.ownership import OwnershipError
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.project.convert import (
    _bom_line_label,
    _fetch_bom_line_labels,
    _gate_label,
    _project_label,
    _task_label,
    do_convert_signed_quote,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_QUOTE_ID = "QUOTE-TEST-W4-001"
_SIGNED_BY = "sindre.haugen@bravosteps.com"
_SIGNATURE_REF = "SIG-REF-TEST-001"

# Fake Sales-frozen baseline row returned by the mock.
_FAKE_BASELINE_ID = "baseline-uuid-0001"
_FAKE_BASELINE_ROW: dict = {
    "id": _FAKE_BASELINE_ID,
    "quote_id": _QUOTE_ID,
    "signed_margin_pct": 0.35,
    "signed_total_nok": 500_000.0,
    "signed_at": "2026-06-22T10:00:00Z",
}

# Patch targets.
_MOCK_BASELINE = "nce.vertical_modules.project.convert._read_signed_baseline"
_MOCK_EMIT = "nce.vertical_modules.project.convert.emit_graph_write"

# BOM_LINE references to seed.
_BOM_REFS = ["LINE-A", "LINE-B"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed(conn: asyncpg.Connection, ns: object) -> None:  # type: ignore[type-arg]
    """Seed ownership registry and set namespace GUC in one transaction."""
    async with conn.transaction():
        await set_namespace_context(conn, ns)
        await seed_node_ownership_registry(conn, ns)


async def _seed_bom_lines(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns: object,
    quote_id: str,
    line_refs: list[str],
) -> list[str]:
    """Insert BOM_LINE nodes directly (owned by System Design / Sales engine).

    The project engine never re-creates these — it edges onto them.  We
    insert them here using the superuser/owner connection to bypass RLS.
    Returns the list of inserted labels.
    """
    labels: list[str] = []
    async with conn.transaction():
        await set_namespace_context(conn, ns)
        for ref in line_refs:
            label = _bom_line_label(quote_id, ref)
            await conn.execute(
                """
                INSERT INTO kg_nodes (label, entity_type, namespace_id)
                VALUES ($1, 'BOM_LINE', $2::uuid)
                ON CONFLICT (label, namespace_id) DO NOTHING
                """,
                label,
                ns,
            )
            labels.append(label)
    return labels


def _make_engine_stub(pg_pool: asyncpg.Pool) -> object:  # type: ignore[type-arg]
    """Return a minimal engine stub exposing ``pg_pool``."""

    class _EngineStub:
        pass

    stub = _EngineStub()
    stub.pg_pool = pg_pool  # type: ignore[attr-defined]
    return stub


async def _convert(
    engine: object,
    ns: object,
    quote_id: str = _QUOTE_ID,
    *,
    baseline_row: dict | None = _FAKE_BASELINE_ROW,
) -> dict:
    """Run do_convert_signed_quote with a mocked baseline and emit."""
    params = {
        "namespace_id": str(ns),
        "quote_id": quote_id,
        "signed_by": _SIGNED_BY,
        "signature_ref": _SIGNATURE_REF,
    }

    async def _fake_baseline(eng, ns_id, q_id):  # noqa: ARG001
        return baseline_row

    with (
        patch(_MOCK_EMIT, new_callable=AsyncMock),
        patch(_MOCK_BASELINE, side_effect=_fake_baseline),
    ):
        return await do_convert_signed_quote(engine, params)


# ---------------------------------------------------------------------------
# Test suite
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestProjectConvert:
    """Integration tests for project/convert.py Wave 4."""

    # ------------------------------------------------------------------
    # a. PROJECT_PROJECT node is created
    # ------------------------------------------------------------------

    async def test_project_node_created(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        pg_pool: asyncpg.Pool,
        make_namespace: object,
    ) -> None:
        """PROJECT_PROJECT node exists in kg_nodes after conversion."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)
        engine = _make_engine_stub(pg_pool)

        result = await _convert(engine, ns)

        expected_label = _project_label(_QUOTE_ID)
        assert result["project_id"] == expected_label

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            row = await pg_app_conn.fetchrow(
                "SELECT entity_type FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                expected_label,
                ns,
            )
        assert row is not None, f"PROJECT_PROJECT node missing: {expected_label}"
        assert row["entity_type"] == "PROJECT_PROJECT"

    # ------------------------------------------------------------------
    # b. PROJECT_GATE at G0 is created
    # ------------------------------------------------------------------

    async def test_gate_g0_node_created(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        pg_pool: asyncpg.Pool,
        make_namespace: object,
    ) -> None:
        """PROJECT_GATE@G0 node exists in kg_nodes after conversion."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)
        engine = _make_engine_stub(pg_pool)

        result = await _convert(engine, ns)
        assert result["gate"] == "G0"

        gate_label = _gate_label(_QUOTE_ID, "G0")
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            row = await pg_app_conn.fetchrow(
                "SELECT entity_type FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                gate_label,
                ns,
            )
        assert row is not None, f"PROJECT_GATE node missing: {gate_label}"
        assert row["entity_type"] == "PROJECT_GATE"

    # ------------------------------------------------------------------
    # c. Initial PROJECT_TASK node is created
    # ------------------------------------------------------------------

    async def test_initial_task_node_created(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        pg_pool: asyncpg.Pool,
        make_namespace: object,
    ) -> None:
        """Initial PROJECT_TASK (INIT:000) node exists in kg_nodes."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)
        engine = _make_engine_stub(pg_pool)

        await _convert(engine, ns)

        task_label = _task_label(_QUOTE_ID, 0)
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            row = await pg_app_conn.fetchrow(
                "SELECT entity_type FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                task_label,
                ns,
            )
        assert row is not None, f"PROJECT_TASK node missing: {task_label}"
        assert row["entity_type"] == "PROJECT_TASK"

    # ------------------------------------------------------------------
    # d. PROJECT -[in_phase]-> GATE@G0 edge is written
    # ------------------------------------------------------------------

    async def test_in_phase_edge_written(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        pg_pool: asyncpg.Pool,
        make_namespace: object,
    ) -> None:
        """PROJECT -[in_phase]-> GATE@G0 edge exists in kg_edges."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)
        engine = _make_engine_stub(pg_pool)

        await _convert(engine, ns)

        project_label = _project_label(_QUOTE_ID)
        gate_label = _gate_label(_QUOTE_ID, "G0")

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            edge = await pg_app_conn.fetchrow(
                """
                SELECT confidence FROM kg_edges
                WHERE subject_label = $1
                  AND predicate     = 'in_phase'
                  AND object_label  = $2
                  AND namespace_id  = $3
                """,
                project_label,
                gate_label,
                ns,
            )
        assert edge is not None, "in_phase edge PROJECT->GATE@G0 missing"
        assert 0.0 <= edge["confidence"] <= 1.0

    # ------------------------------------------------------------------
    # e. PROJECT -[contains]-> BOM_LINE edges are written
    # ------------------------------------------------------------------

    async def test_contains_edges_written_for_bom_lines(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        pg_pool: asyncpg.Pool,
        make_namespace: object,
    ) -> None:
        """contains edges are written onto existing BOM_LINE nodes."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)
        engine = _make_engine_stub(pg_pool)

        bom_labels = await _seed_bom_lines(pg_app_conn, ns, _QUOTE_ID, _BOM_REFS)

        result = await _convert(engine, ns)
        assert result["bom_lines_linked"] == len(_BOM_REFS)
        # A fully-populated conversion must NOT be flagged degraded.
        assert result["degraded"] is False
        assert result["degraded_reasons"] == []

        project_label = _project_label(_QUOTE_ID)

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            for bom_label in bom_labels:
                edge = await pg_app_conn.fetchrow(
                    """
                    SELECT confidence FROM kg_edges
                    WHERE subject_label = $1
                      AND predicate     = 'contains'
                      AND object_label  = $2
                      AND namespace_id  = $3
                    """,
                    project_label,
                    bom_label,
                    ns,
                )
                assert edge is not None, f"contains edge missing for BOM_LINE {bom_label}"
                assert 0.0 <= edge["confidence"] <= 1.0

    # ------------------------------------------------------------------
    # e2. Zero BOM lines is reported as degraded, end-to-end
    # ------------------------------------------------------------------

    async def test_zero_bom_lines_reported_as_degraded(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        pg_pool: asyncpg.Pool,
        make_namespace: object,
    ) -> None:
        """Converting a quote with no BOM_LINE nodes flags the result degraded.

        This is the live production case: nothing in NCE creates BOM_LINE
        nodes, so every real conversion lands here.  Without the flag the
        payload is shaped exactly like a fully-populated conversion and the
        caller gets a project with an empty bill of materials and no signal.
        """
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)
        engine = _make_engine_stub(pg_pool)

        # Deliberately seed NO BOM_LINE nodes.
        result = await _convert(engine, ns)

        assert result["bom_lines_linked"] == 0
        assert result["degraded"] is True, (
            "Conversion linked zero BOM lines but reported no degradation — "
            "a caller cannot tell this apart from a fully-populated project."
        )
        assert "no_bom_lines_in_graph" in result["degraded_reasons"]
        assert result["degraded_detail"]

        # The project itself is still created — degraded, not failed.
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            row = await pg_app_conn.fetchrow(
                "SELECT entity_type FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                _project_label(_QUOTE_ID),
                ns,
            )
        assert row is not None, "degraded conversion must still create the PROJECT node"

    # ------------------------------------------------------------------
    # f. Baseline referenced by id only — no SIGNED_BASELINE node created
    # ------------------------------------------------------------------

    async def test_baseline_referenced_by_id_only_no_signed_baseline_node(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        pg_pool: asyncpg.Pool,
        make_namespace: object,
    ) -> None:
        """Conversion references baseline id but never writes a SIGNED_BASELINE node."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)
        engine = _make_engine_stub(pg_pool)

        result = await _convert(engine, ns)

        # Baseline id is returned in the result (referenced, not created).
        assert result["baseline"]["signed_baseline_id"] == _FAKE_BASELINE_ID
        assert result["baseline"]["sales_available"] is True

        # No SIGNED_BASELINE node must exist.
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            count = await pg_app_conn.fetchval(
                """
                SELECT COUNT(*) FROM kg_nodes
                WHERE entity_type = 'SIGNED_BASELINE'
                  AND namespace_id = $1
                """,
                ns,
            )
        assert count == 0, (
            f"SIGNED_BASELINE nodes found ({count}) — §9.1 violation: "
            "do_convert_signed_quote must never write a SIGNED_BASELINE node"
        )

    # ------------------------------------------------------------------
    # g. Idempotent: second call returns same project_id, no duplicates
    # ------------------------------------------------------------------

    async def test_idempotent_on_quote_id(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        pg_pool: asyncpg.Pool,
        make_namespace: object,
    ) -> None:
        """Two conversions of the same quote_id produce exactly one row per node/edge."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)
        engine = _make_engine_stub(pg_pool)

        await _seed_bom_lines(pg_app_conn, ns, _QUOTE_ID, _BOM_REFS)

        result_1 = await _convert(engine, ns)
        result_2 = await _convert(engine, ns)

        # Both calls return the same project_id.
        assert result_1["project_id"] == result_2["project_id"]

        project_label = _project_label(_QUOTE_ID)
        gate_label = _gate_label(_QUOTE_ID, "G0")
        bom_label_a = _bom_line_label(_QUOTE_ID, _BOM_REFS[0])

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)

            # Exactly one PROJECT_PROJECT node.
            node_count = await pg_app_conn.fetchval(
                "SELECT COUNT(*) FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                project_label,
                ns,
            )
            assert node_count == 1, f"Expected 1 PROJECT_PROJECT node, got {node_count}"

            # Exactly one in_phase edge.
            phase_edge_count = await pg_app_conn.fetchval(
                """
                SELECT COUNT(*) FROM kg_edges
                WHERE subject_label = $1
                  AND predicate     = 'in_phase'
                  AND object_label  = $2
                  AND namespace_id  = $3
                """,
                project_label,
                gate_label,
                ns,
            )
            assert phase_edge_count == 1, f"Expected 1 in_phase edge, got {phase_edge_count}"

            # Exactly one contains edge per BOM_LINE.
            contains_edge_count = await pg_app_conn.fetchval(
                """
                SELECT COUNT(*) FROM kg_edges
                WHERE subject_label = $1
                  AND predicate     = 'contains'
                  AND object_label  = $2
                  AND namespace_id  = $3
                """,
                project_label,
                bom_label_a,
                ns,
            )
            assert contains_edge_count == 1, (
                f"Expected 1 contains edge for {bom_label_a}, got {contains_edge_count}"
            )

    # ------------------------------------------------------------------
    # h. No SIGNED_BASELINE node ever — structural assertion
    # ------------------------------------------------------------------

    async def test_no_signed_baseline_node_written_ever(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        pg_pool: asyncpg.Pool,
        make_namespace: object,
    ) -> None:
        """After conversion, no node with entity_type SIGNED_BASELINE exists."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)
        engine = _make_engine_stub(pg_pool)

        await _convert(engine, ns)

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            row = await pg_app_conn.fetchrow(
                "SELECT label FROM kg_nodes WHERE entity_type = 'SIGNED_BASELINE' AND namespace_id = $1",
                ns,
            )
        assert row is None, (
            "§9.1 violated: do_convert_signed_quote wrote a SIGNED_BASELINE node "
            f"with label={row['label'] if row else 'n/a'!r}"
        )

    # ------------------------------------------------------------------
    # i. No project_signed_baselines table exists (§9.1 hard stop)
    # ------------------------------------------------------------------

    async def test_no_project_signed_baselines_table(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        """§9.1: project_signed_baselines table must not exist in the schema."""
        row = await pg_app_conn.fetchrow(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name   = 'project_signed_baselines'
            """
        )
        assert row is None, (
            "§9.1 violated: project_signed_baselines table exists — "
            "the signed baseline is owned by Sales, not Project."
        )

    # ------------------------------------------------------------------
    # j. Graceful degradation when Sales baseline unavailable
    # ------------------------------------------------------------------

    async def test_degrades_gracefully_when_baseline_unavailable(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        pg_pool: asyncpg.Pool,
        make_namespace: object,
    ) -> None:
        """When _read_signed_baseline raises NotImplementedError, degrade cleanly."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)
        engine = _make_engine_stub(pg_pool)

        params = {
            "namespace_id": str(ns),
            "quote_id": _QUOTE_ID,
            "signed_by": _SIGNED_BY,
            "signature_ref": _SIGNATURE_REF,
        }

        # Do NOT mock the baseline — let it raise NotImplementedError (default).
        with patch(_MOCK_EMIT, new_callable=AsyncMock):
            result = await do_convert_signed_quote(engine, params)

        # Must not raise; must return a valid result with degraded baseline.
        assert result["project_id"] == _project_label(_QUOTE_ID)
        assert result["baseline"]["sales_available"] is False
        assert result["baseline"]["signed_baseline_id"] is None
        # The degradation is reported explicitly, not just implied by
        # sales_available=False.
        assert result["degraded"] is True
        assert "sales_baseline_unavailable" in result["degraded_reasons"]

        # PROJECT node must still exist (conversion proceeds despite no baseline).
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            row = await pg_app_conn.fetchrow(
                "SELECT entity_type FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                _project_label(_QUOTE_ID),
                ns,
            )
        assert row is not None, "PROJECT_PROJECT node must be created even with degraded baseline"

    # ------------------------------------------------------------------
    # k. OwnershipError raised for unseeded namespace
    # ------------------------------------------------------------------

    async def test_raises_ownership_error_for_unseeded_namespace(
        self,
        pg_pool: asyncpg.Pool,
        make_namespace: object,
    ) -> None:
        """do_convert_signed_quote raises OwnershipError on an unseeded namespace."""
        ns = await make_namespace()  # type: ignore[operator]
        # Intentionally skip seed_node_ownership_registry.
        engine = _make_engine_stub(pg_pool)

        params = {
            "namespace_id": str(ns),
            "quote_id": _QUOTE_ID,
            "signed_by": _SIGNED_BY,
            "signature_ref": _SIGNATURE_REF,
        }

        async def _fake_baseline(eng, ns_id, q_id):  # noqa: ARG001
            return _FAKE_BASELINE_ROW

        with (
            patch(_MOCK_EMIT, new_callable=AsyncMock),
            patch(_MOCK_BASELINE, side_effect=_fake_baseline),
            pytest.raises(OwnershipError) as exc_info,
        ):
            await do_convert_signed_quote(engine, params)

        err = exc_info.value
        assert err.node_type == "PROJECT_PROJECT", (
            f"Expected node_type='PROJECT_PROJECT', got {err.node_type!r}"
        )
        assert err.owner_engine is None, (
            "Deny-by-default: owner_engine must be None for unseeded namespace"
        )


# ---------------------------------------------------------------------------
# Regression tests: _fetch_bom_line_labels used to build a raw SQL LIKE
# pattern from a caller-supplied quote_id. `_` and `%` are LIKE
# metacharacters -- a quote id containing either would silently widen the
# match to a DIFFERENT quote's BOM lines, so the WRONG quote's lines would
# get edged onto a converted project (confirmed live:
# 'BOM_LINE:QA1:AMP01' LIKE 'BOM_LINE:Q_1:%' is true). Fixed via a literal
# starts_with() prefix test -- mirrors economy/cascade.py's
# _read_actual_cost_total (Batch 120).
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestFetchBomLineLabelsWildcardSafety:
    async def test_underscore_in_quote_id_does_not_fetch_another_quotes_lines(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """Reproduces the exact live scenario:
        'BOM_LINE:QA1:AMP01' LIKE 'BOM_LINE:Q_1:%' is true under raw LIKE
        because `_` matches any single character. Seeds two quotes whose ids
        differ only at the position an unescaped `_` would wildcard-match,
        and asserts the fetch for the underscore quote returns ONLY its own
        BOM line -- never the victim's."""
        ns = await make_namespace()  # type: ignore[operator]
        suffix = uuid.uuid4().hex[:8]
        quote_with_underscore = f"QU_{suffix}"  # contains a literal '_'
        quote_collision_victim = f"QUZ{suffix}"  # same length, 'Z' where '_' falls

        label_own = _bom_line_label(quote_with_underscore, "AMP01")
        label_victim = _bom_line_label(quote_collision_victim, "AMP01")

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            for label in (label_own, label_victim):
                await pg_app_conn.execute(
                    """
                    INSERT INTO kg_nodes (label, entity_type, namespace_id)
                    VALUES ($1, 'BOM_LINE', $2::uuid)
                    ON CONFLICT (label, namespace_id) DO NOTHING
                    """,
                    label,
                    ns,
                )

            labels = await _fetch_bom_line_labels(pg_app_conn, ns, quote_with_underscore)

        assert labels == [label_own], (
            f"quote {quote_with_underscore!r} picked up another quote's BOM line "
            f"via an unescaped LIKE '_' wildcard: {labels}"
        )

    async def test_percent_in_quote_id_does_not_fetch_another_quotes_lines(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """Same defect class as the underscore case, but for `%` (matches any
        sequence of characters, including zero)."""
        ns = await make_namespace()  # type: ignore[operator]
        suffix = uuid.uuid4().hex[:8]
        quote_with_percent = f"QP%{suffix}"  # contains a literal '%'
        quote_collision_victim = f"QPZZZZ{suffix}"  # extra chars where '%' would match

        label_own = _bom_line_label(quote_with_percent, "AMP01")
        label_victim = _bom_line_label(quote_collision_victim, "AMP01")

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            for label in (label_own, label_victim):
                await pg_app_conn.execute(
                    """
                    INSERT INTO kg_nodes (label, entity_type, namespace_id)
                    VALUES ($1, 'BOM_LINE', $2::uuid)
                    ON CONFLICT (label, namespace_id) DO NOTHING
                    """,
                    label,
                    ns,
                )

            labels = await _fetch_bom_line_labels(pg_app_conn, ns, quote_with_percent)

        assert labels == [label_own], (
            f"quote {quote_with_percent!r} picked up another quote's BOM line "
            f"via an unescaped LIKE '%' wildcard: {labels}"
        )

    async def test_ordinary_quote_id_still_fetches_its_own_lines_only(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """No wildcard characters at all -- the common case must be
        completely unaffected by the switch from LIKE to starts_with()."""
        ns = await make_namespace()  # type: ignore[operator]
        quote_id = f"Q-ORD-{uuid.uuid4().hex[:8]}"
        other_quote_id = f"Q-OTHER-{uuid.uuid4().hex[:8]}"

        label_a = _bom_line_label(quote_id, "AMP01")
        label_b = _bom_line_label(quote_id, "CABLE01")
        label_other = _bom_line_label(other_quote_id, "AMP01")

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            for label in (label_a, label_b, label_other):
                await pg_app_conn.execute(
                    """
                    INSERT INTO kg_nodes (label, entity_type, namespace_id)
                    VALUES ($1, 'BOM_LINE', $2::uuid)
                    ON CONFLICT (label, namespace_id) DO NOTHING
                    """,
                    label,
                    ns,
                )

            labels = await _fetch_bom_line_labels(pg_app_conn, ns, quote_id)

        assert sorted(labels) == sorted([label_a, label_b])
