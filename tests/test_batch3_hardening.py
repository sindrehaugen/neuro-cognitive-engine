import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from nce.consolidation import ConsolidationWorker
from nce.embeddings import VECTOR_DIM
from nce.graph_query import GraphRAGTraverser
from nce.models import MAX_GRAPH_DEPTH, MAX_GRAPH_EDGE_PAGE
from nce.temporal import _assert_not_future

pytestmark = pytest.mark.filterwarnings("ignore::FutureWarning")


def test_temporal_clock_skew_tolerance():
    now = datetime.now(timezone.utc)

    # 4 seconds in the future should pass because of the 5s tolerance
    future_ok = now + timedelta(seconds=4)
    # This should not raise ValueError
    _assert_not_future(future_ok, now, "timestamp", allow_skew=True)

    # 6 seconds in the future should fail
    future_bad = now + timedelta(seconds=6)
    with pytest.raises(ValueError, match="timestamp must not be in the future"):
        _assert_not_future(future_bad, now, "timestamp")


def test_graph_query_validation():
    # We can mock the required attributes to test parameter validation
    traverser = GraphRAGTraverser(pg_pool=None, mongo_client=None, embedding_fn=None)

    # Valid params should not raise error
    traverser._validate_search_params(
        namespace_id=uuid4(),
        as_of=None,
        max_edges_per_node=5,
        edge_offset=0,
        edge_limit=10,
        method_name="test",
        _allow_global_sweep=False,
        max_depth=2,
    )

    # Invalid max_depth > MAX_GRAPH_DEPTH (3)
    with pytest.raises(ValueError, match=f"max_depth must be between 1 and {MAX_GRAPH_DEPTH}"):
        traverser._validate_search_params(
            namespace_id=uuid4(),
            as_of=None,
            max_edges_per_node=5,
            edge_offset=0,
            edge_limit=10,
            method_name="test",
            _allow_global_sweep=False,
            max_depth=4,
        )

    # Invalid max_depth < 1
    with pytest.raises(ValueError, match=f"max_depth must be between 1 and {MAX_GRAPH_DEPTH}"):
        traverser._validate_search_params(
            namespace_id=uuid4(),
            as_of=None,
            max_edges_per_node=5,
            edge_offset=0,
            edge_limit=10,
            method_name="test",
            _allow_global_sweep=False,
            max_depth=0,
        )

    # Invalid edge_limit > MAX_GRAPH_EDGE_PAGE (1000)
    with pytest.raises(ValueError, match=f"edge_limit must be between 1 and {MAX_GRAPH_EDGE_PAGE}"):
        traverser._validate_search_params(
            namespace_id=uuid4(),
            as_of=None,
            max_edges_per_node=5,
            edge_offset=0,
            edge_limit=1001,
            method_name="test",
            _allow_global_sweep=False,
            max_depth=2,
        )


@pytest.mark.heavy
@pytest.mark.asyncio
async def test_consolidation_embedding_hardening():
    worker = ConsolidationWorker(pool=None, provider=None, mongo_client=None)

    valid_vector = [0.1] * VECTOR_DIM
    invalid_dim_vector = [0.1] * (VECTOR_DIM - 1)
    non_finite_vector = [float('nan')] + [0.1] * (VECTOR_DIM - 1)

    memories = [
        {"id": uuid4(), "embedding": json.dumps(valid_vector)},
        {"id": uuid4(), "embedding": json.dumps(valid_vector)},
        {"id": uuid4(), "embedding": "invalid json string"},
        {"id": uuid4(), "embedding": json.dumps(invalid_dim_vector)},
        {"id": uuid4(), "embedding": json.dumps(non_finite_vector)},
    ]

    valid_mems, clusters = await worker._cluster_memories_async(memories)
    # Check that only the 2 valid memories remain
    assert len(valid_mems) == 2
    # And clusters is a dict (even if they form one cluster or -1, HDBSCAN won't crash)
    assert isinstance(clusters, dict)
