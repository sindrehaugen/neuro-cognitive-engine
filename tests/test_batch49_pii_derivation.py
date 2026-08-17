"""Integration test for Batch 49 — Verify PII-before-derivation on every write path (VII.1)."""

from __future__ import annotations

import json
import os
import re
import socket
import uuid
from urllib.parse import urlparse

import pytest
import pytest_asyncio

from nce import NCEEngine
from nce.db_utils import scoped_pg_session
from nce.signing import decrypt_signing_key, require_master_key
from nce.tasks import process_code_indexing
from nce.vertical_modules.dynamics365.ingestion import DataverseIngestionWorker


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
    slug = f"pytest-pii-{uuid.uuid4().hex}"
    meta = {
        "pii": {
            "entity_types": ["EMAIL", "PHONE"],
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
async def test_code_indexing_pii_sanitization(engine, namespace_id, monkeypatch):
    """Verify code indexing sanitizes PII in FTS and embeddings, leaving raw only in pii_redactions."""
    master_key = os.environ.get("NCE_MASTER_KEY", "dev-trimcp-master-key-change-in-prod-32chars!!")
    monkeypatch.setenv("NCE_MASTER_KEY", master_key)

    raw_code = """
    def test_run():
        email = "tester-pii@example.com"
        pass
    """

    # Run the background indexing task synchronously
    result = process_code_indexing(
        filepath="src/test_pii.py",
        raw_code=raw_code,
        language="python",
        namespace_id=str(namespace_id),
    )

    assert result["status"] == "success"

    async with scoped_pg_session(engine.pg_pool, str(namespace_id)) as conn:
        memories = await conn.fetch(
            "SELECT id, content_fts::text AS content_fts_text, pii_redacted, payload_ref FROM memories WHERE filepath = 'src/test_pii.py' AND namespace_id = $1::uuid",
            namespace_id,
        )
        assert len(memories) > 0

        # Check FTS index and ensure PII is pseudonymized
        has_pii_redacted = False
        for mem in memories:
            fts_text = mem["content_fts_text"]
            assert "tester-pii@example.com" not in fts_text

            if mem["pii_redacted"]:
                has_pii_redacted = True
                memory_id = mem["id"]
                # Verify pii_redactions row
                redactions = await conn.fetch(
                    "SELECT token, encrypted_value FROM pii_redactions WHERE memory_id = $1::uuid",
                    memory_id,
                )
                assert len(redactions) == 1
                token = redactions[0]["token"]
                enc_value = redactions[0]["encrypted_value"]

                # Verify token matches base64url pattern
                assert re.match(r"<EMAIL_[A-Za-z0-9_-]{20,24}>", token)

                # Decrypt original value
                with require_master_key() as mk:
                    orig_val = decrypt_signing_key(enc_value, mk).decode("utf-8")
                assert orig_val == "tester-pii@example.com"

        assert has_pii_redacted, "At least one memory chunk should have been flagged as redacted"


@_skip_no_containers
@pytest.mark.integration
@pytest.mark.asyncio
async def test_d365_ingestion_pii_sanitization(engine, namespace_id, monkeypatch):
    """Verify Dynamics 365 ingestion paths sanitize PII across FTS, KG, event log and embeddings."""
    master_key = os.environ.get("NCE_MASTER_KEY", "dev-trimcp-master-key-change-in-prod-32chars!!")
    monkeypatch.setenv("NCE_MASTER_KEY", master_key)

    worker = DataverseIngestionWorker(
        engine.pg_pool, engine.mongo_client, engine.redis_client, namespace_id
    )

    # 1. Test ingest_case_note
    note_text = "Case comment from customer-pii@example.com regarding incident."
    res_note = await worker.ingest_case_note("inc-123", note_text)
    memory_id_note = uuid.UUID(res_note["memory_id"])

    async with scoped_pg_session(engine.pg_pool, str(namespace_id)) as conn:
        mem_note = await conn.fetchrow(
            "SELECT content_fts::text AS content_fts_text, pii_redacted FROM memories WHERE id = $1::uuid",
            memory_id_note,
        )
        assert mem_note is not None
        assert mem_note["pii_redacted"] is True
        assert "customer-pii@example.com" not in mem_note["content_fts_text"]

        redactions = await conn.fetch(
            "SELECT token, encrypted_value FROM pii_redactions WHERE memory_id = $1::uuid",
            memory_id_note,
        )
        assert len(redactions) == 1
        with require_master_key() as mk:
            orig = decrypt_signing_key(redactions[0]["encrypted_value"], mk).decode("utf-8")
        assert orig == "customer-pii@example.com"

    # 2. Test ingest_activity
    subject = "Call with contact-pii@example.com"
    body = "Discussed SLA terms and conditions."
    res_act = await worker.ingest_activity("email", subject, body, "inc-123")
    memory_id_act = uuid.UUID(res_act["memory_id"])

    async with scoped_pg_session(engine.pg_pool, str(namespace_id)) as conn:
        mem_act = await conn.fetchrow(
            "SELECT content_fts::text AS content_fts_text, pii_redacted FROM memories WHERE id = $1::uuid",
            memory_id_act,
        )
        assert mem_act is not None
        assert mem_act["pii_redacted"] is True
        assert "contact-pii@example.com" not in mem_act["content_fts_text"]

    # 3. Test ingest_sla_breach
    account_name = "Alice-Smith-pii@example.com"
    async with scoped_pg_session(engine.pg_pool, str(namespace_id)) as conn:
        async with conn.transaction():
            res_sla = await worker.ingest_sla_breach(
                conn=conn,
                incident_id="inc-999",
                breach_type="resolution",
                account_name=account_name,
            )

    memory_id_sla = uuid.UUID(res_sla["memory_id"])

    async with scoped_pg_session(engine.pg_pool, str(namespace_id)) as conn:
        # Check FTS
        mem_sla = await conn.fetchrow(
            "SELECT content_fts::text AS content_fts_text, pii_redacted FROM memories WHERE id = $1::uuid",
            memory_id_sla,
        )
        assert mem_sla is not None
        assert mem_sla["pii_redacted"] is True
        assert "Alice-Smith-pii@example.com" not in mem_sla["content_fts_text"]

        # Check event_log parameters
        log_row = await conn.fetchrow(
            "SELECT params FROM event_log WHERE event_type = 'd365_sla_breach' AND params->>'memory_id' = $1",
            str(memory_id_sla),
        )
        assert log_row is not None
        params = log_row["params"]
        if isinstance(params, str):
            params = json.loads(params)
        assert params["account_name"] != "Alice-Smith-pii@example.com"
        assert re.match(r"<EMAIL_[A-Za-z0-9_-]{20,24}>", params["account_name"])

        # Check kg_edges table
        edge_row = await conn.fetchrow(
            "SELECT object_label FROM kg_edges WHERE subject_label = 'SLABreach:inc-999:resolution' AND namespace_id = $1::uuid",
            namespace_id,
        )
        assert edge_row is not None
        assert edge_row["object_label"] != "Account:Alice-Smith-pii@example.com"
        assert "<EMAIL_" in edge_row["object_label"]
