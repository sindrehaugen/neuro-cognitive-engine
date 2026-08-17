"""Tests for nce.models — Pydantic validation contracts on MCP request models."""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from nce.models import (
    BoostMemoryRequest,
    ForgetMemoryRequest,
    FrozenForkConfig,
    GetRecentContextRequest,
    GraphSearchRequest,
    KGEdge,
    NamespacePIIConfig,
    ReplayForkRequest,
    SemanticSearchRequest,
    StoreMemoryRequest,
    UnredactMemoryRequest,
)
from nce.replay import ReplayChecksumError
from nce.signing import canonical_json

VALID_UUID = "550e8400-e29b-41d4-a716-446655440000"
TARGET_UUID = "6ba7b810-9dad-41d1-80b4-00c04fd430c8"


def _expected_replay_checksum(
    *,
    source_namespace_id: str,
    target_namespace_id: str,
    fork_seq: int,
    start_seq: int = 1,
    replay_mode: str = "deterministic",
    config_overrides: dict[str, Any] | None = None,
    agent_id_filter: str | None = None,
) -> str:
    payload: dict[str, Any] = {
        "source_namespace_id": source_namespace_id,
        "target_namespace_id": target_namespace_id,
        "fork_seq": fork_seq,
        "start_seq": start_seq,
        "replay_mode": replay_mode,
        "config_overrides": config_overrides,
        "agent_id_filter": agent_id_filter,
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _valid_replay_fork(**overrides: Any) -> ReplayForkRequest:
    expected = _expected_replay_checksum(
        source_namespace_id=VALID_UUID,
        target_namespace_id=TARGET_UUID,
        fork_seq=overrides.pop("fork_seq", 1),
        start_seq=overrides.pop("start_seq", 1),
        replay_mode=overrides.pop("replay_mode", "deterministic"),
        config_overrides=overrides.pop("config_overrides", None),
        agent_id_filter=overrides.pop("agent_id_filter", None),
    )
    data = {
        "source_namespace_id": VALID_UUID,
        "target_namespace_id": TARGET_UUID,
        "fork_seq": 1,
        "expected_sha256": expected,
        **overrides,
    }
    return ReplayForkRequest.model_validate(data)


class TestKGEdgeMetadata:
    def test_nested_dict_in_metadata_raises_validation_error(self):
        with pytest.raises(ValidationError):
            KGEdge(
                subject_label="A",
                predicate="relates_to",
                object_label="B",
                metadata={"nested": {"dict": "value"}},
            )

    def test_flat_scalar_metadata_accepted(self):
        edge = KGEdge(
            subject_label="A",
            predicate="relates_to",
            object_label="B",
            metadata={"source": "test", "count": 1},
        )
        assert edge.metadata == {"source": "test", "count": 1}

    def test_self_referential_edge_raises_validation_error(self):
        with pytest.raises(ValidationError, match="Self-referential"):
            KGEdge(
                subject_label="same",
                predicate="relates_to",
                object_label="same",
                metadata={"source": "test"},
            )


class TestUuidNormalization:
    @pytest.mark.parametrize(
        "model_cls",
        [BoostMemoryRequest, ForgetMemoryRequest, UnredactMemoryRequest],
    )
    def test_valid_uuid_strings_accepted(self, model_cls):
        req = model_cls.model_validate(
            {
                "namespace_id": VALID_UUID,
                "memory_id": VALID_UUID,
                "agent_id": "agent-1",
            }
        )
        assert isinstance(req.namespace_id, UUID)
        assert isinstance(req.memory_id, UUID)
        assert str(req.namespace_id) == VALID_UUID
        assert str(req.memory_id) == VALID_UUID

    @pytest.mark.parametrize(
        "model_cls",
        [BoostMemoryRequest, ForgetMemoryRequest, UnredactMemoryRequest],
    )
    def test_invalid_uuid_string_raises_validation_error(self, model_cls):
        with pytest.raises(ValidationError):
            model_cls.model_validate(
                {
                    "namespace_id": "not-a-uuid",
                    "memory_id": VALID_UUID,
                    "agent_id": "agent-1",
                }
            )
        with pytest.raises(ValidationError):
            model_cls.model_validate(
                {
                    "namespace_id": VALID_UUID,
                    "memory_id": "also-not-a-uuid",
                    "agent_id": "agent-1",
                }
            )


class TestReplaySecurity:
    def test_expected_sha256_spaces_raises_validation_error(self):
        with pytest.raises(ValidationError):
            ReplayForkRequest.model_validate(
                {
                    "source_namespace_id": VALID_UUID,
                    "target_namespace_id": TARGET_UUID,
                    "fork_seq": 1,
                    "expected_sha256": " " * 64,
                }
            )

    def test_expected_sha256_uppercase_hex_raises_validation_error(self):
        with pytest.raises(ValidationError):
            ReplayForkRequest.model_validate(
                {
                    "source_namespace_id": VALID_UUID,
                    "target_namespace_id": TARGET_UUID,
                    "fork_seq": 1,
                    "expected_sha256": "A" * 64,
                }
            )

    def test_valid_lowercase_hex_accepted(self):
        req = _valid_replay_fork()
        assert len(req.expected_sha256) == 64
        assert req.expected_sha256 == req.expected_sha256.lower()

    def test_checksum_mismatch_message_sanitized(self):
        wrong_hash = "f" * 64
        req = ReplayForkRequest.model_validate(
            {
                "source_namespace_id": VALID_UUID,
                "target_namespace_id": TARGET_UUID,
                "fork_seq": 1,
                "expected_sha256": wrong_hash,
            }
        )
        with pytest.raises(ReplayChecksumError) as excinfo:
            FrozenForkConfig._validate_payload_checksum(req)
        msg = str(excinfo.value)
        assert msg == "Payload checksum mismatch"
        assert wrong_hash not in msg


class TestNamespacePIIConfigPseudonymHmacKey:
    def test_shorter_than_8_utf8_bytes_raises_validation_error(self):
        with pytest.raises(ValidationError):
            NamespacePIIConfig(pseudonym_hmac_key="abcdefg")

    def test_exactly_8_bytes_accepted(self):
        cfg = NamespacePIIConfig(pseudonym_hmac_key="abcdefgh")
        assert cfg.pseudonym_hmac_key == "abcdefgh"

    def test_none_accepted(self):
        cfg = NamespacePIIConfig(pseudonym_hmac_key=None)
        assert cfg.pseudonym_hmac_key is None


class TestSizeLimits:
    def test_store_memory_content_over_1mb_utf8_raises_validation_error(self):
        with pytest.raises(ValidationError, match="content exceeds"):
            StoreMemoryRequest.model_validate(
                {
                    "namespace_id": VALID_UUID,
                    "content": "x" * 1_000_001,
                }
            )

    def test_store_memory_single_char_content_accepted(self):
        req = StoreMemoryRequest.model_validate(
            {
                "namespace_id": VALID_UUID,
                "content": "x",
            }
        )
        assert req.content == "x"

    def test_semantic_search_query_over_limit_raises_validation_error(self):
        with pytest.raises(ValidationError, match="query exceeds"):
            SemanticSearchRequest.model_validate(
                {
                    "namespace_id": VALID_UUID,
                    "query": "q" * 10_001,
                }
            )

    def test_graph_search_query_over_limit_raises_validation_error(self):
        with pytest.raises(ValidationError, match="query exceeds"):
            GraphSearchRequest.model_validate(
                {
                    "namespace_id": VALID_UUID,
                    "query": "q" * 10_001,
                }
            )


class TestGetRecentContextRequestIdentity:
    def test_user_id_promoted_when_agent_id_none(self):
        req = GetRecentContextRequest.model_validate(
            {
                "namespace_id": VALID_UUID,
                "agent_id": None,
                "user_id": "alice",
            }
        )
        assert req.agent_id == "alice"

    def test_explicit_agent_id_wins_over_user_id(self):
        req = GetRecentContextRequest.model_validate(
            {
                "namespace_id": VALID_UUID,
                "agent_id": "bob",
                "user_id": "alice",
            }
        )
        assert req.agent_id == "bob"

    def test_default_user_id_does_not_set_agent_id(self):
        req = GetRecentContextRequest.model_validate(
            {
                "namespace_id": VALID_UUID,
                "agent_id": None,
                "user_id": "default",
            }
        )
        assert req.agent_id is None
