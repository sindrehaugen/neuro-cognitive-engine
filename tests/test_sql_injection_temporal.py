from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from nce.orchestrator import NCEEngine


@pytest.mark.asyncio
async def test_semantic_search_temporal_parameters_prevent_sql_injection(monkeypatch):
    # Mock embedding calls to avoid loading the real model and potential network calls
    async def mock_embed(text: str) -> list[float]:
        from nce.embeddings import VECTOR_DIM

        return [0.0] * VECTOR_DIM

    monkeypatch.setattr("nce.embeddings.embed", mock_embed)

    # Setup mock engine
    engine = NCEEngine()

    mock_pool = MagicMock()
    mock_conn = AsyncMock()
    tx = AsyncMock()
    tx.__aenter__.return_value = None
    tx.__aexit__.return_value = False
    mock_conn.transaction = MagicMock(return_value=tx)
    mock_conn.execute = AsyncMock()
    acq = AsyncMock()
    acq.__aenter__.return_value = mock_conn
    acq.__aexit__.return_value = None
    mock_pool.acquire = MagicMock(return_value=acq)
    engine.pg_pool = mock_pool

    # MemoryOrchestrator lazy-init needs mongo_client
    engine.mongo_client = MagicMock()
    engine.mongo_client.memory_archive = MagicMock()

    # Mock fetchrow for namespace metadata
    mock_conn.fetchrow = AsyncMock(return_value={"metadata": {"temporal_retention_days": 90}})
    # Mock fetchval for embedding model
    mock_conn.fetchval = AsyncMock(return_value=None)
    # Mock fetch for the main query
    mock_conn.fetch = AsyncMock(return_value=[])

    as_of_dt = datetime.now(timezone.utc)

    # Invoke semantic search
    await engine.semantic_search(
        query="test query",
        namespace_id="00000000-0000-4000-8000-000000000001",
        agent_id="test_agent",
        limit=5,
        as_of=as_of_dt,
    )

    # Assert
    assert mock_conn.fetch.called
    call_args = mock_conn.fetch.call_args
    query_str = call_args[0][0]
    params = call_args[0][1:]

    # Security invariant: temporal values must NOT be interpolated into the query string.
    # If either assertion fails, a SQL injection vector has been reintroduced.
    assert "INTERVAL '90 days'" not in query_str, (
        "retention_days was interpolated as a literal interval string — "
        "must be passed as a positional parameter"
    )
    assert as_of_dt.isoformat() not in query_str, (
        "as_of datetime was interpolated as a literal string — "
        "must be passed as a positional parameter"
    )

    # Both typed values must appear somewhere in the parameter list.
    # We deliberately do NOT pin to specific indices — that is an implementation
    # detail of the query builder and would break whenever unrelated parameters
    # are added or reordered.
    assert 90 in params, "retention_days (90) not found in query parameters"
    assert as_of_dt in params, "as_of datetime not found in query parameters"
