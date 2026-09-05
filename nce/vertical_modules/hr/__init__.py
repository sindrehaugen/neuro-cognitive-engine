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
from nce.vertical_modules.hr.a2a import (
    get_morning_brief_hr_slice,
    handle_field_tech_dispatch_query,
    handle_project_assignment_query,
    handle_vendor_contractor_skill_align,
)
from nce.vertical_modules.hr.absences import EVENT_TYPE_HR_ABSENCE_REGISTERED
from nce.vertical_modules.hr.compliance import EVENT_TYPE_HR_COMPLIANCE_MILESTONE_RECORDED
from nce.vertical_modules.hr.onboarding import EVENT_TYPE_HR_QUEST_PROGRESSED
from nce.vertical_modules.hr.profile import EVENT_TYPE_HR_EMPLOYEE_CREATED

__all__ = [
    "EVENT_TYPE_HR_ABSENCE_REGISTERED",
    "EVENT_TYPE_HR_COMPLIANCE_MILESTONE_RECORDED",
    "EVENT_TYPE_HR_EMPLOYEE_CREATED",
    "EVENT_TYPE_HR_QUEST_PROGRESSED",
    "HrDisabledError",
    "HrRankingProhibitedError",
    "NCE_HR_RANKING_DISABLED",
    "get_morning_brief_hr_slice",
    "handle_field_tech_dispatch_query",
    "handle_project_assignment_query",
    "handle_vendor_contractor_skill_align",
    "require_hr_enabled",
]
