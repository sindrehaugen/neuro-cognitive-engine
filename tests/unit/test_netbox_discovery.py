"""
tests/unit/test_netbox_discovery.py
====================================
Unit tests for the unregistered asset discovery and draft staging write-back module.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from jsonschema import ValidationError

from nce.config import cfg
from nce.vertical_modules.netbox.discovery import NetBoxDiscoveryReconciler
from nce.vertical_modules.netbox.graphql_activation import NetBoxGraphQLClient

GRAPHQL_INVENTORY_RESPONSE = {
    "data": {
        "site_list": [
            {
                "id": "site-1",
                "name": "Site-A",
                "slug": "site-a",
                "racks": [
                    {
                        "id": "rack-1",
                        "name": "Rack-A1",
                        "devices": [
                            {
                                "id": "device-1",
                                "name": "switch-existing",
                                "interfaces": [
                                    {
                                        "id": "interface-1",
                                        "name": "GigabitEthernet0/1",
                                        "cable": None,
                                    },
                                    {
                                        "id": "interface-2",
                                        "name": "GigabitEthernet0/2",
                                        "cable": None,
                                    },
                                ],
                            }
                        ],
                    }
                ],
                "devices": [],
            }
        ]
    }
}


LIVE_TOPOLOGY = {
    "devices": [
        {
            "name": "switch-new",
            "serial": "SN-NEW-1",
            "device_type": 1,
            "role": 1,
            "site": 1,
            "interfaces": ["GigabitEthernet0/1", "GigabitEthernet0/2"],
        },
        {
            "name": "switch-existing",
            "interfaces": ["GigabitEthernet0/1", "GigabitEthernet0/2", "GigabitEthernet0/3"],
        },
    ],
    "cables": [
        {
            "a_device": "switch-existing",
            "a_interface": "GigabitEthernet0/3",
            "b_device": "switch-new",
            "b_interface": "GigabitEthernet0/1",
        }
    ],
}


def make_mock_response(status_code: int, json_data: dict[str, Any]) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


@pytest.mark.anyio
class TestNetBoxDiscoveryReconciler:
    async def test_reconcile_success(self) -> None:
        graphql_client = MagicMock(spec=NetBoxGraphQLClient)
        graphql_client.base_url = "http://netbox.test"
        graphql_client.headers = {"Authorization": "Token test-token"}
        graphql_client.execute_query = AsyncMock(return_value=GRAPHQL_INVENTORY_RESPONSE)

        reconciler = NetBoxDiscoveryReconciler(netbox_client=graphql_client)
        result = await reconciler.reconcile(LIVE_TOPOLOGY)

        # switch-new is unregistered
        assert len(result["devices"]) == 1
        assert result["devices"][0]["name"] == "switch-new"
        assert result["devices"][0]["serial"] == "SN-NEW-1"

        # switch-new:GigabitEthernet0/1, switch-new:GigabitEthernet0/2, switch-existing:GigabitEthernet0/3 are unregistered
        assert len(result["interfaces"]) == 3
        interfaces = {(i["device"], i["name"]) for i in result["interfaces"]}
        assert ("switch-new", "GigabitEthernet0/1") in interfaces
        assert ("switch-new", "GigabitEthernet0/2") in interfaces
        assert ("switch-existing", "GigabitEthernet0/3") in interfaces

        # cable connecting them is unregistered
        assert len(result["cables"]) == 1
        cable = result["cables"][0]
        assert cable["a_terminations"][0]["object_id"] == "switch-existing:GigabitEthernet0/3"
        assert cable["b_terminations"][0]["object_id"] == "switch-new:GigabitEthernet0/1"

    async def test_get_or_create_staging_branch_existing(self) -> None:
        graphql_client = MagicMock(spec=NetBoxGraphQLClient)
        graphql_client.base_url = "http://netbox.test"
        graphql_client.headers = {"Authorization": "Token test-token"}

        rest_client = MagicMock(spec=httpx.AsyncClient)
        rest_client.get = AsyncMock(
            return_value=make_mock_response(
                status_code=200,
                json_data={
                    "results": [{"name": "nce-staged-discovery", "schema_id": "branch-uuid-123"}]
                },
            )
        )

        reconciler = NetBoxDiscoveryReconciler(
            netbox_client=graphql_client, rest_client=rest_client
        )
        schema_id = await reconciler.get_or_create_staging_branch("nce-staged-discovery")

        assert schema_id == "branch-uuid-123"
        rest_client.get.assert_called_once_with(
            "http://netbox.test/api/plugins/branching/branches/?name=nce-staged-discovery",
            headers={"Authorization": "Token test-token"},
            timeout=10.0,
        )
        rest_client.post.assert_not_called()

    async def test_get_or_create_staging_branch_new(self) -> None:
        graphql_client = MagicMock(spec=NetBoxGraphQLClient)
        graphql_client.base_url = "http://netbox.test"
        graphql_client.headers = {"Authorization": "Token test-token"}

        rest_client = MagicMock(spec=httpx.AsyncClient)
        rest_client.get = AsyncMock(
            return_value=make_mock_response(status_code=200, json_data={"results": []})
        )
        rest_client.post = AsyncMock(
            return_value=make_mock_response(
                status_code=201, json_data={"schema_id": "new-branch-uuid"}
            )
        )

        reconciler = NetBoxDiscoveryReconciler(
            netbox_client=graphql_client, rest_client=rest_client
        )
        schema_id = await reconciler.get_or_create_staging_branch("nce-staged-discovery")

        assert schema_id == "new-branch-uuid"
        rest_client.get.assert_called_once()
        rest_client.post.assert_called_once_with(
            "http://netbox.test/api/plugins/branching/branches/",
            json={"name": "nce-staged-discovery", "status": "new"},
            headers={"Authorization": "Token test-token"},
            timeout=10.0,
        )

    async def test_stage_discovery_success(self) -> None:
        graphql_client = MagicMock(spec=NetBoxGraphQLClient)
        graphql_client.base_url = "http://netbox.test"
        graphql_client.headers = {"Authorization": "Token test-token"}

        rest_client = MagicMock(spec=httpx.AsyncClient)

        # We need mock responses for devices, interfaces, and cables posts
        rest_client.post = AsyncMock(
            side_effect=[
                make_mock_response(201, {"id": 101}),  # Device
                make_mock_response(201, {"id": 201}),  # Interface
                make_mock_response(201, {"id": 301}),  # Cable
            ]
        )

        reconciler = NetBoxDiscoveryReconciler(
            netbox_client=graphql_client, rest_client=rest_client
        )

        unregistered = {
            "devices": [
                {
                    "name": "switch-new",
                    "device_type": "type-1",
                    "role": "role-1",
                    "site": "site-1",
                    "serial": "SN-1",
                }
            ],
            "interfaces": [
                {"device": "switch-new", "name": "GigabitEthernet0/1", "type": "1000base-t"}
            ],
            "cables": [
                {
                    "a_terminations": [{"object_type": "dcim.interface", "object_id": 201}],
                    "b_terminations": [{"object_type": "dcim.interface", "object_id": 202}],
                }
            ],
        }

        proposals = await reconciler.stage_discovery(
            schema_id="branch-uuid-456", unregistered_assets=unregistered
        )

        assert len(proposals) == 3
        assert proposals[0] == {
            "object_type": "device",
            "name": "switch-new",
            "netbox_id": 101,
            "status": "staged",
        }
        assert proposals[1] == {
            "object_type": "interface",
            "name": "switch-new:GigabitEthernet0/1",
            "netbox_id": 201,
            "status": "staged",
        }
        assert proposals[2] == {"object_type": "cable", "netbox_id": 301, "status": "staged"}

        # Verify header inclusion
        expected_headers = {
            "Authorization": "Token test-token",
            "X-NetBox-Branch": "branch-uuid-456",
        }
        rest_client.post.assert_any_call(
            "http://netbox.test/api/dcim/devices/",
            json=unregistered["devices"][0],
            headers=expected_headers,
            timeout=10.0,
        )
        rest_client.post.assert_any_call(
            "http://netbox.test/api/dcim/interfaces/",
            json=unregistered["interfaces"][0],
            headers=expected_headers,
            timeout=10.0,
        )
        rest_client.post.assert_any_call(
            "http://netbox.test/api/dcim/cables/",
            json=unregistered["cables"][0],
            headers=expected_headers,
            timeout=10.0,
        )

    async def test_stage_discovery_validation_failure_device(self) -> None:
        graphql_client = MagicMock(spec=NetBoxGraphQLClient)
        graphql_client.base_url = "http://netbox.test"
        graphql_client.headers = {"Authorization": "Token test-token"}

        rest_client = MagicMock(spec=httpx.AsyncClient)
        reconciler = NetBoxDiscoveryReconciler(
            netbox_client=graphql_client, rest_client=rest_client
        )

        # Missing "site" from device payload
        unregistered = {
            "devices": [
                {
                    "name": "switch-invalid",
                    "device_type": "type-1",
                    "role": "role-1",
                }
            ],
            "interfaces": [],
            "cables": [],
        }

        with pytest.raises(ValidationError) as excinfo:
            await reconciler.stage_discovery(
                schema_id="branch-uuid-456", unregistered_assets=unregistered
            )

        assert "'site' is a required property" in str(excinfo.value)
        rest_client.post.assert_not_called()

    async def test_stage_discovery_validation_failure_interface(self) -> None:
        graphql_client = MagicMock(spec=NetBoxGraphQLClient)
        graphql_client.base_url = "http://netbox.test"
        graphql_client.headers = {"Authorization": "Token test-token"}

        rest_client = MagicMock(spec=httpx.AsyncClient)
        reconciler = NetBoxDiscoveryReconciler(
            netbox_client=graphql_client, rest_client=rest_client
        )

        # Missing "type" from interface payload
        unregistered = {
            "devices": [],
            "interfaces": [
                {
                    "device": "switch-new",
                    "name": "GigabitEthernet0/1",
                }
            ],
            "cables": [],
        }

        with pytest.raises(ValidationError) as excinfo:
            await reconciler.stage_discovery(
                schema_id="branch-uuid-456", unregistered_assets=unregistered
            )

        assert "'type' is a required property" in str(excinfo.value)
        rest_client.post.assert_not_called()

    async def test_stage_discovery_validation_failure_cable(self) -> None:
        graphql_client = MagicMock(spec=NetBoxGraphQLClient)
        graphql_client.base_url = "http://netbox.test"
        graphql_client.headers = {"Authorization": "Token test-token"}

        rest_client = MagicMock(spec=httpx.AsyncClient)
        reconciler = NetBoxDiscoveryReconciler(
            netbox_client=graphql_client, rest_client=rest_client
        )

        # Empty "a_terminations" list
        unregistered = {
            "devices": [],
            "interfaces": [],
            "cables": [
                {
                    "a_terminations": [],
                    "b_terminations": [{"object_type": "dcim.interface", "object_id": 202}],
                }
            ],
        }

        with pytest.raises(ValidationError) as excinfo:
            await reconciler.stage_discovery(
                schema_id="branch-uuid-456", unregistered_assets=unregistered
            )

        assert "non-empty" in str(excinfo.value) or "too short" in str(excinfo.value)
        rest_client.post.assert_not_called()

    async def test_reconcile_duplicate_prevention(self) -> None:
        graphql_client = MagicMock(spec=NetBoxGraphQLClient)
        graphql_client.base_url = "http://netbox.test"
        graphql_client.headers = {"Authorization": "Token test-token"}
        graphql_client.execute_query = AsyncMock(return_value=GRAPHQL_INVENTORY_RESPONSE)

        reconciler = NetBoxDiscoveryReconciler(netbox_client=graphql_client)

        duplicate_topology = {
            "devices": [
                {
                    "name": "switch-new",
                    "serial": "SN-NEW-1",
                    "device_type": 1,
                    "role": 1,
                    "site": 1,
                    "interfaces": [
                        "GigabitEthernet0/1",
                        "GigabitEthernet0/1",
                    ],  # Duplicate interface
                },
                {
                    "name": "switch-new",  # Duplicate device name
                    "serial": "SN-NEW-1",
                    "device_type": 1,
                    "role": 1,
                    "site": 1,
                    "interfaces": ["GigabitEthernet0/1"],
                },
                {
                    "name": "switch-existing",
                    "interfaces": [
                        "GigabitEthernet0/1",
                        "GigabitEthernet0/2",
                        "GigabitEthernet0/3",
                    ],
                },
            ],
            "cables": [
                {
                    "a_device": "switch-existing",
                    "a_interface": "GigabitEthernet0/3",
                    "b_device": "switch-new",
                    "b_interface": "GigabitEthernet0/1",
                },
                {
                    "a_device": "switch-existing",  # Duplicate cable link
                    "a_interface": "GigabitEthernet0/3",
                    "b_device": "switch-new",
                    "b_interface": "GigabitEthernet0/1",
                },
            ],
        }

        result = await reconciler.reconcile(duplicate_topology)

        # Assert duplicate device was filtered
        assert len(result["devices"]) == 1
        assert result["devices"][0]["name"] == "switch-new"

        # Assert duplicate interface was filtered (switch-new:GigabitEthernet0/1, switch-existing:GigabitEthernet0/3)
        assert len(result["interfaces"]) == 2
        interfaces = {(i["device"], i["name"]) for i in result["interfaces"]}
        assert ("switch-new", "GigabitEthernet0/1") in interfaces
        assert ("switch-existing", "GigabitEthernet0/3") in interfaces

        # Assert duplicate cable was filtered
        assert len(result["cables"]) == 1

    async def test_stage_discovery_null_serial(self) -> None:
        graphql_client = MagicMock(spec=NetBoxGraphQLClient)
        graphql_client.base_url = "http://netbox.test"
        graphql_client.headers = {"Authorization": "Token test-token"}

        rest_client = MagicMock(spec=httpx.AsyncClient)
        rest_client.post = AsyncMock(return_value=make_mock_response(201, {"id": 101}))

        reconciler = NetBoxDiscoveryReconciler(
            netbox_client=graphql_client, rest_client=rest_client
        )

        unregistered = {
            "devices": [
                {
                    "name": "switch-null-serial",
                    "device_type": "type-1",
                    "role": "role-1",
                    "site": "site-1",
                    "serial": None,  # Null serial value should be allowed
                }
            ],
            "interfaces": [],
            "cables": [],
        }

        proposals = await reconciler.stage_discovery(
            schema_id="branch-uuid-456", unregistered_assets=unregistered
        )
        assert len(proposals) == 1
        assert proposals[0]["name"] == "switch-null-serial"
        assert proposals[0]["status"] == "staged"

    async def test_reconcile_custom_default_interface_type(self, monkeypatch) -> None:
        graphql_client = MagicMock(spec=NetBoxGraphQLClient)
        graphql_client.base_url = "http://netbox.test"
        graphql_client.headers = {"Authorization": "Token test-token"}
        graphql_client.execute_query = AsyncMock(return_value=GRAPHQL_INVENTORY_RESPONSE)

        # Override default type config
        monkeypatch.setattr(cfg, "NCE_NETBOX_DEFAULT_INTERFACE_TYPE", "10gbase-x-sfpp")

        reconciler = NetBoxDiscoveryReconciler(netbox_client=graphql_client)
        result = await reconciler.reconcile(LIVE_TOPOLOGY)

        assert len(result["interfaces"]) == 3
        # Assert type matches modified configuration
        for interface in result["interfaces"]:
            assert interface["type"] == "10gbase-x-sfpp"
