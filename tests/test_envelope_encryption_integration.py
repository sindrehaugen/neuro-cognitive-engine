"""Integration acceptance test for Batch 46 — Provable Forgetting (Part II.4).

Asserts the end-to-end envelope-encryption contract:

* With ``NCE_ENVELOPE_ENCRYPTION_ENABLED`` on, ``store_memory`` writes the raw
  payload to Mongo ``episodes.raw_data`` as **ciphertext** (the plaintext does
  NOT appear at rest) and sets ``memories.wrapped_dek`` + ``dek_key_id``.
* Read paths (``recall_recent`` and ``verify_memory``) transparently decrypt and
  return the correct plaintext content.
* A legacy row written with encryption OFF (``wrapped_dek IS NULL``, plaintext
  ``raw_data``) still reads back as plaintext — back-compat holds.

Requires live MongoDB + PostgreSQL + Redis (``-m integration``).
"""

from __future__ import annotations

import os
import socket
import uuid
from urllib.parse import urlparse

import pytest
import pytest_asyncio

from nce import MemoryPayload, NCEEngine
from nce.config import cfg
from nce.db_utils import scoped_pg_session


def _reachable(env_var: str, host: str, port: int) -> bool:
    url = os.getenv(env_var)
    if url:
        try:
            if "://" in url:
                parsed = urlparse(url)
                host = parsed.hostname or host
                port = parsed.port or port
            else:
                parts = url.split(":")
                host = parts[0]
                if len(parts) > 1:
                    port = int(parts[1].split("/")[0])
        except Exception:
            pass
    try:
        sock = socket.create_connection((host, port), timeout=1)
        sock.close()
        return True
    except OSError:
        return False


_CONTAINERS_OK = (
    _reachable("MONGO_URI", "127.0.0.1", 27017)
    and _reachable("PG_DSN", "127.0.0.1", 5432)
    and _reachable("REDIS_URL", "127.0.0.1", 6379)
)

_skip_no_containers = pytest.mark.skipif(
    not _CONTAINERS_OK,
    reason="Integration test requires live MongoDB, PostgreSQL, and Redis containers",
)


@pytest_asyncio.fixture
async def engine():
    eng = NCEEngine()
    await eng.connect()
    yield eng
    await eng.disconnect()


@pytest_asyncio.fixture
async def namespace_id(engine) -> uuid.UUID:
    slug = f"pytest-envelope-{uuid.uuid4().hex}"
    async with engine.pg_pool.acquire() as conn:
        ns = await conn.fetchval("INSERT INTO namespaces (slug) VALUES ($1) RETURNING id", slug)
    assert ns is not None
    return ns


@_skip_no_containers
@pytest.mark.integration
@pytest.mark.asyncio
async def test_raw_data_encrypted_at_rest_and_reads_decrypt(engine, namespace_id, monkeypatch):
    """Encryption ON: ciphertext at rest in Mongo, DEK on the row, reads decrypt."""
    from nce.envelope import _DEK_PAYLOAD_PREFIX

    monkeypatch.setattr(cfg, "NCE_ENVELOPE_ENCRYPTION_ENABLED", True, raising=False)

    secret = (
        "PROVABLE-FORGETTING-SENTINEL-"
        + uuid.uuid4().hex
        + " the quick brown fox jumps over the lazy dog"
    )
    test_id = str(uuid.uuid4())
    payload = MemoryPayload(
        namespace_id=namespace_id,
        agent_id="test-agent",
        content=secret,
        summary=secret,
        heavy_payload=secret,
        metadata={"user_id": test_id, "session_id": test_id},
    )

    res = await engine.store_memory(payload)
    payload_ref = res["payload_ref"]
    assert payload_ref

    # 1. Mongo episodes.raw_data is CIPHERTEXT — plaintext must NOT be at rest.
    from bson import ObjectId

    db = engine.mongo_client.memory_archive
    doc = await db.episodes.find_one({"_id": ObjectId(payload_ref)})
    assert doc is not None
    raw = doc["raw_data"]
    raw_bytes = bytes(raw) if isinstance(raw, (bytes, bytearray, memoryview)) else None
    assert raw_bytes is not None, f"raw_data is not bytes ciphertext: {type(raw)!r}"
    assert raw_bytes.startswith(_DEK_PAYLOAD_PREFIX), "raw_data missing DEK wire prefix"
    assert secret.encode("utf-8") not in raw_bytes, "plaintext leaked into Mongo ciphertext"

    # 2. memories.wrapped_dek + dek_key_id are set.
    async with scoped_pg_session(engine.pg_pool, str(namespace_id)) as conn:
        mem = await conn.fetchrow(
            "SELECT wrapped_dek, dek_key_id FROM memories WHERE payload_ref = $1",
            payload_ref,
        )
    assert mem is not None
    assert mem["wrapped_dek"] is not None, "wrapped_dek not persisted"
    assert mem["dek_key_id"], "dek_key_id not persisted"

    # 3a. recall_recent read path decrypts back to plaintext.
    recalled = await engine.recall_recent(
        str(namespace_id), agent_id="test-agent", limit=5, user_id=test_id, session_id=test_id
    )
    assert any(secret == r for r in recalled), f"recall_recent did not decrypt: {recalled!r}"

    # 3b. semantic_search read path also decrypts the hydrated raw_data.
    hits = await engine.semantic_search(
        query=secret,
        namespace_id=str(namespace_id),
        agent_id="test-agent",
        limit=5,
    )
    assert any((h.get("raw_data") or "") == secret for h in hits), (
        f"semantic_search did not decrypt raw_data: {[h.get('raw_data') for h in hits]!r}"
    )

    # 3c. verify_memory, when the memory is signed, hashes the DECRYPTED content
    # (stable across the plaintext→ciphertext rollout).  Unsigned memories return
    # payload_hash=None by design — only assert the hash when one is produced.
    import hashlib

    async with scoped_pg_session(engine.pg_pool, str(namespace_id)) as conn:
        memory_id = await conn.fetchval(
            "SELECT id FROM memories WHERE payload_ref = $1", payload_ref
        )
    verify = await engine.verify_memory(str(memory_id))
    expected_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    if verify.get("payload_hash") is not None:
        assert verify["payload_hash"] == expected_hash, (
            "verify_memory payload_hash is over ciphertext, not decrypted plaintext"
        )


@_skip_no_containers
@pytest.mark.integration
@pytest.mark.asyncio
async def test_legacy_null_wrapped_dek_reads_as_plaintext(engine, namespace_id, monkeypatch):
    """Back-compat: a row written with encryption OFF reads back as plaintext."""
    monkeypatch.setattr(cfg, "NCE_ENVELOPE_ENCRYPTION_ENABLED", False, raising=False)

    legacy = "LEGACY-PLAINTEXT-" + uuid.uuid4().hex
    test_id = str(uuid.uuid4())
    payload = MemoryPayload(
        namespace_id=namespace_id,
        agent_id="legacy-agent",
        content=legacy,
        summary=legacy,
        heavy_payload=legacy,
        metadata={"user_id": test_id, "session_id": test_id},
    )

    res = await engine.store_memory(payload)
    payload_ref = res["payload_ref"]

    # Stored as plaintext str with NULL wrapped_dek (the legacy shape).
    from bson import ObjectId

    db = engine.mongo_client.memory_archive
    doc = await db.episodes.find_one({"_id": ObjectId(payload_ref)})
    assert isinstance(doc["raw_data"], str)
    assert doc["raw_data"] == legacy

    async with scoped_pg_session(engine.pg_pool, str(namespace_id)) as conn:
        wrapped = await conn.fetchval(
            "SELECT wrapped_dek FROM memories WHERE payload_ref = $1", payload_ref
        )
    assert wrapped is None, "legacy write should not set wrapped_dek"

    # Read path returns the plaintext unchanged.
    recalled = await engine.recall_recent(
        str(namespace_id), agent_id="legacy-agent", limit=5, user_id=test_id, session_id=test_id
    )
    assert any(legacy == r for r in recalled), f"legacy plaintext not returned: {recalled!r}"
