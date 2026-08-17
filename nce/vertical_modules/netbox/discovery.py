"""
nce/vertical_modules/netbox/discovery.py
========================================
BATCH-P3-NB-005 — Unregistered Asset Discovery & Draft Staging Write-Back

Implements the discovery reconciliation pipeline comparing network topology against
cached NetBox inventory. Creates staged change proposals on a staging branch
using NetBox's Branching API context, preventing direct production state mutation.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from jsonschema import validate

from nce.config import cfg
from nce.vertical_modules.netbox.graphql_activation import NetBoxGraphQLClient

log = logging.getLogger("nce.vertical_modules.netbox.discovery")

# JSON Schemas for validating NetBox DCIM write payloads before staging
DEVICE_WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "device_type": {"type": ["integer", "string"]},
        "role": {"type": ["integer", "string"]},
        "site": {"type": ["integer", "string"]},
        "serial": {"type": ["string", "null"]},
        "custom_fields": {"type": "object"},
    },
    "required": ["name", "device_type", "role", "site"],
}

INTERFACE_WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "device": {"type": ["integer", "string"]},
        "name": {"type": "string", "minLength": 1},
        "type": {"type": "string", "minLength": 1},
    },
    "required": ["device", "name", "type"],
}

CABLE_WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "a_terminations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "object_type": {"type": "string"},
                    "object_id": {"type": ["integer", "string"]},
                },
                "required": ["object_type", "object_id"],
            },
            "minItems": 1,
        },
        "b_terminations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "object_type": {"type": "string"},
                    "object_id": {"type": ["integer", "string"]},
                },
                "required": ["object_type", "object_id"],
            },
            "minItems": 1,
        },
        "status": {"type": "string"},
    },
    "required": ["a_terminations", "b_terminations"],
}


class NetBoxDiscoveryReconciler:
    """
    Reconciles live discovered hardware inventory and connections against NetBox cached inventory.
    Saves new detections as staging change proposals using the NetBox Branching API.
    """

    def __init__(
        self, netbox_client: NetBoxGraphQLClient, rest_client: httpx.AsyncClient | None = None
    ):
        self.netbox_client = netbox_client
        self.base_url = netbox_client.base_url
        self.headers = netbox_client.headers.copy()
        self._rest_client = rest_client

    async def _send_get(
        self, client: httpx.AsyncClient, url: str, headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        h = headers if headers is not None else self.headers
        resp = await client.get(url, headers=h, timeout=10.0)
        resp.raise_for_status()
        return resp.json()

    async def _send_post(
        self,
        client: httpx.AsyncClient,
        url: str,
        json_data: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        h = headers if headers is not None else self.headers
        resp = await client.post(url, json=json_data, headers=h, timeout=10.0)
        resp.raise_for_status()
        return resp.json()

    async def get_or_create_staging_branch(self, branch_name: str = "nce-staged-discovery") -> str:
        """
        Creates or retrieves the staging branch context. Returns the branch schema_id.
        """
        url = f"{self.base_url}/api/plugins/branching/branches/"
        list_url = f"{url}?name={branch_name}"

        async def execute_ops(client: httpx.AsyncClient) -> str:
            # 1. Fetch branch if it already exists
            data = await self._send_get(client, list_url)
            results = data.get("results") or []
            for branch in results:
                if branch.get("name") == branch_name:
                    return str(branch.get("schema_id") or branch.get("id"))

            # 2. Create the staging branch
            payload = {"name": branch_name, "status": "new"}
            data = await self._send_post(client, url, payload)
            return str(data.get("schema_id") or data.get("id"))

        if self._rest_client is not None:
            return await execute_ops(self._rest_client)
        else:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                return await execute_ops(client)

    async def reconcile(self, live_topology: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        """
        Compares live discovered topology elements against NetBox cached inventory.
        Pinpoints unregistered devices, interfaces, and connections.
        """
        from nce.vertical_modules.netbox.graphql_activation import UNIFIED_TOPOLOGY_QUERY

        response = await self.netbox_client.execute_query(UNIFIED_TOPOLOGY_QUERY)

        cached_devices: dict[str, Any] = {}
        cached_interfaces: set[tuple[str, str]] = set()
        cached_connections: set[tuple[tuple[str, str], tuple[str, str]]] = set()

        data_payload = response.get("data") or {}
        site_list = data_payload.get("site_list") or []

        def process_device(device: dict[str, Any]) -> None:
            dev_name = device.get("name")
            if not dev_name:
                return
            cached_devices[dev_name] = device.get("id")

            # Load interfaces
            interfaces = device.get("interfaces") or []
            for interface in interfaces:
                int_name = interface.get("name")
                if not int_name:
                    continue
                cached_interfaces.add((dev_name, int_name))

                # Load connection cables
                cable = interface.get("cable")
                if cable and isinstance(cable, dict):
                    a_terms = cable.get("a_terminations") or []
                    b_terms = cable.get("b_terminations") or []
                    for a in a_terms:
                        a_dev = a.get("device", {}).get("name")
                        a_int = a.get("name")
                        if not a_dev or not a_int:
                            continue
                        for b in b_terms:
                            b_dev = b.get("device", {}).get("name")
                            b_int = b.get("name")
                            if not b_dev or not b_int:
                                continue
                            conn_key = tuple(sorted([(a_dev, a_int), (b_dev, b_int)]))
                            cached_connections.add(conn_key)  # type: ignore

        # Process all devices from GraphQL tree
        for site in site_list:
            racks = site.get("racks") or []
            for rack in racks:
                devices = rack.get("devices") or []
                for device in devices:
                    process_device(device)

            devices_direct = site.get("devices") or []
            for device in devices_direct:
                process_device(device)

        unregistered_devices = []
        unregistered_interfaces = []
        unregistered_cables = []

        # 1. Reconcile devices and interfaces
        live_devices = live_topology.get("devices") or []
        for dev in live_devices:
            dev_name = dev.get("name")
            if not dev_name:
                continue

            if dev_name not in cached_devices:
                cached_devices[dev_name] = None
                unregistered_devices.append(
                    {
                        "name": dev_name,
                        "serial": dev.get("serial") or "UNKNOWN",
                        "device_type": dev.get("device_type") or 1,
                        "role": dev.get("role") or 1,
                        "site": dev.get("site") or 1,
                        "custom_fields": dev.get("custom_fields") or {},
                    }
                )

            interfaces = dev.get("interfaces") or []
            for int_name in interfaces:
                if (dev_name, int_name) not in cached_interfaces:
                    cached_interfaces.add((dev_name, int_name))
                    unregistered_interfaces.append(
                        {
                            "device": dev_name,
                            "name": int_name,
                            "type": cfg.NCE_NETBOX_DEFAULT_INTERFACE_TYPE,
                        }
                    )

        # 2. Reconcile cables/connections
        live_cables = live_topology.get("cables") or []
        for cable in live_cables:
            a_dev = cable.get("a_device")
            a_int = cable.get("a_interface")
            b_dev = cable.get("b_device")
            b_int = cable.get("b_interface")
            if not all([a_dev, a_int, b_dev, b_int]):
                continue

            conn_key = tuple(sorted([(a_dev, a_int), (b_dev, b_int)]))
            if conn_key not in cached_connections:  # type: ignore
                cached_connections.add(conn_key)  # type: ignore
                unregistered_cables.append(
                    {
                        "a_terminations": [
                            {"object_type": "dcim.interface", "object_id": f"{a_dev}:{a_int}"}
                        ],
                        "b_terminations": [
                            {"object_type": "dcim.interface", "object_id": f"{b_dev}:{b_int}"}
                        ],
                        "status": "connected",
                    }
                )

        return {
            "devices": unregistered_devices,
            "interfaces": unregistered_interfaces,
            "cables": unregistered_cables,
        }

    async def stage_discovery(
        self,
        schema_id: str,
        unregistered_assets: dict[str, list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """
        Stages change proposals into the branch context by appending the X-NetBox-Branch header.
        Enforces schema validations before execution.
        """
        branch_headers = self.headers.copy()
        branch_headers["X-NetBox-Branch"] = schema_id

        async def run_staging(client: httpx.AsyncClient) -> list[dict[str, Any]]:
            proposals = []

            # 1. Stage Devices
            devices = unregistered_assets.get("devices") or []
            for dev in devices:
                validate(instance=dev, schema=DEVICE_WRITE_SCHEMA)
                url = f"{self.base_url}/api/dcim/devices/"
                res = await self._send_post(client, url, dev, headers=branch_headers)
                proposals.append(
                    {
                        "object_type": "device",
                        "name": dev["name"],
                        "netbox_id": res.get("id"),
                        "status": "staged",
                    }
                )

            # 2. Stage Interfaces
            interfaces = unregistered_assets.get("interfaces") or []
            for interface in interfaces:
                validate(instance=interface, schema=INTERFACE_WRITE_SCHEMA)
                url = f"{self.base_url}/api/dcim/interfaces/"
                res = await self._send_post(client, url, interface, headers=branch_headers)
                proposals.append(
                    {
                        "object_type": "interface",
                        "name": f"{interface['device']}:{interface['name']}",
                        "netbox_id": res.get("id"),
                        "status": "staged",
                    }
                )

            # 3. Stage Cables
            cables = unregistered_assets.get("cables") or []
            for cable in cables:
                validate(instance=cable, schema=CABLE_WRITE_SCHEMA)
                url = f"{self.base_url}/api/dcim/cables/"
                res = await self._send_post(client, url, cable, headers=branch_headers)
                proposals.append(
                    {"object_type": "cable", "netbox_id": res.get("id"), "status": "staged"}
                )

            return proposals

        if self._rest_client is not None:
            return await run_staging(self._rest_client)
        else:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                return await run_staging(client)
