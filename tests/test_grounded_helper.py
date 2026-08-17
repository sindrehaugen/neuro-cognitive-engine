"""
Integration tests for nce.structural.grounded — C9a grounded-generation helper.

Acceptance gate (Batch 021 / M0.W21):
  - Every emitted claim carries a source-node link (citation with node_id).
  - The fact text in every citation is the label retrieved FROM kg_nodes, not
    any caller-supplied string.
  - An unbacked claim (no matching kg_nodes row) is excluded from prose and
    reported in ``dropped``.

Requires a live Postgres with schema applied.
Skip automatically when no DB DSN is available.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from nce.db_utils import scoped_pg_session
from nce.structural import ground

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _insert_kg_node(pool, namespace_id, label: str):
    """Insert a minimal kg_nodes row and return its UUID."""
    async with pool.acquire() as conn:
        node_id = await conn.fetchval(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
            VALUES ($1, 'TEST_GROUNDED', $2, 'agent')
            RETURNING id
            """,
            label,
            namespace_id,
        )
    assert node_id is not None
    return node_id


async def _delete_kg_node(pool, node_id):
    """Clean up after ourselves."""
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM kg_nodes WHERE id = $1", node_id)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ground_all_claims_backed(pg_pool, make_namespace) -> None:
    """All backed claims appear in citations; prose contains the DB-stored labels."""
    ns_id = await make_namespace()
    # Use unique labels so we can assert the exact DB-stored text in prose.
    label_a = f"test-grounded-a-{uuid4().hex}"
    label_b = f"test-grounded-b-{uuid4().hex}"

    node_a = await _insert_kg_node(pg_pool, ns_id, label_a)
    node_b = await _insert_kg_node(pg_pool, ns_id, label_b)

    try:
        # Claims carry only node_id — no caller-supplied fact string.
        claims = [
            {"node_id": str(node_a)},
            {"node_id": str(node_b)},
        ]
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            result = await ground(
                conn,
                namespace_id=ns_id,
                claims=claims,
                template="Report: {facts}",
            )

        assert result["dropped"] == []
        assert len(result["citations"]) == 2

        cited_ids = {c["node_id"] for c in result["citations"]}
        assert str(node_a) in cited_ids
        assert str(node_b) in cited_ids

        # Every emitted claim carries a source-node link.
        for citation in result["citations"]:
            assert "node_id" in citation
            assert "fact" in citation

        # Prose is constructed from the DB-retrieved labels, not free-generated.
        # The fact text must equal the label stored in kg_nodes.
        prose = result["prose"]
        assert label_a in prose, f"DB label {label_a!r} not found in prose"
        assert label_b in prose, f"DB label {label_b!r} not found in prose"
        assert prose.startswith("Report:")

        # Confirm each citation's fact matches the corresponding DB label.
        citation_facts = {c["node_id"]: c["fact"] for c in result["citations"]}
        assert citation_facts[str(node_a)] == label_a
        assert citation_facts[str(node_b)] == label_b
    finally:
        await _delete_kg_node(pg_pool, node_a)
        await _delete_kg_node(pg_pool, node_b)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ground_unbacked_claim_excluded(pg_pool, make_namespace) -> None:
    """An unbacked claim (no kg_nodes row) is excluded from prose and in dropped."""
    ns_id = await make_namespace()
    label_real = f"test-grounded-real-{uuid4().hex}"

    node_real = await _insert_kg_node(pg_pool, ns_id, label_real)
    ghost_node_id = uuid4()  # never inserted — not a real graph node

    try:
        claims = [
            {"node_id": str(node_real)},
            {"node_id": str(ghost_node_id)},
        ]
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            result = await ground(
                conn,
                namespace_id=ns_id,
                claims=claims,
                template="Output: {facts}",
            )

        # The unbacked claim must appear in dropped, not citations.
        dropped_ids = {d["node_id"] for d in result["dropped"]}
        assert str(ghost_node_id) in dropped_ids

        cited_ids = {c["node_id"] for c in result["citations"]}
        assert str(ghost_node_id) not in cited_ids
        assert str(node_real) in cited_ids

        # The prose must contain the DB-stored label of the real node.
        prose = result["prose"]
        assert label_real in prose, f"DB label {label_real!r} not found in prose"

        # The prose must NOT contain any trace of the ghost node id.
        assert str(ghost_node_id) not in prose
    finally:
        await _delete_kg_node(pg_pool, node_real)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ground_all_claims_dropped_yields_empty_prose(pg_pool, make_namespace) -> None:
    """When every claim is unbacked, prose is empty and dropped lists all."""
    ns_id = await make_namespace()
    ghost_a = uuid4()
    ghost_b = uuid4()

    claims = [
        {"node_id": str(ghost_a)},
        {"node_id": str(ghost_b)},
    ]
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        result = await ground(
            conn,
            namespace_id=ns_id,
            claims=claims,
            template="Summary: {facts}",
        )

    assert result["prose"] == ""
    assert result["citations"] == []
    assert len(result["dropped"]) == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ground_cross_namespace_node_not_visible(pg_pool, make_namespace) -> None:
    """A node from a different namespace must not resolve — treated as unbacked."""
    ns_owner = await make_namespace()
    ns_other = await make_namespace()
    label = f"test-grounded-cross-{uuid4().hex}"

    # Insert the node in ns_owner.
    node_id = await _insert_kg_node(pg_pool, ns_owner, label)

    try:
        claims = [{"node_id": str(node_id)}]
        # Query under ns_other — the node belongs to ns_owner, must not resolve.
        async with scoped_pg_session(pg_pool, ns_other) as conn:
            result = await ground(
                conn,
                namespace_id=ns_other,
                claims=claims,
                template="{facts}",
            )

        assert result["citations"] == []
        assert len(result["dropped"]) == 1
        assert result["prose"] == ""
    finally:
        await _delete_kg_node(pg_pool, node_id)
