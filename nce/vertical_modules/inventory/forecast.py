"""
nce/vertical_modules/inventory/forecast.py
==================================================
Pipeline-aware demand forecast Advisor (Module 11, Wave 7 -- ``demand-forecast``).

This module recommends; it does not spend -- same discipline as
``replenishment.py``'s ``do_recommend_restock`` (Wave 6). :func:`do_forecast_demand`
is a **return-only advisor**. It performs **no writes of any kind**: no
``kg_nodes``/``kg_edges`` row, no ``inventory_items`` update, no PO. It only
``SELECT``s (inside a read-only ``scoped_pg_session`` transaction, used purely
to establish the RLS namespace context) and returns a plain ``dict``.

The pipeline signal, and how it is represented
------------------------------------------------------------------------------
Sales(#5)/Project(#7)/System Design(#6) do not expose a dedicated
"pipeline demand" table with a SKU/quantity column (verified against this
wave's ``Files:`` -- none of those engines' schemas are touched here). This
module therefore reads the ONE cross-engine channel that already exists for
exactly this purpose: the Knowledge Graph (``kg_nodes``/``kg_edges``) --
the same channel ``sales/commission.py``'s ``do_record_deal_loss_feedback``
already writes ``PRODUCT:{sku}``-labelled facts into.

A pipeline line is represented as:

* a ``kg_nodes`` row -- ``label`` is the pipeline item's own id (e.g.
  ``"QUOTE:Q-100"``), ``entity_type`` is the pipeline **stage** key (e.g.
  ``"quote_draft"``, ``"quote_sent"``, ``"design_approved"``,
  ``"project_signed"``) -- the same key
  ``inventory-forecast-weights.json``'s ``stage_weights`` maps FROM;
* a ``kg_edges`` row -- ``subject_label`` is that same pipeline-item label,
  ``predicate`` is :data:`_PIPELINE_DEMAND_PREDICATE` (``"implies_demand"``),
  ``object_label`` is ``f"PRODUCT:{sku}"`` (``commission.py``'s own
  convention), ``confidence`` is a float in ``0..1`` -- the ONLY quantitative
  field ``kg_edges`` carries (this wave's WORM invariant: ``confidence``
  lives on ``kg_edges`` **only**, never ``kg_nodes``).

Writing those rows is Sales/Project/System-Design's job, not this wave's --
this module only ever reads, filtered by an explicit ``namespace_id`` bound
on every query (never RLS alone -- the B67/B120/B130 lesson
``replenishment.py``'s own docstring documents at length).

The forecast score, per SKU
------------------------------------------------------------------------------
``forecasted_demand_score`` is the sum, over every matching pipeline line, of
``stage_weights[entity_type] * confidence`` -- a probability-weighted SCORE,
not a physical unit count (see "What this wave does NOT do" below). A
pipeline line whose stage is not a key in ``stage_weights`` is reported in
``pipeline_lines`` with ``stage_weight: null, counted: false`` and excluded
from the sum -- the same "config is IP, never guess a policy" fallback
``replenishment.py``'s ``reorder_points`` uses for an unconfigured SKU.

Only pipeline lines **within the requested horizon** contribute: a line's
expected close date is its ``kg_edges.created_at`` plus
``stage_days_to_close[entity_type]``, and it counts only when that date falls
at or before ``now() + horizon_days`` (see the ``horizon_days`` section
below).

What this wave does NOT do (named, not silent)
------------------------------------------------------------------------------
* **No physical quantity per pipeline line.** ``kg_edges`` carries
  ``confidence`` (0..1) only, never a quantity column; the forecast is a
  weighted SCORE, not a literal "N units" figure. A future wave that adds a
  quantity to the pipeline representation is out of this wave's scope.
* **No write path.** Sales/Project/System Design own writing pipeline rows;
  this module never issues ``INSERT``/``UPDATE``/``DELETE`` against
  ``kg_nodes``/``kg_edges``.

How ``horizon_days`` filters (Wave 7b -- ``forecast-horizon-filter``)
------------------------------------------------------------------------------
``kg_edges`` still carries no expected-close/target-date column, so the
horizon is anchored on the field that DOES exist: the demand-implying
``kg_edges`` row's own ``created_at`` (``TIMESTAMPTZ``). Each pipeline stage
gets an expected time-to-close in days from
``inventory-forecast-weights.json``'s ``stage_days_to_close`` map, so a
line's **expected close date** is
``kg_edges.created_at + stage_days_to_close[stage]``. A line is **within
horizon** when that expected close date falls at or before
``now() + horizon_days`` -- inclusive at the boundary. Lines outside the
horizon contribute ZERO and are reported with ``within_horizon: false``.

A stage absent from ``stage_days_to_close`` has **no horizon policy at all**:
its lines are reported with ``within_horizon: null``, ``counted: false`` and
excluded from the sum -- never defaulted to "closes today", never treated as
"always within horizon" (the same "config is IP, never guess" rule
``stage_weights`` uses for an unweighted stage). The two exclusion classes are
distinguishable by a caller: ``stage_weight: null`` means "no weight
configured", ``within_horizon: false``/``null`` means "outside horizon" / "no
days-to-close policy"; both keep ``counted: false``.

Config is IP: ``inventory-forecast-weights.json``
------------------------------------------------------------------------------
Same bare-JSON-read, no-DB, no-config-class discipline as
``replenishment.py``'s ``load_inventory_reorder_points_config`` /
``transactions.py``'s ``load_inventory_valuation_config``. GLOBAL, not
namespace-scoped, for this wave -- the same choice those two files make for
themselves.

Dependency direction (uncle-bob-craft)
------------------------------------------------------------------------------
This module imports only ``asyncpg`` and ``nce.db_utils.scoped_pg_session`` --
no web/HTTP/admin framework imports, and nothing from
``nce.vertical_modules.inventory.replenishment``/``.stock``/``.rma`` (no
cross-module private-helper import; the small Decimal/UUID coercion helpers
below are adapted copies, the same duplication-over-cross-module-private-
import choice ``replenishment.py``'s own docstring already makes and argues
for). ``NCEEngine`` is imported under ``TYPE_CHECKING`` only.

Registration is deliberately NOT this wave's job
------------------------------------------------------------------------------
``do_forecast_demand`` is not registered as an MCP tool or a REST route here
-- Module 11's established pattern (see ``replenishment.py``'s own docstring
section of the same name; registration + the tool-count assertion are later
batches' concerns).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.db_utils import scoped_pg_session

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.inventory.forecast")

# ---------------------------------------------------------------------------
# Decimal coercion -- duplicated from replenishment.py, not imported (see
# module docstring's "Dependency direction" section).
# ---------------------------------------------------------------------------

_SCORE_SCALE: Decimal = Decimal("0.0001")
_ZERO_SCORE: Decimal = Decimal("0.0000")

_DEFAULT_HORIZON_DAYS = 90
# Same ceiling rationale as replenishment.py's _MAX_LOOKBACK_DAYS: two orders
# of magnitude inside the nearest date-arithmetic overflow, so an
# out-of-range value fails as the documented ValueError, here, rather than as
# an OverflowError inside _horizon_state's timedelta arithmetic.
_MAX_HORIZON_DAYS = 36_525

# _horizon_state's tri-state verdict. Deliberately three values, not a bool:
# "this stage has no days-to-close policy" is NOT the same answer as "this
# line's expected close date is past the horizon", and a caller must be able
# to tell them apart (see the module docstring's horizon section).
_HORIZON_WITHIN = "within"
_HORIZON_OUTSIDE = "outside"
_HORIZON_NO_POLICY = "no_policy"

_PIPELINE_DEMAND_PREDICATE = "implies_demand"
_PRODUCT_LABEL_PREFIX = "PRODUCT:"


def _as_decimal(value: Any, where: str) -> Decimal:
    """Coerce a caller/config/DB-supplied number to an exact, finite
    ``Decimal`` -- see ``replenishment.py``'s helper of the same name for the
    full rationale (bool rejected before int; float via ``Decimal(str(x))``,
    never ``Decimal(x)``)."""
    if isinstance(value, bool):
        raise ValueError(f"{where}: bool is not a number, got {value!r}")
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
    if not candidate.is_finite():
        raise ValueError(f"{where}: must be finite, got {value!r}")
    return candidate


def _quantise_score(value: Decimal, where: str) -> Decimal:
    """Round a forecast contribution to a stable 4dp scale, ties away from
    zero -- so the same seeded pipeline always reproduces the same score."""
    try:
        return value.quantize(_SCORE_SCALE, rounding=ROUND_HALF_UP)
    except DecimalException as exc:
        raise ValueError(f"{where}: value is too large to express to 4dp: {value!r}") from exc


def _as_ns_uuid(raw: Any, field: str) -> UUID:
    if not raw:
        raise ValueError(f"'{field}' is required")
    return UUID(str(raw)) if not isinstance(raw, UUID) else raw


def _as_optional_sku(raw: Any) -> str | None:
    """``sku`` is optional -- ``None``/blank means "every SKU with a pipeline
    line in this namespace"."""
    if raw is None:
        return None
    sku = str(raw).strip()
    return sku or None


def _product_label(sku: str) -> str:
    """``commission.py``'s own convention: ``f"PRODUCT:{sku.upper().strip()}"``."""
    return f"{_PRODUCT_LABEL_PREFIX}{sku.upper().strip()}"


def _sku_from_product_label(label: str) -> str:
    """Inverse of :func:`_product_label` -- strips the ``PRODUCT:`` prefix."""
    if label.startswith(_PRODUCT_LABEL_PREFIX):
        return label[len(_PRODUCT_LABEL_PREFIX) :]
    return label


# ---------------------------------------------------------------------------
# Forecast-weights config loader -- reads
# nce/config_data/inventory-forecast-weights.json (no config class), mirrors
# replenishment.py's load_inventory_reorder_points_config().
# ---------------------------------------------------------------------------

_CONFIG_DATA_DIR = Path(__file__).parents[3] / "nce" / "config_data"
_FORECAST_WEIGHTS_CONFIG_FILENAME = "inventory-forecast-weights.json"


def load_inventory_forecast_weights_config() -> dict[str, Any]:
    """Load and return the contents of ``inventory-forecast-weights.json``.

    Returns
    -------
    dict with keys ``stage_weights`` (mapping of pipeline-stage key ->
    demand-probability weight, ``0..1``) and ``stage_days_to_close`` (mapping
    of the same pipeline-stage key -> expected time-to-close, a non-negative
    whole number of days -- the horizon anchor's per-stage offset). Both are
    validated by :func:`_parse_stage_weights` / :func:`_parse_stage_days_to_close`
    at use, which is where a malformed value raises. Global -- not
    namespace-scoped -- for this wave (see module docstring).
    """
    path = _CONFIG_DATA_DIR / _FORECAST_WEIGHTS_CONFIG_FILENAME
    with path.open(encoding="utf-8") as fh:
        config: dict[str, Any] = json.load(fh)
    return config


def _parse_stage_weights(raw: Mapping[str, Any]) -> dict[str, Decimal]:
    """Coerce the config's raw ``{stage: number}`` mapping into
    ``{stage: Decimal}``.

    Each weight must be within ``0..1`` -- a demand-*probability* weight, not
    an arbitrary multiplier: a draft quote must never imply MORE demand than
    a signed project (weight ``1.0``). Out-of-range values are rejected
    rather than silently clamped -- a malformed config value should fail
    loudly, not quietly produce a plausible-looking wrong score.
    """
    weights: dict[str, Decimal] = {}
    for stage, value in raw.items():
        where = f"inventory-forecast-weights.json.stage_weights[{stage!r}]"
        weight = _as_decimal(value, where)
        if not (Decimal("0") <= weight <= Decimal("1")):
            raise ValueError(
                f"{where}: must be a demand-probability weight within 0..1, got {weight!r}"
            )
        weights[str(stage)] = weight
    return weights


def _parse_stage_days_to_close(raw: Mapping[str, Any]) -> dict[str, int]:
    """Coerce the config's raw ``{stage: days}`` mapping into ``{stage: int}``.

    Each value is an expected time-to-close: a **non-negative whole number of
    days**. A malformed value (negative, fractional, non-numeric, bool) is
    rejected with ``ValueError`` at load rather than clamped or defaulted --
    the same loud-failure rule ``_parse_stage_weights``' 0..1 range uses, and
    for the same reason: a quietly-repaired horizon policy produces a
    plausible-looking wrong score.
    """
    days_by_stage: dict[str, int] = {}
    for stage, value in raw.items():
        where = f"inventory-forecast-weights.json.stage_days_to_close[{stage!r}]"
        if isinstance(value, bool):
            raise ValueError(f"{where}: bool is not a number of days, got {value!r}")
        if isinstance(value, int):
            days = value
        elif isinstance(value, float):
            if not value.is_integer():
                raise ValueError(f"{where}: must be a whole number of days, got {value!r}")
            days = int(value)
        else:
            raise ValueError(
                f"{where}: expected an int number of days, got {type(value).__name__} {value!r}"
            )
        if days < 0:
            raise ValueError(f"{where}: must be a non-negative number of days, got {days!r}")
        days_by_stage[str(stage)] = days
    return days_by_stage


def _horizon_state(
    created_at: datetime,
    stage: str,
    stage_days_to_close: Mapping[str, int],
    horizon_days: int,
    now: datetime,
) -> str:
    """Decide whether ONE pipeline line falls within the requested horizon.

    Returns :data:`_HORIZON_WITHIN`, :data:`_HORIZON_OUTSIDE`, or
    :data:`_HORIZON_NO_POLICY` when *stage* is absent from
    *stage_days_to_close* -- an unconfigured stage is never defaulted (module
    docstring's horizon section).

    Decides only; it neither loads config nor formats output.
    """
    days = stage_days_to_close.get(stage)
    if days is None:
        return _HORIZON_NO_POLICY
    if created_at is None:
        raise ValueError("kg_edges.created_at: required to decide horizon membership, got None")
    # kg_edges.created_at is TIMESTAMPTZ; compare tz-aware UTC on both sides.
    created = (
        created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=timezone.utc)
    )
    expected_close = created + timedelta(days=days)
    horizon_end = (now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)) + timedelta(
        days=horizon_days
    )
    return _HORIZON_WITHIN if expected_close <= horizon_end else _HORIZON_OUTSIDE


def _parse_horizon_days(raw: Any) -> int:
    """Coerce ``horizon_days`` to a whole number of days inside
    ``1 .. _MAX_HORIZON_DAYS``. Mirrors ``replenishment.py``'s
    ``_parse_lookback_days``: ABSENT is the only case that falls back to
    :data:`_DEFAULT_HORIZON_DAYS`; a present-but-non-positive value is
    rejected, never coalesced.

    See the module docstring's "How ``horizon_days`` filters" section: this
    value is echoed back in the response AND filters which pipeline lines
    count, via each line's ``kg_edges.created_at`` +
    ``stage_days_to_close[stage]`` expected close date.
    """
    where = "horizon_days"
    if raw is None:
        return _DEFAULT_HORIZON_DAYS
    if isinstance(raw, bool):
        raise ValueError(f"{where}: bool is not a number of days, got {raw!r}")
    if isinstance(raw, int):
        days = raw
    elif isinstance(raw, float):
        if not raw.is_integer():
            raise ValueError(f"{where}: must be a whole number of days, got {raw!r}")
        days = int(raw)
    else:
        raise ValueError(f"{where}: expected an int, got {type(raw).__name__} {raw!r}")
    if days <= 0:
        raise ValueError(f"{where}: must be a positive number of days, got {days!r}")
    if days > _MAX_HORIZON_DAYS:
        raise ValueError(
            f"{where}: must be at most {_MAX_HORIZON_DAYS} days (100 years), got {days!r}"
        )
    return days


# ---------------------------------------------------------------------------
# Pipeline read -- the SOLE data source (module docstring's "pipeline
# signal" section). Explicit namespace_id bound on every query.
# ---------------------------------------------------------------------------


async def _fetch_pipeline_edges(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    sku: str | None,
) -> list[asyncpg.Record]:  # type: ignore[type-arg]
    """Every ``kg_edges`` row implying demand for a ``PRODUCT:{sku}`` object,
    joined to its ``kg_nodes`` stage (``entity_type``). Binds ``namespace_id``
    as an explicit parameter on both the edge and the node join -- never
    relies on RLS alone (module docstring's scoping-discipline section)."""
    object_label = _product_label(sku) if sku else None
    return await conn.fetch(
        """
        SELECT e.subject_label, e.object_label, e.confidence, e.created_at,
               n.entity_type AS stage
        FROM kg_edges e
        JOIN kg_nodes n
          ON n.label = e.subject_label AND n.namespace_id = e.namespace_id
        WHERE e.namespace_id = $1::uuid
          AND e.predicate = $2
          AND e.object_label LIKE 'PRODUCT:%'
          AND ($3::text IS NULL OR e.object_label = $3)
        ORDER BY e.object_label ASC, e.subject_label ASC
        """,
        str(ns_uuid),
        _PIPELINE_DEMAND_PREDICATE,
        object_label,
    )


# ---------------------------------------------------------------------------
# Pure pipeline math -- no DB, no asyncpg awareness.
# ---------------------------------------------------------------------------


class _ForecastLine(NamedTuple):
    subject_label: str
    stage: str
    confidence: Decimal
    stage_weight: Decimal | None
    within_horizon: bool | None
    counted: bool
    contribution: Decimal


def _compute_forecast_lines(
    rows: Sequence[Mapping[str, Any]],
    stage_weights: Mapping[str, Decimal],
    stage_days_to_close: Mapping[str, int],
    horizon_days: int,
    now: datetime,
) -> list[_ForecastLine]:
    """Pure over already-fetched
    ``{"subject_label", "stage", "confidence", "created_at"}`` rows for ONE sku.

    A row counts only when its stage HAS a weight **and**
    :func:`_horizon_state` puts it within the horizon. Both exclusion classes
    are reported, and distinguishable: ``stage_weight=None`` means "no weight
    configured", ``within_horizon=False``/``None`` means "expected close date
    past the horizon" / "stage has no days-to-close policy". Neither is ever
    guessed (module docstring's "config is IP" section); both contribute 0.
    """
    lines: list[_ForecastLine] = []
    for row in rows:
        stage = str(row["stage"])
        confidence = _as_decimal(row["confidence"], "kg_edges.confidence")
        weight = stage_weights.get(stage)
        state = _horizon_state(row["created_at"], stage, stage_days_to_close, horizon_days, now)
        within_horizon = None if state == _HORIZON_NO_POLICY else state == _HORIZON_WITHIN
        if weight is None or state != _HORIZON_WITHIN:
            lines.append(
                _ForecastLine(
                    subject_label=str(row["subject_label"]),
                    stage=stage,
                    confidence=confidence,
                    stage_weight=weight,
                    within_horizon=within_horizon,
                    counted=False,
                    contribution=_ZERO_SCORE,
                )
            )
            continue
        contribution = _quantise_score(weight * confidence, "forecast_line.contribution")
        lines.append(
            _ForecastLine(
                subject_label=str(row["subject_label"]),
                stage=stage,
                confidence=confidence,
                stage_weight=weight,
                within_horizon=True,
                counted=True,
                contribution=contribution,
            )
        )
    return lines


def _build_sku_forecast(sku: str, lines: Sequence[_ForecastLine]) -> dict[str, Any]:
    total = _ZERO_SCORE
    for line in lines:
        total += line.contribution

    counted = [ln for ln in lines if ln.counted]
    excluded = [ln for ln in lines if not ln.counted]
    counted_citations = (
        ", ".join(f"{ln.subject_label}[{ln.stage}]" for ln in counted) or "no pipeline lines"
    )
    rationale = (
        f"{sku}: forecasted demand score {total} from {len(counted)} pipeline line(s) "
        f"({counted_citations})"
    )
    unweighted = [ln for ln in excluded if ln.stage_weight is None]
    out_of_horizon = [ln for ln in excluded if ln.stage_weight is not None]
    if unweighted:
        citations = ", ".join(f"{ln.subject_label}[{ln.stage}]" for ln in unweighted)
        rationale += (
            f"; {len(unweighted)} line(s) excluded -- no stage_weight configured for: {citations}"
        )
    if out_of_horizon:
        citations = ", ".join(f"{ln.subject_label}[{ln.stage}]" for ln in out_of_horizon)
        rationale += (
            f"; {len(out_of_horizon)} line(s) excluded -- expected close date not within the "
            f"requested horizon (or the stage has no stage_days_to_close policy): {citations}"
        )

    return {
        "sku": sku,
        "forecasted_demand_score": total,
        "pipeline_lines": [
            {
                "subject_label": ln.subject_label,
                "stage": ln.stage,
                "confidence": ln.confidence,
                "stage_weight": ln.stage_weight,
                "within_horizon": ln.within_horizon,
                "counted": ln.counted,
            }
            for ln in lines
        ],
        "rationale": rationale,
    }


# ---------------------------------------------------------------------------
# Public: do_forecast_demand -- return-only advisor. NO writes (module
# docstring's "This module recommends; it does not spend" section).
# ---------------------------------------------------------------------------


async def do_forecast_demand(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Pipeline-aware demand forecast, per SKU, from Knowledge Graph pipeline
    lines weighted by configured pipeline-stage probabilities. **Writes
    nothing** -- see module docstring.

    Parameters
    ----------
    params:
        ``{
            "namespace_id": str | UUID,  # required
            "horizon_days": int | None,  # optional -- see module docstring's
                                          # "How horizon_days filters": echoed
                                          # back AND filters which lines count
            "sku":          str | None,  # optional -- forecast only this SKU
                                          # (even with zero pipeline lines);
                                          # omitted means every SKU with at
                                          # least one pipeline line in this
                                          # namespace
        }``

    Returns
    -------
    dict
        ``{"ok": True, "namespace_id", "horizon_days", "forecasts": [
        {sku, forecasted_demand_score, pipeline_lines, rationale}, ...]}``.
        Each ``pipeline_lines`` entry carries ``stage_weight`` and
        ``within_horizon`` so a caller can tell WHY a ``counted: false`` line
        did not count.
        A SKU absent from the pipeline (no matching ``kg_edges`` row) is only
        ever returned when explicitly requested via ``params["sku"]`` -- it
        comes back with ``forecasted_demand_score: 0.0000`` and an empty
        ``pipeline_lines``, never a guessed figure.

    Raises
    ------
    ValueError
        ``namespace_id`` missing; a malformed ``horizon_days`` (outside
        ``1 .. _MAX_HORIZON_DAYS``); or a malformed
        ``inventory-forecast-weights.json`` value (non-numeric weight, or one
        outside ``0..1``; a ``stage_days_to_close`` entry that is not a
        non-negative whole number of days).
    """
    ns_uuid = _as_ns_uuid(params.get("namespace_id"), "namespace_id")
    sku_filter = _as_optional_sku(params.get("sku"))
    if sku_filter is not None:
        # Canonicalise to the SAME case _product_label()/_sku_from_product_label()
        # round-trip through (commission.py's own "PRODUCT:{sku.upper()}"
        # convention) -- otherwise a caller-supplied "sku-1" would build the
        # right kg_edges filter (label matching is exact-string) but then miss
        # its own entry in rows_by_sku, which is always keyed by the
        # UPPERCASED label this module reads back from the KG.
        sku_filter = sku_filter.upper()
    horizon_days = _parse_horizon_days(params.get("horizon_days"))

    config = load_inventory_forecast_weights_config()
    stage_weights = _parse_stage_weights(config.get("stage_weights") or {})
    stage_days_to_close = _parse_stage_days_to_close(config.get("stage_days_to_close") or {})
    now = datetime.now(timezone.utc)

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        db_rows = await _fetch_pipeline_edges(conn, ns_uuid, sku_filter)

    rows_by_sku: dict[str, list[dict[str, Any]]] = {}
    for r in db_rows:
        sku = _sku_from_product_label(r["object_label"])
        rows_by_sku.setdefault(sku, []).append(
            {
                "subject_label": r["subject_label"],
                "stage": r["stage"],
                "confidence": r["confidence"],
                "created_at": r["created_at"],
            }
        )

    if sku_filter is not None:
        skus_to_report = [sku_filter]
    else:
        skus_to_report = sorted(rows_by_sku)

    forecasts: list[dict[str, Any]] = []
    for sku in skus_to_report:
        lines = _compute_forecast_lines(
            rows_by_sku.get(sku, []), stage_weights, stage_days_to_close, horizon_days, now
        )
        forecasts.append(_build_sku_forecast(sku, lines))

    return {
        "ok": True,
        "namespace_id": str(ns_uuid),
        "horizon_days": horizon_days,
        "forecasts": forecasts,
    }
