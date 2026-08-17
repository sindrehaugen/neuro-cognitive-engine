"""
nce/causal/correlation.py
=========================
BATCH-P2-004 — Causal Inference Structural Topology

Implements Judea Pearl's do-calculus over the infrastructure topology_graph,
providing counterfactual "what-if" impact analysis for operational queries
such as "What services are impacted if switch_01 fails?".

Architecture overview
---------------------
                       topology_graph (Citus distributed)
                               │
                        CausalGraph.load()          ← read-only, no telemetry mutation
                               │
                         CausalGraph                ← in-memory DAG
                        (nodes + edges)
                               │
                  DoCalculusEngine.evaluate()
                               │
                   ┌───────────┴────────────┐
              Mutilate G             Path enumeration
          (sever causal           (direction-aware DFS
          causes of do(X))        per edge type semantics)
                               │
                      InterventionResult
                    ┌──────────┴──────────┐
             ImpactScore list       ConfoundingPath list
                               │
                    probability_matrix: dict[node_id, float]
                    (deterministic — same inputs → same output)

Edge direction semantics (TD-CAUSAL-1 / Option B)
--------------------------------------------------
The topology_graph uses four edge types with DIFFERENT causal directions:

  FORWARD-propagating failures (source → target direction):
    "connected_to"     A --connected_to--> B
                       A supplies/connects B. If A fails, B is impacted.
                       Failure propagates FORWARD (source → target).

    "host_application" A --host_application--> B
                       A hosts B. If A fails, B is impacted.
                       Failure propagates FORWARD (source → target).

  REVERSE-propagating failures (target → source direction):
    "depends_on"       A --depends_on--> B
                       A NEEDS B to function. If B fails, A is impacted.
                       Failure propagates BACKWARD (target is the cause).

    "powered_by"       A --powered_by--> B
                       A is powered by B. If B fails, A is impacted.
                       Failure propagates BACKWARD (target is the cause).

Pearl's do-operator (implemented in mutilate()):
  For forward types: sever edges WHERE TARGET = do(X) (things that cause X).
  For reverse types: sever edges WHERE SOURCE = do(X) (things X depends on).

Impact propagation (implemented in impacted_by()):
  For forward types: walk OUTGOING edges from a failing node.
  For reverse types: walk INCOMING edges of a failing node (find sources that depend on it).

Mathematical model
------------------
Probability of impact propagates multiplicatively along directed paths:

  P(Y impacted | do(X = failed)) = 1 - ∏ (1 - P(impact via path_i))

where each path's contribution is the product of edge confidence scores:

  P(impact via path p) = ∏ confidence_score(e) for e in p
                        × decay_factor(last_verified(e))

Decay factor applies the Ebbinghaus model from BATCH-P2-002:

  decay = exp(-t / S_TOPOLOGY_EDGE)   where S = 90 days

Confounding paths are backdoor paths (X ← ... → Y) that exist in the
original graph G but are severed in the mutilated graph G̃ = G_{do(X)}.
These are reported for operator awareness but do NOT contribute to the
final probability estimate (correctly isolated by the do() operator).

Design constraints
------------------
- Pure stdlib: only math, collections, dataclasses, uuid — no NumPy/SciPy.
- Immutable intervention: every do() call returns a new CausalGraph copy.
- No telemetry mutation: DB queries are SELECT-only (parameterised, no f-strings).
- Deterministic: sorted iteration over all collections.
- Cycle-safe: visited-set guard in all graph traversals.
"""

from __future__ import annotations

import logging
import math
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import NamedTuple

log = logging.getLogger("nce.causal")

# ---------------------------------------------------------------------------
# Ebbinghaus decay constants (shared with temporal_decay.py)
# ---------------------------------------------------------------------------

_TOPOLOGY_EDGE_STABILITY_DAYS: float = 90.0  # S value for infrastructure edges
_MIN_CONFIDENCE: float = 0.001  # floor to avoid zero-probability sinks

# ---------------------------------------------------------------------------
# Edge-type propagation direction constants (TD-CAUSAL-1 / Option B)
#
# FORWARD: failure of the SOURCE propagates to the TARGET.
# REVERSE: failure of the TARGET propagates to the SOURCE.
# ---------------------------------------------------------------------------

_FORWARD_FAILURE_TYPES: frozenset[str] = frozenset({"connected_to", "host_application"})
_REVERSE_FAILURE_TYPES: frozenset[str] = frozenset({"depends_on", "powered_by"})

# ---------------------------------------------------------------------------
# Graph data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CausalNode:
    """A node in the causal infrastructure graph."""

    node_id: str
    node_type: str  # "device" | "service" | "app" | "circuit"
    namespace_id: uuid.UUID


@dataclass(frozen=True)
class CausalEdge:
    """A directed causal edge: source_node_id → target_node_id.

    Edge semantics depend on edge_type — see module docstring for the full
    propagation direction table.
    """

    source_node_id: str
    target_node_id: str
    edge_type: str  # "connected_to" | "depends_on" | "host_application" | "powered_by"
    confidence_score: float  # Retention probability R ∈ (0, 1]
    decay_coefficient: float
    last_verified: datetime | None = None

    def decayed_confidence(self, _now: datetime | None = None) -> float:
        """Return confidence_score adjusted by Ebbinghaus decay since last_verified.

        Uses R = exp(-t / S_TOPOLOGY_EDGE) where t is days since last_verified.
        Falls back to confidence_score when last_verified is None.
        """
        if self.last_verified is None:
            return max(_MIN_CONFIDENCE, self.confidence_score)

        now = _now if _now is not None else datetime.now(timezone.utc)
        lv = (
            self.last_verified
            if self.last_verified.tzinfo is not None
            else self.last_verified.replace(tzinfo=timezone.utc)
        )
        elapsed_days = (now - lv).total_seconds() / 86_400.0
        decay = math.exp(-elapsed_days / _TOPOLOGY_EDGE_STABILITY_DAYS)
        raw = self.confidence_score * decay
        return max(_MIN_CONFIDENCE, min(1.0, raw))


# ---------------------------------------------------------------------------
# Probability result types
# ---------------------------------------------------------------------------


class ImpactScore(NamedTuple):
    """Impact probability for a single node under a do() intervention."""

    node_id: str
    node_type: str
    impact_probability: float  # P(Y impacted | do(X)) ∈ [0, 1]
    hop_distance: int  # shortest causal path length from intervention node
    paths_count: int  # number of independent causal paths found


class ConfoundingPath(NamedTuple):
    """A backdoor path that is severed by the do() operator.

    Represents a path through a common cause of the intervention node:
    confounder → ... → sink  (exists in G but severed in G̃).
    Reported for operator awareness — does NOT inflate impact estimates.
    """

    intervention_node: str  # the node that was intervened upon (do(X))
    sink: str  # node reachable through the confounder in G
    path: tuple[str, ...]  # ordered node sequence from confounder to sink
    raw_confidence: float  # product of edge confidences along this path


class InterventionResult(NamedTuple):
    """Full result of a do-calculus intervention evaluation."""

    intervention_node_id: str
    intervention_state: str  # e.g. "failed", "degraded"
    directly_impacted: list[ImpactScore]  # 1-hop impacted nodes
    transitively_impacted: list[ImpactScore]  # 2+-hop impacted nodes
    confounding_paths: list[ConfoundingPath]  # backdoor paths (severed by do())
    probability_matrix: dict[str, float]  # node_id → P(impact | do(X))
    truncated_targets: list[str] = []  # reachable but beyond max_path_depth


# ---------------------------------------------------------------------------
# CausalGraph: in-memory DAG
# ---------------------------------------------------------------------------


class CausalGraph:
    """
    Directed Acyclic Graph (DAG) for causal inference.

    Built from the topology_graph table via :meth:`load_from_db`.
    All mutations return NEW CausalGraph instances (immutable-style API)
    to preserve the original observational distribution.

    Edge direction semantics: see module docstring. In brief —
      FORWARD edge types (connected_to, host_application): source → target
      REVERSE edge types (depends_on, powered_by): target → source
    """

    def __init__(
        self,
        nodes: dict[str, CausalNode] | None = None,
        outgoing: dict[str, list[CausalEdge]] | None = None,
        incoming: dict[str, list[CausalEdge]] | None = None,
    ) -> None:
        self._nodes: dict[str, CausalNode] = nodes or {}
        # Adjacency: source → [edges pointing away from source]
        self._outgoing: dict[str, list[CausalEdge]] = outgoing or defaultdict(list)
        # Reverse adjacency: target → [edges pointing into target]
        self._incoming: dict[str, list[CausalEdge]] = incoming or defaultdict(list)

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    async def load_from_db(
        cls,
        conn,  # asyncpg.Connection
        namespace_id: uuid.UUID,
        *,
        active_only: bool = True,
    ) -> CausalGraph:
        """Load the topology_graph for *namespace_id* into memory.

        Args:
            conn:         asyncpg connection (caller manages transaction).
            namespace_id: Tenant UUID; enforces row-level isolation.
            active_only:  When True (default), skip soft-deleted edges
                          (valid_to IS NULL).

        Returns:
            CausalGraph populated from the current topology_graph rows.
            This is a READ-ONLY snapshot — no telemetry tables are modified.
        """
        # Check if a chrono-branch is active
        from nce.causal.chrono import get_active_branch

        branch = get_active_branch()

        if branch and branch.get("target_time"):
            target_time = branch["target_time"]
            rows = await conn.fetch(
                """
                SELECT
                    tg.source_node_id,
                    tg.source_node_type,
                    tg.target_node_id,
                    tg.target_node_type,
                    tg.edge_type,
                    tg.confidence_score,
                    tg.decay_coefficient,
                    tg.last_verified
                FROM topology_graph tg
                WHERE tg.namespace_id = $1
                  AND tg.valid_from <= $2::timestamptz
                  AND (tg.valid_to IS NULL OR tg.valid_to > $2::timestamptz)
                ORDER BY tg.source_node_id, tg.target_node_id
                """,
                namespace_id,
                target_time,
            )
            base_graph = cls.from_rows(rows, namespace_id)
            from nce.causal.chrono import apply_hypothetical_states

            return apply_hypothetical_states(base_graph, branch.get("hypothetical_states", {}))

        # Default query for current active graph
        rows = await conn.fetch(
            """
            SELECT
                tg.source_node_id,
                tg.source_node_type,
                tg.target_node_id,
                tg.target_node_type,
                tg.edge_type,
                tg.confidence_score,
                tg.decay_coefficient,
                tg.last_verified
            FROM topology_graph tg
            WHERE tg.namespace_id = $1
              AND ($2 IS FALSE OR tg.valid_to IS NULL)
            ORDER BY tg.source_node_id, tg.target_node_id
            """,
            namespace_id,
            active_only,
        )

        return cls.from_rows(rows, namespace_id)

    @classmethod
    def from_rows(
        cls,
        rows: list[dict],
        namespace_id: uuid.UUID,
    ) -> CausalGraph:
        """Build a CausalGraph from a list of row dicts (or asyncpg Records).

        Each row must have keys: source_node_id, source_node_type,
        target_node_id, target_node_type, edge_type, confidence_score,
        decay_coefficient, last_verified.

        Node type resolution (TD-CAUSAL-3 fix): when a node appears in multiple
        rows, source_node_type takes priority over target_node_type.  This is
        deterministic regardless of row ordering.

        Pure function — usable in tests without a DB connection.
        """
        # TD-CAUSAL-3: Collect type information separately so source_type wins.
        # A node may appear as both source (in some rows) and target (in others)
        # with different type strings. source_node_type is the canonical type —
        # it is set explicitly by the inserting system (e.g. NetBox topology seeder).
        source_types: dict[str, str] = {}  # node_id → type from source_node_type column
        target_types: dict[str, str] = {}  # node_id → type from target_node_type column

        edges: list[CausalEdge] = []

        for row in rows:
            src_id = row["source_node_id"]
            tgt_id = row["target_node_id"]
            source_types[src_id] = row["source_node_type"]
            if tgt_id not in source_types:  # don't overwrite a source_type entry
                target_types[tgt_id] = row["target_node_type"]
            edges.append(
                CausalEdge(
                    source_node_id=src_id,
                    target_node_id=tgt_id,
                    edge_type=row["edge_type"],
                    confidence_score=float(row["confidence_score"]),
                    decay_coefficient=float(row["decay_coefficient"]),
                    last_verified=row.get("last_verified"),
                )
            )

        # Merge: source_type beats target_type (last write wins for targets, but
        # any source_type registration immediately overwrites any target_type fallback)
        merged_types: dict[str, str] = {**target_types, **source_types}
        nodes: dict[str, CausalNode] = {
            nid: CausalNode(node_id=nid, node_type=ntype, namespace_id=namespace_id)
            for nid, ntype in merged_types.items()
        }

        outgoing: dict[str, list[CausalEdge]] = defaultdict(list)
        incoming: dict[str, list[CausalEdge]] = defaultdict(list)
        for edge in edges:
            outgoing[edge.source_node_id].append(edge)
            incoming[edge.target_node_id].append(edge)

        return cls(nodes=nodes, outgoing=outgoing, incoming=incoming)

    # ------------------------------------------------------------------
    # Core properties
    # ------------------------------------------------------------------

    @property
    def node_ids(self) -> frozenset[str]:
        return frozenset(self._nodes)

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    def get_node(self, node_id: str) -> CausalNode | None:
        return self._nodes.get(node_id)

    def outgoing_edges(self, node_id: str) -> list[CausalEdge]:
        """Return edges directed AWAY from node_id (i.e. node_id → X)."""
        return list(self._outgoing.get(node_id, []))

    def incoming_edges(self, node_id: str) -> list[CausalEdge]:
        """Return edges directed INTO node_id (i.e. X → node_id)."""
        return list(self._incoming.get(node_id, []))

    # ------------------------------------------------------------------
    # Pure topology traversal (direction-agnostic)
    # ------------------------------------------------------------------

    def descendants(self, node_id: str) -> set[str]:
        """Return all nodes reachable via OUTGOING edges (topology forward walk).

        This is a pure graph-structure traversal — it ignores edge_type semantics.
        For failure-propagation queries, use :meth:`impacted_by` instead.
        """
        result: set[str] = set()
        queue: deque[str] = deque([node_id])
        visited: set[str] = {node_id}

        while queue:
            current = queue.popleft()
            for edge in sorted(self._outgoing.get(current, []), key=lambda e: e.target_node_id):
                nxt = edge.target_node_id
                if nxt not in visited:
                    visited.add(nxt)
                    result.add(nxt)
                    queue.append(nxt)

        return result

    def ancestors(self, node_id: str) -> set[str]:
        """Return all nodes reachable via INCOMING edges (topology reverse walk).

        This is a pure graph-structure traversal — it ignores edge_type semantics.
        """
        result: set[str] = set()
        queue: deque[str] = deque([node_id])
        visited: set[str] = {node_id}

        while queue:
            current = queue.popleft()
            for edge in sorted(self._incoming.get(current, []), key=lambda e: e.source_node_id):
                anc = edge.source_node_id
                if anc not in visited:
                    visited.add(anc)
                    result.add(anc)
                    queue.append(anc)

        return result

    def find_all_paths(
        self,
        source: str,
        target: str,
        max_depth: int = 10,
    ) -> list[list[CausalEdge]]:
        """Return all simple paths from *source* to *target* via OUTGOING edges.

        Pure topology traversal — ignores edge_type semantics.
        For failure-propagation path enumeration, use :meth:`find_all_causal_paths`.
        Cycle-safe: no node appears twice in a single path.
        Sorted for determinism.
        """
        if source == target:
            return []

        results: list[list[CausalEdge]] = []
        stack: list[tuple[str, list[CausalEdge], frozenset[str]]] = [
            (source, [], frozenset({source}))
        ]

        while stack:
            current, path_edges, path_nodes = stack.pop()

            if current == target:
                results.append(list(path_edges))
                continue

            if len(path_edges) >= max_depth:
                continue

            for edge in sorted(
                self._outgoing.get(current, []),
                key=lambda e: e.target_node_id,
            ):
                nxt = edge.target_node_id
                if nxt not in path_nodes:
                    stack.append((nxt, path_edges + [edge], path_nodes | {nxt}))

        results.sort(key=lambda p: (len(p), [e.target_node_id for e in p]))
        return results

    # ------------------------------------------------------------------
    # Semantics-aware failure propagation (TD-CAUSAL-1 / Option B)
    # ------------------------------------------------------------------

    def impacted_by(self, failed_node: str) -> set[str]:
        """Return all nodes impacted when *failed_node* fails.

        Uses edge-type-aware BFS:

          FORWARD types (connected_to, host_application):
            Walk OUTGOING edges — targets of failed_node are impacted.
            Example: pdu --connected_to--> switch → switch is impacted.

          REVERSE types (depends_on, powered_by):
            Walk INCOMING edges — sources that depend on failed_node are impacted.
            Example: server --depends_on--> pdu → server is impacted when pdu fails.

        Propagation is transitive: if switch is impacted, its dependents are too.
        Cycle-safe via visited set.
        """
        result: set[str] = set()
        queue: deque[str] = deque([failed_node])
        visited: set[str] = {failed_node}

        while queue:
            node = queue.popleft()

            # Forward: this node's failure propagates to its targets
            for edge in sorted(self._outgoing.get(node, []), key=lambda e: e.target_node_id):
                if edge.edge_type in _FORWARD_FAILURE_TYPES and edge.target_node_id not in visited:
                    visited.add(edge.target_node_id)
                    result.add(edge.target_node_id)
                    queue.append(edge.target_node_id)

            # Reverse: this node's failure impacts sources that depend on it
            for edge in sorted(self._incoming.get(node, []), key=lambda e: e.source_node_id):
                if edge.edge_type in _REVERSE_FAILURE_TYPES and edge.source_node_id not in visited:
                    visited.add(edge.source_node_id)
                    result.add(edge.source_node_id)
                    queue.append(edge.source_node_id)

        return result

    def find_all_causal_paths(
        self,
        source: str,
        target: str,
        max_depth: int = 10,
    ) -> list[list[CausalEdge]]:
        """Return all simple failure-propagation paths from *source* to *target*.

        Direction-aware DFS: at each node, expands via:
          - Outgoing forward-type edges (connected_to, host_application)
          - Incoming reverse-type edges (depends_on, powered_by) — traversed
            backward to reach the source that is now impacted

        Cycle-safe and deterministic (sorted expansion).
        Returns a list of edge-lists (each list is one path).
        """
        if source == target:
            return []

        results: list[list[CausalEdge]] = []
        # Stack item: (current_node, edges_accumulated, nodes_in_path)
        stack: list[tuple[str, list[CausalEdge], frozenset[str]]] = [
            (source, [], frozenset({source}))
        ]

        while stack:
            current, path_edges, path_nodes = stack.pop()

            if current == target:
                results.append(list(path_edges))
                continue

            if len(path_edges) >= max_depth:
                continue

            # Forward edges: current → next (failure propagates to target)
            for edge in sorted(
                self._outgoing.get(current, []),
                key=lambda e: e.target_node_id,
            ):
                nxt = edge.target_node_id
                if edge.edge_type in _FORWARD_FAILURE_TYPES and nxt not in path_nodes:
                    stack.append((nxt, path_edges + [edge], path_nodes | {nxt}))

            # Reverse edges: next → current (next depends on current, so next is impacted)
            # We traverse the edge "backwards" to reach the dependent (source of the edge)
            for edge in sorted(
                self._incoming.get(current, []),
                key=lambda e: e.source_node_id,
            ):
                nxt = edge.source_node_id
                if edge.edge_type in _REVERSE_FAILURE_TYPES and nxt not in path_nodes:
                    stack.append((nxt, path_edges + [edge], path_nodes | {nxt}))

        # Sort for determinism: by path length, then by edge identities
        results.sort(
            key=lambda p: (
                len(p),
                [(e.source_node_id, e.target_node_id) for e in p],
            )
        )
        return results

    # ------------------------------------------------------------------
    # Intervention operator: do(X) — edge-type-aware (TD-CAUSAL-1)
    # ------------------------------------------------------------------

    def mutilate(self, node_id: str) -> CausalGraph:
        """Return G̃ = G_{do(node_id)}: sever all causal CAUSES of node_id.

        Pearl's do-operator forces a node to a specific value, removing all
        natural causal influences on it. In graph terms: delete all arrows
        that CAUSE node_id (from Pearl's causal perspective).

        Edge-type-aware severing (TD-CAUSAL-1):
          FORWARD types (connected_to, host_application):
            Sever edges WHERE TARGET = node_id.
            (These cause node_id via forward propagation.)

          REVERSE types (depends_on, powered_by):
            Sever edges WHERE SOURCE = node_id.
            (node_id depends on these targets — these are its causal parents.)

        Effects of node_id (forward targets it supplies, reverse sources that
        depend on it) are PRESERVED so failure still propagates downstream.

        The original graph is NEVER modified — a fresh instance is returned.

        Args:
            node_id: The node being intervened upon (e.g. "pdu-rack-12").

        Returns:
            Mutilated graph G̃ as a new CausalGraph instance.
        """
        new_outgoing: dict[str, list[CausalEdge]] = defaultdict(list)
        new_incoming: dict[str, list[CausalEdge]] = defaultdict(list)

        for src, edges in self._outgoing.items():
            for edge in edges:
                # Forward-type: sever if this edge's TARGET is the intervention node
                # (something was supplying/hosting node_id — that causal link is severed)
                if edge.edge_type in _FORWARD_FAILURE_TYPES and edge.target_node_id == node_id:
                    continue
                # Reverse-type: sever if this edge's SOURCE is the intervention node
                # (node_id was depending on something — those dependencies are severed)
                if edge.edge_type in _REVERSE_FAILURE_TYPES and edge.source_node_id == node_id:
                    continue
                new_outgoing[src].append(edge)
                new_incoming[edge.target_node_id].append(edge)

        return CausalGraph(
            nodes=dict(self._nodes),
            outgoing=new_outgoing,
            incoming=new_incoming,
        )


# ---------------------------------------------------------------------------
# Probability computation helpers (pure functions)
# ---------------------------------------------------------------------------


def _path_confidence(
    path: list[CausalEdge],
    _now: datetime | None = None,
) -> float:
    """Compute the joint confidence along a single causal path.

    P(impact via path) = ∏ edge.decayed_confidence() for each edge in path.
    Returns 0.0 for an empty path (no causal link).
    """
    if not path:
        return 0.0
    result = 1.0
    for edge in path:
        result *= edge.decayed_confidence(_now=_now)
    return result


def _combine_path_probabilities(path_probs: list[float]) -> float:
    """Combine independent path probabilities via inclusion-exclusion (union bound).

    P(A ∪ B) = P(A) + P(B) - P(A ∩ B) = P(A) + P(B)(1 - P(A)) for independent A, B.

    For N independent paths: P = 1 - ∏ (1 - P(path_i)).

    This gives the probability that AT LEAST ONE path successfully propagates
    the failure signal to the target node.
    """
    if not path_probs:
        return 0.0
    result = 1.0
    for p in path_probs:
        result *= 1.0 - p
    return 1.0 - result


# ---------------------------------------------------------------------------
# DoCalculusEngine
# ---------------------------------------------------------------------------


class DoCalculusEngine:
    """
    Pearl's do-calculus engine for counterfactual infrastructure impact analysis.

    Usage::

        graph = await CausalGraph.load_from_db(conn, namespace_id)
        engine = DoCalculusEngine()
        result = engine.evaluate(graph, intervention_node_id="pdu-rack-12")

        # Probability matrix: node_id → P(impacted | do(pdu-rack-12 = failed))
        for node_id, prob in sorted(result.probability_matrix.items()):
            print(f"{node_id:40s}  {prob:.4f}")

    The engine is STATELESS — the same instance can evaluate multiple
    interventions on multiple graphs concurrently without lock contention.
    """

    def evaluate(
        self,
        graph: CausalGraph,
        intervention_node_id: str,
        intervention_state: str = "failed",
        *,
        max_path_depth: int = 10,
        _now: datetime | None = None,
    ) -> InterventionResult:
        """
        Evaluate P(Y impacted | do(X = intervention_state)) for all Y in the graph.

        Algorithm:
          1. Validate that intervention_node_id exists in graph.
          2. Build mutilated graph G̃ via graph.mutilate(intervention_node_id).
          3. Find all nodes impacted by the intervention in G̃ via impacted_by().
          4. For each impacted node Y, enumerate all causal paths X → Y in G̃.
          5. Compute per-path confidence via edge decay model.
          6. Combine paths via independence union bound.
          7. Detect confounding paths: causal parents in G that were severed in G̃.
          8. Return deterministic InterventionResult with probability_matrix.

        Args:
            graph:                  Observational graph G (not mutilated).
            intervention_node_id:   Node being intervened upon (do(X = state)).
            intervention_state:     Human-readable label for the intervention.
            max_path_depth:         Maximum path length in causal path DFS.
            _now:                   Override current time (for decay calculation in tests).

        Returns:
            InterventionResult with complete probability_matrix.

        Raises:
            KeyError: if intervention_node_id is not found in graph.
        """
        if graph.get_node(intervention_node_id) is None:
            raise KeyError(
                f"Intervention node {intervention_node_id!r} not found in causal graph. "
                f"Available nodes: {sorted(graph.node_ids)[:10]}"
            )

        # Step 1: Build mutilated graph G̃
        mutilated = graph.mutilate(intervention_node_id)

        # Step 2: Identify nodes impacted in G̃ using direction-aware BFS
        impacted_in_mutilated = mutilated.impacted_by(intervention_node_id)

        # Step 3: Identify directly impacted nodes (1-hop via causal edges in G̃)
        direct_children: set[str] = set()
        # Forward: outgoing edges of forward-type from intervention node
        for edge in mutilated.outgoing_edges(intervention_node_id):
            if edge.edge_type in _FORWARD_FAILURE_TYPES:
                direct_children.add(edge.target_node_id)
        # Reverse: incoming edges of reverse-type into intervention node
        # (sources that depend on the intervention node are directly impacted)
        for edge in mutilated.incoming_edges(intervention_node_id):
            if edge.edge_type in _REVERSE_FAILURE_TYPES:
                direct_children.add(edge.source_node_id)

        # Step 4: Compute impact probability for each reachable impacted node
        probability_matrix: dict[str, float] = {}
        impact_scores: dict[str, ImpactScore] = {}
        _truncated: list[str] = []

        for target_id in sorted(impacted_in_mutilated):
            target_node = mutilated.get_node(target_id) or graph.get_node(target_id)
            target_type = target_node.node_type if target_node else "unknown"

            paths = mutilated.find_all_causal_paths(
                source=intervention_node_id,
                target=target_id,
                max_depth=max_path_depth,
            )

            if not paths:
                # Reachable via impacted_by() but every path exceeds max_path_depth.
                # Record as a truncated target rather than silently dropping it.
                _truncated.append(target_id)
                log.debug(
                    "Truncated target %s: reachable from %s but all paths exceed max_depth=%d",
                    target_id,
                    intervention_node_id,
                    max_path_depth,
                )
                continue

            path_probs = [_path_confidence(p, _now=_now) for p in paths]
            combined_prob = _combine_path_probabilities(path_probs)
            hop_distance = min(len(p) for p in paths)

            probability_matrix[target_id] = combined_prob
            impact_scores[target_id] = ImpactScore(
                node_id=target_id,
                node_type=target_type,
                impact_probability=combined_prob,
                hop_distance=hop_distance,
                paths_count=len(paths),
            )
            log.debug(
                "P(%s impacted | do(%s)) = %.4f via %d path(s)",
                target_id,
                intervention_node_id,
                combined_prob,
                len(paths),
            )

        # Step 5: Separate direct vs transitive
        directly_impacted = sorted(
            [s for nid, s in impact_scores.items() if nid in direct_children],
            key=lambda s: (-s.impact_probability, s.node_id),
        )
        transitively_impacted = sorted(
            [s for nid, s in impact_scores.items() if nid not in direct_children],
            key=lambda s: (-s.impact_probability, s.node_id),
        )

        # Step 6: Detect confounding paths (backdoor paths severed by do())
        confounding_paths = self._find_confounding_paths(
            graph=graph,
            mutilated=mutilated,
            intervention_node_id=intervention_node_id,
            impacted_in_mutilated=impacted_in_mutilated,
            max_depth=max_path_depth,
            _now=_now,
        )

        log.info(
            "do(%s = %s): %d directly impacted, %d transitively impacted, "
            "%d confounding paths, %d truncated targets",
            intervention_node_id,
            intervention_state,
            len(directly_impacted),
            len(transitively_impacted),
            len(confounding_paths),
            len(_truncated),
        )

        return InterventionResult(
            intervention_node_id=intervention_node_id,
            intervention_state=intervention_state,
            directly_impacted=directly_impacted,
            transitively_impacted=transitively_impacted,
            confounding_paths=confounding_paths,
            probability_matrix=probability_matrix,
            truncated_targets=sorted(set(_truncated)),
        )

    def _find_confounding_paths(
        self,
        graph: CausalGraph,
        mutilated: CausalGraph,
        intervention_node_id: str,
        impacted_in_mutilated: set[str],
        max_depth: int,
        _now: datetime | None,
    ) -> list[ConfoundingPath]:
        """Find backdoor paths that exist in G but are severed in G̃.

        A confounding path passes through a causal parent of the intervention node:
          parent → ... → sink  (in original graph G)
          where parent → intervention_node is severed by do().

        Causal parents of X in G are nodes that CAUSE X:
          - Sources of forward-type incoming edges (A --connected_to--> X: A causes X)
          - Targets of reverse-type outgoing edges (X --depends_on--> B: B causes X)
        """
        confounding: list[ConfoundingPath] = []

        # Collect all causal parents of the intervention node in G
        causal_parents: set[str] = set()
        for edge in graph.incoming_edges(intervention_node_id):
            if edge.edge_type in _FORWARD_FAILURE_TYPES:
                causal_parents.add(edge.source_node_id)
        for edge in graph.outgoing_edges(intervention_node_id):
            if edge.edge_type in _REVERSE_FAILURE_TYPES:
                causal_parents.add(edge.target_node_id)

        for parent_id in sorted(causal_parents):
            # From each severed parent, find nodes impacted in G but NOT in G̃
            parent_impacted = graph.impacted_by(parent_id)
            confounded_targets = parent_impacted - impacted_in_mutilated - {intervention_node_id}

            for target_id in sorted(confounded_targets):
                paths_via_parent = graph.find_all_causal_paths(
                    source=parent_id,
                    target=target_id,
                    max_depth=max_depth,
                )
                for path in paths_via_parent[:3]:  # Cap at 3 per (parent, target) pair
                    raw_conf = _path_confidence(path, _now=_now)
                    if raw_conf > 0.01:  # Only report meaningful confounders
                        node_sequence = (parent_id,) + tuple(
                            e.source_node_id
                            if e.edge_type in _REVERSE_FAILURE_TYPES
                            else e.target_node_id
                            for e in path
                        )
                        confounding.append(
                            ConfoundingPath(
                                intervention_node=intervention_node_id,
                                sink=target_id,
                                path=node_sequence,
                                raw_confidence=raw_conf,
                            )
                        )

        confounding.sort(key=lambda c: (c.sink, len(c.path), -c.raw_confidence))
        return confounding


# ---------------------------------------------------------------------------
# Convenience async entry point (wires DB load + evaluation in one call)
# ---------------------------------------------------------------------------


async def evaluate_intervention(
    conn,
    namespace_id: uuid.UUID,
    intervention_node_id: str,
    intervention_state: str = "failed",
    *,
    max_path_depth: int = 10,
    _now: datetime | None = None,
) -> InterventionResult:
    """
    Load the causal graph for *namespace_id* and evaluate a do-calculus intervention.

    Convenience wrapper around CausalGraph.load_from_db() + DoCalculusEngine.evaluate().
    The caller is responsible for providing a connection inside an active transaction.
    No historical telemetry tables are modified.

    Args:
        conn:                   asyncpg Connection (caller manages transaction).
        namespace_id:           Tenant UUID for topology isolation.
        intervention_node_id:   Node being intervened upon (e.g. "pdu-rack-12").
        intervention_state:     Human-readable label (e.g. "failed", "degraded").
        max_path_depth:         Maximum path length in causal path DFS.
        _now:                   Override current time (for decay calculation).

    Returns:
        InterventionResult with full probability_matrix.

    Example::

        async with pool.acquire() as conn:
            async with conn.transaction():
                result = await evaluate_intervention(
                    conn,
                    namespace_id=my_ns,
                    intervention_node_id="pdu-rack-12",
                    intervention_state="failed",
                )
                for node_id, prob in sorted(result.probability_matrix.items()):
                    if prob > 0.5:
                        print(f"HIGH RISK: {node_id}  P={prob:.3f}")
    """
    graph = await CausalGraph.load_from_db(conn, namespace_id)
    engine = DoCalculusEngine()
    return engine.evaluate(
        graph,
        intervention_node_id,
        intervention_state,
        max_path_depth=max_path_depth,
        _now=_now,
    )
