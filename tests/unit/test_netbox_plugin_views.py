from __future__ import annotations

import importlib.util
import json
import sys
import types
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "src" / "nce-netbox-plugin" / "nce_netbox_plugin"


def _load_plugin_modules() -> tuple[types.ModuleType, types.ModuleType]:
    """Load nce_netbox_plugin.__init__ and api.views with lightweight Django/NetBox stubs."""
    django_mod = types.ModuleType("django")
    django_conf = types.ModuleType("django.conf")
    django_conf.settings = types.SimpleNamespace(
        NCE_DEFAULT_NAMESPACE_SLUG="default",
        PLUGINS_CONFIG={"nce_netbox_plugin": {"nce_admin_api_url": "http://localhost:8003"}},
    )

    django_db = types.ModuleType("django.db")
    django_db.connection = MagicMock()

    @contextmanager
    def _atomic():
        yield

    django_db.transaction = types.SimpleNamespace(atomic=_atomic)

    django_http = types.ModuleType("django.http")

    class _FakeJsonResponse:
        def __init__(self, data: dict[str, Any], status: int = 200) -> None:
            self.data = data
            self.status_code = status
            self.content = json.dumps(data).encode("utf-8")

    django_http.JsonResponse = _FakeJsonResponse

    django_views = types.ModuleType("django.views")

    class _FakeView:
        pass

    django_views.View = _FakeView

    netbox_mod = types.ModuleType("netbox")
    netbox_plugins = types.ModuleType("netbox.plugins")

    class _FakePluginConfig:
        pass

    netbox_plugins.PluginConfig = _FakePluginConfig

    sim_spec = importlib.util.spec_from_file_location(
        "nce_netbox_plugin.api.simulators",
        PLUGIN_ROOT / "api" / "simulators.py",
    )
    assert sim_spec and sim_spec.loader
    sim_mod = importlib.util.module_from_spec(sim_spec)
    sim_spec.loader.exec_module(sim_mod)

    pkg_mod = types.ModuleType("nce_netbox_plugin")
    api_pkg_mod = types.ModuleType("nce_netbox_plugin.api")

    stub_modules = {
        "django": django_mod,
        "django.conf": django_conf,
        "django.db": django_db,
        "django.http": django_http,
        "django.views": django_views,
        "netbox": netbox_mod,
        "netbox.plugins": netbox_plugins,
        "nce_netbox_plugin": pkg_mod,
        "nce_netbox_plugin.api": api_pkg_mod,
        "nce_netbox_plugin.api.simulators": sim_mod,
    }

    with patch.dict(sys.modules, stub_modules):
        init_spec = importlib.util.spec_from_file_location(
            "nce_netbox_plugin_init_under_test",
            PLUGIN_ROOT / "__init__.py",
        )
        assert init_spec and init_spec.loader
        init_mod = importlib.util.module_from_spec(init_spec)
        init_spec.loader.exec_module(init_mod)

        views_spec = importlib.util.spec_from_file_location(
            "nce_netbox_plugin_views_under_test",
            PLUGIN_ROOT / "api" / "views.py",
        )
        assert views_spec and views_spec.loader
        views_mod = importlib.util.module_from_spec(views_spec)
        views_spec.loader.exec_module(views_mod)

    return init_mod, views_mod


def test_plugin_default_settings_uses_port_8003() -> None:
    init_mod, _ = _load_plugin_modules()
    cfg = init_mod.NceCognitiveDashboardConfig
    assert cfg.default_settings["nce_admin_api_url"] == "http://localhost:8003"


def test_derive_fault_nodes_from_db_queries_real_kg_nodes_columns() -> None:
    _, views_mod = _load_plugin_modules()
    view = views_mod.NceCognitiveStatsView()

    cursor = MagicMock()
    cursor.fetchall.return_value = [
        ("node-1", "Core Switch Predictive Fault", "predictive_fault"),
        ("node-2", "PSU Fault Warning", "hardware_alert"),
        ("node-3", "Healthy Spine Router", "device"),
    ]
    views_mod.connection.cursor.return_value.__enter__.return_value = cursor

    as_of = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    nodes = view._derive_fault_nodes_from_db("ns-uuid-1", as_of)

    assert cursor.execute.call_count == 1
    sql_query, params = cursor.execute.call_args[0]
    normalized_sql = " ".join(sql_query.split())
    assert "SELECT id, label, entity_type FROM kg_nodes" in normalized_sql
    assert "metadata" not in normalized_sql
    assert "node_type" not in normalized_sql
    assert params == ["ns-uuid-1", as_of]

    assert nodes == [
        {
            "node_id": "node-1",
            "name": "Core Switch Predictive Fault",
            "node_type": "predictive_fault",
            "failure_probability": 0.75,
            "estimated_mtbf_hours": 720.0,
        },
        {
            "node_id": "node-2",
            "name": "PSU Fault Warning",
            "node_type": "hardware_alert",
            "failure_probability": 0.5,
            "estimated_mtbf_hours": 720.0,
        },
    ]


def test_get_uses_remote_nce_admin_api_when_local_tables_missing() -> None:
    _, views_mod = _load_plugin_modules()
    view = views_mod.NceCognitiveStatsView()
    view._verify_nce_tables = MagicMock(return_value=False)

    views_mod.settings.PLUGINS_CONFIG = {
        "nce_netbox_plugin": {
            "nce_admin_api_url": "http://nce-admin.internal:8003/",
            "nce_admin_api_key": "secret-token-123",
        }
    }

    remote_payload = {
        "real_database": True,
        "namespace": "tenant-a",
        "pending_queue_count": 3,
        "incidents": [],
        "operator_stress_trend": [],
        "replay_runs": [],
        "fault_nodes": [],
    }

    fake_resp = MagicMock()
    fake_resp.status = 200
    fake_resp.read.return_value = json.dumps(remote_payload).encode("utf-8")
    fake_resp.__enter__.return_value = fake_resp

    request = types.SimpleNamespace(GET={"namespace": "tenant-a", "as_of": "2026-10-15T12:00:00Z"})

    with patch.object(views_mod.urllib.request, "urlopen", return_value=fake_resp) as mock_urlopen:
        response = view.get(request)

    assert response.status_code == 200
    assert response.data == remote_payload
    assert mock_urlopen.call_count == 1
    req_obj = mock_urlopen.call_args[0][0]
    assert mock_urlopen.call_args[1]["timeout"] == 3.0
    assert (
        req_obj.full_url
        == "http://nce-admin.internal:8003/api/cognitive/stats?namespace=tenant-a&as_of=2026-10-15T12%3A00%3A00%2B00%3A00"
    )
    assert req_obj.headers.get("Authorization") == "Bearer secret-token-123"


def test_get_falls_back_to_simulated_data_when_remote_api_unreachable() -> None:
    _, views_mod = _load_plugin_modules()
    view = views_mod.NceCognitiveStatsView()
    view._verify_nce_tables = MagicMock(return_value=False)

    request = types.SimpleNamespace(
        GET={"namespace": "tenant-sim", "as_of": "2026-10-15T12:00:00Z"}
    )

    with patch.object(
        views_mod.urllib.request, "urlopen", side_effect=OSError("connection refused")
    ):
        response = view.get(request)

    assert response.status_code == 200
    assert response.data["real_database"] is False
    assert response.data["namespace"] == "tenant-sim"


def test_debian_install_includes_portal_server_and_requirements_lock() -> None:
    install_path = REPO_ROOT / "debian" / "nce-server.install"
    lines = {
        line.strip()
        for line in install_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    assert "portal_server.py /opt/nce/" in lines
    assert "requirements.lock /opt/nce/" in lines
