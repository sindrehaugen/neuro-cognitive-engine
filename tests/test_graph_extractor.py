from unittest.mock import patch

import pytest

from nce.graph_extractor import _regex_extract, deduplicate_graph, extract, extract_async
from nce.models import KGEdge, KGNode

# ---------------------------------------------------------------------------
# Existing extraction tests (unchanged)
# ---------------------------------------------------------------------------


def test_regex_extract():
    text = "Redis connects to PostgreSQL"
    entities, triplets = _regex_extract(text)

    assert len(entities) >= 2
    assert any(e.label.lower() == "redis" for e in entities)
    assert any(e.label.lower() == "postgresql" for e in entities)

    assert len(triplets) == 1
    assert triplets[0].subject_label.lower() == "redis"
    assert triplets[0].predicate == "connects to"
    assert triplets[0].object_label.lower() == "postgresql"


@patch("nce.graph_extractor._spacy_extract")
def test_extract_uses_spacy(mock_spacy):
    mock_entities = [KGNode(label="A", entity_type="CONCEPT", source_text="A")]
    mock_triplets = [KGEdge(subject_label="A", predicate="is", object_label="B")]
    mock_spacy.return_value = (mock_entities, mock_triplets)

    text = "A is B"
    entities, triplets = extract(text)

    mock_spacy.assert_called_once_with(text)
    assert entities == mock_entities
    assert triplets == mock_triplets


@patch("nce.graph_extractor._spacy_extract")
@patch("nce.graph_extractor._regex_extract")
def test_extract_falls_back_to_regex(mock_regex, mock_spacy):
    mock_spacy.side_effect = ImportError("spacy not installed")

    mock_entities = [KGNode(label="A", entity_type="CONCEPT", source_text="A")]
    mock_triplets = [KGEdge(subject_label="A", predicate="is", object_label="B")]
    mock_regex.return_value = (mock_entities, mock_triplets)

    text = "A is B"
    entities, triplets = extract(text)

    mock_spacy.assert_called_once_with(text)
    mock_regex.assert_called_once_with(text)
    assert entities == mock_entities
    assert triplets == mock_triplets


# ---------------------------------------------------------------------------
# Case-normalisation tests (regression guard for intra-call dedup fix)
# ---------------------------------------------------------------------------


class TestCaseNormalisation:
    """Verify that the regex backend deduplicates case-variants of known tools."""

    def test_redis_casing_variants_produce_single_node(self):
        """'Redis', 'REDIS', and 'redis' in the same text → one node."""
        text = "Redis is great. REDIS is fast. redis uses memory."
        nodes, _ = _regex_extract(text)
        redis_nodes = [n for n in nodes if n.label.lower() == "redis"]
        assert len(redis_nodes) == 1, (
            f"Expected 1 Redis node, got {len(redis_nodes)}: {[n.label for n in redis_nodes]}"
        )

    def test_postgres_casing_variants_produce_single_node(self):
        """'PostgreSQL' and 'postgresql' → one node."""
        text = "PostgreSQL stores data. postgresql uses pgvector."
        nodes, _ = _regex_extract(text)
        pg_nodes = [n for n in nodes if n.label.lower() in ("postgres", "postgresql")]
        assert len(pg_nodes) == 1, (
            f"Expected 1 Postgres node, got {len(pg_nodes)}: {[n.label for n in pg_nodes]}"
        )

    def test_first_occurrence_label_wins(self):
        """Original casing of the first match is preserved as the node label."""
        text = "Redis is fast. REDIS is cheap."
        nodes, _ = _regex_extract(text)
        redis_nodes = [n for n in nodes if n.label.lower() == "redis"]
        assert len(redis_nodes) == 1
        # First occurrence in text is "Redis" (title-case)
        assert redis_nodes[0].label == "Redis"


# ---------------------------------------------------------------------------
# deduplicate_graph — node deduplication tests
# ---------------------------------------------------------------------------


class TestDeduplicateGraphNodes:
    """Node deduplication by normalised label."""

    def _make_node(self, label: str, entity_type: str = "CONCEPT") -> KGNode:
        return KGNode(label=label, entity_type=entity_type, source_text=label)

    def test_identical_labels_collapsed_to_one(self):
        nodes = [self._make_node("Redis"), self._make_node("Redis")]
        merged, _ = deduplicate_graph(nodes, [])
        assert len(merged) == 1
        assert merged[0].label == "Redis"

    def test_case_variant_labels_collapsed(self):
        """'Redis', 'redis', 'REDIS' → single node, first label wins."""
        nodes = [
            self._make_node("Redis"),
            self._make_node("redis"),
            self._make_node("REDIS"),
        ]
        merged, _ = deduplicate_graph(nodes, [])
        assert len(merged) == 1
        assert merged[0].label == "Redis"  # first occurrence wins

    def test_distinct_labels_all_kept(self):
        nodes = [self._make_node("Redis"), self._make_node("MongoDB")]
        merged, _ = deduplicate_graph(nodes, [])
        assert len(merged) == 2
        labels = {n.label for n in merged}
        assert labels == {"Redis", "MongoDB"}

    def test_graph_size_shrinks_after_overlapping_chunks(self):
        """Simulates two overlapping text chunks both extracting Redis+Postgres."""
        chunk_a_nodes = [self._make_node("Redis"), self._make_node("Postgres")]
        chunk_b_nodes = [self._make_node("redis"), self._make_node("postgres")]
        all_nodes = chunk_a_nodes + chunk_b_nodes

        merged, _ = deduplicate_graph(all_nodes, [])
        assert len(merged) == 2, (
            f"Expected 2 distinct nodes (Redis, Postgres), got {len(merged)}: "
            f"{[n.label for n in merged]}"
        )

    def test_empty_input_returns_empty(self):
        merged_nodes, merged_edges = deduplicate_graph([], [])
        assert merged_nodes == []
        assert merged_edges == []


# ---------------------------------------------------------------------------
# deduplicate_graph — edge weight accumulation tests
# ---------------------------------------------------------------------------


class TestDeduplicateGraphEdgeWeights:
    """Edge deduplication with confidence accumulation and occurrence tracking."""

    def _make_edge(self, subj: str, pred: str, obj: str, conf: float = 0.6) -> KGEdge:
        return KGEdge(subject_label=subj, predicate=pred, object_label=obj, confidence=conf)

    def test_duplicate_edges_merged(self):
        edges = [
            self._make_edge("Redis", "stores", "data"),
            self._make_edge("Redis", "stores", "data"),
        ]
        _, merged = deduplicate_graph([], edges)
        assert len(merged) == 1

    def test_edge_confidence_accumulates(self):
        """Two identical edges with confidence 0.6 → merged confidence 1.0 (capped)."""
        edges = [
            self._make_edge("Redis", "stores", "data", conf=0.6),
            self._make_edge("Redis", "stores", "data", conf=0.6),
        ]
        _, merged = deduplicate_graph([], edges)
        assert len(merged) == 1
        # 0.6 + 0.6 = 1.2, capped at 1.0
        assert merged[0].confidence == pytest.approx(1.0)

    def test_occurrence_count_tracked_in_metadata(self):
        """Occurrences counter increments for each duplicate."""
        edges = [
            self._make_edge("A", "uses", "B"),
            self._make_edge("A", "uses", "B"),
            self._make_edge("A", "uses", "B"),
        ]
        _, merged = deduplicate_graph([], edges)
        assert len(merged) == 1
        assert merged[0].metadata.get("occurrences") == 3

    def test_single_edge_has_occurrence_count_one(self):
        edges = [self._make_edge("A", "is", "B")]
        _, merged = deduplicate_graph([], edges)
        assert merged[0].metadata.get("occurrences") == 1

    def test_case_normalised_edge_keys_merged(self):
        """'Redis stores Data' and 'redis stores data' are the same edge."""
        edges = [
            self._make_edge("Redis", "stores", "Data", conf=0.5),
            self._make_edge("redis", "stores", "data", conf=0.5),
        ]
        _, merged = deduplicate_graph([], edges)
        assert len(merged) == 1
        assert merged[0].confidence == pytest.approx(1.0)

    def test_distinct_edges_all_kept(self):
        edges = [
            self._make_edge("Redis", "stores", "data"),
            self._make_edge("Mongo", "stores", "files"),
        ]
        _, merged = deduplicate_graph([], edges)
        assert len(merged) == 2

    def test_custom_max_accumulator(self):
        """Passing max as accumulator keeps highest confidence, no sum."""
        edges = [
            self._make_edge("A", "is", "B", conf=0.4),
            self._make_edge("A", "is", "B", conf=0.9),
        ]
        _, merged = deduplicate_graph([], edges, confidence_accumulator=max)
        assert len(merged) == 1
        assert merged[0].confidence == pytest.approx(0.9)

    def test_overlapping_chunks_shrink_edge_count(self):
        """
        Two chunks both extract Redis-stores-data → one merged edge,
        not two separate edges.
        """
        chunk_a_edges = [self._make_edge("Redis", "stores", "data", conf=0.6)]
        chunk_b_edges = [self._make_edge("redis", "stores", "data", conf=0.6)]
        all_edges = chunk_a_edges + chunk_b_edges

        _, merged = deduplicate_graph([], all_edges)
        assert len(merged) == 1, (
            f"Expected 1 merged edge but got {len(merged)} — "
            "overlapping chunk deduplication is broken"
        )
        assert merged[0].metadata["occurrences"] == 2


@pytest.mark.anyio
async def test_extract_async():
    """Verify that extract_async offloads successfully and yields expected nodes/edges."""
    text = "Redis connects to PostgreSQL"
    entities, triplets = await extract_async(text)

    assert len(entities) >= 2
    assert any(e.label.lower() == "redis" for e in entities)
    assert any(e.label.lower() == "postgresql" for e in entities)

    assert len(triplets) == 1
    assert triplets[0].subject_label.lower() == "redis"
    assert triplets[0].predicate == "connects to"
    assert triplets[0].object_label.lower() == "postgresql"
