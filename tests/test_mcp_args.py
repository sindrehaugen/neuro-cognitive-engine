"""Tests for nce.mcp_args — metadata validation, cache keys, namespace extraction."""

from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import BaseModel

from nce.mcp_args import (
    _canonicalize,
    _validate_metadata_values,
    build_cache_key,
    extract_namespace_id,
    validate_nested_models,
)

VALID_UUID = "550e8400-e29b-41d4-a716-446655440000"


class Inner(BaseModel):
    model_config = {"extra": "forbid"}

    value: int


class TestValidateMetadataValues:
    def test_valid_flat_metadata_accepted(self):
        _validate_metadata_values({"key": "value", "num": 42, "flag": True, "empty": None})

    def test_nested_dict_rejected(self):
        with pytest.raises(ValueError):
            _validate_metadata_values({"key": {"nested": "dict"}})

    def test_too_many_keys_rejected(self):
        with pytest.raises(ValueError, match="maximum 512"):
            _validate_metadata_values({f"k{i}": "v" for i in range(513)})

    def test_key_too_long_rejected(self):
        with pytest.raises(ValueError):
            _validate_metadata_values({"A" * 257: "value"})

    def test_string_value_too_long_rejected(self):
        with pytest.raises(ValueError, match="4096"):
            _validate_metadata_values({"key": "x" * 4097})

    def test_list_too_large_rejected(self):
        with pytest.raises(ValueError, match="256"):
            _validate_metadata_values({"key": list(range(257))})

    def test_list_with_invalid_item_rejected(self):
        with pytest.raises(ValueError):
            _validate_metadata_values({"key": [{"nested": "obj"}]})

    def test_valid_list_accepted(self):
        _validate_metadata_values({"key": [1, "two", 3.0, None, True]})

    def test_non_dict_input_rejected(self):
        with pytest.raises(ValueError):
            _validate_metadata_values("not a dict")  # type: ignore[arg-type]


class TestExtractNamespaceId:
    def test_valid_uuid_string_returned_canonical(self):
        assert extract_namespace_id({"namespace_id": VALID_UUID}) == VALID_UUID

    def test_valid_uuid_object_returned_as_string(self):
        assert extract_namespace_id({"namespace_id": UUID(VALID_UUID)}) == VALID_UUID

    def test_absent_key_returns_none(self):
        assert extract_namespace_id({}) is None

    def test_none_value_returns_none(self):
        assert extract_namespace_id({"namespace_id": None}) is None

    def test_invalid_uuid_raises_valueerror(self):
        with pytest.raises(ValueError, match="Invalid namespace_id"):
            extract_namespace_id({"namespace_id": "not-a-uuid"})

    def test_invalid_uuid_does_not_fallback_silently(self):
        with pytest.raises(ValueError):
            extract_namespace_id({"namespace_id": "malformed"})


class TestCanonicalizeHelper:
    def test_uuid_converted_to_string(self):
        assert _canonicalize(UUID(VALID_UUID)) == VALID_UUID

    def test_dict_keys_sorted(self):
        assert _canonicalize({"z": 1, "a": 2}) == {"a": 2, "z": 1}

    def test_nested_dict_keys_sorted(self):
        assert _canonicalize({"outer": {"z": 1, "a": 2}}) == {"outer": {"a": 2, "z": 1}}

    def test_list_items_normalized(self):
        assert _canonicalize([UUID(VALID_UUID)]) == [VALID_UUID]

    def test_primitives_unchanged(self):
        assert _canonicalize(42) == 42
        assert _canonicalize("hello") == "hello"
        assert _canonicalize(None) is None


class TestBuildCacheKey:
    def test_same_args_same_key(self):
        args = {"namespace_id": VALID_UUID, "query": "hello"}
        assert build_cache_key("search", args, 0) == build_cache_key("search", args, 0)

    def test_different_dict_ordering_same_key(self):
        a = {"namespace_id": VALID_UUID, "query": "hello", "limit": 10}
        b = {"limit": 10, "query": "hello", "namespace_id": VALID_UUID}
        assert build_cache_key("search", a, 0) == build_cache_key("search", b, 0)

    def test_uuid_object_vs_string_same_key(self):
        args_str = {"namespace_id": VALID_UUID, "q": "hello"}
        args_obj = {"namespace_id": UUID(VALID_UUID), "q": "hello"}
        assert build_cache_key("search", args_str, 0) == build_cache_key("search", args_obj, 0)

    def test_auth_keys_excluded_from_hash(self):
        clean = {"namespace_id": VALID_UUID, "q": "x"}
        with_auth = {
            "namespace_id": VALID_UUID,
            "q": "x",
            "admin_api_key": "secret123",
        }
        assert build_cache_key("t", clean, 0) == build_cache_key("t", with_auth, 0)

    def test_tool_name_too_long_raises(self):
        with pytest.raises(ValueError, match="tool_name too long"):
            build_cache_key("t" * 101, {"namespace_id": VALID_UUID}, 0)

    def test_arguments_too_large_raises(self):
        huge = {"namespace_id": VALID_UUID, "data": "x" * 1_000_001}
        with pytest.raises(ValueError, match="too large"):
            build_cache_key("tool", huge, 0)

    def test_generation_changes_key(self):
        args = {"namespace_id": VALID_UUID}
        assert build_cache_key("t", args, 0) != build_cache_key("t", args, 1)

    def test_namespace_scopes_key(self):
        a = {"namespace_id": VALID_UUID}
        b = {"namespace_id": "aaaabbbb-cccc-dddd-eeee-ffffffffffff"}
        assert build_cache_key("t", a, 0) != build_cache_key("t", b, 0)

    def test_key_format_prefix(self):
        key = build_cache_key("mytool", {"namespace_id": VALID_UUID}, 0)
        assert key.startswith("mcp_cache:v0:")

    def test_invalid_namespace_in_args_raises(self):
        with pytest.raises(ValueError, match="Invalid namespace_id"):
            build_cache_key("t", {"namespace_id": "bad-uuid"}, 0)


class TestValidateNestedModels:
    def test_valid_nested_accepted(self):
        args = {"inner": {"value": 42}}
        result = validate_nested_models(args, nested_fields={"inner": Inner})
        assert isinstance(result["inner"], Inner)
        assert result["inner"].value == 42

    def test_does_not_mutate_original_dict(self):
        original = {"inner": {"value": 42}}
        result = validate_nested_models(original, nested_fields={"inner": Inner})
        assert isinstance(original["inner"], dict)
        assert isinstance(result["inner"], Inner)

    def test_invalid_nested_raises_valueerror(self):
        args = {"inner": {"value": "not_an_int"}}
        with pytest.raises(ValueError, match="Invalid nested field 'inner'"):
            validate_nested_models(args, nested_fields={"inner": Inner})

    def test_non_dict_nested_raises_valueerror(self):
        args = {"inner": "not_a_dict"}
        with pytest.raises(ValueError, match="Expected a JSON object"):
            validate_nested_models(args, nested_fields={"inner": Inner})

    def test_absent_field_passes_through(self):
        args = {"other": "value"}
        result = validate_nested_models(args, nested_fields={"inner": Inner})
        assert "inner" not in result
        assert result["other"] == "value"

    def test_no_nested_fields_returns_same_dict(self):
        args = {"a": 1}
        result = validate_nested_models(args)
        assert result is args
