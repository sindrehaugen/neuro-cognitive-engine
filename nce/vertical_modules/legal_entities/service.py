"""nce.vertical_modules.legal_entities.service — Domain operations for C15 Legal-Entity Register.

Phase A Wave A-5:
Implements registration, lookup, role management, and group structure queries.
Multi-tenant isolated and RLS protected.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from nce.entity_resolution.normalizers import normalize_org_nr
from nce.vertical_modules.legal_entities.models import LegalEntityRecord

# In-memory store for hermetic unit testing and offline harness execution
_MEM_LEGAL_ENTITIES: dict[str, dict[str, LegalEntityRecord]] = {}


def _clear_mem_store() -> None:
    """Clear in-memory storage for hermetic test execution."""
    _MEM_LEGAL_ENTITIES.clear()


# ===========================================================================
# 1. Legal Entity Operations
# ===========================================================================


async def register_legal_entity(
    conn: Any,
    namespace_id: UUID,
    org_nr: str,
    name: str,
    *,
    group_parent_org_nr: str | None = None,
    roles: tuple[str, ...] | list[str] = (),
    country: str = "NO",
    metadata: dict[str, Any] | None = None,
) -> LegalEntityRecord:
    """Register a new legal entity in the master legal-entity register.

    Normalizes org_nr and group_parent_org_nr prior to storage.
    """
    cleaned_org_nr = normalize_org_nr(org_nr)
    cleaned_parent_org_nr = normalize_org_nr(group_parent_org_nr) if group_parent_org_nr else None
    roles_tuple = tuple(sorted(set(roles)))
    entity_id = uuid4()
    now = datetime.now(timezone.utc)
    meta = metadata or {}

    if conn is not None:
        query = """
            INSERT INTO legal_entities (
                id, namespace_id, org_nr, name, group_parent_org_nr,
                roles, country, metadata, archived, created_at, updated_at
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6::jsonb, $7, $8::jsonb, FALSE, $9, $9
            )
            RETURNING id, namespace_id, org_nr, name, group_parent_org_nr,
                      roles, country, metadata, archived, created_at, updated_at
        """
        row = await conn.fetchrow(
            query,
            entity_id,
            namespace_id,
            cleaned_org_nr,
            name,
            cleaned_parent_org_nr,
            json.dumps(list(roles_tuple)),
            country,
            json.dumps(meta),
            now,
        )
        roles_val = row["roles"]
        if isinstance(roles_val, str):
            roles_val = json.loads(roles_val)
        meta_val = row["metadata"]
        if isinstance(meta_val, str):
            meta_val = json.loads(meta_val)
        return LegalEntityRecord(
            id=row["id"],
            namespace_id=row["namespace_id"],
            org_nr=row["org_nr"],
            name=row["name"],
            group_parent_org_nr=row["group_parent_org_nr"],
            roles=tuple(roles_val or ()),
            country=row["country"],
            metadata=dict(meta_val or {}),
            archived=row["archived"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    ns_key = str(namespace_id)
    # Check uniqueness in mem store
    existing_bucket = _MEM_LEGAL_ENTITIES.get(ns_key, {})
    for existing in existing_bucket.values():
        if existing.org_nr == cleaned_org_nr and not existing.archived:
            raise ValueError(
                f"Legal entity with org_nr {cleaned_org_nr} already exists in namespace {namespace_id}"
            )

    rec = LegalEntityRecord(
        id=entity_id,
        namespace_id=namespace_id,
        org_nr=cleaned_org_nr,
        name=name,
        group_parent_org_nr=cleaned_parent_org_nr,
        roles=roles_tuple,
        country=country,
        metadata=meta,
        archived=False,
        created_at=now,
        updated_at=now,
    )
    _MEM_LEGAL_ENTITIES.setdefault(ns_key, {})[str(entity_id)] = rec
    return rec


async def get_legal_entity(
    conn: Any,
    namespace_id: UUID,
    entity_id: UUID,
) -> LegalEntityRecord | None:
    """Retrieve a legal entity by its primary UUID."""
    if conn is not None:
        query = """
            SELECT id, namespace_id, org_nr, name, group_parent_org_nr,
                   roles, country, metadata, archived, created_at, updated_at
            FROM legal_entities
            WHERE namespace_id = $1 AND id = $2
        """
        row = await conn.fetchrow(query, namespace_id, entity_id)
        if not row:
            return None
        roles_val = row["roles"]
        if isinstance(roles_val, str):
            roles_val = json.loads(roles_val)
        meta_val = row["metadata"]
        if isinstance(meta_val, str):
            meta_val = json.loads(meta_val)
        return LegalEntityRecord(
            id=row["id"],
            namespace_id=row["namespace_id"],
            org_nr=row["org_nr"],
            name=row["name"],
            group_parent_org_nr=row["group_parent_org_nr"],
            roles=tuple(roles_val or ()),
            country=row["country"],
            metadata=dict(meta_val or {}),
            archived=row["archived"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    return _MEM_LEGAL_ENTITIES.get(str(namespace_id), {}).get(str(entity_id))


async def get_legal_entity_by_org_nr(
    conn: Any,
    namespace_id: UUID,
    org_nr: str,
) -> LegalEntityRecord | None:
    """Retrieve a legal entity by its normalized org number."""
    cleaned = normalize_org_nr(org_nr)
    if not cleaned:
        return None

    if conn is not None:
        query = """
            SELECT id, namespace_id, org_nr, name, group_parent_org_nr,
                   roles, country, metadata, archived, created_at, updated_at
            FROM legal_entities
            WHERE namespace_id = $1 AND org_nr = $2
        """
        row = await conn.fetchrow(query, namespace_id, cleaned)
        if not row:
            return None
        roles_val = row["roles"]
        if isinstance(roles_val, str):
            roles_val = json.loads(roles_val)
        meta_val = row["metadata"]
        if isinstance(meta_val, str):
            meta_val = json.loads(meta_val)
        return LegalEntityRecord(
            id=row["id"],
            namespace_id=row["namespace_id"],
            org_nr=row["org_nr"],
            name=row["name"],
            group_parent_org_nr=row["group_parent_org_nr"],
            roles=tuple(roles_val or ()),
            country=row["country"],
            metadata=dict(meta_val or {}),
            archived=row["archived"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    for rec in _MEM_LEGAL_ENTITIES.get(str(namespace_id), {}).values():
        if rec.org_nr == cleaned:
            return rec
    return None


async def list_legal_entities(
    conn: Any,
    namespace_id: UUID,
    *,
    role: str | None = None,
    country: str | None = None,
    archived: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[LegalEntityRecord]:
    """List legal entities for a tenant with optional filtering."""
    if conn is not None:
        params: list[Any] = [namespace_id, archived]
        clauses = ["namespace_id = $1", "archived = $2"]

        if role:
            params.append(json.dumps([role]))
            clauses.append(f"roles @> ${len(params)}::jsonb")

        if country:
            params.append(country)
            clauses.append(f"country = ${len(params)}")

        params.append(limit)
        limit_idx = len(params)
        params.append(offset)
        offset_idx = len(params)

        query = f"""
            SELECT id, namespace_id, org_nr, name, group_parent_org_nr,
                   roles, country, metadata, archived, created_at, updated_at
            FROM legal_entities
            WHERE {" AND ".join(clauses)}
            ORDER BY created_at DESC
            LIMIT ${limit_idx} OFFSET ${offset_idx}
        """
        rows = await conn.fetch(query, *params)
        results: list[LegalEntityRecord] = []
        for row in rows:
            roles_val = row["roles"]
            if isinstance(roles_val, str):
                roles_val = json.loads(roles_val)
            meta_val = row["metadata"]
            if isinstance(meta_val, str):
                meta_val = json.loads(meta_val)
            results.append(
                LegalEntityRecord(
                    id=row["id"],
                    namespace_id=row["namespace_id"],
                    org_nr=row["org_nr"],
                    name=row["name"],
                    group_parent_org_nr=row["group_parent_org_nr"],
                    roles=tuple(roles_val or ()),
                    country=row["country"],
                    metadata=dict(meta_val or {}),
                    archived=row["archived"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            )
        return results

    records = list(_MEM_LEGAL_ENTITIES.get(str(namespace_id), {}).values())
    filtered = [
        r
        for r in records
        if r.archived == archived
        and (role is None or role in r.roles)
        and (country is None or r.country == country)
    ]
    filtered.sort(key=lambda r: r.created_at, reverse=True)
    return filtered[offset : offset + limit]


async def update_legal_entity(
    conn: Any,
    namespace_id: UUID,
    entity_id: UUID,
    *,
    name: str | None = None,
    group_parent_org_nr: str | None = None,
    roles: tuple[str, ...] | list[str] | None = None,
    country: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> LegalEntityRecord | None:
    """Update fields on an existing legal entity."""
    existing = await get_legal_entity(conn, namespace_id, entity_id)
    if not existing:
        return None

    now = datetime.now(timezone.utc)
    new_name = name if name is not None else existing.name
    new_parent = (
        normalize_org_nr(group_parent_org_nr)
        if group_parent_org_nr is not None
        else existing.group_parent_org_nr
    )
    new_roles = tuple(sorted(set(roles))) if roles is not None else existing.roles
    new_country = country if country is not None else existing.country
    new_meta = (
        {**existing.metadata, **(metadata or {})} if metadata is not None else existing.metadata
    )

    if conn is not None:
        query = """
            UPDATE legal_entities
            SET name = $1,
                group_parent_org_nr = $2,
                roles = $3::jsonb,
                country = $4,
                metadata = $5::jsonb,
                updated_at = $6
            WHERE namespace_id = $7 AND id = $8
            RETURNING id, namespace_id, org_nr, name, group_parent_org_nr,
                      roles, country, metadata, archived, created_at, updated_at
        """
        row = await conn.fetchrow(
            query,
            new_name,
            new_parent,
            json.dumps(list(new_roles)),
            new_country,
            json.dumps(new_meta),
            now,
            namespace_id,
            entity_id,
        )
        if not row:
            return None
        roles_val = row["roles"]
        if isinstance(roles_val, str):
            roles_val = json.loads(roles_val)
        meta_val = row["metadata"]
        if isinstance(meta_val, str):
            meta_val = json.loads(meta_val)
        return LegalEntityRecord(
            id=row["id"],
            namespace_id=row["namespace_id"],
            org_nr=row["org_nr"],
            name=row["name"],
            group_parent_org_nr=row["group_parent_org_nr"],
            roles=tuple(roles_val or ()),
            country=row["country"],
            metadata=dict(meta_val or {}),
            archived=row["archived"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    updated = LegalEntityRecord(
        id=existing.id,
        namespace_id=existing.namespace_id,
        org_nr=existing.org_nr,
        name=new_name,
        group_parent_org_nr=new_parent,
        roles=new_roles,
        country=new_country,
        metadata=new_meta,
        archived=existing.archived,
        created_at=existing.created_at,
        updated_at=now,
    )
    _MEM_LEGAL_ENTITIES[str(namespace_id)][str(entity_id)] = updated
    return updated


async def add_role_to_legal_entity(
    conn: Any,
    namespace_id: UUID,
    entity_id: UUID,
    role: str,
) -> LegalEntityRecord | None:
    """Add a role (e.g. 'customer', 'vendor') to an existing legal entity."""
    existing = await get_legal_entity(conn, namespace_id, entity_id)
    if not existing:
        return None
    if role in existing.roles:
        return existing
    updated_roles = tuple(sorted(set(existing.roles) | {role}))
    return await update_legal_entity(conn, namespace_id, entity_id, roles=updated_roles)


async def archive_legal_entity(
    conn: Any,
    namespace_id: UUID,
    entity_id: UUID,
) -> bool:
    """Soft-archive a legal entity."""
    if conn is not None:
        query = """
            UPDATE legal_entities
            SET archived = TRUE, updated_at = NOW()
            WHERE namespace_id = $1 AND id = $2 AND archived = FALSE
            RETURNING id
        """
        row = await conn.fetchrow(query, namespace_id, entity_id)
        return row is not None

    rec = _MEM_LEGAL_ENTITIES.get(str(namespace_id), {}).get(str(entity_id))
    if rec and not rec.archived:
        now = datetime.now(timezone.utc)
        _MEM_LEGAL_ENTITIES[str(namespace_id)][str(entity_id)] = LegalEntityRecord(
            id=rec.id,
            namespace_id=rec.namespace_id,
            org_nr=rec.org_nr,
            name=rec.name,
            group_parent_org_nr=rec.group_parent_org_nr,
            roles=rec.roles,
            country=rec.country,
            metadata=rec.metadata,
            archived=True,
            created_at=rec.created_at,
            updated_at=now,
        )
        return True
    return False
