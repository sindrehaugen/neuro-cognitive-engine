"""tests/test_admin_plugins.py
===========================
Comprehensive test suite for NCE Plugin Console, Catalog API, Dual-Axis Filtering,
Runtime Redis Persistence Toggles, and XSS/CSP Hygiene.

Covers:
1. Authentication & Invalid Key Rejection (401 on unauthenticated or invalid key)
2. Pure Core Zero-Plugin Behavior (load_plugins() -> {})
3. Dual-Axis Filter Math & Cross-Faceted Badge Counts
4. Runtime Toggle Persistence, 409 Conflict for Uninstalled Extras, and 503 Fail-Closed
5. XSS & Air-Gapped CSP Hygiene on admin/plugins.html + Content Negotiation
6. Live Integration with nce_verticals (when installed)
"""

from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from nce.admin_app import app
from nce.admin_plugins import (
    _IN_MEMORY_DISABLED_ENGINES,
    build_plugins_catalog_payload,
)
from nce.config import cfg


@pytest.fixture(autouse=True)
def bypass_lifespan():
    """Bypass Starlette app lifespan to prevent real DB startup in tests."""

    @asynccontextmanager
    async def dummy_lifespan(application):
        yield

    original_lifespan = app.router.lifespan_context
    app.router.lifespan_context = dummy_lifespan
    _IN_MEMORY_DISABLED_ENGINES.clear()
    yield
    app.router.lifespan_context = original_lifespan
    _IN_MEMORY_DISABLED_ENGINES.clear()


class MockRedisClient:
    """In-memory async Redis mock for test isolation."""

    def __init__(self, initial_data: dict[str, str] | None = None) -> None:
        self.data: dict[str, str] = dict(initial_data or {})
        self.hkeys = AsyncMock(
            side_effect=lambda name: [
                k.encode() if isinstance(k, str) else k for k in self.data.keys()
            ]
        )
        self.hexists = AsyncMock(side_effect=lambda name, key: key in self.data)
        self.hset = AsyncMock(side_effect=self._hset)
        self.hdel = AsyncMock(side_effect=self._hdel)

    async def _hset(self, name: str, key: str, val: str) -> int:
        self.data[key] = str(val)
        return 1

    async def _hdel(self, name: str, key: str) -> int:
        if key in self.data:
            del self.data[key]
            return 1
        return 0


class DummyPlugin:
    """Mock plugin implementing NCEPlugin descriptor contract."""

    def __init__(self, plugin_id: str, descriptors: list[dict[str, Any]]) -> None:
        self.plugin_id = plugin_id
        self._descriptors = descriptors

    def get_plugin_descriptors(
        self,
        type_filter: str | None = None,
        category_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._descriptors

    def register_tools(self, registry: Any) -> None:
        pass

    def register_routes(self, app_inst: Any) -> None:
        pass

    def register_workers(self, worker_registry: Any) -> None:
        pass

    def register_cognitive_hooks(self, cognitive_engine: Any) -> None:
        pass

    def get_migration_dir(self) -> Path | None:
        return None


MOCK_ENGINES = [
    {
        "slug": "crm_core",
        "id": "crm_core",
        "plugin_id": "test_verticals",
        "name": "CRM Core Engine",
        "description": "Customer Relationship Management Engine",
        "version": "3.0.1",
        "types": ["erp-crm"],
        "categories": ["commercial"],
        "installed": True,
        "enabled_by_env": True,
        "enabled": True,
        "tool_count": 10,
        "route_count": 8,
        "requires_schemas": [],
        "install_extra": "nce-vertical-engines[crm_core]",
    },
    {
        "slug": "iot_sensors",
        "id": "iot_sensors",
        "plugin_id": "test_verticals",
        "name": "IoT Sensors Engine",
        "description": "Infrastructure & Telemetry Engine",
        "version": "3.0.1",
        "types": ["infra-iot"],
        "categories": ["av-it-integrator"],
        "installed": True,
        "enabled_by_env": True,
        "enabled": True,
        "tool_count": 12,
        "route_count": 14,
        "requires_schemas": [],
        "install_extra": "nce-vertical-engines[iot_sensors]",
    },
    {
        "slug": "finance_bridge",
        "id": "finance_bridge",
        "plugin_id": "test_verticals",
        "name": "Finance Bridge Engine",
        "description": "Dual type enterprise engine",
        "version": "3.0.1",
        "types": ["erp-crm", "infra-iot"],
        "categories": ["finance-erp"],
        "installed": True,
        "enabled_by_env": True,
        "enabled": True,
        "tool_count": 15,
        "route_count": 18,
        "requires_schemas": ["crm_core"],
        "install_extra": "nce-vertical-engines[finance_bridge]",
    },
    {
        "slug": "av_controller",
        "id": "av_controller",
        "plugin_id": "test_verticals",
        "name": "AV Controller Engine",
        "description": "AV Integration and Field Operations",
        "version": "3.0.1",
        "types": ["infra-iot"],
        "categories": ["av-it-integrator", "commercial"],
        "installed": True,
        "enabled_by_env": True,
        "enabled": True,
        "tool_count": 8,
        "route_count": 6,
        "requires_schemas": [],
        "install_extra": "nce-vertical-engines[av_controller]",
    },
    {
        "slug": "uninstalled_extra",
        "id": "uninstalled_extra",
        "plugin_id": "test_verticals",
        "name": "Uninstalled Extra Engine",
        "description": "Engine not installed locally",
        "version": "3.0.1",
        "types": ["observability"],
        "categories": ["commercial"],
        "installed": False,
        "enabled_by_env": False,
        "enabled": False,
        "tool_count": 5,
        "route_count": 4,
        "requires_schemas": [],
        "install_extra": "nce-vertical-engines[uninstalled_extra]",
    },
]


# ===========================================================================
# Area 1: Authentication & Invalid Key Rejection
# ===========================================================================


def test_admin_plugins_auth_rejection_when_unauthenticated():
    """Unauthenticated requests when NCE_ADMIN_OVERRIDE=false return 401 on all endpoints."""
    mock_engine = MagicMock()
    mock_engine.redis_client = MockRedisClient()

    with (
        patch.object(cfg, "NCE_ADMIN_OVERRIDE", False),
        patch.dict(
            os.environ, {"NCE_ADMIN_OVERRIDE": "false", "NCE_ADMIN_API_KEY": "secret-admin-key"}
        ),
        patch("nce.admin_state.engine", mock_engine),
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            # 1. GET /admin/plugins
            r1 = client.get("/admin/plugins")
            assert r1.status_code == 401

            # 2. GET /admin/plugins/api
            r2 = client.get("/admin/plugins/api")
            assert r2.status_code == 401

            # 3. POST /admin/plugins/api/toggle
            r3 = client.post(
                "/admin/plugins/api/toggle",
                json={"slug": "crm_core", "enabled": False},
            )
            assert r3.status_code == 401


def test_admin_plugins_auth_rejection_with_invalid_credentials():
    """Invalid X-Admin-Api-Key or Bearer token returns 401 on all endpoints."""
    mock_engine = MagicMock()
    mock_engine.redis_client = MockRedisClient()

    headers_bad_key = {"X-Admin-Api-Key": "wrong-api-key"}
    headers_bad_bearer = {"Authorization": "Bearer wrong-bearer-token"}

    with (
        patch.object(cfg, "NCE_ADMIN_OVERRIDE", True),
        patch.dict(os.environ, {"NCE_ADMIN_API_KEY": "valid-secret-key"}),
        patch("nce.admin_state.engine", mock_engine),
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            for headers in (headers_bad_key, headers_bad_bearer):
                assert client.get("/admin/plugins", headers=headers).status_code == 401
                assert client.get("/admin/plugins/api", headers=headers).status_code == 401
                assert (
                    client.post(
                        "/admin/plugins/api/toggle",
                        json={"slug": "crm_core", "enabled": False},
                        headers=headers,
                    ).status_code
                    == 401
                )


def test_admin_plugins_auth_success_with_valid_key():
    """Valid X-Admin-Api-Key or Bearer token succeeds with 200."""
    mock_engine = MagicMock()
    mock_engine.redis_client = MockRedisClient()
    mock_plugin = DummyPlugin("test_verticals", MOCK_ENGINES)

    headers = {"X-Admin-Api-Key": "valid-secret-key"}

    with (
        patch.object(cfg, "NCE_ADMIN_OVERRIDE", True),
        patch.dict(os.environ, {"NCE_ADMIN_API_KEY": "valid-secret-key"}),
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.plugins.load_plugins", return_value={"test_verticals": mock_plugin}),
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/admin/plugins/api", headers=headers)
            assert resp.status_code == 200
            data = resp.json()
            assert data["summary"]["catalog"] == 5


# ===========================================================================
# Area 2: Pure Core Zero-Plugin Behavior
# ===========================================================================


def test_admin_plugins_pure_core_zero_plugins():
    """When load_plugins() returns {} (zero plugins), /admin/plugins/api returns clean empty structure."""
    mock_engine = MagicMock()
    mock_engine.redis_client = MockRedisClient()
    headers = {"X-Admin-Api-Key": "test-admin-key"}

    with (
        patch.object(cfg, "NCE_ADMIN_OVERRIDE", True),
        patch.dict(os.environ, {"NCE_ADMIN_API_KEY": "test-admin-key"}),
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.plugins.load_plugins", return_value={}),
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/admin/plugins/api", headers=headers)
            assert resp.status_code == 200
            data = resp.json()

            assert data["summary"]["catalog"] == 0
            assert data["summary"]["installed"] == 0
            assert data["summary"]["active"] == 0
            assert data["summary"]["matched"] == 0
            assert data["engines"] == []
            assert data["plugins"] == []
            assert data["facets"] == {"types": [], "categories": []}
            assert data["total"] == 0


# ===========================================================================
# Area 3: Dual-Axis Filter Math & Cross-Faceted Counts
# ===========================================================================


def test_admin_plugins_dual_axis_filter_math_and_cross_facets():
    """Test single type, multi-type OR, multi-category OR, type+cat AND, cross-facets, and snippets."""
    mock_engine = MagicMock()
    mock_engine.redis_client = MockRedisClient()
    mock_plugin = DummyPlugin("test_verticals", MOCK_ENGINES)
    headers = {"X-Admin-Api-Key": "test-admin-key"}

    with (
        patch.object(cfg, "NCE_ADMIN_OVERRIDE", True),
        patch.dict(os.environ, {"NCE_ADMIN_API_KEY": "test-admin-key"}),
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.plugins.load_plugins", return_value={"test_verticals": mock_plugin}),
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            # 1. Single Type filter (?type=erp-crm)
            r_type = client.get("/admin/plugins/api?type=erp-crm", headers=headers)
            assert r_type.status_code == 200
            d_type = r_type.json()
            matched_slugs = {e["slug"] for e in d_type["engines"]}
            assert matched_slugs == {"crm_core", "finance_bridge"}

            # 2. Multi-Type OR filter (?type=erp-crm,infra-iot)
            r_multi_t = client.get("/admin/plugins/api?type=erp-crm,infra-iot", headers=headers)
            assert r_multi_t.status_code == 200
            d_multi_t = r_multi_t.json()
            matched_slugs = {e["slug"] for e in d_multi_t["engines"]}
            assert matched_slugs == {"crm_core", "iot_sensors", "finance_bridge", "av_controller"}

            # 3. Multi-Category OR filter (?category=commercial,finance-erp)
            r_multi_c = client.get(
                "/admin/plugins/api?category=commercial,finance-erp", headers=headers
            )
            assert r_multi_c.status_code == 200
            d_multi_c = r_multi_c.json()
            matched_slugs = {e["slug"] for e in d_multi_c["engines"]}
            assert matched_slugs == {
                "crm_core",
                "finance_bridge",
                "av_controller",
                "uninstalled_extra",
            }

            # 4. Combined Type + Category AND intersection (?type=infra-iot&category=av-it-integrator)
            r_and = client.get(
                "/admin/plugins/api?type=infra-iot&category=av-it-integrator", headers=headers
            )
            assert r_and.status_code == 200
            d_and = r_and.json()
            matched_slugs = {e["slug"] for e in d_and["engines"]}
            assert matched_slugs == {"iot_sensors", "av_controller"}

            # Check cross-faceted badge counts on combined filter:
            # facets.types counts should reflect only engines matching category=av-it-integrator
            # (which are iot_sensors [infra-iot] and av_controller [infra-iot])
            type_facets = {f["name"]: f["count"] for f in d_and["facets"]["types"]}
            assert type_facets.get("infra-iot") == 2
            assert type_facets.get("erp-crm", 0) == 0

            # facets.categories counts should reflect only engines matching type=infra-iot
            # (which are iot_sensors [av-it-integrator], finance_bridge [finance-erp], av_controller [av-it-integrator, commercial])
            cat_facets = {f["name"]: f["count"] for f in d_and["facets"]["categories"]}
            assert cat_facets.get("av-it-integrator") == 2
            assert cat_facets.get("commercial") == 1
            assert cat_facets.get("finance-erp") == 1

            # Check dynamic snippets
            assert "pip install" in d_and["snippets"]["pip_install"]
            assert "NCE_ENGINE_TYPE_FILTER=infra-iot" in d_and["snippets"]["env_vars"]
            assert "NCE_ENGINE_CATEGORY_FILTER=av-it-integrator" in d_and["snippets"]["env_vars"]


def test_build_plugins_catalog_payload_direct_unit():
    """Unit test build_plugins_catalog_payload directly with mock descriptors."""
    payload = build_plugins_catalog_payload(
        MOCK_ENGINES,
        disabled_slugs={"crm_core"},
        type_param="infra-iot",
        category_param="commercial",
    )
    assert payload["summary"]["catalog"] == 5
    assert payload["summary"]["installed"] == 4
    # crm_core is disabled, so active count is 3 (iot_sensors, finance_bridge, av_controller)
    assert payload["summary"]["active"] == 3
    # Matched engines for type=infra-iot AND category=commercial is only av_controller
    assert [e["slug"] for e in payload["engines"]] == ["av_controller"]


# ===========================================================================
# Area 4: Runtime Toggle Persistence, 409 Conflict, and 503 Fail-Closed
# ===========================================================================


def test_admin_plugins_toggle_installed_engine_persists_in_redis():
    """Toggling an installed engine disables it in Redis and reflects in catalog API."""
    mock_redis = MockRedisClient()
    mock_engine = MagicMock()
    mock_engine.redis_client = mock_redis
    mock_plugin = DummyPlugin("test_verticals", MOCK_ENGINES)
    headers = {"X-Admin-Api-Key": "test-admin-key"}

    with (
        patch.object(cfg, "NCE_ADMIN_OVERRIDE", True),
        patch.dict(os.environ, {"NCE_ADMIN_API_KEY": "test-admin-key"}),
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.plugins.load_plugins", return_value={"test_verticals": mock_plugin}),
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            # 1. Disable crm_core
            resp_toggle = client.post(
                "/admin/plugins/api/toggle",
                json={"slug": "crm_core", "enabled": False},
                headers=headers,
            )
            assert resp_toggle.status_code == 200
            toggle_data = resp_toggle.json()
            assert toggle_data["status"] == "ok"
            assert toggle_data["slug"] == "crm_core"
            assert toggle_data["enabled"] is False

            # Verify persisted in Redis
            assert mock_redis.data.get("crm_core") == "1"

            # 2. Verify GET /admin/plugins/api reflects disabled state and reduced active count
            resp_cat = client.get("/admin/plugins/api", headers=headers)
            assert resp_cat.status_code == 200
            cat_data = resp_cat.json()
            crm = next(e for e in cat_data["engines"] if e["slug"] == "crm_core")
            assert crm["enabled"] is False
            assert crm["runtime_disabled"] is True
            assert cat_data["summary"]["active"] == 3  # was 4 installed & enabled, now 3

            # 3. Re-enable crm_core
            resp_re_enable = client.post(
                "/admin/plugins/api/toggle",
                json={"slug": "crm_core", "enabled": True},
                headers=headers,
            )
            assert resp_re_enable.status_code == 200
            assert "crm_core" not in mock_redis.data


def test_admin_plugins_toggle_uninstalled_extra_returns_409():
    """Attempting to enable an uninstalled engine extra returns 409 Conflict with install hint."""
    mock_redis = MockRedisClient()
    mock_engine = MagicMock()
    mock_engine.redis_client = mock_redis
    mock_plugin = DummyPlugin("test_verticals", MOCK_ENGINES)
    headers = {"X-Admin-Api-Key": "test-admin-key"}

    with (
        patch.object(cfg, "NCE_ADMIN_OVERRIDE", True),
        patch.dict(os.environ, {"NCE_ADMIN_API_KEY": "test-admin-key"}),
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.plugins.load_plugins", return_value={"test_verticals": mock_plugin}),
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/admin/plugins/api/toggle",
                json={"slug": "uninstalled_extra", "enabled": True},
                headers=headers,
            )
            assert resp.status_code == 409
            data = resp.json()
            assert data["installed"] is False
            assert "pip install" in data["install_hint"]
            assert "nce-vertical-engines[uninstalled_extra]" in data["install_hint"]


def test_admin_plugins_toggle_unknown_slug_returns_404():
    """Attempting to toggle a non-existent engine slug returns 404."""
    mock_redis = MockRedisClient()
    mock_engine = MagicMock()
    mock_engine.redis_client = mock_redis
    mock_plugin = DummyPlugin("test_verticals", MOCK_ENGINES)
    headers = {"X-Admin-Api-Key": "test-admin-key"}

    with (
        patch.object(cfg, "NCE_ADMIN_OVERRIDE", True),
        patch.dict(os.environ, {"NCE_ADMIN_API_KEY": "test-admin-key"}),
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.plugins.load_plugins", return_value={"test_verticals": mock_plugin}),
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/admin/plugins/api/toggle",
                json={"slug": "non_existent_engine_xyz", "enabled": True},
                headers=headers,
            )
            assert resp.status_code == 404
            assert "Unknown engine" in resp.json()["error"]


def test_admin_plugins_toggle_production_fail_closed_503():
    """In production, if Redis is None or raises an error, POST /admin/plugins/api/toggle returns 503."""
    mock_engine_no_redis = MagicMock()
    mock_engine_no_redis.redis_client = None
    mock_plugin = DummyPlugin("test_verticals", MOCK_ENGINES)
    headers = {"X-Admin-Api-Key": "test-admin-key"}

    with (
        patch.object(cfg, "NCE_ADMIN_OVERRIDE", True),
        patch.object(cfg, "IS_PROD", True),
        patch.dict(os.environ, {"NCE_ENV": "production", "NCE_ADMIN_API_KEY": "test-admin-key"}),
        patch("nce.admin_state.engine", mock_engine_no_redis),
        patch("nce.plugins.load_plugins", return_value={"test_verticals": mock_plugin}),
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/admin/plugins/api/toggle",
                json={"slug": "crm_core", "enabled": False},
                headers=headers,
            )
            assert resp.status_code == 503
            assert "Redis unavailable in production" in resp.json()["error"]

    # Also test when Redis raises an exception in production
    broken_redis = MockRedisClient()
    broken_redis.hset.side_effect = ConnectionError("Redis cluster unreachable")
    mock_engine_broken_redis = MagicMock()
    mock_engine_broken_redis.redis_client = broken_redis

    with (
        patch.object(cfg, "NCE_ADMIN_OVERRIDE", True),
        patch.object(cfg, "IS_PROD", True),
        patch.dict(os.environ, {"NCE_ENV": "production", "NCE_ADMIN_API_KEY": "test-admin-key"}),
        patch("nce.admin_state.engine", mock_engine_broken_redis),
        patch("nce.plugins.load_plugins", return_value={"test_verticals": mock_plugin}),
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/admin/plugins/api/toggle",
                json={"slug": "crm_core", "enabled": False},
                headers=headers,
            )
            assert resp.status_code == 503
            assert "Redis unavailable in production" in resp.json()["error"]


# ===========================================================================
# Area 5: XSS & Air-Gapped CSP Hygiene Check on admin/plugins.html
# ===========================================================================


def test_admin_plugins_html_hygiene():
    """Verify admin/plugins.html has zero external URLs and zero innerHTML usage."""
    html_path = Path(__file__).resolve().parent.parent / "admin" / "plugins.html"
    assert html_path.is_file(), f"Expected plugins.html to exist at {html_path}"

    content = html_path.read_text(encoding="utf-8")

    # 1. Zero external http:// or https:// URLs
    external_urls = re.findall(r"https?://[^\s\"'<>]+", content)
    assert external_urls == [], (
        f"Found forbidden external URLs in admin/plugins.html: {external_urls}"
    )

    # 2. Zero innerHTML usage (must use textContent or safe DOM nodes)
    inner_html_usages = re.findall(r"\.innerHTML\b", content)
    assert inner_html_usages == [], (
        f"Found forbidden innerHTML usages in admin/plugins.html: {len(inner_html_usages)}"
    )


def test_admin_plugins_console_content_negotiation():
    """GET /admin/plugins returns text/html by default, application/json on Accept: application/json."""
    mock_engine = MagicMock()
    mock_engine.redis_client = MockRedisClient()
    mock_plugin = DummyPlugin("test_verticals", MOCK_ENGINES)
    headers = {"X-Admin-Api-Key": "test-admin-key"}

    with (
        patch.object(cfg, "NCE_ADMIN_OVERRIDE", True),
        patch.dict(os.environ, {"NCE_ADMIN_API_KEY": "test-admin-key"}),
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.plugins.load_plugins", return_value={"test_verticals": mock_plugin}),
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            # 1. Default request returns HTML
            r_html = client.get("/admin/plugins", headers=headers)
            assert r_html.status_code == 200
            assert "text/html" in r_html.headers.get("content-type", "")
            assert "<!DOCTYPE html>" in r_html.text

            # 2. Accept: application/json returns JSON
            headers_json = {**headers, "Accept": "application/json"}
            r_json = client.get("/admin/plugins", headers=headers_json)
            assert r_json.status_code == 200
            assert "application/json" in r_json.headers.get("content-type", "")
            data = r_json.json()
            assert "summary" in data
            assert data["summary"]["catalog"] == 5


# ===========================================================================
# Area 6: Dynamic Integration Test with Installed nce.plugins (Domain-Agnostic)
# ===========================================================================


def test_live_installed_plugin_descriptors_contract():
    """Dynamically verify that any installed nce.plugins package satisfies EngineDescriptor."""
    from nce.plugins import list_plugin_engine_descriptors, load_plugins

    plugins = load_plugins(force_reload=True)
    descriptors = list_plugin_engine_descriptors()
    if not plugins:
        assert descriptors == []
        return

    assert len(descriptors) > 0
    for d in descriptors:
        assert isinstance(d["slug"], str) and d["slug"]
        assert isinstance(d["id"], str) and d["id"]
        assert isinstance(d["plugin_id"], str) and d["plugin_id"]
        assert isinstance(d["name"], str) and d["name"]
        assert isinstance(d["description"], str)
        assert isinstance(d["types"], list)
        assert isinstance(d["categories"], list)
        assert isinstance(d["requires_schemas"], list)
        assert isinstance(d["installed"], bool)
        assert isinstance(d["enabled"], bool)
        assert isinstance(d["enabled_by_env"], bool)
        assert isinstance(d["tool_count"], int) and d["tool_count"] >= 0
        assert isinstance(d["route_count"], int) and d["route_count"] >= 0
        assert isinstance(d["install_extra"], str) and d["install_extra"]
