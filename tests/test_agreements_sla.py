"""
tests/test_agreements_sla.py
==============================
Integration tests for M3.W10 — do_set_sla_coverage + get_sla_coverage.

Key invariants asserted
-----------------------
1. Happy path: 3 SLA terms + an FL id → status ok; one AGREEMENT_TERM node per
   term (``AgreementTerm:<id>:sla_<key>``) and the single
   ``Agreement:<id> -covers-> FL:<...>`` edge at confidence 1.0.
2. The coverage edge points at the EXACT FUNCTIONAL_LOCATION label System
   Design would author — asserted against ``system_design.graph._fl_label``
   itself (the source of truth), not a re-derived string.
3. The FL node need NOT pre-exist for the edge to be written — the label-based,
   no-FK coverage assertion lands even with zero FUNCTIONAL_LOCATION nodes.
4. get_sla_coverage returns the covers edge(s) + sla term labels; a second
   namespace sees none (RLS + explicit namespace predicate).
5. Missing required params fail loud with ValueError before any DB access.
6. §9.1 boundary: Agreements writes ONLY term nodes + the covers edge — it
   creates NO FUNCTIONAL_LOCATION node (System Design owns it) and no edge with
   a predicate other than ``covers`` / ``has_term`` for the agreement.

Seeding convention (mirrors tests/test_agreements_kickback.py)
---------------------------------------------------------------
- Seed node ownership before writing AGREEMENT_TERM nodes (assert_owner gate).
- Reads/asserts run inside scoped_pg_session with an explicit namespace_id
  predicate (owner-pool test roles can bypass FORCE RLS).

Run with::

    set -a && source .env && set +a
    .venv/Scripts/python.exe -m pytest tests/test_agreements_sla.py -q -rs

(Never set NCE_INTEGRATION_REFRESH_SIGNING_ON_DECRYPT_FAIL.)
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from nce.auth import set_namespace_context
from nce.db_utils import scoped_pg_session
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.agreements.sla import do_set_sla_coverage, get_sla_coverage
from nce.vertical_modules.system_design.graph import _fl_label

# A representative driftsavtale SLA-term set.
_SLA_TERMS: dict[str, object] = {
    "responseHours": 4,
    "coverageWindow": "24x7",
    "resolutionHours": 24,
}
_EXPECTED_TERM_TYPES = ["sla_responseHours", "sla_coverageWindow", "sla_resolutionHours"]


# ---------------------------------------------------------------------------
# Engine stub
# ---------------------------------------------------------------------------


class _EngineStub:
    """Minimal engine stub — holds pg_pool only."""

    def __init__(self, pg_pool: asyncpg.Pool | None) -> None:
        self.pg_pool = pg_pool


# ---------------------------------------------------------------------------
# Seeding / query helpers
# ---------------------------------------------------------------------------


async def _seed_ownership(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID) -> None:
    """Seed node ownership registry for the test namespace."""
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)
            await seed_node_ownership_registry(conn, namespace_id)


async def _namespace_slug(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID) -> str:
    """Return the slug of a namespace (used to reconstruct the expected FL label)."""
    async with pg_pool.acquire() as conn:
        slug = await conn.fetchval("SELECT slug FROM namespaces WHERE id = $1", namespace_id)
    assert slug is not None, "namespace has no slug"
    return str(slug)


async def _edge_confidence(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    *,
    subject: str,
    predicate: str,
    obj: str,
) -> float | None:
    """Return the confidence of a specific kg_edge, or None if it is absent."""
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        val = await conn.fetchval(
            """
            SELECT confidence FROM kg_edges
            WHERE subject_label = $1 AND predicate = $2 AND object_label = $3
              AND namespace_id = $4::uuid
            """,
            subject,
            predicate,
            obj,
            str(namespace_id),
        )
    return None if val is None else float(val)


async def _node_entity_type(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    label: str,
) -> str | None:
    """Return the entity_type of a kg_node by label, or None if it is absent."""
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        val = await conn.fetchval(
            "SELECT entity_type FROM kg_nodes WHERE label = $1 AND namespace_id = $2::uuid",
            label,
            str(namespace_id),
        )
    return None if val is None else str(val)


async def _count_functional_location_nodes(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> int:
    """Count FUNCTIONAL_LOCATION nodes in a namespace (Agreements must create 0)."""
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        count = await conn.fetchval(
            """
            SELECT count(*) FROM kg_nodes
            WHERE entity_type = 'FUNCTIONAL_LOCATION' AND namespace_id = $1::uuid
            """,
            str(namespace_id),
        )
    return int(count)


async def _edge_predicates_for_subject(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    subject: str,
) -> list[str]:
    """Return every predicate written with the given subject_label."""
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        rows = await conn.fetch(
            "SELECT predicate FROM kg_edges WHERE subject_label = $1 AND namespace_id = $2::uuid",
            subject,
            str(namespace_id),
        )
    return [r["predicate"] for r in rows]


# ---------------------------------------------------------------------------
# 1. Happy path — term nodes + one covers edge at confidence 1.0
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_set_sla_coverage_writes_terms_and_covers_edge(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """3 SLA terms + FL id → ok; term nodes exist; covers edge at confidence 1.0."""
    await _seed_ownership(pg_pool, namespace_id)

    agreement_id = uuid.uuid4()
    fl_id = "OsloHQ:Floor2:Room4"
    slug = await _namespace_slug(pg_pool, namespace_id)
    expected_fl = _fl_label(slug, *fl_id.split(":"))
    agreement_label = f"Agreement:{agreement_id}"

    engine = _EngineStub(pg_pool)
    result = await do_set_sla_coverage(
        engine,
        {
            "namespace_id": str(namespace_id),
            "agreement_id": str(agreement_id),
            "functional_location_id": fl_id,
            "sla_terms": _SLA_TERMS,
        },
    )

    assert result["status"] == "ok"
    assert result["agreement_id"] == str(agreement_id)
    assert result["functional_location_id"] == fl_id
    assert result["sla_terms_written"] == _EXPECTED_TERM_TYPES
    assert result["covers_edge"] == {
        "subject": agreement_label,
        "predicate": "covers",
        "object": expected_fl,
    }

    # Each SLA term is an AGREEMENT_TERM identity node.
    for term_type in _EXPECTED_TERM_TYPES:
        term_label = f"AgreementTerm:{agreement_id}:{term_type}"
        assert await _node_entity_type(pg_pool, namespace_id, term_label) == "AGREEMENT_TERM"

    # The single coverage edge exists at confidence 1.0.
    confidence = await _edge_confidence(
        pg_pool,
        namespace_id,
        subject=agreement_label,
        predicate="covers",
        obj=expected_fl,
    )
    assert confidence == 1.0


# ---------------------------------------------------------------------------
# 2. Coverage edge matches System Design's FUNCTIONAL_LOCATION convention
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_covers_edge_matches_system_design_fl_convention(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """The persisted object_label is EXACTLY what system_design._fl_label yields."""
    await _seed_ownership(pg_pool, namespace_id)

    agreement_id = uuid.uuid4()
    fl_id = "bergen-site:bygg-a:1etg:rom-101"
    slug = await _namespace_slug(pg_pool, namespace_id)
    # Source of truth for the FL label convention — reconstructed via the exact
    # System Design helper (FL:<NS_SLUG>:<PATH> upper-cased, per-part).
    expected_fl = _fl_label(slug, *fl_id.split(":"))
    agreement_label = f"Agreement:{agreement_id}"

    result = await do_set_sla_coverage(
        _EngineStub(pg_pool),
        {
            "namespace_id": str(namespace_id),
            "agreement_id": str(agreement_id),
            "functional_location_id": fl_id,
            "sla_terms": {"responseHours": 8},
        },
    )
    assert result["covers_edge"]["object"] == expected_fl

    # Assert the exact object_label as actually persisted in kg_edges.
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        persisted = await conn.fetchval(
            """
            SELECT object_label FROM kg_edges
            WHERE subject_label = $1 AND predicate = 'covers' AND namespace_id = $2::uuid
            """,
            agreement_label,
            str(namespace_id),
        )
    assert persisted == expected_fl


# ---------------------------------------------------------------------------
# 3. Label-based, no-FK coverage — edge lands with no FL node present
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_covers_edge_written_without_preexisting_fl_node(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """No FUNCTIONAL_LOCATION node is seeded, yet the coverage edge still lands."""
    await _seed_ownership(pg_pool, namespace_id)

    agreement_id = uuid.uuid4()
    fl_id = "unbuilt-site:room-x"
    slug = await _namespace_slug(pg_pool, namespace_id)
    expected_fl = _fl_label(slug, *fl_id.split(":"))
    agreement_label = f"Agreement:{agreement_id}"

    # Precondition: zero FL nodes exist in this namespace.
    assert await _count_functional_location_nodes(pg_pool, namespace_id) == 0

    result = await do_set_sla_coverage(
        _EngineStub(pg_pool),
        {
            "namespace_id": str(namespace_id),
            "agreement_id": str(agreement_id),
            "functional_location_id": fl_id,
            "sla_terms": {"responseHours": 4},
        },
    )
    assert result["status"] == "ok"

    # The edge landed even though its object node does not exist...
    confidence = await _edge_confidence(
        pg_pool,
        namespace_id,
        subject=agreement_label,
        predicate="covers",
        obj=expected_fl,
    )
    assert confidence == 1.0
    # ...and the FL node was NOT conjured into existence by writing the edge.
    assert await _node_entity_type(pg_pool, namespace_id, expected_fl) is None
    assert await _count_functional_location_nodes(pg_pool, namespace_id) == 0


# ---------------------------------------------------------------------------
# 4. get_sla_coverage — queryable coverage + namespace scoping
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_sla_coverage_returns_edge_and_terms_and_scopes(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    make_namespace,
) -> None:
    """get_sla_coverage echoes the covers edge + sla term labels; 2nd ns sees none."""
    await _seed_ownership(pg_pool, namespace_id)

    agreement_id = uuid.uuid4()
    fl_id = "trondheim-site:hovedbygg"
    slug = await _namespace_slug(pg_pool, namespace_id)
    expected_fl = _fl_label(slug, *fl_id.split(":"))

    await do_set_sla_coverage(
        _EngineStub(pg_pool),
        {
            "namespace_id": str(namespace_id),
            "agreement_id": str(agreement_id),
            "functional_location_id": fl_id,
            "sla_terms": _SLA_TERMS,
        },
    )

    view = await get_sla_coverage(pg_pool, namespace_id, agreement_id)
    assert view["agreement_id"] == str(agreement_id)
    assert view["covers"] == [{"object": expected_fl, "confidence": 1.0}]
    expected_term_labels = sorted(f"AgreementTerm:{agreement_id}:{t}" for t in _EXPECTED_TERM_TYPES)
    assert view["sla_terms"] == expected_term_labels

    # A second namespace sees NO coverage for this agreement (RLS scoping).
    other_namespace = await make_namespace()
    other_view = await get_sla_coverage(pg_pool, other_namespace, agreement_id)
    assert other_view["covers"] == []
    assert other_view["sla_terms"] == []


# ---------------------------------------------------------------------------
# 5. Missing required params fail loud
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_missing_required_params_raise_value_error(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Each missing/invalid required param raises ValueError before any DB access."""
    engine = _EngineStub(pg_pool)
    aid = str(uuid.uuid4())

    bad_param_sets = [
        {},  # namespace_id missing
        {"namespace_id": str(namespace_id)},  # agreement_id missing
        {"namespace_id": str(namespace_id), "agreement_id": aid},  # fl id missing
        {  # sla_terms missing
            "namespace_id": str(namespace_id),
            "agreement_id": aid,
            "functional_location_id": "site-x",
        },
        {  # sla_terms present but not a dict
            "namespace_id": str(namespace_id),
            "agreement_id": aid,
            "functional_location_id": "site-x",
            "sla_terms": "responseHours=4",
        },
    ]
    for params in bad_param_sets:
        with pytest.raises(ValueError):
            await do_set_sla_coverage(engine, params)


# ---------------------------------------------------------------------------
# 6. §9.1 boundary — ONLY terms + the covers edge, no FL node
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_only_terms_and_covers_edge_written_no_fl_node(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Agreements creates no FL node and writes no predicate beyond covers/has_term."""
    await _seed_ownership(pg_pool, namespace_id)

    agreement_id = uuid.uuid4()
    agreement_label = f"Agreement:{agreement_id}"

    await do_set_sla_coverage(
        _EngineStub(pg_pool),
        {
            "namespace_id": str(namespace_id),
            "agreement_id": str(agreement_id),
            "functional_location_id": "site-oslo",
            "sla_terms": _SLA_TERMS,
        },
    )

    # Agreements does NOT own FUNCTIONAL_LOCATION — none created by this wave.
    assert await _count_functional_location_nodes(pg_pool, namespace_id) == 0

    # The only edges for the agreement are the coverage edge + one has_term per
    # term (has_term is how upsert_agreement_term_node attaches a term node).
    predicates = await _edge_predicates_for_subject(pg_pool, namespace_id, agreement_label)
    assert set(predicates) <= {"covers", "has_term"}, predicates
    assert predicates.count("covers") == 1
    assert predicates.count("has_term") == len(_SLA_TERMS)
