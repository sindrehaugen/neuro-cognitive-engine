"""Regression suite for v3.4.3 Wave 1 (#3, #4, #10) and Wave 2 (#11-#13, #15-#18) core fixes."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import math
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from bson import ObjectId
from starlette.requests import Request

from nce import ArtifactPayload as RootArtifactPayload
from nce import NCEEngine, StoreArtifactRequest
from nce.admin_mcp_handlers import handle_verify_memory
from nce.admin_plugins import MINTED_MCP_TOKENS, admin_plugins_mint_token
from nce.consolidation import ConsolidatedAbstraction, ConsolidationWorker
from nce.contradictions import ContradictionResult, _detect_contradictions_impl, _select_candidates
from nce.envelope import encrypt_raw_data, maybe_decrypt_raw_data
from nce.garbage_collector import _collect_minio_orphans
from nce.memory_mcp_handlers import handle_forget_memory, handle_store_artifact
from nce.models import (
    A2AGrantRequest,
    ArtifactPayload,
    AssertionType,
    ForgetMemoryRequest,
    MediaPayload,
    MemoryType,
    SagaState,
    StoreMemoryRequest,
)
from nce.orchestrators.graph import GraphOrchestrator
from nce.orchestrators.memory import MemoryOrchestrator
from nce.signing import canonical_json

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module_from_path(mod_name: str, file_path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(mod_name, file_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _TxCtx:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> None:
        return None


class _AcquireCtx:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


# =============================================================================
# #3 — Saga Timeout & Pre-Generated Mongo ObjectId Compensation + RLS
# =============================================================================


@pytest.mark.asyncio
async def test_wave1_item3_connect_configures_bounded_mongo_and_minio_timeouts():
    """NCEEngine.connect sets 5s Motor timeouts and urllib3 PoolManager timeouts/retries on MinIO."""
    engine = NCEEngine()
    captured_motor_kwargs: dict[str, Any] = {}
    captured_minio_kwargs: dict[str, Any] = {}

    def _fake_motor(_uri: str, **kwargs: Any) -> MagicMock:
        captured_motor_kwargs.update(kwargs)
        return MagicMock()

    def _fake_minio(_endpoint: str, **kwargs: Any) -> MagicMock:
        captured_minio_kwargs.update(kwargs)
        m = MagicMock()
        m.bucket_exists.return_value = True
        return m

    with (
        patch("nce.orchestrator.cfg.validate"),
        patch("nce.orchestrator.AsyncIOMotorClient", side_effect=_fake_motor),
        patch("nce.orchestrator.Minio", side_effect=_fake_minio),
        patch("nce.orchestrator.asyncpg.create_pool", AsyncMock(return_value=MagicMock())),
        patch("nce.orchestrator.redis.from_url", return_value=MagicMock()),
        patch("redis.from_url", return_value=MagicMock()),
        patch.object(engine, "_init_pg_schema", AsyncMock()),
        patch.object(engine, "_apply_pg_migrations", AsyncMock()),
        patch.object(engine, "_apply_plugin_pg_migrations", AsyncMock()),
        patch.object(engine, "_verify_schema_version", AsyncMock()),
        patch.object(engine, "_ensure_initial_signing_key", AsyncMock()),
        patch.object(engine, "_verify_master_key_matches_data", AsyncMock()),
        patch.object(engine, "_verify_worm_enforcement", AsyncMock()),
        patch.object(engine, "_verify_rls_enforcement", AsyncMock()),
        patch.object(engine, "_seed_node_ownership_all", AsyncMock()),
        patch.object(engine, "_check_global_legacy_warning", AsyncMock()),
        patch.object(engine, "_init_mongo_indexes", AsyncMock()),
    ):
        await engine.connect()

    assert captured_motor_kwargs["serverSelectionTimeoutMS"] == 5_000
    assert captured_motor_kwargs["connectTimeoutMS"] == 5_000
    assert captured_motor_kwargs["socketTimeoutMS"] == 5_000

    http_client = captured_minio_kwargs["http_client"]
    timeout_cfg = http_client.connection_pool_kw["timeout"]
    retry_cfg = http_client.connection_pool_kw["retries"]
    assert timeout_cfg.connect_timeout == 2.0
    assert timeout_cfg.read_timeout == 5.0
    assert retry_cfg.total == 2


@pytest.mark.asyncio
async def test_wave1_item3_saga_pregenerated_oid_rollback_and_recovery_needed_under_rls():
    """Mid-flight insert_one timeout still deletes pre-generated ObjectId; delete timeout logs recovery_needed under RLS."""
    ns_id = uuid4()
    saga_uuid = str(uuid4())

    conn = MagicMock()
    conn.transaction = MagicMock(return_value=_TxCtx())
    conn.fetchrow = AsyncMock(return_value={"id": saga_uuid})
    conn.execute = AsyncMock(return_value="UPDATE 1")

    pg_pool = MagicMock()
    pg_pool.acquire = MagicMock(return_value=_AcquireCtx(conn))

    raw_episodes = MagicMock()
    raw_episodes._mock_self = True
    seen_insert_oid: list[ObjectId] = []

    async def _timeout_insert(doc: dict[str, Any]) -> Any:
        seen_insert_oid.append(doc["_id"])
        raise asyncio.TimeoutError("simulated paused Mongo socket timeout")

    raw_episodes.insert_one = AsyncMock(side_effect=_timeout_insert)
    # Case A: delete_one succeeds right as Mongo unpauses -> rolled_back
    raw_episodes.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))

    mongo_client = MagicMock()
    mongo_client.memory_archive.episodes = raw_episodes
    mongo_client.__getitem__.return_value.episodes = raw_episodes

    import nce.orchestrators.memory as mem_mod

    orch = mem_mod.MemoryOrchestrator(
        pg_pool=pg_pool, mongo_client=mongo_client, redis_client=MagicMock()
    )
    orch._apply_pii_pipeline = AsyncMock(
        return_value=(
            MagicMock(redacted=False, entities_found=[], vault_entries=[]),
            "summary",
            "heavy",
            [],
            [],
        )
    )

    payload = StoreMemoryRequest(
        namespace_id=ns_id,
        agent_id="agent-alpha",
        content="Saga resiliency test",
        memory_type=MemoryType.episodic,
        assertion_type=AssertionType.fact,
    )

    set_ns_calls: list[Any] = []

    async def _record_set_ns(_c: Any, ns: Any) -> None:
        set_ns_calls.append(str(ns))

    with patch.object(mem_mod, "set_namespace_context", side_effect=_record_set_ns):
        with pytest.raises(asyncio.TimeoutError):
            await orch._run_store_memory_saga(payload)

    assert len(seen_insert_oid) == 1
    pre_oid = seen_insert_oid[0]
    assert isinstance(pre_oid, ObjectId)
    raw_episodes.delete_one.assert_awaited_once_with({"_id": pre_oid, "namespace_id": str(ns_id)})
    assert str(ns_id) in set_ns_calls

    # Case B: delete_one ALSO times out while Mongo remains paused -> transitions to recovery_needed with inserted_mongo_id
    conn.execute.reset_mock()
    seen_insert_oid.clear()
    raw_episodes.delete_one = AsyncMock(side_effect=asyncio.TimeoutError("still paused"))

    with patch.object(mem_mod, "set_namespace_context", AsyncMock()):
        with pytest.raises(asyncio.TimeoutError):
            await orch._run_store_memory_saga(payload)

    second_oid = str(seen_insert_oid[0])
    recovery_updates = [
        call.args
        for call in conn.execute.await_args_list
        if "UPDATE saga_execution_log" in call.args[0]
        and call.args[1] == SagaState.RECOVERY_NEEDED.value
    ]
    assert len(recovery_updates) == 1
    rec_args = recovery_updates[0]
    assert json.loads(rec_args[3]) == {"inserted_mongo_id": second_oid}
    assert rec_args[4] == str(ns_id)
    for pending_task in list(orch._bg_cleanup_tasks):
        pending_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending_task

    # Case C: mongo_client is None -> no AttributeError in rollback handler
    orch_no_mongo = mem_mod.MemoryOrchestrator(
        pg_pool=pg_pool,
        mongo_client=None,  # type: ignore[arg-type]
        redis_client=MagicMock(),
    )
    orch_no_mongo._apply_pii_pipeline = AsyncMock(side_effect=RuntimeError("early failure"))
    with patch.object(mem_mod, "set_namespace_context", AsyncMock()):
        with pytest.raises(RuntimeError, match="early failure"):
            await orch_no_mongo._run_store_memory_saga(payload)


# =============================================================================
# #4 — verify_memory() Under Forced RLS
# =============================================================================


@pytest.mark.asyncio
async def test_wave1_item4_verify_memory_rls_scoped_decrypt_and_event_provenance(
    monkeypatch: pytest.MonkeyPatch,
):
    """verify_memory scopes PG/Mongo/Redis by namespace, decrypts envelope DEK, and verifies event_log provenance."""
    monkeypatch.setenv("NCE_MASTER_KEY", "unit-test-master-secret-key-at-least-32b!!")
    ns_id = uuid4()
    mem_id = uuid4()
    ev_id = uuid4()
    oid = ObjectId()
    plaintext = "Confidential episodic body verified under forced RLS"
    ciphertext, wrapped_dek, _dek_id = encrypt_raw_data(plaintext)
    expected_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()

    mem_row = {
        "id": mem_id,
        "namespace_id": ns_id,
        "agent_id": "agent-1",
        "created_at": datetime.now(timezone.utc),
        "assertion_type": "fact",
        "payload_ref": str(oid),
        "signature": None,
        "signature_key_id": None,
        "origin_event_id": ev_id,
        "wrapped_dek": wrapped_dek,
    }
    event_row = {
        "id": ev_id,
        "namespace_id": ns_id,
        "signature_key_id": "sig-key-2026",
        "occurred_at": datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc),
    }

    fake_conn = MagicMock()
    fetched_sql: list[tuple[str, tuple[Any, ...]]] = []

    async def _fetchrow(sql: str, *args: Any) -> Any:
        fetched_sql.append((sql, args))
        if "FROM memories" in sql:
            return mem_row
        if "FROM event_log" in sql:
            return event_row
        return None

    fake_conn.fetchrow = AsyncMock(side_effect=_fetchrow)

    scoped_ns_seen: list[str] = []

    @asynccontextmanager
    async def _fake_scoped_pg(_pool: Any, ns: Any):
        scoped_ns_seen.append(str(ns))
        yield fake_conn

    fake_s_db = MagicMock()
    fake_s_db.episodes.find_one = AsyncMock(return_value={"_id": oid, "raw_data": ciphertext})

    @asynccontextmanager
    async def _fake_scoped_mongo(_client: Any, ns: Any):
        assert str(ns) == str(ns_id)
        yield fake_s_db

    redis_mock = MagicMock()
    redis_mock.get = AsyncMock(return_value=None)
    redis_mock.setex = AsyncMock()

    import nce.orchestrators.memory as mem_mod

    orch = mem_mod.MemoryOrchestrator(
        pg_pool=MagicMock(),
        mongo_client=MagicMock(),
        redis_client=redis_mock,
    )

    verify_sig_mock = AsyncMock(return_value=None)
    with (
        patch.object(mem_mod, "scoped_pg_session", side_effect=_fake_scoped_pg),
        patch.object(mem_mod, "scoped_mongo_session", side_effect=_fake_scoped_mongo),
        patch("nce.event_log.verify_event_signature", verify_sig_mock),
    ):
        result = await orch.verify_memory(str(mem_id), namespace_id=str(ns_id))

    assert scoped_ns_seen == [str(ns_id)]
    assert any("AND namespace_id = $2::uuid" in sql for sql, _ in fetched_sql)
    fake_s_db.episodes.find_one.assert_awaited_once_with({"_id": oid})
    verify_sig_mock.assert_awaited_once_with(fake_conn, event_row)
    redis_mock.setex.assert_awaited_once()
    assert redis_mock.setex.await_args.args[0] == f"mem_verify_hash:{ns_id}:{mem_id}"
    assert result["valid"] is True
    assert result["memory_id"] == str(mem_id)
    assert result["origin_event_id"] == str(ev_id)
    assert result["signature_key_id"] == "sig-key-2026"
    assert result["payload_hash"] == expected_hash

    # Verify handle_verify_memory plumbs namespace_id to engine.verify_memory
    monkeypatch.setenv("NCE_ADMIN_OVERRIDE", "true")
    mock_engine = MagicMock()
    mock_engine.verify_memory = AsyncMock(return_value=result)
    raw_resp = await handle_verify_memory(
        mock_engine,
        {"memory_id": str(mem_id), "namespace_id": str(ns_id)},
    )
    mock_engine.verify_memory.assert_awaited_once_with(
        memory_id=str(mem_id),
        as_of=None,
        namespace_id=str(ns_id),
    )
    assert json.loads(raw_resp)["valid"] is True


# =============================================================================
# #10 — Decrypt Envelope DEKs in Contradictions & Consolidation
# =============================================================================


@pytest.mark.asyncio
async def test_wave1_item10_contradictions_and_consolidation_decrypt_envelope_deks(
    monkeypatch: pytest.MonkeyPatch,
):
    """Contradictions & Consolidation decrypt TCDEK\\x01 payloads, read legacy plaintext, and skip shredded DEKs."""
    monkeypatch.setenv("NCE_MASTER_KEY", "unit-test-master-secret-key-at-least-32b!!")
    ns_id = uuid4()
    new_mem_id = str(uuid4())
    enc_cand_id = uuid4()
    plain_cand_id = uuid4()
    shred_cand_id = uuid4()

    enc_ref = str(ObjectId())
    plain_ref = str(ObjectId())
    shred_ref = str(ObjectId())

    enc_bytes, enc_wdek, _ = encrypt_raw_data("Service alpha is enabled in cluster.")
    shred_bytes, _orig_wdek, _ = encrypt_raw_data("Shredded secret record.")

    # 1. Verify _select_candidates SQL selects wrapped_dek
    select_conn = MagicMock()
    select_conn.fetch = AsyncMock(return_value=[])
    await _select_candidates(select_conn, [0.1, 0.2], str(ns_id), new_mem_id)
    select_sql = select_conn.fetch.await_args.args[0]
    assert "wrapped_dek" in select_sql

    # 2. Verify _detect_contradictions_impl decrypts encrypted candidate, reads plaintext, skips shredded
    candidates = [
        {"id": enc_cand_id, "payload_ref": enc_ref, "wrapped_dek": enc_wdek, "similarity": 0.94},
        {"id": plain_cand_id, "payload_ref": plain_ref, "wrapped_dek": None, "similarity": 0.91},
        {
            "id": shred_cand_id,
            "payload_ref": shred_ref,
            "wrapped_dek": b"\x00" * 40,
            "similarity": 0.90,
        },
    ]

    pg_conn = MagicMock()
    pg_conn.fetchrow = AsyncMock(return_value={"metadata": {}})
    pg_conn.execute = AsyncMock(return_value="INSERT 0 1")

    @asynccontextmanager
    async def _scoped_pg(_pool: Any, _ns: Any):
        yield pg_conn

    @asynccontextmanager
    async def _scoped_mongo(_client: Any, _ns: Any):
        yield MagicMock()

    nli_seen_premises: list[str] = []

    async def _fake_nli_check(cand_text: str, mem_text: str):
        nli_seen_premises.append(cand_text)
        assert not cand_text.startswith("TCDEK")
        return 0.91, cand_text, True, [{"source": "nli", "confidence": 0.91}]

    with (
        patch("nce.contradictions.scoped_pg_session", side_effect=_scoped_pg),
        patch("nce.db_utils.scoped_mongo_session", side_effect=_scoped_mongo),
        patch("nce.contradictions._select_candidates", AsyncMock(return_value=candidates)),
        patch(
            "nce.contradictions.fetch_episodes_raw_by_ref",
            AsyncMock(
                return_value={
                    enc_ref: enc_bytes,
                    plain_ref: "Service alpha runs on port 8080.",
                    shred_ref: shred_bytes,
                }
            ),
        ),
        patch("nce.contradictions._check_kg_contradiction", AsyncMock(return_value=False)),
        patch("nce.contradictions._check_nli_contradiction", side_effect=_fake_nli_check),
        patch(
            "nce.contradictions._resolve_with_llm",
            AsyncMock(return_value=(0.92, "Direct polarity conflict", True)),
        ),
    ):
        detected = await _detect_contradictions_impl(
            pg_pool=MagicMock(),
            mongo_client=MagicMock(),
            namespace_id=str(ns_id),
            memory_id=new_mem_id,
            memory_text="Service alpha is disabled in cluster.",
            assertion_type="fact",
            embedding=[0.5, 0.5],
            agent_id="agent-1",
            triplets=[],
            detection_path="sync",
        )

    assert nli_seen_premises == [
        "Service alpha is enabled in cluster.",
        "Service alpha runs on port 8080.",
    ]
    assert detected is not None
    assert len(detected["contradictions"]) == 2

    # 3. Verify ConsolidationWorker decrypts cluster episodes AND envelope-encrypts consolidated abstraction
    inserted_docs: list[dict[str, Any]] = []
    fake_mongo_db = MagicMock()

    async def _capture_mongo_insert(doc: dict[str, Any]) -> Any:
        inserted_docs.append(doc)
        return MagicMock(inserted_id=ObjectId())

    fake_mongo_db.episodes.insert_one = AsyncMock(side_effect=_capture_mongo_insert)

    @asynccontextmanager
    async def _scoped_consolidation_mongo(_client: Any, _ns: Any):
        yield fake_mongo_db

    worker = ConsolidationWorker(MagicMock(), MagicMock(), mongo_client=MagicMock())
    with (
        patch("nce.db_utils.scoped_mongo_session", side_effect=_scoped_consolidation_mongo),
        patch(
            "nce.consolidation.fetch_episodes_raw_by_ref",
            AsyncMock(
                return_value={
                    enc_ref: enc_bytes,
                    plain_ref: "Service alpha runs on port 8080.",
                    shred_ref: shred_bytes,
                }
            ),
        ),
    ):
        llm_docs = await worker._build_cluster_llm_documents(candidates, ns_id)
        monkeypatch.setattr("nce.consolidation.cfg.NCE_ENVELOPE_ENCRYPTION_ENABLED", True)
        await worker._store_abstraction_in_mongo(ns_id, "Synthesized encrypted abstraction")

    assert [d["content"] for d in llm_docs] == [
        "Service alpha is enabled in cluster.",
        "Service alpha runs on port 8080.",
    ]
    assert len(inserted_docs) == 1
    stored_raw = inserted_docs[0]["raw_data"]
    assert isinstance(stored_raw, (bytes, bytearray)) and bytes(stored_raw).startswith(b"TCDEK\x01")
    assert worker._last_wrapped_dek is not None
    assert worker._last_dek_key_id is not None
    assert (
        maybe_decrypt_raw_data(stored_raw, worker._last_wrapped_dek)
        == "Synthesized encrypted abstraction"
    )


# =============================================================================
# #11 — Artifact Types & MinIO Bucket Alignment
# =============================================================================


@pytest.mark.asyncio
async def test_wave2_item11_artifact_types_nce_artifacts_bucket_and_mcp_kwargs(tmp_path: Path):
    """StoreArtifactRequest supports pdf/binary/log + other->binary, uploads to nce-artifacts, and GC sweeps it."""
    assert StoreArtifactRequest is MediaPayload
    assert ArtifactPayload is MediaPayload
    assert RootArtifactPayload is MediaPayload

    ns_id = uuid4()
    for art_type in ("image", "audio", "video", "pdf", "binary", "log"):
        m = StoreArtifactRequest(
            namespace_id=ns_id,
            artifact_type=art_type,
            file_path_on_disk="sample.bin",
            summary=f"Summary for {art_type}",
        )
        assert m.artifact_type == art_type
        assert m.media_type == art_type
        assert m.user_id == "system"
        assert m.session_id == "default"

    legacy = MediaPayload(
        namespace_id=ns_id,
        media_type="other",
        file_path_on_disk="legacy.dat",
        summary="Legacy other",
    )
    assert legacy.artifact_type == "binary"
    assert legacy.media_type == "binary"

    # A2AGrantRequest parity field
    grant_req = A2AGrantRequest(
        target_namespace_id=ns_id,
        target_agent_id="peer-agent",
        scopes=[{"resource_type": "namespace", "resource_id": str(ns_id)}],
        one_time=True,
    )
    assert grant_req.one_time is True

    # Upload path uses bucket "nce-artifacts" and "{namespace_id}/{artifact_type}/{session_id}/..."
    sample_pdf = tmp_path / "architecture.pdf"
    sample_pdf.write_bytes(b"%PDF-1.4 sample")

    minio_mock = MagicMock()
    minio_mock.bucket_exists.return_value = False
    orch = MemoryOrchestrator(
        pg_pool=MagicMock(),
        mongo_client=MagicMock(),
        redis_client=MagicMock(),
        minio_client=minio_mock,
    )
    orch.store_memory = AsyncMock(return_value={"payload_ref": str(ObjectId())})

    with patch("nce.orchestrators.memory.cfg.NCE_ARTIFACT_STAGING_DIR", str(tmp_path)):
        mock_engine = MagicMock()
        mock_engine.store_artifact = orch.store_artifact
        resp_json = await handle_store_artifact(
            mock_engine,
            {
                "namespace_id": str(ns_id),
                "artifact_type": "pdf",
                "file_path_on_disk": str(sample_pdf),
                "summary": "Architecture specification PDF",
                "mcp_api_key": "transport-only-token",
            },
        )

    assert json.loads(resp_json)["status"] == "ok"
    minio_mock.make_bucket.assert_called_once_with("nce-artifacts")
    fput_args = minio_mock.fput_object.call_args.args
    assert fput_args[0] == "nce-artifacts"
    assert fput_args[1].startswith(f"{ns_id}/pdf/default/")
    assert fput_args[1].endswith(".pdf")

    # _init_minio_buckets initializes both nce-artifacts and nce-llm-payloads
    eng = NCEEngine()
    eng.minio_client = MagicMock()
    eng.minio_client.bucket_exists.return_value = False
    eng._init_minio_buckets()
    made_buckets = {c.args[0] for c in eng.minio_client.make_bucket.call_args_list}
    assert {
        "nce-artifacts",
        "nce-llm-payloads",
        "mcp-audio",
        "mcp-video",
        "mcp-images",
    } <= made_buckets

    # GC sweeps nce-artifacts
    b_art = MagicMock()
    b_art.name = "nce-artifacts"
    orphan_obj = MagicMock(
        is_dir=False,
        object_name=f"{ns_id}/pdf/default/orphan.pdf",
        last_modified=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    gc_minio = MagicMock()
    gc_minio.list_buckets.return_value = [b_art]
    gc_minio.list_objects.return_value = [orphan_obj]
    removed = await _collect_minio_orphans(gc_minio, set())
    assert removed == 1
    gc_minio.remove_object.assert_called_once_with(
        "nce-artifacts", f"{ns_id}/pdf/default/orphan.pdf"
    )


# =============================================================================
# #12 & #13 — Consolidation Replay Cache & KG origin_event_id Lineage
# =============================================================================


@pytest.mark.asyncio
async def test_wave2_item12_and_item13_consolidation_minio_cache_and_kg_origin_event_id():
    """Consolidation uploads canonical JSON to nce-llm-payloads and populates origin_event_id on memories/KG."""
    ns_id = uuid4()
    src_a, src_b = str(uuid4()), str(uuid4())
    consolidated_id = uuid4()
    abstraction = ConsolidatedAbstraction(
        abstraction="Unified cluster synthesis",
        key_entities=["ServiceAlpha"],
        key_relations=[{"subject": "ServiceAlpha", "predicate": "uses", "object": "Postgres"}],
        supporting_memory_ids=[src_a, src_b],
        contradicting_memory_ids=[],
        confidence=0.91,
    )

    provider = MagicMock()
    provider.model_identifier.return_value = "stub/consolidation-v1"

    minio_mock = MagicMock()
    minio_mock.bucket_exists.return_value = True
    uploaded_objects: list[tuple[str, str, bytes]] = []

    def _capture_put(bucket: str, key: str, stream: Any, length: int, **_kw: Any) -> None:
        data = stream.read(length)
        uploaded_objects.append((bucket, key, data))

    minio_mock.put_object.side_effect = _capture_put

    conn = MagicMock()
    executed_sql: list[tuple[str, tuple[Any, ...]]] = []

    async def _fetchval(sql: str, *args: Any) -> Any:
        if "INSERT INTO memories" in sql:
            return consolidated_id
        if "SELECT id FROM event_log" in sql:
            return None
        return None

    async def _execute(sql: str, *args: Any) -> str:
        executed_sql.append((sql, args))
        return "UPDATE 1"

    conn.fetchval = AsyncMock(side_effect=_fetchval)
    conn.execute = AsyncMock(side_effect=_execute)

    worker = ConsolidationWorker(
        pool=MagicMock(),
        provider=provider,
        minio_client=minio_mock,
    )
    worker._last_wrapped_dek = b"wrapped-dek-bytes"
    worker._last_dek_key_id = "dek-001"

    cluster_event_id = uuid4()
    append_mock = AsyncMock(return_value=cluster_event_id)

    with patch("nce.consolidation.append_event", append_mock):
        mem_id_out, new_depth = await worker._store_consolidated_memory(
            conn,
            namespace_id=ns_id,
            abstraction=abstraction,
            cluster_mems=[{"derivation_depth": 0}, {"derivation_depth": 1}],
            mem_ids=[src_a, src_b],
            payload_ref="507f1f77bcf86cd799439011",
            origin_event_id=cluster_event_id,
            prompt_text="<memory_content>[]</memory_content>",
        )
        await worker._update_kg(
            conn,
            namespace_id=ns_id,
            abstraction=abstraction,
            mem_ids=[src_a, src_b],
            new_depth=new_depth,
            origin_event_id=cluster_event_id,
        )

    assert mem_id_out == consolidated_id
    assert len(uploaded_objects) == 1
    bucket, key, uploaded_bytes = uploaded_objects[0]
    assert bucket == "nce-llm-payloads"
    assert key == f"{ns_id}/{cluster_event_id}.json"

    expected_bytes = canonical_json(
        {
            "prompt": "<memory_content>[]</memory_content>",
            "response": abstraction.model_dump(),
            "provider": "stub/consolidation-v1",
        }
    )
    assert uploaded_bytes == expected_bytes
    expected_digest = hashlib.sha256(expected_bytes).digest()

    append_kwargs = append_mock.await_args.kwargs
    expected_uri = f"nce-llm-payloads/{ns_id}/{cluster_event_id}.json"
    assert append_kwargs["llm_payload_uri"] == expected_uri
    assert append_kwargs["llm_payload_hash"] == expected_digest
    assert append_kwargs["params"]["llm_response_uri"] == expected_uri
    assert append_kwargs["event_id"] == cluster_event_id

    # Verify memories UPDATE and KG INSERTs received cluster_event_id
    assert any("UPDATE memories" in sql and cluster_event_id in args for sql, args in executed_sql)
    assert any(
        "INSERT INTO kg_nodes" in sql and cluster_event_id in args for sql, args in executed_sql
    )
    assert any(
        "INSERT INTO kg_edges" in sql and cluster_event_id in args for sql, args in executed_sql
    )

    # #13: Verify MemoryOrchestrator._insert_graph_nodes_and_edges passes saga_event_id to kg_nodes & kg_edges
    saga_ev_id = uuid4()
    mem_conn = MagicMock()
    mem_conn.fetch = AsyncMock(return_value=[{"id": uuid4(), "label": "NodeX"}])
    mem_conn.execute = AsyncMock(return_value="INSERT 0 1")
    mem_orch = MemoryOrchestrator(
        pg_pool=MagicMock(), mongo_client=MagicMock(), redis_client=MagicMock()
    )
    entity_obj = MagicMock(label="NodeX", entity_type="CONCEPT")
    triplet_obj = MagicMock(
        subject_label="NodeX", predicate="uses", object_label="Redis", confidence=0.9
    )
    store_req = StoreMemoryRequest(
        namespace_id=ns_id,
        agent_id="agent-kg",
        content="NodeX uses Redis",
    )

    with patch("nce.event_log.append_event", AsyncMock()):
        await mem_orch._insert_graph_nodes_and_edges(
            mem_conn,
            payload=store_req,
            entities=[entity_obj],
            node_vecs=[[0.1, 0.2]],
            triplets=[triplet_obj],
            inserted_mongo_id="507f1f77bcf86cd799439012",
            target_model_ids=[],
            memory_id=uuid4(),
            saga_id=str(uuid4()),
            saga_event_id=saga_ev_id,
        )

    kg_node_call = mem_conn.fetch.await_args
    assert "origin_event_id" in kg_node_call.args[0]
    assert kg_node_call.args[7] == saga_ev_id

    kg_edge_calls = [
        c.args for c in mem_conn.execute.await_args_list if "INSERT INTO kg_edges" in c.args[0]
    ]
    assert len(kg_edge_calls) == 1
    assert "origin_event_id" in kg_edge_calls[0][0]
    assert kg_edge_calls[0][8] == saga_ev_id


# =============================================================================
# #15 — Functional Deterministic deploy/cognitive-stub/stub_server.py
# =============================================================================


def test_wave2_item15_cognitive_stub_unit_norm_structured_json_and_nli():
    """Cognitive stub enforces 400 validation, L2=1.0 deterministic vectors, schema completions, and NLI."""
    stub = _load_module_from_path(
        "cognitive_stub_server",
        _REPO_ROOT / "deploy" / "cognitive-stub" / "stub_server.py",
    )

    # 1. Embeddings HTTP 400 validation + deterministic unit-norm vectors
    for invalid_body in [{}, {"input": ""}, {"input": []}, {"input": [123]}, {"input": 42}]:
        code, _ = stub._validate_and_build_embeddings(invalid_body)
        assert code == 400

    code_a, raw_a = stub._validate_and_build_embeddings(
        {"input": ["Postgres uses MVCC", "Redis cache"]}
    )
    code_b, raw_b = stub._validate_and_build_embeddings({"input": "Postgres uses MVCC"})
    assert code_a == 200 and code_b == 200
    parsed_a = json.loads(raw_a)
    parsed_b = json.loads(raw_b)
    vec_a0 = parsed_a["data"][0]["embedding"]
    vec_a1 = parsed_a["data"][1]["embedding"]
    vec_b0 = parsed_b["data"][0]["embedding"]

    assert vec_a0 == vec_b0
    assert vec_a0 != vec_a1
    for vec in (vec_a0, vec_a1):
        norm = math.sqrt(sum(v * v for v in vec))
        assert math.isclose(norm, 1.0, rel_tol=1e-9, abs_tol=1e-9)

    # 2. Structured chat completions: ConsolidatedAbstraction + ContradictionResult
    m1, m2 = str(uuid4()), str(uuid4())
    cluster_prompt = (
        "<memory_content>\n"
        + json.dumps(
            [
                {"memory_id": m1, "content": "Gateway uses Redis"},
                {"memory_id": m2, "content": "Worker uses Postgres"},
            ]
        )
        + "\n</memory_content>"
    )
    cons_resp = json.loads(
        stub._chat_response(
            {
                "model": "stub",
                "messages": [{"role": "user", "content": cluster_prompt}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "ConsolidatedAbstraction",
                        "schema": ConsolidatedAbstraction.model_json_schema(),
                    },
                },
            }
        )
    )
    validated_abs = ConsolidatedAbstraction.model_validate_json(
        cons_resp["choices"][0]["message"]["content"]
    )
    assert validated_abs.supporting_memory_ids == [m1, m2]
    assert validated_abs.confidence >= 0.8

    contra_prompt = (
        "<existing_memory>\nFeature X is enabled in production.\n</existing_memory>\n"
        "<new_memory>\nFeature X is disabled in production.\n</new_memory>"
    )
    contra_resp = json.loads(
        stub._chat_response(
            {
                "model": "stub",
                "messages": [{"role": "user", "content": contra_prompt}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "ContradictionResult",
                        "schema": ContradictionResult.model_json_schema(),
                    },
                },
            }
        )
    )
    validated_contra = ContradictionResult.model_validate_json(
        contra_resp["choices"][0]["message"]["content"]
    )
    assert validated_contra.is_contradiction is True
    assert validated_contra.confidence == pytest.approx(0.92)

    # 3. Heuristic NLI contradiction vs non-contradiction
    is_conf, high_score, _ = stub._heuristic_contradiction(
        "Encryption is enabled for tenant alpha",
        "Encryption is not enabled for tenant alpha",
    )
    assert is_conf is True and high_score == pytest.approx(0.92)

    is_compat, low_score, _ = stub._heuristic_contradiction(
        "Encryption is enabled for tenant alpha",
        "Backups run nightly at midnight",
    )
    assert is_compat is False and low_score == pytest.approx(0.05)


# =============================================================================
# #16 — Signing Key Bootstrap & verify_v1_launch.py X-NCE-Nonce
# =============================================================================


@pytest.mark.asyncio
async def test_wave2_item16_signing_key_bootstrap_and_verify_v1_launch_nonce(
    monkeypatch: pytest.MonkeyPatch,
):
    """_ensure_initial_signing_key seeds ONLY when COUNT(*)==0; verify_v1_launch emits X-NCE-Nonce."""
    monkeypatch.setattr(
        "nce.orchestrator.cfg.NCE_MASTER_KEY", "valid-32-byte-master-key-for-bootstrap!!"
    )

    conn = MagicMock()
    conn.transaction = MagicMock(return_value=_TxCtx())
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AcquireCtx(conn))

    engine = NCEEngine()
    engine.pg_pool = pool

    mock_rotate = AsyncMock(return_value="bootstrapped-key-01")
    with patch("nce.signing.rotate_key", mock_rotate):
        await engine._ensure_initial_signing_key()

    conn.execute.assert_awaited_once_with("SELECT pg_advisory_xact_lock(78636501)")
    mock_rotate.assert_awaited_once_with(conn)

    # Non-empty signing_keys table -> rotate_key MUST NOT be called
    mock_rotate.reset_mock()
    conn.fetchval = AsyncMock(return_value=2)
    with patch("nce.signing.rotate_key", mock_rotate):
        await engine._ensure_initial_signing_key()
    mock_rotate.assert_not_awaited()

    # verify_v1_launch._admin_hmac_headers includes unique 32-hex X-NCE-Nonce
    launch_mod = _load_module_from_path(
        "verify_v1_launch_mod",
        _REPO_ROOT / "verify_v1_launch.py",
    )
    h1 = launch_mod._admin_hmac_headers("secret-api-key", "GET", "/api/health/v1")
    h2 = launch_mod._admin_hmac_headers("secret-api-key", "GET", "/api/health/v1")
    assert "X-NCE-Nonce" in h1 and len(h1["X-NCE-Nonce"]) == 32
    assert h1["X-NCE-Nonce"] != h2["X-NCE-Nonce"]
    assert h1["Authorization"].startswith("HMAC-SHA256 ")


# =============================================================================
# #17 — Scoped Mongo in GraphOrchestrator.search_codebase & mint-token Scopes
# =============================================================================


@pytest.mark.asyncio
async def test_wave2_item17_search_codebase_tenant_mongo_isolation_and_mint_token_scopes():
    """search_codebase prevents cross-tenant Mongo code_files hydration; mint-token validates scopes."""
    ns_a = uuid4()
    ns_b = uuid4()
    mem_id = uuid4()
    foreign_oid = ObjectId()

    pg_conn = MagicMock()

    async def _fetch(sql: str, *_args: Any) -> list[dict[str, Any]]:
        if "vector_candidates" in sql:
            return [{"id": mem_id, "score": 0.03}]
        return [
            {
                "id": mem_id,
                "payload_ref": str(foreign_oid),
                "language": "python",
                "filepath": "secret.py",
                "assertion_type": "fact",
                "metadata": {},
                "content_fts": None,
                "name": "secret_fn",
                "node_type": "function",
                "start_line": 1,
                "end_line": 5,
            }
        ]

    pg_conn.fetch = AsyncMock(side_effect=_fetch)

    # Underlying Mongo collection holds ONLY tenant B's document; ScopedMongoCollection must filter by ns_a!
    stored_code_docs = [
        {"_id": foreign_oid, "namespace_id": str(ns_b), "raw_code": "TOP_SECRET_TENANT_B = 1"},
    ]

    class _FakeCursor:
        def __init__(self, filt: dict[str, Any]) -> None:
            self._filt = filt

        async def to_list(self, length: int = 100) -> list[dict[str, Any]]:
            _ = length
            req_ns = self._filt.get("namespace_id")
            return [d for d in stored_code_docs if d.get("namespace_id") == req_ns]

    raw_code_files = MagicMock()
    raw_code_files.find = MagicMock(side_effect=lambda filt, *_a, **_kw: _FakeCursor(filt))

    mongo_client = MagicMock()
    mongo_client.__getitem__.return_value = {"code_files": raw_code_files}

    graph_orch = GraphOrchestrator(
        pg_pool=MagicMock(),
        mongo_client=mongo_client,
        graph_traverser=MagicMock(),
        embed_fn=AsyncMock(return_value=[0.1, 0.2, 0.3]),
    )

    @asynccontextmanager
    async def _fake_scoped_session(_ns: str):
        yield pg_conn

    graph_orch.scoped_session = _fake_scoped_session  # type: ignore[method-assign]

    results = await graph_orch.search_codebase(
        query="secret_fn",
        namespace_id=str(ns_a),
    )
    assert len(results) == 1
    # Cross-tenant Mongo document MUST NOT leak into tenant A's excerpt!
    assert results[0]["excerpt"] == ""

    # Mint-token custom scopes & invalid-scope HTTP 400 validation
    def _make_json_request(body_dict: dict[str, Any]) -> Request:
        raw = json.dumps(body_dict).encode("utf-8")

        async def _receive() -> dict[str, Any]:
            return {"type": "http.request", "body": raw, "more_body": False}

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/plugins/mint-token",
            "headers": [(b"content-type", b"application/json")],
            "app": MagicMock(state=MagicMock(pg_pool=None)),
        }
        return Request(scope, _receive)

    with (
        patch("nce.admin_plugins.admin_state", MagicMock(engine=None)),
        patch("nce.admin_plugins._check_admin_auth", return_value=None),
    ):
        ok_resp = await admin_plugins_mint_token(
            _make_json_request(
                {
                    "namespace_id": str(ns_a),
                    "scopes": ["mcp:tools:discover", "admin"],
                }
            )
        )
        assert ok_resp.status_code == 201
        ok_data = json.loads(ok_resp.body)
        assert ok_data["scopes"] == ["mcp:tools:discover", "admin"]
        assert MINTED_MCP_TOKENS[ok_data["token_sha256"]]["scopes"] == [
            "mcp:tools:discover",
            "admin",
        ]

        bad_resp = await admin_plugins_mint_token(
            _make_json_request({"namespace_id": str(ns_a), "scopes": ["root:superuser"]})
        )
        assert bad_resp.status_code == 400
        assert "Invalid scope" in json.loads(bad_resp.body)["error"]

        empty_resp = await admin_plugins_mint_token(
            _make_json_request({"namespace_id": str(ns_a), "scopes": []})
        )
        assert empty_resp.status_code == 400


# =============================================================================
# #18 — forget_memory(crypto_shred=True) & Envelope Default Posture
# =============================================================================


@pytest.mark.asyncio
async def test_wave2_item18_forget_memory_crypto_shred_delegates_to_shred_memory():
    """forget_memory(crypto_shred=True) executes cryptographic DEK destruction; False preserves soft-forget."""
    ns_id = str(uuid4())
    mem_id = str(uuid4())

    req_default = ForgetMemoryRequest(
        namespace_id=UUID(ns_id),
        memory_id=UUID(mem_id),
        agent_id="agent-gdpr",
    )
    assert req_default.crypto_shred is False

    engine = NCEEngine()
    engine.cognitive = MagicMock()
    engine.cognitive.forget_memory = AsyncMock(
        return_value={"memory_id": mem_id, "salience_score": 0.0, "forgotten": True}
    )
    fake_receipt = {"memory_id": mem_id, "dek_destroyed": True, "verified": True}
    engine.shred_memory = AsyncMock(return_value={"status": "success", "receipt": fake_receipt})

    # Soft-forget when crypto_shred=False
    soft_out = await handle_forget_memory(
        engine,
        {"namespace_id": ns_id, "memory_id": mem_id, "agent_id": "agent-gdpr"},
    )
    assert json.loads(soft_out)["salience_score"] == 0.0
    engine.cognitive.forget_memory.assert_awaited_once_with(mem_id, "agent-gdpr", ns_id)
    engine.shred_memory.assert_not_awaited()

    # Provable cryptographic shred when crypto_shred=True
    shred_out = json.loads(
        await handle_forget_memory(
            engine,
            {
                "namespace_id": ns_id,
                "memory_id": mem_id,
                "agent_id": "agent-gdpr",
                "crypto_shred": True,
                "mcp_api_key": "stripped-transport-meta",
            },
        )
    )
    engine.shred_memory.assert_awaited_once_with(mem_id, ns_id, "agent-gdpr")
    assert shred_out["forgotten"] is True
    assert shred_out["crypto_shred"] is True
    assert shred_out["receipt"] == fake_receipt


@pytest.mark.asyncio
async def test_wave1_item3_deferred_mongo_cleanup_reaps_delayed_write_after_unpause():
    """_deferred_mongo_cleanup retries delete_one after unpause and transitions saga to rolled_back."""
    import nce.orchestrators.memory as mem_mod

    ns_id = uuid4()
    saga_id = str(uuid4())
    oid = ObjectId()
    collection = MagicMock()
    collection.delete_one = AsyncMock(
        side_effect=[
            asyncio.TimeoutError("still paused on 1st retry"),
            MagicMock(deleted_count=1),
        ]
    )
    orch = mem_mod.MemoryOrchestrator(
        pg_pool=MagicMock(),
        mongo_client=MagicMock(),
        redis_client=MagicMock(),
    )
    orch._saga_log_transition = AsyncMock()  # type: ignore[method-assign]

    with patch("nce.orchestrators.memory.asyncio.sleep", AsyncMock()):
        await orch._deferred_mongo_cleanup(
            collection=collection,
            target_oid=oid,
            namespace_id=ns_id,
            saga_id=saga_id,
        )

    assert collection.delete_one.await_count == 2
    collection.delete_one.assert_awaited_with({"_id": oid, "namespace_id": str(ns_id)})
    orch._saga_log_transition.assert_awaited_once_with(
        saga_id,
        SagaState.ROLLED_BACK,
        namespace_id=ns_id,
    )


def test_wave2_item15_local_cognitive_ssrf_and_sanitized_cluster_braces(
    monkeypatch: pytest.MonkeyPatch,
):
    """SafeAsyncClient(allow_loopback=True) allows Docker cognitive bridge IP while blocking link-local metadata, and stub parses doubled braces."""
    from nce._http_utils import UnsafeURLError, _resolve_safe_ip
    from nce.consolidation import _build_consolidation_messages

    _resolve_safe_ip.cache_clear()
    try:
        monkeypatch.setattr(
            "socket.getaddrinfo",
            lambda host, port, *a, **kw: [(2, 1, 6, "", ("172.18.0.2", 0))],
        )
        with pytest.raises(UnsafeURLError, match="non-public IP"):
            _resolve_safe_ip("cognitive", False)
        assert _resolve_safe_ip("cognitive", True) == "172.18.0.2"

        _resolve_safe_ip.cache_clear()
        monkeypatch.setattr(
            "socket.getaddrinfo",
            lambda host, port, *a, **kw: [(2, 1, 6, "", ("169.254.169.254", 0))],
        )
        with pytest.raises(UnsafeURLError, match="non-public IP"):
            _resolve_safe_ip("metadata-spoof", True)
    finally:
        _resolve_safe_ip.cache_clear()

    stub_mod = _load_module_from_path(
        "cognitive_stub_sanitized_test",
        _REPO_ROOT / "deploy" / "cognitive-stub" / "stub_server.py",
    )
    mid_1 = str(uuid4())
    mid_2 = str(uuid4())
    msgs = _build_consolidation_messages(
        json.dumps(
            [
                {"memory_id": mid_1, "payload_ref": "r1", "content": "Gateway latency is 12ms."},
                {"memory_id": mid_2, "payload_ref": "r2", "content": "Gateway latency is 13ms."},
            ]
        )
    )
    raw = stub_mod._chat_response(
        {
            "model": "stub",
            "messages": [{"role": m.role.value, "content": m.content} for m in msgs],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "ConsolidatedAbstraction", "schema": {}},
            },
        }
    )
    parsed = ConsolidatedAbstraction.model_validate_json(
        json.loads(raw)["choices"][0]["message"]["content"]
    )
    assert parsed.supporting_memory_ids == [mid_1, mid_2]
    assert "Gateway" in " ".join(parsed.key_entities) or "12ms" in parsed.abstraction
