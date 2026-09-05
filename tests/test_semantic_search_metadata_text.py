"""``semantic_search`` must accept ``namespaces.metadata`` arriving as text.

Ported from the steps-ai fork of NCE (backend/nce/semantic_search.py, commit
3cbb4bce9, 2026-08-19).

No jsonb codec is registered on the asyncpg pool (``set_type_codec`` appears
nowhere in ``nce/``), so asyncpg hands the ``metadata`` column back as ``str``.
``"cognitive" in meta`` was then a substring test that matched the metadata every
``manage_namespace create`` writes, and the next line, ``meta["cognitive"]``,
raised ``TypeError: string indices must be integers``. Every search in such a
namespace answered -32602.

``nce/me_app.py``, ``nce/tasks.py`` and ``nce/contradictions.py`` already decode
the same column themselves; this was the one reader that assumed a dict. The
tests drive the real function with a fake connection and check that the values
carried in the text metadata reach the ranking query's bound parameters, so the
fix is shown to honour the configuration, not merely to stop raising.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.embeddings import VECTOR_DIM
from nce.models import NamespaceCognitiveConfig
from nce.semantic_search import semantic_search

NS = "00000000-0000-4000-8000-000000000001"
AGENT = "test-agent"

# Distinct from every default and from every other bound parameter (candidate_k,
# need), so their presence among the parameters proves the metadata was read.
COGNITIVE = {"half_life_days": 7.0, "alpha": 0.4, "reinforcement_delta": 0.05}
RETENTION = 45
META = {"cognitive": COGNITIVE, "temporal_retention_days": RETENTION}

DEFAULTS = NamespaceCognitiveConfig()
DEFAULT_RETENTION = 90


def test_fixture_values_do_not_collide_with_defaults():
    """Guard the guard: a value equal to a default would pass vacuously."""
    assert COGNITIVE["alpha"] != DEFAULTS.alpha
    assert COGNITIVE["half_life_days"] != DEFAULTS.half_life_days
    assert RETENTION not in {DEFAULT_RETENTION, DEFAULTS.half_life_days, DEFAULTS.alpha}


async def _embed(_query: str) -> list[float]:
    return [0.1] * VECTOR_DIM


async def _search_with_metadata(metadata) -> tuple[list, AsyncMock]:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"metadata": metadata})
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])

    @asynccontextmanager
    async def _scoped(_pool, _namespace_id):
        yield conn

    def _discard_background_task(coro):
        coro.close()
        return MagicMock()

    with (
        patch("nce.semantic_search.scoped_pg_session", _scoped),
        patch(
            "nce.semantic_search.asyncio.create_task",
            side_effect=_discard_background_task,
        ),
    ):
        results = await semantic_search(
            pg_pool=MagicMock(),
            mongo_client=MagicMock(),
            embedding_fn=_embed,
            query="network topology",
            namespace_id=NS,
            agent_id=AGENT,
        )
    return results, conn


def _ranking_params(conn: AsyncMock) -> list:
    """Parameters bound to the hybrid ranking query (the first ``fetch``)."""
    assert conn.fetch.await_count >= 1, "the ranking query was never issued"
    _sql, *params = conn.fetch.await_args_list[0].args
    return params


class TestTextMetadataIsDecoded:
    """The production shape: asyncpg returns jsonb as ``str``."""

    @pytest.mark.asyncio
    async def test_cognitive_block_as_text_is_honoured(self):
        results, conn = await _search_with_metadata(json.dumps(META))
        assert results == []
        params = _ranking_params(conn)
        assert COGNITIVE["alpha"] in params
        assert COGNITIVE["half_life_days"] in params
        assert RETENTION in params

    @pytest.mark.asyncio
    async def test_text_without_a_cognitive_block_still_reads_retention(self):
        _, conn = await _search_with_metadata(json.dumps({"temporal_retention_days": RETENTION}))
        params = _ranking_params(conn)
        assert RETENTION in params
        assert DEFAULTS.alpha in params

    @pytest.mark.asyncio
    async def test_a_json_array_containing_the_key_names_is_not_a_config(self):
        """The substring match is what steered the old code into ``meta["cognitive"]``."""
        _, conn = await _search_with_metadata(json.dumps(["cognitive", "temporal_retention_days"]))
        params = _ranking_params(conn)
        assert DEFAULTS.alpha in params
        assert DEFAULT_RETENTION in params


class TestNonDictMetadataFallsBackToDefaults:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("metadata", [None, "", "{}", "null", "[]"])
    async def test_defaults_apply(self, metadata):
        results, conn = await _search_with_metadata(metadata)
        assert results == []
        params = _ranking_params(conn)
        assert DEFAULTS.alpha in params
        assert DEFAULTS.half_life_days in params
        assert DEFAULT_RETENTION in params


class TestDictMetadataKeepsWorking:
    """What a jsonb codec on the pool would deliver, should one be registered."""

    @pytest.mark.asyncio
    async def test_dict_is_honoured_unchanged(self):
        _, conn = await _search_with_metadata(dict(META))
        params = _ranking_params(conn)
        assert COGNITIVE["alpha"] in params
        assert RETENTION in params
