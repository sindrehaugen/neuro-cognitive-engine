"""nce.resource_surface.events — Entity event timeline queries from event_log.

Phase A Wave A-1:
Extracts correlated historical events for a given resource instance from event_log,
filtering by declared event contracts in EVENT_CATALOGUE and entity identifiers.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from nce.events.catalogue import EVENT_CATALOGUE
from nce.resource_surface.spec import ResourceSpec


def get_selectors_for_node_type(node_type: str) -> list[str]:
    """Retrieve all catalogued event selectors matching the given node_type."""
    return [
        contract.selector
        for contract in EVENT_CATALOGUE.values()
        if contract.node_type == node_type
    ]


async def fetch_entity_events(
    conn: Any,
    namespace_id: UUID | str,
    spec: ResourceSpec,
    entity_id: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Query event_log for events associated with this resource instance.

    Finds events where:
      1. namespace matches namespace_id
      2. payload contains matching entity id (via 'id', 'entity_id', or 'node_id')
         OR event_type matches one of the declared selectors for this node_type.
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    selectors = get_selectors_for_node_type(spec.node_type)

    query = """
        SELECT id, event_type, payload, occurred_at, event_seq
        FROM event_log
        WHERE namespace_id = $1
          AND (
            payload->>'id' = $2
            OR payload->>'entity_id' = $2
            OR payload->>'node_id' = $2
            OR ($3::text[] IS NOT NULL AND event_type = ANY($3::text[]) AND (payload->>'id' = $2 OR payload->>'entity_id' = $2))
          )
        ORDER BY occurred_at DESC, event_seq DESC
        LIMIT $4
    """
    rows = await conn.fetch(
        query, ns_uuid, str(entity_id), selectors or None, max(1, min(limit, 500))
    )

    results: list[dict[str, Any]] = []
    for r in rows:
        payload_data = r["payload"]
        if isinstance(payload_data, str):
            try:
                payload_data = json.loads(payload_data)
            except Exception:
                pass

        results.append(
            {
                "id": str(r["id"]),
                "event_type": r["event_type"],
                "occurred_at": r["occurred_at"].isoformat()
                if hasattr(r["occurred_at"], "isoformat")
                else str(r["occurred_at"]),
                "event_seq": r["event_seq"],
                "payload": payload_data,
            }
        )
    return results
