"""Batch 44 — close the raw-PII *side sinks* in the write paths.

Integration tests (require the live Postgres stack) asserting **real DB state**:

1. ``saga_execution_log.payload`` must not persist pre-redaction content
   (the raw ``summary``) after a ``store_memory`` saga-log start.
2. The ``me_app`` edit path must pseudonymize/redact caller-supplied
   entity/triplet metadata before it lands in the immutable ``event_log``.
3. Regression: the **main** ``store_memory`` event must still carry
   ``entities``/``triplets`` in ``event_log.params`` — the time-travel
   GraphRAG precondition, which this batch must NOT break.

These exercise the real code paths against a real database (no mocked PG),
deliberately avoiding the Trivial Test Trap.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

from nce.db_utils import scoped_pg_session
from nce.event_log import append_event
from nce.me_app import _pseudonymize_edit_graph
from nce.models import (
    AssertionType,
    MemoryType,
    NamespacePIIConfig,
    StoreMemoryRequest,
)
from nce.orchestrators.memory import MemoryOrchestrator

# Ensure NCE_MASTER_KEY is populated for the config loader / signing.
os.environ.setdefault("NCE_MASTER_KEY", "x" * 32)

# A raw-PII fragment that must NEVER appear verbatim in any immutable/WORM sink.
_RAW_PII = "john.secret@example.com"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_saga_log_holds_no_raw_pii(pg_pool, make_namespace) -> None:
    """Assertion 1: saga_execution_log.payload stores no raw summary/PII."""
    ns_id = await make_namespace()
    orch = MemoryOrchestrator(pg_pool=pg_pool, mongo_client=None, redis_client=None)

    payload = StoreMemoryRequest(
        namespace_id=ns_id,
        agent_id="batch44-agent",
        content=f"Please contact {_RAW_PII} regarding the invoice.",
        summary=f"Please contact {_RAW_PII} regarding the invoice.",
        heavy_payload="irrelevant",
        memory_type=MemoryType.episodic,
        assertion_type=AssertionType.fact,
        metadata={"user_id": "u-1", "secret_note": _RAW_PII},
    )

    saga_id = await orch._saga_log_start("store_memory", payload)

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        row = await conn.fetchrow(
            "SELECT payload FROM saga_execution_log WHERE id = $1::uuid", saga_id
        )
    assert row is not None
    stored = row["payload"]
    if isinstance(stored, str):
        stored = json.loads(stored)
    blob = json.dumps(stored)

    # The raw PII and the raw summary text must not be persisted.
    assert _RAW_PII not in blob
    assert "summary" not in stored
    assert "metadata" not in stored
    # Recovery references are still present.
    assert stored["memory_type"] == MemoryType.episodic.value
    assert stored["assertion_type"] == AssertionType.fact.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_edit_path_pii_pseudonymized_in_event_log(pg_pool, make_namespace) -> None:
    """Assertion 2: me_app edit path scrubs caller-supplied graph metadata.

    Exercises the real ``_pseudonymize_edit_graph`` helper + real ``append_event``
    + real ``event_log`` row, then asserts no raw PII survives in params.
    """
    ns_id = await make_namespace()

    # Configure the namespace to redact EMAIL so the helper has something to scrub.
    cfg = NamespacePIIConfig(entity_types=["EMAIL"], namespace_id=str(ns_id))
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        await conn.execute(
            "UPDATE namespaces SET metadata = $2::jsonb WHERE id = $1",
            ns_id,
            json.dumps({"pii": cfg.model_dump(mode="json")}),
        )

    # Caller-supplied (UNTRUSTED) entities/triplets carrying raw PII in labels.
    raw_entities = [{"label": _RAW_PII, "entity_type": "EMAIL"}]
    raw_triplets = [
        {
            "subject_label": _RAW_PII,
            "predicate": "emailed",
            "object_label": "acme corp",
            "confidence": 0.9,
        }
    ]

    memory_id = uuid.uuid4()
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        safe_entities, safe_triplets = await _pseudonymize_edit_graph(
            conn, ns_id, raw_entities, raw_triplets
        )
        async with conn.transaction():
            await append_event(
                conn=conn,
                namespace_id=ns_id,
                agent_id="batch44-agent",
                event_type="store_memory",
                params={
                    "saga_id": str(uuid.uuid4()),
                    "memory_id": str(memory_id),
                    "payload_ref": "0" * 24,
                    "assertion_type": "fact",
                    "entities": safe_entities,
                    "triplets": safe_triplets,
                    "action": "edit",
                },
                result_summary={"status": "success", "edited": True},
            )

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        row = await conn.fetchrow(
            "SELECT params FROM event_log WHERE namespace_id = $1 "
            "AND params->>'memory_id' = $2 AND params->>'action' = 'edit'",
            ns_id,
            str(memory_id),
        )
    assert row is not None
    params = row["params"]
    if isinstance(params, str):
        params = json.loads(params)
    blob = json.dumps(params)

    # No raw PII survives in the immutable event_log.
    assert _RAW_PII not in blob
    # The label was actually scrubbed to the redaction token.
    assert params["entities"][0]["label"] == "<EMAIL>"
    assert params["triplets"][0]["subject_label"] == "<EMAIL>"
    # Non-PII fields are preserved.
    assert params["triplets"][0]["predicate"] == "emailed"
    assert params["triplets"][0]["object_label"] == "acme corp"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_main_store_memory_event_still_carries_graph(pg_pool, make_namespace) -> None:
    """Assertion 3 (regression): main-path event_log.params keeps entities/triplets.

    Time-travel GraphRAG reads entities/triplets from event_log.params; this batch
    must NOT make the main log content-free. We assert the main store_memory event
    shape is unchanged (entities + triplets present), mirroring memory.py:325-338.
    """
    ns_id = await make_namespace()

    entities = [{"label": "redis", "entity_type": "TECH"}]
    triplets = [
        {
            "subject_label": "service",
            "predicate": "uses",
            "object_label": "redis",
            "confidence": 0.8,
        }
    ]

    memory_id = uuid.uuid4()
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        async with conn.transaction():
            await append_event(
                conn=conn,
                namespace_id=ns_id,
                agent_id="batch44-agent",
                event_type="store_memory",
                params={
                    "saga_id": str(uuid.uuid4()),
                    "memory_id": str(memory_id),
                    "payload_ref": "1" * 24,
                    "assertion_type": "fact",
                    "entities": entities,
                    "triplets": triplets,
                },
            )

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        row = await conn.fetchrow(
            "SELECT params FROM event_log WHERE namespace_id = $1 "
            "AND params->>'memory_id' = $2 AND event_type = 'store_memory'",
            ns_id,
            str(memory_id),
        )
    assert row is not None
    params = row["params"]
    if isinstance(params, str):
        params = json.loads(params)

    # Time-travel precondition: graph payload is still present in the WORM log.
    assert params["entities"] == entities
    assert params["triplets"] == triplets
