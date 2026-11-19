"""Unit tests for v3.7.0 CoW storage, crypto-shred tombstones, governance hydration,
salience bounds, T_cut watermark backup/restore, and ephemeral drills.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import tarfile
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from bson import ObjectId

from nce.config import cfg
from nce.db_utils import resolve_ops_dsn, scoped_mongo_session
from nce.envelope import (
    compute_master_key_hmac_fp,
    encrypt_raw_data,
    sign_shred_tombstone,
    verify_shred_tombstone,
)
from nce.models import AssertionType, MemoryType, StoreMemoryRequest
from nce.ops.backup import (
    BACKUP_LEASE_NAME,
    DEFERRED_SHRED_REDIS_KEY,
    create_watermark_backup,
    drain_deferred_shred_gc,
    get_backup_status,
    prune_old_backups,
    restore_watermark_backup,
    run_ephemeral_restore_drill,
    sweep_stale_drill_targets,
)
from nce.ops.cli import main as ops_cli_main
from nce.ops.models import OpsDiskHeadroomError, OpsRestorePreflightError
from nce.orchestrators.cognitive import CognitiveOrchestrator
from nce.orchestrators.memory import MemoryOrchestrator
from nce.salience_buffer import buffer_salience_boost
from nce.signing import canonical_json, require_master_key
from nce.storage import build_versioned_cow_object_key, put_cow_object, put_write_once_object
from nce.tool_governance import (
    GOVERNANCE,
    GovernanceUnavailable,
    ToolGovernanceCache,
    get_disabled_engines_with_pg_hydration,
    get_disabled_tools_with_pg_hydration,
    set_runtime_tool_enabled,
)

# ---------------------------------------------------------------------------
# In-memory test doubles for PG, MongoDB, MinIO, and Async Redis
# ---------------------------------------------------------------------------


class _FakeMongoCursor:
    def __init__(self, docs: list[dict[str, Any]]):
        self._docs = docs

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        if length is None:
            return list(self._docs)
        return list(self._docs)[:length]


class _InMemoryMongoCollection:
    def __init__(self, name: str):
        self.name = name
        self.docs: dict[Any, dict[str, Any]] = {}
        self.indexes: list[Any] = []

    def with_options(self, **_kwargs: Any) -> _InMemoryMongoCollection:
        return self

    async def create_index(self, keys: Any, **kwargs: Any) -> str:
        self.indexes.append((list(keys), dict(kwargs)))
        return "created_at_1"

    async def insert_one(self, doc: dict[str, Any]) -> SimpleNamespace:
        copied = dict(doc)
        oid = copied.setdefault("_id", ObjectId())
        self.docs[oid] = copied
        return SimpleNamespace(inserted_id=oid)

    async def insert_many(self, docs: list[dict[str, Any]]) -> None:
        for d in docs:
            await self.insert_one(d)

    def _matches(self, doc: dict[str, Any], flt: dict[str, Any] | None) -> bool:
        if not flt:
            return True
        for k, v in flt.items():
            if isinstance(v, dict) and "$gt" in v:
                val = doc.get(k)
                if val is None:
                    return False
                cmp_v = v["$gt"]
                if isinstance(val, datetime) and val.tzinfo is None:
                    val = val.replace(tzinfo=timezone.utc)
                if isinstance(cmp_v, datetime) and cmp_v.tzinfo is None:
                    cmp_v = cmp_v.replace(tzinfo=timezone.utc)
                if not (val > cmp_v):
                    return False
            else:
                if doc.get(k) != v:
                    return False
        return True

    def find(
        self, flt: dict[str, Any] | None = None, *_args: Any, **_kwargs: Any
    ) -> _FakeMongoCursor:
        return _FakeMongoCursor([dict(d) for d in self.docs.values() if self._matches(d, flt)])

    async def find_one(self, flt: dict[str, Any]) -> dict[str, Any] | None:
        for d in self.docs.values():
            if self._matches(d, flt):
                return dict(d)
        return None

    async def update_one(self, flt: dict[str, Any], update: dict[str, Any]) -> SimpleNamespace:
        for oid, d in self.docs.items():
            if self._matches(d, flt):
                for k, v in (update.get("$set") or {}).items():
                    d[k] = v
                for k in update.get("$unset") or {}:
                    d.pop(k, None)
                return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)

    async def update_many(self, flt: dict[str, Any], update: dict[str, Any]) -> SimpleNamespace:
        matched = 0
        for d in self.docs.values():
            if self._matches(d, flt):
                matched += 1
                for k, v in (update.get("$set") or {}).items():
                    d[k] = v
                for k in update.get("$unset") or {}:
                    d.pop(k, None)
        return SimpleNamespace(matched_count=matched, modified_count=matched)

    async def delete_one(self, flt: dict[str, Any]) -> SimpleNamespace:
        for oid, d in list(self.docs.items()):
            if self._matches(d, flt):
                del self.docs[oid]
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

    async def delete_many(self, flt: dict[str, Any]) -> SimpleNamespace:
        to_del = [oid for oid, d in self.docs.items() if self._matches(d, flt)]
        for oid in to_del:
            del self.docs[oid]
        return SimpleNamespace(deleted_count=len(to_del))


class _InMemoryMongoDatabase:
    def __init__(self, name: str):
        self.name = name
        self.collections: dict[str, _InMemoryMongoCollection] = {}

    def __getattr__(self, item: str) -> _InMemoryMongoCollection:
        return self[item]

    def __getitem__(self, item: str) -> _InMemoryMongoCollection:
        if item not in self.collections:
            self.collections[item] = _InMemoryMongoCollection(item)
        return self.collections[item]

    async def list_collection_names(self) -> list[str]:
        return sorted(self.collections.keys())


class _InMemoryMongoClient:
    def __init__(self) -> None:
        self.dbs: dict[str, _InMemoryMongoDatabase] = {}

    def __getattr__(self, item: str) -> _InMemoryMongoDatabase:
        return self[item]

    def __getitem__(self, item: str) -> _InMemoryMongoDatabase:
        if item not in self.dbs:
            self.dbs[item] = _InMemoryMongoDatabase(item)
        return self.dbs[item]

    async def list_database_names(self) -> list[str]:
        return sorted(self.dbs.keys())

    async def drop_database(self, name: str) -> None:
        self.dbs.pop(name, None)


class _MinioResponse:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data

    def close(self) -> None:
        return None

    def release_conn(self) -> None:
        return None


class _InMemoryMinio:
    def __init__(self) -> None:
        # bucket -> object_name -> (bytes, content_type, metadata)
        self.buckets: dict[str, dict[str, tuple[bytes, str, dict[str, str]]]] = {}

    def bucket_exists(self, bucket: str) -> bool:
        return bucket in self.buckets

    def make_bucket(self, bucket: str) -> None:
        self.buckets.setdefault(bucket, {})

    def list_buckets(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(name=b) for b in sorted(self.buckets.keys())]

    def make_bucket_if_missing(self, bucket: str) -> None:
        self.buckets.setdefault(bucket, {})

    def put_object(
        self,
        bucket: str,
        object_name: str,
        data_stream: Any,
        length: int,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> None:
        self.make_bucket_if_missing(bucket)
        raw = data_stream.read(length) if hasattr(data_stream, "read") else bytes(data_stream)
        formatted_meta = {
            (k if k.lower().startswith("x-amz-meta-") else f"X-Amz-Meta-{k}"): str(v)
            for k, v in (metadata or {}).items()
        }
        self.buckets[bucket][object_name] = (raw, content_type, formatted_meta)

    def fput_object(
        self,
        bucket: str,
        object_name: str,
        file_path: str,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> None:
        raw = Path(file_path).read_bytes()
        self.put_object(
            bucket,
            object_name,
            io.BytesIO(raw),
            len(raw),
            content_type=content_type,
            metadata=metadata,
        )

    def stat_object(self, bucket: str, object_name: str) -> SimpleNamespace:
        if bucket not in self.buckets or object_name not in self.buckets[bucket]:
            raise FileNotFoundError(f"NoSuchKey: {bucket}/{object_name}")
        raw, content_type, meta = self.buckets[bucket][object_name]
        return SimpleNamespace(
            bucket_name=bucket,
            object_name=object_name,
            size=len(raw),
            content_type=content_type,
            metadata=dict(meta),
        )

    def get_object(self, bucket: str, object_name: str) -> _MinioResponse:
        if bucket not in self.buckets or object_name not in self.buckets[bucket]:
            raise FileNotFoundError(f"NoSuchKey: {bucket}/{object_name}")
        return _MinioResponse(self.buckets[bucket][object_name][0])

    def list_objects(
        self, bucket: str, prefix: str = "", recursive: bool = True
    ) -> list[SimpleNamespace]:
        if bucket not in self.buckets:
            return []
        return [
            SimpleNamespace(object_name=k)
            for k in sorted(self.buckets[bucket].keys())
            if k.startswith(prefix)
        ]

    def remove_object(self, bucket: str, object_name: str) -> None:
        if bucket in self.buckets:
            self.buckets[bucket].pop(object_name, None)

    def remove_bucket(self, bucket: str) -> None:
        self.buckets.pop(bucket, None)


class _InMemoryAsyncRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.lists: dict[str, list[str]] = {}
        self.ttls: dict[str, int] = {}

    async def flushall(self) -> None:
        self.kv.clear()
        self.hashes.clear()
        self.lists.clear()
        self.ttls.clear()

    async def get(self, key: str) -> bytes | None:
        val = self.kv.get(key)
        return val.encode("utf-8") if val is not None else None

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.kv[key] = str(value)
        if ex is not None:
            self.ttls[key] = int(ex)
        return True

    async def setex(self, key: str, ttl: int, value: str) -> bool:
        return await self.set(key, value, ex=ttl)

    async def delete(self, *keys: str) -> int:
        deleted = 0
        for k in keys:
            if k in self.kv or k in self.hashes or k in self.lists:
                deleted += 1
            self.kv.pop(k, None)
            self.hashes.pop(k, None)
            self.lists.pop(k, None)
        return deleted

    async def expire(self, key: str, seconds: int) -> bool:
        self.ttls[key] = int(seconds)
        return True

    async def hkeys(self, key: str) -> list[bytes]:
        return [k.encode("utf-8") for k in self.hashes.get(key, {}).keys()]

    async def hset(self, key: str, field: str, value: str) -> int:
        bucket = self.hashes.setdefault(key, {})
        is_new = field not in bucket
        bucket[field] = str(value)
        return 1 if is_new else 0

    async def hdel(self, key: str, *fields: str) -> int:
        bucket = self.hashes.get(key, {})
        cnt = 0
        for f in fields:
            if f in bucket:
                del bucket[f]
                cnt += 1
        return cnt

    async def hgetall(self, key: str) -> dict[bytes, bytes]:
        return {k.encode("utf-8"): v.encode("utf-8") for k, v in self.hashes.get(key, {}).items()}

    async def hincrbyfloat(self, key: str, field: str, delta: float) -> float:
        bucket = self.hashes.setdefault(key, {})
        cur = float(bucket.get(field, 0.0)) + float(delta)
        bucket[field] = str(cur)
        return cur

    async def rpush(self, key: str, *values: str) -> int:
        lst = self.lists.setdefault(key, [])
        lst.extend(str(v) for v in values)
        return len(lst)

    async def lpop(self, key: str) -> bytes | None:
        lst = self.lists.get(key, [])
        if not lst:
            return None
        return lst.pop(0).encode("utf-8")

    async def lrem(self, key: str, count: int, value: str) -> int:
        lst = self.lists.get(key, [])
        before = len(lst)
        self.lists[key] = [x for x in lst if x != str(value)]
        return before - len(self.lists[key])

    async def llen(self, key: str) -> int:
        return len(self.lists.get(key, []))

    async def lrange(self, key: str, start: int, end: int) -> list[bytes]:
        lst = self.lists.get(key, [])
        stop = None if end == -1 else end + 1
        return [x.encode("utf-8") for x in lst[start:stop]]

    async def ltrim(self, key: str, start: int, end: int) -> bool:
        lst = self.lists.get(key, [])
        stop = None if end == -1 else end + 1
        self.lists[key] = lst[start:stop]
        return True

    async def scan_iter(self, match: str = "*", count: int = 100):
        for k in list(self.hashes.keys()):
            if k.startswith("nce:ns:") and k.endswith(":salience_buffer"):
                yield k.encode("utf-8")

    async def publish(self, channel: str, msg: str) -> int:
        return 1


class _InMemoryOpsTx:
    async def __aenter__(self) -> _InMemoryOpsTx:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _InMemoryOpsConn:
    """In-memory fake asyncpg connection implementing tables touched by CoW, shred,
    governance, and backup/restore tests.
    """

    def __init__(self, state: dict[str, Any]):
        self.state = state

    def is_in_transaction(self) -> bool:
        return True

    def transaction(self) -> _InMemoryOpsTx:
        return _InMemoryOpsTx()

    async def fetchval(self, sql: str, *args: Any) -> Any:
        s = " ".join(sql.split()).lower()
        if "select nce_is_backup_lease_active" in s:
            lease = self.state["backup_leases"].get(str(args[0]) if args else BACKUP_LEASE_NAME)
            if not lease:
                return False
            return lease["expires_at"] > datetime.now(timezone.utc)
        if "select pg_database_size" in s:
            return 1024
        if "select transaction_timestamp()" in s:
            return self.state["now_override"] or datetime.now(timezone.utc)
        if "select id from namespaces where slug" in s:
            return self.state["system_ns_id"]
        if "select archive_path from backup_manifests" in s:
            rows = sorted(
                self.state["backup_manifests"].values(),
                key=lambda r: r["t_cut"],
                reverse=True,
            )
            return rows[0]["archive_path"] if rows else None
        if "select count(*) from ops_quarantine" in s:
            return 0
        if "insert into memories" in s:
            new_id = uuid.uuid4()
            self.state["memories"][str(new_id)] = {
                "id": new_id,
                "user_id": args[0],
                "session_id": args[1],
                "namespace_id": str(args[2]),
                "agent_id": args[3],
                "payload_ref": args[5],
                "assertion_type": args[6],
                "memory_type": args[7],
                "metadata": json.loads(args[8]),
                "wrapped_dek": args[9],
                "dek_key_id": args[10],
                "valid_from": args[11],
                "valid_to": None,
                "derived_from": args[12],
                "created_at": args[11],
            }
            return new_id
        return None

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        s = " ".join(sql.split()).lower()
        if "from memories" in s and "where id = $1::uuid" in s:
            mem = self.state["memories"].get(str(args[0]))
            if not mem or str(mem["namespace_id"]) != str(args[1]):
                return None
            if "valid_to is null" in s and mem.get("valid_to") is not None:
                return None
            return {
                **mem,
                "was_encrypted": mem.get("wrapped_dek") is not None,
            }
        if "from backup_manifests" in s:
            rows = sorted(
                self.state["backup_manifests"].values(),
                key=lambda r: r["t_cut"],
                reverse=True,
            )
            return rows[0] if rows else None
        if "from event_log where id" in s:
            ev = self.state["events"][-1] if self.state["events"] else None
            return ev
        return None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        s = " ".join(sql.split()).lower()
        if "from nce_get_disabled_runtime_tools" in s:
            return [
                {"scope_key": k[0], "tool_name": k[1]}
                for k, v in self.state["runtime_tool_governance"].items()
                if not v["enabled"]
            ]
        if "from applied_migrations" in s:
            return [{"version": v} for v in self.state["applied_migrations"]]
        if "from plugin_applied_migrations" in s:
            return list(self.state["plugin_applied_migrations"])
        if "from event_log" in s:
            return []
        if "from memories" in s:
            return [
                {
                    "memory_id": str(m["id"]),
                    "namespace_id": str(m["namespace_id"]),
                    "payload_ref": m.get("payload_ref"),
                    "dek_key_id": m.get("dek_key_id"),
                    "has_dek": m.get("wrapped_dek") is not None,
                    "metadata": m.get("metadata") or {},
                    "valid_from": m.get("valid_from"),
                    "valid_to": m.get("valid_to"),
                    "created_at": m.get("created_at"),
                }
                for m in self.state["memories"].values()
            ]
        return []

    async def execute(self, sql: str, *args: Any) -> str:
        s = " ".join(sql.split()).lower()
        if "set local" in s or "select set_config" in s:
            return "OK"
        if "insert into backup_leases" in s:
            self.state["backup_leases"][str(args[0])] = {
                "lease_name": str(args[0]),
                "holder_id": str(args[1]),
                "expires_at": datetime.now(timezone.utc) + timedelta(minutes=30),
            }
            return "INSERT 0 1"
        if "delete from backup_leases" in s:
            lease = self.state["backup_leases"].get(str(args[0]))
            if lease and lease["holder_id"] == str(args[1]):
                del self.state["backup_leases"][str(args[0])]
            return "DELETE 1"
        if "insert into backup_manifests" in s:
            self.state["backup_manifests"][str(args[0])] = {
                "backup_id": str(args[0]),
                "t_cut": args[1],
                "t_blob_completed": args[2],
                "archive_path": args[3],
                "archive_sha256": args[4],
                "size_bytes": args[5],
                "master_key_hmac_fp": args[6],
                "schema_heads": json.loads(args[7]),
                "worm_heads": json.loads(args[8]),
                "tombstone_count": args[9],
                "status": args[10],
                "last_drill_at": None,
                "last_drill_status": None,
                "last_drill_report": None,
            }
            return "INSERT 0 1"
        if "update backup_manifests" in s:
            rec = self.state["backup_manifests"].get(str(args[0]))
            if rec:
                rec["last_drill_at"] = datetime.now(timezone.utc)
                rec["last_drill_status"] = args[1]
                rec["last_drill_report"] = json.loads(args[2])
            return "UPDATE 1"
        if "insert into runtime_tool_governance" in s:
            self.state["runtime_tool_governance"][(str(args[0]), str(args[1]))] = {
                "scope_key": str(args[0]),
                "tool_name": str(args[1]),
                "enabled": bool(args[2]),
                "reason": str(args[3]),
                "updated_by": str(args[4]),
            }
            return "INSERT 0 1"
        if "update memories" in s and "set wrapped_dek = null" in s:
            cnt = 0
            if len(args) == 2:
                mem = self.state["memories"].get(str(args[0]))
                if mem and str(mem["namespace_id"]) == str(args[1]):
                    mem["wrapped_dek"] = None
                    mem["dek_key_id"] = None
                    mem["valid_to"] = mem.get("valid_to") or datetime.now(timezone.utc)
                    cnt = 1
            elif len(args) == 3:
                ns_s, dek_s, mids = str(args[0]), str(args[1]), set(args[2] or [])
                for mem in self.state["memories"].values():
                    if str(mem["namespace_id"]) == ns_s and (
                        (dek_s and mem.get("dek_key_id") == dek_s)
                        or (mem.get("payload_ref") in mids)
                    ):
                        mem["wrapped_dek"] = None
                        mem["dek_key_id"] = None
                        mem["valid_to"] = mem.get("valid_to") or datetime.now(timezone.utc)
                        cnt += 1
            return f"UPDATE {cnt}"
        if "update memories" in s and "set valid_to = $1" in s:
            mem = self.state["memories"].get(str(args[1]))
            if mem and str(mem["namespace_id"]) == str(args[2]) and mem.get("valid_to") is None:
                mem["valid_to"] = args[0]
                return "UPDATE 1"
            return "UPDATE 0"
        if "update encryption_deks" in s:
            dek = self.state["encryption_deks"].get(str(args[0]))
            if dek and str(dek["namespace_id"]) == str(args[1]):
                dek["wrapped_key"] = b"\x00" * 32
                dek["shredded_at"] = datetime.now(timezone.utc)
                return "UPDATE 1"
            return "UPDATE 0"
        if "delete from" in s:
            return "DELETE 0"
        return "OK"


class _InMemoryOpsPool:
    def __init__(self) -> None:
        self.state: dict[str, Any] = {
            "memories": {},
            "encryption_deks": {},
            "backup_leases": {},
            "backup_manifests": {},
            "runtime_tool_governance": {},
            "applied_migrations": ["042_day2_ops_backup_and_governance"],
            "plugin_applied_migrations": [],
            "events": [],
            "system_ns_id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
            "now_override": None,
        }

    @asynccontextmanager
    async def acquire(self, timeout: float | None = None):
        yield _InMemoryOpsConn(self.state)


# ---------------------------------------------------------------------------
# Task 1: Strict Copy-on-Write (CoW) & Indexed Blob Metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cow_mongo_stamping_index_and_in_place_mutation_forbidden() -> None:
    ns_id = str(uuid.uuid4())
    mongo = _InMemoryMongoClient()
    pg_pool = _InMemoryOpsPool()
    redis = _InMemoryAsyncRedis()
    orch = MemoryOrchestrator(pg_pool=pg_pool, mongo_client=mongo, redis_client=redis)

    payload = StoreMemoryRequest(
        namespace_id=ns_id,
        agent_id="agent-alpha",
        content="original episodic payload",
        summary="summary",
        memory_type=MemoryType.episodic,
        assertion_type=AssertionType.observation,
        metadata={"user_id": "u1", "session_id": "s1"},
    )
    oid_str, _, _, _ = await orch._store_episodic_mongodb(
        payload,
        "original episodic payload",
        SimpleNamespace(redacted=False, entities_found=[]),
        saga_id="saga-cow-1",
    )
    stored_doc = mongo.memory_archive.episodes.docs[ObjectId(oid_str)]
    assert isinstance(stored_doc["created_at"], datetime)
    assert stored_doc["created_at"].tzinfo == timezone.utc
    assert stored_doc["saga_id"] == "saga-cow-1"
    assert (
        [("created_at", 1)],
        {"background": True},
    ) in mongo.memory_archive.episodes.indexes

    # Direct in-place raw_data/raw_code mutation must raise ValueError (CoW violation),
    # while shred_memory tombstone erasure (shredded=True, raw_data=None) remains allowed.
    async with scoped_mongo_session(mongo, ns_id) as s_db:
        with pytest.raises(ValueError, match="CoW violation"):
            await s_db.episodes.update_one(
                {"_id": ObjectId(oid_str)},
                {"$set": {"raw_data": "mutated in place"}},
            )
        await s_db.episodes.update_one(
            {"_id": ObjectId(oid_str)},
            {"$set": {"raw_data": None, "shredded": True}},
        )


@pytest.mark.asyncio
async def test_cow_update_memory_and_minio_versioning(monkeypatch: pytest.MonkeyPatch) -> None:
    ns_id = str(uuid.uuid4())
    pg_pool = _InMemoryOpsPool()
    mongo = _InMemoryMongoClient()
    minio = _InMemoryMinio()
    redis = _InMemoryAsyncRedis()
    orch = MemoryOrchestrator(
        pg_pool=pg_pool, mongo_client=mongo, redis_client=redis, minio_client=minio
    )

    monkeypatch.setattr(cfg, "NCE_ENVELOPE_ENCRYPTION_ENABLED", False)
    old_oid = ObjectId()
    old_mem_id = uuid.uuid4()
    t0 = datetime.now(timezone.utc) - timedelta(minutes=10)
    await mongo.memory_archive.episodes.insert_one(
        {
            "_id": old_oid,
            "namespace_id": ns_id,
            "raw_data": "v1 content",
            "created_at": t0,
            "saga_id": "saga-v1",
        }
    )
    pg_pool.state["memories"][str(old_mem_id)] = {
        "id": old_mem_id,
        "user_id": "u1",
        "session_id": "s1",
        "namespace_id": ns_id,
        "agent_id": "agent-1",
        "payload_ref": str(old_oid),
        "assertion_type": "observation",
        "memory_type": "episodic",
        "metadata": {},
        "wrapped_dek": None,
        "dek_key_id": None,
        "valid_from": t0,
        "valid_to": None,
        "created_at": t0,
    }

    base_key = put_write_once_object(
        minio, "nce-artifacts", ns_id, "document", b"blob-v1", saga_id="saga-1"
    )
    st1 = minio.stat_object("nce-artifacts", base_key)
    assert "X-Amz-Meta-created-at" in st1.metadata
    assert st1.metadata["X-Amz-Meta-saga-id"] == "saga-1"

    versioned = put_cow_object(
        minio,
        "nce-artifacts",
        ns_id,
        "document",
        b"blob-v1",
        saga_id="saga-2",
    )
    assert versioned != base_key
    assert versioned.startswith(f"{base_key}_v_")
    assert (
        build_versioned_cow_object_key(ns_id, "document", b"blob-v1", version_id="custom1")
        == f"{base_key}_v_custom1"
    )
    assert minio.get_object("nce-artifacts", base_key).read() == b"blob-v1"
    assert minio.get_object("nce-artifacts", versioned).read() == b"blob-v1"

    res = await orch.update_memory_cow(
        memory_id=str(old_mem_id),
        namespace_id=ns_id,
        agent_id="agent-1",
        new_raw_data="v2 content",
        minio_payload=b"blob-v3",
        minio_bucket="nce-artifacts",
        minio_artifact_type="document",
        saga_id="saga-cow-2",
    )
    assert res["payload_ref"] != str(old_oid)
    assert res["minio_object_key"] not in (None, base_key)
    assert pg_pool.state["memories"][str(old_mem_id)]["valid_to"] is not None
    assert pg_pool.state["memories"][res["memory_id"]]["valid_to"] is None
    assert mongo.memory_archive.episodes.docs[old_oid]["raw_data"] == "v1 content"
    assert (
        mongo.memory_archive.episodes.docs[ObjectId(res["payload_ref"])]["raw_data"] == "v2 content"
    )


# ---------------------------------------------------------------------------
# Task 2: Two-Phase crypto_shred, Tombstones, Active Lease Deferral & GC Drain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shred_and_forget_crypto_shred_tombstones_and_lease_deferral(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NCE_BACKUP_DIR", str(tmp_path))
    monkeypatch.setattr(cfg, "NCE_BACKUP_DIR", str(tmp_path), raising=False)
    ns_id = str(uuid.uuid4())
    pg_pool = _InMemoryOpsPool()
    mongo = _InMemoryMongoClient()
    minio = _InMemoryMinio()
    redis = _InMemoryAsyncRedis()

    async def _fake_atms_eval(*_args: Any, **_kwargs: Any) -> set[str]:
        return set()

    async def _fake_atms_persist(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _fake_append_event(**_kwargs: Any) -> SimpleNamespace:
        ev_id = uuid.uuid4()
        ev = {
            "id": ev_id,
            "namespace_id": uuid.UUID(ns_id),
            "agent_id": "agent-1",
            "event_type": "memory_shredded",
            "event_seq": 1,
            "occurred_at": datetime.now(timezone.utc),
            "params": {},
            "signature": b"sig",
            "signature_key_id": "k1",
            "signature_version": 1,
            "chain_hash": b"\x01" * 32,
        }
        pg_pool.state["events"].append(ev)
        return SimpleNamespace(event_id=ev_id, event_seq=1, occurred_at=ev["occurred_at"])

    async def _fake_verify_sig(*_args: Any, **_kwargs: Any) -> None:
        return None

    # Scenario A: Active backup lease defers Mongo/MinIO deletion while DEK + tombstone commit.
    mem_id = uuid.uuid4()
    dek_id = str(uuid.uuid4())
    oid = ObjectId()
    ciphertext, wrapped_dek, _ = encrypt_raw_data("sensitive secret content")
    obj_key = put_write_once_object(minio, "nce-artifacts", ns_id, "document", b"cipherbytes")
    await mongo.memory_archive.episodes.insert_one(
        {
            "_id": oid,
            "namespace_id": ns_id,
            "raw_data": ciphertext,
            "created_at": datetime.now(timezone.utc),
        }
    )
    pg_pool.state["encryption_deks"][dek_id] = {
        "dek_id": dek_id,
        "namespace_id": ns_id,
        "wrapped_key": wrapped_dek,
        "shredded_at": None,
    }
    pg_pool.state["memories"][str(mem_id)] = {
        "id": mem_id,
        "user_id": "u1",
        "session_id": "s1",
        "namespace_id": ns_id,
        "agent_id": "agent-1",
        "payload_ref": str(oid),
        "dek_key_id": dek_id,
        "wrapped_dek": wrapped_dek,
        "metadata": {"bucket": "nce-artifacts", "object_name": obj_key},
        "valid_from": datetime.now(timezone.utc),
        "valid_to": None,
    }
    pg_pool.state["backup_leases"][BACKUP_LEASE_NAME] = {
        "lease_name": BACKUP_LEASE_NAME,
        "holder_id": "active-holder",
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=15),
    }

    cog = CognitiveOrchestrator(
        pg_pool=pg_pool, mongo_client=mongo, redis_client=redis, minio_client=minio
    )
    with (
        patch("nce.atms.evaluate_atms_intervention", side_effect=_fake_atms_eval),
        patch("nce.atms.persist_atms_invalidation", side_effect=_fake_atms_persist),
        patch("nce.event_log.append_event", side_effect=_fake_append_event),
        patch("nce.event_log.verify_event_signature", side_effect=_fake_verify_sig),
    ):
        forget_res = await cog.forget_memory(
            memory_id=str(mem_id),
            agent_id="agent-1",
            namespace_id=ns_id,
            crypto_shred=True,
        )

    assert forget_res["crypto_shred"] is True
    assert forget_res["receipt"]["deferred_during_backup"] is True
    assert pg_pool.state["memories"][str(mem_id)]["wrapped_dek"] is None
    assert pg_pool.state["encryption_deks"][dek_id]["wrapped_key"] == b"\x00" * 32
    assert pg_pool.state["encryption_deks"][dek_id]["shredded_at"] is not None

    # Ciphertext must NOT be deleted while backup lease is active!
    assert mongo.memory_archive.episodes.docs[oid]["raw_data"] == ciphertext
    assert minio.bucket_exists("nce-artifacts") and obj_key in minio.buckets["nce-artifacts"]
    assert len(redis.lists.get(DEFERRED_SHRED_REDIS_KEY, [])) == 1
    assert redis.ttls[DEFERRED_SHRED_REDIS_KEY] == 604800

    tomb_path = tmp_path / "shred_tombstones.jsonl"
    assert tomb_path.exists()
    tomb_rec = json.loads(tomb_path.read_text(encoding="utf-8").splitlines()[0])
    assert verify_shred_tombstone(tomb_rec) is True
    assert "sensitive secret content" not in json.dumps(tomb_rec)
    assert tomb_rec["dek_id"] == dek_id

    # Drain skips while lease is still active, then drains cleanly after lease clears.
    skip_res = await drain_deferred_shred_gc(pg_pool, mongo, minio, redis, backup_dir=tmp_path)
    assert skip_res["skipped_lease_active"] is True
    assert obj_key in minio.buckets["nce-artifacts"]

    del pg_pool.state["backup_leases"][BACKUP_LEASE_NAME]
    drain_res = await drain_deferred_shred_gc(pg_pool, mongo, minio, redis, backup_dir=tmp_path)
    assert drain_res["drained"] == 1
    assert obj_key not in minio.buckets["nce-artifacts"]
    assert mongo.memory_archive.episodes.docs[oid]["shredded"] is True
    assert mongo.memory_archive.episodes.docs[oid]["raw_data"] is None

    # Scenario B: Lease active + Redis unavailable falls back to deferred_shreds.jsonl.
    pg_pool.state["backup_leases"][BACKUP_LEASE_NAME] = {
        "lease_name": BACKUP_LEASE_NAME,
        "holder_id": "holder-2",
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
    }
    mem2 = uuid.uuid4()
    oid2 = ObjectId()
    await mongo.memory_archive.episodes.insert_one(
        {
            "_id": oid2,
            "namespace_id": ns_id,
            "raw_data": "cipher-2",
            "created_at": datetime.now(timezone.utc),
        }
    )
    pg_pool.state["memories"][str(mem2)] = {
        "id": mem2,
        "user_id": "u1",
        "session_id": "s1",
        "namespace_id": ns_id,
        "agent_id": "agent-1",
        "payload_ref": str(oid2),
        "dek_key_id": "dek-2",
        "wrapped_dek": b"wrapped-2",
        "metadata": {},
        "valid_from": datetime.now(timezone.utc),
        "valid_to": None,
    }
    orch_no_redis = MemoryOrchestrator(pg_pool=pg_pool, mongo_client=mongo, redis_client=None)
    with (
        patch("nce.atms.evaluate_atms_intervention", side_effect=_fake_atms_eval),
        patch("nce.atms.persist_atms_invalidation", side_effect=_fake_atms_persist),
        patch("nce.event_log.append_event", side_effect=_fake_append_event),
        patch("nce.event_log.verify_event_signature", side_effect=_fake_verify_sig),
    ):
        await orch_no_redis.shred_memory(str(mem2), ns_id, "agent-1")

    assert (tmp_path / "deferred_shreds.jsonl").exists()
    del pg_pool.state["backup_leases"][BACKUP_LEASE_NAME]
    drain2 = await drain_deferred_shred_gc(pg_pool, mongo, minio, None, backup_dir=tmp_path)
    assert drain2["drained"] == 1
    assert mongo.memory_archive.episodes.docs[oid2]["shredded"] is True
    assert not (tmp_path / "deferred_shreds.jsonl").exists()


# ---------------------------------------------------------------------------
# Task 3: resolve_ops_dsn, Governance PG Hydration & Salience Buffer Bounds
# ---------------------------------------------------------------------------


def test_resolve_ops_dsn_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "NCE_GC_DSN", "", raising=False)
    monkeypatch.setattr(cfg, "NCE_MIGRATION_PG_DSN", "", raising=False)
    monkeypatch.setattr(cfg, "PG_DSN", "postgresql://app:pw@db/nce", raising=False)
    monkeypatch.delenv("NCE_GC_DSN", raising=False)
    monkeypatch.delenv("NCE_MIGRATION_PG_DSN", raising=False)
    monkeypatch.delenv("PG_DSN", raising=False)
    assert resolve_ops_dsn() == "postgresql://app:pw@db/nce"

    monkeypatch.setenv("NCE_MIGRATION_PG_DSN", "postgresql://mig:pw@db/nce")
    assert resolve_ops_dsn() == "postgresql://mig:pw@db/nce"

    monkeypatch.setenv("NCE_GC_DSN", "postgresql://gc:pw@db/nce")
    assert resolve_ops_dsn() == "postgresql://gc:pw@db/nce"


@pytest.mark.asyncio
async def test_tool_governance_pg_hydration_after_flushall_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pg_pool = _InMemoryOpsPool()
    redis = _InMemoryAsyncRedis()
    GOVERNANCE._snapshot = None
    GOVERNANCE._fetched_at = None

    await set_runtime_tool_enabled(
        pg_pool, redis, "tool", "danger_tool", False, updated_by="secops", reason="audit"
    )
    await set_runtime_tool_enabled(
        pg_pool, redis, "engine", "legacy_engine", False, updated_by="secops", reason="halt"
    )

    assert "danger_tool" in await get_disabled_tools_with_pg_hydration(redis, pg_pool)
    assert "legacy_engine" in await get_disabled_engines_with_pg_hydration(redis, pg_pool)

    # Simulate Redis FLUSHALL wiping all hashes & sentinel.
    await redis.flushall()
    GOVERNANCE._snapshot = None
    GOVERNANCE._fetched_at = None

    cache = ToolGovernanceCache()
    assert await cache.is_disabled(redis, "danger_tool", pg_pool=pg_pool) is True
    assert await cache.is_disabled(redis, "safe_tool", pg_pool=pg_pool) is False
    assert (await redis.get("nce:governance:cache_hydrated")) == b"1"
    assert redis.ttls["nce:governance:cache_hydrated"] == 300
    assert b"legacy_engine" in await redis.hkeys("nce:engines:disabled")

    # Both Redis and PG failing in prod MUST raise GovernanceUnavailable in < 2.0s.
    monkeypatch.setattr(cfg, "IS_PROD", True)

    class _DownRedis:
        async def get(self, _k: str) -> None:
            raise ConnectionError("redis down")

        async def hkeys(self, _k: str) -> list[bytes]:
            raise ConnectionError("redis down")

    class _SlowBrokenPool:
        @asynccontextmanager
        async def acquire(self, timeout: float | None = None):
            raise TimeoutError("pg unreachable")
            yield  # pragma: no cover

    t0 = time.monotonic()
    with pytest.raises(GovernanceUnavailable):
        await get_disabled_tools_with_pg_hydration(_DownRedis(), _SlowBrokenPool())
    assert (time.monotonic() - t0) < 2.0


@pytest.mark.asyncio
async def test_salience_buffer_enforces_max_len_and_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NCE_SALIENCE_BUFFER_MAX_LEN", "3")
    monkeypatch.setattr(cfg, "NCE_SALIENCE_BUFFER_MAX_LEN", 3, raising=False)
    ns_id = str(uuid.uuid4())
    redis = _InMemoryAsyncRedis()

    m1, m2, m3, m4 = (str(uuid.uuid4()) for _ in range(4))
    for mid in (m1, m2, m3, m4):
        ok = await buffer_salience_boost(redis, ns_id, mid, agent_id="agent-a", delta=0.1)
        assert ok is True

    buf_key = f"nce:ns:{ns_id}:salience_buffer"
    order_key = f"nce:ns:{ns_id}:salience_buffer_order"
    assert len(redis.hashes[buf_key]) == 3
    assert f"{m1}:agent-a" not in redis.hashes[buf_key]
    assert f"{m4}:agent-a" in redis.hashes[buf_key]
    assert redis.ttls[buf_key] == 86400
    assert redis.ttls[order_key] == 86400


# ---------------------------------------------------------------------------
# Task 5: Watermark Backup, Restore Preflight, Reconciliation & Drill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disk_headroom_and_retention_prune_guards(tmp_path: Path) -> None:
    pg_pool = _InMemoryOpsPool()
    mongo = _InMemoryMongoClient()
    minio = _InMemoryMinio()

    # Insufficient headroom raises OpsDiskHeadroomError cleanly without writing archives.
    fake_usage = SimpleNamespace(total=100 * 1024**3, used=99 * 1024**3, free=1024)
    with patch("nce.ops.backup.shutil.disk_usage", return_value=fake_usage):
        with pytest.raises(OpsDiskHeadroomError):
            await create_watermark_backup(
                pg_pool=pg_pool,
                mongo_client=mongo,
                minio_client=minio,
                backup_dir=tmp_path,
                estimated_dataset_bytes=5000,
                min_free_bytes=1024,
            )

    # Retention pruning preserves newest N archives after verified backup.
    old_names = []
    for idx in range(4):
        p = tmp_path / f"nce-backup-2026010{idx + 1}T000000Z-{uuid.uuid4()}.tar.gz"
        p.write_bytes(b"old")
        os.utime(p, (1000 + idx, 1000 + idx))
        old_names.append(p)
    pruned = prune_old_backups(tmp_path, retention_count=2)
    assert len(pruned) == 2
    assert not old_names[0].exists() and not old_names[1].exists()
    assert old_names[2].exists() and old_names[3].exists()


@pytest.mark.asyncio
async def test_backup_restore_zero_resurrection_reconcile_and_drill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NCE_BACKUP_DIR", str(tmp_path))
    monkeypatch.setattr(cfg, "NCE_BACKUP_DIR", str(tmp_path), raising=False)

    ns_id = str(uuid.uuid4())
    pg_pool = _InMemoryOpsPool()
    mongo = _InMemoryMongoClient()
    minio = _InMemoryMinio()
    redis = _InMemoryAsyncRedis()

    t_pre = datetime.now(timezone.utc) - timedelta(minutes=2)
    t_cut_fixed = datetime.now(timezone.utc)
    pg_pool.state["now_override"] = t_cut_fixed

    # Pre-T_cut active CoW memory + shredded/unshredded historical memories.
    oid_live = ObjectId()
    mem_live = uuid.uuid4()
    obj_live = put_write_once_object(
        minio, "nce-artifacts", ns_id, "document", b"live-blob", created_at=t_pre
    )
    await mongo.memory_archive.episodes.insert_one(
        {"_id": oid_live, "namespace_id": ns_id, "raw_data": "live text", "created_at": t_pre}
    )
    pg_pool.state["memories"][str(mem_live)] = {
        "id": mem_live,
        "user_id": "u1",
        "session_id": "s1",
        "namespace_id": ns_id,
        "agent_id": "agent-1",
        "payload_ref": str(oid_live),
        "dek_key_id": None,
        "wrapped_dek": None,
        "metadata": {"bucket": "nce-artifacts", "object_name": obj_live},
        "valid_from": t_pre,
        "valid_to": None,
        "created_at": t_pre,
    }

    # Pre-T_cut encrypted memory that will be shredded AFTER backup T_cut (must NOT resurrect!).
    oid_shred = ObjectId()
    mem_shred = uuid.uuid4()
    dek_shred = str(uuid.uuid4())
    cipher_shred, wrapped_shred, _ = encrypt_raw_data("forget me after backup")
    obj_shred = put_write_once_object(
        minio, "nce-artifacts", ns_id, "document", b"secret-blob", created_at=t_pre
    )
    await mongo.memory_archive.episodes.insert_one(
        {"_id": oid_shred, "namespace_id": ns_id, "raw_data": cipher_shred, "created_at": t_pre}
    )
    pg_pool.state["memories"][str(mem_shred)] = {
        "id": mem_shred,
        "user_id": "u1",
        "session_id": "s1",
        "namespace_id": ns_id,
        "agent_id": "agent-1",
        "payload_ref": str(oid_shred),
        "dek_key_id": dek_shred,
        "wrapped_dek": wrapped_shred,
        "metadata": {"bucket": "nce-artifacts", "object_name": obj_shred},
        "valid_from": t_pre,
        "valid_to": None,
        "created_at": t_pre,
    }

    async def _fake_pg_dump(_dsn: str, out_path: Path) -> None:
        await asyncio.to_thread(out_path.write_bytes, b"PGDMP-synthetic")

    manifest = await create_watermark_backup(
        pg_pool=pg_pool,
        mongo_client=mongo,
        minio_client=minio,
        redis_client=redis,
        backup_dir=tmp_path,
        min_free_bytes=1024,
        pg_dump_fn=_fake_pg_dump,
    )
    assert manifest.status == "verified"
    assert manifest.master_key_hmac_fp == compute_master_key_hmac_fp()
    assert BACKUP_LEASE_NAME not in pg_pool.state["backup_leases"]

    # Simulate post-T_cut crypto_shred recording a signed tombstone after the backup snapshot was cut.
    tomb_rec = {
        "namespace_id": ns_id,
        "dek_id": dek_shred,
        "mongo_ids": [str(oid_shred)],
        "minio_keys": [f"nce-artifacts/{obj_shred}"],
        "shredded_at": (t_cut_fixed + timedelta(seconds=5)).isoformat(),
    }
    tomb_rec["hmac_sha256"] = sign_shred_tombstone(tomb_rec)
    (tmp_path / "shred_tombstones.jsonl").write_text(
        json.dumps(tomb_rec, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    # Also simulate an orphan post-T_cut blob restored into target stores alongside pre-T_cut history.
    orphan_oid = ObjectId()
    t_post = t_cut_fixed + timedelta(seconds=10)
    await mongo.memory_archive.episodes.insert_one(
        {
            "_id": orphan_oid,
            "namespace_id": ns_id,
            "raw_data": "post-cut orphan",
            "created_at": t_post,
        }
    )
    orphan_key = put_write_once_object(
        minio, "nce-artifacts", ns_id, "document", b"orphan-post-cut-bytes", created_at=t_post
    )

    # Preflight failure tests: corrupt archive SHA-256, wrong manifest HMAC, wrong master key FP, stale schema.
    archive_p = Path(manifest.archive_path)
    sidecar_p = Path(f"{archive_p}.manifest.json")
    raw_sidecar = await asyncio.to_thread(sidecar_p.read_text, encoding="utf-8")
    bad_manifest = json.loads(raw_sidecar)
    bad_manifest["manifest_hmac_sha256"] = "0" * 64
    with pytest.raises(OpsRestorePreflightError, match="HMAC"):
        await restore_watermark_backup(archive_p, manifest=bad_manifest, backup_dir=tmp_path)

    with pytest.raises(OpsRestorePreflightError, match="Master key"):
        wrong_fp_manifest = manifest.model_copy(update={"master_key_hmac_fp": "f" * 64}).model_dump(
            mode="json"
        )
        # Re-sign with current key so HMAC passes and key-fp check triggers cleanly.
        payload = {k: v for k, v in wrong_fp_manifest.items() if k != "manifest_hmac_sha256"}
        with require_master_key() as mk:
            import hmac as _hmac

            wrong_fp_manifest["manifest_hmac_sha256"] = _hmac.new(
                bytes(mk.key_bytes), canonical_json(payload), hashlib.sha256
            ).hexdigest()
        await restore_watermark_backup(archive_p, manifest=wrong_fp_manifest, backup_dir=tmp_path)

    with pytest.raises(OpsRestorePreflightError, match="schema"):
        await restore_watermark_backup(
            archive_p,
            manifest=manifest,
            backup_dir=tmp_path,
            current_schema_heads={"core_head": "041_old", "plugins": {}},
        )

    # Path traversal tar entry rejection.
    traversal_tar = tmp_path / "nce-backup-bad.tar.gz"
    with tarfile.open(traversal_tar, "w:gz") as tf:
        data = b"evil"
        ti = tarfile.TarInfo("../escape.txt")
        ti.size = len(data)
        tf.addfile(ti, io.BytesIO(data))
    trav_manifest = manifest.model_copy(
        update={
            "archive_path": str(traversal_tar),
            "archive_sha256": hashlib.sha256(traversal_tar.read_bytes()).hexdigest(),
            "file_digests": {},
        }
    ).model_dump(mode="json")
    trav_payload = {k: v for k, v in trav_manifest.items() if k != "manifest_hmac_sha256"}
    with require_master_key() as mk:
        import hmac as _hmac

        trav_manifest["manifest_hmac_sha256"] = _hmac.new(
            bytes(mk.key_bytes), canonical_json(trav_payload), hashlib.sha256
        ).hexdigest()
    with pytest.raises(OpsRestorePreflightError, match="traversal"):
        await restore_watermark_backup(traversal_tar, manifest=trav_manifest, backup_dir=tmp_path)

    # Real restore + tombstone replay + post-T_cut orphan cleanup + audit log!
    audit_events: list[dict[str, Any]] = []

    async def _capture_audit(**kwargs: Any) -> SimpleNamespace:
        audit_events.append(kwargs)
        return SimpleNamespace(event_id=uuid.uuid4())

    pg_restored_calls: list[Path] = []

    async def _fake_pg_restore(_dsn: str, dump_path: Path) -> None:
        pg_restored_calls.append(dump_path)
        # Simulate PG briefly restoring pre-shred DEK from pg_dump before tombstone replay runs.
        pg_pool.state["memories"][str(mem_shred)]["wrapped_dek"] = wrapped_shred
        pg_pool.state["memories"][str(mem_shred)]["dek_key_id"] = dek_shred

    with patch("nce.event_log.append_event", side_effect=_capture_audit):
        restore_res = await restore_watermark_backup(
            archive_p,
            pg_pool=pg_pool,
            mongo_client=mongo,
            minio_client=minio,
            backup_dir=tmp_path,
            pg_restore_fn=_fake_pg_restore,
        )

    assert len(pg_restored_calls) == 1
    assert restore_res["tombstones_replayed"] == 1
    assert restore_res["resurrected_dek_count"] == 0
    assert restore_res["missing_pre_cut_pointers"] == 0
    assert restore_res["swept_post_cut_blobs"] >= 1
    assert pg_pool.state["memories"][str(mem_shred)]["wrapped_dek"] is None
    assert mongo.memory_archive.episodes.docs[oid_shred]["shredded"] is True
    assert oid_live in mongo.memory_archive.episodes.docs
    assert orphan_key not in minio.buckets["nce-artifacts"]
    assert obj_live in minio.buckets["nce-artifacts"]
    assert obj_shred not in minio.buckets["nce-artifacts"]
    assert audit_events and audit_events[0]["event_type"] == "config_changed"

    # Ephemeral drill restores into isolated DB/buckets and cleans up without WORM noise.
    # Re-create orphan in live mongo & make sure drill targets clean ephemeral DB only.
    async def _noop_drill_pg(_dsn: str, _dump: Path) -> None:
        return None

    drill_report = await run_ephemeral_restore_drill(
        archive_p,
        backup_dir=tmp_path,
        pg_pool=pg_pool,
        mongo_client=mongo,
        minio_client=minio,
        drill_pg_restore_fn=_noop_drill_pg,
    )
    assert drill_report.status == "passed"
    assert drill_report.cleanup_verified is True
    assert drill_report.resurrected_dek_count == 0
    assert not any(db.startswith("nce_drill_") for db in mongo.dbs)
    assert not any(b.startswith("nce-drill-") for b in minio.buckets)
    assert (
        pg_pool.state["backup_manifests"][str(manifest.backup_id)]["last_drill_status"] == "passed"
    )

    # Sweep helper deletes leftover drill namespaces cleanly.
    mongo["nce_drill_stale"].episodes  # touch stale db
    minio.make_bucket("nce-drill-stale-artifacts")
    swept = await sweep_stale_drill_targets(
        mongo_client=mongo, minio_client=minio, backup_dir=tmp_path
    )
    assert swept["removed_mongo"] == 1
    assert swept["removed_minio"] == 1

    # Status inspection smoke test.
    status_info = await get_backup_status(pg_pool=pg_pool, backup_dir=tmp_path)
    assert status_info["latest_manifest"]["backup_id"] == str(manifest.backup_id)
    assert status_info["latest_manifest"]["last_drill_status"] == "passed"
    assert status_info["active_lease"] is False


def test_ops_cli_status_and_missing_restore(tmp_path: Path) -> None:
    with patch("nce.ops.backup.resolve_ops_dsn", return_value=""):
        rc_status = ops_cli_main(["status", "--backup-dir", str(tmp_path), "--json"])
        assert rc_status == 0
        rc_bad_restore = ops_cli_main(["restore", str(tmp_path / "missing.tar.gz")])
        assert rc_bad_restore == 2
