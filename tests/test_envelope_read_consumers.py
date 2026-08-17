"""Integration acceptance test for Batch 109 — envelope-read-residual.

Verifies that with ``NCE_ENVELOPE_ENCRYPTION_ENABLED=true``:

* ``ReembeddingWorker.run_once`` correctly decrypts ``episodes.raw_data`` for a
  normal encrypted memory and embeds the plaintext.
* A memory whose ``wrapped_dek`` has been zeroed (provable forgetting) is
  SKIPPED (not raised), flagged ``metadata.dek_unreadable=true``, and
  ``nce_envelope_decrypt_failures_total`` increments.

Scope audit results:
  - ``contradictions.py``: ZERO raw_data reads — EXCLUDED.
  - ``consolidation.py``: only WRITES its abstraction raw_data — EXCLUDED.
  - ``re_embedder.py``: reads ``episodes.raw_data`` RAW — INCLUDED (patched here).
  - ``reembedding_worker.py``: reads both ``episodes.raw_data`` and
    ``code_files.raw_code`` RAW — primary test target.

Requires live MongoDB + PostgreSQL + Redis on the isolated test stack
(ports 5433/27018/6380); run with ``-m integration``.

``embed_batch`` is mocked so the test does not require a real embedding
service (there is no cognitive/embeddings sidecar in the isolated stack).
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

_FAKE_VECTOR: list[float] = [0.0] * 768


# ---------------------------------------------------------------------------
# Container reachability guards
# ---------------------------------------------------------------------------


def _reachable(host: str, port: int) -> bool:
    try:
        s = socket.create_connection((host, port), timeout=1)
        s.close()
        return True
    except OSError:
        return False


_INTEGRATION_PG_PORT = 5433
_INTEGRATION_MONGO_PORT = 27018
_INTEGRATION_REDIS_PORT = 6380

_CONTAINERS_OK = (
    _reachable("127.0.0.1", _INTEGRATION_PG_PORT)
    and _reachable("127.0.0.1", _INTEGRATION_MONGO_PORT)
    and _reachable("127.0.0.1", _INTEGRATION_REDIS_PORT)
)

_skip_no_containers = pytest.mark.skipif(
    not _CONTAINERS_OK,
    reason=("Integration test requires isolated stack on PG:5433, Mongo:27018, Redis:6380"),
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool_isolated():
    """asyncpg pool connected to the isolated integration database."""
    import asyncpg  # type: ignore[import-untyped]

    dsn = os.environ.get(
        "NCE_INTEGRATION_PG_DSN",
        "postgresql://mcp_user:mcp_password@127.0.0.1:5433/memory_meta",
    )
    try:
        pool = await asyncpg.create_pool(
            dsn,
            min_size=1,
            max_size=4,
            command_timeout=60,
        )
    except Exception as exc:
        pytest.skip(f"Cannot connect to isolated PG: {exc}")
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def mongo_isolated():
    """Motor client connected to the isolated MongoDB."""
    try:
        from motor.motor_asyncio import AsyncIOMotorClient  # type: ignore[import-untyped]
    except ImportError:
        pytest.skip("motor not installed")

    mongo_uri = os.environ.get("MONGO_URI", "mongodb://127.0.0.1:27018")
    client = AsyncIOMotorClient(mongo_uri, serverSelectionTimeoutMS=3_000)
    yield client
    client.close()


@pytest_asyncio.fixture
async def ns_id(pg_pool_isolated) -> uuid.UUID:
    """Create a fresh namespace for each test."""
    slug = f"pytest-b109-{uuid.uuid4().hex}"
    async with pg_pool_isolated.acquire() as conn:
        ns = await conn.fetchval("INSERT INTO namespaces (slug) VALUES ($1) RETURNING id", slug)
    assert ns is not None
    return ns


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _ensure_signing_key(pool: Any) -> None:
    """Ensure an active signing key exists so store operations don't fail."""
    from nce.signing import (
        NoActiveSigningKeyError,
        SigningKeyDecryptionError,
        get_active_key,
        rotate_key,
    )

    async with pool.acquire() as conn:
        try:
            await get_active_key(conn)
        except NoActiveSigningKeyError:
            await rotate_key(conn)
        except SigningKeyDecryptionError:
            await rotate_key(conn)


async def _store_encrypted_memory(
    pool: Any,
    mongo_client: Any,
    ns_id: uuid.UUID,
    plaintext: str,
) -> tuple[uuid.UUID, str]:
    """
    Insert a memory with envelope encryption ON.

    Returns ``(memory_id, payload_ref)``.
    The memory gets a dummy halfvec(768) embedding so
    ``_fetch_memories_batch`` (``embedding IS NOT NULL``) picks it up.
    """
    from nce.config import cfg
    from nce.envelope import encrypt_raw_data

    # Encrypt the payload.
    ciphertext, wrapped_dek, dek_key_id = encrypt_raw_data(plaintext)

    # Write to Mongo.
    db = mongo_client[cfg.MONGO_DATABASE if hasattr(cfg, "MONGO_DATABASE") else "memory_archive"]
    result = await db.episodes.insert_one(
        {
            "raw_data": ciphertext,
            "namespace_id": str(ns_id),
            "source": "test_b109",
        }
    )
    payload_ref = str(result.inserted_id)

    # Write to PG — include a non-null embedding so the re-embedder picks it up,
    # and set embedding_model_id to NULL so it's treated as "stale".
    zero_vec = json.dumps([0.0] * 768)
    async with pool.acquire() as conn:
        mem_id = await conn.fetchval(
            """
            INSERT INTO memories (
                namespace_id, agent_id, memory_type, payload_ref,
                embedding, embedding_model_id,
                wrapped_dek, dek_key_id
            ) VALUES (
                $1, 'test-b109', 'episodic', $2,
                $3::halfvec, NULL,
                $4, $5
            ) RETURNING id
            """,
            ns_id,
            payload_ref,
            zero_vec,
            wrapped_dek,
            dek_key_id,
        )
    return mem_id, payload_ref


async def _store_forgiven_memory(
    pool: Any,
    mongo_client: Any,
    ns_id: uuid.UUID,
    plaintext: str,
) -> tuple[uuid.UUID, str]:
    """
    Insert a memory whose DEK has been zeroed (provable forgetting).

    ``wrapped_dek`` is set to ``b'\\x00' * 1`` (zeroed / destroyed) so
    ``maybe_decrypt_raw_data`` raises ``SigningKeyDecryptionError`` — the
    DEK cannot be unwrapped.  This simulates what ``shred_memory`` does.
    """
    from nce.config import cfg
    from nce.envelope import encrypt_raw_data

    # Encrypt normally first, then we'll overwrite wrapped_dek with zeros.
    ciphertext, _real_wrapped, dek_key_id = encrypt_raw_data(plaintext)

    db = mongo_client[cfg.MONGO_DATABASE if hasattr(cfg, "MONGO_DATABASE") else "memory_archive"]
    result = await db.episodes.insert_one(
        {
            "raw_data": ciphertext,
            "namespace_id": str(ns_id),
            "source": "test_b109_forgiven",
        }
    )
    payload_ref = str(result.inserted_id)

    # Store with a zeroed wrapped_dek — unwrap will fail.
    zeroed_dek = b"\x00" * 32
    zero_vec = json.dumps([0.0] * 768)
    async with pool.acquire() as conn:
        mem_id = await conn.fetchval(
            """
            INSERT INTO memories (
                namespace_id, agent_id, memory_type, payload_ref,
                embedding, embedding_model_id,
                wrapped_dek, dek_key_id
            ) VALUES (
                $1, 'test-b109', 'episodic', $2,
                $3::halfvec, NULL,
                $4, $5
            ) RETURNING id
            """,
            ns_id,
            payload_ref,
            zero_vec,
            zeroed_dek,
            dek_key_id,
        )
    return mem_id, payload_ref


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@_skip_no_containers
@pytest.mark.integration
@pytest.mark.asyncio
async def test_reembedding_worker_decrypts_and_skips_forgiven(
    pg_pool_isolated, mongo_isolated, ns_id, monkeypatch
):
    """
    With NCE_ENVELOPE_ENCRYPTION_ENABLED=true:
    - A normal encrypted memory is decrypted and its plaintext is passed to
      embed_batch (the worker proceeds normally).
    - A memory with a zeroed wrapped_dek is SKIPPED (not raised), its
      metadata.dek_unreadable is set to true, and
      nce_envelope_decrypt_failures_total increments.
    """
    from nce.config import cfg
    from nce.reembedding_worker import ReembeddingWorker

    monkeypatch.setattr(cfg, "NCE_ENVELOPE_ENCRYPTION_ENABLED", True, raising=False)

    await _ensure_signing_key(pg_pool_isolated)

    # --- Store two memories ---
    secret = "BATCH109-SECRET-" + uuid.uuid4().hex
    forgiven_text = "BATCH109-FORGIVEN-" + uuid.uuid4().hex

    good_mem_id, good_ref = await _store_encrypted_memory(
        pg_pool_isolated, mongo_isolated, ns_id, secret
    )
    bad_mem_id, bad_ref = await _store_forgiven_memory(
        pg_pool_isolated, mongo_isolated, ns_id, forgiven_text
    )

    # --- Capture metric before run ---
    # Read initial value by inspecting the underlying prometheus counter if
    # available, otherwise just note it doesn't raise.
    try:
        from prometheus_client import REGISTRY

        def _get_count(consumer: str) -> float:
            try:
                return (
                    REGISTRY.get_sample_value(
                        "nce_envelope_decrypt_failures_total",
                        {"consumer": consumer},
                    )
                    or 0.0
                )
            except Exception:
                return 0.0

        before = _get_count("reembedding_worker")
    except ImportError:
        before = None

    # --- Run the worker with a mocked embedder ---
    embedded_texts: list[list[str]] = []

    async def _mock_embed(texts: list[str]) -> list[list[float]]:
        embedded_texts.append(list(texts))
        return [[0.1] * 768 for _ in texts]

    with patch("nce.reembedding_worker._embeddings") as mock_emb:
        mock_emb.embed_batch = AsyncMock(side_effect=_mock_embed)
        worker = ReembeddingWorker(
            batch_size=50,
            batches_per_minute=600,  # fast for tests
            max_rows_per_run=0,
            include_kg_nodes=False,
        )
        result = await worker.run_once(pg_pool_isolated, mongo_isolated)

    assert result["status"] == "completed", f"Worker failed: {result}"

    # --- Assert the good memory's text was embedded (plaintext, not ciphertext) ---
    all_embedded = [t for batch in embedded_texts for t in batch]
    # The good memory must appear as decrypted plaintext.
    assert any(secret in t for t in all_embedded), (
        f"Expected decrypted plaintext '{secret}' in embedded texts, got: {all_embedded!r}"
    )
    # The forgiven text must NOT appear (it was skipped).
    assert all(forgiven_text not in t for t in all_embedded), (
        f"Forgiven plaintext '{forgiven_text}' should not have been embedded, got: {all_embedded!r}"
    )

    # --- Assert the forgiven memory is flagged dek_unreadable=true ---
    async with pg_pool_isolated.acquire() as conn:
        meta = await conn.fetchval(
            "SELECT metadata FROM memories WHERE id = $1 AND namespace_id = $2",
            bad_mem_id,
            ns_id,
        )
    assert meta is not None, "memories row for forgiven memory not found"
    meta_dict = meta if isinstance(meta, dict) else json.loads(meta)
    assert meta_dict.get("dek_unreadable") is True, (
        f"Expected metadata.dek_unreadable=true for forgiven memory, got: {meta_dict!r}"
    )

    # --- Assert the metric incremented ---
    if before is not None:
        after = _get_count("reembedding_worker")
        assert after > before, (
            f"nce_envelope_decrypt_failures_total{{consumer=reembedding_worker}} "
            f"did not increment: before={before} after={after}"
        )


@_skip_no_containers
@pytest.mark.integration
@pytest.mark.asyncio
async def test_decrypt_failure_does_not_abort_batch(
    pg_pool_isolated, mongo_isolated, ns_id, monkeypatch
):
    """
    A zeroed wrapped_dek must NOT raise out of the worker.

    The batch continues: the forgiven memory is skipped and flagged, and the
    worker returns status='completed' (not 'failed').
    """
    from nce.config import cfg
    from nce.reembedding_worker import ReembeddingWorker

    monkeypatch.setattr(cfg, "NCE_ENVELOPE_ENCRYPTION_ENABLED", True, raising=False)

    await _ensure_signing_key(pg_pool_isolated)

    # Store ONLY the forgiven memory — the worker must still complete cleanly.
    forgiven_text = "BATCH109-ABORT-" + uuid.uuid4().hex
    _bad_mem_id, _bad_ref = await _store_forgiven_memory(
        pg_pool_isolated, mongo_isolated, ns_id, forgiven_text
    )

    with patch("nce.reembedding_worker._embeddings") as mock_emb:
        mock_emb.embed_batch = AsyncMock(return_value=[])
        worker = ReembeddingWorker(
            batch_size=50,
            batches_per_minute=600,
            max_rows_per_run=0,
            include_kg_nodes=False,
        )
        # Must not raise.
        result = await worker.run_once(pg_pool_isolated, mongo_isolated)

    assert result["status"] == "completed", (
        f"Worker must not fail on decrypt failure; got: {result}"
    )
    assert result["memories_done"] == 0, (
        f"Forgiven memory must not count as 'done'; got memories_done={result['memories_done']}"
    )
