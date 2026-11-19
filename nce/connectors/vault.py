"""Encrypted multi-tenant app connection vault backed by app_connections + envelope crypto."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import BaseModel, Field

from nce.config import cfg
from nce.connectors.base import ConnectorAuthKind
from nce.connectors.guards import (
    AllowListRedactor,
    read_nce_master_key_str,
    validate_outbound_url,
)
from nce.crypto.envelope import (
    EnvelopeCryptoManager,
    InMemoryDEKStore,
    KEKRing,
    canonicalize_jcs,
    get_default_crypto_manager,
)
from nce.db_utils import scoped_pg_session

ConnectionStatus = Literal["connected", "disconnected", "error", "pending"]
_MEM_FALLBACK_KEY = b"NCE-TEST-VAULT-KEK-FALLBACK-32B!"


class AppConnectionMetadata(BaseModel):
    """Public metadata view of an app_connections row — never carries decrypted secrets."""

    connection_id: uuid.UUID
    namespace_id: uuid.UUID
    app_slug: str
    connection_name: str = "default"
    auth_kind: ConnectorAuthKind = "api_key"
    endpoint_url: str | None = None
    status: ConnectionStatus = "connected"
    scopes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    dek_id: uuid.UUID | None = None
    kek_version: str | None = None
    last_verified_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


def _build_inmemory_crypto_manager() -> EnvelopeCryptoManager:
    raw_master = read_nce_master_key_str()
    if raw_master:
        key_bytes = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"nce-connector-vault-kek-v1",
            info=b"kek-v1",
        ).derive(raw_master.encode("utf-8"))
    elif bool(getattr(cfg, "IS_PROD", False)) or (
        os.getenv("NCE_ENV", "").strip().lower() in ("prod", "production")
    ):
        raise RuntimeError(
            "CRITICAL SECURITY FAILURE: NCE_MASTER_KEY is required for TenantConnectionVault"
        )
    else:
        key_bytes = _MEM_FALLBACK_KEY
    return EnvelopeCryptoManager(kek_ring=KEKRing(master_key=key_bytes, active_version="kek-v1"))


def _aad_for(namespace_id: uuid.UUID, app_slug: str, connection_name: str) -> bytes:
    return f"app_connections:{namespace_id}:{app_slug}:{connection_name}".encode()


def _parse_json_obj(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return default
    return raw


def _row_to_metadata(row: Any) -> AppConnectionMetadata:
    scopes_raw = _parse_json_obj(row["scopes"], [])
    meta_raw = _parse_json_obj(row["metadata"], {})
    safe_meta = AllowListRedactor(mode="strip").redact(
        meta_raw if isinstance(meta_raw, dict) else {}
    )
    return AppConnectionMetadata(
        connection_id=uuid.UUID(str(row["id"])),
        namespace_id=uuid.UUID(str(row["namespace_id"])),
        app_slug=str(row["app_slug"]),
        connection_name=str(row["connection_name"]),
        auth_kind=row["auth_kind"],
        endpoint_url=row["endpoint_url"],
        status=row["status"],
        scopes=[str(s) for s in scopes_raw] if isinstance(scopes_raw, list) else [],
        metadata=safe_meta,
        dek_id=uuid.UUID(str(row["dek_id"])) if row["dek_id"] is not None else None,
        kek_version=str(row["kek_version"]) if row["kek_version"] is not None else None,
        last_verified_at=row["last_verified_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class TenantConnectionVault:
    """Tenant-scoped CRUD & envelope crypto vault over PostgreSQL ``app_connections``."""

    def __init__(
        self,
        pool: Any = None,
        *,
        crypto_manager: EnvelopeCryptoManager | None = None,
    ) -> None:
        self.pool = pool
        self._crypto_manager = crypto_manager
        self._mem_dek_store = InMemoryDEKStore()
        self._mem_rows: dict[tuple[uuid.UUID, str, str], dict[str, Any]] = {}
        self._redactor = AllowListRedactor(mode="strip")

    def _get_crypto_manager(self) -> EnvelopeCryptoManager:
        if self._crypto_manager is not None:
            return self._crypto_manager
        if self.pool is not None:
            return get_default_crypto_manager()
        self._crypto_manager = _build_inmemory_crypto_manager()
        return self._crypto_manager

    async def upsert_connection(
        self,
        namespace_id: uuid.UUID | str,
        app_slug: str,
        secret_config: dict[str, Any],
        *,
        connection_name: str = "default",
        auth_kind: ConnectorAuthKind = "api_key",
        endpoint_url: str | None = None,
        scopes: list[str] | tuple[str, ...] | None = None,
        metadata: dict[str, Any] | None = None,
        status: ConnectionStatus = "connected",
        resolve_dns: bool = True,
    ) -> AppConnectionMetadata:
        """Encrypt ``secret_config`` with tenant DEK and persist the connection metadata."""
        ns_uuid = uuid.UUID(str(namespace_id))
        clean_slug = app_slug.strip()
        clean_name = connection_name.strip() or "default"
        validated_url: str | None = None
        if endpoint_url:
            validated_url = validate_outbound_url(endpoint_url, resolve_dns=resolve_dns)

        clean_scopes = [str(s).strip() for s in (scopes or ()) if str(s).strip()]
        clean_meta = self._redactor.redact(dict(metadata or {}))
        plaintext = canonicalize_jcs(dict(secret_config or {}))
        aad = _aad_for(ns_uuid, clean_slug, clean_name)
        mgr = self._get_crypto_manager()

        if self.pool is None:
            now = datetime.now(timezone.utc)
            env = await mgr.encrypt_payload(
                self._mem_dek_store,
                str(ns_uuid),
                plaintext,
                subject_ref=f"connector:{clean_slug}:{clean_name}",
                aad=aad,
            )
            key = (ns_uuid, clean_slug, clean_name)
            existing = self._mem_rows.get(key)
            conn_id = existing["id"] if existing else uuid.uuid4()
            created_at = existing["created_at"] if existing else now
            rec = {
                "id": conn_id,
                "namespace_id": ns_uuid,
                "app_slug": clean_slug,
                "connection_name": clean_name,
                "auth_kind": auth_kind,
                "endpoint_url": validated_url,
                "status": status,
                "scopes": clean_scopes,
                "metadata": clean_meta,
                "encrypted_config": env,
                "dek_id": uuid.UUID(str(env["dek_id"])) if env.get("dek_id") else None,
                "kek_version": env.get("kek_version"),
                "last_verified_at": now if status == "connected" else None,
                "created_at": created_at,
                "updated_at": now,
            }
            self._mem_rows[key] = rec
            return _row_to_metadata(rec)

        async with scoped_pg_session(self.pool, ns_uuid) as conn:
            env = await mgr.encrypt_payload(
                conn,
                ns_uuid,
                plaintext,
                subject_ref=f"connector:{clean_slug}:{clean_name}",
                aad=aad,
            )
            dek_uuid = uuid.UUID(str(env["dek_id"])) if env.get("dek_id") else None
            kek_version = str(env["kek_version"]) if env.get("kek_version") else None
            row = await conn.fetchrow(
                """
                INSERT INTO app_connections (
                    namespace_id,
                    app_slug,
                    connection_name,
                    auth_kind,
                    endpoint_url,
                    status,
                    scopes,
                    metadata,
                    encrypted_config,
                    dek_id,
                    kek_version,
                    last_verified_at,
                    updated_at
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6,
                    $7::jsonb, $8::jsonb, $9::jsonb,
                    $10, $11,
                    CASE WHEN $6 = 'connected' THEN NOW() ELSE NULL END,
                    NOW()
                )
                ON CONFLICT (namespace_id, app_slug, connection_name)
                DO UPDATE SET
                    auth_kind = EXCLUDED.auth_kind,
                    endpoint_url = EXCLUDED.endpoint_url,
                    status = EXCLUDED.status,
                    scopes = EXCLUDED.scopes,
                    metadata = EXCLUDED.metadata,
                    encrypted_config = EXCLUDED.encrypted_config,
                    dek_id = EXCLUDED.dek_id,
                    kek_version = EXCLUDED.kek_version,
                    last_verified_at = CASE
                        WHEN EXCLUDED.status = 'connected' THEN NOW()
                        ELSE app_connections.last_verified_at
                    END,
                    updated_at = NOW()
                RETURNING
                    id, namespace_id, app_slug, connection_name, auth_kind,
                    endpoint_url, status, scopes, metadata, dek_id, kek_version,
                    last_verified_at, created_at, updated_at
                """,
                ns_uuid,
                clean_slug,
                clean_name,
                auth_kind,
                validated_url,
                status,
                json.dumps(clean_scopes),
                json.dumps(clean_meta),
                json.dumps(env),
                dek_uuid,
                kek_version,
            )
            return _row_to_metadata(row)

    async def list_connections(
        self,
        namespace_id: uuid.UUID | str,
        *,
        app_slug: str | None = None,
        status: ConnectionStatus | None = None,
    ) -> list[AppConnectionMetadata]:
        """List connection metadata for a tenant — never selects or returns encrypted_config."""
        ns_uuid = uuid.UUID(str(namespace_id))
        if self.pool is None:
            items = [
                _row_to_metadata(rec)
                for (row_ns, row_slug, _), rec in self._mem_rows.items()
                if row_ns == ns_uuid
                and (app_slug is None or row_slug == app_slug)
                and (status is None or rec["status"] == status)
            ]
            items.sort(key=lambda item: (item.app_slug, item.connection_name))
            return items

        clauses = ["namespace_id = $1"]
        params: list[Any] = [ns_uuid]
        if app_slug is not None:
            params.append(app_slug.strip())
            clauses.append(f"app_slug = ${len(params)}")
        if status is not None:
            params.append(status)
            clauses.append(f"status = ${len(params)}")

        sql = f"""
            SELECT
                id, namespace_id, app_slug, connection_name, auth_kind,
                endpoint_url, status, scopes, metadata, dek_id, kek_version,
                last_verified_at, created_at, updated_at
            FROM app_connections
            WHERE {' AND '.join(clauses)}
            ORDER BY app_slug, connection_name
        """
        async with scoped_pg_session(self.pool, ns_uuid) as conn:
            rows = await conn.fetch(sql, *params)
            return [_row_to_metadata(r) for r in rows]

    async def get_connection_metadata(
        self,
        namespace_id: uuid.UUID | str,
        connection_id: uuid.UUID | str,
    ) -> AppConnectionMetadata | None:
        """Fetch a single connection's metadata without decrypting its secret payload."""
        ns_uuid = uuid.UUID(str(namespace_id))
        cid_uuid = uuid.UUID(str(connection_id))
        if self.pool is None:
            for rec in self._mem_rows.values():
                if rec["namespace_id"] == ns_uuid and rec["id"] == cid_uuid:
                    return _row_to_metadata(rec)
            return None

        async with scoped_pg_session(self.pool, ns_uuid) as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    id, namespace_id, app_slug, connection_name, auth_kind,
                    endpoint_url, status, scopes, metadata, dek_id, kek_version,
                    last_verified_at, created_at, updated_at
                FROM app_connections
                WHERE namespace_id = $1 AND id = $2
                """,
                ns_uuid,
                cid_uuid,
            )
            return _row_to_metadata(row) if row is not None else None

    async def get_decrypted_secret(
        self,
        namespace_id: uuid.UUID | str,
        app_slug: str,
        *,
        connection_name: str = "default",
    ) -> dict[str, Any] | None:
        """Decrypt the stored connector credential dict for runtime connector execution."""
        ns_uuid = uuid.UUID(str(namespace_id))
        clean_slug = app_slug.strip()
        clean_name = connection_name.strip() or "default"
        aad = _aad_for(ns_uuid, clean_slug, clean_name)
        mgr = self._get_crypto_manager()

        if self.pool is None:
            rec = self._mem_rows.get((ns_uuid, clean_slug, clean_name))
            if rec is None:
                return None
            plaintext = await mgr.decrypt_payload(
                self._mem_dek_store, str(ns_uuid), rec["encrypted_config"], aad=aad
            )
            return dict(json.loads(plaintext.decode("utf-8")))

        async with scoped_pg_session(self.pool, ns_uuid) as conn:
            row = await conn.fetchrow(
                """
                SELECT encrypted_config
                FROM app_connections
                WHERE namespace_id = $1 AND app_slug = $2 AND connection_name = $3
                """,
                ns_uuid,
                clean_slug,
                clean_name,
            )
            if row is None:
                return None
            env_obj = _parse_json_obj(row["encrypted_config"], {})
            plaintext = await mgr.decrypt_payload(conn, ns_uuid, env_obj, aad=aad)
            return dict(json.loads(plaintext.decode("utf-8")))

    async def delete_connection(
        self,
        namespace_id: uuid.UUID | str,
        connection_id: uuid.UUID | str,
    ) -> bool:
        """Delete a tenant's connection record by ID."""
        ns_uuid = uuid.UUID(str(namespace_id))
        cid_uuid = uuid.UUID(str(connection_id))
        if self.pool is None:
            for key, rec in list(self._mem_rows.items()):
                if rec["namespace_id"] == ns_uuid and rec["id"] == cid_uuid:
                    del self._mem_rows[key]
                    return True
            return False

        async with scoped_pg_session(self.pool, ns_uuid) as conn:
            tag = await conn.execute(
                "DELETE FROM app_connections WHERE namespace_id = $1 AND id = $2",
                ns_uuid,
                cid_uuid,
            )
            return tag.endswith(" 1")

    async def get_connected_app_slugs(self, namespace_id: uuid.UUID | str) -> set[str]:
        """Return distinct app slugs with status='connected' for this namespace."""
        ns_uuid = uuid.UUID(str(namespace_id))
        if self.pool is None:
            return {
                slug
                for (row_ns, slug, _), rec in self._mem_rows.items()
                if row_ns == ns_uuid and rec["status"] == "connected"
            }

        async with scoped_pg_session(self.pool, ns_uuid) as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT app_slug
                FROM app_connections
                WHERE namespace_id = $1 AND status = 'connected'
                """,
                ns_uuid,
            )
            return {str(r["app_slug"]) for r in rows}


__all__ = [
    "AppConnectionMetadata",
    "ConnectionStatus",
    "TenantConnectionVault",
]
