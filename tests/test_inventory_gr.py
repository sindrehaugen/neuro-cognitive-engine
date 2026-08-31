"""Tests for the Inventory engine's goods-receipt module
(Module 11, Wave 4 — Batch 132 — ``nce/vertical_modules/inventory/goods_receipt.py``).

Covers, per :func:`do_record_goods_receipt`:

  1. Pure-logic validation (no DB) — line aggregation, ORDER-INDEPENDENT
     duplicate-sku unit_cost handling (``None`` is "not captured", never
     "disagrees"), scan normalisation, boundary normalisation of ``po_ref``
     and ``delivery_note_ref``, receipt-hash stability/instability, and
     Decimal quantisation, exercised through the module's own helpers (never
     re-derived — a test that reimplements the logic it protects cannot
     detect a divergence in it). Includes the one test in this file that
     pins a LITERAL digest over FIXED inputs
     (``test_a_note_less_receipt_hashes_exactly_as_it_did_before_delivery_note_ref_existed``):
     ``receipt_hash`` is persisted and is the sole arbiter of replay, so any
     payload-shape change that moves a note-less receipt's digest silently
     re-keys every receipt already in the table and turns its retry into a
     double-stock. That must fail at test time, not at stock-count time.
  2. Integration (``@pytest.mark.integration``, live Postgres):
     (a) a costed 3-line receipt's happy path — one ``goods_receipts`` row,
         exact per-sku ``qty_on_hand`` increments, exactly 3 ``goods_receipt``
         ledger rows;
     (b) an identical replay is a no-op — zero further ledger rows, unchanged
         stock;
     (c) TWO concurrent identical submissions still increment exactly once —
         the by-construction claim, over REAL separate connections
         (``asyncio.gather``), never sequential calls;
     (d) two DIFFERENT receipts sharing overlapping SKUs, submitted
         concurrently over ~8 rounds, deadlock zero times — the deterministic
         ascending-sku processing order claim;
     (e) ATOMICITY, gated by a failure that happens AFTER the receipt row is
         inserted and after a whole line has already been applied (a second
         line whose quantity overflows ``NUMERIC(18,3)``) — a bogus
         ``location_id`` does NOT gate this and the test that claimed it did
         says so in its own docstring now; plus the numeric-overflow
         ``ValueError`` contract on both upsert branches, and the receipt
         INSERT's two INDEPENDENT foreign keys (composite location FK vs.
         plain namespace FK) each named correctly by ``exc.constraint_name``;
     (e2) ``po_ref`` reaches the STORED column in the same normal form the
         hash uses, and an optional ``delivery_note_ref`` keeps two genuine
         PARTIAL deliveries apart while a retry of one note stays a replay —
         with the omitted-note case asserted as unchanged behaviour;
     (f) migration 052's structural claims, proven with raw SQL: the
         idempotency unique index, the widened sign-matches-category CHECK,
         and the widened reason_category CHECK;
     (g) FORCE RLS isolation through a REAL unprivileged ``nce_app``
         connection — never the owner pool, which bypasses FORCE RLS;
     (h) end-to-end valuation — B139's declared scope limit (``do_valuation``
         proven only against SEEDED rows) discharged: a receipt's real
         ``unit_cost`` survives to ``transactions.do_valuation``, with
         FIFO/average chosen to differ;
     (i) Decimal discipline, using values that actually discriminate
         ``Decimal(str(x))`` from ``Decimal(x)`` at EACH column's own scale
         (0.0045 at the qty column's 3dp, 1.005 at the unit_cost column's
         2dp — B130's audit-measured 2.6755 discriminates at 3dp but not at
         2dp, so it is not reused here for cost), round-tripped through real
         ``NUMERIC`` columns.

(a), (b), (c) and (h) are proven discriminating by mutation — an out-of-tree
pytest plugin disarms ``append_transaction`` and the RED/GREEN summaries plus
before/after file hashes are reported alongside this file's gate output
(rule 11: no in-tree mutation, ever). (e)'s atomicity gate is proven the same
way, against an out-of-tree COPY of the module whose per-line effects run in
a second ``scoped_pg_session``. (f) is DDL-discriminating and (g) is
isolation-discriminating; neither is claimed as code-discriminating.

``stock.py`` is not imported here except where explicitly noted as a
different module's own convention reference in a comment — this wave's
writer never touches it (rule 4a).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse, urlunparse

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.config import cfg
from nce.entity_resolution.ownership import OwnershipError
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.inventory import goods_receipt as gr_module
from nce.vertical_modules.inventory import transactions
from nce.vertical_modules.inventory.goods_receipt import (
    _as_optional_delivery_note_ref,
    _as_po_ref,
    _compute_receipt_hash,
    _upsert_goods_receipt_node,
    _validate_and_aggregate_lines,
    _validate_scans,
    do_record_goods_receipt,
)
from nce.vertical_modules.inventory.transactions import do_valuation

# ---------------------------------------------------------------------------
# 1. Pure-logic validation (no DB) — exercised through the module's own
#    helpers, the same ones do_record_goods_receipt calls.
# ---------------------------------------------------------------------------


class _DummyEngine:
    """Stands in for NCEEngine in tests that never reach a DB call — the
    validation under test raises before engine.pg_pool is ever touched."""

    pg_pool = None


def _base_params(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "namespace_id": uuid.uuid4(),
        "po_ref": "PO-1001",
        "location_id": uuid.uuid4(),
        "lines": [{"sku": "SKU-A", "qty": 5}],
    }
    params.update(overrides)
    return params


@pytest.mark.asyncio
async def test_rejects_missing_namespace_id() -> None:
    params = _base_params()
    del params["namespace_id"]
    with pytest.raises(ValueError, match="'namespace_id' is required"):
        await do_record_goods_receipt(_DummyEngine(), params)


@pytest.mark.asyncio
async def test_rejects_empty_po_ref() -> None:
    with pytest.raises(ValueError, match="po_ref' is required"):
        await do_record_goods_receipt(_DummyEngine(), _base_params(po_ref="   "))


@pytest.mark.asyncio
async def test_rejects_missing_location_id() -> None:
    params = _base_params()
    del params["location_id"]
    with pytest.raises(ValueError, match="a location id is required"):
        await do_record_goods_receipt(_DummyEngine(), params)


@pytest.mark.asyncio
async def test_rejects_empty_lines() -> None:
    with pytest.raises(ValueError, match="'lines' must be a non-empty list"):
        await do_record_goods_receipt(_DummyEngine(), _base_params(lines=[]))


@pytest.mark.asyncio
async def test_rejects_line_with_non_positive_qty() -> None:
    with pytest.raises(ValueError, match=r"qty must be > 0"):
        await do_record_goods_receipt(
            _DummyEngine(), _base_params(lines=[{"sku": "SKU-A", "qty": 0}])
        )


@pytest.mark.asyncio
async def test_rejects_line_missing_sku() -> None:
    with pytest.raises(ValueError, match="sku must be non-empty"):
        await do_record_goods_receipt(_DummyEngine(), _base_params(lines=[{"qty": 5}]))


def test_aggregates_duplicate_skus_summing_qty() -> None:
    """Two entries for the same sku (agreeing unit_cost) sum, not overwrite."""
    lines = _validate_and_aggregate_lines(
        [
            {"sku": "SKU-B", "qty": 2, "unit_cost": Decimal("10.00")},
            {"sku": "SKU-A", "qty": 3},
            {"sku": "SKU-B", "qty": 4, "unit_cost": Decimal("10.00")},
        ]
    )
    # Ascending-sku order (rule: deterministic lock order), summed qty.
    assert lines == [
        ("SKU-A", Decimal("3.000"), None),
        ("SKU-B", Decimal("6.000"), Decimal("10.00")),
    ]


def test_rejects_duplicate_sku_with_disagreeing_unit_cost() -> None:
    """RED if aggregation silently picked one cost instead of refusing."""
    with pytest.raises(ValueError, match=r"SKU-B.*disagreeing unit_cost|disagreeing.*SKU-B"):
        _validate_and_aggregate_lines(
            [
                {"sku": "SKU-B", "qty": 2, "unit_cost": Decimal("10.00")},
                {"sku": "SKU-B", "qty": 4, "unit_cost": Decimal("11.00")},
            ]
        )


def test_two_genuinely_disagreeing_costs_are_refused_in_EITHER_input_order() -> None:
    """The refusal itself must not depend on array order either — RED if the
    verdict were reached incrementally against a 'last seen' cost that one
    order happens to leave unset."""
    for first, second in (
        (Decimal("10.00"), Decimal("11.00")),
        (Decimal("11.00"), Decimal("10.00")),
    ):
        with pytest.raises(ValueError, match=r"SKU-B.*disagreeing unit_cost"):
            _validate_and_aggregate_lines(
                [
                    {"sku": "SKU-B", "qty": 2, "unit_cost": first},
                    {"sku": "SKU-B", "qty": 4, "unit_cost": second},
                ]
            )


def test_none_unit_cost_never_disagrees_with_a_captured_one_in_either_order() -> None:
    """F2: one PO line split across two pallets with the cost captured on only
    ONE of them — routine in procurement. ``None`` means "not captured", never
    "disagrees", so BOTH serialisations must aggregate identically.

    RED against the shipped defect: the previous implementation only ever
    assigned ``seen_cost[sku]`` from a non-``None`` cost, so
    ``[(3, None), (7, 12.50)]`` aggregated fine while the SAME delivery
    re-serialised as ``[(7, 12.50), (3, None)]`` raised ValueError — turning
    an idempotent retry into a hard failure instead of ``{"duplicate": True}``.
    """
    expected = [("SKU-X", Decimal("10.000"), Decimal("12.50"))]

    none_first = _validate_and_aggregate_lines(
        [
            {"sku": "SKU-X", "qty": 3, "unit_cost": None},
            {"sku": "SKU-X", "qty": 7, "unit_cost": Decimal("12.50")},
        ]
    )
    cost_first = _validate_and_aggregate_lines(
        [
            {"sku": "SKU-X", "qty": 7, "unit_cost": Decimal("12.50")},
            {"sku": "SKU-X", "qty": 3, "unit_cost": None},
        ]
    )

    assert none_first == expected
    assert cost_first == expected, (
        "order-dependent rejection of the same delivery — the retry of a "
        "successful submission must not raise"
    )


def test_absent_unit_cost_key_behaves_exactly_like_an_explicit_none() -> None:
    """A missing ``unit_cost`` key and an explicit ``None`` are the same
    "not captured" statement, in any position among three entries."""
    with_explicit_nones = _validate_and_aggregate_lines(
        [
            {"sku": "SKU-Y", "qty": 1, "unit_cost": None},
            {"sku": "SKU-Y", "qty": 2, "unit_cost": Decimal("4.00")},
            {"sku": "SKU-Y", "qty": 3, "unit_cost": None},
        ]
    )
    with_absent_keys = _validate_and_aggregate_lines(
        [
            {"sku": "SKU-Y", "qty": 1},
            {"sku": "SKU-Y", "qty": 2, "unit_cost": Decimal("4.00")},
            {"sku": "SKU-Y", "qty": 3},
        ]
    )
    assert with_explicit_nones == [("SKU-Y", Decimal("6.000"), Decimal("4.00"))]
    assert with_absent_keys == with_explicit_nones


def test_repeated_identical_costs_are_not_a_disagreement() -> None:
    """Only DISTINCT non-None costs can conflict — the same cost stated three
    times is one cost, not three."""
    assert _validate_and_aggregate_lines(
        [
            {"sku": "SKU-Z", "qty": 1, "unit_cost": Decimal("7.00")},
            {"sku": "SKU-Z", "qty": 1, "unit_cost": Decimal("7.00")},
            {"sku": "SKU-Z", "qty": 1, "unit_cost": Decimal("7.000")},
        ]
    ) == [("SKU-Z", Decimal("3.000"), Decimal("7.00"))]


def test_lines_are_returned_in_ascending_sku_order_regardless_of_input_order() -> None:
    """The deterministic-lock-order claim: caller order must not matter."""
    lines = _validate_and_aggregate_lines(
        [{"sku": "ZEBRA", "qty": 1}, {"sku": "ALPHA", "qty": 1}, {"sku": "MID", "qty": 1}]
    )
    assert [sku for sku, _, _ in lines] == ["ALPHA", "MID", "ZEBRA"]


def test_scans_validation_rejects_non_list() -> None:
    with pytest.raises(ValueError, match="'scans' must be a list"):
        _validate_scans("not-a-list")


def test_scans_validation_rejects_missing_sku() -> None:
    with pytest.raises(ValueError, match="sku must be non-empty"):
        _validate_scans([{"serial": "SN-1"}])


def test_scans_defaults_to_empty_list_when_none() -> None:
    assert _validate_scans(None) == []


# ---------------------------------------------------------------------------
# 1b. Receipt-hash stability / instability — hash stability is load-bearing
# (a different encoding of the same delivery double-stocks); disclosed
# collision behaviour (byte-identical distinct deliveries hash the same).
# ---------------------------------------------------------------------------


def test_po_ref_is_normalised_once_at_the_boundary() -> None:
    """F4: ``_as_po_ref`` is THE normalisation point — it must return the
    upper-cased, stripped form that is BOTH hashed and stored. RED if
    normalisation moved back into ``_compute_receipt_hash`` (where it made
    the hash case-insensitive while the stored column stayed
    case-sensitive)."""
    assert _as_po_ref("  po-1001  ") == "PO-1001"
    assert _as_po_ref("PO-1001") == "PO-1001"


def test_hash_does_not_renormalise_what_the_boundary_already_normalised() -> None:
    """The hash hashes what it is given, verbatim. RED if
    ``_compute_receipt_hash`` re-applied ``.strip().upper()`` internally: the
    two calls below would then collide, hiding the very divergence that made
    idempotency case-insensitive while ``goods_receipts.po_ref`` was not."""
    loc = uuid.uuid4()
    lines = _validate_and_aggregate_lines([{"sku": "SKU-A", "qty": 5}])
    raw = _compute_receipt_hash("  po-1001  ", loc, lines, [], delivery_note_ref=None)
    normalised = _compute_receipt_hash("PO-1001", loc, lines, [], delivery_note_ref=None)
    assert raw != normalised, (
        "the hash must not normalise a second time — _as_po_ref owns that, once"
    )


def test_hash_is_stable_across_po_ref_case_and_whitespace_through_the_boundary() -> None:
    """The real path: two spellings of one PO reference, each taken through
    ``_as_po_ref`` exactly as ``do_record_goods_receipt`` does, hash the
    same — so a case-variant resubmission is still a replay."""
    loc = uuid.uuid4()
    lines_a = _validate_and_aggregate_lines([{"sku": "SKU-A", "qty": 5}])
    h1 = _compute_receipt_hash(_as_po_ref("  po-1001  "), loc, lines_a, [], delivery_note_ref=None)
    h2 = _compute_receipt_hash(_as_po_ref("PO-1001"), loc, lines_a, [], delivery_note_ref=None)
    assert h1 == h2


def test_hash_is_stable_across_caller_line_order() -> None:
    """Aggregation already sorts by sku, so hashing the aggregated result is
    order-independent even though the caller supplied lines in two different
    orders — RED if the hash were computed over caller order instead."""
    loc = uuid.uuid4()
    lines_order_1 = _validate_and_aggregate_lines(
        [{"sku": "SKU-A", "qty": 5}, {"sku": "SKU-B", "qty": 2}]
    )
    lines_order_2 = _validate_and_aggregate_lines(
        [{"sku": "SKU-B", "qty": 2}, {"sku": "SKU-A", "qty": 5}]
    )
    assert _compute_receipt_hash(
        "PO-1001", loc, lines_order_1, [], delivery_note_ref=None
    ) == _compute_receipt_hash("PO-1001", loc, lines_order_2, [], delivery_note_ref=None)


def test_hash_differs_when_scans_differ_even_with_identical_lines() -> None:
    """The secondary escape hatch: two receipts with byte-identical line sets
    but different scan data must NOT collide."""
    loc = uuid.uuid4()
    lines = _validate_and_aggregate_lines([{"sku": "SKU-A", "qty": 5}])
    scans_1 = _validate_scans([{"sku": "SKU-A", "serial": "SN-0001"}])
    scans_2 = _validate_scans([{"sku": "SKU-A", "serial": "SN-0002"}])
    h1 = _compute_receipt_hash("PO-1001", loc, lines, scans_1, delivery_note_ref=None)
    h2 = _compute_receipt_hash("PO-1001", loc, lines, scans_2, delivery_note_ref=None)
    assert h1 != h2


def test_hash_differs_when_quantity_differs() -> None:
    loc = uuid.uuid4()
    lines_5 = _validate_and_aggregate_lines([{"sku": "SKU-A", "qty": 5}])
    lines_6 = _validate_and_aggregate_lines([{"sku": "SKU-A", "qty": 6}])
    assert _compute_receipt_hash(
        "PO-1001", loc, lines_5, [], delivery_note_ref=None
    ) != _compute_receipt_hash("PO-1001", loc, lines_6, [], delivery_note_ref=None)


# ---------------------------------------------------------------------------
# 1c. delivery_note_ref — the F6 fix. Two 5-unit partial deliveries against
# ONE PO line of 10 produce a byte-identical line set; without a delivery
# note they hash identically and the second is swallowed as a replay.
# ---------------------------------------------------------------------------


# The digest a note-less receipt produced at 5abc134 — the commit BEFORE
# delivery_note_ref existed, when the hash payload was
# ``(po_ref, location_id, lines, scans)`` and nothing else. Pinned as a
# literal on purpose; the test below explains why, and why updating it is
# almost always the wrong response to a RED.
_NOTE_LESS_DIGEST_BEFORE_DELIVERY_NOTE_REF = (
    "786f33ffbda34283dfd4842de58fb94f63994e2ae1556999d79bbfceb04b0eda"
)
# What the SAME receipt hashed to while the payload always emitted
# ``"delivery_note_ref": null`` — the regression this test exists to catch.
_NOTE_LESS_DIGEST_WHEN_NULL_WAS_EMITTED = (
    "663fa9d7b2269417d70c124bee11fbc9a61b52c5c26b7e61a5d0ada476b3d96f"
)
# Fixed, never uuid4() — a pinned digest is only meaningful over pinned inputs.
_PINNED_LOCATION_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


def test_a_note_less_receipt_hashes_exactly_as_it_did_before_delivery_note_ref_existed() -> None:
    """F1 — the pinned-digest regression, and this module's upgrade-safety test.

    Adding ``delivery_note_ref`` to the hash payload as an ALWAYS-PRESENT key
    changed the idempotency key of every receipt that omits a note: for the
    receipt below, from ``786f33ff…`` to ``663fa9d7…``. That is not a
    cosmetic difference in an internal encoding. ``receipt_hash`` is
    PERSISTED, and ``goods_receipts_idempotency_uq`` is the sole arbiter of
    replay, so a receipt recorded before such a change keeps its OLD digest
    in the table while the identical delivery resubmitted after the change
    computes a NEW one. The unique index sees no conflict, the replay gate in
    ``do_record_goods_receipt`` never fires, and the delivery is applied a
    SECOND time — a second ``goods_receipts`` row, a second
    ``inventory_transactions`` row, and ``qty_on_hand`` incremented twice for
    ONE physical pallet. Silent double-stocking, discovered at stock-count
    time rather than at deploy time.

    The fix, and what this test pins, is that ``None`` OMITS the key rather
    than hashing as ``null`` — so a note-less receipt's digest is
    byte-identical to the pre-parameter one and the module docstring's claim
    that "when it is omitted, behaviour is exactly as it was" is literally
    true of the idempotency key itself, not merely of its collision
    properties.

    Any future change to the payload SHAPE that alters a note-less receipt's
    digest is the same class of break for every receipt already recorded, and
    must fail HERE, at test time. If this goes RED, updating the literal is
    almost never the correct response — making the new field absent from the
    payload when it is not supplied, exactly as ``delivery_note_ref`` is,
    almost always is.
    """
    lines = _validate_and_aggregate_lines([{"sku": "SKU-A", "qty": 5}])
    digest = _compute_receipt_hash(
        "PO-1001", _PINNED_LOCATION_ID, lines, [], delivery_note_ref=None
    )
    assert digest == _NOTE_LESS_DIGEST_BEFORE_DELIVERY_NOTE_REF, (
        "a receipt that omits delivery_note_ref must hash EXACTLY as it did at "
        "5abc134 — a changed digest silently re-keys every already-recorded "
        "note-less receipt and makes its retry double-stock instead of replay"
    )
    assert digest != _NOTE_LESS_DIGEST_WHEN_NULL_WAS_EMITTED, (
        "the payload is emitting 'delivery_note_ref': null again — omit the key "
        "entirely when the note is absent"
    )


def test_a_blank_delivery_note_also_keeps_the_pre_existing_digest() -> None:
    """The other way to reach ``None``: a whitespace-only note collapses to
    "not supplied" at the boundary, so it must land on the SAME pre-existing
    digest — not on a third, blank-string-flavoured one."""
    lines = _validate_and_aggregate_lines([{"sku": "SKU-A", "qty": 5}])
    assert (
        _compute_receipt_hash(
            "PO-1001",
            _PINNED_LOCATION_ID,
            lines,
            [],
            delivery_note_ref=_as_optional_delivery_note_ref("   "),
        )
        == _NOTE_LESS_DIGEST_BEFORE_DELIVERY_NOTE_REF
    )


def test_a_supplied_delivery_note_moves_the_digest_off_the_pinned_one() -> None:
    """The pinning must not have been achieved by dropping the parameter from
    the payload altogether: a receipt that DOES carry a note must hash to
    something other than the pre-existing note-less digest, or F6's fix is
    gone."""
    lines = _validate_and_aggregate_lines([{"sku": "SKU-A", "qty": 5}])
    assert (
        _compute_receipt_hash("PO-1001", _PINNED_LOCATION_ID, lines, [], delivery_note_ref="DN-1")
        != _NOTE_LESS_DIGEST_BEFORE_DELIVERY_NOTE_REF
    )


def test_delivery_note_ref_is_normalised_like_po_ref_and_blank_is_none() -> None:
    assert _as_optional_delivery_note_ref("  dn-77  ") == "DN-77"
    assert _as_optional_delivery_note_ref(None) is None
    assert _as_optional_delivery_note_ref("   ") is None, "blank is 'not supplied', not a value"
    with pytest.raises(ValueError, match="delivery_note_ref"):
        _as_optional_delivery_note_ref(77)


def test_two_identical_partial_deliveries_collide_when_no_delivery_note_is_given() -> None:
    """The pre-existing behaviour, asserted rather than merely described: with
    NO delivery note, two genuine 5-unit partial deliveries hash the SAME.
    This test documents exactly what the optional parameter leaves unchanged
    when a caller omits it — it is not an aspiration, it is the current
    contract."""
    loc = uuid.uuid4()
    lines = _validate_and_aggregate_lines([{"sku": "SKU-A", "qty": 5}])
    first = _compute_receipt_hash("PO-1001", loc, lines, [], delivery_note_ref=None)
    second = _compute_receipt_hash("PO-1001", loc, lines, [], delivery_note_ref=None)
    assert first == second


def test_distinct_delivery_notes_separate_two_identical_partial_deliveries() -> None:
    """F6: the fix. Same PO, same location, same line set, no scans — but two
    different delivery-note numbers must hash DIFFERENTLY, or the second
    genuine delivery is lost. RED if delivery_note_ref were dropped from the
    hash payload."""
    loc = uuid.uuid4()
    lines = _validate_and_aggregate_lines([{"sku": "SKU-A", "qty": 5}])
    first = _compute_receipt_hash(
        "PO-1001", loc, lines, [], delivery_note_ref=_as_optional_delivery_note_ref("DN-1")
    )
    second = _compute_receipt_hash(
        "PO-1001", loc, lines, [], delivery_note_ref=_as_optional_delivery_note_ref("DN-2")
    )
    assert first != second


def test_the_same_delivery_note_resubmitted_still_hashes_identically() -> None:
    """The other half of F6: distinguishing genuine deliveries must not break
    idempotent retry of ONE delivery — including a case/whitespace variant of
    the same note, which normalisation folds together."""
    loc = uuid.uuid4()
    lines = _validate_and_aggregate_lines([{"sku": "SKU-A", "qty": 5}])
    submitted = _compute_receipt_hash(
        "PO-1001", loc, lines, [], delivery_note_ref=_as_optional_delivery_note_ref("DN-1")
    )
    retried = _compute_receipt_hash(
        "PO-1001", loc, lines, [], delivery_note_ref=_as_optional_delivery_note_ref("  dn-1 ")
    )
    assert submitted == retried


def test_supplying_a_delivery_note_differs_from_omitting_one() -> None:
    loc = uuid.uuid4()
    lines = _validate_and_aggregate_lines([{"sku": "SKU-A", "qty": 5}])
    assert _compute_receipt_hash(
        "PO-1001", loc, lines, [], delivery_note_ref=None
    ) != _compute_receipt_hash("PO-1001", loc, lines, [], delivery_note_ref="DN-1")


def test_hash_discriminates_decimal_str_vs_raw_binary_float() -> None:
    """B130-class discriminating value: 0.0045's nearest binary double sits
    just BELOW the decimal tie, so Decimal(x) would render a DIFFERENT
    string ('0.004...') than Decimal(str(x)) quantised to 3dp ('0.005') —
    a hash computed over the wrong path would disagree with one computed
    over the right path for what should be the identical logical quantity."""
    loc = uuid.uuid4()
    lines_from_float = _validate_and_aggregate_lines([{"sku": "SKU-A", "qty": 0.0045}])
    lines_from_correct_decimal = _validate_and_aggregate_lines(
        [{"sku": "SKU-A", "qty": Decimal("0.005")}]
    )
    assert lines_from_float[0][1] == Decimal("0.005"), (
        "0.0045 must quantise via Decimal(str(x)) to 0.005 — 0.004 would mean "
        "the raw binary-float value was quantised instead"
    )
    assert _compute_receipt_hash(
        "PO-1001", loc, lines_from_float, [], delivery_note_ref=None
    ) == _compute_receipt_hash(
        "PO-1001", loc, lines_from_correct_decimal, [], delivery_note_ref=None
    )


# ---------------------------------------------------------------------------
# Integration helpers — seed directly via the owner pool, matching
# test_inventory_transactions.py / test_inventory_stock.py's convention.
# Every helper takes an explicit namespace_id and scopes its own SQL by it.
# ---------------------------------------------------------------------------


class _EngineStub:
    def __init__(self, pg_pool: asyncpg.Pool) -> None:  # type: ignore[type-arg]
        self.pg_pool = pg_pool


def _app_dsn() -> str:
    """Rewrite the integration DSN onto the restricted nce_app role.

    Verbatim in shape from tests/test_inventory_stock.py::_app_dsn — the
    in-repo precedent for driving a vertical module through a REAL
    FORCE-RLS-subject connection instead of the superuser pg_pool."""
    primary = (
        os.environ.get("NCE_INTEGRATION_PG_DSN")
        or os.environ.get("PG_DSN")
        or os.environ.get("DATABASE_URL")
        or cfg.PG_DSN
    )
    parsed = urlparse(primary)
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    app_pass = cfg.NCE_APP_PASSWORD or "nce_app_secret"
    netloc = f"nce_app:{app_pass}@{netloc}"
    return urlunparse(parsed._replace(netloc=netloc))


async def _seed_location(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    name: str,
) -> uuid.UUID:
    async with pg_pool.acquire() as conn:
        location_id = await conn.fetchval(
            "INSERT INTO stock_locations (namespace_id, kind, name, parent_id, level) "
            "VALUES ($1, 'warehouse', $2, NULL, 0) RETURNING id",
            namespace_id,
            name,
        )
    assert location_id is not None
    return location_id


async def _get_on_hand(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    sku: str,
    location_id: uuid.UUID,
) -> Decimal:
    """Returns 0 (not None) when no row exists yet — a goods receipt into a
    brand-new sku creates the row via upsert, so "no row" and "zero" are the
    same starting state for this module's purposes."""
    async with pg_pool.acquire() as conn:
        value = await conn.fetchval(
            "SELECT qty_on_hand FROM inventory_items "
            "WHERE namespace_id = $1 AND sku = $2 AND location_id = $3",
            namespace_id,
            sku,
            location_id,
        )
    return value if value is not None else Decimal("0.000")


async def _count(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    table: str,
    namespace_id: uuid.UUID,
    **where: Any,
) -> int:
    conditions = ["namespace_id = $1"]
    args: list[Any] = [namespace_id]
    for key, val in where.items():
        args.append(val)
        conditions.append(f"{key} = ${len(args)}")
    async with pg_pool.acquire() as conn:
        return await conn.fetchval(  # type: ignore[no-any-return]
            f"SELECT COUNT(*) FROM {table} WHERE " + " AND ".join(conditions),
            *args,
        )


async def _seed_ownership(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Seed the node-ownership registry so the guarded GOODS_RECEIPT graph
    write (Batch 132b's ``_upsert_goods_receipt_node``) passes for this
    namespace. Copied in shape from B130a's ``_seed_ownership`` in
    ``tests/test_inventory_stock.py`` (one idiom, not two) — NOT called from
    conftest.py's fixtures on purpose, since seeding there would disarm the
    deliberate deny-by-default proofs elsewhere in this repo (rule 9/out of
    scope). NOT autouse: every test that now reaches the guarded write calls
    this explicitly."""
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)
            await seed_node_ownership_registry(conn, namespace_id)


# ---------------------------------------------------------------------------
# (a) Costed receipt, happy path.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_costed_receipt_happy_path(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """RED if: the receipt row is not written exactly once, an increment is
    wrong, a ledger row is missing/duplicated, or unit_cost/ref are dropped."""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    engine = _EngineStub(pg_pool)

    result = await do_record_goods_receipt(
        engine,
        {
            "namespace_id": namespace_id,
            "po_ref": "PO-HAPPY-1",
            "location_id": loc,
            "lines": [
                {"sku": "SKU-HAPPY-A", "qty": 10, "unit_cost": Decimal("5.00")},
                {"sku": "SKU-HAPPY-B", "qty": 3, "unit_cost": Decimal("12.50")},
                {"sku": "SKU-HAPPY-C", "qty": 7, "unit_cost": Decimal("1.25")},
            ],
        },
    )

    assert result["ok"] is True
    assert result["duplicate"] is False
    receipt_id = result["receipt_id"]

    assert await _count(pg_pool, "goods_receipts", namespace_id, po_ref="PO-HAPPY-1") == 1

    assert await _get_on_hand(pg_pool, namespace_id, "SKU-HAPPY-A", loc) == Decimal("10.000")
    assert await _get_on_hand(pg_pool, namespace_id, "SKU-HAPPY-B", loc) == Decimal("3.000")
    assert await _get_on_hand(pg_pool, namespace_id, "SKU-HAPPY-C", loc) == Decimal("7.000")

    async with pg_pool.acquire() as conn:
        ledger_rows = await conn.fetch(
            "SELECT sku, delta, reason_category, unit_cost, ref FROM inventory_transactions "
            "WHERE namespace_id = $1 AND ref = $2 ORDER BY sku",
            namespace_id,
            receipt_id,
        )
    assert len(ledger_rows) == 3, "one costed 3-line receipt must append exactly 3 ledger rows"
    by_sku = {r["sku"]: r for r in ledger_rows}
    assert by_sku["SKU-HAPPY-A"]["delta"] == Decimal("10.000")
    assert by_sku["SKU-HAPPY-A"]["unit_cost"] == Decimal("5.00")
    assert by_sku["SKU-HAPPY-B"]["delta"] == Decimal("3.000")
    assert by_sku["SKU-HAPPY-B"]["unit_cost"] == Decimal("12.50")
    assert by_sku["SKU-HAPPY-C"]["delta"] == Decimal("7.000")
    assert by_sku["SKU-HAPPY-C"]["unit_cost"] == Decimal("1.25")
    for row in ledger_rows:
        assert row["reason_category"] == "goods_receipt"
        assert row["delta"] > 0
        assert row["ref"] == receipt_id


# ---------------------------------------------------------------------------
# (b) Replay is a no-op (sequential).
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_is_a_no_op(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """RED if step 6b's replay-gating were removed (a second identical call
    would double the stock and double the ledger rows)."""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    engine = _EngineStub(pg_pool)
    params = {
        "namespace_id": namespace_id,
        "po_ref": "PO-REPLAY-1",
        "location_id": loc,
        "lines": [{"sku": "SKU-REPLAY", "qty": 8, "unit_cost": Decimal("2.00")}],
    }

    first = await do_record_goods_receipt(engine, dict(params))
    assert first["duplicate"] is False
    on_hand_after_first = await _get_on_hand(pg_pool, namespace_id, "SKU-REPLAY", loc)
    assert on_hand_after_first == Decimal("8.000")

    second = await do_record_goods_receipt(engine, dict(params))
    assert second["duplicate"] is True
    assert second["receipt_id"] == first["receipt_id"]

    on_hand_after_replay = await _get_on_hand(pg_pool, namespace_id, "SKU-REPLAY", loc)
    assert on_hand_after_replay == Decimal("8.000"), "a replay must not touch stock"

    assert await _count(pg_pool, "goods_receipts", namespace_id, po_ref="PO-REPLAY-1") == 1
    ledger_count = await _count(pg_pool, "inventory_transactions", namespace_id, sku="SKU-REPLAY")
    assert ledger_count == 1, "a replay must append zero further ledger rows"


# ---------------------------------------------------------------------------
# (c) CONCURRENT replay — the by-construction claim under real concurrency.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_identical_receipts_increment_exactly_once(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """THE by-construction claim: two byte-identical receipts submitted
    through asyncio.gather over separate pool connections. RED if the
    DO-NOTHING gate were replaced by a Python-side check-then-write — that
    shape passes the sequential replay test above and fails ONLY here."""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    engine = _EngineStub(pg_pool)
    params = {
        "namespace_id": namespace_id,
        "po_ref": "PO-CONCURRENT-1",
        "location_id": loc,
        "lines": [
            {"sku": "SKU-CONCURRENT-A", "qty": 6, "unit_cost": Decimal("3.00")},
            {"sku": "SKU-CONCURRENT-B", "qty": 4, "unit_cost": Decimal("1.50")},
        ],
    }

    results = await asyncio.gather(
        do_record_goods_receipt(engine, dict(params)),
        do_record_goods_receipt(engine, dict(params)),
    )

    duplicates = [r["duplicate"] for r in results]
    assert sorted(duplicates) == [False, True], (
        f"expected exactly one fresh + one duplicate, got: {duplicates}"
    )
    receipt_ids = {r["receipt_id"] for r in results}
    assert len(receipt_ids) == 1, "both calls must agree on the SAME receipt id"

    assert await _get_on_hand(pg_pool, namespace_id, "SKU-CONCURRENT-A", loc) == Decimal("6.000"), (
        "concurrent duplicate submission must increment exactly once"
    )
    assert await _get_on_hand(pg_pool, namespace_id, "SKU-CONCURRENT-B", loc) == Decimal("4.000")

    assert await _count(pg_pool, "goods_receipts", namespace_id, po_ref="PO-CONCURRENT-1") == 1
    ledger_count_a = await _count(
        pg_pool, "inventory_transactions", namespace_id, sku="SKU-CONCURRENT-A"
    )
    ledger_count_b = await _count(
        pg_pool, "inventory_transactions", namespace_id, sku="SKU-CONCURRENT-B"
    )
    assert ledger_count_a == 1
    assert ledger_count_b == 1


# ---------------------------------------------------------------------------
# (d) Overlapping-SKU concurrency does not deadlock.
# ---------------------------------------------------------------------------

# Reset-and-retry rounds. Mirrors tests/test_inventory_stock.py's
# _LOCK_ORDER_ROUNDS convention/rationale (8 rounds makes a false "it never
# happens" result implausible while keeping runtime modest).
_OVERLAP_ROUNDS = 8


@pytest.mark.integration
@pytest.mark.asyncio
async def test_overlapping_sku_concurrent_receipts_do_not_deadlock(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Two DIFFERENT receipts (distinct po_ref -> distinct receipt_hash,
    never colliding with idempotency) sharing 2 SKUs, submitted with
    OPPOSITE line orders, over ~8 rounds. RED if lines were processed in
    caller-supplied order instead of ascending sku order — receipt A would
    lock (SKU-1, SKU-2) while receipt B locked (SKU-2, SKU-1), the classic
    AB-BA cycle."""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    engine = _EngineStub(pg_pool)
    sku_1, sku_2 = "SKU-OVERLAP-1", "SKU-OVERLAP-2"

    async def _receipt(po_ref: str, lines: list[dict[str, Any]]) -> dict[str, Any]:
        return await do_record_goods_receipt(
            engine,
            {
                "namespace_id": namespace_id,
                "po_ref": po_ref,
                "location_id": loc,
                "lines": lines,
            },
        )

    for round_no in range(_OVERLAP_ROUNDS):
        before_1 = await _get_on_hand(pg_pool, namespace_id, sku_1, loc)
        before_2 = await _get_on_hand(pg_pool, namespace_id, sku_2, loc)

        results = await asyncio.gather(
            _receipt(
                f"PO-OVERLAP-A-{round_no}",
                [{"sku": sku_1, "qty": 5}, {"sku": sku_2, "qty": 3}],
            ),
            _receipt(
                f"PO-OVERLAP-B-{round_no}",
                [{"sku": sku_2, "qty": 4}, {"sku": sku_1, "qty": 2}],
            ),
            return_exceptions=True,
        )

        for result in results:
            assert not isinstance(result, asyncpg.exceptions.DeadlockDetectedError), (
                f"round {round_no}: overlapping-SKU receipts deadlocked. Lines must be "
                f"processed in ascending-sku order, not caller order: {result}"
            )
            assert not isinstance(result, BaseException), (
                f"round {round_no}: unexpected failure {type(result).__name__}: {result}"
            )
            assert result["ok"] is True

        after_1 = await _get_on_hand(pg_pool, namespace_id, sku_1, loc)
        after_2 = await _get_on_hand(pg_pool, namespace_id, sku_2, loc)
        assert after_1 - before_1 == Decimal("7.000"), (
            f"round {round_no}: {sku_1} conservation violated"
        )
        assert after_2 - before_2 == Decimal("7.000"), (
            f"round {round_no}: {sku_2} conservation violated"
        )


# ---------------------------------------------------------------------------
# (e) Atomicity, and the validation case that is NOT atomicity.
#
# inventory_items.qty_on_hand and inventory_transactions.delta are
# NUMERIC(18,3): 15 integer digits, so the largest representable value is
# 999999999999999.999 and 10^15 overflows.
# ---------------------------------------------------------------------------

_QTY_COLUMN_MAX = Decimal("999999999999999.999")
_QTY_COLUMN_OVERFLOW = Decimal("1000000000000000")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_invalid_location_is_refused_by_the_receipt_insert_itself(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """A bogus ``location_id`` is refused by ``goods_receipts_location_fk``
    on the RECEIPT INSERT — the first statement in the transaction — and is
    named as a location problem.

    **This test does NOT gate atomicity, and an earlier revision of it
    claimed that it did.** Its docstring asserted the failure happened on the
    first line's stock increment, "after" the receipt row was inserted. Both
    halves were false: ``inventory_items_location_fk`` and
    ``goods_receipts_location_fk`` reference the same
    ``stock_locations (id, namespace_id)`` pair, and the receipt INSERT runs
    first — so ``_increment_qty_on_hand`` is never reached and no receipt row
    was ever written to roll back. An auditor moved every stock/ledger effect
    into a SEPARATE transaction and all 29 tests still passed. The zero-row
    assertions below are therefore worth exactly what they are: proof that a
    refused INSERT wrote nothing, which is true of any single failed
    statement. Atomicity is gated by
    ``test_failure_after_the_receipt_insert_rolls_back_the_entire_transaction``
    below, which fails AFTER the receipt row exists."""
    bogus_location = uuid.uuid4()
    engine = _EngineStub(pg_pool)

    with pytest.raises(ValueError, match="does not exist in this namespace"):
        await do_record_goods_receipt(
            engine,
            {
                "namespace_id": namespace_id,
                "po_ref": "PO-ATOMIC-1",
                "location_id": bogus_location,
                "lines": [
                    {"sku": "SKU-ATOMIC-A", "qty": 5},
                    {"sku": "SKU-ATOMIC-B", "qty": 3},
                ],
            },
        )

    assert await _count(pg_pool, "goods_receipts", namespace_id, po_ref="PO-ATOMIC-1") == 0
    assert await _count(pg_pool, "inventory_transactions", namespace_id, sku="SKU-ATOMIC-A") == 0
    assert await _count(pg_pool, "inventory_items", namespace_id, sku="SKU-ATOMIC-A") == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_failure_after_the_receipt_insert_rolls_back_the_entire_transaction(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """THE atomicity gate: a failure that occurs AFTER the receipt row is
    inserted and AFTER a complete line has already been applied.

    Lines are processed in ascending sku order, so ``…-A`` (qty 5) fully
    succeeds — one ``inventory_items`` row created, one
    ``inventory_transactions`` row appended — and only then does ``…-Z``'s
    10^15 quantity overflow ``NUMERIC(18,3)``. At that instant three writes
    already exist inside the transaction: the receipt row, one stock row, one
    ledger row.

    RED whenever the receipt INSERT, the increments and the ledger appends
    are not all in ONE transaction. Verified as a real gate, out of tree:
    a copy of ``goods_receipt.py`` in a scratch directory with the per-line
    effects moved into a SECOND ``scoped_pg_session`` leaves 1 stock row and
    1 ledger row committed with NO receipt (ghost stock) — this test fails on
    that mutant and passes on the shipped module. The mutation was never
    applied to the file in the worktree."""
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    engine = _EngineStub(pg_pool)

    with pytest.raises(ValueError, match="does not fit the inventory numeric columns"):
        await do_record_goods_receipt(
            engine,
            {
                "namespace_id": namespace_id,
                "po_ref": "PO-ATOMIC-MID",
                "location_id": loc,
                "lines": [
                    {"sku": "SKU-ATOMIC-MID-A", "qty": 5},
                    {"sku": "SKU-ATOMIC-MID-Z", "qty": _QTY_COLUMN_OVERFLOW},
                ],
            },
        )

    assert await _count(pg_pool, "goods_receipts", namespace_id, po_ref="PO-ATOMIC-MID") == 0, (
        "the receipt row was inserted before the failing line — it must not survive"
    )
    # The discriminating assertions: these two rows were WRITTEN, successfully,
    # before the failure. Only a shared transaction takes them back.
    assert await _count(pg_pool, "inventory_items", namespace_id, sku="SKU-ATOMIC-MID-A") == 0, (
        "the first line's stock row committed independently — the increments are "
        "not in the receipt's transaction (ghost stock with no receipt record)"
    )
    assert (
        await _count(pg_pool, "inventory_transactions", namespace_id, sku="SKU-ATOMIC-MID-A") == 0
    ), (
        "the first line's ledger row committed independently — the ledger appends "
        "are not in the receipt's transaction"
    )
    assert await _count(pg_pool, "inventory_items", namespace_id, sku="SKU-ATOMIC-MID-Z") == 0
    assert (
        await _count(pg_pool, "inventory_transactions", namespace_id, sku="SKU-ATOMIC-MID-Z") == 0
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_numeric_overflow_is_a_valueerror_at_the_column_boundary(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """F5: the documented ``Raises`` contract says ``ValueError`` only, and a
    quantity that does not fit ``NUMERIC(18,3)`` used to escape as a raw
    ``asyncpg.NumericValueOutOfRangeError``. ``_quantise_qty``'s own "too
    large" ValueError guards Decimal's 28-digit limit, far above the column's
    10^15 ceiling, so it never fires first.

    Three points, on both sides of the boundary and on BOTH upsert branches:
      * ``999999999999999.999`` (the largest representable value) is ACCEPTED
        — proving the boundary is where the column says it is, not lower;
      * ``10^15`` on a fresh sku overflows on the INSERT branch;
      * ``+1`` onto a sku already at the maximum overflows on the DO UPDATE
        branch — the accumulation case no per-line pre-check could catch.
    All three must be ``ValueError``; RED if the translation is removed (the
    raw driver error is not a ValueError and would not be caught here)."""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    engine = _EngineStub(pg_pool)
    sku = "SKU-OVERFLOW"

    at_max = await do_record_goods_receipt(
        engine,
        {
            "namespace_id": namespace_id,
            "po_ref": "PO-OVERFLOW-MAX",
            "location_id": loc,
            "lines": [{"sku": sku, "qty": _QTY_COLUMN_MAX}],
        },
    )
    assert at_max["duplicate"] is False
    assert await _get_on_hand(pg_pool, namespace_id, sku, loc) == _QTY_COLUMN_MAX

    # DO UPDATE branch: the sku's row exists and is already at the ceiling.
    with pytest.raises(ValueError, match="does not fit the inventory numeric columns"):
        await do_record_goods_receipt(
            engine,
            {
                "namespace_id": namespace_id,
                "po_ref": "PO-OVERFLOW-ACCUMULATE",
                "location_id": loc,
                "lines": [{"sku": sku, "qty": 1}],
            },
        )
    assert await _get_on_hand(pg_pool, namespace_id, sku, loc) == _QTY_COLUMN_MAX

    # INSERT branch: a brand-new sku whose own value is already too large.
    with pytest.raises(ValueError, match="does not fit the inventory numeric columns"):
        await do_record_goods_receipt(
            engine,
            {
                "namespace_id": namespace_id,
                "po_ref": "PO-OVERFLOW-FRESH",
                "location_id": loc,
                "lines": [{"sku": "SKU-OVERFLOW-FRESH", "qty": _QTY_COLUMN_OVERFLOW}],
            },
        )
    assert await _count(pg_pool, "inventory_items", namespace_id, sku="SKU-OVERFLOW-FRESH") == 0


# ---------------------------------------------------------------------------
# (e2) po_ref normalisation reaches the STORED column (F4), and
#      delivery_note_ref separates genuine partial deliveries (F6).
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stored_po_ref_is_the_same_normal_form_idempotency_uses(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """F4: hashing and storage must agree. RED against the shipped defect —
    ``po_ref`` was upper-cased for the hash but stored VERBATIM, so
    ``po-store-1`` and ``PO-STORE-1`` were correctly one receipt while
    ``SELECT … WHERE po_ref = 'PO-STORE-1'`` (Batch 133's matcher's query,
    and ``idx_goods_receipts_namespace_po``'s collation) found nothing."""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    engine = _EngineStub(pg_pool)

    result = await do_record_goods_receipt(
        engine,
        {
            "namespace_id": namespace_id,
            "po_ref": "  po-store-1  ",
            "location_id": loc,
            "lines": [{"sku": "SKU-PO-NORM", "qty": 4}],
        },
    )
    assert result["duplicate"] is False
    assert result["po_ref"] == "PO-STORE-1", "the returned ref must be the normalised one"

    assert await _count(pg_pool, "goods_receipts", namespace_id, po_ref="PO-STORE-1") == 1, (
        "the STORED po_ref must be the normalised form the hash used"
    )
    assert await _count(pg_pool, "goods_receipts", namespace_id, po_ref="po-store-1") == 0, (
        "the verbatim caller spelling must NOT be what landed in the column"
    )

    # A third spelling of the same reference is still the same receipt.
    replay = await do_record_goods_receipt(
        engine,
        {
            "namespace_id": namespace_id,
            "po_ref": "PO-Store-1",
            "location_id": loc,
            "lines": [{"sku": "SKU-PO-NORM", "qty": 4}],
        },
    )
    assert replay["duplicate"] is True
    assert replay["receipt_id"] == result["receipt_id"]
    assert await _get_on_hand(pg_pool, namespace_id, "SKU-PO-NORM", loc) == Decimal("4.000")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_partial_deliveries_with_distinct_delivery_notes_both_land(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """F6: two 5-unit shipments against one PO line of 10 — same location,
    byte-identical line set, no scans — are TWO deliveries, and both must
    increment stock. RED against the shipped defect, where the second was
    swallowed as a replay and 5 units were silently lost.

    The same delivery note resubmitted is still a replay: the fix must not
    buy separation by breaking idempotent retry."""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    engine = _EngineStub(pg_pool)
    sku = "SKU-PARTIAL"

    def _params(note: str | None) -> dict[str, Any]:
        return {
            "namespace_id": namespace_id,
            "po_ref": "PO-PARTIAL-1",
            "delivery_note_ref": note,
            "location_id": loc,
            "lines": [{"sku": sku, "qty": 5, "unit_cost": Decimal("3.00")}],
        }

    first = await do_record_goods_receipt(engine, _params("DN-A"))
    second = await do_record_goods_receipt(engine, _params("DN-B"))

    assert first["duplicate"] is False
    assert second["duplicate"] is False, (
        "a genuine second partial delivery under a different delivery note must "
        "not be swallowed as a replay"
    )
    assert first["receipt_id"] != second["receipt_id"]
    assert await _get_on_hand(pg_pool, namespace_id, sku, loc) == Decimal("10.000"), (
        "both partial deliveries must reach authoritative stock"
    )
    assert await _count(pg_pool, "goods_receipts", namespace_id, po_ref="PO-PARTIAL-1") == 2
    assert await _count(pg_pool, "inventory_transactions", namespace_id, sku=sku) == 2

    async with pg_pool.acquire() as conn:
        stored = await conn.fetch(
            "SELECT delivery_note_ref FROM goods_receipts "
            "WHERE namespace_id = $1 AND po_ref = 'PO-PARTIAL-1' ORDER BY delivery_note_ref",
            namespace_id,
        )
    assert [r["delivery_note_ref"] for r in stored] == ["DN-A", "DN-B"]

    # A true retry of the SECOND note — in a different spelling — is a replay.
    retry = await do_record_goods_receipt(engine, _params("  dn-b  "))
    assert retry["duplicate"] is True
    assert retry["receipt_id"] == second["receipt_id"]
    assert await _get_on_hand(pg_pool, namespace_id, sku, loc) == Decimal("10.000")
    assert await _count(pg_pool, "inventory_transactions", namespace_id, sku=sku) == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_omitting_the_delivery_note_leaves_behaviour_exactly_as_before(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """The honest other half of F6, asserted rather than asserted-in-prose:
    with NO delivery note the parameter changes nothing — two identical
    submissions are still ONE receipt, and the disclosed collision between two
    genuinely distinct identical partial deliveries still stands for callers
    that supply neither a note nor scans."""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    engine = _EngineStub(pg_pool)
    sku = "SKU-NO-NOTE"
    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "po_ref": "PO-NO-NOTE-1",
        "location_id": loc,
        "lines": [{"sku": sku, "qty": 5}],
    }

    first = await do_record_goods_receipt(engine, dict(params))
    second = await do_record_goods_receipt(engine, dict(params))

    assert first["duplicate"] is False
    assert first["delivery_note_ref"] is None
    assert second["duplicate"] is True
    assert second["receipt_id"] == first["receipt_id"]
    assert await _get_on_hand(pg_pool, namespace_id, sku, loc) == Decimal("5.000")
    assert await _count(pg_pool, "goods_receipts", namespace_id, po_ref="PO-NO-NOTE-1") == 1
    assert await _count(pg_pool, "inventory_transactions", namespace_id, sku=sku) == 1

    # An explicitly blank note is "not supplied", not a distinguishing value.
    blank = await do_record_goods_receipt(engine, dict(params, delivery_note_ref="   "))
    assert blank["duplicate"] is True
    assert blank["receipt_id"] == first["receipt_id"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_invalid_namespace_id_is_named_not_the_valid_location(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    """``goods_receipts`` carries TWO independent foreign keys on its own
    receipt INSERT — the composite ``goods_receipts_location_fk`` AND the
    plain, Postgres-auto-named ``goods_receipts_namespace_id_fkey``. A REAL
    ``location_id`` (seeded under a REAL, DIFFERENT namespace) paired with a
    syntactically-valid but NONEXISTENT ``namespace_id`` must raise a
    ``ValueError`` naming ``namespace_id`` — never ``location_id``. A prior
    version of this function collapsed both FK violations into one
    location-flavoured message, actively mis-blaming a valid location when
    the real problem was an invalid tenant. RED if the except block stops
    branching on ``exc.constraint_name`` and reverts to a single
    unconditional message."""
    real_ns = await make_namespace()
    loc = await _seed_location(pg_pool, real_ns, "Warehouse")
    bogus_ns = uuid.uuid4()  # syntactically valid, no namespaces row at all

    with pytest.raises(ValueError, match="namespace_id") as excinfo:
        await do_record_goods_receipt(
            _EngineStub(pg_pool),
            {
                "namespace_id": bogus_ns,
                "po_ref": "PO-BOGUS-NS",
                "location_id": loc,
                "lines": [{"sku": "SKU-BOGUS-NS", "qty": 1}],
            },
        )
    message = str(excinfo.value)
    assert str(bogus_ns) in message, "must name the invalid namespace_id"
    assert "location_id" not in message, (
        "must not blame the (valid) location when the namespace itself is invalid"
    )
    assert "does not exist in this namespace" not in message, (
        "must not reuse the location-flavoured wording for a namespace failure"
    )

    # Nothing must have been written under the real namespace either.
    assert await _count(pg_pool, "goods_receipts", real_ns, po_ref="PO-BOGUS-NS") == 0
    assert await _count(pg_pool, "inventory_items", real_ns, sku="SKU-BOGUS-NS") == 0
    assert await _count(pg_pool, "inventory_transactions", real_ns, sku="SKU-BOGUS-NS") == 0


# ---------------------------------------------------------------------------
# (f) Structural claims, proven with raw SQL (DDL-discriminating, NOT
# code-discriminating — their mutation is deletion of the constraint from
# the migration, not a change to goods_receipt.py).
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_idempotency_unique_index_rejects_duplicate_hash(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Structural (DDL-discriminating): goods_receipts_idempotency_uq refuses
    a second row with the same (namespace_id, receipt_hash) even via a raw
    INSERT that bypasses do_record_goods_receipt's ON CONFLICT DO NOTHING
    entirely."""
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO goods_receipts "
            "(namespace_id, po_ref, location_id, lines, scans, receipt_hash) "
            "VALUES ($1, 'PO-DUP', $2, '[]'::jsonb, '[]'::jsonb, 'dup-hash-1')",
            namespace_id,
            loc,
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO goods_receipts "
                "(namespace_id, po_ref, location_id, lines, scans, receipt_hash) "
                "VALUES ($1, 'PO-DUP-2', $2, '[]'::jsonb, '[]'::jsonb, 'dup-hash-1')",
                namespace_id,
                loc,
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sign_matches_category_check_rejects_negative_goods_receipt_delta(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Structural (DDL-discriminating): migration 052's widened
    inventory_transactions_sign_matches_category CHECK refuses a
    'goods_receipt' row with a negative delta, at the DB level, independent
    of goods_receipt.py's own Python-side guard."""
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO inventory_transactions "
                "(namespace_id, sku, location_id, delta, reason_category) "
                "VALUES ($1, 'SKU-X', $2, -1, 'goods_receipt')",
                namespace_id,
                loc,
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reason_category_check_accepts_goods_receipt_and_rejects_bogus(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Structural (DDL-discriminating): migration 052's widened
    reason_category CHECK admits 'goods_receipt' (positive delta) and still
    refuses an unrelated bogus category."""
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    async with pg_pool.acquire() as conn:
        row_id = await conn.fetchval(
            "INSERT INTO inventory_transactions "
            "(namespace_id, sku, location_id, delta, reason_category) "
            "VALUES ($1, 'SKU-Y', $2, 1, 'goods_receipt') RETURNING id",
            namespace_id,
            loc,
        )
        assert row_id is not None

        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO inventory_transactions "
                "(namespace_id, sku, location_id, delta, reason_category) "
                "VALUES ($1, 'SKU-Y', $2, 1, 'not_a_real_category')",
                namespace_id,
                loc,
            )


# ---------------------------------------------------------------------------
# (g) FORCE RLS through a REAL unprivileged nce_app connection.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_force_rls_isolates_goods_receipts_through_real_nce_app_connection(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    """Isolation-discriminating, NOT code-discriminating. The ENGINE ITSELF
    holds an nce_app pool (precedent: tests/test_inventory_stock.py's
    test_rows_written_by_do_transfer_stock_are_rls_isolated) so
    scoped_pg_session's SET LOCAL nce.namespace_id is load-bearing on the
    write path: without it get_nce_namespace() is NULL and the
    tenant_isolation_policy WITH CHECK refuses the INSERT outright."""
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    await _seed_ownership(pg_pool, ns_a)
    loc_a = await _seed_location(pg_pool, ns_a, "Warehouse A")

    app_pool = await asyncpg.create_pool(_app_dsn(), min_size=1, max_size=2)
    engine = _EngineStub(app_pool)
    try:
        result = await do_record_goods_receipt(
            engine,
            {
                "namespace_id": ns_a,
                "po_ref": "PO-RLS-1",
                "location_id": loc_a,
                "lines": [{"sku": "SKU-RLS", "qty": 5, "unit_cost": Decimal("9.99")}],
            },
        )
        assert result["duplicate"] is False

        # ns_b must not see ns_a's receipt, even asking by explicit namespace_id
        # (RLS, not the query's own WHERE clause, is what refuses this).
        async with app_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, ns_b)
            visible_from_b = await conn.fetchval(
                "SELECT COUNT(*) FROM goods_receipts WHERE namespace_id = $1", ns_a
            )
        assert visible_from_b == 0, "ns_b must not see ns_a's goods_receipts row"

        async with app_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, ns_a)
            visible_from_a = await conn.fetchval(
                "SELECT COUNT(*) FROM goods_receipts WHERE namespace_id = $1", ns_a
            )
        assert visible_from_a == 1, "ns_a must see its own goods_receipts row"

        # Cross-namespace write refused: session context is ns_b, but the row
        # explicitly names ns_a — the WITH CHECK clause refuses the INSERT.
        async with app_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, ns_b)
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(
                    "INSERT INTO goods_receipts "
                    "(namespace_id, po_ref, location_id, lines, scans, receipt_hash) "
                    "VALUES ($1, 'PO-CROSS', $2, '[]'::jsonb, '[]'::jsonb, 'cross-ns-hash')",
                    ns_a,
                    loc_a,
                )
    finally:
        await app_pool.close()


# ---------------------------------------------------------------------------
# (h) END-TO-END VALUATION — B139's declared scope limit, discharged.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_valuation_over_receipt_stock_discriminates_fifo_from_average(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first test in the program where the stock being valued actually
    arrived through a real receipt, not a seeded stand-in row (B139's
    'Honest scope limit' docstring section, now discharged for this ONE
    writer). RED if unit_cost were dropped between params and
    append_transaction: both FIFO and average would then read as 0.00 and
    could never discriminate from each other."""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    engine = _EngineStub(pg_pool)
    sku = "SKU-VALUATION-GR"

    # Two receipts (real DB rows) at different unit costs so FIFO (oldest
    # layer first) and average (blended) give DIFFERENT numbers once 10
    # units are consumed. do_valuation orders by created_at (append_transaction
    # defaults it to the writing transaction's now()) — the explicit sleep
    # between the two sequential calls guarantees a strictly later timestamp
    # for the second receipt's ledger row, so which layer is "oldest" is
    # never a wall-clock-timing coin flip (the exact risk
    # test_inventory_transactions.py's own seeding helper calls out by using
    # an explicit created_at instead of relying on now()).
    await do_record_goods_receipt(
        engine,
        {
            "namespace_id": namespace_id,
            "po_ref": "PO-VAL-1",
            "location_id": loc,
            "lines": [{"sku": sku, "qty": 10, "unit_cost": Decimal("10.00")}],
        },
    )
    await asyncio.sleep(0.05)
    await do_record_goods_receipt(
        engine,
        {
            "namespace_id": namespace_id,
            "po_ref": "PO-VAL-2",
            "location_id": loc,
            "lines": [{"sku": sku, "qty": 10, "unit_cost": Decimal("20.00")}],
        },
    )
    # Consume 10 via a plain adjustment row (this wave writes no consumption
    # writer of its own — do_valuation only needs SOME outbound row to make
    # FIFO and average diverge; the ledger's own reason_category vocabulary
    # already includes 'adjustment' for exactly this open-signed use).
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO inventory_transactions "
            "(namespace_id, sku, location_id, delta, reason_category) "
            "VALUES ($1, $2, $3, -10, 'adjustment')",
            namespace_id,
            sku,
            loc,
        )

    monkeypatch.setattr(transactions, "load_inventory_valuation_config", lambda: {"method": "fifo"})
    fifo_result = await do_valuation(
        engine, {"namespace_id": namespace_id, "sku": sku, "location": loc}
    )
    assert fifo_result["ok"] is True
    assert fifo_result["value"] == Decimal("200.00"), "FIFO must consume the CHEAPEST layer first"
    assert fifo_result["remaining_qty"] == Decimal("10.000")

    monkeypatch.setattr(
        transactions, "load_inventory_valuation_config", lambda: {"method": "average"}
    )
    avg_result = await do_valuation(
        engine, {"namespace_id": namespace_id, "sku": sku, "location": loc}
    )
    assert avg_result["value"] == Decimal("150.00"), "average must blend to 15.00/unit"
    assert avg_result["remaining_qty"] == Decimal("10.000")

    assert fifo_result["value"] != avg_result["value"], "must discriminate FIFO from average"


# ---------------------------------------------------------------------------
# (i) Decimal discipline, discriminatingly — round-tripped through real
# NUMERIC(18,3)/NUMERIC(18,2) columns.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_qty_and_cost_are_quantised_via_decimal_str_not_raw_binary_float(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Discriminating values verified empirically against this module's own
    2dp/3dp scales (never assumed from a different column's scale — 2.6755
    diverges at 3dp, per B130's audit of stock.py's qty column, but rounds
    to 2.68 under EITHER path at 2dp, so it would NOT discriminate the cost
    column; verified via ``Decimal(str(x)).quantize(...)`` vs
    ``Decimal(x).quantize(...)`` before writing this test). 0.0045 (qty,
    3dp) and 1.005 (unit_cost, 2dp — the classic ``1.005`` binary-float
    gotcha: the nearest double is ~1.00499999999999989...) both sit just
    below their respective decimal ties, so ``Decimal(x)`` quantises ONE
    TICK LOWER than ``Decimal(str(x))``. 1.23456 is included only as a
    plain-truncation sanity check — it does NOT discriminate the two paths
    (both give 1.235) and must not be read as proof of anything on its
    own."""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    engine = _EngineStub(pg_pool)

    async def _receive(po_ref: str, qty: float, unit_cost: float) -> dict[str, Any]:
        return await do_record_goods_receipt(
            engine,
            {
                "namespace_id": namespace_id,
                "po_ref": po_ref,
                "location_id": loc,
                "lines": [{"sku": "SKU-QUANT-GR", "qty": qty, "unit_cost": unit_cost}],
            },
        )

    # Plain truncation, no float divergence — sanity check only.
    r1 = await _receive("PO-QUANT-1", 1.23456, 1.0)
    assert r1["lines"][0]["qty"] == Decimal("1.235")

    # Divergent #1: Decimal(x) would yield 0.004 for the qty here (3dp).
    r2 = await _receive("PO-QUANT-2", 0.0045, 1.0)
    assert r2["lines"][0]["qty"] == Decimal("0.005"), (
        "0.0045 must quantise via Decimal(str(x)) to 0.005; 0.004 means the "
        "raw binary-float value was quantised instead"
    )

    # Divergent #2, on the COST column (2dp): Decimal(x) would yield 1.00.
    r3 = await _receive("PO-QUANT-3", 1.0, 1.005)
    assert r3["lines"][0]["unit_cost"] == Decimal("1.01"), (
        "1.005 must quantise via Decimal(str(x)) to 1.01; 1.00 means the "
        "raw binary-float value was quantised instead"
    )

    # Same numbers, re-read from the real NUMERIC columns.
    on_hand = await _get_on_hand(pg_pool, namespace_id, "SKU-QUANT-GR", loc)
    assert on_hand == Decimal("1.235") + Decimal("0.005") + Decimal("1.000")

    async with pg_pool.acquire() as conn:
        cost_row = await conn.fetchrow(
            "SELECT unit_cost FROM inventory_transactions "
            "WHERE namespace_id = $1 AND sku = $2 AND ref = $3",
            namespace_id,
            "SKU-QUANT-GR",
            r3["receipt_id"],
        )
    assert cost_row["unit_cost"] == Decimal("1.01")


# ---------------------------------------------------------------------------
# (Batch 132b) Graph projection + Assets hand-off — the criterion is a
# REFUSAL and a hand-off, not a success (see this wave's Acceptance:).
# ---------------------------------------------------------------------------


async def _fetch_node(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    label: str,
) -> asyncpg.Record | None:  # type: ignore[type-arg]
    async with pg_pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT label, entity_type FROM kg_nodes WHERE namespace_id = $1 AND label = $2",
            namespace_id,
            label,
        )


async def _fetch_edges(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    subject_label: str,
    predicate: str,
) -> list[Any]:
    async with pg_pool.acquire() as conn:
        return await conn.fetch(
            "SELECT object_label, confidence FROM kg_edges "
            "WHERE namespace_id = $1 AND subject_label = $2 AND predicate = $3 "
            "ORDER BY object_label",
            namespace_id,
            subject_label,
            predicate,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_projection_lands_node_and_edges(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """RED if: the GOODS_RECEIPT node is missing/duplicated, the against
    edge is wrong, the of edges are not exactly one per sku, or confidence
    is anything but 1.0 on the edges / present on the node."""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse Graph")
    engine = _EngineStub(pg_pool)

    result = await do_record_goods_receipt(
        engine,
        {
            "namespace_id": namespace_id,
            "po_ref": "po-graph-1",
            "location_id": loc,
            "lines": [
                {"sku": "sku-graph-a", "qty": 1},
                {"sku": "SKU-GRAPH-B", "qty": 2},
                {"sku": "Sku-Graph-C", "qty": 3},
            ],
        },
    )
    assert result["duplicate"] is False
    receipt_label = f"GOODS_RECEIPT:{result['receipt_id']}"

    node = await _fetch_node(pg_pool, namespace_id, receipt_label)
    assert node is not None, "exactly one GOODS_RECEIPT kg_nodes row must exist"
    assert node["entity_type"] == "GOODS_RECEIPT"
    assert "confidence" not in node.keys(), "kg_nodes carries no confidence column at all"

    against = await _fetch_edges(pg_pool, namespace_id, receipt_label, "against")
    assert len(against) == 1
    assert against[0]["object_label"] == "PO:PO-GRAPH-1"
    assert against[0]["confidence"] == 1.0

    of_edges = await _fetch_edges(pg_pool, namespace_id, receipt_label, "of")
    assert len(of_edges) == 3, "exactly one of edge per aggregated sku, never per input row"
    assert [e["object_label"] for e in of_edges] == [
        "PRODUCT_SKU:SKU-GRAPH-A",
        "PRODUCT_SKU:SKU-GRAPH-B",
        "PRODUCT_SKU:SKU-GRAPH-C",
    ]
    assert all(e["confidence"] == 1.0 for e in of_edges)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unseeded_namespace_denies_and_rolls_back_everything(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    """Guard-discriminating. RED if OwnershipError is not raised, OR if it
    is raised but any of the receipt row / ledger row / stock increment
    survives — this is the blast radius made executable."""
    ns = await make_namespace()  # deliberately NOT seeded
    loc = await _seed_location(pg_pool, ns, "Warehouse Unseeded")
    engine = _EngineStub(pg_pool)

    with pytest.raises(OwnershipError):
        await do_record_goods_receipt(
            engine,
            {
                "namespace_id": ns,
                "po_ref": "PO-DENY-1",
                "location_id": loc,
                "lines": [{"sku": "SKU-DENY", "qty": 5, "unit_cost": Decimal("1.00")}],
            },
        )

    assert await _count(pg_pool, "goods_receipts", ns, po_ref="PO-DENY-1") == 0
    assert await _count(pg_pool, "inventory_transactions", ns, sku="SKU-DENY") == 0
    assert await _get_on_hand(pg_pool, ns, "SKU-DENY", loc) == Decimal("0.000")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_wrong_owner_is_refused_at_the_private_writer_call_site(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    monkeypatch: Any,
) -> None:
    """Guard-discriminating, and NOT a bare assert_owner(...) call — this
    drives goods_receipt.py's OWN private writer (_upsert_goods_receipt_node)
    with a node type ("PO") owned by a different engine (procurement),
    by monkeypatching the module's node-type constant for this test only.
    RED if the writer does not refuse, or refuses without naming
    'procurement', or a kg_nodes row is written anyway."""
    await _seed_ownership(pg_pool, namespace_id)
    monkeypatch.setattr(gr_module, "_NODE_TYPE_GOODS_RECEIPT", "PO")
    label = "PO:WRONG-OWNER-TEST"

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)
            with pytest.raises(OwnershipError) as excinfo:
                await _upsert_goods_receipt_node(conn, namespace_id, label)
    assert "procurement" in str(excinfo.value)
    assert await _fetch_node(pg_pool, namespace_id, label) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_assets_seam_fires_exactly_once_with_serials_mocked(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """RED if the seam is not called exactly once for a serial-carrying
    receipt, is called with the wrong payload, or IS called for a receipt
    with no serials at all."""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse Seam")
    engine = _EngineStub(pg_pool)

    with patch(
        "nce.vertical_modules.inventory.goods_receipt._seed_assets_with_serials",
        new_callable=AsyncMock,
    ) as mock_seam:
        mock_seam.return_value = {"seeded": True}

        result = await do_record_goods_receipt(
            engine,
            {
                "namespace_id": namespace_id,
                "po_ref": "PO-SEAM-1",
                "location_id": loc,
                "lines": [{"sku": "SKU-SEAM", "qty": 2}],
                "scans": [
                    {"sku": "SKU-SEAM", "serial": "SN-001"},
                    {"sku": "SKU-SEAM", "serial": "SN-002"},
                ],
            },
        )
        mock_seam.assert_called_once()
        call_kwargs = mock_seam.call_args.kwargs
        assert call_kwargs["goods_receipt_id"] == uuid.UUID(result["receipt_id"])
        assert call_kwargs["goods_receipt_label"] == f"GOODS_RECEIPT:{result['receipt_id']}"
        assert call_kwargs["po_ref"] == "PO-SEAM-1"
        assert call_kwargs["location_id"] == loc
        assert call_kwargs["serials"] == [
            {"sku": "SKU-SEAM", "serial": "SN-001"},
            {"sku": "SKU-SEAM", "serial": "SN-002"},
        ]
        assert result["assets_seed"] == {"seeded": True}

    with patch(
        "nce.vertical_modules.inventory.goods_receipt._seed_assets_with_serials",
        new_callable=AsyncMock,
    ) as mock_seam_no_serials:
        result2 = await do_record_goods_receipt(
            engine,
            {
                "namespace_id": namespace_id,
                "po_ref": "PO-SEAM-2",
                "location_id": loc,
                "lines": [{"sku": "SKU-SEAM-2", "qty": 1}],
            },
        )
        mock_seam_no_serials.assert_not_called()
        assert "assets_seed" not in result2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_serial_carrying_receipt_without_mock_seeds_a_real_asset(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """RED if the real (unmocked) call to do_seed_asset_from_bom raises, or
    if it fails to seed a real ``assets`` row for the captured serial. This
    replaces the old NotImplementedError-expecting test: Batch 132j wires a
    real Assets consumer, so the seam no longer raises on this path."""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse Seam Real")
    engine = _EngineStub(pg_pool)

    result = await do_record_goods_receipt(
        engine,
        {
            "namespace_id": namespace_id,
            "po_ref": "PO-SEAM-REAL-1",
            "location_id": loc,
            "lines": [{"sku": "SKU-SEAM-REAL", "qty": 1}],
            "scans": [{"sku": "SKU-SEAM-REAL", "serial": "SN-REAL-1"}],
        },
    )

    assert await _count(pg_pool, "goods_receipts", namespace_id, po_ref="PO-SEAM-REAL-1") == 1
    assert len(result["assets_seed"]) == 1
    seeded = result["assets_seed"][0]
    assert seeded["ok"] is True
    assert seeded["created"] is True
    assert seeded["serial"] == "SN-REAL-1"
    expected_bom_line_id = f"goods-receipt:{result['receipt_id']}:SKU-SEAM-REAL:SN-REAL-1"
    assert seeded["bom_line_id"] == expected_bom_line_id

    async with pg_pool.acquire() as conn:
        asset_row = await conn.fetchrow(
            "SELECT serial, bom_line_id, functional_location_id FROM assets "
            "WHERE namespace_id = $1 AND bom_line_id = $2",
            namespace_id,
            expected_bom_line_id,
        )
    assert asset_row is not None
    assert asset_row["serial"] == "SN-REAL-1"
    assert asset_row["functional_location_id"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_partial_asset_seed_failure_does_not_roll_back_the_receipt(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Pins Batch 132j round-2's failure posture: a goods receipt is a
    physical fact that already happened, so an asset-projection failure for
    one serial must NOT roll back the receipt, the stock increment, the
    ledger append or the graph writes. RED if any of those are lost, if the
    exception propagates out of do_record_goods_receipt, or if the failed
    serial is not recorded in ``assets_seed`` for later reconciliation."""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse Seam Partial")
    engine = _EngineStub(pg_pool)
    real_seed = gr_module.do_seed_asset_from_bom

    async def _flaky_seed(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
        if params["serial"] == "SN-FAIL-2":
            raise ValueError("simulated Assets failure")
        return await real_seed(engine, params)

    with patch(
        "nce.vertical_modules.inventory.goods_receipt.do_seed_asset_from_bom",
        new=_flaky_seed,
    ):
        result = await do_record_goods_receipt(
            engine,
            {
                "namespace_id": namespace_id,
                "po_ref": "PO-SEAM-PARTIAL-1",
                "location_id": loc,
                "lines": [{"sku": "SKU-SEAM-PARTIAL", "qty": 2}],
                "scans": [
                    {"sku": "SKU-SEAM-PARTIAL", "serial": "SN-FAIL-1"},
                    {"sku": "SKU-SEAM-PARTIAL", "serial": "SN-FAIL-2"},
                ],
            },
        )

    # The receipt, the increment and the ledger append all commit regardless.
    assert await _count(pg_pool, "goods_receipts", namespace_id, po_ref="PO-SEAM-PARTIAL-1") == 1
    assert (
        await _count(pg_pool, "inventory_transactions", namespace_id, sku="SKU-SEAM-PARTIAL") == 1
    )
    async with pg_pool.acquire() as conn:
        node_count = await conn.fetchval(
            "SELECT COUNT(*) FROM kg_nodes WHERE namespace_id = $1 AND entity_type = 'GOODS_RECEIPT'",
            namespace_id,
        )
    assert node_count == 1

    assert len(result["assets_seed"]) == 2
    ok_entry, failed_entry = result["assets_seed"]
    assert ok_entry["ok"] is True
    assert ok_entry["serial"] == "SN-FAIL-1"
    assert failed_entry == {
        "ok": False,
        "sku": "SKU-SEAM-PARTIAL",
        "serial": "SN-FAIL-2",
        "error": "simulated Assets failure",
    }

    async with pg_pool.acquire() as conn:
        seeded_count = await conn.fetchval(
            "SELECT COUNT(*) FROM assets WHERE namespace_id = $1 AND serial = $2",
            namespace_id,
            "SN-FAIL-1",
        )
        unseeded_count = await conn.fetchval(
            "SELECT COUNT(*) FROM assets WHERE namespace_id = $1 AND serial = $2",
            namespace_id,
            "SN-FAIL-2",
        )
    assert seeded_count == 1
    assert unseeded_count == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_performs_no_graph_write_and_no_seam_call(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """RED if a replay adds any kg_nodes/kg_edges row or re-fires the seam."""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse Replay Graph")
    engine = _EngineStub(pg_pool)
    params = {
        "namespace_id": namespace_id,
        "po_ref": "PO-REPLAY-GRAPH-1",
        "location_id": loc,
        "lines": [{"sku": "SKU-REPLAY-GRAPH", "qty": 1}],
        "scans": [{"sku": "SKU-REPLAY-GRAPH", "serial": "SN-REPLAY-1"}],
    }

    with patch(
        "nce.vertical_modules.inventory.goods_receipt._seed_assets_with_serials",
        new_callable=AsyncMock,
    ) as mock_seam:
        first = await do_record_goods_receipt(engine, dict(params))
        assert first["duplicate"] is False
        mock_seam.assert_called_once()

        async with pg_pool.acquire() as conn:
            nodes_before = await conn.fetchval(
                "SELECT COUNT(*) FROM kg_nodes WHERE namespace_id = $1", namespace_id
            )
            edges_before = await conn.fetchval(
                "SELECT COUNT(*) FROM kg_edges WHERE namespace_id = $1", namespace_id
            )

        second = await do_record_goods_receipt(engine, dict(params))
        assert second["duplicate"] is True
        mock_seam.assert_called_once()  # still exactly 1 — replay did not re-fire it

        async with pg_pool.acquire() as conn:
            nodes_after = await conn.fetchval(
                "SELECT COUNT(*) FROM kg_nodes WHERE namespace_id = $1", namespace_id
            )
            edges_after = await conn.fetchval(
                "SELECT COUNT(*) FROM kg_edges WHERE namespace_id = $1", namespace_id
            )
    assert nodes_after == nodes_before
    assert edges_after == edges_before


@pytest.mark.integration
@pytest.mark.asyncio
async def test_graph_writes_are_namespace_scoped_through_real_nce_app_connection(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    """Isolation-discriminating, NOT code-discriminating (rule 7g) — proven
    through a REAL unprivileged nce_app connection, never the owner pool
    (which bypasses FORCE RLS)."""
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    await _seed_ownership(pg_pool, ns_a)
    await _seed_ownership(pg_pool, ns_b)
    loc_a = await _seed_location(pg_pool, ns_a, "Warehouse Iso A")
    loc_b = await _seed_location(pg_pool, ns_b, "Warehouse Iso B")

    app_pool = await asyncpg.create_pool(_app_dsn(), min_size=1, max_size=2)
    try:
        engine = _EngineStub(app_pool)
        for ns, loc in ((ns_a, loc_a), (ns_b, loc_b)):
            result = await do_record_goods_receipt(
                engine,
                {
                    "namespace_id": ns,
                    "po_ref": "PO-ISO-SHARED",
                    "location_id": loc,
                    "lines": [{"sku": "SKU-ISO-SHARED", "qty": 1}],
                },
            )
            assert result["duplicate"] is False

        for ns in (ns_a, ns_b):
            async with app_pool.acquire() as conn, conn.transaction():
                await set_namespace_context(conn, ns)
                own_nodes = await conn.fetchval(
                    "SELECT COUNT(*) FROM kg_nodes WHERE entity_type = 'GOODS_RECEIPT'"
                )
                own_edges = await conn.fetchval(
                    "SELECT COUNT(*) FROM kg_edges WHERE predicate = 'of' "
                    "AND object_label = 'PRODUCT_SKU:SKU-ISO-SHARED'"
                )
            assert own_nodes == 1, f"namespace {ns} must see exactly its own GOODS_RECEIPT node"
            assert own_edges == 1, f"namespace {ns} must see exactly its own of edge"
    finally:
        await app_pool.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_of_edge_targets_product_sku_never_product_label(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Pins a KNOWN LIMITATION (not a desirable property, per this module's
    docstring): the of edge cannot reconstruct Product's compound label."""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse Label Limit")
    engine = _EngineStub(pg_pool)

    result = await do_record_goods_receipt(
        engine,
        {
            "namespace_id": namespace_id,
            "po_ref": "PO-LABEL-LIMIT",
            "location_id": loc,
            "lines": [{"sku": "ACME-PN-123", "qty": 1}],
        },
    )
    receipt_label = f"GOODS_RECEIPT:{result['receipt_id']}"
    of_edges = await _fetch_edges(pg_pool, namespace_id, receipt_label, "of")
    assert len(of_edges) == 1
    assert of_edges[0]["object_label"] == "PRODUCT_SKU:ACME-PN-123"
    assert not of_edges[0]["object_label"].startswith("PRODUCT:"), (
        "must never emit a PRODUCT:{manufacturer}:{mfr_part_no} label — "
        "this module cannot reconstruct it from a flat sku (known limitation)"
    )
