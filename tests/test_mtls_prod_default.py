"""Boot-time zero-trust transport guard — ``assert_server_mtls_or_acknowledged``.

Batch 115 (security). The admin / a2a server lifespans must refuse to start in
production when their mTLS middleware is disabled, *unless* an operator has
explicitly acknowledged the weakened posture via
``NCE_MTLS_ACKNOWLEDGE_DISABLED=true`` — in which case the server boots but a
``CRITICAL`` log line and an immutable WORM audit event are emitted.

These are unit-level tests that exercise the boot-guard helper directly (no
Docker, no real DB). The acknowledged path's WORM write is verified against a
mocked ``append_event`` so we assert the call shape (system namespace, no
secrets/PII) without touching Postgres.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from nce.config import cfg
from nce.mtls import (
    _SYSTEM_NAMESPACE,
    MTLSNotConfiguredError,
    assert_server_mtls_or_acknowledged,
)

pytestmark = pytest.mark.asyncio


class _FakeAcquire:
    """Async context manager standing in for ``pool.acquire()`` -> connection."""

    def __init__(self, conn) -> None:
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _FakeConn:
    """Minimal asyncpg-connection stub exposing only ``.transaction()``."""

    def transaction(self):
        @asynccontextmanager
        async def _tx():
            yield

        return _tx()


class _FakePool:
    """Minimal asyncpg-pool stub: ``acquire(timeout=...)`` -> _FakeConn."""

    def __init__(self) -> None:
        self.conn = _FakeConn()

    def acquire(self, timeout: float | None = None):  # noqa: ARG002
        return _FakeAcquire(self.conn)


# ---------------------------------------------------------------------------
# 1. Prod + mTLS unconfigured + UNACKNOWLEDGED -> boot raises.
# ---------------------------------------------------------------------------


async def test_prod_mtls_disabled_unacknowledged_raises(monkeypatch):
    monkeypatch.setattr(cfg, "IS_PROD", True)
    monkeypatch.setattr(cfg, "NCE_MTLS_ACKNOWLEDGE_DISABLED", False)

    with pytest.raises(MTLSNotConfiguredError):
        await assert_server_mtls_or_acknowledged(service="admin", mtls_enabled=False, pg_pool=None)


async def test_prod_mtls_disabled_unacknowledged_raises_a2a(monkeypatch):
    monkeypatch.setattr(cfg, "IS_PROD", True)
    monkeypatch.setattr(cfg, "NCE_MTLS_ACKNOWLEDGE_DISABLED", False)

    with pytest.raises(MTLSNotConfiguredError):
        await assert_server_mtls_or_acknowledged(service="a2a", mtls_enabled=False, pg_pool=None)


# ---------------------------------------------------------------------------
# 2. Prod + mTLS disabled + ACKNOWLEDGED -> boots, CRITICAL log + WORM event.
# ---------------------------------------------------------------------------


async def test_prod_mtls_disabled_acknowledged_boots_with_critical_and_worm(monkeypatch, caplog):
    monkeypatch.setattr(cfg, "IS_PROD", True)
    monkeypatch.setattr(cfg, "NCE_MTLS_ACKNOWLEDGE_DISABLED", True)

    append_mock = AsyncMock()
    monkeypatch.setattr("nce.event_log.append_event", append_mock)

    pool = _FakePool()
    with caplog.at_level(logging.CRITICAL, logger="nce.mtls"):
        # Must NOT raise — acknowledgement permits boot to continue.
        await assert_server_mtls_or_acknowledged(service="admin", mtls_enabled=False, pg_pool=pool)

    # CRITICAL log emitted.
    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert critical, "expected a CRITICAL log on the acknowledged path"
    assert any("mTLS DISABLED" in r.getMessage() for r in critical)

    # WORM event appended exactly once with the system namespace and no secrets.
    append_mock.assert_awaited_once()
    kwargs = append_mock.await_args.kwargs
    assert kwargs["namespace_id"] == _SYSTEM_NAMESPACE
    assert kwargs["agent_id"] == "system"
    assert kwargs["event_type"] == "config_changed"
    params = kwargs["params"]
    assert params["actor"] == "system"
    assert params["reason"] == "mtls_disabled_acknowledged"
    # No raw secrets / cert material / PII anywhere in the recorded payload.
    flat = repr(params).lower()
    for forbidden in ("key", "secret", "password", "token", "begin", "private"):
        assert forbidden not in flat, f"forbidden token {forbidden!r} in WORM params"


async def test_acknowledged_without_pool_still_boots_and_logs(monkeypatch, caplog):
    """No pool -> still boots, still logs CRITICAL, but writes no WORM event."""
    monkeypatch.setattr(cfg, "IS_PROD", True)
    monkeypatch.setattr(cfg, "NCE_MTLS_ACKNOWLEDGE_DISABLED", True)

    append_mock = AsyncMock()
    monkeypatch.setattr("nce.event_log.append_event", append_mock)

    with caplog.at_level(logging.CRITICAL, logger="nce.mtls"):
        await assert_server_mtls_or_acknowledged(service="a2a", mtls_enabled=False, pg_pool=None)

    assert any(r.levelno == logging.CRITICAL for r in caplog.records)
    append_mock.assert_not_awaited()


async def test_acknowledged_audit_failure_does_not_block_boot(monkeypatch, caplog):
    """Once acknowledged, a WORM-write failure is logged but never blocks boot."""
    monkeypatch.setattr(cfg, "IS_PROD", True)
    monkeypatch.setattr(cfg, "NCE_MTLS_ACKNOWLEDGE_DISABLED", True)

    append_mock = AsyncMock(side_effect=RuntimeError("db down"))
    monkeypatch.setattr("nce.event_log.append_event", append_mock)

    pool = _FakePool()
    with caplog.at_level(logging.ERROR, logger="nce.mtls"):
        # Must not raise despite the audit-write failure.
        await assert_server_mtls_or_acknowledged(service="admin", mtls_enabled=False, pg_pool=pool)

    assert any(
        "Failed to record mtls_disabled_acknowledged" in r.getMessage() for r in caplog.records
    )


# ---------------------------------------------------------------------------
# 3. Non-prod -> boots silently (no raise, no CRITICAL, no WORM event).
# ---------------------------------------------------------------------------


async def test_non_prod_boots_silently(monkeypatch, caplog):
    monkeypatch.setattr(cfg, "IS_PROD", False)
    monkeypatch.setattr(cfg, "NCE_MTLS_ACKNOWLEDGE_DISABLED", False)

    append_mock = AsyncMock()
    monkeypatch.setattr("nce.event_log.append_event", append_mock)

    with caplog.at_level(logging.CRITICAL, logger="nce.mtls"):
        await assert_server_mtls_or_acknowledged(
            service="admin", mtls_enabled=False, pg_pool=_FakePool()
        )

    assert not [r for r in caplog.records if r.levelno >= logging.CRITICAL]
    append_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# 4. Prod + mTLS ENABLED -> boots silently regardless of the ack flag.
# ---------------------------------------------------------------------------


async def test_prod_mtls_enabled_boots_silently(monkeypatch, caplog):
    monkeypatch.setattr(cfg, "IS_PROD", True)
    monkeypatch.setattr(cfg, "NCE_MTLS_ACKNOWLEDGE_DISABLED", False)

    append_mock = AsyncMock()
    monkeypatch.setattr("nce.event_log.append_event", append_mock)

    with caplog.at_level(logging.CRITICAL, logger="nce.mtls"):
        await assert_server_mtls_or_acknowledged(
            service="a2a", mtls_enabled=True, pg_pool=_FakePool()
        )

    assert not [r for r in caplog.records if r.levelno >= logging.CRITICAL]
    append_mock.assert_not_awaited()
