"""Tests for nce.mcp_utils — caller context and A2A scope parsing."""

from __future__ import annotations

import json
from uuid import UUID

import pytest

from nce.a2a import A2AScope
from nce.auth import NamespaceContext
from nce.mcp_utils import (
    _MAX_SCOPES_INPUT_BYTES,
    _MAX_SCOPES_LIST_ITEMS,
    build_caller_context,
    parse_scopes,
)

VALID_UUID = "550e8400-e29b-41d4-a716-446655440000"

_VALID_SCOPE_DICT = {
    "resource_type": "namespace",
    "resource_id": VALID_UUID,
}


class TestBuildCallerContext:
    def test_valid_namespace_and_agent_id_returns_namespace_context(self):
        ctx = build_caller_context({"namespace_id": VALID_UUID, "agent_id": "agent-42"})
        assert isinstance(ctx, NamespaceContext)
        assert ctx.namespace_id == UUID(VALID_UUID)
        assert ctx.agent_id == "agent-42"

    def test_missing_namespace_id_raises_valueerror(self):
        with pytest.raises(ValueError, match="namespace_id is required"):
            build_caller_context({})

    def test_malformed_namespace_id_raises_valueerror(self):
        with pytest.raises(ValueError, match="Invalid namespace_id"):
            build_caller_context({"namespace_id": "not-a-uuid"})

    def test_blank_agent_id_resolves_to_default(self):
        for blank in ("", "   ", None):
            ctx = build_caller_context({"namespace_id": VALID_UUID, "agent_id": blank})
            assert ctx.agent_id == "default"

    def test_agent_id_over_128_chars_truncated(self):
        long_id = "a" * 200
        ctx = build_caller_context({"namespace_id": VALID_UUID, "agent_id": long_id})
        assert len(ctx.agent_id) == 128
        assert ctx.agent_id == "a" * 128


class TestParseScopes:
    def test_valid_json_string_returns_a2a_scopes(self):
        payload = json.dumps([_VALID_SCOPE_DICT])
        scopes = parse_scopes(payload)
        assert len(scopes) == 1
        assert isinstance(scopes[0], A2AScope)
        assert scopes[0].resource_type == "namespace"
        assert scopes[0].resource_id == VALID_UUID
        assert scopes[0].permissions == ["read"]

    def test_valid_list_input_returns_a2a_scopes(self):
        scopes = parse_scopes([_VALID_SCOPE_DICT])
        assert len(scopes) == 1
        assert isinstance(scopes[0], A2AScope)
        assert scopes[0].resource_type == "namespace"

    def test_json_string_exceeding_max_bytes_raises(self):
        # Valid JSON array, but raw string exceeds byte limit.
        huge = json.dumps([_VALID_SCOPE_DICT])
        padding = " " * (_MAX_SCOPES_INPUT_BYTES - len(huge.encode()) + 1)
        oversized = padding + huge
        assert len(oversized.encode()) > _MAX_SCOPES_INPUT_BYTES
        with pytest.raises(
            ValueError,
            match=f"raw_scopes exceeds maximum size \\({_MAX_SCOPES_INPUT_BYTES} bytes\\)",
        ):
            parse_scopes(oversized)

    def test_invalid_json_string_raises_with_not_valid_json(self):
        with pytest.raises(ValueError, match="not valid JSON"):
            parse_scopes("{not json")

    def test_json_decoding_to_dict_raises(self):
        with pytest.raises(ValueError, match="scopes must be a list"):
            parse_scopes(json.dumps({"resource_type": "namespace"}))

    def test_list_longer_than_max_items_raises(self):
        items = [_VALID_SCOPE_DICT] * (_MAX_SCOPES_LIST_ITEMS + 1)
        with pytest.raises(
            ValueError,
            match=f"scopes list exceeds maximum length \\({_MAX_SCOPES_LIST_ITEMS} items\\)",
        ):
            parse_scopes(items)

    def test_none_input_raises(self):
        with pytest.raises(ValueError, match="scopes must be a list"):
            parse_scopes(None)

    def test_empty_list_returns_empty(self):
        assert parse_scopes([]) == []
