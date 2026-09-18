"""Integration tests for Sales DealRoom (Batch 089 / Wave S-3)."""

from __future__ import annotations

import datetime
import json
from typing import Any
from uuid import UUID, uuid4

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.vertical_modules.sales.dealroom import do_open_dealroom


async def _insert_sales_record(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: UUID,
    entity: str,
    source_id: str,
    name: str,
    source_json: dict[str, Any],
) -> None:
    """Helper to insert sales read model records directly for testing."""
    await conn.execute(
        """
        INSERT INTO sales_read_model
            (namespace_id, entity, source_id, name, source_json, manual, is_deleted, modifiedon, synced_at)
        VALUES
            ($1, $2, $3, $4, $5::jsonb, '{}'::jsonb, false, now(), now())
        ON CONFLICT (namespace_id, entity, source_id)
        DO UPDATE SET
            name = EXCLUDED.name,
            source_json = EXCLUDED.source_json,
            updated_at = now()
        """,
        str(namespace_id),
        entity,
        source_id,
        name,
        json.dumps(source_json),
    )


async def _insert_bom_line_content(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: UUID,
    bom_line_label: str,
    quote_id: str,
    line_ref: str,
    qty: float = 1.0,
    unit_price: float | None = None,
    line_total: float | None = None,
    currency: str = "NOK",
    priced: bool = True,
    origin_kind: str = "manual",
    origin_ref: str | None = None,
    writer_engine: str = "sales",
) -> None:
    """Helper to insert bom_line_content records directly for testing."""
    await conn.execute(
        """
        INSERT INTO bom_line_content
            (namespace_id, bom_line_label, quote_id, line_ref, qty, unit_price, line_total, currency, priced, origin_kind, origin_ref, writer_engine)
        VALUES
            ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        ON CONFLICT (namespace_id, bom_line_label) DO UPDATE SET
            qty = EXCLUDED.qty,
            unit_price = EXCLUDED.unit_price,
            line_total = EXCLUDED.line_total,
            priced = EXCLUDED.priced,
            origin_ref = EXCLUDED.origin_ref
        """,
        namespace_id,
        bom_line_label,
        quote_id,
        line_ref,
        qty,
        unit_price if unit_price is not None else 0.0,
        line_total if line_total is not None else 0.0,
        currency,
        priced,
        origin_kind,
        origin_ref,
        writer_engine,
    )


async def _insert_product_catalog(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    manufacturer: str,
    mfr_part_no: str,
    product_source_id: str = "test-prod",
) -> None:
    """Helper to insert product catalog records directly for testing."""
    await conn.execute(
        """
        INSERT INTO product_catalog (manufacturer, mfr_part_no, product_source_id)
        VALUES ($1, $2, $3)
        ON CONFLICT (manufacturer, mfr_part_no) DO NOTHING
        """,
        manufacturer,
        mfr_part_no,
        product_source_id,
    )


def _make_dealroom_engine_stub(pg_pool: asyncpg.Pool) -> Any:  # type: ignore[type-arg]
    """Minimal engine stub exposing ``pg_pool``."""

    class _EngineStub:
        pass

    stub = _EngineStub()
    stub.pg_pool = pg_pool  # type: ignore[attr-defined]
    return stub


@pytest.mark.integration
@pytest.mark.asyncio
class TestSalesDealRoom:
    """Integration tests for Sales DealRoom do_open_dealroom function."""

    async def test_open_dealroom_prices_correctly_and_handles_toggles(
        self,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """Verify that do_open_dealroom fetches bom_line_content, correlates product_catalog, and handles options toggles."""
        ns = await make_namespace()
        quote_id = f"q-{uuid4().hex[:8]}"
        label1 = f"BOM_LINE:{quote_id.upper()}:LINE-1"
        label2 = f"BOM_LINE:{quote_id.upper()}:LINE-2"

        engine = _make_dealroom_engine_stub(pg_pool)

        # 1. Seed PostgreSQL sales_read_model, product_catalog, and bom_line_content
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns)

                # Seed quote in read model
                await _insert_sales_record(
                    conn,
                    ns,
                    "quotes",
                    quote_id,
                    "Interactive Design Quote",
                    {
                        "quoteid": quote_id,
                        "name": "Interactive Design Quote",
                        "description": "AV DealRoom quote description",
                    },
                )

                # Seed product catalog
                await _insert_product_catalog(conn, "Sony", "VPL-XW5000ES")
                await _insert_product_catalog(conn, "Chief", "Mount-X")

                # Seed BOM lines in PostgreSQL bom_line_content
                # Line 1: Sony Projector, unit_price = 50000.0 / 0.7, qty = 1
                unit_p1 = 50000.0 / 0.7
                await _insert_bom_line_content(
                    conn,
                    ns,
                    label1,
                    quote_id,
                    "LINE-1",
                    qty=1.0,
                    unit_price=unit_p1,
                    line_total=unit_p1 * 1.0,
                    priced=True,
                    origin_ref="VPL-XW5000ES",
                )

                # Line 2: Chief Mount, unit_price = 1000.0 / 0.6, qty = 2
                unit_p2 = 1000.0 / 0.6
                await _insert_bom_line_content(
                    conn,
                    ns,
                    label2,
                    quote_id,
                    "LINE-2",
                    qty=2.0,
                    unit_price=unit_p2,
                    line_total=unit_p2 * 2.0,
                    priced=True,
                    origin_ref="Mount-X",
                )

        # 2. Call do_open_dealroom with all toggled on (default)
        res = await do_open_dealroom(
            engine,
            {
                "namespace_id": str(ns),
                "quote_id": quote_id,
            },
        )

        assert res["quote_id"] == quote_id
        assert res["name"] == "Interactive Design Quote"
        assert res["description"] == "AV DealRoom quote description"

        # Check lines
        lines = res["lines"]
        assert len(lines) == 2

        # Line 1: Sony Projector
        assert lines[0]["manufacturer"] == "Sony"
        assert lines[0]["model"] == "VPL-XW5000ES"
        assert lines[0]["quantity"] == 1
        assert abs(lines[0]["unit_price"] - (50000.0 / 0.7)) < 1e-2
        assert lines[0]["toggled"] is True

        # Line 2: Chief Mount
        assert lines[1]["manufacturer"] == "Chief"
        assert lines[1]["model"] == "Mount-X"
        assert lines[1]["quantity"] == 2
        assert abs(lines[1]["unit_price"] - (1000.0 / 0.6)) < 1e-2
        assert lines[1]["toggled"] is True

        # Total quote price: (50000 / 0.7) + 2 * (1000 / 0.6) = 71428.57 + 3333.33 = 74761.90
        expected_total = (50000.0 / 0.7) + (2000.0 / 0.6)
        assert abs(res["total_price_nok"] - expected_total) < 1e-2

        # 3. Toggle off Line 2 (Chief Mount) and verify recalculation
        res_toggled = await do_open_dealroom(
            engine,
            {
                "namespace_id": str(ns),
                "quote_id": quote_id,
                "toggled_options": {
                    label2: False,
                },
            },
        )

        # Recomputed price should only include Sony projector (Line 1)
        expected_toggled_total = 50000.0 / 0.7
        assert abs(res_toggled["total_price_nok"] - expected_toggled_total) < 1e-2
        assert res_toggled["lines"][1]["toggled"] is False


# ---------------------------------------------------------------------------
# Regression tests: the BOM_LINE fetch used to build a raw SQL LIKE pattern
# from a caller-supplied quote_id. `_` and `%` are LIKE metacharacters -- a
# quote id containing either would silently widen the match to a DIFFERENT
# quote's BOM lines, showing one customer another quote's line items on this
# customer-facing DealRoom surface (confirmed live:
# 'BOM_LINE:QA1:AMP01' LIKE 'BOM_LINE:Q_1:%' is true). Fixed via a literal
# starts_with() prefix test -- mirrors economy/cascade.py's
# _read_actual_cost_total (Batch 120).
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestSalesDealRoomBomLineLabelMatching:
    async def test_underscore_in_quote_id_does_not_leak_another_quotes_bom_lines(
        self,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """Reproduces the exact live scenario:
        'BOM_LINE:QA1:AMP01' LIKE 'BOM_LINE:Q_1:%' is true under raw LIKE
        because `_` matches any single character. Seeds two quotes whose ids
        differ only at the position an unescaped `_` would wildcard-match,
        and asserts the DealRoom for the underscore quote shows ONLY its own
        BOM line -- never the victim's."""
        ns = await make_namespace()
        suffix = uuid4().hex[:8]
        quote_with_underscore = f"QU_{suffix}"  # contains a literal '_'
        quote_collision_victim = f"QUZ{suffix}"  # same length, 'Z' where '_' falls
        label_own = f"BOM_LINE:{quote_with_underscore.upper()}:AMP01"
        label_victim = f"BOM_LINE:{quote_collision_victim.upper()}:AMP01"

        engine = _make_dealroom_engine_stub(pg_pool)

        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns)
                await _insert_bom_line_content(
                    conn, ns, label_own, quote_with_underscore, "AMP01", priced=False
                )
                await _insert_bom_line_content(
                    conn, ns, label_victim, quote_collision_victim, "AMP01", priced=False
                )

        result = await do_open_dealroom(
            engine,
            {"namespace_id": str(ns), "quote_id": quote_with_underscore},
        )

        labels = [line["label"] for line in result["lines"]]
        assert labels == [label_own], (
            f"quote {quote_with_underscore!r} leaked another quote's BOM line "
            f"via an unescaped LIKE '_' wildcard: {labels}"
        )

    async def test_percent_in_quote_id_does_not_leak_another_quotes_bom_lines(
        self,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """Same defect class as the underscore case, but for `%` (matches any
        sequence of characters, including zero)."""
        ns = await make_namespace()
        suffix = uuid4().hex[:8]
        quote_with_percent = f"QP%{suffix}"  # contains a literal '%'
        quote_collision_victim = f"QPZZZZ{suffix}"  # extra chars where '%' would match
        label_own = f"BOM_LINE:{quote_with_percent.upper()}:AMP01"
        label_victim = f"BOM_LINE:{quote_collision_victim.upper()}:AMP01"

        engine = _make_dealroom_engine_stub(pg_pool)

        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns)
                await _insert_bom_line_content(
                    conn, ns, label_own, quote_with_percent, "AMP01", priced=False
                )
                await _insert_bom_line_content(
                    conn, ns, label_victim, quote_collision_victim, "AMP01", priced=False
                )

        result = await do_open_dealroom(
            engine,
            {"namespace_id": str(ns), "quote_id": quote_with_percent},
        )

        labels = [line["label"] for line in result["lines"]]
        assert labels == [label_own], (
            f"quote {quote_with_percent!r} leaked another quote's BOM line "
            f"via an unescaped LIKE '%' wildcard: {labels}"
        )

    async def test_ordinary_quote_id_still_fetches_its_own_lines_only(
        self,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """No wildcard characters at all -- the common case must be
        completely unaffected by the switch from LIKE to starts_with()."""
        ns = await make_namespace()
        quote_id = f"Q-ORD-{uuid4().hex[:8]}"
        other_quote_id = f"Q-OTHER-{uuid4().hex[:8]}"
        label_a = f"BOM_LINE:{quote_id.upper()}:AMP01"
        label_b = f"BOM_LINE:{quote_id.upper()}:CABLE01"
        label_other = f"BOM_LINE:{other_quote_id.upper()}:AMP01"

        engine = _make_dealroom_engine_stub(pg_pool)

        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns)
                await _insert_bom_line_content(conn, ns, label_a, quote_id, "AMP01", priced=False)
                await _insert_bom_line_content(conn, ns, label_b, quote_id, "CABLE01", priced=False)
                await _insert_bom_line_content(
                    conn, ns, label_other, other_quote_id, "AMP01", priced=False
                )

        result = await do_open_dealroom(
            engine,
            {"namespace_id": str(ns), "quote_id": quote_id},
        )

        labels = sorted(line["label"] for line in result["lines"])
        assert labels == sorted([label_a, label_b])


@pytest.mark.integration
@pytest.mark.asyncio
class TestSalesDealRoomNeverFabricatesAPrice:
    """D51: a missing or unresolvable price is CARRIED, never substituted.

    The defect these guard is not the direction of the error -- a fabricated
    ``100.0`` overstates and a silent ``0.0`` understates, and both hand the
    caller a confident number that is wrong.  The absence must be visible.
    """

    async def test_line_with_no_price_on_record_is_unpriced_not_fabricated(
        self,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        ns = await make_namespace()
        quote_id = f"Q-NOPRICE-{uuid4().hex[:8]}"
        label = f"BOM_LINE:{quote_id.upper()}:LINE-1"
        engine = _make_dealroom_engine_stub(pg_pool)

        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns)
                await _insert_bom_line_content(
                    conn, ns, label, quote_id, "LINE-1", priced=False, unit_price=0.0
                )

        result = await do_open_dealroom(engine, {"namespace_id": str(ns), "quote_id": quote_id})

        line = result["lines"][0]
        assert line["priced"] is False, (
            f"fabricated a price: unit_price={line['unit_price']!r} "
            f"total_price={line['total_price']!r} base_cost={line['base_cost']!r}"
        )
        assert line["unpriced_reason"] == "no_price_on_record"
        assert line["unit_price"] is None, f"fabricated unit price: {line['unit_price']!r}"
        assert line["total_price"] is None, f"fabricated line total: {line['total_price']!r}"
        assert line["base_cost"] is None, f"fabricated cost: {line['base_cost']!r}"
        # ...and the room must not report a confident total that omits it.
        assert result["total_price_nok"] is None, (
            f"room reported a confident total {result['total_price_nok']!r} "
            "while a toggled line carries an unknown amount of money"
        )
        assert result["unpriced_line_count"] == 1

    async def test_an_UNDATED_price_is_no_price_on_record_not_a_resolution_failure(
        self,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """An amount with no ``*_as_of`` is an INCOMPLETE RECORD, not a server fault.

        ``resolve_price``'s tier builders each require the (amount, as_of) PAIR --
        an undated price cannot be judged stale, so it is not a usable tier. This
        guard used to check the amounts alone, so such a record passed, reached
        ``resolve_price``, and came back 'no price tier available', which dealroom
        reported as ``price_resolution_failed`` -- blaming the server for a record
        that was simply incomplete.

        No monkeypatch here on purpose: this exercises the REAL resolver, which is
        the only way the disagreement between the two sites can show up.
        """
        ns = await make_namespace()
        quote_id = f"Q-UNDATED-{uuid4().hex[:8]}"
        label = f"BOM_LINE:{quote_id.upper()}:LINE-1"
        engine = _make_dealroom_engine_stub(pg_pool)

        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns)
                await _insert_bom_line_content(
                    conn, ns, label, quote_id, "LINE-1", priced=True, unit_price=5000.0
                )

        result = await do_open_dealroom(
            engine,
            {
                "namespace_id": str(ns),
                "quote_id": quote_id,
                "product_override": {
                    label: {
                        "base_price": 5000.0,  # amount, NO base_as_of
                    }
                },
            },
        )

        line = result["lines"][0]
        assert line["priced"] is False
        assert line["unpriced_reason"] == "no_price_on_record", (
            "an undated price is an incomplete record, not a server-side resolution "
            f"failure; got {line['unpriced_reason']!r}"
        )
        assert line["unit_price"] is None
        assert line["total_price"] is None

    async def test_line_whose_price_resolution_fails_reports_the_other_reason(
        self,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ns = await make_namespace()
        quote_id = f"Q-RESOLVEFAIL-{uuid4().hex[:8]}"
        label = f"BOM_LINE:{quote_id.upper()}:LINE-1"
        engine = _make_dealroom_engine_stub(pg_pool)

        async def _boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("pricing service unavailable")

        monkeypatch.setattr("nce.vertical_modules.sales.dealroom.resolve_price", _boom)

        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns)
                await _insert_bom_line_content(
                    conn, ns, label, quote_id, "LINE-1", priced=True, unit_price=5000.0
                )

        result = await do_open_dealroom(
            engine,
            {
                "namespace_id": str(ns),
                "quote_id": quote_id,
                "product_override": {
                    label: {
                        "base_price": 5000.0,
                        "base_as_of": datetime.datetime.now(datetime.timezone.utc),
                    }
                },
            },
        )

        line = result["lines"][0]
        assert line["priced"] is False, (
            f"fabricated a price: unit_price={line['unit_price']!r} "
            f"total_price={line['total_price']!r} base_cost={line['base_cost']!r}"
        )
        # A price WAS on record here -- the failure is a different operator
        # problem from 'nothing was ever recorded', and must read differently.
        assert line["unpriced_reason"] == "price_resolution_failed"
        assert line["unit_price"] is None, (
            f"a pricing FAILURE was turned into the number {line['unit_price']!r}"
        )
        assert line["total_price"] is None, f"fabricated line total: {line['total_price']!r}"
        assert result["total_price_nok"] is None
        assert result["unpriced_line_count"] == 1

    async def test_room_total_is_none_when_only_one_of_two_lines_is_unpriced(
        self,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        ns = await make_namespace()
        quote_id = f"Q-MIXED-{uuid4().hex[:8]}"
        priced_label = f"BOM_LINE:{quote_id.upper()}:LINE-1"
        unpriced_label = f"BOM_LINE:{quote_id.upper()}:LINE-2"
        engine = _make_dealroom_engine_stub(pg_pool)

        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns)
                await _insert_bom_line_content(
                    conn,
                    ns,
                    priced_label,
                    quote_id,
                    "LINE-1",
                    priced=True,
                    unit_price=2000.0,
                    line_total=2000.0,
                )
                await _insert_bom_line_content(
                    conn, ns, unpriced_label, quote_id, "LINE-2", priced=False, unit_price=0.0
                )

        result = await do_open_dealroom(engine, {"namespace_id": str(ns), "quote_id": quote_id})

        by_label = {line["label"]: line for line in result["lines"]}
        assert by_label[priced_label]["priced"] is True
        assert abs(by_label[priced_label]["unit_price"] - 2000.0) < 1e-6
        assert by_label[unpriced_label]["priced"] is False, (
            "fabricated a price for the unpriced line: "
            f"unit_price={by_label[unpriced_label]['unit_price']!r} "
            f"total_price={by_label[unpriced_label]['total_price']!r}"
        )
        assert by_label[unpriced_label]["unit_price"] is None
        # The priced line's 2000.0 must NOT be presented as the room total:
        # that would silently absorb the unpriced line as zero.
        assert result["total_price_nok"] is None, (
            f"room total {result['total_price_nok']!r} silently omits an unpriced line"
        )
        assert result["unpriced_line_count"] == 1

    async def test_untoggled_unpriced_line_does_not_void_the_total(
        self,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """An unpriced line toggled OFF contributes no money, so the total over
        the remaining lines is still honest."""
        ns = await make_namespace()
        quote_id = f"Q-OFF-{uuid4().hex[:8]}"
        priced_label = f"BOM_LINE:{quote_id.upper()}:LINE-1"
        unpriced_label = f"BOM_LINE:{quote_id.upper()}:LINE-2"
        engine = _make_dealroom_engine_stub(pg_pool)

        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns)
                await _insert_bom_line_content(
                    conn,
                    ns,
                    priced_label,
                    quote_id,
                    "LINE-1",
                    priced=True,
                    unit_price=2000.0,
                    line_total=2000.0,
                )
                await _insert_bom_line_content(
                    conn, ns, unpriced_label, quote_id, "LINE-2", priced=False, unit_price=0.0
                )

        result = await do_open_dealroom(
            engine,
            {
                "namespace_id": str(ns),
                "quote_id": quote_id,
                "toggled_options": {unpriced_label: False},
            },
        )

        assert result["unpriced_line_count"] == 0
        assert result["total_price_nok"] is not None
        assert abs(result["total_price_nok"] - 2000.0) < 1e-6
