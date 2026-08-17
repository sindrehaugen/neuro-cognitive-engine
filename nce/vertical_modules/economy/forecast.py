"""
nce/vertical_modules/economy/forecast.py
=========================================
Monte Carlo cashflow forecast — pure Advisor core. Zero DB, zero HTTP, zero web/admin imports.

Per ``docs/vertical_engines/08-economy-engine.md`` (core function ``do_forecast_cashflow``,
build phase B5 — "Monte Carlo cashflow; ... the real probabilistic sim, not a Claude stream")
and ``00-ENGINES-ROADMAP.md`` §9.1/§9.2 (Watcher AI-role: cashflow-risk alerts from the tail of
this simulation). Batch 127 / Module 8.Wave 12.

What this computes
-------------------
Given a caller-supplied list of forecast **periods** (each carrying an ``expected_net``
cashflow and a relative ``uncertainty_pct``), this module runs ``iterations`` independent
Monte Carlo passes. In each pass, every period's net cashflow is perturbed as
``expected_net * (1 + Z * uncertainty_pct)`` where ``Z`` is a standard-normal draw, and a
running cash balance is carried forward period-to-period starting from ``opening_balance``.
Across all iterations, the per-period simulated net and the per-period running balance are
each summarised as P10 / P50 / P90 (10th/50th/90th percentile, nearest-rank).

A ``uncertainty_pct`` of 0 makes a period's outcome identical to its ``expected_net`` in every
iteration — there is no scale to perturb. That is deliberate, not a bug: a period the caller
has full confidence in should forecast as a single point, not a fan of manufactured noise.

**Advisor discipline:** this function computes and returns; it never writes to the DB, never
calls out over HTTP, and never mutates its inputs.

Determinism is a security property here, not a convenience (money-module briefing #6)
--------------------------------------------------------------------------------------
**The caller passes the seed. This module never generates one.** A ``random.Random(seed)``
instance is created fresh, right here, in :func:`do_forecast_cashflow`, and every draw in the
simulation comes from THAT instance's ``.gauss`` method — never from the module-level
``random.seed()`` / ``random.random()`` / ``random.gauss()`` free functions, which read and
mutate **global**, process-wide RNG state. Two consequences, both load-bearing:

1. The same ``(seed, params)`` pair reproduces byte-identical output every time, in any
   process, regardless of what else that process has done with the global ``random`` module
   before or after this call — because this module never touches it, in either direction.
2. A concurrent caller in the same process — another vertical module, another in-flight
   forecast, a test running in parallel — can call ``random.random()`` or reseed the global
   RNG all it wants without perturbing THIS call's sequence, and this call can never perturb
   theirs. Two other agents are editing this same tree concurrently (see the wave's Files:
   line); this property is what keeps concurrently-running test sessions independent even if
   pytest happens to interleave them in one process.

``seed=None`` is refused, not defaulted to "generate one": ``random.Random(None)`` seeds
itself from OS entropy (``os.urandom`` / the clock), which is the *opposite* of deterministic
and would silently convert "reproduce this forecast" into "produce a fresh one every call" —
the exact failure this function exists to prevent. ``bool`` is refused too
(``isinstance(True, int)`` is ``True`` in Python; a stray ``True``/``False`` seed is almost
always a caller bug, not an intentional seed of 1/0).

The float -> Decimal boundary (money-module briefing #2)
----------------------------------------------------------
``random.Random.gauss`` can only ever produce a ``float`` — there is no way around that; it is
where the randomness enters. This module draws that ``float`` exactly ONCE per
(period, iteration), in :func:`_run_iteration`, and immediately converts it via
``Decimal(str(z))`` — the shortest round-tripping decimal text, i.e. the number the draw
actually represents — never ``Decimal(z)``, which would import the binary float's exact, ugly
expansion (``Decimal(0.1)`` is ``0.1000000000000000055511151231257827…``). From that point on,
every value in the computation — the noise term, the multiplier, the simulated net, the
running balance — is an exact ``Decimal``, quantised to øre (2 dp, ``ROUND_HALF_UP``) exactly
ONCE, at the point the simulated net is produced (:func:`_run_iteration`), mirroring
``ngaap.py``'s "quantise once, then only exact subtraction/addition of already-quantised
amounts" discipline. The running balance is built by summing already-quantised ``Decimal``
nets onto an already-quantised opening balance, so it never needs re-quantising. This holds
without drift for any realistic cashflow magnitude: periods are capped at ``_MAX_PERIODS``
(see Tunables) and each period's own amount is itself bounded by what the øre-quantisation
step can express, so the running sum's total significant-digit count stays comfortably
inside the default ``Decimal`` context's 28-digit precision. That is a bound which holds in
practice, not an unconditional guarantee for arbitrary per-period magnitudes: a bare
``Decimal.__add__`` under the default context (``prec=28``, ``Inexact`` not trapped) silently
rounds, rather than raising, once a running sum's digit count exceeds 28 — reachable only by
deliberately pushing per-period amounts to that quantisation ceiling itself, far outside any
real cashflow amount.

Percentiles are computed on the sorted ``Decimal`` outcomes using pure integer nearest-rank
arithmetic (see :func:`_percentile`) — no float division anywhere near a money value.

Coercion boundary (money-module briefing #1, #3, #4)
--------------------------------------------------------
Every input is validated once, at the boundary (:func:`_normalised_params`,
:func:`_coerce_periods`), and only the validated copy is read downstream — the caller's raw
dict is never re-consulted after validation (Batch 116's most-repeated defect was normalising
for validation and then reading the raw value). ``bool`` is rejected wherever a number is
expected (``isinstance(True, int)`` is ``True``); NaN/inf are rejected everywhere a NaN
comparison would otherwise silently read as ``False``. An unknown top-level or per-period key
raises rather than being silently ignored — a typo'd ``"expeced_net"`` would otherwise
periodise that period as entirely absent, and this function would hand back a confident-
looking forecast built on a hole, with no error to say so.

Public API
----------
``do_forecast_cashflow(seed, params) -> dict`` — see its docstring for the parameter/return
shape.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from typing import Any

# ---------------------------------------------------------------------------
# Tunables — code constants, not config-as-IP. This wave adds no new JSON config file (B127
# orchestrator ruling): these are simulation mechanics, not tenant-swappable business IP.
# ---------------------------------------------------------------------------

_DEFAULT_ITERATIONS = 1000
_MIN_ITERATIONS = 1
# A sanity ceiling against a runaway/typo'd iteration count (e.g. a caller passing a value
# meant as a percentage or a year), not a business rule. Comfortably above any real use of
# this Advisor core, which is called synchronously and must return promptly.
_MAX_ITERATIONS = 100_000

# A sanity ceiling against a runaway/typo'd period count (e.g. a caller programmatically
# generating periods off a wrong unit — days instead of months — or a bug that expands a
# single horizon into one entry per day for decades), not a business rule. 1 000 periods is
# comfortably above any real cashflow horizon this Advisor core is asked to forecast (83+
# years of monthly periods, or ~19 years of weekly periods), while still bounding the
# O(periods * iterations) synchronous cost of the simulation below — same reasoning as
# _MAX_ITERATIONS above: this function is called synchronously and must return promptly.
_MAX_PERIODS = 1_000

# uncertainty_pct is a RATIO (relative std-dev), not a percent — 0.10 means "10% of
# expected_net", not "10". Bounded the same way ngaap.py bounds delivery_pct: a sign floor
# (negative uncertainty is meaningless — a std-dev cannot be negative) and a sanity ceiling
# against percent/ratio confusion (a caller passing "50" meaning "50%" must not silently
# become 5000% relative noise).
_MIN_UNCERTAINTY = Decimal(0)
_MAX_UNCERTAINTY = Decimal(5)

_ORE = Decimal("0.01")
_ZERO = Decimal("0.00")

_PERCENTILES: tuple[int, ...] = (10, 50, 90)

_VALID_PARAMS_KEYS = frozenset({"periods", "iterations", "opening_balance", "_comment"})
_VALID_PERIOD_KEYS = frozenset({"period", "expected_net", "uncertainty_pct", "_comment"})


@dataclass(frozen=True)
class _PeriodInput:
    """One validated, coerced forecast period. ``label`` is opaque and echoed unchanged."""

    label: Any
    expected_net: Decimal
    uncertainty_pct: Decimal


# ---------------------------------------------------------------------------
# Coercion boundary — fail loud, never silently permissive
# ---------------------------------------------------------------------------


def _as_seed(value: Any) -> int:
    """Coerce the caller-supplied seed to a real ``int``, or raise.

    ``None`` is refused: ``random.Random(None)`` seeds from OS entropy, silently trading
    determinism for freshness — the opposite of this module's whole contract (see module
    docstring). ``bool`` is refused because ``isinstance(True, int)`` is ``True`` in Python.
    Anything else that is not already an ``int`` (a ``float``, a numeric ``str``) is refused
    rather than coerced: a caller who genuinely has a seed has an ``int``, and guessing at one
    is how ``seed=3`` and ``seed=3.0`` quietly become two different simulations that nobody can
    tell apart from the report alone.
    """
    if value is None:
        raise ValueError(
            "forecast: seed is required and must not be None — random.Random(None) seeds "
            "from OS entropy, which is non-deterministic"
        )
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"forecast: seed must be a real int, got {type(value).__name__} {value!r}")
    return value


def _as_money(value: Any, where: str) -> Decimal:
    """Coerce a documented-numeric money field to an exact, øre-quantised ``Decimal``.

    Same discipline as ``ngaap.py``/``events.py``: ``bool`` rejected first
    (``isinstance(True, int)`` is ``True`` in Python); ``str`` never parsed; NaN/inf rejected;
    ``float`` goes through ``Decimal(str(value))``, never ``Decimal(value)`` (the latter would
    import the binary-float representation error into an exact type).
    """
    if isinstance(value, bool):
        raise ValueError(f"forecast: {where} must be a number, got bool {value!r}")
    if isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, int):
        candidate = Decimal(value)
    elif isinstance(value, float):
        try:
            candidate = Decimal(str(value))
        except DecimalException as exc:  # pragma: no cover - str(float) always parses
            raise ValueError(f"forecast: {where} is not a usable number: {value!r}") from exc
    else:
        raise ValueError(
            f"forecast: {where} must be int/float/Decimal, got {type(value).__name__} {value!r}"
        )
    if not candidate.is_finite():
        raise ValueError(f"forecast: {where} must be finite, got {value!r}")
    return _quantise(candidate, where)


def _as_uncertainty(value: Any, where: str) -> Decimal:
    """Coerce ``uncertainty_pct`` to a ratio ``Decimal`` in ``[0, 5]``. Absent/``None`` -> 0
    (no perturbation — see module docstring)."""
    if value is None:
        return _MIN_UNCERTAINTY
    if isinstance(value, bool):
        raise ValueError(f"forecast: {where} must be a number, got bool {value!r}")
    if isinstance(value, Decimal):
        ratio = value
    elif isinstance(value, int):
        ratio = Decimal(value)
    elif isinstance(value, float):
        try:
            ratio = Decimal(str(value))
        except DecimalException as exc:  # pragma: no cover - str(float) always parses
            raise ValueError(f"forecast: {where} is not a usable number: {value!r}") from exc
    else:
        raise ValueError(
            f"forecast: {where} must be int/float/Decimal, got {type(value).__name__} {value!r}"
        )
    if not ratio.is_finite():
        raise ValueError(f"forecast: {where} must be finite, got {value!r}")
    if ratio < _MIN_UNCERTAINTY:
        raise ValueError(f"forecast: {where} must not be negative, got {ratio}")
    if ratio > _MAX_UNCERTAINTY:
        raise ValueError(
            f"forecast: {where} must be a RATIO in [0, {_MAX_UNCERTAINTY}], got {ratio} — "
            f"values above 1 already mean more than 100% relative noise; pass 0.2, not 20"
        )
    return ratio


def _as_iterations(value: Any) -> int:
    """Coerce ``iterations``. Absent/``None`` -> the documented default."""
    if value is None:
        return _DEFAULT_ITERATIONS
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"forecast: iterations must be a real int, got {type(value).__name__} {value!r}"
        )
    if value < _MIN_ITERATIONS or value > _MAX_ITERATIONS:
        raise ValueError(
            f"forecast: iterations must be in [{_MIN_ITERATIONS}, {_MAX_ITERATIONS}], got {value}"
        )
    return value


def _quantise(value: Decimal, where: str) -> Decimal:
    """Round to øre, ties away from zero — the Norwegian accounting convention (see
    ``ngaap.py``). A real exception, never a silently truncated/rounded surprise."""
    try:
        return value.quantize(_ORE, rounding=ROUND_HALF_UP)
    except DecimalException as exc:
        raise ValueError(f"forecast: {where} is too large to express in øre: {value!r}") from exc


# ---------------------------------------------------------------------------
# Input normalisation
# ---------------------------------------------------------------------------


def _normalised_params(params: Any) -> dict[str, Any]:
    """Reject an unknown top-level key rather than silently ignoring it (a typo'd
    ``"periods"`` would otherwise run a zero-period forecast that looks successful)."""
    if not isinstance(params, dict):
        raise ValueError(f"forecast: params must be an object, got {type(params).__name__}")
    unknown = set(params) - _VALID_PARAMS_KEYS
    if unknown:
        raise ValueError(
            f"forecast: params has unknown key(s) {sorted(unknown)} — expected a subset of "
            f"{sorted(_VALID_PARAMS_KEYS)}"
        )
    return params


def _coerce_periods(raw_periods: Any) -> list[_PeriodInput]:
    """Validate and coerce ``params['periods']``. Must be a non-empty list of objects; an
    unknown key inside a period entry raises (same typo-protection rule as the top level)."""
    if not isinstance(raw_periods, list) or not raw_periods:
        raise ValueError(
            f"forecast: params['periods'] must be a non-empty list, got "
            f"{type(raw_periods).__name__}"
        )
    if len(raw_periods) > _MAX_PERIODS:
        raise ValueError(
            f"forecast: params['periods'] must have at most {_MAX_PERIODS} entries, got "
            f"{len(raw_periods)} — see _MAX_PERIODS for why this is bounded"
        )
    periods: list[_PeriodInput] = []
    for index, raw in enumerate(raw_periods):
        if not isinstance(raw, dict):
            raise ValueError(
                f"forecast: periods[{index}] must be an object, got {type(raw).__name__}"
            )
        unknown = set(raw) - _VALID_PERIOD_KEYS
        if unknown:
            raise ValueError(
                f"forecast: periods[{index}] has unknown key(s) {sorted(unknown)} — expected "
                f"a subset of {sorted(_VALID_PERIOD_KEYS)}"
            )
        if "expected_net" not in raw:
            raise ValueError(f"forecast: periods[{index}] is missing 'expected_net'")
        periods.append(
            _PeriodInput(
                label=raw.get("period"),
                expected_net=_as_money(raw["expected_net"], f"periods[{index}].expected_net"),
                uncertainty_pct=_as_uncertainty(
                    raw.get("uncertainty_pct"), f"periods[{index}].uncertainty_pct"
                ),
            )
        )
    return periods


# ---------------------------------------------------------------------------
# Percentiles — pure integer nearest-rank, never a float near money
# ---------------------------------------------------------------------------


def _percentile(sorted_values: list[Decimal], pct: int) -> Decimal:
    """Nearest-rank percentile of an ascending-sorted list.

    ``rank = ceil(pct * n / 100)``, clamped to ``[1, n]`` and converted to a 0-based index —
    pure integer arithmetic (``pct`` and ``n`` are both ``int``), so this never introduces a
    float anywhere near a money value. Ascending sort order means the *Xth* percentile is,
    correctly, the value at or below which X% of the simulated outcomes fall — so P10 reads as
    the pessimistic (low) tail and P90 as the optimistic (high) tail for a value where more is
    better, exactly as a cashflow forecast wants.
    """
    count = len(sorted_values)
    rank = -(-(pct * count) // 100)  # ceiling division, integers only
    rank = max(1, min(rank, count))
    return sorted_values[rank - 1]


# ---------------------------------------------------------------------------
# The simulation
# ---------------------------------------------------------------------------


def _run_iteration(
    rng: random.Random, opening_balance: Decimal, periods: list[_PeriodInput]
) -> tuple[list[Decimal], list[Decimal]]:
    """One Monte Carlo pass over every period, in order. Returns ``(nets, balances)``, one
    entry per period, same order as *periods*.

    ``rng.gauss`` is the ONLY float produced in this function; everything downstream of it —
    the noise term, the multiplier, the simulated net, the running balance — is an exact
    ``Decimal`` (see the module docstring's float -> Decimal boundary section). ``rng`` must be
    a caller-owned ``random.Random`` instance, never the ``random`` module itself, so this
    function cannot read or mutate global RNG state.
    """
    nets: list[Decimal] = []
    balances: list[Decimal] = []
    balance = opening_balance
    for period in periods:
        z = rng.gauss(0.0, 1.0)
        # THE float -> Decimal boundary: Decimal(str(z)), never Decimal(z) (see module
        # docstring). Everything from here on is exact.
        noise = Decimal(str(z)) * period.uncertainty_pct
        simulated_net = _quantise(period.expected_net * (Decimal(1) + noise), "simulated_net")
        balance = balance + simulated_net
        nets.append(simulated_net)
        balances.append(balance)
    return nets, balances


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def do_forecast_cashflow(seed: int, params: dict[str, Any]) -> dict[str, Any]:
    """Run a Monte Carlo cashflow forecast. Pure Advisor core: computes and returns, never
    writes to the DB, never calls out over HTTP.

    Parameters
    ----------
    seed:
        The caller's deterministic seed. **Required, must be a real int.** This module never
        generates a seed and never reads/mutates the global ``random`` module's state — see
        the module docstring for why that is a security property here, not a convenience.
    params:
        dict with keys:
            ``periods``          list[dict], required, non-empty, at most ``_MAX_PERIODS``
                                  (1000) entries, in forecast order. Each entry:
                ``period``            any, optional — an opaque label, echoed unchanged.
                ``expected_net``      int/float/Decimal, required — the period's expected net
                                      cashflow (may be negative). Quantised to øre.
                ``uncertainty_pct``   int/float/Decimal, optional (default 0) — a RATIO (not a
                                      percent) in ``[0, 5]``: the relative standard deviation
                                      of ``expected_net``. 0 means "certain" — the period
                                      forecasts as a single point in every iteration.
            ``iterations``        int, optional (default 1000) — Monte Carlo pass count, in
                                   ``[1, 100000]``.
            ``opening_balance``   int/float/Decimal, optional (default 0) — starting cash
                                   balance before the first period.
        Any other top-level or per-period key raises (typo protection — see module docstring).

    Returns
    -------
    dict with keys:
        ``seed``              the coerced ``int`` seed, echoed.
        ``iterations``        the resolved iteration count.
        ``opening_balance``   the quantised ``Decimal`` opening balance.
        ``periods``           list of dicts, one per input period, in the SAME order as
                               supplied, each with:
                                   ``period``          the echoed label.
                                   ``expected_net``     the quantised ``Decimal`` input,
                                                        echoed.
                                   ``net_p10`` / ``net_p50`` / ``net_p90``
                                       ``Decimal`` — 10th/50th/90th percentile of that period's
                                       simulated net cashflow across all iterations.
                                   ``balance_p10`` / ``balance_p50`` / ``balance_p90``
                                       ``Decimal`` — the same percentiles of the RUNNING cash
                                       balance after this period (``opening_balance`` plus
                                       every simulated net up to and including this period).

    **Every amount in the result is a ``Decimal``.** Do not ``float()`` them; serialise with
    ``str()`` at the transport boundary (same rule as ``ngaap.py``/``events.py``).

    Determinism
    -----------
    Calling this function twice with the same ``seed`` and the same ``params`` returns an
    identical result, element for element — regardless of any intervening use of the global
    ``random`` module by this process, and regardless of what any concurrently-running caller
    does with the global ``random`` module at the same time. See the module docstring.

    Raises
    ------
    ValueError
        On any unusable seed, iteration count, or period input — see the coercion helpers'
        docstrings. Always fails toward refusal, never toward a guessed number (money-module
        briefing #4).
    """
    seed_int = _as_seed(seed)
    resolved_params = _normalised_params(params)
    periods = _coerce_periods(resolved_params.get("periods"))
    iterations = _as_iterations(resolved_params.get("iterations"))

    raw_opening = resolved_params.get("opening_balance")
    opening_balance = _ZERO if raw_opening is None else _as_money(raw_opening, "opening_balance")

    rng = random.Random(seed_int)

    per_period_nets: list[list[Decimal]] = [[] for _ in periods]
    per_period_balances: list[list[Decimal]] = [[] for _ in periods]
    for _pass in range(iterations):
        nets, balances = _run_iteration(rng, opening_balance, periods)
        for index in range(len(periods)):
            per_period_nets[index].append(nets[index])
            per_period_balances[index].append(balances[index])

    result_periods: list[dict[str, Any]] = []
    for index, period in enumerate(periods):
        sorted_nets = sorted(per_period_nets[index])
        sorted_balances = sorted(per_period_balances[index])
        entry: dict[str, Any] = {
            "period": period.label,
            "expected_net": period.expected_net,
        }
        for pct in _PERCENTILES:
            entry[f"net_p{pct}"] = _percentile(sorted_nets, pct)
            entry[f"balance_p{pct}"] = _percentile(sorted_balances, pct)
        result_periods.append(entry)

    return {
        "seed": seed_int,
        "iterations": iterations,
        "opening_balance": opening_balance,
        "periods": result_periods,
    }
