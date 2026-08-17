"""Regression tests for production-readiness audit P0/P1 hardening."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("NCE_MASTER_KEY", "x" * 32)
os.environ.setdefault("DROPBOX_APP_SECRET", "test-dropbox-secret")
os.environ.setdefault("GRAPH_CLIENT_STATE", "test-graph-state")
os.environ.setdefault("DRIVE_CHANNEL_TOKEN", "test-drive-token")


def test_assert_admin_override_raises_in_production() -> None:
    from nce.config import assert_admin_override_not_in_production, cfg

    with patch.object(cfg, "NCE_ADMIN_OVERRIDE", True), patch.object(cfg, "IS_PROD", True):
        with pytest.raises(RuntimeError, match="NCE_ADMIN_OVERRIDE"):
            assert_admin_override_not_in_production()


def test_validated_cognitive_base_url_allows_loopback() -> None:
    from nce.embeddings import cognitive_health_check_url, validated_cognitive_base_url

    assert validated_cognitive_base_url("http://127.0.0.1:11435") == "http://127.0.0.1:11435"
    assert cognitive_health_check_url("http://127.0.0.1:11435") == "http://127.0.0.1:11435/health"


def test_validated_cognitive_base_url_rejects_metadata_host() -> None:
    from nce.embeddings import validated_cognitive_base_url
    from nce.providers.base import LLMProviderError

    with pytest.raises(LLMProviderError, match="SSRF guard"):
        validated_cognitive_base_url("http://169.254.169.254/")


def test_webhook_client_ip_ignores_xff_without_trust_proxy() -> None:
    from nce.webhook_receiver.main import _client_ip

    request = MagicMock()
    request.headers = {"x-forwarded-for": "203.0.113.50"}
    request.client = MagicMock(host="10.0.0.5")

    with patch("nce.webhook_receiver.main.cfg") as mock_cfg:
        mock_cfg.NCE_WEBHOOK_TRUST_PROXY = False
        assert _client_ip(request) == "10.0.0.5"


def test_webhook_client_ip_honors_xff_when_trust_proxy() -> None:
    from nce.webhook_receiver.main import _client_ip

    request = MagicMock()
    request.headers = {"x-forwarded-for": "203.0.113.50, 10.0.0.1"}
    request.client = MagicMock(host="10.0.0.5")

    with patch("nce.webhook_receiver.main.cfg") as mock_cfg:
        mock_cfg.NCE_WEBHOOK_TRUST_PROXY = True
        assert _client_ip(request) == "203.0.113.50"


def test_validate_admin_credentials_requires_keys_in_production(monkeypatch) -> None:
    from nce.config import _Config

    monkeypatch.setattr(_Config, "IS_PROD", True)
    monkeypatch.setattr(_Config, "NCE_ADMIN_API_KEY", "")
    monkeypatch.setattr(_Config, "NCE_ADMIN_USERNAME", "admin")
    monkeypatch.setattr(_Config, "NCE_ADMIN_PASSWORD", "$pbkdf2$abc")

    with pytest.raises(RuntimeError, match="NCE_ADMIN_API_KEY"):
        _Config.validate_admin_credentials()


def test_validate_admin_credentials_rejects_plaintext_password_in_prod(
    monkeypatch,
) -> None:
    from nce.config import _Config

    monkeypatch.setattr(_Config, "IS_PROD", True)
    monkeypatch.setattr(_Config, "NCE_ADMIN_API_KEY", "admin-api-key")
    monkeypatch.setattr(_Config, "NCE_ADMIN_USERNAME", "admin")
    monkeypatch.setattr(_Config, "NCE_ADMIN_PASSWORD", "plaintext")

    with pytest.raises(RuntimeError, match="pbkdf2"):
        _Config.validate_admin_credentials()


@pytest.mark.asyncio
async def test_datastores_save_rejects_when_persist_disabled() -> None:
    from unittest.mock import AsyncMock

    from admin_server import api_admin_datastores_save

    request = MagicMock()
    request.json = AsyncMock(return_value={})

    with (
        patch("nce.admin_state.engine", MagicMock()),
        patch("nce.admin_handlers._shared.cfg") as mock_cfg,
    ):
        mock_cfg.NCE_ALLOW_ADMIN_DOTENV_PERSIST = False
        resp = await api_admin_datastores_save(request)

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_trigger_gc_hides_exception_detail_in_prod() -> None:
    from admin_server import trigger_gc

    mock_engine = MagicMock()
    mock_engine.force_gc = AsyncMock(side_effect=RuntimeError("internal gc failure"))

    request = MagicMock()

    with (
        patch("nce.admin_state.engine", mock_engine),
        patch("nce.admin_http_support.cfg") as mock_cfg,
    ):
        mock_cfg.IS_PROD = True
        resp = await trigger_gc(request)

    assert resp.status_code == 500
    body = json.loads(resp.body.decode())
    assert body["error"] == "Garbage collection failed"
    assert "detail" not in body
    assert "internal gc failure" not in resp.body.decode()
