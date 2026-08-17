"""Tests for economy/recurring.py — Wave 9 (do_recognize_recurring).

Validates the Acceptance criteria from Batch_124_Module_8_Wave_9.md:

  1. MRR/ARR/churn snapshot is correct (do_snapshot_mrr_arr_churn).
  2. Ratable 1/12 recognition: twelve monthly recognitions sum EXACTLY to the
     annual amount, with the rounding remainder pinned to the final period
     (do_compute_recognition_schedule).
  3. The recurring cron's core (do_recognize_recurring) is idempotent: a
     replay of the same (namespace, contract, period) is a genuine no-op, a
     replay with a DIFFERENT amount is refused, and a colliding
     idempotency_key from an unrelated feature is also refused (never
     silently treated as a valid replay) — the documented limit of reusing
     the shared, not-action_type-scoped ``action_idempotency`` table.

Integration tests are ``@pytest.mark.integration`` — require a live Postgres
with schema.sql applied (``action_idempotency`` already exists — no
migration). Pure-logic tests for the coercion boundary and the two pure
cores (``do_compute_recognition_schedule`` / ``do_snapshot_mrr_arr_churn``)
sit alongside them and need no DB.
"""

from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.vertical_modules.economy.recurring import (
    _add_months,
    _as_money,
    _finago_ref,
    _parse_period,
    do_compute_recognition_schedule,
    do_recognize_recurring,
    do_snapshot_mrr_arr_churn,
)

# ---------------------------------------------------------------------------
# Pure-logic tests: the coercion boundary (no DB)
# ---------------------------------------------------------------------------


class TestAsMoney:
    def test_int_becomes_exact_decimal(self) -> None:
        assert _as_money(1000, "x") == Decimal(1000)

    def test_decimal_passes_through(self) -> None:
        assert _as_money(Decimal("42.50"), "x") == Decimal("42.50")

    def test_float_goes_through_str_not_binary_expansion(self) -> None:
        """``Decimal(str(0.1))`` == ``Decimal("0.1")``, never the binary-float
        expansion ``Decimal(0.1)`` would capture."""
        assert _as_money(0.1, "x") == Decimal("0.1")

    def test_bool_is_rejected_even_though_isinstance_int_is_true(self) -> None:
        """``isinstance(True, int)`` is ``True`` in Python — bool must be
        rejected explicitly, before the int branch."""
        with pytest.raises(ValueError, match="bool is not a money amount"):
            _as_money(True, "x")
        with pytest.raises(ValueError, match="bool is not a money amount"):
            _as_money(False, "x")

    def test_nan_float_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be finite"):
            _as_money(float("nan"), "x")

    def test_infinite_float_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be finite"):
            _as_money(float("inf"), "x")

    def test_nan_decimal_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be finite"):
            _as_money(Decimal("nan"), "x")

    def test_string_amount_is_never_parsed(self) -> None:
        with pytest.raises(ValueError, match="expected int/float/Decimal"):
            _as_money("1000", "x")

    def test_none_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="a money amount is required"):
            _as_money(None, "x")

    def test_third_decimal_quantises_ties_away_from_zero(self) -> None:
        result = _as_money(Decimal("100.005"), "x")
        assert result == Decimal("100.01")
        assert result.as_tuple().exponent == -2

    def test_third_decimal_rounds_down_below_half(self) -> None:
        result = _as_money(Decimal("100.004"), "x")
        assert result == Decimal("100.00")

    def test_negative_third_decimal_ties_away_from_zero(self) -> None:
        result = _as_money(Decimal("-100.005"), "x")
        assert result == Decimal("-100.01")


class TestParsePeriod:
    def test_valid_period(self) -> None:
        assert _parse_period("2026-01", "x") == (2026, 1)

    def test_valid_december(self) -> None:
        assert _parse_period("2026-12", "x") == (2026, 12)

    def test_unpadded_month_rejected(self) -> None:
        with pytest.raises(ValueError, match="expected 'YYYY-MM'"):
            _parse_period("2026-1", "x")

    def test_month_zero_rejected(self) -> None:
        with pytest.raises(ValueError, match="expected 'YYYY-MM'"):
            _parse_period("2026-00", "x")

    def test_month_thirteen_rejected(self) -> None:
        with pytest.raises(ValueError, match="expected 'YYYY-MM'"):
            _parse_period("2026-13", "x")

    def test_non_string_rejected(self) -> None:
        with pytest.raises(ValueError, match="expected a 'YYYY-MM' string"):
            _parse_period(202601, "x")

    def test_none_rejected(self) -> None:
        with pytest.raises(ValueError, match="expected a 'YYYY-MM' string"):
            _parse_period(None, "x")


class TestAddMonths:
    def test_zero_months_is_identity(self) -> None:
        assert _add_months(2026, 5, 0) == "2026-05"

    def test_rolls_into_next_year(self) -> None:
        assert _add_months(2026, 11, 2) == "2027-01"

    def test_full_year_returns_to_same_month_next_year(self) -> None:
        assert _add_months(2026, 3, 12) == "2027-03"

    def test_eleven_months_from_january(self) -> None:
        assert _add_months(2026, 1, 11) == "2026-12"


class TestFinagoRef:
    def test_format(self) -> None:
        assert _finago_ref("CONTRACT-1", "2026-01") == "ms:CONTRACT-1:2026-01"


# ---------------------------------------------------------------------------
# Pure-logic tests: do_compute_recognition_schedule (ratable 1/12)
# ---------------------------------------------------------------------------


class TestDoComputeRecognitionSchedule:
    def test_twelve_periods_sum_exactly_to_annual_amount(self) -> None:
        """The wave's binding requirement: no rounding remainder is ever
        dropped -- removing the exact-subtraction residual (see recurring.py's
        module docstring) would make this test fail."""
        result = do_compute_recognition_schedule(
            {"contract_id": "C1", "annual_amount": Decimal("10000.00"), "start_period": "2026-01"}
        )
        assert len(result["periods"]) == 12
        total = sum((p["amount"] for p in result["periods"]), Decimal("0.00"))
        assert total == Decimal("10000.00")
        assert result["total_recognized"] == Decimal("10000.00")

    def test_remainder_lands_on_final_period(self) -> None:
        """Pinned, deliberate choice: the first 11 periods share the same
        quantised base; the 12th absorbs whatever is left over."""
        result = do_compute_recognition_schedule(
            {"contract_id": "C1", "annual_amount": Decimal("10000.00"), "start_period": "2026-01"}
        )
        base = result["periods"][0]["amount"]
        assert all(p["amount"] == base for p in result["periods"][:11])
        assert result["periods"][11]["amount"] != base
        assert result["periods"][11]["amount"] == Decimal("10000.00") - (base * 11)

    def test_evenly_divisible_amount_has_no_remainder(self) -> None:
        result = do_compute_recognition_schedule(
            {"contract_id": "C1", "annual_amount": Decimal("1200.00"), "start_period": "2026-01"}
        )
        assert all(p["amount"] == Decimal("100.00") for p in result["periods"])

    def test_start_period_rolls_over_year_boundary(self) -> None:
        result = do_compute_recognition_schedule(
            {"contract_id": "C1", "annual_amount": Decimal("1200.00"), "start_period": "2026-11"}
        )
        periods = [p["period"] for p in result["periods"]]
        assert periods[0] == "2026-11"
        assert periods[1] == "2026-12"
        assert periods[2] == "2027-01"
        assert periods[-1] == "2027-10"
        assert len(set(periods)) == 12

    def test_finago_ref_format_per_period(self) -> None:
        result = do_compute_recognition_schedule(
            {"contract_id": "C1", "annual_amount": Decimal("1200.00"), "start_period": "2026-01"}
        )
        assert result["periods"][0]["finago_ref"] == "ms:C1:2026-01"

    def test_zero_annual_amount_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must be > 0"):
            do_compute_recognition_schedule(
                {"contract_id": "C1", "annual_amount": Decimal("0.00"), "start_period": "2026-01"}
            )

    def test_negative_annual_amount_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must be > 0"):
            do_compute_recognition_schedule(
                {
                    "contract_id": "C1",
                    "annual_amount": Decimal("-100.00"),
                    "start_period": "2026-01",
                }
            )

    def test_missing_contract_id_is_refused(self) -> None:
        with pytest.raises(ValueError, match="'contract_id' is required"):
            do_compute_recognition_schedule(
                {"annual_amount": Decimal("1200.00"), "start_period": "2026-01"}
            )


class TestDoComputeRecognitionScheduleNegativeResidualGuard:
    """Round-2 fix-forward (Batch 124 audit REJECT, MEDIUM defect): the final
    period's exact-subtraction residual can go negative for a sufficiently
    small ``annual_amount`` -- the twelve periods still sum EXACTLY to
    ``annual_amount`` in that case (the sum invariant alone does not catch
    it), but the final period in isolation is negative money. See
    recurring.py's module docstring "Edge case" note. This is refused
    outright, never redistributed."""

    def test_pinned_negative_case_from_the_audit_is_refused(self) -> None:
        """The audit's own repro: annual_amount=0.06 -> annual/12=0.005, an
        exact half-øre tie -> ROUND_HALF_UP -> base=0.01 for periods 1-11;
        11*0.01=0.11 > 0.06, so period 12's exact subtraction is
        0.06-0.11=-0.05 -- negative."""
        with pytest.raises(ValueError, match="refusing to recognise a negative period amount"):
            do_compute_recognition_schedule(
                {"contract_id": "C1", "annual_amount": Decimal("0.06"), "start_period": "2026-01"}
            )

    def test_boundary_just_below_the_sign_flip_is_refused(self) -> None:
        """annual_amount=0.54 -> base=0.05, 11*0.05=0.55 > 0.54 -> residual=-0.01."""
        with pytest.raises(ValueError, match="refusing to recognise a negative period amount"):
            do_compute_recognition_schedule(
                {"contract_id": "C1", "annual_amount": Decimal("0.54"), "start_period": "2026-01"}
            )

    def test_boundary_just_above_the_sign_flip_is_accepted(self) -> None:
        """annual_amount=0.55 -> base=0.05, 11*0.05=0.55, residual=0.00 --
        exactly zero (not negative), the schedule is accepted. This is the
        tightest boundary above which no amount ever goes negative again
        (verified by exhaustive scan up to NOK 1000 during fix-forward)."""
        result = do_compute_recognition_schedule(
            {"contract_id": "C1", "annual_amount": Decimal("0.55"), "start_period": "2026-01"}
        )
        assert len(result["periods"]) == 12
        assert all(p["amount"] >= Decimal("0.00") for p in result["periods"])
        total = sum((p["amount"] for p in result["periods"]), Decimal("0.00"))
        assert total == Decimal("0.55")

    def test_amount_below_the_audits_negative_band_is_accepted(self) -> None:
        """annual_amount=0.05 -> base=0.00 (annual/12 rounds down to zero for
        periods 1-11), residual=0.05 -- non-negative, accepted."""
        result = do_compute_recognition_schedule(
            {"contract_id": "C1", "annual_amount": Decimal("0.05"), "start_period": "2026-01"}
        )
        assert all(p["amount"] >= Decimal("0.00") for p in result["periods"])

    @pytest.mark.parametrize(
        "annual_amount", [Decimal("10000.00"), Decimal("1200.00"), Decimal("0.72")]
    )
    def test_realistic_amounts_still_produce_twelve_non_negative_periods(
        self, annual_amount: Decimal
    ) -> None:
        """The fix must not disturb the working path for realistic contract
        values -- eleven equal periods plus a non-negative absorbing
        residual, summing exactly to annual_amount."""
        result = do_compute_recognition_schedule(
            {"contract_id": "C1", "annual_amount": annual_amount, "start_period": "2026-01"}
        )
        assert len(result["periods"]) == 12
        assert all(p["amount"] >= Decimal("0.00") for p in result["periods"])
        total = sum((p["amount"] for p in result["periods"]), Decimal("0.00"))
        assert total == annual_amount


# ---------------------------------------------------------------------------
# Pure-logic tests: do_snapshot_mrr_arr_churn
# ---------------------------------------------------------------------------


class TestDoSnapshotMrrArrChurn:
    def test_empty_contracts_returns_zeroes(self) -> None:
        result = do_snapshot_mrr_arr_churn({"contracts": []})
        assert result["mrr"] == Decimal("0.00")
        assert result["arr"] == Decimal("0.00")
        assert result["churned_mrr"] == Decimal("0.00")
        assert result["churn_rate"] is None
        assert result["active_count"] == 0
        assert result["churned_count"] == 0

    def test_missing_contracts_key_returns_zeroes(self) -> None:
        result = do_snapshot_mrr_arr_churn({})
        assert result["mrr"] == Decimal("0.00")

    def test_mrr_arr_and_churn_rate(self) -> None:
        result = do_snapshot_mrr_arr_churn(
            {
                "contracts": [
                    {"annual_amount": Decimal("1200.00"), "status": "active"},
                    {"annual_amount": Decimal("1200.00"), "status": "active"},
                    {"annual_amount": Decimal("2400.00"), "status": "churned"},
                ]
            }
        )
        assert result["mrr"] == Decimal("200.00")
        assert result["arr"] == Decimal("2400.00")
        assert result["churned_mrr"] == Decimal("200.00")
        assert result["active_count"] == 2
        assert result["churned_count"] == 1
        assert result["churn_rate"] == Decimal("0.5")

    def test_invalid_status_is_refused(self) -> None:
        with pytest.raises(ValueError, match="status must be one of"):
            do_snapshot_mrr_arr_churn(
                {"contracts": [{"annual_amount": Decimal("1200.00"), "status": "pending"}]}
            )

    def test_non_list_contracts_is_refused(self) -> None:
        with pytest.raises(ValueError, match="'contracts' must be a list"):
            do_snapshot_mrr_arr_churn({"contracts": {"not": "a list"}})


# ---------------------------------------------------------------------------
# Integration test helpers
# ---------------------------------------------------------------------------


def _make_engine_stub(pg_pool: asyncpg.Pool) -> Any:  # type: ignore[type-arg]
    class _EngineStub:
        pass

    stub = _EngineStub()
    stub.pg_pool = pg_pool  # type: ignore[attr-defined]
    return stub


async def _count_idempotency_rows(
    pg_pool: asyncpg.Pool,
    ns_uuid: uuid.UUID,
    idempotency_key: str,
) -> int:
    async with pg_pool.acquire() as conn:
        count = await conn.fetchval(
            """
            SELECT COUNT(*) FROM action_idempotency
            WHERE namespace_id = $1::uuid AND idempotency_key = $2
            """,
            str(ns_uuid),
            idempotency_key,
        )
    return int(count)


async def _fetch_idempotency_row(
    pg_pool: asyncpg.Pool,
    ns_uuid: uuid.UUID,
    idempotency_key: str,
) -> asyncpg.Record | None:  # type: ignore[type-arg]
    async with pg_pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT idempotency_key, action_type, target_entity_id, response_hash
            FROM action_idempotency
            WHERE namespace_id = $1::uuid AND idempotency_key = $2
            """,
            str(ns_uuid),
            idempotency_key,
        )


async def _seed_foreign_idempotency_row(
    pg_pool: asyncpg.Pool,
    ns_uuid: uuid.UUID,
    idempotency_key: str,
) -> None:
    """Simulate an unrelated feature (a different action_type) having already
    claimed this exact idempotency_key string in this namespace — proves
    action_idempotency's documented limit (see recurring.py's module
    docstring): its primary key is not scoped by action_type."""
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO action_idempotency (idempotency_key, namespace_id, action_type, target_entity_id)
            VALUES ($1, $2::uuid, 'some_other_features_action', 'foreign-entity')
            ON CONFLICT (namespace_id, idempotency_key) DO NOTHING
            """,
            idempotency_key,
            str(ns_uuid),
        )


# ---------------------------------------------------------------------------
# 1. Ratable recognition, proven end-to-end across all twelve periods
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_twelve_recognitions_sum_exactly_to_annual_amount(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    contract_id = f"B124-T1-{uuid.uuid4().hex[:8]}"
    engine = _make_engine_stub(pg_pool)
    annual_amount = Decimal("10000.00")
    start_period = "2026-01"

    schedule = do_compute_recognition_schedule(
        {"contract_id": contract_id, "annual_amount": annual_amount, "start_period": start_period}
    )
    periods = [p["period"] for p in schedule["periods"]]
    assert len(periods) == 12
    assert len(set(periods)) == 12

    total_recognized = Decimal("0.00")
    for period in periods:
        result = await do_recognize_recurring(
            engine,
            {
                "namespace_id": namespace_id,
                "period": period,
                "contracts": [
                    {
                        "contract_id": contract_id,
                        "annual_amount": annual_amount,
                        "start_period": start_period,
                        "status": "active",
                    }
                ],
            },
        )
        assert len(result["recognized"]) == 1, result
        assert result["already_recognized"] == []
        assert result["not_due"] == []
        total_recognized += result["recognized"][0]["amount"]

    assert total_recognized == annual_amount, (
        f"twelve monthly recognitions summed to {total_recognized}, not the annual amount "
        f"{annual_amount} -- a rounding remainder was dropped"
    )


# ---------------------------------------------------------------------------
# 2. Idempotency — exact replay is a no-op
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_same_period_is_idempotent_no_op(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Removing the ON CONFLICT DO NOTHING guard in recurring.py's
    _record_recognition would make this test fail: the second call would
    insert a second action_idempotency row and report the contract in
    `recognized` a second time."""
    contract_id = f"B124-T2-{uuid.uuid4().hex[:8]}"
    engine = _make_engine_stub(pg_pool)
    params = {
        "namespace_id": namespace_id,
        "period": "2026-03",
        "contracts": [
            {
                "contract_id": contract_id,
                "annual_amount": Decimal("1200.00"),
                "start_period": "2026-01",
                "status": "active",
            }
        ],
    }

    r1 = await do_recognize_recurring(engine, params)
    assert len(r1["recognized"]) == 1
    assert r1["already_recognized"] == []
    finago_ref = r1["recognized"][0]["finago_ref"]

    r2 = await do_recognize_recurring(engine, params)
    assert r2["recognized"] == [], "replay must not re-recognise"
    assert len(r2["already_recognized"]) == 1
    assert r2["already_recognized"][0]["finago_ref"] == finago_ref

    count = await _count_idempotency_rows(pg_pool, namespace_id, finago_ref)
    assert count == 1, "replay must not insert a second row"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_with_different_annual_amount_is_refused(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Batch 120's lesson (cascade.py): a replay guard correct for the
    exact-match case can be silently wrong for every other. Here a replay
    under the SAME finagoRef with a DIFFERENT annual_amount (and therefore a
    different recognised amount) must be refused, not silently applied."""
    contract_id = f"B124-T3-{uuid.uuid4().hex[:8]}"
    engine = _make_engine_stub(pg_pool)
    original_params = {
        "namespace_id": namespace_id,
        "period": "2026-01",
        "contracts": [
            {
                "contract_id": contract_id,
                "annual_amount": Decimal("1200.00"),
                "start_period": "2026-01",
                "status": "active",
            }
        ],
    }
    r1 = await do_recognize_recurring(engine, original_params)
    finago_ref = r1["recognized"][0]["finago_ref"]
    original_amount = r1["recognized"][0]["amount"]

    changed_params = {
        "namespace_id": namespace_id,
        "period": "2026-01",
        "contracts": [
            {
                "contract_id": contract_id,
                "annual_amount": Decimal("2400.00"),
                "start_period": "2026-01",
                "status": "active",
            }
        ],
    }
    with pytest.raises(ValueError, match="refusing to treat this call as a safe replay"):
        await do_recognize_recurring(engine, changed_params)

    row = await _fetch_idempotency_row(pg_pool, namespace_id, finago_ref)
    assert row is not None
    expected_digest = hashlib.sha256(str(original_amount).encode("utf-8")).digest()
    assert bytes(row["response_hash"]) == expected_digest, (
        "the original recognition must be unchanged"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cross_action_type_collision_is_refused_not_silently_replayed(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Documents action_idempotency's limit (recurring.py's module docstring,
    'Idempotency — structural guarantee and its limits'): its primary key is
    (namespace_id, idempotency_key), not scoped by action_type. A foreign
    feature's row with a colliding idempotency_key string must not be
    silently treated as a valid same-amount replay -- it degrades to the same
    loud ValueError refusal a real different-amount replay gets."""
    contract_id = f"B124-T4-{uuid.uuid4().hex[:8]}"
    period = "2026-05"
    finago_ref = f"ms:{contract_id}:{period}"
    await _seed_foreign_idempotency_row(pg_pool, namespace_id, finago_ref)
    engine = _make_engine_stub(pg_pool)

    with pytest.raises(ValueError, match="refusing to treat this call as a safe replay"):
        await do_recognize_recurring(
            engine,
            {
                "namespace_id": namespace_id,
                "period": period,
                "contracts": [
                    {
                        "contract_id": contract_id,
                        "annual_amount": Decimal("1200.00"),
                        "start_period": "2026-01",
                        "status": "active",
                    }
                ],
            },
        )


# ---------------------------------------------------------------------------
# 3. A contract outside its 12-month window is skipped, not an error
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_not_due_contract_is_skipped_without_writing_anything(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    contract_id = f"B124-T5-{uuid.uuid4().hex[:8]}"
    engine = _make_engine_stub(pg_pool)

    result = await do_recognize_recurring(
        engine,
        {
            "namespace_id": namespace_id,
            "period": "2030-01",
            "contracts": [
                {
                    "contract_id": contract_id,
                    "annual_amount": Decimal("1200.00"),
                    "start_period": "2026-01",
                    "status": "active",
                }
            ],
        },
    )
    assert result["recognized"] == []
    assert result["already_recognized"] == []
    assert result["not_due"] == [contract_id]

    finago_ref = f"ms:{contract_id}:2030-01"
    count = await _count_idempotency_rows(pg_pool, namespace_id, finago_ref)
    assert count == 0


# ---------------------------------------------------------------------------
# 4. MRR/ARR/churn snapshot is returned alongside recognition
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mrr_snapshot_matches_direct_computation(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    engine = _make_engine_stub(pg_pool)
    contracts = [
        {
            "contract_id": f"B124-T6A-{uuid.uuid4().hex[:8]}",
            "annual_amount": Decimal("1200.00"),
            "start_period": "2026-01",
            "status": "active",
        },
        {
            "contract_id": f"B124-T6B-{uuid.uuid4().hex[:8]}",
            "annual_amount": Decimal("2400.00"),
            "start_period": "2026-01",
            "status": "churned",
        },
    ]
    result = await do_recognize_recurring(
        engine, {"namespace_id": namespace_id, "period": "2026-01", "contracts": contracts}
    )
    direct = do_snapshot_mrr_arr_churn(
        {
            "contracts": [
                {"annual_amount": c["annual_amount"], "status": c["status"]} for c in contracts
            ]
        }
    )
    assert result["mrr_snapshot"] == direct
    assert direct["mrr"] == Decimal("100.00")
    assert direct["arr"] == Decimal("1200.00")
    assert direct["churned_mrr"] == Decimal("200.00")
    assert direct["active_count"] == 1
    assert direct["churned_count"] == 1


# ---------------------------------------------------------------------------
# 5. FORCE RLS isolates action_idempotency per tenant
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rls_isolates_action_idempotency_rows_between_namespaces(
    pg_pool: asyncpg.Pool,
    pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    contract_id = f"B124-T7-{uuid.uuid4().hex[:8]}"
    engine = _make_engine_stub(pg_pool)

    await do_recognize_recurring(
        engine,
        {
            "namespace_id": ns_a,
            "period": "2026-01",
            "contracts": [
                {
                    "contract_id": contract_id,
                    "annual_amount": Decimal("1200.00"),
                    "start_period": "2026-01",
                    "status": "active",
                }
            ],
        },
    )
    finago_ref = f"ms:{contract_id}:2026-01"

    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_b)
        visible_from_b = await pg_app_conn.fetchval(
            "SELECT COUNT(*) FROM action_idempotency WHERE idempotency_key = $1",
            finago_ref,
        )
    assert visible_from_b == 0, "ns_b must not see ns_a's idempotency row"

    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        visible_from_a = await pg_app_conn.fetchval(
            "SELECT COUNT(*) FROM action_idempotency WHERE idempotency_key = $1",
            finago_ref,
        )
    assert visible_from_a == 1, "ns_a must see its own idempotency row"
