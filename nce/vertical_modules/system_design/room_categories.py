"""nce.vertical_modules.system_design.room_categories — Room Categories & FL Metadata.

Phase C Wave C-2:
Curated AV/engineering room categories as config-as-IP in the module directory,
FL room category associations via kg_edges (_PRED_HAS_CATEGORY = 'has_category'),
and responsible employee assignments via kg_edges (_PRED_RESPONSIBLE_FOR = 'responsible_for')
integrated with C16 Principal Mapping.

Architecture & Governance (Charter §13 & §14 K-C2):
- Config-as-IP in module directory (never in frozen config_data).
- Zero SQL schema migrations; strictly backed by kg_nodes and kg_edges.
- C16 Principal resolution for my-responsible queries (unbound or non-employee callers return []).
- Strict MCP error hierarchy inheriting from ValueError and KeyError.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.orchestrator import NCEEngine
from nce.principal_bindings import PrincipalContext
from nce.vertical_modules.system_design.fl_tree import get_fl_node
from nce.vertical_modules.system_design.graph import (
    assign_fl_responsible_edge,
    get_fl_category_edge,
    list_fl_responsible_edges,
    list_responsible_fl_edges_for_employee,
    set_fl_category_edge,
    unassign_fl_responsible_edge,
)

log = logging.getLogger("nce.vertical_modules.system_design.room_categories")


# ---------------------------------------------------------------------------
# Exceptions (Strict MCP Error Hierarchy)
# ---------------------------------------------------------------------------


class RoomCategoryError(ValueError):
    """Base exception for room category operations."""


class RoomCategoryNotFoundError(RoomCategoryError, KeyError):
    """Raised when a requested room category is not recognized."""


class InvalidResponsibleRoleError(RoomCategoryError, ValueError):
    """Raised when an assigned responsible role is invalid."""


# ---------------------------------------------------------------------------
# Config-as-IP: Standardized AV / Engineering Room Categories
# ---------------------------------------------------------------------------

VALID_RESPONSIBLE_ROLES: set[str] = {
    "primary",
    "backup",
    "lead_technician",
    "commissioning_lead",
    "account_manager",
}

ROOM_CATEGORIES: dict[str, dict[str, Any]] = {
    "BOARDROOM": {
        "id": "BOARDROOM",
        "name": "Executive Boardroom",
        "description": (
            "High-stakes executive presentation and conferencing space with integrated "
            "multi-display, voice tracking, and premium acoustic isolation."
        ),
        "capacity_min": 12,
        "capacity_max": 24,
        "acoustics": {
            "target_rt60_seconds": 0.5,
            "noise_criterion_nc": 25,
            "sound_isolation_stc": 50,
        },
        "video": {
            "display_type": 'Dual 85"+ 4K or Direct View LED',
            "camera_type": "Dual PTZ with presenter/speaker tracking",
            "fov_degrees": 90,
        },
        "audio": {
            "mic_type": "Ceiling beamforming array with steerable lobes",
            "aec_required": True,
            "speaker_type": "Ceiling distributed coax or front line arrays",
        },
        "typical_features": [
            "dual_display",
            "aec_dsp",
            "speaker_tracking",
            "table_well_connectivity",
            "room_scheduler",
        ],
    },
    "CONFERENCE_LARGE": {
        "id": "CONFERENCE_LARGE",
        "name": "Large Conference Room",
        "description": (
            "Formal meeting and collaboration room for large teams with multi-mic pickup "
            "and dual presentation displays."
        ),
        "capacity_min": 14,
        "capacity_max": 30,
        "acoustics": {
            "target_rt60_seconds": 0.6,
            "noise_criterion_nc": 30,
            "sound_isolation_stc": 45,
        },
        "video": {
            "display_type": 'Dual 75"-85" Commercial Displays',
            "camera_type": "Optical zoom PTZ with auto-framing",
            "fov_degrees": 90,
        },
        "audio": {
            "mic_type": "Ceiling microphone arrays",
            "aec_required": True,
            "speaker_type": "Distributed ceiling speakers",
        },
        "typical_features": [
            "dual_display",
            "aec_dsp",
            "auto_framing",
            "wireless_sharing",
            "room_scheduler",
        ],
    },
    "CONFERENCE_MEDIUM": {
        "id": "CONFERENCE_MEDIUM",
        "name": "Medium Conference Room",
        "description": (
            "Standard collaboration and hybrid video conferencing space for project teams."
        ),
        "capacity_min": 6,
        "capacity_max": 12,
        "acoustics": {
            "target_rt60_seconds": 0.6,
            "noise_criterion_nc": 30,
            "sound_isolation_stc": 40,
        },
        "video": {
            "display_type": 'Single or Dual 65"-75" Commercial Displays',
            "camera_type": "Video bar with wide-angle ePTZ",
            "fov_degrees": 110,
        },
        "audio": {
            "mic_type": "Integrated beamforming bar or table mic pods",
            "aec_required": True,
            "speaker_type": "Integrated front stereo soundbar",
        },
        "typical_features": [
            "single_or_dual_display",
            "all_in_one_bar",
            "auto_framing",
            "byod_support",
        ],
    },
    "MEETING_SMALL": {
        "id": "MEETING_SMALL",
        "name": "Small Meeting Room",
        "description": (
            "Compact enclosed focus or 4-6 person sync room with plug-and-play USB/BYOD "
            "peripheral support."
        ),
        "capacity_min": 3,
        "capacity_max": 6,
        "acoustics": {
            "target_rt60_seconds": 0.5,
            "noise_criterion_nc": 35,
            "sound_isolation_stc": 35,
        },
        "video": {
            "display_type": 'Single 55"-65" Commercial Display',
            "camera_type": "Wide FOV ePTZ Video Bar",
            "fov_degrees": 120,
        },
        "audio": {
            "mic_type": "Integrated video bar array",
            "aec_required": True,
            "speaker_type": "Integrated video bar speaker",
        },
        "typical_features": [
            "single_display",
            "all_in_one_bar",
            "byod_usb_c",
        ],
    },
    "HUDDLE": {
        "id": "HUDDLE",
        "name": "Huddle Space",
        "description": "Informal, quick-turn collaboration booth or alcove for 2-4 persons.",
        "capacity_min": 2,
        "capacity_max": 4,
        "acoustics": {
            "target_rt60_seconds": 0.5,
            "noise_criterion_nc": 35,
            "sound_isolation_stc": 30,
        },
        "video": {
            "display_type": 'Single 43"-50" Display',
            "camera_type": "Ultra-wide angle USB webcam/bar",
            "fov_degrees": 120,
        },
        "audio": {
            "mic_type": "Integrated bar or table boundary mic",
            "aec_required": True,
            "speaker_type": "Integrated bar speaker",
        },
        "typical_features": [
            "single_display",
            "compact_bar",
            "plug_and_play",
        ],
    },
    "TRAINING_ROOM": {
        "id": "TRAINING_ROOM",
        "name": "Training Room / Classroom",
        "description": (
            "Instructional space with instructor presentation, audience mic reinforcement, "
            "and confidence monitoring."
        ),
        "capacity_min": 15,
        "capacity_max": 40,
        "acoustics": {
            "target_rt60_seconds": 0.6,
            "noise_criterion_nc": 30,
            "sound_isolation_stc": 45,
        },
        "video": {
            "display_type": 'Dual Projection or 85"+ Displays plus Confidence Monitor',
            "camera_type": "Dual PTZ (Instructor tracking + Student wide)",
            "fov_degrees": 90,
        },
        "audio": {
            "mic_type": "Wireless lapel/handheld + ceiling audience mics",
            "aec_required": True,
            "speaker_type": "Distributed high-intelligibility ceiling speakers",
        },
        "typical_features": [
            "instructor_mic",
            "confidence_monitor",
            "lecture_capture",
            "assisted_listening",
        ],
    },
    "AUDITORIUM": {
        "id": "AUDITORIUM",
        "name": "Auditorium / Large Hall",
        "description": (
            "High-capacity event and plenary hall with theatrical audio reinforcement, "
            "line arrays, and broadcast switching."
        ),
        "capacity_min": 50,
        "capacity_max": 300,
        "acoustics": {
            "target_rt60_seconds": 0.9,
            "noise_criterion_nc": 25,
            "sound_isolation_stc": 55,
        },
        "video": {
            "display_type": "Direct View LED Video Wall or High-Lumen Projection",
            "camera_type": "Multiple broadcast-grade SDI/NDI PTZ cameras",
            "fov_degrees": 75,
        },
        "audio": {
            "mic_type": "Multi-channel wireless system (lavaliers, handhelds, podium)",
            "aec_required": True,
            "speaker_type": "Left-Center-Right Line Array with Subwoofers",
        },
        "typical_features": [
            "led_videowall",
            "ndi_broadcast",
            "production_switcher",
            "hearing_loop",
        ],
    },
    "ALL_HANDS": {
        "id": "ALL_HANDS",
        "name": "All Hands / Multi-Purpose Area",
        "description": (
            "Open or semi-open town hall presentation area with high ambient noise "
            "mitigation and wide audio dispersion."
        ),
        "capacity_min": 30,
        "capacity_max": 150,
        "acoustics": {
            "target_rt60_seconds": 0.8,
            "noise_criterion_nc": 35,
            "sound_isolation_stc": 35,
        },
        "video": {
            "display_type": "Large format LED Wall or Dual High-Output Displays",
            "camera_type": "Presenter tracking PTZ + wide audience camera",
            "fov_degrees": 90,
        },
        "audio": {
            "mic_type": "Wireless handhelds and ceiling steerable arrays",
            "aec_required": True,
            "speaker_type": "Column line-arrays or high-directivity pendants",
        },
        "typical_features": [
            "town_hall_mode",
            "wireless_mics",
            "streaming_encoder",
        ],
    },
    "FLEX_SPACE": {
        "id": "FLEX_SPACE",
        "name": "Flexible Collaboration Space",
        "description": (
            "Reconfigurable multi-zone room with movable furniture, mobile interactive "
            "touch carts, and wireless casting."
        ),
        "capacity_min": 4,
        "capacity_max": 20,
        "acoustics": {
            "target_rt60_seconds": 0.6,
            "noise_criterion_nc": 35,
            "sound_isolation_stc": 35,
        },
        "video": {
            "display_type": 'Mobile Interactive Touch Displays (65"-75")',
            "camera_type": "Integrated touch cart camera",
            "fov_degrees": 110,
        },
        "audio": {
            "mic_type": "Wireless mic pods or cart-mounted array",
            "aec_required": True,
            "speaker_type": "Integrated cart soundbar",
        },
        "typical_features": [
            "interactive_whiteboard",
            "mobile_cart",
            "byod_wireless",
        ],
    },
    "WAR_ROOM": {
        "id": "WAR_ROOM",
        "name": "Command & War Room",
        "description": (
            "Mission-critical incident response room with multi-window video wall "
            "processors and encrypted audio."
        ),
        "capacity_min": 8,
        "capacity_max": 18,
        "acoustics": {
            "target_rt60_seconds": 0.5,
            "noise_criterion_nc": 30,
            "sound_isolation_stc": 50,
        },
        "video": {
            "display_type": "Multi-window Video Wall (2x2 or Ultra-wide LED)",
            "camera_type": "Dual PTZ covering table and whiteboard/wall",
            "fov_degrees": 95,
        },
        "audio": {
            "mic_type": "Low-profile table boundary mics with mute rings",
            "aec_required": True,
            "speaker_type": "Dedicated conference audio system",
        },
        "typical_features": [
            "multiviewer",
            "kvm_switching",
            "secure_conferencing",
            "24_7_continuous",
        ],
    },
    "OPERATIONS_CENTER": {
        "id": "OPERATIONS_CENTER",
        "name": "Network Operations Center (NOC)",
        "description": (
            "24/7 continuous operations room with large monitoring overview wall "
            "and individual console audio routing."
        ),
        "capacity_min": 10,
        "capacity_max": 40,
        "acoustics": {
            "target_rt60_seconds": 0.6,
            "noise_criterion_nc": 35,
            "sound_isolation_stc": 45,
        },
        "video": {
            "display_type": "Seamless Ultra-Narrow Bezel or MicroLED Video Wall",
            "camera_type": "Overhead situational camera and room PTZ",
            "fov_degrees": 90,
        },
        "audio": {
            "mic_type": "Console gooseneck and headset integration",
            "aec_required": False,
            "speaker_type": "Low-level distributed speech and alert sounders",
        },
        "typical_features": [
            "video_wall_processor",
            "kvm_matrix",
            "alert_annunciation",
            "24_7_continuous",
        ],
    },
}


# ---------------------------------------------------------------------------
# Pure / Query Primitives
# ---------------------------------------------------------------------------


def list_room_categories(
    q: str | None = None,
    min_capacity: int | None = None,
    max_capacity: int | None = None,
) -> list[dict[str, Any]]:
    """List standardized room categories with optional search and capacity filters."""
    results = list(ROOM_CATEGORIES.values())

    if q:
        query = q.strip().lower()
        results = [
            cat
            for cat in results
            if query in cat["id"].lower()
            or query in cat["name"].lower()
            or query in cat["description"].lower()
        ]

    if min_capacity is not None:
        results = [cat for cat in results if cat["capacity_max"] >= min_capacity]

    if max_capacity is not None:
        results = [cat for cat in results if cat["capacity_min"] <= max_capacity]

    results.sort(key=lambda c: c["id"])
    return results


def get_room_category(category_id: str) -> dict[str, Any]:
    """Retrieve a standardized room category definition by ID."""
    normalized = category_id.strip().upper()
    if normalized not in ROOM_CATEGORIES:
        raise RoomCategoryNotFoundError(f"Room category {category_id!r} not found")
    return ROOM_CATEGORIES[normalized]


# ---------------------------------------------------------------------------
# Functional Location Category & Personnel Operations
# ---------------------------------------------------------------------------


async def set_fl_room_category(
    conn: Any | None,
    namespace_id: str | UUID,
    *,
    fl_id_or_label: str,
    category_id: str,
    actor: str | None = None,
) -> dict[str, Any]:
    """Associate a room category with a functional location node via kg_edges."""
    cat = get_room_category(category_id)
    node = await get_fl_node(conn, namespace_id, fl_id_or_label)

    await set_fl_category_edge(
        conn,
        namespace_id,
        fl_label=node["label"],
        category_id=cat["id"],
    )

    log.info(
        "set_fl_room_category: ns=%s fl=%s category=%s actor=%s",
        namespace_id,
        node["label"],
        cat["id"],
        actor,
    )
    return {
        "status": "ok",
        "fl_id": node["id"],
        "fl_label": node["label"],
        "category_id": cat["id"],
        "actor": actor,
    }


async def get_fl_room_category(
    conn: Any | None,
    namespace_id: str | UUID,
    *,
    fl_id_or_label: str,
) -> dict[str, Any]:
    """Retrieve the room category associated with a functional location node."""
    node = await get_fl_node(conn, namespace_id, fl_id_or_label)
    category_id = await get_fl_category_edge(conn, namespace_id, fl_label=node["label"])

    category_def = None
    if category_id:
        try:
            category_def = get_room_category(category_id)
        except RoomCategoryNotFoundError:
            category_def = {"id": category_id, "name": category_id}

    return {
        "fl_id": node["id"],
        "fl_label": node["label"],
        "category_id": category_id,
        "category": category_def,
    }


async def assign_fl_responsible(
    conn: Any | None,
    namespace_id: str | UUID,
    *,
    fl_id_or_label: str,
    employee_id: str,
    role: str = "primary",
    actor: str | None = None,
) -> dict[str, Any]:
    """Assign a responsible employee to a functional location with a specific role."""
    clean_emp = str(employee_id).strip()
    if not clean_emp:
        raise ValueError("employee_id cannot be empty")

    clean_role = str(role).strip().lower()
    if clean_role not in VALID_RESPONSIBLE_ROLES:
        raise InvalidResponsibleRoleError(
            f"Invalid role {role!r}. Valid roles are: {sorted(VALID_RESPONSIBLE_ROLES)}"
        )

    node = await get_fl_node(conn, namespace_id, fl_id_or_label)
    await assign_fl_responsible_edge(
        conn,
        namespace_id,
        fl_label=node["label"],
        employee_id=clean_emp,
        role=clean_role,
    )

    log.info(
        "assign_fl_responsible: ns=%s fl=%s employee=%s role=%s actor=%s",
        namespace_id,
        node["label"],
        clean_emp,
        clean_role,
        actor,
    )
    return {
        "status": "ok",
        "fl_id": node["id"],
        "fl_label": node["label"],
        "employee_id": clean_emp,
        "role": clean_role,
        "actor": actor,
    }


async def unassign_fl_responsible(
    conn: Any | None,
    namespace_id: str | UUID,
    *,
    fl_id_or_label: str,
    employee_id: str,
    actor: str | None = None,
) -> dict[str, Any]:
    """Remove a responsible employee assignment from a functional location."""
    clean_emp = str(employee_id).strip()
    if not clean_emp:
        raise ValueError("employee_id cannot be empty")

    node = await get_fl_node(conn, namespace_id, fl_id_or_label)
    removed = await unassign_fl_responsible_edge(
        conn,
        namespace_id,
        fl_label=node["label"],
        employee_id=clean_emp,
    )

    log.info(
        "unassign_fl_responsible: ns=%s fl=%s employee=%s removed=%s actor=%s",
        namespace_id,
        node["label"],
        clean_emp,
        removed,
        actor,
    )
    return {
        "status": "ok",
        "unassigned": removed,
        "fl_id": node["id"],
        "fl_label": node["label"],
        "employee_id": clean_emp,
        "actor": actor,
    }


async def list_fl_responsible(
    conn: Any | None,
    namespace_id: str | UUID,
    *,
    fl_id_or_label: str,
) -> list[dict[str, Any]]:
    """List all employees assigned to a functional location."""
    node = await get_fl_node(conn, namespace_id, fl_id_or_label)
    return await list_fl_responsible_edges(conn, namespace_id, fl_label=node["label"])


async def list_my_responsible_fls(
    conn: Any | None,
    namespace_id: str | UUID,
    *,
    principal: PrincipalContext | None = None,
    employee_id: str | None = None,
) -> list[dict[str, Any]]:
    """List functional locations assigned to the calling employee.

    C16 Security Invariant:
    If caller is not an employee tier, or missing an employee_id, returns []
    without raising an error or leaking cross-tenant data.
    """
    effective_emp_id: str | None = None

    if employee_id:
        effective_emp_id = str(employee_id).strip()
    elif principal is not None:
        if principal.tier == "employee" and principal.employee_id:
            effective_emp_id = str(principal.employee_id).strip()
        else:
            return []
    else:
        return []

    if not effective_emp_id:
        return []

    return await list_responsible_fl_edges_for_employee(
        conn, namespace_id, employee_id=effective_emp_id
    )


# ---------------------------------------------------------------------------
# Orchestrator / Domain Dispatchers (do_* wrappers)
# ---------------------------------------------------------------------------


def do_get_room_categories(
    engine: NCEEngine,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Retrieve room categories matching filter parameters."""
    p = params or {}
    min_cap = p.get("min_capacity")
    max_cap = p.get("max_capacity")
    return list_room_categories(
        q=p.get("q"),
        min_capacity=int(min_cap) if min_cap is not None else None,
        max_capacity=int(max_cap) if max_cap is not None else None,
    )


def do_get_room_category(
    engine: NCEEngine,
    category_id: str,
) -> dict[str, Any]:
    """Retrieve definition for a single room category."""
    return get_room_category(category_id)


async def do_set_fl_room_category(
    engine: NCEEngine,
    namespace_id: str | UUID,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Attach a room category to a functional location."""
    fl_id = str(params.get("fl_id") or params.get("fl_id_or_label") or "").strip()
    if not fl_id:
        raise ValueError("fl_id is required")
    cat_id = str(params.get("category_id") or "").strip()
    if not cat_id:
        raise ValueError("category_id is required")
    actor = params.get("actor")

    if getattr(engine, "pg_pool", None):
        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            return await set_fl_room_category(
                conn, namespace_id, fl_id_or_label=fl_id, category_id=cat_id, actor=actor
            )
    return await set_fl_room_category(
        None, namespace_id, fl_id_or_label=fl_id, category_id=cat_id, actor=actor
    )


async def do_get_fl_room_category(
    engine: NCEEngine,
    namespace_id: str | UUID,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Get assigned room category for a functional location."""
    fl_id = str(params.get("fl_id") or params.get("fl_id_or_label") or "").strip()
    if not fl_id:
        raise ValueError("fl_id is required")

    if getattr(engine, "pg_pool", None):
        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            return await get_fl_room_category(conn, namespace_id, fl_id_or_label=fl_id)
    return await get_fl_room_category(None, namespace_id, fl_id_or_label=fl_id)


async def do_assign_fl_responsible(
    engine: NCEEngine,
    namespace_id: str | UUID,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Assign responsible personnel to a functional location."""
    fl_id = str(params.get("fl_id") or params.get("fl_id_or_label") or "").strip()
    if not fl_id:
        raise ValueError("fl_id is required")
    emp_id = str(params.get("employee_id") or "").strip()
    if not emp_id:
        raise ValueError("employee_id is required")
    role = str(params.get("role") or "primary").strip()
    actor = params.get("actor")

    if getattr(engine, "pg_pool", None):
        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            return await assign_fl_responsible(
                conn,
                namespace_id,
                fl_id_or_label=fl_id,
                employee_id=emp_id,
                role=role,
                actor=actor,
            )
    return await assign_fl_responsible(
        None,
        namespace_id,
        fl_id_or_label=fl_id,
        employee_id=emp_id,
        role=role,
        actor=actor,
    )


async def do_unassign_fl_responsible(
    engine: NCEEngine,
    namespace_id: str | UUID,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Unassign responsible personnel from a functional location."""
    fl_id = str(params.get("fl_id") or params.get("fl_id_or_label") or "").strip()
    if not fl_id:
        raise ValueError("fl_id is required")
    emp_id = str(params.get("employee_id") or "").strip()
    if not emp_id:
        raise ValueError("employee_id is required")
    actor = params.get("actor")

    if getattr(engine, "pg_pool", None):
        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            return await unassign_fl_responsible(
                conn, namespace_id, fl_id_or_label=fl_id, employee_id=emp_id, actor=actor
            )
    return await unassign_fl_responsible(
        None, namespace_id, fl_id_or_label=fl_id, employee_id=emp_id, actor=actor
    )


async def do_list_fl_responsible(
    engine: NCEEngine,
    namespace_id: str | UUID,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    """List responsible personnel for a functional location."""
    fl_id = str(params.get("fl_id") or params.get("fl_id_or_label") or "").strip()
    if not fl_id:
        raise ValueError("fl_id is required")

    if getattr(engine, "pg_pool", None):
        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            return await list_fl_responsible(conn, namespace_id, fl_id_or_label=fl_id)
    return await list_fl_responsible(None, namespace_id, fl_id_or_label=fl_id)


async def do_list_my_responsible_fls(
    engine: NCEEngine,
    namespace_id: str | UUID,
    *,
    principal: PrincipalContext | None = None,
    employee_id: str | None = None,
) -> list[dict[str, Any]]:
    """List functional locations assigned to the current employee."""
    if getattr(engine, "pg_pool", None):
        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            return await list_my_responsible_fls(
                conn, namespace_id, principal=principal, employee_id=employee_id
            )
    return await list_my_responsible_fls(
        None, namespace_id, principal=principal, employee_id=employee_id
    )
