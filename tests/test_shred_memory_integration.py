"""Integration acceptance test for Batch 47 — Provable Forgetting capstone (II.4c).

This is the plan's *headline completeness test* (NCE_MASTER_PLAN II.4 verification):
after ``shred_memory`` runs, assert that **NO plaintext fragment of the content
survives in ANY store**:

* Mongo ``episodes.raw_data`` ciphertext is undecryptable (the wrapped DEK was
  destroyed) — and the sentinel plaintext is absent from the doc entirely.
* ``memories.content_fts`` is empty and ``memories.embedding`` is NULL.
* ``memory_embeddings`` rows for the memory are gone.
* ``kg_nodes`` / ``kg_edges`` labels derived from the content are deleted
  (ATMS cascade — KG labels are plaintext content).
* ``pii_redactions`` rows are deleted.
* the Redis working-memory cache key is purged.
* ``event_log`` holds a signed ``memory_shredded`` event carrying only
  refs/counts/hashes (no content), and the returned deletion receipt verifies.

Requires live MongoDB + PostgreSQL + Redis (``-m integration``).
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from urllib.parse import urlparse

import pytest
import pytest_asyncio
from bson import ObjectId

from nce import MemoryPayload, NCEEngine
from nce.config import cfg
from nce.db_utils import scoped_pg_session
from nce.envelope import _DEK_PAYLOAD_PREFIX, DEKDecryptionError, decrypt_with_dek


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
async def active_embedding_model(engine):
    """Ensure at least one ACTIVE embedding model exists.

    ``_store_semantic_graph_pg`` only writes ``memory_embeddings`` (and
    ``kg_node_embeddings``) rows for models with status in (active, migrating).
    The shred completeness test asserts ``emb_before > 0`` and then that those
    rows are gone post-shred, so a model row is a precondition.  Idempotent:
    inserts the configured model only if no active/migrating model is present,
    so it is a no-op on an already-seeded DB.
    """
    from nce import embeddings as _emb

    async with engine.pg_pool.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT count(*) FROM embedding_models WHERE status IN ('active', 'migrating')"
        )
        if not existing:
            await conn.execute(
                "INSERT INTO embedding_models (name, dimension, status) "
                "VALUES ($1, $2, 'active') ON CONFLICT (name) DO UPDATE SET status = 'active'",
                _emb.MODEL_ID,
                _emb.VECTOR_DIM,
            )
    yield


@pytest_asyncio.fixture
async def namespace_id(engine, active_embedding_model) -> uuid.UUID:
    # Reversible pseudonymisation so the store path writes pii_redactions vault
    # rows (only the reversible-pseudonymise path populates vault_entries) which
    # we then assert are deleted on shred.  Fields must match NamespacePIIConfig
    # exactly (extra="forbid"): entity_types/policy/reversible — NOT
    # enabled/default_policy.  ``entity_types`` is REQUIRED: the scanner returns
    # no entities (and thus no vault row) when it is empty.  "EMAIL" is the entity
    # type recognised by the regex fallback used when Presidio is absent.
    slug = f"pytest-shred-{uuid.uuid4().hex}"
    meta = {
        "pii": {
            "entity_types": ["EMAIL"],
            "policy": "pseudonymise",
            "reversible": True,
        }
    }
    async with engine.pg_pool.acquire() as conn:
        ns = await conn.fetchval(
            "INSERT INTO namespaces (slug, metadata) VALUES ($1, $2::jsonb) RETURNING id",
            slug,
            json.dumps(meta),
        )
    assert ns is not None
    return ns


@_skip_no_containers
@pytest.mark.integration
@pytest.mark.asyncio
async def test_shred_leaves_no_plaintext_in_any_store(engine, namespace_id, monkeypatch):
    """The completeness test: after shred, no plaintext fragment survives anywhere."""
    monkeypatch.setattr(cfg, "NCE_ENVELOPE_ENCRYPTION_ENABLED", True, raising=False)

    sentinel = "SHRED-SENTINEL-" + uuid.uuid4().hex
    # Email guarantees a regex-backed PII vault row; the proper-noun phrase
    # seeds KG node/edge labels (plaintext content in the graph).
    email = f"victim-{uuid.uuid4().hex[:8]}@example.com"
    content = (
        f"{sentinel}. Alice Johnson works at Globex Corporation in Berlin. Contact her at {email}."
    )
    sid = str(uuid.uuid4())
    payload = MemoryPayload(
        namespace_id=namespace_id,
        agent_id="shred-agent",
        content=content,
        summary=content,
        heavy_payload=content,
        metadata={"user_id": sid, "session_id": sid},
    )

    res = await engine.store_memory(payload)
    payload_ref = res["payload_ref"]
    assert payload_ref

    # Resolve the memory id and confirm pre-shred artifacts exist.
    async with scoped_pg_session(engine.pg_pool, str(namespace_id)) as conn:
        mem = await conn.fetchrow(
            "SELECT id, wrapped_dek, dek_key_id FROM memories WHERE payload_ref = $1",
            payload_ref,
        )
        assert mem is not None
        assert mem["wrapped_dek"] is not None, "precondition: memory should be encrypted"
        memory_id = str(mem["id"])

        kg_nodes_before = await conn.fetchval(
            "SELECT count(*) FROM kg_nodes WHERE payload_ref = $1", payload_ref
        )
        emb_before = await conn.fetchval(
            "SELECT count(*) FROM memory_embeddings WHERE memory_id = $1::uuid", memory_id
        )
        pii_before = await conn.fetchval(
            "SELECT count(*) FROM pii_redactions WHERE memory_id = $1::uuid", memory_id
        )
    assert emb_before > 0, "precondition: memory_embeddings should exist"
    assert pii_before > 0, "precondition: a pii_redactions row should exist (email redacted)"

    # Capture the ciphertext + wrapped DEK BEFORE the shred so we can prove the
    # ciphertext is undecryptable afterwards (the DEK that decrypts it is gone).
    db = engine.mongo_client.memory_archive
    doc_before = await db.episodes.find_one({"_id": ObjectId(payload_ref)})
    raw_before = doc_before["raw_data"]
    ciphertext_before = bytes(raw_before)
    assert ciphertext_before.startswith(_DEK_PAYLOAD_PREFIX)

    # Prime the Redis cache key so we can assert it is purged.
    recalled = await engine.recall_recent(
        str(namespace_id), agent_id="shred-agent", limit=1, user_id=sid, session_id=sid
    )
    assert recalled, "precondition: recall should hydrate + cache the summary"
    redis_key = f"cache:{namespace_id}:{sid}:{sid}"
    assert await engine.redis_client.get(redis_key) is not None

    # ── SHRED ─────────────────────────────────────────────────────────────────
    out = await engine.shred_memory(memory_id, str(namespace_id), "shred-agent")
    assert out["status"] == "success"
    receipt = out["receipt"]

    # The receipt verifies (the signed WORM event was self-verified).
    assert receipt["verified"] is True
    assert receipt["dek_destroyed"] is True
    assert receipt["worm_event"]["event_type"] == "memory_shredded"

    # 1. Mongo: the wrapped DEK is gone from PG → ciphertext is undecryptable.
    async with scoped_pg_session(engine.pg_pool, str(namespace_id)) as conn:
        post = await conn.fetchrow(
            "SELECT wrapped_dek, dek_key_id, content_fts, embedding "
            "FROM memories WHERE id = $1::uuid",
            memory_id,
        )
    assert post["wrapped_dek"] is None, "DEK not destroyed"
    assert post["dek_key_id"] is None, "dek_key_id not cleared"

    # 2. content_fts empty + embedding NULL (reversible plaintext derivatives).
    assert post["content_fts"] is None, "content_fts survived the shred"
    assert post["embedding"] is None, "embedding survived the shred"

    # The DEK is unrecoverable: prove the captured ciphertext cannot be decrypted
    # with any wrong key (the real one no longer exists anywhere).
    with pytest.raises(DEKDecryptionError):
        decrypt_with_dek(ciphertext_before, b"\x00" * 32)

    # And the Mongo doc itself no longer carries the plaintext (tombstoned).
    doc_after = await db.episodes.find_one({"_id": ObjectId(payload_ref)})
    assert doc_after is not None
    raw_after = doc_after.get("raw_data")
    raw_after_bytes = (
        bytes(raw_after) if isinstance(raw_after, (bytes, bytearray, memoryview)) else b""
    )
    assert sentinel.encode() not in raw_after_bytes
    assert email.encode() not in raw_after_bytes
    assert sentinel not in json.dumps(doc_after, default=str)

    # 3. memory_embeddings, kg_nodes, kg_edges, pii_redactions are gone.
    async with scoped_pg_session(engine.pg_pool, str(namespace_id)) as conn:
        emb_after = await conn.fetchval(
            "SELECT count(*) FROM memory_embeddings WHERE memory_id = $1::uuid", memory_id
        )
        kg_nodes_after = await conn.fetchval(
            "SELECT count(*) FROM kg_nodes WHERE payload_ref = $1", payload_ref
        )
        kg_edges_after = await conn.fetchval(
            "SELECT count(*) FROM kg_edges WHERE payload_ref = $1", payload_ref
        )
        pii_after = await conn.fetchval(
            "SELECT count(*) FROM pii_redactions WHERE memory_id = $1::uuid", memory_id
        )
    assert emb_after == 0, "memory_embeddings survived"
    assert kg_nodes_after == 0, "kg_nodes (plaintext labels) survived"
    assert kg_edges_after == 0, "kg_edges (plaintext triplets) survived"
    assert pii_after == 0, "pii_redactions survived"
    # If KG nodes existed pre-shred, the ATMS cascade should have removed them.
    if kg_nodes_before:
        assert kg_nodes_after == 0

    # 4. Redis working-memory key purged.
    assert await engine.redis_client.get(redis_key) is None, "Redis cache key survived"
    assert receipt["redis_keys_purged"] >= 1

    # 5. event_log holds a signed, CONTENT-FREE memory_shredded event.
    async with scoped_pg_session(engine.pg_pool, str(namespace_id)) as conn:
        ev = await conn.fetchrow(
            """
            SELECT params, signature, signature_key_id
            FROM event_log
            WHERE namespace_id = $1::uuid AND event_type = 'memory_shredded'
              AND params->>'memory_id' = $2
            ORDER BY occurred_at DESC LIMIT 1
            """,
            namespace_id,
            memory_id,
        )
    assert ev is not None, "memory_shredded event not appended"
    assert ev["signature"] is not None and ev["signature_key_id"], "event not signed"
    params = ev["params"]
    if isinstance(params, str):
        params = json.loads(params)
    # The immutable event must carry refs/counts/hashes ONLY — never content.
    blob = json.dumps(params)
    assert sentinel not in blob, "content sentinel leaked into WORM event"
    assert email not in blob, "PII leaked into WORM event"
    assert "Globex" not in blob and "Alice" not in blob, "entity strings leaked into WORM event"
    assert params["memory_id"] == memory_id
    assert "receipt_digest" in params and len(params["receipt_digest"]) == 64
    # No content-bearing keys present at all.
    for forbidden in ("raw_data", "content", "summary", "heavy_payload", "entities", "triplets"):
        assert forbidden not in params, f"forbidden content key {forbidden!r} in WORM event"
