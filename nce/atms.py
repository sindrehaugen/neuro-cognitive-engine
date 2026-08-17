"""
nce/atms.py
===========
BATCH-P3-001 — Assumption-Based Truth Maintenance System (ATMS)

Implements logical justification tracking, nogood environment recording,
and iterative deprecation cascades for memory nodes and infrastructure
topology entities when underlying assumptions are violated.

Integrates with Judea Pearl's do-calculus CausalGraph from `nce/causal/correlation.py`
to propagate invalidations downstream using directional graph semantics.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from nce.causal.correlation import _FORWARD_FAILURE_TYPES, _REVERSE_FAILURE_TYPES, CausalGraph

log = logging.getLogger("nce.atms")


class ATMSNodeType(str, Enum):
    """Classification of nodes within the ATMS.

    - ASSUMPTION: Baseline belief state that can be directly invalidated/asserted.
    - PREMISE: Fact that is unconditionally and eternally valid.
    - DERIVED: Fact whose validity depends on at least one justification.
    """

    ASSUMPTION = "assumption"
    PREMISE = "premise"
    DERIVED = "derived"


@dataclass(frozen=True)
class Justification:
    """A logical support link: antecedents -> consequent.

    If all antecedents are valid, the consequent receives support to be valid.
    """

    consequent: str
    antecedents: frozenset[str]
    description: str = ""


@dataclass
class ATMSNode:
    """A node tracked by the Truth Maintenance System."""

    node_id: str
    node_type: ATMSNodeType
    is_valid: bool = True
    justifications: list[Justification] = field(default_factory=list)


class ATMSEngine:
    """Assumption-Based Truth Maintenance System Engine.

    Manages logical dependencies (justifications) between nodes, tracks contradictions,
    and runs iterative deprecation cascades when base assumptions fail.
    """

    def __init__(self, namespace_id: uuid.UUID | None = None) -> None:
        self.namespace_id = namespace_id
        self.nodes: dict[str, ATMSNode] = {}
        self.contradictions: list[tuple[str, str]] = []
        # Reverse dependency index: antecedent_id -> set of DERIVED consequent_ids.
        # Built incrementally by add_justification; used by propagate_deprecation to
        # limit the child scan to O(direct dependents) rather than O(all nodes).
        self._dependents: dict[str, set[str]] = {}

    def register_node(
        self,
        node_id: str,
        node_type: ATMSNodeType | str,
        is_valid: bool = True,
    ) -> ATMSNode:
        """Registers a node in the ATMS. If already registered, updates its type/validity."""
        ntype = ATMSNodeType(node_type) if isinstance(node_type, str) else node_type
        if node_id in self.nodes:
            node = self.nodes[node_id]
            node.node_type = ntype
            node.is_valid = is_valid
        else:
            node = ATMSNode(node_id=node_id, node_type=ntype, is_valid=is_valid)
            self.nodes[node_id] = node
        return node

    def add_justification(
        self,
        consequent_id: str,
        antecedents: set[str] | frozenset[str],
        description: str = "",
    ) -> Justification:
        """Adds a logical justification for a consequent node."""
        if consequent_id not in self.nodes:
            self.register_node(consequent_id, ATMSNodeType.DERIVED)

        for ant in antecedents:
            if ant not in self.nodes:
                self.register_node(ant, ATMSNodeType.ASSUMPTION)

        just = Justification(
            consequent=consequent_id,
            antecedents=frozenset(antecedents),
            description=description,
        )
        self.nodes[consequent_id].justifications.append(just)
        # Update the reverse dependency index
        for ant in antecedents:
            self._dependents.setdefault(ant, set()).add(consequent_id)
        return just

    def is_node_provably_valid(
        self,
        node_id: str,
        active_path: set[str],
        memo: dict[str, bool] | None = None,
    ) -> bool:
        """Iteratively checks if a node is logically valid based on active assumptions and premises.

        Detects self-supporting cycles (circular justifications) and marks them invalid.
        Optimized with a memoization cache for acyclic sub-graphs.

        Uses an explicit work stack to avoid Python call-stack overflow on deep justification
        chains.  Preserves the exact cycle-guard and memoization semantics of the original
        recursive implementation:

        - active_path tracks the current derivation path for cycle detection.
        - memo is only consulted / written when active_path is empty (the caller's path was
          empty), matching the original ``if memo is not None and not active_path`` guard.
        - A DERIVED node is True iff it has at least one justification whose every antecedent
          evaluates to True under the path extended with the current node.

        Frame layout (each frame is a list for in-place mutation):
          [0] nid       : str            - node being evaluated
          [1] path      : set[str]       - active_path INCLUDING nid (for its antecedents)
          [2] just_idx  : int            - index of justification currently being checked
          [3] ants      : list[str]      - antecedents of current justification (stable order)
          [4] ant_idx   : int            - index of next antecedent to evaluate
          [5] ant_ok    : bool           - all antecedents evaluated so far were True
          [6] found     : bool           - a fully-satisfied justification was found
        """

        # ------------------------------------------------------------------
        # Leaf resolution: can this node be decided without a recursive DFS?
        # ------------------------------------------------------------------
        def _leaf(nid: str, path: set[str]) -> tuple[bool, bool]:
            """Return (is_leaf, value).  is_leaf=True means no stack frame needed.

            Memo semantics (matching original):
            - memo[nid] = True  is only safe when path is empty (no cycle constraints).
            - memo[nid] = False is safe in ANY path context: if a node is unprovable
              with zero constraints, it is also unprovable with additional path constraints
              (more constraints can only eliminate proofs, never add them).
            """
            if memo is not None and nid in memo:
                cached = memo[nid]
                if not cached:
                    # False is monotone: safe to use regardless of current path.
                    return True, False
                if not path:
                    # True is only safe when we have no extra cycle constraints.
                    return True, True
            n = self.nodes.get(nid)
            if n is None:
                return True, False
            if n.node_type == ATMSNodeType.PREMISE:
                return True, True
            if n.node_type == ATMSNodeType.ASSUMPTION:
                return True, n.is_valid
            if nid in path:
                return True, False
            return False, False  # DERIVED, not in path -> need full eval

        # Fast-path the root call itself
        is_leaf, leaf_val = _leaf(node_id, active_path)
        if is_leaf:
            return leaf_val

        # ------------------------------------------------------------------
        # Build the initial stack frame for node_id.
        # ------------------------------------------------------------------
        def _make_frame(nid: str, path: set[str]) -> list[object]:
            n = self.nodes[nid]  # caller guarantees nid is a DERIVED non-cycle node
            ants: list[str] = list(n.justifications[0].antecedents) if n.justifications else []
            return [nid, path, 0, ants, 0, True, False]

        # ret_stack holds the bool returned by the most recently completed child frame.
        # At any point there is at most ONE unconsumed value in it (LIFO discipline).
        work_stack: list[list[object]] = [_make_frame(node_id, active_path | {node_id})]
        ret_stack: list[bool] = []

        while work_stack:
            frame = work_stack[-1]
            nid: str = frame[0]  # type: ignore[assignment]
            path: set[str] = frame[1]  # type: ignore[assignment]
            just_idx: int = frame[2]  # type: ignore[assignment]
            ants: list[str] = frame[3]  # type: ignore[assignment]
            ant_idx: int = frame[4]  # type: ignore[assignment]
            ant_ok: bool = frame[5]  # type: ignore[assignment]
            found: bool = frame[6]  # type: ignore[assignment]

            cur_node = self.nodes.get(nid)
            if cur_node is None:
                work_stack.pop()
                ret_stack.append(False)
                continue

            justifications = cur_node.justifications

            # If a child frame just completed, incorporate its result into ant_ok.
            if ret_stack:
                child_result = ret_stack.pop()
                ant_ok = ant_ok and child_result
                frame[5] = ant_ok
                # Short-circuit: if the child antecedent was False, skip to next just.
                if not ant_ok:
                    just_idx += 1
                    if just_idx < len(justifications):
                        ants = list(justifications[just_idx].antecedents)
                    else:
                        ants = []
                    ant_idx = 0
                    ant_ok = True
                    frame[2] = just_idx
                    frame[3] = ants
                    frame[4] = ant_idx
                    frame[5] = ant_ok

            # Advance through antecedents / justifications until we need a child call or finish.
            suspended = False
            while not found and just_idx < len(justifications):
                while ant_idx < len(ants):
                    ant = ants[ant_idx]
                    is_leaf2, leaf_val2 = _leaf(ant, path)
                    if is_leaf2:
                        ant_ok = ant_ok and leaf_val2
                        if not ant_ok:
                            # Justification fails; advance to next one.
                            break
                        ant_idx += 1
                        frame[4] = ant_idx
                        frame[5] = ant_ok
                        continue

                    # Need a full DFS for this antecedent -- push a child frame.
                    frame[2] = just_idx
                    frame[3] = ants
                    frame[4] = ant_idx + 1  # resume after this ant when child returns
                    frame[5] = ant_ok
                    frame[6] = found
                    work_stack.append(_make_frame(ant, path | {ant}))
                    suspended = True
                    break

                if suspended:
                    break

                if ant_idx >= len(ants):
                    # All antecedents for this justification were processed.
                    if ant_ok:
                        found = True
                        frame[6] = found
                        break
                    # Justification failed; move to the next one.
                    just_idx += 1
                    if just_idx < len(justifications):
                        ants = list(justifications[just_idx].antecedents)
                    else:
                        ants = []
                    ant_idx = 0
                    ant_ok = True
                    frame[2] = just_idx
                    frame[3] = ants
                    frame[4] = ant_idx
                    frame[5] = ant_ok
                else:
                    # ant_ok went False mid-list; advance to next justification.
                    just_idx += 1
                    if just_idx < len(justifications):
                        ants = list(justifications[just_idx].antecedents)
                    else:
                        ants = []
                    ant_idx = 0
                    ant_ok = True
                    frame[2] = just_idx
                    frame[3] = ants
                    frame[4] = ant_idx
                    frame[5] = ant_ok

            if not suspended:
                # This frame is done -- pop and report result to caller.
                work_stack.pop()
                result = found
                # Memo semantics (mirrors original):
                #   memo[nid] is written only when path == {nid}, meaning the
                #   caller's active_path was empty when it initiated this frame.
                #   For sub-frames the caller had a non-empty path, so path != {nid}.
                if memo is not None and path == {nid}:
                    memo[nid] = result
                ret_stack.append(result)

        return ret_stack[-1] if ret_stack else False

    def invalidate_assumption(self, assumption_id: str) -> set[str]:
        """Invalidates an assumption and cascades deprecation downstream.

        Returns the set of all node IDs affected (invalidated) by the cascade.
        """
        node = self.nodes.get(assumption_id)
        if not node:
            log.warning("Node %s not found in ATMS", assumption_id)
            return set()

        if node.node_type == ATMSNodeType.PREMISE:
            log.warning("Cannot invalidate PREMISE node %s", assumption_id)
            return set()

        return self.propagate_deprecation(assumption_id)

    def register_contradiction(
        self,
        node_a_id: str,
        node_b_id: str,
        resolution_strategy: str = "invalidate_a",
    ) -> set[str]:
        """Registers a contradiction (nogood environment) between two nodes.

        Applies the selected resolution strategy to invalidate the target baseline
        belief and cascades invalidation downstream.
        """
        self.contradictions.append((node_a_id, node_b_id))
        self.add_justification("FALSE", {node_a_id, node_b_id}, "Contradiction")

        node_a = self.nodes.get(node_a_id)
        node_b = self.nodes.get(node_b_id)

        cascade_set: set[str] = set()
        if node_a and node_b and node_a.is_valid and node_b.is_valid:
            if resolution_strategy == "invalidate_a":
                cascade_set.update(self.invalidate_assumption(node_a_id))
            elif resolution_strategy == "invalidate_b":
                cascade_set.update(self.invalidate_assumption(node_b_id))
            elif resolution_strategy == "invalidate_both":
                cascade_set.update(self.invalidate_assumption(node_a_id))
                cascade_set.update(self.invalidate_assumption(node_b_id))

        return cascade_set

    def propagate_deprecation(self, node_id: str, visited: set[str] | None = None) -> set[str]:
        """Iteratively flags all downstream dependent nodes linked to an invalidated node.

        Ensures cycle-safety via visited-set tracking and proof re-checking.  Uses an
        explicit worklist instead of Python recursion so arbitrarily deep chains do not
        overflow the C stack.

        Preserves the exact semantics of the original recursive implementation:
        - visited guards prevent re-processing a node in this cascade.
        - A derived node is added to the worklist iff is_node_provably_valid(..., set(), memo)
          returns False (same re-proof check as the original).
        - The memo dict is shared across the whole propagation pass (same as before).
        - Returns the set of node IDs whose is_valid flipped from True -> False.
        """
        if visited is None:
            visited = set()

        cascade_set: set[str] = set()
        # Evaluation memoization cache shared across the whole propagation pass
        memo: dict[str, bool] = {}

        # Worklist of node IDs to process (replaces recursive calls)
        worklist: list[str] = [node_id]

        while worklist:
            nid = worklist.pop()

            if nid in visited:
                continue
            visited.add(nid)

            node = self.nodes.get(nid)
            if not node:
                continue

            # Invalidate current node (non-PREMISE only) and record flip.
            old_valid = node.is_valid
            if node.node_type != ATMSNodeType.PREMISE:
                node.is_valid = False
                # Explicitly record False in memo so subsequent is_node_provably_valid
                # calls for nodes depending on this one can short-circuit immediately
                # via the monotone-False memo optimisation in _leaf.
                memo[nid] = False

            if old_valid:
                cascade_set.add(nid)

            # Find all directly dependent DERIVED nodes that are still valid but are
            # no longer provable.  Using the reverse dependency index (_dependents) limits
            # the scan to O(direct dependents of nid) instead of O(all nodes), preserving
            # the "O(dependents) child scan" requirement from the batch spec.
            for child_id in self._dependents.get(nid, set()):
                child_node = self.nodes.get(child_id)
                if (
                    child_node
                    and child_node.node_type == ATMSNodeType.DERIVED
                    and child_node.is_valid
                ):
                    if not self.is_node_provably_valid(child_id, set(), memo):
                        worklist.append(child_id)

        return cascade_set

    def evaluate_belief_states(self) -> None:
        """Evaluates belief states for all nodes using iterative proof search."""
        memo: dict[str, bool] = {}
        for node_id, node in self.nodes.items():
            if node.node_type == ATMSNodeType.DERIVED:
                node.is_valid = self.is_node_provably_valid(node_id, set(), memo)
            elif node.node_type == ATMSNodeType.PREMISE:
                node.is_valid = True


def build_atms_from_causal_graph(graph: CausalGraph) -> ATMSEngine:
    """Translates a CausalGraph into an ATMSEngine structure.

    Maps topology failure propagation directions directly to logical dependencies:
    - FORWARD propagation: target depends on source.
    - REVERSE propagation: source depends on target.
    """
    ns_id = None
    if graph._nodes:
        first_node = next(iter(graph._nodes.values()))
        ns_id = first_node.namespace_id

    atms = ATMSEngine(namespace_id=ns_id)

    # 1. Gather all incoming justifications
    dependencies: dict[str, set[str]] = {nid: set() for nid in graph.node_ids}

    for src_id, edges in graph._outgoing.items():
        for edge in edges:
            if edge.edge_type in _FORWARD_FAILURE_TYPES:
                # FORWARD: target depends on source
                dependencies[edge.target_node_id].add(edge.source_node_id)
            elif edge.edge_type in _REVERSE_FAILURE_TYPES:
                # REVERSE: source depends on target
                dependencies[edge.source_node_id].add(edge.target_node_id)

    # 2. Register nodes (ASSUMPTION for roots, DERIVED for dependent nodes)
    for nid in graph.node_ids:
        if dependencies[nid]:
            atms.register_node(nid, ATMSNodeType.DERIVED)
            atms.add_justification(nid, dependencies[nid], "Causal dependency")
        else:
            atms.register_node(nid, ATMSNodeType.ASSUMPTION)

    return atms


# ---------------------------------------------------------------------------
# Database-driven state updates & wiring
# ---------------------------------------------------------------------------


def is_valid_uuid(val: str) -> bool:
    """Returns True if val is a valid UUID string."""
    try:
        uuid.UUID(val)
        return True
    except ValueError:
        return False


async def evaluate_atms_intervention(
    conn: Any,  # asyncpg.Connection
    namespace_id: uuid.UUID,
    invalidated_node_id: str,
) -> set[str]:
    """Loads the causal graph for a namespace, builds an ATMS, and cascades invalidation."""
    graph = await CausalGraph.load_from_db(conn, namespace_id)
    atms = build_atms_from_causal_graph(graph)

    # Invalidate the target node and get all affected downstream nodes
    cascade = atms.invalidate_assumption(invalidated_node_id)
    if invalidated_node_id in atms.nodes:
        # Force invalidation if registered as DERIVED
        cascade.update(atms.propagate_deprecation(invalidated_node_id))

    return cascade


async def persist_atms_invalidation(
    conn: Any,  # asyncpg.Connection
    namespace_id: uuid.UUID,
    invalidated_node_ids: set[str],
) -> int:
    """Soft-deletes (valid_to = now()) invalidated memories and topology edges in DB."""
    if not invalidated_node_ids:
        return 0

    # 1. Update memories
    uuid_candidates = [uuid.UUID(nid) for nid in invalidated_node_ids if is_valid_uuid(nid)]
    count_mem = 0
    if uuid_candidates:
        res_mem = await conn.execute(
            """
            UPDATE memories
            SET valid_to = now()
            WHERE namespace_id = $1::uuid
              AND id = ANY($2::uuid[])
              AND valid_to IS NULL
            """,
            namespace_id,
            uuid_candidates,
        )
        count_mem = int(res_mem.split()[-1]) if res_mem else 0

    # 2. Update topology edges
    res_topo = await conn.execute(
        """
        UPDATE topology_graph
        SET valid_to = now()
        WHERE namespace_id = $1::uuid
          AND (source_node_id = ANY($2::text[]) OR target_node_id = ANY($2::text[]))
          AND valid_to IS NULL
        """,
        namespace_id,
        list(invalidated_node_ids),
    )
    count_topo = int(res_topo.split()[-1]) if res_topo else 0

    log.info(
        "Persisted ATMS invalidation cascade for namespace=%s: "
        "soft-deleted %d memories, %d topology edges",
        namespace_id,
        count_mem,
        count_topo,
    )
    return count_mem + count_topo


_EDGE_CONFIDENCE_FLOOR: float = 0.1


async def floor_retracted_kg_edges(
    conn: Any,  # asyncpg.Connection
    namespace_id: uuid.UUID,
    retracted_memory_ids: set[str],
    contradiction_id: str,
    agent_id: str,
) -> int:
    """Floor confidence of kg_edges traced to retracted memories via origin_event_id.

    Finds kg_edges whose ``origin_event_id`` is recorded in ``event_log`` rows whose
    ``params->>'memory_id'`` is in *retracted_memory_ids*, then sets
    ``confidence = LEAST(confidence, 0.1)`` — auditable decay, NOT deletion.

    An ``edge_confidence_floored`` event is appended inside the caller's transaction
    so the floor is part of the same nested SAVEPOINT as the ATMS cascade.

    Returns the number of edges floored (0 when none match).
    """
    if not retracted_memory_ids:
        return 0

    uuid_candidates = [uuid.UUID(mid) for mid in retracted_memory_ids if is_valid_uuid(mid)]
    if not uuid_candidates:
        return 0

    # Collect origin_event_ids whose store_memory event logged one of the retracted memory_ids.
    origin_event_rows = await conn.fetch(
        """
        SELECT id
        FROM event_log
        WHERE namespace_id = $1::uuid
          AND event_type = 'store_memory'
          AND (params->>'memory_id')::uuid = ANY($2::uuid[])
        """,
        namespace_id,
        uuid_candidates,
    )
    if not origin_event_rows:
        return 0

    origin_event_ids = [row["id"] for row in origin_event_rows]

    res = await conn.execute(
        """
        UPDATE kg_edges
        SET confidence = LEAST(confidence, $1)
        WHERE namespace_id = $2::uuid
          AND origin_event_id = ANY($3::uuid[])
          AND confidence > $1
        """,
        _EDGE_CONFIDENCE_FLOOR,
        namespace_id,
        origin_event_ids,
    )
    floored_count = int(res.split()[-1]) if res else 0

    if floored_count > 0:
        from nce.event_log import append_event

        await append_event(
            conn=conn,
            namespace_id=namespace_id,
            agent_id=agent_id,
            event_type="edge_confidence_floored",
            params={
                "contradiction_id": contradiction_id,
                "retracted_memory_ids": sorted(retracted_memory_ids),
                "floored_edge_count": floored_count,
            },
            result_summary={"status": "success", "floored_count": floored_count},
        )

    log.info(
        "floor_retracted_kg_edges: namespace=%s floored %d kg_edges for %d retracted memories",
        namespace_id,
        floored_count,
        len(retracted_memory_ids),
    )
    return floored_count
