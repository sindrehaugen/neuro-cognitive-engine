"""
nce/vertical_modules/marketing/mcp_handlers.py
==============================================
MCP tool handlers for Module 14 (Marketing Engine):
  - handle_marketing_find_case_study_candidates: Watcher; read-only, cacheable.
  - handle_marketing_draft_case_study: Actor; mutation, admin_only.
  - handle_marketing_request_testimonial: Actor; mutation, admin_only.
  - handle_marketing_capture_testimonial: Actor; mutation, admin_only.
  - handle_marketing_suggest_content: Advisor; read-only, cacheable.
  - handle_marketing_audit_seo: Advisor; read-only, cacheable.
  - handle_marketing_approve_content: Actor; mutation, admin_only (human gate).
  - handle_marketing_publish_content: Actor; mutation, admin_only (sign-off gated).

Flags mirror the Marketing Engine contract:
| Tool                                 | cacheable | admin_only | mutation | AI-role |
|--------------------------------------|-----------|------------|----------|---------|
| marketing_find_case_study_candidates | Y         | N          | N        | Watcher |
| marketing_draft_case_study           | N         | Y          | Y        | Actor   |
| marketing_request_testimonial        | N         | Y          | Y        | Actor   |
| marketing_capture_testimonial        | N         | Y          | Y        | Actor   |
| marketing_suggest_content            | Y         | N          | N        | Advisor |
| marketing_audit_seo                  | Y         | N          | N        | Advisor |
| marketing_approve_content            | N         | Y          | Y        | Actor   |
| marketing_publish_content            | N         | Y          | Y        | Actor   |
"""

from __future__ import annotations

import json
import logging
from typing import Any

from nce.mcp_args import require_namespace_id
from nce.mcp_errors import MCP_SCOPE_FORBIDDEN, McpError, mcp_handler
from nce.vertical_modules.marketing._guard import (
    MarketingDisabledError,
    require_marketing_enabled,
)
from nce.vertical_modules.marketing.candidates import do_find_case_study_candidates
from nce.vertical_modules.marketing.drafting import do_draft_case_study

log = logging.getLogger("nce.vertical_modules.marketing.mcp_handlers")


async def _check_marketing_enabled(engine: Any, ns: str) -> None:
    pool = getattr(engine, "pg_pool", None)
    if pool is not None:
        try:
            await require_marketing_enabled(pool, ns)
        except MarketingDisabledError as exc:
            raise McpError(MCP_SCOPE_FORBIDDEN, str(exc)) from exc


@mcp_handler
async def handle_marketing_find_case_study_candidates(
    engine: Any,
    params: dict[str, Any],
) -> str:
    """Find delivered projects that score high on outcome metrics for case studies."""
    ns = require_namespace_id(params)
    await _check_marketing_enabled(engine, ns)
    result = await do_find_case_study_candidates(engine, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_marketing_draft_case_study(
    engine: Any,
    params: dict[str, Any],
) -> str:
    """Assemble a retrieval-grounded case study draft from graph facts (MK-2 & MK-3)."""
    ns = require_namespace_id(params)
    await _check_marketing_enabled(engine, ns)
    anonymize = bool(params.get("anonymize", True))
    result = do_draft_case_study(params, anonymize=anonymize)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_marketing_request_testimonial(
    engine: Any,
    params: dict[str, Any],
) -> str:
    """Create a testimonial request for a customer, gated on high NPS (MK-5)."""
    ns = require_namespace_id(params)
    await _check_marketing_enabled(engine, ns)
    from nce.vertical_modules.marketing.testimonials import do_request_testimonial

    result = await do_request_testimonial(engine, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_marketing_capture_testimonial(
    engine: Any,
    params: dict[str, Any],
) -> str:
    """Record customer quote with structured consent (MK-4)."""
    ns = require_namespace_id(params)
    await _check_marketing_enabled(engine, ns)
    from nce.vertical_modules.marketing.testimonials import do_capture_testimonial

    result = await do_capture_testimonial(engine, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_marketing_suggest_content(
    engine: Any,
    params: dict[str, Any],
) -> str:
    """Suggest thought-leadership / drip ideas grounded in delivered graph patterns."""
    ns = require_namespace_id(params)
    await _check_marketing_enabled(engine, ns)
    from nce.vertical_modules.marketing.advisor import do_suggest_content

    result = await do_suggest_content(engine, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_marketing_audit_seo(
    engine: Any,
    params: dict[str, Any],
) -> str:
    """Audit content asset for AEO/GEO citation readiness and JSON-LD schema."""
    ns = require_namespace_id(params)
    await _check_marketing_enabled(engine, ns)
    from nce.vertical_modules.marketing.advisor import do_audit_seo

    result = await do_audit_seo(engine, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_marketing_approve_content(
    engine: Any,
    params: dict[str, Any],
) -> str:
    """Human approval sign-off gate for drafted marketing content (MK-1)."""
    ns = require_namespace_id(params)
    await _check_marketing_enabled(engine, ns)
    from nce.vertical_modules.marketing.approval import do_approve_content

    result = await do_approve_content(engine, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_marketing_publish_content(
    engine: Any,
    params: dict[str, Any],
) -> str:
    """Publish content via PublishTransport, enforcing approval & consent gates."""
    ns = require_namespace_id(params)
    await _check_marketing_enabled(engine, ns)
    from nce.vertical_modules.marketing.publish import do_publish_content

    result = await do_publish_content(engine, params)
    return json.dumps(result, default=str)
