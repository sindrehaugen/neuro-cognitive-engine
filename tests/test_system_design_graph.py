"""Integration tests for System Design graph upserts (Wave 2 — functional-location-nodes).

Validates:
  a. Full SITE>BUILDING>FLOOR>ROOM>POSITION tree authors all FUNCTIONAL_LOCATION nodes.
  b. DESIGN and DESIGN_LINE nodes upsert idempotently (re-run is a no-op).
  c. ``confidence`` is on edges only — kg_nodes has no confidence column (structural).
  d. Design-intent is encoded in entity_type='FUNCTIONAL_LOCATION' — no phantom column.
  e. Tree edges (parent_of) and DESIGN-contains edges land with correct predicates.
  f. DESIGN_LINE -[references]-> PRODUCT cross-engine edge lands without a PRODUCT kg_node.
  g. RLS isolates data across namespaces (ns_a rows invisible from ns_b).
  h. Idempotent: a second do_author_functional_location call is a no-op (no duplicates).
  i. system_design_source_id and change_origin='sync' are persisted on nodes and edges.
  j. OwnershipError is raised for an unseeded namespace.

Fixtures used:
  ``pg_app_conn``        — asyncpg connection as nce_app (RLS enforced).
  ``make_namespace``     — factory that inserts a new namespace row.
  ``set_namespace_context`` (nce.auth) — sets the GUC required by RLS.
  ``seed_node_ownership_registry`` — seeds all three system_design node-type rows.

Runs as @pytest.mark.integration — requires a live Postgres with schema.sql and
migrations 036 + 037 applied (run scratch/_apply_probe_b032.py first).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.entity_resolution.ownership import OwnershipError
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.system_design.graph import (
    _design_label,
    _design_line_label,
    _fl_label,
    _product_label,
    do_author_functional_location,
)

# ---------------------------------------------------------------------------
# Constants shared across tests
# ---------------------------------------------------------------------------

_MOCK_EMIT = "nce.vertical_modules.system_design.graph.emit_graph_write"

_SITE = "TestSite"
_BUILDING = "MainBuilding"
_FLOOR = "Floor1"
_ROOM = "Room101"
_POSITION = "RackA"
_DESIGN_ID = "DESIGN-TEST-001"
_LINE_REF_A = "LINE-A"
_LINE_REF_B = "LINE-B"
_MFR = "Biamp"
_PART_A = "TesiraFORTE-CI"
_PART_B = "TesiraLUX-AIB"

_BUILDINGS = [
    {
        "name": _BUILDING,
        "floors": [
            {
                "name": _FLOOR,
                "rooms": [
                    {
                        "name": _ROOM,
                        "positions": [_POSITION],
                    }
                ],
            }
        ],
    }
]

_DESIGN_LINES = [
    {
        "line_ref": _LINE_REF_A,
        "manufacturer": _MFR,
        "mfr_part_no": _PART_A,
        "confidence": 0.9,
        "source_id": "src-line-a",
    },
    {
        "line_ref": _LINE_REF_B,
        "manufacturer": _MFR,
        "mfr_part_no": _PART_B,
        "confidence": 0.75,
        "source_id": "src-line-b",
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed(conn: asyncpg.Connection, ns: object) -> None:  # type: ignore[type-arg]
    """Seed ownership registry + set namespace GUC inside one transaction."""
    async with conn.transaction():
        await set_namespace_context(conn, ns)
        await seed_node_ownership_registry(conn, ns)


async def _author(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns: object,
    *,
    ns_slug: str = "testns",
    source_id: str | None = "src-design-001",
) -> dict:  # type: ignore[type-arg]
    """Run do_author_functional_location inside a mocked-emit transaction."""
    with patch(_MOCK_EMIT, new_callable=AsyncMock):
        async with conn.transaction():
            await set_namespace_context(conn, ns)
            return await do_author_functional_location(
                conn,
                ns,
                namespace_slug=ns_slug,
                design_id=_DESIGN_ID,
                site_name=_SITE,
                buildings=_BUILDINGS,
                design_lines=_DESIGN_LINES,
                source_id=source_id,
            )


# ---------------------------------------------------------------------------
# Test suite
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestSystemDesignGraphUpserts:
    """Integration tests for system_design/graph.py Wave 2."""

    # ------------------------------------------------------------------
    # a. Full tree — all FUNCTIONAL_LOCATION nodes authored
    # ------------------------------------------------------------------

    async def test_full_tree_nodes_authored(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """All FUNCTIONAL_LOCATION nodes land in kg_nodes with the correct entity_type."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)
        await _author(pg_app_conn, ns)

        ns_slug = "testns"
        expected_labels = [
            _fl_label(ns_slug, _SITE),
            _fl_label(ns_slug, _SITE, _BUILDING),
            _fl_label(ns_slug, _SITE, _BUILDING, _FLOOR),
            _fl_label(ns_slug, _SITE, _BUILDING, _FLOOR, _ROOM),
            _fl_label(ns_slug, _SITE, _BUILDING, _FLOOR, _ROOM, _POSITION),
        ]

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            for lbl in expected_labels:
                row = await pg_app_conn.fetchrow(
                    """
                    SELECT entity_type FROM kg_nodes
                    WHERE label = $1 AND namespace_id = $2
                    """,
                    lbl,
                    ns,
                )
                assert row is not None, f"FUNCTIONAL_LOCATION node missing: {lbl}"
                assert row["entity_type"] == "FUNCTIONAL_LOCATION", (
                    f"entity_type wrong for {lbl}: got {row['entity_type']!r}"
                )

    # ------------------------------------------------------------------
    # b. DESIGN and DESIGN_LINE nodes upsert idempotently
    # ------------------------------------------------------------------

    async def test_design_nodes_idempotent(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """Two do_author_functional_location calls produce exactly one row per node."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)

        for _ in range(2):
            await _author(pg_app_conn, ns)

        design_lbl = _design_label(_DESIGN_ID)
        dl_a_lbl = _design_line_label(_DESIGN_ID, _LINE_REF_A)
        dl_b_lbl = _design_line_label(_DESIGN_ID, _LINE_REF_B)

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            for lbl, expected_type in [
                (design_lbl, "DESIGN"),
                (dl_a_lbl, "DESIGN_LINE"),
                (dl_b_lbl, "DESIGN_LINE"),
            ]:
                count = await pg_app_conn.fetchval(
                    "SELECT COUNT(*) FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                    lbl,
                    ns,
                )
                assert count == 1, f"Expected 1 row for {lbl}, got {count}"
                row = await pg_app_conn.fetchrow(
                    "SELECT entity_type FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                    lbl,
                    ns,
                )
                assert row is not None
                assert row["entity_type"] == expected_type, (
                    f"entity_type wrong for {lbl}: {row['entity_type']!r}"
                )

    # ------------------------------------------------------------------
    # c. kg_nodes has no confidence column (structural assertion — rule 7)
    # ------------------------------------------------------------------

    async def test_kg_nodes_has_no_confidence_column(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        """Structural: kg_nodes must not have a confidence column."""
        row = await pg_app_conn.fetchrow(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name  = 'kg_nodes'
              AND column_name = 'confidence'
            """
        )
        assert row is None, (
            "kg_nodes must NOT have a confidence column — confidence belongs to kg_edges only"
        )

    # ------------------------------------------------------------------
    # d. Design-intent encoding: entity_type='FUNCTIONAL_LOCATION' is the marker
    # ------------------------------------------------------------------

    async def test_design_intent_encoded_as_entity_type(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """entity_type='FUNCTIONAL_LOCATION' is the design-intent marker — no state column."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)
        await _author(pg_app_conn, ns)

        site_lbl = _fl_label("testns", _SITE)

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            row = await pg_app_conn.fetchrow(
                """
                SELECT entity_type FROM kg_nodes
                WHERE label = $1 AND namespace_id = $2
                """,
                site_lbl,
                ns,
            )
        assert row is not None
        assert row["entity_type"] == "FUNCTIONAL_LOCATION", (
            "SITE node must have entity_type='FUNCTIONAL_LOCATION'"
        )

        # Confirm no 'state' or 'metadata' column exists on kg_nodes.
        for col in ("state", "metadata"):
            col_row = await pg_app_conn.fetchrow(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='kg_nodes' AND column_name=$1",
                col,
            )
            assert col_row is None, (
                f"kg_nodes must NOT have a '{col}' column — no phantom state/metadata column"
            )

    # ------------------------------------------------------------------
    # e. Tree edges land with correct predicates
    # ------------------------------------------------------------------

    async def test_tree_edges_correct_predicates(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """parent_of and contains edges are written between the correct nodes."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)
        await _author(pg_app_conn, ns)

        ns_slug = "testns"
        site_lbl = _fl_label(ns_slug, _SITE)
        bld_lbl = _fl_label(ns_slug, _SITE, _BUILDING)
        flr_lbl = _fl_label(ns_slug, _SITE, _BUILDING, _FLOOR)
        room_lbl = _fl_label(ns_slug, _SITE, _BUILDING, _FLOOR, _ROOM)
        pos_lbl = _fl_label(ns_slug, _SITE, _BUILDING, _FLOOR, _ROOM, _POSITION)
        design_lbl = _design_label(_DESIGN_ID)

        expected_edges = [
            (design_lbl, "contains", site_lbl),
            (site_lbl, "parent_of", bld_lbl),
            (bld_lbl, "parent_of", flr_lbl),
            (flr_lbl, "parent_of", room_lbl),
            (room_lbl, "parent_of", pos_lbl),
        ]

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            for subj, pred, obj in expected_edges:
                row = await pg_app_conn.fetchrow(
                    """
                    SELECT confidence FROM kg_edges
                    WHERE subject_label = $1
                      AND predicate     = $2
                      AND object_label  = $3
                      AND namespace_id  = $4
                    """,
                    subj,
                    pred,
                    obj,
                    ns,
                )
                assert row is not None, f"Edge missing: {subj} -[{pred}]-> {obj}"
                assert 0.0 <= row["confidence"] <= 1.0, (
                    f"Edge confidence out of range for {subj} -[{pred}]-> {obj}"
                )

    # ------------------------------------------------------------------
    # f. DESIGN_LINE -[references]-> PRODUCT cross-engine edge (no PRODUCT node needed)
    # ------------------------------------------------------------------

    async def test_design_line_references_product_cross_engine(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """DESIGN_LINE-references->PRODUCT edge lands without a PRODUCT kg_node row."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)
        await _author(pg_app_conn, ns)

        dl_lbl = _design_line_label(_DESIGN_ID, _LINE_REF_A)
        product_lbl = _product_label(_MFR, _PART_A)

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)

            # Edge must exist.
            edge = await pg_app_conn.fetchrow(
                """
                SELECT confidence FROM kg_edges
                WHERE subject_label = $1
                  AND predicate     = 'references'
                  AND object_label  = $2
                  AND namespace_id  = $3
                """,
                dl_lbl,
                product_lbl,
                ns,
            )
            assert edge is not None, "DESIGN_LINE -[references]-> PRODUCT edge missing"
            assert abs(edge["confidence"] - 0.9) < 1e-6, (
                f"Expected confidence=0.9, got {edge['confidence']}"
            )

            # PRODUCT node must NOT exist (not owned by system_design).
            product_node = await pg_app_conn.fetchrow(
                "SELECT label FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                product_lbl,
                ns,
            )
            assert product_node is None, (
                "PRODUCT kg_node must not be created by system_design engine"
            )

    # ------------------------------------------------------------------
    # g. RLS isolates data across namespaces
    # ------------------------------------------------------------------

    async def test_rls_isolation_across_namespaces(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """Rows written to ns_a are invisible when the GUC is set to ns_b."""
        ns_a = await make_namespace()  # type: ignore[operator]
        ns_b = await make_namespace()  # type: ignore[operator]

        await _seed(pg_app_conn, ns_a)
        await _seed(pg_app_conn, ns_b)

        await _author(pg_app_conn, ns_a)

        site_lbl = _fl_label("testns", _SITE)

        # Visible under ns_a.
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns_a)
            row_a = await pg_app_conn.fetchrow(
                "SELECT label FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                site_lbl,
                ns_a,
            )
        assert row_a is not None, "SITE node not visible under its own namespace ns_a"

        # Invisible under ns_b (RLS).
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns_b)
            row_b = await pg_app_conn.fetchrow(
                "SELECT label FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                site_lbl,
                ns_a,  # querying ns_a row while GUC is ns_b
            )
        assert row_b is None, "RLS isolation failed: ns_a node visible when GUC is set to ns_b"

    # ------------------------------------------------------------------
    # h. Idempotency: second call produces no duplicate rows
    # ------------------------------------------------------------------

    async def test_idempotent_second_call_no_duplicates(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """Two author calls produce exactly one row per node and edge."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)

        for _ in range(2):
            await _author(pg_app_conn, ns)

        site_lbl = _fl_label("testns", _SITE)

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)

            node_count = await pg_app_conn.fetchval(
                "SELECT COUNT(*) FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                site_lbl,
                ns,
            )
            assert node_count == 1, f"Expected 1 SITE node, got {node_count}"

            design_lbl = _design_label(_DESIGN_ID)
            edge_count = await pg_app_conn.fetchval(
                """
                SELECT COUNT(*) FROM kg_edges
                WHERE subject_label = $1
                  AND predicate     = 'contains'
                  AND object_label  = $2
                  AND namespace_id  = $3
                """,
                design_lbl,
                site_lbl,
                ns,
            )
            assert edge_count == 1, f"Expected 1 contains edge, got {edge_count}"

    # ------------------------------------------------------------------
    # i. system_design_source_id and change_origin='sync' persisted
    # ------------------------------------------------------------------

    async def test_node_source_id_and_change_origin(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """DESIGN node has system_design_source_id set and change_origin='sync'."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)
        await _author(pg_app_conn, ns, source_id="src-design-001")

        design_lbl = _design_label(_DESIGN_ID)

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            row = await pg_app_conn.fetchrow(
                """
                SELECT change_origin, system_design_source_id
                FROM kg_nodes
                WHERE label = $1 AND namespace_id = $2
                """,
                design_lbl,
                ns,
            )

        assert row is not None, "DESIGN node not found after author"
        assert row["change_origin"] == "sync", (
            f"Expected change_origin='sync', got {row['change_origin']!r}"
        )
        assert row["system_design_source_id"] == "src-design-001", (
            f"Expected system_design_source_id='src-design-001', "
            f"got {row['system_design_source_id']!r}"
        )

    async def test_edge_source_id_and_change_origin(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """contains edge has system_design_source_id set and change_origin='sync'."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)
        await _author(pg_app_conn, ns, source_id="src-design-001")

        design_lbl = _design_label(_DESIGN_ID)
        site_lbl = _fl_label("testns", _SITE)

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            row = await pg_app_conn.fetchrow(
                """
                SELECT change_origin, system_design_source_id
                FROM kg_edges
                WHERE subject_label = $1
                  AND predicate     = 'contains'
                  AND object_label  = $2
                  AND namespace_id  = $3
                """,
                design_lbl,
                site_lbl,
                ns,
            )

        assert row is not None, "contains edge not found after author"
        assert row["change_origin"] == "sync"
        assert row["system_design_source_id"] == "src-design-001"

    # ------------------------------------------------------------------
    # j. OwnershipError raised for unseeded namespace
    # ------------------------------------------------------------------

    async def test_raises_ownership_error_for_unseeded_namespace(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """do_author_functional_location raises OwnershipError on an unseeded namespace."""
        ns = await make_namespace()  # type: ignore[operator]
        # Intentionally skip seed_node_ownership_registry.

        with patch(_MOCK_EMIT, new_callable=AsyncMock):
            async with pg_app_conn.transaction():
                await set_namespace_context(pg_app_conn, ns)
                with pytest.raises(OwnershipError) as exc_info:
                    await do_author_functional_location(
                        pg_app_conn,
                        ns,
                        namespace_slug="unseeded",
                        design_id="DESIGN-NO-SEED",
                        site_name="NoSeedSite",
                        buildings=[],
                    )

        err = exc_info.value
        assert err.node_type == "DESIGN", f"Expected DESIGN, got {err.node_type!r}"
        assert err.owner_engine is None, "Deny-by-default: owner_engine must be None"
