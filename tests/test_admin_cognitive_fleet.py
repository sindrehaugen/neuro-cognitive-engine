"""Tests for cognitive (salience-map, LLM payload) and fleet admin helpers."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request

os.environ.setdefault("NCE_MASTER_KEY", "dev-test-key-32chars-long!!")


class AttrDict(dict):
    """asyncpg-ish row allowing key access."""

    __getattr__ = dict.__getitem__


@pytest.mark.asyncio
async def test_fetch_pg_rls_snapshot_fills_missing_tables():
    from nce.admin_routes import ADMIN_FLEET_RLS_TABLES, fetch_pg_rls_snapshot

    conn = AsyncMock()
    conn.fetch = AsyncMock(
        return_value=[
            AttrDict(table_name="memories", rls_enabled=True),
        ]
    )

    snap = await fetch_pg_rls_snapshot(conn)
    assert snap["memories"] is True
    for t in ADMIN_FLEET_RLS_TABLES:
        assert t in snap
        if t != "memories":
            assert snap[t] is False


@pytest.mark.asyncio
async def test_fetch_fleet_overview_page_slug_prefix_sql_args():
    from nce.admin_routes import fetch_fleet_overview_page

    now = datetime.now(timezone.utc)
    row = AttrDict(
        namespace_id=uuid.uuid4(),
        slug="fleet-demo",
        memory_count=7,
        salience_p50=0.62,
        open_contradictions=2,
        consolidation_last_status="completed",
        consolidation_last_finished_at=now - timedelta(days=1),
        quota_entries=[
            {"resource_type": "x", "agent_id": "a", "used_amount": 1, "limit_amount": 10}
        ],
        bridge_active_count=1,
        bridge_next_expiry=now + timedelta(days=3),
        last_event_at=now - timedelta(days=1),
        memory_velocity_7d=[1, 2, 0, 3, 1, 0, 2],
    )

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=AttrDict(total=99))
    conn.fetch = AsyncMock(return_value=[row])

    items, total = await fetch_fleet_overview_page(
        conn,
        slug_prefix="demo",
        page=2,
        limit=25,
        half_life_days=14.5,
    )

    assert total == 99
    assert len(items) == 1
    assert items[0]["slug"] == "fleet-demo"
    assert items[0]["memory_count"] == 7
    assert items[0]["health"]["event_feed"] == "ok"
    assert items[0]["open_contradictions"] == 2
    assert items[0]["consolidation"]["last_status"] == "completed"
    assert items[0]["quota"]["entries"][0]["resource_type"] == "x"
    assert items[0]["bridges"]["active_count"] == 1
    assert items[0]["memory_velocity_7d"] == [1, 2, 0, 3, 1, 0, 2]

    fargs = conn.fetch.await_args.args
    assert fargs[1] == "demo%"
    assert fargs[2:5] == (14.5, 25, 25)


@pytest.mark.asyncio
async def test_fetch_fleet_quota_entries_json_string_parsed():
    from nce.admin_routes import fetch_fleet_overview_page

    quota_json = json.dumps(
        [{"resource_type": "q", "agent_id": "", "used_amount": 0, "limit_amount": 1}]
    )
    row = AttrDict(
        namespace_id=uuid.uuid4(),
        slug="ns",
        memory_count=0,
        salience_p50=None,
        open_contradictions=0,
        consolidation_last_status=None,
        consolidation_last_finished_at=None,
        quota_entries=quota_json,
        bridge_active_count=0,
        bridge_next_expiry=None,
        last_event_at=None,
        memory_velocity_7d=[0, 0, 0, 0, 0, 0, 0],
    )

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=AttrDict(total=1))
    conn.fetch = AsyncMock(return_value=[row])

    items, _total = await fetch_fleet_overview_page(
        conn, slug_prefix=None, page=1, limit=10, half_life_days=30.0
    )
    assert items[0]["quota"]["entries"][0]["resource_type"] == "q"
    assert items[0]["health"]["event_feed"] == "quiet"


@pytest.mark.asyncio
async def test_fetch_salience_map_points_shapes_output():
    from nce.admin_routes import fetch_salience_map_points

    ns = uuid.uuid4()
    mid = uuid.uuid4()
    agent_id = "agent:test/1"

    sal_updated = datetime(2026, 1, 15, tzinfo=timezone.utc)
    mem_created = datetime(2026, 1, 10, tzinfo=timezone.utc)

    conn = AsyncMock()
    conn.fetch = AsyncMock(
        side_effect=[
            [
                AttrDict(
                    memory_id=mid,
                    agent_id=agent_id,
                    salience_score=0.8,
                    updated_at=sal_updated,
                )
            ],
            [
                AttrDict(
                    id=mid,
                    assertion_type="asserted",
                    memory_type="semantic",
                    created_at=mem_created,
                )
            ],
        ]
    )

    points = await fetch_salience_map_points(
        conn,
        namespace_id=ns,
        agent_id=None,
        top_k=10,
        half_life_days=30.0,
    )

    assert len(points) == 1
    p = points[0]
    assert p["memory_id"] == str(mid)
    assert p["agent_id"] == agent_id
    assert p["raw_salience"] == pytest.approx(0.8)
    assert "decayed_salience" in p
    assert p["assertion_type"] == "asserted"
    assert p["memory_type"] == "semantic"
    assert isinstance(p["age_days"], float) and p["age_days"] >= 0.0


@pytest.mark.asyncio
async def test_api_admin_salience_map_requires_namespace_id():
    mock_engine = MagicMock()
    with patch("nce.admin_state.engine", mock_engine):
        from admin_server import api_admin_salience_map

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/admin/salience-map",
                "query_string": b"",
            }
        )
        response = await api_admin_salience_map(request)
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_api_admin_llm_payload_missing_ids():
    mock_engine = MagicMock()
    with patch("nce.admin_state.engine", mock_engine):
        from admin_server import api_admin_llm_payload

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/admin/llm-payload",
                "query_string": b"",
            }
        )
        response = await api_admin_llm_payload(request)
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_api_admin_llm_payload_fetches_when_uri_present():
    ns_id = uuid.uuid4()
    evt_id = uuid.uuid4()
    uri = "llm-artifacts/obj1"

    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_engine.pg_pool.acquire.return_value.__aenter__.return_value = mock_conn
    mock_conn.fetchrow.return_value = {"llm_payload_uri": uri}

    with patch("nce.admin_state.engine", mock_engine):
        with patch(
            "nce.salience.fetch_llm_payload",
            AsyncMock(return_value={"prompt": "x", "response": "y"}),
        ) as fetch_minio:
            from admin_server import api_admin_llm_payload

            qs = f"namespace_id={ns_id}&event_id={evt_id}".encode()
            request = Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/api/admin/llm-payload",
                    "query_string": qs,
                }
            )
            response = await api_admin_llm_payload(request)

    assert response.status_code == 200
    body = json.loads(response.body.decode())
    assert body["llm_payload_uri"] == uri
    assert body["payload"] == {"prompt": "x", "response": "y"}
    fetch_minio.assert_awaited_once_with(uri)


@pytest.mark.asyncio
async def test_api_admin_fleet_overview_includes_rls():
    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_engine.pg_pool.acquire.return_value.__aenter__.return_value = mock_conn

    rls = {"memories": True, "replay_runs": False}
    items = [{"namespace_id": str(uuid.uuid4()), "slug": "a"}]

    with patch("nce.admin_state.engine", mock_engine):
        with patch(
            "nce.admin_handlers.fleet.fetch_pg_rls_snapshot",
            AsyncMock(return_value=rls),
        ):
            with patch(
                "nce.admin_handlers.fleet.fetch_fleet_overview_page",
                AsyncMock(return_value=(items, 1)),
            ):
                from admin_server import api_admin_fleet_overview

                request = Request(
                    {
                        "type": "http",
                        "method": "GET",
                        "path": "/api/admin/fleet-overview",
                        "query_string": b"",
                    }
                )
                response = await api_admin_fleet_overview(request)

    assert response.status_code == 200
    body = json.loads(response.body.decode())
    assert body["rls_tenant_tables"] == rls
    assert body["total"] == 1
    assert body["items"] == items


@pytest.mark.asyncio
async def test_api_admin_bridge_renew_audit_and_dispatch():
    ns = uuid.uuid4()
    bridge = uuid.uuid4()

    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_engine.pg_pool.acquire.return_value.__aenter__.return_value = mock_conn
    mock_conn.fetchrow.return_value = {
        "id": bridge,
        "namespace_id": ns,
        "provider": "gdrive",
    }

    mock_renew = AsyncMock(return_value=None)

    with patch("nce.admin_state.engine", mock_engine):
        with patch("nce.admin_handlers._shared.logger") as log:
            with patch("nce.bridge_renewal.renew_gdrive", mock_renew):
                from admin_server import api_admin_bridge_renew

                request = Request(
                    {
                        "type": "http",
                        "method": "POST",
                        "path": f"/api/admin/bridges/{bridge}/renew",
                        "path_params": {"bridge_id": str(bridge)},
                        "query_string": b"",
                    }
                )
                response = await api_admin_bridge_renew(request)

    assert response.status_code == 200
    body = json.loads(response.body.decode())
    assert body["action"] == "renewed_gdrive"
    mock_renew.assert_awaited_once()

    infos = [c.args[0] for c in log.info.call_args_list if c.args]
    assert any("audit bridge_admin_renew_requested" in str(m) for m in infos)
    assert any("audit bridge_admin_renew_succeeded" in str(m) for m in infos)


@pytest.mark.asyncio
async def test_api_admin_memory_boost_calls_engine():
    ns_id = uuid.uuid4()
    mem_id = uuid.uuid4()

    mock_engine = MagicMock()
    mock_engine.boost_memory = AsyncMock(return_value={"status": "success", "boosted_by": 0.2})

    with patch("nce.admin_state.engine", mock_engine):
        from admin_server import api_admin_memory_boost

        request = MagicMock()
        request.json = AsyncMock(
            return_value={
                "namespace_id": str(ns_id),
                "memory_id": str(mem_id),
                "agent_id": "agent:test",
                "factor": 0.35,
            }
        )
        response = await api_admin_memory_boost(request)

    assert response.status_code == 200
    body = json.loads(response.body.decode())
    assert body["status"] == "success"
    mock_engine.boost_memory.assert_awaited_once()
    call_kw = mock_engine.boost_memory.await_args.kwargs
    assert call_kw["namespace_id"] == str(ns_id)
    assert call_kw["memory_id"] == str(mem_id)
    assert call_kw["agent_id"] == "agent:test"
    assert call_kw["factor"] == pytest.approx(0.35)


@pytest.mark.asyncio
async def test_api_admin_contradictions_recent_lists_items():
    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_engine.pg_pool.acquire.return_value.__aenter__.return_value = mock_conn

    sample = [
        {
            "id": str(uuid.uuid4()),
            "namespace_id": str(uuid.uuid4()),
            "namespace_slug": "alpha",
            "detected_at": "2026-01-01T00:00:00+00:00",
            "confidence": 0.91,
            "detection_path": "nli",
            "memory_a_id": str(uuid.uuid4()),
            "memory_b_id": str(uuid.uuid4()),
        }
    ]

    with patch("nce.admin_state.engine", mock_engine):
        with patch(
            "nce.admin_handlers.fleet.fetch_recent_open_contradictions",
            AsyncMock(return_value=sample),
        ):
            from admin_server import api_admin_contradictions_recent

            request = Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/api/admin/contradictions/recent",
                    "query_string": b"limit=5",
                }
            )
            response = await api_admin_contradictions_recent(request)

    assert response.status_code == 200
    body = json.loads(response.body.decode())
    assert body["limit"] == 5
    assert body["items"] == sample


@pytest.mark.asyncio
async def test_api_admin_namespace_bridges_requires_uuid():
    mock_engine = MagicMock()
    with patch("nce.admin_state.engine", mock_engine):
        from admin_server import api_admin_namespace_bridges

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/admin/namespaces/not-a-uuid/bridges",
                "path_params": {"namespace_id": "not-a-uuid"},
                "query_string": b"",
            }
        )
        response = await api_admin_namespace_bridges(request)

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_api_admin_events_include_details():
    ns = uuid.uuid4()
    now = datetime.now(timezone.utc)
    row = AttrDict(
        id=uuid.uuid4(),
        namespace_id=ns,
        agent_id="system",
        event_type="consolidation_run",
        event_seq=1,
        occurred_at=now,
        parent_event_id=None,
        params='{"abstraction": "x", "confidence": 0.9}',
        result_summary=None,
        llm_payload_uri="s3://x",
    )

    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_engine.pg_pool.acquire.return_value.__aenter__.return_value = mock_conn
    mock_conn.fetchrow = AsyncMock(return_value=AttrDict(total=1))
    mock_conn.fetch = AsyncMock(return_value=[row])

    with patch("nce.admin_state.engine", mock_engine):
        from admin_server import api_admin_events

        qs = f"namespace_id={ns}&include_details=1&limit=5".encode()
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/admin/events",
                "query_string": qs,
            }
        )
        response = await api_admin_events(request)

    assert response.status_code == 200
    body = json.loads(response.body.decode())
    assert body["total"] == 1
    item = body["items"][0]
    assert item["params"]["abstraction"] == "x"
    assert item["params"]["confidence"] == pytest.approx(0.9)
    assert item["llm_payload_uri"] == "s3://x"
