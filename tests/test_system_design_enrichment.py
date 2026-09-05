"""
tests/test_system_design_enrichment.py
=======================================
Integration tests for Batch 061 re-dispatch — Module 6.Wave 6
(scoped-a2a-enrichment, graph-fetch rewrite).

Why this file exists
--------------------
The original unit tests (tests/unit/test_system_design_enrichment.py) mocked
``scoped_pg_session`` so ``_fetch_design_lines`` never actually ran SQL.  That
hid the root cause: the prior implementation queried a non-existent relational
table ``design_lines``.  These integration tests seed a REAL design via
``do_author_functional_location`` (Wave 2) so the graph-fetch path hits the
live database.

Assertions
----------
1. **Scoped enrichment**: ``enqueue_product_enrichment`` fires ONCE per unique
   resolved product UUID (never per PRODUCT label, never a bulk catalog sweep).
   Two design-lines sharing the same product trigger exactly one A2A call.

2. **Unresolved products skipped**: PRODUCT labels that have no matching
   ``product_catalog`` row produce zero A2A calls and zero TCO calculations,
   but do NOT raise.

3. **Fire-and-backfill — never raises**: even when ``enqueue_product_enrichment``
   raises, ``do_enrich_design_lines`` returns a normal result dict; the caller
   is not affected.

4. **TCO computed when price present**: when ``product_prices`` rows exist, TCO
   is computed; when absent, tco_skipped is incremented.

5. **Return shape**: the result dict carries all documented summary keys.

Test strategy
-------------
- Seed DESIGN + DESIGN_LINE nodes + kg_edges using the real ``do_author_functional_location``.
- Insert matching ``product_catalog`` rows directly.
- Mock only ``enqueue_product_enrichment`` (count calls, optionally raise).
- The price/TCO path uses the real ``_resolve_unit_price``; for tests that
  need a price, we also seed a ``product_prices`` row.

Runs as ``@pytest.mark.integration`` — requires a live Postgres with schema.sql
and migrations applied.  Skips automatically when no DSN is configured.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import patch

import pytest

from nce.auth import set_namespace_context
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.system_design.graph import do_author_functional_location

# ---------------------------------------------------------------------------
# Module-level patch targets
# ---------------------------------------------------------------------------

_MOCK_EMIT = "nce.vertical_modules.system_design.graph.emit_graph_write"
_ENQUEUE_PATCH = "nce.vertical_modules.system_design.enrichment.enqueue_product_enrichment"
_LOAD_CFG_PATCH = "nce.vertical_modules.system_design.enrichment.load_procurement_config"

# ---------------------------------------------------------------------------
# Shared fixture constants
# ---------------------------------------------------------------------------

_NS_SLUG = "enrichtest"
_DESIGN_ID = "ENRICH-DESIGN-001"
_SITE = "Site-Enrich"
_BUILDING = "BuildingA"
_FLOOR = "F1"
_ROOM = "R101"

_MFR_A = "Biamp"
_PART_A = "TesiraFORTE-CI"
_LINE_REF_A = "LINE-EA"

_MFR_B = "Shure"
_PART_B = "MXA910"
_LINE_REF_B = "LINE-EB"

# Design lines for the two-product fixture.
_DESIGN_LINES_TWO = [
    {
        "line_ref": _LINE_REF_A,
        "manufacturer": _MFR_A,
        "mfr_part_no": _PART_A,
        "confidence": 0.95,
    },
    {
        "line_ref": _LINE_REF_B,
        "manufacturer": _MFR_B,
        "mfr_part_no": _PART_B,
        "confidence": 0.90,
    },
]

# Design with two lines sharing the same product (dedup test).
_DESIGN_ID_SHARED = "ENRICH-DESIGN-SHARED"
_LINE_REF_C = "LINE-EC"
_DESIGN_LINES_SHARED = [
    {
        "line_ref": _LINE_REF_A,
        "manufacturer": _MFR_A,
        "mfr_part_no": _PART_A,
        "confidence": 0.90,
    },
    {
        "line_ref": _LINE_REF_C,
        "manufacturer": _MFR_A,
        "mfr_part_no": _PART_A,  # same product — should fire only once
        "confidence": 0.85,
    },
]

# Design with a product that has NO product_catalog row (unresolvable).
_DESIGN_ID_UNKNOWN = "ENRICH-DESIGN-UNKNOWN"
_MFR_UNKNOWN = "NoSuchMfr"
_PART_UNKNOWN = "NOPE-9999"
_DESIGN_LINES_UNKNOWN = [
    {
        "line_ref": "LINE-EU",
        "manufacturer": _MFR_UNKNOWN,
        "mfr_part_no": _PART_UNKNOWN,
        "confidence": 0.70,
    },
]

_BUILDINGS = [
    {
        "name": _BUILDING,
        "floors": [
            {
                "name": _FLOOR,
                "rooms": [{"name": _ROOM, "positions": ["POS-1"]}],
            }
        ],
    }
]


# ---------------------------------------------------------------------------
# Minimal NCEEngine stub
# ---------------------------------------------------------------------------


class _EngineStub:
    """Minimal engine stub exposing ``pg_pool`` only."""

    def __init__(self, pg_pool: Any) -> None:
        self.pg_pool = pg_pool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_ownership(pg_pool: Any, ns_id: uuid.UUID) -> None:
    """Seed node-ownership registry for this namespace."""
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns_id)
            await seed_node_ownership_registry(conn, ns_id)


async def _author_design(
    pg_pool: Any,
    ns_id: uuid.UUID,
    *,
    design_id: str,
    design_lines: list[dict],  # type: ignore[type-arg]
) -> None:
    """Author DESIGN + DESIGN_LINE nodes via the real W2 graph writer."""
    with patch(_MOCK_EMIT) as mock_emit:
        mock_emit.return_value = None
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns_id)
                await do_author_functional_location(
                    conn,
                    ns_id,
                    namespace_slug=_NS_SLUG,
                    design_id=design_id,
                    site_name=_SITE,
                    buildings=_BUILDINGS,
                    design_lines=design_lines,
                    source_id=f"src-{design_id}",
                )


async def _insert_product_catalog(
    pg_pool: Any,
    ns_id: uuid.UUID,
    *,
    manufacturer: str,
    mfr_part_no: str,
) -> uuid.UUID:
    """Insert a minimal product_catalog row; return its id."""
    async with pg_pool.acquire() as conn:
        product_id: uuid.UUID = await conn.fetchval(
            """
            INSERT INTO product_catalog
                (manufacturer, mfr_part_no, product_source_id,
                 lifecycle_status, etim_specs)
            VALUES ($1, $2, $3, 'active', '{}'::jsonb)
            ON CONFLICT (manufacturer, mfr_part_no)
                DO UPDATE SET updated_at = NOW()
            RETURNING id
            """,
            manufacturer,
            mfr_part_no,
            f"src-{mfr_part_no}",
        )
    return product_id


async def _insert_product_price(
    pg_pool: Any,
    ns_id: uuid.UUID,
    *,
    mfr_part_no: str,
    list_price: float,
) -> None:
    """Insert a product_prices row so TCO can resolve a price."""
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO product_prices
                (namespace_id, mfr_part_no, supplier, bid_id, list_price)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (namespace_id, mfr_part_no, supplier, bid_id)
                DO UPDATE SET list_price = EXCLUDED.list_price
            """,
            ns_id,
            mfr_part_no,
            "test-supplier",
            "test-bid",
            list_price,
        )


def _make_params(
    ns_id: uuid.UUID,
    design_id: str,
    missing_fields: list[str] | None = None,
) -> dict[str, Any]:
    p: dict[str, Any] = {
        "namespace_id": str(ns_id),
        "design_id": design_id,
    }
    if missing_fields is not None:
        p["missing_fields"] = missing_fields
    return p


def _fake_tco_config() -> tuple[dict[str, Any], dict[str, Any]]:
    weights = {
        "TCO_WEIGHTS": {
            "freight": 0.05,
            "warranty": 0.02,
            "stock": 0.03,
            "delivery_risk": 0.01,
        }
    }
    return weights, {}


def _enqueue_return(product_id: str) -> dict[str, Any]:
    return {"product_id": product_id, "enrichment": "queued", "specs_pending": True}


# ---------------------------------------------------------------------------
# Integration test suite
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestDesignEnrichmentIntegration:
    """Integration tests for system_design/enrichment.py Wave 6 (graph-fetch)."""

    # ------------------------------------------------------------------
    # 1. Scoped enrichment: once per unique resolved product UUID
    # ------------------------------------------------------------------

    async def test_enrichment_fires_once_per_unique_product(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """With 2 lines referencing 2 different products, enqueue fires exactly twice."""
        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        await _author_design(pg_pool, ns_id, design_id=_DESIGN_ID, design_lines=_DESIGN_LINES_TWO)

        # Seed both products in product_catalog.
        await _insert_product_catalog(pg_pool, ns_id, manufacturer=_MFR_A, mfr_part_no=_PART_A)
        await _insert_product_catalog(pg_pool, ns_id, manufacturer=_MFR_B, mfr_part_no=_PART_B)

        engine = _EngineStub(pg_pool)

        with (
            patch(_ENQUEUE_PATCH) as mock_enqueue,
            patch(_LOAD_CFG_PATCH, return_value=_fake_tco_config()),
        ):
            mock_enqueue.side_effect = lambda **kw: _enqueue_return(kw["product_id"])

            from nce.vertical_modules.system_design.enrichment import do_enrich_design_lines

            result = await do_enrich_design_lines(engine, _make_params(ns_id, _DESIGN_ID))

        assert mock_enqueue.call_count == 2, (  # noqa: PLR2004
            f"Expected 2 enqueue calls (one per unique product); got {mock_enqueue.call_count}"
        )
        assert result["products_enqueued"] == 2  # noqa: PLR2004
        assert result["lines_found"] == 2  # noqa: PLR2004
        assert result["enrichment"] == "queued"

    async def test_enrichment_fires_once_for_shared_product(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """Two design-lines sharing the same product UUID must trigger ONLY ONE enqueue."""
        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        await _author_design(
            pg_pool, ns_id, design_id=_DESIGN_ID_SHARED, design_lines=_DESIGN_LINES_SHARED
        )

        # Only one product in catalog — both lines reference it.
        await _insert_product_catalog(pg_pool, ns_id, manufacturer=_MFR_A, mfr_part_no=_PART_A)

        engine = _EngineStub(pg_pool)

        with (
            patch(_ENQUEUE_PATCH) as mock_enqueue,
            patch(_LOAD_CFG_PATCH, return_value=_fake_tco_config()),
        ):
            mock_enqueue.side_effect = lambda **kw: _enqueue_return(kw["product_id"])

            from nce.vertical_modules.system_design.enrichment import do_enrich_design_lines

            result = await do_enrich_design_lines(engine, _make_params(ns_id, _DESIGN_ID_SHARED))

        assert mock_enqueue.call_count == 1, (
            f"Expected exactly 1 enqueue call for shared product; got {mock_enqueue.call_count}"
        )
        assert result["products_enqueued"] == 1
        assert result["lines_found"] == 2  # noqa: PLR2004

    # ------------------------------------------------------------------
    # 2. Unresolved products are skipped (not in catalog)
    # ------------------------------------------------------------------

    async def test_unresolved_product_skipped_no_enqueue(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """A PRODUCT label with no product_catalog row must be skipped silently."""
        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        await _author_design(
            pg_pool, ns_id, design_id=_DESIGN_ID_UNKNOWN, design_lines=_DESIGN_LINES_UNKNOWN
        )
        # Intentionally do NOT insert a product_catalog row.

        engine = _EngineStub(pg_pool)

        with (
            patch(_ENQUEUE_PATCH) as mock_enqueue,
            patch(_LOAD_CFG_PATCH, return_value=_fake_tco_config()),
        ):
            from nce.vertical_modules.system_design.enrichment import do_enrich_design_lines

            result = await do_enrich_design_lines(engine, _make_params(ns_id, _DESIGN_ID_UNKNOWN))

        mock_enqueue.assert_not_called()
        assert result["products_enqueued"] == 0
        assert result["products_skipped"] >= 1
        assert result["enrichment"] == "queued"

    # ------------------------------------------------------------------
    # 3. Fire-and-backfill: a raising enqueue must NOT propagate
    # ------------------------------------------------------------------

    async def test_raising_enqueue_does_not_propagate(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """enqueue_product_enrichment raising must NOT raise into do_enrich_design_lines."""
        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        await _author_design(pg_pool, ns_id, design_id=_DESIGN_ID, design_lines=_DESIGN_LINES_TWO)
        await _insert_product_catalog(pg_pool, ns_id, manufacturer=_MFR_A, mfr_part_no=_PART_A)
        await _insert_product_catalog(pg_pool, ns_id, manufacturer=_MFR_B, mfr_part_no=_PART_B)

        engine = _EngineStub(pg_pool)

        with (
            patch(_ENQUEUE_PATCH, side_effect=RuntimeError("A2A unavailable")),
            patch(_LOAD_CFG_PATCH, return_value=_fake_tco_config()),
        ):
            from nce.vertical_modules.system_design.enrichment import do_enrich_design_lines

            # Must NOT raise — fire-and-backfill contract.
            result = await do_enrich_design_lines(engine, _make_params(ns_id, _DESIGN_ID))

        assert result["enrichment"] == "queued"
        assert result["design_id"] == _DESIGN_ID
        # products_enqueued reflects enqueue *attempts*; the exception was swallowed.
        assert "products_enqueued" in result

    # ------------------------------------------------------------------
    # 4. TCO computed when price is present; skipped when absent
    # ------------------------------------------------------------------

    async def test_tco_computed_when_price_present(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """TCO is computed for products that have a price row in product_prices."""
        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        # Single-line design with one product.
        single_line = [_DESIGN_LINES_TWO[0]]  # LINE-EA → BIAMP/TesiraFORTE-CI
        await _author_design(pg_pool, ns_id, design_id=_DESIGN_ID, design_lines=single_line)
        await _insert_product_catalog(pg_pool, ns_id, manufacturer=_MFR_A, mfr_part_no=_PART_A)
        await _insert_product_price(pg_pool, ns_id, mfr_part_no=_PART_A, list_price=500.0)

        engine = _EngineStub(pg_pool)

        with (
            patch(_ENQUEUE_PATCH) as mock_enqueue,
            patch(_LOAD_CFG_PATCH, return_value=_fake_tco_config()),
        ):
            mock_enqueue.side_effect = lambda **kw: _enqueue_return(kw["product_id"])

            from nce.vertical_modules.system_design.enrichment import do_enrich_design_lines

            result = await do_enrich_design_lines(engine, _make_params(ns_id, _DESIGN_ID))

        assert result["tco_computed"] >= 1, (
            f"Expected at least 1 TCO computation when price present; got {result['tco_computed']}"
        )

    async def test_tco_skipped_when_no_price(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """TCO is skipped when no product_prices row exists for the product."""
        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        single_line = [_DESIGN_LINES_TWO[0]]  # LINE-EA
        await _author_design(pg_pool, ns_id, design_id=_DESIGN_ID, design_lines=single_line)
        await _insert_product_catalog(pg_pool, ns_id, manufacturer=_MFR_A, mfr_part_no=_PART_A)
        # Intentionally do NOT insert a product_prices row.

        engine = _EngineStub(pg_pool)

        with (
            patch(_ENQUEUE_PATCH) as mock_enqueue,
            patch(_LOAD_CFG_PATCH, return_value=_fake_tco_config()),
        ):
            mock_enqueue.side_effect = lambda **kw: _enqueue_return(kw["product_id"])

            from nce.vertical_modules.system_design.enrichment import do_enrich_design_lines

            result = await do_enrich_design_lines(engine, _make_params(ns_id, _DESIGN_ID))

        assert result["tco_skipped"] >= 1, (
            f"Expected tco_skipped >= 1 when no price; got {result['tco_skipped']}"
        )

    # ------------------------------------------------------------------
    # 5. Return shape: all documented keys present
    # ------------------------------------------------------------------

    async def test_return_shape_contains_expected_keys(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """Result dict must carry all documented summary keys."""
        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        await _author_design(pg_pool, ns_id, design_id=_DESIGN_ID, design_lines=_DESIGN_LINES_TWO)
        await _insert_product_catalog(pg_pool, ns_id, manufacturer=_MFR_A, mfr_part_no=_PART_A)
        await _insert_product_catalog(pg_pool, ns_id, manufacturer=_MFR_B, mfr_part_no=_PART_B)

        engine = _EngineStub(pg_pool)

        with (
            patch(_ENQUEUE_PATCH) as mock_enqueue,
            patch(_LOAD_CFG_PATCH, return_value=_fake_tco_config()),
        ):
            mock_enqueue.side_effect = lambda **kw: _enqueue_return(kw["product_id"])

            from nce.vertical_modules.system_design.enrichment import do_enrich_design_lines

            result = await do_enrich_design_lines(engine, _make_params(ns_id, _DESIGN_ID))

        required_keys = {
            "design_id",
            "lines_found",
            "products_enqueued",
            "products_skipped",
            "tco_computed",
            "tco_skipped",
            "enrichment",
        }
        missing = required_keys - set(result.keys())
        assert not missing, f"Result is missing keys: {missing}"
        assert result["design_id"] == _DESIGN_ID
        assert result["enrichment"] == "queued"

    # ------------------------------------------------------------------
    # 6. Empty design: no DESIGN_LINEs → no enrichment, no error
    # ------------------------------------------------------------------

    async def test_design_with_no_lines_returns_zero_counts(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """A design with no DESIGN_LINE nodes returns all-zero counts without error."""
        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        # Author a design with NO design_lines.
        await _author_design(pg_pool, ns_id, design_id=_DESIGN_ID, design_lines=[])

        engine = _EngineStub(pg_pool)

        with (
            patch(_ENQUEUE_PATCH) as mock_enqueue,
            patch(_LOAD_CFG_PATCH, return_value=_fake_tco_config()),
        ):
            from nce.vertical_modules.system_design.enrichment import do_enrich_design_lines

            result = await do_enrich_design_lines(engine, _make_params(ns_id, _DESIGN_ID))

        mock_enqueue.assert_not_called()
        assert result["lines_found"] == 0
        assert result["products_enqueued"] == 0
        assert result["tco_computed"] == 0
