"""Batch 54 — config time-travel & rollback (V.6).

Unit tests (no Docker) for:
  1. GET /api/admin/settings/effective?as_of=T reconstructs the exact past
     non-secret config by folding ordered ``config_changed`` WORM events over the
     env/default baseline.
  2. POST /api/admin/settings/rollback {dry_run:true} returns the correct inverse
     diff, skips prod-locked keys, and flags secrets (never fabricates them).
  3. MCP explain_config_change(key) returns a key's full change history.

These exercise the real folding/diff code paths against a mocked asyncpg pool;
the event rows are crafted to mirror what ``api_admin_settings_patch`` writes.
"""

from __future__ import annotations

import datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.admin_handlers import settings as settings_handlers

UTC = datetime.timezone.utc


def _row(event_type: str, params: dict, *, seq: int, occurred_at: datetime.datetime) -> dict:
    """Build an event_log row as asyncpg would return it (params as a dict)."""
    return {
        "event_id": f"00000000-0000-4000-8000-{seq:012d}",
        "event_type": event_type,
        "agent_id": params.get("actor", "admin"),
        "params": params,
        "event_seq": seq,
        "occurred_at": occurred_at,
    }


def _make_engine(fetch_results):
    """Return a mock engine whose pool.acquire() conn.fetch yields the given results.

    *fetch_results* is a list, one entry per expected ``conn.fetch`` call; each entry
    is itself the list of rows that call should return.
    """
    conn = AsyncMock()
    conn.fetch.side_effect = list(fetch_results)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    engine = MagicMock()
    engine.pg_pool.acquire.return_value = ctx
    engine.redis_client = None
    return engine, conn


def _request(query: dict | None = None, body: dict | None = None):
    """Minimal request shim exposing the attrs the handlers actually touch."""
    req = MagicMock()
    req.query_params = query or {}
    req.state = SimpleNamespace(namespace_ctx=None)
    req.headers = {}
    if body is not None:
        req.json = AsyncMock(return_value=body)
    return req


# ---------------------------------------------------------------------------
# 1. effective?as_of=T reconstructs the exact past non-secret config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_effective_as_of_reconstructs_past_config():
    now = datetime.datetime.now(UTC)
    t1 = now - datetime.timedelta(days=3)
    t2 = now - datetime.timedelta(days=2)
    t3 = now - datetime.timedelta(days=1)  # AFTER the as_of cutoff

    as_of = (now - datetime.timedelta(days=1, hours=12)).isoformat()

    # Sequence of config_changed events. Only events with occurred_at <= as_of
    # are folded — the SQL cutoff is mimicked by the test fetch below.
    events = [
        _row(
            "config_changed",
            {
                "actor": "admin",
                "reason": "raise limit",
                "changes": {"NCE_ADMIN_HTTP_RATE_LIMIT": {"old_value": 100, "new_value": 200}},
            },
            seq=1,
            occurred_at=t1,
        ),
        _row(
            "config_changed",
            {
                "actor": "admin",
                "reason": "raise again",
                "changes": {
                    "NCE_ADMIN_HTTP_RATE_LIMIT": {"old_value": 200, "new_value": 300},
                    "WEBHOOK_RATE_LIMIT": {"old_value": 50, "new_value": 75},
                },
            },
            seq=2,
            occurred_at=t2,
        ),
    ]
    # t3 event would have changed it to 999, but it's after as_of so the handler's
    # SQL (occurred_at <= $1) excludes it; we simulate that by NOT returning it.
    _ = t3

    engine, _conn = _make_engine([events])

    with patch("nce.admin_state.engine", engine):
        resp = await settings_handlers.api_admin_settings_effective(
            _request(query={"as_of": as_of})
        )

    assert resp.status_code == 200
    data = json.loads(bytes(resp.body).decode("utf-8"))
    eff = data["effective"]
    # Folded forward to the last value at/just before T:
    assert eff["NCE_ADMIN_HTTP_RATE_LIMIT"] == 300
    assert eff["WEBHOOK_RATE_LIMIT"] == 75
    # A key never touched keeps its env/default baseline (not the rolled value).
    assert "NCE_QUOTAS_ENABLED" in eff


@pytest.mark.asyncio
async def test_effective_as_of_never_exposes_secret_value():
    now = datetime.datetime.now(UTC)
    as_of = now.isoformat()
    events = [
        _row(
            "config_changed",
            {
                "actor": "admin",
                "changes": {
                    # secrets are redacted to lifecycle tokens in the WORM event
                    "NCE_GEMINI_API_KEY": {"old_value": "••••unset", "new_value": "••••set"}
                },
            },
            seq=1,
            occurred_at=now - datetime.timedelta(hours=1),
        ),
    ]
    engine, _conn = _make_engine([events])

    with patch("nce.admin_state.engine", engine):
        resp = await settings_handlers.api_admin_settings_effective(
            _request(query={"as_of": as_of})
        )

    data = json.loads(bytes(resp.body).decode("utf-8"))
    # The reconstructed secret is ONLY the masked token — never a real value.
    assert data["effective"]["NCE_GEMINI_API_KEY"] == "••••set"


# ---------------------------------------------------------------------------
# 2. rollback dry_run returns the correct inverse diff (+ guardrails)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_dry_run_inverse_diff_and_guardrails():
    now = datetime.datetime.now(UTC)
    as_of = (now - datetime.timedelta(days=1)).isoformat()

    # Past events (folded for the as_of reconstruction):
    past_events = [
        _row(
            "config_changed",
            {
                "actor": "admin",
                "changes": {"NCE_ADMIN_HTTP_RATE_LIMIT": {"old_value": 100, "new_value": 120}},
            },
            seq=1,
            occurred_at=now - datetime.timedelta(days=2),
        ),
    ]
    # Current DB overrides (second fetch in the rollback handler): the live value
    # differs from the reconstructed past (200 now vs 120 at T) so the inverse
    # change-set must restore 120. A prod-locked guardrail and a secret are also
    # overridden to exercise the skip/flag paths.
    current_overrides = [
        {
            "key": "NCE_ADMIN_HTTP_RATE_LIMIT",
            "value": "200",
            "has_secret": False,
            "is_secret": False,
        },
        {
            "key": "NCE_BYPASS_WORM",
            "value": "true",
            "has_secret": False,
            "is_secret": False,
        },
        {
            "key": "NCE_GEMINI_API_KEY",
            "value": None,
            "has_secret": True,
            "is_secret": True,
        },
    ]

    engine, _conn = _make_engine([past_events, current_overrides])

    with patch("nce.admin_state.engine", engine):
        resp = await settings_handlers.api_admin_settings_rollback(
            _request(body={"as_of": as_of, "dry_run": True})
        )

    assert resp.status_code == 200
    data = json.loads(bytes(resp.body).decode("utf-8"))
    assert data["dry_run"] is True

    # Inverse diff restores the non-secret HOT key from its current 200 back to 120.
    assert data["diff"]["NCE_ADMIN_HTTP_RATE_LIMIT"] == {
        "old_value": 200,
        "new_value": 120,
    }
    # prod-locked guardrail is never silently re-enabled — it is skipped.
    assert "NCE_BYPASS_WORM" in data["skipped_prod_locked"]
    assert data["skipped_prod_locked"]["NCE_BYPASS_WORM"]["reason"] == "prod_locked"
    # secret that changed since T is flagged for manual re-entry, not fabricated.
    assert "NCE_GEMINI_API_KEY" in data["flagged_secrets"]
    flagged = data["flagged_secrets"]["NCE_GEMINI_API_KEY"]
    assert flagged["reason"] == "secret_rotated_since_as_of"
    # Only the masked lifecycle token is ever present — never a real secret value.
    assert flagged["current_value"] == "••••set"
    # The secret is never auto-applied: it must not appear in the apply diff.
    assert "NCE_GEMINI_API_KEY" not in data["diff"]


@pytest.mark.asyncio
async def test_rollback_requires_as_of():
    engine, _conn = _make_engine([])
    with patch("nce.admin_state.engine", engine):
        resp = await settings_handlers.api_admin_settings_rollback(_request(body={"dry_run": True}))
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 3. explain_config_change(key) returns the change history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explain_config_change_returns_history():
    now = datetime.datetime.now(UTC)
    events = [
        _row(
            "config_changed",
            {
                "actor": "alice",
                "reason": "bump",
                "changes": {
                    "NCE_ADMIN_HTTP_RATE_LIMIT": {"old_value": 100, "new_value": 150},
                    "WEBHOOK_RATE_LIMIT": {"old_value": 10, "new_value": 20},
                },
            },
            seq=1,
            occurred_at=now - datetime.timedelta(days=2),
        ),
        _row(
            "config_changed",
            {
                "actor": "bob",
                "reason": "bump again",
                "changes": {"NCE_ADMIN_HTTP_RATE_LIMIT": {"old_value": 150, "new_value": 175}},
            },
            seq=2,
            occurred_at=now - datetime.timedelta(days=1),
        ),
    ]
    engine, _conn = _make_engine([events])

    out = await settings_handlers.handle_explain_config_change(
        engine, {"key": "NCE_ADMIN_HTTP_RATE_LIMIT"}
    )
    data = json.loads(out)

    assert data["key"] == "NCE_ADMIN_HTTP_RATE_LIMIT"
    assert data["change_count"] == 2  # only the two entries touching this key
    assert [h["new_value"] for h in data["history"]] == [150, 175]
    assert [h["actor"] for h in data["history"]] == ["alice", "bob"]
    # The unrelated key's change must NOT appear in this key's history.
    assert all("WEBHOOK_RATE_LIMIT" not in json.dumps(h) for h in data["history"])


@pytest.mark.asyncio
async def test_explain_config_change_unknown_key():
    engine, _conn = _make_engine([])
    out = await settings_handlers.handle_explain_config_change(engine, {"key": "NOPE"})
    data = json.loads(out)
    assert "error" in data
