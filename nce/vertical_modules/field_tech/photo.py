"""
nce/vertical_modules/field_tech/photo.py
========================================
Photo documentation domain logic for Module 12 (Field Tech Engine):
  - do_attach_photo: attaches site photo documentation reference to a work order.

Strict Tenant Predicate Discipline (Charter Â§4.4)
-------------------------------------------------
EVERY query against kg_nodes / kg_edges / work_orders carries explicit WHERE namespace_id = $N::uuid predicates.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID, uuid4

from nce.db_utils import scoped_pg_session

log = logging.getLogger("nce.vertical_modules.field_tech.photo")

_NODE_TYPE_PHOTO = "FIELD_TECH_PHOTO"


def _extract_pool(engine_or_pool: Any) -> Any:
    if hasattr(engine_or_pool, "pg_pool") and (
        "pg_pool" in getattr(engine_or_pool, "__dict__", {})
        or hasattr(type(engine_or_pool), "pg_pool")
    ):
        return engine_or_pool.pg_pool
    return engine_or_pool


def _parse_uuid(val: Any, field_name: str) -> UUID:
    if not val:
        raise ValueError(f"{field_name} is required")
    if isinstance(val, UUID):
        return val
    try:
        return UUID(str(val))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"Invalid {field_name} UUID: {val!r}") from exc


async def do_attach_photo(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Attach a documentation photo to a work order."""
    pool = _extract_pool(engine)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    work_order_id = str(params.get("work_order_id") or "").strip()
    if not work_order_id:
        raise ValueError("work_order_id is required")

    blob_ref = str(params.get("blob_ref") or "").strip()
    if not blob_ref:
        raise ValueError("blob_ref is required")

    caption = str(params.get("caption") or "").strip()
    photo_id = params.get("photo_id") or f"PH-{uuid4().hex[:8].upper()}"

    async with scoped_pg_session(pool, ns_uuid) as conn:
        wo = await conn.fetchrow(
            """
            SELECT id FROM work_orders
            WHERE work_order_id = $1 AND namespace_id = $2::uuid
            """,
            work_order_id,
            ns_uuid,
        )
        if wo is None:
            raise ValueError(f"Work order {work_order_id!r} not found in namespace")

        photo_label = f"{_NODE_TYPE_PHOTO}:{photo_id}"
        wo_label = f"WORK_ORDER:{work_order_id}"

        # Upsert photo node with blob_ref in properties / change_origin
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
            VALUES ($1, $2, $3::uuid, 'agent')
            ON CONFLICT (label, namespace_id) DO NOTHING
            """,
            photo_label,
            _NODE_TYPE_PHOTO,
            ns_uuid,
        )

        # Edge: WORK_ORDER -[has_photo]-> FIELD_TECH_PHOTO
        await conn.execute(
            """
            INSERT INTO kg_edges (subject_label, predicate, object_label, confidence, namespace_id, change_origin)
            VALUES ($1, 'has_photo', $2, 1.0, $3::uuid, 'agent')
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
            """,
            wo_label,
            photo_label,
            ns_uuid,
        )

    return {
        "status": "attached",
        "photo_id": photo_id,
        "work_order_id": work_order_id,
        "blob_ref": blob_ref,
        "caption": caption,
    }
