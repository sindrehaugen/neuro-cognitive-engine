"""
tests/test_system_design_propose.py
=====================================
Integration tests for Module 6 Wave 3 — ``do_propose_design``
(System Design similarity-recall BOM proposal).

Validates:
  1. ``do_propose_design`` returns a non-empty list of proposed lines, each
     with ``validated == False``.
  2. Recall evidence is ordered by similarity (closest first) — with the
     dormant outcome-weighting flag OFF, ranking is pure similarity.
  3. No line is ever auto-accepted / validated=True.
  4. ``outcome_weighting_applied`` is False when the dormant flag is OFF.

Seeding strategy (deterministic similarity):
  The embedding backend may be a non-semantic fallback in tests, so we do NOT
  rely on "similar text → near vector".  Instead:
    - Compute the query vector via ``embed(brief)``.
    - Insert one DESIGN memory whose ``embedding`` equals the query vector
      (distance ~0 → top hit).
    - Insert one PROJECT memory with a clearly different vector (larger distance
      → lower rank).
  Both memories carry a usable ``product_ref`` and ``qty`` in their ``metadata``
  JSONB so ``_build_proposed_lines`` emits a line for each.

Namespace creation uses ``make_namespace`` (which uses ``pg_pool``, the owner
role via PG_DSN) — the app role gets permission-denied on ``namespaces``.
Memory rows are seeded via ``scoped_pg_session(pg_pool, ns)`` inside the same
owner pool.

Runs as @pytest.mark.integration — requires a live Postgres with schema.sql
and all migrations applied.  Run ``scratch/_apply_probe_b032.py`` first if
tables are missing.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from nce.config import cfg
from nce.db_utils import scoped_pg_session
from nce.embeddings import embed
from nce.vertical_modules.system_design.propose import do_propose_design

# ---------------------------------------------------------------------------
# Stub engine (mirrors _EngineStub pattern from other integration tests)
# ---------------------------------------------------------------------------


class _EngineStub:
    """Minimal engine stub that exposes ``pg_pool`` for ``do_propose_design``."""

    def __init__(self, pg_pool: Any) -> None:
        self.pg_pool = pg_pool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MOCK_EMIT = "nce.vertical_modules.system_design.graph.emit_graph_write"


def _far_vec(dim: int = 768) -> list[float]:
    """A vector that is clearly different from any real embedding.

    All components are 0 except the last, which is 1.0 — this vector is
    orthogonal to any L2-normalised embedding that has non-zero first
    components (cosine distance ≈ 1.0 from most real embeddings).
    """
    v = [0.0] * dim
    v[-1] = 1.0
    return v


async def _seed_memory(
    conn: Any,
    *,
    ns_id: uuid.UUID,
    embedding: list[float],
    node_type: str,
    product_ref: str,
    qty: int = 1,
    name: str | None = None,
) -> uuid.UUID:
    """Insert one memory row into the ``memories`` table.

    Uses ``$n::vector`` cast (pgvector accepts JSON array string).
    Returns the generated memory UUID.
    """
    mem_id = uuid.uuid4()
    # payload_ref must be a 24-char lowercase hex string (MongoDB ObjectId format).
    payload_ref = mem_id.hex[:24]
    metadata = json.dumps({"product_ref": product_ref, "qty": qty})
    await conn.execute(
        """
        INSERT INTO memories
            (id, namespace_id, agent_id, embedding, assertion_type,
             memory_type, payload_ref, metadata, node_type, name,
             change_origin)
        VALUES ($1, $2::uuid, $3, $4::vector, 'fact', 'episodic',
                $5, $6::jsonb, $7, $8, 'sync')
        """,
        mem_id,
        str(ns_id),
        "test-propose-agent",
        json.dumps(embedding),
        payload_ref,
        metadata,
        node_type,
        name,
    )
    return mem_id


# ---------------------------------------------------------------------------
# Integration test suite
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestSystemDesignPropose:
    """Integration tests for system_design/propose.py Wave 3."""

    # ------------------------------------------------------------------
    # 1. Non-empty result; all lines have validated=False
    # ------------------------------------------------------------------

    async def test_propose_returns_nonempty_lines_with_validated_false(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """do_propose_design returns >=1 proposed line, each with validated=False."""
        ns_id: uuid.UUID = await make_namespace()
        engine = _EngineStub(pg_pool)
        brief = "conference room AV system with ceiling mic and DSP"

        # Compute the query vector once so we can seed an exact-match memory.
        query_vec = await embed(brief)

        async with scoped_pg_session(pg_pool, ns_id) as conn:
            # Exact-match: embed equals query_vec → distance ~0
            await _seed_memory(
                conn,
                ns_id=ns_id,
                embedding=query_vec,
                node_type="DESIGN",
                product_ref="Biamp:TesiraFORTE-CI",
                qty=1,
                name="conf-room-dsp",
            )
            # Far: orthogonal vector → larger distance
            await _seed_memory(
                conn,
                ns_id=ns_id,
                embedding=_far_vec(),
                node_type="PROJECT",
                product_ref="Shure:MXCW640",
                qty=4,
                name="far-project",
            )

        result = await do_propose_design(
            engine,
            {"namespace_id": str(ns_id), "room_brief": brief},
        )

        lines = result["proposed_lines"]
        assert isinstance(lines, list), "proposed_lines must be a list"
        assert len(lines) >= 1, "Expected at least one proposed line"
        for line in lines:
            assert line["validated"] is False, (
                f"proposed line must have validated=False; got {line['validated']!r}"
            )

    # ------------------------------------------------------------------
    # 2. Recall evidence ordered by similarity (exact-match ranks first)
    # ------------------------------------------------------------------

    async def test_recall_evidence_ordered_closest_first(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """Recall evidence is ordered by similarity descending (closest first)."""
        ns_id: uuid.UUID = await make_namespace()
        engine = _EngineStub(pg_pool)
        brief = "board room display and audio"

        query_vec = await embed(brief)

        async with scoped_pg_session(pg_pool, ns_id) as conn:
            await _seed_memory(
                conn,
                ns_id=ns_id,
                embedding=query_vec,
                node_type="DESIGN",
                product_ref="Samsung:QM75R",
                qty=1,
                name="exact-display",
            )
            await _seed_memory(
                conn,
                ns_id=ns_id,
                embedding=_far_vec(),
                node_type="PROJECT",
                product_ref="Crestron:DM-NVX-360",
                qty=2,
                name="far-av",
            )

        result = await do_propose_design(
            engine,
            {"namespace_id": str(ns_id), "room_brief": brief},
        )

        evidence = result["recall_evidence"]
        assert len(evidence) >= 2, "Expected at least 2 recall evidence rows"

        # Similarity is descending (closest first).
        sims = [e["similarity"] for e in evidence]
        assert sims == sorted(sims, reverse=True), (
            f"recall_evidence not ordered by similarity desc: {sims}"
        )

        # The exact-match seed (distance ~0, similarity ~1) must rank above the far seed.
        assert evidence[0]["similarity"] > evidence[-1]["similarity"], (
            "Exact-match memory must rank above far-vector memory"
        )

    # ------------------------------------------------------------------
    # 3. No line is ever auto-accepted / validated=True
    # ------------------------------------------------------------------

    async def test_no_line_is_validated_true(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """All proposed lines have validated=False; no auto-accept invariant."""
        ns_id: uuid.UUID = await make_namespace()
        engine = _EngineStub(pg_pool)
        brief = "training room with wireless presentation"

        query_vec = await embed(brief)

        async with scoped_pg_session(pg_pool, ns_id) as conn:
            for i in range(3):
                await _seed_memory(
                    conn,
                    ns_id=ns_id,
                    embedding=query_vec,
                    node_type="DESIGN",
                    product_ref=f"Barco:CS-100-{i}",
                    qty=i + 1,
                )

        result = await do_propose_design(
            engine,
            {"namespace_id": str(ns_id), "room_brief": brief},
        )

        for line in result["proposed_lines"]:
            assert line["validated"] is False, (
                "PROPOSE-ONLY invariant violated: validated must always be False"
            )
        # Outcome-weighting dormant flag is OFF → outcome_weighting_applied=False.
        assert result["outcome_weighting_applied"] is False, (
            "outcome_weighting_applied must be False when the dormant flag is OFF"
        )

    # ------------------------------------------------------------------
    # 4. outcome_weighting_applied is False when dormant flag is OFF
    # ------------------------------------------------------------------

    async def test_outcome_weighting_applied_false_when_dormant(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """outcome_weighting_applied=False and order=pure-similarity when flag is OFF."""
        ns_id: uuid.UUID = await make_namespace()
        engine = _EngineStub(pg_pool)
        brief = "lobby background music and paging"

        # Ensure the dormant flag is OFF (it defaults to False; be explicit).
        monkeypatch.setattr(cfg, "NCE_SYSTEM_DESIGN_OUTCOME_WEIGHTING_ENABLED", False)

        query_vec = await embed(brief)

        async with scoped_pg_session(pg_pool, ns_id) as conn:
            await _seed_memory(
                conn,
                ns_id=ns_id,
                embedding=query_vec,
                node_type="DESIGN",
                product_ref="QSC:AD-C4T",
                qty=8,
                name="close-speaker",
            )
            await _seed_memory(
                conn,
                ns_id=ns_id,
                embedding=_far_vec(),
                node_type="PROJECT",
                product_ref="Crown:CDi-4|1200",
                qty=1,
                name="far-amp",
            )

        result = await do_propose_design(
            engine,
            {"namespace_id": str(ns_id), "room_brief": brief},
        )

        assert result["outcome_weighting_applied"] is False
        evidence = result["recall_evidence"]
        sims = [e["similarity"] for e in evidence]
        assert sims == sorted(sims, reverse=True), "Pure-similarity order violated"

    # ------------------------------------------------------------------
    # 5. Memories without product_ref in metadata are skipped gracefully
    # ------------------------------------------------------------------

    async def test_memories_without_product_ref_skipped(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """Memories with no product_ref in metadata are skipped; no crash."""
        ns_id: uuid.UUID = await make_namespace()
        engine = _EngineStub(pg_pool)
        brief = "video conferencing room"

        query_vec = await embed(brief)

        async with scoped_pg_session(pg_pool, ns_id) as conn:
            # This one has product_ref → should produce a proposed line.
            await _seed_memory(
                conn,
                ns_id=ns_id,
                embedding=query_vec,
                node_type="DESIGN",
                product_ref="Poly:Studio-E70",
                qty=1,
            )
            # This one has NO product_ref in metadata → must be skipped.
            mem_id = uuid.uuid4()
            await conn.execute(
                """
                INSERT INTO memories
                    (id, namespace_id, agent_id, embedding, assertion_type,
                     memory_type, payload_ref, metadata, node_type, change_origin)
                VALUES ($1, $2::uuid, $3, $4::vector, 'fact', 'episodic',
                        $5, $6::jsonb, $7, 'sync')
                """,
                mem_id,
                str(ns_id),
                "test-propose-agent",
                json.dumps(query_vec),
                mem_id.hex[:24],  # 24-char hex (ObjectId format)
                json.dumps({"note": "no product_ref here"}),
                "DESIGN",
            )

        result = await do_propose_design(
            engine,
            {"namespace_id": str(ns_id), "room_brief": brief},
        )

        # At least the valid seed landed.
        lines = result["proposed_lines"]
        assert any("Poly:Studio-E70" in ln["product_ref"] for ln in lines), (
            "Expected Poly:Studio-E70 in proposed lines"
        )

        # All returned lines still have validated=False.
        for line in lines:
            assert line["validated"] is False
