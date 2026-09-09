"""The chain verifier must not append to a chain it has already declared corrupt.

Appending a ``chain_verification_failed`` event into the broken namespace is
self-amplifying: the new event's predecessor is broken, so it can never verify either,
which guarantees the next tick fails and appends again.

Observed on the deployed stack 2026-09-07..09. Namespace
``wormpinned-teardown-probe-76199e4f98c8`` had exactly ONE bad row -- ``event_seq=1``,
``chain_hash IS NULL``, written directly by ``agent="a"`` -- and had accumulated **eight**
``chain_verification_failed`` events plus an unthrottled CRITICAL alert on every tick for
two days. The detector had become the majority contributor to the corruption it reported.

Two deliberate choices, because the failure mode this suite keeps finding is a test that
cannot fail:

* These tests **invoke ``_chain_verification_tick`` itself**, so the guard in ``cron.py``
  is the code under test. An earlier draft hand-called a local fake and asserted the
  fake's behaviour -- it passed without executing a single line of the production branch.
* ``_StatefulConn.fetchval`` answers the "already recorded?" query from the events
  actually appended so far, rather than returning a constant. With a fixed return value
  the duplicate-suppression assertion would be unfalsifiable.
"""

from __future__ import annotations

import contextlib
from typing import Any
from uuid import UUID, uuid4

import pytest


class _StatefulConn:
    """asyncpg-shaped connection whose EXISTS query reflects the appends made so far."""

    def __init__(self) -> None:
        self.appended: list[dict[str, Any]] = []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "COALESCE(max(event_seq), 0)" in sql:
            return len(self.appended)
        if "chain_verification_failed" in sql and "EXISTS" in sql:
            wanted = str(args[1])
            return any(str(e["params"].get("first_break")) == wanted for e in self.appended)
        return None

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        return []

    async def execute(self, sql: str, *args: Any) -> str:
        return "OK"


def _install(monkeypatch: pytest.MonkeyPatch, ns_id: UUID, conn: _StatefulConn) -> list[str]:
    """Point the tick at one namespace and one stateful connection. Returns alert keys."""

    from nce import cron

    alert_keys: list[str] = []

    class _Rows:
        async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
            return [{"id": ns_id}]

        async def fetchval(self, sql: str, *args: Any) -> Any:
            return None

    @contextlib.asynccontextmanager
    async def fake_unmanaged(pool: Any, *, site: str = ""):
        yield _Rows()

    @contextlib.asynccontextmanager
    async def fake_scoped(pool: Any, namespace_id: Any):
        yield conn

    async def fake_verify(_conn: Any, *, namespace_id: Any, start_seq: int) -> dict[str, Any]:
        return {"valid": False, "first_break": 1, "reason": "hash mismatch"}

    async def fake_append(
        *, conn: Any, namespace_id: Any, agent_id: str, event_type: str, params: dict
    ) -> None:
        conn.appended.append({"event_type": event_type, "params": params})

    async def fake_alert(key: str, title: str, message: str) -> None:
        alert_keys.append(key)

    # The tick takes a Redis cron lock first; without these it returns before reaching
    # the branch under test. (Finding this is how I confirmed the test really invokes the
    # tick rather than a local stand-in.)
    async def fake_acquire(name: str, ttl: int) -> object:
        return object()

    async def fake_release(lock: Any) -> None:
        return None

    monkeypatch.setattr(cron, "acquire_cron_lock", fake_acquire)
    monkeypatch.setattr(cron, "release_cron_lock", fake_release)
    monkeypatch.setattr(cron, "unmanaged_pg_connection", fake_unmanaged)
    monkeypatch.setattr(cron, "scoped_pg_session", fake_scoped)
    monkeypatch.setattr("nce.event_log.verify_merkle_chain", fake_verify)
    monkeypatch.setattr("nce.event_log.append_event", fake_append)
    monkeypatch.setattr(cron, "_dispatch_throttled_alert", fake_alert)
    return alert_keys


@pytest.mark.asyncio
async def test_repeated_ticks_record_the_same_break_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two real tick invocations over one broken chain append exactly one audit event."""

    from nce import cron

    ns_id = uuid4()
    conn = _StatefulConn()
    _install(monkeypatch, ns_id, conn)

    await cron._chain_verification_tick(object())
    assert len(conn.appended) == 1, "the first tick must record the break once"

    await cron._chain_verification_tick(object())
    assert len(conn.appended) == 1, (
        "a second tick appended another chain_verification_failed event -- this is the "
        "self-amplifying loop that turned one bad row into eight events over two days"
    )

    await cron._chain_verification_tick(object())
    assert len(conn.appended) == 1, "the suppression must hold across further ticks"


@pytest.mark.asyncio
async def test_a_new_break_is_still_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard keys on ``first_break``, so a genuinely different break still records.

    Without this, "stop appending" could be implemented as "never append twice per
    namespace", which would silently hide a second, real corruption.
    """

    from nce import cron

    ns_id = uuid4()
    conn = _StatefulConn()
    conn.appended.append(
        {"event_type": "chain_verification_failed", "params": {"first_break": 7}}
    )
    _install(monkeypatch, ns_id, conn)

    await cron._chain_verification_tick(object())
    assert len(conn.appended) == 2, (
        "a break at a different event_seq than the one already recorded must still be "
        "appended; suppressing it would hide a second corruption"
    )
