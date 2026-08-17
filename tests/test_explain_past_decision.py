"""Acceptance test for Batch 43 — II.5 Bi-temporal Accountability.

`explain_past_decision(as_of=T)` must:
  1. Reconstruct the *belief set valid at T* (memories whose temporal validity
     window covers T) with each belief annotated by the signed epistemic receipt
     that was valid then; and
  2. when a counterfactual fork is requested, run a forked replay and return a
     ``digest_match``-verified alternate state (source vs. target canonical state
     digest taken as of the fork point).

This exercises the real handler against live Postgres/Mongo (integration), reusing
the proven replay monkeypatch shims from ``test_replay_handlers_integration``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from nce.db_utils import scoped_pg_session
from nce.event_log import append_event


class _AcquireContext:
    """Acquire wrapper that registers an *idempotent* json/jsonb codec.

    ``event_log.append_event`` pre-serialises ``params`` to a JSON string before
    binding it to a ``jsonb`` parameter.  A naive ``encoder=json.dumps`` codec
    would double-encode that string (storing a quoted JSON scalar), which breaks
    ``params->>'memory_id'`` lookups in ``get_event_provenance``.  Passing strings
    through unchanged keeps the codec safe for both already-serialised and raw
    values, while still decoding reads so handlers see dicts.
    """

    def __init__(self, ctx):
        self.ctx = ctx
        self.conn = None

    @staticmethod
    def _enc(v):
        return v if isinstance(v, str) else json.dumps(v)

    async def __aenter__(self):
        self.conn = await self.ctx.__aenter__()
        for schema_type in ("jsonb", "json"):
            try:
                await self.conn.set_type_codec(
                    schema_type,
                    encoder=self._enc,
                    decoder=json.loads,
                    schema="pg_catalog",
                )
            except Exception:
                pass
        return self.conn

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return await self.ctx.__aexit__(exc_type, exc_val, exc_tb)


class PoolProxy:
    """Pool wrapper whose ``acquire`` yields connections with the idempotent codec."""

    def __init__(self, pool):
        self._pool = pool

    def __getattr__(self, name):
        return getattr(self._pool, name)

    def acquire(self, *args, **kwargs):
        return _AcquireContext(self._pool.acquire(*args, **kwargs))


class _EngineStub:
    """Minimal stand-in exposing only ``pg_pool`` — all the handler touches."""

    def __init__(self, pool: PoolProxy) -> None:
        self.pg_pool = pool


def _fork_checksum(
    *,
    source_ns: uuid.UUID,
    target_ns: uuid.UUID,
    fork_seq: int,
    start_seq: int = 1,
) -> str:
    """Recompute the canonical payload checksum the handler/model verifies."""
    from nce.signing import canonical_json

    payload = {
        "source_namespace_id": str(source_ns),
        "target_namespace_id": str(target_ns),
        "fork_seq": fork_seq,
        "start_seq": start_seq,
        "replay_mode": "deterministic",
        "config_overrides": None,
        "agent_id_filter": None,
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_explain_past_decision_belief_set_and_verified_fork(
    pg_pool, make_namespace, monkeypatch
) -> None:
    import os

    from bson import ObjectId
    from motor.motor_asyncio import AsyncIOMotorClient

    pool_proxy = PoolProxy(pg_pool)
    engine = _EngineStub(pool_proxy)

    source_ns = await make_namespace()
    target_ns = await make_namespace()
    agent_id = "test-agent"

    # ── Seed one episodic memory in Mongo + Postgres in the source namespace ──
    src_oid = ObjectId()
    src_payload_ref = str(src_oid)
    mongo_client = AsyncIOMotorClient(os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017"))
    db = mongo_client.memory_archive
    await db.episodes.insert_one(
        {
            "_id": src_oid,
            "raw_data": "Bi-temporal belief content",
            "source": "test_explain_past_decision",
            "namespace_id": str(source_ns),
        }
    )

    src_memory_id = uuid.uuid4()
    embedding = [0.1] * 768
    # The belief becomes valid 2 days ago; T is "1 day ago" — so it is valid at T.
    valid_from = (datetime.now(timezone.utc) - timedelta(days=2)).replace(microsecond=0)
    as_of_t = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0)
    # A memory created AFTER T must NOT appear in the belief set valid at T.
    future_memory_id = uuid.uuid4()
    future_valid_from = datetime.now(timezone.utc).replace(microsecond=0)

    async with scoped_pg_session(pool_proxy, source_ns) as conn:
        await conn.execute(
            """
            INSERT INTO memories (id, namespace_id, agent_id, embedding, assertion_type,
                                  memory_type, payload_ref, metadata, valid_from, created_at)
            VALUES ($1, $2, $3, $4::vector, 'fact', 'episodic', $5, $6::jsonb, $7, $7)
            """,
            src_memory_id,
            source_ns,
            agent_id,
            json.dumps(embedding),
            src_payload_ref,
            json.dumps({"source_text": "Bi-temporal belief"}),
            valid_from,
        )
        await conn.execute(
            """
            INSERT INTO memories (id, namespace_id, agent_id, embedding, assertion_type,
                                  memory_type, payload_ref, metadata, valid_from, created_at)
            VALUES ($1, $2, $3, $4::vector, 'fact', 'episodic', $5, $6::jsonb, $7, $7)
            """,
            future_memory_id,
            source_ns,
            agent_id,
            json.dumps(embedding),
            "000000000000000000000099",
            json.dumps({"source_text": "Learned later"}),
            future_valid_from,
        )
        store_params = {
            "saga_id": str(uuid.uuid4()),
            "memory_id": str(src_memory_id),
            "payload_ref": src_payload_ref,
            "assertion_type": "fact",
            "entities": [],
            "triplets": [],
            "source_namespace_id": str(source_ns),
        }
        await conn.execute("ALTER TABLE event_log DISABLE TRIGGER trg_event_log_worm")
        try:
            res = await append_event(
                conn=conn,
                namespace_id=source_ns,
                agent_id=agent_id,
                event_type="store_memory",
                params=store_params,
            )
            # Backdate the creating event to valid_from so its signed receipt is
            # "valid at T" (T is one day after the belief was formed).
            from nce.event_log import (
                _GENESIS_SENTINEL,
                _build_signing_fields,
                _compute_chain_hash,
                _compute_content_hash,
            )
            from nce.signing import get_active_key, sign_fields

            key_id, raw_key = await get_active_key(conn)
            row = await conn.fetchrow("SELECT * FROM event_log WHERE id = $1", res.event_id)
            signing_fields = _build_signing_fields(
                event_id=row["id"],
                namespace_id=row["namespace_id"],
                agent_id=row["agent_id"],
                event_type=row["event_type"],
                event_seq=row["event_seq"],
                occurred_at_iso=valid_from.isoformat(),
                params=store_params,
                parent_event_id=row["parent_event_id"],
                prev_chain_hash_hex=_GENESIS_SENTINEL.hex(),
            )
            sig = sign_fields(signing_fields, raw_key)
            c_hash = _compute_content_hash(signing_fields=signing_fields)
            ch_hash = _compute_chain_hash(
                content_hash=c_hash, previous_chain_hash=_GENESIS_SENTINEL
            )
            await conn.execute(
                "UPDATE event_log SET occurred_at = $1, signature = $2, chain_hash = $3 WHERE id = $4",
                valid_from,
                sig,
                ch_hash,
                row["id"],
            )
        finally:
            await conn.execute("ALTER TABLE event_log ENABLE TRIGGER trg_event_log_worm")

    # ── Replay shims (identical to the proven integration suite) ──
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

        async def fetchrow(self, query, *args, **kwargs):
            row = await self._conn.fetchrow(query, *args, **kwargs)
            if row is None:
                return None
            d = dict(row)
            # The fork's read connection may not have the jsonb codec applied;
            # decode JSON-text columns the store_memory handler will dict()/parse.
            for col in ("metadata", "params", "result_summary"):
                val = d.get(col)
                if isinstance(val, str):
                    try:
                        d[col] = json.loads(val)
                    except Exception:
                        pass
            return d

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

    # ── Call the handler under test ──
    from nce.replay_mcp_handlers import handle_explain_past_decision

    expected_sha = _fork_checksum(source_ns=source_ns, target_ns=target_ns, fork_seq=1, start_seq=1)
    raw = await handle_explain_past_decision(
        engine,
        {
            "namespace_id": str(source_ns),
            "as_of": as_of_t.isoformat(),
            # counterfactual: verified forked replay
            "source_namespace_id": str(source_ns),
            "target_namespace_id": str(target_ns),
            "fork_seq": 1,
            "start_seq": 1,
            "replay_mode": "deterministic",
            "expected_sha256": expected_sha,
        },
    )
    result = json.loads(raw)

    # 1. Belief set valid at T: the day-2 belief is present, the future one is NOT.
    belief_ids = {b["memory_id"] for b in result["beliefs"]}
    assert str(src_memory_id) in belief_ids, "belief valid at T must be reconstructed"
    assert str(future_memory_id) not in belief_ids, (
        "a memory only valid after T must not leak into the past belief set"
    )
    assert result["belief_count"] == 1

    # Each reconstructed belief carries its signed epistemic receipt valid at T.
    belief = next(b for b in result["beliefs"] if b["memory_id"] == str(src_memory_id))
    assert belief["receipt"] is not None
    assert belief["receipt"]["verified"] is True
    assert belief["receipt"]["event_seq"] >= 1

    # 2. The counterfactual fork is digest_match-verified.
    cf = result["counterfactual"]
    assert cf["status"] == "success"
    assert cf["digest_match"] is True, (
        f"fork not faithful: src={cf['source_state_digest']} tgt={cf['target_state_digest']}"
    )
    assert cf["source_state_digest"] == cf["target_state_digest"]

    await db.episodes.delete_one({"_id": src_oid})
    mongo_client.close()
