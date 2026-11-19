"""Tests for v3.6.0 Admin Beta UI, Connection Vault API, Connected-First Discovery, and Docs."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib.util
import json
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import nce
from nce import mcp_http_gateway
from nce.admin_app import app as real_admin_app
from nce.admin_app import build_admin_middleware, build_app
from nce.admin_plugins import (
    _IN_MEMORY_DISABLED_ENGINES,
    register_admin_plugin_routes,
)
from nce.auth import NonceStore
from nce.config import cfg
from nce.connectors.vault import TenantConnectionVault
from nce.mcp_http_gateway import (
    MCPGatewaySession,
    create_mcp_gateway_app,
    discover_tools_handler,
    register_test_mcp_client_token,
)
from nce.tool_registry import TOOL_REGISTRY, ToolSpec

REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_hmac_headers(
    key: str,
    method: str,
    path: str,
    body: bytes = b"",
    *,
    nonce: str | None = None,
) -> dict[str, str]:
    ts = int(time.time())
    nonce_val = nonce or f"nonce-{uuid.uuid4().hex}"
    parts = [method.upper(), path, str(ts), nonce_val]
    if body:
        parts.append(hashlib.sha256(body).hexdigest())
    sig = hmac.new(
        key.encode("utf-8"), "\n".join(parts).encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return {
        "X-NCE-Timestamp": str(ts),
        "X-NCE-Nonce": nonce_val,
        "Authorization": f"HMAC-SHA256 {sig}",
    }


@pytest.fixture(autouse=True)
def _isolate_admin_lifespan():
    @asynccontextmanager
    async def _noop_lifespan(_app):
        yield

    orig = real_admin_app.router.lifespan_context
    real_admin_app.router.lifespan_context = _noop_lifespan
    _IN_MEMORY_DISABLED_ENGINES.clear()
    yield
    real_admin_app.router.lifespan_context = orig
    _IN_MEMORY_DISABLED_ENGINES.clear()


class _DummyPlugin:
    def __init__(self, plugin_id: str, descriptors: list[dict[str, Any]]) -> None:
        self.plugin_id = plugin_id
        self._descriptors = descriptors

    def get_plugin_descriptors(self) -> list[dict[str, Any]]:
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


_STAGE_ENGINES: list[dict[str, Any]] = [
    {
        "slug": "sales_core",
        "id": "sales_core",
        "plugin_id": "nce_verticals",
        "name": "Sales Core",
        "description": "Stable vertical engine",
        "types": ["erp-crm"],
        "categories": ["commercial"],
        "installed": True,
        "enabled": True,
        "maturity": "stable",
        "vendor_specific": False,
        "tool_count": 4,
        "route_count": 2,
    },
    {
        "slug": "kicad",
        "id": "kicad",
        "plugin_id": "nce_apps",
        "name": "KiCad Connector",
        "description": "Beta CAD app connector",
        "types": ["app-connector"],
        "categories": ["operations-delivery"],
        "installed": True,
        "enabled": True,
        "maturity": "beta",
        "vendor_specific": True,
        "tool_count": 5,
        "route_count": 1,
    },
    {
        "slug": "experimental_lab",
        "id": "experimental_lab",
        "plugin_id": "nce_apps",
        "name": "Experimental Lab",
        "description": "Experimental preview connector",
        "types": ["app-connector"],
        "categories": ["commercial"],
        "installed": True,
        "enabled": True,
        "maturity": "experimental",
        "vendor_specific": True,
        "tool_count": 2,
        "route_count": 0,
    },
    {
        "slug": "legacy_untagged",
        "id": "legacy_untagged",
        "plugin_id": "nce_verticals",
        "name": "Legacy Untagged",
        "description": "No explicit maturity field",
        "types": ["erp-crm"],
        "categories": ["commercial"],
        "installed": True,
        "enabled": True,
        "tool_count": 1,
        "route_count": 0,
    },
]


def test_admin_plugins_api_maturity_fields_and_filtering() -> None:
    mock_engine = MagicMock()
    mock_engine.redis_client = None
    plugin = _DummyPlugin("nce_mix", _STAGE_ENGINES)
    headers = {"X-Admin-Api-Key": "v360-admin-key"}

    with (
        patch.object(cfg, "NCE_ADMIN_OVERRIDE", True),
        patch.dict(os.environ, {"NCE_ADMIN_API_KEY": "v360-admin-key"}),
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.plugins.load_plugins", return_value={"nce_mix": plugin}),
    ):
        with TestClient(real_admin_app, raise_server_exceptions=False) as client:
            r_all = client.get("/admin/plugins/api", headers=headers)
            assert r_all.status_code == 200
            d_all = r_all.json()
            assert d_all["summary"]["maturity"] == {"stable": 2, "beta": 1, "experimental": 1}
            assert d_all["maturity_counts"] == {"stable": 2, "beta": 1, "experimental": 1}
            by_slug = {e["slug"]: e for e in d_all["engines"]}
            assert by_slug["sales_core"]["maturity"] == "stable"
            assert by_slug["sales_core"]["vendor_specific"] is False
            assert by_slug["kicad"]["maturity"] == "beta"
            assert by_slug["kicad"]["vendor_specific"] is True
            assert by_slug["experimental_lab"]["maturity"] == "experimental"
            assert by_slug["legacy_untagged"]["maturity"] == "stable"
            assert by_slug["legacy_untagged"]["vendor_specific"] is False

            # Filter ?maturity=beta on /api/admin/plugins (HMAC middleware protected)
            hmac_key = cfg.NCE_API_KEY or "v360-hmac-key"
            with (
                patch.object(cfg, "NCE_API_KEY", hmac_key),
                patch("nce.admin_app._hmac_nonce_store", None),
            ):
                # Also verify direct /admin/plugins/api?maturity=beta and /api/admin/plugins?maturity=beta
                r_beta_ui = client.get("/admin/plugins/api?maturity=beta", headers=headers)
                assert r_beta_ui.status_code == 200
                assert [e["slug"] for e in r_beta_ui.json()["engines"]] == ["kicad"]

                # Standalone app with build_admin_middleware using patched cfg.NCE_API_KEY
                api_app = Starlette(
                    routes=register_admin_plugin_routes(),
                    middleware=build_admin_middleware(),
                )
                with TestClient(api_app, raise_server_exceptions=False) as api_client:
                    r_beta = api_client.get(
                        "/api/admin/plugins?maturity=beta",
                        headers=_make_hmac_headers(hmac_key, "GET", "/api/admin/plugins"),
                    )
                    assert r_beta.status_code == 200
                    assert [e["slug"] for e in r_beta.json()["engines"]] == ["kicad"]

            # Case-insensitive comma-separated filter ?maturity=BETA,EXPERIMENTAL
            r_multi = client.get(
                "/admin/plugins/api?maturity=%20BETA%20,%20EXPERIMENTAL%20",
                headers=headers,
            )
            assert r_multi.status_code == 200
            assert {e["slug"] for e in r_multi.json()["engines"]} == {
                "kicad",
                "experimental_lab",
            }

            # Combined maturity + type + category cross-faceting
            r_combo = client.get(
                "/admin/plugins/api?maturity=beta,experimental&category=commercial",
                headers=headers,
            )
            assert r_combo.status_code == 200
            d_combo = r_combo.json()
            assert [e["slug"] for e in d_combo["engines"]] == ["experimental_lab"]
            type_counts = {f["name"]: f["count"] for f in d_combo["facets"]["types"]}
            assert type_counts["app-connector"] == 1


def test_admin_plugins_html_beta_badges_stage_filter_and_vault_ui() -> None:
    html_path = REPO_ROOT / "admin" / "plugins.html"
    content = html_path.read_text(encoding="utf-8")

    # Zero external URLs and zero innerHTML
    assert re.findall(r"https?://[^\s\"'<>]+", content) == []
    assert re.findall(r"\.innerHTML\b", content) == []

    # Air-gapped inline CSS pill badges with amber/orange, emerald, purple
    assert ".badge-maturity-beta" in content
    assert "#f59e0b" in content
    assert "#ea580c" in content
    assert ".badge-maturity-stable" in content
    assert ".badge-maturity-experimental" in content

    # Stage filter control and Connections drawer modal wired to /admin/plugins/api/connections
    assert 'id="maturity-segmented"' in content
    assert 'data-maturity="all"' in content
    assert 'data-maturity="stable"' in content
    assert 'data-maturity="beta"' in content
    assert 'id="connections-modal"' in content
    assert "/admin/plugins/api/connections" in content


def test_admin_plugins_connections_vault_crud_auth_and_zero_secret_leak() -> None:
    ns_a = str(uuid.uuid4())
    ns_b = str(uuid.uuid4())
    headers = {"X-Admin-Api-Key": "vault-admin-key"}
    hmac_key = "vault-hmac-key"

    @asynccontextmanager
    async def _noop_lifespan(_app):
        yield

    # 1. Uninitialized pool/vault returns explicit 503 error
    with patch.object(cfg, "NCE_API_KEY", hmac_key):
        uninit_app = build_app(extra_routes=register_admin_plugin_routes())
    uninit_app.router.lifespan_context = _noop_lifespan
    with (
        patch.object(cfg, "NCE_ADMIN_OVERRIDE", False),
        patch.object(cfg, "NCE_API_KEY", hmac_key),
        patch.dict(os.environ, {"NCE_ADMIN_API_KEY": "vault-admin-key"}),
        patch("nce.admin_state.engine", None),
    ):
        with TestClient(uninit_app, raise_server_exceptions=False) as client:
            assert client.get("/admin/plugins/api/connections").status_code == 401
            assert (
                client.get(
                    "/admin/plugins/api/connections",
                    headers={"X-Admin-Api-Key": "wrong"},
                ).status_code
                == 401
            )
            r_503 = client.get(
                f"/admin/plugins/api/connections?namespace_id={ns_a}",
                headers=headers,
            )
            assert r_503.status_code == 503
            assert "unavailable" in r_503.json()["error"].lower()

    # 2. Injected vault roundtrip CRUD via /admin/plugins/api/connections and /api/admin/plugins/connections
    vault = TenantConnectionVault(pool=None)
    ctx = SimpleNamespace(connection_vault=vault, redis_client=None)
    with patch.object(cfg, "NCE_API_KEY", hmac_key):
        vault_app = build_app(extra_routes=register_admin_plugin_routes(ctx=ctx))
    vault_app.router.lifespan_context = _noop_lifespan

    with (
        patch.object(cfg, "NCE_ADMIN_OVERRIDE", False),
        patch.object(cfg, "NCE_API_KEY", hmac_key),
        patch("nce.admin_app._hmac_nonce_store", None),
        patch.dict(os.environ, {"NCE_ADMIN_API_KEY": "vault-admin-key"}),
        patch("nce.admin_state.engine", None),
    ):
        with TestClient(vault_app, raise_server_exceptions=False) as client:
            # SSRF blocked endpoint_url rejected with 400
            r_ssrf = client.post(
                "/admin/plugins/api/connections",
                headers=headers,
                json={
                    "namespace_id": ns_a,
                    "app_slug": "kicad",
                    "auth_kind": "api_key",
                    "endpoint_url": "http://169.254.169.254/latest/meta-data/",
                    "secret_config": {"api_key": "secret-val"},
                },
            )
            assert r_ssrf.status_code == 400

            # Valid POST upsert (using public IP literal to stay offline-DNS deterministic)
            r_post = client.post(
                "/admin/plugins/api/connections",
                headers=headers,
                json={
                    "namespace_id": ns_a,
                    "app_slug": "kicad",
                    "connection_name": "primary",
                    "auth_kind": "api_key",
                    "endpoint_url": "https://93.184.216.34/v1/kicad",
                    "scopes": ["projects.read", "schematics.write"],
                    "metadata": {"region": "eu-north", "client_secret": "should-strip"},
                    "secret_config": {"api_key": "top-secret-kicad-999"},
                },
            )
            assert r_post.status_code == 201
            post_body = r_post.text
            assert "top-secret-kicad-999" not in post_body
            assert "should-strip" not in post_body
            assert "encrypted_config" not in post_body
            conn_obj = r_post.json()["connection"]
            assert conn_obj["app_slug"] == "kicad"
            assert conn_obj["connection_name"] == "primary"
            assert conn_obj["credentials_masked"] == "••••••••"
            assert conn_obj["has_credentials"] is True
            assert conn_obj["metadata"] == {"region": "eu-north"}
            conn_id = conn_obj["connection_id"]

            # GET list via HMAC-protected /api/admin/plugins/connections returns metadata only, zero secrets
            r_get = client.get(
                f"/api/admin/plugins/connections?namespace_id={ns_a}",
                headers=_make_hmac_headers(hmac_key, "GET", "/api/admin/plugins/connections"),
            )
            assert r_get.status_code == 200
            assert "top-secret-kicad-999" not in r_get.text
            assert "encrypted_config" not in r_get.text
            get_data = r_get.json()
            assert get_data["total"] == 1
            assert get_data["connections"][0]["connection_id"] == conn_id

            # Tenant isolation on GET & DELETE
            r_other = client.get(
                f"/admin/plugins/api/connections?namespace_id={ns_b}",
                headers=headers,
            )
            assert r_other.status_code == 200
            assert r_other.json()["total"] == 0

            r_cross_del = client.delete(
                f"/admin/plugins/api/connections?namespace_id={ns_b}&connection_id={conn_id}",
                headers=headers,
            )
            assert r_cross_del.status_code == 404

            # Authorized DELETE via /api/admin/plugins/connections removes connection
            r_del = client.delete(
                f"/api/admin/plugins/connections?namespace_id={ns_a}&connection_id={conn_id}",
                headers=_make_hmac_headers(hmac_key, "DELETE", "/api/admin/plugins/connections"),
            )
            assert r_del.status_code == 200
            assert r_del.json()["deleted"] is True

            r_after = client.get(
                f"/admin/plugins/api/connections?namespace_id={ns_a}",
                headers=headers,
            )
            assert r_after.status_code == 200
            assert r_after.json()["total"] == 0

    # 3. Pool resolution from admin_state.engine.pg_pool cleanly constructs TenantConnectionVault(pool=pg_pool)
    fake_pool = object()
    mock_engine = SimpleNamespace(pg_pool=fake_pool)
    mock_vault_inst = MagicMock()
    mock_vault_inst.list_connections = AsyncMock(return_value=[])
    pool_app = Starlette(routes=register_admin_plugin_routes())

    with (
        patch.object(cfg, "NCE_ADMIN_OVERRIDE", False),
        patch.dict(os.environ, {"NCE_ADMIN_API_KEY": "vault-admin-key"}),
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.admin_plugins.TenantConnectionVault", return_value=mock_vault_inst) as mock_cls,
    ):
        with TestClient(pool_app, raise_server_exceptions=False) as client:
            r_pool = client.get(
                f"/admin/plugins/api/connections?namespace_id={ns_a}",
                headers=headers,
            )
            assert r_pool.status_code == 200
            mock_cls.assert_called_once_with(pool=fake_pool)


@pytest.mark.asyncio
async def test_admin_plugins_connections_hmac_nonce_replay_protection() -> None:
    seen: set[str] = set()
    store = MagicMock(spec=NonceStore)
    store.ping = AsyncMock(return_value=True)

    async def _check_and_store(val: str) -> bool:
        if val in seen:
            return False
        seen.add(val)
        return True

    store.check_and_store = AsyncMock(side_effect=_check_and_store)

    def _make_hmac_headers(method: str, path: str, nonce: str) -> dict[str, str]:
        ts = int(time.time())
        sig = hmac.new(
            b"hmac-vault-secret",
            f"{method}\n{path}\n{ts}\n{nonce}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return {
            "X-NCE-Timestamp": str(ts),
            "X-NCE-Nonce": nonce,
            "Authorization": f"HMAC-SHA256 {sig}",
        }

    vault = TenantConnectionVault(pool=None)
    app = Starlette(
        routes=register_admin_plugin_routes(ctx=SimpleNamespace(connection_vault=vault))
    )

    with (
        patch.object(cfg, "NCE_API_KEY", "hmac-vault-secret"),
        patch.object(cfg, "NCE_ADMIN_OVERRIDE", False),
        patch("nce.admin_app._hmac_nonce_store", store),
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            hdrs = _make_hmac_headers(
                "GET", "/admin/plugins/api/connections", "nonce-vault-00000001"
            )
            r1 = client.get("/admin/plugins/api/connections", headers=hdrs)
            assert r1.status_code == 200
            r_replay = client.get("/admin/plugins/api/connections", headers=hdrs)
            assert r_replay.status_code == 401


@pytest.mark.asyncio
async def test_discover_tools_connected_first_boost_and_12_tool_compact_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Register 16 synthetic app-connector tools (8 drawio, 8 kicad) without mutating global caches permanently
    async def _noop_handler(_engine: Any, _args: dict[str, Any]) -> str:
        return "ok"

    synthetic_specs: dict[str, ToolSpec] = {}
    for i in range(8):
        synthetic_specs[f"drawio_schematic_{i}"] = ToolSpec(_noop_handler, engine="drawio")
    for i in range(8):
        synthetic_specs[f"kicad_schematic_{i}"] = ToolSpec(_noop_handler, engine="kicad")

    for name, spec in synthetic_specs.items():
        monkeypatch.setitem(TOOL_REGISTRY, name, spec)

    monkeypatch.setattr(mcp_http_gateway, "_ALL_GATEWAY_TOOLS_CACHE", {})
    monkeypatch.setattr(mcp_http_gateway, "_DISCOVER_INDEX_CACHE", {})

    app_descs = [
        {
            "slug": "drawio",
            "id": "drawio",
            "name": "Draw.io",
            "description": "Diagram schematic editor",
            "types": ["app-connector"],
            "categories": ["operations-delivery"],
            "maturity": "beta",
            "vendor_specific": True,
        },
        {
            "slug": "kicad",
            "id": "kicad",
            "name": "KiCad",
            "description": "PCB schematic CAD",
            "types": ["app-connector"],
            "categories": ["operations-delivery"],
            "maturity": "beta",
            "vendor_specific": True,
        },
    ]

    ns_id = str(uuid.uuid4())
    compact_session = MCPGatewaySession(
        session_id="sess-compact-boost",
        namespace_id=ns_id,
        principal_id="agent-1",
        scopes=["tools:read", "tools:call"],
        allowed_engines=["*"],
        tool_exposure_mode="compact",
        expanded_tools=set(),
        rate_limit_rpm=120,
        event_queue=asyncio.Queue(maxsize=32),
    )

    class _FakeVaultConnected:
        def __init__(self, _pool: Any = None) -> None:
            pass

        async def get_connected_app_slugs(self, _namespace_id: str) -> set[str]:
            return {"kicad"}

    with (
        patch("nce.plugins.list_plugin_engine_descriptors", return_value=app_descs),
        patch.object(mcp_http_gateway, "TenantConnectionVault", _FakeVaultConnected),
    ):
        # 1. With limit omitted in compact mode, cap is strictly 12 and connected kicad ranks first
        res_default = await discover_tools_handler(
            compact_session,
            {"query": "schematic"},
            pg_pool=MagicMock(),
        )
        assert res_default["matched_count"] == 12
        assert len(res_default["tools"]) == 12
        top_8_names = [t["name"] for t in res_default["tools"][:8]]
        assert all(name.startswith("kicad_schematic_") for name in top_8_names)

        # 2. Requesting limit=50 in compact mode still enforces 12-tool cap
        compact_session.expanded_tools.clear()
        res_over_cap = await discover_tools_handler(
            compact_session,
            {"type_filter": "app-connector", "limit": 50},
            pg_pool=MagicMock(),
        )
        assert res_over_cap["matched_count"] == 12
        assert all(t["name"].startswith("kicad_schematic_") for t in res_over_cap["tools"][:8])

        # 3. Smaller explicit limit (limit=5) is respected
        compact_session.expanded_tools.clear()
        res_small = await discover_tools_handler(
            compact_session,
            {"query": "schematic", "limit": 5},
            pg_pool=MagicMock(),
        )
        assert res_small["matched_count"] == 5

    # 4. Full exposure mode permits >12 when requested
    full_session = MCPGatewaySession(
        session_id="sess-full-boost",
        namespace_id=ns_id,
        principal_id="agent-2",
        scopes=["tools:read", "tools:call"],
        allowed_engines=["*"],
        tool_exposure_mode="full",
        expanded_tools=set(),
        rate_limit_rpm=120,
        event_queue=asyncio.Queue(maxsize=32),
    )
    with (
        patch("nce.plugins.list_plugin_engine_descriptors", return_value=app_descs),
        patch.object(mcp_http_gateway, "TenantConnectionVault", _FakeVaultConnected),
    ):
        res_full = await discover_tools_handler(
            full_session,
            {"query": "schematic", "limit": 16},
            pg_pool=MagicMock(),
        )
        assert res_full["matched_count"] == 16

    # 5. Offline / broken pool fails open silently to empty connected set
    class _BrokenVault:
        def __init__(self, _pool: Any = None) -> None:
            pass

        async def get_connected_app_slugs(self, _namespace_id: str) -> set[str]:
            raise ConnectionError("PG unreachable")

    with (
        patch("nce.plugins.list_plugin_engine_descriptors", return_value=app_descs),
        patch.object(mcp_http_gateway, "TenantConnectionVault", _BrokenVault),
    ):
        res_offline = await discover_tools_handler(
            compact_session,
            {"query": "schematic"},
            pg_pool=MagicMock(),
        )
        assert res_offline["matched_count"] == 12
        assert res_offline["tools"][0]["name"].startswith("drawio_schematic_")

    # 6. End-to-end /mcp HTTP call wires engine.pg_pool into discover_tools_handler
    tok = f"nce_mcp_boost_{uuid.uuid4().hex[:8]}"
    register_test_mcp_client_token(
        tok,
        namespace_id=ns_id,
        principal_id="http-agent",
        allowed_engines=["*"],
        tool_exposure_mode="compact",
    )
    gw_engine = MagicMock()
    gw_engine.redis_client = None
    gw_engine.pg_pool = MagicMock()
    gw_app = create_mcp_gateway_app(engine=gw_engine)
    with (
        patch("nce.plugins.list_plugin_engine_descriptors", return_value=app_descs),
        patch.object(mcp_http_gateway, "TenantConnectionVault", _FakeVaultConnected),
    ):
        with TestClient(gw_app) as client:
            r_http = client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {tok}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "discover_tools", "arguments": {"query": "schematic"}},
                },
            )
            assert r_http.status_code == 200
            payload = json.loads(r_http.json()["result"]["content"][0]["text"])
            assert payload["matched_count"] == 12
            assert all(t["name"].startswith("kicad_schematic_") for t in payload["tools"][:8])


def test_docs_integrity_offline_static_badges_and_v360_metadata() -> None:
    script_path = REPO_ROOT / "scripts" / "check_docs_integrity.py"
    spec = importlib.util.spec_from_file_location("check_docs_integrity", script_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.is_static_shields_badge_url("https://img.shields.io/badge/stage-beta-orange") is True
    assert mod.is_static_shields_badge_url("https://img.shields.io/badge/") is False
    assert mod.is_static_shields_badge_url("https://example.com/badge") is False
    assert mod.main() == 0

    pyproj = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "3.6.0"' in pyproj
    assert nce.__version__ == "3.6.0"

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "https://img.shields.io/badge/stage-beta-orange" in readme
    assert "nce-app-engines" in readme
    assert "nce-vertical-engines" in readme

    plugin_doc = (REPO_ROOT / "docs" / "plugin_development.md").read_text(encoding="utf-8")
    for symbol in (
        "BaseAppConnector",
        "ConnectorDescriptor",
        "@governed_action",
        "TenantConnectionVault",
        "041_app_connections_vault.sql",
        "maturity",
        "vendor_specific",
    ):
        assert symbol in plugin_doc

    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [3.6.0]" in changelog
