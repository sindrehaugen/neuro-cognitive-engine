"""Unit tests for Module 15 Resources capacity and foundation."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nce.vertical_modules.resources.capacity import (
    do_check_capacity,
    do_resolve_capacity,
)


@pytest.mark.asyncio
async def test_resolve_capacity_defaults_to_zero_utilization(monkeypatch) -> None:
    """Verify that resources with empty attrs default to 0.0 utilization and empty utilized_resources."""
    res_id = uuid4()
    ns_id = uuid4()
    mock_rows = [
        {
            "id": res_id,
            "namespace_id": ns_id,
            "kind": "technician",
            "ref_id": "TECH-01",
            "display_name": "Test Tech",
            "attrs": {},
        }
    ]

    conn = AsyncMock()

    async def fake_fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        q = query.lower()
        if "from resources" in q:
            return mock_rows
        return []

    conn.fetch = AsyncMock(side_effect=fake_fetch)

    @asynccontextmanager
    async def fake_session(_pool: Any, _ns: Any):
        yield conn

    monkeypatch.setattr(
        "nce.vertical_modules.resources.capacity.scoped_pg_session",
        fake_session,
    )

    engine = MagicMock()
    engine.pg_pool = MagicMock()

    params = {
        "namespace_id": str(ns_id),
        "namespace_metadata": {"features": {"resources": True}},
        "window": {"starts_at": "2026-10-01T08:00:00Z", "ends_at": "2026-10-01T17:00:00Z"},
    }

    res = await do_resolve_capacity(engine, params)
    assert res["total_resources"] == 1
    assert len(res["available_resources"]) == 1
    assert res["utilized_resources"] == []
    assert res["utilization_pct"] == 0.0

    # Also test do_check_capacity alias
    res_alias = await do_check_capacity(engine, params)
    assert res_alias["utilization_pct"] == 0.0


@pytest.mark.asyncio
async def test_resolve_capacity_computes_utilization_from_attrs(monkeypatch) -> None:
    """Verify capacity resolution calculates real utilization from utilization_pct and allocated_hours."""
    ns_id = uuid4()
    res1_id = uuid4()
    res2_id = uuid4()

    mock_rows = [
        {
            "id": res1_id,
            "namespace_id": ns_id,
            "kind": "technician",
            "ref_id": "TECH-UTIL",
            "display_name": "Utilized Tech",
            "attrs": json.dumps({"utilization_pct": 80.0}),
        },
        {
            "id": res2_id,
            "namespace_id": ns_id,
            "kind": "technician",
            "ref_id": "TECH-HOURS",
            "display_name": "Hours Tech",
            "attrs": {"allocated_hours": 20, "capacity_hours": 40},  # 50%
        },
    ]

    conn = AsyncMock()

    async def fake_fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        q = query.lower()
        if "from resources" in q:
            return mock_rows
        return []

    conn.fetch = AsyncMock(side_effect=fake_fetch)

    @asynccontextmanager
    async def fake_session(_pool: Any, _ns: Any):
        yield conn

    monkeypatch.setattr(
        "nce.vertical_modules.resources.capacity.scoped_pg_session",
        fake_session,
    )

    engine = MagicMock()
    engine.pg_pool = MagicMock()

    params = {
        "namespace_id": str(ns_id),
        "namespace_metadata": {"features": {"resources": True}},
        "window": {"starts_at": "2026-10-01T08:00:00Z", "ends_at": "2026-10-01T17:00:00Z"},
    }

    res = await do_resolve_capacity(engine, params)
    assert res["total_resources"] == 2
    assert len(res["utilized_resources"]) == 2
    # Average of 80% and 50% = 65%
    assert abs(res["utilization_pct"] - 65.0) < 0.01

    utilized_ids = {u["id"] for u in res["utilized_resources"]}
    assert str(res1_id) in utilized_ids
    assert str(res2_id) in utilized_ids
