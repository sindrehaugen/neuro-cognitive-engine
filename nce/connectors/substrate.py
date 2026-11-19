"""Vendor-neutral Core substrate helpers for NCE application and vertical connectors."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import inspect
import json
import logging
import os
import re
import uuid
from collections import defaultdict
from collections.abc import Callable, Sequence
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from nce.config import cfg
from nce.connectors.guards import read_nce_master_key_str, redact_c8_allowlist
from nce.db_utils import set_namespace_context
from nce.event_log import append_event

log = logging.getLogger("nce.connectors.substrate")

_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_EPHEMERAL_DEV_KEY: bytes = os.urandom(32)

# In-memory test substrates (exercised when no PG/MinIO pool is attached).
_MEM_TABLES: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
_MEM_BLOBS: dict[str, bytes] = {}
_MEM_LINKS: list[dict[str, Any]] = []
_MEM_WORKER_LANES: dict[tuple[str, str], Callable[..., Any]] = {}
_MEM_DLQ: list[dict[str, Any]] = []


class SubstrateError(ValueError):
    """Raised when connector substrate validation or persistence contracts fail."""


def _is_prod_mode() -> bool:
    return bool(getattr(cfg, "IS_PROD", False)) or (
        os.getenv("NCE_ENV", "").strip().lower() in ("prod", "production")
    )


def _get_signing_key() -> bytes:
    raw = read_nce_master_key_str()
    if raw:
        return hashlib.sha256(raw.encode("utf-8")).digest()
    if _is_prod_mode():
        raise SubstrateError("CRITICAL SECURITY FAILURE: NCE_MASTER_KEY missing in production")
    return _EPHEMERAL_DEV_KEY


def _is_missing_table_or_fk_error(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    return any(
        tok in name or tok in msg
        for tok in (
            "undefinedtable",
            "does not exist",
            "foreignkeyviolation",
            "foreign key",
            "undefinedcolumn",
        )
    )


def _validate_ident(name: str, kind: str = "identifier") -> str:
    clean = str(name or "").strip()
    if not _IDENT_RE.fullmatch(clean):
        raise SubstrateError(f"Unsafe SQL {kind}: {name!r}")
    return clean


def normalize_tenant_namespace(raw: Any) -> str:
    """Normalize any tenant identifier into a deterministic UUID string."""
    text = str(raw or "").strip()
    if not text:
        raise SubstrateError("namespace_id is required")
    try:
        return str(uuid.UUID(text))
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"nce-ns:{text}"))


def normalize_record_uuid(
    raw_id: Any,
    table: str = "record",
    namespace_id: Any = None,
) -> str:
    """Normalize an external record ID into a deterministic tenant-scoped UUID string."""
    text = str(raw_id or "").strip()
    if not text:
        raise SubstrateError(f"{table} record id is required")
    try:
        return str(uuid.UUID(text))
    except ValueError:
        ns_part = str(namespace_id or "global")
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"nce:{table}:{ns_part}:{text}"))


def _resolve_pg_pool(engine: Any) -> Any | None:
    if engine is None:
        return None
    for attr in ("pg_pool", "pool", "_pg_pool"):
        candidate = getattr(engine, attr, None)
        if candidate is not None and callable(getattr(candidate, "acquire", None)):
            return candidate
    if callable(getattr(engine, "acquire", None)):
        return engine
    return None


def _encode_pg_value(val: Any) -> Any:
    if isinstance(val, (dict, list)):
        return json.dumps(val, default=str)
    return val


def _row_to_clean_dict(row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in dict(row).items():
        if isinstance(v, uuid.UUID):
            out[k] = str(v)
        else:
            out[k] = v
    return out


async def pg_tenant_upsert(
    engine: Any,
    table: str,
    namespace_id: Any,
    record: dict[str, Any],
    *,
    conflict_cols: Sequence[str] = ("id",),
) -> dict[str, Any]:
    """Upsert a tenant-isolated row setting Core ``nce.namespace_id`` GUC."""
    tbl = _validate_ident(table, "table")
    ns_uuid_str = normalize_tenant_namespace(namespace_id)
    ns_uuid = uuid.UUID(ns_uuid_str)
    cols_clean = [_validate_ident(k, "column") for k in record.keys() if k != "namespace_id"]
    conflict_clean = [_validate_ident(c, "conflict column") for c in conflict_cols]
    row_copy: dict[str, Any] = {
        "namespace_id": ns_uuid_str,
        **{k: record[k] for k in cols_clean},
    }
    row_id = str(row_copy.get(conflict_clean[0] if conflict_clean else "id") or uuid.uuid4())
    row_copy.setdefault("id", row_id)

    pool = _resolve_pg_pool(engine)
    if pool is None:
        mem_key = "|".join(str(row_copy.get(c, "")) for c in conflict_clean) or row_id
        _MEM_TABLES[(ns_uuid_str, tbl)][mem_key] = dict(row_copy)
        return dict(row_copy)

    insert_cols = ["namespace_id", *cols_clean]
    values = [ns_uuid, *(_encode_pg_value(record[k]) for k in cols_clean)]
    placeholders = ", ".join(f"${i}" for i in range(1, len(insert_cols) + 1))
    col_list = ", ".join(insert_cols)
    conflict_target = ", ".join(conflict_clean)
    update_cols = [c for c in cols_clean if c not in conflict_clean]
    if update_cols:
        assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
        conflict_clause = f"ON CONFLICT ({conflict_target}) DO UPDATE SET {assignments}"
    else:
        conflict_clause = f"ON CONFLICT ({conflict_target}) DO NOTHING"

    sql = f"INSERT INTO {tbl} ({col_list}) VALUES ({placeholders}) {conflict_clause} RETURNING *"

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns_uuid)
                res = await conn.fetchrow(sql, *values)
                if res is not None:
                    return _row_to_clean_dict(res)
                return dict(row_copy)
    except Exception as exc:
        if _is_missing_table_or_fk_error(exc):
            log.debug("Fallback to memory substrate for %s (%s)", tbl, exc)
            mem_key = "|".join(str(row_copy.get(c, "")) for c in conflict_clean) or row_id
            _MEM_TABLES[(ns_uuid_str, tbl)][mem_key] = dict(row_copy)
            return dict(row_copy)
        raise


async def pg_tenant_list(
    engine: Any,
    table: str,
    namespace_id: Any,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List tenant-isolated rows inside an ``nce.namespace_id`` transaction."""
    tbl = _validate_ident(table, "table")
    ns_uuid_str = normalize_tenant_namespace(namespace_id)
    ns_uuid = uuid.UUID(ns_uuid_str)
    bounded = max(1, min(int(limit), 1000))

    pool = _resolve_pg_pool(engine)
    if pool is None:
        return [dict(v) for v in list(_MEM_TABLES.get((ns_uuid_str, tbl), {}).values())[:bounded]]

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns_uuid)
                rows = await conn.fetch(
                    f"SELECT * FROM {tbl} WHERE namespace_id = $1 LIMIT $2",
                    ns_uuid,
                    bounded,
                )
                return [_row_to_clean_dict(r) for r in rows]
    except Exception as exc:
        if _is_missing_table_or_fk_error(exc):
            return [
                dict(v) for v in list(_MEM_TABLES.get((ns_uuid_str, tbl), {}).values())[:bounded]
            ]
        raise


class ArtifactStoreAdapter:
    """Adapter persisting binary CAD/document blobs to MinIO/S3 or in-memory fallback."""

    @staticmethod
    async def store_blob(
        engine: Any,
        namespace_id: Any,
        key_prefix: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        """Store blob bytes and return deterministic key + SHA-256 metadata."""
        ns_uuid = normalize_tenant_namespace(namespace_id)
        digest = hashlib.sha256(data).hexdigest()
        clean_prefix = key_prefix.strip("/")
        object_key = f"{ns_uuid}/{clean_prefix}/{digest[:16]}"
        bucket = getattr(cfg, "MINIO_BUCKET", "nce-artifacts")

        minio_client = getattr(engine, "minio_client", None) or getattr(engine, "_minio", None)
        if minio_client is not None:
            import io

            put_obj = getattr(minio_client, "put_object", None)
            if callable(put_obj):
                put_obj(
                    bucket,
                    object_key,
                    io.BytesIO(data),
                    len(data),
                    content_type=content_type,
                )
                return {
                    "minio_key": object_key,
                    "bucket": bucket,
                    "sha256": digest,
                    "size_bytes": len(data),
                    "content_type": content_type,
                }

        _MEM_BLOBS[object_key] = bytes(data)
        return {
            "minio_key": object_key,
            "bucket": bucket,
            "sha256": digest,
            "size_bytes": len(data),
            "content_type": content_type,
        }

    @staticmethod
    async def fetch_blob(engine: Any, minio_key: str) -> bytes | None:
        """Fetch stored blob bytes by key from MinIO or in-memory fallback."""
        minio_client = getattr(engine, "minio_client", None) or getattr(engine, "_minio", None)
        bucket = getattr(cfg, "MINIO_BUCKET", "nce-artifacts")
        if minio_client is not None:
            get_obj = getattr(minio_client, "get_object", None)
            if callable(get_obj):
                resp = None
                try:
                    resp = get_obj(bucket, minio_key)
                    return resp.read()
                finally:
                    if resp is not None:
                        resp.close()
                        resp.release_conn()
        return _MEM_BLOBS.get(minio_key)


class TenantSecretBox:
    """Tenant-bound AES-256-GCM symmetric seal/open helper."""

    @staticmethod
    def _derive_key(ns_uuid: str) -> bytes:
        master = _get_signing_key()
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"nce-connector-secretbox-v1",
            info=f"tenant:{ns_uuid}".encode(),
        ).derive(master)

    @classmethod
    def seal_secret(cls, namespace_id: Any, plaintext: str) -> str:
        """Encrypt a string secret bound to ``namespace_id`` with AAD authentication."""
        ns_uuid = normalize_tenant_namespace(namespace_id)
        key = cls._derive_key(ns_uuid)
        nonce = os.urandom(12)
        aad = f"tenant-secret:{ns_uuid}".encode()
        ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), aad)
        return "v1:" + base64.urlsafe_b64encode(nonce + ct).decode("ascii")

    @classmethod
    def open_secret(cls, namespace_id: Any, token: str) -> str:
        """Decrypt a token created by ``seal_secret`` under matching ``namespace_id``."""
        ns_uuid = normalize_tenant_namespace(namespace_id)
        raw = str(token or "")
        if not raw.startswith("v1:"):
            raise SubstrateError("Unsupported sealed secret format")
        blob = base64.urlsafe_b64decode(raw[3:].encode("ascii"))
        if len(blob) < 13:
            raise SubstrateError("Malformed sealed secret payload")
        nonce, ct = blob[:12], blob[12:]
        key = cls._derive_key(ns_uuid)
        aad = f"tenant-secret:{ns_uuid}".encode()
        pt = AESGCM(key).decrypt(nonce, ct, aad)
        return pt.decode("utf-8")


class BackgroundJobRunner:
    """Async background worker lane registry with retry + DLQ tracking."""

    @staticmethod
    def register_worker_lane(
        engine_slug: str,
        lane_name: str,
        handler: Callable[..., Any],
    ) -> None:
        """Register an async/sync job handler under ``(engine_slug, lane_name)``."""
        _MEM_WORKER_LANES[(engine_slug.strip(), lane_name.strip())] = handler

    @staticmethod
    def bind_to_worker_registry(engine_slug: str, worker_registry: Any) -> int:
        """Attach registered lanes for ``engine_slug`` onto a Core worker registry."""
        count = 0
        for (slug, lane), fn in _MEM_WORKER_LANES.items():
            if slug != engine_slug:
                continue
            if isinstance(worker_registry, dict):
                worker_registry[f"{slug}.{lane}"] = fn
                count += 1
            elif callable(getattr(worker_registry, "register", None)):
                worker_registry.register(f"{slug}.{lane}", fn)
                count += 1
        return count

    @staticmethod
    async def run_with_retry_and_dlq(
        engine_slug: str,
        lane_name: str,
        payload: dict[str, Any],
        *,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        """Execute a registered lane with bounded exponential backoff and DLQ capture."""
        handler = _MEM_WORKER_LANES.get((engine_slug.strip(), lane_name.strip()))
        if handler is None:
            raise SubstrateError(f"Unregistered worker lane: {engine_slug}.{lane_name}")

        last_exc: Exception | None = None
        for attempt in range(1, max(1, max_attempts) + 1):
            try:
                res = handler(payload)
                if inspect.isawaitable(res):
                    res = await res
                return res if isinstance(res, dict) else {"status": "ok", "result": res}
            except Exception as exc:
                last_exc = exc
                if attempt < max_attempts:
                    await asyncio.sleep(min(0.05 * (2 ** (attempt - 1)), 0.5))

        dlq_entry = {
            "engine_slug": engine_slug,
            "lane_name": lane_name,
            "payload": redact_c8_allowlist(payload),
            "error": str(last_exc),
        }
        _MEM_DLQ.append(dlq_entry)
        return {"status": "dlq", "error": str(last_exc), "dlq": dlq_entry}

    @staticmethod
    def list_worker_dlq(engine_slug: str | None = None) -> list[dict[str, Any]]:
        """List DLQ entries captured by BackgroundJobRunner."""
        if engine_slug is None:
            return list(_MEM_DLQ)
        return [e for e in _MEM_DLQ if e.get("engine_slug") == engine_slug]

    @staticmethod
    def clear_dlq() -> None:
        """Clear captured DLQ items."""
        _MEM_DLQ.clear()


async def emit_kg_and_worm_link(
    engine: Any,
    namespace_id: Any,
    *,
    source_engine: str,
    source_id: str,
    target_type: str,
    target_id: str,
    relation: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record a cross-entity link in KG/topology & emit a WORM ``connector_action`` event."""
    ns_uuid_str = normalize_tenant_namespace(namespace_id)
    ns_uuid = uuid.UUID(ns_uuid_str)
    clean_meta = redact_c8_allowlist(dict(metadata or {}))
    sig_input = (
        f"{ns_uuid_str}:{source_engine}:{source_id}:{target_type}:{target_id}:{relation}".encode()
    )
    hmac_hex = hmac.new(_get_signing_key(), sig_input, hashlib.sha256).hexdigest()

    link_record: dict[str, Any] = {
        "namespace_id": ns_uuid_str,
        "source_engine": source_engine,
        "source_id": str(source_id),
        "target_type": target_type,
        "target_id": str(target_id),
        "relation": relation,
        "metadata": clean_meta,
        "link_hmac_sha256": hmac_hex,
    }

    pool = _resolve_pg_pool(engine)
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await set_namespace_context(conn, ns_uuid)
                    appended = await append_event(
                        conn=conn,
                        namespace_id=ns_uuid,
                        agent_id=f"connector:{source_engine}",
                        event_type="connector_action",
                        params={
                            "connector_slug": source_engine,
                            "action": "emit_kg_link",
                            "source_id": str(source_id),
                            "target_type": target_type,
                            "target_id": str(target_id),
                            "relation": relation,
                        },
                        result_summary=clean_meta,
                    )
                    link_record["event_id"] = str(appended.event_id)
                    link_record["event_seq"] = appended.event_seq
                    return link_record
        except Exception as exc:
            if not _is_missing_table_or_fk_error(exc):
                log.debug("emit_kg_and_worm_link PG path fell back to memory: %s", exc)

    _MEM_LINKS.append(link_record)
    return link_record


def clear_mem_substrate() -> None:
    """Reset in-memory substrate caches between unit tests."""
    _MEM_TABLES.clear()
    _MEM_BLOBS.clear()
    _MEM_LINKS.clear()
    _MEM_DLQ.clear()


__all__ = [
    "ArtifactStoreAdapter",
    "BackgroundJobRunner",
    "SubstrateError",
    "TenantSecretBox",
    "clear_mem_substrate",
    "emit_kg_and_worm_link",
    "normalize_record_uuid",
    "normalize_tenant_namespace",
    "pg_tenant_list",
    "pg_tenant_upsert",
]
