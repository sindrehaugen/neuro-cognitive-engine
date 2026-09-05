"""
nce/vertical_modules/hr
=======================
Module 13: HR Engine -- Native master data, skills taxonomy, cert lifecycle & expiry watcher,
absences, onboarding quest, private coaching, and assignment infrastructure.

Enforces RL-1 (NEVER ranking), RL-2 (EU AI Act Art. 5 emotion inference prohibition),
and RL-3 (GDPR erasure & PII protection).
"""

from __future__ import annotations

from nce.vertical_modules.hr._guard import (
    NCE_HR_RANKING_DISABLED,
    HrDisabledError,
    HrRankingProhibitedError,
    require_hr_enabled,
)

__all__ = [
    "HrDisabledError",
    "HrRankingProhibitedError",
    "NCE_HR_RANKING_DISABLED",
    "require_hr_enabled",
]
