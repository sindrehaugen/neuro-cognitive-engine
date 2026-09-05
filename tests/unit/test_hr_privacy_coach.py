"""
tests/unit/test_hr_privacy_coach.py
===================================
Unit tests for Module 13 (HR Engine) Phase 6:
  - RL-1: Hard-pinned NEVER-ranking refusal and control that can fail
  - RL-2: EU AI Act Article 5 compliance -- objective sustained overload monitoring
          (operational hours/utilization only, NO sentiment or emotional inference)
  - RL-3: GDPR PII erasure & redaction gate (Fodselsnummer Mod-11, bank, cards, phone, email)
  - Private agent memory scoping (hr_private_coach)
  - Access scoping (peer cannot access another employee's 1-on-1 / coach record)
  - Individual coaching skill-gap advisor
  - Identity scrub compliance (ALPHA/BETA fixtures, @example.test)
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nce.config import cfg
from nce.vertical_modules.hr import coaching, privacy
from nce.vertical_modules.hr._guard import HrRankingProhibitedError

_NS_A = "00000000-0000-4000-8000-000000000001"
_EMP_A = "EMP-ALPHA"
_EMP_B = "EMP-BETA"


class _AsyncCtx:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *args: Any) -> None:
        pass


def _make_engine(fetch_skills: Any = None, fetch_certs: Any = None) -> MagicMock:
    engine = MagicMock()
    conn = AsyncMock()
    conn.fetch.side_effect = [fetch_skills or [], fetch_certs or []]
    conn.transaction = MagicMock(return_value=_AsyncCtx(None))

    pool = MagicMock(spec=["acquire"])
    pool.acquire.return_value = _AsyncCtx(conn)
    engine.pg_pool = pool
    return engine


@pytest.fixture(autouse=True)
def _patch_scoped_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace scoped_pg_session with a pass-through for unit tests."""

    @asynccontextmanager
    async def _fake_scoped(pool: Any, ns: Any) -> Any:
        if hasattr(pool, "acquire"):
            ctx = pool.acquire()
            if hasattr(ctx, "__aenter__"):
                conn = await ctx.__aenter__()
                try:
                    yield conn
                finally:
                    await ctx.__aexit__(None, None, None)
            else:
                yield ctx
        else:
            yield pool

    monkeypatch.setattr("nce.vertical_modules.hr.coaching.scoped_pg_session", _fake_scoped)


# =========================================================================
# 1. RL-1: Hard-pinned NEVER Ranking & Refusal Controls (Controls that Can Fail)
# =========================================================================


def test_rl1_global_ranking_disabled_is_true():
    """Verify NCE_HR_RANKING_DISABLED is hard-pinned True."""
    assert cfg.NCE_HR_RANKING_DISABLED is True


def test_rl1_control_fails_when_flag_cleared(monkeypatch: pytest.MonkeyPatch):
    """Guard-the-guard: verify enforce_privacy_preconditions fails if flag is False."""
    monkeypatch.setattr(cfg, "NCE_HR_RANKING_DISABLED", False)
    with pytest.raises(HrRankingProhibitedError, match="CRITICAL SECURITY VIOLATION"):
        privacy.enforce_privacy_preconditions({"employee_id": _EMP_A})


def test_rl1_refuses_leaderboard_in_coaching():
    with pytest.raises(HrRankingProhibitedError, match="prohibited"):
        privacy.enforce_privacy_preconditions({"employee_id": _EMP_A, "leaderboard": True})


def test_rl1_refuses_sort_by_score():
    with pytest.raises(HrRankingProhibitedError, match="prohibited"):
        privacy.enforce_privacy_preconditions({"employee_id": _EMP_A, "sort_by": "standing_score"})


# =========================================================================
# 2. RL-3: Access Scope Enforcement
# =========================================================================


def test_peer_access_to_other_employee_refused():
    with pytest.raises(PermissionError, match="Access denied"):
        privacy.enforce_privacy_preconditions(
            {
                "employee_id": _EMP_A,
                "caller_role": "peer",
                "caller_employee_id": _EMP_B,
            }
        )


def test_peer_access_to_self_allowed():
    # Self-access does not raise
    privacy.enforce_privacy_preconditions(
        {
            "employee_id": _EMP_A,
            "caller_role": "peer",
            "caller_employee_id": _EMP_A,
        }
    )


def test_manager_access_to_employee_allowed():
    # Manager access does not raise
    privacy.enforce_privacy_preconditions(
        {
            "employee_id": _EMP_A,
            "caller_role": "manager",
            "caller_employee_id": _EMP_B,
        }
    )


# =========================================================================
# 3. RL-3: GDPR Redaction Gate (Fodselsnummer, Bank, Cards, Phone, Email)
# =========================================================================


def test_redact_valid_fodselsnummer():
    from nce.pii import _fodselsnummer_check

    valid_sample = None
    for seq in range(10000, 99999):
        candidate = f"010190{seq}"
        if _fodselsnummer_check(candidate):
            valid_sample = candidate
            break

    assert valid_sample is not None
    text = f"Employee identity number is {valid_sample} for payroll setup."
    redacted = privacy.redact_hr_text(text)
    assert "[REDACTED_FODSELSNUMMER]" in redacted
    assert valid_sample not in redacted


def test_redact_bank_account_and_card():
    text = "Account 1234.56.78901 and card 4111 2222 3333 4444 should be masked."
    redacted = privacy.redact_hr_text(text)
    assert "[REDACTED_BANK_ACCOUNT]" in redacted
    assert "[REDACTED_CREDIT_CARD]" in redacted
    assert "1234.56.78901" not in redacted
    assert "4111 2222 3333 4444" not in redacted


def test_redact_contact_details():
    text = "Contact user at alpha@example.test or call +47 98765432."
    redacted = privacy.redact_hr_text(text)
    assert "[REDACTED_EMAIL]" in redacted
    assert "[REDACTED_PHONE]" in redacted
    assert "alpha@example.test" not in redacted
    assert "98765432" not in redacted


def test_scan_hr_pii_entities():
    text = "User phone 98765432 and email alpha@example.test found."
    entities = privacy.scan_hr_pii(text)
    types = {e["type"] for e in entities}
    assert "PHONE" in types
    assert "EMAIL" in types


# =========================================================================
# 4. RL-2: EU AI Act Article 5 Compliance & Sustained Overload
# =========================================================================


def test_overload_empty_or_nominal_workload():
    res = privacy.calculate_sustained_overload([37.5, 38.0, 37.5, 37.5])
    assert res["sustained_overload"] is False
    assert res["workload_status"] == "nominal"
    assert res["recommended_action"] == "none"
    assert "EU AI Act Article 5" in res["legal_boundary"]


def test_overload_elevated_load_single_week():
    res = privacy.calculate_sustained_overload([37.5, 46.0, 38.0, 37.5])
    assert res["sustained_overload"] is False
    assert res["workload_status"] == "elevated_load"
    assert res["recommended_action"] == "monitor_capacity"


def test_overload_sustained_three_consecutive_weeks():
    # 3 consecutive weeks of > 45h
    res = privacy.calculate_sustained_overload([37.5, 48.0, 50.0, 47.0])
    assert res["sustained_overload"] is True
    assert res["workload_status"] == "sustained_overload"
    assert res["consecutive_overtime_weeks"] == 3
    assert res["recommended_action"] == "workload_rebalance_advisory"


def test_overload_strictly_objective_no_emotion_words():
    """Verify return payload contains zero emotional or sentiment inference fields."""
    res = privacy.calculate_sustained_overload([52.0, 51.0])
    forbidden_keys = {
        "emotion",
        "sentiment",
        "stress_level",
        "burnout_risk",
        "mental_state",
        "mood",
    }
    assert not any(k in res for k in forbidden_keys)
    assert res["sustained_overload"] is True


# =========================================================================
# 5. Private 1-on-1 Logging & Coach Execution
# =========================================================================


@pytest.mark.asyncio
async def test_do_log_one_on_one_redaction_and_memory_scope():
    engine = _make_engine()
    raw_notes = (
        "Discussed career goals. Personal email is beta@example.test and phone 91234567. "
        "Agreed to focus on Crestron DM-NVX certification."
    )

    res = await coaching.do_log_one_on_one(
        engine,
        {
            "namespace_id": _NS_A,
            "employee_id": _EMP_A,
            "interviewer_id": _EMP_B,
            "notes": raw_notes,
            "action_items": ["Enroll in DM-NVX training"],
            "weekly_hours_history": [37.5, 38.0, 37.5],
            "hr_source_id": "SOURCE-ONE-ON-ONE-1",
        },
    )

    assert res["agent_id"] == "hr_private_coach"
    assert res["employee_id"] == _EMP_A
    assert "[REDACTED_EMAIL]" in res["notes"]
    assert "[REDACTED_PHONE]" in res["notes"]
    assert "beta@example.test" not in res["notes"]
    assert "91234567" not in res["notes"]
    assert "hr_private_coach" in res["tags"]
    assert res["hr_source_id"] == "SOURCE-ONE-ON-ONE-1"


@pytest.mark.asyncio
async def test_do_coach_skill_gap_and_overload_advisory():
    # Mock employee holding audio skill and dante cert
    mock_skills = [{"name": "audio_dsp_tuning"}]
    mock_certs = [{"name": "dante_level_2"}]
    engine = _make_engine(fetch_skills=mock_skills, fetch_certs=mock_certs)

    res = await coaching.do_coach(
        engine,
        {
            "namespace_id": _NS_A,
            "employee_id": _EMP_A,
            "target_role": "lead_audio_visual_commissioner",
            "focus_areas": ["network_av"],
            "weekly_hours_history": [48.0, 49.0, 50.0],
        },
    )

    assert res["employee_id"] == _EMP_A
    assert res["current_skills_count"] == 1
    assert len(res["growth_recommendations"]) > 0

    # Overload advisory surfaced objectively
    assert res["overload_advisory"] is not None
    assert res["overload_advisory"]["sustained_overload"] is True
    assert res["overload_advisory"]["workload_status"] == "sustained_overload"


@pytest.mark.asyncio
async def test_do_coach_refuses_ranking():
    engine = _make_engine()
    with pytest.raises(HrRankingProhibitedError, match="prohibited"):
        await coaching.do_coach(
            engine,
            {
                "namespace_id": _NS_A,
                "employee_id": _EMP_A,
                "rank_against_peers": True,
            },
        )
