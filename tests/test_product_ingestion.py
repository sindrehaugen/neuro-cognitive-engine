"""
tests/test_product_ingestion.py
================================
Integration tests for ``nce.vertical_modules.product.ingestion.do_ingest_spec``.

Verifies:
- A ``memories`` row is written with a non-null embedding and a populated
  ``content_fts`` tsvector.
- A matching ``v3_cognitive_ledger`` row is written for the same ``memory_id``.
- All rows are namespace-scoped (RLS isolation: namespace B cannot see A's rows).
- Graceful degradation: when embeddings are unavailable the row is still written
  and the ledger records ``degraded_embedding=true``.

All tests are ``@pytest.mark.integration`` (require a live Postgres via
``NCE_INTEGRATION_PG_DSN`` / ``PG_DSN`` / ``DATABASE_URL``).
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import patch

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from nce.auth import set_namespace_context
from nce.vertical_modules.product.ingestion import do_ingest_spec

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def ns_a(make_namespace) -> uuid.UUID:
    """Fresh namespace for the primary test tenant."""
    return await make_namespace()


@pytest_asyncio.fixture
async def ns_b(make_namespace) -> uuid.UUID:
    """Second namespace — used to verify RLS isolation."""
    return await make_namespace()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAMPLE_SPEC = (
    "Cisco Catalyst 9300 Series Switch: 48-port PoE+, "
    "stackable, 1GbE/10GbE uplinks, IOS-XE 17.x, "
    "switching capacity 592 Gbps, latency <1us, "
    "operating temperature 0-40°C, 1U form factor."
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestDoIngestSpec:
    """Integration suite for ``do_ingest_spec``."""

    async def test_memories_row_written(
        self, pg_pool: asyncpg.Pool, pg_admin_conn: asyncpg.Connection, ns_a: uuid.UUID
    ) -> None:
        """Ingesting spec text writes exactly one memories row, namespace-scoped."""
        product_id = f"sku-{uuid.uuid4().hex[:8]}"

        result = await do_ingest_spec(
            pg_pool,
            ns_a,
            product_id=product_id,
            spec_text=_SAMPLE_SPEC,
        )

        assert "memory_id" in result, f"Expected memory_id in result, got: {result}"
        memory_id = result["memory_id"]

        row = await pg_admin_conn.fetchrow(
            "SELECT id, namespace_id, embedding, content_fts, payload_ref, agent_id "
            "FROM memories WHERE id = $1::uuid",
            memory_id,
        )
        assert row is not None, "memories row not found"
        assert str(row["namespace_id"]) == str(ns_a), "namespace_id mismatch"
        assert row["embedding"] is not None, "embedding must be non-null"
        assert row["content_fts"] is not None, "content_fts must be non-null"
        # payload_ref is the first 24 hex chars of the memory UUID (satisfies 24-char ObjectId constraint).
        assert len(row["payload_ref"]) == 24, "payload_ref must be 24-char hex"
        assert row["payload_ref"] == memory_id.replace("-", "")[:24], (
            "payload_ref must derive from memory_id"
        )
        assert row["agent_id"] == "product-spec-ingest"

    async def test_cognitive_ledger_entry_written(
        self, pg_pool: asyncpg.Pool, pg_admin_conn: asyncpg.Connection, ns_a: uuid.UUID
    ) -> None:
        """Each ingest produces a matching v3_cognitive_ledger entry."""
        product_id = f"sku-{uuid.uuid4().hex[:8]}"

        result = await do_ingest_spec(
            pg_pool,
            ns_a,
            product_id=product_id,
            spec_text=_SAMPLE_SPEC,
            source="datasheet",
            trigger="webhook",
        )
        memory_id = result["memory_id"]

        ledger_row = await pg_admin_conn.fetchrow(
            "SELECT memory_id, namespace_id, tlx_scores FROM v3_cognitive_ledger "
            "WHERE memory_id = $1::uuid",
            memory_id,
        )
        assert ledger_row is not None, "v3_cognitive_ledger row not found"
        assert str(ledger_row["namespace_id"]) == str(ns_a)

        tlx = json.loads(ledger_row["tlx_scores"])
        assert tlx.get("source") == "datasheet"
        assert tlx.get("trigger") == "webhook"
        assert tlx.get("product_id") == product_id

    async def test_rls_namespace_isolation(
        self,
        pg_pool: asyncpg.Pool,
        pg_app_conn: asyncpg.Connection,
        ns_a: uuid.UUID,
        ns_b: uuid.UUID,
    ) -> None:
        """Namespace B cannot see memories written into namespace A."""
        product_id = f"sku-{uuid.uuid4().hex[:8]}"

        result = await do_ingest_spec(
            pg_pool,
            ns_a,
            product_id=product_id,
            spec_text=_SAMPLE_SPEC,
        )
        memory_id = result["memory_id"]

        # Querying as namespace B must return no row (RLS isolation).
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns_b)
            row = await pg_app_conn.fetchrow(
                "SELECT id FROM memories WHERE id = $1::uuid",
                memory_id,
            )
        assert row is None, "RLS isolation violated: namespace B can see namespace A's memory"

    async def test_empty_spec_text_returns_skipped(
        self, pg_pool: asyncpg.Pool, ns_a: uuid.UUID
    ) -> None:
        """Empty or whitespace-only spec text is skipped gracefully."""
        result = await do_ingest_spec(
            pg_pool,
            ns_a,
            product_id="sku-empty",
            spec_text="   ",
        )
        assert result.get("skipped") == "empty spec_text"

    async def test_degraded_embedding_still_writes_row(
        self, pg_pool: asyncpg.Pool, pg_admin_conn: asyncpg.Connection, ns_a: uuid.UUID
    ) -> None:
        """When the embedding backend is degraded, the memories + ledger rows are still written."""
        product_id = f"sku-{uuid.uuid4().hex[:8]}"

        # Simulate a degraded embedding backend: embed_batch returns a deterministic
        # stub vector (non-empty) and degraded_embedding_flag is True.
        stub_dim = 768
        stub_vector = [0.0] * stub_dim

        async def _stubbed_embed_batch(texts: list[str]) -> list[list[float]]:
            return [list(stub_vector) for _ in texts]

        with (
            patch("nce.embeddings.embed_batch", side_effect=_stubbed_embed_batch),
            patch("nce.embeddings.degraded_embedding_flag") as mock_flag,
        ):
            mock_flag.get.return_value = True

            result = await do_ingest_spec(
                pg_pool,
                ns_a,
                product_id=product_id,
                spec_text=_SAMPLE_SPEC,
            )

        assert "memory_id" in result, f"Expected memory_id even in degraded mode: {result}"
        assert result["degraded"] is True

        memory_id = result["memory_id"]

        row = await pg_admin_conn.fetchrow(
            "SELECT embedding, metadata FROM memories WHERE id = $1::uuid",
            memory_id,
        )
        assert row is not None, "memories row not written in degraded mode"
        assert row["embedding"] is not None, "embedding must be non-null even when degraded"

        meta = json.loads(row["metadata"])
        assert meta.get("degraded_embedding") is True, "metadata must record degraded_embedding"

        ledger_row = await pg_admin_conn.fetchrow(
            "SELECT tlx_scores FROM v3_cognitive_ledger WHERE memory_id = $1::uuid",
            memory_id,
        )
        assert ledger_row is not None, "v3_cognitive_ledger row not written in degraded mode"
        tlx = json.loads(ledger_row["tlx_scores"])
        assert tlx.get("degraded_embedding") is True, "ledger must record degraded_embedding"
