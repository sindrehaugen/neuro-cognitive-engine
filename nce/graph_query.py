"""
Phase 2 — Graphify Layer: Deterministic GraphRAG Traverser
Algorithm:
  1. Embed the query → cosine search ``kg_nodes`` for an anchor node.
  2. BFS outward over ``kg_edges`` up to ``max_depth`` hops using a single
     PostgreSQL **recursive CTE** (one round-trip instead of N).
  3. Fetch all discovered edges in one batch query (``WHERE subject_label = ANY``
     or ``object_label = ANY``).
  4. Hydrate source documents from MongoDB using two batch ``$in`` queries
     (``episodes`` + ``code_files``) — always exactly 2 round-trips.
  5. Return a structured subgraph: nodes, edges, and source excerpts (optional
     ``edge_limit`` / ``edge_offset`` on deduplicated edges).

Security model for ``kg_nodes`` / ``kg_edges``:
  Both tables carry ``namespace_id`` and PostgreSQL row-level security (RLS),
  enforced via ``set_namespace_context()`` / ``SET LOCAL nce.namespace_id``
  on tenant-scoped connections (same pattern as ``memories``).  Application
  queries still include explicit ``WHERE namespace_id = $N`` filters for clarity
  and planner hints.
  The ``_allow_global_sweep`` flag preserves the admin/diagnostic global-sweep
  path by passing ``NULL`` as the namespace parameter, which the ``IS NULL``
  guard in the query passes through without filtering.
  Only admin / internal operations should pass ``_allow_global_sweep=True``.
  Tenant-facing code paths must always supply a ``namespace_id``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

import asyncpg
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

from nce.config import cfg
from nce.models import MAX_GRAPH_DEPTH, MAX_GRAPH_EDGE_PAGE
from nce.providers import CircuitBreaker, LLMCircuitOpenError

log = logging.getLogger("nce-graphrag")

MAX_NODES = 50  # hard cap — prevents runaway BFS on dense graphs
# Cap incident edges loaded per BFS expansion (hub nodes can have huge degree).
# Frontend / MCP: document that each hop samples at most this many edges (by confidence).
MAX_EDGES_PER_NODE = 512


@dataclass
class GraphNode:
    label: str
    entity_type: str
    payload_ref: str | None
    distance: float = 0.0  # cosine distance from query anchor


@dataclass
class GraphEdge:
    subject: str
    predicate: str
    obj: str
    confidence: float
    payload_ref: str | None


@dataclass
class Subgraph:
    anchor: str
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)  # hydrated Mongo excerpts
    # Pagination / limits (optional metadata for API consumers)
    edge_total: int | None = None
    edge_offset: int | None = None
    edge_limit: int | None = None
    has_more_edges: bool | None = None
    max_edges_per_node: int | None = None

    def to_dict(self) -> dict:
        import math

        out: dict = {
            "anchor": self.anchor,
            "nodes": [
                {
                    "label": n.label,
                    "type": n.entity_type,
                    "distance": (
                        round(n.distance, 4)
                        if n.distance is not None and math.isfinite(n.distance)
                        else None
                    ),
                }
                for n in self.nodes
            ],
            "edges": [
                {
                    "subject": e.subject,
                    "predicate": e.predicate,
                    "object": e.obj,
                    "confidence": (
                        round(e.confidence, 4)
                        if e.confidence is not None and math.isfinite(e.confidence)
                        else None
                    ),
                }
                for e in self.edges
            ],
            "sources": self.sources,
        }
        if self.max_edges_per_node is not None:
            out["max_edges_per_node"] = self.max_edges_per_node
        if self.edge_total is not None:
            out["edge_total"] = self.edge_total
        if self.edge_offset is not None:
            out["edge_offset"] = self.edge_offset
        if self.edge_limit is not None:
            out["edge_limit"] = self.edge_limit
        if self.has_more_edges is not None:
            out["has_more_edges"] = self.has_more_edges
        return out


class SpikingActivationEngine:
    """
    Neuromorphic spreading activation engine modeling membrane potential,
    decay, and threshold-based firing.
    """

    def __init__(
        self,
        theta: float = 0.5,
        decay: float = 0.85,
        alpha: float = 1.0,
        max_charge: float | None = 10.0,
    ) -> None:
        self.theta = theta
        self.decay = decay
        self.alpha = alpha
        self.max_charge = max_charge
        self.potentials: dict[str, float] = {}
        self.max_potentials: dict[str, float] = {}  # Track maximum potential reached by any node
        self.fired_nodes: set[str] = set()

    def set_potentials(self, initial_potentials: dict[str, float]) -> None:
        """Set the initial membrane potentials for nodes."""
        self.potentials = {k: float(v) for k, v in initial_potentials.items()}
        self.max_potentials = {k: float(v) for k, v in initial_potentials.items()}

    def step(self, graph_edges: dict[str, list[tuple[str, float]]]) -> set[str]:
        """
        Executes one tick/step of spiking activation propagation.

        graph_edges: adjacency list mapping source_label -> list of (target_label, edge_weight)
        Returns the set of labels that fired in this step.
        """
        fired = set()
        # Find firing nodes in this step
        for node, v in list(self.potentials.items()):
            if v >= self.theta:
                fired.add(node)
                self.fired_nodes.add(node)

        # Calculate charge transfers
        transfers: dict[str, float] = {}
        for u in fired:
            V_u = self.potentials[u]
            # Reset potential of firing node to 0
            self.potentials[u] = 0.0

            # Distribute potential to neighbors
            neighbors = graph_edges.get(u, [])
            if not neighbors:
                continue

            for neighbor, weight in neighbors:
                delta = self.alpha * V_u * weight
                transfers[neighbor] = transfers.get(neighbor, 0.0) + delta

        # Decay potentials of all existing nodes
        for node in list(self.potentials.keys()):
            self.potentials[node] = self.potentials[node] * self.decay

        # Apply transfers to potentials with optional clamping
        for node, delta in transfers.items():
            val = self.potentials.get(node, 0.0) + delta
            if self.max_charge is not None:
                val = min(self.max_charge, val)
            self.potentials[node] = val

        # Update historical maximum potentials
        for node, pot in self.potentials.items():
            self.max_potentials[node] = max(self.max_potentials.get(node, 0.0), pot)

        return fired


async def adapt_synaptic_weights(
    conn: asyncpg.Connection,
    namespace_id: UUID | str,
    decision_outcome: str,
    reinforced_edges: list[tuple[str, str] | tuple[str, str, str]],
    learning_rate: float = 0.1,
) -> int:
    """
    Dynamically adapt synaptic weights (LTP/LTD) for the given reinforced edges
    in both `kg_edges` and `topology_graph` under row-level locking parameters.

    On 'success': w = w + learning_rate * (1.0 - w)
    On 'failure': w = w - learning_rate * w

    All confidence values are clipped between 0.0 and 1.0.
    """
    if not namespace_id:
        raise ValueError("namespace_id is required for tenant-scoped synaptic adaptation.")
    ns_uuid = UUID(str(namespace_id))
    from nce.auth import set_namespace_context

    await set_namespace_context(conn, ns_uuid)

    updated_count = 0

    for edge in reinforced_edges:
        if len(edge) == 3:
            src, pred_or_type, tgt = edge
            has_pred = True
        elif len(edge) == 2:
            src, tgt = edge
            pred_or_type = None
            has_pred = False
        else:
            log.warning("Invalid edge format in adapt_synaptic_weights: %s", edge)
            continue

        try:
            async with conn.transaction():
                # 1. Update kg_edges
                if has_pred:
                    # Find matching rows in either direction (bidirectional/symmetrical support)
                    rows = await conn.fetch(
                        """
                        SELECT id, confidence 
                        FROM kg_edges 
                        WHERE namespace_id = $1::uuid 
                          AND predicate = $2
                          AND (
                              (subject_label = $3 AND object_label = $4)
                              OR (subject_label = $4 AND object_label = $3)
                          )
                        FOR UPDATE NOWAIT
                        """,
                        ns_uuid,
                        pred_or_type,
                        src,
                        tgt,
                    )
                else:
                    # Find matching rows without predicate
                    rows = await conn.fetch(
                        """
                        SELECT id, confidence 
                        FROM kg_edges 
                        WHERE namespace_id = $1::uuid 
                          AND (
                              (subject_label = $2 AND object_label = $3)
                              OR (subject_label = $3 AND object_label = $2)
                          )
                        FOR UPDATE NOWAIT
                        """,
                        ns_uuid,
                        src,
                        tgt,
                    )

                for r in rows:
                    w = r["confidence"]
                    if decision_outcome == "success":
                        w_new = w + learning_rate * (1.0 - w)
                    else:
                        w_new = w - learning_rate * w
                    w_new = max(0.0, min(1.0, w_new))

                    await conn.execute(
                        """
                        UPDATE kg_edges
                        SET confidence = $1, updated_at = NOW()
                        WHERE id = $2 AND namespace_id = $3::uuid
                        """,
                        w_new,
                        r["id"],
                        ns_uuid,
                    )
                    updated_count += 1

                # 2. Update topology_graph
                if has_pred:
                    rows_topo = await conn.fetch(
                        """
                        SELECT id, confidence_score 
                        FROM topology_graph 
                        WHERE namespace_id = $1::uuid 
                          AND edge_type = $2
                          AND (
                              (source_node_id = $3 AND target_node_id = $4)
                              OR (source_node_id = $4 AND target_node_id = $3)
                          )
                          AND valid_to IS NULL
                        FOR UPDATE NOWAIT
                        """,
                        ns_uuid,
                        pred_or_type,
                        src,
                        tgt,
                    )
                else:
                    rows_topo = await conn.fetch(
                        """
                        SELECT id, confidence_score 
                        FROM topology_graph 
                        WHERE namespace_id = $1::uuid 
                          AND (
                              (source_node_id = $2 AND target_node_id = $3)
                              OR (source_node_id = $3 AND target_node_id = $2)
                          )
                          AND valid_to IS NULL
                        FOR UPDATE NOWAIT
                        """,
                        ns_uuid,
                        src,
                        tgt,
                    )

                for r in rows_topo:
                    w = r["confidence_score"]
                    if decision_outcome == "success":
                        w_new = w + learning_rate * (1.0 - w)
                    else:
                        w_new = w - learning_rate * w
                    w_new = max(0.0, min(1.0, w_new))

                    await conn.execute(
                        """
                        UPDATE topology_graph
                        SET confidence_score = $1, updated_at = NOW()
                        WHERE id = $2 AND namespace_id = $3::uuid
                        """,
                        w_new,
                        r["id"],
                        ns_uuid,
                    )
                    updated_count += 1
        except asyncpg.LockNotAvailableError:
            log.warning("Lock not available for transaction updates on edge %s", edge)

    return updated_count


class GraphRAGTraverser:
    def __init__(
        self,
        pg_pool: asyncpg.Pool,
        mongo_client: AsyncIOMotorClient,
        embedding_fn,  # async callable: (str) -> list[float]
        max_concurrent_searches: int = 10,
    ):
        self.pg_pool = pg_pool
        self.mongo_client = mongo_client
        self._embed = embedding_fn
        self._search_semaphore = asyncio.Semaphore(max_concurrent_searches)
        self.circuit_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)

    # --- Time-travel signature verification ---

    async def _verify_time_travel_event_signatures(
        self,
        conn: asyncpg.Connection,
        event_ids: list[str],
    ) -> None:
        """
        Verify HMAC signatures on event_log rows that contributed to a time-travel result.

        Time-travel CTE queries reconstruct historical KG state from ``event_log``
        rows entirely inside Postgres, bypassing Python-level ``verify_event_signature()``.
        This method closes that gap by fetching the winning event rows and validating
        their signatures before the subgraph is returned to the caller.

        Raises ``DataIntegrityError`` if any event has an invalid or missing signature.
        """
        if not event_ids:
            return
        from nce.event_log import DataIntegrityError, verify_event_signature

        # Deduplicate — the same event may appear from multiple CTE branches.
        unique_ids = list(set(event_ids))

        # Fetch full event_log rows.  event_log is RANGE-partitioned so this
        # scans all partitions, but the subgraph size is bounded by MAX_NODES.
        rows = await conn.fetch(
            "SELECT * FROM event_log WHERE id = ANY($1::uuid[])",
            unique_ids,
        )
        found_ids = {str(r["id"]) for r in rows}
        missing = set(unique_ids) - found_ids
        if missing:
            raise DataIntegrityError(
                f"Missing event_log rows for signature verification: {missing}"
            )
        for row in rows:
            try:
                await verify_event_signature(conn, row)
            except DataIntegrityError:
                raise  # re-raise — tampering is a critical security event
            except Exception as exc:
                log.error(
                    "Signature verification failed for event_id=%s: %s",
                    row.get("id"),
                    exc,
                )
                raise DataIntegrityError(
                    f"Event signature verification error for event_id={row.get('id')}: {exc}"
                ) from exc

    # --- Step 1: Vector anchor search ---

    async def _find_anchor(
        self,
        query: str,
        namespace_id: str | None = None,
        top_k: int = 3,
        as_of: datetime | None = None,
        *,
        _allow_global_sweep: bool = False,
        conn: asyncpg.Connection | None = None,
    ) -> list[GraphNode]:
        """
        Vector anchor search against kg_nodes.

        Security contract:
            ``namespace_id=None`` performs a **global** sweep across ALL tenants.
            This is ONLY safe for admin/diagnostic operations.  Tenant-facing
            callers MUST pass a namespace_id OR explicitly opt in with
            ``_allow_global_sweep=True``.

        Raises:
            ValueError: if ``namespace_id`` is None and ``_allow_global_sweep``
                        is not True (accidental global-sweep guard).
        """
        if namespace_id is None and not _allow_global_sweep:
            raise ValueError(
                "_find_anchor: namespace_id is required for tenant-scoped searches. "
                "Pass _allow_global_sweep=True only for admin/diagnostic cross-tenant operations."
            )
        if as_of is not None and as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware (UTC). Use datetime.now(timezone.utc).")
        vector = await self._embed(query)

        async def _run_find_anchor(c):
            if namespace_id:
                from nce.auth import set_namespace_context

                await set_namespace_context(c, UUID(str(namespace_id)))

            if as_of and namespace_id:
                # Phase 2.2: Time Travel Anchor Search
                rows = await c.fetch(
                    """
                    WITH ns AS (
                        SELECT id, parent_id, (metadata->'fork_config'->>'forked_from_as_of')::timestamptz AS forked_as_of
                        FROM namespaces WHERE id = $3::uuid
                    ),
                    memory_events AS (
                        SELECT DISTINCT ON ((params->>'memory_id')::uuid)
                            (params->>'memory_id')::uuid AS memory_id,
                            event_type,
                            params->'entities' AS entities,
                            id AS event_id
                        FROM event_log
                        CROSS JOIN ns
                        WHERE (
                            (namespace_id = ns.id AND occurred_at <= $4)
                            OR
                            (namespace_id = ns.parent_id AND occurred_at <= LEAST($4, ns.forked_as_of))
                        )
                          AND event_type IN ('store_memory', 'forget_memory')
                        ORDER BY (params->>'memory_id')::uuid, occurred_at DESC, event_seq DESC
                    ),
                    active_memories AS (
                        SELECT memory_id, entities, event_id
                        FROM memory_events
                        WHERE event_type = 'store_memory'
                    ),
                    historical_nodes AS (
                        SELECT DISTINCT ON (label)
                            jsonb_array_elements(entities)->>'label' AS label,
                            jsonb_array_elements(entities)->>'entity_type' AS entity_type,
                            memory_id,
                            event_id
                        FROM active_memories
                    )
                    SELECT n.label, n.entity_type, m.payload_ref,
                           m.embedding <=> $1::vector AS distance,
                           n.event_id
                    FROM historical_nodes n
                    JOIN memories m ON n.memory_id = m.id
                    ORDER BY distance ASC
                    LIMIT $2
                    """,
                    json.dumps(vector),
                    top_k,
                    namespace_id,
                    as_of,
                )
                # Verify signatures on event_log rows that contributed to this result.
                event_ids = [str(r["event_id"]) for r in rows if r.get("event_id")]
                await self._verify_time_travel_event_signatures(c, event_ids)
            else:
                rows = await c.fetch(
                    """
                    SELECT label, entity_type, payload_ref,
                           embedding <=> $1::vector AS distance
                    FROM kg_nodes
                    WHERE ($3::uuid IS NULL OR namespace_id = $3::uuid)
                    ORDER BY distance ASC
                    LIMIT $2
                    """,
                    json.dumps(vector),
                    top_k,
                    namespace_id,
                )
            return [
                GraphNode(
                    label=r["label"],
                    entity_type=r["entity_type"],
                    payload_ref=r["payload_ref"],
                    distance=r["distance"],
                )
                for r in rows
            ]

        if conn is not None:
            return await _run_find_anchor(conn)
        async with self.pg_pool.acquire(timeout=10.0) as c:
            async with c.transaction():
                return await _run_find_anchor(c)

    # --- Step 2: BFS edge traversal (single PostgreSQL recursive CTE) ---

    async def _bfs(
        self,
        start_label: str,
        max_depth: int,
        namespace_id: str | None = None,
        as_of: datetime | None = None,
        *,
        _allow_global_sweep: bool = False,
        max_edges_per_node: int = MAX_EDGES_PER_NODE,
        conn=None,
    ) -> tuple[set[str], list[GraphEdge]]:
        """
        BFS edge traversal over kg_edges — single PostgreSQL recursive CTE.

        Replaces the previous Python ``while queue:`` loop that issued N
        sequential SQL queries (one per BFS hop + one per visited label).
        The recursive CTE traverses the entire subgraph in one round-trip,
        then a follow-up ``SELECT`` fetches all matching edges at once.

        Security contract:
            ``namespace_id=None`` performs a **global** sweep across ALL tenants
            (queries ``kg_edges`` without namespace isolation).  This is ONLY
            safe for admin/diagnostic operations.  Tenant-facing callers MUST
            pass a namespace_id OR explicitly opt in with
            ``_allow_global_sweep=True``.

        Raises:
            ValueError: if ``namespace_id`` is None and ``_allow_global_sweep``
                        is not True (accidental global-sweep guard).
        """
        if namespace_id is None and not _allow_global_sweep:
            raise ValueError(
                "_bfs: namespace_id is required for tenant-scoped traversals. "
                "Pass _allow_global_sweep=True only for admin/diagnostic cross-tenant operations."
            )

        async def _run_bfs(c):
            if namespace_id:
                from nce.auth import set_namespace_context

                await set_namespace_context(c, UUID(str(namespace_id)))

            # ---- Single recursive CTE: find all reachable labels ----
            # The recursive branch discovers neighbors via ``kg_edges``
            # (or time-travel ``event_log``) up to ``max_depth``.  The
            # anchor is the caller-supplied ``start_label``.
            visited: set[str] = set()
            all_edges: list[GraphEdge] = []
            bfs_event_ids: list[str] = []

            if as_of and namespace_id:
                # Time-travel BFS: reconstruct edges from event_log
                labels = await c.fetch(
                    """
                    WITH RECURSIVE traversal AS (
                        SELECT $1::text AS label, 0 AS depth, ARRAY[$1::text] AS path
                        UNION
                        SELECT DISTINCT
                            h.neighbor,
                            t.depth + 1,
                            t.path || h.neighbor
                        FROM traversal t
                        JOIN LATERAL (
                            WITH ns AS (
                                SELECT id, parent_id,
                                       (metadata->'fork_config'->>'forked_from_as_of')::timestamptz AS forked_as_of
                                FROM namespaces WHERE id = $3::uuid
                            ),
                            memory_events AS (
                                SELECT DISTINCT ON ((params->>'memory_id')::uuid)
                                    (params->>'memory_id')::uuid AS memory_id,
                                    event_type, params->'triplets' AS triplets, id AS event_id
                                FROM event_log CROSS JOIN ns
                                WHERE (
                                    (namespace_id = ns.id AND occurred_at <= $4)
                                    OR (namespace_id = ns.parent_id AND occurred_at <= LEAST($4, ns.forked_as_of))
                                )
                                  AND event_type IN ('store_memory', 'forget_memory')
                                ORDER BY (params->>'memory_id')::uuid, occurred_at DESC, event_seq DESC
                            ),
                            active_memories AS (
                                SELECT memory_id, triplets, event_id
                                FROM memory_events WHERE event_type = 'store_memory'
                            ),
                            historical_edges AS (
                                SELECT
                                    jsonb_array_elements(triplets)->>'subject_label' AS subject_label,
                                    jsonb_array_elements(triplets)->>'predicate' AS predicate,
                                    jsonb_array_elements(triplets)->>'object_label' AS object_label,
                                    (jsonb_array_elements(triplets)->>'confidence')::float AS confidence,
                                    memory_id, event_id
                                FROM active_memories
                            )
                            SELECT 
                                CASE WHEN e.subject_label = t.label THEN e.object_label ELSE e.subject_label END AS neighbor
                            FROM historical_edges e
                            JOIN memories m ON e.memory_id = m.id
                            WHERE e.subject_label = t.label OR e.object_label = t.label
                            LIMIT $5
                        ) h ON true
                        WHERE t.depth < $2
                          AND h.neighbor <> ALL(t.path)
                    )
                    SELECT label FROM (
                        SELECT label, MIN(depth) as min_depth FROM traversal GROUP BY label
                    ) AS distinct_labels ORDER BY min_depth ASC LIMIT $6
                    """,
                    start_label,
                    max_depth,
                    namespace_id,
                    as_of,
                    max_edges_per_node,
                    MAX_NODES,
                )
                visited = {r["label"] for r in labels}

                # Batch-fetch all time-travel edges between visited labels
                if visited:
                    edge_rows = await c.fetch(
                        """
                        WITH ns AS (
                            SELECT id, parent_id,
                                   (metadata->'fork_config'->>'forked_from_as_of')::timestamptz AS forked_as_of
                            FROM namespaces WHERE id = $2::uuid
                        ),
                        memory_events AS (
                            SELECT DISTINCT ON ((params->>'memory_id')::uuid)
                                (params->>'memory_id')::uuid AS memory_id,
                                event_type, params->'triplets' AS triplets, id AS event_id
                            FROM event_log CROSS JOIN ns
                            WHERE (
                                (namespace_id = ns.id AND occurred_at <= $3)
                                OR (namespace_id = ns.parent_id AND occurred_at <= LEAST($3, ns.forked_as_of))
                            )
                              AND event_type IN ('store_memory', 'forget_memory')
                            ORDER BY (params->>'memory_id')::uuid, occurred_at DESC, event_seq DESC
                        ),
                        active_memories AS (
                            SELECT memory_id, triplets, event_id
                            FROM memory_events WHERE event_type = 'store_memory'
                        ),
                        historical_edges AS (
                            SELECT
                                jsonb_array_elements(triplets)->>'subject_label' AS subject_label,
                                jsonb_array_elements(triplets)->>'predicate' AS predicate,
                                jsonb_array_elements(triplets)->>'object_label' AS object_label,
                                (jsonb_array_elements(triplets)->>'confidence')::float AS confidence,
                                memory_id, event_id
                            FROM active_memories
                        )
                        SELECT DISTINCT ON (e.subject_label, e.predicate, e.object_label)
                            e.subject_label, e.predicate, e.object_label, m.payload_ref,
                            e.confidence AS decayed_confidence, e.event_id
                        FROM historical_edges e
                        JOIN memories m ON e.memory_id = m.id
                        WHERE (e.subject_label = ANY($1::text[]) OR e.object_label = ANY($1::text[]))
                        ORDER BY e.subject_label, e.predicate, e.object_label, decayed_confidence DESC
                        """,
                        list(visited),
                        namespace_id,
                        as_of,
                    )
                    bfs_event_ids = [str(r["event_id"]) for r in edge_rows if r.get("event_id")]
                    all_edges = [
                        GraphEdge(
                            subject=r["subject_label"],
                            predicate=r["predicate"],
                            obj=r["object_label"],
                            confidence=r["decayed_confidence"],
                            payload_ref=r["payload_ref"],
                        )
                        for r in edge_rows
                    ]
            else:
                # Current-state BFS: single recursive CTE + batch edge fetch
                labels = await c.fetch(
                    """
                    WITH RECURSIVE traversal AS (
                        SELECT $1::text AS label, 0 AS depth, ARRAY[$1::text] AS path
                        UNION
                        SELECT DISTINCT
                            e.neighbor,
                            t.depth + 1,
                            t.path || e.neighbor
                        FROM traversal t
                        JOIN LATERAL (
                            SELECT 
                                CASE WHEN e2.subject_label = t.label THEN e2.object_label ELSE e2.subject_label END AS neighbor
                            FROM kg_edges e2
                            WHERE (e2.subject_label = t.label OR e2.object_label = t.label)
                              AND ($4::uuid IS NULL OR e2.namespace_id = $4::uuid)
                            ORDER BY e2.confidence DESC
                            LIMIT $5
                        ) e ON true
                        WHERE t.depth < $2
                          -- Path-array cycle guard: new label must not already be in this path
                          AND e.neighbor <> ALL(t.path)
                    )
                    SELECT label FROM (
                        SELECT label, MIN(depth) as min_depth FROM traversal GROUP BY label
                    ) AS distinct_labels ORDER BY min_depth ASC LIMIT $3
                    """,
                    start_label,
                    max_depth,
                    MAX_NODES,
                    namespace_id,
                    max_edges_per_node,
                )
                visited = {r["label"] for r in labels}

                # Batch-fetch all edges between visited labels in one query
                if visited:
                    edge_rows = await c.fetch(
                        """
                        SELECT DISTINCT ON (e.subject_label, e.predicate, e.object_label)
                            e.subject_label, e.predicate, e.object_label, e.payload_ref,
                            e.confidence * EXP(-0.01 * EXTRACT(EPOCH FROM (NOW() - e.updated_at)) / 86400)
                                AS decayed_confidence
                        FROM kg_edges e
                        WHERE (e.subject_label = ANY($1::text[]) OR e.object_label = ANY($1::text[]))
                          AND ($2::uuid IS NULL OR e.namespace_id = $2::uuid)
                        ORDER BY e.subject_label, e.predicate, e.object_label, decayed_confidence DESC
                        """,
                        list(visited),
                        namespace_id,
                    )
                    all_edges = [
                        GraphEdge(
                            subject=r["subject_label"],
                            predicate=r["predicate"],
                            obj=r["object_label"],
                            confidence=r["decayed_confidence"],
                            payload_ref=r["payload_ref"],
                        )
                        for r in edge_rows
                    ]

            # Verify signatures on event_log rows if time-travel mode.
            if bfs_event_ids:
                await self._verify_time_travel_event_signatures(c, bfs_event_ids)
            return visited, all_edges

        if conn is not None:
            return await _run_bfs(conn)
        async with self.pg_pool.acquire(timeout=10.0) as c:
            async with c.transaction():
                return await _run_bfs(c)

    # --- Step 3: Hydrate source documents from MongoDB (batch) ---

    async def _hydrate_sources(
        self,
        mongo_ref_ids: set[str],
        restrict_user_id: str | None = None,
        namespace_id: str | None = None,
    ) -> list[dict]:
        """
        Hydrate source documents from MongoDB using batch ``$in`` queries.

        Replaces the previous N+1 pattern that called ``find_one`` sequentially
        for each ``payload_ref`` (up to 100 round-trips per graph search).
        Now uses two batch ``$in`` queries (one per collection), reducing
        round-trips to exactly 2 regardless of result size.

        When restrict_user_id is set (private graph search), only include
        documents owned by that user.
        """
        valid_refs = [ref for ref in mongo_ref_ids if ref]
        if not valid_refs:
            return []

        # Build ObjectId list — skip invalid refs with a warning.
        oids: list[ObjectId] = []
        for ref in valid_refs:
            try:
                oids.append(ObjectId(ref))
            except Exception as e:
                log.warning("Invalid payload_ref=%s: %s", ref, e)

        if not oids:
            return []

        from nce.db_utils import scoped_mongo_session

        # Two batch queries — always exactly 2 round-trips, never N.
        ep_docs: dict[str, dict] = {}
        code_docs: dict[str, dict] = {}

        if namespace_id:
            try:
                async with scoped_mongo_session(self.mongo_client, namespace_id) as s_db:
                    cursor = s_db.episodes.find({"_id": {"$in": oids}})
                    async for doc in cursor:
                        ep_docs[str(doc["_id"])] = doc
            except Exception as e:
                log.warning("Batch episodes hydration failed: %s", e)

            try:
                async with scoped_mongo_session(self.mongo_client, namespace_id) as s_db:
                    cursor = s_db.code_files.find({"_id": {"$in": oids}})
                    async for doc in cursor:
                        code_docs[str(doc["_id"])] = doc
            except Exception as e:
                log.warning("Batch code_files hydration failed: %s", e)
        else:
            db = self.mongo_client.memory_archive
            try:
                cursor = db.episodes.find({"_id": {"$in": oids}})
                async for doc in cursor:
                    ep_docs[str(doc["_id"])] = doc
            except Exception as e:
                log.warning("Batch episodes hydration failed: %s", e)

            try:
                cursor = db.code_files.find({"_id": {"$in": oids}})
                async for doc in cursor:
                    code_docs[str(doc["_id"])] = doc
            except Exception as e:
                log.warning("Batch code_files hydration failed: %s", e)

        # Part II.4: fetch the wrapped DEK for each episode payload_ref so an
        # encrypted raw_data excerpt can be decrypted; legacy rows → NULL →
        # plaintext.  code_files.raw_code is not envelope-encrypted by this batch.
        wrapped_by_ref: dict[str, bytes | None] = {}
        if ep_docs:
            try:
                async with self.pg_pool.acquire(timeout=10.0) as c:
                    dek_rows = await c.fetch(
                        "SELECT payload_ref, wrapped_dek FROM memories WHERE payload_ref = ANY($1::text[])",
                        list(ep_docs.keys()),
                    )
                for dek_row in dek_rows:
                    wd = dek_row["wrapped_dek"]
                    wrapped_by_ref[str(dek_row["payload_ref"])] = (
                        bytes(wd) if wd is not None else None
                    )
            except Exception as e:
                log.warning("wrapped_dek lookup failed; treating raw_data as plaintext: %s", e)

        from nce.envelope import maybe_decrypt_raw_data

        sources: list[dict] = []
        for ref_id in valid_refs:
            doc = ep_docs.get(ref_id)
            if doc is not None:
                if restrict_user_id is not None and doc.get("user_id") != restrict_user_id:
                    continue
                raw = maybe_decrypt_raw_data(doc.get("raw_data", ""), wrapped_by_ref.get(ref_id))
                sources.append(
                    {
                        "payload_ref": ref_id,
                        "collection": "episodes",
                        "type": doc.get("type", "unknown"),
                        "excerpt": str(raw)[:600],
                    }
                )
                continue

            code_doc = code_docs.get(ref_id)
            if code_doc is not None:
                if restrict_user_id is not None and code_doc.get("user_id") != restrict_user_id:
                    continue
                raw = code_doc.get("raw_code", "")
                sources.append(
                    {
                        "payload_ref": ref_id,
                        "collection": "code_files",
                        "type": "code",
                        "filepath": code_doc.get("filepath"),
                        "language": code_doc.get("language"),
                        "excerpt": str(raw)[:600],
                    }
                )

        return sources

    # --- Shared Traversal Helpers ---

    def _validate_search_params(
        self,
        namespace_id: str | None,
        as_of: datetime | None,
        max_edges_per_node: int | None,
        edge_offset: int,
        edge_limit: int | None,
        method_name: str,
        _allow_global_sweep: bool,
        max_depth: int = 2,
    ) -> int:
        if namespace_id is None and not _allow_global_sweep:
            raise ValueError(
                f"{method_name}: namespace_id is required for tenant-scoped graph searches. "
                "Pass _allow_global_sweep=True only for admin/diagnostic cross-tenant operations."
            )
        if not (1 <= max_depth <= MAX_GRAPH_DEPTH):
            raise ValueError(f"max_depth must be between 1 and {MAX_GRAPH_DEPTH}, got {max_depth}")
        if as_of is not None:
            if not isinstance(as_of, datetime):
                raise ValueError("as_of must be a datetime object")
            if as_of.tzinfo is None:
                raise ValueError(
                    "as_of must be timezone-aware (UTC). Use datetime.now(timezone.utc)."
                )
        per_node = max_edges_per_node if max_edges_per_node is not None else MAX_EDGES_PER_NODE
        if per_node < 1:
            raise ValueError("max_edges_per_node must be >= 1")
        if edge_offset < 0:
            raise ValueError("edge_offset must be >= 0")
        if edge_limit is not None:
            if edge_limit < 1 or edge_limit > MAX_GRAPH_EDGE_PAGE:
                raise ValueError(
                    f"edge_limit must be between 1 and {MAX_GRAPH_EDGE_PAGE}, got {edge_limit}"
                )
        return per_node

    async def _execute_graph_traversal(
        self,
        conn: asyncpg.Connection,
        query: str,
        namespace_id: str | None,
        anchor_top_k: int,
        as_of: datetime | None,
        _allow_global_sweep: bool,
        max_depth: int,
        per_node: int,
        method_name: str,
    ) -> tuple[GraphNode, set[str], list[GraphEdge]] | None:
        """Finds the anchor node and executes BFS traversal in a single database context."""
        anchors = await self._find_anchor(
            query,
            namespace_id=namespace_id,
            top_k=anchor_top_k,
            as_of=as_of,
            _allow_global_sweep=_allow_global_sweep,
            conn=conn,
        )
        if not anchors:
            log.info("No anchor node found in knowledge graph.")
            return None

        anchor = anchors[0]
        log.info("[%s] Anchor: '%s' (distance=%.4f)", method_name, anchor.label, anchor.distance)

        visited_labels, edges = await self._bfs(
            anchor.label,
            max_depth=max_depth,
            namespace_id=namespace_id,
            as_of=as_of,
            _allow_global_sweep=_allow_global_sweep,
            max_edges_per_node=per_node,
            conn=conn,
        )
        return anchor, visited_labels, edges

    def _temporal_cte_prefix(self) -> str:
        """Returns the common recursive CTE query prefix for fetching historical node metadata."""
        return """
            WITH ns AS (
                SELECT id, parent_id, (metadata->'fork_config'->>'forked_from_as_of')::timestamptz AS forked_as_of
                FROM namespaces WHERE id = $2::uuid
            ),
            memory_events AS (
                SELECT DISTINCT ON ((params->>'memory_id')::uuid)
                    (params->>'memory_id')::uuid AS memory_id,
                    event_type,
                    params->'entities' AS entities,
                    id AS event_id
                FROM event_log
                CROSS JOIN ns
                WHERE (
                    (namespace_id = ns.id AND occurred_at <= $3)
                    OR 
                    (namespace_id = ns.parent_id AND occurred_at <= LEAST($3, ns.forked_as_of))
                )
                  AND event_type IN ('store_memory', 'forget_memory')
                ORDER BY (params->>'memory_id')::uuid, occurred_at DESC, event_seq DESC
            ),
            active_memories AS (
                SELECT memory_id, entities, event_id
                FROM memory_events 
                WHERE event_type = 'store_memory'
            ),
            historical_nodes AS (
                SELECT DISTINCT ON (label)
                    jsonb_array_elements(entities)->>'label' AS label,
                    jsonb_array_elements(entities)->>'entity_type' AS entity_type,
                    memory_id,
                    event_id
                FROM active_memories
            )
            SELECT n.label, n.entity_type, m.payload_ref, n.event_id
            FROM historical_nodes n
            JOIN memories m ON n.memory_id = m.id
            WHERE n.label = ANY($1::text[])
        """

    async def _fetch_nodes_metadata(
        self,
        conn: asyncpg.Connection,
        labels: list[str],
        namespace_id: str | None,
        as_of: datetime | None,
        anchor: GraphNode,
    ) -> list[GraphNode]:
        """Fetches full node metadata (historical time-travel or current state) from DB."""
        if as_of and namespace_id:
            # Case 1 (as_of + namespace_id): Maintain the existing tenant time-travel temporal CTE path.
            rows = await conn.fetch(
                self._temporal_cte_prefix(),
                labels,
                namespace_id,
                as_of,
            )
            # Verify signatures on event_log rows that contributed to node metadata.
            event_ids = [str(r["event_id"]) for r in rows if r.get("event_id")]
            await self._verify_time_travel_event_signatures(conn, event_ids)
        elif as_of and not namespace_id:
            # Case 2 (as_of + no namespace_id): Explicitly raise NotImplementedError
            raise NotImplementedError(
                "Global cross-tenant time-travel sweeps are not structurally supported."
            )
        elif not as_of and namespace_id:
            # Case 3 (no as_of + namespace_id): Perform an isolated tenant current-state query filtering directly via a parameterized query signature.
            rows = await conn.fetch(
                "SELECT label, entity_type, payload_ref FROM kg_nodes WHERE label = ANY($1::text[]) AND namespace_id = $2::uuid",
                labels,
                namespace_id,
            )
        else:
            # Case 4 (no as_of + no namespace_id): Execute a clean global sweep query without filtering or calling session parameters.
            # SECURITY TRACKING: This connection invariant requires a database role with active BYPASSRLS privileges
            # to successfully bypass row-level security on kg_nodes.
            rows = await conn.fetch(
                "SELECT label, entity_type, payload_ref FROM kg_nodes WHERE label = ANY($1::text[])",
                labels,
            )
        return [
            GraphNode(
                label=r["label"],
                entity_type=r["entity_type"],
                payload_ref=r["payload_ref"],
                distance=anchor.distance if r["label"] == anchor.label else 0.0,
            )
            for r in rows
        ]

    async def _build_subgraph(
        self,
        anchor: GraphNode,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
        edge_offset: int,
        edge_limit: int | None,
        per_node: int,
        private: bool,
        user_id: str | None,
        namespace_id: str | None = None,
    ) -> Subgraph:
        """Helper to deduplicate, page, and format BFS/neuromorphic traversal results into a Subgraph."""
        # Deduplicate edges (BFS can traverse same edge from both directions)
        seen_edges: set[tuple] = set()
        unique_edges = []
        for e in edges:
            key = (e.subject, e.predicate, e.obj)
            if key not in seen_edges:
                seen_edges.add(key)
                unique_edges.append(e)

        edge_total = len(unique_edges)
        off = edge_offset
        if edge_limit is None:
            page_edges = unique_edges[off:]
        else:
            page_edges = unique_edges[off : off + edge_limit]
        has_more = edge_total > off + len(page_edges)

        labels_for_page = {anchor.label}
        for e in page_edges:
            labels_for_page.add(e.subject)
            labels_for_page.add(e.obj)
        nodes_for_page = [n for n in nodes if n.label in labels_for_page]

        all_refs = {n.payload_ref for n in nodes_for_page if n.payload_ref}
        all_refs |= {e.payload_ref for e in page_edges if e.payload_ref}
        restrict = user_id if private else None
        sources = await self._hydrate_sources(
            all_refs, restrict_user_id=restrict, namespace_id=namespace_id
        )

        return Subgraph(
            anchor=anchor.label,
            nodes=nodes_for_page,
            edges=page_edges,
            sources=sources,
            edge_total=edge_total,
            edge_offset=off,
            edge_limit=edge_limit,
            has_more_edges=has_more,
            max_edges_per_node=per_node,
        )

    # --- Public API ---

    async def search(
        self,
        query: str,
        namespace_id: str | None = None,
        max_depth: int = 2,
        anchor_top_k: int = 1,
        *,
        private: bool = False,
        user_id: str | None = None,
        as_of=None,
        _allow_global_sweep: bool = False,
        max_edges_per_node: int | None = None,
        edge_limit: int | None = None,
        edge_offset: int = 0,
    ) -> Subgraph:
        """
        Full GraphRAG traversal pipeline.
        Returns a Subgraph with nodes, edges, and hydrated source excerpts.
        """
        per_node = self._validate_search_params(
            namespace_id=namespace_id,
            as_of=as_of,
            max_edges_per_node=max_edges_per_node,
            edge_offset=edge_offset,
            edge_limit=edge_limit,
            method_name="search",
            _allow_global_sweep=_allow_global_sweep,
            max_depth=max_depth,
        )
        # Check circuit breaker
        allowed = await self.circuit_breaker.check()
        if not allowed:
            raise LLMCircuitOpenError(
                f"GraphRAG search circuit breaker is OPEN (failures={self.circuit_breaker._failure_count}/{self.circuit_breaker.failure_threshold}).",
                provider="GraphRAG/search",
                status_code=503,
            )

        async with self._search_semaphore:
            try:
                async with self.pg_pool.acquire(timeout=10.0) as conn:
                    async with conn.transaction():
                        if namespace_id:
                            from nce.auth import set_namespace_context

                            await set_namespace_context(conn, UUID(str(namespace_id)))

                        traversal = await self._execute_graph_traversal(
                            conn=conn,
                            query=query,
                            namespace_id=namespace_id,
                            anchor_top_k=anchor_top_k,
                            as_of=as_of,
                            _allow_global_sweep=_allow_global_sweep,
                            max_depth=max_depth,
                            per_node=per_node,
                            method_name="search",
                        )
                        if traversal is None:
                            await self.circuit_breaker.record_success()
                            return Subgraph(anchor="<none>")

                        anchor, visited_labels, edges = traversal

                        nodes = await self._fetch_nodes_metadata(
                            conn=conn,
                            labels=list(visited_labels),
                            namespace_id=namespace_id,
                            as_of=as_of,
                            anchor=anchor,
                        )
                await self.circuit_breaker.record_success()
            except Exception as exc:
                if not isinstance(exc, (ValueError, NotImplementedError, LLMCircuitOpenError)):
                    await self.circuit_breaker.record_failure()
                raise

            return await self._build_subgraph(
                anchor=anchor,
                nodes=nodes,
                edges=edges,
                edge_offset=edge_offset,
                edge_limit=edge_limit,
                per_node=per_node,
                private=private,
                user_id=user_id,
                namespace_id=namespace_id,
            )

    async def neuromorphic_search(
        self,
        query: str,
        namespace_id: str | None = None,
        max_depth: int = 2,
        anchor_top_k: int = 1,
        *,
        private: bool = False,
        user_id: str | None = None,
        as_of=None,
        _allow_global_sweep: bool = False,
        max_edges_per_node: int | None = None,
        edge_limit: int | None = None,
        edge_offset: int = 0,
        telemetry_severity: float | None = None,
        theta: float = 0.5,
        decay: float = 0.85,
        alpha: float = 1.0,
        ticks: int | None = None,
    ) -> Subgraph:
        """
        Neuromorphic spreading activation traversal pipeline.

        Uses a spiking neural model to search and traverse the knowledge graph
        instead of legacy BFS.
        """
        per_node = self._validate_search_params(
            namespace_id=namespace_id,
            as_of=as_of,
            max_edges_per_node=max_edges_per_node,
            edge_offset=edge_offset,
            edge_limit=edge_limit,
            method_name="neuromorphic_search",
            _allow_global_sweep=_allow_global_sweep,
            max_depth=max_depth,
        )

        # Check circuit breaker
        allowed = await self.circuit_breaker.check()
        if not allowed:
            raise LLMCircuitOpenError(
                f"GraphRAG neuromorphic_search circuit breaker is OPEN (failures={self.circuit_breaker._failure_count}/{self.circuit_breaker.failure_threshold}).",
                provider="GraphRAG/neuromorphic_search",
                status_code=503,
            )

        async with self._search_semaphore:
            try:
                async with self.pg_pool.acquire(timeout=10.0) as conn:
                    async with conn.transaction():
                        if namespace_id:
                            from nce.auth import set_namespace_context

                            await set_namespace_context(conn, UUID(str(namespace_id)))

                        traversal = await self._execute_graph_traversal(
                            conn=conn,
                            query=query,
                            namespace_id=namespace_id,
                            anchor_top_k=anchor_top_k,
                            as_of=as_of,
                            _allow_global_sweep=_allow_global_sweep,
                            max_depth=max_depth,
                            per_node=per_node,
                            method_name="neuromorphic_search",
                        )
                        if traversal is None:
                            await self.circuit_breaker.record_success()
                            return Subgraph(anchor="<none>")

                        anchor, visited_candidate_labels, candidate_edges = traversal

                        # Build local adjacency list for spreading activation
                        # Max-weight unify parallel edges
                        edge_map: dict[tuple[str, str], float] = {}
                        for e in candidate_edges:
                            pair = (e.subject, e.obj)
                            edge_map[pair] = max(edge_map.get(pair, 0.0), e.confidence)

                        adj: dict[str, list[tuple[str, float]]] = {}
                        for (src, tgt), conf in edge_map.items():
                            adj.setdefault(src, []).append((tgt, conf))
                            adj.setdefault(tgt, []).append((src, conf))

                        # Scale threshold and initial potential if system telemetry severity exceeds threshold
                        actual_theta = theta
                        initial_charge = 1.0
                        spike_thresh = cfg.NCE_TELEMETRY_SPIKE_THRESHOLD
                        if telemetry_severity is not None and telemetry_severity > spike_thresh:
                            actual_theta = cfg.NCE_TELEMETRY_SPIKE_THETA
                            initial_charge = cfg.NCE_TELEMETRY_SPIKE_CHARGE
                            log.info(
                                "Telemetry severity spike detected (%.1f > %.1f). Lowering threshold to %.2f "
                                "and raising initial charge to %.2f for wider pre-fetching.",
                                telemetry_severity,
                                spike_thresh,
                                actual_theta,
                                initial_charge,
                            )

                        # Initialize Spiking Neural Engine
                        engine = SpikingActivationEngine(
                            theta=actual_theta,
                            decay=decay,
                            alpha=alpha,
                        )
                        engine.set_potentials({anchor.label: initial_charge})

                        # Run propagation simulation for specified ticks or max_depth
                        simulation_ticks = ticks if ticks is not None else max_depth
                        for _ in range(simulation_ticks):
                            engine.step(adj)

                        # Select active nodes: fired nodes, anchor node, and sub-threshold activated nodes
                        active_labels = set(engine.fired_nodes) | {anchor.label}
                        # Also include any nodes that reached at least 10% of firing threshold
                        sub_threshold = actual_theta * 0.1
                        for node_label, pot in engine.max_potentials.items():
                            if pot >= sub_threshold:
                                active_labels.add(node_label)

                        # Restrict candidate edges to active nodes
                        active_edges = [
                            e
                            for e in candidate_edges
                            if e.subject in active_labels and e.obj in active_labels
                        ]

                        nodes = await self._fetch_nodes_metadata(
                            conn=conn,
                            labels=list(active_labels),
                            namespace_id=namespace_id,
                            as_of=as_of,
                            anchor=anchor,
                        )
                await self.circuit_breaker.record_success()
            except Exception as exc:
                if not isinstance(exc, (ValueError, NotImplementedError, LLMCircuitOpenError)):
                    await self.circuit_breaker.record_failure()
                raise

            return await self._build_subgraph(
                anchor=anchor,
                nodes=nodes,
                edges=active_edges,
                edge_offset=edge_offset,
                edge_limit=edge_limit,
                per_node=per_node,
                private=private,
                user_id=user_id,
                namespace_id=namespace_id,
            )

    async def get_subgraph(self, *args, **kwargs) -> Subgraph:
        """Alias for :meth:`search` — subgraph retrieval with edge pagination."""
        return await self.search(*args, **kwargs)
