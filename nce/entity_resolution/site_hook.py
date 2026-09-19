"""C1 Site & Functional Location Building Resolution Hook — cadastre match key & merge queue gating.

Phase A Wave A-9:
Uses cadastre_id (C17 Site Master Data) as the strongest FL-building match key.
SCOPE LOCK / C1 Invariant:
Two FL buildings (or an FL building and a Site) with the same cadastre_id resolve
through the merge queue (entity_merge_queue), NEVER auto-merged.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from nce.entity_resolution.merge_queue import enqueue as _db_enqueue
from nce.vertical_modules.sites.service import get_site_by_cadastre_id, normalize_cadastre_id

# In-memory merge queue for hermetic unit testing
_MEM_MERGE_QUEUE: list[dict[str, Any]] = []

# In-memory registry of FL buildings for matching when running offline without DB
_MEM_FL_BUILDINGS: dict[str, dict[str, Any]] = {}


def _clear_hook_mem_store() -> None:
    """Clear in-memory merge queue and building items."""
    _MEM_MERGE_QUEUE.clear()
    _MEM_FL_BUILDINGS.clear()


def get_mem_merge_queue() -> list[dict[str, Any]]:
    """Return in-memory merge queue items for test assertions."""
    return list(_MEM_MERGE_QUEUE)


def register_mem_fl_building(
    namespace_id: str | UUID,
    building_id: str | UUID,
    cadastre_id: str,
    name: str = "",
) -> None:
    """Register an in-memory FL building for offline unit testing."""
    ns_key = str(namespace_id)
    cleaned = normalize_cadastre_id(cadastre_id)
    if cleaned:
        _MEM_FL_BUILDINGS.setdefault(ns_key, {})[cleaned] = {
            "id": UUID(str(building_id)),
            "cadastre_id": cleaned,
            "name": name,
        }


def _to_uuid(val: str | UUID) -> UUID:
    if isinstance(val, UUID):
        return val
    return UUID(str(val))


async def reconcile_fl_building_with_site(
    conn: Any,
    *,
    namespace_id: str | UUID,
    candidate: dict[str, Any],
    entity_type: str = "FUNCTIONAL_LOCATION",
    cadastre_id: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Reconcile a candidate FL building against C17 sites / existing FL buildings by cadastre_id.

    Invariant (Wave A-9 / C1):
      Cadastre ID is the FL-building match key.
      When a candidate FL building shares a cadastre_id with an existing site or FL building,
      it resolves through `entity_merge_queue` with status 'pending' (score=1.0).
      It NEVER auto-merges or mutates entity ownership/graphs directly.
    """
    ns_uuid = _to_uuid(namespace_id)
    raw_cadastre = (
        cadastre_id
        or candidate.get("cadastre_id")
        or candidate.get("cadastreId")
        or candidate.get("matrikkel_id")
    )
    if not raw_cadastre:
        return {
            "matched": False,
            "reason": "no_cadastre_id",
            "auto_merged": False,
        }

    cleaned = normalize_cadastre_id(str(raw_cadastre))
    if not cleaned:
        return {
            "matched": False,
            "reason": "invalid_cadastre_id",
            "auto_merged": False,
        }

    # 1. Look up existing site by cadastre_id
    target_id: UUID | None = None
    target_type: str = "SITE"

    site = await get_site_by_cadastre_id(conn, ns_uuid, cleaned)
    if site is not None:
        target_id = site.id
        target_type = "SITE"

    # 2. Check in-memory / DB FL buildings if no site matched
    if target_id is None:
        ns_key = str(ns_uuid)
        existing_fl = _MEM_FL_BUILDINGS.get(ns_key, {}).get(cleaned)
        if existing_fl:
            target_id = existing_fl["id"]
            target_type = "FUNCTIONAL_LOCATION"

    if target_id is None:
        return {
            "matched": False,
            "normalized_cadastre_id": cleaned,
            "reason": "not_found",
            "auto_merged": False,
        }

    # Match found: C1 strongest match key.
    # CRITICAL INVARIANT: NEVER auto-merge. Enqueue into entity_merge_queue for human review.
    queue_id: UUID
    if conn is not None:
        queue_id = await _db_enqueue(
            conn,
            namespace_id=ns_uuid,
            node_type=entity_type,
            candidate=candidate,
            target=target_id,
            score=1.0,
        )
    else:
        queue_id = uuid4()
        _MEM_MERGE_QUEUE.append(
            {
                "id": queue_id,
                "namespace_id": ns_uuid,
                "node_type": entity_type,
                "target_type": target_type,
                "candidate": candidate,
                "target_id": target_id,
                "cadastre_id": cleaned,
                "score": 1.0,
                "status": "pending",
                "auto_merged": False,
            }
        )

    return {
        "matched": True,
        "target_id": target_id,
        "target_type": target_type,
        "normalized_cadastre_id": cleaned,
        "score": 1.0,
        "queued_for_merge": True,
        "merge_queue_id": queue_id,
        "auto_merged": False,
        "status": "pending",
    }
