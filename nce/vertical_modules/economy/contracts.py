"""
nce/vertical_modules/economy/contracts.py
============================================
Recurring-revenue contract store, CPI-cap validator, and the 90-day renewal
scan (M8.Wave 10, ``contracts-renewal``).

Per ``docs/vertical_engines/08-economy-engine.md`` (core functions
``do_validate_contract`` / ``do_scan_renewals``, Build phase B4) and
``00-ENGINES-ROADMAP.md`` §9.1/§9.2.

Retires the Wave-9 metadata shim
-----------------------------------
Wave 9 shipped with no ``economy_contracts`` table yet, so the recurring
recognition cron (``nce/cron.py``'s ``_economy_recurring_recognition_tick``)
sourced its per-namespace contracts from a temporary substitute:
``namespaces.metadata->'economy'->'recurring_contracts'``. An audit then
found this created a *latent* trap even while dormant: ``NamespaceMetadata``
(``nce/models.py``) is ``extra="forbid"`` with no ``economy`` field, and
every write path re-validates the WHOLE merged metadata dict
(``nce/orchestrators/namespace.py``'s ``_update_namespace_metadata``) — so
the instant any raw-SQL write ever put an ``economy`` key into a namespace's
metadata, every FUTURE ``manage_namespace(update_metadata)`` call for that
namespace would start failing, for entirely unrelated fields too.

This wave retires that shim in full:
  * Contract records now live in ``economy_contracts`` (this file's table,
    migration 049) — not in JSONB. :func:`fetch_contracts_for_recognition`
    is the new read path; ``nce/cron.py``'s recognition tick now calls it
    instead of reading ``metadata->'economy'->'recurring_contracts'``.
  * The per-namespace opt-in switch (``metadata.economy.enabled``) is a
    genuinely small, non-growing boolean — it stays in metadata, but is now
    a properly TYPED field (``NamespaceEconomyConfig``, ``nce/models.py``).
    That field closes the ``extra="forbid"`` trap for metadata written from
    this point forward. It ALSO strips the one specific legacy sub-key a
    namespace's already-STORED metadata may still carry from the Wave-9 era
    — ``recurring_contracts`` — via a ``model_validator(mode="before")``, so
    a namespace whose metadata predates this table can still complete a
    later, unrelated ``update_metadata`` call. This is a narrow, named
    amnesty for that one retired key, not a blanket loosening: any OTHER
    unknown key under ``economy`` still raises via ``extra="forbid"``, exactly
    as before — see ``NamespaceEconomyConfig``'s docstring (``nce/models.py``).

Module boundary (why this file does not import recurring.py, or vice versa)
------------------------------------------------------------------------------
``recurring.py``'s own docstring explains why it reimplements the money
coercion boundary locally rather than importing ``ngaap.py``'s: "this
module's dependencies point inward (``asyncpg``, ``nce.db_utils`` only)".
This file follows the identical discipline for the same reason, and adds one
more: the dependency direction between the two vertical files is
one-directional by design — ``nce/cron.py`` calls INTO this file to fetch
contract rows and then passes them, as a plain list of dicts, into
``recurring.py``'s already contract-source-agnostic
``do_recognize_recurring``. Neither vertical file imports the other; only
the shared dict shape couples them.

The CPI cap is a money ceiling (inclusive boundary, refuse — don't clamp)
------------------------------------------------------------------------
A contract's ``cpi_cap`` (default/ceiling 5%, ``Decimal("0.05")``) bounds how
much a renewal quote may increase over the current ``annual_amount``.
Structural guarantee first: ``economy_contracts.cpi_cap`` itself carries
``CHECK (cpi_cap >= 0 AND cpi_cap <= 0.05)`` (migration 049) — no row, not
even one written by a future bug or an admin's raw-SQL fix, can ever carry a
cap ABOVE 5%, independent of anything this module separately checks. On top
of that DB-level ceiling, :func:`_validate_cpi_uplift` enforces the
PER-CONTRACT cap against a caller-PROPOSED uplift:

  * The boundary is INCLUSIVE: a proposal exactly equal to the cap is
    accepted ("a 5% cap" means "up to and including 5%"); anything strictly
    greater is refused.
  * A proposal that exceeds the cap is REFUSED, never silently clamped down
    to the cap — clamping would silently transform the caller's input
    instead of refusing it, the same defect class Batch 116 shipped (this
    wave's brief calls it out by name).
  * Zero is a valid uplift (no increase this period) and is accepted.
  * A missing (``None``), negative, non-finite (NaN/Infinity), or
    non-numeric proposed figure is REFUSED outright. This validator's job is
    to validate a proposed INCREASE; a negative figure is not one, and this
    module has no documented business rule for how a negative CPI reading
    should affect a contract (floor-at-zero vs. pass-through is a policy
    decision nobody has specified) — refusing is the same "fail toward
    refusal, never toward looseness" discipline the rest of this engine
    already applies to an unrecognised contract ``status``.

``tests/test_economy_contracts.py``'s ``TestValidateCpiUplift`` pins every
one of these boundaries; removing the ``proposed > cpi_cap`` check (or
changing it to clamp instead of raise) fails that suite.

Money — Decimal end-to-end
----------------------------
Every amount is coerced via :func:`_as_money` (bool rejected before int,
NaN/inf rejected, ``str`` never parsed) and quantised to øre once via
:func:`_quantise` — the identical discipline ``ngaap.py`` / ``recurring.py``
already establish for this engine. CPI fractions go through the parallel
:func:`_as_fraction` (same finiteness/bool/str rules, but zero and small
positive values are the expected range rather than money's "> 0").

``cpi_cap`` is additionally quantised to ``economy_contracts.cpi_cap``'s own
``NUMERIC(5,4)`` column scale (4 dp, ``ROUND_HALF_UP``) — but that happens in
:func:`do_upsert_contract`, right after ``_as_fraction`` returns and before
the value is bound to the query, NOT inside :func:`_as_fraction` itself.
``_as_fraction`` is shared with ``proposed_cpi_pct``
(:func:`_validate_cpi_uplift`), a value that is never stored and must not be
scale-constrained to a DB column's precision. Without the write-path
quantisation, an unquantised ``Decimal`` (e.g. ``Decimal("0.033333")``)
meeting the ``NUMERIC(5,4)`` column lets Postgres silently round it on the
way in, with no error — the same defect class Batch 120 shipped. This module
decides the rounding, not Postgres.

A mutable master record still needs its own history
-----------------------------------------------------
``economy_contracts`` is a LIVE, mutable record (natural-keyed
``ON CONFLICT DO UPDATE`` — see :func:`do_upsert_contract`), unlike
``economy_postings``' append-only ledger lines. Money already recognised
through those postings is safe from rewrite, but the contract's own
``annual_amount`` / ``cpi_cap`` / ``status`` / ``next_renewal_date`` can be
overwritten with no trace, the instant the ``UPDATE`` commits. On the
``ON CONFLICT DO UPDATE`` branch (never on a fresh ``INSERT`` — there is no
prior value to protect), :func:`do_upsert_contract` appends one
``event_log`` entry (``event_type="config_changed"``, mirroring
``nce/orchestrators/namespace.py``'s own ``_update_namespace_metadata``
audit-every-write convention, and ``nce/autonomy/governor.py`` /
``nce/vertical_modules/procurement/po.py``'s precedent of using
``config_changed`` as "the closest generic governance event in the current
registry" rather than registering a new ``event_type``) capturing the
old -> new values for all four fields, inside the SAME transaction as the
upsert. This is a compensating record, not a WORM table: renewals and churn
updates remain ordinary mutable writes; only the history of them was
previously missing.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from typing import TYPE_CHECKING, Any
from uuid import UUID

from nce.db_utils import scoped_pg_session

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.economy.contracts")

# Agent label for this module's own event_log writes (the compensating
# config_changed audit on contract updates — see do_upsert_contract). Mirrors
# ingestion.py's own _AGENT_ID convention for this vertical.
_AGENT_ID = "economy-contracts"

# Money scale — øre, 2 dp. Mirrors ngaap.py / recurring.py's own _ORE.
_ORE: Decimal = Decimal("0.01")
_ZERO: Decimal = Decimal("0.00")

# economy_contracts.cpi_cap's own column scale — NUMERIC(5,4), 4 dp
# (migration 049). Quantised in do_upsert_contract (NOT inside _as_fraction
# — see the module docstring's "Money — Decimal end-to-end" section) so an
# unquantised Decimal never reaches Postgres to be silently rounded there.
_CPI_CAP_SCALE: Decimal = Decimal("0.0001")

# The global CPI-cap ceiling (5%) — mirrors economy_contracts.cpi_cap's own
# CHECK constraint (migration 049). No contract's cpi_cap may exceed this,
# enforced BOTH at the DB layer (structural) and here (defense in depth on
# the write path).
_CPI_CAP_CEILING: Decimal = Decimal("0.05")

# Mirrors recurring.py's _VALID_CONTRACT_STATUSES verbatim — not imported;
# see the module docstring's "Module boundary" note (each vertical file's
# dependencies point inward only; a four-line literal is not worth coupling
# the two files together).
_VALID_CONTRACT_STATUSES: frozenset[str] = frozenset({"active", "churned"})

# Mirrors recurring.py's _PERIOD_RE verbatim — see the note above.
_PERIOD_RE = re.compile(r"(\d{4})-(0[1-9]|1[0-2])")

_DEFAULT_RENEWAL_WINDOW_DAYS: int = 90


# ---------------------------------------------------------------------------
# Coercion boundary — mirrors recurring.py / ngaap.py's discipline (bool-
# before-int, NaN/inf rejected, str never parsed, quantise once).
# ---------------------------------------------------------------------------


def _quantise(value: Decimal, where: str) -> Decimal:
    """Round *value* to øre (2 dp), ties away from zero.

    Mirrors ``ngaap.py`` / ``recurring.py``'s own ``_quantise`` exactly
    (same scale, same ``ROUND_HALF_UP``, same ``DecimalException`` ->
    ``ValueError`` translation) — reimplemented locally, see the module
    docstring's "Module boundary" note.
    """
    try:
        return value.quantize(_ORE, rounding=ROUND_HALF_UP)
    except DecimalException as exc:
        raise ValueError(f"{where}: amount is too large to express in øre: {value!r}") from exc


def _quantise_cpi_cap(value: Decimal, where: str) -> Decimal:
    """Round *value* to ``economy_contracts.cpi_cap``'s own column scale
    (``NUMERIC(5,4)``, 4 dp), ties away from zero.

    Deliberately a SEPARATE function from :func:`_quantise` (øre, 2 dp) and
    NOT folded into :func:`_as_fraction` — ``_as_fraction`` is shared with
    ``proposed_cpi_pct`` (:func:`_validate_cpi_uplift`), a value that is
    never stored and must not be scale-constrained to a DB column's
    precision. Only :func:`do_upsert_contract` — the sole writer of the
    ``cpi_cap`` column — calls this, right after ``_as_fraction`` returns and
    before the value is bound to the query.

    Mirrors ``ngaap.py`` / this module's own :func:`_quantise` convention: it
    rounds; it does not raise on inexact, only on a value too large to
    express at this scale (``DecimalException`` -> ``ValueError``, one
    exception type for every unusable number).
    """
    try:
        return value.quantize(_CPI_CAP_SCALE, rounding=ROUND_HALF_UP)
    except DecimalException as exc:
        raise ValueError(f"{where}: value is too large to express to 4dp: {value!r}") from exc


def _as_money(value: Any, where: str) -> Decimal:
    """Coerce a required money field to an exact ``Decimal``, quantised to
    øre once. Mirrors recurring.py's ``_as_money`` exactly (``None``
    rejected — every money field in this module is required)."""
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


def _as_fraction(value: Any, where: str) -> Decimal:
    """Coerce a required, non-negative fraction (a CPI cap or a proposed CPI
    uplift, e.g. ``Decimal("0.05")`` == 5%) to an exact ``Decimal``.

    Shared by both ``cpi_cap`` validation (the write path, bounded against
    the global ceiling) and ``proposed_cpi_pct`` validation
    (:func:`_validate_cpi_uplift`, bounded against a specific contract's own
    cap) — the two contexts differ only in WHICH upper bound applies, never
    in how the raw value is coerced. Rejects ``None``, ``bool`` (before the
    ``int`` branch — ``isinstance(True, int)`` is ``True`` in Python),
    non-finite floats, a negative value, and any non-numeric type — a
    fraction is never parsed from a string.
    """
    if value is None:
        raise ValueError(f"{where}: a value is required, got None")
    if isinstance(value, bool):
        raise ValueError(f"{where}: bool is not a valid fraction, got {value!r}")
    if isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, int):
        candidate = Decimal(value)
    elif isinstance(value, float):
        try:
            candidate = Decimal(str(value))
        except DecimalException as exc:  # pragma: no cover - str(float) always parses
            raise ValueError(f"{where}: not a usable number: {value!r}") from exc
    else:
        raise ValueError(
            f"{where}: expected int/float/Decimal, got {type(value).__name__} {value!r}"
        )
    if not candidate.is_finite():  # NaN, sNaN, +-Infinity
        raise ValueError(f"{where}: must be finite, got {value!r}")
    if candidate < _ZERO:
        raise ValueError(
            f"{where}: must be >= 0 (a negative fraction is refused, not silently applied "
            f"or reinterpreted), got {candidate}"
        )
    return candidate


def _as_ns_uuid(raw: Any, field: str) -> UUID:
    if not raw:
        raise ValueError(f"'{field}' is required")
    return UUID(str(raw)) if not isinstance(raw, UUID) else raw


def _parse_period(value: Any, where: str) -> str:
    """Validate a ``YYYY-MM`` string, returned normalised (zero-padded).

    Mirrors recurring.py's ``_parse_period`` validation; this file has no
    need for the ``(year, month)`` tuple recurring.py's schedule computation
    wants — it only stores/forwards the string.
    """
    if not isinstance(value, str):
        raise ValueError(f"{where}: expected a 'YYYY-MM' string, got {type(value).__name__}")
    match = _PERIOD_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError(f"{where}: expected 'YYYY-MM' (zero-padded month), got {value!r}")
    return f"{match.group(1)}-{match.group(2)}"


def _as_date(value: Any, where: str) -> date:
    """Coerce a required date field (``date``, ``datetime``, or an ISO
    ``YYYY-MM-DD`` string) to a plain ``date``.

    ``datetime`` is checked before ``date`` — ``datetime`` is a subclass of
    ``date``, so the order matters (mirrors the bool-before-int gotcha this
    module already guards against elsewhere).
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValueError(f"{where}: expected an ISO 'YYYY-MM-DD' date, got {value!r}") from exc
    raise ValueError(f"{where}: expected a date/datetime/ISO string, got {type(value).__name__}")


def _as_contract_id(raw: Any, where: str) -> str:
    contract_id = str(raw or "").strip()
    if not contract_id:
        raise ValueError(f"{where}: 'contract_id' is required")
    return contract_id


# ---------------------------------------------------------------------------
# Pure: CPI-cap validator (contract-validator) + renewal-window boundary.
# ---------------------------------------------------------------------------


def _validate_cpi_uplift(cpi_cap: Decimal, proposed_cpi_pct: Any) -> Decimal:
    """Validate a PROPOSED CPI uplift fraction against a contract's own
    ``cpi_cap`` ceiling. See the module docstring's "The CPI cap is a money
    ceiling" section for the full boundary rationale (inclusive at the cap,
    refuse-never-clamp, negative/missing/non-finite all refused).

    Raises
    ------
    ValueError
        ``proposed_cpi_pct`` is missing, not a real number, negative, or
        strictly exceeds ``cpi_cap``.
    """
    proposed = _as_fraction(proposed_cpi_pct, "proposed_cpi_pct")
    if proposed > cpi_cap:
        raise ValueError(
            f"do_validate_contract: proposed CPI uplift {proposed} exceeds this contract's "
            f"cap of {cpi_cap} — refusing rather than silently clamping the uplift down to "
            f"the cap"
        )
    return proposed


def _is_due_for_renewal(next_renewal_date: date, as_of: date, window_days: int) -> bool:
    """True when *next_renewal_date* falls within *window_days* of *as_of*
    (INCLUSIVE at the boundary — exactly ``window_days`` away counts as
    due), or is already in the past (a lapsed renewal is at least as urgent
    as one due 90 days out — never excluded from the scan)."""
    return (next_renewal_date - as_of).days <= window_days


# ---------------------------------------------------------------------------
# DB-touching: contract store (write path) — sole writer of economy_contracts.
# ---------------------------------------------------------------------------


async def do_upsert_contract(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """
    Create or update one contract's ``economy_contracts`` row.

    Parameters
    ----------
    params:
        ``{
            "namespace_id":      str | UUID,                # required
            "contract_id":       str,                        # required
            "status":            "active" | "churned",       # required
            "annual_amount":     int | float | Decimal,      # required, > 0
            "start_period":      "YYYY-MM",                  # required
            "next_renewal_date": date | datetime | "YYYY-MM-DD",  # required
            "cpi_cap":           int | float | Decimal,      # optional, default 0.05
            "raw":               dict,                       # optional, default {}
        }``

    A live, mutable record — unlike ``economy_postings`` /
    ``economy_bom_actual_costs``' append-only ``ON CONFLICT DO NOTHING``,
    this upserts on the natural key ``(namespace_id, contract_id)``: a
    renewal, an amendment, or a churn transition all update the SAME row.

    On the ``ON CONFLICT DO UPDATE`` branch — i.e. every call after the
    first for a given ``(namespace_id, contract_id)`` — this ALSO appends
    one compensating ``event_log`` entry capturing the old -> new values for
    ``annual_amount``, ``cpi_cap``, ``status``, and ``next_renewal_date``,
    in the SAME transaction as the update (see the module docstring's "A
    mutable master record still needs its own history" section). A fresh
    ``INSERT`` writes no such event — there is no prior value to protect.

    Returns
    -------
    dict
        ``{"ok": True, "contract_id", "status", "annual_amount",
        "start_period", "cpi_cap", "next_renewal_date"}``.

    Raises
    ------
    ValueError
        Any required field missing/malformed, ``status`` not one of
        ``active``/``churned``, ``annual_amount`` not ``> 0``, or
        ``cpi_cap`` exceeding the global 5% ceiling (:data:`_CPI_CAP_CEILING`
        — also enforced structurally by the table's own CHECK constraint).
    """
    ns_uuid = _as_ns_uuid(params.get("namespace_id"), "namespace_id")
    contract_id = _as_contract_id(params.get("contract_id"), "contract_id")

    status = params.get("status")
    if status not in _VALID_CONTRACT_STATUSES:
        raise ValueError(
            f"do_upsert_contract: 'status' must be one of {sorted(_VALID_CONTRACT_STATUSES)}, "
            f"got {status!r}"
        )

    annual_amount = _as_money(params.get("annual_amount"), "annual_amount")
    if annual_amount <= _ZERO:
        raise ValueError(f"do_upsert_contract: 'annual_amount' must be > 0, got {annual_amount}")

    start_period = _parse_period(params.get("start_period"), "start_period")
    next_renewal_date = _as_date(params.get("next_renewal_date"), "next_renewal_date")

    raw_cpi_cap = params.get("cpi_cap", _CPI_CAP_CEILING)
    cpi_cap = _as_fraction(raw_cpi_cap, "cpi_cap")
    # Quantise to the column's own NUMERIC(5,4) scale BEFORE the ceiling
    # check and BEFORE binding — the code decides the rounding, not Postgres
    # (see the module docstring's "Money — Decimal end-to-end" section; NOT
    # folded into _as_fraction, which proposed_cpi_pct also uses and which
    # must not be scale-constrained).
    cpi_cap = _quantise_cpi_cap(cpi_cap, "cpi_cap")
    if cpi_cap > _CPI_CAP_CEILING:
        raise ValueError(
            f"do_upsert_contract: 'cpi_cap' {cpi_cap} exceeds the global ceiling of "
            f"{_CPI_CAP_CEILING} — refusing rather than silently clamping it down"
        )

    raw = params.get("raw") or {}
    if not isinstance(raw, dict):
        raise ValueError("do_upsert_contract: 'raw' must be an object")

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        # `prior` captures the row's pre-update state (if any) in the SAME
        # statement as the upsert — a non-data-modifying CTE referenced by a
        # later INSERT sees the pre-statement snapshot, so this is race-free
        # within the statement, no separate SELECT-then-INSERT round trip.
        # `xmax <> 0` is the standard Postgres idiom for "this RETURNING row
        # came from the DO UPDATE branch, not a fresh INSERT".
        row = await conn.fetchrow(
            """
            WITH prior AS (
                SELECT annual_amount, cpi_cap, status, next_renewal_date
                FROM economy_contracts
                WHERE namespace_id = $1::uuid AND contract_id = $2
            )
            INSERT INTO economy_contracts
                (namespace_id, contract_id, status, annual_amount, start_period,
                 cpi_cap, next_renewal_date, raw)
            VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8::jsonb)
            ON CONFLICT (namespace_id, contract_id) DO UPDATE SET
                status            = EXCLUDED.status,
                annual_amount     = EXCLUDED.annual_amount,
                start_period      = EXCLUDED.start_period,
                cpi_cap           = EXCLUDED.cpi_cap,
                next_renewal_date = EXCLUDED.next_renewal_date,
                raw               = EXCLUDED.raw,
                updated_at        = now()
            RETURNING contract_id, status, annual_amount, start_period, cpi_cap,
                      next_renewal_date,
                      (xmax <> 0) AS was_update,
                      (SELECT annual_amount FROM prior)     AS prior_annual_amount,
                      (SELECT cpi_cap FROM prior)           AS prior_cpi_cap,
                      (SELECT status FROM prior)            AS prior_status,
                      (SELECT next_renewal_date FROM prior) AS prior_next_renewal_date
            """,
            str(ns_uuid),
            contract_id,
            status,
            annual_amount,
            start_period,
            cpi_cap,
            next_renewal_date,
            json.dumps(raw),
        )

        assert row is not None  # RETURNING on INSERT ... DO UPDATE always yields a row

        if row["was_update"]:
            from nce.event_log import append_event

            await append_event(
                conn=conn,
                namespace_id=ns_uuid,
                agent_id=_AGENT_ID,
                event_type="config_changed",
                params={
                    "actor": _AGENT_ID,
                    "changes": {
                        "event": "economy_contract_updated",
                        "contract_id": contract_id,
                        "annual_amount": {
                            "old": str(row["prior_annual_amount"]),
                            "new": str(annual_amount),
                        },
                        "cpi_cap": {
                            "old": str(row["prior_cpi_cap"]),
                            "new": str(cpi_cap),
                        },
                        "status": {
                            "old": row["prior_status"],
                            "new": status,
                        },
                        "next_renewal_date": {
                            "old": row["prior_next_renewal_date"].isoformat(),
                            "new": next_renewal_date.isoformat(),
                        },
                    },
                },
            )

    log.info(
        "do_upsert_contract: ns=%s contract_id=%s status=%s annual_amount=%s",
        ns_uuid,
        contract_id,
        status,
        annual_amount,
    )

    return {
        "ok": True,
        "contract_id": row["contract_id"],
        "status": row["status"],
        "annual_amount": row["annual_amount"],
        "start_period": row["start_period"],
        "cpi_cap": row["cpi_cap"],
        "next_renewal_date": row["next_renewal_date"].isoformat(),
    }


async def fetch_contracts_for_recognition(
    engine: NCEEngine, namespace_id: str | UUID
) -> list[dict[str, Any]]:
    """
    Read this namespace's contracts from ``economy_contracts``, shaped for
    ``do_recognize_recurring``'s ``params["contracts"]`` (recurring.py, Wave
    9): ``{"contract_id", "annual_amount", "start_period", "status"}``.

    Both ``active`` and ``churned`` rows are returned (mirrors
    ``do_recognize_recurring``'s own accepted statuses) so a just-churned
    contract's already-scheduled remaining periods still get recognised and
    its row still feeds the MRR/ARR/churn snapshot correctly.

    Replaces the Wave-9 ``namespaces.metadata->'economy'->'recurring_contracts'``
    shim — see the module docstring's "Retires the Wave-9 metadata shim"
    section. This is the ONLY read path ``nce/cron.py``'s
    ``_economy_recurring_recognition_tick`` uses to source contracts.
    """
    ns_uuid = _as_ns_uuid(namespace_id, "namespace_id")
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        rows = await conn.fetch(
            """
            SELECT contract_id, annual_amount, start_period, status
            FROM economy_contracts
            WHERE namespace_id = $1::uuid
            """,
            str(ns_uuid),
        )
    return [
        {
            "contract_id": row["contract_id"],
            "annual_amount": row["annual_amount"],
            "start_period": row["start_period"],
            "status": row["status"],
        }
        for row in rows
    ]


async def do_validate_contract(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """
    Validate a PROPOSED CPI uplift for one contract's renewal against that
    contract's own ``cpi_cap`` (the contract-validator).

    Parameters
    ----------
    params:
        ``{
            "namespace_id":     str | UUID,             # required
            "contract_id":      str,                     # required
            "proposed_cpi_pct": int | float | Decimal,   # required, e.g. Decimal("0.05") = 5%
        }``

    Read-only — writes nothing. A caller that wants the validated uplift
    actually applied calls :func:`do_upsert_contract` separately with the
    new ``annual_amount``.

    Returns
    -------
    dict
        ``{"ok": True, "contract_id", "cpi_cap", "proposed_cpi_pct",
        "current_annual_amount", "renewal_annual_amount"}`` —
        ``renewal_annual_amount`` is ``current_annual_amount * (1 +
        proposed_cpi_pct)``, quantised to øre.

    Raises
    ------
    ValueError
        No such contract in this namespace, or the proposed uplift is
        missing/negative/non-finite/exceeds the contract's cap — see
        :func:`_validate_cpi_uplift`.
    """
    ns_uuid = _as_ns_uuid(params.get("namespace_id"), "namespace_id")
    contract_id = _as_contract_id(params.get("contract_id"), "contract_id")

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        row = await conn.fetchrow(
            """
            SELECT annual_amount, cpi_cap
            FROM economy_contracts
            WHERE namespace_id = $1::uuid AND contract_id = $2
            """,
            str(ns_uuid),
            contract_id,
        )
    if row is None:
        raise ValueError(
            f"do_validate_contract: no contract {contract_id!r} found in namespace {ns_uuid}"
        )

    cpi_cap: Decimal = row["cpi_cap"]
    current_annual_amount: Decimal = row["annual_amount"]
    validated_uplift = _validate_cpi_uplift(cpi_cap, params.get("proposed_cpi_pct"))
    renewal_annual_amount = _quantise(
        current_annual_amount * (Decimal(1) + validated_uplift), "renewal_annual_amount"
    )

    return {
        "ok": True,
        "contract_id": contract_id,
        "cpi_cap": cpi_cap,
        "proposed_cpi_pct": validated_uplift,
        "current_annual_amount": current_annual_amount,
        "renewal_annual_amount": renewal_annual_amount,
    }


async def do_scan_renewals(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """
    Return the ACTIVE contracts due for renewal within *window_days* of
    *as_of_date* — the renewal-engine's 90-day scan.

    Parameters
    ----------
    params:
        ``{
            "namespace_id": str | UUID,                    # required
            "as_of_date":   date | datetime | "YYYY-MM-DD", # optional, default today (UTC)
            "window_days":  int,                            # optional, default 90
        }``

    Read-only — writes nothing, matching Agreements' ``do_coverage_matrix``
    Watcher-role convention (observe, never mutate). Deterministic given the
    same DB state: calling it twice with nothing changed in between returns
    the identical result — the sense in which the docs call this scan
    "idempotent".

    The 90-day boundary is INCLUSIVE (a contract renewing in exactly 90 days
    is flagged) and an already-past ``next_renewal_date`` is always included
    — see :func:`_is_due_for_renewal`.

    Returns
    -------
    dict
        ``{"ok": True, "namespace_id", "as_of_date", "window_days",
        "due": [{"contract_id", "annual_amount", "cpi_cap",
        "next_renewal_date", "days_until_renewal"}, ...]}`` — ``due`` is
        sorted soonest-first.

    Raises
    ------
    ValueError
        ``window_days`` is not a positive int, or ``as_of_date`` cannot be
        parsed.
    """
    ns_uuid = _as_ns_uuid(params.get("namespace_id"), "namespace_id")
    raw_as_of = params.get("as_of_date") or datetime.now(timezone.utc).date()
    as_of = _as_date(raw_as_of, "as_of_date")

    window_days = params.get("window_days", _DEFAULT_RENEWAL_WINDOW_DAYS)
    if not isinstance(window_days, int) or isinstance(window_days, bool) or window_days <= 0:
        raise ValueError(
            f"do_scan_renewals: 'window_days' must be a positive int, got {window_days!r}"
        )

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        rows = await conn.fetch(
            """
            SELECT contract_id, annual_amount, cpi_cap, next_renewal_date
            FROM economy_contracts
            WHERE namespace_id = $1::uuid AND status = 'active'
            """,
            str(ns_uuid),
        )

    due = [
        {
            "contract_id": row["contract_id"],
            "annual_amount": row["annual_amount"],
            "cpi_cap": row["cpi_cap"],
            "next_renewal_date": row["next_renewal_date"].isoformat(),
            "days_until_renewal": (row["next_renewal_date"] - as_of).days,
        }
        for row in rows
        if _is_due_for_renewal(row["next_renewal_date"], as_of, window_days)
    ]
    due.sort(key=lambda entry: entry["days_until_renewal"])

    log.info(
        "do_scan_renewals: ns=%s as_of=%s window_days=%d due=%d",
        ns_uuid,
        as_of,
        window_days,
        len(due),
    )

    return {
        "ok": True,
        "namespace_id": str(ns_uuid),
        "as_of_date": as_of.isoformat(),
        "window_days": window_days,
        "due": due,
    }
