"""
nce/causal/synthesis.py
======================
BATCH-P3-006 — Predictive Memory Synthesis

Implements the "Alert before the alert" predictive engine. Evaluates historical
failure trends from the event_log and NetBox MTBF indices, generating
probabilistic predictive fault nodes to alert operators before physical thresholds break.
"""

from __future__ import annotations

import json
import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Any

from jsonschema import ValidationError, validate

from nce.causal.correlation import CausalGraph

log = logging.getLogger("nce.causal.synthesis")

# JSON Schema for predictive fault nodes ensuring strict schema compatibility
PREDICTIVE_NODE_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "PredictiveFaultNode",
    "type": "object",
    "properties": {
        "node_id": {"type": "string"},
        "node_type": {"type": "string", "const": "predictive_fault"},
        "target_device_id": {"type": "string"},
        "target_device_type": {"type": "string"},
        "failure_probability": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "mtbf_hours": {"type": "number", "minimum": 1.0},
        "empirical_failures_count": {"type": "integer", "minimum": 0},
        "estimated_time_to_failure_days": {"type": "number", "minimum": 0.0},
        "created_at": {"type": "string", "format": "date-time"},
    },
    "required": [
        "node_id",
        "node_type",
        "target_device_id",
        "target_device_type",
        "failure_probability",
        "mtbf_hours",
    ],
}


class PredictiveSynthesisEngine:
    """
    Predictive engine that evaluates historical structural failures against
    NetBox MTBF data to insert ahead-of-time fault alerts into the topology.
    """

    def __init__(self, pg_pool: Any):
        self.pg_pool = pg_pool
        self._netbox_available: bool | None = None

    async def fetch_historical_incidents(
        self, conn: Any, namespace_id: uuid.UUID, time_window_days: float = 30.0
    ) -> dict[str, int]:
        """
        Query event_log to extract and aggregate recent incident/failure indicators.
        Matches rollbacks and fail event types, parsing params to identify node IDs.
        """
        rows = await conn.fetch(
            """
            SELECT event_type, params
            FROM event_log
            WHERE namespace_id = $1::uuid
              AND occurred_at >= NOW() - ($2::float * INTERVAL '1 day')
              AND (
                  event_type IN ('store_memory_rolled_back', 'saga_recovered')
                  OR event_type LIKE '%error%'
                  OR event_type LIKE '%fail%'
              )
            """,
            namespace_id,
            time_window_days,
        )

        incident_counts: dict[str, int] = {}
        for row in rows:
            event_type = row["event_type"]
            params_raw = row["params"]

            params: dict[str, Any] = {}
            if isinstance(params_raw, str):
                try:
                    params = json.loads(params_raw)
                except Exception:
                    pass
            elif isinstance(params_raw, dict):
                params = params_raw

            # Determine if this event indicates a structural or logical failure
            is_failure = (
                event_type == "store_memory_rolled_back"
                or event_type == "saga_recovered"
                or "error" in event_type
                or "fail" in event_type
            )

            if is_failure:
                # 1. Check direct reference fields
                node_id = (
                    params.get("memory_id") or params.get("device_id") or params.get("node_id")
                )
                if node_id:
                    node_id_str = str(node_id)
                    incident_counts[node_id_str] = incident_counts.get(node_id_str, 0) + 1
                    continue

                # 2. Check entities nested parameter
                entities = params.get("entities", [])
                for ent in entities:
                    if isinstance(ent, dict) and "label" in ent:
                        lbl = str(ent["label"])
                        incident_counts[lbl] = incident_counts.get(lbl, 0) + 1
                    elif isinstance(ent, str):
                        incident_counts[ent] = incident_counts.get(ent, 0) + 1

        return incident_counts

    async def fetch_netbox_mtbf(self, conn: Any) -> dict[str, float]:
        """
        Query device MTBF indices from NetBox tables if available.
        Defensively handles missing NetBox tables by falling back to static baselines.
        """
        if self._netbox_available is False:
            return {}

        mtbf_dict: dict[str, float] = {}
        try:
            rows = await conn.fetch(
                """
                SELECT node_id, mtbf_hours FROM netbox_devices
                """
            )
            for r in rows:
                if r["node_id"] and r["mtbf_hours"]:
                    mtbf_dict[str(r["node_id"])] = float(r["mtbf_hours"])
            self._netbox_available = True
        except Exception as exc:
            if self._netbox_available is None:
                log.warning(
                    "[PREDICTIVE-SYNTHESIS] NetBox device tables not available: %s. "
                    "Defaulting to baseline hardware model MTBF values (future checks muted).",
                    exc,
                )
            self._netbox_available = False
        return mtbf_dict

    def _resolve_mtbf_for_node(
        self, node_id: str, node_type: str, netbox_mtbf: dict[str, float]
    ) -> float:
        """Resolve device MTBF using NetBox overrides or industry default baselines."""
        if node_id in netbox_mtbf:
            return netbox_mtbf[node_id]

        # Standard baseline values in hours (MTBF)
        baselines = {
            "device": 100000.0,  # ~11.4 years
            "service": 50000.0,  # ~5.7 years
            "app": 30000.0,  # ~3.4 years
            "circuit": 80000.0,  # ~9.1 years
        }
        return baselines.get(node_type, 75000.0)

    async def generate_predictive_fault_nodes(
        self,
        conn: Any,
        namespace_id: uuid.UUID,
        time_window_days: float = 30.0,
        probability_threshold: float = 0.01,
    ) -> list[dict[str, Any]]:
        """
        Evaluate structural behavior and MTBF data to generate JSON-schema compliant
        predictive fault alerts for high-risk topological entities.
        """
        # Load observational causal infrastructure graph
        graph = await CausalGraph.load_from_db(conn, namespace_id)
        if not graph or graph.node_count == 0:
            return []

        incident_counts = await self.fetch_historical_incidents(
            conn, namespace_id, time_window_days
        )
        netbox_mtbf = await self.fetch_netbox_mtbf(conn)

        predictive_nodes: list[dict[str, Any]] = []

        for node_id in sorted(graph.node_ids):
            node = graph.get_node(node_id)
            if not node or node.node_type == "predictive_fault":
                continue

            # Compute MTBF failure rate (lambda_mtbf)
            mtbf = self._resolve_mtbf_for_node(node_id, node.node_type, netbox_mtbf)
            lambda_mtbf = 1.0 / mtbf

            # Compute empirical failure rate (lambda_empirical)
            failures_count = incident_counts.get(node_id, 0)
            lambda_empirical = failures_count / (time_window_days * 24.0)

            # Combined predictive failure rate (lambda_total)
            lambda_total = lambda_mtbf + lambda_empirical

            # Probabilistic failure over the time window: P(fail) = 1 - exp(-lambda * hours)
            time_window_hours = time_window_days * 24.0
            failure_probability = 1.0 - math.exp(-lambda_total * time_window_hours)

            # Estimated Time to Failure in days
            etf_days = 1.0 / (lambda_total * 24.0) if lambda_total > 0 else float("inf")

            if failure_probability >= probability_threshold:
                pred_node = {
                    "node_id": f"predictive_fault_{node_id}",
                    "node_type": "predictive_fault",
                    "target_device_id": node_id,
                    "target_device_type": node.node_type,
                    "failure_probability": float(failure_probability),
                    "mtbf_hours": float(mtbf),
                    "empirical_failures_count": int(failures_count),
                    "estimated_time_to_failure_days": (
                        float(etf_days) if etf_days != float("inf") else 99999.0
                    ),
                    "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                }

                # Schema verification
                try:
                    validate(instance=pred_node, schema=PREDICTIVE_NODE_SCHEMA)
                except ValidationError as err:
                    log.error(
                        "[PREDICTIVE-SYNTHESIS] Node %s validation failed: %s",
                        pred_node["node_id"],
                        err,
                    )
                    raise

                predictive_nodes.append(pred_node)

        return predictive_nodes

    async def sync_predictive_nodes_to_topology(
        self, conn: Any, namespace_id: uuid.UUID, predictive_nodes: list[dict[str, Any]]
    ) -> None:
        """
        Atomically clear old and write new predictive nodes into the topology_graph matrix.
        Predictive nodes propagate failure forward to target entities.
        """
        # Clear legacy predictive entries for the tenant
        await conn.execute(
            """
            DELETE FROM topology_graph
            WHERE namespace_id = $1::uuid
              AND (source_node_type = 'predictive_fault' OR target_node_type = 'predictive_fault')
            """,
            namespace_id,
        )

        if not predictive_nodes:
            return

        insert_data = [
            (
                namespace_id,
                node["node_id"],
                node["target_device_id"],
                node["target_device_type"],
                node["failure_probability"],
                json.dumps(
                    {
                        "mtbf_hours": node["mtbf_hours"],
                        "empirical_failures_count": node["empirical_failures_count"],
                        "estimated_time_to_failure_days": node["estimated_time_to_failure_days"],
                        "created_at": node["created_at"],
                    }
                ),
            )
            for node in predictive_nodes
        ]

        await conn.executemany(
            """
            INSERT INTO topology_graph (
                namespace_id,
                source_node_id, source_node_type,
                target_node_id, target_node_type,
                edge_type, confidence_score, decay_coefficient,
                last_verified, metadata
            ) VALUES (
                $1::uuid,
                $2, 'predictive_fault',
                $3, $4,
                'connected_to', $5::float8, 0.001,
                NOW(), $6::jsonb
            )
            """,
            insert_data,
        )
        log.info(
            "[PREDICTIVE-SYNTHESIS] Synced %d predictive fault nodes for namespace=%s",
            len(predictive_nodes),
            namespace_id,
        )
