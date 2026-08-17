"""
tests/test_agreements_authoring.py
===================================
Integration tests for M3.W7 — the authored ("create-negotiate") path:
``do_create_agreement`` / ``do_suggest_revision`` / ``do_add_comment`` /
``get_agreement_activity``.

Key invariants asserted
-----------------------
1. Create with supplier + full terms: the AGREEMENT node, the graph-native
   ``lifecycleState`` = DRAFT term node, every money/legal term node, and the
   ``Vendor:{supplier} -under-> Agreement:{id}`` edge all exist — exact labels,
   exact entity_types, no vacuous checks.
2. Create with NO terms still writes the node + a DRAFT lifecycle term; None
   values and unknown keys are ignored (no bogus term node materializes).
3. ``do_suggest_revision`` is PROPOSE-ONLY: it appends one ``revision_suggestion``
   ledger row (correct field / proposed_value) and leaves the underlying
   AGREEMENT_TERM node byte-for-byte untouched — proven by an unchanged
   ``updated_at`` (a stronger proof than value-equality: NO write occurred).
   ``applied`` is False.
4. ``do_add_comment`` appends one ``comment`` ledger row and mutates NO kg_nodes
   for the agreement (identical label set + identical ``updated_at`` snapshot).
5. ``get_agreement_activity`` returns both rows newest-first; a second namespace
   sees none (explicit namespace scoping, never RLS-only).
6. Missing required params raise ValueError (create w/o namespace_id; suggest
   w/o field; comment w/o comment).
7. Ledger immutability discipline: authoring.py contains no UPDATE/DELETE
   against v3_cognitive_ledger (append-only audit trail).

Seeding convention (mirrors tests/test_agreements_kickback.py)
--------------------------------------------------------------
- Seed node ownership before the graph writers run (``upsert_agreement_node``
  asserts the ``agreements`` engine owns AGREEMENT / AGREEMENT_TERM).
- Use direct INSERTs / reads inside scoped_pg_session for deterministic state.
- VENDOR labels follow the module convention ``Vendor:{orgnr}``.

All tests are ``@pytest.mark.integration`` (+ ``@pytest.mark.asyncio`` for the
async ones) and require a live Postgres via the ``pg_pool`` / ``namespace_id`` /
``make_namespace`` fixtures in conftest.py.  Run with::

    set -a && source .env && set +a
    .venv/Scripts/python.exe -m pytest tests/test_agreements_authoring.py -q -rs
"""

from __future__ import annotations

import inspect
import json
import re
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

from nce.auth import set_namespace_context
from nce.db_utils import scoped_pg_session
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.agreements.authoring import (
    _DRAFT_ROW_STATUS,
    _KIND_COMMENT,
    _KIND_SUGGESTION,
    _MODEL_VERSION,
    do_add_comment,
    do_create_agreement,
    do_suggest_revision,
    get_agreement_activity,
)
from nce.vertical_modules.agreements.kickback import do_reconcile_kickback
from nce.vertical_modules.agreements.mcp_handlers import handle_agreements_lookup_terms

# Kickback's Economy GL seam, patched at the site kickback looks it up.
_KICKBACK_GL_SEAM = "nce.vertical_modules.agreements.kickback._read_economy_gl_rows"

# The full money/legal term set an authored agreement can carry (matches
# authoring._KNOWN_TERM_FIELDS order, which drives terms_written).
_FULL_TERMS: dict[str, Any] = {
    "validFrom": "2026-01-01",
    "validTo": "2026-12-31",
    "paymentTermsDays": 30,
    "frameDiscountPct": 2.5,
    "volumeCommitment": 1_000_000.0,
    "kickbackTiers": [{"threshold": 100_000, "pct": 3.0}],
}
_EXPECTED_TERMS_WRITTEN = [
    "validFrom",
    "validTo",
    "paymentTermsDays",
    "frameDiscountPct",
    "volumeCommitment",
    "kickbackTiers",
]


# ---------------------------------------------------------------------------
# Engine stub
# ---------------------------------------------------------------------------


class _EngineStub:
    """Minimal engine stub — holds the pg_pool the cores read/write through."""

    def __init__(self, pg_pool: asyncpg.Pool) -> None:
        self.pg_pool = pg_pool


# ---------------------------------------------------------------------------
# Seeding + query helpers
# ---------------------------------------------------------------------------


async def _seed_ownership(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID) -> None:
    """Seed the node-ownership registry so the AGREEMENT owner-guard passes."""
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)
            await seed_node_ownership_registry(conn, namespace_id)


async def _insert_vendor_node(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    *,
    label: str,
) -> None:
    """Insert a VENDOR node (realistic edge target for the ``under`` link)."""
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
            VALUES ($1, 'VENDOR', $2::uuid, 'agent')
            ON CONFLICT (label, namespace_id) DO NOTHING
            """,
            label,
            str(namespace_id),
        )


async def _node_row(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    label: str,
) -> asyncpg.Record | None:
    """Return (entity_type, updated_at) for one node label, or None."""
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        return await conn.fetchrow(
            "SELECT entity_type, updated_at FROM kg_nodes "
            "WHERE label = $1 AND namespace_id = $2::uuid",
            label,
            str(namespace_id),
        )


async def _edge_exists(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    *,
    subject_label: str,
    predicate: str,
    object_label: str,
) -> bool:
    """True when the exact (subject, predicate, object) edge exists in-namespace."""
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        row = await conn.fetchrow(
            """
            SELECT 1 FROM kg_edges
            WHERE subject_label = $1 AND predicate = $2 AND object_label = $3
              AND namespace_id = $4::uuid
            """,
            subject_label,
            predicate,
            object_label,
            str(namespace_id),
        )
    return row is not None


async def _agreement_node_snapshot(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    agreement_id: uuid.UUID,
) -> dict[str, Any]:
    """Map every kg_node of one agreement (AGREEMENT + AGREEMENT_TERMs) to its updated_at."""
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        rows = await conn.fetch(
            """
            SELECT label, updated_at FROM kg_nodes
            WHERE namespace_id = $1::uuid
              AND (label = $2 OR label LIKE $3)
            """,
            str(namespace_id),
            f"Agreement:{agreement_id}",
            f"AgreementTerm:{agreement_id}:%",
        )
    return {row["label"]: row["updated_at"] for row in rows}


async def _term_labels(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    agreement_id: uuid.UUID,
) -> set[str]:
    """Set of AGREEMENT_TERM node labels for one agreement."""
    snapshot = await _agreement_node_snapshot(pg_pool, namespace_id, agreement_id)
    prefix = f"AgreementTerm:{agreement_id}:"
    return {label for label in snapshot if label.startswith(prefix)}


async def _ledger_payloads(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    agreement_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Newest-first authoring ledger payloads for one agreement in one namespace."""
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        rows = await conn.fetch(
            """
            SELECT tlx_scores FROM v3_cognitive_ledger
            WHERE namespace_id = $1::uuid
              AND model_version = $2
              AND tlx_scores->>'agreement_id' = $3
            ORDER BY created_at DESC
            """,
            str(namespace_id),
            _MODEL_VERSION,
            str(agreement_id),
        )
    payloads: list[dict[str, Any]] = []
    for row in rows:
        payload = row["tlx_scores"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        payloads.append(payload or {})
    return payloads


# ---------------------------------------------------------------------------
# 1. Create with supplier + full terms — exact nodes, term nodes, edge
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_agreement_writes_node_terms_lifecycle_and_edge(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    await _seed_ownership(pg_pool, namespace_id)

    orgnr = "912345678"
    await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr}")

    engine = _EngineStub(pg_pool)
    result = await do_create_agreement(
        engine,
        {
            "namespace_id": str(namespace_id),
            "supplier_id": orgnr,
            "terms": _FULL_TERMS,
        },
    )

    assert result["status"] == "ok"
    assert result["lifecycle_state"] == "DRAFT"
    assert result["terms_written"] == _EXPECTED_TERMS_WRITTEN
    agreement_id = result["agreement_id"]
    assert result["label"] == f"Agreement:{agreement_id}"

    # AGREEMENT node exists with the right entity_type.
    ag_row = await _node_row(pg_pool, namespace_id, f"Agreement:{agreement_id}")
    assert ag_row is not None
    assert ag_row["entity_type"] == "AGREEMENT"

    # Graph-native lifecycle: DRAFT lifecycleState term node exists.
    lifecycle_row = await _node_row(
        pg_pool, namespace_id, f"AgreementTerm:{agreement_id}:lifecycleState"
    )
    assert lifecycle_row is not None
    assert lifecycle_row["entity_type"] == "AGREEMENT_TERM"

    # Every money/legal term node exists with the exact label + entity_type.
    for field in _EXPECTED_TERMS_WRITTEN:
        term_row = await _node_row(pg_pool, namespace_id, f"AgreementTerm:{agreement_id}:{field}")
        assert term_row is not None, f"missing term node for {field}"
        assert term_row["entity_type"] == "AGREEMENT_TERM"

    # Vendor:{supplier} -under-> Agreement:{id} edge exists.
    assert await _edge_exists(
        pg_pool,
        namespace_id,
        subject_label=f"Vendor:{orgnr}",
        predicate="under",
        object_label=f"Agreement:{agreement_id}",
    )


# ---------------------------------------------------------------------------
# 2. Create with no terms / unknown keys — node + DRAFT only, nothing bogus
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_agreement_no_terms_and_unknown_keys_ignored(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    await _seed_ownership(pg_pool, namespace_id)
    engine = _EngineStub(pg_pool)

    # (a) No terms key at all → node + DRAFT lifecycle term only.
    result_a = await do_create_agreement(engine, {"namespace_id": str(namespace_id)})
    assert result_a["status"] == "ok"
    assert result_a["terms_written"] == []
    id_a = uuid.UUID(result_a["agreement_id"])
    assert await _node_row(pg_pool, namespace_id, f"Agreement:{id_a}") is not None
    assert await _term_labels(pg_pool, namespace_id, id_a) == {
        f"AgreementTerm:{id_a}:lifecycleState"
    }

    # (b) None-valued known key + unknown key → both ignored, no bogus node.
    result_b = await do_create_agreement(
        engine,
        {
            "namespace_id": str(namespace_id),
            "terms": {"paymentTermsDays": None, "bogusKey": 999},
        },
    )
    assert result_b["status"] == "ok"
    assert result_b["terms_written"] == []
    id_b = uuid.UUID(result_b["agreement_id"])
    # Only the DRAFT lifecycle term node — the None value and unknown key wrote nothing.
    assert await _term_labels(pg_pool, namespace_id, id_b) == {
        f"AgreementTerm:{id_b}:lifecycleState"
    }
    assert await _node_row(pg_pool, namespace_id, f"AgreementTerm:{id_b}:bogusKey") is None
    assert await _node_row(pg_pool, namespace_id, f"AgreementTerm:{id_b}:paymentTermsDays") is None


# ---------------------------------------------------------------------------
# 3. Suggest revision — propose-only, term node untouched, applied False
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_suggest_revision_is_propose_only_and_never_mutates_term(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    await _seed_ownership(pg_pool, namespace_id)
    engine = _EngineStub(pg_pool)

    created = await do_create_agreement(
        engine,
        {
            "namespace_id": str(namespace_id),
            "supplier_id": "912345678",
            "terms": {"paymentTermsDays": 30},
        },
    )
    agreement_id = created["agreement_id"]
    term_label = f"AgreementTerm:{agreement_id}:paymentTermsDays"

    # Snapshot the term node BEFORE suggesting — updated_at is our immutability probe.
    before = await _node_row(pg_pool, namespace_id, term_label)
    assert before is not None

    result = await do_suggest_revision(
        engine,
        {
            "namespace_id": str(namespace_id),
            "agreement_id": agreement_id,
            "field": "paymentTermsDays",
            "proposed_value": 45,
            "rationale": "Net 45 improves cash flow",
            "author": "advisor@example",
        },
    )

    assert result["status"] == "ok"
    assert result["applied"] is False
    assert result["agreement_id"] == agreement_id
    assert result["suggestion_id"]

    # Exactly one ledger row, of the suggestion kind, with the exact payload.
    payloads = await _ledger_payloads(pg_pool, namespace_id, uuid.UUID(agreement_id))
    assert len(payloads) == 1
    assert payloads[0]["kind"] == _KIND_SUGGESTION
    assert payloads[0]["field"] == "paymentTermsDays"
    assert payloads[0]["proposed_value"] == 45
    assert payloads[0]["rationale"] == "Net 45 improves cash flow"

    # The underlying AGREEMENT_TERM node is byte-for-byte unchanged (no write).
    after = await _node_row(pg_pool, namespace_id, term_label)
    assert after is not None
    assert after["updated_at"] == before["updated_at"]


# ---------------------------------------------------------------------------
# 4. Add comment — ledger row appended, zero graph mutation
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_add_comment_appends_ledger_and_does_not_touch_graph(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    await _seed_ownership(pg_pool, namespace_id)
    engine = _EngineStub(pg_pool)

    created = await do_create_agreement(
        engine,
        {
            "namespace_id": str(namespace_id),
            "supplier_id": "912345678",
            "terms": {"paymentTermsDays": 30, "frameDiscountPct": 2.0},
        },
    )
    agreement_id = uuid.UUID(created["agreement_id"])

    # Snapshot every agreement node (labels + updated_at) BEFORE commenting.
    before = await _agreement_node_snapshot(pg_pool, namespace_id, agreement_id)
    assert before  # sanity: nodes were written

    result = await do_add_comment(
        engine,
        {
            "namespace_id": str(namespace_id),
            "agreement_id": str(agreement_id),
            "comment": "Please review clause 4 before signing.",
            "author": "legal@example",
        },
    )

    assert result["status"] == "ok"
    assert result["comment_id"]
    assert result["agreement_id"] == str(agreement_id)

    # One ledger row of the comment kind with the exact payload.
    payloads = await _ledger_payloads(pg_pool, namespace_id, agreement_id)
    assert len(payloads) == 1
    assert payloads[0]["kind"] == _KIND_COMMENT
    assert payloads[0]["comment"] == "Please review clause 4 before signing."
    assert payloads[0]["author"] == "legal@example"

    # No node added, removed, or updated for the agreement.
    after = await _agreement_node_snapshot(pg_pool, namespace_id, agreement_id)
    assert after == before


# ---------------------------------------------------------------------------
# 5. get_agreement_activity — newest-first, namespace-scoped
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_agreement_activity_newest_first_and_namespace_scoped(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    make_namespace: Any,
) -> None:
    await _seed_ownership(pg_pool, namespace_id)
    engine = _EngineStub(pg_pool)

    created = await do_create_agreement(
        engine,
        {
            "namespace_id": str(namespace_id),
            "supplier_id": "912345678",
            "terms": {"paymentTermsDays": 30},
        },
    )
    agreement_id = created["agreement_id"]

    # Suggestion first, comment second (comment is therefore the newest row).
    await do_suggest_revision(
        engine,
        {
            "namespace_id": str(namespace_id),
            "agreement_id": agreement_id,
            "field": "paymentTermsDays",
            "proposed_value": 45,
        },
    )
    await do_add_comment(
        engine,
        {
            "namespace_id": str(namespace_id),
            "agreement_id": agreement_id,
            "comment": "Agreed on Net 45.",
        },
    )

    activity = await get_agreement_activity(pg_pool, namespace_id, agreement_id)
    assert len(activity) == 2
    # Newest-first: the comment (recorded last) precedes the suggestion.
    assert activity[0]["kind"] == _KIND_COMMENT
    assert activity[1]["kind"] == _KIND_SUGGESTION
    assert activity[0]["created_at_iso"] >= activity[1]["created_at_iso"]
    assert activity[0]["agreement_id"] == agreement_id
    assert activity[1]["field"] == "paymentTermsDays"
    assert activity[1]["proposed_value"] == 45

    # A second namespace sees NO activity for this agreement (explicit scoping).
    other_namespace = await make_namespace()
    other_activity = await get_agreement_activity(pg_pool, other_namespace, agreement_id)
    assert other_activity == []


# ---------------------------------------------------------------------------
# 6. Missing required params raise ValueError
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_missing_required_params_raise_value_error(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    engine = _EngineStub(pg_pool)

    # create without namespace_id
    with pytest.raises(ValueError, match="namespace_id"):
        await do_create_agreement(engine, {"supplier_id": "912345678"})

    # suggest without field
    with pytest.raises(ValueError, match="field"):
        await do_suggest_revision(
            engine,
            {
                "namespace_id": str(namespace_id),
                "agreement_id": str(uuid.uuid4()),
                "proposed_value": 45,
            },
        )

    # comment without comment
    with pytest.raises(ValueError, match="comment"):
        await do_add_comment(
            engine,
            {
                "namespace_id": str(namespace_id),
                "agreement_id": str(uuid.uuid4()),
            },
        )


# ---------------------------------------------------------------------------
# 7. Ledger immutability discipline — source-level assertion
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_module_source_never_updates_or_deletes_ledger_rows() -> None:
    """authoring.py must contain no UPDATE/DELETE against v3_cognitive_ledger."""
    from nce.vertical_modules.agreements import authoring as authoring_module

    source = inspect.getsource(authoring_module)
    assert not re.search(r"(?i)\bUPDATE\s+v3_cognitive_ledger\b", source), (
        "authoring.py must never UPDATE ledger rows (append-only audit trail)"
    )
    assert not re.search(r"(?i)\bDELETE\s+FROM\s+v3_cognitive_ledger\b", source), (
        "authoring.py must never DELETE ledger rows (append-only audit trail)"
    )


# ---------------------------------------------------------------------------
# 8. Authored term VALUES are persisted to a searchable memory (TAG fix-forward)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_agreement_persists_terms_to_searchable_memory(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """The drafted term VALUES must be retrievable — kg_nodes term nodes are
    identity anchors with no value column, so create writes a text memory (like
    the OCR path). Without it the values are persisted nowhere (the TAG-blocking
    defect). Prove: a memory row exists for the agreement AND is full-text
    searchable by the supplier id and a term name."""
    await _seed_ownership(pg_pool, namespace_id)
    orgnr = "912345678"

    engine = _EngineStub(pg_pool)
    result = await do_create_agreement(
        engine,
        {
            "namespace_id": str(namespace_id),
            "supplier_id": orgnr,
            "terms": {"paymentTermsDays": 30, "frameDiscountPct": 2.5},
        },
    )

    memory_id = result["memory_id"]
    assert memory_id is not None, "authored terms were not persisted to a memory"

    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        # The memory row exists for THIS agreement in THIS namespace.
        row = await conn.fetchrow(
            """
            SELECT memory_type, payload_ref
            FROM   memories
            WHERE  id = $1::uuid AND namespace_id = $2::uuid
            """,
            memory_id,
            str(namespace_id),
        )
        assert row is not None, "memory row missing after create"
        assert row["memory_type"] == "episodic"

        # The drafted values are full-text searchable (answerable), not dropped.
        for token in (orgnr, "paymentTermsDays"):
            hit = await conn.fetchval(
                """
                SELECT count(*)
                FROM   memories
                WHERE  id = $1::uuid AND namespace_id = $2::uuid
                  AND  content_fts @@ plainto_tsquery('english', $3)
                """,
                memory_id,
                str(namespace_id),
                token,
            )
            assert hit == 1, f"authored memory not searchable by {token!r}"


# ---------------------------------------------------------------------------
# 9. Structured term store: create -> lookup -> (sign) -> reconcile (chip)
# ---------------------------------------------------------------------------


async def _review_row(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID, agreement_id: str) -> Any:
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        return await conn.fetchrow(
            """
            SELECT review_status, extracted
            FROM   agreement_review_queue
            WHERE  agreement_id = $1 AND namespace_id = $2::uuid
            """,
            uuid.UUID(agreement_id),
            str(namespace_id),
        )


async def _sign_agreement(
    pg_pool: asyncpg.Pool, namespace_id: uuid.UUID, agreement_id: str
) -> None:
    """Stand-in for W9 do_record_signature: promote the row to auto_green.

    (W9 performs this flip for real; here it isolates the create/gate/reconcile
    architecture without a cross-branch dependency on signing.py.)
    """
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        await conn.execute(
            """
            UPDATE agreement_review_queue
            SET    review_status = 'auto_green'
            WHERE  agreement_id = $1 AND namespace_id = $2::uuid
            """,
            uuid.UUID(agreement_id),
            str(namespace_id),
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_persists_terms_to_review_queue_as_draft(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Authored terms land in agreement_review_queue.extracted (the structured
    store the readers use) as a needs_review_yellow DRAFT."""
    await _seed_ownership(pg_pool, namespace_id)
    orgnr = "912345678"
    await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr}")

    engine = _EngineStub(pg_pool)
    result = await do_create_agreement(
        engine,
        {
            "namespace_id": str(namespace_id),
            "supplier_id": orgnr,
            "terms": {
                "paymentTermsDays": 30,
                "kickbackTiers": [{"threshold": 100_000, "pct": 3.0}],
            },
        },
    )
    assert result["review_status"] == _DRAFT_ROW_STATUS

    row = await _review_row(pg_pool, namespace_id, result["agreement_id"])
    assert row is not None, "authored agreement wrote no review-queue row"
    assert row["review_status"] == "needs_review_yellow"
    extracted = row["extracted"]
    if isinstance(extracted, str):
        extracted = json.loads(extracted)
    assert extracted["supplierId"]["value"] == orgnr
    assert extracted["paymentTermsDays"]["value"] == 30
    assert extracted["kickbackTiers"]["value"] == [{"threshold": 100_000, "pct": 3.0}]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_then_lookup_terms_sees_authored_agreement(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """agreements_lookup_terms (B109) returns an authored agreement + its terms
    immediately — it reads the same review-queue store create now writes.

    The namespace opt-in guard is isolated out (patched enabled) — it is a
    separate concern covered by B109's own suite; what this test proves is that
    the lookup QUERY sees an authored agreement (create writes where lookup
    reads)."""
    await _seed_ownership(pg_pool, namespace_id)
    orgnr = "912345678"
    await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr}")

    engine = _EngineStub(pg_pool)
    result = await do_create_agreement(
        engine,
        {
            "namespace_id": str(namespace_id),
            "supplier_id": orgnr,
            "terms": {"paymentTermsDays": 45},
        },
    )

    with patch(
        "nce.vertical_modules.agreements.mcp_handlers.require_agreements_enabled",
        new=AsyncMock(return_value=None),
    ):
        parsed = json.loads(
            await handle_agreements_lookup_terms(
                engine, {"namespace_id": str(namespace_id), "supplier": orgnr}
            )
        )
    assert parsed["status"] == "ok"
    match = next(
        (a for a in parsed["agreements"] if a["agreement_id"] == result["agreement_id"]), None
    )
    assert match is not None, "authored agreement invisible to lookup"
    assert match["terms"]["paymentTermsDays"]["value"] == 45


@pytest.mark.integration
@pytest.mark.asyncio
async def test_authored_draft_gated_until_signed_then_reconcilable(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """The load-bearing path: an authored DRAFT is NON-reconcilable (§9.3 gate)
    until signing promotes it to auto_green; then kickback reconciles it."""
    await _seed_ownership(pg_pool, namespace_id)
    orgnr = "912345678"
    await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr}")

    engine = _EngineStub(pg_pool)
    result = await do_create_agreement(
        engine,
        {
            "namespace_id": str(namespace_id),
            "supplier_id": orgnr,
            "terms": {"kickbackTiers": [{"threshold": 100_000, "pct": 3.0}]},
        },
    )
    agreement_id = result["agreement_id"]

    # Unsigned DRAFT: kickback's §9.3 gate refuses to reconcile, without any GL read.
    seam = AsyncMock(return_value=[])
    with patch(_KICKBACK_GL_SEAM, seam):
        gated = await do_reconcile_kickback(
            engine, {"namespace_id": str(namespace_id), "agreement_id": agreement_id}
        )
    assert gated["status"] == "unconfirmed_terms"
    assert gated["review_status"] == "needs_review_yellow"
    seam.assert_not_called()

    # Sign it (W9 promotes the row) — now reconcilable.
    await _sign_agreement(pg_pool, namespace_id, agreement_id)

    gl_rows = [
        {
            "supplier_name": "S",
            "supplier_id": orgnr,
            "amount_nok": 200_000.0,
            "gl_date": "2026-03-15",
        }
    ]
    with patch(_KICKBACK_GL_SEAM, AsyncMock(return_value=gl_rows)):
        rec = await do_reconcile_kickback(
            engine, {"namespace_id": str(namespace_id), "agreement_id": agreement_id}
        )
    assert rec["status"] == "ok"
    # 200_000 spend at the 100_000/3% tier -> earned 6_000.00.
    assert rec["earned_to_date_nok"] == 6_000.0


# ---------------------------------------------------------------------------
# 10. Re-authoring must never leave a row asserting "signed" over unsigned terms
# ---------------------------------------------------------------------------


async def _author(engine: Any, namespace_id: uuid.UUID, orgnr: str, terms: dict) -> str:
    res = await do_create_agreement(
        engine,
        {"namespace_id": str(namespace_id), "supplier_id": orgnr, "terms": terms},
    )
    return res["agreement_id"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reauthoring_signed_agreement_with_changed_terms_resets_to_yellow(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Changing the money terms of a SIGNED agreement must revoke its signed
    status — otherwise the row asserts a human signed terms they never saw.

    `extracted` is the blob compliance.py / kickback.py trust as signed ground
    truth, so rewriting it while review_status stays auto_green would let
    unsigned numbers authorise money.
    """
    await _seed_ownership(pg_pool, namespace_id)
    orgnr = "912345678"
    await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr}")
    engine = _EngineStub(pg_pool)

    agreement_id = await _author(
        engine, namespace_id, orgnr, {"kickbackTiers": [{"threshold": 100_000, "pct": 3.0}]}
    )
    await _sign_agreement(pg_pool, namespace_id, agreement_id)
    assert (await _review_row(pg_pool, namespace_id, agreement_id))["review_status"] == "auto_green"

    # Re-author the SAME agreement with a materially better kickback rate.
    await do_create_agreement(
        engine,
        {
            "namespace_id": str(namespace_id),
            "supplier_id": orgnr,
            "agreement_id": agreement_id,
            "terms": {"kickbackTiers": [{"threshold": 100_000, "pct": 25.0}]},
        },
    )

    row = await _review_row(pg_pool, namespace_id, agreement_id)
    extracted = row["extracted"]
    if isinstance(extracted, str):
        extracted = json.loads(extracted)
    assert extracted["kickbackTiers"]["value"] == [{"threshold": 100_000, "pct": 25.0}], (
        "re-author should still update the terms"
    )
    assert row["review_status"] == "needs_review_yellow", (
        "changed terms on a signed agreement must revoke auto_green — otherwise "
        "unsigned money terms remain reconcilable"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reauthoring_identical_terms_preserves_signature(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """An idempotent re-author (same terms) must NOT invalidate a real signature."""
    await _seed_ownership(pg_pool, namespace_id)
    orgnr = "912345678"
    await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr}")
    engine = _EngineStub(pg_pool)

    terms = {"kickbackTiers": [{"threshold": 100_000, "pct": 3.0}]}
    agreement_id = await _author(engine, namespace_id, orgnr, terms)
    await _sign_agreement(pg_pool, namespace_id, agreement_id)

    await do_create_agreement(
        engine,
        {
            "namespace_id": str(namespace_id),
            "supplier_id": orgnr,
            "agreement_id": agreement_id,
            "terms": terms,
        },
    )

    row = await _review_row(pg_pool, namespace_id, agreement_id)
    assert row["review_status"] == "auto_green", (
        "an identical re-author must not revoke a signature"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reauthoring_never_lifts_a_human_veto(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """manual_red is a human rejection; re-authoring must not upgrade it to
    yellow (which would put a vetoed agreement back into the review flow)."""
    await _seed_ownership(pg_pool, namespace_id)
    orgnr = "912345678"
    await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr}")
    engine = _EngineStub(pg_pool)

    agreement_id = await _author(
        engine, namespace_id, orgnr, {"kickbackTiers": [{"threshold": 100_000, "pct": 3.0}]}
    )
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        await conn.execute(
            """
            UPDATE agreement_review_queue
            SET    review_status = 'manual_red'
            WHERE  agreement_id = $1 AND namespace_id = $2::uuid
            """,
            uuid.UUID(agreement_id),
            str(namespace_id),
        )

    await do_create_agreement(
        engine,
        {
            "namespace_id": str(namespace_id),
            "supplier_id": orgnr,
            "agreement_id": agreement_id,
            "terms": {"kickbackTiers": [{"threshold": 100_000, "pct": 9.0}]},
        },
    )

    row = await _review_row(pg_pool, namespace_id, agreement_id)
    assert row["review_status"] == "manual_red", "re-authoring must not lift a human veto"
