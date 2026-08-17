"""
nce/vertical_modules/economy/dunning.py
=========================================
Norwegian dunning / credit policy — pure Advisor (Watcher-triggering) core. Zero DB, zero
HTTP, zero web/admin imports.

Per ``docs/vertical_engines/08-economy-engine.md`` (core function ``do_compute_dunning`` —
"Bisnode risk-score -> dunning aggression (days -3/+3/+10/+21), Lindorff handoff; risk>60 ->
require 100% HW-signing. Pure over config-as-IP (credit bureau swappable)") and
``00-ENGINES-ROADMAP.md`` §9.1/§9.2 (Watcher AI-role: dunning triggers on a Bisnode risk-score
crossing). Batch 127 / Module 8.Wave 12.

What this computes
-------------------
A Norwegian credit bureau (Bisnode, or another bureau behind the same numeric contract) scores
a customer's default risk on a 0-100 scale, higher = riskier. This module maps that single
risk score onto:

* a **dunning aggression tier** (``LOW`` / ``STANDARD`` / ``ELEVATED`` / ``CRITICAL``);
* the **reminder schedule** the tier follows, expressed as day-offsets relative to the
  invoice's due date (the roadmap's canonical Norwegian dunning schedule: -3 / +3 / +10 / +21);
* whether the tier requires **100% hardware(HW)-signing** on future purchase orders for this
  customer (the escalation the roadmap names explicitly: ``risk > 60``);
* whether the tier hands the account to **Lindorff** (external Norwegian debt collection).

This wave adds **no** new JSON config file (B127 orchestrator ruling: config-as-IP is a real
future option for a swappable credit bureau, but is not authorised by this wave). The tier
boundaries below are therefore code constants, exactly like ``matching.py``'s ``_MIN_GREEN`` —
a **deliberate, documented policy decision**, not a magic number standing in for config that
does not exist yet. If a future wave needs per-tenant-adjustable boundaries, that is a config
file addition to make explicitly, not something to infer from this module's constants.

One threshold, not two (money-module briefing #3 — "normalise once")
------------------------------------------------------------------------
The single constant :data:`_CRITICAL_RISK_THRESHOLD` (60) drives BOTH the ``CRITICAL`` tier
boundary AND the HW-signing/Lindorff escalation. There is exactly one ``risk_score >
_CRITICAL_RISK_THRESHOLD`` comparison in this module (:func:`_tier_for_risk_score`), and every
other decision (``hw_signing_required``, ``lindorff_handoff``) is derived FROM the resolved
tier, never from a second, independently-written comparison against the same number. Two
comparisons against the same boundary is exactly the shape of bug this module is guarding
against: a maintainer changes one copy of "60" and not the other, and the tier and the
escalation silently disagree about the same customer.

Tier boundaries — inclusive/exclusive, pinned exactly (money-module briefing #5)
-------------------------------------------------------------------------------
::

    risk_score  < 20                     -> LOW        (friendly; no early/at-due reminder)
    20 <= risk_score <= 39.999...  (< 40) -> STANDARD   (full canonical schedule)
    40 <= risk_score <= 60               -> ELEVATED    (full schedule; NOT escalated)
    60 <  risk_score <= 100              -> CRITICAL    (full schedule; HW-signing + Lindorff)

The roadmap's own wording is "risk>60 -> require 100% HW-signing" — strictly greater-than. A
risk score of *exactly* 60 is deliberately kept in ``ELEVATED``, not escalated: the boundary is
tested at both 60 (``ELEVATED``, no escalation) and the smallest step above it (``CRITICAL``,
escalated) so the cut line can never silently drift in either direction. The other two
boundaries (20, 40) are picked as clean, evenly-spaced bands with the same left-closed,
right-open shape (``[lo, hi)``) as the CRITICAL cut is right-closed at its own lower edge —
every boundary in this module is pinned by an explicit test at the exact value, not just at a
value comfortably inside a band.

Fail toward refusal for a missing/unknown/negative signal (money-module briefing #4, #5)
------------------------------------------------------------------------------------------
An **unrecognised credit signal must never silently fall into the most aggressive tier, nor
the least.** Concretely, this module *raises* — it does not tier — for:

* ``None`` (no bureau data available yet for a brand-new company). Defaulting a "no data"
  customer to ``LOW`` would under-protect a genuinely unscored, unknown-risk counterparty;
  defaulting to ``CRITICAL`` would over-escalate (unnecessary HW-signing + a Lindorff handoff)
  a customer who may simply be new. Neither guess is safe, so neither is made: the caller must
  supply a real score or get a refusal, never a confident tier.
* ``bool`` (``isinstance(True, int)`` is ``True`` in Python; a stray boolean is a caller bug,
  never an intentional risk score of 0 or 1).
* non-numeric values (a ``str`` risk score is never parsed — same rule as every other money/
  score boundary in this module family).
* ``NaN`` / ``+-inf`` (a NaN comparison silently evaluates ``False`` in every direction, which
  is exactly the shape of bug that would let an unscoreable customer slip through unescalated).
* negative values (a negative default-risk score is not "extremely safe"; it is a caller/
  bureau-integration bug, and treating it as an extreme-LOW score would be a confident guess in
  the permissive direction).
* values above 100 (the documented scale is 0-100; anything above signals a caller passed the
  wrong scale entirely — e.g. a raw, un-normalised bureau index — and silently clamping it to
  100 (max aggression) or accepting it as a valid CRITICAL score would hide that bug rather
  than surface it).

Every one of the above raises ``ValueError`` naming the offending value. There is no default
tier for a bad signal — a caller that cannot supply a valid risk score gets no dunning plan,
not a tier chosen for them.

Contract note — the escalation flags are policy labels, not action-now triggers
--------------------------------------------------------------------------------
``do_compute_dunning`` takes only a credit-bureau risk score — no invoice, no due date, no
overdue status. Its ``hw_signing_required``/``lindorff_handoff`` flags say "this customer's
CURRENT risk tier calls for this policy", never "act on this customer right now". This module
is a pure Advisor and cannot itself act on those flags, so today there is no live defect — but
any future Actor-tier caller that treats ``lindorff_handoff=True`` as sufficient justification
to act, without independently checking that this customer actually has an overdue invoice,
could refer a customer with no overdue invoice at all to collections. Gating on real invoice
timing before acting is that future caller's responsibility, not something this module can
enforce from a risk score alone.

Public API
----------
``do_compute_dunning(customer) -> dict`` — see its docstring for the parameter/return shape.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

# ---------------------------------------------------------------------------
# Policy constants — a deliberate code decision (see module docstring), not config-as-IP.
# ---------------------------------------------------------------------------

_MIN_RISK_SCORE = 0.0
_MAX_RISK_SCORE = 100.0

# Tier lower bounds. LOW has no explicit lower bound (0 is its floor via _MIN_RISK_SCORE).
# Each is the INCLUSIVE lower edge of its own tier — see the module docstring's table.
_STANDARD_MIN = 20.0
_ELEVATED_MIN = 40.0
# The escalation boundary IS the CRITICAL tier's own lower edge, and it is EXCLUSIVE
# (risk_score must be STRICTLY greater than this to reach CRITICAL) — the one comparison
# every other decision in this module derives from. See "One threshold, not two" above.
_CRITICAL_RISK_THRESHOLD = 60.0

_TIER_LOW = "LOW"
_TIER_STANDARD = "STANDARD"
_TIER_ELEVATED = "ELEVATED"
_TIER_CRITICAL = "CRITICAL"

# Reminder schedule, in days relative to the invoice due date (roadmap's canonical Norwegian
# dunning schedule: -3 pre-due reminder, +3/+10/+21 post-due escalation). LOW skips the -3/+3
# early touchpoints — a low-risk customer does not need to be chased before or immediately at
# the due date; STANDARD/ELEVATED/CRITICAL all use the full canonical schedule. No tier
# invents a day number the roadmap does not name.
_FULL_SCHEDULE: tuple[int, ...] = (-3, 3, 10, 21)
_LOW_SCHEDULE: tuple[int, ...] = (10, 21)

_SCHEDULE_BY_TIER: dict[str, tuple[int, ...]] = {
    _TIER_LOW: _LOW_SCHEDULE,
    _TIER_STANDARD: _FULL_SCHEDULE,
    _TIER_ELEVATED: _FULL_SCHEDULE,
    _TIER_CRITICAL: _FULL_SCHEDULE,
}

_VALID_CUSTOMER_KEYS = frozenset({"credit_risk_score", "customer_id", "_comment"})


# ---------------------------------------------------------------------------
# Coercion boundary — fail loud, never silently permissive
# ---------------------------------------------------------------------------


def _as_risk_score(value: Any) -> float | Decimal:
    """Coerce the Bisnode/credit-bureau risk score to a ``float`` or ``Decimal`` in
    ``[0, 100]``, or raise.

    See the module docstring's "Fail toward refusal" section for why each rejected case is
    rejected rather than defaulted to a tier. This is a SCORE, not money, so ``float`` is a
    fine exactness level for an ``int``/``float`` input — every boundary compared against it
    (20, 40, 60, 100) is an integer exactly representable in binary floating point. A
    ``Decimal`` input is different: a caller may hand this module a genuinely
    arbitrary-precision score (e.g. one computed elsewhere in exact decimal arithmetic), and
    ``float(some_decimal)`` silently rounds it to the nearest representable double — which can
    round a value that is genuinely ``> 60`` down to exactly ``60.0``, moving a real CRITICAL
    customer into ELEVATED with no error to say so (see FIX 2 in the wave's audit). So a
    ``Decimal`` input is kept as a ``Decimal`` all the way through to the tier comparison —
    never round-tripped through ``float()`` — relying on the fact that Python's ``Decimal``
    supports exact, lossless rich comparison against both ``float`` and ``int`` (comparisons,
    unlike arithmetic, never raise ``TypeError`` for mixed ``Decimal``/``float`` operands and
    never lose precision doing it).
    """
    if value is None:
        raise ValueError(
            "dunning: credit_risk_score is required and must not be None — an unscored "
            "customer must not silently default into any tier (least or most aggressive)"
        )
    if isinstance(value, bool):
        raise ValueError(f"dunning: credit_risk_score must be a number, got bool {value!r}")
    score: float | Decimal
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"dunning: credit_risk_score must be finite, got {value!r}")
        score = value
    elif isinstance(value, (int, float)):
        # A str is never parsed here — same rule as every other money/score boundary in this
        # module family (matching.py/ngaap.py/events.py/forecast.py all refuse to guess at a
        # stringified number). ``float()`` on a plain ``int`` raises ``OverflowError`` (not
        # ``ValueError``) above ~1.8e308 — reraise as the documented ``ValueError`` rather than
        # letting an undocumented exception type escape (FIX 3 in the wave's audit).
        try:
            score = float(value)
        except OverflowError as exc:
            raise ValueError(
                f"dunning: credit_risk_score is too large to represent: {value!r}"
            ) from exc
        if not math.isfinite(score):
            raise ValueError(f"dunning: credit_risk_score must be finite, got {value!r}")
    else:
        raise ValueError(
            f"dunning: credit_risk_score must be int/float/Decimal, got "
            f"{type(value).__name__} {value!r}"
        )
    if score < _MIN_RISK_SCORE or score > _MAX_RISK_SCORE:
        raise ValueError(
            f"dunning: credit_risk_score must be in [{_MIN_RISK_SCORE}, {_MAX_RISK_SCORE}], "
            f"got {score} — a value outside this range signals the wrong bureau scale was "
            f"passed, not a genuine extreme score"
        )
    return score


def _normalised_customer(customer: Any) -> dict[str, Any]:
    """Reject an unknown key rather than silently ignoring it (a typo'd
    ``"credit_risk_scor"`` would otherwise read as 'no score supplied' and raise anyway, but
    only after masking the real cause; failing on the unknown key names it directly)."""
    if not isinstance(customer, dict):
        raise ValueError(f"dunning: customer must be an object, got {type(customer).__name__}")
    unknown = set(customer) - _VALID_CUSTOMER_KEYS
    if unknown:
        raise ValueError(
            f"dunning: customer has unknown key(s) {sorted(unknown)} — expected a subset of "
            f"{sorted(_VALID_CUSTOMER_KEYS)}"
        )
    return customer


# ---------------------------------------------------------------------------
# Tier resolution
# ---------------------------------------------------------------------------


def _tier_for_risk_score(risk_score: float | Decimal) -> str:
    """Map a validated ``[0, 100]`` risk score to its dunning aggression tier.

    Boundary shape (see module docstring table): LOW/STANDARD/ELEVATED are left-closed,
    right-open bands (``lo <= score < hi``); the CRITICAL cut is right-closed at its OWN lower
    edge in the opposite sense — ``score`` must be STRICTLY greater than
    :data:`_CRITICAL_RISK_THRESHOLD` to qualify, so a score of exactly 60 stays ELEVATED. This
    is the ONLY comparison against ``_CRITICAL_RISK_THRESHOLD`` in the module — see "One
    threshold, not two" in the module docstring. ``risk_score`` may be a ``float`` or a
    ``Decimal``; every comparison below is exact for either type (Python's ``Decimal`` never
    round-trips through binary ``float`` for rich comparisons), so a ``Decimal`` input never
    loses precision at this boundary — see :func:`_as_risk_score`.
    """
    if risk_score > _CRITICAL_RISK_THRESHOLD:
        return _TIER_CRITICAL
    if risk_score >= _ELEVATED_MIN:
        return _TIER_ELEVATED
    if risk_score >= _STANDARD_MIN:
        return _TIER_STANDARD
    return _TIER_LOW


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def do_compute_dunning(customer: dict[str, Any]) -> dict[str, Any]:
    """Map a customer's credit-bureau risk score to a Norwegian dunning plan.

    Pure Advisor core: computes and returns; never writes to the DB, never calls out over
    HTTP, never mutates its input.

    Parameters
    ----------
    customer:
        dict with keys:
            ``credit_risk_score``  int/float/Decimal, required — the Bisnode (or equivalent
                                    bureau) default-risk score, ``0`` (safest) to ``100``
                                    (riskiest). See the module docstring's "Fail toward
                                    refusal" section for exactly which inputs raise instead of
                                    silently choosing a tier.
            ``customer_id``        any, optional — an opaque identifier, echoed unchanged
                                    (``None`` when absent). Never used in the tier decision.
        Any other key raises (typo protection — see module docstring).

    Returns
    -------
    dict with keys:
        ``customer_id``         the echoed identifier, or ``None``.
        ``risk_score``          the coerced risk score — ``float`` for an ``int``/``float``
                                 input, or the caller's own ``Decimal`` echoed unchanged for a
                                 ``Decimal`` input (never round-tripped through ``float()``,
                                 which would lose precision — see :func:`_as_risk_score`).
        ``tier``                one of ``"LOW"``, ``"STANDARD"``, ``"ELEVATED"``,
                                 ``"CRITICAL"`` — see the module docstring's boundary table.
        ``reminder_days``       list[int] — day-offsets relative to the invoice due date this
                                 tier's schedule uses (a subset of the canonical
                                 -3/+3/+10/+21).
        ``hw_signing_required`` bool — ``True`` only for ``CRITICAL`` (``risk_score`` strictly
                                 greater than 60): future purchase orders for this customer
                                 require 100% hardware-signing.
        ``lindorff_handoff``    bool — ``True`` only for ``CRITICAL``: the account is handed
                                 to Lindorff (external Norwegian debt collection).
        ``reasons``             list[str] — a short human-readable audit trail explaining the
                                 tier assignment (mirrors ``matching.py``'s ``reasons`` list).

    Contract note — ``hw_signing_required``/``lindorff_handoff`` are forward-looking policy
    labels, not action-now triggers
    ---------------------------------------------------------------------------------------
    This function takes only a credit-bureau risk score (and an opaque customer id) — no
    invoice, no due date, no overdue status. A ``True`` value on either flag means "this
    customer's CURRENT risk tier calls for this policy", not "act on this customer right now".
    This module is a pure Advisor and cannot itself act, so today that distinction has no live
    effect — but a future Actor-tier caller that took ``lindorff_handoff=True`` at face value,
    without additionally checking whether this customer actually has an overdue invoice, could
    refer a customer with no overdue invoice at all to collections. Any Actor-tier consumer of
    this result MUST additionally gate on real invoice timing before acting on either flag.

    Raises
    ------
    ValueError
        The customer object is malformed, carries an unknown key, or ``credit_risk_score`` is
        missing, non-numeric, non-finite, out of ``[0, 100]``, or a ``bool``. Always fails
        toward refusal, never toward a guessed tier (money-module briefing #4).
    """
    validated = _normalised_customer(customer)
    risk_score = _as_risk_score(validated.get("credit_risk_score"))
    customer_id = validated.get("customer_id")

    tier = _tier_for_risk_score(risk_score)
    is_critical = tier == _TIER_CRITICAL

    reasons: list[str] = [f"credit_risk_score={risk_score} -> tier {tier}"]
    if is_critical:
        reasons.append(
            f"risk_score {risk_score} > {_CRITICAL_RISK_THRESHOLD}: 100% HW-signing required, "
            f"handed off to Lindorff"
        )
    elif risk_score == _CRITICAL_RISK_THRESHOLD:
        reasons.append(
            f"risk_score exactly {_CRITICAL_RISK_THRESHOLD}: stays ELEVATED, not escalated "
            f"(escalation requires strictly greater than {_CRITICAL_RISK_THRESHOLD})"
        )

    return {
        "customer_id": customer_id,
        "risk_score": risk_score,
        "tier": tier,
        "reminder_days": list(_SCHEDULE_BY_TIER[tier]),
        "hw_signing_required": is_critical,
        "lindorff_handoff": is_critical,
        "reasons": reasons,
    }
