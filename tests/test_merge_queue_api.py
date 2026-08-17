"""Integration tests for the C1 merge-review queue API (Wave 6).

Verifies the acceptance gate requirements:
  - enqueue  → row created with status ``pending``
  - confirm  → status becomes ``confirmed``, decider recorded
  - reject   → status becomes ``rejected``, decider recorded
  - No node (kg_nodes / kg_edges) is mutated by any path

All tests are ``@pytest.mark.integration`` (require a live Postgres DB).
They use the shared ``pg_pool`` and ``make_namespace`` fixtures from conftest.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from nce.db_utils import scoped_pg_session
from nce.entity_resolution.merge_queue import confirm, enqueue, list_pending, reject

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _kg_nodes_count(conn, *, namespace_id: UUID) -> int:
    """Count kg_nodes rows in the namespace (snapshot for mutation guard)."""
    return int(
        await conn.fetchval(
            "SELECT COUNT(*) FROM kg_nodes WHERE namespace_id = $1",
            namespace_id,
        )
    )


async def _kg_edges_count(conn, *, namespace_id: UUID) -> int:
    """Count kg_edges rows in the namespace (snapshot for mutation guard)."""
    return int(
        await conn.fetchval(
            "SELECT COUNT(*) FROM kg_edges WHERE namespace_id = $1",
            namespace_id,
        )
    )


async def _fetch_queue_row(conn, *, queue_id: UUID) -> dict:  # type: ignore[type-arg]
    """Fetch a single entity_merge_queue row as a dict (admin conn, no RLS needed)."""
    row = await conn.fetchrow(
        "SELECT * FROM entity_merge_queue WHERE id = $1",
        queue_id,
    )
    assert row is not None, f"Queue row {queue_id} not found"
    return dict(row)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def ns(make_namespace) -> UUID:
    """A fresh namespace for each test."""
    return await make_namespace()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestEnqueue:
    """enqueue() inserts a row with status='pending'."""

    async def test_enqueue_returns_uuid(self, pg_pool, ns: UUID) -> None:
        """enqueue() returns the new queue row id as a UUID."""
        async with scoped_pg_session(pg_pool, ns) as conn:
            queue_id = await enqueue(
                conn,
                namespace_id=ns,
                node_type="device",
                candidate={"label": "Cisco Catalyst 9300"},
                target=None,
                score=0.72,
            )
        assert isinstance(queue_id, UUID), f"Expected UUID, got {type(queue_id)}"

    async def test_enqueue_status_is_pending(self, pg_pool, pg_admin_conn, ns: UUID) -> None:
        """enqueue() creates a row with status 'pending'."""
        async with scoped_pg_session(pg_pool, ns) as conn:
            queue_id = await enqueue(
                conn,
                namespace_id=ns,
                node_type="device",
                candidate={"label": "Cisco Catalyst 9300"},
                target=None,
                score=0.72,
            )
        row = await _fetch_queue_row(pg_admin_conn, queue_id=queue_id)
        assert row["status"] == "pending", f"Expected 'pending', got {row['status']!r}"

    async def test_enqueue_stores_candidate_payload(self, pg_pool, pg_admin_conn, ns: UUID) -> None:
        """enqueue() persists the candidate dict as JSONB in candidate_payload."""
        import json

        payload = {"label": "Switch-A", "serial": "SN123456"}
        async with scoped_pg_session(pg_pool, ns) as conn:
            queue_id = await enqueue(
                conn,
                namespace_id=ns,
                node_type="device",
                candidate=payload,
                target=None,
                score=0.65,
            )
        row = await _fetch_queue_row(pg_admin_conn, queue_id=queue_id)
        # asyncpg may return JSONB columns as a JSON string or dict depending on
        # codec registration; normalise to dict before comparing.
        stored = row["candidate_payload"]
        if isinstance(stored, str):
            stored = json.loads(stored)
        assert stored == payload

    async def test_enqueue_stores_target_node_id(self, pg_pool, pg_admin_conn, ns: UUID) -> None:
        """enqueue() stores the optional target_node_id."""
        target_id = uuid4()
        async with scoped_pg_session(pg_pool, ns) as conn:
            queue_id = await enqueue(
                conn,
                namespace_id=ns,
                node_type="device",
                candidate={"label": "Router-B"},
                target=target_id,
                score=0.81,
            )
        row = await _fetch_queue_row(pg_admin_conn, queue_id=queue_id)
        assert row["target_node_id"] == target_id

    async def test_enqueue_no_node_mutation(self, pg_pool, pg_admin_conn, ns: UUID) -> None:
        """enqueue() does not create or modify any kg_nodes / kg_edges row."""
        nodes_before = await _kg_nodes_count(pg_admin_conn, namespace_id=ns)
        edges_before = await _kg_edges_count(pg_admin_conn, namespace_id=ns)

        async with scoped_pg_session(pg_pool, ns) as conn:
            await enqueue(
                conn,
                namespace_id=ns,
                node_type="device",
                candidate={"label": "Arista EOS"},
                target=None,
                score=0.55,
            )

        nodes_after = await _kg_nodes_count(pg_admin_conn, namespace_id=ns)
        edges_after = await _kg_edges_count(pg_admin_conn, namespace_id=ns)
        assert nodes_after == nodes_before, (
            f"kg_nodes changed after enqueue: {nodes_before} → {nodes_after}"
        )
        assert edges_after == edges_before, (
            f"kg_edges changed after enqueue: {edges_before} → {edges_after}"
        )


@pytest.mark.integration
@pytest.mark.asyncio
class TestListPending:
    """list_pending() returns only pending rows scoped to the namespace."""

    async def test_list_pending_returns_enqueued_row(self, pg_pool, ns: UUID) -> None:
        """list_pending() includes the row just enqueued."""
        async with scoped_pg_session(pg_pool, ns) as conn:
            queue_id = await enqueue(
                conn,
                namespace_id=ns,
                node_type="device",
                candidate={"label": "Pending Device"},
                target=None,
                score=0.60,
            )

        async with scoped_pg_session(pg_pool, ns) as conn:
            pending = await list_pending(conn, namespace_id=ns)

        ids = [r["id"] for r in pending]
        assert queue_id in ids, f"Enqueued row {queue_id} not in list_pending result"

    async def test_list_pending_excludes_confirmed(self, pg_pool, pg_admin_conn, ns: UUID) -> None:
        """list_pending() does not return rows that have been confirmed."""
        async with scoped_pg_session(pg_pool, ns) as conn:
            queue_id = await enqueue(
                conn,
                namespace_id=ns,
                node_type="device",
                candidate={"label": "Confirmed Device"},
                target=None,
                score=0.77,
            )
            await confirm(conn, namespace_id=ns, queue_id=queue_id, decided_by="tester")

        async with scoped_pg_session(pg_pool, ns) as conn:
            pending = await list_pending(conn, namespace_id=ns)

        ids = [r["id"] for r in pending]
        assert queue_id not in ids, "Confirmed row must not appear in list_pending"

    async def test_list_pending_excludes_rejected(self, pg_pool, ns: UUID) -> None:
        """list_pending() does not return rows that have been rejected."""
        async with scoped_pg_session(pg_pool, ns) as conn:
            queue_id = await enqueue(
                conn,
                namespace_id=ns,
                node_type="device",
                candidate={"label": "Rejected Device"},
                target=None,
                score=0.50,
            )
            await reject(conn, namespace_id=ns, queue_id=queue_id, decided_by="tester")

        async with scoped_pg_session(pg_pool, ns) as conn:
            pending = await list_pending(conn, namespace_id=ns)

        ids = [r["id"] for r in pending]
        assert queue_id not in ids, "Rejected row must not appear in list_pending"

    async def test_list_pending_namespace_isolation(self, pg_pool, make_namespace) -> None:
        """list_pending() only returns rows belonging to the requested namespace."""
        ns_a: UUID = await make_namespace()
        ns_b: UUID = await make_namespace()

        async with scoped_pg_session(pg_pool, ns_a) as conn:
            queue_id_a = await enqueue(
                conn,
                namespace_id=ns_a,
                node_type="device",
                candidate={"label": "Device-A"},
                target=None,
                score=0.70,
            )

        async with scoped_pg_session(pg_pool, ns_b) as conn:
            pending_b = await list_pending(conn, namespace_id=ns_b)

        ids_b = [r["id"] for r in pending_b]
        assert queue_id_a not in ids_b, "Namespace B must not see namespace A's pending rows"


@pytest.mark.integration
@pytest.mark.asyncio
class TestConfirm:
    """confirm() marks a row as confirmed with decider; never mutates nodes."""

    async def test_confirm_sets_status_confirmed(self, pg_pool, pg_admin_conn, ns: UUID) -> None:
        """confirm() transitions status from pending to confirmed."""
        async with scoped_pg_session(pg_pool, ns) as conn:
            queue_id = await enqueue(
                conn,
                namespace_id=ns,
                node_type="device",
                candidate={"label": "Switch-Confirm"},
                target=None,
                score=0.88,
            )
            await confirm(conn, namespace_id=ns, queue_id=queue_id, decided_by="operator-1")

        row = await _fetch_queue_row(pg_admin_conn, queue_id=queue_id)
        assert row["status"] == "confirmed", f"Expected 'confirmed', got {row['status']!r}"

    async def test_confirm_records_decided_by(self, pg_pool, pg_admin_conn, ns: UUID) -> None:
        """confirm() stores the decider identifier on the row."""
        decider = "human-reviewer-alice"
        async with scoped_pg_session(pg_pool, ns) as conn:
            queue_id = await enqueue(
                conn,
                namespace_id=ns,
                node_type="device",
                candidate={"label": "Switch-DeciderCheck"},
                target=None,
                score=0.85,
            )
            await confirm(conn, namespace_id=ns, queue_id=queue_id, decided_by=decider)

        row = await _fetch_queue_row(pg_admin_conn, queue_id=queue_id)
        assert row["decided_by"] == decider

    async def test_confirm_records_decided_at(self, pg_pool, pg_admin_conn, ns: UUID) -> None:
        """confirm() sets decided_at to a non-null timestamp."""
        async with scoped_pg_session(pg_pool, ns) as conn:
            queue_id = await enqueue(
                conn,
                namespace_id=ns,
                node_type="device",
                candidate={"label": "Switch-Timestamp"},
                target=None,
                score=0.80,
            )
            await confirm(conn, namespace_id=ns, queue_id=queue_id, decided_by="tester")

        row = await _fetch_queue_row(pg_admin_conn, queue_id=queue_id)
        assert row["decided_at"] is not None, "decided_at must be set after confirm"

    async def test_confirm_no_node_mutation(self, pg_pool, pg_admin_conn, ns: UUID) -> None:
        """confirm() does not create or modify any kg_nodes / kg_edges row."""
        nodes_before = await _kg_nodes_count(pg_admin_conn, namespace_id=ns)
        edges_before = await _kg_edges_count(pg_admin_conn, namespace_id=ns)

        async with scoped_pg_session(pg_pool, ns) as conn:
            queue_id = await enqueue(
                conn,
                namespace_id=ns,
                node_type="device",
                candidate={"label": "No-Merge-Device"},
                target=None,
                score=0.90,
            )
            await confirm(conn, namespace_id=ns, queue_id=queue_id, decided_by="tester")

        nodes_after = await _kg_nodes_count(pg_admin_conn, namespace_id=ns)
        edges_after = await _kg_edges_count(pg_admin_conn, namespace_id=ns)
        assert nodes_after == nodes_before, (
            f"kg_nodes changed after confirm: {nodes_before} → {nodes_after} "
            "(SCOPE VIOLATION — confirm must never mutate nodes)"
        )
        assert edges_after == edges_before, (
            f"kg_edges changed after confirm: {edges_before} → {edges_after} "
            "(SCOPE VIOLATION — confirm must never mutate edges)"
        )

    async def test_confirm_raises_on_missing_row(self, pg_pool, ns: UUID) -> None:
        """confirm() raises LookupError for a non-existent queue_id."""
        phantom_id = uuid4()
        async with scoped_pg_session(pg_pool, ns) as conn:
            with pytest.raises(LookupError):
                await confirm(conn, namespace_id=ns, queue_id=phantom_id, decided_by="tester")

    async def test_confirm_raises_on_already_decided_row(self, pg_pool, ns: UUID) -> None:
        """confirm() raises LookupError when the row is already decided."""
        async with scoped_pg_session(pg_pool, ns) as conn:
            queue_id = await enqueue(
                conn,
                namespace_id=ns,
                node_type="device",
                candidate={"label": "Double-Decide"},
                target=None,
                score=0.83,
            )
            await confirm(conn, namespace_id=ns, queue_id=queue_id, decided_by="tester")

        async with scoped_pg_session(pg_pool, ns) as conn:
            with pytest.raises(LookupError):
                await confirm(conn, namespace_id=ns, queue_id=queue_id, decided_by="tester-2")


@pytest.mark.integration
@pytest.mark.asyncio
class TestReject:
    """reject() marks a row as rejected with decider; never mutates nodes."""

    async def test_reject_sets_status_rejected(self, pg_pool, pg_admin_conn, ns: UUID) -> None:
        """reject() transitions status from pending to rejected."""
        async with scoped_pg_session(pg_pool, ns) as conn:
            queue_id = await enqueue(
                conn,
                namespace_id=ns,
                node_type="device",
                candidate={"label": "Switch-Reject"},
                target=None,
                score=0.45,
            )
            await reject(conn, namespace_id=ns, queue_id=queue_id, decided_by="operator-2")

        row = await _fetch_queue_row(pg_admin_conn, queue_id=queue_id)
        assert row["status"] == "rejected", f"Expected 'rejected', got {row['status']!r}"

    async def test_reject_records_decided_by(self, pg_pool, pg_admin_conn, ns: UUID) -> None:
        """reject() stores the decider identifier on the row."""
        decider = "human-reviewer-bob"
        async with scoped_pg_session(pg_pool, ns) as conn:
            queue_id = await enqueue(
                conn,
                namespace_id=ns,
                node_type="device",
                candidate={"label": "Switch-RejectDecider"},
                target=None,
                score=0.40,
            )
            await reject(conn, namespace_id=ns, queue_id=queue_id, decided_by=decider)

        row = await _fetch_queue_row(pg_admin_conn, queue_id=queue_id)
        assert row["decided_by"] == decider

    async def test_reject_records_decided_at(self, pg_pool, pg_admin_conn, ns: UUID) -> None:
        """reject() sets decided_at to a non-null timestamp."""
        async with scoped_pg_session(pg_pool, ns) as conn:
            queue_id = await enqueue(
                conn,
                namespace_id=ns,
                node_type="device",
                candidate={"label": "Switch-RejectTimestamp"},
                target=None,
                score=0.42,
            )
            await reject(conn, namespace_id=ns, queue_id=queue_id, decided_by="tester")

        row = await _fetch_queue_row(pg_admin_conn, queue_id=queue_id)
        assert row["decided_at"] is not None, "decided_at must be set after reject"

    async def test_reject_no_node_mutation(self, pg_pool, pg_admin_conn, ns: UUID) -> None:
        """reject() does not create or modify any kg_nodes / kg_edges row."""
        nodes_before = await _kg_nodes_count(pg_admin_conn, namespace_id=ns)
        edges_before = await _kg_edges_count(pg_admin_conn, namespace_id=ns)

        async with scoped_pg_session(pg_pool, ns) as conn:
            queue_id = await enqueue(
                conn,
                namespace_id=ns,
                node_type="device",
                candidate={"label": "Reject-No-Merge"},
                target=None,
                score=0.35,
            )
            await reject(conn, namespace_id=ns, queue_id=queue_id, decided_by="tester")

        nodes_after = await _kg_nodes_count(pg_admin_conn, namespace_id=ns)
        edges_after = await _kg_edges_count(pg_admin_conn, namespace_id=ns)
        assert nodes_after == nodes_before, (
            f"kg_nodes changed after reject: {nodes_before} → {nodes_after} "
            "(SCOPE VIOLATION — reject must never mutate nodes)"
        )
        assert edges_after == edges_before, (
            f"kg_edges changed after reject: {edges_before} → {edges_after} "
            "(SCOPE VIOLATION — reject must never mutate edges)"
        )

    async def test_reject_raises_on_missing_row(self, pg_pool, ns: UUID) -> None:
        """reject() raises LookupError for a non-existent queue_id."""
        phantom_id = uuid4()
        async with scoped_pg_session(pg_pool, ns) as conn:
            with pytest.raises(LookupError):
                await reject(conn, namespace_id=ns, queue_id=phantom_id, decided_by="tester")

    async def test_reject_raises_on_already_decided_row(self, pg_pool, ns: UUID) -> None:
        """reject() raises LookupError when the row is already decided."""
        async with scoped_pg_session(pg_pool, ns) as conn:
            queue_id = await enqueue(
                conn,
                namespace_id=ns,
                node_type="device",
                candidate={"label": "Double-Reject"},
                target=None,
                score=0.38,
            )
            await reject(conn, namespace_id=ns, queue_id=queue_id, decided_by="tester")

        async with scoped_pg_session(pg_pool, ns) as conn:
            with pytest.raises(LookupError):
                await reject(conn, namespace_id=ns, queue_id=queue_id, decided_by="tester-2")
