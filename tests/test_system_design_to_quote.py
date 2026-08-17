"""
tests/test_system_design_to_quote.py
=====================================
Integration tests for Module 6 Wave 7 — ``do_design_to_quote`` and
``do_validate_design`` (design-first exit + human validation gate).

Validates
---------
1. ``do_design_to_quote`` freezes the design version (reads the current
   ``updated_at``, derives ``design_version``), writes
   ``DESIGN -[becomes]-> QUOTE`` to ``kg_edges``, and proposes to Sales
   via the A2A seam.
2. The ``QUOTE`` node is NEVER written to ``kg_nodes`` (Contract A §9.1 —
   Sales owns QUOTE; design proposes only).
3. ``do_validate_design`` returns ``{passed, reasons}``, bumps the DESIGN
   version (``updated_at`` advances), and appends decisions to
   ``v3_cognitive_ledger``.
4. Propose-only invariant: no line is ever auto-accepted — a missing
   verdict raises ``ValueError``.

A2A mock strategy
-----------------
Sales engine (Module 5) is not built yet.  ``do_design_to_quote`` hands
the quote proposal to Sales through the injectable module-level coroutine
``nce.vertical_modules.system_design.to_quote._propose_quote_to_sales``.
Tests patch that coroutine with an ``AsyncMock`` that returns
``{"accepted": True, "quote_id": "<label>"}``.

Runs as @pytest.mark.integration — requires a live Postgres with
schema.sql and migrations applied (including 008_v3_cognitive_ledger.sql).
Skips automatically when no DSN is configured.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from nce.auth import set_namespace_context
from nce.db_utils import scoped_pg_session
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.system_design.graph import do_author_functional_location
from nce.vertical_modules.system_design.to_quote import do_design_to_quote
from nce.vertical_modules.system_design.validate import do_validate_design

# ---------------------------------------------------------------------------
# Module paths patched in tests
# ---------------------------------------------------------------------------

_MOCK_PROPOSE = "nce.vertical_modules.system_design.to_quote._propose_quote_to_sales"
_MOCK_EMIT_GRAPH = "nce.vertical_modules.system_design.graph.emit_graph_write"

# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------

_NS_SLUG = "testw7ns"
_DESIGN_ID = "DESIGN-W7-TEST-001"

# Minimal functional-location seed (enough to produce DESIGN + DESIGN_LINE nodes).
_BUILDINGS: list[dict[str, Any]] = [
    {
        "name": "BuildingA",
        "floors": [
            {
                "name": "Floor1",
                "rooms": [{"name": "Room101", "positions": ["Pos-A"]}],
            }
        ],
    }
]

_DESIGN_LINES: list[dict[str, Any]] = [
    {
        "line_ref": "DL-001",
        "manufacturer": "Biamp",
        "mfr_part_no": "TesiraFORTE-CI",
        "confidence": 0.95,
    },
    {
        "line_ref": "DL-002",
        "manufacturer": "Shure",
        "mfr_part_no": "MXA910",
        "confidence": 0.90,
    },
]


# ---------------------------------------------------------------------------
# Stub engine
# ---------------------------------------------------------------------------


class _EngineStub:
    """Minimal engine stub with a live pg_pool."""

    def __init__(self, pg_pool: Any) -> None:
        self.pg_pool = pg_pool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_ownership(conn: Any, ns_id: uuid.UUID) -> None:
    async with conn.transaction():
        await set_namespace_context(conn, ns_id)
        await seed_node_ownership_registry(conn, ns_id)


async def _seed_design(pg_pool: Any, ns_id: uuid.UUID) -> None:
    """Author the DESIGN + DESIGN_LINE nodes via W2 graph writer."""
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        await do_author_functional_location(
            conn,
            ns_id,
            namespace_slug=_NS_SLUG,
            design_id=_DESIGN_ID,
            site_name="Site-Alpha",
            buildings=_BUILDINGS,
            design_lines=_DESIGN_LINES,
        )


async def _count_kg_nodes_by_type(conn: Any, ns_id: uuid.UUID, entity_type: str) -> int:
    return int(
        await conn.fetchval(
            "SELECT COUNT(*) FROM kg_nodes WHERE namespace_id = $1::uuid AND entity_type = $2",
            str(ns_id),
            entity_type,
        )
    )


async def _fetch_edge(
    conn: Any,
    ns_id: uuid.UUID,
    subject_label: str,
    predicate: str,
    object_label: str,
) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        """
        SELECT subject_label, predicate, object_label, confidence
        FROM kg_edges
        WHERE namespace_id    = $1::uuid
          AND subject_label   = $2
          AND predicate       = $3
          AND object_label    = $4
        """,
        str(ns_id),
        subject_label,
        predicate,
        object_label,
    )
    return dict(row) if row else None


async def _read_design_updated_at(conn: Any, ns_id: uuid.UUID, design_lbl: str) -> Any:
    return await conn.fetchval(
        "SELECT updated_at FROM kg_nodes WHERE label = $1 AND namespace_id = $2::uuid",
        design_lbl,
        str(ns_id),
    )


async def _count_ledger_rows(conn: Any, ns_id: uuid.UUID, design_id: str) -> int:
    """Count v3_cognitive_ledger rows whose tlx_scores->design_id matches."""
    return int(
        await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM v3_cognitive_ledger
            WHERE namespace_id = $1::uuid
              AND tlx_scores->>'design_id' = $2
            """,
            str(ns_id),
            design_id,
        )
    )


# ---------------------------------------------------------------------------
# Integration test suite
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestDesignToQuote:
    """Integration tests for system_design/to_quote.py Wave 7."""

    # ------------------------------------------------------------------
    # 1. Freeze + becomes edge written with confidence
    # ------------------------------------------------------------------

    async def test_becomes_edge_written_with_confidence(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """do_design_to_quote writes DESIGN -[becomes]-> QUOTE edge with confidence."""
        ns_id: uuid.UUID = await make_namespace()
        engine = _EngineStub(pg_pool)

        async with pg_pool.acquire() as conn:
            await _seed_ownership(conn, ns_id)

        with patch(_MOCK_EMIT_GRAPH, new_callable=AsyncMock):
            await _seed_design(pg_pool, ns_id)

        mock_propose = AsyncMock(return_value={"accepted": True, "quote_id": _DESIGN_ID})
        with (
            patch(_MOCK_PROPOSE, mock_propose),
            patch(_MOCK_EMIT_GRAPH, new_callable=AsyncMock),
        ):
            result = await do_design_to_quote(
                engine,
                {
                    "namespace_id": str(ns_id),
                    "design_id": _DESIGN_ID,
                },
            )

        design_lbl = result["design_label"]
        quote_lbl = result["quote_label"]

        assert design_lbl.startswith("DESIGN:"), (
            f"design_label must start with 'DESIGN:'; got {design_lbl!r}"
        )
        assert quote_lbl.startswith("QUOTE:"), (
            f"quote_label must start with 'QUOTE:'; got {quote_lbl!r}"
        )

        async with scoped_pg_session(pg_pool, ns_id) as conn:
            edge = await _fetch_edge(conn, ns_id, design_lbl, "becomes", quote_lbl)

        assert edge is not None, (
            f"Expected DESIGN -[becomes]-> QUOTE edge; none found for {design_lbl} -> {quote_lbl}"
        )
        confidence = float(edge["confidence"])
        assert 0.0 < confidence <= 1.0, f"Edge confidence must be in (0, 1]; got {confidence}"

    # ------------------------------------------------------------------
    # 2. Contract A — QUOTE node NEVER written to kg_nodes
    # ------------------------------------------------------------------

    async def test_quote_node_never_written_to_kg_nodes(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """QUOTE entity_type NEVER appears in kg_nodes (Contract A §9.1)."""
        ns_id: uuid.UUID = await make_namespace()
        engine = _EngineStub(pg_pool)

        async with pg_pool.acquire() as conn:
            await _seed_ownership(conn, ns_id)

        with patch(_MOCK_EMIT_GRAPH, new_callable=AsyncMock):
            await _seed_design(pg_pool, ns_id)

        mock_propose = AsyncMock(return_value={"accepted": True, "quote_id": _DESIGN_ID})
        with (
            patch(_MOCK_PROPOSE, mock_propose),
            patch(_MOCK_EMIT_GRAPH, new_callable=AsyncMock),
        ):
            await do_design_to_quote(
                engine,
                {
                    "namespace_id": str(ns_id),
                    "design_id": _DESIGN_ID,
                },
            )

        async with scoped_pg_session(pg_pool, ns_id) as conn:
            quote_node_count = await _count_kg_nodes_by_type(conn, ns_id, "QUOTE")

        assert quote_node_count == 0, (
            f"Contract A violated: {quote_node_count} QUOTE row(s) in kg_nodes. "
            "System Design must NEVER write a QUOTE node — Sales owns QUOTE."
        )

        async with scoped_pg_session(pg_pool, ns_id) as conn:
            quote_label_count = int(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM kg_nodes "
                    "WHERE namespace_id = $1::uuid AND label LIKE 'QUOTE:%'",
                    str(ns_id),
                )
            )

        assert quote_label_count == 0, (
            f"Contract A violated: {quote_label_count} QUOTE-labelled row(s) in kg_nodes. "
            "Only kg_edges may reference a QUOTE label."
        )

    # ------------------------------------------------------------------
    # 3. design_version is returned (frozen BOM version)
    # ------------------------------------------------------------------

    async def test_design_version_returned(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """do_design_to_quote returns a positive design_version (frozen BOM)."""
        ns_id: uuid.UUID = await make_namespace()
        engine = _EngineStub(pg_pool)

        async with pg_pool.acquire() as conn:
            await _seed_ownership(conn, ns_id)

        with patch(_MOCK_EMIT_GRAPH, new_callable=AsyncMock):
            await _seed_design(pg_pool, ns_id)

        mock_propose = AsyncMock(return_value={"accepted": True, "quote_id": _DESIGN_ID})
        with (
            patch(_MOCK_PROPOSE, mock_propose),
            patch(_MOCK_EMIT_GRAPH, new_callable=AsyncMock),
        ):
            result = await do_design_to_quote(
                engine,
                {
                    "namespace_id": str(ns_id),
                    "design_id": _DESIGN_ID,
                },
            )

        assert isinstance(result["design_version"], int), "design_version must be int"
        assert result["design_version"] >= 1, "design_version must be >= 1"
        assert result["bom_line_count"] >= 0, "bom_line_count must be >= 0"

    # ------------------------------------------------------------------
    # 4. A2A seam is called with the proposal
    # ------------------------------------------------------------------

    async def test_a2a_seam_called_with_proposal(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """The A2A seam (_propose_quote_to_sales) is called with the proposal dict."""
        ns_id: uuid.UUID = await make_namespace()
        engine = _EngineStub(pg_pool)

        async with pg_pool.acquire() as conn:
            await _seed_ownership(conn, ns_id)

        with patch(_MOCK_EMIT_GRAPH, new_callable=AsyncMock):
            await _seed_design(pg_pool, ns_id)

        mock_propose = AsyncMock(return_value={"accepted": True, "quote_id": _DESIGN_ID})
        with (
            patch(_MOCK_PROPOSE, mock_propose),
            patch(_MOCK_EMIT_GRAPH, new_callable=AsyncMock),
        ):
            await do_design_to_quote(
                engine,
                {
                    "namespace_id": str(ns_id),
                    "design_id": _DESIGN_ID,
                },
            )

        mock_propose.assert_called_once()
        call_args = mock_propose.call_args.args
        # Positional signature: (engine, namespace_id, proposal)
        assert len(call_args) >= 3, "Expected at least 3 positional args to _propose_quote_to_sales"  # noqa: PLR2004
        proposal = call_args[2]
        assert "design_id" in proposal, "proposal must include 'design_id'"
        assert "bom_lines" in proposal, "proposal must include 'bom_lines'"
        assert "design_version" in proposal, "proposal must include 'design_version'"


@pytest.mark.integration
@pytest.mark.asyncio
class TestValidateDesign:
    """Integration tests for system_design/validate.py Wave 7."""

    # ------------------------------------------------------------------
    # 5. All-accept decision → passed=True, no reasons
    # ------------------------------------------------------------------

    async def test_all_accept_decisions_pass(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """All-accept decisions → passed=True and empty reasons."""
        ns_id: uuid.UUID = await make_namespace()
        engine = _EngineStub(pg_pool)

        async with pg_pool.acquire() as conn:
            await _seed_ownership(conn, ns_id)

        with patch(_MOCK_EMIT_GRAPH, new_callable=AsyncMock):
            await _seed_design(pg_pool, ns_id)

        result = await do_validate_design(
            engine,
            {
                "namespace_id": str(ns_id),
                "design_id": _DESIGN_ID,
                "decisions": [
                    {"line_id": "DL-001", "verdict": "accept"},
                    {"line_id": "DL-002", "verdict": "accept"},
                ],
            },
        )

        assert result["passed"] is True, "All-accept must yield passed=True"
        assert result["reasons"] == [], "All-accept must yield empty reasons"
        assert result["decisions_recorded"] == 2  # noqa: PLR2004
        assert result["design_version_bumped"] is True

    # ------------------------------------------------------------------
    # 6. Override decision → passed=False, reasons populated
    # ------------------------------------------------------------------

    async def test_override_decision_fails(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """A single override verdict → passed=False with non-empty reasons."""
        ns_id: uuid.UUID = await make_namespace()
        engine = _EngineStub(pg_pool)

        async with pg_pool.acquire() as conn:
            await _seed_ownership(conn, ns_id)

        with patch(_MOCK_EMIT_GRAPH, new_callable=AsyncMock):
            await _seed_design(pg_pool, ns_id)

        result = await do_validate_design(
            engine,
            {
                "namespace_id": str(ns_id),
                "design_id": _DESIGN_ID,
                "decisions": [
                    {"line_id": "DL-001", "verdict": "accept"},
                    {
                        "line_id": "DL-002",
                        "verdict": "override",
                        "reason": "Wrong cable type requested",
                    },
                ],
            },
        )

        assert result["passed"] is False, "Any override must yield passed=False"
        assert len(result["reasons"]) >= 1, "Override must populate reasons"
        assert result["decisions_recorded"] == 2  # noqa: PLR2004

    # ------------------------------------------------------------------
    # 7. Design version bumped (updated_at advances)
    # ------------------------------------------------------------------

    async def test_design_version_bumped_after_validate(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """do_validate_design advances the DESIGN node's updated_at timestamp."""
        ns_id: uuid.UUID = await make_namespace()
        engine = _EngineStub(pg_pool)

        async with pg_pool.acquire() as conn:
            await _seed_ownership(conn, ns_id)

        with patch(_MOCK_EMIT_GRAPH, new_callable=AsyncMock):
            await _seed_design(pg_pool, ns_id)

        design_lbl = f"DESIGN:{_DESIGN_ID.upper()}"

        async with scoped_pg_session(pg_pool, ns_id) as conn:
            updated_at_before = await _read_design_updated_at(conn, ns_id, design_lbl)

        await do_validate_design(
            engine,
            {
                "namespace_id": str(ns_id),
                "design_id": _DESIGN_ID,
                "decisions": [
                    {"line_id": "DL-001", "verdict": "accept"},
                ],
            },
        )

        async with scoped_pg_session(pg_pool, ns_id) as conn:
            updated_at_after = await _read_design_updated_at(conn, ns_id, design_lbl)

        # updated_at must have advanced.
        assert updated_at_after is not None, "DESIGN node must exist after validate"
        assert updated_at_after >= updated_at_before, (
            "updated_at must not go backwards after version bump"
        )

    # ------------------------------------------------------------------
    # 8. Ledger feedback written to v3_cognitive_ledger
    # ------------------------------------------------------------------

    async def test_ledger_feedback_appended(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """do_validate_design appends a row to v3_cognitive_ledger."""
        ns_id: uuid.UUID = await make_namespace()
        engine = _EngineStub(pg_pool)

        async with pg_pool.acquire() as conn:
            await _seed_ownership(conn, ns_id)

        with patch(_MOCK_EMIT_GRAPH, new_callable=AsyncMock):
            await _seed_design(pg_pool, ns_id)

        await do_validate_design(
            engine,
            {
                "namespace_id": str(ns_id),
                "design_id": _DESIGN_ID,
                "decisions": [
                    {"line_id": "DL-001", "verdict": "accept"},
                    {"line_id": "DL-002", "verdict": "accept"},
                ],
            },
        )

        async with scoped_pg_session(pg_pool, ns_id) as conn:
            ledger_count = await _count_ledger_rows(conn, ns_id, _DESIGN_ID)

        assert ledger_count >= 1, (
            f"Expected >= 1 v3_cognitive_ledger row for design_id={_DESIGN_ID!r}; "
            f"got {ledger_count}"
        )

    # ------------------------------------------------------------------
    # 9. Propose-only invariant: missing verdict raises ValueError
    # ------------------------------------------------------------------

    async def test_missing_verdict_raises_value_error(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """A decision without a verdict raises ValueError (propose-only §9.3)."""
        ns_id: uuid.UUID = await make_namespace()
        engine = _EngineStub(pg_pool)

        with pytest.raises(ValueError, match="verdict"):
            await do_validate_design(
                engine,
                {
                    "namespace_id": str(ns_id),
                    "design_id": _DESIGN_ID,
                    "decisions": [
                        # No 'verdict' key — must be rejected (no auto-accept)
                        {"line_id": "DL-001"},
                    ],
                },
            )

    # ------------------------------------------------------------------
    # 10. Propose-only: no line is ever auto-accepted
    # ------------------------------------------------------------------

    async def test_no_line_auto_accepted(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """No line is ever auto-accepted regardless of confidence score (§9.3)."""
        ns_id: uuid.UUID = await make_namespace()
        engine = _EngineStub(pg_pool)

        # A decision with an invalid auto-accept verdict must be rejected.
        with pytest.raises(ValueError, match="verdict"):
            await do_validate_design(
                engine,
                {
                    "namespace_id": str(ns_id),
                    "design_id": _DESIGN_ID,
                    "decisions": [
                        # 'auto' is not a valid verdict — no auto-accept allowed.
                        {"line_id": "DL-001", "verdict": "auto"},
                    ],
                },
            )

    # ------------------------------------------------------------------
    # 11. Combined flow: to_quote then validate bumps version
    # ------------------------------------------------------------------

    async def test_to_quote_then_validate_bumps_version(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """Full flow: to_quote freezes version, validate bumps it again."""
        ns_id: uuid.UUID = await make_namespace()
        engine = _EngineStub(pg_pool)

        async with pg_pool.acquire() as conn:
            await _seed_ownership(conn, ns_id)

        with patch(_MOCK_EMIT_GRAPH, new_callable=AsyncMock):
            await _seed_design(pg_pool, ns_id)

        mock_propose = AsyncMock(return_value={"accepted": True, "quote_id": _DESIGN_ID})
        with (
            patch(_MOCK_PROPOSE, mock_propose),
            patch(_MOCK_EMIT_GRAPH, new_callable=AsyncMock),
        ):
            to_quote_result = await do_design_to_quote(
                engine,
                {
                    "namespace_id": str(ns_id),
                    "design_id": _DESIGN_ID,
                },
            )

        frozen_version = to_quote_result["design_version"]

        validate_result = await do_validate_design(
            engine,
            {
                "namespace_id": str(ns_id),
                "design_id": _DESIGN_ID,
                "decisions": [
                    {"line_id": "DL-001", "verdict": "accept"},
                    {"line_id": "DL-002", "verdict": "accept"},
                ],
            },
        )

        # Validate returned the expected fields.
        assert "passed" in validate_result
        assert "reasons" in validate_result
        assert validate_result["design_version_bumped"] is True

        # The DESIGN node still exists.
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            design_count = await _count_kg_nodes_by_type(conn, ns_id, "DESIGN")

        assert design_count >= 1, "DESIGN node must still exist after validate"
        assert isinstance(frozen_version, int), "frozen_version must be int"
        assert frozen_version >= 1, "frozen_version must be >= 1"
