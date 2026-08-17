import json
import uuid
from unittest.mock import MagicMock, patch

import pytest

from nce.config import cfg
from nce.db_utils import scoped_pg_session
from nce.event_log import append_event
from nce.replay import get_event_provenance
from nce.replay_mcp_handlers import handle_explain_memory


@pytest.mark.integration
@pytest.mark.asyncio
async def test_explain_memory_tamper_detection(pg_pool, make_namespace, monkeypatch) -> None:
    """
    Integration test for Batch 38 Epistemic Receipts:
    1. Store a memory via append_event.
    2. Check that get_event_provenance and explain_memory report verified=True.
    3. Tamper the event row (e.g. modify event parameters).
    4. Assert that verify_event_signature fails and verified is returned as False.
    """
    ns_id = await make_namespace()
    agent_id = "test-explain-agent"
    memory_id = uuid.uuid4()

    # Append one store_memory event
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        res = await append_event(
            conn=conn,
            namespace_id=ns_id,
            agent_id=agent_id,
            event_type="store_memory",
            params={
                "saga_id": str(uuid.uuid4()),
                "memory_id": str(memory_id),
                "payload_ref": "100000000000000000000001",
                "assertion_type": "fact",
                "entities": [],
                "triplets": [],
            },
        )

    # Call get_event_provenance
    provenance = await get_event_provenance(pg_pool, memory_id)
    assert len(provenance["chain"]) == 1
    assert provenance["chain"][0]["verified"] is True
    assert provenance["chain"][0]["signature"] != ""

    # Call handle_explain_memory
    mock_engine = MagicMock()
    mock_engine.pg_pool = pg_pool
    explain_raw = await handle_explain_memory(mock_engine, {"memory_id": str(memory_id)})
    explain_res = json.loads(explain_raw)
    assert explain_res["verified"] is True
    assert explain_res["event_seq"] == res.event_seq
    assert explain_res["agent_id"] == agent_id
    assert explain_res["signature"] == provenance["chain"][0]["signature"]

    # Tamper parameters of the event while keeping memory_id intact
    monkeypatch.setenv("NCE_BYPASS_WORM", "true")
    with patch.object(cfg, "NCE_BYPASS_WORM", True):
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            await conn.execute("ALTER TABLE event_log DISABLE TRIGGER trg_event_log_worm")
            try:
                await conn.execute(
                    "UPDATE event_log SET params = jsonb_set(params, '{payload_ref}', '\"tampered_ref\"') WHERE namespace_id = $1 AND event_seq = $2",
                    ns_id,
                    res.event_seq,
                )
            finally:
                await conn.execute("ALTER TABLE event_log ENABLE TRIGGER trg_event_log_worm")

    # Call get_event_provenance on tampered event
    provenance_tampered = await get_event_provenance(pg_pool, memory_id)
    assert len(provenance_tampered["chain"]) == 1
    assert provenance_tampered["chain"][0]["verified"] is False

    # Call handle_explain_memory on tampered event
    explain_tampered_raw = await handle_explain_memory(mock_engine, {"memory_id": str(memory_id)})
    explain_tampered_res = json.loads(explain_tampered_raw)
    assert explain_tampered_res["verified"] is False
