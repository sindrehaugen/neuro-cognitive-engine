"""
tests/test_economy_finago.py
==============================
Tests for ``nce/vertical_modules/economy/finago.py`` (Module 8, Wave 8 --
finago-reconcile).

Two halves, mirroring ``test_economy_postings.py``'s own convention:

* Plain (unmarked) unit tests for the pure Finago-reader validation logic
  (money coercion, payload shape, materiality arithmetic, threshold parsing)
  plus the read-only HTTP reader itself (mocked via
  ``unittest.mock.patch`` on ``request_with_retry``, the same pattern
  ``tests/unit/test_system_design_sharepoint.py`` uses -- no real network
  calls, no DB). These run in the always-on unit job.

* ``@pytest.mark.integration`` tests for ``do_reconcile_gl`` /
  ``do_gl_sync_status`` -- both touch Postgres (``economy_postings`` reads,
  ``divergence_log`` writes via the shared C5 ``record_divergence``) and
  require the ``pg_pool`` / ``make_namespace`` fixtures.

Covers the wave's Acceptance line: "GL reconciliation logs divergence via C5
with the Finago=legal truth-rule; coverage reported."
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.source_mode import divergence
from nce.vertical_modules.economy.finago import (
    ENGINE_KEY,
    FinagoGLReadError,
    _as_money,
    _empty_coverage,
    _materiality,
    _materiality_threshold,
    _quantise,
    _validate_gl_payload,
    do_gl_sync_status,
    do_reconcile_gl,
    fetch_gl_period,
)

_FAKE_URL = "https://finago.example.invalid/api"
_FAKE_TOKEN = "FAKE_FINAGO_TOKEN_NEVER_IN_LOGS"  # test fixture, not a real secret


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_response(body: Any) -> MagicMock:
    """Minimal mock httpx.Response -- mirrors
    tests/unit/test_system_design_sharepoint.py's ``_make_mock_response``."""
    resp = MagicMock()
    resp.status_code = 200
    resp.is_success = True
    resp.json = MagicMock(return_value=body)
    return resp


def _make_engine_stub(pg_pool: Any) -> Any:
    """Mirrors test_economy_cascade.py's ``_make_engine_stub``."""

    class _EngineStub:
        pass

    stub = _EngineStub()
    stub.pg_pool = pg_pool  # type: ignore[attr-defined]
    return stub


# ---------------------------------------------------------------------------
# Pure-logic tests: money coercion (_as_money / _quantise)
# ---------------------------------------------------------------------------


class TestAsMoney:
    def test_string_amount_accepted(self) -> None:
        assert _as_money("1500.00", "x") == Decimal("1500.00")

    def test_int_amount_accepted(self) -> None:
        assert _as_money(1500, "x") == Decimal(1500)

    def test_decimal_amount_passes_through(self) -> None:
        d = Decimal("42.50")
        assert _as_money(d, "x") is d

    def test_negative_string_amount_accepted(self) -> None:
        assert _as_money("-1500.00", "x") == Decimal("-1500.00")

    def test_bool_is_rejected_even_though_bool_is_an_int_subclass(self) -> None:
        """isinstance(True, int) is True in Python -- bool must be refused
        BEFORE the int branch, not silently coerced to 1/0."""
        with pytest.raises(FinagoGLReadError, match="must be numeric"):
            _as_money(True, "x")
        with pytest.raises(FinagoGLReadError, match="must be numeric"):
            _as_money(False, "x")

    def test_raw_json_float_is_rejected_not_coerced(self) -> None:
        """Money must never be coerced through float -- Finago must send a
        string (or bare int), never a raw JSON number that parsed as float."""
        with pytest.raises(FinagoGLReadError, match="raw JSON float"):
            _as_money(1500.5, "x")

    def test_non_numeric_string_rejected(self) -> None:
        with pytest.raises(FinagoGLReadError, match="not a valid decimal"):
            _as_money("not-a-number", "x")

    def test_nan_string_rejected(self) -> None:
        with pytest.raises(FinagoGLReadError, match="not finite"):
            _as_money("NaN", "x")

    def test_infinity_string_rejected(self) -> None:
        with pytest.raises(FinagoGLReadError, match="not finite"):
            _as_money("Infinity", "x")

    def test_unsupported_type_rejected(self) -> None:
        with pytest.raises(FinagoGLReadError, match="must be a string, int, or Decimal"):
            _as_money(["not", "money"], "x")


class TestQuantise:
    def test_exact_ore_amount_unaffected(self) -> None:
        result = _quantise(Decimal("42.50"), "x")
        assert result == Decimal("42.50")
        assert result.as_tuple().exponent == -2

    def test_third_decimal_ties_away_from_zero(self) -> None:
        assert _quantise(Decimal("100.005"), "x") == Decimal("100.01")

    def test_negative_third_decimal_ties_away_from_zero(self) -> None:
        assert _quantise(Decimal("-100.005"), "x") == Decimal("-100.01")

    def test_overflow_raises_finago_gl_read_error(self) -> None:
        huge = Decimal("1E+9999")
        with pytest.raises(FinagoGLReadError, match="too large to express in øre"):
            _quantise(huge, "x")


# ---------------------------------------------------------------------------
# Pure-logic tests: materiality arithmetic + threshold parsing
# ---------------------------------------------------------------------------


class TestMateriality:
    def test_zero_delta_is_zero_materiality(self) -> None:
        assert _materiality(Decimal("100.00"), Decimal("100.00")) == 0.0

    def test_positive_delta_direction(self) -> None:
        """NCE above Finago (nce=105, finago=100 -> +5 raw delta).

        denom = max(105, 100, floor=1) = 105 -> materiality = 5 / 105.
        """
        assert _materiality(Decimal("105.00"), Decimal("100.00")) == pytest.approx(5 / 105)

    def test_negative_delta_direction_is_symmetric_with_positive(self) -> None:
        """NCE below Finago (nce=100, finago=105 -> -5 raw delta) must score
        the SAME materiality as the positive-delta case above -- magnitude
        only, same denom (max(100, 105, floor=1) = 105)."""
        assert _materiality(Decimal("100.00"), Decimal("105.00")) == pytest.approx(5 / 105)

    def test_tiny_amounts_use_the_materiality_floor(self) -> None:
        """A one-øre difference between two tiny accounts must not read as
        100% materiality -- the floor (1.00) is the denominator, not the
        (near-zero) amounts themselves."""
        result = _materiality(Decimal("0.02"), Decimal("0.01"))
        assert result == pytest.approx(0.01)  # 0.01 / max(0.02, 0.01, 1) = 0.01

    def test_one_side_zero_scores_full_materiality(self) -> None:
        assert _materiality(Decimal("0.00"), Decimal("500.00")) == pytest.approx(1.0)


class TestMaterialityThreshold:
    def test_default_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NCE_DIVERGENCE_ALERT_THRESHOLD", raising=False)
        assert _materiality_threshold() == pytest.approx(0.1)

    def test_reads_env_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NCE_DIVERGENCE_ALERT_THRESHOLD", "0.25")
        assert _materiality_threshold() == pytest.approx(0.25)

    def test_invalid_env_value_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NCE_DIVERGENCE_ALERT_THRESHOLD", "not-a-float")
        assert _materiality_threshold() == pytest.approx(0.1)


class TestMaterialityThresholdIsTheSharedDivergenceAccessor:
    """Round-2 fix: an adversarial audit found ``_materiality_threshold`` was
    a BYTE-FOR-BYTE reimplementation of
    ``nce.source_mode.divergence``'s alert-threshold parse -- same env var,
    same 0.1 default, same parse-and-warn fallback -- and that retuning
    ONLY ``divergence.py``'s default constant (an entirely ordinary future
    edit) silently desynchronised alert-paging from this module's own
    ``material: true/false`` classification while the always-on unit suite
    (1197 tests) stayed green throughout, because each file's own test only
    pinned its own hardcoded literal.

    These tests assert ``_materiality_threshold`` no longer merely *agrees*
    with ``divergence.alert_threshold`` -- it *is* that function, so the two
    are structurally incapable of drifting apart again.
    """

    def test_is_the_same_function_object_as_divergence_alert_threshold(self) -> None:
        """Identity, not mere behavioural agreement today -- this is what
        makes a future default-drift structurally impossible rather than
        merely improbable-until-someone-forgets."""
        assert _materiality_threshold is divergence.alert_threshold

    def test_unset_default_matches_the_shared_accessor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NCE_DIVERGENCE_ALERT_THRESHOLD", raising=False)
        assert _materiality_threshold() == divergence.alert_threshold() == pytest.approx(0.1)

    def test_explicit_env_value_matches_the_shared_accessor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NCE_DIVERGENCE_ALERT_THRESHOLD", "0.37")
        assert _materiality_threshold() == divergence.alert_threshold() == pytest.approx(0.37)

    def test_malformed_env_value_matches_the_shared_accessor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NCE_DIVERGENCE_ALERT_THRESHOLD", "not-a-float")
        assert _materiality_threshold() == divergence.alert_threshold() == pytest.approx(0.1)

    def test_a_default_only_drift_in_divergence_py_is_caught(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """This is the exact mutation the round-1 audit proved slipped past
        the entire unit suite (1197 passed / 0 failed): change ONLY
        ``divergence.py``'s default constant, leave ``finago.py`` untouched.
        Under the old (reimplemented) ``_materiality_threshold`` this
        assertion would FAIL -- finago stayed pinned to its own,
        independently hardcoded 0.1. Under the shared-accessor fix it must
        move WITH ``divergence.py``, because it is the same function."""
        monkeypatch.delenv("NCE_DIVERGENCE_ALERT_THRESHOLD", raising=False)
        monkeypatch.setattr(divergence, "_DEFAULT_ALERT_THRESHOLD", 0.15)
        assert _materiality_threshold() == pytest.approx(0.15)


# ---------------------------------------------------------------------------
# Pure-logic tests: Finago payload validation (_validate_gl_payload)
# ---------------------------------------------------------------------------


class TestValidateGlPayload:
    def test_valid_payload_returns_balances(self) -> None:
        payload = {
            "period": "2026-08",
            "accounts": [
                {"account": "4300", "amount": "1500.00"},
                {"account": "2400", "amount": "-1500.00"},
            ],
        }
        result = _validate_gl_payload(payload, "2026-08")
        assert result == {"4300": Decimal("1500.00"), "2400": Decimal("-1500.00")}

    def test_empty_accounts_list_is_a_legitimate_empty_book(self) -> None:
        """An explicit [] means 'nothing posted' -- a valid answer, not malformed."""
        result = _validate_gl_payload({"accounts": []}, "2026-08")
        assert result == {}

    def test_non_dict_payload_raises(self) -> None:
        with pytest.raises(FinagoGLReadError, match="must be an object"):
            _validate_gl_payload(["not", "a", "dict"], "2026-08")

    def test_missing_accounts_key_raises_not_treated_as_empty(self) -> None:
        with pytest.raises(FinagoGLReadError, match="must be a list"):
            _validate_gl_payload({"period": "2026-08"}, "2026-08")

    def test_null_accounts_raises_not_treated_as_empty(self) -> None:
        with pytest.raises(FinagoGLReadError, match="must be a list"):
            _validate_gl_payload({"accounts": None}, "2026-08")

    def test_accounts_not_a_list_raises(self) -> None:
        with pytest.raises(FinagoGLReadError, match="must be a list"):
            _validate_gl_payload({"accounts": {"4300": "1500.00"}}, "2026-08")

    def test_entry_not_a_dict_raises(self) -> None:
        with pytest.raises(FinagoGLReadError, match=r"accounts\[0\] must be an object"):
            _validate_gl_payload({"accounts": ["4300"]}, "2026-08")

    def test_missing_account_field_raises(self) -> None:
        with pytest.raises(FinagoGLReadError, match="account must be a non-empty string"):
            _validate_gl_payload({"accounts": [{"amount": "100.00"}]}, "2026-08")

    def test_empty_account_field_raises(self) -> None:
        with pytest.raises(FinagoGLReadError, match="account must be a non-empty string"):
            _validate_gl_payload({"accounts": [{"account": "  ", "amount": "100.00"}]}, "2026-08")

    def test_missing_amount_field_raises_not_treated_as_zero(self) -> None:
        """The core refuse-not-guess invariant: an absent amount must never
        silently become 0.00 -- it must fail loud."""
        with pytest.raises(FinagoGLReadError, match="amount is missing"):
            _validate_gl_payload({"accounts": [{"account": "4300"}]}, "2026-08")

    def test_null_amount_field_raises_not_treated_as_zero(self) -> None:
        with pytest.raises(FinagoGLReadError, match="amount is missing"):
            _validate_gl_payload({"accounts": [{"account": "4300", "amount": None}]}, "2026-08")

    def test_period_mismatch_raises(self) -> None:
        payload = {"period": "2026-07", "accounts": []}
        with pytest.raises(FinagoGLReadError, match="period mismatch"):
            _validate_gl_payload(payload, "2026-08")

    def test_absent_period_field_is_not_a_mismatch(self) -> None:
        """`period` is optional metadata -- its absence is not itself malformed."""
        result = _validate_gl_payload({"accounts": []}, "2026-08")
        assert result == {}

    def test_float_amount_in_payload_rejected(self) -> None:
        payload = {"accounts": [{"account": "4300", "amount": 1500.5}]}
        with pytest.raises(FinagoGLReadError, match="raw JSON float"):
            _validate_gl_payload(payload, "2026-08")


# ---------------------------------------------------------------------------
# Pure-logic / mocked-HTTP tests: fetch_gl_period (no DB, no real network)
# ---------------------------------------------------------------------------


class TestFetchGlPeriod:
    @pytest.mark.asyncio
    async def test_noop_when_unconfigured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NCE_ECONOMY_FINAGO_URL", raising=False)

        with patch(
            "nce.vertical_modules.economy.finago.request_with_retry",
            new=AsyncMock(),
        ) as mock_req:
            result = await fetch_gl_period("2026-08")

        assert result is None
        mock_req.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fetches_and_validates_when_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NCE_ECONOMY_FINAGO_URL", _FAKE_URL)
        monkeypatch.setenv("NCE_ECONOMY_FINAGO_TOKEN", _FAKE_TOKEN)

        response = _make_mock_response(
            {
                "period": "2026-08",
                "accounts": [{"account": "4300", "amount": "1500.00"}],
            }
        )

        with patch(
            "nce.vertical_modules.economy.finago.request_with_retry",
            new=AsyncMock(return_value=response),
        ) as mock_req:
            result = await fetch_gl_period("2026-08")

        assert result == {"4300": Decimal("1500.00")}
        mock_req.assert_awaited_once()
        assert mock_req.call_args.args[1] == "GET"
        assert "2026-08" in mock_req.call_args.args[2]

    @pytest.mark.asyncio
    async def test_malformed_response_propagates_finago_gl_read_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NCE_ECONOMY_FINAGO_URL", _FAKE_URL)

        response = _make_mock_response({"period": "2026-08"})  # missing 'accounts'

        with patch(
            "nce.vertical_modules.economy.finago.request_with_retry",
            new=AsyncMock(return_value=response),
        ):
            with pytest.raises(FinagoGLReadError, match="must be a list"):
                await fetch_gl_period("2026-08")

    @pytest.mark.asyncio
    async def test_empty_period_id_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="period_id"):
            await fetch_gl_period("")

    @pytest.mark.asyncio
    async def test_token_never_appears_in_log_output(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        monkeypatch.setenv("NCE_ECONOMY_FINAGO_URL", _FAKE_URL)
        monkeypatch.setenv("NCE_ECONOMY_FINAGO_TOKEN", _FAKE_TOKEN)

        response = _make_mock_response({"accounts": []})

        with caplog.at_level(logging.DEBUG, logger="nce.vertical_modules.economy.finago"):
            with patch(
                "nce.vertical_modules.economy.finago.request_with_retry",
                new=AsyncMock(return_value=response),
            ):
                await fetch_gl_period("2026-08")

        full_log = "\n".join(caplog.messages)
        assert _FAKE_TOKEN not in full_log


# ---------------------------------------------------------------------------
# Pure validation tests: do_reconcile_gl / do_gl_sync_status argument checks
# (raise before any I/O -- no DB required)
# ---------------------------------------------------------------------------


class TestDoReconcileGlValidation:
    @pytest.mark.asyncio
    async def test_missing_namespace_id_raises(self) -> None:
        with pytest.raises(ValueError, match="namespace_id"):
            await do_reconcile_gl(_make_engine_stub(None), {"period_id": "2026-08"})

    @pytest.mark.asyncio
    async def test_missing_period_id_raises(self) -> None:
        with pytest.raises(ValueError, match="period_id"):
            await do_reconcile_gl(_make_engine_stub(None), {"namespace_id": str(uuid.uuid4())})


class TestDoGlSyncStatusValidation:
    @pytest.mark.asyncio
    async def test_missing_namespace_id_raises(self) -> None:
        with pytest.raises(ValueError, match="namespace_id"):
            await do_gl_sync_status(_make_engine_stub(None), {})

    @pytest.mark.asyncio
    async def test_non_positive_window_hours_raises(self) -> None:
        with pytest.raises(ValueError, match="window_hours"):
            await do_gl_sync_status(
                _make_engine_stub(None),
                {"namespace_id": str(uuid.uuid4()), "window_hours": 0},
            )

    @pytest.mark.asyncio
    async def test_non_numeric_window_hours_raises(self) -> None:
        with pytest.raises(ValueError, match="window_hours"):
            await do_gl_sync_status(
                _make_engine_stub(None),
                {"namespace_id": str(uuid.uuid4()), "window_hours": "not-a-number"},
            )


class TestEmptyCoverage:
    def test_shape(self) -> None:
        assert _empty_coverage() == {
            "accounts_checked": 0,
            "accounts_matched": 0,
            "accounts_diverged": 0,
            "material_diverged": 0,
            "coverage_pct": 0.0,
        }


# ---------------------------------------------------------------------------
# Integration test helpers (DB-backed)
# ---------------------------------------------------------------------------


async def _seed_posting_event(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    ns_uuid: uuid.UUID,
    *,
    period_id: str,
    event_id: str,
    lines: list[tuple[str, Decimal]],
) -> None:
    """Insert one balanced event's postings directly (bypassing
    persist_financial_event -- this test only needs rows in economy_postings,
    not the graph/POSTING-node side effects). Each call's `lines` must sum
    to zero -- the storage-level trigger enforces this per statement."""
    from nce.auth import set_namespace_context

    accounts = [account for account, _ in lines]
    amounts = [amount for _, amount in lines]
    line_numbers = list(range(len(lines)))

    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, ns_uuid)
        await conn.execute(
            """
            INSERT INTO economy_postings
                (namespace_id, event_id, event_type, line_no, account, amount, period_id)
            SELECT $1::uuid, $2, 'test.finago_reconcile', u.line_no, u.account, u.amount, $6
            FROM unnest($3::int[], $4::text[], $5::numeric[]) AS u(line_no, account, amount)
            """,
            str(ns_uuid),
            event_id,
            line_numbers,
            accounts,
            amounts,
            period_id,
        )


def _mock_finago_response(accounts: list[tuple[str, str]], period_id: str | None = None) -> Any:
    body: dict[str, Any] = {"accounts": [{"account": a, "amount": amt} for a, amt in accounts]}
    if period_id is not None:
        body["period"] = period_id
    return _make_mock_response(body)


def _patch_finago(response: Any) -> Any:
    return patch(
        "nce.vertical_modules.economy.finago.request_with_retry",
        new=AsyncMock(return_value=response),
    )


async def _count_divergence_rows(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    ns_uuid: uuid.UUID,
) -> int:
    async with pg_pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*)::int FROM divergence_log WHERE namespace_id = $1 AND engine = $2",
            ns_uuid,
            ENGINE_KEY,
        )


# ---------------------------------------------------------------------------
# Integration: do_reconcile_gl logs a divergence via the C5 service
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestDoReconcileGlIntegration:
    async def test_divergence_logged_via_c5_with_truth_rule(
        self,
        pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A real mismatch between economy_postings and Finago's GL is
        recorded through nce.source_mode.divergence.record_divergence
        (the shared C5 service, ``engine='economy'``) -- not a bespoke
        table -- and the response encodes Finago=legal / NCE=operational
        as two distinct fields."""
        monkeypatch.setenv("NCE_ECONOMY_FINAGO_URL", _FAKE_URL)
        ns = await make_namespace()
        period_id = f"2026-08-{uuid.uuid4().hex[:6]}"

        # NCE's internal book: account 4300 = 1500.00 (balanced by 2400).
        await _seed_posting_event(
            pg_pool,
            ns,
            period_id=period_id,
            event_id=f"evt-{uuid.uuid4().hex}",
            lines=[("4300", Decimal("1500.00")), ("2400", Decimal("-1500.00"))],
        )

        # Finago's legal book disagrees: 4300 = 1400.00.
        response = _mock_finago_response(
            [("4300", "1400.00"), ("2400", "-1500.00")], period_id=period_id
        )

        with _patch_finago(response):
            result = await do_reconcile_gl(
                _make_engine_stub(pg_pool), {"namespace_id": ns, "period_id": period_id}
            )

        assert result["configured"] is True
        assert result["truth_rule"] == "finago_legal_nce_operational"

        div_by_account = {d["account"]: d for d in result["divergences"]}
        assert "2400" not in div_by_account, "matching accounts must not be reported as divergent"
        assert "4300" in div_by_account
        entry = div_by_account["4300"]
        assert entry["operational_value"] == "1500.00"  # NCE
        assert entry["legal_value"] == "1400.00"  # Finago
        assert entry["materiality"] == pytest.approx(100.00 / 1500.00)

        assert result["coverage"]["accounts_checked"] == 2
        assert result["coverage"]["accounts_matched"] == 1
        assert result["coverage"]["accounts_diverged"] == 1

        # Persisted via the SHARED C5 table, not a bespoke one.
        rows_written = await _count_divergence_rows(pg_pool, ns)
        assert rows_written == 1
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT entity, field, nce_value, ext_value, materiality "
                "FROM divergence_log WHERE namespace_id = $1 AND engine = $2",
                ns,
                ENGINE_KEY,
            )
        assert row is not None
        assert row["entity"] == f"gl_account:{period_id}:4300"
        assert row["field"] == "balance"
        assert row["nce_value"] == "1500.00"
        assert row["ext_value"] == "1400.00"

    async def test_matched_accounts_are_never_logged_as_divergences(
        self,
        pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A ZERO delta must not produce a divergence_log row at all."""
        monkeypatch.setenv("NCE_ECONOMY_FINAGO_URL", _FAKE_URL)
        ns = await make_namespace()
        period_id = f"2026-08-{uuid.uuid4().hex[:6]}"

        await _seed_posting_event(
            pg_pool,
            ns,
            period_id=period_id,
            event_id=f"evt-{uuid.uuid4().hex}",
            lines=[("4300", Decimal("1500.00")), ("2400", Decimal("-1500.00"))],
        )
        response = _mock_finago_response([("4300", "1500.00"), ("2400", "-1500.00")])

        with _patch_finago(response):
            result = await do_reconcile_gl(
                _make_engine_stub(pg_pool), {"namespace_id": ns, "period_id": period_id}
            )

        assert result["divergences"] == []
        assert result["coverage"]["accounts_matched"] == 2
        assert result["coverage"]["coverage_pct"] == 100.0
        assert await _count_divergence_rows(pg_pool, ns) == 0

    async def test_negative_delta_treated_symmetrically_with_positive(
        self,
        pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """NCE BELOW Finago (negative raw delta) must land the same
        materiality as the equivalent positive-delta case, and must still
        report operational_value/legal_value with the correct sides -- never
        swapped just because the sign flipped."""
        monkeypatch.setenv("NCE_ECONOMY_FINAGO_URL", _FAKE_URL)
        ns = await make_namespace()
        period_id = f"2026-08-{uuid.uuid4().hex[:6]}"

        # NCE = 1400.00 (below Finago's 1500.00).
        await _seed_posting_event(
            pg_pool,
            ns,
            period_id=period_id,
            event_id=f"evt-{uuid.uuid4().hex}",
            lines=[("4300", Decimal("1400.00")), ("2400", Decimal("-1400.00"))],
        )
        response = _mock_finago_response([("4300", "1500.00"), ("2400", "-1400.00")])

        with _patch_finago(response):
            result = await do_reconcile_gl(
                _make_engine_stub(pg_pool), {"namespace_id": ns, "period_id": period_id}
            )

        entry = next(d for d in result["divergences"] if d["account"] == "4300")
        assert entry["operational_value"] == "1400.00"
        assert entry["legal_value"] == "1500.00"
        assert entry["materiality"] == pytest.approx(100.00 / 1500.00)

    async def test_not_configured_returns_empty_result_without_db_comparison(
        self,
        pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Finago unconfigured is an honest no-op -- never a fabricated
        comparison against an absent GL, and never a logged divergence."""
        monkeypatch.delenv("NCE_ECONOMY_FINAGO_URL", raising=False)
        ns = await make_namespace()
        period_id = f"2026-08-{uuid.uuid4().hex[:6]}"

        await _seed_posting_event(
            pg_pool,
            ns,
            period_id=period_id,
            event_id=f"evt-{uuid.uuid4().hex}",
            lines=[("4300", Decimal("1500.00")), ("2400", Decimal("-1500.00"))],
        )

        result = await do_reconcile_gl(
            _make_engine_stub(pg_pool), {"namespace_id": ns, "period_id": period_id}
        )

        assert result["configured"] is False
        assert result["divergences"] == []
        assert result["coverage"] == _empty_coverage()
        assert await _count_divergence_rows(pg_pool, ns) == 0

    async def test_zero_accounts_both_sides_is_vacuous_full_coverage(
        self,
        pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A period with nothing posted anywhere is 100% coverage, not an
        error and not a divergence."""
        monkeypatch.setenv("NCE_ECONOMY_FINAGO_URL", _FAKE_URL)
        ns = await make_namespace()
        period_id = f"2026-08-{uuid.uuid4().hex[:6]}"

        response = _mock_finago_response([])

        with _patch_finago(response):
            result = await do_reconcile_gl(
                _make_engine_stub(pg_pool), {"namespace_id": ns, "period_id": period_id}
            )

        assert result["divergences"] == []
        assert result["coverage"]["accounts_checked"] == 0
        assert result["coverage"]["coverage_pct"] == 100.0

    async def test_namespace_isolation_explicit_filter(
        self,
        pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Reconciling ns_a must never see ns_b's postings for the same
        period_id -- namespace_id is filtered EXPLICITLY in SQL, not left to
        RLS alone (invariant 3)."""
        monkeypatch.setenv("NCE_ECONOMY_FINAGO_URL", _FAKE_URL)
        ns_a = await make_namespace()
        ns_b = await make_namespace()
        period_id = f"2026-08-{uuid.uuid4().hex[:6]}"

        await _seed_posting_event(
            pg_pool,
            ns_a,
            period_id=period_id,
            event_id=f"evt-a-{uuid.uuid4().hex}",
            lines=[("4300", Decimal("1500.00")), ("2400", Decimal("-1500.00"))],
        )
        await _seed_posting_event(
            pg_pool,
            ns_b,
            period_id=period_id,
            event_id=f"evt-b-{uuid.uuid4().hex}",
            lines=[("4300", Decimal("9999.00")), ("2400", Decimal("-9999.00"))],
        )

        # ns_a reconciles against a GL that agrees with ns_a's OWN book
        # (1500.00) -- if ns_b's rows leaked in, this would wrongly diverge.
        response = _mock_finago_response([("4300", "1500.00"), ("2400", "-1500.00")])

        with _patch_finago(response):
            result = await do_reconcile_gl(
                _make_engine_stub(pg_pool), {"namespace_id": ns_a, "period_id": period_id}
            )

        assert result["divergences"] == []
        assert result["coverage"]["accounts_matched"] == 2

    # -----------------------------------------------------------------
    # Materiality boundary -- exact pin required by this wave's
    # acceptance gate: inclusive/exclusive, and alert-dispatch agreement.
    # -----------------------------------------------------------------

    async def test_materiality_exactly_at_threshold_is_not_material(
        self,
        pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """materiality == threshold is the sub-threshold branch (strict
        `>`), matching record_divergence's own alert boundary exactly."""
        monkeypatch.setenv("NCE_ECONOMY_FINAGO_URL", _FAKE_URL)
        monkeypatch.setenv("NCE_DIVERGENCE_ALERT_THRESHOLD", "0.2")
        ns = await make_namespace()
        period_id = f"2026-08-{uuid.uuid4().hex[:6]}"

        # nce=80.00, finago=100.00 -> delta=20 / denom=max(80,100,1)=100 -> materiality = 0.2 exactly.
        await _seed_posting_event(
            pg_pool,
            ns,
            period_id=period_id,
            event_id=f"evt-{uuid.uuid4().hex}",
            lines=[("4300", Decimal("80.00")), ("2400", Decimal("-80.00"))],
        )
        response = _mock_finago_response([("4300", "100.00"), ("2400", "-80.00")])

        mock_dispatch = AsyncMock()
        with (
            patch("nce.source_mode.divergence.dispatcher.dispatch_alert", mock_dispatch),
            _patch_finago(response),
        ):
            result = await do_reconcile_gl(
                _make_engine_stub(pg_pool), {"namespace_id": ns, "period_id": period_id}
            )

        entry = next(d for d in result["divergences"] if d["account"] == "4300")
        assert entry["materiality"] == pytest.approx(0.2)
        assert entry["material"] is False
        assert result["coverage"]["material_diverged"] == 0
        mock_dispatch.assert_not_awaited()

    async def test_materiality_just_above_threshold_is_material_and_alerts(
        self,
        pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NCE_ECONOMY_FINAGO_URL", _FAKE_URL)
        monkeypatch.setenv("NCE_DIVERGENCE_ALERT_THRESHOLD", "0.2")
        ns = await make_namespace()
        period_id = f"2026-08-{uuid.uuid4().hex[:6]}"

        # nce=79.00, finago=100.00 -> delta=21 / denom=100 -> materiality = 0.21 > 0.2.
        await _seed_posting_event(
            pg_pool,
            ns,
            period_id=period_id,
            event_id=f"evt-{uuid.uuid4().hex}",
            lines=[("4300", Decimal("79.00")), ("2400", Decimal("-79.00"))],
        )
        response = _mock_finago_response([("4300", "100.00"), ("2400", "-79.00")])

        mock_dispatch = AsyncMock()
        with (
            patch("nce.source_mode.divergence.dispatcher.dispatch_alert", mock_dispatch),
            _patch_finago(response),
        ):
            result = await do_reconcile_gl(
                _make_engine_stub(pg_pool), {"namespace_id": ns, "period_id": period_id}
            )

        entry = next(d for d in result["divergences"] if d["account"] == "4300")
        assert entry["materiality"] == pytest.approx(0.21)
        assert entry["material"] is True
        assert result["coverage"]["material_diverged"] == 1
        mock_dispatch.assert_awaited_once()

    async def test_materiality_below_threshold_is_not_material(
        self,
        pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NCE_ECONOMY_FINAGO_URL", _FAKE_URL)
        monkeypatch.setenv("NCE_DIVERGENCE_ALERT_THRESHOLD", "0.2")
        ns = await make_namespace()
        period_id = f"2026-08-{uuid.uuid4().hex[:6]}"

        # nce=90.00, finago=100.00 -> delta=10 / denom=100 -> materiality = 0.1 < 0.2.
        await _seed_posting_event(
            pg_pool,
            ns,
            period_id=period_id,
            event_id=f"evt-{uuid.uuid4().hex}",
            lines=[("4300", Decimal("90.00")), ("2400", Decimal("-90.00"))],
        )
        response = _mock_finago_response([("4300", "100.00"), ("2400", "-90.00")])

        mock_dispatch = AsyncMock()
        with (
            patch("nce.source_mode.divergence.dispatcher.dispatch_alert", mock_dispatch),
            _patch_finago(response),
        ):
            result = await do_reconcile_gl(
                _make_engine_stub(pg_pool), {"namespace_id": ns, "period_id": period_id}
            )

        entry = next(d for d in result["divergences"] if d["account"] == "4300")
        assert entry["material"] is False
        mock_dispatch.assert_not_awaited()


# ---------------------------------------------------------------------------
# Integration: do_gl_sync_status
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestDoGlSyncStatusIntegration:
    async def test_clean_when_no_divergences_recorded(
        self,
        pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
        make_namespace: Any,
    ) -> None:
        ns = await make_namespace()
        result = await do_gl_sync_status(_make_engine_stub(pg_pool), {"namespace_id": ns})
        assert result["clean"] is True
        assert result["divergence_count"] == 0
        assert result["last_divergence_at"] is None

    async def test_reflects_recorded_divergence_within_window(
        self,
        pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NCE_ECONOMY_FINAGO_URL", _FAKE_URL)
        monkeypatch.setenv("NCE_DIVERGENCE_ALERT_THRESHOLD", "0.9")  # suppress alert noise
        ns = await make_namespace()
        period_id = f"2026-08-{uuid.uuid4().hex[:6]}"

        await _seed_posting_event(
            pg_pool,
            ns,
            period_id=period_id,
            event_id=f"evt-{uuid.uuid4().hex}",
            lines=[("4300", Decimal("1500.00")), ("2400", Decimal("-1500.00"))],
        )
        response = _mock_finago_response([("4300", "1400.00"), ("2400", "-1500.00")])

        with _patch_finago(response):
            await do_reconcile_gl(
                _make_engine_stub(pg_pool), {"namespace_id": ns, "period_id": period_id}
            )

        status = await do_gl_sync_status(_make_engine_stub(pg_pool), {"namespace_id": ns})
        assert status["clean"] is False
        assert status["divergence_count"] == 1
        assert status["last_divergence_at"] is not None

    async def test_window_hours_excludes_old_rows(
        self,
        pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
        make_namespace: Any,
    ) -> None:
        """Mirrors test_c5_donewhen.py's flip-window convention: a row older
        than the requested window must not count."""
        ns = await make_namespace()

        async with pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO divergence_log
                       (namespace_id, engine, entity, field, nce_value, ext_value,
                        materiality, detected_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, now() - INTERVAL '7200 seconds')
                """,
                ns,
                ENGINE_KEY,
                "gl_account:old:4300",
                "balance",
                "1.00",
                "2.00",
                0.5,
            )

        status = await do_gl_sync_status(
            _make_engine_stub(pg_pool), {"namespace_id": ns, "window_hours": 1.0}
        )
        assert status["divergence_count"] == 0
        assert status["clean"] is True
