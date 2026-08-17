"""Integration tests for economy/cascade.py — Wave 5 (do_cascade_on_approval).

Validates the Acceptance criteria from Batch_120_Module_8_Wave_5.md
(as scoped by the orchestrator's migration-047 ruling):

  1. The 7 effects apply atomically inside one transaction.
  2. A replay with the same ``approval_id`` is a no-op — no double-post, no
     double-cost (and a replay under the same key with a DIFFERENT cost is
     refused rather than silently applied).
  3. ``economy_bom_actual_costs.actual_cost`` is the write; ``BOM_LINE``
     ``kg_nodes``/``kg_edges`` content (including any ``has_status`` edge) is
     never touched — the cascade writes zero graph rows.
  4. ``sales_signed_baselines.signed_margin_pct`` (marginSignedPct) is
     read-only and byte-for-byte unchanged after the cascade.
  5. An unbalanced posting raises ``UnbalancedPostingsError`` and rolls back
     the WHOLE transaction — including ``actual_cost`` writes already issued
     for earlier lines in the same call (no partial write).
  6. A ``bom_line_label`` with no ``BOM_LINE`` kg_nodes row is refused rather
     than silently creating an orphan cost row.
  7. FORCE RLS isolates ``economy_bom_actual_costs`` per tenant.
  8. Round 2 Fix 1 (money-semantics REJECT, CRITICAL): two DIFFERENT
     approvals against the SAME BOM line SUM rather than overwrite, and a
     replay of either approval — in any order, interleaved — never changes
     the total or inserts a duplicate row. A credit note (negative
     ``actual_cost``) is a legitimate row that reduces the total.
  9. Round 2 Fix 2 (money-semantics MEDIUM): effects 3-7 carry the reference's
     computed content (margin-trinity, supplier/invoice/trigger fields), not
     a bare ``{"postings": None}`` — and never fabricate a margin figure when
     no signed baseline exists.
 10. Round 3 Fix 1 (money-semantics HIGH): the margin-aggregation root
     (``_read_actual_cost_total``) no longer uses an unescaped SQL ``LIKE``
     pattern built from a caller-supplied ``quote_id`` — a quote id
     containing ``_`` or ``%`` used to silently widen the match to a
     DIFFERENT quote's BOM lines. Fixed via a literal ``starts_with()``
     prefix test with no pattern semantics at all.
 11. Round 3 Fix 2 (money-semantics MEDIUM): ``_as_actual_cost`` now
     quantises to øre (``ROUND_HALF_UP``, mirrors ``ngaap.py``'s
     ``_quantise``) at the coercion boundary, so a third-decimal amount is
     rounded to a documented, pinned value by the code — never silently by
     Postgres's ``NUMERIC(18,2)`` column.

Integration tests are ``@pytest.mark.integration`` — require a live Postgres
with schema.sql + migration 047 applied. A handful of plain (unmarked)
pure-logic tests for cascade.py's coercion boundary (``_as_actual_cost`` /
``_as_lines`` / ``_as_ns_uuid``) sit alongside them and need no DB — this is
the "pure-logic coverage standing alone" the gate asks for whenever the
integration tests skip locally (e.g. the documented
``NCE_MASTER_KEY does not decrypt signing_keys`` local-environment condition).

Design notes
------------
- BOM_LINE nodes are seeded directly (bypassing Sales/System Design) so
  these tests don't depend on another engine's write path.
- Unique quote_id per test avoids kg_nodes label collisions in a shared DB.
- Money assertions compare ``Decimal`` values directly — never through
  ``float()`` (see cascade.py / events.py module docstrings for why).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.vertical_modules.economy.cascade import (
    _as_actual_cost,
    _as_lines,
    _as_ns_uuid,
    do_cascade_on_approval,
)
from nce.vertical_modules.economy.events import UnbalancedPostingsError

# ---------------------------------------------------------------------------
# Pure-logic tests: the coercion boundary (no DB — run even when the
# integration tests below skip for a local-environment reason)
# ---------------------------------------------------------------------------


class TestAsActualCost:
    def test_int_becomes_exact_decimal(self) -> None:
        assert _as_actual_cost(1000, "x") == Decimal(1000)

    def test_decimal_passes_through(self) -> None:
        assert _as_actual_cost(Decimal("42.50"), "x") == Decimal("42.50")

    def test_float_goes_through_str_not_binary_expansion(self) -> None:
        """``Decimal(str(0.1))`` == ``Decimal("0.1")``, never the binary-float
        expansion ``Decimal(0.1)`` would capture."""
        assert _as_actual_cost(0.1, "x") == Decimal("0.1")

    def test_bool_is_rejected_even_though_isinstance_int_is_true(self) -> None:
        """``isinstance(True, int)`` is ``True`` in Python — bool must be
        rejected explicitly, before the int branch, or a stray ``True`` would
        be summed as a 1 NOK cost."""
        with pytest.raises(ValueError, match="bool is not a cost amount"):
            _as_actual_cost(True, "x")
        with pytest.raises(ValueError, match="bool is not a cost amount"):
            _as_actual_cost(False, "x")

    def test_nan_float_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be finite"):
            _as_actual_cost(float("nan"), "x")

    def test_infinite_float_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be finite"):
            _as_actual_cost(float("inf"), "x")

    def test_nan_decimal_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be finite"):
            _as_actual_cost(Decimal("nan"), "x")

    def test_string_amount_is_never_parsed(self) -> None:
        """A string amount is an ingest bug, not something to guess at inside
        the write-path that puts the number in the ledger."""
        with pytest.raises(ValueError, match="expected int/float/Decimal"):
            _as_actual_cost("1000", "x")

    def test_none_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="expected int/float/Decimal"):
            _as_actual_cost(None, "x")

    # -- Round 3 Defect 2 (money-semantics MEDIUM): quantise to øre at the
    # boundary, not whatever Postgres's NUMERIC(18,2) column happens to do. --

    def test_third_decimal_quantises_ties_away_from_zero(self) -> None:
        """Pinned to a specific value -- not "whatever Postgres did". Mirrors
        ngaap.py's ROUND_HALF_UP (ties away from zero)."""
        result = _as_actual_cost(Decimal("100.005"), "x")
        assert result == Decimal("100.01")
        assert result.as_tuple().exponent == -2

    def test_third_decimal_rounds_down_below_half(self) -> None:
        result = _as_actual_cost(Decimal("100.004"), "x")
        assert result == Decimal("100.00")
        assert result.as_tuple().exponent == -2

    def test_negative_third_decimal_ties_away_from_zero(self) -> None:
        """Credit notes are legal negative amounts (see the sign-convention
        docstring note) and must quantise the same way as a positive one --
        ROUND_HALF_UP rounds a negative tie away from zero too."""
        result = _as_actual_cost(Decimal("-100.005"), "x")
        assert result == Decimal("-100.01")

    def test_third_decimal_float_quantises_the_same_way(self) -> None:
        """A float input goes through ``Decimal(str(x))`` first (never the
        binary expansion), then the same quantisation applies."""
        result = _as_actual_cost(100.005, "x")
        assert result == Decimal("100.01")

    def test_exact_ore_amount_is_unaffected(self) -> None:
        """An amount already at 2 dp passes through unchanged in value AND
        in scale -- quantising an already-exact amount is a no-op."""
        result = _as_actual_cost(Decimal("42.50"), "x")
        assert result == Decimal("42.50")
        assert result.as_tuple().exponent == -2

    def test_int_amount_is_quantised_to_ore_scale(self) -> None:
        """An int input is exact at scale 0; quantising still normalises it
        to the same 2 dp scale every other amount gets."""
        result = _as_actual_cost(1000, "x")
        assert result == Decimal(1000)
        assert result.as_tuple().exponent == -2


class TestAsLines:
    def test_none_returns_empty_list(self) -> None:
        assert _as_lines(None) == []

    def test_non_list_raises(self) -> None:
        with pytest.raises(ValueError, match="'lines' must be a list"):
            _as_lines({"bom_line_label": "x"})

    def test_non_dict_entry_raises(self) -> None:
        with pytest.raises(ValueError, match=r"lines\[0\] must be an object"):
            _as_lines(["not-a-dict"])

    def test_missing_bom_line_label_raises(self) -> None:
        with pytest.raises(ValueError, match="bom_line_label is required"):
            _as_lines([{"actual_cost": 100}])

    def test_missing_actual_cost_raises(self) -> None:
        with pytest.raises(ValueError, match="actual_cost is required"):
            _as_lines([{"bom_line_label": "BOM_LINE:Q1:L1"}])

    def test_valid_entry_normalises_postings_default_to_none(self) -> None:
        result = _as_lines([{"bom_line_label": "BOM_LINE:Q1:L1", "actual_cost": 100}])
        assert result == [
            {"bom_line_label": "BOM_LINE:Q1:L1", "actual_cost": Decimal(100), "postings": None}
        ]

    def test_valid_entry_preserves_supplied_postings(self) -> None:
        postings = [{"account": "4300", "amount": 10}]
        result = _as_lines(
            [{"bom_line_label": "BOM_LINE:Q1:L1", "actual_cost": 1, "postings": postings}]
        )
        assert result[0]["postings"] is postings


class TestAsNsUuid:
    def test_missing_raises(self) -> None:
        with pytest.raises(ValueError, match="'namespace_id' is required"):
            _as_ns_uuid(None, "namespace_id")
        with pytest.raises(ValueError, match="'namespace_id' is required"):
            _as_ns_uuid("", "namespace_id")

    def test_string_uuid_is_converted(self) -> None:
        raw = "12345678-1234-5678-1234-567812345678"
        assert _as_ns_uuid(raw, "namespace_id") == UUID(raw)

    def test_uuid_passes_through_unchanged(self) -> None:
        u = UUID("12345678-1234-5678-1234-567812345678")
        assert _as_ns_uuid(u, "namespace_id") is u


# ---------------------------------------------------------------------------
# Integration test helpers
# ---------------------------------------------------------------------------


def _make_engine_stub(pg_pool: asyncpg.Pool) -> Any:  # type: ignore[type-arg]
    class _EngineStub:
        pass

    stub = _EngineStub()
    stub.pg_pool = pg_pool  # type: ignore[attr-defined]
    return stub


def _bom_line_label(quote_id: str, line_ref: str) -> str:
    return f"BOM_LINE:{quote_id.upper()}:{line_ref.upper()}"


async def _seed_bom_line(
    pg_pool: asyncpg.Pool,
    ns_uuid: uuid.UUID,
    bom_line_label: str,
) -> None:
    """Seed a bare BOM_LINE kg_nodes row (owned by another engine — inserted
    directly, mirroring test_project_sync_bom_tasks.py's convention)."""
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id)
            VALUES ($1, 'BOM_LINE', $2::uuid)
            ON CONFLICT (label, namespace_id) DO NOTHING
            """,
            bom_line_label,
            str(ns_uuid),
        )


async def _seed_signed_baseline(
    pg_pool: asyncpg.Pool,
    ns_uuid: uuid.UUID,
    quote_id: str,
    signed_margin_pct: Decimal,
    signed_total_nok: Decimal,
) -> None:
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO sales_signed_baselines
                (namespace_id, quote_id, signed_margin_pct, signed_total_nok)
            VALUES ($1::uuid, $2, $3, $4)
            ON CONFLICT (namespace_id, quote_id) DO NOTHING
            """,
            str(ns_uuid),
            quote_id,
            signed_margin_pct,
            signed_total_nok,
        )


async def _fetch_signed_baseline_row(
    pg_pool: asyncpg.Pool,
    ns_uuid: uuid.UUID,
    quote_id: str,
) -> asyncpg.Record:  # type: ignore[type-arg]
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT quote_id, signed_margin_pct, signed_total_nok, signed_at
            FROM sales_signed_baselines
            WHERE namespace_id = $1::uuid AND quote_id = $2
            """,
            str(ns_uuid),
            quote_id,
        )
    assert row is not None
    return row


async def _fetch_cost_row(
    pg_pool: asyncpg.Pool,
    ns_uuid: uuid.UUID,
    bom_line_label: str,
) -> asyncpg.Record | None:  # type: ignore[type-arg]
    async with pg_pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT actual_cost, source_approval_id
            FROM economy_bom_actual_costs
            WHERE namespace_id = $1::uuid AND bom_line_label = $2
            """,
            str(ns_uuid),
            bom_line_label,
        )


async def _count_cost_rows(
    pg_pool: asyncpg.Pool,
    ns_uuid: uuid.UUID,
    bom_line_label: str,
) -> int:
    async with pg_pool.acquire() as conn:
        count = await conn.fetchval(
            """
            SELECT COUNT(*) FROM economy_bom_actual_costs
            WHERE namespace_id = $1::uuid AND bom_line_label = $2
            """,
            str(ns_uuid),
            bom_line_label,
        )
    return int(count)


async def _sum_actual_cost(
    pg_pool: asyncpg.Pool,
    ns_uuid: uuid.UUID,
    bom_line_label: str,
) -> Decimal:
    """Round 2 (Fix 1): a line's actual cost is one row per (line, approval),
    never a single scalar -- this is the grouped read the cascade itself uses
    (see ``cascade.py``'s ``_read_actual_cost_total``, aggregated per-quote;
    this helper aggregates per-line for test assertions)."""
    async with pg_pool.acquire() as conn:
        total = await conn.fetchval(
            """
            SELECT COALESCE(SUM(actual_cost), 0) FROM economy_bom_actual_costs
            WHERE namespace_id = $1::uuid AND bom_line_label = $2
            """,
            str(ns_uuid),
            bom_line_label,
        )
    return total if isinstance(total, Decimal) else Decimal(total)


async def _fetch_bom_line_entity_type(
    pg_pool: asyncpg.Pool,
    ns_uuid: uuid.UUID,
    bom_line_label: str,
) -> str | None:
    async with pg_pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT entity_type FROM kg_nodes WHERE label = $1 AND namespace_id = $2::uuid",
            bom_line_label,
            str(ns_uuid),
        )


async def _count_has_status_edges(
    pg_pool: asyncpg.Pool,
    ns_uuid: uuid.UUID,
    bom_line_label: str,
) -> int:
    async with pg_pool.acquire() as conn:
        count = await conn.fetchval(
            """
            SELECT COUNT(*) FROM kg_edges
            WHERE subject_label = $1 AND predicate = 'has_status' AND namespace_id = $2::uuid
            """,
            bom_line_label,
            str(ns_uuid),
        )
    return int(count)


async def _count_any_kg_edges_from(
    pg_pool: asyncpg.Pool,
    ns_uuid: uuid.UUID,
    bom_line_label: str,
) -> int:
    """Total kg_edges rows with *bom_line_label* as subject — proves the
    cascade wrote NO edges of any kind from the line (not just no has_status)."""
    async with pg_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM kg_edges WHERE subject_label = $1 AND namespace_id = $2::uuid",
            bom_line_label,
            str(ns_uuid),
        )
    return int(count)


# ---------------------------------------------------------------------------
# 1. The 7 effects apply atomically; actual_cost is written
# ---------------------------------------------------------------------------


_SEVEN_EFFECT_TYPES: frozenset[str] = frozenset(
    {
        "economy.invoice.approved",
        "economy.bom_line.cost_updated",
        "economy.project.margin_recalculated",
        "economy.project.supplier_scorecard_updated",
        "economy.project.kickback_accrued",
        "economy.project.delivery_recalculated",
        "economy.project.cashflow_reprojected",
    }
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_seven_effects_apply_atomically_and_actual_cost_is_written(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """One line -> exactly 7 effect events, one of each of the 7 canonical
    kinds (docs/08-economy-engine.md B2 / the reference's own 7-effect
    numbering)."""
    quote_id = f"B120-T1-{uuid.uuid4().hex[:8]}"
    label = _bom_line_label(quote_id, "AMP01")
    await _seed_bom_line(pg_pool, namespace_id, label)
    await _seed_signed_baseline(
        pg_pool, namespace_id, quote_id, Decimal("0.35"), Decimal("100000.00")
    )
    engine = _make_engine_stub(pg_pool)

    result = await do_cascade_on_approval(
        engine,
        {
            "namespace_id": namespace_id,
            "approval_id": f"approval-{uuid.uuid4().hex[:8]}",
            "quote_id": quote_id,
            "project_id": f"PROJECT:{quote_id}",
            "invoice_id": "INV-2026-0042",
            "invoice_postings": [
                {"account": "4300", "amount": Decimal("1500.00")},
                {"account": "2400", "amount": Decimal("-1500.00")},
            ],
            "lines": [{"bom_line_label": label, "actual_cost": Decimal("999.50")}],
        },
    )

    assert result["ok"] is True, result
    assert len(result["effects"]) == 7, (
        f"expected exactly 7 effects for a single-line call, "
        f"got {len(result['effects'])}: {[e['type'] for e in result['effects']]}"
    )
    assert {e["type"] for e in result["effects"]} == _SEVEN_EFFECT_TYPES
    assert result["bom_lines_written"] == [label]
    assert result["bom_lines_replayed"] == []
    assert result["signed_margin_pct"] == Decimal("0.35")

    row = await _fetch_cost_row(pg_pool, namespace_id, label)
    assert row is not None and row["actual_cost"] == Decimal("999.50")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cost_updated_effect_and_write_fire_once_per_line(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """N lines -> N ``economy.bom_line.cost_updated`` effects (one per line)
    plus the 6 call-level effects (invoice-approved + the 5 per-project
    snapshots) = N + 6 total, and N actual_cost rows are written — the
    "7 effects" in the wave's acceptance text names 7 effect KINDS, not a
    fixed total event count independent of how many BOM lines a call
    touches (the reference's own "bom.line.cost.updated -- per matched
    line")."""
    quote_id = f"B120-T1B-{uuid.uuid4().hex[:8]}"
    label_a = _bom_line_label(quote_id, "AMP01")
    label_b = _bom_line_label(quote_id, "CABLE01")
    await _seed_bom_line(pg_pool, namespace_id, label_a)
    await _seed_bom_line(pg_pool, namespace_id, label_b)
    engine = _make_engine_stub(pg_pool)

    result = await do_cascade_on_approval(
        engine,
        {
            "namespace_id": namespace_id,
            "approval_id": f"approval-{uuid.uuid4().hex[:8]}",
            "quote_id": quote_id,
            "lines": [
                {"bom_line_label": label_a, "actual_cost": Decimal("999.50")},
                {"bom_line_label": label_b, "actual_cost": Decimal("42.00")},
            ],
        },
    )

    assert result["ok"] is True, result
    assert len(result["effects"]) == 2 + 6, (
        f"2 lines -> 2 cost-updated + 6 call-level effects, "
        f"got {len(result['effects'])}: {[e['type'] for e in result['effects']]}"
    )
    cost_effects = [e for e in result["effects"] if e["type"] == "economy.bom_line.cost_updated"]
    assert len(cost_effects) == 2
    assert {e["bom_line_label"] for e in cost_effects} == {label_a, label_b}
    assert result["bom_lines_written"] == [label_a, label_b]

    row_a = await _fetch_cost_row(pg_pool, namespace_id, label_a)
    row_b = await _fetch_cost_row(pg_pool, namespace_id, label_b)
    assert row_a is not None and row_a["actual_cost"] == Decimal("999.50")
    assert row_b is not None and row_b["actual_cost"] == Decimal("42.00")


# ---------------------------------------------------------------------------
# 1b. Round 3 Fix 2 (money-semantics MEDIUM): a third-decimal actual_cost is
# quantised to a documented, pinned value by cascade.py -- not "whatever
# Postgres's NUMERIC(18,2) column happened to round it to". End-to-end
# proof: the row actually stored in the DB after the cascade matches the
# code's quantised value.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_third_decimal_actual_cost_is_quantised_before_it_reaches_the_db(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Round 2 shipped ``actual_cost NUMERIC(18,2)`` with nothing quantising
    in code, so ``100.005`` reached Postgres un-rounded and the column
    silently rounded it on write with no signal. This proves the STORED row
    already carries the code's own quantised value (ROUND_HALF_UP, ties away
    from zero -- same as ``ngaap.py``), not a value the DB corrected."""
    quote_id = f"B120R3-QUANT-{uuid.uuid4().hex[:8]}"
    label = _bom_line_label(quote_id, "AMP01")
    await _seed_bom_line(pg_pool, namespace_id, label)
    engine = _make_engine_stub(pg_pool)

    result = await do_cascade_on_approval(
        engine,
        {
            "namespace_id": namespace_id,
            "approval_id": f"approval-{uuid.uuid4().hex[:8]}",
            "quote_id": quote_id,
            "lines": [{"bom_line_label": label, "actual_cost": Decimal("100.005")}],
        },
    )

    cost_effect = next(e for e in result["effects"] if e["type"] == "economy.bom_line.cost_updated")
    assert cost_effect["actual_cost"] == Decimal("100.01")

    row = await _fetch_cost_row(pg_pool, namespace_id, label)
    assert row is not None and row["actual_cost"] == Decimal("100.01")


# ---------------------------------------------------------------------------
# 2. Idempotency — replay with same key is a no-op; different cost is refused
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_with_same_approval_id_is_a_no_op(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Removing the exact-key-replay guard in cascade.py (the `continue` branch
    in do_cascade_on_approval) would make this test fail: the second call
    would re-UPSERT and appear in `bom_lines_written` instead of
    `bom_lines_replayed`, and — if the UPSERT ever became additive instead of
    a SET — the stored actual_cost would double."""
    quote_id = f"B120-T2-{uuid.uuid4().hex[:8]}"
    label = _bom_line_label(quote_id, "SPEAKER01")
    await _seed_bom_line(pg_pool, namespace_id, label)
    approval_id = f"approval-{uuid.uuid4().hex[:8]}"
    params = {
        "namespace_id": namespace_id,
        "approval_id": approval_id,
        "quote_id": quote_id,
        "lines": [{"bom_line_label": label, "actual_cost": Decimal("1000.00")}],
    }
    engine = _make_engine_stub(pg_pool)

    r1 = await do_cascade_on_approval(engine, params)
    assert r1["ok"] is True, r1
    assert r1["bom_lines_written"] == [label]
    assert r1["bom_lines_replayed"] == []

    r2 = await do_cascade_on_approval(engine, params)
    assert r2["ok"] is True, r2
    assert r2["bom_lines_written"] == [], "replay must not re-write actual_cost"
    assert r2["bom_lines_replayed"] == [label]

    # Exactly one row, and the cost did NOT double.
    assert await _count_cost_rows(pg_pool, namespace_id, label) == 1
    row = await _fetch_cost_row(pg_pool, namespace_id, label)
    assert row is not None
    assert row["actual_cost"] == Decimal("1000.00"), (
        f"replay must not accumulate cost; got {row['actual_cost']}"
    )
    assert row["source_approval_id"] == approval_id

    # Effect content hash is deterministic across the two calls (same input,
    # same normalised/hashed event — proves the replay produced no drift).
    cost_effect_1 = next(e for e in r1["effects"] if e["type"] == "economy.bom_line.cost_updated")
    cost_effect_2 = next(e for e in r2["effects"] if e["type"] == "economy.bom_line.cost_updated")
    assert cost_effect_1["hash"] == cost_effect_2["hash"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_with_same_key_different_cost_is_refused(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    quote_id = f"B120-T3-{uuid.uuid4().hex[:8]}"
    label = _bom_line_label(quote_id, "SWITCH01")
    await _seed_bom_line(pg_pool, namespace_id, label)
    approval_id = f"approval-{uuid.uuid4().hex[:8]}"
    engine = _make_engine_stub(pg_pool)

    await do_cascade_on_approval(
        engine,
        {
            "namespace_id": namespace_id,
            "approval_id": approval_id,
            "quote_id": quote_id,
            "lines": [{"bom_line_label": label, "actual_cost": Decimal("1000.00")}],
        },
    )

    with pytest.raises(ValueError, match="refusing to apply a different actual_cost"):
        await do_cascade_on_approval(
            engine,
            {
                "namespace_id": namespace_id,
                "approval_id": approval_id,
                "quote_id": quote_id,
                "lines": [{"bom_line_label": label, "actual_cost": Decimal("2000.00")}],
            },
        )

    # The refused replay must not have applied the new value.
    row = await _fetch_cost_row(pg_pool, namespace_id, label)
    assert row is not None and row["actual_cost"] == Decimal("1000.00")


# ---------------------------------------------------------------------------
# 2b. Round 2 Fix 1 (money-semantics REJECT, CRITICAL) — two DIFFERENT
# approvals against the SAME BOM line must SUM, never overwrite. Round 1's
# `ON CONFLICT ... DO UPDATE SET actual_cost = EXCLUDED.actual_cost` silently
# replaced approval A's cost (60 000,00) with approval B's (40 000,00),
# leaving the row at 40 000,00 instead of 100 000,00. Fixed by a natural key
# of (namespace_id, bom_line_label, source_approval_id) with `INSERT ... ON
# CONFLICT DO NOTHING` -- one row per (line, approval); the line's cost is
# SUM(actual_cost), never a single stored scalar.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_different_approvals_on_same_line_sum_and_replays_in_any_order_are_noops(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Reproduces the exact live scenario from the money-semantics audit:
    approval A (60 000,00) then approval B (40 000,00) against the same BOM
    line must total 100 000,00 -- partial delivery / split invoicing against
    one BOM line is ordinary in this domain, not an error. Then replays of
    EITHER approval, in SEVERAL different orders (A, B, replay-A, replay-B,
    replay-B-again, replay-A-again), must leave the total unchanged and must
    never insert a new row -- idempotency by constraint, not by a guard that
    has to stay correct across interleaving."""
    quote_id = f"B120-R2F1-{uuid.uuid4().hex[:8]}"
    label = _bom_line_label(quote_id, "AMP02")
    await _seed_bom_line(pg_pool, namespace_id, label)
    engine = _make_engine_stub(pg_pool)
    approval_a = f"approval-A-{uuid.uuid4().hex[:8]}"
    approval_b = f"approval-B-{uuid.uuid4().hex[:8]}"

    def _params(approval_id: str, cost: str) -> dict[str, Any]:
        return {
            "namespace_id": namespace_id,
            "approval_id": approval_id,
            "quote_id": quote_id,
            "lines": [{"bom_line_label": label, "actual_cost": Decimal(cost)}],
        }

    r_a = await do_cascade_on_approval(engine, _params(approval_a, "60000.00"))
    assert r_a["bom_lines_written"] == [label]
    r_b = await do_cascade_on_approval(engine, _params(approval_b, "40000.00"))
    assert r_b["bom_lines_written"] == [label]

    assert await _sum_actual_cost(pg_pool, namespace_id, label) == Decimal("100000.00"), (
        "two DIFFERENT approvals against the same line must SUM, not overwrite "
        "(round-1 regression: this used to land at 40000.00)"
    )
    assert await _count_cost_rows(pg_pool, namespace_id, label) == 2

    # Interleaved replays in several orders: A, B, then replay-B, replay-A,
    # replay-B-again, replay-A-again -- the total must never move.
    for approval_id, cost in [
        (approval_b, "40000.00"),
        (approval_a, "60000.00"),
        (approval_b, "40000.00"),
        (approval_a, "60000.00"),
    ]:
        result = await do_cascade_on_approval(engine, _params(approval_id, cost))
        assert result["bom_lines_written"] == [], "a replay must never re-insert a row"
        assert result["bom_lines_replayed"] == [label]
        assert await _sum_actual_cost(pg_pool, namespace_id, label) == Decimal("100000.00"), (
            "replaying either approval, in any order, must not change the total"
        )

    assert await _count_cost_rows(pg_pool, namespace_id, label) == 2, (
        "still exactly 2 rows -- one per approval, no duplicates from any replay"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_credit_note_negative_actual_cost_reduces_total(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """A supplier credit note is a legitimate NEGATIVE actual_cost row -- one
    more (line, approval) row, summed like any other. `_as_actual_cost` has
    no sign check and none should be added (see cascade.py's sign-convention
    docstring note)."""
    quote_id = f"B120-R2F1C-{uuid.uuid4().hex[:8]}"
    label = _bom_line_label(quote_id, "AMP03")
    await _seed_bom_line(pg_pool, namespace_id, label)
    engine = _make_engine_stub(pg_pool)

    await do_cascade_on_approval(
        engine,
        {
            "namespace_id": namespace_id,
            "approval_id": f"approval-orig-{uuid.uuid4().hex[:8]}",
            "quote_id": quote_id,
            "lines": [{"bom_line_label": label, "actual_cost": Decimal("100000.00")}],
        },
    )
    await do_cascade_on_approval(
        engine,
        {
            "namespace_id": namespace_id,
            "approval_id": f"approval-credit-{uuid.uuid4().hex[:8]}",
            "quote_id": quote_id,
            "lines": [{"bom_line_label": label, "actual_cost": Decimal("-15000.00")}],
        },
    )

    assert await _sum_actual_cost(pg_pool, namespace_id, label) == Decimal("85000.00")
    assert await _count_cost_rows(pg_pool, namespace_id, label) == 2


# ---------------------------------------------------------------------------
# 2c. Round 3 Fix 1 (money-semantics HIGH): the margin-aggregation root used
# an unescaped SQL LIKE pattern built from a caller-supplied quote_id. `_`
# and `%` are LIKE metacharacters -- a quote id containing either would
# silently widen the match to a DIFFERENT quote's BOM lines, summing another
# quote's cost into this quote's actual_cost_total / margin figures. Fixed by
# a literal `starts_with()` prefix test (no pattern semantics at all) in
# `_read_actual_cost_total`.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_underscore_in_quote_id_does_not_aggregate_another_quotes_lines(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Reproduces the exact live scenario from the round-3 audit:
    ``'BOM_LINE:QA1:AMP01' LIKE 'BOM_LINE:Q_1:%'`` is true under raw LIKE
    because `_` matches any single character. Seeds two quotes whose ids
    differ only at the position an unescaped `_` would wildcard-match, and
    asserts the quote WHOSE OWN id contains the underscore aggregates ONLY
    its own line -- not the other quote's cost folded in via the wildcard."""
    suffix = uuid.uuid4().hex[:8]
    quote_with_underscore = f"QU_{suffix}"  # contains a literal '_'
    quote_collision_victim = f"QUZ{suffix}"  # same length, 'Z' where '_' falls
    label_own = _bom_line_label(quote_with_underscore, "AMP01")
    label_victim = _bom_line_label(quote_collision_victim, "AMP01")
    await _seed_bom_line(pg_pool, namespace_id, label_own)
    await _seed_bom_line(pg_pool, namespace_id, label_victim)
    engine = _make_engine_stub(pg_pool)

    await do_cascade_on_approval(
        engine,
        {
            "namespace_id": namespace_id,
            "approval_id": f"approval-own-{uuid.uuid4().hex[:8]}",
            "quote_id": quote_with_underscore,
            "lines": [{"bom_line_label": label_own, "actual_cost": Decimal("111.00")}],
        },
    )
    await do_cascade_on_approval(
        engine,
        {
            "namespace_id": namespace_id,
            "approval_id": f"approval-victim-{uuid.uuid4().hex[:8]}",
            "quote_id": quote_collision_victim,
            "lines": [{"bom_line_label": label_victim, "actual_cost": Decimal("222.00")}],
        },
    )

    # A fresh call for quote_with_underscore (no new lines -- just re-reads
    # the aggregation) must total ONLY its own 111.00, never 333.00 (its own
    # line plus the victim's, which the unescaped-LIKE bug would have
    # produced).
    result = await do_cascade_on_approval(
        engine,
        {
            "namespace_id": namespace_id,
            "approval_id": f"approval-reread-{uuid.uuid4().hex[:8]}",
            "quote_id": quote_with_underscore,
            "lines": [],
        },
    )
    margin_effect = next(
        e for e in result["effects"] if e["type"] == "economy.project.margin_recalculated"
    )
    assert margin_effect["actual_cost_total"] == Decimal("111.00"), (
        f"quote {quote_with_underscore!r} picked up another quote's cost via an "
        f"unescaped LIKE '_' wildcard: {margin_effect['actual_cost_total']}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_percent_in_quote_id_does_not_aggregate_another_quotes_lines(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Same defect class as the underscore case, but for `%` (matches any
    sequence of characters, including zero). A quote id containing `%` must
    not widen the aggregation to swallow a differently-named quote's BOM
    lines."""
    suffix = uuid.uuid4().hex[:8]
    quote_with_percent = f"QP%{suffix}"  # contains a literal '%'
    quote_collision_victim = f"QPZZZZ{suffix}"  # extra chars where '%' would match
    label_own = _bom_line_label(quote_with_percent, "AMP01")
    label_victim = _bom_line_label(quote_collision_victim, "AMP01")
    await _seed_bom_line(pg_pool, namespace_id, label_own)
    await _seed_bom_line(pg_pool, namespace_id, label_victim)
    engine = _make_engine_stub(pg_pool)

    await do_cascade_on_approval(
        engine,
        {
            "namespace_id": namespace_id,
            "approval_id": f"approval-own-{uuid.uuid4().hex[:8]}",
            "quote_id": quote_with_percent,
            "lines": [{"bom_line_label": label_own, "actual_cost": Decimal("333.00")}],
        },
    )
    await do_cascade_on_approval(
        engine,
        {
            "namespace_id": namespace_id,
            "approval_id": f"approval-victim-{uuid.uuid4().hex[:8]}",
            "quote_id": quote_collision_victim,
            "lines": [{"bom_line_label": label_victim, "actual_cost": Decimal("444.00")}],
        },
    )

    result = await do_cascade_on_approval(
        engine,
        {
            "namespace_id": namespace_id,
            "approval_id": f"approval-reread-{uuid.uuid4().hex[:8]}",
            "quote_id": quote_with_percent,
            "lines": [],
        },
    )
    margin_effect = next(
        e for e in result["effects"] if e["type"] == "economy.project.margin_recalculated"
    )
    assert margin_effect["actual_cost_total"] == Decimal("333.00"), (
        f"quote {quote_with_percent!r} picked up another quote's cost via an "
        f"unescaped LIKE '%' wildcard: {margin_effect['actual_cost_total']}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ordinary_quote_id_still_aggregates_correctly(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """No wildcard characters at all -- the common case must be completely
    unaffected by the switch from LIKE to starts_with()."""
    quote_id = f"B120R3-ORDINARY-{uuid.uuid4().hex[:8]}"
    label_a = _bom_line_label(quote_id, "AMP01")
    label_b = _bom_line_label(quote_id, "CABLE01")
    await _seed_bom_line(pg_pool, namespace_id, label_a)
    await _seed_bom_line(pg_pool, namespace_id, label_b)
    engine = _make_engine_stub(pg_pool)

    await do_cascade_on_approval(
        engine,
        {
            "namespace_id": namespace_id,
            "approval_id": f"approval-{uuid.uuid4().hex[:8]}",
            "quote_id": quote_id,
            "lines": [{"bom_line_label": label_a, "actual_cost": Decimal("100.00")}],
        },
    )
    result = await do_cascade_on_approval(
        engine,
        {
            "namespace_id": namespace_id,
            "approval_id": f"approval-{uuid.uuid4().hex[:8]}",
            "quote_id": quote_id,
            "lines": [{"bom_line_label": label_b, "actual_cost": Decimal("50.00")}],
        },
    )
    margin_effect = next(
        e for e in result["effects"] if e["type"] == "economy.project.margin_recalculated"
    )
    assert margin_effect["actual_cost_total"] == Decimal("150.00")


# ---------------------------------------------------------------------------
# 3. BOM_LINE.status (and all other BOM_LINE content) is never touched
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bom_line_status_and_content_are_never_written(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Proves the §9.1 audit-bug guard: if do_cascade_on_approval ever wrote a
    `has_status` edge or mutated the BOM_LINE node's entity_type (the way
    Andreas's reference advances `orderStatusEnum`), this test goes red."""
    quote_id = f"B120-T4-{uuid.uuid4().hex[:8]}"
    label = _bom_line_label(quote_id, "RACK01")
    await _seed_bom_line(pg_pool, namespace_id, label)
    engine = _make_engine_stub(pg_pool)

    et_before = await _fetch_bom_line_entity_type(pg_pool, namespace_id, label)
    edges_before = await _count_any_kg_edges_from(pg_pool, namespace_id, label)
    assert et_before == "BOM_LINE"
    assert edges_before == 0

    await do_cascade_on_approval(
        engine,
        {
            "namespace_id": namespace_id,
            "approval_id": f"approval-{uuid.uuid4().hex[:8]}",
            "quote_id": quote_id,
            "lines": [{"bom_line_label": label, "actual_cost": Decimal("500.00")}],
        },
    )

    et_after = await _fetch_bom_line_entity_type(pg_pool, namespace_id, label)
    edges_after = await _count_any_kg_edges_from(pg_pool, namespace_id, label)
    has_status_after = await _count_has_status_edges(pg_pool, namespace_id, label)

    assert et_after == "BOM_LINE", f"cascade wrote BOM_LINE entity_type: {et_after!r}"
    assert edges_after == 0, f"cascade wrote kg_edges from the BOM_LINE: {edges_after}"
    assert has_status_after == 0, "cascade must never write a has_status edge"


# ---------------------------------------------------------------------------
# 4. marginSignedPct is read-only and byte-for-byte unchanged
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_signed_margin_pct_is_read_only_and_never_overwritten(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """If cascade.py ever gained a write against sales_signed_baselines, this
    test's before/after row comparison would catch it (signed_at especially —
    any UPDATE bumps no column here, but an errant re-INSERT or UPDATE of
    signed_margin_pct/signed_total_nok would fail the equality assertions)."""
    quote_id = f"B120-T5-{uuid.uuid4().hex[:8]}"
    label = _bom_line_label(quote_id, "PROJECTOR01")
    await _seed_bom_line(pg_pool, namespace_id, label)
    await _seed_signed_baseline(
        pg_pool, namespace_id, quote_id, Decimal("0.4125"), Decimal("250000.00")
    )
    engine = _make_engine_stub(pg_pool)

    row_before = await _fetch_signed_baseline_row(pg_pool, namespace_id, quote_id)

    result = await do_cascade_on_approval(
        engine,
        {
            "namespace_id": namespace_id,
            "approval_id": f"approval-{uuid.uuid4().hex[:8]}",
            "quote_id": quote_id,
            "lines": [{"bom_line_label": label, "actual_cost": Decimal("777.77")}],
        },
    )

    row_after = await _fetch_signed_baseline_row(pg_pool, namespace_id, quote_id)

    assert result["signed_margin_pct"] == Decimal("0.4125")
    assert row_after["quote_id"] == row_before["quote_id"]
    assert row_after["signed_margin_pct"] == row_before["signed_margin_pct"]
    assert row_after["signed_total_nok"] == row_before["signed_total_nok"]
    assert row_after["signed_at"] == row_before["signed_at"]


# ---------------------------------------------------------------------------
# 5. An unbalanced posting rolls back the WHOLE transaction — no partial write
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unbalanced_line_posting_rolls_back_whole_transaction_no_partial_write(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    quote_id = f"B120-T6B-{uuid.uuid4().hex[:8]}"
    label_a = _bom_line_label(quote_id, "AMP03")
    label_b = _bom_line_label(quote_id, "CABLE03")
    await _seed_bom_line(pg_pool, namespace_id, label_a)
    await _seed_bom_line(pg_pool, namespace_id, label_b)
    engine = _make_engine_stub(pg_pool)

    with pytest.raises(UnbalancedPostingsError):
        await do_cascade_on_approval(
            engine,
            {
                "namespace_id": namespace_id,
                "approval_id": f"approval-{uuid.uuid4().hex[:8]}",
                "quote_id": quote_id,
                "lines": [
                    {"bom_line_label": label_a, "actual_cost": Decimal("100.00")},
                    {
                        "bom_line_label": label_b,
                        "actual_cost": Decimal("200.00"),
                        # Single leg, does not sum to zero -> raises.
                        "postings": [{"account": "4300", "amount": Decimal("50.00")}],
                    },
                ],
            },
        )

    assert await _count_cost_rows(pg_pool, namespace_id, label_a) == 0, (
        "line_a's actual_cost UPSERT must have been rolled back with the rest of the transaction"
    )
    assert await _count_cost_rows(pg_pool, namespace_id, label_b) == 0


# ---------------------------------------------------------------------------
# 6. A bom_line_label with no BOM_LINE node is refused (no orphan cost row)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unknown_bom_line_label_is_refused(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    quote_id = f"B120-T7-{uuid.uuid4().hex[:8]}"
    missing_label = _bom_line_label(quote_id, "GHOST01")
    engine = _make_engine_stub(pg_pool)

    with pytest.raises(ValueError, match="no BOM_LINE node found"):
        await do_cascade_on_approval(
            engine,
            {
                "namespace_id": namespace_id,
                "approval_id": f"approval-{uuid.uuid4().hex[:8]}",
                "quote_id": quote_id,
                "lines": [{"bom_line_label": missing_label, "actual_cost": Decimal("1.00")}],
            },
        )

    assert await _count_cost_rows(pg_pool, namespace_id, missing_label) == 0


# ---------------------------------------------------------------------------
# 7. FORCE RLS isolates economy_bom_actual_costs per tenant
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rls_isolates_actual_cost_rows_between_namespaces(
    pg_pool: asyncpg.Pool,
    pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    quote_id = f"B120-T8-{uuid.uuid4().hex[:8]}"
    label = _bom_line_label(quote_id, "SEALED01")
    await _seed_bom_line(pg_pool, ns_a, label)
    engine = _make_engine_stub(pg_pool)

    await do_cascade_on_approval(
        engine,
        {
            "namespace_id": ns_a,
            "approval_id": f"approval-{uuid.uuid4().hex[:8]}",
            "quote_id": quote_id,
            "lines": [{"bom_line_label": label, "actual_cost": Decimal("321.00")}],
        },
    )

    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_b)
        visible_from_b = await pg_app_conn.fetchval(
            "SELECT COUNT(*) FROM economy_bom_actual_costs WHERE bom_line_label = $1",
            label,
        )
    assert visible_from_b == 0, "ns_b must not see ns_a's actual_cost row"

    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        visible_from_a = await pg_app_conn.fetchval(
            "SELECT COUNT(*) FROM economy_bom_actual_costs WHERE bom_line_label = $1",
            label,
        )
    assert visible_from_a == 1, "ns_a must see its own actual_cost row"


# ---------------------------------------------------------------------------
# 8. Round 2 Fix 2 (money-semantics MEDIUM finding) — effects 3-7 must carry
# real computed content (the reference computes real payloads for the SAME
# 5 events), not a bare `{"postings": None}`. The margin-trinity snapshot is
# derived from sales_signed_baselines (read-only) + the newly-summed
# actual_cost -- event payload, no projection table (Wave 6 still owns that).
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_margin_recalculated_effect_carries_computed_margin_trinity(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """signed (Sales-frozen baseline) vs actual (this call's own summed
    actual_cost) vs their variance -- all three legs of the trinity, computed
    from real inputs, never fabricated."""
    quote_id = f"B120-R2F2-{uuid.uuid4().hex[:8]}"
    label = _bom_line_label(quote_id, "AMP04")
    await _seed_bom_line(pg_pool, namespace_id, label)
    await _seed_signed_baseline(
        pg_pool, namespace_id, quote_id, Decimal("0.30"), Decimal("100000.00")
    )
    engine = _make_engine_stub(pg_pool)

    result = await do_cascade_on_approval(
        engine,
        {
            "namespace_id": namespace_id,
            "approval_id": f"approval-{uuid.uuid4().hex[:8]}",
            "quote_id": quote_id,
            "project_id": f"PROJECT:{quote_id}",
            "lines": [{"bom_line_label": label, "actual_cost": Decimal("60000.00")}],
        },
    )

    margin_effect = next(
        e for e in result["effects"] if e["type"] == "economy.project.margin_recalculated"
    )
    assert margin_effect["signed_margin_pct"] == Decimal("0.30")
    assert margin_effect["signed_margin_amount"] == Decimal("30000.00")
    assert margin_effect["actual_cost_total"] == Decimal("60000.00")
    # actual margin = revenue (100000.00) - actual_cost (60000.00) = 40000.00 -> 40%
    assert margin_effect["actual_margin_amount"] == Decimal("40000.00")
    assert margin_effect["actual_margin_pct"] == Decimal("40000.00") / Decimal("100000.00")
    assert margin_effect["margin_variance_amount"] == Decimal("10000.00")
    assert margin_effect["trigger"] == "economy.invoice.approved"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_margin_recalculated_effect_never_fabricates_margin_without_a_baseline(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """No sales_signed_baselines row for this quote -> every margin figure
    that DEPENDS on the baseline is None, never a fabricated zero (this repo
    has shipped a fabricated-baseline money bug before). actual_cost_total
    needs no baseline to compute and is still reported."""
    quote_id = f"B120-R2F2B-{uuid.uuid4().hex[:8]}"
    label = _bom_line_label(quote_id, "AMP05")
    await _seed_bom_line(pg_pool, namespace_id, label)
    engine = _make_engine_stub(pg_pool)

    result = await do_cascade_on_approval(
        engine,
        {
            "namespace_id": namespace_id,
            "approval_id": f"approval-{uuid.uuid4().hex[:8]}",
            "quote_id": quote_id,
            "lines": [{"bom_line_label": label, "actual_cost": Decimal("5000.00")}],
        },
    )

    margin_effect = next(
        e for e in result["effects"] if e["type"] == "economy.project.margin_recalculated"
    )
    assert margin_effect["signed_margin_pct"] is None
    assert margin_effect["signed_margin_amount"] is None
    assert margin_effect["actual_margin_pct"] is None
    assert margin_effect["actual_margin_amount"] is None
    assert margin_effect["margin_variance_pct"] is None
    assert margin_effect["margin_variance_amount"] is None
    assert margin_effect["actual_cost_total"] == Decimal("5000.00")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scorecard_kickback_delivery_cashflow_effects_carry_reference_fields(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """The reference's supplier/invoice/trigger fields for the same 4 events,
    sourced from the call's own inputs (supplier_id / invoice_id /
    invoice_amount) -- never fabricated, never read from a table this
    cascade does not own."""
    quote_id = f"B120-R2F2C-{uuid.uuid4().hex[:8]}"
    label = _bom_line_label(quote_id, "AMP06")
    await _seed_bom_line(pg_pool, namespace_id, label)
    engine = _make_engine_stub(pg_pool)

    result = await do_cascade_on_approval(
        engine,
        {
            "namespace_id": namespace_id,
            "approval_id": f"approval-{uuid.uuid4().hex[:8]}",
            "quote_id": quote_id,
            "invoice_id": "INV-2026-0099",
            "supplier_id": "SUPPLIER:ACME",
            "invoice_amount": Decimal("12345.67"),
            "lines": [{"bom_line_label": label, "actual_cost": Decimal("1000.00")}],
        },
    )

    by_type = {e["type"]: e for e in result["effects"]}

    scorecard = by_type["economy.project.supplier_scorecard_updated"]
    assert scorecard["supplier_id"] == "SUPPLIER:ACME"
    assert scorecard["invoice_id"] == "INV-2026-0099"

    kickback = by_type["economy.project.kickback_accrued"]
    assert kickback["supplier_id"] == "SUPPLIER:ACME"
    assert kickback["invoice_amount"] == Decimal("12345.67")

    delivery = by_type["economy.project.delivery_recalculated"]
    assert delivery["trigger"] == "economy.invoice.approved"

    cashflow = by_type["economy.project.cashflow_reprojected"]
    assert cashflow["trigger"] == "economy.invoice.approved"
    assert cashflow["invoice_amount"] == Decimal("12345.67")
