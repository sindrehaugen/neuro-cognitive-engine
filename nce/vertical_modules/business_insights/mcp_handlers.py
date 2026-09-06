"""
nce/vertical_modules/business_insights/mcp_handlers.py
======================================================
MCP tool handlers for Module 16 (Business Insights Engine):
  - handle_business_insights_morning_brief: Executive 12-min morning brief (Watcher/Advisor, cacheable, admin_only).
  - handle_business_insights_risk_radar: Cross-engine collision detection (Watcher, cacheable, admin_only).
  - handle_business_insights_run_scenario: Monte-Carlo what-if scenario (Advisor, uncacheable, admin_only).
  - handle_business_insights_generate_board_pack: Draft board pack generator (Advisor, uncacheable, admin_only).
  - handle_business_insights_kpi_dashboard: Live KPI cockpit & trend persistence (Watcher, cacheable, admin_only).
  - handle_business_insights_ask_business: NL executive query with BI-1/BI-3 gates (Advisor, uncacheable, admin_only).

Flags mirror the Business Insights Engine contract:
| Tool                                    | cacheable | admin_only | mutation | AI-role         |
|-----------------------------------------|-----------|------------|----------|-----------------|
| business_insights_morning_brief         | Y         | Y          | N        | Watcher/Advisor |
| business_insights_risk_radar            | Y         | Y          | N        | Watcher         |
| business_insights_run_scenario          | N         | Y          | N        | Advisor         |
| business_insights_generate_board_pack   | N         | Y          | N        | Advisor         |
| business_insights_kpi_dashboard         | Y         | Y          | N        | Watcher         |
| business_insights_ask_business          | N         | Y          | N        | Advisor         |
"""

from __future__ import annotations

import json
import logging
from typing import Any

from nce.mcp_args import require_namespace_id
from nce.mcp_errors import MCP_SCOPE_FORBIDDEN, McpError, mcp_handler
from nce.vertical_modules.business_insights._guard import (
    BusinessInsightsDisabledError,
    PersonRankingProhibitedError,
    ThirdPartyEgressUnauthorizedError,
    require_business_insights_enabled,
)
from nce.vertical_modules.business_insights.ask import do_ask_business
from nce.vertical_modules.business_insights.board_pack import do_generate_board_pack
from nce.vertical_modules.business_insights.brief import do_morning_brief
from nce.vertical_modules.business_insights.kpi import do_kpi_dashboard
from nce.vertical_modules.business_insights.radar import do_risk_radar
from nce.vertical_modules.business_insights.scenario import do_run_scenario

log = logging.getLogger("nce.vertical_modules.business_insights.mcp_handlers")


def _extract_pool(engine_or_pool: Any) -> Any:
    if hasattr(engine_or_pool, "pg_pool") and (
        "pg_pool" in getattr(engine_or_pool, "__dict__", {})
        or hasattr(type(engine_or_pool), "pg_pool")
    ):
        return engine_or_pool.pg_pool
    return engine_or_pool


async def _check_business_insights_enabled(engine: Any, arguments: dict[str, Any]) -> str:
    namespace_id = require_namespace_id(arguments)
    pool = _extract_pool(engine)
    if pool is not None:
        try:
            await require_business_insights_enabled(pool, namespace_id)
        except BusinessInsightsDisabledError as exc:
            raise McpError(
                MCP_SCOPE_FORBIDDEN,
                "Business Insights vertical is not enabled for this namespace",
                data={"reason": "business_insights_disabled", "detail": str(exc)},
            ) from exc
    return namespace_id


@mcp_handler
async def handle_business_insights_morning_brief(
    engine: Any,
    arguments: dict[str, Any],
) -> str:
    """Generate executive morning brief across operational pillars with provenance tracing.

    Requires `namespace` (or `namespace_id`); optionally `lookback_hours`, `as_of`.
    """
    namespace_id = await _check_business_insights_enabled(engine, arguments)
    params = dict(arguments)
    params["namespace_id"] = namespace_id
    try:
        res = await do_morning_brief(engine, params)
    except ValueError as exc:
        raise McpError(-32602, str(exc)) from exc
    except Exception as exc:
        log.exception("Error in handle_business_insights_morning_brief: %s", exc)
        raise
    return json.dumps(res, default=str)


@mcp_handler
async def handle_business_insights_risk_radar(
    engine: Any,
    arguments: dict[str, Any],
) -> str:
    """Cross-engine collision detection (risk radar) identifying systemic multi-domain risks.

    Requires `namespace` (or `namespace_id`); optionally `lookback_days`, `as_of`.
    """
    namespace_id = await _check_business_insights_enabled(engine, arguments)
    params = dict(arguments)
    params["namespace_id"] = namespace_id
    try:
        res = await do_risk_radar(engine, params)
    except ValueError as exc:
        raise McpError(-32602, str(exc)) from exc
    except Exception as exc:
        log.exception("Error in handle_business_insights_risk_radar: %s", exc)
        raise
    return json.dumps(res, default=str)


@mcp_handler
async def handle_business_insights_run_scenario(
    engine: Any,
    arguments: dict[str, Any],
) -> str:
    """Forward scenario modeling with Monte-Carlo cashflow simulation and capacity impact analysis.

    Requires `namespace` (or `namespace_id`), `name` (or `scenario_name`);
    optionally `simulation_runs`, `assumptions`, `horizon_days`.
    """
    namespace_id = await _check_business_insights_enabled(engine, arguments)
    params = dict(arguments)
    params["namespace_id"] = namespace_id
    try:
        res = await do_run_scenario(engine, params)
    except ValueError as exc:
        raise McpError(-32602, str(exc)) from exc
    except Exception as exc:
        log.exception("Error in handle_business_insights_run_scenario: %s", exc)
        raise
    return json.dumps(res, default=str)


@mcp_handler
async def handle_business_insights_generate_board_pack(
    engine: Any,
    arguments: dict[str, Any],
) -> str:
    """Generate draft quarterly board pack with executive summary and multi-domain KPIs staged for review.

    Requires `namespace` (or `namespace_id`), `quarter`; optionally `meeting_date`, `as_of`.
    """
    namespace_id = await _check_business_insights_enabled(engine, arguments)
    params = dict(arguments)
    params["namespace_id"] = namespace_id
    try:
        res = await do_generate_board_pack(engine, params)
    except ValueError as exc:
        raise McpError(-32602, str(exc)) from exc
    except Exception as exc:
        log.exception("Error in handle_business_insights_generate_board_pack: %s", exc)
        raise
    return json.dumps(res, default=str)


@mcp_handler
async def handle_business_insights_kpi_dashboard(
    engine: Any,
    arguments: dict[str, Any],
) -> str:
    """Retrieve multi-domain KPI cockpit metrics and snapshot trends.

    Requires `namespace` (or `namespace_id`); optionally `period`, `as_of`.
    """
    namespace_id = await _check_business_insights_enabled(engine, arguments)
    params = dict(arguments)
    params["namespace_id"] = namespace_id
    try:
        res = await do_kpi_dashboard(engine, params)
    except ValueError as exc:
        raise McpError(-32602, str(exc)) from exc
    except Exception as exc:
        log.exception("Error in handle_business_insights_kpi_dashboard: %s", exc)
        raise
    return json.dumps(res, default=str)


@mcp_handler
async def handle_business_insights_ask_business(
    engine: Any,
    arguments: dict[str, Any],
) -> str:
    """Role-scoped natural language query answering with EU AI Act Art 5 person barrier and third-party egress gating.

    Requires `namespace` (or `namespace_id`), `query`;
    optionally `caller_role`, `allow_external_ai`, `board_signoff_reference`.
    """
    namespace_id = await _check_business_insights_enabled(engine, arguments)
    params = dict(arguments)
    params["namespace_id"] = namespace_id
    try:
        res = await do_ask_business(engine, params)
    except PersonRankingProhibitedError as exc:
        raise McpError(-32602, str(exc), data={"code": "person_ranking_prohibited"}) from exc
    except ThirdPartyEgressUnauthorizedError as exc:
        raise McpError(
            MCP_SCOPE_FORBIDDEN, str(exc), data={"code": "third_party_egress_unauthorized"}
        ) from exc
    except PermissionError as exc:
        raise McpError(MCP_SCOPE_FORBIDDEN, str(exc)) from exc
    except ValueError as exc:
        raise McpError(-32602, str(exc)) from exc
    except Exception as exc:
        log.exception("Error in handle_business_insights_ask_business: %s", exc)
        raise
    return json.dumps(res, default=str)
