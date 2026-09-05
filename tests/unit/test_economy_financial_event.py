"""
tests/unit/test_economy_financial_event.py
===========================================
Acceptance tests for Batch 118 — Module 8.Wave 3 (financial-event).

Split the same way as ``test_economy_match.py``:
  (a) REFERENCE tests — lifted from the reference implementation's ``tests/finance/events.test.ts`` and from the
      ``assertBalanced`` contract in the reference implementation. The reference cases are
      DB-backed (they assert on the persisted row); the persistence half belongs to Wave 6,
      so each case is lifted to the half this wave owns: the balance verdict, the normalised
      shape, and the canonical hash.
  (b) WAVE tests — this wave's required cases: the epsilon boundary from both directions,
      never-auto-balances, hash stability/order rules, mixed Decimal/int/float postings.
  (c) BOUNDARY tests — the hostile-input surface (B116's lesson: every defect but none of
      the arithmetic lived at the coercion boundary).

All plain unit tests: no DB, no HTTP, no ``@pytest.mark.integration``.

``_EPSILON`` is a literal defined HERE, never read from config — epsilon is a parameter of
``do_emit_financial_event`` precisely so the boundary is testable from both sides.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from nce.vertical_modules.economy.events import (
    UnbalancedPostingsError,
    do_emit_financial_event,
)

# The call-site default (NCE_ECONOMY_BALANCE_EPSILON = 0.01) as a local literal.
_EPSILON = 0.01

_BASE_EVENT = {
    "type": "supplier.invoice.approved",
    "entity_type": "SupplierInvoice",
    "entity_id": "inv-123",
    "occurred_at": "2026-04-18T10:00:00Z",
    "actor_type": "user",
    "actor_id": "user-oyvind",
    "retention_tier": "primary",
    "payload": {"invoice_id": "inv-123"},
}


def _event(**overrides: object) -> dict:
    """A fresh copy of the base event with *overrides* applied."""
    event = dict(_BASE_EVENT)
    event.update(overrides)
    return event


def _balanced_postings() -> list[dict]:
    return [
        {"account": "4300", "amount": Decimal("100000.00"), "comment": "Varekost"},
        {"account": "2400", "amount": Decimal("-100000.00"), "comment": "Leverandorgjeld"},
    ]


# ===========================================================================
# (a) REFERENCE tests — lifted from events.test.ts / assertBalanced
# ===========================================================================


def test_balanced_event_returns_postings_and_hash() -> None:
    """Reference: "persists an event with computed hash" — the half this wave owns."""
    result = do_emit_financial_event(_EPSILON, _event(postings=_balanced_postings()))

    assert set(result) >= {"postings", "hash", "type"}
    assert len(result["hash"]) == 64
    assert all(char in "0123456789abcdef" for char in result["hash"])
    assert result["type"] == "supplier.invoice.approved"
    assert result["retention_tier"] == "primary"
    assert [posting["account"] for posting in result["postings"]] == ["4300", "2400"]


def test_hash_is_stable_across_identical_events() -> None:
    """Reference: "hash is stable across identical emits (same canonical body -> same hash)"."""
    first = do_emit_financial_event(_EPSILON, _event(postings=_balanced_postings()))
    second = do_emit_financial_event(_EPSILON, _event(postings=_balanced_postings()))

    assert first["hash"] == second["hash"]


def test_different_payload_produces_different_hash() -> None:
    """Reference: "different payload produces different hash"."""
    first = do_emit_financial_event(_EPSILON, _event(payload={"month": "2026-04"}))
    second = do_emit_financial_event(_EPSILON, _event(payload={"month": "2026-05"}))

    assert first["hash"] != second["hash"]


def test_caused_by_and_correlation_id_are_carried_and_hashed() -> None:
    """Reference: "supports causedBy + correlationId for cascade traceability"."""
    child = _event(
        type="project.margin.recalculated",
        caused_by="evt-parent",
        correlation_id="evt-parent",
    )
    result = do_emit_financial_event(_EPSILON, child)

    assert result["caused_by"] == "evt-parent"
    assert result["correlation_id"] == "evt-parent"
    # The traceability fields are part of the attested content, not decoration.
    without = do_emit_financial_event(_EPSILON, _event(type="project.margin.recalculated"))
    assert result["hash"] != without["hash"]


def test_explicit_retention_tier_is_preserved() -> None:
    """Reference: "accepts explicit retention tier"."""
    result = do_emit_financial_event(_EPSILON, _event(retention_tier="permanent"))

    assert result["retention_tier"] == "permanent"


def test_unbalanced_event_raises() -> None:
    """Reference ``assertBalanced``: sum outside tolerance -> UnbalancedPostingsError."""
    postings = [
        {"account": "4300", "amount": Decimal("100000.00")},
        {"account": "2400", "amount": Decimal("-99999.00")},
    ]
    with pytest.raises(UnbalancedPostingsError) as excinfo:
        do_emit_financial_event(_EPSILON, _event(postings=postings))

    error = excinfo.value
    assert error.event_type == "supplier.invoice.approved"
    assert error.diff == Decimal("1.00")
    assert len(error.postings) == 2
    assert "unbalanced postings" in str(error)


def test_event_without_postings_passes() -> None:
    """Reference: ``if (!postings || postings.length === 0) return`` — cascade events bear
    no postings and must not be rejected."""
    assert do_emit_financial_event(_EPSILON, _event())["postings"] is None
    assert do_emit_financial_event(_EPSILON, _event(postings=None))["postings"] is None
    assert do_emit_financial_event(_EPSILON, _event(postings=[]))["postings"] == []


def test_empty_postings_and_absent_postings_hash_differently() -> None:
    """``[]`` is not ``None`` — the reference stores ``postings ?? null``, so an explicit
    empty journal and no journal at all are distinct content."""
    absent = do_emit_financial_event(_EPSILON, _event())
    empty = do_emit_financial_event(_EPSILON, _event(postings=[]))

    assert absent["hash"] != empty["hash"]


# ===========================================================================
# (b) WAVE tests — epsilon boundary, no-auto-balance, hash rules, mixed types
# ===========================================================================


@pytest.mark.parametrize("residue", ["0.01", "-0.01", "0.009", "-0.009", "0"])
def test_epsilon_boundary_passes_at_and_inside_tolerance(residue: str) -> None:
    """Ported ``Math.abs(sum) > TOLERANCE`` — ``>``, never ``>=``: exactly epsilon PASSES,
    from above and from below."""
    postings = [
        {"account": "4300", "amount": Decimal("100.00")},
        {"account": "2400", "amount": Decimal("-100.00") + Decimal(residue)},
    ]
    result = do_emit_financial_event(_EPSILON, _event(postings=postings))

    assert len(result["postings"]) == 2


@pytest.mark.parametrize("residue", ["0.011", "-0.011", "0.0100001", "-0.0100001"])
def test_epsilon_boundary_rejects_just_outside_tolerance(residue: str) -> None:
    """The other side of the same boundary, also from above and below."""
    postings = [
        {"account": "4300", "amount": Decimal("100.00")},
        {"account": "2400", "amount": Decimal("-100.00") + Decimal(residue)},
    ]
    with pytest.raises(UnbalancedPostingsError):
        do_emit_financial_event(_EPSILON, _event(postings=postings))


def test_tolerance_comparison_is_exact_beyond_default_precision() -> None:
    """The sum is compared with ``copy_abs()``, which is context-independent.

    ``abs()`` is a context operation and rounds to the ambient 28-digit precision: this sum
    is ``0.010000000000000000000000000001`` (29 significant digits), which ``abs()`` rounds
    to exactly ``0.01`` — landing it back inside the tolerance and letting an out-of-balance
    event through. Silently permissive, in the one direction that matters.
    """
    postings = [
        {"account": "4300", "amount": Decimal("100")},
        {"account": "2400", "amount": Decimal("-99.989999999999999999999999999999")},
    ]
    with pytest.raises(UnbalancedPostingsError):
        do_emit_financial_event(_EPSILON, _event(postings=postings))


def test_zero_epsilon_demands_exact_balance() -> None:
    """``epsilon = 0`` is legal and means exact. One ore out is out."""
    exact = [{"account": "4300", "amount": 100}, {"account": "2400", "amount": -100}]
    assert do_emit_financial_event(0, _event(postings=exact))["postings"]

    one_ore_out = [
        {"account": "4300", "amount": Decimal("100.00")},
        {"account": "2400", "amount": Decimal("-99.99")},
    ]
    with pytest.raises(UnbalancedPostingsError):
        do_emit_financial_event(0, _event(postings=one_ore_out))


def test_float_postings_sum_exactly_not_as_binary_floats() -> None:
    """``0.1 + 0.2 - 0.3`` is ``5.55e-17`` in binary floats and exactly ``0`` in Decimal.

    Pinned with ``epsilon = 0`` so no tolerance can hide the drift: this test is RED if the
    sum is ever computed in ``float``, and it is the reason this module coerces through
    ``Decimal(str(value))``.
    """
    postings = [
        {"account": "4300", "amount": 0.1},
        {"account": "4301", "amount": 0.2},
        {"account": "2400", "amount": -0.3},
    ]
    result = do_emit_financial_event(0, _event(postings=postings))

    assert [posting["amount"] for posting in result["postings"]] == [
        Decimal("0.1"),
        Decimal("0.2"),
        Decimal("-0.3"),
    ]


def test_mixed_decimal_int_float_postings_balance() -> None:
    """Money arrives as ``Decimal`` from Postgres ``numeric``, as ``int``/``float`` from
    JSON. ``Decimal - float`` raises TypeError in Python; every amount is coerced first, so
    a mixed journal must simply balance."""
    postings = [
        {"account": "4300", "amount": Decimal("100.005")},
        {"account": "4301", "amount": 50},
        {"account": "2400", "amount": -150.0},
        {"account": "2740", "amount": Decimal("-0.005")},
    ]
    result = do_emit_financial_event(0, _event(postings=postings))

    assert len(result["postings"]) == 4


def test_numerically_equal_amounts_hash_identically_across_types() -> None:
    """``100``, ``100.0`` and ``Decimal("100.00")`` are the same money, so the same hash."""
    hashes = {
        do_emit_financial_event(
            _EPSILON,
            _event(
                postings=[
                    {"account": "4300", "amount": amount},
                    {"account": "2400", "amount": negative},
                ]
            ),
        )["hash"]
        for amount, negative in (
            (100, -100),
            (100.0, -100.0),
            (Decimal("100.00"), Decimal("-100.00")),
            (Decimal("1E+2"), Decimal("-1E+2")),
        )
    }

    assert len(hashes) == 1


def test_hash_ignores_dict_key_order_but_not_posting_order() -> None:
    """Key order IS normalised (ported ``canonicalJson`` key sort, recursively). Posting
    ELEMENT order is NOT — a JSON array is ordered, and silently reordering a caller's
    journal to make hashes agree would be a rewrite of their data."""
    forward = {
        "type": "supplier.invoice.approved",
        "payload": {"a": 1, "b": 2},
        "postings": _balanced_postings(),
    }
    reordered_keys = {
        "payload": {"b": 2, "a": 1},
        "postings": _balanced_postings(),
        "type": "supplier.invoice.approved",
    }
    reordered_postings = {
        "type": "supplier.invoice.approved",
        "payload": {"a": 1, "b": 2},
        "postings": list(reversed(_balanced_postings())),
    }

    assert (
        do_emit_financial_event(_EPSILON, forward)["hash"]
        == do_emit_financial_event(_EPSILON, reordered_keys)["hash"]
    )
    assert (
        do_emit_financial_event(_EPSILON, forward)["hash"]
        != do_emit_financial_event(_EPSILON, reordered_postings)["hash"]
    )


def test_hash_excludes_db_filled_fields_only() -> None:
    """``id``/``recorded_at``/``hash`` are excluded (the DB fills them; including them makes
    the hash non-deterministic). Everything else is attested."""
    plain = do_emit_financial_event(_EPSILON, _event())
    with_db_fields = do_emit_financial_event(
        _EPSILON,
        _event(id="evt-1", recorded_at="2026-04-18T10:00:01Z", hash="stale-value"),
    )
    with_business_field = do_emit_financial_event(_EPSILON, _event(project_id="proj-1"))

    assert plain["hash"] == with_db_fields["hash"]
    assert plain["hash"] != with_business_field["hash"]
    # A stale inbound hash is replaced, never trusted.
    assert with_db_fields["hash"] != "stale-value"


def test_number_and_its_string_spelling_hash_differently() -> None:
    """The canonical encoding is type-tagged, so ``0.1`` cannot collide with ``"0.1"``. A
    hash collision is a silent failure — an edited event would verify as intact."""
    as_number = do_emit_financial_event(_EPSILON, _event(payload={"rate": 0.1}))
    as_string = do_emit_financial_event(_EPSILON, _event(payload={"rate": "0.1"}))

    assert as_number["hash"] != as_string["hash"]


def test_hash_handles_nested_payload_and_datetimes() -> None:
    """Nested structures, lists and datetimes are canonicalised, and nested key order is
    normalised too.

    CHANGED (B118 audit fix 1): this test used to pass a NAIVE ``datetime(2026, 4, 30, 23,
    0, 0)``, which the module hashed via bare ``isoformat()``. That spelling is now rejected
    outright, so the case is re-pinned with the same instant expressed as an aware datetime.
    The naive rejection itself is pinned by ``test_naive_datetime_is_rejected``; what THIS
    test still owns is the nested-structure/key-order behaviour, unchanged.
    """
    occurred = datetime(2026, 4, 30, 23, 0, 0, tzinfo=timezone.utc)
    first = do_emit_financial_event(
        _EPSILON,
        _event(occurred_at=occurred, payload={"lines": [{"x": 1, "y": 2}], "flag": True}),
    )
    second = do_emit_financial_event(
        _EPSILON,
        _event(occurred_at=occurred, payload={"flag": True, "lines": [{"y": 2, "x": 1}]}),
    )

    assert first["hash"] == second["hash"]


# --- datetimes: one instant, one hash (B118 audit fix 1) -------------------


def test_same_instant_hashes_identically_across_offsets() -> None:
    """PRE-FIX: ``occurred_at`` was hashed with bare ``isoformat()``, which renders whatever
    offset the datetime carries, so ONE instant had as many hashes as spellings::

        2026-04-18T12:00:00+02:00 -> bb97ed46d53ac430
        2026-04-18T10:00:00+00:00 -> fa08e834298f3845   same instant, different hash

    Wave 6 stores ``occurred_at`` as ``timestamptz`` and asyncpg returns it UTC-aware, so an
    event emitted with an Oslo-offset datetime — the natural thing for a Norwegian caller —
    verified as TAMPERED as soon as its hash was recomputed from the stored row. Amounts and
    accounts were never the risk here; the timestamp was.
    """
    oslo_summer = timezone(timedelta(hours=2))
    new_york = timezone(timedelta(hours=-4))
    same_instant = [
        datetime(2026, 4, 18, 12, 0, 0, tzinfo=oslo_summer),
        datetime(2026, 4, 18, 10, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 18, 6, 0, 0, tzinfo=new_york),
    ]

    hashes = {
        do_emit_financial_event(_EPSILON, _event(occurred_at=moment))["hash"]
        for moment in same_instant
    }

    assert len(hashes) == 1


def test_same_wall_clock_in_different_zones_hashes_differently() -> None:
    """The other half of the same rule: normalising to UTC must not flatten genuinely
    different instants. 12:00 in Oslo and 12:00 in UTC are two hours apart, i.e. different
    content, so they must not share a hash."""
    oslo_summer = timezone(timedelta(hours=2))
    in_oslo = do_emit_financial_event(
        _EPSILON, _event(occurred_at=datetime(2026, 4, 18, 12, 0, 0, tzinfo=oslo_summer))
    )
    in_utc = do_emit_financial_event(
        _EPSILON, _event(occurred_at=datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc))
    )

    assert in_oslo["hash"] != in_utc["hash"]


def test_stored_timestamptz_round_trip_verifies() -> None:
    """The concrete Wave-6 failure the fix removes: emit with an Oslo-offset ``occurred_at``,
    then recompute the hash from the value a ``timestamptz`` column hands back (UTC-aware,
    same instant). PRE-FIX the two hashes differed and the intact row read as TAMPERED."""
    oslo_summer = timezone(timedelta(hours=2))
    emitted = do_emit_financial_event(
        _EPSILON,
        _event(
            occurred_at=datetime(2026, 4, 18, 12, 0, 0, tzinfo=oslo_summer),
            postings=_balanced_postings(),
        ),
    )
    as_read_back = _event(
        occurred_at=emitted["occurred_at"].astimezone(timezone.utc),
        postings=_balanced_postings(),
    )
    recomputed = do_emit_financial_event(_EPSILON, as_read_back)

    assert recomputed["hash"] == emitted["hash"]


@pytest.mark.parametrize(
    "event_overrides",
    [
        {"occurred_at": datetime(2026, 4, 18, 10, 0, 0)},
        {"payload": {"approved_at": datetime(2026, 4, 18, 10, 0, 0)}},
        {"payload": {"lines": [{"due": datetime(2026, 4, 18, 10, 0, 0)}]}},
    ],
)
def test_naive_datetime_is_rejected(event_overrides: dict) -> None:
    """PRE-FIX a naive datetime hashed as ``ce57980c85d4729a`` — a THIRD spelling of the same
    instant, distinct from both aware ones.

    It is rejected rather than assumed to be UTC: assuming would recreate the exact defect
    being fixed (one instant, several hashes), and this module cannot know whether the caller
    meant UTC or Oslo. Rejection is checked at any depth, not just at ``occurred_at``.
    """
    with pytest.raises(ValueError, match="timezone-aware"):
        do_emit_financial_event(_EPSILON, _event(**event_overrides))


def test_plain_date_is_still_accepted_and_distinct_from_a_datetime() -> None:
    """A ``date`` names a calendar day, not an instant, so it has no offset to normalise and
    must keep passing. It must not collide with midnight-UTC of the same day either."""
    as_day = do_emit_financial_event(_EPSILON, _event(occurred_at=date(2026, 4, 18)))
    as_instant = do_emit_financial_event(
        _EPSILON, _event(occurred_at=datetime(2026, 4, 18, 0, 0, 0, tzinfo=timezone.utc))
    )

    assert as_day["hash"] != as_instant["hash"]


def test_hash_rejects_unhashable_values_rather_than_using_repr() -> None:
    """A ``repr`` fallback would embed a memory address and make the hash change between
    runs — that is not a hash. Fail loud instead."""
    with pytest.raises(ValueError, match="cannot be canonically hashed"):
        do_emit_financial_event(_EPSILON, _event(payload={"obj": object()}))


# --- never auto-balances ---------------------------------------------------


def test_never_inserts_a_balancing_posting() -> None:
    """The returned journal has exactly the caller's postings — same count, same accounts,
    same amounts. No balancing leg, no rounding, no re-scaling."""
    postings = [
        {"account": "4300", "amount": Decimal("100.123456")},
        {"account": "2400", "amount": Decimal("-100.123456")},
    ]
    result = do_emit_financial_event(_EPSILON, _event(postings=postings))

    assert len(result["postings"]) == len(postings)
    assert [posting["account"] for posting in result["postings"]] == ["4300", "2400"]
    assert [posting["amount"] for posting in result["postings"]] == [
        Decimal("100.123456"),
        Decimal("-100.123456"),
    ]


def test_unbalanced_event_is_rejected_not_repaired() -> None:
    """The near-miss case an auto-balancer would 'helpfully' fix: 0.05 out with a 0.01
    tolerance. It must raise, and the caller's data must come back untouched."""
    postings = [
        {"account": "4300", "amount": Decimal("100.00")},
        {"account": "2400", "amount": Decimal("-99.95")},
    ]
    event = _event(postings=postings)

    with pytest.raises(UnbalancedPostingsError):
        do_emit_financial_event(_EPSILON, event)

    assert len(postings) == 2
    assert postings[1]["amount"] == Decimal("-99.95")
    assert "hash" not in event


def test_caller_input_is_never_mutated() -> None:
    """Normalisation copies; mutating the caller's postings in place would be an
    auto-balance by another name."""
    postings = [
        {"account": "4300", "amount": 100},
        {"account": "2400", "amount": -100},
    ]
    event = _event(postings=postings)

    result = do_emit_financial_event(_EPSILON, event)

    assert event["postings"] is postings
    assert postings[0]["amount"] == 100 and isinstance(postings[0]["amount"], int)
    assert "hash" not in event
    assert result["postings"] is not postings
    assert result["postings"][0] is not postings[0]


# ===========================================================================
# (c) BOUNDARY tests — hostile untyped input (the B116 lesson)
# ===========================================================================


@pytest.mark.parametrize(
    "amount",
    [True, False, "100", None, [100], {"value": 100}, object()],
)
def test_non_numeric_amounts_are_rejected(amount: object) -> None:
    """``isinstance(True, int)`` is True in Python, so a bool would be summed as 1 NOK; a
    string amount is never parsed. Every one of these must fail loud, not degrade."""
    postings = [
        {"account": "4300", "amount": amount},
        {"account": "2400", "amount": Decimal("-100")},
    ]
    with pytest.raises(ValueError):
        do_emit_financial_event(_EPSILON, _event(postings=postings))


@pytest.mark.parametrize(
    "amount",
    [float("nan"), float("inf"), float("-inf"), Decimal("NaN"), Decimal("Infinity")],
)
def test_non_finite_amounts_are_rejected(amount: object) -> None:
    """``abs(nan) > epsilon`` is ``False``: a NaN amount would sail straight through the
    balance guard. This is the single most permissive silent failure available here."""
    postings = [
        {"account": "4300", "amount": amount},
        {"account": "2400", "amount": Decimal("-100")},
    ]
    with pytest.raises(ValueError, match="finite"):
        do_emit_financial_event(_EPSILON, _event(postings=postings))


def test_posting_without_amount_is_rejected() -> None:
    postings = [{"account": "4300"}, {"account": "2400", "amount": Decimal("-100")}]
    with pytest.raises(ValueError, match="missing 'amount'"):
        do_emit_financial_event(_EPSILON, _event(postings=postings))


@pytest.mark.parametrize(
    ("label", "postings"),
    [
        ("key absent", [{"amount": 100.00}, {"amount": -100.00}]),
        (
            "None",
            [{"account": None, "amount": 100.00}, {"account": None, "amount": -100.00}],
        ),
        ("empty", [{"account": "", "amount": 100.00}, {"account": "", "amount": -100.00}]),
        (
            "whitespace",
            [{"account": "   ", "amount": 100.00}, {"account": " ", "amount": -100.00}],
        ),
        ("int", [{"account": 4300, "amount": 100.00}, {"account": 2400, "amount": -100.00}]),
        (
            "typo'd key",
            [{"acount": "4300", "amount": 100.00}, {"acount": "2400", "amount": -100.00}],
        ),
    ],
)
def test_posting_without_a_usable_account_is_rejected(label: str, postings: list[dict]) -> None:
    """PRE-FIX every one of these was ACCEPTED — ``[{'amount': 100}, {'amount': -100}]``
    balanced and came back with hash ``7a92d7b19b07``.

    A journal with no GL account is arithmetically perfect and financially meaningless, and
    the hash it carried attested to exactly that. The reference ``JournalPosting`` declares
    ``account: string`` non-optional and TypeScript enforces it at every call site; the
    Python port dropped the field with no runtime replacement. B120 builds postings by
    looking the account up in ``finago-account-mapping.json``, where a miss yields ``None``
    and a mistyped key yields nothing — the "None" and "typo'd key" cases above are that
    failure exactly. An ``int`` account is rejected too: ``4300`` and ``"4300"`` canonicalise
    as ``"d:4300"`` and ``"s:4300"``, so allowing both gives one GL account two hashes.

    ``ValueError``, not ``UnbalancedPostingsError``: the money adds up fine, the posting is
    malformed — reporting an imbalance would be a false diagnosis.
    """
    with pytest.raises(ValueError, match="'account' must be a non-empty string"):
        do_emit_financial_event(_EPSILON, _event(postings=postings))


def test_account_is_inside_the_hashed_material() -> None:
    """The hash must attest to WHICH accounts were posted to, not just that the amounts tie
    out — otherwise a re-pointed journal verifies as intact. Amounts, accounts and signs are
    all covered: changing one account, and swapping which line is the debit, both move it."""
    baseline = do_emit_financial_event(_EPSILON, _event(postings=_balanced_postings()))
    other_account = do_emit_financial_event(
        _EPSILON,
        _event(
            postings=[
                {"account": "4301", "amount": Decimal("100000.00"), "comment": "Varekost"},
                {
                    "account": "2400",
                    "amount": Decimal("-100000.00"),
                    "comment": "Leverandorgjeld",
                },
            ]
        ),
    )
    signs_swapped = do_emit_financial_event(
        _EPSILON,
        _event(
            postings=[
                {"account": "4300", "amount": Decimal("-100000.00"), "comment": "Varekost"},
                {
                    "account": "2400",
                    "amount": Decimal("100000.00"),
                    "comment": "Leverandorgjeld",
                },
            ]
        ),
    )

    assert len({baseline["hash"], other_account["hash"], signs_swapped["hash"]}) == 3


@pytest.mark.parametrize("postings", ["not-a-list", {"account": "4300", "amount": 1}, 42])
def test_non_list_postings_are_rejected(postings: object) -> None:
    """A ``str`` iterates character by character and a ``dict`` iterates its keys — both
    would 'work' and produce nonsense."""
    with pytest.raises(ValueError, match="must be a list"):
        do_emit_financial_event(_EPSILON, _event(postings=postings))


def test_non_dict_posting_is_rejected() -> None:
    with pytest.raises(ValueError, match="expected an object"):
        do_emit_financial_event(_EPSILON, _event(postings=[["4300", 100]]))


@pytest.mark.parametrize(
    "epsilon",
    [float("nan"), float("inf"), Decimal("NaN"), -0.01, True, "0.01", None],
)
def test_invalid_epsilon_is_rejected(epsilon: object) -> None:
    """A NaN or infinite tolerance makes ``abs(sum) > epsilon`` ``False`` for EVERY event —
    the guard would be disabled wholesale and silently. ``True`` would become a 1 NOK
    tolerance. All must raise before any event is judged."""
    postings = [
        {"account": "4300", "amount": Decimal("100.00")},
        {"account": "2400", "amount": Decimal("-50.00")},
    ]
    with pytest.raises(ValueError):
        do_emit_financial_event(epsilon, _event(postings=postings))  # type: ignore[arg-type]


def test_valid_epsilon_types_are_accepted() -> None:
    postings = [
        {"account": "4300", "amount": Decimal("100.00")},
        {"account": "2400", "amount": Decimal("-100.01")},
    ]
    for epsilon in (0.01, Decimal("0.01"), 1):
        assert do_emit_financial_event(epsilon, _event(postings=postings))["hash"]


@pytest.mark.parametrize("event", [None, "event", ["type"], 42])
def test_non_dict_event_is_rejected(event: object) -> None:
    with pytest.raises(ValueError):
        do_emit_financial_event(_EPSILON, event)  # type: ignore[arg-type]


@pytest.mark.parametrize("event_type", [None, "", "   ", 42, True])
def test_event_without_a_usable_type_is_rejected(event_type: object) -> None:
    """An event with no type is not a ledger event — and the type names the event in the
    rejection message."""
    event = _event(postings=_balanced_postings())
    event["type"] = event_type
    with pytest.raises(ValueError, match="'type' must be a non-empty string"):
        do_emit_financial_event(_EPSILON, event)


def test_amounts_too_extreme_to_sum_exactly_raise_rather_than_round() -> None:
    """A sum rounded to the working precision is a wrong sum. Comparing it against the
    tolerance would be a silent, arbitrary verdict — so the ``Inexact`` trap turns it into a
    loud refusal instead."""
    postings = [
        {"account": "4300", "amount": Decimal("1E+900")},
        {"account": "4301", "amount": Decimal("1E-900")},
        {"account": "2400", "amount": Decimal("-1E+900")},
    ]
    with pytest.raises(ValueError, match="too many digits"):
        do_emit_financial_event(_EPSILON, _event(postings=postings))


def test_non_string_payload_keys_are_rejected() -> None:
    """``json.dumps`` would coerce ``1`` and ``"1"`` to the same key and silently drop one."""
    with pytest.raises(ValueError, match="object keys must be strings"):
        do_emit_financial_event(_EPSILON, _event(payload={1: "a", "1": "b"}))


def test_many_postings_balance_exactly() -> None:
    """A realistic multi-line journal: 100 ore-level debits against one credit. Exact in
    Decimal; in floats this accumulates a residue."""
    debits = [{"account": "4300", "amount": 0.01} for _ in range(100)]
    credit = [{"account": "2400", "amount": Decimal("-1.00")}]
    result = do_emit_financial_event(0, _event(postings=debits + credit))

    assert len(result["postings"]) == 101
    assert math.isclose(float(sum(p["amount"] for p in result["postings"])), 0.0)
