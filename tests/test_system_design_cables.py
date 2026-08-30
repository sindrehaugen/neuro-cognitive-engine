"""
tests/test_system_design_cables.py
===================================
Module 6.Wave 15 — a CABLE is a two-ended object in the graph.

The defect this file pins down
------------------------------
``do_author_device_topology`` wrote ``uses_cable`` from the **source port only**,
so a cable reached the graph with a single termination and could not be drawn
between two ports.  The wave decision, recorded in ``devices.py``'s module
docstring and in the commit message, is:

    PORT -[uses_cable]-> CABLE, written from BOTH terminations.

What is asserted here
---------------------
* **Two-ended traversal** — both the ``from`` and the ``to`` PORT reach the
  CABLE node (RED before the fix: only the ``from`` edge existed).
* **Return contract** — ``authored.edges`` counts both cable edges.
* **Idempotent re-author** — re-authoring the same connection upserts onto the
  ``UNIQUE (subject_label, predicate, object_label, namespace_id)`` constraint on
  ``kg_edges`` (``nce/schema.sql``); it never duplicates.
* **Backfill safety** — a row authored before this wave has only the ``from``
  edge.  That state is *simulated* by deleting the ``to`` edge, and a re-author
  must add the missing edge back without duplicating the surviving one.
* **Namespace isolation** — two namespaces author byte-identical labels; every
  count query discriminates on the **SQL namespace predicate**, never on fixture
  uniqueness.  (§6.4's named trap: the integration pool is an owner pool and
  owner pools bypass FORCE RLS, so label uniqueness would prove nothing.)

Wildcard-safe labels
--------------------
No device / port / cable ref used here contains ``_`` or ``%``, and every query
below matches with ``=`` rather than ``LIKE``, so the LIKE-wildcard bug class
cannot hide a false pass.

All tests in this file are ``@pytest.mark.integration`` — they require live
Postgres.  There are no pure-unit tests here; two-endedness is a property of
what reaches the database, and only the database can witness it.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from nce.vertical_modules.system_design.devices import cable_label, port_label

# ---------------------------------------------------------------------------
# Fixture topology — one cabled connection between two single-port devices.
#
#   SOURCE:HDMI-OUT  --[connected_to]-->  SINK:HDMI-IN
#                    \                   /
#                     \--[uses_cable]-->/   CABLE:...:CBL-01   (both ends)
#
# Expected authored counts once the fix is in:
#   nodes = 2 DEVICE + 2 PORT + 1 CABLE                          = 5
#   edges = 2 contains + 2 has_port + 1 connected_to + 2 uses_cable = 7
# Before the fix `edges` was 6 — only the source port carried a cable edge.
# ---------------------------------------------------------------------------

_DESIGN_ID = "DESIGN-W15-CABLE"
_CABLE_REF = "CBL-01"

_EXPECTED_NODES = 5
_EXPECTED_EDGES = 7

_PRED_USES_CABLE = "uses_cable"

_DEVICES: list[dict[str, Any]] = [
    {
        "device_ref": "SOURCE",
        "capability": {"device_category": "Communication Devices"},
        "ports": [
            {
                "port_ref": "HDMI-OUT",
                "capability": {"signal_format": "HDMI", "port_direction": "output"},
            }
        ],
        "rack_ref": None,
    },
    {
        "device_ref": "SINK",
        "capability": {"device_category": "Displays"},
        "ports": [
            {
                "port_ref": "HDMI-IN",
                "capability": {"signal_format": "HDMI", "port_direction": "input"},
            }
        ],
        "rack_ref": None,
    },
]

_CONNECTIONS: list[dict[str, Any]] = [
    {
        "from_device_ref": "SOURCE",
        "from_port_ref": "HDMI-OUT",
        "to_device_ref": "SINK",
        "to_port_ref": "HDMI-IN",
        "confidence": 1.0,
        "cable_ref": _CABLE_REF,
    }
]

_FROM_PORT_LABEL = port_label(_DESIGN_ID, "SOURCE", "HDMI-OUT")
_TO_PORT_LABEL = port_label(_DESIGN_ID, "SINK", "HDMI-IN")
_CABLE_LABEL = cable_label(_DESIGN_ID, _CABLE_REF)

_MOCK_EMIT_DEV = "nce.vertical_modules.system_design.devices.emit_graph_write"


# ---------------------------------------------------------------------------
# Helpers — one job each, no shared mutable state.
# ---------------------------------------------------------------------------


async def _seed_ownership(pg_pool: Any, ns_id: uuid.UUID) -> None:
    """Register node ownership so ``assert_owner`` admits DEVICE/PORT/CABLE writes."""
    from nce.auth import set_namespace_context
    from nce.entity_resolution.ownership_seed import seed_node_ownership_registry

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns_id)
            await seed_node_ownership_registry(conn, ns_id)


async def _author_topology(pg_pool: Any, ns_id: uuid.UUID) -> dict[str, Any]:
    """Author the fixture topology once.  Returns the authored-count contract."""
    from nce.db_utils import scoped_pg_session
    from nce.vertical_modules.system_design.devices import do_author_device_topology

    with patch(_MOCK_EMIT_DEV, new_callable=AsyncMock):
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            return await do_author_device_topology(
                conn,
                ns_id,
                design_id=_DESIGN_ID,
                devices=_DEVICES,
                connections=_CONNECTIONS,
            )


async def _cable_subjects(pg_pool: Any, ns_id: uuid.UUID) -> list[str]:
    """Subjects of every ``uses_cable`` edge pointing at the fixture CABLE.

    The namespace predicate is in the SQL, not in the labels: the integration
    pool is an owner pool and bypasses FORCE RLS, so ``namespace_id = $3`` is the
    only real discriminator (§6.4).
    """
    from nce.auth import set_namespace_context

    async with pg_pool.acquire() as conn:
        await set_namespace_context(conn, ns_id)
        rows = await conn.fetch(
            """
            SELECT subject_label
            FROM kg_edges
            WHERE predicate    = $1
              AND object_label = $2
              AND namespace_id = $3::uuid
            ORDER BY subject_label
            """,
            _PRED_USES_CABLE,
            _CABLE_LABEL,
            str(ns_id),
        )
    return [row["subject_label"] for row in rows]


async def _delete_cable_edge(pg_pool: Any, ns_id: uuid.UUID, subject: str) -> int:
    """Delete one ``uses_cable`` edge.  Returns the number of rows removed."""
    from nce.auth import set_namespace_context

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns_id)
            status = await conn.execute(
                """
                DELETE FROM kg_edges
                WHERE subject_label = $1
                  AND predicate     = $2
                  AND object_label  = $3
                  AND namespace_id  = $4::uuid
                """,
                subject,
                _PRED_USES_CABLE,
                _CABLE_LABEL,
                str(ns_id),
            )
    return int(status.split()[-1])


@pytest.mark.integration
@pytest.mark.asyncio
class TestCableIsTwoEnded:
    """``PORT -[uses_cable]-> CABLE`` must exist from both terminations."""

    async def test_both_terminations_reach_the_cable(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """RED before the fix: only the source port had a ``uses_cable`` edge."""
        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        await _author_topology(pg_pool, ns_id)

        subjects = await _cable_subjects(pg_pool, ns_id)

        assert subjects == sorted([_FROM_PORT_LABEL, _TO_PORT_LABEL]), (
            f"a cable must be reachable from BOTH terminations; uses_cable subjects were {subjects}"
        )

    async def test_edge_count_contract_includes_both_cable_edges(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """The returned ``authored.edges`` counts the second cable edge."""
        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)

        result = await _author_topology(pg_pool, ns_id)

        assert result["authored"]["nodes"] == _EXPECTED_NODES
        assert result["authored"]["edges"] == _EXPECTED_EDGES, (
            "authored.edges must count uses_cable from both terminations; "
            f"got {result['authored']['edges']}"
        )


@pytest.mark.integration
@pytest.mark.asyncio
class TestCableReauthorIsIdempotent:
    """Re-authoring upserts onto the kg_edges UNIQUE constraint — never duplicates."""

    async def test_reauthor_does_not_duplicate(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)

        await _author_topology(pg_pool, ns_id)
        first = await _cable_subjects(pg_pool, ns_id)

        await _author_topology(pg_pool, ns_id)
        second = await _cable_subjects(pg_pool, ns_id)

        assert first == second, f"re-author changed the edge set: {first} -> {second}"
        assert len(second) == 2, f"expected exactly 2 uses_cable rows, got {second}"


@pytest.mark.integration
@pytest.mark.asyncio
class TestCableBackfillIsSafe:
    """A pre-wave one-ended row gains its missing edge and keeps the one it had."""

    async def test_reauthor_backfills_the_missing_termination(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        await _author_topology(pg_pool, ns_id)

        # Simulate a row authored before this wave: source edge only.
        deleted = await _delete_cable_edge(pg_pool, ns_id, _TO_PORT_LABEL)
        assert deleted == 1, "fixture setup failed: no destination cable edge to delete"

        pre_wave = await _cable_subjects(pg_pool, ns_id)
        assert pre_wave == [_FROM_PORT_LABEL], f"simulated pre-wave state is wrong: {pre_wave}"

        await _author_topology(pg_pool, ns_id)

        healed = await _cable_subjects(pg_pool, ns_id)
        assert healed == sorted([_FROM_PORT_LABEL, _TO_PORT_LABEL]), (
            f"re-author did not backfill the missing termination: {healed}"
        )


@pytest.mark.integration
@pytest.mark.asyncio
class TestCableEdgesAreNamespaceScoped:
    """Byte-identical labels in two namespaces stay separate rows.

    Fixture uniqueness is deliberately removed as a discriminator — both
    namespaces author the same ``design_id`` and the same refs, so the only
    thing that can separate them is ``namespace_id`` in the SQL predicate.
    """

    async def test_identical_labels_in_two_namespaces_do_not_bleed(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        ns_one: uuid.UUID = await make_namespace()
        ns_two: uuid.UUID = await make_namespace()
        assert ns_one != ns_two

        for ns_id in (ns_one, ns_two):
            await _seed_ownership(pg_pool, ns_id)
            await _author_topology(pg_pool, ns_id)

        expected = sorted([_FROM_PORT_LABEL, _TO_PORT_LABEL])
        assert await _cable_subjects(pg_pool, ns_one) == expected
        assert await _cable_subjects(pg_pool, ns_two) == expected
