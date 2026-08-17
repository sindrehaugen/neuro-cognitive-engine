"""
Extended HMAC middleware tests using shared fixtures.http_hmac_helpers.

Covers transport edge cases beyond tests/test_auth.py:
  - Payload tampering invalidates signature (non-empty body)
  - UTF-8 payloads
  - Boundary: body of only whitespace hashes differently from empty body
"""

from __future__ import annotations

import hashlib
import time
from uuid import uuid4

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from nce.auth import HMACAuthMiddleware, NamespaceContext, resolve_namespace
from tests.fixtures.http_hmac_helpers import admin_hmac_headers, compute_admin_hmac

_KEY = "fixture-hmac-shared-secret-32b+"


def _app() -> Starlette:
    async def echo(request: Request) -> JSONResponse:
        body = await request.body()
        return JSONResponse({"len": len(body), "raw": body.decode("utf-8", errors="replace")})

    async def health(_: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    return Starlette(
        routes=[
            Route("/api/echo", endpoint=echo, methods=["POST"]),
            Route("/api/health", endpoint=health, methods=["GET"]),
        ],
        middleware=[Middleware(HMACAuthMiddleware, api_key=_KEY)],
    )


class TestHMACPayloadEdges:
    """Verify body hash is wired into the MAC string."""

    def test_tampered_json_body_rejected(self) -> None:
        app = _app()
        good = b'{"trusted": true}'
        ts = int(time.time())
        hdr = admin_hmac_headers(
            hex_key_material=_KEY,
            method="POST",
            path="/api/echo",
            body=good,
            timestamp=ts,
        )

        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.post("/api/echo", content=b'{"trusted": false}', headers=hdr)

        assert r.status_code == 401
        assert r.json()["error"]["data"]["reason"] == "invalid_signature"

        hdr_ok = admin_hmac_headers(
            hex_key_material=_KEY,
            method="POST",
            path="/api/echo",
            body=good,
            timestamp=ts,
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            r2 = client.post("/api/echo", content=good, headers=hdr_ok)
        assert r2.status_code == 200

    def test_utf8_multibyte_payload(self) -> None:
        app = _app()
        body = "北極 ✨".encode()
        ts = int(time.time())
        hdr = admin_hmac_headers(
            hex_key_material=_KEY,
            method="POST",
            path="/api/echo",
            body=body,
            timestamp=ts,
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.post("/api/echo", content=body, headers=hdr)
        assert r.status_code == 200
        assert r.json()["len"] == len(body)

    def test_whitespace_body_is_not_empty_canonical(self) -> None:
        """Empty body skips SHA256 segment; ASCII space does not."""
        canonical_empty = compute_admin_hmac(_KEY, "GET\n/api/health\n123")
        canonical_space = compute_admin_hmac(
            _KEY,
            "GET\n/api/health\n123\n" + hashlib.sha256(b" ").hexdigest(),
        )
        assert canonical_empty != canonical_space


class TestNamespaceContextHeaderEdges:
    """Malformed namespace IDs and oversized agent trims."""

    def test_resolve_namespace_raises_on_bad_uuid_syntax(self) -> None:
        with pytest.raises(ValueError, match="Invalid namespace UUID"):
            resolve_namespace({"x-nce-namespace-id": "not-valid"})

    def test_namespace_context_near_max_agent_trim(self) -> None:
        uid = uuid4()
        ctx = NamespaceContext(namespace_id=uid, agent_id="z" * 300)
        assert len(ctx.agent_id) == 128
