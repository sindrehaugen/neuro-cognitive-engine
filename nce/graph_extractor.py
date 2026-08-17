"""
Phase 0.2 — Graphify Layer: Entity & Relation Extraction
Extracts (subject, predicate, object) triplets from text for Knowledge Graph storage.
Primary backend: spaCy noun-chunk + dependency parsing (zero-shot, no LLM call needed).
Fallback: regex heuristic for environments without spaCy models installed.

Deduplication
-------------
Both backends normalise node labels to lowercase when tracking ``seen`` keys,
so ``"Redis"`` and ``"redis"`` from overlapping text chunks are treated as the
same entity.  The node label itself retains its original casing (first occurrence
wins for display; subsequent occurrences are discarded from the list).

For callers that call ``extract()`` on multiple overlapping chunks and want to
merge the results, use ``deduplicate_graph()``.  It:

* Merges nodes by normalised (lowercase, stripped) label — first label wins.
* Merges edges by normalised ``(subject_label, predicate, object_label)`` key.
  Duplicate edges accumulate their confidence scores (capped at 1.0) and record
  the occurrence count in ``edge.metadata["occurrences"]``.
"""

import asyncio
import logging
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

from nce.models import KGEdge, KGNode

log = logging.getLogger("nce-graphify")


_graph_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nce-graphify")


def shutdown_graph_executor() -> None:
    """Gracefully drain the graph executor. Call during app shutdown."""
    _graph_executor.shutdown(wait=False, cancel_futures=True)


def _add_unique_node(
    nodes: list[KGNode],
    seen: set[str],
    label: str,
    entity_type: str,
    source_text: str,
    *,
    min_len: int = 1,
) -> None:
    """Add a KGNode to the list if its normalised label is not already in seen."""
    cleaned = label.strip()
    key = cleaned.lower()
    if cleaned and len(cleaned) >= min_len and key not in seen:
        nodes.append(KGNode(label=cleaned, entity_type=entity_type, source_text=source_text))
        seen.add(key)


# --- spaCy backend ---


@lru_cache(maxsize=1)
def _get_spacy_model(model_name: str = "en_core_web_sm"):
    """Load and cache the spaCy model. Called once per process lifetime."""
    import spacy  # noqa: PLC0415

    return spacy.load(model_name)


def _spacy_extract(text: str) -> tuple[list[KGNode], list[KGEdge]]:
    """
    Uses spaCy en_core_web_sm NER + dependency parse.
    Returns (nodes, edges). Raises ImportError if spaCy unavailable.
    """
    try:
        nlp = _get_spacy_model()
    except OSError:
        raise ImportError(
            "spaCy model 'en_core_web_sm' not installed. Run: python -m spacy download en_core_web_sm"
        )

    doc = nlp(text[:4096])  # cap to prevent runaway on huge payloads

    # --- Entities from NER ---
    entity_type_map = {
        "ORG": "ORG",
        "PRODUCT": "TOOL",
        "PERSON": "PERSON",
        "GPE": "PLACE",
        "LOC": "PLACE",
        "WORK_OF_ART": "CONCEPT",
        "LANGUAGE": "CONCEPT",
        "LAW": "CONCEPT",
    }
    nodes: list[KGNode] = []
    seen: set[str] = set()
    for ent in doc.ents:
        _add_unique_node(
            nodes,
            seen,
            ent.text,
            entity_type_map.get(ent.label_, "UNKNOWN"),
            ent.text,
        )

    # Also capture noun chunks not caught by NER
    for chunk in doc.noun_chunks:
        _add_unique_node(
            nodes,
            seen,
            chunk.root.lemma_,
            "CONCEPT",
            chunk.text,
            min_len=3,
        )

    # --- Triplets from dependency parse (SVO extraction) ---
    edges: list[KGEdge] = []
    for token in doc:
        if token.pos_ == "VERB" and token.dep_ in ("ROOT", "relcl", "advcl"):
            subj = next((c for c in token.lefts if c.dep_ in ("nsubj", "nsubjpass")), None)
            obj = next(
                (c for c in token.rights if c.dep_ in ("dobj", "attr", "prep", "pobj")),
                None,
            )
            if subj and obj:
                edges.append(
                    KGEdge(
                        subject_label=subj.lemma_,
                        predicate=token.lemma_,
                        object_label=obj.lemma_,
                        confidence=0.85,
                    )
                )

    return nodes, edges


# --- Regex fallback ---

_IS_RELATION = re.compile(
    r"(\b\w[\w\s]{1,30}?\b)\s+(is|are|uses|has|contains|stores|connects to|depends on|runs on)\s+([\w][\w\s]{1,30}?\b)",
    re.IGNORECASE,
)
_KNOWN_TOOLS = {
    "redis",
    "postgres",
    "postgresql",
    "mongodb",
    "mongo",
    "docker",
    "python",
    "fastapi",
    "mcp",
    "nce",
    "pgvector",
    "tree-sitter",
}


def _regex_extract(text: str) -> tuple[list[KGNode], list[KGEdge]]:
    nodes: list[KGNode] = []
    edges: list[KGEdge] = []
    seen: set[str] = set()

    # Detect known tool names
    for word in re.findall(r"\b\w[\w\-]+\b", text):
        lower = word.lower()
        if lower in _KNOWN_TOOLS:
            _add_unique_node(nodes, seen, word, "TOOL", word)

    # Extract simple SVO triplets
    for m in _IS_RELATION.finditer(text):
        subj, pred, obj = (
            m.group(1).strip(),
            m.group(2).strip().lower(),
            m.group(3).strip(),
        )
        edges.append(KGEdge(subject_label=subj, predicate=pred, object_label=obj, confidence=0.6))
        for label in (subj, obj):
            _add_unique_node(nodes, seen, label, "CONCEPT", label)

    return nodes, edges


# --- Deduplication / merge ---


def deduplicate_graph(
    nodes: list[KGNode],
    edges: list[KGEdge],
    *,
    confidence_accumulator: Callable[[float, float], float] = lambda a, b: min(a + b, 1.0),
) -> tuple[list[KGNode], list[KGEdge]]:
    """
    Merge duplicate nodes and accumulate edge weights from overlapping extractions.

    Parameters
    ----------
    nodes:
        Combined node list from one or more ``extract()`` calls.
    edges:
        Combined edge list from one or more ``extract()`` calls.
    confidence_accumulator:
        Binary function ``(existing_confidence, new_confidence) -> merged_confidence``.
        Default: additive accumulation, capped at 1.0.
        Pass ``max`` to keep the highest confidence instead.

    Returns
    -------
    (deduped_nodes, merged_edges)
        Nodes deduplicated by ``label.lower().strip()``.  First label wins.
        Edges deduplicated by ``(subject_label.lower(), predicate.lower(),
        object_label.lower())``.  Confidence is accumulated; occurrence count
        is tracked in ``edge.metadata["occurrences"]``.

    Example
    -------
    Calling ``extract()`` on two overlapping text windows that both mention
    "Redis" produces two ``KGNode(label="Redis")`` entries.  After
    ``deduplicate_graph()`` the list contains exactly one node for "Redis".

    >>> from nce.graph_extractor import extract, deduplicate_graph
    >>> all_nodes, all_edges = [], []
    >>> for chunk in chunks:
    ...     n, e = extract(chunk)
    ...     all_nodes.extend(n)
    ...     all_edges.extend(e)
    >>> nodes, edges = deduplicate_graph(all_nodes, all_edges)
    """
    # --- Merge nodes (first-occurrence label wins) ---
    seen_node_key: dict[str, KGNode] = {}
    for node in nodes:
        key = node.label.lower().strip()
        if key not in seen_node_key:
            seen_node_key[key] = node

    merged_nodes = list(seen_node_key.values())

    # --- Merge edges (accumulate confidence, track occurrences) ---
    # Key: normalised (subject, predicate, object) triple.
    edge_index: dict[tuple[str, str, str], KGEdge] = {}

    for edge in edges:
        ekey: tuple[str, str, str] = (
            edge.subject_label.lower().strip(),
            edge.predicate.lower().strip(),
            edge.object_label.lower().strip(),
        )
        if ekey not in edge_index:
            # First occurrence — copy into index with occurrences=1.
            merged = KGEdge(
                subject_label=edge.subject_label,
                predicate=edge.predicate,
                object_label=edge.object_label,
                confidence=edge.confidence,
                payload_ref=edge.payload_ref,
                metadata={**edge.metadata, "occurrences": 1},
            )
            edge_index[ekey] = merged
        else:
            existing = edge_index[ekey]
            new_conf = confidence_accumulator(existing.confidence, edge.confidence)
            new_occ = existing.metadata.get("occurrences", 1) + 1
            edge_index[ekey] = KGEdge(
                subject_label=existing.subject_label,
                predicate=existing.predicate,
                object_label=existing.object_label,
                confidence=new_conf,
                payload_ref=existing.payload_ref,
                metadata={**existing.metadata, "occurrences": new_occ},
            )

    merged_edges = list(edge_index.values())
    return merged_nodes, merged_edges


def _spacy_extract_remote(text: str, base_url: str) -> tuple[list[KGNode], list[KGEdge]]:
    """Send text to cognitive sidecar for spaCy entity and triplet extraction."""
    import httpx

    from nce.http_resilience import request_with_retry_sync

    url = f"{base_url}/v1/nlp/spacy"
    with httpx.Client(timeout=30.0) as client:
        resp = request_with_retry_sync(
            client,
            "POST",
            url,
            json={"text": text},
            operation_name="nlp_sidecar:spacy",
        )
        data = resp.json()

    nodes = []
    for n in data.get("nodes", []):
        nodes.append(
            KGNode(
                label=n["label"],
                entity_type=n["entity_type"],
                source_text=n["source_text"],
            )
        )
    edges = []
    for e in data.get("edges", []):
        edges.append(
            KGEdge(
                subject_label=e["subject_label"],
                predicate=e["predicate"],
                object_label=e["object_label"],
                confidence=e.get("confidence", 0.85),
            )
        )
    return nodes, edges


# --- Public API ---


def extract(text: str) -> tuple[list[KGNode], list[KGEdge]]:
    """
    Extract entities and triplets from text.
    Tries spaCy first; silently falls back to regex heuristic.

    Node labels are deduplicated within this single call using case-insensitive
    comparison (``"Redis"`` and ``"redis"`` are treated as the same node).

    For merging results from multiple ``extract()`` calls on overlapping chunks,
    use ``deduplicate_graph()``.
    """
    try:
        from nce.embeddings import validated_cognitive_base_url

        base_url = validated_cognitive_base_url()
    except Exception:
        base_url = ""

    try:
        if base_url:
            nodes, edges = _spacy_extract_remote(text, base_url)
            log.debug("spaCy (remote) extracted %d nodes, %d edges.", len(nodes), len(edges))
        else:
            nodes, edges = _spacy_extract(text)
            log.debug("spaCy (local) extracted %d nodes, %d edges.", len(nodes), len(edges))
        return nodes, edges
    except (ImportError, Exception) as e:
        log.info("spaCy unavailable (%s), using regex fallback.", e)
        nodes, edges = _regex_extract(text)
        log.debug("Regex extracted %d nodes, %d edges.", len(nodes), len(edges))
        return nodes, edges


async def extract_async(text: str) -> tuple[list[KGNode], list[KGEdge]]:
    """
    Extract entities and triplets from text asynchronously.
    Offloads CPU-bound spaCy / Regex operations to a dedicated thread pool.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_graph_executor, extract, text)
