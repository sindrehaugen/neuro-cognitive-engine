"""Canonical entity envelopes and CAD/diagram Intermediate Representation (IR) for NCE connectors."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_SAFE_URN_PART_RE = re.compile(r"^[A-Za-z0-9._:@/-]{1,128}$")

SIGNAL_DOMAIN_STYLES: dict[str, dict[str, str]] = {
    "power": {"stroke": "#dc2626", "strokeWidth": "2.5", "dash": "none"},
    "av_signal": {"stroke": "#2563eb", "strokeWidth": "2", "dash": "none"},
    "control": {"stroke": "#16a34a", "strokeWidth": "1.75", "dash": "4 2"},
    "network": {"stroke": "#7c3aed", "strokeWidth": "2", "dash": "none"},
    "safety": {"stroke": "#ea580c", "strokeWidth": "2.5", "dash": "6 3"},
    "rf": {"stroke": "#0891b2", "strokeWidth": "1.5", "dash": "2 2"},
}


class CanonicalEntityEnvelope(BaseModel):
    """Canonical untrusted external entity envelope mapped to deterministic KG URNs."""

    model_config = ConfigDict(extra="allow")

    app_slug: str = Field(..., min_length=1, max_length=64)
    namespace_id: uuid.UUID | str
    entity_type: str = Field(..., min_length=1, max_length=64)
    external_id: str = Field(..., min_length=1, max_length=128)
    title: str = Field(..., min_length=1, max_length=256)
    summary: str = Field(default="", max_length=2048)
    attributes: dict[str, Any] = Field(default_factory=dict)
    provenance_tier: Literal["external_untrusted", "verified_internal"] = "external_untrusted"
    source_uri: str | None = None
    content_sha256: str | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("app_slug", "entity_type", "external_id")
    @classmethod
    def _validate_urn_segment(cls, value: str) -> str:
        clean = value.strip()
        if not _SAFE_URN_PART_RE.fullmatch(clean):
            raise ValueError(f"Unsafe URN segment: {value!r}")
        return clean

    @field_validator("namespace_id")
    @classmethod
    def _validate_namespace_uuid(cls, value: uuid.UUID | str) -> str:
        return str(uuid.UUID(str(value)))

    def kg_uri(self) -> str:
        """Return deterministic tenant-scoped URN: urn:nce:<app>:<ns>:<type>:<id>."""
        return f"urn:nce:{self.app_slug}:{self.namespace_id}:{self.entity_type}:{self.external_id}"

    def to_kg_node_payload(self) -> dict[str, Any]:
        """Build a Core-ready KG node ingestion dictionary preserving provenance_tier."""
        return {
            "uri": self.kg_uri(),
            "label": self.title,
            "entity_type": self.entity_type,
            "namespace_id": str(self.namespace_id),
            "summary": self.summary,
            "metadata": {
                **self.attributes,
                "app_slug": self.app_slug,
                "external_id": self.external_id,
                "provenance_tier": self.provenance_tier,
                "source_uri": self.source_uri,
                "content_sha256": self.content_sha256,
                "observed_at": self.observed_at.isoformat(),
            },
        }


class DiagramPortPinIR(BaseModel):
    """Explicit left/right/top/bottom port pin on a schematic block."""

    port_id: str
    side: Literal["left", "right", "top", "bottom"] = "right"
    label: str | None = None
    signal_domain: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DiagramSwimlaneIR(BaseModel):
    """Horizontal or vertical schematic swimlane band."""

    swimlane_id: str
    title: str
    y_top: float = 0.0
    height: float = 200.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class Iso7200TitleBlockIR(BaseModel):
    """ISO 7200 drawing title block metadata."""

    drawing_number: str
    title: str
    project_code: str = "NCE"
    revision: str = "A"
    author: str | None = None
    standard: str = "ISO 7200"
    metadata: dict[str, Any] = Field(default_factory=dict)


class DiagramNodeIR(BaseModel):
    """Canonical diagram node model shared across CAD/diagram connectors."""

    node_id: str
    label: str
    shape_type: str = "rectangle"
    x: float = 0.0
    y: float = 0.0
    width: float = 140.0
    height: float = 64.0
    layer_rank: int | None = None
    fl_code: str | None = None
    sku: str | None = None
    ip_address: str | None = None
    ports: list[DiagramPortPinIR] = Field(default_factory=list)
    props: dict[str, Any] = Field(default_factory=dict)


class DiagramEdgeIR(BaseModel):
    """Canonical diagram connector edge model."""

    edge_id: str
    source_id: str
    target_id: str
    source_port: str | None = None
    target_port: str | None = None
    label: str | None = None
    signal_domain: str | None = None
    waypoints: list[tuple[float, float]] = Field(default_factory=list)
    props: dict[str, Any] = Field(default_factory=dict)


class DiagramPageIR(BaseModel):
    """Single schematic/diagram page containing nodes, edges, and swimlanes."""

    name: str = "Page-1"
    nodes: list[DiagramNodeIR] = Field(default_factory=list)
    edges: list[DiagramEdgeIR] = Field(default_factory=list)
    swimlanes: list[DiagramSwimlaneIR] = Field(default_factory=list)
    title_block: Iso7200TitleBlockIR | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CanonicalDiagramIR(BaseModel):
    """Canonical vendor-neutral diagram intermediate representation."""

    diagram_id: str
    title: str = "Untitled Diagram"
    source_app: str | None = None
    pages: list[DiagramPageIR] = Field(default_factory=list)
    title_block: Iso7200TitleBlockIR | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


DiagramIR = CanonicalDiagramIR


def auto_layout_page(
    page: DiagramPageIR,
    *,
    spacing_x: float = 200.0,
    spacing_y: float = 120.0,
    columns: int = 4,
) -> DiagramPageIR:
    """Assign deterministic grid coordinates to nodes lacking explicit layout positions."""
    cols = max(1, columns)
    laid_out_nodes: list[DiagramNodeIR] = []
    for idx, node in enumerate(page.nodes):
        if node.x != 0.0 or node.y != 0.0:
            laid_out_nodes.append(node)
            continue
        col = idx % cols
        row = idx // cols
        laid_out_nodes.append(
            node.model_copy(
                update={
                    "x": 40.0 + col * spacing_x,
                    "y": 40.0 + row * spacing_y,
                }
            )
        )
    return page.model_copy(update={"nodes": laid_out_nodes})


def layout_sugiyama_orthogonal(
    page: DiagramPageIR,
    *,
    layer_spacing_x: float = 240.0,
    row_spacing_y: float = 130.0,
    margin_x: float = 60.0,
    margin_y: float = 60.0,
) -> DiagramPageIR:
    """Assign layered Sugiyama rank coordinates and orthogonal bus-channel waypoints."""
    node_ids = [n.node_id for n in page.nodes]
    adj: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for e in page.edges:
        if e.source_id in adj and e.target_id in adj and e.source_id != e.target_id:
            adj[e.source_id].append(e.target_id)

    ranks: dict[str, int] = {
        n.node_id: (n.layer_rank if n.layer_rank is not None else 0) for n in page.nodes
    }
    for _ in range(max(1, len(node_ids))):
        changed = False
        for u in node_ids:
            for v in adj[u]:
                if ranks[v] <= ranks[u]:
                    ranks[v] = ranks[u] + 1
                    changed = True
        if not changed:
            break

    layers: dict[int, list[DiagramNodeIR]] = {}
    for n in page.nodes:
        layers.setdefault(ranks[n.node_id], []).append(n)

    pos_in_layer: dict[str, int] = {}
    laid_nodes_map: dict[str, DiagramNodeIR] = {}
    parents: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for e in page.edges:
        if e.target_id in parents and e.source_id in ranks:
            parents[e.target_id].append(e.source_id)

    for rank in sorted(layers.keys()):
        bucket = layers[rank]
        if rank > 0:
            bucket.sort(
                key=lambda n: (
                    sum(pos_in_layer.get(p, 0) for p in parents.get(n.node_id, []))
                    / max(1, len(parents.get(n.node_id, []))),
                    n.node_id,
                )
            )
        for row_idx, n in enumerate(bucket):
            pos_in_layer[n.node_id] = row_idx
            nx = margin_x + rank * layer_spacing_x
            ny = margin_y + row_idx * row_spacing_y
            laid_nodes_map[n.node_id] = n.model_copy(update={"x": nx, "y": ny, "layer_rank": rank})

    routed_edges: list[DiagramEdgeIR] = []
    for idx, e in enumerate(page.edges):
        src = laid_nodes_map.get(e.source_id)
        tgt = laid_nodes_map.get(e.target_id)
        if src is None or tgt is None:
            routed_edges.append(e)
            continue
        sx = src.x + src.width
        sy = src.y + src.height / 2.0
        tx = tgt.x
        ty = tgt.y + tgt.height / 2.0
        channel_offset = ((idx % 5) - 2) * 10.0
        mid_x = ((sx + tx) / 2.0) + channel_offset
        waypoints = [(sx, sy), (mid_x, sy), (mid_x, ty), (tx, ty)]
        props = dict(e.props)
        if e.signal_domain and e.signal_domain in SIGNAL_DOMAIN_STYLES:
            props.setdefault("stroke", SIGNAL_DOMAIN_STYLES[e.signal_domain]["stroke"])
        routed_edges.append(e.model_copy(update={"waypoints": waypoints, "props": props}))

    swimlanes = list(page.swimlanes)
    if not swimlanes and laid_nodes_map:
        max_y = max(n.y + n.height for n in laid_nodes_map.values())
        swimlanes = [
            DiagramSwimlaneIR(
                swimlane_id="lane_main",
                title=page.name,
                y_top=margin_y - 20.0,
                height=max(160.0, max_y - margin_y + 60.0),
            )
        ]

    return page.model_copy(
        update={
            "nodes": [laid_nodes_map[n.node_id] for n in page.nodes],
            "edges": routed_edges,
            "swimlanes": swimlanes,
        }
    )


def diff_diagram_ir(base: DiagramIR, updated: DiagramIR) -> dict[str, Any]:
    """Compute structural node, edge, and property diffs between two canonical diagrams."""
    base_nodes = {(page.name, node.node_id): node for page in base.pages for node in page.nodes}
    upd_nodes = {(page.name, node.node_id): node for page in updated.pages for node in page.nodes}
    base_edges = {(page.name, edge.edge_id): edge for page in base.pages for edge in page.edges}
    upd_edges = {(page.name, edge.edge_id): edge for page in updated.pages for edge in page.edges}

    added_nodes = sorted(
        f"{p}:{nid}" for (p, nid) in (set(upd_nodes.keys()) - set(base_nodes.keys()))
    )
    removed_nodes = sorted(
        f"{p}:{nid}" for (p, nid) in (set(base_nodes.keys()) - set(upd_nodes.keys()))
    )
    modified_nodes: list[dict[str, Any]] = []
    for key in sorted(set(base_nodes.keys()) & set(upd_nodes.keys())):
        b = base_nodes[key]
        u = upd_nodes[key]
        changes: dict[str, Any] = {}
        for field in ("label", "shape_type", "fl_code", "sku", "ip_address", "props"):
            bv = getattr(b, field)
            uv = getattr(u, field)
            if bv != uv:
                changes[field] = {"from": bv, "to": uv}
        if changes:
            modified_nodes.append({"page": key[0], "node_id": key[1], "changes": changes})

    added_edges = sorted(
        f"{p}:{eid}" for (p, eid) in (set(upd_edges.keys()) - set(base_edges.keys()))
    )
    removed_edges = sorted(
        f"{p}:{eid}" for (p, eid) in (set(base_edges.keys()) - set(upd_edges.keys()))
    )
    modified_edges: list[dict[str, Any]] = []
    for key in sorted(set(base_edges.keys()) & set(upd_edges.keys())):
        b_e = base_edges[key]
        u_e = upd_edges[key]
        e_changes: dict[str, Any] = {}
        for field in ("source_id", "target_id", "source_port", "target_port", "label", "props"):
            bv = getattr(b_e, field)
            uv = getattr(u_e, field)
            if bv != uv:
                e_changes[field] = {"from": bv, "to": uv}
        if e_changes:
            modified_edges.append({"page": key[0], "edge_id": key[1], "changes": e_changes})

    has_changes = bool(
        added_nodes
        or removed_nodes
        or modified_nodes
        or added_edges
        or removed_edges
        or modified_edges
    )
    return {
        "has_changes": has_changes,
        "added_nodes": added_nodes,
        "removed_nodes": removed_nodes,
        "modified_nodes": modified_nodes,
        "added_edges": added_edges,
        "removed_edges": removed_edges,
        "modified_edges": modified_edges,
    }


__all__ = [
    "CanonicalDiagramIR",
    "CanonicalEntityEnvelope",
    "DiagramEdgeIR",
    "DiagramIR",
    "DiagramNodeIR",
    "DiagramPageIR",
    "DiagramPortPinIR",
    "DiagramSwimlaneIR",
    "Iso7200TitleBlockIR",
    "SIGNAL_DOMAIN_STYLES",
    "auto_layout_page",
    "diff_diagram_ir",
    "layout_sugiyama_orthogonal",
]
