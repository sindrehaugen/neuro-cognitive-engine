"""
nce/causal/chrono.py
====================
BATCH-P3-002 — Counterfactual Chrono-Branching

Implements in-memory transient branching of the memory timeline using ContextVars
to evaluate do-calculus and causal failure propagation under hypothetical conditions.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Any

from nce.causal.correlation import CausalEdge, CausalGraph, CausalNode
from nce.temporal import parse_as_of

log = logging.getLogger("nce.causal.chrono")

chrono_branch_var: ContextVar[dict[str, Any] | None] = ContextVar("chrono_branch", default=None)


@contextmanager
def branch_timeline(target_time: datetime | str, hypothetical_states: dict[str, Any]):
    """Context manager to branch the memory timeline for alternative reality lookup.

    Operates in-memory by setting a ContextVar that overrides CausalGraph loads.

    Nested ``branch_timeline`` scopes are explicitly rejected.  A second
    ``with branch_timeline(...)`` opened inside an already-active branch would
    silently shadow the outer scope via ``ContextVar.set`` and then restore
    only the *inner* token on exit, corrupting the outer counterfactual.
    Sequential usage (enter → exit → enter) is fully supported.

    Raises:
        RuntimeError: If a ``branch_timeline`` scope is already active in this
            context, indicating an unsupported nested counterfactual branch.
    """
    if chrono_branch_var.get() is not None:
        raise RuntimeError(
            "branch_timeline: a counterfactual branch is already active in this context; "
            "nested branch_timeline scopes are not supported. "
            "Exit the outer branch before opening a new one."
        )

    dt = parse_as_of(target_time) if isinstance(target_time, str) else target_time

    token = chrono_branch_var.set(
        {
            "target_time": dt,
            "hypothetical_states": hypothetical_states,
        }
    )
    try:
        yield
    finally:
        chrono_branch_var.reset(token)


def get_active_branch() -> dict[str, Any] | None:
    """Returns the active chrono branch context, if any."""
    return chrono_branch_var.get()


def apply_hypothetical_states(
    graph: CausalGraph, hypothetical_states: dict[str, Any]
) -> CausalGraph:
    """Applies in-memory overrides, deletions, and injections to a CausalGraph, returning a new copy."""
    new_nodes = dict(graph._nodes)
    new_outgoing = {k: list(v) for k, v in graph._outgoing.items()}
    new_incoming = {k: list(v) for k, v in graph._incoming.items()}

    # Resolve fallback namespace from the existing causal graph nodes to maintain RLS consistency
    fallback_ns = None
    if new_nodes:
        fallback_ns = next(iter(new_nodes.values())).namespace_id
    else:
        fallback_ns = uuid.uuid4()

    # 1. Deletions
    deletions = hypothetical_states.get("deletions", {})
    deleted_nodes = set(deletions.get("nodes", []))
    deleted_edges = set(deletions.get("edges", []))  # (source, target) tuples

    for nid in deleted_nodes:
        if nid in new_nodes:
            del new_nodes[nid]
        if nid in new_outgoing:
            del new_outgoing[nid]
        if nid in new_incoming:
            del new_incoming[nid]

    # Clean edges containing deleted nodes
    for src in list(new_outgoing.keys()):
        new_outgoing[src] = [
            e
            for e in new_outgoing[src]
            if e.source_node_id not in deleted_nodes and e.target_node_id not in deleted_nodes
        ]
    for tgt in list(new_incoming.keys()):
        new_incoming[tgt] = [
            e
            for e in new_incoming[tgt]
            if e.source_node_id not in deleted_nodes and e.target_node_id not in deleted_nodes
        ]

    # Clean deleted edges
    for src, tgt in deleted_edges:
        if src in new_outgoing:
            new_outgoing[src] = [e for e in new_outgoing[src] if e.target_node_id != tgt]
        if tgt in new_incoming:
            new_incoming[tgt] = [e for e in new_incoming[tgt] if e.source_node_id != src]

    # 2. Node additions/overrides
    nodes_override = hypothetical_states.get("nodes", {})
    for nid, node_data in nodes_override.items():
        if nid in deleted_nodes:
            continue
        existing = new_nodes.get(nid)
        ntype = node_data.get("node_type", existing.node_type if existing else "device")
        ns_id = node_data.get("namespace_id", existing.namespace_id if existing else fallback_ns)

        if isinstance(ns_id, str):
            try:
                ns_id = uuid.UUID(ns_id)
            except ValueError as exc:
                log.warning("Invalid namespace_id UUID string %r in chrono branch: %s", ns_id, exc)
                ns_id = fallback_ns

        new_nodes[nid] = CausalNode(node_id=nid, node_type=ntype, namespace_id=ns_id)

    # 3. Edge additions/overrides
    edges_override = hypothetical_states.get("edges", [])
    for edge_data in edges_override:
        src = edge_data["source_node_id"]
        tgt = edge_data["target_node_id"]
        if src in deleted_nodes or tgt in deleted_nodes:
            continue
        if (src, tgt) in deleted_edges:
            continue

        edge_type = edge_data.get("edge_type", "connected_to")
        confidence = float(edge_data.get("confidence_score", 1.0))
        decay_coeff = float(edge_data.get("decay_coefficient", 0.001))
        last_verified = edge_data.get("last_verified", None)

        if isinstance(last_verified, str):
            try:
                last_verified = parse_as_of(last_verified)
            except ValueError as exc:
                log.warning(
                    "Invalid last_verified string %r in chrono branch: %s", last_verified, exc
                )
                last_verified = None

        new_edge = CausalEdge(
            source_node_id=src,
            target_node_id=tgt,
            edge_type=edge_type,
            confidence_score=confidence,
            decay_coefficient=decay_coeff,
            last_verified=last_verified,
        )

        # Remove existing edge between src and tgt
        new_outgoing.setdefault(src, [])
        new_incoming.setdefault(tgt, [])

        new_outgoing[src] = [e for e in new_outgoing[src] if e.target_node_id != tgt]
        new_incoming[tgt] = [e for e in new_incoming[tgt] if e.source_node_id != src]

        # Append new edge
        new_outgoing[src].append(new_edge)
        new_incoming[tgt].append(new_edge)

        # Make sure endpoints exist
        if src not in new_nodes:
            new_nodes[src] = CausalNode(node_id=src, node_type="device", namespace_id=fallback_ns)
        if tgt not in new_nodes:
            new_nodes[tgt] = CausalNode(node_id=tgt, node_type="service", namespace_id=fallback_ns)

    return CausalGraph(nodes=new_nodes, outgoing=new_outgoing, incoming=new_incoming)
