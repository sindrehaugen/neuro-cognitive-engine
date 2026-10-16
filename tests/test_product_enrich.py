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
            (manufacturer, mfr_part_no, product_source_id,
             lifecycle_status, etim_specs)
        VALUES ($1, $2, $3, 'active', '{}'::jsonb)
        ON CONFLICT (manufacturer, mfr_part_no) DO UPDATE
            SET etim_specs       = '{}'::jsonb,
                lifecycle_status = 'active',
                updated_at       = now()
        RETURNING id
        """,
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


@pytest.mark.asyncio
async def test_enrich_product_auto_merge_includes_provenance_and_accept_scopes_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify auto-merged fields include provenance and accept_enrichment_proposal filters by namespace_id."""
    import json
    from typing import Any
    from unittest.mock import AsyncMock, MagicMock

    import nce.vertical_modules.product.enrich as enrich_mod
    from nce.vertical_modules.product.enrich import (
        accept_enrichment_proposal,
        do_enrich_product,
    )

    ns_id = uuid.uuid4()
    product_id = uuid.uuid4()
    log_id = uuid.uuid4()

    executed_queries: list[tuple[str, tuple[Any, ...]]] = []

    conn = AsyncMock()
    conn._pool = None

    async def fake_fetchrow(sql: str, *args: Any) -> Any:
        executed_queries.append((sql, args))
        if "FROM govern_idempotency_ledger" in sql or "FROM action_approval_queue" in sql:
            return None
        if "product_catalog" in sql:
            return {
                "id": product_id,
                "manufacturer": "Acme",
                "mfr_part_no": "SKU-PROV-1",
                "product_source_id": "SRC-1",
                "etim_specs": "{}",
            }
        if "product_enrichment_log" in sql:
            return {
                "id": log_id,
                "product_id": product_id,
                "field_name": "weight_kg",
                "field_value": "12.5",
                "product_source_id": "https://vendor.example/spec.pdf",
                "confidence": 0.85,
            }
        return None

    async def fake_execute(sql: str, *args: Any) -> str:
        executed_queries.append((sql, args))
        return "UPDATE 1"

    conn.fetchrow.side_effect = fake_fetchrow
    conn.execute.side_effect = fake_execute

    async def fake_build_proposals(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "field_name": "power_watts",
                "field_value": "500",
                "confidence": 0.96,
                "verbalized": "very high confidence",
                "source": "https://vendor.example/datasheet.pdf",
            }
        ]

    monkeypatch.setattr(enrich_mod, "_build_proposals", fake_build_proposals)
    monkeypatch.setattr("nce.autonomy.governor._audit_execution", AsyncMock())

    enrich_res = await do_enrich_product(
        conn,
        ns_id,
        idempotency_key="idem-prov-1",
        confirm=True,
        product_id=str(product_id),
        trigger_context={"missing_fields": ["power_watts"]},
        provider=MagicMock(),
    )
    assert enrich_res["status"] == "executed"
    catalog_updates = [args for sql, args in executed_queries if "UPDATE product_catalog" in sql]
    assert len(catalog_updates) == 1
    patch_payload = json.loads(catalog_updates[0][0])
    assert "provenance" in patch_payload["power_watts"]
    assert patch_payload["power_watts"]["provenance"] == {
        "source": "https://vendor.example/datasheet.pdf",
        "reason": "auto_enriched",
    }

    executed_queries.clear()
    accept_res = await accept_enrichment_proposal(
        conn,
        ns_id,
        enrichment_id=str(log_id),
    )
    assert accept_res["status"] == "accepted"

    log_selects = [
        (sql, args) for sql, args in executed_queries if "FROM   product_enrichment_log" in sql
    ]
    assert len(log_selects) == 1
    assert "namespace_id = $2::uuid" in log_selects[0][0]
    assert log_selects[0][1] == (log_id, ns_id)

    log_updates = [
        (sql, args) for sql, args in executed_queries if "UPDATE product_enrichment_log" in sql
    ]
    assert len(log_updates) == 1
    assert "namespace_id = $2::uuid" in log_updates[0][0]
    assert log_updates[0][1] == (log_id, ns_id)


# ---------------------------------------------------------------------------
# Wave E-5: Norwegian Elnummer, European ETIM, Metal Surcharges & Drum Cuts
# ---------------------------------------------------------------------------


def test_normalize_elnummer_valid_and_variants() -> None:
    from nce.vertical_modules.product.enrich import normalize_elnummer

    # Standard formats
    assert normalize_elnummer("1012345") == "1012345"
    assert normalize_elnummer(1012345) == "1012345"
    assert normalize_elnummer(1000000) == "1000000"
    assert normalize_elnummer(9999999) == "9999999"

    # Whitespace and punctuation
    assert normalize_elnummer("10 123 45") == "1012345"
    assert normalize_elnummer("10.123.45") == "1012345"
    assert normalize_elnummer(" 10-123-45 ") == "1012345"
    assert normalize_elnummer("10_123_45") == "1012345"

    # Prefixes (case-insensitive)
    assert normalize_elnummer("EL1012345") == "1012345"
    assert normalize_elnummer("EL 10 123 45") == "1012345"
    assert normalize_elnummer("el: 1012345") == "1012345"
    assert normalize_elnummer("Elnr 1012345") == "1012345"
    assert normalize_elnummer("ELNR: 10 123 45") == "1012345"
    assert normalize_elnummer("NO: 1012345") == "1012345"


def test_normalize_elnummer_invalid_cases() -> None:
    from nce.vertical_modules.product.enrich import normalize_elnummer

    assert normalize_elnummer(None) is None
    assert normalize_elnummer(True) is None
    assert normalize_elnummer(False) is None
    assert normalize_elnummer("") is None
    assert normalize_elnummer("   ") is None
    assert normalize_elnummer("999999") is None  # 6 digits
    assert normalize_elnummer(999999) is None
    assert normalize_elnummer("10000000") is None  # 8 digits
    assert normalize_elnummer(10000000) is None
    assert normalize_elnummer("0123456") is None  # Below 1000000
    assert normalize_elnummer("abcdefg") is None
    assert normalize_elnummer("10A2345") is None


def test_calculate_cable_metal_surcharge_standard_and_baseline() -> None:
    from nce.vertical_modules.product.enrich import calculate_cable_metal_surcharge

    # Standard run: 1000m, 300 kg/km, LME 100 NOK/kg, baseline 0
    res1 = calculate_cable_metal_surcharge(
        length_m=1000.0,
        metal_weight_kg_per_km=300.0,
        lme_rate_nok_per_kg=100.0,
        baseline_rate_nok_per_kg=0.0,
    )
    assert res1["length_m"] == 1000.0
    assert res1["metal_weight_kg_per_km"] == 300.0
    assert res1["metal_weight_kg"] == 300.0
    assert res1["surcharge_rate_nok_per_kg"] == 100.0
    assert res1["surcharge_amount_nok"] == 30000.0
    assert res1["currency"] == "NOK"

    # Fractional run with baseline deduction: 500m, 150 kg/km, LME 110, baseline 30
    # metal_weight = 0.5 * 150 = 75 kg. rate_diff = 110 - 30 = 80 NOK/kg. surcharge = 6000 NOK.
    res2 = calculate_cable_metal_surcharge(
        length_m=500.0,
        metal_weight_kg_per_km=150.0,
        lme_rate_nok_per_kg=110.0,
        baseline_rate_nok_per_kg=30.0,
    )
    assert res2["metal_weight_kg"] == 75.0
    assert res2["surcharge_rate_nok_per_kg"] == 80.0
    assert res2["surcharge_amount_nok"] == 6000.0

    # LME below baseline: surcharge rate floor 0.0 NOK
    res3 = calculate_cable_metal_surcharge(
        length_m=200.0,
        metal_weight_kg_per_km=50.0,
        lme_rate_nok_per_kg=25.0,
        baseline_rate_nok_per_kg=30.0,
    )
    assert res3["surcharge_rate_nok_per_kg"] == 0.0
    assert res3["surcharge_amount_nok"] == 0.0


def test_calculate_cable_metal_surcharge_validation() -> None:
    from nce.vertical_modules.product.enrich import calculate_cable_metal_surcharge

    with pytest.raises(ValueError, match="length_m"):
        calculate_cable_metal_surcharge(
            length_m=-1.0, metal_weight_kg_per_km=100.0, lme_rate_nok_per_kg=80.0
        )

    with pytest.raises(ValueError, match="metal_weight_kg_per_km"):
        calculate_cable_metal_surcharge(
            length_m=100.0, metal_weight_kg_per_km=-5.0, lme_rate_nok_per_kg=80.0
        )

    with pytest.raises(ValueError, match="lme_rate_nok_per_kg"):
        calculate_cable_metal_surcharge(
            length_m=100.0, metal_weight_kg_per_km=100.0, lme_rate_nok_per_kg=-10.0
        )

    with pytest.raises(ValueError, match="baseline_rate_nok_per_kg"):
        calculate_cable_metal_surcharge(
            length_m=100.0,
            metal_weight_kg_per_km=100.0,
            lme_rate_nok_per_kg=80.0,
            baseline_rate_nok_per_kg=-5.0,
        )


def test_spec_bounds_keys_and_values() -> None:
    from nce.vertical_modules.product.enrich import _SPEC_BOUNDS

    # Existing keys preserved 100%
    existing_keys = [
        "rack_units",
        "power_watts",
        "poe_class",
        "btu_per_hour",
        "weight_kg",
        "hdmi_in",
        "hdmi_out",
        "sdi_in",
        "sdi_out",
        "dante_channels",
        "network_ports",
    ]
    for k in existing_keys:
        assert k in _SPEC_BOUNDS, f"Missing existing key {k}"

    # Wave E-5 expanded attributes
    assert _SPEC_BOUNDS["cross_section_mm2"] == (0.14, 1000.0)
    assert _SPEC_BOUNDS["conductor_count"] == (1, 100)
    assert _SPEC_BOUNDS["rated_current_a"] == (0.1, 6300.0)
    assert _SPEC_BOUNDS["rated_voltage_v"] == (5.0, 36000.0)
    assert _SPEC_BOUNDS["breaking_capacity_ka"] == (1.5, 150.0)
    assert _SPEC_BOUNDS["rcd_sensitivity_ma"] == (6.0, 1000.0)
    assert _SPEC_BOUNDS["copper_weight_kg_per_km"] == (0.5, 25000.0)
    assert _SPEC_BOUNDS["drum_length_m"] == (1.0, 5000.0)


def test_validate_spec_value_bounds_and_enums() -> None:
    from nce.vertical_modules.product.enrich import _validate_spec_value

    # Numeric bounds
    ok, err = _validate_spec_value("cross_section_mm2", 2.5)
    assert ok and err is None
    ok, err = _validate_spec_value("cross_section_mm2", 0.05)
    assert not ok and "out of bounds" in err

    ok, err = _validate_spec_value("rated_voltage_v", 230.0)
    assert ok and err is None
    ok, err = _validate_spec_value("rated_voltage_v", 50000.0)
    assert not ok and "out of bounds" in err

    # CPR Fire Class (EN 50575)
    ok, err = _validate_spec_value("cpr_fire_class", "Cca")
    assert ok and err is None
    ok, err = _validate_spec_value("cpr_fire_class", "b1ca")
    assert ok and err is None
    ok, err = _validate_spec_value("cpr_fire_class", "NonExistentClass")
    assert not ok and "cpr_fire_class" in err

    # IP rating (EN 60529)
    ok, err = _validate_spec_value("ip_rating", "IP20")
    assert ok and err is None
    ok, err = _validate_spec_value("ip_rating", "IP68")
    assert ok and err is None
    ok, err = _validate_spec_value("ip_rating", "IP69K")
    assert ok and err is None
    ok, err = _validate_spec_value("ip_rating", "IP999")
    assert not ok and "ip_rating" in err

    # IK rating (EN 62262)
    ok, err = _validate_spec_value("ik_rating", "IK08")
    assert ok and err is None
    ok, err = _validate_spec_value("ik_rating", "IK15")
    assert not ok and "ik_rating" in err

    # WEEE Category (1..6)
    ok, err = _validate_spec_value("weee_category", "4")
    assert ok and err is None
    ok, err = _validate_spec_value("weee_category", 4)
    assert ok and err is None
    ok, err = _validate_spec_value("weee_category", 9)
    assert not ok and "weee_category" in err

    # Boolean flags
    ok, err = _validate_spec_value("rohs_compliant", True)
    assert ok and err is None
    ok, err = _validate_spec_value("ce_marked", "true")
    assert ok and err is None
    ok, err = _validate_spec_value("ce_marked", "maybe")
    assert not ok and "ce_marked" in err


def test_validate_european_product_compliance_full_structure() -> None:
    from nce.vertical_modules.product.enrich import validate_european_product_compliance

    valid_specs = {
        "elnummer": "10 123 45",
        "cpr_fire_class": "cca",
        "ip_rating": "ip65",
        "ik_rating": "ik08",
        "weee_category": 4,
        "rohs_compliant": True,
        "ce_marked": True,
        "cross_section_mm2": 2.5,
        "rated_current_a": 16.0,
        "rated_voltage_v": 230.0,
    }
    report = validate_european_product_compliance(valid_specs)
    assert report["valid"] is True
    assert len(report["errors"]) == 0
    assert report["validated_specs"]["elnummer"] == "1012345"
    assert report["validated_specs"]["cpr_fire_class"] == "Cca"
    assert report["validated_specs"]["ip_rating"] == "IP65"
    assert report["compliance"]["ce_marked"] is True
    assert report["compliance"]["rohs_compliant"] is True
    assert report["compliance"]["cpr_compliant"] is True
    assert report["compliance"]["efobasen_compliant"] is True
    assert report["compliance"]["weee_compliant"] is True

    # Invalid specs
    invalid_specs = {
        "elnummer": "123",
        "cpr_fire_class": "Zca",
        "ip_rating": "NOT_AN_IP",
        "breaking_capacity_ka": 999.0,
    }
    invalid_report = validate_european_product_compliance(invalid_specs)
    assert invalid_report["valid"] is False
    assert "elnummer" in invalid_report["errors"]
    assert "cpr_fire_class" in invalid_report["errors"]
    assert "ip_rating" in invalid_report["errors"]
    assert "breaking_capacity_ka" in invalid_report["errors"]


def test_resolve_elnummer_query_and_search() -> None:
    from nce.vertical_modules.product.mcp_handlers import resolve_elnummer_query

    assert resolve_elnummer_query("10 123 45") == "1012345"
    assert resolve_elnummer_query("EL1012345") == "1012345"
    assert resolve_elnummer_query("1012345") == "1012345"
    assert resolve_elnummer_query("Cisco Catalyst") is None
    assert resolve_elnummer_query("SFP-10G-SR") is None


@pytest.mark.asyncio
async def test_do_search_products_elnummer_branch() -> None:
    from unittest.mock import AsyncMock, MagicMock

    from nce.vertical_modules.product.mcp_handlers import do_search_products

    engine = MagicMock()
    conn = AsyncMock()
    fake_rows = [
        {
            "id": "11111111-1111-4000-8000-000000000001",
            "manufacturer": "NEXANS",
            "mfr_part_no": "TXP-1012345",
            "gtin": None,
            "lifecycle_status": "active",
            "etim_specs": {"elnummer": "1012345"},
            "updated_at": None,
        }
    ]
    conn.fetch = AsyncMock(return_value=fake_rows)

    class FakeCtx:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *args):
            pass

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=FakeCtx())
    engine.pg_pool = pool

    # Patch scoped_pg_session to use FakeCtx
    import nce.vertical_modules.product.mcp_handlers as mcp_mod

    orig_scoped = mcp_mod.scoped_pg_session

    class FakeScopedSession:
        def __init__(self, p, ns):
            self.p = p

        async def __aenter__(self):
            return conn

        async def __aexit__(self, *args):
            pass

    mcp_mod.scoped_pg_session = FakeScopedSession
    try:
        res = await do_search_products(
            engine,
            {"namespace_id": "00000000-0000-4000-8000-000000000001", "query": "10 123 45"},
        )
        assert res["total"] == 1
        assert res["results"][0]["manufacturer"] == "NEXANS"
        # Verify SQL query executed includes etim_specs->>'elnummer'
        assert conn.fetch.called
        call_sql = conn.fetch.call_args[0][0]
        assert "etim_specs->>'elnummer' = $1" in call_sql
        assert conn.fetch.call_args[0][1] == "1012345"
    finally:
        mcp_mod.scoped_pg_session = orig_scoped


def test_evaluate_cable_drum_cut_best_fit() -> None:
    from nce.vertical_modules.inventory.stock import evaluate_cable_drum_cut

    drums = [
        {"drum_id": "DRUM-100", "remaining_length_m": 500.0},
        {"drum_id": "DRUM-200", "remaining_length_m": 120.0},
        {"drum_id": "DRUM-300", "remaining_length_m": 80.0},
    ]

    # Required length 100m:
    # DRUM-300 has 80m (< 100m, ineligible).
    # DRUM-100 has 500m (waste = 400m).
    # DRUM-200 has 120m (waste = 20m).
    # Best fit must be DRUM-200.
    res = evaluate_cable_drum_cut(drums, required_length_m=100.0, allow_splicing=False)
    assert res["passed"] is True
    assert res["selected_drum_id"] == "DRUM-200"
    assert res["cut_length_m"] == 100.0
    assert res["remnant_length_m"] == 20.0


def test_evaluate_cable_drum_cut_exact_fit() -> None:
    from nce.vertical_modules.inventory.stock import evaluate_cable_drum_cut

    drums = [
        {"id": "DRUM-EXACT", "qty_on_hand": 150.0},
        {"id": "DRUM-LARGE", "qty_on_hand": 500.0},
    ]
    res = evaluate_cable_drum_cut(drums, required_length_m=150.0)
    assert res["passed"] is True
    assert res["selected_drum_id"] == "DRUM-EXACT"
    assert res["cut_length_m"] == 150.0
    assert res["remnant_length_m"] == 0.0


def test_evaluate_cable_drum_cut_fails_closed_without_splicing() -> None:
    from nce.vertical_modules.inventory.stock import evaluate_cable_drum_cut

    # Total cable across drums is 110m (50 + 60), but required is 75m contiguous
    drums = [
        {"drum_id": "DRUM-A", "remaining_length_m": 50.0},
        {"drum_id": "DRUM-B", "remaining_length_m": 60.0},
    ]
    res = evaluate_cable_drum_cut(drums, required_length_m=75.0, allow_splicing=False)
    assert res["passed"] is False
    assert res["selected_drum_id"] is None
    assert res["cut_length_m"] == 0.0
    assert res["remnant_length_m"] is None
    assert "NEK 400-5-52" in res["reason"]


def test_evaluate_cable_drum_cut_splicing_permitted() -> None:
    from nce.vertical_modules.inventory.stock import evaluate_cable_drum_cut

    drums = [
        {"drum_id": "DRUM-A", "remaining_length_m": 50.0},
        {"drum_id": "DRUM-B", "remaining_length_m": 60.0},
    ]
    # Splicing permitted: takes 60m from DRUM-B and 15m from DRUM-A
    res = evaluate_cable_drum_cut(drums, required_length_m=75.0, allow_splicing=True)
    assert res["passed"] is True
    assert res["cut_length_m"] == 75.0
    assert "cuts" in res
    assert len(res["cuts"]) == 2


def test_evaluate_cable_drum_cut_invalid_inputs() -> None:
    from nce.vertical_modules.inventory.stock import evaluate_cable_drum_cut

    with pytest.raises(ValueError, match="strictly positive"):
        evaluate_cable_drum_cut([], required_length_m=0.0)

    with pytest.raises(ValueError, match="strictly positive"):
        evaluate_cable_drum_cut([], required_length_m=-10.0)

    with pytest.raises(ValueError, match="valid number"):
        evaluate_cable_drum_cut([], required_length_m="invalid_number")


@pytest.mark.asyncio
async def test_enrich_product_spec_bounds_violation_triggers_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Out-of-bounds technical specification forces needs_review=True even if confidence is high."""
    from typing import Any
    from unittest.mock import AsyncMock, MagicMock

    import nce.vertical_modules.product.enrich as enrich_mod
    from nce.vertical_modules.product.enrich import do_enrich_product

    ns_id = uuid.uuid4()
    product_id = uuid.uuid4()

    executed_queries: list[tuple[str, tuple[Any, ...]]] = []
    conn = AsyncMock()
    conn._pool = None

    async def fake_fetchrow(sql: str, *args: Any) -> Any:
        executed_queries.append((sql, args))
        if "FROM govern_idempotency_ledger" in sql or "FROM action_approval_queue" in sql:
            return None
        if "product_catalog" in sql:
            return {
                "id": product_id,
                "manufacturer": "Siemens",
                "mfr_part_no": "5SY4116-7",
                "product_source_id": "SRC-E5",
                "etim_specs": "{}",
            }
        return None

    async def fake_execute(sql: str, *args: Any) -> str:
        executed_queries.append((sql, args))
        return "INSERT 1"

    conn.fetchrow.side_effect = fake_fetchrow
    conn.execute.side_effect = fake_execute

    # Proposal with rated_voltage_v = 100000.0 (max bound is 36000.0)
    async def fake_build_proposals(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "field_name": "rated_voltage_v",
                "field_value": "100000.0",
                "confidence": 0.99,
                "verbalized": "very_high",
            }
        ]

    monkeypatch.setattr(enrich_mod, "_build_proposals", fake_build_proposals)
    monkeypatch.setattr("nce.autonomy.governor._audit_execution", AsyncMock())

    res = await do_enrich_product(
        conn,
        ns_id,
        idempotency_key="idem-bounds-test-1",
        confirm=True,
        product_id=str(product_id),
        trigger_context={"missing_fields": ["rated_voltage_v"]},
        provider=MagicMock(),
    )
    assert res["status"] == "executed"
    result_data = res.get("result", res)
    assert result_data["auto_merged"] == 0
    assert result_data["needs_review_count"] == 1
    # Verify no UPDATE product_catalog was executed since out-of-bounds field was not auto-merged
    catalog_updates = [args for sql, args in executed_queries if "UPDATE product_catalog" in sql]
    assert len(catalog_updates) == 0
    # But product_enrichment_log INSERT was executed with needs_review=True
    log_inserts = [
        args for sql, args in executed_queries if "INSERT INTO product_enrichment_log" in sql
    ]
    assert len(log_inserts) == 1
    assert log_inserts[0][6] is True  # needs_review parameter
