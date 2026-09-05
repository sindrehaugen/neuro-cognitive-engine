"""
nce/vertical_modules/economy/ngaap.py
======================================
Pure NGAAP periodisering — zero DB, zero HTTP, zero web/admin imports.

Ported near-1:1 from the reference implementation
(tests: ``tests/finance/cost-engine.test.ts``). Per ``docs/vertical_engines/08-economy-engine.md``
"Core functions" / "Config keys" / round-2 hardening #5, and ``00-ENGINES-ROADMAP.md`` §2.9.

This is an accrual ENGINE, not an account-number config swap
--------------------------------------------------------------
Round-2 hardening #5 is the governing design point of this module. The **periodisation logic**
— how a cost or a revenue straddling a period boundary splits into recognised / accrued /
deferred / WIP — is **code, right here**. The only thing that comes from
``finago-chart-of-accounts.json`` / ``finago-account-mapping.json`` is **which account number
and which MVA code** each of those computed amounts posts to. Swap the chart JSON and the
*target accounts* change while the *split* is bit-for-bit identical; that is asserted directly
in ``tests/unit/test_economy_ngaap.py``. There is deliberately **no account number literal in
any function body** in this file, and equally deliberately **no split percentage, delivery
curve or recognition rule in either JSON file**. If a future change wants to encode a split in
config, the design has been inverted and must be stopped.

**Jurisdiction: Norwegian GAAP only.** The rules implemented below are regnskapsloven §4-1:
*opptjeningsprinsippet* (§4-1 nr. 2 — revenue is booked when earned, not when invoiced),
*sammenstillingsprinsippet* (§4-1 nr. 3 — cost is matched to the revenue it produced), and
*kongruensprinsippet* (§4-1 nr. 5 — the result is the period effect). IFRS and US-GAAP have
materially different accrual rules (IFRS 15's five-step model, ASC 606 performance
obligations); supporting them is an **engine extension, not a config swap**, and is explicitly
**future work**. Nothing in this module branches on jurisdiction, and nothing should be added
that does — a second GAAP gets its own core.

Where the period boundary is
-----------------------------
The reference ``computeBucketTargets`` carries **no dates**; ``periodEnd`` lives one layer out,
on the run. The period boundary in this engine is expressed by ``delivery_pct``: at period end
it is the fraction of the bucket's contracted scope that has actually been delivered, and it is
what cuts every amount into an in-period half and a carried-across-the-boundary half. That is
the whole point of an accrual engine, and it is why this module does **not** prorate by elapsed
days: time-proration is exactly the flat allocation §4-1 forbids. A bucket that is 0% delivered
halfway through a period earns nothing, no matter how much time has passed. ``period_end`` and
``project_id``, if supplied in ``params``, are echoed into the result untouched.

The seven buckets
------------------
``hardware, materials, freight`` (HW) + ``pm, tek, programming, travel`` (soft) — the reference implementation's
``HW_BUCKETS`` then ``SOFT_BUCKETS``, in that order. This module always iterates that canonical
tuple, never the caller's dict order, so the output ordering is deterministic regardless of how
``params["buckets"]`` was built. A bucket the caller omits is computed from all-zero inputs
(the reference's ``makeInput`` default); a bucket key the caller *invents* is a hard error —
silently dropping a typo'd ``"hardwares"`` would lose that bucket's whole cost.

Per-bucket algorithm (ported 1:1 — same order, same guards)
------------------------------------------------------------
::

    gated_base       = expected_revenue - expected_revenue_from_co  if co_recognition_gated
                       else expected_revenue
    earned_revenue   = max(0, gated_base * delivery_pct)
    revenue_gap      = earned_revenue - actual_invoiced
    accrued          = max(0, revenue_gap)          # under-invoiced -> asset (1531...)
    deferred         = max(0, -revenue_gap)         # over-invoiced  -> liability (2901...)
    recognized_cogs  = expected_cost * delivery_pct # sammenstillingsprinsippet
    wip              = actual_cost - recognized_cogs        # 1771; MAY BE NEGATIVE
    unrecognized     = expected_revenue_from_co * delivery_pct  if co_recognition_gated else 0

``wip`` being signed is load-bearing and is the sharpest proof this is a real accrual and not a
pro-rata allocation: positive WIP is cost spent ahead of delivery (capitalised, carried into the
next period), negative WIP is delivery ahead of cost (cost accrued but not yet invoiced by the
supplier). A flat allocation of a positive total can never produce a negative component.

``delivery_pct`` is **not capped at 1.0** here — 1.05 (over-delivery) flows straight through —
because the reference deliberately leaves that cap to its input layer ("Engine-en capper IKKE").
Porting the 1.0 cap in would silently change numbers relative to the reference. What *is*
enforced is a sign floor and a sanity ceiling: ``delivery_pct`` must be ``0 <= pct <= 10``.
A **negative** ratio is refused because it matches cost against zero revenue (``earned_revenue``
floors at 0 while ``recognized_cogs`` does not) — the exact opposite of the §4-1 nr. 3 matching
this module exists to perform, and the identity guards cannot catch it because WIP absorbs the
whole error. The ceiling of 10 catches the percent-vs-ratio confusion this module itself invites
by reporting ``recognition_basis_pct = delivery_pct * 100``: passing ``50`` meaning "50 %" would
otherwise earn 5 000 000 on a 100 000 contract. 10 is far above any real over-delivery, so no
reference-parity number moves.

Exactness — why every amount is a ``Decimal``
-----------------------------------------------
This is money, and the wave's binding requirement is that the seven buckets sum to the input
total **exactly**. Floats cannot do that: ``0.1 + 0.2 != 0.3``, and a periodisation that loses
an øre puts an unbalanced voucher into a ledger. So:

1. Every money input is coerced to ``Decimal`` and quantised **once**, at the boundary, to øre
   (2 dp, ``ROUND_HALF_UP``). ``float`` inputs go through ``Decimal(str(x))``, never
   ``Decimal(x)`` — the latter would import the binary-float error (``Decimal(0.1)`` is
   ``0.1000000000000000055511151231257827021181583404541015625``).
2. ``delivery_pct`` is a **ratio, not money**, so it is NOT quantised — it keeps full precision.
3. The two *derived* products (``earned_revenue``, ``recognized_cogs``) are quantised to øre.
4. Every remaining amount is obtained by **exact subtraction of already-quantised amounts**
   (``wip = actual_cost - recognized_cogs``; ``accrued``/``deferred`` from ``revenue_gap``) and
   is therefore exact at 2 dp with no second rounding anywhere. This is the whole trick: the
   residual is never rounded independently, so it cannot drift away from its complement.

That makes both §4-1 identities hold with zero tolerance, per bucket and summed over all seven:

* **cost side** — ``recognized_cogs + wip == actual_cost``
* **revenue side** — ``actual_invoiced + accrued - deferred == earned_revenue``

Both are re-checked at runtime in :func:`_assert_bucket_identities` and
:func:`_assert_total_identities` (a real ``raise``, not ``assert``, which ``python -O`` strips)
and are asserted from the outside by the tests. The returned amounts are ``Decimal`` objects on
purpose. **Do not call ``float()`` on them** — that reintroduces exactly the drift this
representation exists to prevent. Serialise with ``str()`` (or a ``Decimal``-aware JSON
encoder) at the transport boundary; that is the route layer's job, not this core's.

Coercion boundary — untyped dicts are hostile (Batch 116 lessons, applied up front)
------------------------------------------------------------------------------------
``matching.py`` degrades a bad input to ``0`` because its output is a *score* and a lost point
merely routes an invoice to a human. This module's output is a *posting*, so it makes the
opposite call and **fails loud**: a non-numeric, NaN or infinite amount raises ``ValueError``
naming the bucket and field rather than silently entering the ledger as zero. Likewise
``co_recognition_gated`` accepts **only a real ``bool``** — it is a gate that *withholds*
unapproved change-order revenue, so a truthy-but-not-``True`` value (``"false"``,
``float('nan')``, ``[0]``) degrading to ``False`` would silently recognise revenue the customer
never approved. That is the permissive direction, and it is refused. ``bool`` is rejected
wherever a number is accepted (``isinstance(True, int)`` is ``True`` in Python). Every mapping
whose keys are normalised is normalised **exactly once**, up front, and only the normalised copy
is ever read — normalising for validation while looking up against the raw dict was Batch 116's
worst defect.
"""

from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Config loaders — read from nce/config_data/ (no config class), mirroring
# load_economy_thresholds() in matching.py / load_procurement_config() in procurement/tco.py
# ---------------------------------------------------------------------------

_CONFIG_DATA_DIR = Path(__file__).parents[3] / "nce" / "config_data"


def load_finago_chart_of_accounts() -> dict[str, Any]:
    """Load ``finago-chart-of-accounts.json`` — the account numbers, and nothing else.

    Returns
    -------
    dict with keys ``bucket_accounts`` (bucket -> role -> account number),
    ``shared_accounts`` (``wip``), ``accounts`` (account number -> name/type),
    ``buckets``, ``country``, ``gaap``.
    """
    return _load_config("finago-chart-of-accounts.json")


def load_finago_account_mapping() -> dict[str, Any]:
    """Load ``finago-account-mapping.json`` — the MVA / balance-side resolver.

    ``role_balance_side`` is the account's **natural balance side** (accounting metadata), not
    the side a given leg posts on; see :func:`do_compute_bucket_targets`.

    Returns
    -------
    dict with keys ``role_mva_code``, ``role_balance_side``, ``account_mva_overrides``,
    ``mva_codes``, ``roles``.
    """
    return _load_config("finago-account-mapping.json")


def _load_config(filename: str) -> dict[str, Any]:
    path = _CONFIG_DATA_DIR / filename
    with path.open(encoding="utf-8") as fh:
        loaded: dict[str, Any] = json.load(fh)
    return loaded


# ---------------------------------------------------------------------------
# Canonical shape — buckets and roles. Order is the reference implementation's HW_BUCKETS + SOFT_BUCKETS.
# ---------------------------------------------------------------------------

HW_BUCKETS: tuple[str, ...] = ("hardware", "materials", "freight")
SOFT_BUCKETS: tuple[str, ...] = ("pm", "tek", "programming", "travel")
ALL_BUCKETS: tuple[str, ...] = HW_BUCKETS + SOFT_BUCKETS

# Roles resolved per bucket from chart["bucket_accounts"][bucket]; "wip" is shared across all
# seven buckets (one 1771-style account) and comes from chart["shared_accounts"].
_PER_BUCKET_ROLES: tuple[str, ...] = ("cogs", "revenue", "accrued", "deferred")
_SHARED_ROLES: tuple[str, ...] = ("wip",)

# The money fields a bucket's inputs may carry. Anything else is a typo -> hard error.
_MONEY_FIELDS: tuple[str, ...] = (
    "expected_revenue",
    "expected_cost",
    "actual_cost",
    "actual_invoiced",
    "expected_revenue_from_co",
)
_RATIO_FIELDS: tuple[str, ...] = ("delivery_pct",)
_GATE_FIELDS: tuple[str, ...] = ("co_recognition_gated",)
_VALID_BUCKET_INPUT_KEYS = frozenset(
    _MONEY_FIELDS + _RATIO_FIELDS + _GATE_FIELDS + ("bucket", "_comment")
)

# The TOP-LEVEL `params` keys. Held to exactly the same standard as the bucket names one level
# down: a typo'd bucket name loses that bucket's whole cost, and a typo'd `"buckets"` loses all
# seven at once — a fully-formed, apparently-successful periodisering with every amount zero,
# both §4-1 identities satisfied (0 == 0) and no exception. That is strictly worse than a crash.
_VALID_PARAMS_KEYS = frozenset({"buckets", "project_id", "period_end", "_comment"})

# `delivery_pct` bounds — see the module docstring. The 1.0 over-delivery cap is deliberately
# ABSENT (reference parity); these two are a sign floor and a percent-vs-ratio sanity ceiling.
_MIN_RATIO = Decimal(0)
_MAX_RATIO = Decimal(10)

_ORE = Decimal("0.01")
# Money zero carries the øre scale so every amount in the result serialises consistently
# ("0.00", not "0"). Decimal comparison and arithmetic are value-based, so the scale is
# presentation only and can never affect an identity check.
_ZERO = Decimal("0.00")


# ---------------------------------------------------------------------------
# Coercion boundary — fail loud, never silently permissive
# ---------------------------------------------------------------------------


def _normalised_keys(raw: dict[str, Any], what: str) -> dict[str, Any]:
    """Return *raw* with every key ``str(...).strip()``-normalised, **exactly once**.

    Raises when two keys collide after normalisation. Keeping one of them silently would make
    the winner depend on JSON/dict key order — the same project could periodise differently
    depending on file layout — and the loser is just as likely to be the intended value.
    Callers must read only from the returned copy, never from *raw* (Batch 116's worst defect
    was normalising for validation and then looking up against the raw dict).
    """
    out: dict[str, Any] = {}
    for key, value in raw.items():
        normalised = str(key).strip()
        if normalised in out:
            raise ValueError(
                f"ngaap periodisering: {what} has two keys that normalise to {normalised!r} — "
                f"ambiguous, refusing to guess which applies"
            )
        out[normalised] = value
    return out


def _as_decimal(value: Any, where: str) -> Decimal:
    """Coerce a documented-numeric field to an exact ``Decimal``. Absent/``None`` -> 0.

    Accepts ``int`` / ``float`` / ``Decimal`` only. Rejects ``bool`` FIRST, because
    ``isinstance(True, int)`` is ``True`` in Python and ``"actual_cost": true`` would otherwise
    periodise 1 krone. Rejects ``str`` — a string amount is an ingest bug, and parsing it here
    would be guessing at a number that lands in a ledger. Rejects NaN/inf: ``float('nan')`` is
    TRUTHY in Python and FALSY in JS (a real port-fidelity trap), and a NaN silently becoming 0
    would understate cost with no trace.

    ``float`` is converted via ``Decimal(str(value))``, never ``Decimal(value)`` — the latter
    imports the binary-float representation error into an exact type.
    """
    if value is None:
        return _ZERO
    if isinstance(value, bool):
        raise ValueError(f"ngaap periodisering: {where} must be a number, got bool {value!r}")
    if isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, int):
        return Decimal(value)
    elif isinstance(value, float):
        try:
            candidate = Decimal(str(value))
        except DecimalException as exc:  # pragma: no cover — str(float) is always parseable
            raise ValueError(
                f"ngaap periodisering: {where} is not a usable number: {value!r}"
            ) from exc
    else:
        raise ValueError(
            f"ngaap periodisering: {where} must be int/float/Decimal, "
            f"got {type(value).__name__} {value!r}"
        )
    if not candidate.is_finite():  # NaN, sNaN, +-Infinity
        raise ValueError(f"ngaap periodisering: {where} must be finite, got {value!r}")
    return candidate


def _as_money(value: Any, where: str) -> Decimal:
    """A money amount, quantised to øre exactly once, at the boundary."""
    return _quantise(_as_decimal(value, where), where)


def _as_ratio(value: Any, where: str) -> Decimal:
    """A ratio (``delivery_pct``). NOT quantised — it is a fraction, not an amount, and rounding
    it to 2 dp would change every downstream amount.

    Bounds: ``0 <= value <= 10``. Deliberately **not capped at 1.0** — the reference leaves
    over-delivery to its input layer ("Engine-en capper IKKE") and capping here would silently
    diverge from it, so 1.05 must still flow through. The two bounds that ARE enforced:

    * **Negative is refused.** ``earned_revenue`` floors at zero but ``recognized_cogs`` does
      not, so a negative ratio matches cost against no revenue at all — the direct inverse of
      §4-1 nr. 3. Neither identity guard can see it: WIP is the exact residual, so it absorbs
      the whole wrong-signed COGS and both identities still balance. It has to be caught here.
    * **Above 10 is refused.** This module reports ``recognition_basis_pct = delivery_pct * 100``,
      which invites a caller to hand back ``50`` meaning "50 %"; unbounded, that earns 5 000 000
      on a 100 000 contract with no error. 10 = 1000 % delivered is far beyond any real
      over-delivery, so the ceiling catches the unit confusion without touching a reference
      number.
    """
    ratio = _as_decimal(value, where)
    if ratio < _MIN_RATIO:
        raise ValueError(
            f"ngaap periodisering: {where} must not be negative, got {ratio} — a negative "
            f"delivery ratio matches cost against zero revenue (§4-1 nr. 3) and the balance "
            f"identities cannot detect it"
        )
    if ratio > _MAX_RATIO:
        raise ValueError(
            f"ngaap periodisering: {where} must be a RATIO in [0, {_MAX_RATIO}], got {ratio} — "
            f"values above 1 are over-delivery, not percent; pass 0.5, not 50"
        )
    return ratio


def _as_gate(value: Any, where: str) -> bool:
    """A boolean gate. Absent/``None`` -> ``False`` (the reference default); a real ``bool`` ->
    itself; **anything else raises**.

    This is deliberately stricter than ``matching.py``'s ``_flag`` (which degrades a non-bool to
    ``False``) because the two have opposite risk directions. ``_flag`` gates *points*, so
    falling to ``False`` withholds a score — conservative. ``co_recognition_gated`` gates
    *revenue recognition*: falling to ``False`` would recognise change-order revenue the
    customer has not approved (A.7), i.e. book income that does not exist. That is the
    permissive direction and it is refused loudly. ``1``/``0`` are rejected too — an integer
    here means the caller lost the type somewhere, and guessing is how unapproved revenue gets
    recognised.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    raise ValueError(
        f"ngaap periodisering: {where} must be a bool, got {type(value).__name__} {value!r}"
    )


def _quantise(value: Decimal, where: str) -> Decimal:
    """Round to øre, ties away from zero (the Norwegian accounting convention).

    Port note: the TypeScript reference's ``round2`` is ``Math.round(n * 100) / 100``, which
    breaks ties toward ``+Infinity`` — so ``-0.005`` rounds to ``-0.00`` there and ``-0.01``
    here. That asymmetry only bites on an exact half-øre, which requires an odd ``delivery_pct``
    against a negative base, and rounding an accrual *toward* zero understates a liability. The
    accounting convention wins. Note also that the reference does not round inside
    ``computeBucketTargets`` at all (it rounds one layer out, in ``updateProjectionFromTargets``),
    so this is a boundary this port owns rather than a behaviour it inherited.

    Catches ``DecimalException``, not just ``InvalidOperation``: a caller gets **one** exception
    type out of this module (``ValueError``) for every unusable number, so it never has to also
    know the ``decimal`` exception hierarchy to handle a bad amount.
    """
    try:
        return value.quantize(_ORE, rounding=ROUND_HALF_UP)
    except DecimalException as exc:
        raise ValueError(
            f"ngaap periodisering: {where} is too large to express in øre: {value!r}"
        ) from exc


def _product(left: Decimal, right: Decimal, where: str) -> Decimal:
    """Multiply two exact amounts, converting any ``decimal`` failure into a ``ValueError``.

    ``Decimal.__mul__`` raises ``decimal.Overflow`` (an ``ArithmeticError``, **not** a
    ``ValueError`` and not an ``InvalidOperation``) when the product exceeds the context's
    ``Emax``. That escaped the module untranslated, so a caller who correctly handled
    ``ValueError`` around a periodisering still got an unhandled ``Overflow`` from deep inside
    the engine. Every numeric guard in this module raises ``ValueError``; this is the last hole.
    """
    try:
        return left * right
    except DecimalException as exc:
        raise ValueError(
            f"ngaap periodisering: {where} is not expressible as a number "
            f"({left!r} * {right!r}): {type(exc).__name__}"
        ) from exc


# ---------------------------------------------------------------------------
# Input normalisation
# ---------------------------------------------------------------------------


def _bucket_inputs(params_buckets: dict[str, Any], bucket: str) -> dict[str, Any]:
    """Return the coerced inputs for one bucket. A missing bucket is all-zero (the reference's
    ``makeInput`` default); an unknown key inside a bucket is a hard error."""
    raw = params_buckets.get(bucket)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"ngaap periodisering: params['buckets'][{bucket!r}] must be an object, "
            f"got {type(raw).__name__}"
        )
    inputs = _normalised_keys(raw, f"params['buckets'][{bucket!r}]")

    unknown = set(inputs) - _VALID_BUCKET_INPUT_KEYS
    if unknown:
        raise ValueError(
            f"ngaap periodisering: params['buckets'][{bucket!r}] has unknown key(s) "
            f"{sorted(unknown)} — expected a subset of {sorted(_VALID_BUCKET_INPUT_KEYS)}"
        )

    # The reference's BucketInputs carries a redundant `bucket` field. Accept it (callers port
    # the shape wholesale) but require it to AGREE with the key it is filed under — a mismatch
    # means the caller built the wrong bucket's numbers, and silently trusting the key would
    # post one bucket's cost to another bucket's accounts.
    declared = inputs.get("bucket")
    if declared is not None and str(declared).strip() != bucket:
        raise ValueError(
            f"ngaap periodisering: params['buckets'][{bucket!r}] declares bucket "
            f"{declared!r} — the key and the 'bucket' field must agree"
        )

    coerced: dict[str, Any] = {
        field: _as_money(inputs.get(field), f"buckets[{bucket!r}].{field}")
        for field in _MONEY_FIELDS
    }
    for field in _RATIO_FIELDS:
        coerced[field] = _as_ratio(inputs.get(field), f"buckets[{bucket!r}].{field}")
    for field in _GATE_FIELDS:
        coerced[field] = _as_gate(inputs.get(field), f"buckets[{bucket!r}].{field}")
    return coerced


def _normalised_params(params: Any) -> dict[str, Any]:
    """Normalise the TOP-LEVEL ``params`` keys once and reject any key outside
    :data:`_VALID_PARAMS_KEYS`.

    The same rule the bucket names get one level down in :func:`_normalised_params_buckets`, for
    the same reason and with more at stake. ``params.get("buckets")`` on a raw dict silently
    reads ``None`` — and therefore all-zero inputs — for ``"bucket"``, ``"buckets "``,
    ``"Buckets"`` or any other near-miss, so every one of the seven buckets is lost at once and
    the engine returns a complete, internally consistent, entirely zero periodisering: both §4-1
    identities hold trivially (0 == 0), nothing raises, and the caller has no way to tell it
    apart from a genuinely empty period. ``"projectId"`` likewise dropped the project the run
    belongs to. Normalise once, read only the copy (Batch 116's worst defect was doing the
    opposite).
    """
    if not isinstance(params, dict):
        raise ValueError(
            f"ngaap periodisering: params must be an object, got {type(params).__name__}"
        )
    normalised = _normalised_keys(params, "params")
    unknown = set(normalised) - _VALID_PARAMS_KEYS
    if unknown:
        raise ValueError(
            f"ngaap periodisering: params has unknown key(s) {sorted(unknown)} — expected a "
            f"subset of {sorted(_VALID_PARAMS_KEYS)}"
        )
    return normalised


def _normalised_params_buckets(params: dict[str, Any]) -> dict[str, Any]:
    """Normalise ``params['buckets']`` once and reject any bucket name outside the canonical
    seven. A typo'd bucket must never be silently dropped — that would lose its whole cost.

    *params* must already have been through :func:`_normalised_params`."""
    raw = params.get("buckets")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"ngaap periodisering: params['buckets'] must be an object, got {type(raw).__name__}"
        )
    buckets = _normalised_keys(raw, "params['buckets']")
    unknown = set(buckets) - set(ALL_BUCKETS)
    if unknown:
        raise ValueError(
            f"ngaap periodisering: params['buckets'] has unknown bucket(s) {sorted(unknown)} — "
            f"expected a subset of {list(ALL_BUCKETS)}"
        )
    return buckets


# ---------------------------------------------------------------------------
# Account resolution — config-as-IP. No account number literal exists below this line.
# ---------------------------------------------------------------------------


def _resolve_accounts(chart: dict[str, Any], mapping: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Resolve every ``(bucket, role)`` target from *chart* + *mapping*, once, up front.

    Returns ``{bucket: {role: {"account", "account_name", "account_type", "mva_code",
    "balance_side"}}}``. Every lookup failure raises and names the offender: an unresolvable
    target means an amount has nowhere to post, and a periodisering that silently drops a leg
    produces an unbalanced voucher.

    ``balance_side`` is the account's NATURAL BALANCE SIDE — never a posting direction; see
    :func:`do_compute_bucket_targets`.
    """
    bucket_accounts = _require_mapping(chart, "bucket_accounts", "chart-of-accounts")
    shared_accounts = _require_mapping(chart, "shared_accounts", "chart-of-accounts")
    plan = _require_mapping(chart, "accounts", "chart-of-accounts")
    role_mva = _require_mapping(mapping, "role_mva_code", "account-mapping")
    role_side = _require_mapping(mapping, "role_balance_side", "account-mapping")
    overrides = _require_mapping(mapping, "account_mva_overrides", "account-mapping", optional=True)

    resolved: dict[str, dict[str, Any]] = {}
    for bucket in ALL_BUCKETS:
        per_bucket = bucket_accounts.get(bucket)
        if not isinstance(per_bucket, dict):
            raise ValueError(
                f"ngaap periodisering: chart-of-accounts 'bucket_accounts' is missing an object "
                f"for bucket {bucket!r}"
            )
        per_bucket = _normalised_keys(per_bucket, f"chart bucket_accounts[{bucket!r}]")
        targets: dict[str, Any] = {}
        for role in _PER_BUCKET_ROLES:
            targets[role] = _target(
                per_bucket.get(role), role, plan, role_mva, role_side, overrides, bucket
            )
        for role in _SHARED_ROLES:
            targets[role] = _target(
                shared_accounts.get(role), role, plan, role_mva, role_side, overrides, bucket
            )
        resolved[bucket] = targets
    return resolved


def _require_mapping(
    config: dict[str, Any], key: str, what: str, *, optional: bool = False
) -> dict[str, Any]:
    value = config.get(key)
    if value is None and optional:
        return {}
    if not isinstance(value, dict):
        raise ValueError(
            f"ngaap periodisering: {what} key {key!r} must be an object, got {type(value).__name__}"
        )
    return _normalised_keys(value, f"{what}[{key!r}]")


def _target(
    account: Any,
    role: str,
    plan: dict[str, Any],
    role_mva: dict[str, Any],
    role_side: dict[str, Any],
    overrides: dict[str, Any],
    bucket: str,
) -> dict[str, Any]:
    """Build one resolved target. Every field comes from config; nothing is defaulted in code."""
    if not isinstance(account, str) or not account.strip():
        raise ValueError(
            f"ngaap periodisering: chart-of-accounts has no account for "
            f"bucket={bucket!r} role={role!r} (got {account!r})"
        )
    account = account.strip()
    definition = plan.get(account)
    if not isinstance(definition, dict):
        raise ValueError(
            f"ngaap periodisering: chart-of-accounts 'accounts' has no entry for account "
            f"{account!r} (referenced by bucket={bucket!r} role={role!r})"
        )
    raw_mva = overrides[account] if account in overrides else role_mva.get(role)
    if isinstance(raw_mva, bool) or not isinstance(raw_mva, int):
        raise ValueError(
            f"ngaap periodisering: account-mapping has no integer MVA code for account "
            f"{account!r} / role {role!r} (got {raw_mva!r})"
        )
    side = role_side.get(role)
    if side not in ("debit", "credit"):
        raise ValueError(
            f"ngaap periodisering: account-mapping 'role_balance_side' for role {role!r} must be "
            f"'debit' or 'credit', got {side!r}"
        )
    return {
        "account": account,
        "account_name": definition.get("name"),
        "account_type": definition.get("type"),
        "mva_code": raw_mva,
        # NATURAL BALANCE SIDE of the account, NOT the direction this leg posts in. See the
        # Returns section of do_compute_bucket_targets.
        "balance_side": side,
    }


# ---------------------------------------------------------------------------
# The accrual engine — regnskapsloven §4-1. THIS is the part config must never own.
# ---------------------------------------------------------------------------


def _compute_bucket(inputs: dict[str, Any]) -> dict[str, Decimal]:
    """Periodise one bucket over the period boundary. Ported 1:1 from ``computeBucketTargets``.

    Norwegian GAAP only (round-2 #5) — see the module docstring. No jurisdiction branch here,
    and none should be added: IFRS/US-GAAP is an engine extension, future work.
    """
    expected_revenue: Decimal = inputs["expected_revenue"]
    expected_cost: Decimal = inputs["expected_cost"]
    actual_cost: Decimal = inputs["actual_cost"]
    actual_invoiced: Decimal = inputs["actual_invoiced"]
    delivery_pct: Decimal = inputs["delivery_pct"]
    expected_revenue_from_co: Decimal = inputs["expected_revenue_from_co"]
    co_recognition_gated: bool = inputs["co_recognition_gated"]

    # A.7 — a change order the customer has not approved is NOT earned revenue. Strip it from
    # the earning base; it reappears below as `unrecognized`, held out of the P&L.
    gated_base = (
        expected_revenue - expected_revenue_from_co if co_recognition_gated else expected_revenue
    )

    # §4-1 nr. 2 opptjeningsprinsippet — revenue is earned by DELIVERY, not by invoicing or by
    # the calendar. This is the period-boundary cut, and it is why this is not a pro-rata split.
    earned_revenue = _quantise(
        _product(gated_base, delivery_pct, "earned_revenue"), "earned_revenue"
    )
    # `<= _ZERO` rather than `< _ZERO`, and an assignment rather than max(): quantising a tiny
    # negative product yields Decimal("-0.00"), which compares EQUAL to zero, so `max()` would
    # keep the negative-signed zero and emit "-0.00" into a ledger.
    if earned_revenue <= _ZERO:
        earned_revenue = _ZERO

    # The whole accrual: what we earned versus what we actually billed. The gap is an asset when
    # positive (opptjent, ikke fakturert) and a liability when negative (uopptjent inntekt). It
    # is never both — the max(0, ...) pair is the signature of an accrual, not an allocation.
    revenue_gap = earned_revenue - actual_invoiced
    target_accrued = revenue_gap if revenue_gap > _ZERO else _ZERO
    target_deferred = -revenue_gap if revenue_gap < _ZERO else _ZERO

    # §4-1 nr. 3 sammenstillingsprinsippet — cost follows the revenue it produced, so recognised
    # COGS tracks delivery, not spend.
    target_recognized_cogs = _quantise(
        _product(expected_cost, delivery_pct, "recognized_cogs"), "recognized_cogs"
    )

    # WIP (1771) is the RESIDUAL, by exact subtraction — never rounded independently, which is
    # what makes `recognized_cogs + wip == actual_cost` hold to the øre. Signed on purpose:
    # positive = cost spent ahead of delivery (capitalised into the next period);
    # negative = delivered ahead of cost (cost accrued, supplier has not invoiced yet).
    target_wip = actual_cost - target_recognized_cogs

    target_unrecognized = (
        _quantise(_product(expected_revenue_from_co, delivery_pct, "unrecognized"), "unrecognized")
        if co_recognition_gated
        else _ZERO
    )

    return {
        "target_accrued": target_accrued,
        "target_deferred": target_deferred,
        "target_recognized_cogs": target_recognized_cogs,
        "target_wip": target_wip,
        "target_unrecognized": target_unrecognized,
        "earned_revenue": earned_revenue,
        "actual_cost": actual_cost,
        "actual_invoiced": actual_invoiced,
        # Reference parity: `recognitionBasisPct: deliveryPct * 100`. Not money, so not
        # quantised — it is reported, never posted.
        "recognition_basis_pct": _product(delivery_pct, Decimal(100), "recognition_basis_pct"),
    }


def _assert_bucket_identities(bucket: str, amounts: dict[str, Decimal]) -> None:
    """Re-check the two §4-1 identities for one bucket, with zero tolerance.

    A real ``raise``, not ``assert``: ``python -O`` strips ``assert``, and this is the guard
    that stops an unbalanced periodisering reaching a ledger.
    """
    cost_side = amounts["target_recognized_cogs"] + amounts["target_wip"]
    if cost_side != amounts["actual_cost"]:
        raise ValueError(
            f"ngaap periodisering: cost identity broken for bucket {bucket!r}: "
            f"recognized_cogs + wip = {cost_side} != actual_cost {amounts['actual_cost']}"
        )
    revenue_side = (
        amounts["actual_invoiced"] + amounts["target_accrued"] - amounts["target_deferred"]
    )
    if revenue_side != amounts["earned_revenue"]:
        raise ValueError(
            f"ngaap periodisering: revenue identity broken for bucket {bucket!r}: "
            f"invoiced + accrued - deferred = {revenue_side} != "
            f"earned_revenue {amounts['earned_revenue']}"
        )


def _assert_total_identities(totals: dict[str, Decimal]) -> None:
    """Re-check both identities summed over all seven buckets — the wave's binding requirement
    that the seven buckets sum to the input total exactly."""
    cost_side = totals["recognized_cogs"] + totals["wip"]
    if cost_side != totals["actual_cost"]:
        raise ValueError(
            f"ngaap periodisering: total cost identity broken: recognized_cogs + wip = "
            f"{cost_side} != actual_cost {totals['actual_cost']}"
        )
    revenue_side = totals["actual_invoiced"] + totals["accrued"] - totals["deferred"]
    if revenue_side != totals["earned_revenue"]:
        raise ValueError(
            f"ngaap periodisering: total revenue identity broken: invoiced + accrued - deferred "
            f"= {revenue_side} != earned_revenue {totals['earned_revenue']}"
        )


_TOTAL_OF: dict[str, str] = {
    "accrued": "target_accrued",
    "deferred": "target_deferred",
    "recognized_cogs": "target_recognized_cogs",
    "wip": "target_wip",
    "unrecognized": "target_unrecognized",
    "earned_revenue": "earned_revenue",
    "actual_cost": "actual_cost",
    "actual_invoiced": "actual_invoiced",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def do_compute_bucket_targets(
    chart: dict[str, Any], mapping: dict[str, Any], params: dict[str, Any]
) -> dict[str, Any]:
    """Periodise a project's seven buckets over a period boundary under Norwegian GAAP.

    Parameters
    ----------
    chart:
        Contents of ``finago-chart-of-accounts.json`` — the **account numbers**
        (``bucket_accounts``, ``shared_accounts``, ``accounts``). Swapping this changes the
        target accounts and nothing about the split.
    mapping:
        Contents of ``finago-account-mapping.json`` — the **MVA / balance-side resolver**
        (``role_mva_code``, ``role_balance_side``, ``account_mva_overrides``).
    params:
        dict whose keys are exactly a subset of ``buckets``, ``project_id``, ``period_end``,
        ``_comment``. **Any other top-level key raises** — a typo'd ``"buckets"`` would
        otherwise drop all seven buckets at once and return a zero periodisering that satisfies
        both identities and looks successful.

            ``buckets``     dict, optional — a subset of
                            ``hardware|materials|freight|pm|tek|programming|travel``. Each entry
                            may carry ``expected_revenue``, ``expected_cost``, ``actual_cost``,
                            ``actual_invoiced``, ``expected_revenue_from_co`` (money),
                            ``delivery_pct`` (a RATIO, ``0 <= pct <= 10``; deliberately **not**
                            capped at 1.0, so over-delivery flows through — see the module
                            docstring) and ``co_recognition_gated`` (bool). Omitted buckets and
                            omitted fields are 0/False. An unknown bucket name or field name
                            raises.
            ``project_id``  optional — echoed unchanged.
            ``period_end``  optional — echoed unchanged. The engine does **not** prorate by
                            date; the boundary cut is ``delivery_pct`` (see module docstring).

    Returns
    -------
    dict with keys:
        ``project_id`` / ``period_end``  echoed from *params* (``None`` when absent).
        ``gaap`` / ``country``           echoed from *chart* — Norwegian GAAP only.
        ``buckets``   list of 7 dicts in canonical order (HW then soft), each carrying the five
                      targets plus ``earned_revenue``, ``actual_cost``, ``actual_invoiced``,
                      ``recognition_basis_pct`` and an ``accounts`` sub-dict of resolved
                      ``{role: {account, account_name, account_type, mva_code, balance_side}}``.
        ``totals``    the eight per-bucket amounts summed over all seven buckets.

    A leg's debit/credit direction follows the SIGN of its amount; ``balance_side`` is the
    account's natural balance side and must never be used to derive posting direction. (The
    reference posts the revenue account on *both* sides — debited in the deferred leg, credited
    in the accrued leg — and credits the WIP account by ``-cogsDelta``, so reading a direction
    off this field mis-signs a leg; the balance guard cannot catch it, because both sign
    orderings sum to zero.)

    **Every amount is a ``Decimal``.** Do not ``float()`` them — serialise with ``str()``.

    Raises
    ------
    ValueError
        On any unusable input or unresolvable account, naming the offending param/bucket/field/
        account. This engine never degrades a bad number to zero: its output is a posting, and
        ``ValueError`` is the ONLY exception type it raises for a bad number (``decimal``
        failures are translated).
    """
    accounts_by_bucket = _resolve_accounts(chart, mapping)
    params = _normalised_params(params)
    params_buckets = _normalised_params_buckets(params)

    totals: dict[str, Decimal] = {name: _ZERO for name in _TOTAL_OF}
    buckets: list[dict[str, Any]] = []

    for bucket in ALL_BUCKETS:
        amounts = _compute_bucket(_bucket_inputs(params_buckets, bucket))
        _assert_bucket_identities(bucket, amounts)
        for total_name, amount_name in _TOTAL_OF.items():
            totals[total_name] += amounts[amount_name]
        entry: dict[str, Any] = {"bucket": bucket}
        entry.update(amounts)
        entry["accounts"] = accounts_by_bucket[bucket]
        buckets.append(entry)

    _assert_total_identities(totals)

    return {
        "project_id": params.get("project_id"),
        "period_end": params.get("period_end"),
        "gaap": chart.get("gaap"),
        "country": chart.get("country"),
        "buckets": buckets,
        "totals": totals,
    }
