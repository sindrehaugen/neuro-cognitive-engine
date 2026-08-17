"""
nce/vertical_modules/product/ingestion.py
==========================================
Semantic Track: product spec / datasheet text → NCE memory + cognitive-recall ledger.

``do_ingest_spec`` is the single public entry-point.  It:

  1. Calls ``nce.embeddings.embed_batch`` (public contract; same vector space
     as ``semantic_search``) to produce a search embedding for the spec text.
  2. INSERTs a ``memories`` row with ``embedding`` (pgvector) + ``content_fts``
     (tsvector for lexical search), namespace-scoped via ``scoped_pg_session``.
  3. INSERTs a ``v3_cognitive_ledger`` row per ingest (source + trigger metadata).

Graceful degradation:
  When the embedding backend is unavailable (``embed_batch`` returns fallback
  hash-stub vectors), ``degraded_embedding_flag`` is ``True``.  The ``memories``
  row is still written (with the stub vector so pgvector stays non-null) and the
  ledger entry records ``"degraded_embedding": true`` in ``tlx_scores``.
  The row is therefore always written — lexical FTS works regardless.

Constraints respected:
  - No cost / margin / BID data stored or logged (ADR-0017).
  - All writes are inside ``scoped_pg_session`` (RLS enforced).
  - No UPDATE/DELETE on ``event_log``.
  - No external I/O (embedding call) inside the pg transaction (per db_utils warning).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import asyncpg  # type: ignore[import-untyped]

log = logging.getLogger("nce.vertical_modules.product.ingestion")

# Agent label written to memories.agent_id for spec ingest rows.
_AGENT_ID = "product-spec-ingest"


async def do_ingest_spec(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    *,
    product_id: str,
    spec_text: str,
    source: str = "product_spec",
    trigger: str = "manual",
) -> dict[str, Any]:
    """Ingest raw spec / datasheet text for a product into the cognitive-recall substrate.

    Parameters
    ----------
    pg_pool:
        asyncpg connection pool.  RLS context is set inside ``scoped_pg_session``.
    namespace_id:
        Tenant namespace UUID — all writes are scoped to this namespace.
    product_id:
        Identifier of the product whose spec text is being ingested (stored in
        ``memories.payload_ref`` and in the ledger metadata).
    spec_text:
        Raw spec / datasheet text.  Truncated at 4 000 characters for
        ``content_fts`` and at the configured embedding character limit for
        the embedding call.
    source:
        Where the spec text came from (e.g. ``"product_spec"``, ``"datasheet"``).
        Recorded in ``v3_cognitive_ledger.tlx_scores`` for audit.
    trigger:
        What caused this ingest (e.g. ``"manual"``, ``"webhook"``, ``"sync"``).
        Recorded alongside ``source`` in the ledger.

    Returns
    -------
    dict with ``memory_id`` (str UUID) and ``degraded`` (bool).
    """
    if not spec_text or not spec_text.strip():
        log.warning("[PRODUCT-INGEST] skipped empty spec_text for product_id=%s", product_id)
        return {"skipped": "empty spec_text"}

    # ------------------------------------------------------------------
    # 1. Embed — outside the pg transaction (slow I/O must not hold a lock)
    # ------------------------------------------------------------------
    from nce import embeddings as _embeddings

    vectors = await _embeddings.embed_batch([spec_text])
    vector: list[float] = vectors[0] if vectors else []
    degraded: bool = _embeddings.degraded_embedding_flag.get()

    # ------------------------------------------------------------------
    # 2. INSERT memories + 3. INSERT v3_cognitive_ledger
    #    Both writes inside one scoped session so they share the transaction.
    # ------------------------------------------------------------------
    from nce.db_utils import scoped_pg_session

    memory_id = uuid.uuid4()
    vector_str = f"[{','.join(str(v) for v in vector)}]" if vector else None

    # Metadata stored on the memories row for provenance.
    row_metadata: dict[str, Any] = {
        "product_id": product_id,
        "source": source,
        "trigger": trigger,
    }
    if degraded:
        row_metadata["degraded_embedding"] = True

    # payload_ref has a CHECK constraint requiring a 24-char hex MongoDB ObjectId.
    # Product spec ingest has no Mongo document; derive a stable 24-char ref from
    # the memory UUID so the constraint is satisfied and the value is traceable.
    payload_ref = memory_id.hex[:24]

    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        await conn.execute(
            """
            INSERT INTO memories (
                id, namespace_id, agent_id, content_fts,
                payload_ref, memory_type, assertion_type,
                embedding, pii_redacted, metadata
            ) VALUES (
                $1::uuid, $2::uuid, $3, to_tsvector('english', $4),
                $5, $6, $7, $8::vector, $9, $10::jsonb
            )
            """,
            str(memory_id),
            str(namespace_id),
            _AGENT_ID,
            spec_text[:4000],
            payload_ref,
            "episodic",
            "observation",
            vector_str,
            False,
            json.dumps(row_metadata),
        )

        await conn.execute(
            """
            INSERT INTO v3_cognitive_ledger (
                memory_id, namespace_id, empathic_tensor,
                tlx_scores, vad_scores, model_version
            ) VALUES (
                $1::uuid, $2::uuid, $3::float[], $4::jsonb, $5::jsonb, $6
            )
            """,
            str(memory_id),
            str(namespace_id),
            [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            json.dumps(
                {
                    "source": source,
                    "trigger": trigger,
                    "product_id": product_id,
                    "degraded_embedding": degraded,
                }
            ),
            json.dumps({}),
            "1.0",
        )

    log.info(
        "[PRODUCT-INGEST] spec ingested product_id=%s memory_id=%s degraded=%s",
        product_id,
        memory_id,
        degraded,
    )
    return {"memory_id": str(memory_id), "degraded": degraded}
