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
from nce.vertical_modules.hr.absences import (
    EVENT_TYPE_HR_ABSENCE_REGISTERED,
    do_query_absences,
    do_register_absence,
)
from nce.vertical_modules.hr.capacity import do_capacity
from nce.vertical_modules.hr.certs import do_cert_status
from nce.vertical_modules.hr.coaching import do_coach, do_log_one_on_one
from nce.vertical_modules.hr.compliance import (
    EVENT_TYPE_HR_COMPLIANCE_MILESTONE_RECORDED,
    do_query_compliance_deadlines,
    do_update_absence_compliance,
)
from nce.vertical_modules.hr.onboarding import (
    EVENT_TYPE_HR_QUEST_PROGRESSED,
    do_build_onboarding_quest,
    do_get_onboarding_progress,
)
from nce.vertical_modules.hr.profile import (
    EVENT_TYPE_HR_EMPLOYEE_CREATED,
    do_create_employee,
    do_get_employee,
    do_query_employees,
)
from nce.vertical_modules.hr.skills import (
    do_match_skills,
    do_record_certification,
    do_record_skill,
)

__all__ = [
    "EVENT_TYPE_HR_ABSENCE_REGISTERED",
    "EVENT_TYPE_HR_COMPLIANCE_MILESTONE_RECORDED",
    "EVENT_TYPE_HR_EMPLOYEE_CREATED",
    "EVENT_TYPE_HR_QUEST_PROGRESSED",
    "HrDisabledError",
    "HrRankingProhibitedError",
    "NCE_HR_RANKING_DISABLED",
    "do_build_onboarding_quest",
    "do_capacity",
    "do_cert_status",
    "do_coach",
    "do_create_employee",
    "do_get_employee",
    "do_get_onboarding_progress",
    "do_log_one_on_one",
    "do_match_skills",
    "do_query_absences",
    "do_query_compliance_deadlines",
    "do_query_employees",
    "do_record_certification",
    "do_record_skill",
    "do_register_absence",
    "do_update_absence_compliance",
    "get_morning_brief_hr_slice",
    "handle_field_tech_dispatch_query",
    "handle_project_assignment_query",
    "handle_vendor_contractor_skill_align",
    "require_hr_enabled",
]
