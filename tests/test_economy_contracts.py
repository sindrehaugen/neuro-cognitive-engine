"""Tests for economy/contracts.py — Wave 10 (contracts-renewal).

Validates the Acceptance criteria from Batch_125_Module_8_Wave_10.md:

  1. The CPI cap (5%, or a contract's own smaller cap) is enforced: a
     proposed uplift AT the cap is accepted, ABOVE it is refused (never
     clamped), and a missing/negative/non-finite proposal is refused too.
     ``TestValidateCpiUplift`` pins the boundary — removing the
     ``proposed > cpi_cap`` check in ``_validate_cpi_uplift`` (or changing
     it to clamp instead of raise) fails ``test_exceeding_the_cap_is_refused``.
  2. Renewals are flagged at 90 days: ``do_scan_renewals`` includes a
     contract exactly 90 days out and excludes one 91 days out.
  3. ``economy_contracts`` is RLS-isolated (FORCE ROW LEVEL SECURITY),
     proven via a real ``nce_app`` connection (``pg_app_conn``), not the
     superuser pool — an owner-connection test would prove nothing (Batch 32).
  4. The Wave-9 metadata shim is fully retired: ``NamespaceEconomyConfig``
     closes the ``extra="forbid"`` trap (an ``economy`` key no longer breaks
     subsequent ``update_metadata`` calls), and
     ``fetch_contracts_for_recognition`` round-trips a row written by
     ``do_upsert_contract`` into the exact shape
     ``do_recognize_recurring`` (recurring.py) expects.

Integration tests are ``@pytest.mark.integration`` — require a live Postgres
with migration 049 applied. Pure-logic tests for the coercion boundary, the
CPI-cap validator, and the renewal-window boundary sit alongside them and
need no DB.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.models import (
    ManageNamespaceCommand,
    ManageNamespaceRequest,
    NamespaceEconomyConfig,
    NamespaceMetadata,
    NamespaceMetadataPatch,
)
from nce.orchestrators.namespace import NamespaceOrchestrator
from nce.vertical_modules.economy.contracts import (
    _as_date,
    _as_fraction,
    _as_money,
    _is_due_for_renewal,
    _parse_period,
    _quantise_cpi_cap,
    _validate_cpi_uplift,
    do_scan_renewals,
    do_upsert_contract,
    do_validate_contract,
    fetch_contracts_for_recognition,
)

# ---------------------------------------------------------------------------
# Pure-logic tests: the coercion boundary (no DB)
# ---------------------------------------------------------------------------


class TestAsMoney:
    def test_decimal_passes_through(self) -> None:
        assert _as_money(Decimal("1200.00"), "x") == Decimal("1200.00")

    def test_bool_is_rejected_even_though_isinstance_int_is_true(self) -> None:
        with pytest.raises(ValueError, match="bool is not a money amount"):
            _as_money(True, "x")

    def test_nan_float_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be finite"):
            _as_money(float("nan"), "x")

    def test_none_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="a money amount is required"):
            _as_money(None, "x")

    def test_quantises_to_oere(self) -> None:
        assert _as_money(Decimal("100.005"), "x") == Decimal("100.01")


class TestAsFraction:
    def test_decimal_passes_through(self) -> None:
        assert _as_fraction(Decimal("0.05"), "x") == Decimal("0.05")

    def test_int_zero_is_accepted(self) -> None:
        assert _as_fraction(0, "x") == Decimal("0")

    def test_bool_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="bool is not a valid fraction"):
            _as_fraction(True, "x")

    def test_none_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="a value is required"):
            _as_fraction(None, "x")

    def test_negative_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be >= 0"):
            _as_fraction(Decimal("-0.01"), "x")

    def test_nan_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be finite"):
            _as_fraction(float("nan"), "x")

    def test_infinite_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be finite"):
            _as_fraction(float("inf"), "x")

    def test_string_is_never_parsed(self) -> None:
        with pytest.raises(ValueError, match="expected int/float/Decimal"):
            _as_fraction("0.05", "x")


class TestQuantiseCpiCap:
    """Round-2 Fix 2: economy_contracts.cpi_cap is NUMERIC(5,4) — an
    unquantised Decimal must be rounded by THIS code, not silently rounded by
    Postgres on the way in (Batch 120's defect class). See
    ``_as_fraction`` — deliberately NOT quantised there, since that helper is
    also used for ``proposed_cpi_pct``, which is never stored."""

    def test_more_than_four_decimals_is_rounded_to_the_column_scale(self) -> None:
        assert _quantise_cpi_cap(Decimal("0.033333"), "x") == Decimal("0.0333")

    def test_exactly_four_decimals_is_unchanged(self) -> None:
        assert _quantise_cpi_cap(Decimal("0.05"), "x") == Decimal("0.0500")

    def test_ties_round_half_up(self) -> None:
        assert _quantise_cpi_cap(Decimal("0.033350"), "x") == Decimal("0.0334")


class TestParsePeriod:
    def test_valid_period_is_normalised(self) -> None:
        assert _parse_period("2026-01", "x") == "2026-01"

    def test_unpadded_month_rejected(self) -> None:
        with pytest.raises(ValueError, match="expected 'YYYY-MM'"):
            _parse_period("2026-1", "x")

    def test_none_rejected(self) -> None:
        with pytest.raises(ValueError, match="expected a 'YYYY-MM' string"):
            _parse_period(None, "x")


class TestAsDate:
    def test_iso_string(self) -> None:
        assert _as_date("2026-06-01", "x") == date(2026, 6, 1)

    def test_date_passthrough(self) -> None:
        assert _as_date(date(2026, 6, 1), "x") == date(2026, 6, 1)

    def test_datetime_is_reduced_to_date(self) -> None:
        assert _as_date(datetime(2026, 6, 1, 12, 30, tzinfo=timezone.utc), "x") == date(2026, 6, 1)

    def test_malformed_string_rejected(self) -> None:
        with pytest.raises(ValueError, match="expected an ISO 'YYYY-MM-DD' date"):
            _as_date("06/01/2026", "x")

    def test_wrong_type_rejected(self) -> None:
        with pytest.raises(ValueError, match="expected a date/datetime/ISO string"):
            _as_date(20260601, "x")


# ---------------------------------------------------------------------------
# Pure-logic tests: _validate_cpi_uplift — THE CPI cap (Acceptance #1)
# ---------------------------------------------------------------------------


class TestValidateCpiUplift:
    """Pins the CPI-cap boundary. See contracts.py's module docstring "The
    CPI cap is a money ceiling" section for the full rationale."""

    def test_exactly_at_the_cap_is_accepted(self) -> None:
        """The boundary is INCLUSIVE — "a 5% cap" means up to and including
        5%, not strictly less than 5%."""
        result = _validate_cpi_uplift(Decimal("0.05"), Decimal("0.05"))
        assert result == Decimal("0.05")

    def test_exceeding_the_cap_is_refused(self) -> None:
        """The pinned test: removing the ``proposed > cpi_cap`` check (or
        changing it to clamp the value down to the cap instead of raising)
        makes this test fail — it asserts a ValueError is raised, not that
        the return value equals the cap."""
        with pytest.raises(ValueError, match="exceeds this contract's cap"):
            _validate_cpi_uplift(Decimal("0.05"), Decimal("0.0501"))

    def test_a_smaller_per_contract_cap_is_also_enforced(self) -> None:
        """The cap is PER CONTRACT, not just the global 5% ceiling — a
        contract with a stricter cap (e.g. 3%) refuses a 4% proposal even
        though 4% is under the global ceiling."""
        with pytest.raises(ValueError, match="exceeds this contract's cap"):
            _validate_cpi_uplift(Decimal("0.03"), Decimal("0.04"))

    def test_exactly_at_a_smaller_cap_is_accepted(self) -> None:
        result = _validate_cpi_uplift(Decimal("0.03"), Decimal("0.03"))
        assert result == Decimal("0.03")

    def test_zero_uplift_is_accepted(self) -> None:
        """No increase this period is a valid, common case."""
        result = _validate_cpi_uplift(Decimal("0.05"), Decimal("0"))
        assert result == Decimal("0")

    def test_negative_uplift_is_refused(self) -> None:
        """Fail toward refusal: this validator's job is to validate a
        proposed INCREASE. A negative figure is not one, and this module has
        no documented business rule for how a negative CPI reading should
        affect a contract (floor-at-zero vs. pass-through) — refuse rather
        than silently pick a policy."""
        with pytest.raises(ValueError, match="must be >= 0"):
            _validate_cpi_uplift(Decimal("0.05"), Decimal("-0.01"))

    def test_missing_uplift_is_refused(self) -> None:
        with pytest.raises(ValueError, match="a value is required"):
            _validate_cpi_uplift(Decimal("0.05"), None)

    def test_nan_uplift_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must be finite"):
            _validate_cpi_uplift(Decimal("0.05"), float("nan"))

    def test_infinite_uplift_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must be finite"):
            _validate_cpi_uplift(Decimal("0.05"), float("inf"))

    def test_non_numeric_uplift_is_refused(self) -> None:
        with pytest.raises(ValueError, match="expected int/float/Decimal"):
            _validate_cpi_uplift(Decimal("0.05"), "5%")


# ---------------------------------------------------------------------------
# Pure-logic tests: _is_due_for_renewal — the 90-day boundary (Acceptance #2)
# ---------------------------------------------------------------------------


class TestIsDueForRenewal:
    def test_exactly_ninety_days_out_is_due(self) -> None:
        as_of = date(2026, 1, 1)
        assert _is_due_for_renewal(as_of + timedelta(days=90), as_of, 90) is True

    def test_ninety_one_days_out_is_not_due(self) -> None:
        as_of = date(2026, 1, 1)
        assert _is_due_for_renewal(as_of + timedelta(days=91), as_of, 90) is False

    def test_eighty_nine_days_out_is_due(self) -> None:
        as_of = date(2026, 1, 1)
        assert _is_due_for_renewal(as_of + timedelta(days=89), as_of, 90) is True

    def test_today_is_due(self) -> None:
        as_of = date(2026, 1, 1)
        assert _is_due_for_renewal(as_of, as_of, 90) is True

    def test_already_past_due_is_due(self) -> None:
        """A lapsed renewal is at least as urgent as one 90 days out — never
        excluded from the scan."""
        as_of = date(2026, 1, 1)
        assert _is_due_for_renewal(as_of - timedelta(days=10), as_of, 90) is True


# ---------------------------------------------------------------------------
# Pure-logic test: the Wave-9 metadata trap is closed (Acceptance #4, part 1)
# ---------------------------------------------------------------------------


class TestNamespaceEconomyConfigClosesTheMetadataTrap:
    def test_economy_key_is_now_a_recognised_field(self) -> None:
        """Before this wave, NamespaceMetadata had no 'economy' field
        (extra='forbid') — this would have raised a pydantic ValidationError."""
        meta = NamespaceMetadata(economy=NamespaceEconomyConfig(enabled=True))
        assert meta.economy.enabled is True

    def test_merged_metadata_revalidation_survives_the_clean_enabled_only_shape(self) -> None:
        """Reproduces nce/orchestrators/namespace.py's
        _update_namespace_metadata's merge shape (`old_meta.update(patch);
        validated = NamespaceMetadata(**old_meta)`) for the CLEAN shape — a
        namespace whose metadata already carries an 'economy' key with only
        'enabled' set. This narrower shape is NOT the full round-2 regression
        (see test_legacy_recurring_contracts_shim_key_is_stripped_before_validation
        and the real-orchestrator integration test below for the shim shape
        that actually broke)."""
        old_meta: dict[str, Any] = {"economy": {"enabled": True}}
        patch = NamespaceMetadataPatch(temporal_retention_days=30)
        old_meta.update(patch.model_dump(exclude_none=True))

        validated = NamespaceMetadata(**old_meta)

        assert validated.economy.enabled is True
        assert validated.temporal_retention_days == 30

    def test_legacy_recurring_contracts_shim_key_is_stripped_before_validation(self) -> None:
        """Round-2 fix: the DOCUMENTED Wave-9 shim shape — contracts.py's own
        module docstring's ``{"enabled": true, "recurring_contracts": [...]}``,
        not the narrower ``{"enabled": true}`` the previous version of this
        test used — must also survive re-validation.

        Before the round-2 fix, this raised: ``NamespaceEconomyConfig`` was
        ``extra="forbid"`` with no knowledge of the retired
        ``recurring_contracts`` sub-key, so ANY namespace whose stored
        metadata still carried it (written before this table existed) would
        fail every future ``update_metadata`` call, for entirely unrelated
        fields. ``_drop_legacy_wave9_shim_keys`` (a ``model_validator(mode=
        "before")`` on ``NamespaceEconomyConfig``) strips exactly that one
        named legacy key — not a blanket loosening of ``extra="forbid"``, see
        ``test_unknown_economy_key_other_than_recurring_contracts_still_forbidden``.
        """
        old_meta: dict[str, Any] = {
            "economy": {"enabled": True, "recurring_contracts": []},
        }
        patch = NamespaceMetadataPatch(temporal_retention_days=30)
        old_meta.update(patch.model_dump(exclude_none=True))

        validated = NamespaceMetadata(**old_meta)

        assert validated.economy.enabled is True
        assert "recurring_contracts" not in validated.economy.model_dump()
        assert validated.temporal_retention_days == 30

    def test_unknown_economy_key_other_than_recurring_contracts_still_forbidden(self) -> None:
        """The legacy-key amnesty is narrow: any OTHER unrecognised key under
        ``economy`` still raises via ``extra="forbid"`` — this is a named
        exception for one retired field, not a loosened guard."""
        with pytest.raises(Exception, match="extra_forbidden|Extra inputs"):
            NamespaceEconomyConfig(enabled=True, some_other_unknown_key="x")

    def test_patch_can_also_set_economy_enabled(self) -> None:
        patch = NamespaceMetadataPatch(economy=NamespaceEconomyConfig(enabled=True))
        old_meta: dict[str, Any] = {}
        old_meta.update(patch.model_dump(exclude_none=True))
        validated = NamespaceMetadata(**old_meta)
        assert validated.economy.enabled is True

    def test_default_metadata_has_economy_disabled(self) -> None:
        assert NamespaceMetadata().economy.enabled is False


# ---------------------------------------------------------------------------
# Pure-logic tests: validation-before-DB paths on the async write functions.
# Each of these raises inside do_upsert_contract / do_scan_renewals BEFORE
# the function ever opens a scoped_pg_session — so no live Postgres is
# needed, and these are plain (non-integration) async tests, not DB-fixture
# tests, per this repo's "DB-dependent tests are @pytest.mark.integration;
# pure-logic tests are plain unit tests" convention.
# ---------------------------------------------------------------------------


class _DummyEngine:
    """Stands in for NCEEngine in tests that never reach a DB call — the
    validation under test raises before ``engine.pg_pool`` is ever touched."""

    pg_pool = None


@pytest.mark.asyncio
async def test_upsert_contract_refuses_annual_amount_not_greater_than_zero() -> None:
    with pytest.raises(ValueError, match="must be > 0"):
        await do_upsert_contract(
            _DummyEngine(),
            {
                "namespace_id": uuid.uuid4(),
                "contract_id": "does-not-matter",
                "status": "active",
                "annual_amount": Decimal("0.00"),
                "start_period": "2026-01",
                "next_renewal_date": "2026-09-01",
            },
        )


@pytest.mark.asyncio
async def test_upsert_contract_refuses_cpi_cap_above_the_global_ceiling() -> None:
    """Application-level defense in depth — the same 5% ceiling the DB CHECK
    constraint enforces structurally (see
    test_cpi_cap_check_constraint_rejects_row_above_ceiling below)."""
    with pytest.raises(ValueError, match="exceeds the global ceiling"):
        await do_upsert_contract(
            _DummyEngine(),
            {
                "namespace_id": uuid.uuid4(),
                "contract_id": "does-not-matter",
                "status": "active",
                "annual_amount": Decimal("12000.00"),
                "start_period": "2026-01",
                "next_renewal_date": "2026-09-01",
                "cpi_cap": Decimal("0.06"),
            },
        )


@pytest.mark.asyncio
async def test_upsert_contract_refuses_invalid_status() -> None:
    with pytest.raises(ValueError, match="'status' must be one of"):
        await do_upsert_contract(
            _DummyEngine(),
            {
                "namespace_id": uuid.uuid4(),
                "contract_id": "does-not-matter",
                "status": "pending",
                "annual_amount": Decimal("12000.00"),
                "start_period": "2026-01",
                "next_renewal_date": "2026-09-01",
            },
        )


@pytest.mark.asyncio
async def test_scan_renewals_rejects_non_positive_window() -> None:
    with pytest.raises(ValueError, match="must be a positive int"):
        await do_scan_renewals(_DummyEngine(), {"namespace_id": uuid.uuid4(), "window_days": 0})


@pytest.mark.asyncio
async def test_scan_renewals_rejects_bool_window() -> None:
    """``isinstance(True, int)`` is ``True`` in Python — window_days=True
    must not silently pass as window_days=1."""
    with pytest.raises(ValueError, match="must be a positive int"):
        await do_scan_renewals(_DummyEngine(), {"namespace_id": uuid.uuid4(), "window_days": True})


# ---------------------------------------------------------------------------
# Integration test helpers
# ---------------------------------------------------------------------------


def _make_engine_stub(pg_pool: asyncpg.Pool) -> Any:  # type: ignore[type-arg]
    class _EngineStub:
        pass

    stub = _EngineStub()
    stub.pg_pool = pg_pool  # type: ignore[attr-defined]
    return stub


def _contract_id(tag: str) -> str:
    return f"B125-{tag}-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# 1. do_upsert_contract — create + idempotent update (write path)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_contract_creates_row(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    engine = _make_engine_stub(pg_pool)
    contract_id = _contract_id("T1")

    result = await do_upsert_contract(
        engine,
        {
            "namespace_id": namespace_id,
            "contract_id": contract_id,
            "status": "active",
            "annual_amount": Decimal("12000.00"),
            "start_period": "2026-01",
            "next_renewal_date": "2026-09-01",
        },
    )

    assert result["ok"] is True
    assert result["contract_id"] == contract_id
    assert result["status"] == "active"
    assert result["annual_amount"] == Decimal("12000.00")
    assert result["cpi_cap"] == Decimal("0.0500")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_contract_is_a_live_update_not_a_second_row(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """A contract is a mutable record (unlike economy_postings' append-only
    ledger lines) — a second upsert against the same natural key UPDATES the
    row in place, it never creates a second one."""
    engine = _make_engine_stub(pg_pool)
    contract_id = _contract_id("T2")
    base_params = {
        "namespace_id": namespace_id,
        "contract_id": contract_id,
        "status": "active",
        "annual_amount": Decimal("12000.00"),
        "start_period": "2026-01",
        "next_renewal_date": "2026-09-01",
    }
    await do_upsert_contract(engine, base_params)
    updated = await do_upsert_contract(engine, {**base_params, "status": "churned"})
    assert updated["status"] == "churned"

    async with pg_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM economy_contracts WHERE namespace_id = $1::uuid "
            "AND contract_id = $2",
            str(namespace_id),
            contract_id,
        )
    assert count == 1


# ---------------------------------------------------------------------------
# 2. The CPI-cap ceiling is a DB-level structural guarantee, not just a guard
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cpi_cap_check_constraint_rejects_row_above_ceiling(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Bypasses contracts.py entirely (raw SQL) to prove the CHECK
    constraint itself is the backstop — not just the Python-level guard in
    do_upsert_contract."""
    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO economy_contracts
                    (namespace_id, contract_id, status, annual_amount, start_period,
                     cpi_cap, next_renewal_date)
                VALUES ($1::uuid, $2, 'active', 1200.00, '2026-01', 0.06, '2026-09-01')
                """,
                str(namespace_id),
                _contract_id("T5"),
            )


# ---------------------------------------------------------------------------
# 3. do_validate_contract — CPI cap enforced end-to-end (Acceptance #1)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_do_validate_contract_computes_renewal_quote_within_cap(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    engine = _make_engine_stub(pg_pool)
    contract_id = _contract_id("T6")
    await do_upsert_contract(
        engine,
        {
            "namespace_id": namespace_id,
            "contract_id": contract_id,
            "status": "active",
            "annual_amount": Decimal("10000.00"),
            "start_period": "2026-01",
            "next_renewal_date": "2026-09-01",
            "cpi_cap": Decimal("0.05"),
        },
    )

    result = await do_validate_contract(
        engine,
        {
            "namespace_id": namespace_id,
            "contract_id": contract_id,
            "proposed_cpi_pct": Decimal("0.05"),
        },
    )

    assert result["ok"] is True
    assert result["proposed_cpi_pct"] == Decimal("0.05")
    assert result["current_annual_amount"] == Decimal("10000.00")
    assert result["renewal_annual_amount"] == Decimal("10500.00")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_do_validate_contract_refuses_proposal_exceeding_this_contracts_cap(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    engine = _make_engine_stub(pg_pool)
    contract_id = _contract_id("T7")
    await do_upsert_contract(
        engine,
        {
            "namespace_id": namespace_id,
            "contract_id": contract_id,
            "status": "active",
            "annual_amount": Decimal("10000.00"),
            "start_period": "2026-01",
            "next_renewal_date": "2026-09-01",
            "cpi_cap": Decimal("0.03"),
        },
    )

    with pytest.raises(ValueError, match="exceeds this contract's cap"):
        await do_validate_contract(
            engine,
            {
                "namespace_id": namespace_id,
                "contract_id": contract_id,
                "proposed_cpi_pct": Decimal("0.05"),
            },
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_do_validate_contract_missing_contract_is_refused(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    engine = _make_engine_stub(pg_pool)
    with pytest.raises(ValueError, match="no contract"):
        await do_validate_contract(
            engine,
            {
                "namespace_id": namespace_id,
                "contract_id": "does-not-exist",
                "proposed_cpi_pct": Decimal("0.05"),
            },
        )


# ---------------------------------------------------------------------------
# 4. do_scan_renewals — 90-day flagging (Acceptance #2)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_do_scan_renewals_flags_within_window_and_excludes_beyond(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    engine = _make_engine_stub(pg_pool)
    as_of = date(2026, 1, 1)
    due_soon = _contract_id("T8A")
    due_later = _contract_id("T8B")

    await do_upsert_contract(
        engine,
        {
            "namespace_id": namespace_id,
            "contract_id": due_soon,
            "status": "active",
            "annual_amount": Decimal("1200.00"),
            "start_period": "2026-01",
            "next_renewal_date": (as_of + timedelta(days=90)).isoformat(),
        },
    )
    await do_upsert_contract(
        engine,
        {
            "namespace_id": namespace_id,
            "contract_id": due_later,
            "status": "active",
            "annual_amount": Decimal("1200.00"),
            "start_period": "2026-01",
            "next_renewal_date": (as_of + timedelta(days=91)).isoformat(),
        },
    )

    result = await do_scan_renewals(
        engine, {"namespace_id": namespace_id, "as_of_date": as_of.isoformat()}
    )

    due_ids = {entry["contract_id"] for entry in result["due"]}
    assert due_soon in due_ids
    assert due_later not in due_ids
    assert result["window_days"] == 90


@pytest.mark.integration
@pytest.mark.asyncio
async def test_do_scan_renewals_excludes_churned_contracts(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    engine = _make_engine_stub(pg_pool)
    as_of = date(2026, 1, 1)
    churned_id = _contract_id("T9")

    await do_upsert_contract(
        engine,
        {
            "namespace_id": namespace_id,
            "contract_id": churned_id,
            "status": "churned",
            "annual_amount": Decimal("1200.00"),
            "start_period": "2026-01",
            "next_renewal_date": as_of.isoformat(),
        },
    )

    result = await do_scan_renewals(
        engine, {"namespace_id": namespace_id, "as_of_date": as_of.isoformat()}
    )

    assert churned_id not in {entry["contract_id"] for entry in result["due"]}


# ---------------------------------------------------------------------------
# 5. fetch_contracts_for_recognition — the retired shim's replacement
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fetch_contracts_for_recognition_shapes_rows_for_do_recognize_recurring(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """End-to-end proof the shim retirement works: a row written by
    do_upsert_contract (this file) round-trips through
    fetch_contracts_for_recognition into the EXACT shape
    do_recognize_recurring (recurring.py, Wave 9) expects, and recognition
    succeeds using it — all without either module importing the other."""
    from nce.vertical_modules.economy.recurring import do_recognize_recurring

    engine = _make_engine_stub(pg_pool)
    contract_id = _contract_id("T10")
    await do_upsert_contract(
        engine,
        {
            "namespace_id": namespace_id,
            "contract_id": contract_id,
            "status": "active",
            "annual_amount": Decimal("1200.00"),
            "start_period": "2026-01",
            "next_renewal_date": "2026-09-01",
        },
    )

    contracts = await fetch_contracts_for_recognition(engine, namespace_id)
    fetched = next(c for c in contracts if c["contract_id"] == contract_id)
    assert fetched == {
        "contract_id": contract_id,
        "annual_amount": Decimal("1200.00"),
        "start_period": "2026-01",
        "status": "active",
    }

    result = await do_recognize_recurring(
        engine,
        {"namespace_id": namespace_id, "period": "2026-01", "contracts": contracts},
    )
    recognized_ids = {r["contract_id"] for r in result["recognized"]}
    assert contract_id in recognized_ids


# ---------------------------------------------------------------------------
# 6. FORCE RLS isolates economy_contracts per tenant (Acceptance #3)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rls_isolates_economy_contracts_between_namespaces(
    pg_pool: asyncpg.Pool,
    pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    """Uses pg_app_conn (the nce_app role), not the superuser pool — an
    owner-connection test would prove nothing against FORCE RLS
    (Batch 32's false-confidence lesson)."""
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    engine = _make_engine_stub(pg_pool)
    contract_id = _contract_id("T11")

    await do_upsert_contract(
        engine,
        {
            "namespace_id": ns_a,
            "contract_id": contract_id,
            "status": "active",
            "annual_amount": Decimal("1200.00"),
            "start_period": "2026-01",
            "next_renewal_date": "2026-09-01",
        },
    )

    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_b)
        visible_from_b = await pg_app_conn.fetchval(
            "SELECT COUNT(*) FROM economy_contracts WHERE contract_id = $1",
            contract_id,
        )
    assert visible_from_b == 0, "ns_b must not see ns_a's contract row"

    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        visible_from_a = await pg_app_conn.fetchval(
            "SELECT COUNT(*) FROM economy_contracts WHERE contract_id = $1",
            contract_id,
        )
    assert visible_from_a == 1, "ns_a must see its own contract row"


# ---------------------------------------------------------------------------
# 7. Round-2 Fix 1: the documented Wave-9 shim shape survives the REAL
# NamespaceOrchestrator._update_namespace_metadata (Acceptance #4, part 2)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_namespace_metadata_survives_the_documented_wave9_shim_shape(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Reproduces the round-2 audit finding exactly: a namespace whose
    STORED metadata already carries the FULL documented Wave-9 shim shape —
    ``{"economy": {"enabled": true, "recurring_contracts": []}}``, contracts.py's
    own module docstring's shape, NOT the narrower ``{"enabled": true}`` the
    previous version of this test's sibling used — must still be able to
    complete a subsequent, entirely UNRELATED ``update_metadata`` call.

    Drives the REAL orchestrator path
    (``NamespaceOrchestrator.manage_namespace`` -> the private
    ``_update_namespace_metadata``), not a reimplementation of its
    merge-and-revalidate logic — a test that re-derives the code path it is
    meant to protect cannot detect a divergence in that path. Before the
    round-2 fix, this raised ``pydantic.ValidationError`` on
    ``economy.recurring_contracts``.
    """
    # Simulate the pre-existing legacy state: a namespace whose metadata was
    # written (e.g. via raw SQL, before NamespaceEconomyConfig existed) with
    # the Wave-9 shim's full documented shape, including the sub-key the real
    # economy_contracts table (migration 049) has since retired.
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE namespaces SET metadata = $1::jsonb WHERE id = $2",
            json.dumps({"economy": {"enabled": True, "recurring_contracts": []}}),
            namespace_id,
        )

    orchestrator = NamespaceOrchestrator(pg_pool)
    payload = ManageNamespaceRequest(
        command=ManageNamespaceCommand.update_metadata,
        namespace_id=namespace_id,
        metadata_patch=NamespaceMetadataPatch(temporal_retention_days=30),
    )

    result = await orchestrator.manage_namespace(payload, admin_identity="pytest-fix1")

    assert result["status"] == "ok"
    assert result["metadata"]["economy"]["enabled"] is True
    assert "recurring_contracts" not in result["metadata"]["economy"]
    assert result["metadata"]["temporal_retention_days"] == 30


# ---------------------------------------------------------------------------
# 8. Round-2 Fix 2: cpi_cap is quantised to the column scale by THIS code,
# not silently rounded by Postgres.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_contract_quantises_cpi_cap_to_the_column_scale(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """A >4-decimal cpi_cap must be quantised by do_upsert_contract BEFORE
    it is bound to the NUMERIC(5,4) column — the returned value AND the
    stored value must both equal the quantised value, not the raw input.
    Before the round-2 fix, Postgres silently rounded the unquantised
    Decimal on the way in with no error (Batch 120's defect class)."""
    engine = _make_engine_stub(pg_pool)
    contract_id = _contract_id("T12")

    result = await do_upsert_contract(
        engine,
        {
            "namespace_id": namespace_id,
            "contract_id": contract_id,
            "status": "active",
            "annual_amount": Decimal("12000.00"),
            "start_period": "2026-01",
            "next_renewal_date": "2026-09-01",
            "cpi_cap": Decimal("0.033333"),
        },
    )

    assert result["cpi_cap"] == Decimal("0.0333")

    async with pg_pool.acquire() as conn:
        stored_cpi_cap = await conn.fetchval(
            "SELECT cpi_cap FROM economy_contracts WHERE namespace_id = $1::uuid "
            "AND contract_id = $2",
            str(namespace_id),
            contract_id,
        )
    assert stored_cpi_cap == Decimal("0.0333")


# ---------------------------------------------------------------------------
# 9. Round-2 Fix 3: a compensating event_log record on the ON CONFLICT DO
# UPDATE branch — the contract master record's own change history.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_contract_update_writes_a_compensating_event_log_entry(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Before the round-2 fix, updating annual_amount left ZERO event_log
    rows — the prior value was unrecoverable the instant the UPDATE
    committed. do_upsert_contract's ON CONFLICT DO UPDATE branch now appends
    one config_changed event capturing old -> new for annual_amount, cpi_cap,
    status, and next_renewal_date, in the SAME transaction as the update.

    A fresh INSERT (the first upsert below) must NOT write this event — there
    is no prior value to protect."""
    engine = _make_engine_stub(pg_pool)
    contract_id = _contract_id("T13")
    base_params = {
        "namespace_id": namespace_id,
        "contract_id": contract_id,
        "status": "active",
        "annual_amount": Decimal("12000.00"),
        "start_period": "2026-01",
        "next_renewal_date": "2026-09-01",
        "cpi_cap": Decimal("0.05"),
    }

    await do_upsert_contract(engine, base_params)

    async with pg_pool.acquire() as conn:
        count_after_insert = await conn.fetchval(
            "SELECT COUNT(*) FROM event_log WHERE namespace_id = $1 "
            "AND event_type = 'config_changed' "
            "AND params->'changes'->>'contract_id' = $2",
            namespace_id,
            contract_id,
        )
    assert count_after_insert == 0, "a fresh INSERT must not write a compensating event"

    await do_upsert_contract(
        engine, {**base_params, "annual_amount": Decimal("15000.00"), "status": "churned"}
    )

    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT params FROM event_log WHERE namespace_id = $1 "
            "AND event_type = 'config_changed' "
            "AND params->'changes'->>'contract_id' = $2",
            namespace_id,
            contract_id,
        )
    assert len(rows) == 1, "exactly one compensating event for the one UPDATE"
    payload = (
        json.loads(rows[0]["params"]) if isinstance(rows[0]["params"], str) else rows[0]["params"]
    )
    changes = payload["changes"]
    assert changes["annual_amount"] == {"old": "12000.00", "new": "15000.00"}
    assert changes["status"] == {"old": "active", "new": "churned"}
