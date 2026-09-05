"""
nce/vertical_modules/economy/recurring.py
============================================
Recurring-revenue: MRR/ARR/churn snapshot, ratable 1/12 revenue recognition, and
the idempotent recognition cron (M8.Wave 9, ``finagoRef``-keyed).

Per ``docs/vertical_engines/08-economy-engine.md`` (core function
``do_recognize_recurring``, Build phase B4) and ``00-ENGINES-ROADMAP.md`` §9.1/§9.2.
Inspired by the reference implementation (MRR/ARR/churn +
ratable recognition + recurring cron) — lifted as a **pattern**, not a
transliteration (the reference is outside this repo; no differential harness was
run against it, unlike Batch 116's port).

Scope note — contract source lives in ``economy_contracts`` (Wave 10, retired shim)
-------------------------------------------------------------------------------------
This module writes **zero** ``kg_nodes``/``kg_edges`` rows: no ``CONTRACT`` node
type is registered in ``node_ownership_registry`` (Contract A / C1 deny-by-default),
so contract identity here is purely the caller-supplied ``contract_id`` string, not
a graph node. Every function in this module takes contract data (``contract_id``,
``annual_amount``, ``start_period``, ``status``) **as an explicit parameter** — the
same way ``do_cascade_on_approval`` takes ``lines`` rather than discovering them
from a table — and remains entirely agnostic to where ``params["contracts"]``
came from.

Originally (Wave 9) there was no persisted contract store yet, so the
recurring-cron tick in ``nce/cron.py`` sourced its per-namespace contracts from
``namespaces.metadata->'economy'->'recurring_contracts'`` as a temporary,
no-migration substitute. **Wave 10** (M8.W10 contracts-renewal) added the real
``economy_contracts`` table (migration 049,
``nce/vertical_modules/economy/contracts.py``) and retired that shim: the tick now
calls ``contracts.fetch_contracts_for_recognition`` instead. Nothing changed in
*this* module's own functions — they were contract-source-agnostic from the start,
which is exactly what made the swap a cron.py-only change.

Ratable 1/12 recognition — rounding-remainder convention (pinned, deliberate)
------------------------------------------------------------------------------
``annual_amount / 12`` quantised to øre does not, in general, multiply back up to
``annual_amount`` — a residual of a few øre is unavoidable whenever the annual
amount is not a multiple of 12 øre. Naively posting ``annual/12`` in all twelve
periods would silently drop that residual from the ledger, twelve times a year,
in every contract. This module places the **entire residual in the twelfth
(final) period**, computed by exact subtraction of the eleven already-quantised
base amounts from ``annual_amount`` (mirrors ``ngaap.py``'s residual-by-exact-
subtraction pattern) — never by rounding the last period independently. The
result: the twelve periods sum to ``annual_amount`` **exactly**, re-checked at
runtime with a real ``raise`` (not ``assert`` — ``python -O`` strips those,
per this engine's established convention) in
:func:`do_compute_recognition_schedule`, and pinned by a test that sums all
twelve. Landing the residual on the final period (rather than the first) means a
contract that terminates early always recognised *slightly less* than a full 1/12
share in its steady-state months and reconciles the difference at year-end — the
conventional close-out placement for a ratable schedule.

**Edge case — a negative residual is refused, not redistributed** (Round-2
fix-forward, Batch 124 audit). For a sufficiently small ``annual_amount`` (below
roughly NOK 0.66), the eleven quantised base periods can sum to MORE than the
whole annual amount, so the final period's exact subtraction goes negative —
e.g. ``annual_amount = Decimal("0.06")`` quantises to a per-period base of
``0.01`` (``0.005`` is an exact half-øre tie, ``ROUND_HALF_UP``), and
``11 * 0.01 = 0.11 > 0.06``. The twelve periods still sum to ``annual_amount``
**exactly** in that case — the sum invariant above does not catch it — but
period 12 in isolation is negative money. :func:`do_compute_recognition_schedule`
therefore also checks every period for a negative amount and refuses with a
``ValueError`` rather than redistributing the remainder in ±0.01 increments
across periods: redistribution would change the schedule shape for every
amount, including the realistic ones whose current eleven-equal-plus-residual
behaviour is correct, well-tested, and unchanged by this guard.

Money — Decimal end-to-end
----------------------------
Every amount is coerced via :func:`_as_money` (bool rejected before int —
``isinstance(True, int)`` is ``True`` in Python; NaN/inf rejected — ``float('nan')``
is truthy; ``str`` never parsed) and quantised to øre **once**, via
:func:`_quantise`. :func:`_quantise` mirrors ``ngaap.py``'s own ``_quantise`` —
same scale (``Decimal("0.01")``), same rounding (``ROUND_HALF_UP``), same
``DecimalException`` -> ``ValueError`` translation — and is **reimplemented
locally** rather than imported: this module's dependencies point inward
(``asyncpg``, ``nce.db_utils`` only), the same reasoning ``cascade.py`` and
``forecast.py`` already give for not reaching across to ``ngaap.py`` for a
four-line helper.

Idempotency — structural guarantee, and its limits (say so plainly)
-----------------------------------------------------------------------
Keyed on ``finagoRef = ms:{contractId}:{YYYY-MM}`` (docs' own format). This wave
may not add a migration, so there is no Economy-owned table (unlike
``economy_bom_actual_costs``, migration 047, W5) to hold a
``UNIQUE (namespace_id, contract_id, period)`` constraint. Instead this module
reuses the **already-existing, already-FORCE-RLS**, generic ``action_idempotency``
table (``PRIMARY KEY (namespace_id, idempotency_key)`` — see ``schema.sql`` and
``nce.autonomy.governor``'s identical use of it) with ``idempotency_key =
finagoRef``. ``INSERT ... ON CONFLICT (namespace_id, idempotency_key) DO NOTHING
RETURNING 1`` is the exact idiom ``cascade.py``'s ``_upsert_actual_cost`` uses for
``economy_bom_actual_costs`` — a **single atomic statement**, so there is no
read-then-write race window at all (stronger than a "check, then insert" guard).
A replay of the SAME finagoRef is therefore a genuine structural no-op, not a
guard that has to stay correct.

Batch 120's lesson (cascade.py) was that a replay guard can be correct for the
exact-match case and silently wrong for every other: this module closes that gap
by also storing a ``response_hash`` (SHA-256 of the recognised amount) and, on
every conflict, reading it back and comparing. A replay under the same
``finagoRef`` with a **different** amount is refused (:func:`_record_recognition`
raises ``ValueError``), never silently applied.

**Explicit limit** (the wave asked for this to be named, not buried): ``action_
idempotency``'s primary key is ``(namespace_id, idempotency_key)`` — it is **not**
scoped by ``action_type``. This table is shared across features (also used by
``nce.autonomy.governor`` and ``procurement/po.py``), and this module cannot add
an ``action_type``-scoped constraint without a migration. In practice a
collision is vanishingly unlikely — ``ms:{contractId}:{YYYY-MM}`` is a
distinctive, namespaced string no other caller has any reason to produce — but
if a foreign feature's key ever DID collide, the ``response_hash`` compare
degrades that collision into the same loud ``ValueError`` refusal as a genuine
different-amount replay (see ``test_cross_action_type_collision_is_refused``),
never a silent skip and never a silent double-recognition. Fail toward refusal.
"""

from __future__ import annotations

import hashlib
import logging
import re
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from typing import TYPE_CHECKING, Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.db_utils import scoped_pg_session

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.economy.recurring")

# Money scale — øre, 2 dp. Mirrors ngaap.py / cascade.py's own _ORE.
_ORE: Decimal = Decimal("0.01")
_ZERO: Decimal = Decimal("0.00")

_MONTHS_PER_YEAR: int = 12

_FINAGO_REF_PREFIX: str = "ms"

# The action_idempotency.action_type this module's rows are recorded under.
_ACTION_TYPE: str = "economy_recognize_recurring"

_VALID_CONTRACT_STATUSES: frozenset[str] = frozenset({"active", "churned"})

_PERIOD_RE = re.compile(r"(\d{4})-(0[1-9]|1[0-2])")


# ---------------------------------------------------------------------------
# Coercion boundary — mirrors ngaap.py / cascade.py's discipline (bool-before-
# int, NaN/inf rejected, str never parsed, quantise once).
# ---------------------------------------------------------------------------


def _quantise(value: Decimal, where: str) -> Decimal:
    """Round *value* to øre (2 dp), ties away from zero.

    Mirrors ``ngaap.py``'s ``_quantise`` exactly (same scale, same
    ``ROUND_HALF_UP``, same ``DecimalException`` -> ``ValueError`` translation).
    Reimplemented locally rather than imported — see the module docstring.
    """
    try:
        return value.quantize(_ORE, rounding=ROUND_HALF_UP)
    except DecimalException as exc:
        raise ValueError(f"{where}: amount is too large to express in øre: {value!r}") from exc


def _as_money(value: Any, where: str) -> Decimal:
    """Coerce a documented-numeric field to an exact ``Decimal``, quantised to
    øre once. ``None`` is rejected (unlike ``ngaap.py``'s bucket inputs, every
    money field in this module is required — a missing annual amount must
    never silently become zero recognised revenue)."""
    if value is None:
        raise ValueError(f"{where}: a money amount is required, got None")
    if isinstance(value, bool):
        # isinstance(True, int) is True in Python — reject bool BEFORE int.
        raise ValueError(f"{where}: bool is not a money amount, got {value!r}")
    if isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, int):
        candidate = Decimal(value)
    elif isinstance(value, float):
        try:
            # Decimal(str(x)), never Decimal(x) — the latter imports the
            # binary-float representation error.
            candidate = Decimal(str(value))
        except DecimalException as exc:  # pragma: no cover - str(float) always parses
            raise ValueError(f"{where}: not a usable number: {value!r}") from exc
    else:
        raise ValueError(
            f"{where}: expected int/float/Decimal, got {type(value).__name__} {value!r}"
        )
    if not candidate.is_finite():  # NaN, sNaN, +-Infinity
        raise ValueError(f"{where}: must be finite, got {value!r}")
    return _quantise(candidate, where)


def _as_ns_uuid(raw: Any, field: str) -> UUID:
    if not raw:
        raise ValueError(f"'{field}' is required")
    return UUID(str(raw)) if not isinstance(raw, UUID) else raw


def _parse_period(value: Any, where: str) -> tuple[int, int]:
    """Parse a ``YYYY-MM`` string into ``(year, month)``. Refuses anything else
    (unpadded months, non-strings, out-of-range months) rather than guessing."""
    if not isinstance(value, str):
        raise ValueError(f"{where}: expected a 'YYYY-MM' string, got {type(value).__name__}")
    match = _PERIOD_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError(f"{where}: expected 'YYYY-MM' (zero-padded month), got {value!r}")
    return int(match.group(1)), int(match.group(2))


def _add_months(year: int, month: int, n: int) -> str:
    """Return ``year``-``month`` advanced by *n* months, formatted ``YYYY-MM``.

    Plain integer arithmetic on a 0-based month index — correctly rolls over
    year boundaries (e.g. ``_add_months(2026, 11, 2) == "2027-01"``)."""
    total = year * 12 + (month - 1) + n
    new_year, new_month0 = divmod(total, 12)
    return f"{new_year:04d}-{new_month0 + 1:02d}"


def _finago_ref(contract_id: str, period: str) -> str:
    """``finagoRef = ms:{contractId}:{YYYY-MM}`` — the docs' own format."""
    return f"{_FINAGO_REF_PREFIX}:{contract_id}:{period}"


# ---------------------------------------------------------------------------
# Pure: ratable 1/12 revenue recognition schedule for one contract.
# ---------------------------------------------------------------------------


def do_compute_recognition_schedule(params: dict[str, Any]) -> dict[str, Any]:
    """Compute one contract's twelve-month ratable recognition schedule.

    Parameters
    ----------
    params:
        ``{
            "contract_id":   str,               # required
            "annual_amount": int|float|Decimal, # required, must be > 0
            "start_period":  "YYYY-MM",         # required — first recognised month
        }``

    Returns
    -------
    dict
        ``{
            "contract_id": str,
            "annual_amount": Decimal,
            "start_period": "YYYY-MM",           # normalised
            "periods": [
                {"period": "YYYY-MM", "finago_ref": "ms:{id}:{period}", "amount": Decimal},
                ...  # exactly 12 entries, in chronological order
            ],
            "total_recognized": Decimal,          # == annual_amount, exactly
        }``

    The twelve ``amount`` values sum to ``annual_amount`` **exactly** — see the
    module docstring's "Rounding-remainder convention" for how and why. Re-
    checked here with a real ``raise`` (not ``assert``).

    Raises
    ------
    ValueError
        Missing/malformed ``contract_id`` or ``start_period``, a non-positive
        ``annual_amount``, (defensively) a broken sum identity, or a schedule
        whose residual period would be negative (see module docstring's
        "Edge case" note — this is reachable for real, very small amounts,
        not just a defensive/theoretical guard).
    """
    contract_id = str(params.get("contract_id") or "").strip()
    if not contract_id:
        raise ValueError("do_compute_recognition_schedule: 'contract_id' is required")

    annual_amount = _as_money(params.get("annual_amount"), "annual_amount")
    if annual_amount <= _ZERO:
        raise ValueError(
            f"do_compute_recognition_schedule: 'annual_amount' must be > 0 for contract "
            f"{contract_id!r}, got {annual_amount} — a ratable schedule with no positive "
            f"annual value is refused rather than emitting twelve zero/negative periods"
        )

    start_year, start_month = _parse_period(params.get("start_period"), "start_period")
    start_period_normalized = _add_months(start_year, start_month, 0)

    base = _quantise(annual_amount / _MONTHS_PER_YEAR, "annual_amount / 12")

    periods: list[dict[str, Any]] = []
    running_total = _ZERO
    for i in range(_MONTHS_PER_YEAR):
        period = _add_months(start_year, start_month, i)
        if i < _MONTHS_PER_YEAR - 1:
            amount = base
        else:
            # Final period absorbs the whole rounding remainder by EXACT
            # subtraction of the eleven already-quantised base amounts —
            # never rounded independently. See module docstring.
            amount = annual_amount - running_total
        running_total += amount
        periods.append(
            {
                "period": period,
                "finago_ref": _finago_ref(contract_id, period),
                "amount": amount,
            }
        )

    if running_total != annual_amount:  # pragma: no cover - defensive, construction guarantees this
        raise ValueError(
            f"do_compute_recognition_schedule: internal invariant broken for {contract_id!r} — "
            f"twelve periods sum to {running_total}, not annual_amount {annual_amount}"
        )

    # Extends the invariant above: the sum can be exact while a single period
    # is still negative money (see module docstring's "Edge case" note) — this
    # branch IS reachable for real, very small annual_amount values, unlike
    # the sum check above.
    negative_period = next((p for p in periods if p["amount"] < _ZERO), None)
    if negative_period is not None:
        raise ValueError(
            f"do_compute_recognition_schedule: refusing to recognise a negative period amount "
            f"for contract {contract_id!r} — period {negative_period['period']!r} would be "
            f"{negative_period['amount']}, computed from annual_amount {annual_amount!r}. A "
            f"ratable schedule whose residual period goes negative is refused outright, never "
            f"redistributed across periods."
        )

    return {
        "contract_id": contract_id,
        "annual_amount": annual_amount,
        "start_period": start_period_normalized,
        "periods": periods,
        "total_recognized": running_total,
    }


# ---------------------------------------------------------------------------
# Pure: MRR/ARR/churn snapshot.
# ---------------------------------------------------------------------------


def _as_snapshot_contracts(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("do_snapshot_mrr_arr_churn: 'contracts' must be a list")
    contracts: list[dict[str, Any]] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"do_snapshot_mrr_arr_churn: contracts[{index}] must be an object")
        annual_amount = _as_money(entry.get("annual_amount"), f"contracts[{index}].annual_amount")
        status = entry.get("status")
        if status not in _VALID_CONTRACT_STATUSES:
            raise ValueError(
                f"do_snapshot_mrr_arr_churn: contracts[{index}].status must be one of "
                f"{sorted(_VALID_CONTRACT_STATUSES)}, got {status!r}"
            )
        contracts.append({"annual_amount": annual_amount, "status": status})
    return contracts


def do_snapshot_mrr_arr_churn(params: dict[str, Any]) -> dict[str, Any]:
    """Compute an MRR/ARR/churn snapshot from a list of contracts.

    Parameters
    ----------
    params:
        ``{"contracts": [{"annual_amount": int|float|Decimal, "status": "active"|"churned"}, ...]}``
        ``contracts`` may be omitted/``None`` (an empty snapshot).

    Each contract's monthly contribution is ``annual_amount / 12`` quantised to
    øre — the same steady-state base :func:`do_compute_recognition_schedule`
    uses for every period but the last, so "MRR" here means the same thing a
    contract's own recognition schedule means by "one month".

    Returns
    -------
    dict
        ``{
            "mrr": Decimal,           # sum of ACTIVE contracts' monthly amount
            "arr": Decimal,           # mrr * 12 (exact — mrr is already quantised)
            "churned_mrr": Decimal,   # sum of CHURNED contracts' monthly amount
            "churn_rate": Decimal | None,  # churned_mrr / (mrr + churned_mrr), or
                                            # None when there is no prior MRR base
            "active_count": int,
            "churned_count": int,
        }``

    Raises
    ------
    ValueError
        ``contracts`` is not a list, an entry is malformed, or ``status`` is
        not exactly ``"active"`` or ``"churned"`` — an unrecognised status is
        refused rather than silently excluded from every total.
    """
    contracts = _as_snapshot_contracts(params.get("contracts"))

    mrr = _ZERO
    churned_mrr = _ZERO
    active_count = 0
    churned_count = 0
    for contract in contracts:
        monthly = _quantise(contract["annual_amount"] / _MONTHS_PER_YEAR, "annual_amount / 12")
        if contract["status"] == "active":
            mrr += monthly
            active_count += 1
        else:
            churned_mrr += monthly
            churned_count += 1

    arr = mrr * _MONTHS_PER_YEAR
    total_prior_mrr = mrr + churned_mrr
    churn_rate = (churned_mrr / total_prior_mrr) if total_prior_mrr > _ZERO else None

    return {
        "mrr": mrr,
        "arr": arr,
        "churned_mrr": churned_mrr,
        "churn_rate": churn_rate,
        "active_count": active_count,
        "churned_count": churned_count,
    }


# ---------------------------------------------------------------------------
# DB-touching: idempotent recognition, keyed on finagoRef.
# ---------------------------------------------------------------------------


def _response_digest(amount: Decimal) -> bytes:
    """Deterministic digest of a recognised amount, stored in
    ``action_idempotency.response_hash`` so a replay under the same
    ``finagoRef`` with a DIFFERENT amount can be detected and refused (see
    module docstring)."""
    return hashlib.sha256(str(amount).encode("utf-8")).digest()


async def _record_recognition(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    finago_ref: str,
    contract_id: str,
    amount: Decimal,
) -> bool:
    """Record one recognition in ``action_idempotency``. Returns ``True`` if
    this call performed the first recording for ``(namespace_id, finago_ref)``
    and ``False`` if it already existed (a genuine no-op replay).

    ``INSERT ... ON CONFLICT (namespace_id, idempotency_key) DO NOTHING
    RETURNING 1`` is one atomic statement — no read-then-write race window
    (the same idiom as ``cascade.py``'s ``_upsert_actual_cost``). On conflict,
    the stored ``response_hash`` is read back and compared: a mismatch means
    either a genuine different-amount replay of this same contract/period, or
    a foreign ``action_type`` whose idempotency_key happened to collide (see
    module docstring) — both are refused identically.
    """
    digest = _response_digest(amount)
    inserted = await conn.fetchval(
        """
        INSERT INTO action_idempotency
            (idempotency_key, namespace_id, action_type, target_entity_id, response_hash)
        VALUES ($1, $2::uuid, $3, $4, $5)
        ON CONFLICT (namespace_id, idempotency_key) DO NOTHING
        RETURNING 1
        """,
        finago_ref,
        str(ns_uuid),
        _ACTION_TYPE,
        contract_id,
        digest,
    )
    if inserted is not None:
        return True

    existing = await conn.fetchrow(
        """
        SELECT response_hash FROM action_idempotency
        WHERE namespace_id = $1::uuid AND idempotency_key = $2
        """,
        str(ns_uuid),
        finago_ref,
    )
    if existing is None:  # pragma: no cover - unreachable, ON CONFLICT guarantees a row
        raise RuntimeError(
            f"_record_recognition: ON CONFLICT fired for {finago_ref!r} but no row was "
            f"found on read-back"
        )
    stored_hash = existing["response_hash"]
    if stored_hash is None or bytes(stored_hash) != digest:
        raise ValueError(
            f"do_recognize_recurring: finagoRef {finago_ref!r} is already recorded with a "
            f"different response_hash — refusing to treat this call as a safe replay. Either "
            f"contract {contract_id!r} was already recognised for this period with a DIFFERENT "
            f"amount, or this finagoRef collided with an unrelated feature's idempotency_key "
            f"(action_idempotency's primary key is (namespace_id, idempotency_key), not scoped "
            f"by action_type — see this module's docstring)."
        )
    return False


def _as_contracts(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("do_recognize_recurring: 'contracts' must be a list")
    seen: set[str] = set()
    contracts: list[dict[str, Any]] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"do_recognize_recurring: contracts[{index}] must be an object")
        contract_id = str(entry.get("contract_id") or "").strip()
        if not contract_id:
            raise ValueError(f"do_recognize_recurring: contracts[{index}].contract_id is required")
        if contract_id in seen:
            raise ValueError(
                f"do_recognize_recurring: duplicate contract_id {contract_id!r} in 'contracts' "
                f"— ambiguous, refusing to guess which entry is authoritative"
            )
        seen.add(contract_id)
        annual_amount = _as_money(entry.get("annual_amount"), f"contracts[{index}].annual_amount")
        start_year, start_month = _parse_period(
            entry.get("start_period"), f"contracts[{index}].start_period"
        )
        status = entry.get("status")
        if status not in _VALID_CONTRACT_STATUSES:
            raise ValueError(
                f"do_recognize_recurring: contracts[{index}].status must be one of "
                f"{sorted(_VALID_CONTRACT_STATUSES)}, got {status!r}"
            )
        contracts.append(
            {
                "contract_id": contract_id,
                "annual_amount": annual_amount,
                "start_period": _add_months(start_year, start_month, 0),
                "status": status,
            }
        )
    return contracts


async def do_recognize_recurring(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Idempotent ratable-1/12 revenue recognition for one period, across a
    caller-supplied list of contracts.

    Parameters
    ----------
    engine:
        ``NCEEngine`` instance (provides ``pg_pool``).
    params:
        ``{
            "namespace_id": str | UUID,   # required
            "period": "YYYY-MM",           # required — the period being recognised now
            "contracts": [
                {
                    "contract_id":   str,               # required
                    "annual_amount": int|float|Decimal, # required, > 0
                    "start_period":  "YYYY-MM",          # required
                    "status":        "active"|"churned", # required
                },
                ...
            ],
        }``

    For each contract, the twelve-month schedule (:func:`do_compute_recognition_schedule`)
    is computed and the entry matching ``period`` is looked up. A contract whose
    schedule does not cover ``period`` (not yet started / already past its
    12-month window) is reported in ``not_due`` — skipped, not an error. A
    contract that IS due is recorded via :func:`_record_recognition`, keyed on
    its ``finagoRef``.

    Returns
    -------
    dict
        ``{
            "ok": True,
            "namespace_id": str,
            "period": "YYYY-MM",
            "recognized":         [{"contract_id","finago_ref","period","amount"}, ...],
            "already_recognized": [{"contract_id","finago_ref","period"}, ...],  # replay no-ops
            "not_due":            [contract_id, ...],
            "mrr_snapshot":       {...},  # do_snapshot_mrr_arr_churn over the same contracts
        }``

    Raises
    ------
    ValueError
        Missing/malformed params, or a finagoRef replayed with a different
        amount (or a foreign collision) — see :func:`_record_recognition`.
    """
    ns_uuid = _as_ns_uuid(params.get("namespace_id"), "namespace_id")
    period_year, period_month = _parse_period(params.get("period"), "period")
    period = _add_months(period_year, period_month, 0)
    contracts = _as_contracts(params.get("contracts"))

    recognized: list[dict[str, Any]] = []
    already_recognized: list[dict[str, Any]] = []
    not_due: list[str] = []

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
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

            is_new = await _record_recognition(
                conn, ns_uuid, entry["finago_ref"], contract["contract_id"], entry["amount"]
            )
            if is_new:
                recognized.append(
                    {
                        "contract_id": contract["contract_id"],
                        "finago_ref": entry["finago_ref"],
                        "period": period,
                        "amount": entry["amount"],
                    }
                )
            else:
                already_recognized.append(
                    {
                        "contract_id": contract["contract_id"],
                        "finago_ref": entry["finago_ref"],
                        "period": period,
                    }
                )

    mrr_snapshot = do_snapshot_mrr_arr_churn(
        {
            "contracts": [
                {"annual_amount": c["annual_amount"], "status": c["status"]} for c in contracts
            ]
        }
    )

    log.info(
        "do_recognize_recurring: ns=%s period=%s recognized=%d already_recognized=%d not_due=%d",
        ns_uuid,
        period,
        len(recognized),
        len(already_recognized),
        len(not_due),
    )

    return {
        "ok": True,
        "namespace_id": str(ns_uuid),
        "period": period,
        "recognized": recognized,
        "already_recognized": already_recognized,
        "not_due": not_due,
        "mrr_snapshot": mrr_snapshot,
    }
