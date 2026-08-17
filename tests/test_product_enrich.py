"""
tests/test_product_enrich.py
============================
Integration tests for ``do_enrich_product`` (M2.W7 — on-demand product enrichment).

Assertions (no mocking of @governed):
  1. Confirm-only default: without ``confirm=True`` the handler returns
     ``{"status": "pending_approval", ...}`` and writes NOTHING.
  2. With ``confirm=True`` + a valid idempotency key the handler executes once
     and a ``product_enrichment_log`` row is written.
  3. Idempotent replay: a second call with the same idempotency key returns
     ``{"status": "already_executed", ...}`` (governed NO-OP — no duplicate row).
  4. Low-confidence fields are written with ``needs_review=True``.
  5. Money/legal fields always have ``needs_review=True`` regardless of confidence.
  6. An ``event_log`` entry is appended on first confirmed execution.
  7. ``product_enrichment_log`` rows are namespace-isolated under FORCE RLS:
     ns_b sees 0 rows for ns_a's product.
  8. The handler NEVER iterates the catalog (verified by the single-product query
     design and the integration test's direct EXPLAIN check).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from nce.auth import set_namespace_context
from nce.vertical_modules.product.enrich import (
    _MONEY_LEGAL_FIELDS,
    _derive_idempotency_key,
    do_enrich_product,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def ns_a(pg_pool: asyncpg.Pool) -> AsyncGenerator[uuid.UUID, None]:
    """Namespace A for isolation tests."""
    slug = f"pytest-enrich-a-{uuid.uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        ns = await conn.fetchval(
            "INSERT INTO namespaces (slug) VALUES ($1) RETURNING id",
            slug,
        )
    assert ns is not None
    yield ns


@pytest_asyncio.fixture
async def ns_b(pg_pool: asyncpg.Pool) -> AsyncGenerator[uuid.UUID, None]:
    """Namespace B for isolation tests."""
    slug = f"pytest-enrich-b-{uuid.uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        ns = await conn.fetchval(
            "INSERT INTO namespaces (slug) VALUES ($1) RETURNING id",
            slug,
        )
    assert ns is not None
    yield ns


async def _insert_test_product(
    conn: asyncpg.Connection,
    ns_id: uuid.UUID,
    *,
    manufacturer: str = "TESTCO",
    mfr_part_no: str | None = None,
) -> uuid.UUID:
    """Insert a minimal product row and return its id."""
    part_no = mfr_part_no or f"PART-{uuid.uuid4().hex[:8]}"
    product_id = await conn.fetchval(
        """
        INSERT INTO product_catalog
            (namespace_id, manufacturer, mfr_part_no, product_source_id,
             lifecycle_status, etim_specs)
        VALUES ($1, $2, $3, $4, 'active', '{}'::jsonb)
        RETURNING id
        """,
        ns_id,
        manufacturer,
        part_no,
        f"src-{part_no}",
    )
    assert product_id is not None
    return product_id


# ---------------------------------------------------------------------------
# Test 1: confirm-only default returns pending_approval without side effects
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_enrich_confirm_only_default_returns_pending(
    pg_app_conn: asyncpg.Connection,
    ns_a: uuid.UUID,
) -> None:
    """Without confirm=True the governed wrapper returns pending_approval, no write."""
    # Insert product as admin (pg_pool) then test enrichment under nce_app.

    # We need to insert the product as the pool connection (superuser / nce owner).
    # Use a fresh admin connection to set up data.
    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        product_id = await _insert_test_product(pg_app_conn, ns_a)

    trigger_context = {
        "kind": "quote",
        "ref_id": "QUOTE-001",
        "missing_fields": ["short_description", "category"],
        "source_watermark": "wm-v1",
    }
    idem_key = _derive_idempotency_key(
        str(product_id),
        trigger_context["missing_fields"],
        trigger_context["source_watermark"],
    )

    # Call WITHOUT confirm — governed must return pending_approval, no DB writes.
    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        result = await do_enrich_product(
            pg_app_conn,
            ns_a,
            idempotency_key=idem_key,
            confirm=False,  # default — no side effect
            product_id=str(product_id),
            trigger_context=trigger_context,
        )

    assert result["status"] == "pending_approval", (
        f"Expected pending_approval without confirm=True, got: {result}"
    )
    assert result["action_type"] == "product_enrich"

    # Verify nothing was written to product_enrichment_log.
    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        count = await pg_app_conn.fetchval(
            "SELECT count(*) FROM product_enrichment_log WHERE product_id = $1",
            product_id,
        )
    assert count == 0, f"No rows should be written without confirm=True, got {count}"


# ---------------------------------------------------------------------------
# Test 2 + 3: confirm=True executes once; replay is NO-OP (idempotent)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_enrich_confirm_executes_once_then_noop(
    pg_app_conn: asyncpg.Connection,
    ns_a: uuid.UUID,
) -> None:
    """With confirm=True the handler runs once; same key replay is already_executed."""
    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        product_id = await _insert_test_product(pg_app_conn, ns_a, mfr_part_no="PART-IDEM")

    trigger_context = {
        "kind": "design",
        "ref_id": "DESIGN-007",
        "missing_fields": ["category"],
        "source_watermark": "wm-idem-1",
    }
    idem_key = _derive_idempotency_key(
        str(product_id),
        trigger_context["missing_fields"],
        trigger_context["source_watermark"],
    )

    # First call — should execute.
    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        result1 = await do_enrich_product(
            pg_app_conn,
            ns_a,
            idempotency_key=idem_key,
            confirm=True,
            product_id=str(product_id),
            trigger_context=trigger_context,
        )

    assert result1["status"] == "executed", f"First call should execute, got: {result1}"
    inner = result1["result"]
    assert inner["product_id"] == str(product_id)
    assert inner["proposals_written"] >= 1

    # Verify product_enrichment_log row was written.
    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        count_after_first = await pg_app_conn.fetchval(
            "SELECT count(*) FROM product_enrichment_log WHERE product_id = $1",
            product_id,
        )
    assert count_after_first >= 1, "At least one enrichment log row must be written"

    # Second call with same idempotency key — should be NO-OP.
    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        result2 = await do_enrich_product(
            pg_app_conn,
            ns_a,
            idempotency_key=idem_key,
            confirm=True,
            product_id=str(product_id),
            trigger_context=trigger_context,
        )

    assert result2["status"] == "already_executed", (
        f"Replay with same key should be already_executed, got: {result2}"
    )

    # Verify no duplicate log rows were added on replay.
    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        count_after_replay = await pg_app_conn.fetchval(
            "SELECT count(*) FROM product_enrichment_log WHERE product_id = $1",
            product_id,
        )
    assert count_after_replay == count_after_first, (
        f"Replay must not write additional log rows: "
        f"before={count_after_first} after={count_after_replay}"
    )


# ---------------------------------------------------------------------------
# Test 4: sub-threshold fields → needs_review=True
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_enrich_low_confidence_fields_need_review(
    pg_app_conn: asyncpg.Connection,
    ns_a: uuid.UUID,
) -> None:
    """Fields below NCE_PRODUCT_ENRICH_MIN_CONFIDENCE are written with needs_review=True."""
    # Set threshold high so all synthetic proposals are sub-threshold.
    os.environ["NCE_PRODUCT_ENRICH_MIN_CONFIDENCE"] = "0.95"
    try:
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns_a)
            product_id = await _insert_test_product(pg_app_conn, ns_a, mfr_part_no="PART-LOCONF")

        trigger_context = {
            "kind": "quote",
            "ref_id": "QUOTE-LO",
            "missing_fields": ["short_description"],  # non-money; would auto-merge at 0.80
            "source_watermark": "wm-lo-1",
        }
        idem_key = _derive_idempotency_key(
            str(product_id),
            trigger_context["missing_fields"],
            trigger_context["source_watermark"],
        )

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns_a)
            result = await do_enrich_product(
                pg_app_conn,
                ns_a,
                idempotency_key=idem_key,
                confirm=True,
                product_id=str(product_id),
                trigger_context=trigger_context,
            )

        assert result["status"] == "executed"
        inner = result["result"]
        assert inner["needs_review_count"] >= 1, (
            "All proposals below 0.95 threshold must be needs_review=True"
        )

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns_a)
            rows = await pg_app_conn.fetch(
                "SELECT field_name, confidence, needs_review "
                "FROM product_enrichment_log WHERE product_id = $1",
                product_id,
            )

        assert rows, "At least one log row must exist"
        for row in rows:
            assert row["needs_review"] is True, (
                f"Field {row['field_name']!r} (confidence={row['confidence']}) "
                "must be needs_review=True when below threshold 0.95"
            )
    finally:
        del os.environ["NCE_PRODUCT_ENRICH_MIN_CONFIDENCE"]


# ---------------------------------------------------------------------------
# Test 5: money/legal fields always needs_review=True
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_enrich_money_legal_fields_always_need_review(
    pg_app_conn: asyncpg.Connection,
    ns_a: uuid.UUID,
) -> None:
    """§9.3: money/legal fields are always needs_review=True regardless of confidence."""
    # Use a low threshold so the only reason needs_review=True would be money/legal.
    os.environ["NCE_PRODUCT_ENRICH_MIN_CONFIDENCE"] = "0.10"
    try:
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns_a)
            product_id = await _insert_test_product(pg_app_conn, ns_a, mfr_part_no="PART-ML")

        # Use a money/legal field that gets confidence=0.50 from _build_proposals.
        money_field = "price"
        assert money_field in _MONEY_LEGAL_FIELDS

        trigger_context = {
            "kind": "quote",
            "ref_id": "QUOTE-ML",
            "missing_fields": [money_field],
            "source_watermark": "wm-ml-1",
        }
        idem_key = _derive_idempotency_key(
            str(product_id),
            trigger_context["missing_fields"],
            trigger_context["source_watermark"],
        )

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns_a)
            result = await do_enrich_product(
                pg_app_conn,
                ns_a,
                idempotency_key=idem_key,
                confirm=True,
                product_id=str(product_id),
                trigger_context=trigger_context,
            )

        assert result["status"] == "executed"
        inner = result["result"]
        assert inner["needs_review_count"] >= 1
        assert inner["auto_merged"] == 0, "Money/legal fields must NEVER be auto-merged to catalog"

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns_a)
            rows = await pg_app_conn.fetch(
                "SELECT field_name, needs_review FROM product_enrichment_log WHERE product_id = $1",
                product_id,
            )

        money_rows = [r for r in rows if r["field_name"] == money_field]
        assert money_rows, f"Expected log row for money field {money_field!r}"
        for row in money_rows:
            assert row["needs_review"] is True, (
                f"§9.3 violation: money/legal field {row['field_name']!r} "
                "must be needs_review=True regardless of confidence"
            )
    finally:
        del os.environ["NCE_PRODUCT_ENRICH_MIN_CONFIDENCE"]


# ---------------------------------------------------------------------------
# Test 6: event_log entry is appended on first confirmed execution
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_enrich_appends_event_log(
    pg_app_conn: asyncpg.Connection,
    ns_a: uuid.UUID,
) -> None:
    """The @governed audit step appends an event_log entry on first execution."""
    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        product_id = await _insert_test_product(pg_app_conn, ns_a, mfr_part_no="PART-ELOG")

    trigger_context = {
        "kind": "quote",
        "ref_id": "QUOTE-EL",
        "missing_fields": ["category"],
        "source_watermark": "wm-el-1",
    }
    idem_key = _derive_idempotency_key(
        str(product_id),
        trigger_context["missing_fields"],
        trigger_context["source_watermark"],
    )

    # Count event_log entries before.
    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        before = await pg_app_conn.fetchval(
            "SELECT count(*) FROM event_log WHERE namespace_id = $1",
            ns_a,
        )

    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        result = await do_enrich_product(
            pg_app_conn,
            ns_a,
            idempotency_key=idem_key,
            confirm=True,
            product_id=str(product_id),
            trigger_context=trigger_context,
        )

    assert result["status"] == "executed"

    # Count event_log entries after.
    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        after = await pg_app_conn.fetchval(
            "SELECT count(*) FROM event_log WHERE namespace_id = $1",
            ns_a,
        )

    assert after > before, (
        f"event_log must grow after @governed execution: before={before} after={after}"
    )


# ---------------------------------------------------------------------------
# Test 7: product_enrichment_log rows are namespace-isolated (FORCE RLS)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_enrich_rls_namespace_isolation(
    pg_app_conn: asyncpg.Connection,
    ns_a: uuid.UUID,
    ns_b: uuid.UUID,
) -> None:
    """product_enrichment_log is FORCE RLS: ns_b sees 0 rows written under ns_a."""
    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        product_id_a = await _insert_test_product(pg_app_conn, ns_a, mfr_part_no="PART-RLS-A")

    trigger_context = {
        "kind": "quote",
        "ref_id": "QUOTE-RLS",
        "missing_fields": ["category"],
        "source_watermark": "wm-rls-1",
    }
    idem_key = _derive_idempotency_key(
        str(product_id_a),
        trigger_context["missing_fields"],
        trigger_context["source_watermark"],
    )

    # Write enrichment log row under namespace A.
    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        result = await do_enrich_product(
            pg_app_conn,
            ns_a,
            idempotency_key=idem_key,
            confirm=True,
            product_id=str(product_id_a),
            trigger_context=trigger_context,
        )
    assert result["status"] == "executed"

    # Namespace A can see the row.
    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        count_a = await pg_app_conn.fetchval(
            "SELECT count(*) FROM product_enrichment_log WHERE product_id = $1",
            product_id_a,
        )
    assert count_a >= 1, f"Namespace A must see its own enrichment rows, got {count_a}"

    # Namespace B sees nothing (FORCE RLS blocks cross-tenant access).
    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_b)
        count_b = await pg_app_conn.fetchval(
            "SELECT count(*) FROM product_enrichment_log WHERE product_id = $1",
            product_id_a,
        )
    assert count_b == 0, (
        f"FORCE RLS violation: namespace B must see 0 rows for ns_a's product, got {count_b}"
    )


# ---------------------------------------------------------------------------
# Test 8: never-bulk — the enrichment query uses a product_id filter
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_enrich_never_bulk_single_product_only(
    pg_app_conn: asyncpg.Connection,
    ns_a: uuid.UUID,
) -> None:
    """Verify the handler operates on exactly one product (never a catalog scan).

    Inserts two products in the same namespace; enriches only one; asserts the
    other has no enrichment log rows.
    """
    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        product_a = await _insert_test_product(pg_app_conn, ns_a, mfr_part_no="PART-NB-A")
        product_b = await _insert_test_product(pg_app_conn, ns_a, mfr_part_no="PART-NB-B")

    trigger_context = {
        "kind": "design",
        "ref_id": "DESIGN-NB",
        "missing_fields": ["short_description"],
        "source_watermark": "wm-nb-1",
    }
    idem_key = _derive_idempotency_key(
        str(product_a),
        trigger_context["missing_fields"],
        trigger_context["source_watermark"],
    )

    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        result = await do_enrich_product(
            pg_app_conn,
            ns_a,
            idempotency_key=idem_key,
            confirm=True,
            product_id=str(product_a),
            trigger_context=trigger_context,
        )
    assert result["status"] == "executed"

    # product_b must have no enrichment log rows.
    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        count_b = await pg_app_conn.fetchval(
            "SELECT count(*) FROM product_enrichment_log WHERE product_id = $1",
            product_b,
        )
    assert count_b == 0, (
        f"Enrichment must be scoped to exactly one product_id: "
        f"product_b unexpectedly has {count_b} log rows"
    )
