"""Contract tests for nce.memory_mcp_handlers (serialization, agent_id, async paths)."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from inspect import iscoroutine
from unittest.mock import AsyncMock, MagicMock

import pytest

from nce import memory_mcp_handlers
from nce.mcp_errors import MCP_INVALID_PARAMS, McpError

NS = "00000000-0000-4000-8000-000000000001"

_BASE_STORE_MEMORY = {
    "namespace_id": NS,
    "agent_id": "ag",
    "content": "hello",
    "summary": "s",
    "heavy_payload": "",
}

_MEDIA_ARGS = {
    "namespace_id": NS,
    "user_id": "mediauser",
    "session_id": "sess1",
    "media_type": "image",
    "file_path_on_disk": "C:\\tmp\\a.png",
    "summary": "pic",
}


class TestSerializeSafety:
    def test_serialize_uuid_values_as_strings(self) -> None:
        uid = uuid.uuid4()
        out = memory_mcp_handlers._serialize({"id": uid})
        parsed = json.loads(out)
        assert parsed["id"] == str(uid)

    def test_serialize_datetime_values(self) -> None:
        dt = datetime(2024, 6, 1, 12, 30, 0, tzinfo=timezone.utc)
        out = memory_mcp_handlers._serialize({"ts": dt})
        parsed = json.loads(out)
        assert isinstance(parsed["ts"], str)
        assert "2024" in parsed["ts"]

    def test_ok_response_serializes_uuid_extras(self) -> None:
        uid = uuid.uuid4()
        out = memory_mcp_handlers._ok_response("ref-1", memory_id=uid)
        parsed = json.loads(out)
        assert parsed["status"] == "ok"
        assert parsed["payload_ref"] == "ref-1"
        assert parsed["memory_id"] == str(uid)


class TestAgentIdVsUserId:
    @pytest.mark.asyncio
    async def test_get_recent_context_uses_agent_id_not_user_id(self) -> None:
        engine = MagicMock()
        engine.recall_recent = AsyncMock(return_value=[])

        await memory_mcp_handlers.handle_get_recent_context(
            engine,
            {
                "namespace_id": NS,
                "agent_id": "caller",
                "user_id": "session-user",
                "limit": 5,
            },
        )

        engine.recall_recent.assert_awaited_once()
        kwargs = engine.recall_recent.await_args.kwargs
        assert kwargs["agent_id"] == "caller"
        assert kwargs["namespace_id"] == NS
        assert "user_id" not in kwargs


class TestAsyncDirectReturn:
    @pytest.mark.asyncio
    async def test_semantic_search_returns_json_string_not_coroutine(self) -> None:
        engine = MagicMock()
        engine.semantic_search = AsyncMock(return_value=[{"score": 0.9}])

        result = await memory_mcp_handlers.handle_semantic_search(
            engine,
            {"namespace_id": NS, "query": "network topology"},
        )

        assert isinstance(result, str)
        assert not iscoroutine(result)
        assert json.loads(result) == [{"score": 0.9}]

    @pytest.mark.asyncio
    async def test_get_recent_context_returns_json_string_not_coroutine(self) -> None:
        engine = MagicMock()
        engine.recall_recent = AsyncMock(return_value=["a", "b"])

        result = await memory_mcp_handlers.handle_get_recent_context(
            engine,
            {"namespace_id": NS, "agent_id": "ag", "limit": 2},
        )

        assert isinstance(result, str)
        assert not iscoroutine(result)
        assert json.loads(result) == {"context": ["a", "b"]}


class TestStoreMemoryValidation:
    @pytest.mark.asyncio
    async def test_store_memory_ok_when_payload_ref_present(self) -> None:
        engine = MagicMock()
        engine.store_memory = AsyncMock(return_value={"payload_ref": "abc", "contradiction": None})

        result = await memory_mcp_handlers.handle_store_memory(engine, dict(_BASE_STORE_MEMORY))
        data = json.loads(result)
        assert data["status"] == "ok"
        assert data["payload_ref"] == "abc"

    @pytest.mark.asyncio
    async def test_store_memory_empty_engine_response_raises_mcp_error(self) -> None:
        engine = MagicMock()
        engine.store_memory = AsyncMock(return_value={})

        with pytest.raises(McpError) as exc_info:
            await memory_mcp_handlers.handle_store_memory(engine, dict(_BASE_STORE_MEMORY))

        assert exc_info.value.code == MCP_INVALID_PARAMS
        assert exc_info.value.data["reason"] == "invalid_arguments"

    @pytest.mark.asyncio
    async def test_store_memory_accepts_content_type(self) -> None:
        engine = MagicMock()
        engine.store_memory = AsyncMock(return_value={"payload_ref": "abc", "contradiction": None})

        args = dict(_BASE_STORE_MEMORY)
        args["content_type"] = "chat"

        result = await memory_mcp_handlers.handle_store_memory(engine, args)
        data = json.loads(result)
        assert data["status"] == "ok"
        assert data["payload_ref"] == "abc"


class TestStoreMediaDeprecation:
    @pytest.mark.asyncio
    async def test_store_media_response_includes_deprecation_fields(self) -> None:
        engine = MagicMock()
        engine.store_artifact = AsyncMock(return_value="mongo-id-123")

        result = await memory_mcp_handlers.handle_store_media(engine, dict(_MEDIA_ARGS))
        data = json.loads(result)

        assert data["status"] == "ok"
        assert data["deprecated"] is True
        assert data["replacement"] == "store_artifact"


class TestGetRecentContextNoTimeout:
    @pytest.mark.asyncio
    async def test_get_recent_context_completes_with_slow_engine(self) -> None:
        async def slow_recall(**_kwargs: object) -> list[str]:
            await asyncio.sleep(0.05)
            return ["recent-item"]

        engine = MagicMock()
        engine.recall_recent = slow_recall

        result = await memory_mcp_handlers.handle_get_recent_context(
            engine,
            {"namespace_id": NS, "agent_id": "ag", "limit": 1},
        )

        assert json.loads(result) == {"context": ["recent-item"]}
