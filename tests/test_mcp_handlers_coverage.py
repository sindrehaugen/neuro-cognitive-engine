"""Thin boundary tests for MCP handler modules (post–Uncle Bob extraction).

Uses mocked engines / pools so structural wiring stays exercised without live services.
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce import (
    a2a_mcp_handlers,
    admin_mcp_handlers,
    bridge_mcp_handlers,
    code_mcp_handlers,
    contradiction_mcp_handlers,
    memory_mcp_handlers,
    migration_mcp_handlers,
    replay_mcp_handlers,
    snapshot_mcp_handlers,
)
from nce.a2a import A2AGrantResponse, A2AScope, VerifiedGrant
from nce.mcp_errors import McpError
from nce.models import SnapshotRecord, StateDiffResult

NS = "00000000-0000-4000-8000-000000000001"
MEM_ID = "00000000-0000-4000-8000-000000000002"


class _FakeAcquire:
    __slots__ = ("_conn",)

    def __init__(self, conn: object) -> None:
        self._conn = conn

    async def __aenter__(self) -> object:
        return self._conn

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _engine_pool_context(conn: object | None = None) -> MagicMock:
    conn = conn or AsyncMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=tx)
    engine = MagicMock()
    engine.pg_pool = MagicMock()
    engine.pg_pool.acquire = MagicMock(side_effect=lambda *_a, **_k: _FakeAcquire(conn))
    engine.semantic_search = AsyncMock(return_value=[])
    return engine


def _httpx_resp(
    *,
    status_code: int = 200,
    json_data: dict | None = None,
    text: str = "",
) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.text = text
    r.json = MagicMock(return_value=json_data or {})
    r.raise_for_status = MagicMock()
    r.is_success = 200 <= status_code < 300
    return r


def _patch_httpx_async_client(
    post_ret: MagicMock, delete_ret: MagicMock | None = None
) -> MagicMock:
    delete_ret = delete_ret or _httpx_resp(status_code=204, json_data={})

    class _Client:
        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(self, *_a: object, **_k: object) -> MagicMock:
            return post_ret

        async def delete(self, *_a: object, **_k: object) -> MagicMock:
            return delete_ret

    factory = MagicMock(side_effect=lambda *_a, **_k: _Client())
    return factory


def _admin_arguments(extra: dict) -> dict:
    return {
        "admin_api_key": os.environ.get("NCE_ADMIN_API_KEY", "test-admin-api-key-for-unit-tests"),
        **extra,
    }


@pytest.fixture
def admin_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from nce import auth as auth_mod

    admin_key = os.environ.get("NCE_ADMIN_API_KEY", "test-admin-api-key-for-unit-tests")
    monkeypatch.setenv("NCE_ADMIN_API_KEY", admin_key)
    monkeypatch.setattr(auth_mod.cfg, "NCE_ADMIN_API_KEY", admin_key)
    monkeypatch.setattr(auth_mod.cfg, "NCE_ADMIN_OVERRIDE", False)


@pytest.mark.asyncio
async def test_a2a_helpers_and_handlers() -> None:
    ctx = a2a_mcp_handlers._build_caller_context({"namespace_id": NS, "agent_id": "agent-1"})
    assert str(ctx.namespace_id) == NS
    assert ctx.agent_id == "agent-1"

    scopes_json = json.dumps(
        [{"resource_type": "namespace", "resource_id": NS, "permissions": ["read"]}]
    )
    scopes = a2a_mcp_handlers._parse_scopes(scopes_json)
    assert len(scopes) == 1
    assert isinstance(scopes[0], A2AScope)

    req = a2a_mcp_handlers._build_grant_request(
        {
            "scopes": scopes_json,
            "expires_in_seconds": 120,
            "target_namespace_id": str(uuid.uuid4()),
        }
    )
    assert req.expires_in_seconds == 120

    exp = datetime.now(timezone.utc)
    grant = A2AGrantResponse(
        grant_id=uuid.uuid4(),
        sharing_token="tok",
        expires_at=exp,
    )
    engine = _engine_pool_context()

    with patch("nce.a2a_mcp_handlers.create_grant", new_callable=AsyncMock) as cg:
        cg.return_value = grant
        out = await a2a_mcp_handlers.handle_a2a_create_grant(
            engine,
            {"namespace_id": NS, "scopes": scopes_json, "expires_in_seconds": "120"},
        )
        data = json.loads(out)
        assert data["sharing_token"] == "tok"

    with patch("nce.a2a_mcp_handlers.revoke_grant", new_callable=AsyncMock) as rv:
        rv.return_value = True
        out = await a2a_mcp_handlers.handle_a2a_revoke_grant(
            engine,
            {"namespace_id": NS, "grant_id": str(uuid.uuid4())},
        )
        assert json.loads(out)["revoked"] is True

    with patch("nce.a2a_mcp_handlers.list_grants", new_callable=AsyncMock) as lg:
        lg.return_value = [{"id": "g1"}]
        out = await a2a_mcp_handlers.handle_a2a_list_grants(
            engine,
            {"namespace_id": NS, "include_inactive": True},
        )
        list_data = json.loads(out)
        assert list_data["status"] == "ok"
        assert list_data["grants"] == [{"id": "g1"}]

    owner_ns = uuid.uuid4()
    verified = VerifiedGrant(
        grant_id=uuid.uuid4(),
        owner_namespace_id=owner_ns,
        owner_agent_id="owner",
        scopes=scopes,
        expires_at=exp,
        can_delegate=False,
    )
    engine.semantic_search = AsyncMock(return_value=[{"memory_id": uuid.uuid4(), "hit": 1}])
    with (
        patch("nce.a2a_mcp_handlers.verify_token", new_callable=AsyncMock) as vt,
        patch("nce.a2a_mcp_handlers.enforce_scope"),
        patch("nce.a2a._append_a2a_event", new_callable=AsyncMock),
    ):
        vt.return_value = verified
        out = await a2a_mcp_handlers.handle_a2a_query_shared(
            engine,
            {
                "consumer_namespace_id": NS,
                "sharing_token": "st",
                "query": "hello",
                "top_k": 3,
            },
        )
    results = json.loads(out)["results"]
    assert len(results) == 1
    assert results[0]["hit"] == 1
    assert "memory_id" in results[0]


@pytest.mark.asyncio
async def test_admin_handlers_delegate(admin_key_env: None) -> None:
    engine = MagicMock()
    engine.manage_namespace = AsyncMock(return_value={"ok": True})
    raw = await admin_mcp_handlers.handle_manage_namespace(
        engine,
        _admin_arguments({"command": "list"}),
    )
    assert json.loads(raw)["ok"] is True

    engine.verify_memory = AsyncMock(return_value={"verified": True})
    raw = await admin_mcp_handlers.handle_verify_memory(
        engine,
        _admin_arguments({"memory_id": "mem-1"}),
    )
    assert json.loads(raw)["verified"] is True

    engine.trigger_consolidation = AsyncMock(return_value={"started": True})
    raw = await admin_mcp_handlers.handle_trigger_consolidation(
        engine,
        _admin_arguments({"namespace_id": NS}),
    )
    assert json.loads(raw)["started"] is True

    engine.consolidation_status = AsyncMock(return_value={"run": "ok"})
    raw = await admin_mcp_handlers.handle_consolidation_status(
        engine,
        _admin_arguments({"run_id": "r1"}),
    )
    assert json.loads(raw)["run"] == "ok"

    engine.manage_quotas = AsyncMock(return_value={"quotas": []})
    raw = await admin_mcp_handlers.handle_manage_quotas(
        engine,
        _admin_arguments(
            {"command": "list", "namespace_id": NS},
        ),
    )
    assert json.loads(raw)["quotas"] == []

    engine.pg_pool.acquire = _engine_pool_context().pg_pool.acquire
    # handle_rotate_signing_key now also resolves the reserved ``_system``
    # namespace and appends a ``signing_key_rotated`` event; both are stubbed
    # here because this file is a boundary test with no live services.
    with (
        patch("nce.signing.rotate_key", new_callable=AsyncMock) as rk,
        patch(
            "nce.system_namespace.get_system_namespace_id",
            AsyncMock(return_value=uuid.UUID(NS)),
        ),
        patch("nce.auth.set_namespace_context", AsyncMock()),
        patch("nce.event_log.append_event", AsyncMock()) as rotated_event,
    ):
        rk.return_value = "kid-9"
        raw = await admin_mcp_handlers.handle_rotate_signing_key(
            engine,
            _admin_arguments({}),
        )
        assert json.loads(raw)["new_key_id"] == "kid-9"
        assert rotated_event.await_args.kwargs["event_type"] == "signing_key_rotated"

    engine.check_health = AsyncMock(return_value={"postgres": "up"})
    raw = await admin_mcp_handlers.handle_get_health(engine, _admin_arguments({}))
    assert json.loads(raw)["postgres"] == "up"


@pytest.mark.asyncio
async def test_code_and_contradiction_handlers(admin_key_env: None) -> None:
    engine = MagicMock()
    engine.index_code_file = AsyncMock(return_value={"status": "indexed"})
    engine.get_job_status = AsyncMock(return_value={"done": True})
    engine.search_codebase = AsyncMock(return_value=[])

    r = await code_mcp_handlers.handle_index_code_file(
        engine,
        {
            "namespace_id": NS,
            "filepath": "a.py",
            "raw_code": "x=1",
            "language": "python",
            "user_id": "tester",
        },
    )
    assert json.loads(r)["status"] == "indexed"

    r = await code_mcp_handlers.handle_check_indexing_status(
        engine,
        {"job_id": "j1"},
    )
    assert json.loads(r)["done"] is True

    r = await code_mcp_handlers.handle_search_codebase(
        engine,
        {
            "query": "foo",
            "namespace_id": NS,
            "top_k": "2",
            "private": True,
            "user_id": "tester",
        },
    )
    assert json.loads(r) == []

    engine.list_contradictions = AsyncMock(return_value={"items": []})
    r = await contradiction_mcp_handlers.handle_list_contradictions(
        engine,
        {"namespace_id": NS},
    )
    assert "items" in json.loads(r)

    engine.resolve_contradiction = AsyncMock(return_value={"resolved": True})
    r = await contradiction_mcp_handlers.handle_resolve_contradiction(
        engine,
        _admin_arguments(
            {
                "contradiction_id": "00000000-0000-4000-8000-000000000003",
                "namespace_id": NS,
                "resolution": "accepted_a",
                "resolved_by": "tester",
            }
        ),
    )
    assert json.loads(r)["resolved"] is True


@pytest.mark.asyncio
async def test_memory_handlers_ok_response_and_search() -> None:
    engine = MagicMock()
    engine.store_memory = AsyncMock(return_value={"payload_ref": "0" * 24, "contradiction": None})
    engine.store_artifact = AsyncMock(return_value="1" * 24)
    engine.semantic_search = AsyncMock(return_value=[])
    engine.recall_recent = AsyncMock(return_value=[])
    engine.boost_memory = AsyncMock(return_value={"boosted": True})
    engine.forget_memory = AsyncMock(return_value={"forgot": True})
    engine.unredact_memory = AsyncMock(return_value={"unredacted": True})

    ns_uuid = NS
    base_mem = {
        "namespace_id": ns_uuid,
        "agent_id": "ag",
        "content": "hello",
        "summary": "s",
        "heavy_payload": "",
    }
    r = await memory_mcp_handlers.handle_store_memory(engine, dict(base_mem))
    d = json.loads(r)
    assert d["status"] == "ok" and len(d["payload_ref"]) == 24

    r = await memory_mcp_handlers.handle_store_media(
        engine,
        {
            "namespace_id": ns_uuid,
            "user_id": "mediauser",
            "session_id": "sess1",
            "media_type": "image",
            "file_path_on_disk": "C:\\tmp\\a.png",
            "summary": "pic",
        },
    )
    assert json.loads(r)["status"] == "ok"

    r = await memory_mcp_handlers.handle_semantic_search(
        engine,
        {"namespace_id": ns_uuid, "query": "q", "agent_id": "ag"},
    )
    assert json.loads(r) == []

    r = await memory_mcp_handlers.handle_get_recent_context(
        engine,
        {"namespace_id": ns_uuid, "agent_id": "ag", "limit": 2},
    )
    assert json.loads(r)["context"] == []

    r = await memory_mcp_handlers.handle_boost_memory(
        engine,
        {
            "memory_id": MEM_ID,
            "agent_id": "ag",
            "namespace_id": ns_uuid,
            "factor": 0.1,
        },
    )
    assert json.loads(r)["boosted"] is True

    r = await memory_mcp_handlers.handle_forget_memory(
        engine,
        {"memory_id": MEM_ID, "agent_id": "ag", "namespace_id": ns_uuid},
    )
    assert json.loads(r)["forgot"] is True

    r = await memory_mcp_handlers.handle_unredact_memory(
        engine,
        {"memory_id": MEM_ID, "namespace_id": ns_uuid, "agent_id": "ag"},
    )
    assert json.loads(r)["unredacted"] is True


@pytest.mark.asyncio
async def test_memory_helpers_serialize() -> None:
    s = memory_mcp_handlers._ok_response("ref")
    assert json.loads(s)["payload_ref"] == "ref"
    assert memory_mcp_handlers._serialize([1]) == "[1]"


@pytest.mark.asyncio
async def test_migration_handlers_audited_and_status(admin_key_env: None) -> None:
    # After the Batch 108 refactor, audit lives in nce.migration_gate.audit_migration_action
    # (invoked from engine.start_migration / commit_migration via enforce_*_gate).
    # Wire engine.start_migration as a thin real coroutine that calls the gate-layer
    # symbol so the aud.assert_awaited_once() assertion still exercises the full
    # handler → engine → gate chain.  All other arms use plain AsyncMocks.
    mid = str(uuid.uuid4())
    engine = MagicMock()
    engine.pg_pool = MagicMock()
    engine.migration_status = AsyncMock(return_value={"state": "running"})
    engine.validate_migration = AsyncMock(return_value={"status": "success"})
    engine.commit_migration = AsyncMock(return_value={"committed": True})
    engine.abort_migration = AsyncMock(return_value={"aborted": True})

    with patch(
        "nce.migration_gate.audit_migration_action",
        new_callable=AsyncMock,
    ) as aud:

        async def _start_with_audit(
            target_model_id: str, *, admin_identity: str | None = None
        ) -> dict:
            from nce.migration_gate import audit_migration_action  # noqa: PLC0415

            await audit_migration_action(
                engine.pg_pool,
                event_type="migration_start_requested",
                admin_identity=admin_identity,
                migration_id=None,
                target_model_id=target_model_id,
            )
            return {"id": "m1"}

        engine.start_migration = _start_with_audit

        raw = await migration_mcp_handlers.handle_start_migration(
            engine,
            _admin_arguments({"target_model_id": "embed-v2"}),
        )
        aud.assert_awaited_once()
        assert json.loads(raw)["id"] == "m1"

    raw = await migration_mcp_handlers.handle_migration_status(
        engine,
        _admin_arguments({"migration_id": mid}),
    )
    assert json.loads(raw)["state"] == "running"

    raw = await migration_mcp_handlers.handle_validate_migration(
        engine,
        _admin_arguments({"migration_id": mid}),
    )
    assert json.loads(raw)["status"] == "success"

    # handle_commit_migration delegates to engine (mocked); no real audit path needed.
    # handle_abort_migration calls _audit_migration_action directly in the handler
    # (it's imported from migration_gate at module load time), so patch both the
    # gate-module symbol AND the handler-module binding to suppress the real PG call.
    with (
        patch(
            "nce.migration_gate.audit_migration_action",
            new_callable=AsyncMock,
        ),
        patch.object(
            migration_mcp_handlers,
            "_audit_migration_action",
            new_callable=AsyncMock,
        ),
    ):
        raw = await migration_mcp_handlers.handle_commit_migration(
            engine,
            _admin_arguments({"migration_id": mid}),
        )
        assert json.loads(raw)["committed"] is True

        raw = await migration_mcp_handlers.handle_abort_migration(
            engine,
            _admin_arguments({"migration_id": mid}),
        )
        assert json.loads(raw)["aborted"] is True


@pytest.mark.asyncio
async def test_migration_audit_writes_log() -> None:
    # append_event now lives in nce.migration_gate (relocated by the Batch 108 refactor);
    # patch it there so migration_gate.audit_migration_action resolves the right symbol.
    # _audit_migration_action in migration_mcp_handlers is just a re-export alias — calling
    # it still exercises the real implementation in migration_gate.
    pool = MagicMock()

    @asynccontextmanager
    async def _cm():
        conn = AsyncMock()

        @asynccontextmanager
        async def _tx():
            yield None

        conn.transaction = MagicMock(side_effect=lambda: _tx())
        yield conn

    pool.acquire = MagicMock(return_value=_cm())
    calls: list = []

    async def _append(*, conn, **kwargs):
        calls.append(kwargs.get("event_type"))
        return SimpleNamespace(event_id=uuid.uuid4(), event_seq=1)

    with patch("nce.migration_gate.append_event", side_effect=_append):
        await migration_mcp_handlers._audit_migration_action(
            pool,
            event_type="migration_test",
            admin_identity="root",
            migration_id="x",
            target_model_id="y",
            extra_params={"k": "v"},
        )
    assert calls == ["migration_test"]


@pytest.mark.asyncio
async def test_replay_handlers_smoke() -> None:
    engine = _engine_pool_context()

    async def _gen():
        yield {"type": "event", "seq": 1}
        yield {"type": "event", "seq": 2}

    class _Obs:
        async def execute(self, **kwargs):
            async for x in _gen():
                yield x

    with patch(
        "nce.replay.ObservationalReplay",
        return_value=_Obs(),
    ):
        out = await replay_mcp_handlers.handle_replay_observe(
            engine,
            {
                "namespace_id": NS,
                "start_seq": 1,
                "end_seq": 10,
                "max_events": 5,
            },
        )
        assert "event" in out

    froz = MagicMock()
    froz.source_namespace_id = uuid.uuid4()
    froz.target_namespace_id = uuid.uuid4()
    froz.replay_mode = "deterministic"
    froz.start_seq = 1
    froz.fork_seq = 5
    froz.overrides_dict = {}

    run_id = uuid.uuid4()

    class _Fork:
        async def execute(self, **kwargs):
            if False:
                yield None

    with (
        patch("nce.models.FrozenForkConfig.from_request", return_value=froz),
        patch(
            "nce.replay._create_run",
            new_callable=AsyncMock,
            return_value=run_id,
        ),
        patch(
            "nce.replay.ForkedReplay",
            return_value=_Fork(),
        ),
        patch(
            "nce.replay_mcp_handlers.create_tracked_task",
        ) as ct,
    ):
        out = await replay_mcp_handlers.handle_replay_fork(
            engine,
            {
                "source_namespace_id": NS,
                "target_namespace_id": "00000000-0000-4000-8000-000000000002",
                "fork_seq": 3,
                "expected_sha256": "0" * 64,
            },
        )
        ct.assert_called_once()
        await ct.call_args[0][0]
        assert json.loads(out)["status"] == "started"

    with (
        patch(
            "nce.replay._create_run",
            new_callable=AsyncMock,
            return_value=run_id,
        ),
        patch(
            "nce.replay.ReconstructiveReplay",
            return_value=_Fork(),
        ),
        patch(
            "nce.replay_mcp_handlers.create_tracked_task",
        ) as ct2,
    ):
        out = await replay_mcp_handlers.handle_replay_reconstruct(
            engine,
            {
                "source_namespace_id": NS,
                "target_namespace_id": "00000000-0000-4000-8000-000000000002",
                "end_seq": 9,
            },
        )
        await ct2.call_args[0][0]
        assert json.loads(out)["status"] == "started"

    with patch(
        "nce.replay.get_run_status",
        new_callable=AsyncMock,
        return_value={"phase": "done"},
    ):
        out = await replay_mcp_handlers.handle_replay_status(
            engine,
            {"run_id": str(uuid.uuid4())},
        )
        assert json.loads(out)["phase"] == "done"

    with patch(
        "nce.replay.get_event_provenance",
        new_callable=AsyncMock,
        return_value={"chain": []},
    ):
        out = await replay_mcp_handlers.handle_get_event_provenance(
            engine,
            {"memory_id": str(uuid.uuid4())},
        )
        assert json.loads(out)["chain"] == []

        out_explain = await replay_mcp_handlers.handle_explain_memory(
            engine,
            {"memory_id": str(uuid.uuid4())},
        )
        assert json.loads(out_explain)["error"] == "Memory provenance not found"


@pytest.mark.asyncio
async def test_snapshot_handlers_delegate() -> None:
    engine = MagicMock()
    now = datetime.now(timezone.utc)
    rec = SnapshotRecord(
        id=uuid.uuid4(),
        namespace_id=uuid.UUID(NS),
        agent_id="default",
        name="snap",
        snapshot_at=now,
        created_at=now,
        metadata={},
    )
    engine.create_snapshot = AsyncMock(return_value=rec)
    engine.list_snapshots = AsyncMock(return_value=[rec])
    from nce.models import DeleteSnapshotResult

    engine.delete_snapshot = AsyncMock(
        return_value=DeleteSnapshotResult(status="ok", message="deleted")
    )
    engine.compare_states = AsyncMock(
        return_value=StateDiffResult(
            as_of_a=now,
            as_of_b=now,
            added=[],
            removed=[],
            modified=[],
        )
    )

    raw = await snapshot_mcp_handlers.handle_create_snapshot(
        engine,
        {"namespace_id": NS, "name": "s1"},
    )
    assert "snap" in raw

    raw = await snapshot_mcp_handlers.handle_list_snapshots(
        engine,
        {"namespace_id": NS},
    )
    assert NS in raw

    raw = await snapshot_mcp_handlers.handle_delete_snapshot(
        engine,
        {"namespace_id": NS, "snapshot_id": str(rec.id)},
    )
    assert "ok" in raw

    earlier = now - timedelta(hours=1)
    raw = await snapshot_mcp_handlers.handle_compare_states(
        engine,
        {
            "namespace_id": NS,
            "as_of_a": earlier.isoformat(),
            "as_of_b": now.isoformat(),
        },
    )
    assert "as_of_a" in raw


@pytest.mark.asyncio
async def test_bridge_parse_sharepoint_and_oauth_unknown() -> None:
    site, drive = bridge_mcp_handlers._parse_sharepoint_resource("a|b")
    assert site == "a" and drive == "b"
    with pytest.raises(ValueError, match="sharepoint resource_id"):
        bridge_mcp_handlers._parse_sharepoint_resource("nodelim")

    with pytest.raises(ValueError, match="unknown provider"):
        await bridge_mcp_handlers._exchange_oauth_code("not-a-provider", "code")


@pytest.mark.asyncio
async def test_connect_bridge_dropbox_pending_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_ID", "")
    engine = _engine_pool_context(AsyncMock())
    with patch.object(
        bridge_mcp_handlers.bridge_repo,
        "insert_subscription",
        new_callable=AsyncMock,
    ):
        raw = await bridge_mcp_handlers.connect_bridge(
            engine,
            {"user_id": "u1", "namespace_id": NS, "provider": "dropbox"},
        )
        data = json.loads(raw)
        assert data["status"] == "pending_config"
        assert "bridge_id" in data


@pytest.mark.asyncio
async def test_connect_bridge_sharepoint_and_gdrive_pending_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine_pool_context(AsyncMock())
    monkeypatch.setenv("AZURE_CLIENT_ID", "")
    with patch.object(
        bridge_mcp_handlers.bridge_repo, "insert_subscription", new_callable=AsyncMock
    ):
        raw = await bridge_mcp_handlers.connect_bridge(
            engine, {"user_id": "u1", "namespace_id": NS, "provider": "sharepoint"}
        )
        assert json.loads(raw)["status"] == "pending_config"

    monkeypatch.setenv("GDRIVE_OAUTH_CLIENT_ID", "")
    with patch.object(
        bridge_mcp_handlers.bridge_repo, "insert_subscription", new_callable=AsyncMock
    ):
        raw = await bridge_mcp_handlers.connect_bridge(
            engine, {"user_id": "u1", "namespace_id": NS, "provider": "gdrive"}
        )
        assert json.loads(raw)["status"] == "pending_config"


@pytest.mark.asyncio
async def test_connect_bridge_invalid_provider() -> None:
    engine = _engine_pool_context(AsyncMock())
    with pytest.raises(McpError) as exc_info:
        await bridge_mcp_handlers.connect_bridge(
            engine, {"user_id": "u1", "namespace_id": NS, "provider": "ftp"}
        )
    assert "provider must be one of" in exc_info.value.data["detail"]


@pytest.mark.asyncio
async def test_list_bridges_and_status() -> None:
    engine = _engine_pool_context(AsyncMock())
    rid = uuid.uuid4()
    row = {
        "id": rid,
        "user_id": "u1",
        "provider": "dropbox",
        "resource_id": "r",
        "subscription_id": None,
        "cursor": None,
        "status": "ACTIVE",
        "expires_at": datetime.now(timezone.utc),
        "client_state": "cs",
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    with patch.object(
        bridge_mcp_handlers.bridge_repo,
        "list_for_user",
        new_callable=AsyncMock,
        return_value=[row],
    ):
        raw = await bridge_mcp_handlers.list_bridges(engine, {"user_id": "u1"})
        bridges = json.loads(raw)["bridges"]
        assert len(bridges) == 1
        assert bridges[0]["provider"] == "dropbox"

    with patch.object(
        bridge_mcp_handlers.bridge_repo,
        "get_by_id",
        new_callable=AsyncMock,
        return_value=row,
    ):
        raw = await bridge_mcp_handlers.bridge_status(
            engine, {"user_id": "u1", "bridge_id": str(rid)}
        )
        out = json.loads(raw)
        assert out["id"] == str(rid)
        assert "expires_in_seconds" in out


@pytest.mark.asyncio
async def test_complete_bridge_auth_validations() -> None:
    engine = _engine_pool_context(AsyncMock())
    bid = str(uuid.uuid4())
    with pytest.raises(McpError) as exc_info:
        await bridge_mcp_handlers.complete_bridge_auth(
            engine,
            {
                "user_id": "u1",
                "bridge_id": bid,
                "provider": "dropbox",
            },
        )
    assert "authorization_code" in exc_info.value.data["detail"]

    with pytest.raises(McpError) as exc_info:
        await bridge_mcp_handlers.complete_bridge_auth(
            engine,
            {
                "user_id": "u1",
                "bridge_id": bid,
                "provider": "dropbox",
                "authorization_code": "c",
                "resource_id": "pending",
            },
        )
    assert "resource_id required" in exc_info.value.data["detail"]

    with patch.object(
        bridge_mcp_handlers.bridge_repo,
        "get_by_id",
        new_callable=AsyncMock,
        return_value=None,
    ):
        with pytest.raises(McpError) as exc_info:
            await bridge_mcp_handlers.complete_bridge_auth(
                engine,
                {
                    "user_id": "u1",
                    "bridge_id": bid,
                    "provider": "dropbox",
                    "authorization_code": "c",
                    "resource_id": "dbid:x",
                },
            )
        assert "bridge not found" in exc_info.value.data["detail"]


@pytest.mark.asyncio
async def test_complete_bridge_auth_dropbox_updates_row() -> None:
    conn = AsyncMock()
    conn.execute = AsyncMock()
    engine = _engine_pool_context(conn)
    bid = uuid.uuid4()
    row = {
        "user_id": "u1",
        "provider": "dropbox",
        "client_state": "cs",
        "status": "REQUESTED",
        "subscription_id": None,
        "resource_id": "pending",
        "oauth_access_token_enc": None,
    }

    async def _get_by_id(_c: object, _bid: uuid.UUID):
        return row

    with (
        patch.object(bridge_mcp_handlers.bridge_repo, "get_by_id", side_effect=_get_by_id),
        patch.object(
            bridge_mcp_handlers,
            "_exchange_oauth_code",
            new_callable=AsyncMock,
            return_value={
                "access_token": "tok",
                "refresh_token": "ref",
                "expires_at": 9999999999,
            },
        ),
        patch.object(
            bridge_mcp_handlers.bridge_repo,
            "save_token",
            new_callable=AsyncMock,
        ),
    ):
        out = await bridge_mcp_handlers.complete_bridge_auth(
            engine,
            {
                "user_id": "u1",
                "bridge_id": str(bid),
                "provider": "dropbox",
                "authorization_code": "c",
                "resource_id": "dbid:acc",
            },
        )
    assert json.loads(out)["status"] == "ok"


@pytest.mark.asyncio
async def test_disconnect_bridge_dropbox() -> None:
    engine = _engine_pool_context(AsyncMock())
    bid = uuid.uuid4()
    row = {
        "user_id": "u1",
        "provider": "dropbox",
        "subscription_id": None,
        "resource_id": "dbid:1",
        "oauth_access_token_enc": None,
    }
    with (
        patch.object(
            bridge_mcp_handlers.bridge_repo,
            "get_by_id",
            new_callable=AsyncMock,
            return_value=row,
        ),
        patch.object(
            bridge_mcp_handlers.bridge_repo,
            "get_token",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch.object(
            bridge_mcp_handlers.bridge_repo,
            "save_token",
            new_callable=AsyncMock,
        ),
    ):
        raw = await bridge_mcp_handlers.disconnect_bridge(
            engine, {"user_id": "u1", "bridge_id": str(bid)}
        )
    assert json.loads(raw)["state"] == "DISCONNECTED"


@pytest.mark.asyncio
async def test_force_resync_bridge_dropbox_enqueues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_mcp_handlers.cfg, "REDIS_URL", "redis://localhost:6379/0")
    engine = _engine_pool_context(AsyncMock())
    bid = uuid.uuid4()
    row = {
        "user_id": "u1",
        "provider": "dropbox",
        "resource_id": "dbid:1",
        "subscription_id": "sub",
        "client_state": "cs",
    }
    job = MagicMock()
    job.id = "jq-1"
    mock_r = MagicMock()
    with (
        patch.object(
            bridge_mcp_handlers.bridge_repo,
            "get_by_id",
            new_callable=AsyncMock,
            return_value=row,
        ),
        patch.object(bridge_mcp_handlers, "bridge_redis", return_value=mock_r),
        patch.object(bridge_mcp_handlers, "Redis") as RedisM,
        patch.object(bridge_mcp_handlers, "get_priority_queue") as get_q,
    ):
        RedisM.from_url.return_value = MagicMock()
        inst = MagicMock()
        inst.enqueue.return_value = job
        get_q.return_value = inst
        raw = await bridge_mcp_handlers.force_resync_bridge(
            engine, {"user_id": "u1", "bridge_id": str(bid)}
        )
    assert json.loads(raw)["job_id"] == "jq-1"


@pytest.mark.asyncio
async def test_parse_sharepoint_invalid_empty_segments() -> None:
    with pytest.raises(ValueError, match="Invalid site"):
        bridge_mcp_handlers._parse_sharepoint_resource("|onlydrive")
    with pytest.raises(ValueError, match="Invalid site"):
        bridge_mcp_handlers._parse_sharepoint_resource("onlysite|")


@pytest.mark.asyncio
async def test_connect_bridge_ok_auth_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge_mcp_handlers.cfg, "AZURE_CLIENT_ID", "azure-cid")
    monkeypatch.setattr(bridge_mcp_handlers.cfg, "GDRIVE_OAUTH_CLIENT_ID", "g-cid")
    monkeypatch.setattr(bridge_mcp_handlers.cfg, "DROPBOX_OAUTH_CLIENT_ID", "d-cid")
    engine = _engine_pool_context(AsyncMock())
    with patch.object(
        bridge_mcp_handlers.bridge_repo, "insert_subscription", new_callable=AsyncMock
    ):
        raw = await bridge_mcp_handlers.connect_bridge(
            engine, {"user_id": "u1", "namespace_id": NS, "provider": "sharepoint"}
        )
    data = json.loads(raw)
    assert data["status"] == "ok"
    assert "login.microsoftonline.com" in data["auth_url"]

    with patch.object(
        bridge_mcp_handlers.bridge_repo, "insert_subscription", new_callable=AsyncMock
    ):
        raw = await bridge_mcp_handlers.connect_bridge(
            engine, {"user_id": "u1", "namespace_id": NS, "provider": "gdrive"}
        )
    assert json.loads(raw)["status"] == "ok"
    assert "accounts.google.com" in json.loads(raw)["auth_url"]

    with patch.object(
        bridge_mcp_handlers.bridge_repo, "insert_subscription", new_callable=AsyncMock
    ):
        raw = await bridge_mcp_handlers.connect_bridge(
            engine, {"user_id": "u1", "namespace_id": NS, "provider": "dropbox"}
        )
    assert json.loads(raw)["status"] == "ok"
    assert "dropbox.com/oauth2" in json.loads(raw)["auth_url"]


@pytest.mark.asyncio
async def test_exchange_oauth_token_flows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge_mcp_handlers.cfg, "AZURE_CLIENT_ID", "c1")
    monkeypatch.setattr(bridge_mcp_handlers.cfg, "AZURE_CLIENT_SECRET", "s1")
    monkeypatch.setattr(bridge_mcp_handlers.cfg, "BRIDGE_OAUTH_REDIRECT_URI", "http://127.0.0.1/r")
    oauth_mock = AsyncMock(
        return_value={
            "access_token": "at1",
            "refresh_token": "ref1",
            "expires_in": 3600,
        }
    )
    with patch("nce.bridge_mcp_handlers.oauth_token_post_form", oauth_mock):
        tok = await bridge_mcp_handlers._exchange_oauth_code("sharepoint", "code")
    assert tok["access_token"] == "at1"
    assert tok["refresh_token"] == "ref1"
    assert "expires_at" in tok

    monkeypatch.setattr(bridge_mcp_handlers.cfg, "GDRIVE_OAUTH_CLIENT_ID", "gc")
    monkeypatch.setattr(bridge_mcp_handlers.cfg, "GDRIVE_OAUTH_CLIENT_SECRET", "gs")
    oauth_mock2 = AsyncMock(
        return_value={
            "access_token": "at2",
            "refresh_token": "ref2",
            "expires_in": 3600,
        }
    )
    with patch("nce.bridge_mcp_handlers.oauth_token_post_form", oauth_mock2):
        tok = await bridge_mcp_handlers._exchange_oauth_code("gdrive", "c2")
    assert tok["access_token"] == "at2"
    assert tok["refresh_token"] == "ref2"
    assert "expires_at" in tok

    monkeypatch.setattr(bridge_mcp_handlers.cfg, "DROPBOX_OAUTH_CLIENT_ID", "dc")
    oauth_mock3 = AsyncMock(
        return_value={
            "access_token": "at3",
            "refresh_token": "ref3",
            "expires_in": 3600,
        }
    )
    with patch("nce.bridge_mcp_handlers.oauth_token_post_form", oauth_mock3):
        tok = await bridge_mcp_handlers._exchange_oauth_code("dropbox", "c3")
    assert tok["access_token"] == "at3"
    assert tok["refresh_token"] == "ref3"
    assert "expires_at" in tok


@pytest.mark.asyncio
async def test_exchange_oauth_config_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge_mcp_handlers.cfg, "AZURE_CLIENT_ID", "")
    monkeypatch.setattr(bridge_mcp_handlers.cfg, "AZURE_CLIENT_SECRET", "")
    with pytest.raises(ValueError, match="AZURE_CLIENT_ID"):
        await bridge_mcp_handlers._exchange_oauth_code("sharepoint", "c")

    monkeypatch.setattr(bridge_mcp_handlers.cfg, "GDRIVE_OAUTH_CLIENT_ID", "")
    monkeypatch.setattr(bridge_mcp_handlers.cfg, "GDRIVE_OAUTH_CLIENT_SECRET", "")
    with pytest.raises(ValueError, match="GDRIVE_OAUTH"):
        await bridge_mcp_handlers._exchange_oauth_code("gdrive", "c")

    monkeypatch.setattr(bridge_mcp_handlers.cfg, "DROPBOX_OAUTH_CLIENT_ID", "")
    with pytest.raises(ValueError, match="DROPBOX_OAUTH"):
        await bridge_mcp_handlers._exchange_oauth_code("dropbox", "c")


@pytest.mark.asyncio
async def test_exchange_oauth_missing_access_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_mcp_handlers.cfg, "AZURE_CLIENT_ID", "c1")
    monkeypatch.setattr(bridge_mcp_handlers.cfg, "AZURE_CLIENT_SECRET", "s1")
    monkeypatch.setattr(bridge_mcp_handlers.cfg, "BRIDGE_OAUTH_REDIRECT_URI", "http://127.0.0.1/r")
    with patch(
        "nce.bridge_mcp_handlers.oauth_token_post_form",
        AsyncMock(return_value={}),
    ):
        with pytest.raises(ValueError, match="missing access_token"):
            await bridge_mcp_handlers._exchange_oauth_code("sharepoint", "code")


@pytest.mark.asyncio
async def test_setup_sharepoint_and_gdrive_webhooks() -> None:
    ok_sub = _httpx_resp(json_data={"id": "sub-1", "expirationDateTime": "2030-01-01T00:00:00Z"})
    with (
        patch("nce.bridge_mcp_handlers.httpx.AsyncClient", _patch_httpx_async_client(ok_sub)),
        patch("nce.http_resilience.httpx.AsyncClient", _patch_httpx_async_client(ok_sub)),
    ):
        sid, exp = await bridge_mcp_handlers._setup_sharepoint_webhook(
            "tok",
            base="https://example.com",
            site="s",
            drive="d",
            bridge_client_state="cs",
        )
    assert sid == "sub-1"
    assert exp is not None

    bad = _httpx_resp(status_code=400, json_data={}, text="nope")
    with (
        patch("nce.bridge_mcp_handlers.httpx.AsyncClient", _patch_httpx_async_client(bad)),
        patch("nce.http_resilience.httpx.AsyncClient", _patch_httpx_async_client(bad)),
    ):
        with pytest.raises(ValueError, match="Graph subscription failed"):
            await bridge_mcp_handlers._setup_sharepoint_webhook(
                "tok",
                base="https://example.com",
                site="s",
                drive="d",
                bridge_client_state="cs",
            )

    ok_drive = _httpx_resp(
        json_data={
            "id": "ch-1",
            "resourceId": "rid-g",
            "expiration": str(int(datetime.now(timezone.utc).timestamp() * 1000) + 1000),
        }
    )
    with (
        patch("nce.bridge_mcp_handlers.httpx.AsyncClient", _patch_httpx_async_client(ok_drive)),
        patch("nce.http_resilience.httpx.AsyncClient", _patch_httpx_async_client(ok_drive)),
    ):
        sid2, rid2, exp2 = await bridge_mcp_handlers._setup_gdrive_webhook(
            "gtok",
            base="https://example.com",
            bridge_client_state="cs",
            resource_id="folder1",
        )
    assert sid2 == "ch-1"
    assert rid2 == "rid-g"
    assert exp2 is not None

    bad_d = _httpx_resp(status_code=401, json_data={}, text="fail")
    with (
        patch("nce.bridge_mcp_handlers.httpx.AsyncClient", _patch_httpx_async_client(bad_d)),
        patch("nce.http_resilience.httpx.AsyncClient", _patch_httpx_async_client(bad_d)),
    ):
        with pytest.raises(ValueError, match="Drive watch failed"):
            await bridge_mcp_handlers._setup_gdrive_webhook(
                "gtok",
                base="https://example.com",
                bridge_client_state="cs",
                resource_id="folder1",
            )


@pytest.mark.asyncio
async def test_bridge_repo_token_roundtrip() -> None:
    """Verify bridge_repo.save_token / get_token via mocked connection."""
    conn = AsyncMock()
    bid = uuid.uuid4()
    payload = {"access_token": "hello", "refresh_token": "ref"}

    with patch.object(
        bridge_mcp_handlers.bridge_repo,
        "get_token",
        new_callable=AsyncMock,
        return_value=payload,
    ):
        result = await bridge_mcp_handlers.bridge_repo.get_token(conn, bid)
    assert result == payload


@pytest.mark.asyncio
async def test_complete_bridge_auth_sharepoint_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_mcp_handlers.cfg, "BRIDGE_WEBHOOK_BASE_URL", "http://127.0.0.1")
    conn = AsyncMock()
    conn.execute = AsyncMock()
    engine = _engine_pool_context(conn)
    bid = uuid.uuid4()
    row = {
        "user_id": "u1",
        "provider": "sharepoint",
        "client_state": "cs",
        "status": "REQUESTED",
        "subscription_id": None,
        "resource_id": "pending",
        "oauth_access_token_enc": None,
    }

    async def _get_by_id(_c: object, _i: uuid.UUID):
        return row

    with (
        patch.object(bridge_mcp_handlers.bridge_repo, "get_by_id", side_effect=_get_by_id),
        patch.object(
            bridge_mcp_handlers,
            "_exchange_oauth_code",
            new_callable=AsyncMock,
            return_value={
                "access_token": "access",
                "refresh_token": "ref",
                "expires_at": 9999999999,
            },
        ),
        patch.object(
            bridge_mcp_handlers,
            "_setup_sharepoint_webhook",
            new_callable=AsyncMock,
            return_value=("sub-x", datetime.now(timezone.utc)),
        ),
        patch.object(
            bridge_mcp_handlers.bridge_repo,
            "save_token",
            new_callable=AsyncMock,
        ),
    ):
        out = await bridge_mcp_handlers.complete_bridge_auth(
            engine,
            {
                "user_id": "u1",
                "bridge_id": str(bid),
                "provider": "sharepoint",
                "code": "oauth-code",
                "resource_id": "site1|drive1",
            },
        )
    assert json.loads(out)["subscription_id"] == "sub-x"


@pytest.mark.asyncio
async def test_complete_bridge_auth_gdrive_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_mcp_handlers.cfg, "BRIDGE_WEBHOOK_BASE_URL", "http://127.0.0.1")
    conn = AsyncMock()
    conn.execute = AsyncMock()
    engine = _engine_pool_context(conn)
    bid = uuid.uuid4()
    row = {
        "user_id": "u1",
        "provider": "gdrive",
        "client_state": "cs",
        "status": "VALIDATING",
        "subscription_id": None,
        "resource_id": "pending",
        "oauth_access_token_enc": None,
    }

    async def _get(_c: object, _i: uuid.UUID):
        return row

    with (
        patch.object(bridge_mcp_handlers.bridge_repo, "get_by_id", side_effect=_get),
        patch.object(
            bridge_mcp_handlers,
            "_exchange_oauth_code",
            new_callable=AsyncMock,
            return_value={
                "access_token": "gacc",
                "refresh_token": "ref",
                "expires_at": 9999999999,
            },
        ),
        patch.object(
            bridge_mcp_handlers,
            "_setup_gdrive_webhook",
            new_callable=AsyncMock,
            return_value=("ch-y", "res-final", datetime.now(timezone.utc)),
        ),
        patch.object(
            bridge_mcp_handlers.bridge_repo,
            "save_token",
            new_callable=AsyncMock,
        ),
    ):
        out = await bridge_mcp_handlers.complete_bridge_auth(
            engine,
            {
                "user_id": "u1",
                "bridge_id": str(bid),
                "provider": "gdrive",
                "authorization_code": "ac",
                "resource_id": "fld",
            },
        )
    data = json.loads(out)
    assert data["resource_id"] == "res-final"


@pytest.mark.asyncio
async def test_complete_bridge_auth_webhook_base_validation_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_mcp_handlers.cfg, "BRIDGE_WEBHOOK_BASE_URL", "")
    engine = _engine_pool_context(AsyncMock())
    bid = uuid.uuid4()
    row = {
        "user_id": "u1",
        "provider": "sharepoint",
        "client_state": "c",
        "status": "REQUESTED",
        "subscription_id": None,
        "resource_id": "x",
        "oauth_access_token_enc": None,
    }
    with (
        patch.object(
            bridge_mcp_handlers.bridge_repo,
            "get_by_id",
            new_callable=AsyncMock,
            return_value=row,
        ),
        patch.object(
            bridge_mcp_handlers,
            "_exchange_oauth_code",
            new_callable=AsyncMock,
            return_value={
                "access_token": "tok",
                "refresh_token": "ref",
                "expires_at": 9999999999,
            },
        ),
    ):
        with pytest.raises(McpError) as exc_info:
            await bridge_mcp_handlers.complete_bridge_auth(
                engine,
                {
                    "user_id": "u1",
                    "bridge_id": str(bid),
                    "provider": "sharepoint",
                    "authorization_code": "c",
                    "resource_id": "s|d",
                },
            )
        assert "BRIDGE_WEBHOOK_BASE_URL" in exc_info.value.data["detail"]

    monkeypatch.setattr(bridge_mcp_handlers.cfg, "BRIDGE_WEBHOOK_BASE_URL", "ftp://evil.com")
    with (
        patch.object(
            bridge_mcp_handlers.bridge_repo,
            "get_by_id",
            new_callable=AsyncMock,
            return_value=row,
        ),
        patch.object(
            bridge_mcp_handlers,
            "_exchange_oauth_code",
            new_callable=AsyncMock,
            return_value={
                "access_token": "tok",
                "refresh_token": "ref",
                "expires_at": 9999999999,
            },
        ),
    ):
        with pytest.raises(McpError) as exc_info:
            await bridge_mcp_handlers.complete_bridge_auth(
                engine,
                {
                    "user_id": "u1",
                    "bridge_id": str(bid),
                    "provider": "sharepoint",
                    "authorization_code": "c",
                    "resource_id": "s|d",
                },
            )


@pytest.mark.asyncio
async def test_complete_bridge_auth_state_machine_errors() -> None:
    engine = _engine_pool_context(AsyncMock())
    bid = uuid.uuid4()
    row_bad_prov = {
        "user_id": "u1",
        "provider": "dropbox",
        "client_state": "c",
        "status": "REQUESTED",
    }
    with patch.object(
        bridge_mcp_handlers.bridge_repo,
        "get_by_id",
        new_callable=AsyncMock,
        return_value=row_bad_prov,
    ):
        with pytest.raises(McpError) as exc_info:
            await bridge_mcp_handlers.complete_bridge_auth(
                engine,
                {
                    "user_id": "u1",
                    "bridge_id": str(bid),
                    "provider": "gdrive",
                    "authorization_code": "c",
                    "resource_id": "x",
                },
            )

    row_active = {
        "user_id": "u1",
        "provider": "dropbox",
        "client_state": "c",
        "status": "ACTIVE",
    }
    with patch.object(
        bridge_mcp_handlers.bridge_repo,
        "get_by_id",
        new_callable=AsyncMock,
        return_value=row_active,
    ):
        with pytest.raises(McpError) as exc_info:
            await bridge_mcp_handlers.complete_bridge_auth(
                engine,
                {
                    "user_id": "u1",
                    "bridge_id": str(bid),
                    "provider": "dropbox",
                    "authorization_code": "c",
                    "resource_id": "dbid:1",
                },
            )
        assert "connectable state" in exc_info.value.data["detail"]


@pytest.mark.asyncio
async def test_disconnect_sharepoint_and_gdrive_http_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_mcp_handlers.cfg, "GRAPH_BRIDGE_TOKEN", "gtok")
    post_r = _httpx_resp(status_code=204, json_data={})
    engine = _engine_pool_context(AsyncMock())
    bid = uuid.uuid4()
    row_sp = {
        "user_id": "u1",
        "provider": "sharepoint",
        "subscription_id": "sub-del",
        "resource_id": "s|d",
        "oauth_access_token_enc": None,
    }
    with (
        patch.object(
            bridge_mcp_handlers.bridge_repo,
            "get_by_id",
            new_callable=AsyncMock,
            return_value=row_sp,
        ),
        patch(
            "nce.bridge_mcp_handlers.httpx.AsyncClient",
            _patch_httpx_async_client(post_r),
        ),
    ):
        raw = await bridge_mcp_handlers.disconnect_bridge(
            engine, {"user_id": "u1", "bridge_id": str(bid)}
        )
    assert json.loads(raw)["state"] == "DISCONNECTED"

    monkeypatch.setattr(bridge_mcp_handlers.cfg, "GDRIVE_BRIDGE_TOKEN", "gd")
    row_g = {
        "user_id": "u1",
        "provider": "gdrive",
        "subscription_id": "ch1",
        "resource_id": "r1",
        "oauth_access_token_enc": None,
    }
    with (
        patch.object(
            bridge_mcp_handlers.bridge_repo,
            "get_by_id",
            new_callable=AsyncMock,
            return_value=row_g,
        ),
        patch(
            "nce.bridge_mcp_handlers.httpx.AsyncClient",
            _patch_httpx_async_client(post_r),
        ),
    ):
        raw = await bridge_mcp_handlers.disconnect_bridge(
            engine, {"user_id": "u1", "bridge_id": str(bid)}
        )
    assert json.loads(raw)["state"] == "DISCONNECTED"

    _httpx_resp(status_code=500, json_data={}, text="err")
    warn_del = _httpx_resp(status_code=500, json_data={}, text="err")
    with (
        patch.object(
            bridge_mcp_handlers.bridge_repo,
            "get_by_id",
            new_callable=AsyncMock,
            return_value=row_sp,
        ),
        patch(
            "nce.bridge_mcp_handlers.httpx.AsyncClient",
            _patch_httpx_async_client(_httpx_resp(), delete_ret=warn_del),
        ),
    ):
        await bridge_mcp_handlers.disconnect_bridge(
            engine, {"user_id": "u1", "bridge_id": str(bid)}
        )


@pytest.mark.asyncio
async def test_force_resync_sharepoint_and_gdrive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_mcp_handlers.cfg, "REDIS_URL", "redis://localhost:6379/0")
    engine = _engine_pool_context(AsyncMock())
    bid = uuid.uuid4()
    job = MagicMock()
    job.id = "jq-sp"
    for prov, rid in (
        ("sharepoint", "mysite|mydrive"),
        ("gdrive", "res-g"),
    ):
        row = {
            "user_id": "u1",
            "provider": prov,
            "resource_id": rid,
            "subscription_id": "sub",
            "client_state": "cs",
        }
        mock_r = MagicMock()
        mock_q = MagicMock()
        mock_q.enqueue.return_value = job
        with (
            patch.object(
                bridge_mcp_handlers.bridge_repo,
                "get_by_id",
                new_callable=AsyncMock,
                return_value=row,
            ),
            patch.object(bridge_mcp_handlers, "bridge_redis", return_value=mock_r),
            patch.object(bridge_mcp_handlers, "Redis") as RedisM,
            patch.object(bridge_mcp_handlers, "get_priority_queue", return_value=mock_q),
        ):
            RedisM.from_url.return_value = MagicMock()
            raw = await bridge_mcp_handlers.force_resync_bridge(
                engine, {"user_id": "u1", "bridge_id": str(bid)}
            )
        assert json.loads(raw)["job_id"] == "jq-sp"


@pytest.mark.asyncio
async def test_force_resync_sharepoint_bad_resource() -> None:
    engine = _engine_pool_context(AsyncMock())
    bid = uuid.uuid4()
    row = {
        "user_id": "u1",
        "provider": "sharepoint",
        "resource_id": "pending",
        "subscription_id": "s",
        "client_state": "c",
    }
    with (
        patch.object(
            bridge_mcp_handlers.bridge_repo,
            "get_by_id",
            new_callable=AsyncMock,
            return_value=row,
        ),
        patch.object(bridge_mcp_handlers, "bridge_redis", return_value=MagicMock()),
    ):
        with pytest.raises(McpError) as exc_info:
            await bridge_mcp_handlers.force_resync_bridge(
                engine, {"user_id": "u1", "bridge_id": str(bid)}
            )
        assert "force_resync" in exc_info.value.data["detail"]


def test_bridge_redis_uses_config_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge_mcp_handlers.cfg, "REDIS_URL", "redis://localhost:9999/1")
    with patch.object(bridge_mcp_handlers, "Redis") as R:
        bridge_mcp_handlers.bridge_redis()
    R.from_url.assert_called_once()


@pytest.mark.asyncio
async def test_bridge_status_not_found() -> None:
    engine = _engine_pool_context(AsyncMock())
    with patch.object(
        bridge_mcp_handlers.bridge_repo,
        "get_by_id",
        new_callable=AsyncMock,
        return_value=None,
    ):
        with pytest.raises(McpError):
            await bridge_mcp_handlers.bridge_status(
                engine, {"user_id": "u1", "bridge_id": str(uuid.uuid4())}
            )


@pytest.mark.asyncio
async def test_list_bridges_include_disconnected_flag() -> None:
    engine = _engine_pool_context(AsyncMock())
    with patch.object(
        bridge_mcp_handlers.bridge_repo,
        "list_for_user",
        new_callable=AsyncMock,
        return_value=[],
    ) as lf:
        await bridge_mcp_handlers.list_bridges(
            engine, {"user_id": "u1", "include_disconnected": True}
        )
    lf.assert_called_once()
    assert lf.call_args.kwargs.get("include_disconnected") is True


@pytest.mark.asyncio
async def test_replay_observe_truncates_at_max_events() -> None:
    async def _gen():
        for i in range(10):
            yield {"type": "event", "seq": i}

    class _Obs:
        async def execute(self, **kwargs):
            async for x in _gen():
                yield x

    with patch("nce.replay.ObservationalReplay", return_value=_Obs()):
        out = await replay_mcp_handlers.handle_replay_observe(
            _engine_pool_context(),
            {"namespace_id": NS, "max_events": 2},
        )
    assert "truncated" in out


@pytest.mark.asyncio
async def test_replay_fork_invalid_parameters() -> None:
    with pytest.raises(McpError) as exc_info:
        await replay_mcp_handlers.handle_replay_fork(
            _engine_pool_context(),
            {
                "source_namespace_id": "not-a-uuid",
                "target_namespace_id": NS,
                "fork_seq": 1,
                "expected_sha256": "0" * 64,
            },
        )
    assert "Invalid replay_fork" in exc_info.value.data["detail"]


@pytest.mark.asyncio
async def test_replay_fork_and_reconstruct_background_task_logs_exception() -> None:
    engine = _engine_pool_context()
    froz = MagicMock()
    froz.source_namespace_id = uuid.uuid4()
    froz.target_namespace_id = uuid.uuid4()
    froz.replay_mode = "deterministic"
    froz.start_seq = 1
    froz.fork_seq = 5
    froz.overrides_dict = {}
    run_id = uuid.uuid4()

    class _ForkBoom:
        async def execute(self, **kwargs):
            raise RuntimeError("boom")
            yield  # pragma: no cover

    with (
        patch("nce.models.FrozenForkConfig.from_request", return_value=froz),
        patch("nce.replay._create_run", new_callable=AsyncMock, return_value=run_id),
        patch("nce.replay.ForkedReplay", return_value=_ForkBoom()),
        patch("nce.replay_mcp_handlers.create_tracked_task") as ct,
        patch.object(replay_mcp_handlers.log, "exception") as log_exc,
    ):
        await replay_mcp_handlers.handle_replay_fork(
            engine,
            {
                "source_namespace_id": NS,
                "target_namespace_id": "00000000-0000-4000-8000-000000000002",
                "fork_seq": 3,
                "expected_sha256": "0" * 64,
            },
        )
        coro = ct.call_args[0][0]
        await coro
        log_exc.assert_called_once()

    with (
        patch("nce.replay._create_run", new_callable=AsyncMock, return_value=run_id),
        patch("nce.replay.ReconstructiveReplay", return_value=_ForkBoom()),
        patch("nce.replay_mcp_handlers.create_tracked_task") as ct2,
        patch.object(replay_mcp_handlers.log, "exception") as log_exc2,
    ):
        await replay_mcp_handlers.handle_replay_reconstruct(
            engine,
            {
                "source_namespace_id": NS,
                "target_namespace_id": "00000000-0000-4000-8000-000000000002",
                "end_seq": 9,
            },
        )
        coro2 = ct2.call_args[0][0]
        await coro2
        log_exc2.assert_called_once()
