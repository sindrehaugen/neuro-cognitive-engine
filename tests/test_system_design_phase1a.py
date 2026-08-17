"""
tests/test_system_design_phase1a.py
=====================================
Phase-1a end-to-end integration test for Module 6 System Design.

Proves the value core ships with ZERO external systems (Correction #6):
  - No NetBox in the path
  - No SharePoint in the path
  - No Lucid in the path

Two paths are exercised:

Design-first path (author → propose → sow → to_quote):
  ``do_author_functional_location``
  → ``do_propose_design``
  → ``do_generate_sow``
  → ``do_design_to_quote``

Quote-first path:
  ``do_design_from_quote``

Mocked A2A seams (Sales not built):
  - ``nce.vertical_modules.system_design.from_quote._read_quote_lines``
  - ``nce.vertical_modules.system_design.to_quote._propose_quote_to_sales``

All domain ``do_*`` functions run against the real Postgres (no stubbing).

Assertions:
  - Flow completes without error.
  - ``do_propose_design`` lines all have ``validated: False`` (propose-only).
  - ``do_design_to_quote`` returns a frozen ``design_version`` > 0.
  - No ``QUOTE`` kg_nodes row is written (Contract A §9.1).
  - ``do_design_from_quote`` realises quote lines without writing QUOTE kg_nodes.

Runs as ``@pytest.mark.integration`` — requires a live Postgres with schema.sql
and migrations applied.  Skips automatically when no DSN is configured.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from nce.auth import set_namespace_context
from nce.db_utils import scoped_pg_session
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.system_design.from_quote import do_design_from_quote
from nce.vertical_modules.system_design.graph import do_author_functional_location
from nce.vertical_modules.system_design.propose import do_propose_design
from nce.vertical_modules.system_design.sow import do_generate_sow
from nce.vertical_modules.system_design.to_quote import do_design_to_quote

# ---------------------------------------------------------------------------
# A2A seam patch targets
# ---------------------------------------------------------------------------

_MOCK_READ_QUOTE_LINES = "nce.vertical_modules.system_design.from_quote._read_quote_lines"
_MOCK_PROPOSE_TO_SALES = "nce.vertical_modules.system_design.to_quote._propose_quote_to_sales"

# emit_graph_write targets — patched so event_log writes don't fail in offline CI.
_MOCK_EMIT_GRAPH = "nce.vertical_modules.system_design.graph.emit_graph_write"
_MOCK_EMIT_FQ = "nce.vertical_modules.system_design.from_quote.emit_graph_write"

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_NS_SLUG = "p1a-inttest"
_DESIGN_ID = "DESIGN-PHASE1A-001"

_BUILDINGS = [
    {
        "name": "MainBuilding",
        "floors": [
            {
                "name": "Floor1",
                "rooms": [
                    {
                        "name": "ConfRoom101",
                        "positions": ["RACK-A"],
                    }
                ],
            }
        ],
    }
]

_DESIGN_LINES = [
    {
        "line_ref": "DL-001",
        "manufacturer": "Biamp",
        "mfr_part_no": "TesiraFORTE-CI",
        "confidence": 0.95,
        "source_id": None,
    }
]

_QUOTE_LINES: list[dict[str, Any]] = [
    {
        "line_ref": "QL-001",
        "fl_path": ["SiteAlpha", "MainBuilding", "Floor1", "ConfRoom101"],
        "manufacturer": "Shure",
        "mfr_part_no": "MXA910",
        "qty": 1,
        "confidence": 0.90,
    },
]


# ---------------------------------------------------------------------------
# Stub engine (same pattern as test_system_design_from_quote.py)
# ---------------------------------------------------------------------------


class _EngineStub:
    """Minimal engine stub exposing ``pg_pool``."""

    def __init__(self, pg_pool: Any) -> None:
        self.pg_pool = pg_pool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_ownership(conn: Any, ns_id: uuid.UUID) -> None:
    async with conn.transaction():
        await set_namespace_context(conn, ns_id)
        await seed_node_ownership_registry(conn, ns_id)


async def _count_kg_nodes_by_type(conn: Any, ns_id: uuid.UUID, entity_type: str) -> int:
    return int(
        await conn.fetchval(
            "SELECT COUNT(*) FROM kg_nodes WHERE namespace_id = $1::uuid AND entity_type = $2",
            str(ns_id),
            entity_type,
        )
    )


# ---------------------------------------------------------------------------
# Integration test suite
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestPhase1aDesignFirst:
    """Design-first path: author → propose → sow → to_quote.

    No NetBox / SharePoint / Lucid in the path — value core ships
    integration-free (Correction #6).
    """

    async def test_design_first_flow_completes(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """Full design-first Phase-1a flow runs end-to-end without error."""
        ns_id: uuid.UUID = await make_namespace()
        engine = _EngineStub(pg_pool)

        # Seed ownership registry so RLS passes.
        async with pg_pool.acquire() as conn:
            await _seed_ownership(conn, ns_id)

        # Step 1: author functional location + design.
        with patch(_MOCK_EMIT_GRAPH, new_callable=AsyncMock):
            async with scoped_pg_session(pg_pool, ns_id) as conn:
                author_result = await do_author_functional_location(
                    conn,
                    ns_id,
                    namespace_slug=_NS_SLUG,
                    design_id=_DESIGN_ID,
                    site_name="SiteAlpha",
                    buildings=_BUILDINGS,
                    design_lines=_DESIGN_LINES,
                )

        assert "authored" in author_result
        assert author_result["authored"]["nodes"] >= 1
        assert author_result["authored"]["edges"] >= 1

        # Step 2: propose design (propose-only; no lines validated).
        propose_result = await do_propose_design(
            engine,
            {
                "namespace_id": str(ns_id),
                "room_brief": "AV conference room with ceiling mics and DSP",
            },
        )

        assert "proposed_lines" in propose_result
        for line in propose_result["proposed_lines"]:
            assert line["validated"] is False, (
                f"Propose-only invariant violated: line has validated={line['validated']!r}"
            )

        # Step 3: generate SoW — version number must be positive.
        sow_result = await do_generate_sow(
            engine,
            {
                "namespace_id": str(ns_id),
                "design_id": _DESIGN_ID,
            },
        )

        assert "version_number" in sow_result
        assert sow_result["version_number"] > 0, (
            f"SoW version must be > 0; got {sow_result['version_number']!r}"
        )
        assert "sow" in sow_result

        # Step 4: to_quote — mock A2A seam (Sales not built).
        mock_sales = AsyncMock(return_value={"accepted": True})
        with patch(_MOCK_PROPOSE_TO_SALES, mock_sales):
            to_quote_result = await do_design_to_quote(
                engine,
                {
                    "namespace_id": str(ns_id),
                    "design_id": _DESIGN_ID,
                },
            )

        assert to_quote_result["design_id"] == _DESIGN_ID
        frozen_version = to_quote_result["design_version"]
        assert frozen_version > 0, f"Frozen design version must be > 0; got {frozen_version!r}"
        assert to_quote_result["quote_label"].startswith("QUOTE:")
        assert to_quote_result["design_label"].startswith("DESIGN:")

    async def test_design_version_freezes_correctly(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """``_derive_version_number`` produces a consistent positive integer.

        Two calls to ``do_design_to_quote`` on the same unchanged DESIGN node
        must return the same ``design_version`` (deterministic hash of label +
        updated_at).
        """
        ns_id: uuid.UUID = await make_namespace()
        engine = _EngineStub(pg_pool)

        async with pg_pool.acquire() as conn:
            await _seed_ownership(conn, ns_id)

        with patch(_MOCK_EMIT_GRAPH, new_callable=AsyncMock):
            async with scoped_pg_session(pg_pool, ns_id) as conn:
                await do_author_functional_location(
                    conn,
                    ns_id,
                    namespace_slug=_NS_SLUG,
                    design_id=_DESIGN_ID,
                    site_name="SiteAlpha",
                    buildings=_BUILDINGS,
                    design_lines=_DESIGN_LINES,
                )

        mock_sales = AsyncMock(return_value={"accepted": True})
        with patch(_MOCK_PROPOSE_TO_SALES, mock_sales):
            result_a = await do_design_to_quote(
                engine,
                {
                    "namespace_id": str(ns_id),
                    "design_id": _DESIGN_ID,
                },
            )

        with patch(_MOCK_PROPOSE_TO_SALES, mock_sales):
            result_b = await do_design_to_quote(
                engine,
                {
                    "namespace_id": str(ns_id),
                    "design_id": _DESIGN_ID,
                },
            )

        assert result_a["design_version"] == result_b["design_version"], (
            "Version number must be deterministic for the same unchanged DESIGN node; "
            f"first call={result_a['design_version']}, second call={result_b['design_version']}"
        )

    async def test_no_quote_kg_node_written_design_first(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """Contract A §9.1 — QUOTE entity_type NEVER written to kg_nodes (design-first)."""
        ns_id: uuid.UUID = await make_namespace()
        engine = _EngineStub(pg_pool)

        async with pg_pool.acquire() as conn:
            await _seed_ownership(conn, ns_id)

        with patch(_MOCK_EMIT_GRAPH, new_callable=AsyncMock):
            async with scoped_pg_session(pg_pool, ns_id) as conn:
                await do_author_functional_location(
                    conn,
                    ns_id,
                    namespace_slug=_NS_SLUG,
                    design_id=_DESIGN_ID,
                    site_name="SiteAlpha",
                    buildings=_BUILDINGS,
                    design_lines=_DESIGN_LINES,
                )

        mock_sales = AsyncMock(return_value={"accepted": True})
        with patch(_MOCK_PROPOSE_TO_SALES, mock_sales):
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
            f"Contract A §9.1 violated: {quote_node_count} QUOTE row(s) in kg_nodes. "
            "System Design must NEVER write a QUOTE node — Sales owns QUOTE."
        )


@pytest.mark.integration
@pytest.mark.asyncio
class TestPhase1aQuoteFirst:
    """Quote-first path: do_design_from_quote.

    No NetBox / SharePoint / Lucid in the path.  Proposals stay propose-only.
    QUOTE node must never appear in kg_nodes.
    """

    async def test_quote_first_flow_completes(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """do_design_from_quote realises quote lines without external systems."""
        ns_id: uuid.UUID = await make_namespace()
        engine = _EngineStub(pg_pool)
        quote_id = "QUOTE-PHASE1A-001"
        design_id = "DESIGN-FROM-Q-001"

        async with pg_pool.acquire() as conn:
            await _seed_ownership(conn, ns_id)

        mock_quote = AsyncMock(return_value=_QUOTE_LINES)
        with (
            patch(_MOCK_READ_QUOTE_LINES, mock_quote),
            patch(_MOCK_EMIT_GRAPH, new_callable=AsyncMock),
            patch(_MOCK_EMIT_FQ, new_callable=AsyncMock),
        ):
            result = await do_design_from_quote(
                engine,
                {
                    "namespace_id": str(ns_id),
                    "quote_id": quote_id,
                    "design_id": design_id,
                    "namespace_slug": _NS_SLUG,
                },
            )

        assert result["quote_lines_realized"] == len(_QUOTE_LINES), (
            f"Expected {len(_QUOTE_LINES)} lines realised; got {result['quote_lines_realized']}"
        )
        assert result["quote_label"].startswith("QUOTE:")
        assert result["design_label"].startswith("DESIGN:")

    async def test_no_quote_kg_node_written_quote_first(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """Contract A §9.1 — QUOTE entity_type NEVER written to kg_nodes (quote-first)."""
        ns_id: uuid.UUID = await make_namespace()
        engine = _EngineStub(pg_pool)
        quote_id = "QUOTE-PHASE1A-002"
        design_id = "DESIGN-FROM-Q-002"

        async with pg_pool.acquire() as conn:
            await _seed_ownership(conn, ns_id)

        mock_quote = AsyncMock(return_value=_QUOTE_LINES)
        with (
            patch(_MOCK_READ_QUOTE_LINES, mock_quote),
            patch(_MOCK_EMIT_GRAPH, new_callable=AsyncMock),
            patch(_MOCK_EMIT_FQ, new_callable=AsyncMock),
        ):
            await do_design_from_quote(
                engine,
                {
                    "namespace_id": str(ns_id),
                    "quote_id": quote_id,
                    "design_id": design_id,
                    "namespace_slug": _NS_SLUG,
                },
            )

        async with scoped_pg_session(pg_pool, ns_id) as conn:
            quote_node_count = await _count_kg_nodes_by_type(conn, ns_id, "QUOTE")

        assert quote_node_count == 0, (
            f"Contract A §9.1 violated: {quote_node_count} QUOTE row(s) in kg_nodes. "
            "System Design must NEVER write a QUOTE node — Sales owns QUOTE."
        )
