"""Tests for nce.snapshot_serializer — batch 1 (create) and batch 2 (compare/serialize)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from nce.models import (
    _MAX_TOP_K,
    AssertionType,
    MemoryType,
    SemanticSearchResult,
    SnapshotRecord,
    StateDiffResult,
)
from nce.snapshot_serializer import (
    SNAPSHOT_ARG_KEYS,
    build_compare_states_request,
    build_create_snapshot_request,
    serialize_snapshot_record,
    serialize_state_diff,
)

VALID_NAMESPACE = "550e8400-e29b-41d4-a716-446655440000"
AS_OF_A = "2024-01-01T00:00:00Z"
AS_OF_B = "2024-01-02T00:00:00Z"


def _valid_arguments(**overrides: object) -> dict:
    """Minimal valid MCP arguments dict for create_snapshot."""
    args: dict = {
        SNAPSHOT_ARG_KEYS.NAMESPACE_ID: VALID_NAMESPACE,
        SNAPSHOT_ARG_KEYS.NAME: "snapshot-name",
    }
    args.update(overrides)
    return args


def _compare_arguments(**overrides: object) -> dict:
    """Minimal valid MCP arguments dict for compare_states."""
    args: dict = {
        SNAPSHOT_ARG_KEYS.NAMESPACE_ID: VALID_NAMESPACE,
        SNAPSHOT_ARG_KEYS.AS_OF_A: AS_OF_A,
        SNAPSHOT_ARG_KEYS.AS_OF_B: AS_OF_B,
    }
    args.update(overrides)
    return args


@pytest.fixture
def _compare_states_temporal_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin wall clock and disable lookback so fixed 2024 ISO timestamps parse."""
    import nce.config as cfg_mod
    import nce.temporal as temporal_mod

    fixed = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            if tz is None or tz == timezone.utc:
                return fixed
            return fixed.astimezone(tz)

    monkeypatch.setattr(temporal_mod, "datetime", _DT)
    monkeypatch.setattr(cfg_mod.cfg, "NCE_MAX_TEMPORAL_LOOKBACK_DAYS", 0)


class TestBuildCreateSnapshotRequest:
    def test_missing_name_raises_valueerror(self):
        args = _valid_arguments()
        del args[SNAPSHOT_ARG_KEYS.NAME]
        with pytest.raises(ValueError, match="name is required"):
            build_create_snapshot_request(args)

    def test_empty_name_raises_valueerror(self):
        with pytest.raises(ValueError, match="name is required"):
            build_create_snapshot_request(_valid_arguments(name=""))

    def test_name_over_256_chars_raises_valueerror(self):
        with pytest.raises(ValueError, match="256"):
            build_create_snapshot_request(_valid_arguments(name="x" * 257))

    def test_name_strips_whitespace(self):
        result = build_create_snapshot_request(_valid_arguments(name="  valid  "))
        assert result.name == "valid"

    def test_metadata_string_raises_valueerror(self):
        with pytest.raises(ValueError, match="metadata must be a JSON object"):
            build_create_snapshot_request(_valid_arguments(metadata="string"))

    def test_metadata_defaults_are_independent_dicts(self):
        first = build_create_snapshot_request(_valid_arguments())
        second = build_create_snapshot_request(_valid_arguments())
        assert first.metadata is not second.metadata
        first.metadata["key"] = "value"
        assert second.metadata == {}

    def test_empty_agent_id_normalized_to_default(self):
        result = build_create_snapshot_request(_valid_arguments(**{SNAPSHOT_ARG_KEYS.AGENT_ID: ""}))
        assert result.agent_id == "default"


@pytest.mark.usefixtures("_compare_states_temporal_env")
class TestBuildCompareStatesRequest:
    def test_top_k_clamps_to_max_top_k(self):
        result = build_compare_states_request(
            _compare_arguments(**{SNAPSHOT_ARG_KEYS.TOP_K: 1_000_000})
        )
        assert result.top_k == _MAX_TOP_K

    def test_top_k_zero_clamps_to_one(self):
        result = build_compare_states_request(_compare_arguments(**{SNAPSHOT_ARG_KEYS.TOP_K: 0}))
        assert result.top_k == 1

    def test_as_of_a_equal_as_of_b_raises_valueerror(self):
        with pytest.raises(ValueError, match="as_of_a must be strictly before as_of_b"):
            build_compare_states_request(
                _compare_arguments(
                    **{
                        SNAPSHOT_ARG_KEYS.AS_OF_A: AS_OF_A,
                        SNAPSHOT_ARG_KEYS.AS_OF_B: AS_OF_A,
                    }
                )
            )

    def test_as_of_a_after_as_of_b_raises_valueerror(self):
        with pytest.raises(ValueError, match="as_of_a must be strictly before as_of_b"):
            build_compare_states_request(
                _compare_arguments(
                    **{
                        SNAPSHOT_ARG_KEYS.AS_OF_A: AS_OF_B,
                        SNAPSHOT_ARG_KEYS.AS_OF_B: AS_OF_A,
                    }
                )
            )

    def test_as_of_a_before_as_of_b_succeeds(self):
        result = build_compare_states_request(_compare_arguments())
        assert result.as_of_a < result.as_of_b
        assert str(result.namespace_id) == VALID_NAMESPACE

    def test_query_over_max_length_raises_valueerror(self):
        with pytest.raises(ValueError, match="query exceeds maximum length"):
            build_compare_states_request(
                _compare_arguments(**{SNAPSHOT_ARG_KEYS.QUERY: "x" * 2049})
            )

    def test_query_at_max_length_succeeds(self):
        query = "x" * 2048
        result = build_compare_states_request(
            _compare_arguments(**{SNAPSHOT_ARG_KEYS.QUERY: query})
        )
        assert result.query == query


class TestSerializeSnapshotRecord:
    def test_returns_valid_json(self):
        now = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
        record = SnapshotRecord(
            id=uuid4(),
            namespace_id=UUID(VALID_NAMESPACE),
            agent_id="default",
            name="snap",
            snapshot_at=now,
            created_at=now,
            metadata={"key": "value"},
        )
        payload = json.loads(serialize_snapshot_record(record))
        assert payload["name"] == "snap"
        assert payload["metadata"] == {"key": "value"}


class TestSerializeStateDiff:
    def test_returns_valid_json_with_uuid_and_datetime_fields(self):
        mem_id = uuid4()
        ns_id = UUID(VALID_NAMESPACE)
        as_of_a = datetime(2024, 1, 1, tzinfo=timezone.utc)
        as_of_b = datetime(2024, 1, 2, tzinfo=timezone.utc)
        valid_from = datetime(2024, 1, 1, 6, 0, tzinfo=timezone.utc)
        hit = SemanticSearchResult(
            memory_id=mem_id,
            namespace_id=ns_id,
            agent_id="default",
            score=0.95,
            payload_ref="ref://test",
            assertion_type=AssertionType.fact,
            memory_type=MemoryType.episodic,
            valid_from=valid_from,
        )
        diff = StateDiffResult(
            as_of_a=as_of_a,
            as_of_b=as_of_b,
            added=[hit],
            removed=[],
            modified=[],
        )
        payload = json.loads(serialize_state_diff(diff))
        assert payload["as_of_a"] == as_of_a.isoformat().replace("+00:00", "Z")
        assert payload["added"][0]["memory_id"] == str(mem_id)
        assert payload["added"][0]["valid_from"] == valid_from.isoformat().replace("+00:00", "Z")
