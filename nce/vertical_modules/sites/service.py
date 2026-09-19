"""nce.vertical_modules.sites.service — Domain operations for C17 Site Master Data.

Phase A Wave A-9:
Implements site registration, lookup by ID or cadastre_id, updates, archiving,
and vessel telemetry stream ingestion.
Multi-tenant isolated and RLS protected.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from nce.vertical_modules.sites.models import SiteCreate, SiteItem, SiteUpdate

# In-memory store for hermetic unit testing and offline execution:
# _MEM_SITES[ns_str][site_id_str] = SiteItem
_MEM_SITES: dict[str, dict[str, SiteItem]] = {}


def _clear_mem_store() -> None:
    """Clear in-memory storage for hermetic test execution."""
    _MEM_SITES.clear()


def normalize_cadastre_id(val: str | None) -> str | None:
    """Normalize cadastre_id (strip whitespace, clean string)."""
    if not val:
        return None
    cleaned = str(val).strip()
    return cleaned if cleaned else None


def _row_to_site_item(row: Any) -> SiteItem:
    def _parse_json(v: Any) -> Any:
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return v
        return v or {}

    return SiteItem(
        id=row["id"] if isinstance(row["id"], UUID) else UUID(str(row["id"])),
        namespace_id=(
            row["namespace_id"]
            if isinstance(row["namespace_id"], UUID)
            else UUID(str(row["namespace_id"]))
        ),
        name=row["name"],
        cadastre_id=row["cadastre_id"],
        site_type=row["site_type"],
        address=_parse_json(row["address"]),
        latitude=row["latitude"],
        longitude=row["longitude"],
        altitude=row["altitude"],
        height=row["height"],
        footprint_geometry=_parse_json(row["footprint_geometry"]),
        telemetry_stream=(
            _parse_json(row["telemetry_stream"]) if row["telemetry_stream"] is not None else None
        ),
        metadata=_parse_json(row["metadata"]),
        archived=row["archived"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def register_site(
    conn: Any,
    namespace_id: UUID | str,
    payload: SiteCreate | dict[str, Any],
) -> SiteItem:
    """Register a new site or building in the master site register."""
    ns_uuid = UUID(str(namespace_id))
    if isinstance(payload, dict):
        data = SiteCreate(**payload)
    else:
        data = payload

    site_id = uuid4()
    now = datetime.now(timezone.utc)
    cadastre = normalize_cadastre_id(data.cadastre_id)
    address_dict = data.address or {}
    footprint_dict = data.footprint_geometry or {}
    telemetry_dict = data.telemetry_stream
    meta_dict = data.metadata or {}

    item = SiteItem(
        id=site_id,
        namespace_id=ns_uuid,
        name=data.name,
        cadastre_id=cadastre,
        site_type=data.site_type,
        address=address_dict,
        latitude=data.latitude,
        longitude=data.longitude,
        altitude=data.altitude,
        height=data.height,
        footprint_geometry=footprint_dict,
        telemetry_stream=telemetry_dict,
        metadata=meta_dict,
        archived=False,
        created_at=now,
        updated_at=now,
    )

    if conn is not None:
        query = """
            INSERT INTO sites (
                id, namespace_id, name, cadastre_id, site_type,
                address, latitude, longitude, altitude, height,
                footprint_geometry, telemetry_stream, metadata,
                archived, created_at, updated_at
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6::jsonb, $7, $8, $9, $10,
                $11::jsonb, $12::jsonb, $13::jsonb,
                FALSE, $14, $14
            )
            RETURNING id, namespace_id, name, cadastre_id, site_type,
                      address, latitude, longitude, altitude, height,
                      footprint_geometry, telemetry_stream, metadata,
                      archived, created_at, updated_at
        """
        row = await conn.fetchrow(
            query,
            site_id,
            ns_uuid,
            data.name,
            cadastre,
            data.site_type,
            json.dumps(address_dict),
            data.latitude,
            data.longitude,
            data.altitude,
            data.height,
            json.dumps(footprint_dict),
            json.dumps(telemetry_dict) if telemetry_dict is not None else None,
            json.dumps(meta_dict),
            now,
        )
        return _row_to_site_item(row)

    # In-memory storage
    ns_key = str(ns_uuid)
    _MEM_SITES.setdefault(ns_key, {})[str(site_id)] = item
    return item


async def get_site(
    conn: Any,
    namespace_id: UUID | str,
    site_id: UUID | str,
) -> SiteItem | None:
    """Retrieve a site by primary key with tenant isolation."""
    ns_uuid = UUID(str(namespace_id))
    s_uuid = UUID(str(site_id))

    if conn is not None:
        query = """
            SELECT id, namespace_id, name, cadastre_id, site_type,
                   address, latitude, longitude, altitude, height,
                   footprint_geometry, telemetry_stream, metadata,
                   archived, created_at, updated_at
            FROM sites
            WHERE namespace_id = $1 AND id = $2 AND NOT archived
        """
        row = await conn.fetchrow(query, ns_uuid, s_uuid)
        if not row:
            return None
        return _row_to_site_item(row)

    ns_key = str(ns_uuid)
    item = _MEM_SITES.get(ns_key, {}).get(str(s_uuid))
    if item and not item.archived:
        return item
    return None


async def get_site_by_cadastre_id(
    conn: Any,
    namespace_id: UUID | str,
    cadastre_id: str,
) -> SiteItem | None:
    """Look up a site by cadastre_id within tenant namespace."""
    ns_uuid = UUID(str(namespace_id))
    cleaned = normalize_cadastre_id(cadastre_id)
    if not cleaned:
        return None

    if conn is not None:
        query = """
            SELECT id, namespace_id, name, cadastre_id, site_type,
                   address, latitude, longitude, altitude, height,
                   footprint_geometry, telemetry_stream, metadata,
                   archived, created_at, updated_at
            FROM sites
            WHERE namespace_id = $1 AND cadastre_id = $2 AND NOT archived
        """
        row = await conn.fetchrow(query, ns_uuid, cleaned)
        if not row:
            return None
        return _row_to_site_item(row)

    ns_key = str(ns_uuid)
    for s in _MEM_SITES.get(ns_key, {}).values():
        if s.cadastre_id == cleaned and not s.archived:
            return s
    return None


async def list_sites(
    conn: Any,
    namespace_id: UUID | str,
    *,
    site_type: str | None = None,
    cadastre_id: str | None = None,
    archived: bool = False,
    limit: int = 50,
) -> list[SiteItem]:
    """List sites in a namespace with optional filtering."""
    ns_uuid = UUID(str(namespace_id))

    if conn is not None:
        clauses = ["namespace_id = $1", "archived = $2"]
        params: list[Any] = [ns_uuid, archived]
        if site_type:
            params.append(site_type)
            clauses.append(f"site_type = ${len(params)}")
        if cadastre_id:
            params.append(normalize_cadastre_id(cadastre_id))
            clauses.append(f"cadastre_id = ${len(params)}")
        params.append(limit)
        limit_idx = len(params)

        query = f"""
            SELECT id, namespace_id, name, cadastre_id, site_type,
                   address, latitude, longitude, altitude, height,
                   footprint_geometry, telemetry_stream, metadata,
                   archived, created_at, updated_at
            FROM sites
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC
            LIMIT ${limit_idx}
        """
        rows = await conn.fetch(query, *params)
        return [_row_to_site_item(r) for r in rows]

    ns_key = str(ns_uuid)
    items: list[SiteItem] = []
    for s in _MEM_SITES.get(ns_key, {}).values():
        if s.archived != archived:
            continue
        if site_type and s.site_type != site_type:
            continue
        if cadastre_id and s.cadastre_id != normalize_cadastre_id(cadastre_id):
            continue
        items.append(s)
    items.sort(key=lambda x: x.created_at, reverse=True)
    return items[:limit]


async def update_site(
    conn: Any,
    namespace_id: UUID | str,
    site_id: UUID | str,
    updates: SiteUpdate | dict[str, Any],
) -> SiteItem | None:
    """Update site attributes."""
    ns_uuid = UUID(str(namespace_id))
    s_uuid = UUID(str(site_id))
    data = updates if isinstance(updates, SiteUpdate) else SiteUpdate(**updates)
    now = datetime.now(timezone.utc)

    existing = await get_site(conn, ns_uuid, s_uuid)
    if not existing:
        return None

    new_cadastre = (
        normalize_cadastre_id(data.cadastre_id)
        if data.cadastre_id is not None
        else existing.cadastre_id
    )

    if conn is not None:
        query = """
            UPDATE sites
            SET name = COALESCE($3, name),
                cadastre_id = COALESCE($4, cadastre_id),
                site_type = COALESCE($5, site_type),
                address = CASE WHEN $6::jsonb IS NOT NULL THEN $6::jsonb ELSE address END,
                latitude = COALESCE($7, latitude),
                longitude = COALESCE($8, longitude),
                altitude = COALESCE($9, altitude),
                height = COALESCE($10, height),
                footprint_geometry = CASE WHEN $11::jsonb IS NOT NULL THEN $11::jsonb ELSE footprint_geometry END,
                telemetry_stream = CASE WHEN $12::jsonb IS NOT NULL THEN $12::jsonb ELSE telemetry_stream END,
                metadata = CASE WHEN $13::jsonb IS NOT NULL THEN $13::jsonb ELSE metadata END,
                archived = COALESCE($14, archived),
                updated_at = $15
            WHERE namespace_id = $1 AND id = $2
            RETURNING id, namespace_id, name, cadastre_id, site_type,
                      address, latitude, longitude, altitude, height,
                      footprint_geometry, telemetry_stream, metadata,
                      archived, created_at, updated_at
        """
        row = await conn.fetchrow(
            query,
            ns_uuid,
            s_uuid,
            data.name,
            new_cadastre,
            data.site_type,
            json.dumps(data.address) if data.address is not None else None,
            data.latitude,
            data.longitude,
            data.altitude,
            data.height,
            json.dumps(data.footprint_geometry) if data.footprint_geometry is not None else None,
            json.dumps(data.telemetry_stream) if data.telemetry_stream is not None else None,
            json.dumps(data.metadata) if data.metadata is not None else None,
            data.archived,
            now,
        )
        if not row:
            return None
        return _row_to_site_item(row)

    # In-memory update
    ns_key = str(ns_uuid)
    item = _MEM_SITES[ns_key][str(s_uuid)]
    updated = SiteItem(
        id=item.id,
        namespace_id=item.namespace_id,
        name=data.name if data.name is not None else item.name,
        cadastre_id=new_cadastre,
        site_type=data.site_type if data.site_type is not None else item.site_type,
        address=data.address if data.address is not None else item.address,
        latitude=data.latitude if data.latitude is not None else item.latitude,
        longitude=data.longitude if data.longitude is not None else item.longitude,
        altitude=data.altitude if data.altitude is not None else item.altitude,
        height=data.height if data.height is not None else item.height,
        footprint_geometry=(
            data.footprint_geometry
            if data.footprint_geometry is not None
            else item.footprint_geometry
        ),
        telemetry_stream=(
            data.telemetry_stream if data.telemetry_stream is not None else item.telemetry_stream
        ),
        metadata=data.metadata if data.metadata is not None else item.metadata,
        archived=data.archived if data.archived is not None else item.archived,
        created_at=item.created_at,
        updated_at=now,
    )
    _MEM_SITES[ns_key][str(s_uuid)] = updated
    return updated


async def archive_site(
    conn: Any,
    namespace_id: UUID | str,
    site_id: UUID | str,
) -> bool:
    """Soft delete / archive a site."""
    ns_uuid = UUID(str(namespace_id))
    s_uuid = UUID(str(site_id))

    if conn is not None:
        query = """
            UPDATE sites
            SET archived = TRUE, updated_at = NOW()
            WHERE namespace_id = $1 AND id = $2 AND NOT archived
            RETURNING id
        """
        row = await conn.fetchrow(query, ns_uuid, s_uuid)
        return row is not None

    ns_key = str(ns_uuid)
    item = _MEM_SITES.get(ns_key, {}).get(str(s_uuid))
    if item and not item.archived:
        item.archived = True
        item.updated_at = datetime.now(timezone.utc)
        return True
    return False


async def update_vessel_telemetry(
    conn: Any,
    namespace_id: UUID | str,
    site_id: UUID | str,
    *,
    latitude: float,
    longitude: float,
    telemetry: dict[str, Any] | None = None,
) -> SiteItem | None:
    """Update position and telemetry stream payload for a vessel site."""
    updates = {
        "latitude": latitude,
        "longitude": longitude,
        "telemetry_stream": telemetry or {"timestamp": datetime.now(timezone.utc).isoformat()},
    }
    return await update_site(conn, namespace_id, site_id, updates)
