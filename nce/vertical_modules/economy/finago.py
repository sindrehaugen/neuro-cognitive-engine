"""
nce/vertical_modules/economy/finago.py
========================================
Finago GL reader + ``do_reconcile_gl`` (Module 8, Wave 8 -- finago-reconcile).

Per ``docs/vertical_engines/08-economy-engine.md`` build phase B3 ("Finago
**GL reader** + ``do_reconcile_gl`` + sync status/coverage report") and
``00-ENGINES-ROADMAP.md`` Section 9.2 ("Multi-master divergence & continuous
reconciliation").

The governing decision (roadmap Section 8.3, restated here because it is the
whole point of this module)
------------------------------------------------------------------------------
**NCE Economy mirrors + periodises internally; Finago stays the GL / legal
system-of-record.** Economy is NOT a GL replacement -- it computes the
correct internal numbers (match, cascade, accruals, projections, balanced
postings, persisted in ``economy_postings``) and *mirrors* the legal book.
The two books **will** diverge -- a manual Finago journal NCE never saw, or a
Finago posting for a period NCE periodised differently -- and Section 9.2
names this explicitly as a **permanent structural cost, not a transitional
bug**. This module's whole job is to make that divergence visible,
continuously, without ever pretending one side corrects the other.

Truth-rule (Section 9.2, encoded structurally, not just documented)
------------------------------------------------------------------------------
``Finago = legal system-of-record; NCE = authoritative for operational
decisions`` (cascade, margin, dunning). This is an asymmetric rule, not a
"whoever's right wins" rule, and it is encoded in the shape of the data this
module returns, not merely asserted in a comment:

* Every entry in ``do_reconcile_gl``'s ``divergences`` list carries
  ``legal_value`` (Finago's figure) and ``operational_value`` (NCE's figure)
  as two DISTINCT, always-present keys -- never a single ambiguous "value" or
  a "correct"/"wrong" pair. A caller cannot collapse the asymmetry by
  accident because there is no single field to collapse it into.
* ``do_reconcile_gl`` never writes to ``economy_postings`` (nor anywhere
  else) to "correct" NCE's operational number toward Finago's legal one --
  this module is READ + COMPARE + LOG only. A divergence is a fact about two
  books disagreeing, not a verdict about which one is wrong.
* The GL reader itself (:func:`fetch_gl_period`) never writes to Finago --
  Finago's Normal-mode GL-commit is deliberately locked by CFO policy
  (Section "Dependencies" / External blockers), and this reader has no write
  method at all, not even one that is unused.

Reuse of the C5 divergence service (the central discipline of this wave)
------------------------------------------------------------------------------
Every detected divergence is recorded via
:func:`nce.source_mode.divergence.record_divergence` -- the SAME shared
``divergence_log`` table (migration 030) that ``sales/source_mode.py``
already writes to for the D365 parity checks (``engine="sales"``; see that
module's ``log_sales_divergence``). This module is simply the second
caller, tagged ``engine="economy"`` (:data:`ENGINE_KEY`). No bespoke
reconciler, no second table, no parallel alert path: ``record_divergence``
already does append-only logging PLUS above-threshold alert dispatch via the
existing ``nce.notifications.dispatcher`` -- reusing it is what this wave
requires (its own Step 2 says so explicitly: "do NOT build a bespoke
reconciler").

Materiality: one knob, one implementation (read this before touching the
threshold)
------------------------------------------------------------------------------
``record_divergence`` already decides "alert vs log-only" by comparing the
materiality this module computes against ``NCE_DIVERGENCE_ALERT_THRESHOLD``
(default 0.1), using **strict greater-than** (``mat > threshold``): a
divergence whose materiality lands EXACTLY on the threshold is sub-threshold
-- logged, not alerted. :func:`_materiality_threshold` **is**
``nce.source_mode.divergence``'s public :func:`~nce.source_mode.divergence.alert_threshold`
accessor -- imported and assigned directly as the same function object, not
wrapped and not re-derived -- so this module's own ``"material": true/false``
classification on each divergence entry can never disagree with whether
``record_divergence`` actually paged anyone for that same row: one
materiality knob for the whole system, and now exactly one implementation of
it. The boundary is pinned explicitly:

* materiality > threshold           -> ``material: true``  (alerted)
* materiality == threshold exactly  -> ``material: false`` (logged only)
* materiality < threshold           -> ``material: false`` (logged only)
* a ZERO delta (the two books agree on an account) is never logged at all --
  there is no divergence to record, materiality is not computed, and the
  account is simply counted as "matched" in the coverage report.
* a NEGATIVE delta (NCE's figure below Finago's) is treated identically to a
  positive one -- materiality is a magnitude (``.copy_abs()``), the SIGN is
  preserved separately via the two distinct ``operational_value`` /
  ``legal_value`` fields so a caller can still tell which direction the
  books disagree in.

**Round-2 correction -- this used to be reimplemented, not imported.** An
earlier version of this module reasoned that reaching across a module
boundary for ``nce.source_mode.divergence``'s underscore-prefixed
``_alert_threshold`` was the "dependencies do not point inward cleanly"
smell uncle-bob-craft calls out, and reimplemented the identical env-var
parse locally instead. An adversarial audit found the actual smell was the
opposite one: **one rule, two implementations.** Both copies agreed on every
axis anyone thought to test -- boundary operator, env value present, env
value malformed -- except the one nobody tested: retuning
``divergence.py``'s default constant in isolation (an entirely ordinary
future edit) silently desynchronised alert-paging from this module's
materiality classification while the always-on unit suite stayed green,
because each file's own test only pinned its own hardcoded literal. The
sibling wave in this same module already established the right pattern for
exactly this situation: ``recalibration.py`` imports ``matching._MIN_GREEN``,
``_coerce_cutoff``, and ``_resolve_thresholds`` from the reader it validates
against **specifically so the writer cannot drift from the reader**. This
module now does the same thing: ``divergence.py`` exports the public
``alert_threshold()`` accessor (``_alert_threshold`` still exists and still
works, but only as a delegating alias -- the public name is the one real
implementation), and ``_materiality_threshold`` here is that same function
object. ``divergence.py`` is shared infrastructure --
``sales/source_mode.py`` is a second, independent live consumer of
``record_divergence`` -- so importing its public accessor is purely
additive: no existing caller's behaviour changes.

Refuse, not guess (invariant: a reconciliation that cannot be trusted must
say so, never silently substitute zero)
------------------------------------------------------------------------------
:func:`fetch_gl_period` validates Finago's response shape before this module
will compare a single figure against it. Missing/absent/non-list
``accounts``, a missing ``account`` or ``amount`` field on any entry, an
``amount`` that arrives as a raw JSON float (see below), or a ``period``
field that does not match the period requested, all raise
:class:`FinagoGLReadError` -- they are never treated as "must be zero" or
silently skipped. Treating a missing GL figure as 0 would either manufacture
a divergence (NCE has NOK 50 000 posted, a dropped Finago field reads as 0)
or hide a real one (both sides silently coerced to 0) -- this module never
does either.

The one deliberate exception is the whole reconciliation being **unconfigured**
(``NCE_ECONOMY_FINAGO_URL`` unset): that is not "Finago said something we
could not parse", it is "Finago integration is not wired up here yet" --
:func:`fetch_gl_period` returns ``None`` (a clean no-op, mirroring
``system_design/sharepoint.py``'s Phase-1b convention) and
:func:`do_reconcile_gl` reports ``"configured": false`` with an EMPTY
divergence list -- it never fabricates a comparison against an absent GL.

Money -- amounts arrive as strings, never as a raw JSON float
------------------------------------------------------------------------------
Every amount, on both sides of the comparison, ends up an exact ``Decimal``,
quantised once to øre (:func:`_quantise`, ``Decimal("0.01")``,
``ROUND_HALF_UP`` -- same scale and rounding as ``ngaap.py::_quantise``; it
rounds, it does not raise on an inexact value). ``bool`` is rejected before
the ``int`` branch (``isinstance(True, int)`` is ``True`` in Python) and a
raw JSON ``float`` is rejected outright rather than coerced -- a caller who
"never puts money through float" also cannot ask this reader to parse
Finago's money as one: the response must carry ``amount`` as a JSON string
(or bare int), so a binary-float artefact never enters a balanced-ledger
comparison. NaN/Infinity string values (``Decimal("NaN")`` /
``Decimal("Infinity")``) are rejected via ``Decimal.is_finite()``.

Secrets -- environment-only, no config.py entry (mirrors Batch 65's pattern)
------------------------------------------------------------------------------
``NCE_ECONOMY_FINAGO_URL`` / ``NCE_ECONOMY_FINAGO_TOKEN`` are read at call
time via :func:`nce.config.resolve_secret`, exactly the way
``system_design/sharepoint.py`` reads its SharePoint credentials -- neither
name is registered anywhere in ``nce/config.py`` (no new config key; per
this wave's scope, the *resolve_secret* accessor itself is the "config key"
seam, not a new ``_Config`` class attribute). Never logged.

WORM / RLS / namespace invariants
------------------------------------------------------------------------------
* Every SQL query is namespace-scoped EXPLICITLY (``WHERE namespace_id =
  $N``) -- never left to RLS alone (owner-pool test connections bypass
  FORCE RLS).
* No slow I/O (the Finago HTTP call) happens inside ``scoped_pg_session`` --
  the read-only Finago fetch and the read-only ``economy_postings``
  aggregate both complete OUTSIDE any write transaction; only
  ``record_divergence``'s own internal insert opens one, and it is scoped to
  exactly that one row.
* This module never touches ``event_log`` and never issues an ``UPDATE`` or
  ``DELETE`` anywhere -- it is read (Finago), read (``economy_postings``),
  and append-only write (``divergence_log``, via ``record_divergence``).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from typing import TYPE_CHECKING, Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
import httpx

from nce.config import resolve_secret
from nce.db_utils import scoped_pg_session
from nce.http_resilience import request_with_retry
from nce.source_mode.divergence import alert_threshold, record_divergence

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.economy.finago")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TIMEOUT_S = 30.0
_ORE = Decimal("0.01")
_ZERO = Decimal("0.00")
# Floor for the materiality denominator -- mirrors sales/source_mode.py's
# `log_sales_divergence` (`max(abs(n), abs(e), 1.0)`): without a floor, a
# tiny account (NCE=0.01, Finago=0.02) would score materiality 1.0 off a
# one-øre difference, drowning out genuinely material divergences.
_MATERIALITY_FLOOR = Decimal("1")

# The `engine` tag every divergence_log row this module writes carries --
# the same column `sales/source_mode.py` tags "sales" with.
ENGINE_KEY = "economy"

# Documents which side is authoritative for what -- echoed verbatim into
# every do_reconcile_gl response so a caller never has to guess the rule.
_TRUTH_RULE = "finago_legal_nce_operational"


class FinagoGLReadError(ValueError):
    """The Finago GL response was malformed, partial, or missing a field.

    Raised instead of guessing: a caller must never see a fabricated zero in
    place of a figure Finago's response failed to provide.
    """


# ---------------------------------------------------------------------------
# Secrets (env-only; no nce/config.py entry -- mirrors
# system_design/sharepoint.py's Batch 65 pattern)
# ---------------------------------------------------------------------------


def _finago_creds() -> tuple[str, str] | None:
    """Return ``(base_url, token)``, or ``None`` when Finago is not configured.

    Credentials are resolved at call time (not cached at import) so
    ``monkeypatch.setenv``/``delenv`` in tests take effect immediately.
    """
    base_url = resolve_secret("NCE_ECONOMY_FINAGO_URL")
    if not base_url:
        return None
    token = resolve_secret("NCE_ECONOMY_FINAGO_TOKEN") or ""
    return base_url, token


def _auth_headers(token: str) -> dict[str, str]:
    """Build the Finago auth header -- never log the token value."""
    return {"Authorization": f"Bearer {token}"} if token else {}


def _gl_balances_url(base_url: str, period_id: str) -> str:
    """Construct the read-only GL-balances endpoint for one accounting period."""
    return f"{base_url.rstrip('/')}/gl/periods/{period_id}/balances"


# ---------------------------------------------------------------------------
# Money coercion + quantisation -- reimplemented locally rather than imported
# from ngaap.py (see module docstring); same scale, same rounding.
# ---------------------------------------------------------------------------


def _as_money(value: Any, where: str) -> Decimal:
    """Coerce *value* to an exact ``Decimal``, refusing anything unsafe.

    Rejects (raises :class:`FinagoGLReadError`):
      - ``bool`` (checked before ``int`` -- ``isinstance(True, int)`` is
        ``True`` in Python, so a bare ``isinstance(value, int)`` check would
        silently accept a JSON ``true``/``false`` as ``1``/``0``).
      - a raw JSON ``float`` -- money must arrive as a string (or bare int)
        so a binary-float artefact never enters the comparison.
      - non-finite values (``NaN``/``Infinity``, via ``Decimal.is_finite()``).
    """
    if isinstance(value, bool):
        raise FinagoGLReadError(f"finago gl: {where} must be numeric, got bool {value!r}")
    if isinstance(value, float):
        raise FinagoGLReadError(
            f"finago gl: {where} arrived as a raw JSON float ({value!r}) -- "
            "Finago must send money as a string (or bare int) to avoid "
            "binary-float imprecision entering a balanced-ledger comparison"
        )
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, (int, str)):
        try:
            result = Decimal(str(value))
        except (DecimalException, ValueError) as exc:
            raise FinagoGLReadError(
                f"finago gl: {where} is not a valid decimal: {value!r}"
            ) from exc
    else:
        raise FinagoGLReadError(
            f"finago gl: {where} must be a string, int, or Decimal, got "
            f"{type(value).__name__} {value!r}"
        )

    if not result.is_finite():
        raise FinagoGLReadError(f"finago gl: {where} is not finite: {value!r}")
    return result


def _quantise(value: Decimal, where: str) -> Decimal:
    """Round to øre, ties away from zero -- mirrors ``ngaap.py::_quantise``.

    Catches ``DecimalException`` so a caller sees one exception type
    (:class:`FinagoGLReadError`) for every unusable amount, never a raw
    ``decimal`` exception.
    """
    try:
        return value.quantize(_ORE, rounding=ROUND_HALF_UP)
    except DecimalException as exc:
        raise FinagoGLReadError(
            f"finago gl: {where} is too large to express in øre: {value!r}"
        ) from exc


# ---------------------------------------------------------------------------
# Finago GL reader -- read-only, HTTP via request_with_retry
# ---------------------------------------------------------------------------


def _validate_gl_payload(payload: Any, period_id: str) -> dict[str, Decimal]:
    """Validate Finago's GL-balances response and return ``{account: amount}``.

    Refuses (raises :class:`FinagoGLReadError`) rather than guessing when:
      - the top-level payload is not an object;
      - ``accounts`` is absent, ``null``, or not a list (an explicit empty
        list ``[]`` is accepted -- it is a legitimate "no balances this
        period" answer, distinct from "we don't know");
      - any entry is not an object, or is missing a non-empty ``account`` or
        a present ``amount``;
      - the response's own ``period`` field (when present) disagrees with
        the period actually requested.
    """
    if not isinstance(payload, dict):
        raise FinagoGLReadError("finago gl response: top-level payload must be an object")

    response_period = payload.get("period")
    if response_period is not None and response_period != period_id:
        raise FinagoGLReadError(
            f"finago gl response: period mismatch -- requested {period_id!r}, "
            f"response says {response_period!r}"
        )

    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        raise FinagoGLReadError(
            "finago gl response: 'accounts' must be a list (missing/null is "
            "refused, never treated as an empty book)"
        )

    balances: dict[str, Decimal] = {}
    for index, entry in enumerate(accounts):
        if not isinstance(entry, dict):
            raise FinagoGLReadError(f"finago gl response: accounts[{index}] must be an object")

        account = entry.get("account")
        if not isinstance(account, str) or not account.strip():
            raise FinagoGLReadError(
                f"finago gl response: accounts[{index}].account must be a non-empty string"
            )

        if "amount" not in entry or entry["amount"] is None:
            raise FinagoGLReadError(
                f"finago gl response: accounts[{index}].amount is missing for account {account!r}"
            )

        amount = _as_money(entry["amount"], f"accounts[{index}].amount ({account!r})")
        balances[account] = _quantise(amount, f"accounts[{index}].amount ({account!r})")

    return balances


async def fetch_gl_period(period_id: str) -> dict[str, Decimal] | None:
    """Read-only Finago GL reader: fetch account balances for one period.

    Returns ``None`` -- a clean no-op, never a fabricated empty book -- when
    ``NCE_ECONOMY_FINAGO_URL`` is unset (Finago integration not configured
    here). Raises :class:`FinagoGLReadError` when Finago responds but the
    payload is malformed, partial, or disagrees with the requested period.

    Never writes to Finago -- there is no write path in this module at all;
    Normal-mode GL-commit is deliberately locked by policy (see module
    docstring), and this reader has nothing to unlock.
    """
    if not period_id or not isinstance(period_id, str):
        raise ValueError("fetch_gl_period: 'period_id' must be a non-empty string")

    creds = _finago_creds()
    if creds is None:
        log.debug("fetch_gl_period: NCE_ECONOMY_FINAGO_URL unset -- no-op (not configured)")
        return None

    base_url, token = creds
    url = _gl_balances_url(base_url, period_id)

    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        resp = await request_with_retry(
            client,
            "GET",
            url,
            operation_name="economy:finago:fetch_gl_period",
            headers=_auth_headers(token),
        )

    payload = resp.json()
    balances = _validate_gl_payload(payload, period_id)
    log.info(
        "fetch_gl_period: retrieved period_id=%s account_count=%d",
        period_id,
        len(balances),
    )
    return balances


# ---------------------------------------------------------------------------
# Internal book -- economy_postings aggregate (read-only)
# ---------------------------------------------------------------------------


async def _read_internal_balances(
    pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: UUID,
    period_id: str,
) -> dict[str, Decimal]:
    """Aggregate NCE's own book: ``SUM(amount)`` per account for one period.

    Namespace-scoped EXPLICITLY in the ``WHERE`` clause (never relies on RLS
    alone). An account absent from ``economy_postings`` for this period is a
    genuine zero -- unlike a missing Finago field, "no posting rows" really
    does mean "nothing posted here", so ``COALESCE``-free absence handling
    (the caller default-fills with :data:`_ZERO`) is correct here even
    though it would NOT be correct for the external Finago side.
    """
    async with scoped_pg_session(pool, namespace_id) as conn:
        rows = await conn.fetch(
            """
            SELECT account, SUM(amount) AS total
              FROM economy_postings
             WHERE namespace_id = $1
               AND period_id = $2
             GROUP BY account
            """,
            namespace_id,
            period_id,
        )

    return {
        row["account"]: _quantise(row["total"], f"internal balance for account {row['account']}")
        for row in rows
    }


# ---------------------------------------------------------------------------
# Materiality -- one knob shared with record_divergence's own alert gate
# ---------------------------------------------------------------------------


# `_materiality_threshold` IS `nce.source_mode.divergence.alert_threshold` --
# the same function object, not a wrapper and not a second parser of
# `NCE_DIVERGENCE_ALERT_THRESHOLD`. See the module docstring's "Materiality:
# one knob, one implementation" section (round-2 correction) for why a
# reimplementation here -- even a byte-identical one -- was the actual
# smell: it let this module's default silently drift from
# ``divergence.py``'s the moment only one of them was retuned, with the
# always-on unit suite staying green throughout. Kept under this name (module
# scope, no ``def``) so existing call sites and tests need no changes.
_materiality_threshold = alert_threshold


def _materiality(operational_value: Decimal, legal_value: Decimal) -> float:
    """Relative divergence magnitude -- mirrors sales/source_mode.py's
    ``log_sales_divergence`` numeric-field formula (``abs(n - e) /
    max(abs(n), abs(e), floor)``). A magnitude, never signed -- direction is
    preserved separately via the two distinct value fields the caller keeps.
    """
    delta = (operational_value - legal_value).copy_abs()
    denom = max(operational_value.copy_abs(), legal_value.copy_abs(), _MATERIALITY_FLOOR)
    return float(delta / denom)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_coverage() -> dict[str, Any]:
    return {
        "accounts_checked": 0,
        "accounts_matched": 0,
        "accounts_diverged": 0,
        "material_diverged": 0,
        "coverage_pct": 0.0,
    }


def _as_ns_uuid(namespace_id: Any) -> UUID:
    if not namespace_id:
        raise ValueError("'namespace_id' is required")
    return namespace_id if isinstance(namespace_id, UUID) else UUID(str(namespace_id))


# ---------------------------------------------------------------------------
# Public: do_reconcile_gl
# ---------------------------------------------------------------------------


async def do_reconcile_gl(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Reconcile NCE's internal book against Finago's GL for one period.

    Continuous reconciliation, not a one-off: every divergence found is
    recorded via the shared C5 divergence service
    (:func:`nce.source_mode.divergence.record_divergence`, ``engine="economy"``)
    -- calling this repeatedly (e.g. from a cron tick, out of this wave's
    scope) is exactly how the "continuous" half of Section 9.2's discipline
    is meant to work; this function itself is stateless between calls.

    Parameters
    ----------
    engine:
        ``NCEEngine`` instance (provides ``pg_pool``).
    params:
        ``{"namespace_id": str | UUID, "period_id": str}`` -- both required.

    Returns
    -------
    dict
        ``configured`` (``False`` when Finago credentials are unset -- an
        empty, honest no-op, never a fabricated comparison);
        ``truth_rule`` (echoes :data:`_TRUTH_RULE` so a caller never has to
        guess which side wins for what); ``divergences`` (list of
        ``{"account", "period_id", "operational_value", "legal_value",
        "materiality", "material"}``); ``coverage`` (accounts checked /
        matched / diverged / material_diverged / coverage_pct).

    Never writes to ``economy_postings`` or to Finago -- read, compare, log.
    """
    ns_uuid = _as_ns_uuid(params.get("namespace_id"))

    period_id = params.get("period_id")
    if not isinstance(period_id, str) or not period_id.strip():
        raise ValueError("do_reconcile_gl: 'period_id' is required")

    finago_balances = await fetch_gl_period(period_id)
    if finago_balances is None:
        return {
            "namespace_id": str(ns_uuid),
            "period_id": period_id,
            "configured": False,
            "truth_rule": _TRUTH_RULE,
            "divergences": [],
            "coverage": _empty_coverage(),
            "reconciled_at": _utc_now_iso(),
        }

    internal_balances = await _read_internal_balances(engine.pg_pool, ns_uuid, period_id)

    accounts = sorted(set(finago_balances) | set(internal_balances))
    threshold = _materiality_threshold()

    divergences: list[dict[str, Any]] = []
    matched = 0
    material_count = 0

    for account in accounts:
        operational_value = internal_balances.get(account, _ZERO)  # NCE
        legal_value = finago_balances.get(account, _ZERO)  # Finago

        if operational_value == legal_value:
            matched += 1
            continue

        materiality = _materiality(operational_value, legal_value)
        is_material = materiality > threshold  # strict: AT threshold is NOT material

        await record_divergence(
            engine.pg_pool,
            namespace_id=ns_uuid,
            engine=ENGINE_KEY,
            entity=f"gl_account:{period_id}:{account}",
            field="balance",
            nce_value=str(operational_value),
            ext_value=str(legal_value),
            materiality=materiality,
        )

        if is_material:
            material_count += 1

        divergences.append(
            {
                "account": account,
                "period_id": period_id,
                "operational_value": str(operational_value),
                "legal_value": str(legal_value),
                "materiality": materiality,
                "material": is_material,
            }
        )

    total = len(accounts)
    coverage = {
        "accounts_checked": total,
        "accounts_matched": matched,
        "accounts_diverged": len(divergences),
        "material_diverged": material_count,
        # Vacuously 100% when neither book has a single row for this period --
        # zero accounts examined, zero disagreements found.
        "coverage_pct": round((matched / total) * 100.0, 2) if total else 100.0,
    }

    return {
        "namespace_id": str(ns_uuid),
        "period_id": period_id,
        "configured": True,
        "truth_rule": _TRUTH_RULE,
        "divergences": divergences,
        "coverage": coverage,
        "reconciled_at": _utc_now_iso(),
    }


# ---------------------------------------------------------------------------
# Public: do_gl_sync_status -- continuous-reconciliation health, sourced
# entirely from the shared divergence_log (no engine-owned sync-run table;
# this wave adds none).
# ---------------------------------------------------------------------------


async def do_gl_sync_status(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Report Economy's recent GL-reconciliation activity for one namespace.

    Answers "when did Economy last find a Finago divergence, and how dirty
    is the log right now" purely by querying the existing
    ``divergence_log`` table (``engine="economy"``) -- no bespoke
    ``economy_sync_runs`` history table, which this wave may not add.

    Parameters
    ----------
    engine:
        ``NCEEngine`` instance (provides ``pg_pool``).
    params:
        ``{"namespace_id": str | UUID, "window_hours": float = 24.0}``.

    Returns
    -------
    dict
        ``divergence_count`` / ``material_divergence_count`` (within the
        window), ``last_divergence_at`` (ISO-8601 or ``None``), ``clean``
        (``True`` when the window has zero divergence rows).
    """
    ns_uuid = _as_ns_uuid(params.get("namespace_id"))

    window_hours_raw = params.get("window_hours", 24.0)
    try:
        window_hours = float(window_hours_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("do_gl_sync_status: 'window_hours' must be numeric") from exc
    if window_hours <= 0:
        raise ValueError("do_gl_sync_status: 'window_hours' must be > 0")

    threshold = _materiality_threshold()

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        row = await conn.fetchrow(
            """
            SELECT COUNT(*)::int AS total,
                   COUNT(*) FILTER (WHERE materiality > $3::numeric)::int AS material,
                   MAX(detected_at) AS last_detected_at
              FROM divergence_log
             WHERE namespace_id = $1
               AND engine = $2
               AND detected_at >= now() - ($4 * INTERVAL '1 hour')
            """,
            ns_uuid,
            ENGINE_KEY,
            threshold,
            window_hours,
        )

    total = int(row["total"]) if row else 0
    material = int(row["material"]) if row else 0
    last_detected_at = row["last_detected_at"] if row else None

    return {
        "namespace_id": str(ns_uuid),
        "engine": ENGINE_KEY,
        "window_hours": window_hours,
        "divergence_count": total,
        "material_divergence_count": material,
        "last_divergence_at": last_detected_at.isoformat() if last_detected_at else None,
        "clean": total == 0,
    }
