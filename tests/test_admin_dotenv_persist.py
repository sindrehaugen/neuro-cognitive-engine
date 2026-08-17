"""Tests for admin .env persistence guard and sanitized error responses."""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

os.environ.setdefault("NCE_MASTER_KEY", "dev-test-key-32chars-long!!")


def test_update_dotenv_raises_when_persist_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from admin_server import update_dotenv

    with patch("nce.admin_http_support.cfg") as mock_cfg:
        mock_cfg.NCE_ALLOW_ADMIN_DOTENV_PERSIST = False
        with pytest.raises(RuntimeError, match="disabled"):
            update_dotenv({"FOO": "bar"})


def test_update_dotenv_atomic_write(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from admin_server import update_dotenv

    dotenv = tmp_path / ".env"
    dotenv.write_text("EXISTING=old\nOTHER=keep\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with patch("nce.admin_http_support.cfg") as mock_cfg:
        mock_cfg.NCE_ALLOW_ADMIN_DOTENV_PERSIST = True
        update_dotenv({"EXISTING": "new", "ADDED": "yes"})

    text = dotenv.read_text(encoding="utf-8")
    assert "EXISTING=new" in text
    assert "ADDED=yes" in text
    assert "OTHER=keep" in text
    assert "EXISTING=old" not in text


def test_admin_error_response_hides_detail_in_prod() -> None:
    from admin_server import _admin_error_response

    with patch("nce.admin_http_support.cfg") as mock_cfg:
        mock_cfg.IS_PROD = True
        resp = _admin_error_response("boom", ValueError("secret internals"))
    body = json.loads(resp.body)
    assert body["error"] == "boom"
    assert "detail" not in body


def test_admin_error_response_includes_detail_in_dev() -> None:
    from admin_server import _admin_error_response

    with patch("nce.admin_http_support.cfg") as mock_cfg:
        mock_cfg.IS_PROD = False
        resp = _admin_error_response("boom", ValueError("secret internals"))
    body = json.loads(resp.body)
    assert body["detail"] == "secret internals"


def test_admin_validation_error_hides_pydantic_detail_in_prod() -> None:
    from pydantic import BaseModel, ValidationError

    from nce.admin_http_support import admin_validation_error

    class _M(BaseModel):
        x: int

    try:
        _M.model_validate({"x": "nope"})
    except ValidationError as exc:
        with patch("nce.admin_http_support.cfg") as mock_cfg:
            mock_cfg.IS_PROD = True
            resp = admin_validation_error(exc, status_code=422)
    body = json.loads(resp.body)
    assert body["error"] == "Validation failed"
    assert "detail" not in body


def test_sanitize_admin_reason_redacts_unexpected_in_prod() -> None:
    from nce.admin_http_support import sanitize_admin_reason

    with patch("nce.admin_http_support.cfg") as mock_cfg:
        mock_cfg.IS_PROD = True
        reason = sanitize_admin_reason(RuntimeError("connection string leaked"))
    assert reason == "RuntimeError"
