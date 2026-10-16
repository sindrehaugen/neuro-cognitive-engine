"""Unit tests for Dynamics 365 MCP tool handlers."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.vertical_modules.dynamics365.mcp_handlers import (
    handle_d365_case_stress_report,
    handle_d365_list_sla_breaches,
    handle_d365_netbox_mappings,
    handle_d365_query_case,
    handle_d365_sync_now,
)

_NS = "00000000-0000-4000-8000-000000000001"


@pytest.mark.asyncio
async def test_handle_d365_query_case_closes_redis_client_on_success() -> None:
    redis_mock = MagicMock()
    redis_mock.aclose = AsyncMock()

    mock_client = MagicMock()
    mock_client.get_entity = AsyncMock(
        return_value={"incidentid": "case-1", "ticketnumber": "CAS-1001"}
    )

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[])
    engine = SimpleNamespace(pg_pool=MagicMock())

    with (
        patch("redis.asyncio.from_url", return_value=redis_mock),
        patch(
            "nce.vertical_modules.dynamics365.auth.DataverseTokenManager.get_access_token",
            new=AsyncMock(return_value="tok-123"),
        ),
        patch(
            "nce.vertical_modules.dynamics365.client.DataverseClient",
            return_value=mock_client,
        ),
        patch("nce.db_utils.scoped_pg_session") as mock_scoped,
    ):
        mock_scoped.return_value.__aenter__.return_value = mock_conn
        raw = await handle_d365_query_case(
            engine,  # type: ignore[arg-type]
            {
                "namespace_id": _NS,
                "case_id": "case-1",
                "include_notes": False,
                "include_activities": False,
            },
        )

    payload = json.loads(raw)
    assert payload["case"]["ticketnumber"] == "CAS-1001"
    redis_mock.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_d365_sync_now_closes_redis_client_on_error() -> None:
    redis_mock = MagicMock()
    redis_mock.aclose = AsyncMock()
    engine = SimpleNamespace(pg_pool=MagicMock())

    with (
        patch("redis.asyncio.from_url", return_value=redis_mock),
        patch(
            "nce.vertical_modules.dynamics365.auth.DataverseTokenManager.get_access_token",
            new=AsyncMock(side_effect=RuntimeError("token failure")),
        ),
    ):
        raw = await handle_d365_sync_now(
            engine,  # type: ignore[arg-type]
            {"namespace_id": _NS},
        )

    payload = json.loads(raw)
    assert "token failure" in payload["error"]
    redis_mock.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_d365_case_stress_report_parses_string_empathic_tensor() -> None:
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(
        side_effect=[
            [{"subject_label": "Incident:CAS-100"}],
            [
                {
                    "empathic_tensor": "[0.1, 0.2, 0.3, 0.4, 0.5, 8.25, 0.0]",
                    "created_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
                },
                {
                    "empathic_tensor": [0.1, 0.2, 0.3, 0.4, 0.5, 7.75, 0.0],
                    "created_at": datetime(2026, 6, 2, tzinfo=timezone.utc),
                },
                {
                    "empathic_tensor": "[0.0, 0.0, 0.0, 0.0, 0.0, 8.0, 0.0]",
                    "created_at": datetime(2026, 6, 3, tzinfo=timezone.utc),
                },
            ],
        ]
    )
    engine = SimpleNamespace(pg_pool=MagicMock())

    with patch("nce.db_utils.scoped_pg_session") as mock_scoped:
        mock_scoped.return_value.__aenter__.return_value = mock_conn
        raw = await handle_d365_case_stress_report(
            engine,  # type: ignore[arg-type]
            {"namespace_id": _NS, "account_name": "Acme"},
        )

    payload = json.loads(raw)
    assert payload["frustration_trend"] == [8.25, 7.75, 8.0]
    assert payload["avg_frustration"] == 8.0
    assert payload["burnout_alert"] is True


@pytest.mark.asyncio
async def test_handle_d365_list_sla_breaches_decodes_json_string_params() -> None:
    event_id = uuid.uuid4()
    ts = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(
        return_value=[
            {
                "id": event_id,
                "agent_id": "d365-watcher",
                "event_type": "d365_sla_breach",
                "params": '{"incident_id": "inc-42", "overdue_minutes": 45}',
                "created_at": ts,
            }
        ]
    )
    engine = SimpleNamespace(pg_pool=MagicMock())

    with patch("nce.db_utils.scoped_pg_session") as mock_scoped:
        mock_scoped.return_value.__aenter__.return_value = mock_conn
        raw = await handle_d365_list_sla_breaches(
            engine,  # type: ignore[arg-type]
            {"namespace_id": _NS, "since": "2026-06-01T00:00:00Z"},
        )

    payload = json.loads(raw)
    assert payload["count"] == 1
    assert payload["breaches"][0]["params"] == {
        "incident_id": "inc-42",
        "overdue_minutes": 45,
    }


@pytest.mark.asyncio
async def test_handle_d365_netbox_mappings_sets_namespace_context() -> None:
    row_id = uuid.uuid4()
    ts = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(
        return_value=[
            {
                "id": row_id,
                "d365_entity_type": "account",
                "d365_entity_name": "Acme Corp",
                "d365_entity_id": "acc-1",
                "nb_entity_type": "tenant",
                "nb_entity_name": "Acme",
                "nb_entity_slug": "acme",
                "nb_entity_id": 10,
                "match_method": "exact",
                "match_confidence": 0.98,
                "confirmed": True,
                "created_at": ts,
                "updated_at": ts,
            }
        ]
    )

    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    engine = SimpleNamespace(pg_pool=pool)

    with patch("nce.auth.set_namespace_context", new_callable=AsyncMock) as mock_set_ns:
        raw = await handle_d365_netbox_mappings(
            engine,  # type: ignore[arg-type]
            {"namespace_id": _NS, "entity_type": "account", "confirmed_only": True},
        )

    mock_set_ns.assert_awaited_once_with(mock_conn, _NS)
    payload = json.loads(raw)
    assert payload["count"] == 1
    assert payload["mappings"][0]["d365_entity_name"] == "Acme Corp"
