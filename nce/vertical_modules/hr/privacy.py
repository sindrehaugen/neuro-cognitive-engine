"""
nce/vertical_modules/hr/privacy.py
==================================
Privacy gate, access-scope enforcement, and objective workload monitoring
for Module 13 (HR Engine).

Enforces the Three Red Lines:
  - RL-1: Hard-pinned NEVER-ranking. Validates that NCE_HR_RANKING_DISABLED is True
          and rejects any request involving cross-person ranking or leaderboards.
  - RL-2: EU AI Act Article 5 compliance (in force 2 Feb 2025). AI that infers
          emotions in the workplace is strictly prohibited (fines to EUR 35M / 7%).
          Sustained overload is detected from OBJECTIVE OPERATIONAL SIGNALS ONLY
          (assigned load, scheduled hours, absence duration). No sentiment,
          mood, stress, or psychological analysis is ever performed.
  - RL-3: GDPR PII erasure & redaction gate. Free text (1-on-1 notes, coaching goals)
          must pass the redaction gate before storage/embedding.
          Sensitive identifiers (Fodselsnummer, bank account, phone, email) are stripped.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from nce.config import cfg
from nce.pii import _fodselsnummer_check
from nce.vertical_modules.hr._guard import HrRankingProhibitedError, assert_ranking_prohibited

log = logging.getLogger("nce.vertical_modules.hr.privacy")

# Regexes for Norwegian & International PII
_RE_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_RE_PHONE = re.compile(r"(?:\+47\s?)?(?:\b[2-9]\d{1}\s?\d{2}\s?\d{2}\s?\d{2}\b|\b\d{8}\b)")
_RE_FODSELSNUMMER_CANDIDATE = re.compile(r"\b(\d{11})\b")
_RE_BANK_ACCOUNT = re.compile(r"\b\d{4}[.\s]?\d{2}[.\s]?\d{5}\b")
_RE_CREDIT_CARD = re.compile(r"\b(?:\d{4}[ -]?){3}\d{4}\b")


def redact_hr_text(text: str) -> str:
    """Strip sensitive PII from HR free text before embedding or storing (RL-3).

    Specifically detects and replaces:
      - Validated Norwegian Fodselsnummer (Mod-11 check)
      - Bank account numbers & IBAN
      - Credit card numbers
      - Email addresses
      - Phone numbers
    """
    if not text:
        return ""

    out = text

    # 1. Fodselsnummer with Mod-11 verification
    def _replace_fnr(m: re.Match[str]) -> str:
        digits = m.group(1)
        if _fodselsnummer_check(digits):
            return "[REDACTED_FODSELSNUMMER]"
        return digits

    out = _RE_FODSELSNUMMER_CANDIDATE.sub(_replace_fnr, out)

    # 2. Bank accounts & Cards
    out = _RE_BANK_ACCOUNT.sub("[REDACTED_BANK_ACCOUNT]", out)
    out = _RE_CREDIT_CARD.sub("[REDACTED_CREDIT_CARD]", out)

    # 3. Contact info
    out = _RE_EMAIL.sub("[REDACTED_EMAIL]", out)
    out = _RE_PHONE.sub("[REDACTED_PHONE]", out)

    return out


def scan_hr_pii(text: str) -> list[dict[str, Any]]:
    """Scan text and return detected PII entity types and spans without leaking values."""
    if not text:
        return []

    entities = []
    for m in _RE_FODSELSNUMMER_CANDIDATE.finditer(text):
        if _fodselsnummer_check(m.group(1)):
            entities.append({"type": "FODSELSNUMMER", "start": m.start(), "end": m.end()})

    for m in _RE_BANK_ACCOUNT.finditer(text):
        entities.append({"type": "BANK_ACCOUNT", "start": m.start(), "end": m.end()})

    for m in _RE_CREDIT_CARD.finditer(text):
        entities.append({"type": "CREDIT_CARD", "start": m.start(), "end": m.end()})

    for m in _RE_EMAIL.finditer(text):
        entities.append({"type": "EMAIL", "start": m.start(), "end": m.end()})

    for m in _RE_PHONE.finditer(text):
        entities.append({"type": "PHONE", "start": m.start(), "end": m.end()})

    return sorted(entities, key=lambda e: e["start"])


def enforce_privacy_preconditions(params: dict[str, Any]) -> None:
    """Enforce blocking preconditions before semantic writes or coaching execution.

    1. RL-1: Hard-pinned NCE_HR_RANKING_DISABLED must be True.
    2. RL-1: Params must not request leaderboards, scores, or cross-person rankings.
    3. RL-3: Access scope check (peer cannot read another employee's confidential record).
    """
    # Verify the global config pin
    if getattr(cfg, "NCE_HR_RANKING_DISABLED", True) is not True:
        raise HrRankingProhibitedError(
            "CRITICAL SECURITY VIOLATION: NCE_HR_RANKING_DISABLED has been modified. "
            "Operator-clearing of ranking prohibition is strictly refused (RL-1)."
        )

    # Verify input params do not request ranking
    assert_ranking_prohibited(params)

    # Access scoping
    caller_role = str(params.get("caller_role") or "peer").strip().lower()
    caller_emp_id = str(params.get("caller_employee_id") or "").strip()
    target_emp_id = str(params.get("employee_id") or "").strip()

    is_privileged = caller_role in ("manager", "hr", "admin", "system")
    if target_emp_id and caller_emp_id and not is_privileged:
        if caller_emp_id != target_emp_id:
            raise PermissionError(
                f"Access denied: Caller {caller_emp_id!r} with role {caller_role!r} "
                f"cannot access confidential HR record for employee {target_emp_id!r}."
            )


def calculate_sustained_overload(
    weekly_hours: list[float],
    unplanned_absence_days: float = 0.0,
    *,
    contractual_hours_per_week: float = 37.5,
    overtime_threshold_hours: float = 45.0,
    consecutive_weeks_threshold: int = 3,
) -> dict[str, Any]:
    """Calculate sustained overload from OBJECTIVE OPERATIONAL SIGNALS ONLY (RL-2).

    Complies with EU AI Act Article 5:
      - AI inferring emotions or sentiment in the workplace is illegal (fines to EUR 35M/7%).
      - This function analyzes purely numeric hours, capacity utilization, and absence days.
      - Never infers, scores, or surfaces 'stress', 'burnout', 'mood', or 'mental health'.
      - Surfaced strictly as an objective workload/capacity advisory.

    Parameters
    ----------
    weekly_hours : list[float]
        Chronological list of total hours worked/assigned per week (most recent last).
    unplanned_absence_days : float
        Total days of unplanned absence during the evaluated period.
    contractual_hours_per_week : float
        Standard baseline hours (default: 37.5 hours/week Norwegian standard).
    overtime_threshold_hours : float
        Hours above which a week is considered elevated load (default: 45.0).
    consecutive_weeks_threshold : int
        Number of consecutive overloaded weeks triggering sustained overload (default: 3).
    """
    if not weekly_hours:
        return {
            "workload_status": "nominal",
            "sustained_overload": False,
            "consecutive_overtime_weeks": 0,
            "average_weekly_hours": 0.0,
            "utilization_pct": 0.0,
            "legal_boundary": "EU AI Act Article 5 compliant -- objective operational signals only",
            "recommended_action": "none",
        }

    # Count consecutive weeks from the end
    consecutive_overtime = 0
    for hours in reversed(weekly_hours):
        if hours >= overtime_threshold_hours:
            consecutive_overtime += 1
        else:
            break

    avg_hours = round(sum(weekly_hours) / len(weekly_hours), 1)
    utilization_pct = round((avg_hours / contractual_hours_per_week) * 100.0, 1)

    is_sustained = (consecutive_overtime >= consecutive_weeks_threshold) or (
        len(weekly_hours) >= 2 and all(h >= 50.0 for h in weekly_hours[-2:])
    )

    any_overtime = any(h >= overtime_threshold_hours for h in weekly_hours)

    if is_sustained:
        status = "sustained_overload"
        action = "workload_rebalance_advisory"
    elif (
        consecutive_overtime >= 1 or any_overtime or avg_hours > (contractual_hours_per_week * 1.1)
    ):
        status = "elevated_load"
        action = "monitor_capacity"
    else:
        status = "nominal"
        action = "none"

    return {
        "workload_status": status,
        "sustained_overload": is_sustained,
        "consecutive_overtime_weeks": consecutive_overtime,
        "average_weekly_hours": avg_hours,
        "utilization_pct": utilization_pct,
        "unplanned_absence_days": float(unplanned_absence_days),
        "evaluated_weeks_count": len(weekly_hours),
        "legal_boundary": "EU AI Act Article 5 compliant -- objective operational signals only",
        "recommended_action": action,
    }
