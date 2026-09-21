"""
nce/vertical_modules/economy/reconcile_agreements.py
=======================================================
Revenue-side Agreements<->GL reconciliation (charter Wave B-11).

Design answer from Sindre (2026-09-20, relayed via ML-orch): reuse the
existing recognition schedule as "the register value" rather than inventing
a new evaluation of agreement price rules. This module compares POSTED GL
revenue (``economy_get_gl_records`` / ``do_get_gl_records``, ``gl.py``)
against what the contract's own ratable recognition schedule
(``do_compute_recognition_schedule`` / ``do_recognize_recurring``,
``recurring.py``) says SHOULD have been recognized for the period. It
invents no new comparison rule, no new identity-mapping rule, and no new
materiality formula -- every piece is an existing, already-shipping function
or the shared ``record_divergence``/``alert_threshold`` machinery.

Why this is an AGGREGATE comparison, not per-contract
--------------------------------------------------------
``B11_DESIGN_BRIEF.md`` (this lane, same session) named an open question
that Sindre's answer does not resolve: how does one agreement/contract map
to a specific GL account/row? ``economy_postings`` (the table
``do_get_gl_records`` queries) carries no ``contract_id`` column at all --
only ``account``/``period_id``/``economy_source_id``. Inventing a
per-contract identity-resolution rule here would be exactly the kind of
unilateral accounting-policy guess the design brief flagged as unsafe (the
host's own equivalent routine, calibrated by people who own the real
accounts, still produced a 50% false-positive rate). This module sidesteps
that open question entirely: the caller supplies WHICH GL account (or
account prefix) represents recurring revenue for their chart of accounts,
and the comparison is SUM(expected recognition across every contract due
this period) vs SUM(posted GL amount for that account/period) -- an
aggregate check, not a claim about which GL row belongs to which contract.

This is not just documentation: every response carries a literal
``"comparison_scope": "aggregate"`` plus an ``attribution_caveat`` string,
and the ``divergence_log`` row a non-zero delta writes uses the ``field``
value ``recognized_revenue_aggregate_not_attributable_to_contract`` rather
than a bare ``"recognized_revenue"`` -- both so a reader with no access to
this docstring (a caller inspecting the JSON response, or someone querying
``divergence_log`` directly months later) cannot mistake the aggregate for
a per-contract finding (ML-orch review, 2026-09-20: "otherwise the next
person adds per-contract reporting on top of an aggregate that cannot
support it").

No new node type, no new table, no migration
-----------------------------------------------
Every table this module reads already exists and is already RLS-enforced:
``economy_contracts`` (via ``fetch_contracts_for_recognition``),
``economy_postings`` (via ``do_get_gl_records``, itself C8-redacted),
``divergence_log`` (via ``record_divergence``). This module writes nothing
of its own.

Materiality -- one knob, reused, not reinvented
---------------------------------------------------
``alert_threshold()`` (``nce.source_mode.divergence``) is the ONE shared
implementation of ``NCE_DIVERGENCE_ALERT_THRESHOLD`` -- imported directly,
never reimplemented, so this module's classification can never disagree
with whether ``record_divergence`` actually paged anyone for the same
delta (the exact reasoning ``finago.py``'s own docstring gives). The
relative-magnitude formula itself (``abs(delta) / max(abs(a), abs(b),
floor)``) is duplicated here rather than imported from ``finago.py`` --
matching this codebase's own established convention (``recurring.py``'s
docstring: "this module's dependencies point inward... the same reasoning
cascade.py and forecast.py already give") of each economy submodule keeping
its own private arithmetic helper while sharing only the one public
threshold accessor.

A zero delta is never logged at all (mirrors ``finago.py``'s own
convention exactly) -- there is no divergence to record, and computing a
materiality ratio for a zero numerator is pointless.

Alerting is opt-in, off by default -- calibration has not happened
------------------------------------------------------------------------
Unlike the Sales/D365 and Finago pairings, this comparison's materiality
rule has never been validated against real historical data. The design
brief (``B11_DESIGN_BRIEF.md``, "Why this is a decision, not an
engineering task") records direct evidence that this specific problem is
hard even for people with real account access: the host's own equivalent
routine, calibrated by its owners against 36 known loss keys, still
produced 18 false positives -- a 50% false-positive rate. A rule invented
here, without that same access, has no reason to do better. An alert
channel with a 50% false-positive rate is worse than none: it trains
everyone to ignore it, degrading it into an inert instrument with extra
steps -- and because ``dispatch_alert`` and ``NCE_DIVERGENCE_ALERT_THRESHOLD``
are shared with every other divergence pairing, a noisy channel here
would train people to ignore alerts from Sales and Finago too.

So this module computes the comparison and writes the ``divergence_log``
row unconditionally (the data is useful and nobody should lose it), but
only lets ``record_divergence`` actually page when
:func:`alerts_enabled` returns ``True`` -- gated on
``NCE_ECONOMY_RECONCILE_AGREEMENTS_ALERTS_ENABLED``, default **off**. This
is a temporary gate, not a permanent policy: once someone with real
agreement/GL data validates the false-positive rate and decides it is
acceptable, flipping the env var on is the whole rollout. It does not
touch, and is not a substitute for, the AGGREGATE-only attribution
limitation above -- that limitation is architectural (``economy_postings``
has no ``contract_id``) and remains regardless of how alerting is gated.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from nce import config as _config
from nce.source_mode.divergence import alert_threshold, record_divergence
from nce.vertical_modules.economy.contracts import fetch_contracts_for_recognition
from nce.vertical_modules.economy.gl import do_get_gl_records
from nce.vertical_modules.economy.recurring import do_compute_recognition_schedule

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

_ZERO = Decimal("0.00")

# Floor for the materiality denominator -- mirrors finago.py's own
# _MATERIALITY_FLOOR (NOK 1) exactly, same reasoning: without a floor, a
# tiny expected/actual pair would score materiality 1.0 off a one-øre
# difference, drowning out genuinely material divergences.
_MATERIALITY_FLOOR = Decimal("1")

_ENGINE_KEY = "economy"

# Recorded on every divergence_log row this module writes (via `field`, the
# only free-text column available on that table -- see schema.sql,
# divergence_log has no separate notes column) so a reader who finds the row
# later, with no access to this module's source, still sees that the value
# is an aggregate and cannot be read as "contract X is wrong". See the
# module docstring's "aggregate, not per-contract" section for the full
# reasoning (ML-orch review, 2026-09-20).
_DIVERGENCE_FIELD = "recognized_revenue_aggregate_not_attributable_to_contract"

# Echoed verbatim in every response so a caller never has to infer the scope
# from field names alone (ML-orch review, 2026-09-20: "the output must say
# it is an aggregate").
_ATTRIBUTION_CAVEAT = (
    "This compares SUM(expected recognition across every contract due this "
    "period) against SUM(posted GL for the named account/period). It cannot "
    "attribute a discrepancy to any specific contract: economy_postings has "
    "no contract_id column, so no per-contract mapping rule exists (see "
    "B11_DESIGN_BRIEF.md's still-open question #2). A non-zero delta means "
    "the totals disagree, not that any named contract is wrong."
)

# `_materiality_threshold` IS `nce.source_mode.divergence.alert_threshold` --
# the same function object, not a wrapper. See finago.py's own precedent for
# why a reimplementation, even byte-identical, is the actual smell.
_materiality_threshold = alert_threshold

# Whether a material divergence from this specific comparison is allowed to
# page anyone. Default OFF -- see the module docstring's "Alerting is
# opt-in" section for why: this rule has never been calibrated against real
# data, unlike the Sales/D365 and Finago pairings that share the same
# record_divergence/dispatch_alert machinery.
ALERTS_ENABLED_ENV = "NCE_ECONOMY_RECONCILE_AGREEMENTS_ALERTS_ENABLED"


def alerts_enabled() -> bool:
    """Whether a material divergence here is allowed to dispatch an alert.

    Default **False**. Read at call time (not bound onto a cached settings
    object) so a reviewer only has to look at this one function to know the
    default, and so no import-order accident can turn paging on -- same
    reasoning as ``vertical_modules/system_design/archive.py``'s
    ``sweep_enabled()``. The ``divergence_log`` row is written regardless of
    this flag; only the page is gated.
    """
    return _config._bool_env(ALERTS_ENABLED_ENV, False)


def _as_ns_uuid(namespace_id: Any) -> UUID:
    if not namespace_id:
        raise ValueError("'namespace_id' is required")
    return namespace_id if isinstance(namespace_id, UUID) else UUID(str(namespace_id))


def _require_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"do_reconcile_agreements: {field!r} is required")
    return text


def _materiality(expected_value: Decimal, actual_value: Decimal) -> float:
    """Relative divergence magnitude -- mirrors finago.py's own ``_materiality``
    formula exactly (``abs(a - b) / max(abs(a), abs(b), floor)``), a
    magnitude never signed -- direction is preserved separately via the two
    distinct value fields the caller keeps."""
    delta = (expected_value - actual_value).copy_abs()
    denom = max(expected_value.copy_abs(), actual_value.copy_abs(), _MATERIALITY_FLOOR)
    return float(delta / denom)


async def do_reconcile_agreements(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Compare posted GL revenue against the recognition schedule for one period.

    AGGREGATE ONLY -- CANNOT ATTRIBUTE A DISCREPANCY TO A SPECIFIC CONTRACT.
    This compares SUM(expected recognition across every contract due this
    period) against SUM(posted GL for the named account/period).
    ``economy_postings`` has no ``contract_id`` column, so no per-contract
    mapping rule exists (``B11_DESIGN_BRIEF.md``'s still-open question #2) --
    see the module docstring's "aggregate, not per-contract" section for the
    full reasoning. A non-zero ``delta`` means the two TOTALS disagree, never
    that any one named contract is wrong. Both the response
    (``attribution_caveat``) and the ``divergence_log`` row this writes
    (``field``) restate this, so a reader with no access to this docstring
    still sees the scope.

    Parameters
    ----------
    engine:
        ``NCEEngine`` instance (provides ``pg_pool``).
    params:
        ``{
            "namespace_id": str | UUID,     # required
            "period": "YYYY-MM",             # required -- the period to reconcile
            "gl_account": str,                # required -- exact GL account code
                                               # representing recurring revenue for
                                               # this tenant's chart of accounts.
                                               # Never inferred -- see module
                                               # docstring's "aggregate, not
                                               # per-contract" section for why.
            "gl_account_prefix": str | None,  # optional alternative to an exact
                                               # account match (mirrors
                                               # do_get_gl_records's own
                                               # account/account_prefix duality).
                                               # Exactly one of gl_account /
                                               # gl_account_prefix is used --
                                               # gl_account wins if both given.
        }``

    Returns
    -------
    dict
        ``{
            "ok": True,
            "namespace_id": str,
            "period": "YYYY-MM",
            "gl_account": str,
            "expected_recognized_total": float,  # sum across every contract due this period
            "actual_gl_total": float,             # sum of posted GL amount for the account/period
            "delta": float,                       # expected - actual, signed
            "materiality": float | None,          # None when delta == 0 (never computed, never logged)
            "material": bool,                     # materiality > alert_threshold(); False when delta == 0
            "contracts_due": int,                 # count of contracts whose schedule covers this period
            "not_due": list[str],                 # contract_ids not yet due / already past their window
            "comparison_scope": "aggregate",      # always this literal -- see the AGGREGATE ONLY notice above
            "attribution_caveat": str,             # the same notice, as prose, echoed on every call
            "alerts_enabled": bool,                # alerts_enabled() at call time -- False means a
                                                     # "material": true divergence below was still logged
                                                     # but did NOT page anyone; see module docstring's
                                                     # "Alerting is opt-in" section
        }``

    Raises
    ------
    ValueError
        Missing/malformed ``namespace_id``, ``period``, or ``gl_account``.
    """
    ns_uuid = _as_ns_uuid(params.get("namespace_id"))
    period = _require_text(params.get("period"), "period")

    gl_account_raw = params.get("gl_account")
    gl_account_prefix_raw = params.get("gl_account_prefix")
    gl_account = str(gl_account_raw).strip() if gl_account_raw else None
    gl_account_prefix = str(gl_account_prefix_raw).strip() if gl_account_prefix_raw else None
    if not gl_account and not gl_account_prefix:
        raise ValueError(
            "do_reconcile_agreements: either 'gl_account' or 'gl_account_prefix' is required"
        )

    contracts = await fetch_contracts_for_recognition(engine, ns_uuid)

    expected_total = _ZERO
    contracts_due = 0
    not_due: list[str] = []
    for contract in contracts:
        schedule = do_compute_recognition_schedule(
            {
                "contract_id": contract["contract_id"],
                "annual_amount": contract["annual_amount"],
                "start_period": contract["start_period"],
            }
        )
        entry = next((p for p in schedule["periods"] if p["period"] == period), None)
        if entry is None:
            not_due.append(contract["contract_id"])
            continue
        contracts_due += 1
        expected_total += entry["amount"]

    gl_result = await do_get_gl_records(
        engine,
        {
            "namespace_id": str(ns_uuid),
            "account": gl_account,
            "account_prefix": None if gl_account else gl_account_prefix,
            "period_id": period,
        },
    )
    actual_total = sum((Decimal(str(r["amount_nok"])) for r in gl_result["records"]), start=_ZERO)

    delta = expected_total - actual_total
    materiality: float | None = None
    material = False
    alerting_enabled = alerts_enabled()

    if delta != _ZERO:
        materiality = _materiality(expected_total, actual_total)
        material = materiality > _materiality_threshold()

        await record_divergence(
            engine.pg_pool,
            namespace_id=ns_uuid,
            engine=_ENGINE_KEY,
            entity=f"agreement_gl:{period}:{gl_account or gl_account_prefix}",
            field=_DIVERGENCE_FIELD,
            nce_value=str(expected_total),
            ext_value=str(actual_total),
            materiality=materiality,
            alert=alerting_enabled,
        )

    return {
        "ok": True,
        "namespace_id": str(ns_uuid),
        "period": period,
        "gl_account": gl_account or gl_account_prefix,
        "expected_recognized_total": float(expected_total),
        "actual_gl_total": float(actual_total),
        "delta": float(delta),
        "materiality": materiality,
        "material": material,
        "contracts_due": contracts_due,
        "not_due": not_due,
        "comparison_scope": "aggregate",
        "attribution_caveat": _ATTRIBUTION_CAVEAT,
        "alerts_enabled": alerting_enabled,
    }
