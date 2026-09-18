"""nce.resource_surface.comments — Comments and tags management for resources.

Phase A Wave A-1:
Stores and queries comments and tags associated with any C12 resource.
Reuses the append-only v3_cognitive_ledger pattern from agreements/authoring.py,
ensuring RLS-enforced multi-tenant isolation without requiring schema migrations.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from nce.resource_surface.spec import ResourceSpec

_ZERO_TENSOR = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
_MODEL_VERSION = "v3.0-c12-resource"


async def append_entity_comment(
    conn: Any,
    namespace_id: UUID | str,
    spec: ResourceSpec,
    entity_id: str,
    comment_text: str,
    author: str = "operator",
) -> dict[str, Any]:
    """Append one comment entry for an entity into v3_cognitive_ledger."""
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    comment_id = uuid.uuid4()
    now_iso = datetime.now(timezone.utc).isoformat()

    payload = {
        "kind": "resource_comment",
        "entity_type": spec.node_type,
        "entity_id": str(entity_id),
        "comment": comment_text,
        "author": author,
        "created_at": now_iso,
    }

    query = """
        INSERT INTO v3_cognitive_ledger (
            id, namespace_id, memory_id,
            empathic_tensor, tlx_scores, vad_scores, model_version
        ) VALUES (
            $1::uuid, $2::uuid, NULL,
            $3::float[], $4::jsonb, $5::jsonb, $6
        )
    """
    await conn.execute(
        query,
        str(comment_id),
        str(ns_uuid),
        _ZERO_TENSOR,
        json.dumps(payload),
        json.dumps({}),
        _MODEL_VERSION,
    )

    return {
        "id": str(comment_id),
        "entity_id": str(entity_id),
        "comment": comment_text,
        "author": author,
        "created_at": now_iso,
    }


async def fetch_entity_comments(
    conn: Any,
    namespace_id: UUID | str,
    spec: ResourceSpec,
    entity_id: str,
) -> list[dict[str, Any]]:
    """Retrieve all comments for an entity, chronological order."""
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id

    query = """
        SELECT id, tlx_scores, created_at
        FROM v3_cognitive_ledger
        WHERE namespace_id = $1
          AND tlx_scores->>'kind' = 'resource_comment'
          AND tlx_scores->>'entity_id' = $2
        ORDER BY created_at ASC
    """
    rows = await conn.fetch(query, ns_uuid, str(entity_id))

    comments: list[dict[str, Any]] = []
    for r in rows:
        data = r["tlx_scores"]
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                data = {}
        comments.append(
            {
                "id": str(r["id"]),
                "entity_id": str(entity_id),
                "comment": data.get("comment", ""),
                "author": data.get("author", "operator"),
                "created_at": data.get("created_at") or r["created_at"].isoformat(),
            }
        )
    return comments


async def add_entity_tag(
    conn: Any,
    namespace_id: UUID | str,
    spec: ResourceSpec,
    entity_id: str,
    tag: str,
    author: str = "operator",
) -> list[str]:
    """Add a tag to an entity."""
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    clean_tag = tag.strip().lower()
    if not clean_tag:
        return await fetch_entity_tags(conn, ns_uuid, spec, entity_id)

    tag_entry_id = uuid.uuid4()
    now_iso = datetime.now(timezone.utc).isoformat()
    payload = {
        "kind": "resource_tag_event",
        "entity_type": spec.node_type,
        "entity_id": str(entity_id),
        "tag": clean_tag,
        "action": "add",
        "author": author,
        "created_at": now_iso,
    }

    query = """
        INSERT INTO v3_cognitive_ledger (
            id, namespace_id, memory_id,
            empathic_tensor, tlx_scores, vad_scores, model_version
        ) VALUES (
            $1::uuid, $2::uuid, NULL,
            $3::float[], $4::jsonb, $5::jsonb, $6
        )
    """
    await conn.execute(
        query,
        str(tag_entry_id),
        str(ns_uuid),
        _ZERO_TENSOR,
        json.dumps(payload),
        json.dumps({}),
        _MODEL_VERSION,
    )
    return await fetch_entity_tags(conn, ns_uuid, spec, entity_id)


async def remove_entity_tag(
    conn: Any,
    namespace_id: UUID | str,
    spec: ResourceSpec,
    entity_id: str,
    tag: str,
    author: str = "operator",
) -> list[str]:
    """Remove a tag from an entity."""
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    clean_tag = tag.strip().lower()
    if not clean_tag:
        return await fetch_entity_tags(conn, ns_uuid, spec, entity_id)

    tag_entry_id = uuid.uuid4()
    now_iso = datetime.now(timezone.utc).isoformat()
    payload = {
        "kind": "resource_tag_event",
        "entity_type": spec.node_type,
        "entity_id": str(entity_id),
        "tag": clean_tag,
        "action": "remove",
        "author": author,
        "created_at": now_iso,
    }

    query = """
        INSERT INTO v3_cognitive_ledger (
            id, namespace_id, memory_id,
            empathic_tensor, tlx_scores, vad_scores, model_version
        ) VALUES (
            $1::uuid, $2::uuid, NULL,
            $3::float[], $4::jsonb, $5::jsonb, $6
        )
    """
    await conn.execute(
        query,
        str(tag_entry_id),
        str(ns_uuid),
        _ZERO_TENSOR,
        json.dumps(payload),
        json.dumps({}),
        _MODEL_VERSION,
    )
    return await fetch_entity_tags(conn, ns_uuid, spec, entity_id)


async def fetch_entity_tags(
    conn: Any,
    namespace_id: UUID | str,
    spec: ResourceSpec,
    entity_id: str,
) -> list[str]:
    """Calculate current active tags for an entity by replaying tag events."""
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id

    query = """
        SELECT tlx_scores
        FROM v3_cognitive_ledger
        WHERE namespace_id = $1
          AND tlx_scores->>'kind' = 'resource_tag_event'
          AND tlx_scores->>'entity_id' = $2
        ORDER BY created_at ASC
    """
    rows = await conn.fetch(query, ns_uuid, str(entity_id))

    active_tags: set[str] = set()
    for r in rows:
        data = r["tlx_scores"]
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                continue
        tag = data.get("tag")
        action = data.get("action")
        if tag and action == "add":
            active_tags.add(tag)
        elif tag and action == "remove":
            active_tags.discard(tag)

    return sorted(active_tags)
