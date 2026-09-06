"""
nce/vertical_modules/project/__init__.py
======================================
Project Engine vertical module (Module 7).

Exports public core functions for project phase gates, quote conversion,
scope creep detection, status reports, project manager recall, and task synchronization.
"""

from __future__ import annotations

from nce.vertical_modules.project.advance import do_advance_phase
from nce.vertical_modules.project.case_study import do_generate_case_study_edge
from nce.vertical_modules.project.convert import do_convert_signed_quote
from nce.vertical_modules.project.insights import (
    do_detect_scope_creep,
    do_status_report,
)
from nce.vertical_modules.project.pl import do_capacity, do_my_day
from nce.vertical_modules.project.recall import (
    do_recall_similar_projects,
    do_record_project_outcome,
    do_suggest_pl,
)
from nce.vertical_modules.project.tasks import do_sync_bom_tasks

__all__ = [
    "do_advance_phase",
    "do_capacity",
    "do_convert_signed_quote",
    "do_detect_scope_creep",
    "do_generate_case_study_edge",
    "do_my_day",
    "do_recall_similar_projects",
    "do_record_project_outcome",
    "do_status_report",
    "do_suggest_pl",
    "do_sync_bom_tasks",
]
