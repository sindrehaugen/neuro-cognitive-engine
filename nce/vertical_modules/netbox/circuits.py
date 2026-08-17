"""
nce/vertical_modules/netbox/circuits.py
======================================
BATCH-P3-NB-002 — Circuit Provider Intelligence & Escalation Engine

Integrates NetBox Circuits APIs with NCE's do-calculus causal inference graph.
Evaluates telemetry degradation patterns, and auto-generates structured
upstream escalation tickets targeting external provider interfaces when
degradation is causally linked to circuit nodes.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import httpx

from nce.causal.correlation import CausalGraph, DoCalculusEngine

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.netbox.circuits")


class NetBoxCircuitsClient:
    """
    HTTP client for querying NetBox Circuits model records.
    """

    def __init__(self, base_url: str, token: str, client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Token {token}",
            "Accept": "application/json",
        }
        self._client = client

    async def fetch_circuits(self) -> list[dict[str, Any]]:
        """Fetch all circuit records from NetBox."""
        url = f"{self.base_url}/api/circuits/circuits/"
        if self._client is not None:
            return await self._send_get(self._client, url)

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            return await self._send_get(client, url)

    async def _send_get(self, client: httpx.AsyncClient, url: str) -> list[dict[str, Any]]:
        resp = await client.get(url, headers=self.headers, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        return data.get("results", [])


class NetBoxCircuitEscalator:
    """
    Automated telemetry-to-circuit escalation workflow.
    Evaluates real-time telemetry degradation against circuit topologies
    using Judea Pearl's do-calculus.
    """

    def __init__(self, netbox_client: NetBoxCircuitsClient):
        self.netbox_client = netbox_client

    async def evaluate_and_escalate(
        self,
        conn: Any,
        namespace_id: uuid.UUID,
        telemetry_degradations: dict[str, float],
        degradation_threshold: float = 0.5,
        causal_threshold: float = 0.5,
    ) -> list[dict[str, Any]]:
        """
        Analytics loop that evaluates real-time telemetry degradation patterns against
        circuit topologies using Pearl's do-calculus.
        Auto-generates structured upstream escalation tickets for circuits targeting
        external provider interfaces when degradation matches.
        """
        # 1. Fetch circuits from NetBox
        circuits = await self.netbox_client.fetch_circuits()
        if not circuits:
            log.info("[NETBOX-CIRCUITS] No circuits retrieved from NetBox.")
            return []

        # 2. Load causal graph
        graph = await CausalGraph.load_from_db(conn, namespace_id)
        if not graph or graph.node_count == 0:
            log.info("[NETBOX-CIRCUITS] Empty causal graph loaded.")
            return []

        engine = DoCalculusEngine()
        tickets = []

        # 3. Filter telemetry degradations
        degraded_nodes = {
            node_id: val
            for node_id, val in telemetry_degradations.items()
            if val >= degradation_threshold
        }
        if not degraded_nodes:
            log.info("[NETBOX-CIRCUITS] No telemetry nodes exceed degradation threshold.")
            return []

        # 4. Evaluate each circuit
        for circuit in circuits:
            # Map circuit id and details defensively
            circuit_id = circuit.get("cid") or str(circuit.get("id"))
            if not circuit_id:
                continue

            # The circuit node ID in the CausalGraph could be circuit_id itself or have a prefix
            circuit_node_id = None
            if circuit_id in graph.node_ids:
                circuit_node_id = circuit_id
            elif f"circuit_{circuit_id}" in graph.node_ids:
                circuit_node_id = f"circuit_{circuit_id}"

            if not circuit_node_id:
                # This circuit is not registered in our topology graph
                continue

            # Run do-calculus intervention do(C = failed)
            try:
                eval_result = engine.evaluate(graph, intervention_node_id=circuit_node_id)
            except Exception as exc:
                log.warning(
                    "[NETBOX-CIRCUITS] do-calculus evaluation failed for circuit node %s: %s",
                    circuit_node_id,
                    exc,
                )
                continue

            # Cross-reference with degraded nodes
            causally_linked = {}
            for node_id, severity in degraded_nodes.items():
                prob = eval_result.probability_matrix.get(node_id, 0.0)
                if prob >= causal_threshold:
                    causally_linked[node_id] = {
                        "degradation_severity": severity,
                        "causal_probability": prob,
                    }

            if causally_linked:
                # Pull provider details defensively
                provider = circuit.get("provider") or {}
                provider_id = provider.get("id") or circuit.get("provider_id")
                provider_name = provider.get("name") or "Unknown Provider"

                custom_fields = circuit.get("custom_fields") or {}
                account_string = (
                    custom_fields.get("account_string")
                    or custom_fields.get("account")
                    or circuit.get("account")
                    or f"ACCT-{provider_name.upper()}"
                )

                commit_rate = circuit.get("commit_rate") or custom_fields.get("commit_rate") or 0

                # Auto-generate structured upstream escalation ticket targeting external provider
                ticket = {
                    "ticket_id": str(uuid.uuid4()),
                    "circuit_id": circuit_id,
                    "circuit_node_id": circuit_node_id,
                    "provider_id": str(provider_id) if provider_id else None,
                    "provider_name": provider_name,
                    "account_string": account_string,
                    "commit_rate_kbps": int(commit_rate) if commit_rate else None,
                    "causally_linked_degradations": causally_linked,
                    "severity": "CRITICAL"
                    if any(v["degradation_severity"] >= 0.8 for v in causally_linked.values())
                    else "WARNING",
                    "description": (
                        f"Automated NetBox Circuit Escalation for Account {account_string}. "
                        f"Circuit {circuit_id} provided by {provider_name} has been causally linked to telemetry degradation "
                        f"on {len(causally_linked)} nodes via Judea Pearl's do-calculus evaluations."
                    ),
                    "escalated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                }
                tickets.append(ticket)
                log.info(
                    "[NETBOX-CIRCUITS] Generated escalation ticket %s for circuit %s.",
                    ticket["ticket_id"],
                    circuit_id,
                )

        return tickets


async def handle_evaluate_circuit_impact(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """
    On-demand do-calculus circuit impact evaluation tool.

    Arguments
    ---------
    namespace_id           : str
    telemetry_degradations : dict[str, float]
    degradation_threshold  : float (optional, default 0.5)
    causal_threshold       : float (optional, default 0.5)
    """
    namespace_id = arguments.get("namespace_id", "")
    telemetry_degradations = arguments.get("telemetry_degradations", {})
    degradation_threshold = float(arguments.get("degradation_threshold", 0.5))
    causal_threshold = float(arguments.get("causal_threshold", 0.5))

    if not namespace_id:
        return json.dumps({"error": "namespace_id is required"})

    from nce.config import cfg

    if not cfg.NCE_NETBOX_URL or not cfg.NCE_NETBOX_TOKEN:
        return json.dumps({"error": "NetBox is not configured (NCE_NETBOX_URL/TOKEN unset)"})

    import uuid as _uuid

    from nce.db_utils import scoped_pg_session

    try:
        ns_uuid = _uuid.UUID(namespace_id)
        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            client = NetBoxCircuitsClient(cfg.NCE_NETBOX_URL, cfg.NCE_NETBOX_TOKEN)
            escalator = NetBoxCircuitEscalator(client)
            tickets = await escalator.evaluate_and_escalate(
                conn=conn,
                namespace_id=ns_uuid,
                telemetry_degradations=telemetry_degradations,
                degradation_threshold=degradation_threshold,
                causal_threshold=causal_threshold,
            )
        return json.dumps({"status": "success", "tickets": tickets})
    except Exception as exc:
        log.exception("handle_evaluate_circuit_impact failed")
        return json.dumps({"error": str(exc)})
