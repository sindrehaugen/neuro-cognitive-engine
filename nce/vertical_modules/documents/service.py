"""nce.vertical_modules.documents.service — Domain operations for C14 Document Register.

Phase A Wave A-4:
Implements registry, linking, share token generation, and validation.
Critical Invariant:
  No code path deletes at the external storage source (SharePoint, MinIO, S3).
  Archive and unlink operations manipulate the NCE register and link edges only.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from nce.vertical_modules.documents.models import (
    DocumentLinkRecord,
    DocumentRecord,
    DocumentShareRecord,
)

# In-memory stores for unit testing and offline harness execution
_MEM_DOCUMENTS: dict[str, dict[str, DocumentRecord]] = {}
_MEM_LINKS: dict[str, list[DocumentLinkRecord]] = {}
_MEM_SHARES: dict[str, dict[str, DocumentShareRecord]] = {}


def _parse_metadata(value: Any) -> dict[str, Any]:
    """Normalise a ``documents.metadata`` jsonb column's value to a dict.

    This pool registers no jsonb codec (confirmed by
    tests/test_semantic_search_metadata_text.py's own docstring), so
    asyncpg hands back the RAW JSON TEXT for a jsonb column, never a
    decoded dict -- ``dict(a_json_string)`` doesn't parse it, it tries to
    treat the string as an iterable of key-value pairs and raises
    ``ValueError: dictionary update sequence element #0 has length 1; 2
    is required`` for every row, including the schema's own default empty
    ``'{}'::jsonb``. Every real Postgres read of this column in this file
    hit this before the fix; the in-memory fallback's own ``metadata``
    value is already a native dict (never round-tripped through
    Postgres), so it is returned as-is rather than re-parsed. Same
    str-or-dict shape ``nce/resource_surface/comments.py``'s
    ``fetch_entity_comments``/``fetch_entity_tags`` already use for their
    own jsonb columns on the same pool.
    """
    if isinstance(value, str):
        try:
            return json.loads(value) if value else {}
        except (TypeError, ValueError):
            return {}
    return dict(value or {})


def _clear_mem_store() -> None:
    """Clear in-memory storage for hermetic test execution."""
    _MEM_DOCUMENTS.clear()
    _MEM_LINKS.clear()
    _MEM_SHARES.clear()


# ===========================================================================
# 1. Document Registry Operations
# ===========================================================================


async def register_document(
    conn: Any,
    namespace_id: UUID,
    title: str,
    document_ref: str,
    *,
    source_kind: str = "sharepoint",
    document_kind: str = "other",
    file_name: str | None = None,
    mime_type: str | None = None,
    file_size_bytes: int | None = None,
    sha256: str | None = None,
    tags: tuple[str, ...] = (),
    metadata: dict[str, Any] | None = None,
) -> DocumentRecord:
    """Register a new document in the master document register."""
    doc_id = uuid4()
    now = datetime.now(timezone.utc)
    meta = metadata or {}

    if conn is not None:
        query = """
            INSERT INTO documents (
                id, namespace_id, title, document_ref, source_kind, document_kind,
                file_name, mime_type, file_size_bytes, sha256, tags, metadata,
                archived, created_at, updated_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6,
                $7, $8, $9, $10, $11, $12,
                FALSE, $13, $13
            )
            RETURNING id, namespace_id, title, document_ref, source_kind, document_kind,
                      file_name, mime_type, file_size_bytes, sha256, tags, metadata,
                      archived, created_at, updated_at
        """
        row = await conn.fetchrow(
            query,
            doc_id,
            namespace_id,
            title,
            document_ref,
            source_kind,
            document_kind,
            file_name,
            mime_type,
            file_size_bytes,
            sha256,
            list(tags),
            json.dumps(meta),
            now,
        )
        return DocumentRecord(
            id=row["id"],
            namespace_id=row["namespace_id"],
            title=row["title"],
            document_ref=row["document_ref"],
            source_kind=row["source_kind"],
            document_kind=row["document_kind"],
            file_name=row["file_name"],
            mime_type=row["mime_type"],
            file_size_bytes=row["file_size_bytes"],
            sha256=row["sha256"],
            tags=tuple(row["tags"] or ()),
            metadata=_parse_metadata(row["metadata"]),
            archived=row["archived"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # In-memory path
    ns_key = str(namespace_id)
    doc = DocumentRecord(
        id=doc_id,
        namespace_id=namespace_id,
        title=title,
        document_ref=document_ref,
        source_kind=source_kind,
        document_kind=document_kind,
        file_name=file_name,
        mime_type=mime_type,
        file_size_bytes=file_size_bytes,
        sha256=sha256,
        tags=tags,
        metadata=meta,
        archived=False,
        created_at=now,
        updated_at=now,
    )
    _MEM_DOCUMENTS.setdefault(ns_key, {})[str(doc_id)] = doc
    return doc


async def get_document(
    conn: Any,
    namespace_id: UUID,
    document_id: UUID,
) -> DocumentRecord | None:
    """Retrieve document metadata by document ID."""
    if conn is not None:
        query = """
            SELECT id, namespace_id, title, document_ref, source_kind, document_kind,
                   file_name, mime_type, file_size_bytes, sha256, tags, metadata,
                   archived, created_at, updated_at
            FROM documents
            WHERE namespace_id = $1 AND id = $2
        """
        row = await conn.fetchrow(query, namespace_id, document_id)
        if not row:
            return None
        return DocumentRecord(
            id=row["id"],
            namespace_id=row["namespace_id"],
            title=row["title"],
            document_ref=row["document_ref"],
            source_kind=row["source_kind"],
            document_kind=row["document_kind"],
            file_name=row["file_name"],
            mime_type=row["mime_type"],
            file_size_bytes=row["file_size_bytes"],
            sha256=row["sha256"],
            tags=tuple(row["tags"] or ()),
            metadata=_parse_metadata(row["metadata"]),
            archived=row["archived"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    return _MEM_DOCUMENTS.get(str(namespace_id), {}).get(str(document_id))


async def archive_document(
    conn: Any,
    namespace_id: UUID,
    document_id: UUID,
) -> bool:
    """Soft-archive a document in NCE.

    CRITICAL INVARIANT (ADR 0041):
    This never deletes or mutates the document at external storage sources.
    """
    now = datetime.now(timezone.utc)
    if conn is not None:
        query = """
            UPDATE documents
            SET archived = TRUE, updated_at = $3
            WHERE namespace_id = $1 AND id = $2
            RETURNING id
        """
        row = await conn.fetchrow(query, namespace_id, document_id, now)
        return row is not None

    ns_key = str(namespace_id)
    doc_key = str(document_id)
    if ns_key in _MEM_DOCUMENTS and doc_key in _MEM_DOCUMENTS[ns_key]:
        old = _MEM_DOCUMENTS[ns_key][doc_key]
        _MEM_DOCUMENTS[ns_key][doc_key] = DocumentRecord(
            id=old.id,
            namespace_id=old.namespace_id,
            title=old.title,
            document_ref=old.document_ref,
            source_kind=old.source_kind,
            document_kind=old.document_kind,
            file_name=old.file_name,
            mime_type=old.mime_type,
            file_size_bytes=old.file_size_bytes,
            sha256=old.sha256,
            tags=old.tags,
            metadata=old.metadata,
            archived=True,
            created_at=old.created_at,
            updated_at=now,
        )
        return True
    return False


# ===========================================================================
# 2. Polymorphic Entity Link Operations
# ===========================================================================


async def link_document(
    conn: Any,
    namespace_id: UUID,
    document_id: UUID,
    entity_type: str,
    entity_id: str,
    relation: str = "about",
) -> DocumentLinkRecord:
    """Link a document to a business entity (e.g. FL, ASSET, PROJECT, etc.)."""
    link_id = uuid4()
    now = datetime.now(timezone.utc)
    norm_entity_type = entity_type.lower().strip()

    if conn is not None:
        query = """
            INSERT INTO document_links (
                id, namespace_id, document_id, entity_type, entity_id, relation, created_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7
            )
            ON CONFLICT (namespace_id, document_id, entity_type, entity_id)
            DO UPDATE SET relation = EXCLUDED.relation
            RETURNING id, namespace_id, document_id, entity_type, entity_id, relation, created_at
        """
        row = await conn.fetchrow(
            query,
            link_id,
            namespace_id,
            document_id,
            norm_entity_type,
            str(entity_id),
            relation,
            now,
        )
        return DocumentLinkRecord(
            id=row["id"],
            namespace_id=row["namespace_id"],
            document_id=row["document_id"],
            entity_type=row["entity_type"],
            entity_id=row["entity_id"],
            relation=row["relation"],
            created_at=row["created_at"],
        )

    link = DocumentLinkRecord(
        id=link_id,
        namespace_id=namespace_id,
        document_id=document_id,
        entity_type=norm_entity_type,
        entity_id=str(entity_id),
        relation=relation,
        created_at=now,
    )
    ns_key = str(namespace_id)
    links = _MEM_LINKS.setdefault(ns_key, [])
    # Replace if duplicate (namespace_id, document_id, entity_type, entity_id)
    for idx, existing in enumerate(links):
        if (
            existing.document_id == document_id
            and existing.entity_type == norm_entity_type
            and existing.entity_id == str(entity_id)
        ):
            links[idx] = link
            return link
    links.append(link)
    return link


async def unlink_document(
    conn: Any,
    namespace_id: UUID,
    document_id: UUID,
    entity_type: str,
    entity_id: str,
) -> bool:
    """Remove a link between a document and an entity.

    CRITICAL INVARIANT (ADR 0041):
    Unlinking never deletes the document or its external source content.
    """
    norm_entity_type = entity_type.lower().strip()
    if conn is not None:
        query = """
            DELETE FROM document_links
            WHERE namespace_id = $1 AND document_id = $2
              AND entity_type = $3 AND entity_id = $4
            RETURNING id
        """
        row = await conn.fetchrow(
            query, namespace_id, document_id, norm_entity_type, str(entity_id)
        )
        return row is not None

    ns_key = str(namespace_id)
    links = _MEM_LINKS.get(ns_key, [])
    init_len = len(links)
    _MEM_LINKS[ns_key] = [
        it
        for it in links
        if not (
            it.document_id == document_id
            and it.entity_type == norm_entity_type
            and it.entity_id == str(entity_id)
        )
    ]
    return len(_MEM_LINKS[ns_key]) < init_len


async def list_entity_documents(
    conn: Any,
    namespace_id: UUID,
    entity_type: str,
    entity_id: str,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """List all documents linked to a given entity."""
    norm_entity_type = entity_type.lower().strip()
    if conn is not None:
        query = """
            SELECT d.id, d.namespace_id, d.title, d.document_ref, d.source_kind,
                   d.document_kind, d.file_name, d.mime_type, d.file_size_bytes,
                   d.sha256, d.tags, d.metadata, d.archived, d.created_at, d.updated_at,
                   l.relation, l.created_at AS linked_at
            FROM document_links l
            JOIN documents d ON d.id = l.document_id AND d.namespace_id = l.namespace_id
            WHERE l.namespace_id = $1 AND l.entity_type = $2 AND l.entity_id = $3
              AND ($4 OR d.archived = FALSE)
            ORDER BY l.created_at DESC
        """
        rows = await conn.fetch(
            query, namespace_id, norm_entity_type, str(entity_id), include_archived
        )
        return [
            {
                "id": str(r["id"]),
                "namespace_id": str(r["namespace_id"]),
                "title": r["title"],
                "document_ref": r["document_ref"],
                "source_kind": r["source_kind"],
                "document_kind": r["document_kind"],
                "file_name": r["file_name"],
                "mime_type": r["mime_type"],
                "file_size_bytes": r["file_size_bytes"],
                "sha256": r["sha256"],
                "tags": r["tags"] or [],
                "metadata": _parse_metadata(r["metadata"]),
                "archived": r["archived"],
                "relation": r["relation"],
                "linked_at": r["linked_at"].isoformat() if r["linked_at"] else None,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
            for r in rows
        ]

    ns_key = str(namespace_id)
    links = _MEM_LINKS.get(ns_key, [])
    docs_map = _MEM_DOCUMENTS.get(ns_key, {})
    result = []
    for lk in links:
        if lk.entity_type == norm_entity_type and lk.entity_id == str(entity_id):
            doc = docs_map.get(str(lk.document_id))
            if doc is None:
                try:
                    from nce.resource_surface.rest import _MEM_STORE

                    raw = _MEM_STORE.get(f"documents:documents:{ns_key}", {}).get(
                        str(lk.document_id)
                    )
                    if raw:
                        doc = DocumentRecord(
                            id=UUID(str(raw["id"])),
                            namespace_id=UUID(str(raw.get("namespace_id") or namespace_id)),
                            title=str(raw["title"]),
                            document_ref=str(raw["document_ref"]),
                            source_kind=str(raw.get("source_kind", "sharepoint")),
                            document_kind=str(raw.get("document_kind", "other")),
                            file_name=raw.get("file_name"),
                            mime_type=raw.get("mime_type"),
                            file_size_bytes=raw.get("file_size_bytes"),
                            sha256=raw.get("sha256"),
                            tags=tuple(raw.get("tags") or ()),
                            metadata=dict(raw.get("metadata") or {}),
                            archived=bool(raw.get("archived", False)),
                            created_at=datetime.fromisoformat(raw["created_at"])
                            if "created_at" in raw and isinstance(raw["created_at"], str)
                            else datetime.now(timezone.utc),
                            updated_at=datetime.fromisoformat(raw["updated_at"])
                            if "updated_at" in raw and isinstance(raw["updated_at"], str)
                            else datetime.now(timezone.utc),
                        )
                except Exception:
                    pass
            if doc and (include_archived or not doc.archived):
                result.append(
                    {
                        "id": str(doc.id),
                        "namespace_id": str(doc.namespace_id),
                        "title": doc.title,
                        "document_ref": doc.document_ref,
                        "source_kind": doc.source_kind,
                        "document_kind": doc.document_kind,
                        "file_name": doc.file_name,
                        "mime_type": doc.mime_type,
                        "file_size_bytes": doc.file_size_bytes,
                        "sha256": doc.sha256,
                        "tags": list(doc.tags),
                        "metadata": doc.metadata,
                        "archived": doc.archived,
                        "relation": lk.relation,
                        "linked_at": lk.created_at.isoformat(),
                        "created_at": doc.created_at.isoformat(),
                        "updated_at": doc.updated_at.isoformat(),
                    }
                )
    return result


# ===========================================================================
# 3. Share Token Operations
# ===========================================================================


def is_share_active(share: DocumentShareRecord, now: datetime | None = None) -> bool:
    """Verify that a share token is neither revoked nor expired."""
    if now is None:
        now = datetime.now(timezone.utc)
    if share.revoked_at is not None:
        return False
    if share.expires_at is not None:
        exp = share.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= now:
            return False
    return True


async def create_document_share(
    conn: Any,
    namespace_id: UUID,
    document_id: UUID,
    *,
    customer_scope_id: UUID | None = None,
    granted_by: str = "system",
    expires_at: datetime | None = None,
) -> DocumentShareRecord:
    """Generate a secure expiring share token for a document."""
    share_uuid = uuid4()
    share_token = f"dsh_{secrets.token_urlsafe(32)}"
    now = datetime.now(timezone.utc)

    if conn is not None:
        query = """
            INSERT INTO document_shares (
                id, share_id, namespace_id, document_id, customer_scope_id,
                granted_by, expires_at, revoked_at, created_at, updated_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, NULL, $8, $8
            )
            RETURNING id, share_id, namespace_id, document_id, customer_scope_id,
                      granted_by, expires_at, revoked_at, created_at, updated_at
        """
        row = await conn.fetchrow(
            query,
            share_uuid,
            share_token,
            namespace_id,
            document_id,
            customer_scope_id,
            granted_by,
            expires_at,
            now,
        )
        return DocumentShareRecord(
            id=row["id"],
            share_id=row["share_id"],
            namespace_id=row["namespace_id"],
            document_id=row["document_id"],
            customer_scope_id=row["customer_scope_id"],
            granted_by=row["granted_by"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    record = DocumentShareRecord(
        id=share_uuid,
        share_id=share_token,
        namespace_id=namespace_id,
        document_id=document_id,
        customer_scope_id=customer_scope_id,
        granted_by=granted_by,
        expires_at=expires_at,
        revoked_at=None,
        created_at=now,
        updated_at=now,
    )
    _MEM_SHARES.setdefault(str(namespace_id), {})[share_token] = record
    return record


async def get_document_share(
    conn: Any,
    namespace_id: UUID,
    share_id: str,
) -> DocumentShareRecord | None:
    """Retrieve document share record by token."""
    if conn is not None:
        query = """
            SELECT id, share_id, namespace_id, document_id, customer_scope_id,
                   granted_by, expires_at, revoked_at, created_at, updated_at
            FROM document_shares
            WHERE namespace_id = $1 AND share_id = $2
        """
        row = await conn.fetchrow(query, namespace_id, share_id)
        if not row:
            return None
        return DocumentShareRecord(
            id=row["id"],
            share_id=row["share_id"],
            namespace_id=row["namespace_id"],
            document_id=row["document_id"],
            customer_scope_id=row["customer_scope_id"],
            granted_by=row["granted_by"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    return _MEM_SHARES.get(str(namespace_id), {}).get(share_id)


async def revoke_document_share(
    conn: Any,
    namespace_id: UUID,
    share_id: str,
) -> bool:
    """Revoke an active document share token."""
    now = datetime.now(timezone.utc)
    if conn is not None:
        query = """
            UPDATE document_shares
            SET revoked_at = $3, updated_at = $3
            WHERE namespace_id = $1 AND share_id = $2 AND revoked_at IS NULL
            RETURNING id
        """
        row = await conn.fetchrow(query, namespace_id, share_id, now)
        return row is not None

    ns_key = str(namespace_id)
    if ns_key in _MEM_SHARES and share_id in _MEM_SHARES[ns_key]:
        old = _MEM_SHARES[ns_key][share_id]
        if old.revoked_at is not None:
            return False
        _MEM_SHARES[ns_key][share_id] = DocumentShareRecord(
            id=old.id,
            share_id=old.share_id,
            namespace_id=old.namespace_id,
            document_id=old.document_id,
            customer_scope_id=old.customer_scope_id,
            granted_by=old.granted_by,
            expires_at=old.expires_at,
            revoked_at=now,
            created_at=old.created_at,
            updated_at=now,
        )
        return True
    return False
