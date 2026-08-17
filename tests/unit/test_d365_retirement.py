"""Unit tests for D365 change-tracking delete detection + edge/node retirement.

Covers `NCE_D365_CHANGE_TRACKING_ENABLED`: the vertical consumes Dataverse
deltaLinks, hard-deletes kg_edges/kg_nodes tagged with a removed record's
`d365_source_id`, and persists the new deltaLink. See
`nce/vertical_modules/dynamics365/sync.py`.
"""

from __future__ import annotations

import uuid

import pytest

from nce.config import cfg
from nce.vertical_modules.dynamics365.sync import DataverseSyncEngine


class _FakeClient:
    def __init__(self, removed_by_entity: dict[str, list[str]] | None = None) -> None:
        self.removed_by_entity = removed_by_entity or {}
        self.track_calls: list[str] = []

    async def track_changes(self, entity_set, *, select=None, delta_link=None, page_size=1000):
        self.track_calls.append(entity_set)
        removed = self.removed_by_entity.get(entity_set, [])
        return [], removed, f"https://org/api/data/v9.2/{entity_set}?$deltatoken=NEW"


class _FakeConn:
    def __init__(self, delta_row=None) -> None:
        self._delta_row = delta_row
        self.executes: list[tuple[str, tuple]] = []

    async def fetchrow(self, _sql, *_args):
        return self._delta_row

    async def execute(self, sql, *args):
        self.executes.append((sql, args))
        return "DELETE 1"


def _engine(conn=None, client=None) -> DataverseSyncEngine:
    return DataverseSyncEngine(conn or _FakeConn(), uuid.uuid4(), client or _FakeClient())


@pytest.mark.asyncio
async def test_retire_source_deletes_from_edges_and_nodes():
    conn = _FakeConn()
    eng = _engine(conn=conn)
    deleted = await eng._retire_source(["g1", "g2"])
    deletes = [s for (s, _a) in conn.executes if s.strip().startswith("DELETE")]
    assert len(deletes) == 2
    assert any("kg_edges" in s for s in deletes)
    assert any("kg_nodes" in s for s in deletes)
    assert deleted == 2  # "DELETE 1" parsed per statement


@pytest.mark.asyncio
async def test_retire_source_empty_is_noop():
    conn = _FakeConn()
    eng = _engine(conn=conn)
    assert await eng._retire_source([]) == 0
    assert conn.executes == []


@pytest.mark.asyncio
async def test_detect_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(cfg, "NCE_D365_CHANGE_TRACKING_ENABLED", False)
    client = _FakeClient(removed_by_entity={"accounts": ["g1"]})
    eng = _engine(conn=_FakeConn(), client=client)
    out = await eng.detect_and_retire_deletions()
    assert out == {"enabled": False}
    assert client.track_calls == []  # no Dataverse calls when disabled


@pytest.mark.asyncio
async def test_detect_enabled_retires_removed_and_saves_delta(monkeypatch):
    monkeypatch.setattr(cfg, "NCE_D365_CHANGE_TRACKING_ENABLED", True)
    client = _FakeClient(removed_by_entity={"accounts": ["g1", "g2"]})
    conn = _FakeConn(delta_row=None)
    eng = _engine(conn=conn, client=client)
    out = await eng.detect_and_retire_deletions()
    assert out["enabled"] is True
    assert out["removed"] == 2
    assert len(client.track_calls) == 9  # all tracked entity-sets polled
    deletes = [s for (s, _a) in conn.executes if s.strip().startswith("DELETE")]
    assert len(deletes) == 2  # one kg_edges + one kg_nodes for the accounts removals
    saves = [s for (s, _a) in conn.executes if "d365_delta_tokens" in s]
    assert len(saves) == 9  # new deltaLink persisted per entity-set
