"""C1 Legal Entity Resolution Hook — strongest match key & merge queue gating.

Phase A Wave A-5:
Uses normalized org.nr (C15 Legal Entity Register) as the strongest customer/vendor
match key.
SCOPE LOCK / C1 Invariant:
A customer and vendor with the same org_nr resolve to one legal entity through the
merge queue (entity_merge_queue), NEVER auto-merged.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from nce.entity_resolution.merge_queue import enqueue as _db_enqueue
from nce.entity_resolution.normalizers import normalize_org_nr
from nce.vertical_modules.legal_entities.service import get_legal_entity_by_org_nr

# In-memory merge queue for hermetic unit testing
_MEM_MERGE_QUEUE: list[dict[str, Any]] = []


def _clear_hook_mem_store() -> None:
    """Clear in-memory merge queue items."""
    _MEM_MERGE_QUEUE.clear()


def get_mem_merge_queue() -> list[dict[str, Any]]:
    """Return in-memory merge queue items for test assertions."""
    return list(_MEM_MERGE_QUEUE)


def _to_uuid(val: str | UUID) -> UUID:
    if isinstance(val, UUID):
        return val
    return UUID(str(val))


async def reconcile_with_legal_entity(
    conn: Any,
    *,
    namespace_id: str | UUID,
    entity_type: str,
    candidate: dict[str, Any],
    org_nr: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Reconcile a candidate entity (e.g. customer, vendor) against C15 legal entities.

    Invariant:
      When a candidate matches an existing legal entity by org_nr,
      it resolves through `entity_merge_queue` with status 'pending' (score=1.0).
      It NEVER auto-merges or mutates entity ownership/graphs directly.
    """
    ns_uuid = _to_uuid(namespace_id)
    raw_org_nr = (
        org_nr
        or candidate.get("org_nr")
        or candidate.get("orgnr")
        or candidate.get("organization_number")
    )
    if not raw_org_nr:
        return {
            "matched": False,
            "reason": "no_org_nr",
            "auto_merged": False,
        }

    cleaned = normalize_org_nr(str(raw_org_nr))
    if not cleaned:
        return {
            "matched": False,
            "reason": "invalid_org_nr",
            "auto_merged": False,
        }

    # Look up existing legal entity by normalized org_nr
    le = await get_legal_entity_by_org_nr(conn, ns_uuid, cleaned)
    if le is None:
        return {
            "matched": False,
            "normalized_org_nr": cleaned,
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
            target=le.id,
            score=1.0,
        )
    else:
        queue_id = uuid4()
        _MEM_MERGE_QUEUE.append(
            {
                "id": queue_id,
                "namespace_id": ns_uuid,
                "node_type": entity_type,
                "candidate": candidate,
                "target_id": le.id,
                "score": 1.0,
                "status": "pending",
            }
        )

    return {
        "matched": True,
        "legal_entity_id": le.id,
        "normalized_org_nr": cleaned,
        "score": 1.0,
        "queued_for_merge": True,
        "merge_queue_id": queue_id,
        "auto_merged": False,
        "status": "pending",
    }
