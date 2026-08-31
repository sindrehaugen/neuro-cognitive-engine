"""Tests for the Inventory engine's pipeline-aware demand forecast Advisor
(Module 11, Wave 7 — Batch 135 — ``nce/vertical_modules/inventory/forecast.py``).

Covers:

  1. Pure-logic validation (no DB) — ``do_forecast_demand``'s required-field
     validation, exercised through the PUBLIC function before it ever
     touches ``engine.pg_pool`` (mirrors
     ``test_inventory_replenishment.py``'s ``_DummyEngine`` pattern).
  2. Config parsing (no DB) — ``_parse_stage_weights`` rejects a weight
     outside ``0..1``; ``_parse_horizon_days`` distinguishes an ABSENT
     ``horizon_days`` (falls back to the default) from a present-but-
     out-of-range one.
  3. Pure pipeline math (no DB) — ``_compute_forecast_lines``: a line whose
     stage IS configured contributes ``weight * confidence``; a line whose
     stage is NOT configured is excluded from the sum and reported
     ``counted: False, stage_weight: None``.
  4. Integration (``@pytest.mark.integration``, live Postgres) — the wave's
     own acceptance: demand forecast reflects a seeded pipeline and the
     configured stage weights.
     - a forecast for a SKU with two configured-stage pipeline lines totals
       ``sum(weight * confidence)`` across both;
     - a pipeline line whose stage has no configured weight is excluded from
       the score but still reported in ``pipeline_lines``;
     - a second namespace's pipeline lines never influence the first
       namespace's forecast (every query in the module binds an explicit
       ``namespace_id``);
     - a SKU with zero pipeline lines, requested explicitly, is reported
       with a zero score and an empty ``pipeline_lines`` — never a guess;
     - omitting ``sku`` reports exactly the SKUs that have at least one
       pipeline line, and a KG edge for an unrelated predicate (not
       ``implies_demand``) is never mistaken for a pipeline line.

Each integration test is written so that deleting the predicate or guard it
claims to cover makes it FAIL:

  - the isolation test seeds the SAME sku for both namespaces so ns_b's row
    is genuinely reachable by the query under test if the ``namespace_id``
    predicate were dropped;
  - the "unconfigured stage excluded" test pins the SCORE (not just presence
    in ``pipeline_lines``) — the configured-stage line alone produces a
    different number than "both lines counted";
  - the "wrong predicate ignored" test seeds a real ``kg_edges`` row with a
    different predicate pointing at the same ``PRODUCT:{sku}`` object, so a
    query that dropped the ``predicate = 'implies_demand'`` filter would
    pick it up and change the SKU set / score.

SKUs in the integration tests are per-run unique (``_unique_sku``) for the
same reason ``test_inventory_replenishment.py`` uses one — the shared
integration database accumulates ``kg_nodes``/``kg_edges`` rows across many
previous runs and namespaces.

Every integration test monkeypatches
``forecast.load_inventory_forecast_weights_config`` rather than mutating the
on-disk shared config file — same discipline
``test_inventory_replenishment.py``'s reorder-point tests already use.

This module never writes ``kg_nodes``/``kg_edges`` — there is nothing to
assert-was-not-written here because ``do_forecast_demand`` never opens a
write-capable statement at all; see the wave report's NOT VERIFIED section.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.vertical_modules.inventory import forecast
from nce.vertical_modules.inventory.forecast import (
    _DEFAULT_HORIZON_DAYS,
    _HORIZON_NO_POLICY,
    _HORIZON_OUTSIDE,
    _HORIZON_WITHIN,
    _MAX_HORIZON_DAYS,
    _as_optional_sku,
    _compute_forecast_lines,
    _horizon_state,
    _parse_horizon_days,
    _parse_stage_days_to_close,
    _parse_stage_weights,
    _product_label,
    _sku_from_product_label,
    do_forecast_demand,
    load_inventory_forecast_weights_config,
)

# A fixed "now" for the pure horizon tests, so they never depend on wall clock.
_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

# ---------------------------------------------------------------------------
# 1. Pure-logic validation (no DB) — exercised through the PUBLIC function.
# ---------------------------------------------------------------------------


class _DummyEngine:
    """Stands in for NCEEngine in tests that never reach a DB call — the
    validation under test raises before ``engine.pg_pool`` is ever touched."""

    pg_pool = None


@pytest.mark.asyncio
async def test_do_forecast_demand_rejects_missing_namespace_id() -> None:
    with pytest.raises(ValueError, match="'namespace_id' is required"):
        await do_forecast_demand(_DummyEngine(), {})


def test_as_optional_sku_none_and_blank_mean_every_sku_with_a_pipeline_line() -> None:
    assert _as_optional_sku(None) is None
    assert _as_optional_sku("   ") is None
    assert _as_optional_sku(" SKU-1 ") == "SKU-1"


def test_product_label_round_trips_through_its_inverse() -> None:
    assert _product_label("sku-abc") == "PRODUCT:SKU-ABC"
    assert _sku_from_product_label("PRODUCT:SKU-ABC") == "SKU-ABC"
    # a label without the prefix is returned unchanged (defensive, never raises)
    assert _sku_from_product_label("SKU-ABC") == "SKU-ABC"


# ---------------------------------------------------------------------------
# 2. Config parsing (no DB).
# ---------------------------------------------------------------------------


def test_load_inventory_forecast_weights_config_has_expected_shape() -> None:
    """Sanity check on the actual on-disk file this module ships — not
    monkeypatched. Every integration test below DOES monkeypatch the loader
    for its own scenario data (never mutates this shared file)."""
    config = load_inventory_forecast_weights_config()
    assert isinstance(config.get("stage_weights"), dict)
    assert len(config["stage_weights"]) > 0
    # the approved expected-time-to-close defaults, verbatim -- never rounded,
    # interpolated or invented.
    assert isinstance(config.get("stage_days_to_close"), dict)
    assert _parse_stage_days_to_close(config["stage_days_to_close"]) == {
        "quote_draft": 60,
        "quote_sent": 30,
        "design_approved": 14,
        "project_signed": 7,
    }


def test_parse_stage_weights_accepts_the_documented_range() -> None:
    parsed = _parse_stage_weights({"quote_draft": 0.2, "project_signed": 1, "floor": 0})
    assert parsed == {
        "quote_draft": Decimal("0.2"),
        "project_signed": Decimal("1"),
        "floor": Decimal("0"),
    }


def test_parse_stage_weights_rejects_a_weight_above_one() -> None:
    with pytest.raises(ValueError, match="must be a demand-probability weight within 0..1"):
        _parse_stage_weights({"quote_draft": 1.5})


def test_parse_stage_weights_rejects_a_negative_weight() -> None:
    with pytest.raises(ValueError, match="must be a demand-probability weight within 0..1"):
        _parse_stage_weights({"quote_draft": -0.1})


def test_parse_horizon_days_absent_falls_back_to_the_documented_default() -> None:
    assert _parse_horizon_days(None) == _DEFAULT_HORIZON_DAYS == 90


def test_parse_horizon_days_accepts_a_positive_value_unchanged() -> None:
    assert _parse_horizon_days(30) == 30
    assert _parse_horizon_days(7.0) == 7


def test_parse_horizon_days_rejects_zero_rather_than_silently_defaulting() -> None:
    with pytest.raises(ValueError, match="must be a positive number of days"):
        _parse_horizon_days(0)


def test_parse_horizon_days_rejects_negative() -> None:
    with pytest.raises(ValueError, match="must be a positive number of days"):
        _parse_horizon_days(-1)


def test_parse_horizon_days_accepts_the_ceiling_and_rejects_one_day_past_it() -> None:
    assert _parse_horizon_days(_MAX_HORIZON_DAYS) == _MAX_HORIZON_DAYS
    with pytest.raises(ValueError, match="must be at most"):
        _parse_horizon_days(_MAX_HORIZON_DAYS + 1)


def test_parse_horizon_days_rejects_bool_and_non_numeric() -> None:
    with pytest.raises(ValueError, match="bool is not a number of days"):
        _parse_horizon_days(True)
    with pytest.raises(ValueError, match="expected an int"):
        _parse_horizon_days("30")
    with pytest.raises(ValueError, match="whole number of days"):
        _parse_horizon_days(1.5)


def test_parse_stage_days_to_close_accepts_non_negative_whole_numbers() -> None:
    assert _parse_stage_days_to_close({"quote_sent": 30, "same_day": 0, "float_ok": 14.0}) == {
        "quote_sent": 30,
        "same_day": 0,
        "float_ok": 14,
    }


@pytest.mark.parametrize(
    ("bad", "match"),
    [
        (-1, "non-negative"),
        (-0.5, "whole number of days"),
        (1.5, "whole number of days"),
        ("30", "expected an int"),
        (True, "bool is not a number of days"),
        (None, "expected an int"),
    ],
)
def test_parse_stage_days_to_close_rejects_malformed_values_loudly(bad: Any, match: str) -> None:
    """A malformed stage_days_to_close value fails at config load -- never
    clamped, never defaulted (the same discipline stage_weights' 0..1 uses)."""
    with pytest.raises(ValueError, match=match):
        _parse_stage_days_to_close({"quote_sent": bad})


@pytest.mark.asyncio
async def test_do_forecast_demand_rejects_malformed_days_before_touching_the_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_DummyEngine.pg_pool is None, so this only passes if the ValueError is
    raised at config load -- before any DB call."""
    monkeypatch.setattr(
        forecast,
        "load_inventory_forecast_weights_config",
        lambda: {"stage_weights": {"quote_sent": 0.4}, "stage_days_to_close": {"quote_sent": -3}},
    )
    with pytest.raises(ValueError, match="non-negative"):
        await do_forecast_demand(_DummyEngine(), {"namespace_id": uuid.uuid4()})


# ---------------------------------------------------------------------------
# 2b. The horizon predicate (no DB) -- _horizon_state called directly.
# ---------------------------------------------------------------------------


def test_horizon_state_boundary_expected_close_exactly_on_the_horizon_is_within() -> None:
    """PINNED CHOICE: the horizon is INCLUSIVE -- "at or before now + horizon_days".
    A line landing EXACTLY on the boundary counts; one day past it does not."""
    created = _NOW - timedelta(days=10)
    # 40 days to close on a line created 10 days ago == expected close exactly now+30d
    assert _horizon_state(created, "s", {"s": 40}, 30, _NOW) == _HORIZON_WITHIN
    assert _horizon_state(created, "s", {"s": 41}, 30, _NOW) == _HORIZON_OUTSIDE
    assert _horizon_state(created, "s", {"s": 39}, 30, _NOW) == _HORIZON_WITHIN


def test_horizon_state_unconfigured_stage_has_no_policy_and_is_never_defaulted() -> None:
    """An unconfigured stage is never treated as "closes today" nor as "always
    within horizon" -- even at the maximum horizon it reports no policy."""
    assert _horizon_state(_NOW, "mystery", {"quote_sent": 30}, 1, _NOW) == _HORIZON_NO_POLICY
    assert (
        _horizon_state(_NOW, "mystery", {"quote_sent": 30}, _MAX_HORIZON_DAYS, _NOW)
        == _HORIZON_NO_POLICY
    )


def test_horizon_state_treats_a_naive_created_at_as_utc() -> None:
    naive = _NOW.replace(tzinfo=None)
    assert _horizon_state(naive, "s", {"s": 0}, 1, _NOW) == _HORIZON_WITHIN


# ---------------------------------------------------------------------------
# 3. Pure pipeline math (no DB) — _compute_forecast_lines called directly.
# ---------------------------------------------------------------------------


def test_compute_forecast_lines_configured_stage_contributes_weight_times_confidence() -> None:
    rows: list[dict[str, Any]] = [
        {"subject_label": "QUOTE:Q1", "stage": "quote_sent", "confidence": 0.5, "created_at": _NOW},
    ]
    lines = _compute_forecast_lines(
        rows, {"quote_sent": Decimal("0.4")}, {"quote_sent": 30}, 90, _NOW
    )
    assert len(lines) == 1
    line = lines[0]
    assert line.counted is True
    assert line.stage_weight == Decimal("0.4")
    assert line.contribution == Decimal("0.2000"), "0.4 * 0.5 = 0.20"


def test_compute_forecast_lines_unconfigured_stage_is_excluded_not_guessed() -> None:
    rows: list[dict[str, Any]] = [
        {
            "subject_label": "QUOTE:Q2",
            "stage": "no_such_stage",
            "confidence": 0.9,
            "created_at": _NOW,
        },
    ]
    lines = _compute_forecast_lines(
        rows, {"quote_sent": Decimal("0.4")}, {"quote_sent": 30}, 90, _NOW
    )
    assert len(lines) == 1
    line = lines[0]
    assert line.counted is False
    assert line.stage_weight is None
    assert line.contribution == Decimal("0.0000"), (
        "an unconfigured stage must contribute ZERO, never a guessed weight"
    )
    assert line.within_horizon is None, "no stage_days_to_close policy -> no horizon verdict"


def test_compute_forecast_lines_sums_multiple_lines_and_excludes_only_the_unconfigured_one() -> (
    None
):
    rows: list[dict[str, Any]] = [
        {
            "subject_label": "QUOTE:Q1",
            "stage": "quote_sent",
            "confidence": 1.0,
            "created_at": _NOW,
        },
        {
            "subject_label": "PROJECT:P1",
            "stage": "project_signed",
            "confidence": 1.0,
            "created_at": _NOW,
        },
        {
            "subject_label": "QUOTE:Q3",
            "stage": "unknown_stage",
            "confidence": 1.0,
            "created_at": _NOW,
        },
    ]
    lines = _compute_forecast_lines(
        rows,
        {"quote_sent": Decimal("0.4"), "project_signed": Decimal("1.0")},
        {"quote_sent": 30, "project_signed": 7},
        90,
        _NOW,
    )
    total = sum((ln.contribution for ln in lines), Decimal("0.0000"))
    assert total == Decimal("1.4000"), "0.4 (quote_sent) + 1.0 (project_signed); unknown excluded"
    counted = [ln for ln in lines if ln.counted]
    excluded = [ln for ln in lines if not ln.counted]
    assert len(counted) == 2
    assert len(excluded) == 1
    assert excluded[0].subject_label == "QUOTE:Q3"


def test_compute_forecast_lines_empty_rows_are_zero() -> None:
    lines = _compute_forecast_lines(
        [], {"quote_sent": Decimal("0.4")}, {"quote_sent": 30}, 90, _NOW
    )
    assert lines == []


# ---------------------------------------------------------------------------
# Integration helpers — seed directly via the owner pool, matching
# test_inventory_replenishment.py's convention. Every helper takes an
# explicit namespace_id and scopes its own SQL by it.
# ---------------------------------------------------------------------------


class _EngineStub:
    def __init__(self, pg_pool: asyncpg.Pool) -> None:  # type: ignore[type-arg]
        self.pg_pool = pg_pool


def _unique_sku(prefix: str) -> str:
    """A per-run unique SKU — see test_inventory_replenishment.py's helper of
    the same name for the full rationale (the shared integration database
    accumulates rows across many previous runs and namespaces).

    Upper-cased so it is already the canonical form ``_product_label()``
    stores and ``do_forecast_demand`` reports back — the module's own
    case-fold is exercised separately by the pure ``_product_label``/
    ``_sku_from_product_label`` round-trip test above, not by every
    integration test incidentally relying on it."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}".upper()


async def _seed_pipeline_node(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    label: str,
    stage: str,
) -> None:
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
            VALUES ($1, $2, $3::uuid, 'agent')
            ON CONFLICT (label, namespace_id) DO UPDATE SET entity_type = EXCLUDED.entity_type
            """,
            label,
            stage,
            namespace_id,
        )


async def _seed_pipeline_edge(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    subject_label: str,
    sku: str,
    confidence: float,
    predicate: str = "implies_demand",
    created_at: datetime | None = None,
) -> None:
    """*created_at* is the horizon anchor -- ``None`` means "now" (the column
    default), which is what every non-horizon test wants."""
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO kg_edges
                (subject_label, predicate, object_label, confidence, namespace_id,
                 change_origin, created_at)
            VALUES ($1, $2, $3, $4::float, $5::uuid, 'agent',
                    COALESCE($6::timestamptz, NOW()))
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
                SET confidence = EXCLUDED.confidence, created_at = EXCLUDED.created_at
            """,
            subject_label,
            predicate,
            _product_label(sku),
            confidence,
            namespace_id,
            created_at,
        )


def _patch_stage_weights(
    monkeypatch: pytest.MonkeyPatch,
    stage_weights: dict[str, Any],
    stage_days_to_close: dict[str, Any] | None = None,
) -> None:
    """*stage_days_to_close* defaults to "every configured stage closes the day
    it was created", making the horizon filter a deliberate no-op for the tests
    that are not about the horizon. The horizon tests pass it explicitly."""
    if stage_days_to_close is None:
        stage_days_to_close = dict.fromkeys(stage_weights, 0)
    monkeypatch.setattr(
        forecast,
        "load_inventory_forecast_weights_config",
        lambda: {"stage_weights": stage_weights, "stage_days_to_close": stage_days_to_close},
    )


# ---------------------------------------------------------------------------
# 4a. A seeded pipeline (two configured-stage lines) produces the summed,
#     weighted score.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forecast_reflects_a_seeded_pipeline_and_config_weights(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_stage_weights(monkeypatch, {"quote_sent": 0.4, "project_signed": 1.0})
    sku = _unique_sku("SKU-FORECAST")

    await _seed_pipeline_node(pg_pool, namespace_id, "QUOTE:Q-1", "quote_sent")
    await _seed_pipeline_edge(pg_pool, namespace_id, "QUOTE:Q-1", sku, confidence=0.5)

    await _seed_pipeline_node(pg_pool, namespace_id, "PROJECT:P-1", "project_signed")
    await _seed_pipeline_edge(pg_pool, namespace_id, "PROJECT:P-1", sku, confidence=1.0)

    engine = _EngineStub(pg_pool)
    result = await do_forecast_demand(engine, {"namespace_id": namespace_id, "sku": sku})

    assert result["ok"] is True
    assert result["horizon_days"] == _DEFAULT_HORIZON_DAYS
    assert len(result["forecasts"]) == 1
    entry = result["forecasts"][0]
    assert entry["sku"] == sku
    assert entry["forecasted_demand_score"] == Decimal("1.2000"), (
        "0.4*0.5 (quote_sent) + 1.0*1.0 (project_signed) = 0.2 + 1.0 = 1.2"
    )
    subjects = {line["subject_label"] for line in entry["pipeline_lines"]}
    assert subjects == {"QUOTE:Q-1", "PROJECT:P-1"}
    assert all(line["counted"] for line in entry["pipeline_lines"])
    assert "QUOTE:Q-1" in entry["rationale"]
    assert "PROJECT:P-1" in entry["rationale"]


# ---------------------------------------------------------------------------
# 4b. A pipeline line with an unconfigured stage is excluded from the score
#     but still reported.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unconfigured_stage_line_is_excluded_from_score_but_still_reported(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_stage_weights(monkeypatch, {"quote_sent": 0.4})
    sku = _unique_sku("SKU-UNCONFIGURED-STAGE")

    await _seed_pipeline_node(pg_pool, namespace_id, "QUOTE:Q-2", "quote_sent")
    await _seed_pipeline_edge(pg_pool, namespace_id, "QUOTE:Q-2", sku, confidence=1.0)

    await _seed_pipeline_node(pg_pool, namespace_id, "DESIGN:D-2", "design_no_such_stage")
    await _seed_pipeline_edge(pg_pool, namespace_id, "DESIGN:D-2", sku, confidence=1.0)

    engine = _EngineStub(pg_pool)
    result = await do_forecast_demand(engine, {"namespace_id": namespace_id, "sku": sku})
    entry = result["forecasts"][0]

    assert entry["forecasted_demand_score"] == Decimal("0.4000"), (
        "only the quote_sent line (weight 0.4) counts; the unconfigured-stage line "
        "contributes zero, not a guessed weight"
    )
    by_subject = {line["subject_label"]: line for line in entry["pipeline_lines"]}
    assert set(by_subject) == {"QUOTE:Q-2", "DESIGN:D-2"}, "both lines are still REPORTED"
    assert by_subject["QUOTE:Q-2"]["counted"] is True
    assert by_subject["DESIGN:D-2"]["counted"] is False
    assert by_subject["DESIGN:D-2"]["stage_weight"] is None
    assert "excluded" in entry["rationale"]
    assert "DESIGN:D-2" in entry["rationale"]


# ---------------------------------------------------------------------------
# 4c. Cross-namespace isolation.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_second_namespace_pipeline_lines_never_influence_the_first(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    make_namespace: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both namespaces use the SAME (unique) sku and the SAME subject label,
    so ns_b's row is genuinely reachable by the query under test if the
    ``namespace_id`` predicate were dropped from either the edge or the node
    join."""
    _patch_stage_weights(monkeypatch, {"project_signed": 1.0})
    ns_a = namespace_id
    ns_b = await make_namespace()
    sku = _unique_sku("SKU-ISO")

    await _seed_pipeline_node(pg_pool, ns_a, "PROJECT:SHARED", "project_signed")
    await _seed_pipeline_edge(pg_pool, ns_a, "PROJECT:SHARED", sku, confidence=1.0)

    await _seed_pipeline_node(pg_pool, ns_b, "PROJECT:SHARED", "project_signed")
    await _seed_pipeline_edge(pg_pool, ns_b, "PROJECT:SHARED", sku, confidence=1.0)

    engine = _EngineStub(pg_pool)
    result = await do_forecast_demand(engine, {"namespace_id": ns_a, "sku": sku})
    entry = result["forecasts"][0]

    assert entry["forecasted_demand_score"] == Decimal("1.0000"), (
        "ns_b's identically-labelled pipeline line must never double the score"
    )
    assert len(entry["pipeline_lines"]) == 1


# ---------------------------------------------------------------------------
# 4d. An explicitly-requested SKU with zero pipeline lines is reported with
#     a zero score, never a guess.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sku_with_no_pipeline_lines_is_reported_with_a_zero_score(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_stage_weights(monkeypatch, {"project_signed": 1.0})
    sku = _unique_sku("SKU-NO-PIPELINE")

    engine = _EngineStub(pg_pool)
    result = await do_forecast_demand(engine, {"namespace_id": namespace_id, "sku": sku})

    assert len(result["forecasts"]) == 1
    entry = result["forecasts"][0]
    assert entry["sku"] == sku
    assert entry["forecasted_demand_score"] == Decimal("0.0000")
    assert entry["pipeline_lines"] == []


# ---------------------------------------------------------------------------
# 4e. Omitting "sku" reports exactly the SKUs with a pipeline line, and a
#     wrong-predicate edge is never mistaken for one.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_omitting_sku_reports_only_skus_with_a_pipeline_line_and_ignores_other_predicates(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_stage_weights(monkeypatch, {"project_signed": 1.0})
    sku_a = _unique_sku("SKU-MULTI-A")
    sku_b = _unique_sku("SKU-MULTI-B")

    await _seed_pipeline_node(pg_pool, namespace_id, "PROJECT:MULTI", "project_signed")
    await _seed_pipeline_edge(pg_pool, namespace_id, "PROJECT:MULTI", sku_a, confidence=1.0)

    # A real kg_edges row for a DIFFERENT predicate, pointing at sku_b — must
    # never be mistaken for a pipeline demand line.
    await _seed_pipeline_edge(
        pg_pool,
        namespace_id,
        "PROJECT:MULTI",
        sku_b,
        confidence=1.0,
        predicate="failure_pattern",
    )

    engine = _EngineStub(pg_pool)
    result = await do_forecast_demand(engine, {"namespace_id": namespace_id})

    reported_skus = {entry["sku"] for entry in result["forecasts"]}
    assert sku_a in reported_skus
    assert sku_b not in reported_skus, (
        "a kg_edges row under a different predicate must not be read as pipeline demand"
    )


# ---------------------------------------------------------------------------
# 4f. THE HEADLINE: horizon_days actually filters. A shorter horizon scores
#     STRICTLY LOWER over the same seeded pipeline. This assertion is false on
#     main, where both horizons return the very same number.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_shorter_horizon_scores_strictly_lower_than_a_longer_one(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_stage_weights(
        monkeypatch,
        {"quote_draft": 0.2, "quote_sent": 0.4, "project_signed": 1.0},
        {"quote_draft": 60, "quote_sent": 30, "project_signed": 7},
    )
    sku = _unique_sku("SKU-HORIZON")
    now = datetime.now(timezone.utc)

    # created_at spans a range; each line's expected close is
    # created_at + stage_days_to_close[stage]:
    #   QUOTE:H-1   created 100d ago, closes 30d after creation -> 70d in the PAST
    #   PROJECT:H-2 created   1d ago, closes  7d after creation -> now +  6d
    #   QUOTE:H-3   created   5d ago, closes 60d after creation -> now + 55d
    await _seed_pipeline_node(pg_pool, namespace_id, "QUOTE:H-1", "quote_sent")
    await _seed_pipeline_edge(
        pg_pool, namespace_id, "QUOTE:H-1", sku, 0.5, created_at=now - timedelta(days=100)
    )
    await _seed_pipeline_node(pg_pool, namespace_id, "PROJECT:H-2", "project_signed")
    await _seed_pipeline_edge(
        pg_pool, namespace_id, "PROJECT:H-2", sku, 1.0, created_at=now - timedelta(days=1)
    )
    await _seed_pipeline_node(pg_pool, namespace_id, "QUOTE:H-3", "quote_draft")
    await _seed_pipeline_edge(
        pg_pool, namespace_id, "QUOTE:H-3", sku, 1.0, created_at=now - timedelta(days=5)
    )

    engine = _EngineStub(pg_pool)
    short = await do_forecast_demand(
        engine, {"namespace_id": namespace_id, "sku": sku, "horizon_days": 30}
    )
    wide = await do_forecast_demand(
        engine, {"namespace_id": namespace_id, "sku": sku, "horizon_days": 365}
    )

    short_score = short["forecasts"][0]["forecasted_demand_score"]
    wide_score = wide["forecasts"][0]["forecasted_demand_score"]

    assert short_score == Decimal("1.2000"), "0.4*0.5 + 1.0*1.0; the 60-day quote_draft is out"
    assert wide_score == Decimal("1.4000"), "+ 0.2*1.0 once the 60-day quote_draft fits"
    assert short_score < wide_score, (
        "horizon_days must FILTER: a 30-day horizon must score strictly lower than a "
        "365-day one over the same seeded pipeline"
    )

    # the response still echoes horizon_days back unchanged
    assert short["horizon_days"] == 30
    assert wide["horizon_days"] == 365

    # and the excluded line reports WHY it was excluded
    by_subject = {ln["subject_label"]: ln for ln in short["forecasts"][0]["pipeline_lines"]}
    assert set(by_subject) == {"QUOTE:H-1", "PROJECT:H-2", "QUOTE:H-3"}
    assert by_subject["QUOTE:H-3"]["counted"] is False
    assert by_subject["QUOTE:H-3"]["within_horizon"] is False
    assert by_subject["QUOTE:H-3"]["stage_weight"] == Decimal("0.2"), (
        "excluded by HORIZON, not by a missing stage weight -- the two reasons are distinct"
    )
    assert by_subject["QUOTE:H-1"]["within_horizon"] is True
    assert "horizon" in short["forecasts"][0]["rationale"]


# ---------------------------------------------------------------------------
# 4g. The per-stage map is really read -- two lines with the SAME created_at
#     land on opposite sides of ONE horizon because their days_to_close differ.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_same_created_at_different_stages_split_across_one_horizon(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_stage_weights(
        monkeypatch,
        {"quote_draft": 1.0, "quote_sent": 1.0},
        {"quote_draft": 60, "quote_sent": 30},
    )
    sku = _unique_sku("SKU-PER-STAGE")
    created = datetime.now(timezone.utc) - timedelta(days=1)

    await _seed_pipeline_node(pg_pool, namespace_id, "QUOTE:PS-SENT", "quote_sent")
    await _seed_pipeline_edge(pg_pool, namespace_id, "QUOTE:PS-SENT", sku, 1.0, created_at=created)
    await _seed_pipeline_node(pg_pool, namespace_id, "QUOTE:PS-DRAFT", "quote_draft")
    await _seed_pipeline_edge(pg_pool, namespace_id, "QUOTE:PS-DRAFT", sku, 1.0, created_at=created)

    engine = _EngineStub(pg_pool)
    result = await do_forecast_demand(
        engine, {"namespace_id": namespace_id, "sku": sku, "horizon_days": 45}
    )
    entry = result["forecasts"][0]

    by_subject = {ln["subject_label"]: ln for ln in entry["pipeline_lines"]}
    # created+30d = now+29d <= now+45d
    assert by_subject["QUOTE:PS-SENT"]["within_horizon"] is True
    # created+60d = now+59d > now+45d
    assert by_subject["QUOTE:PS-DRAFT"]["within_horizon"] is False
    assert entry["forecasted_demand_score"] == Decimal("1.0000"), (
        "identical weights and confidences -- only the per-stage days_to_close separates "
        "them, so a single global constant would score 2.0000 or 0.0000, never 1.0000"
    )


# ---------------------------------------------------------------------------
# 4h. A stage absent from stage_days_to_close is EXCLUDED, never defaulted --
#     even though it HAS a stage_weight, and at every horizon.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stage_without_a_days_to_close_policy_is_excluded_at_every_horizon(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unconfigured stage carries a WEIGHT of 0.9, so if it counted at all
    the score could never be exactly 0.4000 -- this pins "no policy => excluded"
    rather than "no policy => closes today" or "=> always within horizon"."""
    _patch_stage_weights(
        monkeypatch,
        {"quote_sent": 0.4, "mystery_stage": 0.9},
        {"quote_sent": 0},  # closes the day it was created -> within EVERY horizon
    )
    sku = _unique_sku("SKU-NO-DAYS-POLICY")

    await _seed_pipeline_node(pg_pool, namespace_id, "QUOTE:NP-1", "quote_sent")
    await _seed_pipeline_edge(pg_pool, namespace_id, "QUOTE:NP-1", sku, 1.0)
    await _seed_pipeline_node(pg_pool, namespace_id, "DESIGN:NP-2", "mystery_stage")
    await _seed_pipeline_edge(pg_pool, namespace_id, "DESIGN:NP-2", sku, 1.0)

    engine = _EngineStub(pg_pool)
    for horizon in (1, 90, _MAX_HORIZON_DAYS):
        result = await do_forecast_demand(
            engine, {"namespace_id": namespace_id, "sku": sku, "horizon_days": horizon}
        )
        entry = result["forecasts"][0]
        assert entry["forecasted_demand_score"] == Decimal("0.4000"), (
            f"at horizon_days={horizon} the no-policy stage must contribute ZERO"
        )
        by_subject = {ln["subject_label"]: ln for ln in entry["pipeline_lines"]}
        assert by_subject["DESIGN:NP-2"]["counted"] is False
        assert by_subject["DESIGN:NP-2"]["within_horizon"] is None, "the no-policy marker"
        assert by_subject["DESIGN:NP-2"]["stage_weight"] == Decimal("0.9"), (
            "it HAS a weight -- it is excluded purely for want of a days_to_close policy"
        )
