"""
nce/vertical_modules/support/triage.py
======================================
Intelligent ticket triage and specialist routing for Module 10 (Support Engine):
  - do_triage_ticket: assesses urgency, impact, required domain skills,
    and routing target (Advisor role per Roadmap §2).

Strict Tenant Predicate Discipline (Charter §5.5)
-------------------------------------------------
Every query against service_tickets enforces explicit WHERE id = $1::uuid
AND namespace_id = $2::uuid predicates.
"""

from __future__ import annotations

import logging
from typing import Any

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.support.tickets import (
    TicketNotFoundError,
    _extract_pool,
    _parse_uuid,
)

log = logging.getLogger("nce.vertical_modules.support.triage")

_CRITICAL_ROOM_KEYWORDS = frozenset(
    {"boardroom", "executive", "auditorium", "allhands", "townhall", "ceo", "board"}
)
_OUTAGE_KEYWORDS = frozenset(
    {"offline", "failure", "broken", "blackout", "unusable", "down", "emergency", "urgent"}
)

_AUDIO_KEYWORDS = frozenset(
    {
        "mic",
        "microphone",
        "speaker",
        "audio",
        "sound",
        "feedback",
        "echo",
        "screech",
        "dsp",
        "dante",
        "amplifier",
    }
)
_VIDEO_KEYWORDS = frozenset(
    {
        "nvx",
        "video",
        "display",
        "projector",
        "screen",
        "wall",
        "hdmi",
        "edid",
        "crestron",
        "camera",
        "ptz",
    }
)
_SYSTEMS_KEYWORDS = frozenset(
    {
        "network",
        "switch",
        "vlan",
        "poe",
        "ping",
        "ip",
        "controller",
        "processor",
        "control",
        "gateway",
    }
)


async def do_triage_ticket(
    engine_or_pool: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Triage ticket priority, urgency, required engineering skill and routing.

    Parameters
    ----------
    engine_or_pool:
        NCEEngine instance or asyncpg.Pool.
    params:
        - namespace_id: (required) tenant UUID string or UUID.
        - ticket_id: (required) ticket UUID to triage.
    """
    pool = _extract_pool(engine_or_pool)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")
    ticket_id = _parse_uuid(params.get("ticket_id") or params.get("id"), "ticket_id")

    async with scoped_pg_session(pool, ns_uuid) as conn:
        ticket_row = await conn.fetchrow(
            """
            SELECT id, namespace_id, summary, description, room_id, asset_id, priority, status
            FROM service_tickets
            WHERE id = $1::uuid AND namespace_id = $2::uuid
            """,
            ticket_id,
            ns_uuid,
        )
        if ticket_row is None:
            raise TicketNotFoundError(ticket_id=str(ticket_id))

    summary = (ticket_row["summary"] or "").lower()
    description = (ticket_row["description"] or "").lower()
    full_text = f"{summary} {description}"
    room_id = (ticket_row["room_id"] or "").lower()

    # 1. Criticality & Urgency assessment
    has_critical_room = any(k in room_id or k in full_text for k in _CRITICAL_ROOM_KEYWORDS)
    has_outage = any(k in full_text for k in _OUTAGE_KEYWORDS)

    if has_critical_room and has_outage:
        recommended_priority = "critical"
        urgency = "critical"
        urgency_reason = "Outage affecting high-impact Executive / Boardroom space"
    elif has_outage:
        recommended_priority = "high"
        urgency = "high"
        urgency_reason = "Significant functional outage reported"
    elif has_critical_room:
        recommended_priority = "high"
        urgency = "medium"
        urgency_reason = "Issue in high-visibility room but partial functionality remains"
    elif "intermittent" in full_text or "glitch" in full_text:
        recommended_priority = "medium"
        urgency = "medium"
        urgency_reason = "Intermittent degradation observed"
    else:
        recommended_priority = ticket_row["priority"] or "low"
        urgency = "normal"
        urgency_reason = "Standard non-blocking operational request"

    # 2. Skill matching
    if any(k in full_text for k in _AUDIO_KEYWORDS):
        suggested_skill = "audio_specialist"
    elif any(k in full_text for k in _VIDEO_KEYWORDS):
        suggested_skill = "video_specialist"
    elif any(k in full_text for k in _SYSTEMS_KEYWORDS):
        suggested_skill = "systems_engineer"
    else:
        suggested_skill = "field_technician"

    # 3. Routing suggestion
    if any(k in full_text for k in {"cable", "wire", "plug", "loose", "mount", "physical"}):
        suggested_route = "tier_2_field_dispatch"
        auto_dispatch_candidate = recommended_priority in ("high", "critical")
    else:
        suggested_route = "tier_1_remote_triage"
        auto_dispatch_candidate = False

    return {
        "ticket_id": str(ticket_id),
        "recommended_priority": recommended_priority,
        "urgency": urgency,
        "urgency_reason": urgency_reason,
        "suggested_skill": suggested_skill,
        "suggested_route": suggested_route,
        "auto_dispatch_candidate": auto_dispatch_candidate,
    }
