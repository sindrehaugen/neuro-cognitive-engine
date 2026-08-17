"""
nce/vertical_modules/economy/matching.py
=========================================
Pure invoice-match scoring — zero DB, zero HTTP, zero web/admin imports.

Reconstructed near-1:1 from Andreas's ``lib/finance/matching/score.ts:computeMatchScore``
(tests: ``tests/finance/matching-score.test.ts``). Per PFT v1 design §6.3.2 / docs
``vertical_engines/08-economy-engine.md`` §9.1 and ``00-ENGINES-ROADMAP.md`` §9.1/§2.9.

Score shape
-----------
Andreas's reference scores **one invoice line against one candidate**. This module lifts
that per-pair scorer 1:1 and adds one thin layer on top: an **invoice** carries multiple
lines, and each line is scored against every element of a candidate pool. That outer layer
is *not* part of the ported algorithm — it is this wave's own design (see module docstring
of ``do_match_invoice`` for the exact aggregation rules).

Per-pair component scoring (ported 1:1, same thresholds/points/guard order):

    scorePoNumberMatch   -> 50  when ``context["po_number_exact_match"]`` (optional, absent = false)
    scoreSupplier        -> 40 exact orgnr / 20 fuzzy name / 0 unknown
    scoreAmount          -> guard ``expected_amount <= 0`` FIRST (returns 0, also avoids
                             division by zero); else pct = abs(line_total - expected_amount)
                             / expected_amount; pct <= 0.02 -> 30; pct <= 0.10 -> 15; else 0
    scoreArticle         -> 0 if no bom_line; 20 if both article_nos truthy and equal;
                             else 10 if description token-overlap >= 2; else 0
    scoreProject         -> 10 present / 5 inferred / 0
    scoreBomLine         -> 15 when bom_line exists AND invoice line article_no truthy AND
                             article_nos equal AND bom_line["project_id"] truthy
    scoreExpectedFromPo  -> 10 when ``context["po_expected"]``
    scoreSupplierPattern -> 5 when ``context["supplier_pattern_seen"]``

Every component scorer returns a plain ``int`` literal — never a computed float — so the
total is a bounded int whenever the numeric/boolean inputs are of a documented type
(NaN/inf/negative ``expected_amount`` all degrade to the ``0`` branch of ``scoreAmount``,
never raise, never produce a non-int). Two hardenings over that documented-type baseline,
proven necessary by the round-1 adversarial audit (Batch 116 fix-forward):
boolean gates (``_flag``) reject non-bool truthy values (``"false"``, ``float('nan')``)
instead of scoring them as true, and numeric money fields (``_as_amount``) coerce
``Decimal``/``int``/``float`` and degrade anything else (``None``, ``str``, ``bool``) to
``0.0`` rather than raising ``TypeError`` on mixed ``Decimal``/``float`` arithmetic or on
a non-numeric value reaching the ``<= 0`` guard.

Score scale — 130-pt name, true max 180, NOT clamped
------------------------------------------------------
The classical 100-pt match (supplier 40 + amount 30 + article 20 + project 10) plus the
30-pt PFT context bonus (bomLine 15 + poExpected 10 + supplierPattern 5) sum to the
"130-point" scale the engine is historically named after — Andreas's reference test asserts
exactly 130 for a perfect match. The PO-nr bonus (50 pts, Plan-D 2026-04-25) was added
*additively on top* of that 130-pt scale, not folded into it — the reference test asserts
``withPo.score - baseline.score === 50``, i.e. a true max of 180, and does **not** clamp.
This module ports that decision: no cap is applied anywhere. Clamping to 130 (the wave
file's stale ``Acceptance:`` text) would collapse distinct strong matches to one value and
break the designed 115/70 threshold semantics, so it is deliberately NOT done here.

Invoice-level aggregation (this wave's design, not in the reference)
----------------------------------------------------------------------
1. Each invoice line is scored against **every** candidate in the pool using the ported
   per-pair scorer. A line's result is the candidate with the **highest total**; ties keep
   the **first** candidate encountered (stable, deterministic).
2. A line's tier comes from the resolved thresholds: ``total >= green`` -> GREEN,
   ``total >= yellow`` -> YELLOW, else RED.
3. The invoice tier is the **worst** line tier (RED > YELLOW > GREEN) — conservative
   aggregation.
4. The invoice ``score`` is the **lowest** total among the lines that hold the worst tier
   (the most conservative score consistent with rule 3).
5. Threshold resolution starts from the ``thresholds`` dict's ``green``/``yellow`` defaults,
   then overlays a per-supplier override (keyed by the invoice's ``supplier_orgnr``) when
   present. A missing/absent override leaves the defaults untouched. No literal ``115``/
   ``70`` cutoff appears anywhere in this file's function bodies — cutoffs always come from
   the ``thresholds`` dict (config-as-IP, §2.9).
6. Edge cases: an invoice with **no lines** returns
   ``{"score": 0, "tier": "RED", "breakdown": []}`` (fail conservative). A line with **no
   candidates** is still scored — against a synthetic empty candidate (bom_line absent,
   empty context, no three-way result) — never skipped; header/context components can still
   earn points even with zero candidates.

Procurement boundary (must not be violated)
----------------------------------------------
Each candidate may carry a ``three_way_result`` field — Procurement's PO x GR x invoice
**receiving** verdict (``nce/vertical_modules/procurement/three_way_match.py``). This module
reads that field and echoes it **unchanged** into the winning line's breakdown entry. It
never contributes to the score and is never recomputed here: Procurement owns the receiving
match (goods vs order), Economy owns the financial/posting match (invoice vs commitment) and
is authoritative for the cascade (§9.1). One invoice must never carry two conflicting triage
verdicts.

Loader helper
-------------
``load_economy_thresholds()`` reads ``economy-match-thresholds.json`` from
``nce/config_data/`` and returns it as a plain dict — no config class, mirrors the
``load_procurement_config()`` pattern in ``nce/vertical_modules/procurement/tco.py``.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Config loader — reads from nce/config_data/ (no config class)
# ---------------------------------------------------------------------------

_CONFIG_DATA_DIR = Path(__file__).parents[3] / "nce" / "config_data"


def load_economy_thresholds() -> dict[str, Any]:
    """Load and return the contents of ``economy-match-thresholds.json``.

    Returns
    -------
    dict with keys ``green`` (int), ``yellow`` (int), ``supplier_overrides`` (dict).
    """
    path = _CONFIG_DATA_DIR / "economy-match-thresholds.json"
    with path.open(encoding="utf-8") as fh:
        thresholds: dict[str, Any] = json.load(fh)
    return thresholds


# ---------------------------------------------------------------------------
# Tokenizer — ported 1:1 from score.ts
# ---------------------------------------------------------------------------

# Note: Andreas's TS STOPWORDS literal contains 'for' twice — a Python set naturally
# dedupes duplicate literals, which is not a behaviour change (membership is identical).
_STOPWORDS = {
    "the",
    "a",
    "for",
    "of",
    "and",
    "to",
    "with",
    "i",
    "og",
    "en",
    "et",
    "til",
    "med",
    "for",
}

_TOKEN_SPLIT_RE = re.compile(r"[^a-zæøå0-9]+")


def _tokenize(text: str) -> list[str]:
    """Lowercase, split on non [a-zæøå0-9] runs, keep tokens len>=3 not in STOPWORDS."""
    return [
        token
        for token in _TOKEN_SPLIT_RE.split(text.lower())
        if len(token) >= 3 and token not in _STOPWORDS
    ]


def _flag(value: Any) -> bool:
    """Documented-bool gate. Anything that is not a real bool (or 0/1) counts as absent:
    NaN is falsy here (matching JS truthiness) and a stringified 'false' can never earn
    points. Deliberate, documented hardening over score.ts, which relies on TypeScript's
    compile-time `boolean` for the same guarantee."""
    return value is True or value == 1


def _as_amount(value: Any) -> float:
    """Coerce a documented-numeric money field. Decimal/int/float -> float (restores the
    reference's float arithmetic exactly). Anything else (None, str, bool) -> 0.0, so the
    `<= 0` guard fires and the component scores 0 — never raises, never inflates.

    Deliberately conservative in the untyped direction: a string "30000" now scores 0
    where a loosely-typed reference would parse and score 30. That is the safe direction
    for money code and must stay — never add string-parsing here."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return 0.0
    try:
        return float(value)
    except (ValueError, OverflowError, ArithmeticError):
        return 0.0


# ---------------------------------------------------------------------------
# Component scoring — one function per component (SRP, mirrors score.ts shape)
# Each takes the minimal slice of (invoice, line, bom_line, context) it needs and
# a mutable ``reasons`` list to append to, exactly like the TS reference.
# ---------------------------------------------------------------------------


def _score_supplier(context: dict[str, Any], reasons: list[str]) -> int:
    if _flag(context.get("supplier_exact")):
        reasons.append("supplier exact orgnr match")
        return 40
    if _flag(context.get("supplier_fuzzy")):
        reasons.append("supplier fuzzy name match")
        return 20
    reasons.append("supplier unknown")
    return 0


def _score_amount(context: dict[str, Any], line: dict[str, Any], reasons: list[str]) -> int:
    expected_amount = _as_amount(context.get("expected_amount", 0))
    # Guard FIRST: also prevents division by zero. NaN/inf/negative all fail this
    # comparison to True only when <= 0 (NaN comparisons are always False in Python,
    # so a NaN expected_amount falls through to the pct branch below and degrades to 0).
    if expected_amount <= 0:
        reasons.append("amount: no expected baseline")
        return 0
    line_total = _as_amount(line.get("line_total", 0))
    diff = abs(line_total - expected_amount)
    pct = diff / expected_amount
    if pct <= 0.02:
        reasons.append(f"amount within 2% ({pct * 100:.1f}%)")
        return 30
    if pct <= 0.10:
        reasons.append(f"amount within 10% ({pct * 100:.1f}%)")
        return 15
    reasons.append(f"amount divergence {pct * 100:.1f}%")
    return 0


def _score_article(
    line: dict[str, Any], bom_line: dict[str, Any] | None, reasons: list[str]
) -> int:
    if not bom_line:
        reasons.append("article: no BOM candidate")
        return 0
    line_article_no = line.get("article_no")
    bom_article_no = bom_line.get("article_no")
    if line_article_no and bom_article_no and line_article_no == bom_article_no:
        reasons.append("articleNo exact match")
        return 20
    line_description = line.get("description", "")
    bom_description = bom_line.get("description", "")
    # An explicit None (vs an absent key) defeats the .get default; coerce any non-str
    # to "" rather than let None.lower() raise (D5 — a None description must not raise).
    a = _tokenize(line_description if isinstance(line_description, str) else "")
    b = _tokenize(bom_description if isinstance(bom_description, str) else "")
    overlap = sum(1 for token in a if token in b)
    if overlap >= 2:
        reasons.append(f"article semantic match ({overlap} shared tokens)")
        return 10
    reasons.append("article no match")
    return 0


def _score_project(invoice: dict[str, Any], reasons: list[str]) -> int:
    if _flag(invoice.get("project_dimension_present")):
        reasons.append("project dimension present on invoice")
        return 10
    if _flag(invoice.get("project_dimension_inferred")):
        reasons.append("project inferred from articleNo + supplier context")
        return 5
    reasons.append("project not identifiable")
    return 0


def _score_bom_line(
    line: dict[str, Any], bom_line: dict[str, Any] | None, reasons: list[str]
) -> int:
    if (
        bom_line
        and line.get("article_no")
        and bom_line.get("article_no") == line.get("article_no")
        and bom_line.get("project_id")
    ):
        reasons.append("live BOMLine tie-back via articleNo + projectId")
        return 15
    return 0


def _score_expected_from_po(context: dict[str, Any], reasons: list[str]) -> int:
    if _flag(context.get("po_expected")):
        reasons.append("PO exists expecting this invoice +/-7 days")
        return 10
    return 0


def _score_supplier_pattern(context: dict[str, Any], reasons: list[str]) -> int:
    if _flag(context.get("supplier_pattern_seen")):
        reasons.append("supplier pattern recognized (MatchLearning)")
        return 5
    return 0


def _score_po_number_match(context: dict[str, Any], reasons: list[str]) -> int:
    """2026-04-25 Plan-D: PO-nr match is the strongest deterministic signal. 50 points
    places it as the heaviest single signal (above supplier-exact's 40), riding
    additively on top of the classical+bonus 130-pt scale (see module docstring)."""
    if _flag(context.get("po_number_exact_match")):
        reasons.append("PO-nr eksakt match (orderReference == PurchaseOrder.poNumber)")
        return 50
    return 0


# ---------------------------------------------------------------------------
# Per-pair scoring — one invoice line vs one candidate (the ported algorithm)
# ---------------------------------------------------------------------------

_EMPTY_CANDIDATE: dict[str, Any] = {"bom_line": None, "context": {}, "three_way_result": None}


def _score_pair(
    invoice: dict[str, Any], line: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    """Score one invoice line against one candidate. Returns the 8 component values,
    ``total``, and ``reasons`` — no tier here (tier needs invoice-resolved thresholds)."""
    bom_line = candidate.get("bom_line")
    context = candidate.get("context") or {}
    reasons: list[str] = []

    supplier_match = _score_supplier(context, reasons)
    amount_match = _score_amount(context, line, reasons)
    article_match = _score_article(line, bom_line, reasons)
    project_match = _score_project(invoice, reasons)
    bom_line_match = _score_bom_line(line, bom_line, reasons)
    expected_from_po = _score_expected_from_po(context, reasons)
    supplier_pattern = _score_supplier_pattern(context, reasons)
    po_number_match = _score_po_number_match(context, reasons)

    total = (
        supplier_match
        + amount_match
        + article_match
        + project_match
        + bom_line_match
        + expected_from_po
        + supplier_pattern
        + po_number_match
    )

    return {
        "supplier_match": supplier_match,
        "amount_match": amount_match,
        "article_match": article_match,
        "project_match": project_match,
        "bom_line_match": bom_line_match,
        "expected_from_po": expected_from_po,
        "supplier_pattern": supplier_pattern,
        "po_number_match": po_number_match,
        "total": total,
        "reasons": reasons,
    }


def _best_candidate_result(
    invoice: dict[str, Any], line: dict[str, Any], candidates: list[dict[str, Any]]
) -> tuple[int, dict[str, Any], dict[str, Any]]:
    """Score ``line`` against every candidate; return (index, candidate, result) for the
    highest-scoring one. Ties keep the first (stable, deterministic) — a later candidate
    only replaces the incumbent when it scores strictly higher."""
    pool = candidates if candidates else [_EMPTY_CANDIDATE]

    best_index = 0
    best_result = _score_pair(invoice, line, pool[0])
    for index in range(1, len(pool)):
        result = _score_pair(invoice, line, pool[index])
        if result["total"] > best_result["total"]:
            best_index = index
            best_result = result

    return best_index, pool[best_index], best_result


# ---------------------------------------------------------------------------
# Threshold resolution + tier mapping
# ---------------------------------------------------------------------------


# The floor `green` must exceed, so that auto-approval can never rest on a single
# candidate-tied signal. Two parts, because they are earned differently:
#   * the heaviest CANDIDATE-tied signal is the Plan-D PO-nr exact match (50);
#   * `_score_project` (10) is read from the INVOICE, not the candidate, so it is earned
#     unconditionally and says nothing about which commitment a line ties to.
# A PO-nr-only match on a project-dimensioned invoice therefore reaches 50 + 10 = 60 with
# supplier/amount/article all zero, so the floor must sit ABOVE their sum, not above 50.
# Raising it is a deliberate policy decision, not a config knob.
_MAX_CANDIDATE_TIED_COMPONENT = 50
_MAX_INVOICE_ONLY_COMPONENT = 10
_MIN_GREEN = _MAX_CANDIDATE_TIED_COMPONENT + _MAX_INVOICE_ONLY_COMPONENT

# The only keys an override entry may carry. Anything else is a typo or a drifted schema,
# and silently ignoring it always fails toward LOOSENESS (the inherited default is by
# construction looser than any tightening an operator was trying to express).
_VALID_OVERRIDE_KEYS = frozenset({"green", "yellow", "_comment"})


def _normalised_keys(raw: dict[str, Any], what: str) -> dict[str, Any]:
    """Return *raw* with every key ``str(...).strip()``-normalised, exactly once.

    Raises when two keys collide after normalisation (e.g. ``"green"`` and ``"green "``,
    or ``"987654321"`` and ``" 987654321"``). Silently keeping one of them would make the
    winner depend on JSON key order — the same invoice could tier GREEN or RED depending
    on file layout — and the loser is, by construction, just as likely to be the operator's
    intended tightening.
    """
    out: dict[str, Any] = {}
    for k, v in raw.items():
        k2 = str(k).strip()
        if k2 in out:
            raise ValueError(
                f"economy match thresholds: {what} has two keys that normalise to {k2!r} — "
                f"ambiguous, refusing to guess which applies"
            )
        out[k2] = v
    return out


def _coerce_cutoff(value: Any) -> int | float | None:
    """Return *value* as a usable numeric cutoff, or ``None`` if it is not one.

    Accepts ``int``/``float``/``Decimal`` — ``115.0`` from a hand-edited JSON or a Postgres
    ``numeric`` is a perfectly valid cutoff and must not take the engine down (that was a
    regression introduced by an earlier int-only check). Rejects ``bool`` FIRST, because
    ``isinstance(True, int)`` is True in Python and ``"green": true`` would otherwise
    resolve to a cutoff of 1. Rejects NaN/inf, which would silently disable a whole tier.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        # Guarded: Decimal("sNaN") raises ValueError and Decimal("1E+400") can overflow.
        # This helper's contract is total — it RETURNS None for anything uncoercible so the
        # caller emits one diagnostic naming the offending supplier, rather than leaking a
        # raw conversion error that says nothing about which config is at fault.
        try:
            value = float(value)
        except (ValueError, OverflowError, ArithmeticError):
            return None
    if not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):  # NaN / +-inf
        return None
    return value


def _resolve_thresholds(thresholds: dict[str, Any], invoice: dict[str, Any]) -> dict[str, Any]:
    """Resolve (green, yellow) cutoffs: JSON defaults overlaid by a per-supplier override
    keyed on the invoice's ``supplier_orgnr``. Cutoffs always come from ``thresholds`` —
    never a literal here (config-as-IP, §2.9).

    Every failure mode below is **fail-loud**, and deliberately so: this function decides
    which invoices may auto-post, and every silent failure it could have has a direction —
    toward looseness. A raised ``ValueError`` naming the offending supplier stops one
    tenant's matching; a silent miss auto-approves money.

    The supplier lookup is string-normalised (``str(...).strip()``) because a Postgres
    ``bigint``/unquoted-JSON ``supplier_orgnr`` arrives as a Python ``int`` while JSON
    object keys are always strings — a bare hash lookup would silently miss a real
    override. Deeper canonicalisation ("987 654 321", "NO987654321") belongs at the
    caller/ingest boundary, not here.
    """
    supplier_orgnr = invoice.get("supplier_orgnr")

    # Normalise EVERY mapping exactly once, up front, and read only from the normalised
    # copies below. Normalising at the point of *validation* while looking up against the
    # *raw* dict is what produced this wave's worst defect: a padded inner key `"green "`
    # stripped to `"green"` so it passed the unknown-key guard, then failed the raw lookup
    # and silently inherited the LOOSER default — an operator's tightening evaporated and
    # the invoice auto-posted. One normalisation, one source of truth, no disagreement.
    top = _normalised_keys(thresholds, "thresholds")

    raw_overrides = top.get("supplier_overrides")
    if raw_overrides is None:  # absent, or an explicit JSON null meaning "no overrides"
        raw_overrides = {}
    if not isinstance(raw_overrides, dict):
        # Checked WITHOUT an `or {}` short-circuit: that idiom let every falsy non-dict
        # ([], "", 0, False) slip past the guard it was supposed to be caught by.
        raise ValueError(
            f"economy match thresholds: 'supplier_overrides' must be an object, "
            f"got {type(raw_overrides).__name__}"
        )
    overrides = _normalised_keys(raw_overrides, "supplier_overrides")

    key = "" if supplier_orgnr is None else str(supplier_orgnr).strip()
    override: Any = overrides.get(key, {}) if key else {}
    if not isinstance(override, dict):  # JSON null / scalar / list entry
        raise ValueError(
            f"economy match thresholds: override entry for supplier_orgnr={key!r} must be "
            f"an object, got {type(override).__name__}"
        )
    override = _normalised_keys(override, f"override for supplier_orgnr={key!r}")

    # Unknown/typo'd inner keys are rejected rather than ignored — see _VALID_OVERRIDE_KEYS.
    unknown = set(override) - _VALID_OVERRIDE_KEYS
    if unknown:
        raise ValueError(
            f"economy match thresholds: override for supplier_orgnr={key!r} has unknown "
            f"key(s) {sorted(unknown)} — expected a subset of {sorted(_VALID_OVERRIDE_KEYS)}"
        )

    raw_green = override.get("green", top.get("green"))
    raw_yellow = override.get("yellow", top.get("yellow"))
    green = _coerce_cutoff(raw_green)
    yellow = _coerce_cutoff(raw_yellow)

    # Each clause closes a hole that was reached by execution during this wave's audits:
    #   green/yellow None  -> missing key or a bool/NaN/str cutoff
    #   green <= _MIN_GREEN-> one candidate-tied signal alone could auto-approve a posting
    #   yellow <= 0        -> RED unreachable; every invoice is at least YELLOW
    #   green < yellow     -> inverted pair; a should-be-YELLOW score returns GREEN
    # Note green == yellow stays legal: it is a coherent (if blunt) operator choice that
    # collapses the review band, and all weights are multiples of 5 so 70/69 is identical.
    if green is None or yellow is None or green <= _MIN_GREEN or yellow <= 0 or green < yellow:
        raise ValueError(
            f"economy match thresholds incoherent after override resolution: "
            f"green={raw_green!r} yellow={raw_yellow!r} "
            f"(supplier_orgnr={supplier_orgnr!r}; green must be a non-bool finite number "
            f"> {_MIN_GREEN}, yellow > 0, green >= yellow)"
        )
    return {"green": green, "yellow": yellow}


def _tier_for_score(score: int, green: int, yellow: int) -> str:
    if score >= green:
        return "GREEN"
    if score >= yellow:
        return "YELLOW"
    return "RED"


_TIER_SEVERITY = {"RED": 2, "YELLOW": 1, "GREEN": 0}


def _worst_tier(tiers: list[str]) -> str:
    return max(tiers, key=lambda tier: _TIER_SEVERITY[tier])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def do_match_invoice(
    thresholds: dict[str, Any],
    invoice: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Match an invoice's lines against a candidate pool and triage the whole invoice.

    Parameters
    ----------
    thresholds:
        Contents of ``economy-match-thresholds.json`` — ``green``/``yellow`` defaults plus
        an optional ``supplier_overrides`` map. All cutoffs are read from this dict.
    invoice:
        dict with keys:
            ``supplier_orgnr``            str, optional — drives per-supplier override lookup.
            ``project_dimension_present``  bool, optional.
            ``project_dimension_inferred`` bool, optional.
            ``lines``                      list[dict] — each with ``article_no``,
                                            ``description``, ``line_total`` (and any other
                                            invoice-line fields the caller wants to carry).
    candidates:
        list[dict], each with:
            ``bom_line``         dict | None — ``article_no``, ``description``,
                                  ``project_id``.
            ``context``          dict — ``supplier_exact``, ``supplier_fuzzy``,
                                  ``expected_amount``, ``po_expected``,
                                  ``supplier_pattern_seen``, ``po_number_exact_match``
                                  (optional, absent = false).
            ``three_way_result``  Procurement's receiving verdict — read and echoed
                                  unchanged into the breakdown, never scored, never
                                  recomputed.
            ``candidate_id``      optional identifier echoed into the breakdown.

    Returns
    -------
    dict with keys:
        ``score``      int — the invoice-level score (see aggregation rule 4 above).
        ``tier``       str — GREEN | YELLOW | RED (the worst line tier).
        ``breakdown``  list[dict], one entry per invoice line. Each entry carries BOTH
                        ``candidate_id`` (the winning candidate's explicit id, or ``None``
                        when it supplied none or the pool was empty — never a positional
                        integer) AND ``candidate_index`` (the winning candidate's position
                        in the pool, or ``None`` when the pool was empty and the synthetic
                        candidate was used). Keeping both is lossless and collision-free —
                        a positional fallback alone could collide with a different
                        candidate's explicit ``candidate_id=0`` (D5).
    """
    # Validate the config FIRST, before the no-lines short-circuit: the guard is about the
    # CONFIG, not about this particular invoice. Resolving after the early return meant a
    # poisoned threshold set (green=0, "green": true, an inverted pair) passed silently for
    # every empty invoice and only surfaced once a line happened to exist.
    resolved = _resolve_thresholds(thresholds, invoice)
    green, yellow = resolved["green"], resolved["yellow"]

    lines: list[dict[str, Any]] = invoice.get("lines") or []
    if not lines:
        return {"score": 0, "tier": "RED", "breakdown": []}

    breakdown: list[dict[str, Any]] = []
    for line_index, line in enumerate(lines):
        candidate_index, candidate, result = _best_candidate_result(invoice, line, candidates)
        line_tier = _tier_for_score(result["total"], green, yellow)

        entry = dict(result)
        entry["line_index"] = line_index
        entry["tier"] = line_tier
        # D5: candidate_id must never be a positional fallback — a positional index can
        # collide with a DIFFERENT candidate's explicit candidate_id=0, misattributing
        # which commitment a line matched. Emit both keys, lossless and collision-free:
        # candidate_id = the explicit id or None (never a positional integer);
        # candidate_index = the winning candidate's pool position, or None when the pool
        # was empty and the synthetic candidate was used.
        entry["candidate_id"] = candidate.get("candidate_id") if candidates else None
        entry["candidate_index"] = candidate_index if candidates else None
        entry["three_way_result"] = candidate.get("three_way_result")
        breakdown.append(entry)

    invoice_tier = _worst_tier([entry["tier"] for entry in breakdown])
    invoice_score = min(entry["total"] for entry in breakdown if entry["tier"] == invoice_tier)

    return {"score": invoice_score, "tier": invoice_tier, "breakdown": breakdown}
