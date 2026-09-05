"""
nce/vertical_modules/marketing/events.py
========================================
Event types, contracts, and emission helpers for Module 14 (Marketing Engine).

Charter M14.W7:
  - marketing_case_study_drafted
  - marketing_testimonial_requested
  - marketing_testimonial_captured
  - marketing_testimonial_retracted
  - marketing_content_approved
  - marketing_content_published
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from nce.event_log import append_event

log = logging.getLogger("nce.vertical_modules.marketing.events")

EVENT_MARKETING_CASE_STUDY_DRAFTED: str = "marketing_case_study_drafted"
EVENT_MARKETING_TESTIMONIAL_REQUESTED: str = "marketing_testimonial_requested"
EVENT_MARKETING_TESTIMONIAL_CAPTURED: str = "marketing_testimonial_captured"
EVENT_MARKETING_TESTIMONIAL_RETRACTED: str = "marketing_testimonial_retracted"
EVENT_MARKETING_CONTENT_APPROVED: str = "marketing_content_approved"
EVENT_MARKETING_CONTENT_PUBLISHED: str = "marketing_content_published"


async def emit_marketing_event(
    engine: Any,
    namespace_id: str | UUID,
    event_type: str,
    params: dict[str, Any],
) -> None:
    """Emit an auditable marketing lifecycle event to the append-only event_log."""
    pool = getattr(engine, "pg_pool", None) or getattr(engine, "pool", None)
    if pool is None:
        return

    ns_uuid = UUID(str(namespace_id))
    try:
        async with pool.acquire() as conn:
            await append_event(
                conn,
                namespace_id=ns_uuid,
                event_type=event_type,
                params=params,
            )
    except Exception as exc:
        log.warning("Failed to emit marketing event %s: %s", event_type, exc)
