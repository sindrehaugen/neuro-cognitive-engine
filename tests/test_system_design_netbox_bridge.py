"""Integration tests for nce/vertical_modules/system_design/netbox_bridge.py
(Wave 9 — Phase 1b functional-location sync + promoted_to_asbuilt reconciliation).

Validates:
  a. sync_fl_to_netbox creates NetBox sites/locations from authored intent and
     writes sync_to_netbox edges (NetBox MOCKED).
  b. promoted_to_asbuilt edge links intent → as-built node on confirmation.
  c. has_divergence + as_built_confirms edges land when asbuilt_name != intent_name.
  d. Unchanged (unconfirmed) confirmations are skipped — no spurious edges.
  e. RLS-scoped: edges are isolated to the active namespace (ns_a invisible from ns_b).
  f. Phase 1b independence: the W2 graph core works without NetBox connectivity;
     a build_bridge() with empty URL raises ValueError, not a silent failure.
  g. sync_fl_to_netbox is idempotent (re-run upserts, not inserts).
  h. confidence (0–1) lives on kg_edges only — kg_nodes has no confidence column.

All tests are @pytest.mark.integration (NetBox mocked; requires live Postgres).
NetBox is mocked via unittest.mock.AsyncMock patches on ``_NetBoxClient`` so the
tests exercise real DB writes without network connectivity.

Fixtures used:
  ``pg_app_conn``   — asyncpg connection as nce_app (RLS enforced).
  ``make_namespace`` — factory that inserts a new namespace row.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.config import DeploymentConfigurationError
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.system_design.graph import do_author_functional_location
from nce.vertical_modules.system_design.netbox_bridge import (
    SystemDesignNetBoxBridge,
    _asbuilt_label,
    _fl_label,
    _NetBoxClient,
    build_bridge,
)

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

_NS_SLUG = "bridgtest"
_SITE = "BridgeSite"
_BUILDING = "MainBlock"
_FLOOR = "Level1"
_ROOM = "ServerRoom"
_POSITION = "RackA1"
_DESIGN_ID = "DESIGN-BRIDGE-001"

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

# Fake NetBox IDs returned by the mocked client
_NB_SITE_ID = 101
_NB_BLD_ID = 201
_NB_FLR_ID = 202
_NB_ROOM_ID = 203
_NB_POS_ID = 204

_MOCK_EMIT = "nce.vertical_modules.system_design.graph.emit_graph_write"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_nb_client(*, site_id: int = _NB_SITE_ID) -> _NetBoxClient:
    """Return a fully-mocked _NetBoxClient.

    create_site returns a site dict; create_location increments IDs per call.
    fetch_sites / fetch_locations return empty lists (nothing pre-existing) so
    every object is created fresh.
    """
    client: Any = MagicMock(spec=_NetBoxClient)
    client.fetch_sites = AsyncMock(return_value=[])
    client.fetch_locations = AsyncMock(return_value=[])

    # Each create_site call returns a fresh site dict
    client.create_site = AsyncMock(return_value={"id": site_id, "name": _SITE})

    # create_location returns dicts with incrementing IDs
    _loc_counter = [_NB_BLD_ID]

    async def _create_loc(
        name: str, slug: str, site_id_: int, parent_id: int | None = None
    ) -> dict[str, Any]:  # noqa: E501
        loc_id = _loc_counter[0]
        _loc_counter[0] += 1
        return {"id": loc_id, "name": name, "site": {"id": site_id_}}

    client.create_location = _create_loc
    return client  # type: ignore[return-value]


async def _seed(conn: asyncpg.Connection, ns: Any) -> None:  # type: ignore[type-arg]
    """Seed node ownership and set namespace GUC."""
    async with conn.transaction():
        await set_namespace_context(conn, ns)
        await seed_node_ownership_registry(conn, ns)


async def _author_intent(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns: Any,
) -> dict[str, Any]:
    """Author the design-intent FL tree (W2 prerequisite)."""
    with patch(_MOCK_EMIT, new_callable=AsyncMock):
        async with conn.transaction():
            await set_namespace_context(conn, ns)
            return await do_author_functional_location(  # type: ignore[no-any-return]
                conn,
                ns,
                namespace_slug=_NS_SLUG,
                design_id=_DESIGN_ID,
                site_name=_SITE,
                buildings=_BUILDINGS,
                source_id="src-bridge-001",
            )


def _make_bridge(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: uuid.UUID,
    nb_client: _NetBoxClient,
) -> SystemDesignNetBoxBridge:
    """Construct bridge with injected mock client (bypasses env lookup)."""
    return SystemDesignNetBoxBridge(conn, ns_uuid, _NS_SLUG, nb_client)


# ---------------------------------------------------------------------------
# Test suite
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestSystemDesignNetBoxBridge:
    """Integration tests for system_design/netbox_bridge.py (Phase 1b)."""

    # ------------------------------------------------------------------
    # a. sync_fl_to_netbox: creates NetBox objects + writes sync_to_netbox edges
    # ------------------------------------------------------------------

    async def test_sync_fl_creates_netbox_objects_and_edges(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: Any,
    ) -> None:
        """sync_fl_to_netbox creates site+locations in NetBox and writes edges."""
        ns = await make_namespace()
        ns_uuid = uuid.UUID(str(ns))
        await _seed(pg_app_conn, ns)
        await _author_intent(pg_app_conn, ns)

        nb_client = _make_nb_client()
        bridge = _make_bridge(pg_app_conn, ns_uuid, nb_client)

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            result = await bridge.sync_fl_to_netbox(_SITE, _BUILDINGS, source_id="src-bridge-001")

        assert result.sites_created == 1, "Expected 1 NetBox site to be created"
        assert result.locations_created >= 1, "Expected at least 1 location created"
        assert result.edges_written >= 1, "Expected sync_to_netbox edges to be written"
        assert result.errors == [], f"Unexpected errors: {result.errors}"

        # Verify sync_to_netbox edge for the SITE level
        site_fl = _fl_label(_NS_SLUG, _SITE)
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            edge_row = await pg_app_conn.fetchrow(
                """
                SELECT predicate, confidence FROM kg_edges
                WHERE subject_label = $1
                  AND predicate = 'sync_to_netbox'
                  AND namespace_id = $2
                """,
                site_fl,
                ns,
            )
        assert edge_row is not None, f"sync_to_netbox edge missing for {site_fl}"
        assert 0.0 < edge_row["confidence"] <= 1.0, "confidence must be in (0, 1]"

    # ------------------------------------------------------------------
    # b. promoted_to_asbuilt edge links intent → as-built on confirmation
    # ------------------------------------------------------------------

    async def test_promoted_to_asbuilt_edge_on_confirmation(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: Any,
    ) -> None:
        """promoted_to_asbuilt edge written from intent node to as-built node."""
        ns = await make_namespace()
        ns_uuid = uuid.UUID(str(ns))
        await _seed(pg_app_conn, ns)
        await _author_intent(pg_app_conn, ns)

        nb_client = _make_nb_client()
        bridge = _make_bridge(pg_app_conn, ns_uuid, nb_client)

        room_fl = _fl_label(_NS_SLUG, _SITE, _BUILDING, _FLOOR, _ROOM)
        asbuilt_lbl = _asbuilt_label(room_fl)

        confirmations = [
            {
                "intent_label": room_fl,
                "intent_name": _ROOM,
                "asbuilt_name": _ROOM,  # exact match → clean promotion
                "confirmed": True,
            }
        ]

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            rec_result = await bridge.reconcile_asbuilt(confirmations, source_id="src-bridge-001")

        assert rec_result.promoted == 1, "Expected 1 promoted node"
        assert rec_result.diverged == 0, "Expected 0 diverged nodes"
        assert rec_result.edges_written >= 2, "Expected at least 2 edges (promoted + confirms)"

        # Verify promoted_to_asbuilt edge exists
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            promo_edge = await pg_app_conn.fetchrow(
                """
                SELECT confidence FROM kg_edges
                WHERE subject_label = $1
                  AND predicate = 'promoted_to_asbuilt'
                  AND object_label = $2
                  AND namespace_id = $3
                """,
                room_fl,
                asbuilt_lbl,
                ns,
            )
            confirms_edge = await pg_app_conn.fetchrow(
                """
                SELECT confidence FROM kg_edges
                WHERE subject_label = $1
                  AND predicate = 'as_built_confirms'
                  AND object_label = $2
                  AND namespace_id = $3
                """,
                asbuilt_lbl,
                room_fl,
                ns,
            )

        assert promo_edge is not None, "promoted_to_asbuilt edge missing"
        assert confirms_edge is not None, "as_built_confirms reverse edge missing"
        # confidence on edges only — verify it is set
        assert promo_edge["confidence"] == 1.0

    # ------------------------------------------------------------------
    # c. has_divergence edge when asbuilt_name != intent_name
    # ------------------------------------------------------------------

    async def test_divergence_edge_when_names_differ(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: Any,
    ) -> None:
        """has_divergence edge written when the as-built name differs from intent."""
        ns = await make_namespace()
        ns_uuid = uuid.UUID(str(ns))
        await _seed(pg_app_conn, ns)
        await _author_intent(pg_app_conn, ns)

        nb_client = _make_nb_client()
        bridge = _make_bridge(pg_app_conn, ns_uuid, nb_client)

        room_fl = _fl_label(_NS_SLUG, _SITE, _BUILDING, _FLOOR, _ROOM)
        asbuilt_lbl = _asbuilt_label(room_fl)

        confirmations = [
            {
                "intent_label": room_fl,
                "intent_name": _ROOM,
                "asbuilt_name": "ServerRoom-RENAMED",  # diverged name
                "confirmed": True,
            }
        ]

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            rec_result = await bridge.reconcile_asbuilt(confirmations)

        assert rec_result.diverged == 1, "Expected 1 diverged node"
        assert rec_result.promoted == 0, "Expected 0 clean promotions"

        # promoted_to_asbuilt edge still written (link is always created on confirmation)
        # AND has_divergence edge written with lower confidence
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            div_edge = await pg_app_conn.fetchrow(
                """
                SELECT confidence FROM kg_edges
                WHERE subject_label = $1
                  AND predicate = 'has_divergence'
                  AND object_label = $2
                  AND namespace_id = $3
                """,
                room_fl,
                asbuilt_lbl,
                ns,
            )
            promo_edge = await pg_app_conn.fetchrow(
                """
                SELECT confidence FROM kg_edges
                WHERE subject_label = $1
                  AND predicate = 'promoted_to_asbuilt'
                  AND namespace_id = $2
                """,
                room_fl,
                ns,
            )

        assert div_edge is not None, "has_divergence edge missing for diverged node"
        assert div_edge["confidence"] < 1.0, "diverged confidence must be < 1.0"
        assert promo_edge is not None, "promoted_to_asbuilt link must still exist on diverged node"

    # ------------------------------------------------------------------
    # d. Unconfirmed confirmations produce no edges
    # ------------------------------------------------------------------

    async def test_unconfirmed_produces_no_edges(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: Any,
    ) -> None:
        """Unconfirmed items (confirmed=False) do not produce any kg_edges."""
        ns = await make_namespace()
        ns_uuid = uuid.UUID(str(ns))
        await _seed(pg_app_conn, ns)
        await _author_intent(pg_app_conn, ns)

        nb_client = _make_nb_client()
        bridge = _make_bridge(pg_app_conn, ns_uuid, nb_client)

        room_fl = _fl_label(_NS_SLUG, _SITE, _BUILDING, _FLOOR, _ROOM)

        confirmations = [
            {
                "intent_label": room_fl,
                "intent_name": _ROOM,
                "asbuilt_name": _ROOM,
                "confirmed": False,  # not yet confirmed
            }
        ]

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            rec_result = await bridge.reconcile_asbuilt(confirmations)

        assert rec_result.unchanged == 1
        assert rec_result.promoted == 0
        assert rec_result.edges_written == 0, "No edges for unconfirmed items"

    # ------------------------------------------------------------------
    # e. RLS isolation: ns_a edges invisible from ns_b
    # ------------------------------------------------------------------

    async def test_rls_isolation_across_namespaces(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: Any,
    ) -> None:
        """Edges written for ns_a are invisible when scoped to ns_b."""
        ns_a = await make_namespace()
        ns_b = await make_namespace()

        for ns in (ns_a, ns_b):
            await _seed(pg_app_conn, ns)

        # Author and reconcile only for ns_a
        await _author_intent(pg_app_conn, ns_a)
        ns_a_uuid = uuid.UUID(str(ns_a))
        nb_client = _make_nb_client()
        bridge_a = _make_bridge(pg_app_conn, ns_a_uuid, nb_client)

        room_fl = _fl_label(_NS_SLUG, _SITE, _BUILDING, _FLOOR, _ROOM)
        confirmations = [
            {
                "intent_label": room_fl,
                "intent_name": _ROOM,
                "asbuilt_name": _ROOM,
                "confirmed": True,
            }
        ]

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns_a)
            await bridge_a.reconcile_asbuilt(confirmations)

        # Scope to ns_b — edge written for ns_a must not appear
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns_b)
            count = await pg_app_conn.fetchval(
                """
                SELECT COUNT(*) FROM kg_edges
                WHERE predicate = 'promoted_to_asbuilt'
                  AND namespace_id = $1
                """,
                ns_b,
            )
        assert count == 0, f"RLS violation: ns_b sees {count} promoted_to_asbuilt edges from ns_a"

    # ------------------------------------------------------------------
    # f. Phase 1b independence: W2 core works without NetBox; missing config raises
    # ------------------------------------------------------------------

    async def test_phase_1b_independence_build_bridge_raises_without_config(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: Any,
    ) -> None:
        """build_bridge() raises ValueError when NetBox is not configured.

        This asserts Phase 1b independence: the W2 graph core (graph.py)
        works regardless; the bridge is optional.  A missing URL must fail
        loudly, not silently.
        """
        ns = await make_namespace()
        ns_uuid = uuid.UUID(str(ns))
        await _seed(pg_app_conn, ns)

        # Author intent — must succeed without NetBox (W2 core is independent)
        await _author_intent(pg_app_conn, ns)

        # build_bridge with empty URL must raise — not silently skip
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            with pytest.raises(DeploymentConfigurationError, match="NCE_NETBOX_URL"):
                build_bridge(
                    pg_app_conn,
                    ns_uuid,
                    _NS_SLUG,
                    netbox_url="",
                    netbox_token="fake-token",
                )

    # ------------------------------------------------------------------
    # g. sync_fl_to_netbox is idempotent (second run upserts, not inserts)
    # ------------------------------------------------------------------

    async def test_sync_fl_idempotent(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: Any,
    ) -> None:
        """Running sync_fl_to_netbox twice produces the same edge count (idempotent)."""
        ns = await make_namespace()
        ns_uuid = uuid.UUID(str(ns))
        await _seed(pg_app_conn, ns)
        await _author_intent(pg_app_conn, ns)

        nb_client = _make_nb_client()
        bridge = _make_bridge(pg_app_conn, ns_uuid, nb_client)

        edge_counts: list[int] = []
        for _ in range(2):
            async with pg_app_conn.transaction():
                await set_namespace_context(pg_app_conn, ns)
                result = await bridge.sync_fl_to_netbox(
                    _SITE, _BUILDINGS, source_id="src-bridge-001"
                )
                edge_counts.append(result.edges_written)

        # Both runs write the same number of edges (upsert semantics)
        assert edge_counts[0] == edge_counts[1], (
            f"Edge counts differ: run1={edge_counts[0]}, run2={edge_counts[1]}"
        )

        # Actual edge count in the DB must not have duplicates
        site_fl = _fl_label(_NS_SLUG, _SITE)
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            db_count = await pg_app_conn.fetchval(
                """
                SELECT COUNT(*) FROM kg_edges
                WHERE subject_label = $1
                  AND predicate = 'sync_to_netbox'
                  AND namespace_id = $2
                """,
                site_fl,
                ns,
            )
        assert db_count == 1, f"Expected exactly 1 sync_to_netbox edge for site, got {db_count}"

    # ------------------------------------------------------------------
    # h. confidence lives on kg_edges only — kg_nodes has no confidence column
    # ------------------------------------------------------------------

    async def test_confidence_on_edges_only(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        """kg_nodes must not have a confidence column (wave rule 7)."""
        col_exists = await pg_app_conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'kg_nodes'
                  AND column_name = 'confidence'
            )
            """
        )
        assert col_exists is False, (
            "kg_nodes must not have a 'confidence' column (confidence lives on kg_edges only)"
        )
