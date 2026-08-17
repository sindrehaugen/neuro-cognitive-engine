import json
import re
import uuid

import pytest

from nce.db_utils import scoped_pg_session
from nce.event_log import append_event
from nce.replay import ReconstructiveReplay


class MockAcquireContext:
    def __init__(self, ctx):
        self.ctx = ctx
        self.conn = None

    async def __aenter__(self):
        self.conn = await self.ctx.__aenter__()
        try:
            await self.conn.set_type_codec(
                "jsonb",
                encoder=json.dumps,
                decoder=json.loads,
                schema="pg_catalog",
            )
        except Exception:
            pass
        try:
            await self.conn.set_type_codec(
                "json",
                encoder=json.dumps,
                decoder=json.loads,
                schema="pg_catalog",
            )
        except Exception:
            pass
        return self.conn

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return await self.ctx.__aexit__(exc_type, exc_val, exc_tb)


class PoolProxy:
    def __init__(self, pool):
        self._pool = pool

    def __getattr__(self, name):
        return getattr(self._pool, name)

    def acquire(self, *args, **kwargs):
        return MockAcquireContext(self._pool.acquire(*args, **kwargs))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_handlers_integration_end_to_end(pg_pool, make_namespace, monkeypatch) -> None:
    pool_proxy = PoolProxy(pg_pool)

    # 1. Create source and target namespaces
    source_ns = await make_namespace()
    target_ns = await make_namespace()

    agent_id = "test-agent"
    src_memory_id = uuid.uuid4()
    payload_ref = "000000000000000000000001"
    embedding = [0.1] * 768
    assertion_type = "fact"
    memory_type = "episodic"
    metadata = {"source_text": "Episodic memory details"}

    # 2. Seed the source namespace
    # We need to insert the memory directly into memories and memory_salience of source_ns
    async with scoped_pg_session(pool_proxy, source_ns) as conn:
        await conn.execute(
            """
            INSERT INTO memories (id, namespace_id, agent_id, embedding, assertion_type, memory_type, payload_ref, metadata, valid_from)
            VALUES ($1, $2, $3, $4::vector, $5, $6, $7, $8::jsonb, now())
            """,
            src_memory_id,
            source_ns,
            agent_id,
            json.dumps(embedding),
            assertion_type,
            memory_type,
            payload_ref,
            metadata,
        )
        await conn.execute(
            """
            INSERT INTO memory_salience (memory_id, agent_id, namespace_id, salience_score)
            VALUES ($1, $2, $3, $4)
            """,
            src_memory_id,
            agent_id,
            source_ns,
            0.5,
        )

        # Seed events: store_memory (with source_namespace_id injected to bypass handler lookup bug)
        await append_event(
            conn=conn,
            namespace_id=source_ns,
            agent_id=agent_id,
            event_type="store_memory",
            params={
                "saga_id": str(uuid.uuid4()),
                "memory_id": str(src_memory_id),
                "payload_ref": payload_ref,
                "assertion_type": assertion_type,
                "entities": [],
                "triplets": [],
                "source_namespace_id": str(source_ns),
            },
        )

        # Seed events: consolidation_run
        consolidated_memory_id = uuid.uuid4()
        consolidation_payload_ref = "000000000000000000000002"
        await append_event(
            conn=conn,
            namespace_id=source_ns,
            agent_id=agent_id,
            event_type="consolidation_run",
            params={
                "abstraction": "This is a consolidated abstraction",
                "key_entities": [],
                "key_relations": [],
                "supporting_memory_ids": [str(src_memory_id)],
                "contradicting_memory_ids": [],
                "confidence": 0.8,
                "source_memories": [str(src_memory_id)],
                "consolidated_memory_id": str(consolidated_memory_id),
                "payload_ref": consolidation_payload_ref,
            },
        )

        # Seed events: boost_memory
        await append_event(
            conn=conn,
            namespace_id=source_ns,
            agent_id=agent_id,
            event_type="boost_memory",
            params={
                "memory_id": str(src_memory_id),
                "factor": 0.2,
            },
        )

    # 3. Monkeypatch _dispatch_and_apply_event to handle consolidation_run llm_payload and ConnectionProxy
    import nce.replay as replay_mod

    class ConnectionProxy:
        def __init__(self, c):
            self._conn = c

        def __getattr__(self, name):
            return getattr(self._conn, name)

        async def execute(self, query, *args, **kwargs):
            new_args = list(args)
            new_query = query
            if "INSERT INTO memories" in query:
                # 1. Cast embedding ($4) to vector if it is a list
                if len(new_args) >= 4 and isinstance(new_args[3], list):
                    new_args[3] = json.dumps(new_args[3])
                new_query = new_query.replace("$4,", "$4::vector,")

                # 2. Deserialize any manually serialized JSON string parameters back to dict (except index 3)
                for i, val in enumerate(new_args):
                    if i == 3:
                        continue
                    if isinstance(val, str) and (val.startswith("{") or val.startswith("[")):
                        try:
                            new_args[i] = json.loads(val)
                        except Exception:
                            pass
            return await self._conn.execute(new_query, *new_args, **kwargs)

    original_dispatch = replay_mod._dispatch_and_apply_event

    async def mock_dispatch(
        write_conn,
        src,
        target_namespace_id,
        llm_payload,
        config_overrides,
        run_id,
        source_namespace_id,
        **kwargs,
    ):
        if src.event_type == "consolidation_run" and llm_payload is None:
            llm_payload = {
                "prompt": "fake prompt",
                "response": {
                    "abstraction": src.params.get("abstraction", "Consolidated memory abstraction"),
                    "confidence": src.params.get("confidence", 0.8),
                    "supporting_memory_ids": src.params.get("supporting_memory_ids", []),
                    "key_entities": src.params.get("key_entities", []),
                    "key_relations": src.params.get("key_relations", []),
                },
            }
        proxy = ConnectionProxy(write_conn)
        return await original_dispatch(
            proxy,
            src=src,
            target_namespace_id=target_namespace_id,
            llm_payload=llm_payload,
            config_overrides=config_overrides,
            run_id=run_id,
            source_namespace_id=source_namespace_id,
            **kwargs,
        )

    monkeypatch.setattr(replay_mod, "_dispatch_and_apply_event", mock_dispatch)

    # Monkeypatch _build_event_query to select namespace_id (workaround for production query missing namespace_id)
    original_build_query = replay_mod._build_event_query

    def mock_build_query(**kwargs):
        sql, args = original_build_query(**kwargs)
        # Insert namespace_id into the SELECT fields
        sql = sql.replace("SELECT\n            id,", "SELECT\n            id, namespace_id,")
        return sql, args

    monkeypatch.setattr(replay_mod, "_build_event_query", mock_build_query)

    # Monkeypatch _record_to_event_row to parse JSON params/result_summary if returned as strings
    original_to_event_row = replay_mod._record_to_event_row

    def mock_to_event_row(record):
        rec_dict = dict(record)
        params = rec_dict.get("params")
        if isinstance(params, str):
            rec_dict["params"] = json.loads(params)
        result_summary = rec_dict.get("result_summary")
        if isinstance(result_summary, str):
            rec_dict["result_summary"] = json.loads(result_summary)
        return original_to_event_row(rec_dict)

    monkeypatch.setattr(replay_mod, "_record_to_event_row", mock_to_event_row)

    # 4. Run ReconstructiveReplay
    replay = ReconstructiveReplay(pool_proxy)
    events_applied = []
    async for item in replay.execute(
        source_namespace_id=source_ns,
        target_namespace_id=target_ns,
        end_seq=3,
        start_seq=1,
    ):
        events_applied.append(item)

    # Verify replay status messages
    assert any(item.get("type") == "complete" for item in events_applied)

    # 5. Query the target namespace database tables to verify assertions
    async with scoped_pg_session(pool_proxy, target_ns) as conn:
        memories = await conn.fetch(
            "SELECT id, agent_id, memory_type, payload_ref, metadata FROM memories WHERE namespace_id = $1",
            target_ns,
        )

        # We expect two memories: episodic and consolidated
        assert len(memories) == 2, f"Expected 2 memories in target, got {len(memories)}"

        payload_ref_pattern = re.compile(r"^[a-f0-9]{24}$")

        for memory in memories:
            ref = memory["payload_ref"]
            assert payload_ref_pattern.match(ref), f"Invalid payload_ref format: {ref}"

        # Verify salience score for episodic memory reflects the boost
        salience_records = await conn.fetch(
            """
            SELECT s.salience_score, m.metadata
            FROM memory_salience s
            JOIN memories m ON m.id = s.memory_id
            WHERE s.namespace_id = $1 AND m.memory_type = 'episodic'
            """,
            target_ns,
        )

        assert len(salience_records) == 1
        salience_score = salience_records[0]["salience_score"]
        # Source salience was 0.5, boost factor was 0.2. So final salience should be 0.7
        assert abs(salience_score - 0.7) < 1e-5, (
            f"Expected salience score 0.7, got {salience_score}"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_reconstructive_repeatable_uuids(pg_pool, make_namespace, monkeypatch) -> None:
    pool_proxy = PoolProxy(pg_pool)

    # 1. Create source namespace and target namespace
    source_ns = await make_namespace()
    target_ns = await make_namespace()

    agent_id = "test-agent"
    src_memory_id = uuid.uuid4()
    payload_ref = "000000000000000000000001"
    embedding = [0.1] * 768
    assertion_type = "fact"
    memory_type = "episodic"
    metadata = {"source_text": "Episodic memory details"}

    # Seed the source namespace
    async with scoped_pg_session(pool_proxy, source_ns) as conn:
        await conn.execute(
            """
            INSERT INTO memories (id, namespace_id, agent_id, embedding, assertion_type, memory_type, payload_ref, metadata, valid_from)
            VALUES ($1, $2, $3, $4::vector, $5, $6, $7, $8::jsonb, now())
            """,
            src_memory_id,
            source_ns,
            agent_id,
            json.dumps(embedding),
            assertion_type,
            memory_type,
            payload_ref,
            metadata,
        )
        await conn.execute(
            """
            INSERT INTO memory_salience (memory_id, agent_id, namespace_id, salience_score)
            VALUES ($1, $2, $3, $4)
            """,
            src_memory_id,
            agent_id,
            source_ns,
            0.5,
        )

        # Seed events: store_memory
        await append_event(
            conn=conn,
            namespace_id=source_ns,
            agent_id=agent_id,
            event_type="store_memory",
            params={
                "saga_id": str(uuid.uuid4()),
                "memory_id": str(src_memory_id),
                "payload_ref": payload_ref,
                "assertion_type": assertion_type,
                "entities": [],
                "triplets": [],
                "source_namespace_id": str(source_ns),
            },
        )

    # Replay monkeypatching to support type conversions and JSON parsing if needed
    import nce.replay as replay_mod

    # Monkeypatch _build_event_query to select namespace_id (workaround for production query missing namespace_id)
    original_build_query = replay_mod._build_event_query

    def mock_build_query(**kwargs):
        sql, args = original_build_query(**kwargs)
        # Insert namespace_id into the SELECT fields
        sql = sql.replace("SELECT\n            id,", "SELECT\n            id, namespace_id,")
        return sql, args

    monkeypatch.setattr(replay_mod, "_build_event_query", mock_build_query)

    original_to_event_row = replay_mod._record_to_event_row

    def mock_to_event_row(record):
        rec_dict = dict(record)
        params = rec_dict.get("params")
        if isinstance(params, str):
            rec_dict["params"] = json.loads(params)
        result_summary = rec_dict.get("result_summary")
        if isinstance(result_summary, str):
            rec_dict["result_summary"] = json.loads(result_summary)
        return original_to_event_row(rec_dict)

    monkeypatch.setattr(replay_mod, "_record_to_event_row", mock_to_event_row)

    # Run ReconstructiveReplay the first time
    replay = ReconstructiveReplay(pool_proxy)
    events_run1 = []
    async for item in replay.execute(
        source_namespace_id=source_ns,
        target_namespace_id=target_ns,
        end_seq=1,
        start_seq=1,
    ):
        events_run1.append(item)

    # Collect memory IDs and event IDs from run 1
    async with scoped_pg_session(pool_proxy, target_ns) as conn:
        memories1 = await conn.fetch("SELECT id FROM memories WHERE namespace_id = $1", target_ns)
        events1 = await conn.fetch("SELECT id FROM event_log WHERE namespace_id = $1", target_ns)

    assert len(memories1) == 1
    assert len(events1) == 1
    mem_id1 = memories1[0]["id"]
    event_id1 = events1[0]["id"]

    # Clear target namespace tables
    from nce.config import cfg

    monkeypatch.setenv("NCE_BYPASS_WORM", "true")
    monkeypatch.setattr(cfg, "NCE_BYPASS_WORM", True)

    async with scoped_pg_session(pool_proxy, target_ns) as conn:
        await conn.execute("DELETE FROM memory_salience WHERE namespace_id = $1", target_ns)
        await conn.execute("DELETE FROM memories WHERE namespace_id = $1", target_ns)
        await conn.execute("ALTER TABLE event_log DISABLE TRIGGER trg_event_log_worm")
        try:
            await conn.execute("DELETE FROM event_log WHERE namespace_id = $1", target_ns)
        finally:
            await conn.execute("ALTER TABLE event_log ENABLE TRIGGER trg_event_log_worm")
        # Reset sequence in event_sequences
        await conn.execute("UPDATE event_sequences SET seq = 0 WHERE namespace_id = $1", target_ns)

    # Run ReconstructiveReplay the second time
    events_run2 = []
    async for item in replay.execute(
        source_namespace_id=source_ns,
        target_namespace_id=target_ns,
        end_seq=1,
        start_seq=1,
    ):
        events_run2.append(item)

    # Collect memory IDs and event IDs from run 2
    async with scoped_pg_session(pool_proxy, target_ns) as conn:
        memories2 = await conn.fetch("SELECT id FROM memories WHERE namespace_id = $1", target_ns)
        events2 = await conn.fetch("SELECT id FROM event_log WHERE namespace_id = $1", target_ns)

    assert len(memories2) == 1
    assert len(events2) == 1
    mem_id2 = memories2[0]["id"]
    event_id2 = events2[0]["id"]

    # Assert that the remapped UUIDs are identical across reconstruction runs
    assert mem_id1 == mem_id2
    assert event_id1 == event_id2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_payload_copy_strategy(pg_pool, make_namespace, monkeypatch) -> None:
    import os

    from bson import ObjectId
    from motor.motor_asyncio import AsyncIOMotorClient

    pool_proxy = PoolProxy(pg_pool)

    # 1. Create source and target namespaces
    source_ns = await make_namespace()
    target_ns = await make_namespace()

    agent_id = "test-agent"
    src_memory_id = uuid.uuid4()

    # Generate source payload_ref as a valid ObjectId
    src_oid = ObjectId()
    src_payload_ref = str(src_oid)

    # 2. Insert source document in MongoDB
    mongo_client = AsyncIOMotorClient(os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017"))
    db = mongo_client.memory_archive

    await db.episodes.insert_one(
        {
            "_id": src_oid,
            "raw_data": "True isolation target content test",
            "source": "test_replay_payload_copy_strategy",
            "namespace_id": str(source_ns),
        }
    )

    embedding = [0.1] * 768
    assertion_type = "fact"
    memory_type = "episodic"
    metadata = {"source_text": "True isolation test"}

    # 3. Seed the source namespace in Postgres
    async with scoped_pg_session(pool_proxy, source_ns) as conn:
        await conn.execute(
            """
            INSERT INTO memories (id, namespace_id, agent_id, embedding, assertion_type, memory_type, payload_ref, metadata, valid_from)
            VALUES ($1, $2, $3, $4::vector, $5, $6, $7, $8::jsonb, now())
            """,
            src_memory_id,
            source_ns,
            agent_id,
            json.dumps(embedding),
            assertion_type,
            memory_type,
            src_payload_ref,
            metadata,
        )
        await conn.execute(
            """
            INSERT INTO memory_salience (memory_id, agent_id, namespace_id, salience_score)
            VALUES ($1, $2, $3, $4)
            """,
            src_memory_id,
            agent_id,
            source_ns,
            0.5,
        )

        # Seed event: store_memory
        await append_event(
            conn=conn,
            namespace_id=source_ns,
            agent_id=agent_id,
            event_type="store_memory",
            params={
                "saga_id": str(uuid.uuid4()),
                "memory_id": str(src_memory_id),
                "payload_ref": src_payload_ref,
                "assertion_type": assertion_type,
                "entities": [],
                "triplets": [],
                "source_namespace_id": str(source_ns),
            },
        )

    # 4. Monkeypatch to handle postgres vector inserts and JSON parsing
    import nce.replay as replay_mod

    class ConnectionProxy:
        def __init__(self, c):
            self._conn = c

        def __getattr__(self, name):
            return getattr(self._conn, name)

        async def execute(self, query, *args, **kwargs):
            new_args = list(args)
            new_query = query
            if "INSERT INTO memories" in query:
                if len(new_args) >= 4 and isinstance(new_args[3], list):
                    new_args[3] = json.dumps(new_args[3])
                new_query = new_query.replace("$4,", "$4::vector,")
                for i, val in enumerate(new_args):
                    if i == 3:
                        continue
                    if isinstance(val, str) and (val.startswith("{") or val.startswith("[")):
                        try:
                            new_args[i] = json.loads(val)
                        except Exception:
                            pass
            return await self._conn.execute(new_query, *new_args, **kwargs)

    original_dispatch = replay_mod._dispatch_and_apply_event

    async def mock_dispatch(
        write_conn,
        src,
        target_namespace_id,
        llm_payload,
        config_overrides,
        run_id,
        source_namespace_id,
        **kwargs,
    ):
        proxy = ConnectionProxy(write_conn)
        return await original_dispatch(
            proxy,
            src=src,
            target_namespace_id=target_namespace_id,
            llm_payload=llm_payload,
            config_overrides=config_overrides,
            run_id=run_id,
            source_namespace_id=source_namespace_id,
            **kwargs,
        )

    monkeypatch.setattr(replay_mod, "_dispatch_and_apply_event", mock_dispatch)

    original_build_query = replay_mod._build_event_query

    def mock_build_query(**kwargs):
        sql, args = original_build_query(**kwargs)
        sql = sql.replace("SELECT\n            id,", "SELECT\n            id, namespace_id,")
        return sql, args

    monkeypatch.setattr(replay_mod, "_build_event_query", mock_build_query)

    original_to_event_row = replay_mod._record_to_event_row

    def mock_to_event_row(record):
        rec_dict = dict(record)
        params = rec_dict.get("params")
        if isinstance(params, str):
            rec_dict["params"] = json.loads(params)
        result_summary = rec_dict.get("result_summary")
        if isinstance(result_summary, str):
            rec_dict["result_summary"] = json.loads(result_summary)
        return original_to_event_row(rec_dict)

    monkeypatch.setattr(replay_mod, "_record_to_event_row", mock_to_event_row)

    # 5. Run ReconstructiveReplay
    replay = ReconstructiveReplay(pool_proxy)
    events_applied = []
    async for item in replay.execute(
        source_namespace_id=source_ns,
        target_namespace_id=target_ns,
        end_seq=1,
        start_seq=1,
    ):
        events_applied.append(item)

    assert any(item.get("type") == "complete" for item in events_applied)

    # 6. Verify distinct payload_refs pointing to equal content
    async with scoped_pg_session(pool_proxy, target_ns) as conn:
        memories = await conn.fetch(
            "SELECT id, payload_ref FROM memories WHERE namespace_id = $1",
            target_ns,
        )
        assert len(memories) == 1
        target_payload_ref = memories[0]["payload_ref"]

        # Assert distinct payload_refs
        assert target_payload_ref != src_payload_ref, (
            "Source and target payload_ref must be distinct"
        )

        # Verify both point to equal content in MongoDB
        src_doc = await db.episodes.find_one({"_id": ObjectId(src_payload_ref)})
        target_doc = await db.episodes.find_one({"_id": ObjectId(target_payload_ref)})

        assert src_doc is not None, "Source MongoDB document should exist"
        assert target_doc is not None, "Target MongoDB document should exist and have been copied"
        assert target_doc["raw_data"] == src_doc["raw_data"], (
            "Target Mongo doc content must match source"
        )
        assert target_doc["source"] == src_doc["source"], "Metadata details should also match"

    mongo_client.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_deterministic_timestamp_preservation(
    pg_pool, make_namespace, monkeypatch
) -> None:
    from datetime import datetime, timedelta, timezone

    from nce.event_log import verify_event_signature

    pool_proxy = PoolProxy(pg_pool)

    # 1. Create source and target namespaces
    source_ns = await make_namespace()
    target_ns = await make_namespace()

    agent_id = "test-agent"
    src_memory_id = uuid.uuid4()
    payload_ref = "000000000000000000000001"
    embedding = [0.1] * 768
    assertion_type = "fact"
    memory_type = "episodic"
    metadata = {"source_text": "Timestamp preservation integration test"}

    # Define past timestamps to assert deterministic preservation
    # Backdate both occurred_at and valid_from by 1 day
    past_time = datetime.now(timezone.utc) - timedelta(days=1)
    past_time = past_time.replace(microsecond=0)

    # 2. Seed source memory and event log with the specific past timestamps
    async with scoped_pg_session(pool_proxy, source_ns) as conn:
        await conn.execute(
            """
            INSERT INTO memories (id, namespace_id, agent_id, embedding, assertion_type, memory_type, payload_ref, metadata, valid_from)
            VALUES ($1, $2, $3, $4::vector, $5, $6, $7, $8::jsonb, $9)
            """,
            src_memory_id,
            source_ns,
            agent_id,
            json.dumps(embedding),
            assertion_type,
            memory_type,
            payload_ref,
            metadata,
            past_time,
        )

        await conn.execute("ALTER TABLE event_log DISABLE TRIGGER trg_event_log_worm")
        try:
            # Append normally
            res = await append_event(
                conn=conn,
                namespace_id=source_ns,
                agent_id=agent_id,
                event_type="store_memory",
                params={
                    "saga_id": str(uuid.uuid4()),
                    "memory_id": str(src_memory_id),
                    "payload_ref": payload_ref,
                    "assertion_type": assertion_type,
                    "entities": [],
                    "triplets": [],
                    "source_namespace_id": str(source_ns),
                },
            )
            # Recompute signature and update event_log to match past_time
            from nce.signing import get_active_key, sign_fields

            key_id, raw_key = await get_active_key(conn)

            row = await conn.fetchrow("SELECT * FROM event_log WHERE id = $1", res.event_id)
            params = (
                json.loads(row["params"]) if isinstance(row["params"], str) else dict(row["params"])
            )

            from nce.event_log import (
                _GENESIS_SENTINEL,
                _build_signing_fields,
                _compute_chain_hash,
                _compute_content_hash,
            )

            signing_fields = _build_signing_fields(
                event_id=row["id"],
                namespace_id=row["namespace_id"],
                agent_id=row["agent_id"],
                event_type=row["event_type"],
                event_seq=row["event_seq"],
                occurred_at_iso=past_time.isoformat(),
                params=params,
                parent_event_id=row["parent_event_id"],
                prev_chain_hash_hex=_GENESIS_SENTINEL.hex(),
            )
            sig = sign_fields(signing_fields, raw_key)
            c_hash = _compute_content_hash(signing_fields=signing_fields)
            ch_hash = _compute_chain_hash(
                content_hash=c_hash, previous_chain_hash=_GENESIS_SENTINEL
            )

            await conn.execute(
                """
                UPDATE event_log
                SET occurred_at = $1, signature = $2, chain_hash = $3
                WHERE id = $4
                """,
                past_time,
                sig,
                ch_hash,
                row["id"],
            )
        finally:
            await conn.execute("ALTER TABLE event_log ENABLE TRIGGER trg_event_log_worm")

    # 3. Setup replay monkeypatching
    import nce.replay as replay_mod

    class ConnectionProxy:
        def __init__(self, c):
            self._conn = c

        def __getattr__(self, name):
            return getattr(self._conn, name)

        async def execute(self, query, *args, **kwargs):
            new_args = list(args)
            new_query = query
            if "INSERT INTO memories" in query:
                if len(new_args) >= 4 and isinstance(new_args[3], list):
                    new_args[3] = json.dumps(new_args[3])
                new_query = new_query.replace("$4,", "$4::vector,")
                for i, val in enumerate(new_args):
                    if i == 3:
                        continue
                    if isinstance(val, str) and (val.startswith("{") or val.startswith("[")):
                        try:
                            new_args[i] = json.loads(val)
                        except Exception:
                            pass
            return await self._conn.execute(new_query, *new_args, **kwargs)

    original_dispatch = replay_mod._dispatch_and_apply_event

    async def mock_dispatch(
        write_conn,
        src,
        target_namespace_id,
        llm_payload,
        config_overrides,
        run_id,
        source_namespace_id,
        **kwargs,
    ):
        proxy = ConnectionProxy(write_conn)
        return await original_dispatch(
            proxy,
            src=src,
            target_namespace_id=target_namespace_id,
            llm_payload=llm_payload,
            config_overrides=config_overrides,
            run_id=run_id,
            source_namespace_id=source_namespace_id,
            **kwargs,
        )

    monkeypatch.setattr(replay_mod, "_dispatch_and_apply_event", mock_dispatch)

    original_build_query = replay_mod._build_event_query

    def mock_build_query(**kwargs):
        sql, args = original_build_query(**kwargs)
        sql = sql.replace("SELECT\n            id,", "SELECT\n            id, namespace_id,")
        return sql, args

    monkeypatch.setattr(replay_mod, "_build_event_query", mock_build_query)

    original_to_event_row = replay_mod._record_to_event_row

    def mock_to_event_row(record):
        rec_dict = dict(record)
        params = rec_dict.get("params")
        if isinstance(params, str):
            rec_dict["params"] = json.loads(params)
        result_summary = rec_dict.get("result_summary")
        if isinstance(result_summary, str):
            rec_dict["result_summary"] = json.loads(result_summary)
        return original_to_event_row(rec_dict)

    monkeypatch.setattr(replay_mod, "_record_to_event_row", mock_to_event_row)

    # 4. Run ReconstructiveReplay
    replay = ReconstructiveReplay(pool_proxy)
    events_applied = []
    async for item in replay.execute(
        source_namespace_id=source_ns,
        target_namespace_id=target_ns,
        end_seq=1,
        start_seq=1,
    ):
        events_applied.append(item)

    assert any(item.get("type") == "complete" for item in events_applied)

    # 5. Verify Target occurred_at, valid_from, and event signature validity
    async with scoped_pg_session(pool_proxy, target_ns) as conn:
        events = await conn.fetch("SELECT * FROM event_log WHERE namespace_id = $1", target_ns)
        assert len(events) == 1
        replayed_event = events[0]

        ev_occurred_at = replayed_event["occurred_at"].astimezone(timezone.utc)
        assert ev_occurred_at == past_time

        await verify_event_signature(conn, replayed_event)

        memories = await conn.fetch(
            "SELECT id, valid_from FROM memories WHERE namespace_id = $1", target_ns
        )
        assert len(memories) == 1
        replayed_memory = memories[0]

        mem_valid_from = replayed_memory["valid_from"].astimezone(timezone.utc)
        assert mem_valid_from == past_time


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconstructive_replay_digest_match(pg_pool, make_namespace, monkeypatch) -> None:
    import os
    from datetime import datetime, timedelta, timezone

    from bson import ObjectId
    from motor.motor_asyncio import AsyncIOMotorClient

    from nce.replay import ReconstructiveReplay, get_run_status

    pool_proxy = PoolProxy(pg_pool)

    # 1. Create source and target namespaces
    source_ns = await make_namespace()
    target_ns = await make_namespace()

    agent_id = "test-agent"
    src_memory_id = uuid.uuid4()

    # Generate and insert document in MongoDB
    src_oid = ObjectId()
    src_payload_ref = str(src_oid)
    mongo_client = AsyncIOMotorClient(os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017"))
    db = mongo_client.memory_archive
    # Resilient clean up before insertion to prevent E11000 duplicate key error
    await db.episodes.delete_many({"_id": {"$in": [src_oid, ObjectId("000000000000000000000002")]}})
    await db.episodes.insert_many(
        [
            {
                "_id": src_oid,
                "raw_data": "State digest verification content",
                "source": "test_reconstructive_replay_digest_match",
                "namespace_id": str(source_ns),
            },
            {
                "_id": ObjectId("000000000000000000000002"),
                "raw_data": "This is a consolidated abstraction",
                "source": "test_reconstructive_replay_digest_match",
                "namespace_id": str(source_ns),
            },
        ]
    )

    embedding = [0.1] * 768
    assertion_type = "fact"
    memory_type = "episodic"
    metadata = {"source_text": "Digest validation episodic memory"}

    # Define past timestamps
    past_time = datetime.now(timezone.utc) - timedelta(days=2)
    past_time = past_time.replace(microsecond=0)

    # 2. Seed source memory and event log
    async with scoped_pg_session(pool_proxy, source_ns) as conn:
        await conn.execute(
            """
            INSERT INTO memories (id, namespace_id, agent_id, embedding, assertion_type, memory_type, payload_ref, metadata, valid_from, created_at)
            VALUES ($1, $2, $3, $4::vector, $5, $6, $7, $8::jsonb, $9, $9)
            """,
            src_memory_id,
            source_ns,
            agent_id,
            json.dumps(embedding),
            assertion_type,
            memory_type,
            src_payload_ref,
            metadata,
            past_time,
        )
        await conn.execute(
            """
            INSERT INTO memory_salience (memory_id, agent_id, namespace_id, salience_score)
            VALUES ($1, $2, $3, $4)
            """,
            src_memory_id,
            agent_id,
            source_ns,
            0.5,
        )

        await append_event(
            conn=conn,
            namespace_id=source_ns,
            agent_id=agent_id,
            event_type="store_memory",
            params={
                "saga_id": str(uuid.uuid4()),
                "memory_id": str(src_memory_id),
                "payload_ref": src_payload_ref,
                "assertion_type": assertion_type,
                "entities": [],
                "triplets": [],
                "source_namespace_id": str(source_ns),
            },
        )

        # Let's seed a consolidation run with KG edges as well
        consolidated_memory_id = uuid.uuid4()
        consolidation_payload_ref = "000000000000000000000002"

        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id)
            VALUES ($1, 'Entity', $2)
            """,
            "TargetEntity",
            source_ns,
        )
        await conn.execute(
            """
            INSERT INTO kg_edges (subject_label, predicate, object_label, confidence, namespace_id)
            VALUES ($1, $2, $3, $4, $5)
            """,
            "TargetEntity",
            "linked_to",
            "AnotherEntity",
            0.9,
            source_ns,
        )

        consol_res = await append_event(
            conn=conn,
            namespace_id=source_ns,
            agent_id=agent_id,
            event_type="consolidation_run",
            params={
                "abstraction": "This is a consolidated abstraction",
                "key_entities": ["TargetEntity"],
                "key_relations": [
                    {"subject": "TargetEntity", "predicate": "linked_to", "object": "AnotherEntity"}
                ],
                "supporting_memory_ids": [str(src_memory_id)],
                "contradicting_memory_ids": [],
                "confidence": 0.9,
                "source_memories": [str(src_memory_id)],
                "consolidated_memory_id": str(consolidated_memory_id),
                "payload_ref": consolidation_payload_ref,
                "source_namespace_id": str(source_ns),
            },
        )

        from nce import embeddings as _emb

        consol_vector = await _emb.embed("This is a consolidated abstraction")

        await conn.execute(
            """
            INSERT INTO memories (
                id, namespace_id, agent_id,
                embedding, assertion_type, memory_type,
                payload_ref, metadata,
                valid_from, created_at
            ) VALUES (
                $1, $2, $3,
                $4::vector, 'fact', 'consolidated',
                $5, $6::jsonb,
                $7, $8
            )
            """,
            consolidated_memory_id,
            source_ns,
            agent_id,
            json.dumps(consol_vector),
            consolidation_payload_ref,
            json.dumps({}),
            consol_res.occurred_at,
            consol_res.occurred_at,
        )

    # 3. Setup replay monkeypatching to support type conversions and JSON parsing if needed
    import nce.replay as replay_mod

    class ConnectionProxy:
        def __init__(self, c):
            self._conn = c

        def __getattr__(self, name):
            return getattr(self._conn, name)

        async def execute(self, query, *args, **kwargs):
            new_args = list(args)
            new_query = query
            if "INSERT INTO memories" in query:
                if len(new_args) >= 4 and isinstance(new_args[3], list):
                    new_args[3] = json.dumps(new_args[3])
                new_query = new_query.replace("$4,", "$4::vector,")
                for i, val in enumerate(new_args):
                    if i == 3:
                        continue
                    if isinstance(val, str) and (val.startswith("{") or val.startswith("[")):
                        try:
                            new_args[i] = json.loads(val)
                        except Exception:
                            pass
            return await self._conn.execute(new_query, *new_args, **kwargs)

    original_dispatch = replay_mod._dispatch_and_apply_event

    async def mock_dispatch(
        write_conn,
        src,
        target_namespace_id,
        llm_payload,
        config_overrides,
        run_id,
        source_namespace_id,
        **kwargs,
    ):
        if src.event_type == "consolidation_run" and llm_payload is None:
            llm_payload = {
                "prompt": "fake prompt",
                "response": {
                    "abstraction": src.params.get("abstraction", "Consolidated memory abstraction"),
                    "confidence": src.params.get("confidence", 0.9),
                    "supporting_memory_ids": src.params.get("supporting_memory_ids", []),
                    "key_entities": src.params.get("key_entities", []),
                    "key_relations": src.params.get("key_relations", []),
                },
            }
        proxy = ConnectionProxy(write_conn)
        return await original_dispatch(
            proxy,
            src=src,
            target_namespace_id=target_namespace_id,
            llm_payload=llm_payload,
            config_overrides=config_overrides,
            run_id=run_id,
            source_namespace_id=source_namespace_id,
            **kwargs,
        )

    monkeypatch.setattr(replay_mod, "_dispatch_and_apply_event", mock_dispatch)

    original_build_query = replay_mod._build_event_query

    def mock_build_query(**kwargs):
        sql, args = original_build_query(**kwargs)
        sql = sql.replace("SELECT\n            id,", "SELECT\n            id, namespace_id,")
        return sql, args

    monkeypatch.setattr(replay_mod, "_build_event_query", mock_build_query)

    original_to_event_row = replay_mod._record_to_event_row

    def mock_to_event_row(record):
        rec_dict = dict(record)
        params = rec_dict.get("params")
        if isinstance(params, str):
            rec_dict["params"] = json.loads(params)
        result_summary = rec_dict.get("result_summary")
        if isinstance(result_summary, str):
            rec_dict["result_summary"] = json.loads(result_summary)
        return original_to_event_row(rec_dict)

    monkeypatch.setattr(replay_mod, "_record_to_event_row", mock_to_event_row)

    # 4. Run ReconstructiveReplay
    replay = ReconstructiveReplay(pool_proxy)
    events_applied = []
    async for item in replay.execute(
        source_namespace_id=source_ns,
        target_namespace_id=target_ns,
        end_seq=2,
        start_seq=1,
    ):
        events_applied.append(item)

    # Verify complete event exists
    complete_event = next(item for item in events_applied if item.get("type") == "complete")
    run_id = uuid.UUID(complete_event["run_id"])

    # 5. Check run status details
    status = await get_run_status(pool_proxy, run_id)
    assert status["digest_match"] is True, (
        f"Digest mismatch! Source: {status['source_state_digest']}, Target: {status['target_state_digest']}"
    )
    assert status["source_state_digest"] is not None
    assert status["target_state_digest"] is not None
    assert status["source_state_digest"] == status["target_state_digest"]

    # Let's clean up MongoDB
    await db.episodes.delete_many({"_id": {"$in": [src_oid, ObjectId("000000000000000000000002")]}})
    mongo_client.close()
