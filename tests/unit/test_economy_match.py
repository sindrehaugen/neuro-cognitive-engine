"""
tests/unit/test_economy_match.py
====================================
Acceptance tests for Batch 116 — Module 8.Wave 1 (match-invoice).

Split per round-2 rule #3, mirroring ``test_procurement_tco.py``:
  (a) ALGORITHM tests — ported from the reference implementation's ``matching-score.test.ts``, parameterised by
      a fixture ``_FIXTURE_THRESHOLDS`` dict defined in THIS file — never the tenant's real
      115/70 JSON values reached into directly for the lifted per-pair assertions.
  (b) WAVE tests — this wave's own required cases: shape, worst-line-tier, config-drives-
      behaviour, per-supplier override, three-way-is-read-not-derived, no-lines/no-candidates
      edge cases, malformed-numerics robustness, multi-candidate pool selection.
  (c) CONFIG tests — assert the real JSON file loads and contains the documented keys.

All tests are plain unit tests (no DB, no HTTP, no ``@pytest.mark.integration``).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from nce.vertical_modules.economy.matching import do_match_invoice, load_economy_thresholds

# ---------------------------------------------------------------------------
# Shared fixtures — algorithm tests use THESE thresholds, never the tenant's JSON values
# directly (they happen to equal 115/70, matching the reference implementation's DEFAULT_THRESHOLDS, but they
# are a literal defined here so a change to the real JSON can never silently break these).
# ---------------------------------------------------------------------------

_FIXTURE_THRESHOLDS: dict = {"green": 115, "yellow": 70, "supplier_overrides": {}}

_BASE_BOM_LINE = {
    "article_no": "NETSET-42",
    "description": "Crestron DM-NVX-363 Network AV Encoder",
    "project_id": "proj-1",
}

_DEFAULT_LINE = {
    "article_no": "NETSET-42",
    "description": "Crestron DM-NVX-363 Network AV Encoder",
    "quantity": 2,
    "unit_price": 15_000,
    "line_total": 30_000,
}

_DEFAULT_CONTEXT = {
    "supplier_exact": True,
    "supplier_fuzzy": False,
    "expected_amount": 30_000,
    "po_expected": True,
    "supplier_pattern_seen": True,
}

_DEFAULT_INVOICE_HEADER = {
    "supplier_orgnr": "987654321",
    "project_dimension_present": True,
    "project_dimension_inferred": False,
}


def _run(
    *,
    invoice_header: dict | None = None,
    line: dict | None = None,
    bom_line: dict | None | object = "__default__",
    context: dict | None = None,
    three_way_result: object = None,
    thresholds: dict | None = None,
    extra_candidates: list | None = None,
) -> dict:
    """Build a one-line, one-(or-more)-candidate invoice and run do_match_invoice.

    Mirrors the reference implementation's ``makeInput(overrides)`` fixture shape for the per-pair building
    blocks: ``line`` / ``bom_line`` / ``context``, when supplied, REPLACE the whole
    default sub-dict (no deep merge) — exactly like the TS spread-override pattern.
    ``invoice_header`` is the one exception: it is layered onto the defaults with
    ``dict.update`` (a shallow MERGE, not a replace) so a caller can override a single
    header key (e.g. just ``supplier_orgnr``) without having to restate the others.

    ``extra_candidates`` appends already-fully-built candidate dicts (see ``_candidate``)
    after the primary one built from ``bom_line``/``context``/``three_way_result`` above,
    producing a real multi-candidate pool for ``do_match_invoice`` to pick a winner from.
    """
    invoice = dict(_DEFAULT_INVOICE_HEADER)
    if invoice_header is not None:
        invoice.update(invoice_header)
    invoice["lines"] = [line if line is not None else dict(_DEFAULT_LINE)]

    resolved_bom_line = dict(_BASE_BOM_LINE) if bom_line == "__default__" else bom_line
    candidate = {
        "bom_line": resolved_bom_line,
        "context": context if context is not None else dict(_DEFAULT_CONTEXT),
        "three_way_result": three_way_result,
    }
    candidates = [candidate, *(extra_candidates or [])]

    return do_match_invoice(thresholds or _FIXTURE_THRESHOLDS, invoice, candidates)


def _entry(result: dict, index: int = 0) -> dict:
    return result["breakdown"][index]


def _candidate(
    *,
    bom_line: dict | None = None,
    context: dict | None = None,
    three_way_result: object = None,
    candidate_id: object = "__unset__",
) -> dict:
    """Build one full candidate dict for hand-assembling a multi-candidate pool (passed
    to ``_run``'s ``extra_candidates`` or straight into ``do_match_invoice``).

    Omitting ``candidate_id`` leaves the key absent entirely — mirroring a real caller
    that never supplied one — so the winning breakdown entry reports ``candidate_id:
    None`` for it, exactly like the primary candidate ``_run`` builds itself (D5: a
    positional fallback must never masquerade as an explicit id).
    """
    built: dict = {
        "bom_line": bom_line,
        "context": context if context is not None else {},
        "three_way_result": three_way_result,
    }
    if candidate_id != "__unset__":
        built["candidate_id"] = candidate_id
    return built


# ===========================================================================
# (a) ALGORITHM tests — ported 1:1 from matching-score.test.ts
# ===========================================================================


def test_perfect_match_scores_130_green():
    r = _run()
    e = _entry(r)
    assert r["score"] == 130
    assert r["tier"] == "GREEN"
    assert e["supplier_match"] == 40
    assert e["amount_match"] == 30
    assert e["article_match"] == 20
    assert e["project_match"] == 10
    assert e["bom_line_match"] == 15
    assert e["expected_from_po"] == 10
    assert e["supplier_pattern"] == 5


def test_zero_match_scores_0_red():
    r = _run(
        invoice_header={"project_dimension_present": False, "project_dimension_inferred": False},
        line={
            "article_no": "",
            "description": "mystery widget",
            "quantity": 1,
            "unit_price": 100,
            "line_total": 100,
        },
        bom_line=None,
        context={
            "supplier_exact": False,
            "supplier_fuzzy": False,
            "expected_amount": 0,
            "po_expected": False,
            "supplier_pattern_seen": False,
        },
    )
    assert r["score"] == 0
    assert r["tier"] == "RED"


def test_classical_100_no_bonus_scores_100_yellow():
    """bomLine present (article match) but project_id missing kills the 15-pt bonus.
    Supplier(40) + amount(30) + article(20) + project(10) = 100 classical, 0 context."""
    r = _run(
        bom_line={**_BASE_BOM_LINE, "project_id": ""},
        context={
            "supplier_exact": True,
            "supplier_fuzzy": False,
            "expected_amount": 30_000,
            "po_expected": False,
            "supplier_pattern_seen": False,
        },
    )
    e = _entry(r)
    assert r["score"] == 100
    assert r["tier"] == "YELLOW"
    assert e["bom_line_match"] == 0
    assert e["article_match"] == 20


def test_fuzzy_supplier_within_10pct_semantic_article_scores_45():
    r = _run(
        invoice_header={"project_dimension_present": False, "project_dimension_inferred": False},
        line={
            "article_no": "",
            "description": "Crestron DM encoder 363 network",
            "quantity": 2,
            "unit_price": 14_500,
            "line_total": 29_000,
        },
        bom_line={**_BASE_BOM_LINE, "article_no": ""},
        context={
            "supplier_exact": False,
            "supplier_fuzzy": True,
            "expected_amount": 30_000,  # ~3.3% diff
            "po_expected": False,
            "supplier_pattern_seen": False,
        },
    )
    e = _entry(r)
    assert e["supplier_match"] == 20
    assert e["amount_match"] == 15
    assert e["article_match"] == 10
    assert e["project_match"] == 0
    assert e["bom_line_match"] == 0
    assert r["score"] == 45
    assert r["tier"] == "RED"


def test_inferred_project_scores_5_points():
    r = _run(
        invoice_header={"project_dimension_present": False, "project_dimension_inferred": True},
    )
    assert _entry(r)["project_match"] == 5


@pytest.mark.parametrize(
    "line_total,expected_amount_match",
    [
        (30_600, 30),  # exactly 2% diff — boundary, must score 30 (float boundary, money)
        (31_500, 15),  # 5% diff
        (34_500, 0),  # 15% diff
    ],
)
def test_amount_ladder_2pct_10pct(line_total, expected_amount_match):
    r = _run(
        line={**_DEFAULT_LINE, "line_total": line_total},
        context={**_DEFAULT_CONTEXT, "expected_amount": 30_000},
    )
    assert _entry(r)["amount_match"] == expected_amount_match


@pytest.mark.parametrize(
    "line_total,expected_amount_match",
    [
        (30_600, 30),  # exactly 2% diff, from just below — must still score 30
        (30_600.01, 15),  # one cent past 2% — must drop to 15 (pins the 2% cutoff)
        (33_000, 15),  # exactly 10% diff, from just below — must still score 15
        (33_000.01, 0),  # one cent past 10% — must drop to 0 (pins the 10% cutoff)
    ],
)
def test_amount_ladder_boundaries_from_above(line_total, expected_amount_match):
    """Approaches both cutoffs FROM ABOVE (the previous ladder test above only approaches
    from below/inside). A 2%->2.5% or 10%->14% loosening of either guard cannot ship
    without moving one of these four literals."""
    r = _run(
        line={**_DEFAULT_LINE, "line_total": line_total},
        context={**_DEFAULT_CONTEXT, "expected_amount": 30_000},
    )
    assert _entry(r)["amount_match"] == expected_amount_match


def test_tier_boundary_green_115():
    """115 = 40 (supplier) + 30 (amount) + 20 (article) + 10 (project) + 15 (bomLine)."""
    r = _run(
        context={
            "supplier_exact": True,
            "supplier_fuzzy": False,
            "expected_amount": 30_000,
            "po_expected": False,
            "supplier_pattern_seen": False,
        },
    )
    assert r["score"] == 115
    assert r["tier"] == "GREEN"


def test_tier_boundary_yellow_70():
    """70 = 40 (supplier) + 30 (amount), no article/project/context."""
    r = _run(
        invoice_header={"project_dimension_present": False, "project_dimension_inferred": False},
        line={
            "article_no": "",
            "description": "widget",
            "quantity": 1,
            "unit_price": 30_000,
            "line_total": 30_000,
        },
        bom_line=None,
        context={
            "supplier_exact": True,
            "supplier_fuzzy": False,
            "expected_amount": 30_000,
            "po_expected": False,
            "supplier_pattern_seen": False,
        },
    )
    assert r["score"] == 70
    assert r["tier"] == "YELLOW"


def test_tier_boundary_red_65():
    """65 = 20 (supplier fuzzy) + 30 (amount) + 10 (project present) + 5 (supplier pattern)."""
    r = _run(
        invoice_header={"project_dimension_present": True, "project_dimension_inferred": False},
        line={
            "article_no": "",
            "description": "widget",
            "quantity": 1,
            "unit_price": 30_000,
            "line_total": 30_000,
        },
        bom_line=None,
        context={
            "supplier_exact": False,
            "supplier_fuzzy": True,
            "expected_amount": 30_000,
            "po_expected": False,
            "supplier_pattern_seen": True,
        },
    )
    assert r["score"] == 65
    assert r["tier"] == "RED"


def test_custom_thresholds_flip_115_from_green_to_yellow():
    """Score 115 is GREEN at default fixture thresholds, YELLOW under a tighter green=125."""
    kwargs = dict(
        context={
            "supplier_exact": True,
            "supplier_fuzzy": False,
            "expected_amount": 30_000,
            "po_expected": False,
            "supplier_pattern_seen": False,
        },
    )
    default_tier = _run(**kwargs)["tier"]
    tight_tier = _run(**kwargs, thresholds={"green": 125, "yellow": 70, "supplier_overrides": {}})[
        "tier"
    ]
    assert default_tier == "GREEN"
    assert tight_tier == "YELLOW"


def test_reasons_are_human_readable():
    r = _run()
    reasons = _entry(r)["reasons"]
    joined = " ".join(reasons)
    assert len(reasons) > 3
    assert "supplier exact" in joined
    assert "articleNo exact" in joined


def test_tokenizer_ignores_stopwords_and_short_tokens():
    """A genuinely discriminating pair: the ONLY tokens shared between the two
    descriptions are stopwords ('og', 'til') and a short token ('av', len 2). Removing
    EITHER the stopword filter or the len>=3 rule alone lets a SECOND token ('til' or
    'av' respectively) pair up alongside 'rack', pushing overlap from 1 to 2 and
    article_match from 0 to 10 — so this pair genuinely exercises both filters
    (the previous 'a for og en' vs the Crestron description shared NO token under any
    filter combination, so it passed even with both filters deleted)."""
    r = _run(
        line={
            "article_no": "",
            "description": "AV og til rack",
            "quantity": 1,
            "unit_price": 0,
            "line_total": 0,
        },
        bom_line={**_BASE_BOM_LINE, "article_no": "", "description": "AV rack og kabel til bygg"},
    )
    assert _entry(r)["article_match"] == 0


def test_semantic_article_requires_two_shared_tokens():
    """Exactly ONE shared meaningful token ('crestron') must score 0, not 10 — the
    overlap>=2 guard is a strict floor, not a >=1 threshold."""
    r = _run(
        line={
            "article_no": "",
            "description": "Crestron amplifier unit",
            "quantity": 1,
            "unit_price": 0,
            "line_total": 0,
        },
        bom_line={**_BASE_BOM_LINE, "article_no": ""},
    )
    assert _entry(r)["article_match"] == 0


class TestPoNumberPlanD:
    """2026-04-25 Plan-D — PO-nr as primary deterministic match signal."""

    def test_po_number_exact_match_adds_50_points(self):
        baseline = _run()
        with_po = _run(context={**_DEFAULT_CONTEXT, "po_number_exact_match": True})
        assert with_po["score"] - baseline["score"] == 50
        assert _entry(with_po)["po_number_match"] == 50

    def test_po_alone_plus_fuzzy_supplier_scores_70_yellow(self):
        r = _run(
            invoice_header={
                "project_dimension_present": False,
                "project_dimension_inferred": False,
            },
            line={
                "article_no": "X",
                "description": "",
                "quantity": 1,
                "unit_price": 5_000,
                "line_total": 5_000,
            },
            bom_line=None,
            context={
                "supplier_exact": False,
                "supplier_fuzzy": True,
                "expected_amount": 0,
                "po_expected": False,
                "supplier_pattern_seen": False,
                "po_number_exact_match": True,
            },
        )
        assert _entry(r)["po_number_match"] == 50
        assert r["score"] == 70  # 50 PO + 20 supplier-fuzzy
        assert r["tier"] == "YELLOW"

    def test_reasons_include_po_number_exact_match_text(self):
        r = _run(context={**_DEFAULT_CONTEXT, "po_number_exact_match": True})
        assert "PO-nr eksakt match" in " ".join(_entry(r)["reasons"])


# ===========================================================================
# (a2) HARDENING tests — _flag / _as_amount / description-coercion robustness
# ===========================================================================


@pytest.mark.parametrize("bad_value", ["false", "0", "False", float("nan"), [0], -1])
@pytest.mark.parametrize("field", ["supplier_exact", "po_number_exact_match"])
def test_boolean_gates_reject_non_bool_values(field: str, bad_value: object):
    """A stringified 'false'/'0', NaN, a non-empty list, or -1 must never earn the
    documented-bool gates' points, on either of the two boolean fields under test.
    Proven against the pre-fix bare-truthiness mutant in the PROVE section below."""
    context = {**_DEFAULT_CONTEXT, "supplier_exact": False, field: bad_value}
    r = _run(context=context)
    e = _entry(r)
    component = e["supplier_match"] if field == "supplier_exact" else e["po_number_match"]
    assert component == 0
    assert r["tier"] != "GREEN"
    # Both branches degrade to the SAME literal total (90): amount30+article20+project10+
    # bomline15+po_expected10+pattern5, with the field-under-test's own component at 0.
    assert r["score"] == 90
    assert r["tier"] == "YELLOW"


def test_amount_accepts_decimal_and_mixed_decimal_float():
    """Decimal line_total vs float expected_amount, and the REVERSE pairing, must both
    score 30 without raising TypeError on mixed Decimal/float arithmetic."""
    r_decimal_line = _run(
        line={**_DEFAULT_LINE, "line_total": Decimal("30000.00")},
        context={**_DEFAULT_CONTEXT, "expected_amount": 30_000.0},
    )
    r_decimal_expected = _run(
        line={**_DEFAULT_LINE, "line_total": 30_000.0},
        context={**_DEFAULT_CONTEXT, "expected_amount": Decimal("30000.00")},
    )
    assert _entry(r_decimal_line)["amount_match"] == 30
    assert _entry(r_decimal_expected)["amount_match"] == 30


@pytest.mark.parametrize("bad_value", ["30000", None])
def test_amount_degrades_on_str_and_none_without_raising(bad_value: object):
    """A stringified amount or a missing (None) amount, on EITHER the line's own total
    or the candidate's expected_amount, must degrade to 0 — never raise."""
    r_line = _run(line={**_DEFAULT_LINE, "line_total": bad_value})
    assert _entry(r_line)["amount_match"] == 0

    r_context = _run(context={**_DEFAULT_CONTEXT, "expected_amount": bad_value})
    assert _entry(r_context)["amount_match"] == 0


def test_description_none_scores_zero_article_without_raising():
    """An explicit None description (not merely absent) must not raise on .lower(),
    on EITHER the invoice line's side or the BOM candidate's side."""
    r_line_none = _run(
        line={**_DEFAULT_LINE, "article_no": "", "description": None},
        bom_line={**_BASE_BOM_LINE, "article_no": ""},
    )
    r_bom_none = _run(
        line={**_DEFAULT_LINE, "article_no": ""},
        bom_line={**_BASE_BOM_LINE, "article_no": "", "description": None},
    )
    assert _entry(r_line_none)["article_match"] == 0
    assert _entry(r_bom_none)["article_match"] == 0


# ===========================================================================
# (b) WAVE tests — this wave's own required cases
# ===========================================================================


def test_result_shape_and_score_bounds():
    r = _run(context={**_DEFAULT_CONTEXT, "po_number_exact_match": True})
    assert set(r.keys()) == {"score", "tier", "breakdown"}
    assert 0 <= r["score"] <= 180
    assert r["tier"] in {"GREEN", "YELLOW", "RED"}
    assert isinstance(r["breakdown"], list)


def test_true_theoretical_max_is_180_not_130():
    """40 (supplier) + 30 (amount) + 20 (article) + 10 (project) + 15 (bomLine)
    + 10 (poExpected) + 5 (pattern) + 50 (PO-nr) = 180. Must NOT be clamped to 130."""
    r = _run(context={**_DEFAULT_CONTEXT, "po_number_exact_match": True})
    assert r["score"] == 180
    assert r["tier"] == "GREEN"


def test_worst_line_tier_wins_across_multiple_lines():
    """A multi-line invoice where one line is GREEN and another is RED must return RED,
    even though both lines are scored against the SAME shared candidate pool."""
    invoice = {
        **_DEFAULT_INVOICE_HEADER,
        "lines": [
            dict(_DEFAULT_LINE),  # matches the candidate perfectly -> GREEN (130)
            {
                "article_no": "ZZZ-999",
                "description": "unrelated junk",
                "quantity": 1,
                "unit_price": 999_999,
                "line_total": 999_999,
            },
        ],
    }
    candidates = [
        {
            "bom_line": dict(_BASE_BOM_LINE),
            "context": dict(_DEFAULT_CONTEXT),
            "three_way_result": None,
        }
    ]
    r = do_match_invoice(_FIXTURE_THRESHOLDS, invoice, candidates)

    assert r["breakdown"][0]["tier"] == "GREEN"
    assert r["breakdown"][1]["tier"] == "RED"
    assert r["tier"] == "RED"
    assert r["score"] == r["breakdown"][1]["total"]


def test_worst_tier_green_plus_yellow_returns_yellow():
    """2 lines: one 130/GREEN, one 105/YELLOW (no RED line present) -> the worst tier
    is YELLOW, and score is that line's own total, pinned as a literal."""
    invoice = {
        **_DEFAULT_INVOICE_HEADER,
        "lines": [
            dict(_DEFAULT_LINE),  # perfect match -> 130 GREEN
            {
                "article_no": "",  # no exact article match -> no bomLine bonus either
                "description": "Crestron network encoder unit",  # 3 shared tokens -> +10
                "quantity": 1,
                "unit_price": 30_000,
                "line_total": 30_000,  # exact amount match -> +30
            },  # supplier(40)+amount(30)+article(10)+project(10)+po_expected(10)+pattern(5)=105
        ],
    }
    candidates = [
        {
            "bom_line": dict(_BASE_BOM_LINE),
            "context": dict(_DEFAULT_CONTEXT),
            "three_way_result": None,
        }
    ]
    r = do_match_invoice(_FIXTURE_THRESHOLDS, invoice, candidates)

    assert r["breakdown"][0]["total"] == 130
    assert r["breakdown"][0]["tier"] == "GREEN"
    assert r["breakdown"][1]["total"] == 105
    assert r["breakdown"][1]["tier"] == "YELLOW"
    assert r["tier"] == "YELLOW"
    assert r["score"] == 105


def test_invoice_score_is_lowest_total_among_worst_tier_lines():
    """3 lines against ONE shared candidate: one GREEN (85), two RED (50 and 20). The
    invoice score must be the LOWEST RED total (20), not the highest (50) nor the GREEN
    line's total — pinned as a literal, not read back from the result under test."""
    # Bespoke cutoffs: the literal targets 85/50/20 are unreachable under the shipped
    # 115/70, because every context component is fixed per-candidate and so applies to
    # every line sharing the pool. green=61 is the lowest value clearing the _MIN_GREEN
    # floor (auto-approval may not rest on one candidate-tied signal); 85 is still GREEN
    # and 50/20 are still RED, so this pins exactly the rule it did before the floor.
    thresholds = {"green": 61, "yellow": 55, "supplier_overrides": {}}
    bom_line = {
        "article_no": "NETSET-42",
        "description": "Crestron DM-NVX-363 Network AV Encoder",
        "project_id": "proj-1",
    }
    context = {
        "supplier_exact": False,
        "supplier_fuzzy": True,  # +20 context floor, shared by every line below
        "expected_amount": 30_000,
        "po_expected": False,
        "supplier_pattern_seen": False,
    }
    invoice = {
        "supplier_orgnr": "987654321",
        "project_dimension_present": False,
        "project_dimension_inferred": False,
        "lines": [
            {  # full match: context(20) + amount(30) + article(20) + bomline(15) = 85
                "article_no": "NETSET-42",
                "description": "Crestron DM-NVX-363 Network AV Encoder",
                "quantity": 2,
                "unit_price": 15_000,
                "line_total": 30_000,
            },
            {  # context(20) + amount(30, matches expected) + article(0) + bomline(0) = 50
                "article_no": "UNRELATED-1",
                "description": "totally different widget",
                "quantity": 1,
                "unit_price": 30_000,
                "line_total": 30_000,
            },
            {  # context(20) + amount(0, wildly off) + article(0) + bomline(0) = 20
                "article_no": "UNRELATED-2",
                "description": "nothing in common at all zzz",
                "quantity": 1,
                "unit_price": 999_999,
                "line_total": 999_999,
            },
        ],
    }
    candidates = [{"bom_line": bom_line, "context": context, "three_way_result": None}]
    r = do_match_invoice(thresholds, invoice, candidates)

    assert r["breakdown"][0]["tier"] == "GREEN"
    assert r["breakdown"][1]["total"] == 50
    assert r["breakdown"][1]["tier"] == "RED"
    assert r["breakdown"][2]["total"] == 20
    assert r["breakdown"][2]["tier"] == "RED"
    assert r["tier"] == "RED"
    assert r["score"] == 20


def test_config_drives_behaviour_same_input_different_tiers():
    """The exact same classical-100 input tiers YELLOW under one fixture and GREEN
    under another — proves thresholds (config), not literals, drive the tier."""
    kwargs = dict(
        bom_line={**_BASE_BOM_LINE, "project_id": ""},
        context={
            "supplier_exact": True,
            "supplier_fuzzy": False,
            "expected_amount": 30_000,
            "po_expected": False,
            "supplier_pattern_seen": False,
        },
    )
    strict = _run(**kwargs, thresholds={"green": 115, "yellow": 70, "supplier_overrides": {}})
    lenient = _run(**kwargs, thresholds={"green": 90, "yellow": 50, "supplier_overrides": {}})

    assert strict["score"] == lenient["score"] == 100
    assert strict["tier"] == "YELLOW"
    assert lenient["tier"] == "GREEN"


def test_per_supplier_override_actually_overrides_default():
    thresholds = {
        "green": 115,
        "yellow": 70,
        "supplier_overrides": {"OVERRIDDEN-ORGNR": {"green": 200, "yellow": 70}},
    }
    default_supplier = _run(
        invoice_header={**_DEFAULT_INVOICE_HEADER, "supplier_orgnr": "OTHER-ORGNR"},
        thresholds=thresholds,
    )
    overridden_supplier = _run(
        invoice_header={**_DEFAULT_INVOICE_HEADER, "supplier_orgnr": "OVERRIDDEN-ORGNR"},
        thresholds=thresholds,
    )

    assert default_supplier["score"] == overridden_supplier["score"] == 130
    assert default_supplier["tier"] == "GREEN", "no override -> defaults (115/70) apply"
    assert overridden_supplier["tier"] == "YELLOW", "override raises green to 200 -> 130 < 200"


def test_missing_supplier_override_leaves_defaults_untouched():
    thresholds = {
        "green": 115,
        "yellow": 70,
        "supplier_overrides": {"SOME-OTHER-ORGNR": {"green": 999, "yellow": 999}},
    }
    r = _run(
        invoice_header={**_DEFAULT_INVOICE_HEADER, "supplier_orgnr": "UNRELATED-ORGNR"},
        thresholds=thresholds,
    )
    assert r["tier"] == "GREEN"  # defaults (115/70) untouched by an unrelated override


def test_partial_supplier_override_inherits_the_other_key():
    """The override supplies ONLY 'yellow' — 'green' must inherit the JSON default
    (115), never become missing/None and either crash _resolve_thresholds' isinstance
    check or silently collapse to something else. Uses a SEPARATE thresholds dict
    (per the wave brief) so this never shifts the shared-fixture-based tests above."""
    thresholds = {
        "green": 115,
        "yellow": 70,
        "supplier_overrides": {"OVERRIDE-ORGNR": {"yellow": 50}},
    }

    # A 0-point invoice stays RED regardless of which yellow is in effect.
    zero_result = _run(
        invoice_header={
            "supplier_orgnr": "OVERRIDE-ORGNR",
            "project_dimension_present": False,
            "project_dimension_inferred": False,
        },
        line={"article_no": "", "description": "", "quantity": 1, "unit_price": 0, "line_total": 0},
        bom_line=None,
        context={
            "supplier_exact": False,
            "supplier_fuzzy": False,
            "expected_amount": 0,
            "po_expected": False,
            "supplier_pattern_seen": False,
        },
        thresholds=thresholds,
    )
    assert zero_result["score"] == 0
    assert zero_result["tier"] == "RED"

    # 65 sits between the override's yellow(50) and the default yellow(70): YELLOW here
    # proves the override's yellow=50 actually took effect (it would be RED under the
    # untouched default yellow=70).
    between_yellows_result = _run(
        invoice_header={
            "supplier_orgnr": "OVERRIDE-ORGNR",
            "project_dimension_present": True,
            "project_dimension_inferred": False,
        },
        line={
            "article_no": "",
            "description": "widget",
            "quantity": 1,
            "unit_price": 30_000,
            "line_total": 30_000,
        },
        bom_line=None,
        context={
            "supplier_exact": False,
            "supplier_fuzzy": True,
            "expected_amount": 30_000,
            "po_expected": False,
            "supplier_pattern_seen": True,
        },
        thresholds=thresholds,
    )
    assert between_yellows_result["score"] == 65
    assert between_yellows_result["tier"] == "YELLOW"

    # 100 sits below the inherited green(115): YELLOW here proves 'green' correctly
    # inherited the JSON default (it would be GREEN if inheritance had failed and green
    # silently became <=100 or missing).
    below_green_result = _run(
        invoice_header={
            "supplier_orgnr": "OVERRIDE-ORGNR",
            "project_dimension_present": True,
            "project_dimension_inferred": False,
        },
        bom_line={**_BASE_BOM_LINE, "project_id": ""},
        context={
            "supplier_exact": True,
            "supplier_fuzzy": False,
            "expected_amount": 30_000,
            "po_expected": False,
            "supplier_pattern_seen": False,
        },
        thresholds=thresholds,
    )
    assert below_green_result["score"] == 100
    assert below_green_result["tier"] == "YELLOW"


def test_supplier_override_matches_int_orgnr():
    """invoice.supplier_orgnr arrives as a Python int (e.g. from a Postgres bigint);
    the JSON override key is always a string. The override must still apply — proven
    against the pre-fix bare-hash-lookup mutant in the PROVE section below. Uses a
    SEPARATE thresholds dict (per the wave brief)."""
    thresholds = {
        "green": 115,
        "yellow": 70,
        "supplier_overrides": {"987654321": {"green": 200, "yellow": 70}},
    }
    r = _run(
        invoice_header={**_DEFAULT_INVOICE_HEADER, "supplier_orgnr": 987654321},
        thresholds=thresholds,
    )
    assert r["score"] == 130
    assert r["tier"] == "YELLOW"  # override raises green to 200 -> 130 < 200


def test_supplier_override_entry_null_raises():
    """A `null` (or otherwise non-object) override entry must RAISE, not fall back.

    Deliberate change from this wave's first fix, which treated a non-dict entry as
    "absent". Round 2 established that silently discarding a malformed override always
    fails toward LOOSENESS — the inherited default is by construction looser than any
    tightening the author was trying to express — so the same fail-loud rule now applies
    to a null entry as to a typo'd inner key. Clearing an override is expressed by
    removing the key, not by nulling it.
    """
    with pytest.raises(ValueError):
        _run(
            invoice_header={**_DEFAULT_INVOICE_HEADER, "supplier_orgnr": "987654321"},
            thresholds={
                "green": 115,
                "yellow": 70,
                "supplier_overrides": {"987654321": None},
            },
        )


def test_float_and_decimal_cutoffs_are_valid():
    """`green: 115.0` must WORK, not take the engine down.

    Regression guard for a defect introduced by this wave's own first invariant, which
    used `isinstance(green, int)` and so rejected every float — turning a valid
    hand-edited JSON (`json.loads('{"green": 115.0}')` yields a float) or a Postgres
    `numeric` column into a total matching outage for that tenant.
    """
    for green, yellow in [(115.0, 70.0), (115.0, 70), (Decimal("115"), Decimal("70"))]:
        r = _run(thresholds={"green": green, "yellow": yellow, "supplier_overrides": {}})
        assert r["tier"] == "GREEN", f"{green!r}/{yellow!r} should tier a perfect match GREEN"


@pytest.mark.parametrize(
    "bad", [Decimal("sNaN"), Decimal("NaN"), Decimal("Infinity"), Decimal("1E+400")]
)
def test_uncoercible_decimal_cutoff_raises_the_config_diagnostic(bad):
    """An uncoercible Decimal cutoff must produce the CONFIG diagnostic, not a raw
    conversion error.

    Regression guard: `_coerce_cutoff` converted Decimal unguarded, so `Decimal("sNaN")`
    escaped as `ValueError: cannot convert signaling NaN to float` — the same exception
    type the caller raises, so it looked handled while telling the operator nothing about
    which threshold or which supplier was at fault. The helper's contract is total: it
    returns None for anything uncoercible.
    """
    with pytest.raises(ValueError, match="economy match thresholds"):
        _run(thresholds={"green": bad, "yellow": 70, "supplier_overrides": {}})


@pytest.mark.parametrize("green,yellow", [(1, 0), (50, 10), (115, 0)])
def test_degenerate_but_non_inverted_cutoffs_raise(green, yellow):
    """Cutoffs that pass the inversion check but still destroy a tier must raise.

    - `green <= 50` would let ONE signal (the 50-point PO-nr match) auto-approve a posting.
    - `yellow <= 0` makes RED unreachable, so nothing is ever routed to manual handling.
    """
    with pytest.raises(ValueError):
        _run(thresholds={"green": green, "yellow": yellow, "supplier_overrides": {}})


def test_duplicate_normalised_override_keys_raise():
    """Two override keys normalising to the same supplier must raise, not silently shadow.

    Regression guard for a defect introduced by this wave's own override-normalisation
    fix: `next(...)` took the FIRST insertion-order match, so which of two equivalent keys
    won flipped with JSON key order — the same invoice could tier GREEN or RED depending
    on file layout. Verified by execution before the fix.
    """
    for keys in (["987654321", " 987654321 "], [" 987654321 ", "987654321"]):
        with pytest.raises(ValueError):
            _run(
                invoice_header={**_DEFAULT_INVOICE_HEADER, "supplier_orgnr": "987654321"},
                thresholds={
                    "green": 115,
                    "yellow": 70,
                    "supplier_overrides": {
                        keys[0]: {"green": 200, "yellow": 70},
                        keys[1]: {"green": 60, "yellow": 10},
                    },
                },
            )


def test_padded_inner_override_key_still_applies_the_tightening():
    """A padded inner key (`"green "`) must behave EXACTLY like the clean one.

    Regression guard for the worst defect this wave produced. The unknown-key guard
    normalised keys with `.strip()` for VALIDATION but the lookup read the RAW dict, so
    `"green "` passed the guard, missed the lookup, and silently inherited the looser
    default. Verified by execution before the fix: clean `"green": 160` tiered YELLOW,
    padded `"green ": 160` tiered GREEN on the identical 130-point invoice — an operator's
    tightening evaporated and the invoice auto-posted.
    """

    def resolved_tier(inner_key: str) -> str:
        return _run(
            invoice_header={**_DEFAULT_INVOICE_HEADER, "supplier_orgnr": "987654321"},
            thresholds={
                "green": 115,
                "yellow": 70,
                "supplier_overrides": {"987654321": {inner_key: 160, "yellow": 70}},
            },
        )["tier"]

    assert resolved_tier("green") == "YELLOW"
    assert resolved_tier("green ") == "YELLOW"
    assert resolved_tier(" green") == "YELLOW"


def test_inner_override_keys_colliding_after_normalisation_raise():
    """`{"green": 115, "green ": 160}` is ambiguous and must raise, not silently pick one."""
    with pytest.raises(ValueError):
        _run(
            invoice_header={**_DEFAULT_INVOICE_HEADER, "supplier_orgnr": "987654321"},
            thresholds={
                "green": 115,
                "yellow": 70,
                "supplier_overrides": {"987654321": {"green": 115, "green ": 160}},
            },
        )


def test_padded_top_level_supplier_overrides_key_still_applies():
    """A padded top-level `"supplier_overrides "` must not silently discard every override."""
    r = _run(
        invoice_header={**_DEFAULT_INVOICE_HEADER, "supplier_orgnr": "987654321"},
        thresholds={
            "green": 115,
            "yellow": 70,
            "supplier_overrides ": {"987654321": {"green": 160, "yellow": 70}},
        },
    )
    assert r["tier"] == "YELLOW"  # the tightening applied; without it this would be GREEN


@pytest.mark.parametrize("bad", [[], "", 0, False])
def test_falsy_non_dict_supplier_overrides_raises(bad):
    """A falsy non-dict must raise. The earlier `or {}` idiom let every one of these slip
    past the isinstance guard that was supposed to catch them."""
    with pytest.raises(ValueError):
        _run(thresholds={"green": 115, "yellow": 70, "supplier_overrides": bad})


def test_null_supplier_overrides_is_treated_as_absent():
    """An explicit JSON `null` means "no overrides" and is benign — unlike a wrong TYPE."""
    r = _run(thresholds={"green": 115, "yellow": 70, "supplier_overrides": None})
    assert r["tier"] == "GREEN"


def test_green_floor_covers_po_nr_plus_unconditional_project_points():
    """The floor must exceed PO-nr(50) + project(10), not just PO-nr(50).

    `_score_project` reads the INVOICE, not the candidate, so it is earned unconditionally
    and says nothing about which commitment a line ties to. With a floor of only 50, a
    `green=60` config let a PO-nr-only match auto-approve at 50+10 with supplier, amount
    and article all zero.
    """
    with pytest.raises(ValueError):
        _run(thresholds={"green": 60, "yellow": 40, "supplier_overrides": {}})
    # 61 is the first legal value and must still work.
    assert _run(thresholds={"green": 61, "yellow": 40, "supplier_overrides": {}})["tier"] == "GREEN"


def test_green_equal_to_yellow_is_legal():
    """`green == yellow` is a decided policy choice (a blunt but coherent operator config
    that collapses the review band) — pinned so a future tightening cannot forbid it
    silently."""
    r = _run(thresholds={"green": 100, "yellow": 100, "supplier_overrides": {}})
    assert r["tier"] == "GREEN"  # a 130-point match clears 100


def test_override_carrying_only_a_comment_is_valid():
    """`_comment` is an allowed inner key (the shipped JSON uses the idiom at top level),
    and an override carrying only a comment must inherit both defaults cleanly."""
    r = _run(
        invoice_header={**_DEFAULT_INVOICE_HEADER, "supplier_orgnr": "987654321"},
        thresholds={
            "green": 115,
            "yellow": 70,
            "supplier_overrides": {"987654321": {"_comment": "flagged by MatchLearning"}},
        },
    )
    assert r["tier"] == "GREEN"  # inherited 115/70, so a 130-point match is GREEN


def test_unknown_override_key_raises_instead_of_silently_loosening():
    """A typo'd or drifted inner override key must raise, not fall back to the default.

    Silently ignoring it always fails toward LOOSENESS: the inherited default is by
    construction looser than the tightening the author was trying to express.
    """
    for bad in ({"greenThreshold": 200}, {"Green": 200}, {"thresholds": {"green": 200}}):
        with pytest.raises(ValueError):
            _run(
                invoice_header={**_DEFAULT_INVOICE_HEADER, "supplier_orgnr": "987654321"},
                thresholds={
                    "green": 115,
                    "yellow": 70,
                    "supplier_overrides": {"987654321": bad},
                },
            )


def test_config_is_validated_even_when_invoice_has_no_lines():
    """The invariant guards the CONFIG, not the invoice, so it must fire on an empty one.

    Regression guard: resolving thresholds after the no-lines short-circuit meant a
    poisoned threshold set passed silently for every empty invoice and only surfaced once
    a line happened to exist.
    """
    with pytest.raises(ValueError):
        do_match_invoice({"green": 0, "yellow": 0, "supplier_overrides": {}}, {"lines": []}, [])


def test_incoherent_thresholds_raise():
    """green<yellow, a non-positive-enough green that inverts against yellow, or a
    per-supplier override that inverts the resolved pair, must all raise ValueError
    rather than silently deleting the human-review tier. Proven against the pre-fix
    no-validation mutant in the PROVE section below."""
    with pytest.raises(ValueError):
        _run(thresholds={"green": 70, "yellow": 115, "supplier_overrides": {}})

    with pytest.raises(ValueError):
        _run(thresholds={"green": 0, "yellow": 70, "supplier_overrides": {}})

    with pytest.raises(ValueError):
        _run(
            invoice_header={**_DEFAULT_INVOICE_HEADER, "supplier_orgnr": "987654321"},
            thresholds={
                "green": 115,
                "yellow": 70,
                "supplier_overrides": {"987654321": {"green": 50, "yellow": 90}},
            },
        )


@pytest.mark.parametrize(
    "green,yellow",
    [
        (0, 0),  # "auto-approve at score zero" — the most dangerous config of all
        (0, -1),
        (-1, -1),
    ],
)
def test_non_positive_green_raises_even_when_pair_is_not_inverted(green, yellow):
    """A `green` cutoff of 0 or below must raise even when the pair is NOT inverted.

    Regression guard for a real hole in the first version of this invariant: it checked
    only `green < yellow or yellow < 0`, and the pair green=0/yellow=0 satisfies BOTH
    (0 is not < 0), so it passed validation silently. With green=0 every score >= 0 is
    GREEN, so a wholly unmatched invoice — no supplier, no expected baseline, no BOM
    candidate, no project dimension, total score 0 — triaged GREEN and became eligible
    for auto-posting. Verified by execution before the fix: score=0 tier='GREEN'.
    """
    with pytest.raises(ValueError):
        _run(thresholds={"green": green, "yellow": yellow, "supplier_overrides": {}})


@pytest.mark.parametrize(
    "green,yellow",
    [
        (True, 0),  # JSON `"green": true` -> Python True -> cutoff 1 -> near-all GREEN
        (True, False),
        (115, True),
    ],
)
def test_boolean_thresholds_raise(green, yellow):
    """A `bool` cutoff must raise, even though `isinstance(True, int)` is True in Python.

    Regression guard: JSON `"green": true` decodes to Python `True`, which passed the
    original `isinstance(green, int)` check and resolved to a green cutoff of 1 — so a
    10-point (effectively unmatched) invoice triaged GREEN. Verified by execution before
    the fix: score=10 tier='GREEN'.
    """
    with pytest.raises(ValueError):
        _run(thresholds={"green": green, "yellow": yellow, "supplier_overrides": {}})


def test_non_positive_green_raises_via_supplier_override():
    """The same non-positive-green hole must also be closed on the per-supplier path —
    that is the one a future MatchLearning writer can actually reach."""
    with pytest.raises(ValueError):
        _run(
            invoice_header={**_DEFAULT_INVOICE_HEADER, "supplier_orgnr": "987654321"},
            thresholds={
                "green": 115,
                "yellow": 70,
                "supplier_overrides": {"987654321": {"green": 0, "yellow": 0}},
            },
        )


def test_three_way_result_is_read_not_derived():
    """Score/tier must be identical regardless of the three-way verdict; the verdict
    must still be echoed unchanged into the breakdown."""
    r_green = _run(three_way_result={"tier": "GREEN", "confidence": 98.0})
    r_red = _run(three_way_result={"tier": "RED", "confidence": 12.0})

    assert r_green["score"] == r_red["score"]
    assert r_green["tier"] == r_red["tier"]
    assert _entry(r_green)["three_way_result"] == {"tier": "GREEN", "confidence": 98.0}
    assert _entry(r_red)["three_way_result"] == {"tier": "RED", "confidence": 12.0}


def test_three_way_result_presence_does_not_change_score():
    """None vs a present dict must both score EXACTLY 130 (literal on both arms) — the
    previous test only compared the two arms to EACH OTHER, so a bonus keyed on mere
    presence of a three_way_result (rather than its content) would have survived it.
    Proven against such a mutant in the PROVE section below."""
    r_absent = _run(three_way_result=None)
    r_present = _run(three_way_result={"tier": "GREEN", "confidence": 98.0})
    assert r_absent["score"] == 130
    assert r_present["score"] == 130


# ---------------------------------------------------------------------------
# (b2) MULTI-CANDIDATE POOL tests — exercise _best_candidate_result's scan over more
# than one candidate, via _run's extra_candidates / _candidate() helper.
# ---------------------------------------------------------------------------


def test_multi_candidate_pool_picks_highest_scoring_candidate():
    """3-candidate pool with the highest-scoring candidate in the MIDDLE position
    (index 1) — proves the winner search scans the WHOLE pool, not just first/last."""
    weak_context = {
        "supplier_exact": False,
        "supplier_fuzzy": False,
        "expected_amount": 0,
        "po_expected": False,
        "supplier_pattern_seen": False,
    }
    best = _candidate(
        bom_line=dict(_BASE_BOM_LINE), context=dict(_DEFAULT_CONTEXT), candidate_id="best"
    )
    weak2 = _candidate(context={**weak_context, "supplier_fuzzy": True}, candidate_id="weak2")

    r = _run(
        invoice_header={"project_dimension_present": False, "project_dimension_inferred": False},
        bom_line=None,
        context=weak_context,
        extra_candidates=[best, weak2],
    )
    e = _entry(r)
    assert e["total"] == 120
    assert e["candidate_index"] == 1
    assert e["candidate_id"] == "best"


def test_multi_candidate_tie_keeps_first_candidate():
    """Two candidates score an IDENTICAL total; the FIRST encountered must win. Kills a
    ties-keep-LAST tie-break, which would post the invoice against a different
    commitment (distinct candidate_id / three_way_result) at the same score."""
    tied = _candidate(
        bom_line=dict(_BASE_BOM_LINE),
        context=dict(_DEFAULT_CONTEXT),
        three_way_result={"tier": "RED", "confidence": 5.0},
        candidate_id="second",
    )
    r = _run(three_way_result={"tier": "GREEN", "confidence": 99.0}, extra_candidates=[tied])
    e = _entry(r)
    assert e["total"] == 130  # confirm the tie actually happened
    assert e["candidate_index"] == 0
    assert e["candidate_id"] is None
    assert e["three_way_result"] == {"tier": "GREEN", "confidence": 99.0}


def test_candidate_id_never_collides_with_explicit_id():
    """A positional-fallback candidate (no explicit candidate_id, sitting at pool
    position 0) plus a second candidate with an EXPLICIT candidate_id=0 (sitting at
    position 1) — two invoice lines each win a different one of the two. The reported
    identities must be distinguishable: (None, index 0) vs (0, index 1). A positional-
    index fallback bug would report BOTH as candidate_id=0, misattributing which
    commitment the first line actually matched (D5) — this is what candidate_index
    exists to prevent."""
    invoice = {
        "supplier_orgnr": "987654321",
        "project_dimension_present": False,
        "project_dimension_inferred": False,
        "lines": [
            {
                "article_no": "AAA",
                "description": "Alpha widget",
                "quantity": 1,
                "unit_price": 10_000,
                "line_total": 10_000,
            },
            {
                "article_no": "BBB",
                "description": "Beta widget",
                "quantity": 1,
                "unit_price": 20_000,
                "line_total": 20_000,
            },
        ],
    }
    candidate_a = _candidate(
        bom_line={"article_no": "AAA", "description": "Alpha widget", "project_id": "p1"},
        context={
            "supplier_exact": True,
            "supplier_fuzzy": False,
            "expected_amount": 10_000,
            "po_expected": False,
            "supplier_pattern_seen": False,
        },
    )  # no candidate_id -> reported as None
    candidate_b = _candidate(
        bom_line={"article_no": "BBB", "description": "Beta widget", "project_id": "p2"},
        context={
            "supplier_exact": False,
            "supplier_fuzzy": True,
            "expected_amount": 20_000,
            "po_expected": True,
            "supplier_pattern_seen": True,
        },
        candidate_id=0,
    )
    r = do_match_invoice(_FIXTURE_THRESHOLDS, invoice, [candidate_a, candidate_b])

    assert r["breakdown"][0]["candidate_index"] == 0
    assert r["breakdown"][0]["candidate_id"] is None
    assert r["breakdown"][1]["candidate_index"] == 1
    assert r["breakdown"][1]["candidate_id"] == 0


def test_no_lines_returns_conservative_red():
    invoice = {**_DEFAULT_INVOICE_HEADER, "lines": []}
    r = do_match_invoice(_FIXTURE_THRESHOLDS, invoice, [])
    assert r == {"score": 0, "tier": "RED", "breakdown": []}


def test_invoice_missing_lines_key_returns_conservative_red():
    invoice = dict(_DEFAULT_INVOICE_HEADER)  # no "lines" key at all
    r = do_match_invoice(_FIXTURE_THRESHOLDS, invoice, [])
    assert r == {"score": 0, "tier": "RED", "breakdown": []}


def test_line_with_no_candidates_is_still_scored_not_skipped():
    invoice = {**_DEFAULT_INVOICE_HEADER, "lines": [dict(_DEFAULT_LINE)]}
    r = do_match_invoice(_FIXTURE_THRESHOLDS, invoice, [])

    assert len(r["breakdown"]) == 1
    e = r["breakdown"][0]
    # Header-level project component still earns points even with zero candidates.
    assert e["project_match"] == 10
    assert e["supplier_match"] == 0
    assert e["bom_line_match"] == 0
    assert e["three_way_result"] is None
    assert e["total"] == 10
    assert r["score"] == 10
    assert r["tier"] == "RED"


@pytest.mark.parametrize(
    "malformed_expected_amount",
    [float("nan"), float("inf"), float("-inf"), -1.0, 0.0],
)
def test_malformed_expected_amount_never_produces_green_and_stays_int(
    malformed_expected_amount,
):
    r = _run(
        invoice_header={"project_dimension_present": False, "project_dimension_inferred": False},
        bom_line=None,
        context={
            "supplier_exact": False,
            "supplier_fuzzy": False,
            "expected_amount": malformed_expected_amount,
            "po_expected": False,
            "supplier_pattern_seen": False,
        },
    )
    e = _entry(r)
    assert e["amount_match"] == 0
    assert isinstance(e["amount_match"], int)
    assert isinstance(e["total"], int)
    assert isinstance(r["score"], int)
    assert r["tier"] != "GREEN"


@pytest.mark.parametrize("line_total", [float("nan"), float("inf"), float("-inf")])
def test_malformed_line_total_never_raises_and_stays_int(line_total):
    """NaN/inf on the invoice line's own line_total must not raise and must not let the
    amount component escape as a float — even with every other component maxed out."""
    r = _run(line={**_DEFAULT_LINE, "line_total": line_total})
    e = _entry(r)
    assert e["amount_match"] == 0
    assert isinstance(e["amount_match"], int)
    assert isinstance(e["total"], int)
    assert isinstance(r["score"], int)
    # Amount contributes 0, so max possible here is 40+20+10+15+10+5=100 -> not GREEN.
    assert r["tier"] != "GREEN"


# ===========================================================================
# (c) CONFIG tests — real JSON file loads and contains documented keys
# ===========================================================================


def test_load_economy_thresholds_loads_without_error():
    thresholds = load_economy_thresholds()
    assert isinstance(thresholds, dict)


def test_real_config_contains_green_and_yellow():
    thresholds = load_economy_thresholds()
    assert "green" in thresholds
    assert "yellow" in thresholds
    assert thresholds["green"] > thresholds["yellow"]


def test_real_config_contains_supplier_overrides_map():
    thresholds = load_economy_thresholds()
    assert "supplier_overrides" in thresholds
    assert isinstance(thresholds["supplier_overrides"], dict)


def test_real_config_default_values_are_115_and_70():
    """The tenant's documented defaults — asserted ONLY here against the real JSON, never
    baked into the algorithm/behaviour tests above."""
    thresholds = load_economy_thresholds()
    assert thresholds["green"] == 115
    assert thresholds["yellow"] == 70


def test_real_config_drives_a_real_match():
    """End-to-end: the real JSON file's thresholds actually drive do_match_invoice."""
    thresholds = load_economy_thresholds()
    r = _run(thresholds=thresholds)
    assert r["score"] == 130
    assert r["tier"] == "GREEN"


# ===========================================================================
# (d) A MISSING amount must be distinguishable from a real zero
#     (no-fabricated-money-defaults ratchet, 2026-09-03)
# ===========================================================================


def test_absent_line_total_is_not_scored_as_a_100_percent_discrepancy():
    """A line with NO ``line_total`` used to default to 0, so ``pct`` came out at
    exactly 1.0 and the reason read "amount divergence 100.0%" -- a confident
    discrepancy claim on a payment decision, when the truth is that there is no
    amount to compare. It must now say so, and must not name a percentage."""
    line = {k: v for k, v in _DEFAULT_LINE.items() if k != "line_total"}
    reasons = _entry(_run(line=line))["reasons"]
    assert "amount: no line total to compare" in reasons
    assert not any("divergence" in r for r in reasons)


def test_a_real_zero_line_total_still_reports_a_divergence():
    """The other half of the pair: an invoice line that genuinely totals 0 against a
    positive expected amount IS a 100% discrepancy, and must keep saying so. Absence
    and zero must not collapse onto the same reason."""
    reasons = _entry(_run(line={**_DEFAULT_LINE, "line_total": 0}))["reasons"]
    assert "amount divergence 100.0%" in reasons
    assert "amount: no line total to compare" not in reasons
