"""
tests/test_constants.py

Structural contracts for nce.constants — verifies the canonical values
that were previously scattered across three modules.
"""

from __future__ import annotations

import re

from nce.constants import (
    ALLOWED_LANGUAGES,
    MAX_GRAPH_DEPTH,
    MAX_TOP_K,
    MCP_CACHE_TTL_S,
    SAFE_ID_RE,
)


def test_allowed_languages_count() -> None:
    # 39 entries in code_mcp_handlers.py canonical list.
    assert len(ALLOWED_LANGUAGES) == 39


def test_allowed_languages_contains_core_set() -> None:
    # The 5-lang subset that existed in orchestrator.py / graph.py must be present.
    assert {"python", "javascript", "typescript", "go", "rust"}.issubset(ALLOWED_LANGUAGES)


def test_allowed_languages_contains_extended_set() -> None:
    assert {"java", "cpp", "sql", "terraform", "zig"}.issubset(ALLOWED_LANGUAGES)


def test_allowed_languages_is_frozenset() -> None:
    assert isinstance(ALLOWED_LANGUAGES, frozenset)


def test_safe_id_re_accepts_valid_ids() -> None:
    for valid in ("agent-1", "user_abc", "a" * 128, "x", "abc123"):
        assert SAFE_ID_RE.match(valid), f"Expected match for {valid!r}"


def test_safe_id_re_rejects_invalid_ids() -> None:
    for invalid in ("", "a b", "a" * 129, "id with spaces", "dot.id"):
        assert not SAFE_ID_RE.match(invalid), f"Expected no match for {invalid!r}"


def test_safe_id_re_is_compiled_pattern() -> None:
    assert isinstance(SAFE_ID_RE, re.Pattern)


def test_mcp_cache_ttl_is_300() -> None:
    """Pins the TTL that dispatch was already using; guards against regression."""
    assert MCP_CACHE_TTL_S == 300


def test_max_top_k() -> None:
    assert MAX_TOP_K == 100


def test_max_graph_depth() -> None:
    assert MAX_GRAPH_DEPTH == 3
