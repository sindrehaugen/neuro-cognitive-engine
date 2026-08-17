"""
nce/vertical_modules/netbox/graphql_activation.py
==================================================
BATCH-P3-NB-003 — GraphQL-Powered Multi-Hop Spreading Activation

Refactors the network data-fetching layer by replacing single-hop REST loops
with GraphQL pipelines. Fetches nested infrastructure contexts and feeds
the parsed topology into the SpikingActivationEngine for context pre-fetching
during high-severity incident telemetry.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Any

import httpx

from nce.config import cfg
from nce.graph_query import SpikingActivationEngine

log = logging.getLogger("nce.vertical_modules.netbox.graphql_activation")

# Unified multi-hop GraphQL query targeting Sites -> Racks -> Devices -> Interfaces -> Cables
UNIFIED_TOPOLOGY_QUERY = """
query GetTopology {
  site_list {
    id
    name
    slug
    racks {
      id
      name
      devices {
        id
        name
        interfaces {
          id
          name
          cable {
            id
            status
            a_terminations {
              ... on InterfaceType {
                id
                name
                device {
                  id
                  name
                }
              }
            }
            b_terminations {
              ... on InterfaceType {
                id
                name
                device {
                  id
                  name
                }
              }
            }
          }
        }
      }
    }
    devices {
      id
      name
      rack {
        id
        name
      }
      interfaces {
        id
        name
        cable {
          id
          status
          a_terminations {
            ... on InterfaceType {
              id
              name
              device {
                id
                name
              }
            }
          }
          b_terminations {
            ... on InterfaceType {
              id
              name
              device {
                id
                name
              }
            }
          }
        }
      }
    }
  }
}
"""


class NetBoxGraphQLClient:
    """
    HTTP client for executing queries against NetBox's GraphQL endpoint.
    """

    def __init__(self, base_url: str, token: str, client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.url = f"{self.base_url}/graphql/"
        self.headers = {
            "Authorization": f"Token {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._client = client

    async def execute_query(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """
        Executes a GraphQL query payload. Logs and raises on GraphQL-level errors.
        """
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        if self._client is not None:
            return await self._send_request(self._client, payload)

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            return await self._send_request(client, payload)

    async def _send_request(
        self, client: httpx.AsyncClient, payload: dict[str, Any]
    ) -> dict[str, Any]:
        resp = await client.post(self.url, json=payload, headers=self.headers, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            log.error("[NETBOX-GRAPHQL] Query execution errors: %s", data["errors"])
            raise ValueError(f"NetBox GraphQL Error: {data['errors']}")
        return data


def parse_cable(cable: dict[str, Any], add_edge_fn: Callable[[str, str, float], None]) -> None:
    """
    Parses cable terminations and adds them to the topology.
    """
    status = cable.get("status") or ""
    # Set weight based on status
    weight = 1.0
    if isinstance(status, str) and status.upper() in (
        "PLANNED",
        "DEPRECATED",
        "FAILED",
        "DISCONNECTED",
    ):
        weight = 0.0

    a_terms = cable.get("a_terminations") or []
    b_terms = cable.get("b_terminations") or []

    for a in a_terms:
        if not isinstance(a, dict):
            continue
        a_device = a.get("device") or {}
        a_dev_name = a_device.get("name")
        a_int_name = a.get("name")
        if not a_dev_name or not a_int_name:
            continue
        a_label = f"{a_dev_name}:{a_int_name}"

        for b in b_terms:
            if not isinstance(b, dict):
                continue
            b_device = b.get("device") or {}
            b_dev_name = b_device.get("name")
            b_int_name = b.get("name")
            if not b_dev_name or not b_int_name:
                continue
            b_label = f"{b_dev_name}:{b_int_name}"

            add_edge_fn(a_label, b_label, weight)


def parse_topology(data: dict[str, Any]) -> dict[str, list[tuple[str, float]]]:
    """
    Parses the nested GraphQL response into a unified network topology graph represented as an adjacency list.
    Deduplicates and unifies weights using O(1) hashing with sorted tuples.
    """
    edge_map: dict[tuple[str, str], float] = {}

    def add_edge(u: str, v: str, weight: float = 1.0) -> None:
        if not u or not v or u == v:
            return
        # Sort node labels to ensure undirected edge uniqueness
        pair = (u, v) if u < v else (v, u)
        edge_map[pair] = max(edge_map.get(pair, 0.0), weight)

    data_payload = data.get("data") or {}
    site_list = data_payload.get("site_list") or []

    for site in site_list:
        if not isinstance(site, dict):
            continue
        site_name = site.get("name")
        if not site_name:
            continue

        # Map Site -> Racks
        racks = site.get("racks") or []
        for rack in racks:
            if not isinstance(rack, dict):
                continue
            rack_name = rack.get("name")
            if not rack_name:
                continue
            add_edge(site_name, rack_name, 1.0)

            # Map Rack -> Devices
            devices = rack.get("devices") or []
            for device in devices:
                if not isinstance(device, dict):
                    continue
                device_name = device.get("name")
                if not device_name:
                    continue
                add_edge(rack_name, device_name, 1.0)

                # Interfaces and cables
                interfaces = device.get("interfaces") or []
                for interface in interfaces:
                    if not isinstance(interface, dict):
                        continue
                    interface_name = interface.get("name")
                    if not interface_name:
                        continue
                    interface_label = f"{device_name}:{interface_name}"
                    add_edge(device_name, interface_label, 1.0)

                    # Cable terminations
                    cable = interface.get("cable")
                    if cable and isinstance(cable, dict):
                        parse_cable(cable, add_edge)

        # Map Site -> Devices directly
        devices_direct = site.get("devices") or []
        for device in devices_direct:
            if not isinstance(device, dict):
                continue
            device_name = device.get("name")
            if not device_name:
                continue

            rack = device.get("rack")
            if rack and isinstance(rack, dict) and rack.get("name"):
                rack_name = rack.get("name")
                add_edge(rack_name, device_name, 1.0)
                add_edge(site_name, rack_name, 1.0)
            else:
                add_edge(site_name, device_name, 1.0)

            # Interfaces and cables
            interfaces = device.get("interfaces") or []
            for interface in interfaces:
                if not isinstance(interface, dict):
                    continue
                interface_name = interface.get("name")
                if not interface_name:
                    continue
                interface_label = f"{device_name}:{interface_name}"
                add_edge(device_name, interface_label, 1.0)

                # Cable terminations
                cable = interface.get("cable")
                if cable and isinstance(cable, dict):
                    parse_cable(cable, add_edge)

    # Build adjacency list in O(E) after deduplication
    adj: dict[str, list[tuple[str, float]]] = {}
    for (u, v), w in edge_map.items():
        adj.setdefault(u, []).append((v, w))
        adj.setdefault(v, []).append((u, w))

    return adj


class GraphQLSpikingActivator:
    """
    Spiking Neural context pre-fetch engine powered by NetBox GraphQL queries.
    """

    def __init__(self, graphql_client: NetBoxGraphQLClient):
        self.client = graphql_client

    async def fetch_and_build_topology(self) -> dict[str, list[tuple[str, float]]]:
        """
        Fetches nested infrastructure dependency contexts and parses them into an adjacency list.
        """
        response = await self.client.execute_query(UNIFIED_TOPOLOGY_QUERY)
        return parse_topology(response)

    async def pre_fetch_context(
        self,
        anchor_label: str,
        telemetry_severity: float,
        conn: Any = None,
        namespace_id: uuid.UUID | str | None = None,
        theta: float = 0.5,
        decay: float = 0.85,
        alpha: float = 1.0,
        ticks: int = 2,
    ) -> set[str]:
        """
        Fires SpikingActivationEngine on the GraphQL-fetched topology context
        when severity exceeds 8.0. Enforces strict RLS namespace boundaries.
        """
        # Enforce RLS filtering using database EXISTS
        if namespace_id is not None:
            ns_uuid = uuid.UUID(str(namespace_id))
            if conn is not None:
                # Set local namespace context
                from nce.auth import set_namespace_context

                await set_namespace_context(conn, ns_uuid)

                # Fetch check if anchor_label is authorized (exists in kg_nodes or topology_graph)
                authorized = await conn.fetchval(
                    """
                    SELECT EXISTS(
                        SELECT 1 FROM kg_nodes WHERE namespace_id = $1::uuid AND label = $2
                        UNION
                        SELECT 1 FROM topology_graph 
                        WHERE namespace_id = $1::uuid 
                          AND (source_node_id = $2 OR target_node_id = $2)
                          AND valid_to IS NULL
                    )
                    """,
                    ns_uuid,
                    anchor_label,
                )
                if not authorized:
                    log.warning(
                        "[NETBOX-GRAPHQL-ACTIVATION] RLS Enforcer: Anchor node '%s' is not authorized for namespace %s.",
                        anchor_label,
                        namespace_id,
                    )
                    return set()
            else:
                # namespace_id supplied but no connection object: default-deny
                log.warning(
                    "[NETBOX-GRAPHQL-ACTIVATION] RLS Enforcer: namespace_id %s supplied without connection. Default-denied.",
                    namespace_id,
                )
                return set()

        # Fetch and build physical topology from NetBox
        adj = await self.fetch_and_build_topology()
        if not adj:
            log.info("[NETBOX-GRAPHQL-ACTIVATION] Empty topology fetched.")
            return set()

        # Set thresholds based on telemetry severity
        actual_theta = theta
        initial_charge = 1.0
        spike_thresh = cfg.NCE_TELEMETRY_SPIKE_THRESHOLD  # 8.0

        if telemetry_severity > spike_thresh:
            actual_theta = cfg.NCE_TELEMETRY_SPIKE_THETA  # 0.25
            initial_charge = cfg.NCE_TELEMETRY_SPIKE_CHARGE  # 2.0
            log.info(
                "[NETBOX-GRAPHQL-ACTIVATION] Telemetry severity spike (%.1f > %.1f). "
                "Lowering theta to %.2f and raising charge to %.2f.",
                telemetry_severity,
                spike_thresh,
                actual_theta,
                initial_charge,
            )

        # Initialize SpikingActivationEngine
        engine = SpikingActivationEngine(
            theta=actual_theta,
            decay=decay,
            alpha=alpha,
        )
        engine.set_potentials({anchor_label: initial_charge})

        # Run simulation for specified ticks
        for _ in range(ticks):
            engine.step(adj)

        # Active nodes: fired, anchor, and sub-threshold activated nodes (>= 10% of theta)
        active_labels = set(engine.fired_nodes) | {anchor_label}
        sub_threshold = actual_theta * 0.1
        for label, pot in engine.max_potentials.items():
            if pot >= sub_threshold:
                active_labels.add(label)

        # Apply RLS allowed labels filter to output if namespace_id was provided
        if namespace_id is not None and conn is not None:
            # Query db for intersection of active_labels to prevent memory bloat
            active_list = list(active_labels)
            allowed_rows = await conn.fetch(
                """
                SELECT label FROM kg_nodes WHERE namespace_id = $1::uuid AND label = ANY($2::text[])
                UNION
                SELECT source_node_id AS label FROM topology_graph 
                WHERE namespace_id = $1::uuid AND source_node_id = ANY($2::text[]) AND valid_to IS NULL
                UNION
                SELECT target_node_id AS label FROM topology_graph 
                WHERE namespace_id = $1::uuid AND target_node_id = ANY($2::text[]) AND valid_to IS NULL
                """,
                ns_uuid,
                active_list,
            )
            allowed_set = {r["label"] for r in allowed_rows}
            active_labels &= allowed_set

        return active_labels
