"""
tests/test_jwt_auth.py

Unit tests for ``nce/jwt_auth.py`` — decode, key loading, and middleware.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from uuid import UUID

import jwt
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from nce.config import cfg
from nce.jwt_auth import (
    JWTAuthMiddleware,
    JWTDecodeError,
    _build_jwt_key,
    _load_public_key,
    decode_agent_token,
)

# Spec uses a short dev secret; PyJWT warns under pytest filterwarnings=error.
pytestmark = pytest.mark.filterwarnings("ignore::jwt.warnings.InsecureKeyLengthWarning")

# ---------------------------------------------------------------------------
# Constants & helpers
# ---------------------------------------------------------------------------

hs256_secret = "test-secret-for-unit-tests"
valid_ns_id = "aaaabbbb-cccc-dddd-eeee-ffffffffffff"
far_future = int(time.time()) + 3600
past_timestamp = int(time.time()) - 3600

_SAMPLE_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAu1SU1LfVLPHCozMxH2Mo
4lgOEePzNm0tRgeLezV6ffAt0ugFVZS8RTibGQWTplOqf41xMko2ffO2tdFVoNw
hLKn2+KedY5tOER8LC4t75SzpRpJLLU1i6lErIgclfMMBlj4yHkX5YxXy1c2vQ
0wIDAQAB
-----END PUBLIC KEY-----"""


def make_token(
    payload: dict[str, Any],
    *,
    secret: str = hs256_secret,
    algorithm: str = "HS256",
) -> str:
    return jwt.encode(payload, secret, algorithm=algorithm)


def _base_payload(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "namespace_id": valid_ns_id,
        "exp": far_future,
    }
    data.update(overrides)
    return data


@pytest.fixture
def hs256_cfg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "NCE_JWT_SECRET", hs256_secret)
    monkeypatch.setattr(cfg, "NCE_JWT_ALGORITHM", "HS256")
    monkeypatch.setattr(cfg, "NCE_JWT_ISSUER", "")
    monkeypatch.setattr(cfg, "NCE_JWT_AUDIENCE", "")
    monkeypatch.setattr(cfg, "IS_PROD", False)
    monkeypatch.setattr(cfg, "NCE_JWT_LEEWAY_SECONDS", 0)
    monkeypatch.setattr(cfg, "NCE_JWT_PUBLIC_KEY", "")


# ---------------------------------------------------------------------------
# decode_agent_token
# ---------------------------------------------------------------------------


class TestDecodeAgentToken:
    def test_valid_hs256_token_returns_namespace_ctx(self, hs256_cfg: None) -> None:
        token = make_token(_base_payload(agent_id="agent-42"))
        ctx = decode_agent_token(token)
        assert ctx.namespace_id == UUID(valid_ns_id)
        assert ctx.agent_id == "agent-42"

    def test_agent_id_in_token_is_used(self, hs256_cfg: None) -> None:
        token = make_token(_base_payload(agent_id="custom-agent"))
        ctx = decode_agent_token(token)
        assert ctx.agent_id == "custom-agent"

    def test_agent_id_absent_defaults_to_default(self, hs256_cfg: None) -> None:
        token = make_token(_base_payload())
        ctx = decode_agent_token(token)
        assert ctx.agent_id == "default"

    def test_expired_token_raises_jwt_invalid(self, hs256_cfg: None) -> None:
        token = make_token(_base_payload(exp=past_timestamp))
        with pytest.raises(JWTDecodeError) as excinfo:
            decode_agent_token(token)
        assert excinfo.value.code == -32005
        assert excinfo.value.reason == "jwt_expired"

    def test_bad_signature_raises_jwt_invalid(self, hs256_cfg: None) -> None:
        token = make_token(_base_payload(), secret="wrong-secret")
        with pytest.raises(JWTDecodeError) as excinfo:
            decode_agent_token(token)
        assert excinfo.value.code == -32005

    def test_missing_namespace_id_raises_missing_claim(self, hs256_cfg: None) -> None:
        payload = _base_payload()
        del payload["namespace_id"]
        token = make_token(payload)
        with pytest.raises(JWTDecodeError) as excinfo:
            decode_agent_token(token)
        assert excinfo.value.code == -32006
        assert excinfo.value.reason == "missing_claim:namespace_id"

    def test_invalid_namespace_id_uuid_raises_bad_claim(self, hs256_cfg: None) -> None:
        token = make_token(_base_payload(namespace_id="not-a-uuid"))
        with pytest.raises(JWTDecodeError) as excinfo:
            decode_agent_token(token)
        assert excinfo.value.code == -32007
        assert excinfo.value.reason == "invalid_claim:namespace_id"

    def test_invalid_namespace_id_not_in_response_reason(self, hs256_cfg: None) -> None:
        attacker = "ATTACKER_PAYLOAD"
        token = make_token(_base_payload(namespace_id=attacker))
        with pytest.raises(JWTDecodeError) as excinfo:
            decode_agent_token(token)
        assert attacker not in excinfo.value.reason


# ---------------------------------------------------------------------------
# Required claims policy (iss / aud)
# ---------------------------------------------------------------------------


class TestRequiredClaimsPolicy:
    def test_iss_not_required_when_issuer_not_configured(self, hs256_cfg: None) -> None:
        token = make_token(_base_payload())
        ctx = decode_agent_token(token)
        assert ctx.namespace_id == UUID(valid_ns_id)

    def test_iss_required_when_issuer_configured(
        self, hs256_cfg: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cfg, "NCE_JWT_ISSUER", "nce-issuer")
        token = make_token(_base_payload())
        with pytest.raises(JWTDecodeError) as excinfo:
            decode_agent_token(token)
        assert "iss" in excinfo.value.reason

    def test_iss_validated_when_configured(
        self, hs256_cfg: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cfg, "NCE_JWT_ISSUER", "nce-issuer")
        token = make_token(_base_payload(iss="nce-issuer"))
        ctx = decode_agent_token(token)
        assert ctx.namespace_id == UUID(valid_ns_id)

    def test_aud_not_required_when_audience_not_configured(self, hs256_cfg: None) -> None:
        token = make_token(_base_payload())
        ctx = decode_agent_token(token, audience=None)
        assert ctx.namespace_id == UUID(valid_ns_id)

    def test_aud_required_when_audience_arg_provided(self, hs256_cfg: None) -> None:
        token = make_token(_base_payload())
        with pytest.raises(JWTDecodeError) as excinfo:
            decode_agent_token(token, audience="nce_a2a")
        assert "aud" in excinfo.value.reason or excinfo.value.reason == ("jwt_audience_mismatch")

    def test_aud_validated_when_audience_arg_provided(self, hs256_cfg: None) -> None:
        token = make_token(_base_payload(aud="nce_web"))
        with pytest.raises(JWTDecodeError) as excinfo:
            decode_agent_token(token, audience="nce_a2a")
        assert excinfo.value.reason == "jwt_audience_mismatch"

    def test_correct_aud_accepted(self, hs256_cfg: None) -> None:
        token = make_token(_base_payload(aud="nce_a2a"))
        ctx = decode_agent_token(token, audience="nce_a2a")
        assert ctx.namespace_id == UUID(valid_ns_id)

    def test_aud_from_global_config_used_when_not_overridden(
        self, hs256_cfg: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cfg, "NCE_JWT_AUDIENCE", "nce_global")
        token = make_token(_base_payload(aud="nce_global"))
        ctx = decode_agent_token(token, audience=None)
        assert ctx.namespace_id == UUID(valid_ns_id)


# ---------------------------------------------------------------------------
# _build_jwt_key
# ---------------------------------------------------------------------------


class TestBuildJwtKey:
    def test_public_key_with_hmac_algorithm_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cfg, "NCE_JWT_PUBLIC_KEY", _SAMPLE_PEM)
        monkeypatch.setattr(cfg, "NCE_JWT_ALGORITHM", "HS256")
        monkeypatch.setattr(cfg, "NCE_JWT_SECRET", "")
        with pytest.raises(RuntimeError, match="not asymmetric"):
            _build_jwt_key("HS256")

    def test_asymmetric_algorithm_without_public_key_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cfg, "NCE_JWT_PUBLIC_KEY", "")
        monkeypatch.setattr(cfg, "NCE_JWT_SECRET", hs256_secret)
        with pytest.raises(RuntimeError, match="requires NCE_JWT_PUBLIC_KEY"):
            _build_jwt_key("RS256")

    def test_unsupported_algorithm_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cfg, "NCE_JWT_PUBLIC_KEY", "")
        monkeypatch.setattr(cfg, "NCE_JWT_SECRET", hs256_secret)
        with pytest.raises(RuntimeError, match="Unsupported JWT algorithm"):
            _build_jwt_key("RS9999")

    def test_no_key_configured_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cfg, "NCE_JWT_PUBLIC_KEY", "")
        monkeypatch.setattr(cfg, "NCE_JWT_SECRET", "")
        with pytest.raises(RuntimeError, match="JWT key not configured"):
            _build_jwt_key("HS256")


# ---------------------------------------------------------------------------
# _load_public_key
# ---------------------------------------------------------------------------


class TestLoadPublicKey:
    def test_raw_pem_returned_as_is(self) -> None:
        pem = _SAMPLE_PEM.strip()
        assert _load_public_key(pem) == pem

    def test_file_uri_inside_allowed_dir_loads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        key_dir = tmp_path / "keys"
        key_dir.mkdir()
        key_file = key_dir / "pub.pem"
        pem_content = _SAMPLE_PEM.strip()
        key_file.write_text(pem_content, encoding="utf-8")
        monkeypatch.setattr(cfg, "NCE_JWT_KEY_DIR", str(key_dir))
        uri = f"file://{key_file.resolve()}"
        assert _load_public_key(uri) == pem_content

    def test_file_uri_outside_allowed_dir_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        outside = tmp_path / "outside.pem"
        outside.write_text(_SAMPLE_PEM, encoding="utf-8")
        monkeypatch.setattr(cfg, "NCE_JWT_KEY_DIR", str(allowed))
        uri = f"file://{outside.resolve()}"
        with pytest.raises(ValueError, match="escapes allowed directory"):
            _load_public_key(uri)

    def test_nonexistent_key_dir_gives_clean_valueerror(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        missing = "/nonexistent/nce-jwt-key-dir"
        monkeypatch.setattr(cfg, "NCE_JWT_KEY_DIR", missing)
        with pytest.raises(ValueError, match="NCE_JWT_KEY_DIR does not exist"):
            _load_public_key(f"file://{missing}/key.pem")

    def test_missing_file_raises_valueerror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        key_dir = tmp_path / "keys"
        key_dir.mkdir()
        monkeypatch.setattr(cfg, "NCE_JWT_KEY_DIR", str(key_dir))
        missing = key_dir / "missing.pem"
        with pytest.raises(ValueError, match="file not found"):
            _load_public_key(f"file://{missing.resolve()}")

    def test_invalid_uri_raises_valueerror(self) -> None:
        with pytest.raises(ValueError, match="must be a PEM string or a file://"):
            _load_public_key("http://example.com/key.pem")


# ---------------------------------------------------------------------------
# JWTAuthMiddleware (Starlette TestClient)
# ---------------------------------------------------------------------------


def _jwt_middleware_app() -> Starlette:
    route_called: dict[str, bool] = {"protected": False}

    async def health(_request: Any) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def protected(request: Any) -> JSONResponse:
        route_called["protected"] = True
        ns_ctx = request.state.namespace_ctx
        return JSONResponse(
            {
                "namespace_id": str(ns_ctx.namespace_id),
                "agent_id": ns_ctx.agent_id,
            }
        )

    app = Starlette(
        middleware=[
            Middleware(
                JWTAuthMiddleware,
                protected_prefix="/api/v1/",
                expected_audience=None,
            ),
        ],
        routes=[
            Route("/health", endpoint=health, methods=["GET"]),
            Route("/api/v1/something", endpoint=protected, methods=["GET"]),
        ],
    )
    app.state._route_called = route_called  # type: ignore[attr-defined]
    return app


class TestJWTAuthMiddleware:
    def test_unprotected_path_passes_without_token(self, hs256_cfg: None) -> None:
        app = _jwt_middleware_app()
        with TestClient(app) as client:
            resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_protected_path_missing_auth_header_returns_401(self, hs256_cfg: None) -> None:
        app = _jwt_middleware_app()
        with TestClient(app) as client:
            resp = client.get("/api/v1/something")
        assert resp.status_code == 401
        body = resp.json()
        assert body["error"]["code"] == -32005

    def test_protected_path_wrong_scheme_returns_401(self, hs256_cfg: None) -> None:
        token = make_token(_base_payload())
        app = _jwt_middleware_app()
        with TestClient(app) as client:
            resp = client.get(
                "/api/v1/something",
                headers={"Authorization": f"Basic {token}"},
            )
        assert resp.status_code == 401
        assert resp.json()["error"]["data"]["reason"] == "invalid_authorization_scheme"

    def test_valid_token_attaches_namespace_ctx(self, hs256_cfg: None) -> None:
        token = make_token(_base_payload(agent_id="mw-agent"))
        app = _jwt_middleware_app()
        with TestClient(app) as client:
            resp = client.get(
                "/api/v1/something",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["namespace_id"] == valid_ns_id
        assert data["agent_id"] == "mw-agent"

    def test_invalid_token_does_not_call_route(self, hs256_cfg: None) -> None:
        route_called = {"protected": False}

        async def protected(request: Any) -> JSONResponse:
            route_called["protected"] = True
            return JSONResponse({"ok": True})

        async def health(_request: Any) -> JSONResponse:
            return JSONResponse({"status": "ok"})

        app = Starlette(
            middleware=[
                Middleware(
                    JWTAuthMiddleware,
                    protected_prefix="/api/v1/",
                    expected_audience=None,
                ),
            ],
            routes=[
                Route("/health", endpoint=health, methods=["GET"]),
                Route("/api/v1/something", endpoint=protected, methods=["GET"]),
            ],
        )
        with TestClient(app) as client:
            resp = client.get(
                "/api/v1/something",
                headers={"Authorization": "Bearer not.a.valid.jwt"},
            )
        assert resp.status_code == 401
        assert route_called["protected"] is False

    def test_401_response_includes_www_authenticate(self, hs256_cfg: None) -> None:
        app = _jwt_middleware_app()
        with TestClient(app) as client:
            resp = client.get("/api/v1/something")
        assert resp.status_code == 401
        assert resp.headers.get("www-authenticate") == 'Bearer realm="nce"'

    def test_oversized_token_rejected(self, hs256_cfg: None) -> None:
        oversized = "a" * 8193
        app = _jwt_middleware_app()
        with TestClient(app) as client:
            resp = client.get(
                "/api/v1/something",
                headers={"Authorization": f"Bearer {oversized}"},
            )
        assert resp.status_code == 401
        assert resp.json()["error"]["data"]["reason"] == "jwt_too_large"
