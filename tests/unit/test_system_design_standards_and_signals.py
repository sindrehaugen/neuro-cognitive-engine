"""
tests/unit/test_system_design_standards_and_signals.py
======================================================
Unit tests for System Design Wave C-5:
- Standards config-as-IP retrieval and search (nce/vertical_modules/system_design/standards.py)
- Signal distribution rules config-as-IP and rule evaluation (nce/vertical_modules/system_design/signal_distribution.py)
- Device capability and port sync from ETIM specs (nce/vertical_modules/system_design/capability_sync.py)
- Corresponding MCP handlers and Admin HTTP routes.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce import admin_state
from nce.admin_handlers import system_design as admin_routes
from nce.mcp_errors import McpError
from nce.mcp_stdio_tools import TOOLS
from nce.tool_registry import TOOL_REGISTRY
from nce.vertical_modules.system_design.capability_sync import (
    _extract_capabilities_from_etim,
    do_sync_device_capabilities,
)
from nce.vertical_modules.system_design.mcp_handlers import (
    handle_system_design_get_signal_rules,
    handle_system_design_get_standards,
    handle_system_design_sync_device_capabilities,
)
from nce.vertical_modules.system_design.signal_distribution import (
    do_get_signal_rules,
    evaluate_signal_distribution,
)
from nce.vertical_modules.system_design.standards import do_get_standards

# ---------------------------------------------------------------------------
# Helpers & Stubs
# ---------------------------------------------------------------------------

_TEST_NS = str(uuid.uuid4())
_TEST_DEVICE = "device:main-dsp"


class _StubRequest:
    """Minimal request stub for Starlette/FastAPI-style handlers."""

    def __init__(
        self,
        body: Any = None,
        query_params: dict[str, Any] | None = None,
    ) -> None:
        self._body = body or {}
        self.query_params = query_params or {}

    async def json(self) -> Any:
        return self._body


class _EngineStub:
    def __init__(self) -> None:
        self.pg_pool = MagicMock()


# ---------------------------------------------------------------------------
# 1. Standards Tests
# ---------------------------------------------------------------------------


class TestSystemDesignStandards:
    def test_get_standards_all(self) -> None:
        res = do_get_standards(None, {})
        assert "categories" in res
        assert "standards" in res
        assert res["total"] == len(res["standards"])
        assert res["total"] > 0

        # Verify default categories exist
        cat_keys = {c["key"] for c in res["categories"]}
        assert "cabling_hdmi" in cat_keys
        assert "cabling_usbc" in cat_keys
        assert "cabling_category" in cat_keys
        assert "cabling_audio" in cat_keys
        assert "mounting_display" in cat_keys
        assert "power_poe" in cat_keys

    def test_get_standards_filter_category(self) -> None:
        res = do_get_standards(None, {"category": "cabling_hdmi"})
        assert res["total"] > 0
        for s in res["standards"]:
            assert s["category"] == "cabling_hdmi"

    def test_get_standards_filter_standard_id(self) -> None:
        res = do_get_standards(None, {"standard_id": "HDMI-2.0"})
        assert res["total"] == 1
        std = res["standards"][0]
        assert std["id"] == "HDMI-2.0"
        assert "HDMI 2.0" in std["name"]

    def test_get_standards_search(self) -> None:
        res = do_get_standards(None, {"search": "HDBaseT"})
        assert res["total"] >= 1
        names_and_rules = " ".join(
            s["name"] + " " + s.get("usage_rule", "") for s in res["standards"]
        )
        assert "HDBaseT" in names_and_rules

    def test_get_standards_non_matching_filter(self) -> None:
        res = do_get_standards(None, {"category": "non_existent_category"})
        assert res["total"] == 0
        assert res["standards"] == []

    @pytest.mark.asyncio
    async def test_mcp_handler_get_standards(self) -> None:
        engine = _EngineStub()
        payload = await handle_system_design_get_standards(engine, {"category": "mounting_display"})
        data = json.loads(payload)
        assert "standards" in data
        assert all(s["category"] == "mounting_display" for s in data["standards"])

    @pytest.mark.asyncio
    async def test_api_route_get_standards(self) -> None:
        req = _StubRequest(query_params={"category": "cabling_hdmi"})
        resp = await admin_routes.api_system_design_get_standards(req)
        assert resp.status_code == 200
        data = json.loads(resp.body)
        assert "standards" in data
        assert len(data["standards"]) > 0


# ---------------------------------------------------------------------------
# 2. Signal Distribution Rules Tests
# ---------------------------------------------------------------------------


class TestSystemDesignSignalRules:
    def test_get_signal_rules_all(self) -> None:
        res = do_get_signal_rules(None, {})
        assert "roles" in res
        assert "rules" in res
        assert len(res["rules"]) > 0

        rule_ids = {r["id"] for r in res["rules"]}
        assert "RULE-USBC-SHORT-DIRECT" in rule_ids
        assert "RULE-NO-PATHWAY-WIRELESS" in rule_ids
        assert "RULE-CAT6A-LONG-CHARGING" in rule_ids

    def test_evaluate_signal_short_usbc(self) -> None:
        eval_result = evaluate_signal_distribution(
            {
                "containment": "surface",
                "distance_m": 1.8,
                "altmode": True,
                "allow_usbc": True,
                "wants_wireless": False,
                "wants_charging": True,
                "laptop_watt": 65.0,
                "vendor_ecosystem": "native",
            }
        )
        assert eval_result["recommended_role"] == "USBC_DIRECT"
        assert eval_result["power_delivery_status"] == "sufficient"
        assert eval_result["rule_id"] == "RULE-USBC-SHORT-DIRECT"

    def test_evaluate_signal_long_distance_extender(self) -> None:
        eval_result = evaluate_signal_distribution(
            {
                "containment": "in_wall",
                "distance_m": 25.0,
                "altmode": False,
                "allow_usbc": False,
                "wants_wireless": False,
                "wants_charging": True,
                "laptop_watt": 65.0,
                "vendor_ecosystem": "crestron",
            }
        )
        assert eval_result["recommended_role"] == "CAT_EXTENDER_GENERIC"
        assert eval_result["rule_id"] == "RULE-CAT6A-LONG-CHARGING"

    def test_evaluate_signal_wireless_byod(self) -> None:
        eval_result = evaluate_signal_distribution(
            {
                "containment": "none",
                "distance_m": 5.0,
                "altmode": False,
                "allow_usbc": True,
                "wants_wireless": True,
                "wants_charging": False,
                "laptop_watt": 0.0,
                "vendor_ecosystem": "byod",
            }
        )
        assert eval_result["recommended_role"] == "WIRELESS"
        assert eval_result["rule_id"] == "RULE-NO-PATHWAY-WIRELESS"

    @pytest.mark.asyncio
    async def test_mcp_handler_get_signal_rules(self) -> None:
        engine = _EngineStub()
        payload = await handle_system_design_get_signal_rules(
            engine,
            {
                "distance_m": 2.0,
                "altmode": True,
                "allow_usbc": True,
            },
        )
        data = json.loads(payload)
        assert "recommended_role" in data
        assert data["recommended_role"] == "USBC_DIRECT"

    @pytest.mark.asyncio
    async def test_api_route_get_signal_rules(self) -> None:
        req = _StubRequest(
            query_params={
                "distance_m": "30.0",
                "wants_charging": "true",
            }
        )
        resp = await admin_routes.api_system_design_get_signal_rules(req)
        assert resp.status_code == 200
        data = json.loads(resp.body)
        assert "recommended_role" in data
        assert data["recommended_role"] == "CAT_EXTENDER_GENERIC"


# ---------------------------------------------------------------------------
# 3. Capability Sync Tests
# ---------------------------------------------------------------------------


class TestSystemDesignCapabilitySync:
    def test_extract_capabilities_from_etim(self) -> None:
        etim = {
            "manufacturer": "QSC",
            "model_number": "Core 110f",
            "power_draw_watts": 60,
            "heat_btu_hr": 205,
            "poe_class": 0,
            "dante_rx_channels": 16,
            "dante_tx_channels": 16,
            "ports": [
                {
                    "name": "mic_in_1",
                    "signal_format": "analog_audio",
                    "direction": "input",
                },
                {
                    "name": "dante_primary",
                    "signal_format": "ethernet_dante",
                    "direction": "bidirectional",
                    "dante_rx_channels": 16,
                    "dante_tx_channels": 16,
                },
            ],
        }
        device_cap, port_caps = _extract_capabilities_from_etim(etim)

        assert device_cap["manufacturer"] == "QSC"
        assert device_cap["model_number"] == "Core 110f"
        assert device_cap["power_draw_watts"] == 60
        assert device_cap["dante_rx_channels"] == 16
        assert len(port_caps) == 2
        assert port_caps[0]["port_name"] == "mic_in_1"
        assert port_caps[1]["signal_format"] == "ethernet_dante"

    @pytest.mark.asyncio
    async def test_sync_missing_namespace_id_raises(self) -> None:
        engine = _EngineStub()
        with pytest.raises(ValueError, match="namespace_id"):
            await do_sync_device_capabilities(
                engine,
                {"device_label": "device:dsp"},
            )

    @pytest.mark.asyncio
    async def test_sync_missing_device_label_raises(self) -> None:
        engine = _EngineStub()
        with pytest.raises(ValueError, match="device_label"):
            await do_sync_device_capabilities(
                engine,
                {"namespace_id": _TEST_NS},
            )

    @pytest.mark.asyncio
    async def test_sync_device_capabilities_from_catalog(self) -> None:
        engine = _EngineStub()
        product_id = str(uuid.uuid4())

        mock_catalog_row = {
            "id": uuid.UUID(product_id),
            "manufacturer": "Shure",
            "mfr_part_no": "MXA910",
            "etim_specs": {
                "power_draw_watts": 10,
                "poe_class": 3,
                "poe_watts": 15.4,
                "dante_tx_channels": 8,
                "ports": [
                    {
                        "name": "dante_out",
                        "signal_format": "dante",
                        "port_direction": "output",
                    }
                ],
            },
        }

        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = mock_catalog_row

        # Fake async context manager for scoped_pg_session
        class _FakeScoped:
            def __init__(self, pool: Any, ns: Any) -> None:
                pass

            async def __aenter__(self) -> AsyncMock:
                return mock_conn

            async def __aexit__(self, *args: Any) -> None:
                pass

        with (
            patch(
                "nce.vertical_modules.system_design.capability_sync.scoped_pg_session", _FakeScoped
            ),
            patch(
                "nce.vertical_modules.system_design.capability_sync._upsert_capability",
                new_callable=AsyncMock,
            ) as mock_upsert,
        ):
            res = await do_sync_device_capabilities(
                engine,
                {
                    "namespace_id": _TEST_NS,
                    "device_label": _TEST_DEVICE,
                    "product_id": product_id,
                },
            )

            assert res["status"] == "synced"
            assert res["device_label"] == _TEST_DEVICE
            assert res["manufacturer"] == "Shure"
            assert res["model_number"] == "MXA910"
            assert res["ports_synced_count"] == 1
            assert res["synced_port_labels"] == [f"{_TEST_DEVICE}:dante_out"]

            # Verify _upsert_capability was called twice (once for device, once for port)
            assert mock_upsert.call_count == 2
            # 1st call: device cap
            first_call_args = mock_upsert.call_args_list[0]
            assert first_call_args.args[2] == _TEST_DEVICE
            assert first_call_args.args[3]["manufacturer"] == "Shure"
            # 2nd call: port cap
            second_call_args = mock_upsert.call_args_list[1]
            assert second_call_args.args[2] == f"{_TEST_DEVICE}:dante_out"

    @pytest.mark.asyncio
    async def test_sync_device_capabilities_with_explicit_overrides(self) -> None:
        engine = _EngineStub()
        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = None

        class _FakeScoped:
            def __init__(self, pool: Any, ns: Any) -> None:
                pass

            async def __aenter__(self) -> AsyncMock:
                return mock_conn

            async def __aexit__(self, *args: Any) -> None:
                pass

        with (
            patch(
                "nce.vertical_modules.system_design.capability_sync.scoped_pg_session", _FakeScoped
            ),
            patch(
                "nce.vertical_modules.system_design.capability_sync._upsert_capability",
                new_callable=AsyncMock,
            ) as mock_upsert,
        ):
            res = await do_sync_device_capabilities(
                engine,
                {
                    "namespace_id": _TEST_NS,
                    "device_label": "device:custom_display",
                    "manufacturer": "Samsung",
                    "mfr_part_no": "QM55R",
                    "port_specs": [
                        {
                            "port_name": "hdmi_in_1",
                            "signal_format": "hdmi",
                            "signal_version": "2.0",
                            "port_direction": "input",
                        },
                        {
                            "port_name": "hdmi_in_2",
                            "signal_format": "hdmi",
                            "signal_version": "2.0",
                            "port_direction": "input",
                        },
                    ],
                },
            )

            assert res["status"] == "synced"
            assert res["ports_synced_count"] == 2
            assert mock_upsert.call_count == 3  # 1 device + 2 ports

    @pytest.mark.asyncio
    async def test_mcp_handler_sync_capabilities(self) -> None:
        engine = _EngineStub()
        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = None

        class _FakeScoped:
            def __init__(self, pool: Any, ns: Any) -> None:
                pass

            async def __aenter__(self) -> AsyncMock:
                return mock_conn

            async def __aexit__(self, *args: Any) -> None:
                pass

        with (
            patch(
                "nce.vertical_modules.system_design.capability_sync.scoped_pg_session", _FakeScoped
            ),
            patch(
                "nce.vertical_modules.system_design.capability_sync._upsert_capability",
                new_callable=AsyncMock,
            ),
        ):
            payload = await handle_system_design_sync_device_capabilities(
                engine,
                {
                    "namespace_id": _TEST_NS,
                    "device_label": "device:cam1",
                    "manufacturer": "Huddly",
                    "mfr_part_no": "IQ",
                },
            )
            data = json.loads(payload)
            assert data["status"] == "synced"

    @pytest.mark.asyncio
    async def test_mcp_handler_sync_missing_namespace(self) -> None:
        engine = _EngineStub()
        with pytest.raises(McpError) as exc_info:
            await handle_system_design_sync_device_capabilities(
                engine,
                {"device_label": "device:cam1"},
            )
        assert exc_info.value.code == -32602

    @pytest.mark.asyncio
    async def test_api_route_sync_device_capabilities_guards(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 1. 503 if engine is None
        monkeypatch.setattr(admin_state, "engine", None, raising=False)
        req = _StubRequest(body={"namespace_id": _TEST_NS, "device_label": _TEST_DEVICE})
        resp = await admin_routes.api_system_design_sync_device_capabilities(req)
        assert resp.status_code == 503

        # 2. 422 if namespace_id missing
        monkeypatch.setattr(admin_state, "engine", _EngineStub(), raising=False)
        req = _StubRequest(body={"device_label": _TEST_DEVICE})
        resp = await admin_routes.api_system_design_sync_device_capabilities(req)
        assert resp.status_code == 422

        # 3. 422 if device_label missing
        req = _StubRequest(body={"namespace_id": _TEST_NS})
        resp = await admin_routes.api_system_design_sync_device_capabilities(req)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_api_route_sync_device_capabilities_success_bumps_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(admin_state, "engine", _EngineStub(), raising=False)
        req = _StubRequest(body={"namespace_id": _TEST_NS, "device_label": _TEST_DEVICE})

        with (
            patch(
                "nce.admin_handlers.system_design.do_sync_device_capabilities",
                new_callable=AsyncMock,
            ) as mock_sync,
            patch(
                "nce.admin_handlers.system_design.bump_mcp_cache_generation", new_callable=AsyncMock
            ) as mock_bump,
        ):
            mock_sync.return_value = {"status": "synced", "device_label": _TEST_DEVICE}

            resp = await admin_routes.api_system_design_sync_device_capabilities(req)
            assert resp.status_code == 200
            data = json.loads(resp.body)
            assert data["status"] == "synced"
            mock_bump.assert_awaited_once_with(
                admin_state.engine,
                route="api_system_design_sync_device_capabilities",
            )


# ---------------------------------------------------------------------------
# 4. Tool Registry Parity Tests
# ---------------------------------------------------------------------------


def test_tool_registry_and_mcp_stdio_parity() -> None:
    expected_new_tools = {
        "system_design_get_standards",
        "system_design_get_signal_rules",
        "system_design_sync_device_capabilities",
    }

    # Verify presence in TOOL_REGISTRY
    for tool_name in expected_new_tools:
        assert tool_name in TOOL_REGISTRY, f"Tool {tool_name} missing from TOOL_REGISTRY"

    # Verify tool flags
    assert TOOL_REGISTRY["system_design_get_standards"].cacheable is True
    assert TOOL_REGISTRY["system_design_get_standards"].mutation is False

    assert TOOL_REGISTRY["system_design_get_signal_rules"].cacheable is True
    assert TOOL_REGISTRY["system_design_get_signal_rules"].mutation is False

    assert TOOL_REGISTRY["system_design_sync_device_capabilities"].cacheable is False
    assert TOOL_REGISTRY["system_design_sync_device_capabilities"].mutation is True

    # Verify presence in mcp_stdio_tools schemas
    stdio_names = {t.name for t in TOOLS}
    for tool_name in expected_new_tools:
        assert tool_name in stdio_names, f"Tool {tool_name} missing from mcp_stdio_tools"
