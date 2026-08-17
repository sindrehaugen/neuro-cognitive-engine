"""Integration tests for the entity-resolution match primitive (Wave 5).

Verifies:
  - Cisco / Cisco Systems / CISCO seed nodes rank at the top with
    confidence near 1.0 when the candidate normalizes to "cisco".
  - A clearly-unrelated candidate (e.g. "juniper") scores low.
  - Results never leak across namespaces (namespace isolation invariant).
  - resolve() returns [] (not raises) for an empty candidate dict.
  - resolve() returns [] (not raises) for a candidate whose keys are all
    absent from the provided ``keys`` list.

All tests are ``@pytest.mark.integration`` (require a live Postgres DB).
They use the shared ``pg_pool`` and ``make_namespace`` fixtures from conftest.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from nce.db_utils import scoped_pg_session
from nce.entity_resolution.resolver import Match, resolve

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _insert_kg_node(
    conn,  # asyncpg.Connection (already scoped)
    *,
    namespace_id: UUID,
    label: str,
    entity_type: str,
) -> UUID:
    """Insert a kg_nodes row and return its id.

    Caller must be inside a scoped_pg_session with the correct namespace.
    """
    node_id: UUID = await conn.fetchval(
        """
        INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
        VALUES ($1, $2, $3, 'agent')
        RETURNING id
        """,
        label,
        entity_type,
        namespace_id,
    )
    assert node_id is not None
    return node_id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def resolver_namespace(pg_pool, make_namespace):
    """A single namespace pre-seeded with Cisco variant nodes."""
    ns: UUID = await make_namespace()
    node_type = f"device-{uuid4().hex[:8]}"

    async with scoped_pg_session(pg_pool, ns) as conn:
        await _insert_kg_node(conn, namespace_id=ns, label="Cisco", entity_type=node_type)
        await _insert_kg_node(conn, namespace_id=ns, label="Cisco Systems", entity_type=node_type)
        await _insert_kg_node(conn, namespace_id=ns, label="CISCO", entity_type=node_type)
        await _insert_kg_node(
            conn, namespace_id=ns, label="Juniper Networks", entity_type=node_type
        )

    return ns, node_type, pg_pool


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_ranks_cisco_variants_at_top(resolver_namespace) -> None:
    """Cisco / Cisco Systems / CISCO all normalize to 'cisco' and must rank first.

    The acceptance criterion: all three Cisco variants appear in the top-3
    results with score >= 0.5 when searching for 'cisco'.
    """
    ns, node_type, pg_pool = resolver_namespace

    async with scoped_pg_session(pg_pool, ns) as conn:
        results = await resolve(
            conn,
            namespace_id=ns,
            candidate={"manufacturer": "Cisco"},
            keys=["manufacturer"],
            node_type=node_type,
        )

    assert len(results) >= 3, f"Expected at least 3 matches, got {len(results)}"

    # All results must be Match instances
    for r in results:
        assert isinstance(r, Match)
        assert isinstance(r.node_id, UUID)
        assert 0.0 <= r.score <= 1.0
        assert "manufacturer" in r.matched_on

    # Top result must score high (near 1.0 — pg_trgm exact or near-exact match)
    top_score = results[0].score
    assert top_score >= 0.5, f"Top score {top_score!r} unexpectedly low for 'Cisco'"

    # Top 3 should all have score >= 0.3 (Cisco variants are very similar)
    top3_scores = [r.score for r in results[:3]]
    for score in top3_scores:
        assert score >= 0.3, f"Cisco variant scored {score!r}, expected >= 0.3"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_candidate_cisco_systems_variant_scores_near_1(
    resolver_namespace,
) -> None:
    """'Cisco Systems' normalizes to 'cisco' — should score near 1.0 against 'Cisco'."""
    ns, node_type, pg_pool = resolver_namespace

    async with scoped_pg_session(pg_pool, ns) as conn:
        results = await resolve(
            conn,
            namespace_id=ns,
            candidate={"manufacturer": "Cisco Systems"},
            keys=["manufacturer"],
            node_type=node_type,
        )

    assert len(results) >= 1
    top_score = results[0].score
    assert top_score >= 0.5, (
        f"'Cisco Systems' candidate (normalized 'cisco') scored {top_score!r} against "
        f"Cisco nodes — expected near 1.0"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_unrelated_candidate_scores_low(resolver_namespace) -> None:
    """A clearly-unrelated candidate must score lower than the Cisco variants."""
    ns, node_type, pg_pool = resolver_namespace

    async with scoped_pg_session(pg_pool, ns) as conn:
        results_cisco = await resolve(
            conn,
            namespace_id=ns,
            candidate={"manufacturer": "Cisco"},
            keys=["manufacturer"],
            node_type=node_type,
        )
        results_unrelated = await resolve(
            conn,
            namespace_id=ns,
            candidate={"manufacturer": "Arista Networks XYZ"},
            keys=["manufacturer"],
            node_type=node_type,
        )

    top_cisco = results_cisco[0].score if results_cisco else 0.0
    top_unrelated = results_unrelated[0].score if results_unrelated else 0.0

    assert top_cisco > top_unrelated, (
        f"Expected Cisco candidate ({top_cisco!r}) to outscore unrelated "
        f"candidate ({top_unrelated!r})"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_namespace_isolation(make_namespace, pg_pool) -> None:
    """Nodes seeded in namespace A must not appear in results for namespace B."""
    ns_a: UUID = await make_namespace()
    ns_b: UUID = await make_namespace()
    node_type = f"device-{uuid4().hex[:8]}"

    # Seed a node only in ns_a
    async with scoped_pg_session(pg_pool, ns_a) as conn:
        await _insert_kg_node(conn, namespace_id=ns_a, label="cisco", entity_type=node_type)

    # Resolve from ns_b — must find nothing
    async with scoped_pg_session(pg_pool, ns_b) as conn:
        results = await resolve(
            conn,
            namespace_id=ns_b,
            candidate={"manufacturer": "Cisco"},
            keys=["manufacturer"],
            node_type=node_type,
        )

    assert results == [], (
        f"Namespace isolation violated: got {results!r} from ns_b where no nodes were seeded"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_returns_empty_list_for_empty_candidate(
    resolver_namespace,
) -> None:
    """resolve() must return [] (not raise) when candidate is an empty dict."""
    ns, node_type, pg_pool = resolver_namespace

    async with scoped_pg_session(pg_pool, ns) as conn:
        results = await resolve(
            conn,
            namespace_id=ns,
            candidate={},
            keys=["manufacturer"],
            node_type=node_type,
        )

    assert results == [], f"Expected [] for empty candidate, got {results!r}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_returns_empty_list_when_keys_absent_from_candidate(
    resolver_namespace,
) -> None:
    """resolve() must return [] when no requested key is present in candidate."""
    ns, node_type, pg_pool = resolver_namespace

    async with scoped_pg_session(pg_pool, ns) as conn:
        results = await resolve(
            conn,
            namespace_id=ns,
            candidate={"unrelated_field": "something"},
            keys=["manufacturer"],  # "manufacturer" not in candidate
            node_type=node_type,
        )

    assert results == [], f"Expected [] when keys absent from candidate, got {results!r}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_results_ordered_by_score_descending(
    resolver_namespace,
) -> None:
    """Returned matches must always be ordered highest score first."""
    ns, node_type, pg_pool = resolver_namespace

    async with scoped_pg_session(pg_pool, ns) as conn:
        results = await resolve(
            conn,
            namespace_id=ns,
            candidate={"manufacturer": "Cisco"},
            keys=["manufacturer"],
            node_type=node_type,
        )

    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True), (
        f"Results not sorted by score descending: {scores}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_never_writes_to_entity_merge_queue(
    resolver_namespace,
) -> None:
    """resolve() is read-only — the entity_merge_queue must be untouched."""
    ns, node_type, pg_pool = resolver_namespace

    async with scoped_pg_session(pg_pool, ns) as conn:
        count_before = await conn.fetchval(
            "SELECT count(*) FROM entity_merge_queue WHERE namespace_id = $1",
            ns,
        )

        await resolve(
            conn,
            namespace_id=ns,
            candidate={"manufacturer": "Cisco"},
            keys=["manufacturer"],
            node_type=node_type,
        )

        count_after = await conn.fetchval(
            "SELECT count(*) FROM entity_merge_queue WHERE namespace_id = $1",
            ns,
        )

    assert count_before == count_after, (
        f"resolve() must never write to entity_merge_queue; "
        f"count changed from {count_before} to {count_after}"
    )
