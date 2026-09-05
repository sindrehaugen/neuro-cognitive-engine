"""
nce/vertical_modules/business_insights/__init__.py
==================================================
Module 16: Business Insights Engine (Management/Executive Decision Support).

Core functions:
- do_morning_brief: daily 12-minute brief (Economy + Project + Support + Sales + Resources)
- do_risk_radar: cross-engine collision detection (the moat)
- do_run_scenario: Monte-Carlo what-if modelling
- do_generate_board_pack: draft board narrative staged for human review
- do_kpi_dashboard: live KPI roll-up and snapshot trend persistence
- do_ask_business: NL executive queries with structural person barrier (BI-1) & third-party egress audit (BI-3)
"""

from __future__ import annotations

from nce.vertical_modules.business_insights.board_pack import do_generate_board_pack
from nce.vertical_modules.business_insights.brief import do_morning_brief
from nce.vertical_modules.business_insights.kpi import do_kpi_dashboard
from nce.vertical_modules.business_insights.radar import do_risk_radar
from nce.vertical_modules.business_insights.scenario import do_run_scenario

__all__ = [
    "do_generate_board_pack",
    "do_kpi_dashboard",
    "do_morning_brief",
    "do_risk_radar",
    "do_run_scenario",
]
