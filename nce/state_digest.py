"""
Namespace state digest calculator for byte-identical verification.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

from nce.config import cfg
from nce.temporal import as_of_query

log = logging.getLogger(__name__)


async def compute_namespace_state_digest(
    conn: asyncpg.Connection,
    ns: uuid.UUID,
    *,
    as_of: datetime | None = None,
) -> str:
    """
    Compute SHA-256 over a canonical sorted projection of durable, deterministic state
    for namespace `ns`.
    """
    # 1. Fetch memories
    clause, as_of_params = as_of_query("", as_of, start_index=2)
    sql_memories = f"""
        SELECT id, agent_id, created_at, memory_type, assertion_type,
               valid_from, valid_to, derived_from, metadata, payload_ref
        FROM memories
        WHERE namespace_id = $1 {clause}
    """
    mem_rows = await conn.fetch(sql_memories, ns, *as_of_params)

    # 2. Build target-to-source ID remapping mapping
    id_map: dict[str, str] = {}
    for m in mem_rows:
        metadata_val = m["metadata"]
        meta_dict: dict[str, Any] = {}
        if isinstance(metadata_val, str):
            try:
                meta_dict = json.loads(metadata_val)
            except Exception:
                pass
        elif isinstance(metadata_val, dict):
            meta_dict = metadata_val

        src_mem_id = meta_dict.get("source_memory_id") if meta_dict else None
        m_id_str = str(m["id"])
        if src_mem_id:
            id_map[m_id_str] = str(src_mem_id)
        else:
            id_map[m_id_str] = m_id_str

    # 3. Bulk-fetch payload contents from MongoDB
    episode_contents: dict[str, str] = {}
    code_contents: dict[str, str] = {}

    episode_oids: list[ObjectId] = []
    code_oids: list[ObjectId] = []
    for m in mem_rows:
        ref = m["payload_ref"]
        if not ref:
            continue
        try:
            oid = ObjectId(ref)
            if m["memory_type"] == "code_chunk":
                code_oids.append(oid)
            else:
                episode_oids.append(oid)
        except Exception:
            pass

    mongo_client: AsyncIOMotorClient | None = None
    try:
        from nce.db_utils import scoped_mongo_session

        mongo_client = AsyncIOMotorClient(cfg.MONGO_URI, serverSelectionTimeoutMS=2000)
        async with scoped_mongo_session(mongo_client, ns) as s_db:
            if episode_oids:
                cursor = s_db.episodes.find(
                    {"_id": {"$in": episode_oids}}, projection={"raw_data": 1}
                )
                async for doc in cursor:
                    episode_contents[str(doc["_id"])] = doc.get("raw_data") or ""
            if code_oids:
                cursor = s_db.code_files.find(
                    {"_id": {"$in": code_oids}}, projection={"raw_code": 1}
                )
                async for doc in cursor:
                    code_contents[str(doc["_id"])] = doc.get("raw_code") or ""
    except Exception as exc:
        log.warning("Failed to connect to MongoDB or fetch payloads for digest: %s", exc)
    finally:
        if mongo_client:
            mongo_client.close()

    # 4. Canonicalize memories
    canonical_memories: list[dict[str, Any]] = []
    for m in mem_rows:
        m_id_str = str(m["id"])
        norm_id = id_map.get(m_id_str, m_id_str)

        # Parse derived_from and map UUIDs back
        derived_from_val = m["derived_from"]
        derived_list: list[str] = []
        if derived_from_val:
            raw_list: list[Any] = []
            if isinstance(derived_from_val, str):
                try:
                    raw_list = json.loads(derived_from_val)
                except Exception:
                    pass
            elif isinstance(derived_from_val, list):
                raw_list = derived_from_val

            for item in raw_list:
                item_str = str(item)
                derived_list.append(id_map.get(item_str, item_str))
        derived_list.sort()

        # Parse and sanitize metadata
        metadata_val = m["metadata"]
        meta_dict = {}
        if isinstance(metadata_val, str):
            try:
                meta_dict = json.loads(metadata_val)
            except Exception:
                pass
        elif isinstance(metadata_val, dict):
            meta_dict = metadata_val

        # Exclude target-specific / replay-specific metadata keys
        exclude_keys = {
            "source_memory_id",
            "replay_fork",
            "source_memory_ids",
            "key_entities",
            "key_relations",
        }
        sanitized_meta = {k: v for k, v in meta_dict.items() if k not in exclude_keys}

        # Resolve payload hash
        ref = m["payload_ref"]
        content_str = ""
        if ref:
            if m["memory_type"] == "code_chunk":
                content_str = code_contents.get(str(ref), "")
            else:
                content_str = episode_contents.get(str(ref), "")
        # Normalise line endings to ensure OS-independent hashes
        content_str = content_str.replace("\r\n", "\n")
        payload_hash = hashlib.sha256(content_str.encode("utf-8")).hexdigest()

        # Format timestamps
        created_at_str = (
            m["created_at"].astimezone(timezone.utc).isoformat() if m["created_at"] else ""
        )
        valid_from_str = (
            m["valid_from"].astimezone(timezone.utc).isoformat() if m["valid_from"] else ""
        )
        valid_to_str = m["valid_to"].astimezone(timezone.utc).isoformat() if m["valid_to"] else ""

        canonical_memories.append(
            {
                "id": norm_id,
                "agent_id": m["agent_id"],
                "created_at": created_at_str,
                "memory_type": m["memory_type"],
                "assertion_type": m["assertion_type"],
                "valid_from": valid_from_str,
                "valid_to": valid_to_str,
                "derived_from": derived_list,
                "metadata": sanitized_meta,
                "payload_hash": payload_hash,
            }
        )

    # Sort memories alphabetically by normalized ID
    canonical_memories.sort(key=lambda x: x["id"])

    # 5. Fetch and canonicalize KG edges
    sql_kg = """
        SELECT subject_label, predicate, object_label, confidence
        FROM kg_edges
        WHERE namespace_id = $1
    """
    kg_rows = await conn.fetch(sql_kg, ns)

    canonical_kg: list[dict[str, Any]] = []
    for r in kg_rows:
        canonical_kg.append(
            {
                "subject_label": r["subject_label"],
                "predicate": r["predicate"],
                "object_label": r["object_label"],
                "confidence": float(r["confidence"]),
            }
        )

    # Sort KG edges lexicographically by subject, predicate, object, confidence
    canonical_kg.sort(
        key=lambda x: (x["subject_label"], x["predicate"], x["object_label"], x["confidence"])
    )

    # 6. Combine and hash
    state_data = {
        "memories": canonical_memories,
        "kg_edges": canonical_kg,
    }
    canonical_str = json.dumps(state_data, sort_keys=True)
    return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()
