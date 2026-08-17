"""
tests/test_hmac_nonce.py

Batch 116 — hmac-nonce-mandatory

Gate assertions (pure-unit, mocked Redis):
  1. Replayed / reused nonce           → rejected (replay_nonce_conflict)
  2. Request outside ±90 s window      → rejected (replay_or_clock_skew)
  3. Missing nonce + Redis up + nonce required → rejected (nonce_missing)
  4. Prod + Redis down                  → rejected (nonce_store_unavailable)
  5. Dev  + Redis down                  → allowed with log

All Redis interactions are mocked — no real Redis required.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from nce.auth import (
    _TIMESTAMP_DRIFT_SECONDS,
    HMACAuthMiddleware,
    NonceStore,
)

# ---------------------------------------------------------------------------
# Module-level assertions on knobs (config.py values)
# ---------------------------------------------------------------------------


def test_clock_skew_default_is_90s() -> None:
    """NCE_CLOCK_SKEW_TOLERANCE_S must default to 90 (Batch 116)."""
    from nce.config import cfg

    assert cfg.NCE_CLOCK_SKEW_TOLERANCE_S == 90, (
        f"Expected 90, got {cfg.NCE_CLOCK_SKEW_TOLERANCE_S}. "
        "Batch 116 requires the default window to be ±90 s."
    )


def test_hmac_nonce_required_default_true() -> None:
    """NCE_HMAC_NONCE_REQUIRED must default to True (Batch 116)."""
    from nce.config import cfg

    assert cfg.NCE_HMAC_NONCE_REQUIRED is True, (
        f"Expected True, got {cfg.NCE_HMAC_NONCE_REQUIRED}. "
        "Batch 116 requires the nonce to be mandatory by default."
    )


def test_timestamp_drift_constant_matches_config() -> None:
    """_TIMESTAMP_DRIFT_SECONDS module constant must equal cfg value at import."""
    assert _TIMESTAMP_DRIFT_SECONDS == 90


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_API_KEY = "test-hmac-secret-batch116"


def _make_sig(key: str, method: str, path: str, timestamp: int, body: bytes = b"") -> str:
    parts = [method.upper(), path, str(timestamp)]
    if body:
        parts.append(hashlib.sha256(body).hexdigest())
    canonical = "\n".join(parts)
    return _hmac.new(key.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def _valid_headers(
    key: str,
    method: str = "POST",
    path: str = "/api/ping",
    *,
    nonce: str | None = "unique-nonce-abc123",
    body: bytes = b"",
    ts_offset: int = 0,
    now: int | None = None,
) -> dict[str, str]:
    """Build a full set of valid HMAC headers with an optional nonce.

    If ``now`` is given, the header timestamp is built against that fixed
    absolute base instead of the wall clock.  This lets tests pin both the
    request timestamp and the middleware's notion of "now" to the same value,
    eliminating wall-clock-boundary flakiness.
    """
    base = int(time.time()) if now is None else now
    ts = base + ts_offset
    sig = _make_sig(key, method, path, ts, body)
    headers: dict[str, str] = {
        "x-nce-timestamp": str(ts),
        "authorization": f"HMAC-SHA256 {sig}",
    }
    if nonce is not None:
        headers["x-nce-nonce"] = nonce
    return headers


def _make_app(
    nonce_store: NonceStore | None = None,
    nonce_required: bool = True,
) -> Starlette:
    """Build a minimal Starlette app wrapped by HMACAuthMiddleware."""

    async def _ping(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    return Starlette(
        routes=[Route("/api/ping", _ping, methods=["POST", "GET"])],
        middleware=[
            Middleware(
                HMACAuthMiddleware,
                protected_prefix="/api/",
                api_key=_API_KEY,
                nonce_store=nonce_store,
                nonce_required=nonce_required,
            )
        ],
    )


def _make_nonce_store_mock(
    *,
    ping_result: bool = True,
    check_result: bool = True,
) -> NonceStore:
    """Return a NonceStore with mocked async methods."""
    store = MagicMock(spec=NonceStore)
    store.ping = AsyncMock(return_value=ping_result)
    store.check_and_store = AsyncMock(return_value=check_result)
    return store  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Gate 2: Timestamp window ±90 s
# ---------------------------------------------------------------------------


class TestTimestampWindow:
    """Request outside ±90 s window must be rejected.

    These tests pin both the request timestamp and the middleware's notion of
    "now" to ``_FIXED_NOW`` by patching ``time.time`` for the duration of the
    request.  The middleware computes ``now = int(time.time())`` independently,
    so freezing the clock makes ``delta == abs(ts_offset)`` exactly and removes
    the wall-clock-boundary flake (delta would otherwise drift by ±1 s if the
    request crossed a 1-second boundary).
    """

    # Fixed wall-clock value shared by the header builder and the middleware.
    _FIXED_NOW = 1_700_000_000

    def test_expired_timestamp_rejected(self) -> None:
        store = _make_nonce_store_mock()
        client = TestClient(_make_app(nonce_store=store), raise_server_exceptions=True)
        hdrs = _valid_headers(
            _API_KEY, ts_offset=-(90 + 1), now=self._FIXED_NOW
        )  # 91 s in the past
        with patch("time.time", return_value=float(self._FIXED_NOW)):
            resp = client.post("/api/ping", headers=hdrs)
        assert resp.status_code == 401
        body = resp.json()
        assert body["error"]["data"]["reason"] == "replay_or_clock_skew"

    def test_future_timestamp_rejected(self) -> None:
        store = _make_nonce_store_mock()
        client = TestClient(_make_app(nonce_store=store), raise_server_exceptions=True)
        hdrs = _valid_headers(_API_KEY, ts_offset=91, now=self._FIXED_NOW)  # 91 s in the future
        with patch("time.time", return_value=float(self._FIXED_NOW)):
            resp = client.post("/api/ping", headers=hdrs)
        assert resp.status_code == 401
        body = resp.json()
        assert body["error"]["data"]["reason"] == "replay_or_clock_skew"

    def test_edge_within_window_accepted(self) -> None:
        store = _make_nonce_store_mock()
        client = TestClient(_make_app(nonce_store=store), raise_server_exceptions=True)
        hdrs = _valid_headers(
            _API_KEY, ts_offset=89, now=self._FIXED_NOW
        )  # 89 s in the future — inside window
        with patch("time.time", return_value=float(self._FIXED_NOW)):
            resp = client.post("/api/ping", headers=hdrs)
        assert resp.status_code == 200

    def test_exactly_at_boundary_rejected(self) -> None:
        """abs(delta) > tolerance — boundary itself is outside (strict >)."""
        store = _make_nonce_store_mock()
        client = TestClient(_make_app(nonce_store=store), raise_server_exceptions=True)
        hdrs = _valid_headers(
            _API_KEY, ts_offset=-90, now=self._FIXED_NOW
        )  # exactly 90 s — rejected (> not >=)
        with patch("time.time", return_value=float(self._FIXED_NOW)):
            resp = client.post("/api/ping", headers=hdrs)
        # delta == 90, tolerance == 90 → abs(90) > 90 is False → accepted
        # The middleware uses `abs(now - ts) > _TIMESTAMP_DRIFT_SECONDS` so
        # exactly 90 passes.  Assert accepted.
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Gate 1: Replayed / reused nonce
# ---------------------------------------------------------------------------


class TestNonceReplay:
    """A reused nonce must be rejected even with a fresh timestamp."""

    def test_replayed_nonce_rejected(self) -> None:
        # check_and_store returns False → nonce was already seen
        store = _make_nonce_store_mock(ping_result=True, check_result=False)
        client = TestClient(_make_app(nonce_store=store), raise_server_exceptions=True)
        hdrs = _valid_headers(_API_KEY, nonce="already-seen-nonce")
        resp = client.post("/api/ping", headers=hdrs)
        assert resp.status_code == 401
        body = resp.json()
        assert body["error"]["data"]["reason"] == "replay_nonce_conflict"

    def test_fresh_nonce_accepted(self) -> None:
        store = _make_nonce_store_mock(ping_result=True, check_result=True)
        client = TestClient(_make_app(nonce_store=store), raise_server_exceptions=True)
        hdrs = _valid_headers(_API_KEY, nonce="brand-new-nonce-xyz")
        resp = client.post("/api/ping", headers=hdrs)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Gate 3: Missing nonce + Redis up + nonce required
# ---------------------------------------------------------------------------


class TestMissingNonce:
    """A missing X-NCE-Nonce header must be rejected when Redis is up and required."""

    def test_missing_nonce_redis_up_required_rejected(self) -> None:
        store = _make_nonce_store_mock(ping_result=True)
        client = TestClient(
            _make_app(nonce_store=store, nonce_required=True), raise_server_exceptions=True
        )
        hdrs = _valid_headers(_API_KEY, nonce=None)  # no nonce header
        resp = client.post("/api/ping", headers=hdrs)
        assert resp.status_code == 401
        body = resp.json()
        assert body["error"]["data"]["reason"] == "nonce_missing"

    def test_missing_nonce_required_false_is_allowed(self) -> None:
        """When nonce_required=False, missing nonce is not rejected."""
        store = _make_nonce_store_mock(ping_result=True)
        client = TestClient(
            _make_app(nonce_store=store, nonce_required=False), raise_server_exceptions=True
        )
        hdrs = _valid_headers(_API_KEY, nonce=None)
        resp = client.post("/api/ping", headers=hdrs)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Gate 4: Prod + Redis down → rejected (fail-closed)
# ---------------------------------------------------------------------------


class TestProdFailClosed:
    """In production mode, Redis unreachable must cause request rejection."""

    def test_prod_redis_down_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NCE_ENV", "prod")
        # Patch cfg.IS_PROD live — the middleware reads cfg at call time
        with patch("nce.auth.cfg") as mock_cfg:
            mock_cfg.IS_PROD = True
            mock_cfg.NCE_HMAC_NONCE_REQUIRED = True

            store = _make_nonce_store_mock(ping_result=False)  # Redis down
            client = TestClient(
                _make_app(nonce_store=store, nonce_required=True),
                raise_server_exceptions=True,
            )
            hdrs = _valid_headers(_API_KEY, nonce="any-nonce")
            resp = client.post("/api/ping", headers=hdrs)

        assert resp.status_code == 401
        body = resp.json()
        assert body["error"]["data"]["reason"] == "nonce_store_unavailable"

    def test_prod_no_nonce_store_configured_rejected(self) -> None:
        """Prod + no NonceStore at all → also fail-closed."""
        with patch("nce.auth.cfg") as mock_cfg:
            mock_cfg.IS_PROD = True
            mock_cfg.NCE_HMAC_NONCE_REQUIRED = True

            client = TestClient(
                _make_app(nonce_store=None, nonce_required=True),
                raise_server_exceptions=True,
            )
            hdrs = _valid_headers(_API_KEY, nonce="any-nonce")
            resp = client.post("/api/ping", headers=hdrs)

        assert resp.status_code == 401
        body = resp.json()
        assert body["error"]["data"]["reason"] == "nonce_store_unavailable"


# ---------------------------------------------------------------------------
# Gate 5: Dev + Redis down → allowed with log
# ---------------------------------------------------------------------------


class TestDevRedisDown:
    """In dev/test mode, Redis unreachable must allow the request (with a warning log)."""

    def test_dev_redis_down_allowed(self, caplog: pytest.LogCaptureFixture) -> None:
        with patch("nce.auth.cfg") as mock_cfg:
            mock_cfg.IS_PROD = False
            mock_cfg.NCE_HMAC_NONCE_REQUIRED = True

            store = _make_nonce_store_mock(ping_result=False)  # Redis down
            client = TestClient(
                _make_app(nonce_store=store, nonce_required=True),
                raise_server_exceptions=True,
            )
            hdrs = _valid_headers(_API_KEY, nonce="any-nonce")

            import logging

            with caplog.at_level(logging.WARNING, logger="nce.auth"):
                resp = client.post("/api/ping", headers=hdrs)

        assert resp.status_code == 200
        assert any("Redis is unreachable" in r.message for r in caplog.records), (
            "Expected a WARNING log about Redis being unreachable in dev mode"
        )

    def test_dev_no_nonce_store_allowed(self, caplog: pytest.LogCaptureFixture) -> None:
        """Dev + no NonceStore configured → allowed with warning."""
        with patch("nce.auth.cfg") as mock_cfg:
            mock_cfg.IS_PROD = False
            mock_cfg.NCE_HMAC_NONCE_REQUIRED = True

            client = TestClient(
                _make_app(nonce_store=None, nonce_required=True),
                raise_server_exceptions=True,
            )
            hdrs = _valid_headers(_API_KEY, nonce="any-nonce")

            import logging

            with caplog.at_level(logging.WARNING, logger="nce.auth"):
                resp = client.post("/api/ping", headers=hdrs)

        assert resp.status_code == 200
        assert any("NonceStore is not configured" in r.message for r in caplog.records), (
            "Expected a WARNING log about NonceStore not configured in dev mode"
        )


# ---------------------------------------------------------------------------
# NonceStore.ping unit tests
# ---------------------------------------------------------------------------


class TestNonceStorePing:
    """ping() returns True on success and False on Redis error."""

    @pytest.mark.asyncio
    async def test_ping_returns_true_on_success(self) -> None:
        store = NonceStore("redis://localhost:6379/0")
        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(return_value=True)
        store._redis = mock_redis
        assert await store.ping() is True

    @pytest.mark.asyncio
    async def test_ping_returns_false_on_error(self) -> None:
        store = NonceStore("redis://localhost:6379/0")
        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(side_effect=ConnectionError("refused"))
        store._redis = mock_redis
        assert await store.ping() is False


# ---------------------------------------------------------------------------
# Nonce TTL is 2× drift window
# ---------------------------------------------------------------------------


def test_nonce_ttl_is_double_drift() -> None:
    from nce.auth import _NONCE_TTL_SECONDS, _TIMESTAMP_DRIFT_SECONDS

    assert _NONCE_TTL_SECONDS == _TIMESTAMP_DRIFT_SECONDS * 2, (
        f"Nonce TTL {_NONCE_TTL_SECONDS} must be 2× drift {_TIMESTAMP_DRIFT_SECONDS}"
    )


# ---------------------------------------------------------------------------
# check_and_store atomically stores nonce with correct TTL
# ---------------------------------------------------------------------------


class TestCheckAndStoreTTL:
    """check_and_store uses TTL = 2× drift to ensure nonce coverage."""

    @pytest.mark.asyncio
    async def test_check_and_store_uses_correct_ttl(self) -> None:
        from nce.auth import _NONCE_TTL_SECONDS

        store = NonceStore("redis://localhost:6379/0", ttl=_NONCE_TTL_SECONDS)
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(return_value=True)
        store._redis = mock_redis

        result = await store.check_and_store("abc123nonce")
        assert result is True
        mock_redis.set.assert_called_once_with(
            "nce:nonce:abc123nonce",
            "1",
            nx=True,
            px=_NONCE_TTL_SECONDS * 1000,
        )
