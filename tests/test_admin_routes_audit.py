"""Admin list validation, pagination bounds, and handler security filters."""

from __future__ import annotations

import json
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request

os.environ.setdefault("NCE_MASTER_KEY", "dev-test-key-32chars-long!!")


def test_offset_from_page_limit_rejects_deep_scan() -> None:
    from nce.admin_routes import ADMIN_MAX_ROWS_SKIP, offset_from_page_limit

    max_page = (ADMIN_MAX_ROWS_SKIP // 50) + 1
    with pytest.raises(ValueError):
        offset_from_page_limit(max_page + 1, 50)


def test_sanitize_filters() -> None:
    from nce.admin_routes import (
        sanitize_event_type_filter,
        sanitize_resource_type_filter,
        sanitize_slug_prefix_filter,
        sanitize_task_name_filter,
        validate_dlq_status,
    )

    assert sanitize_event_type_filter("x" * 200)[1] is not None
    assert sanitize_slug_prefix_filter("bad!")[1] is not None
    assert sanitize_resource_type_filter("bad type")[1] is not None
    assert sanitize_task_name_filter(";drop")[1] is not None
    assert validate_dlq_status("nope")[1] is not None


@pytest.mark.asyncio
async def test_api_admin_dlq_bad_status_and_task_name() -> None:
    mock_engine = MagicMock()
    with patch("nce.admin_state.engine", mock_engine):
        from admin_server import api_admin_dlq_list

        r = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/admin/dlq",
                "query_string": b"status=evil",
            }
        )
        resp = await api_admin_dlq_list(r)
        assert resp.status_code == 422

        r2 = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/admin/dlq",
                "query_string": b"task_name=hack;--",
            }
        )
        resp2 = await api_admin_dlq_list(r2)
        assert resp2.status_code == 422


@pytest.mark.asyncio
async def test_api_admin_dlq_page_mode_calls_count() -> None:
    mock_engine = MagicMock()

    async def acquire_cm():
        return AsyncMock()

    # list/count use pool directly, not conn
    with (
        patch("nce.admin_state.engine", mock_engine),
        patch(
            "nce.dead_letter_queue.count_dead_letters",
            new=AsyncMock(return_value=42),
        ) as mock_count,
        patch(
            "nce.dead_letter_queue.list_dead_letters",
            new=AsyncMock(return_value=[]),
        ),
    ):
        from admin_server import api_admin_dlq_list

        r = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/admin/dlq",
                "query_string": b"page=2&limit=10",
            }
        )
        resp = await api_admin_dlq_list(r)
        assert resp.status_code == 200
        data = json.loads(resp.body.decode())
        assert data["total"] == 42
        assert data["page"] == 2
        assert data["limit"] == 10
        assert data["offset"] == 10
        mock_count.assert_awaited_once()


@pytest.mark.asyncio
async def test_api_admin_quotas_resource_type_and_pagination() -> None:
    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_engine.pg_pool.acquire.return_value.__aenter__.return_value = mock_conn

    ns_id = uuid.uuid4()
    mock_conn.fetchrow.side_effect = [
        {"total": 99},
        {"used_sum": 5, "limit_sum": 100},
    ]
    mock_conn.fetch.return_value = [
        {
            "namespace_id": ns_id,
            "agent_id": "a1",
            "resource_type": "tool_calls",
            "limit_amount": 100,
            "used_amount": 5,
            "reset_at": None,
            "updated_at": None,
        }
    ]

    with patch("nce.admin_state.engine", mock_engine):
        from admin_server import api_admin_quotas

        qs = f"namespace_id={ns_id}&resource_type=tool_calls&page=1&limit=20".encode()
        r = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/admin/quotas",
                "query_string": qs,
            }
        )
        resp = await api_admin_quotas(r)
        assert resp.status_code == 200
        data = json.loads(resp.body.decode())
        assert data["total"] == 99
        assert data["page"] == 1
        assert len(data["tools"]) == 1
        assert data["tools"][0]["resource_type"] == "tool_calls"
        assert mock_conn.fetch.await_count == 1


@pytest.mark.asyncio
async def test_api_admin_quotas_bad_resource_type() -> None:
    mock_engine = MagicMock()
    with patch("nce.admin_state.engine", mock_engine):
        from admin_server import api_admin_quotas

        r = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/admin/quotas",
                "query_string": b"resource_type=bad%",
            }
        )
        resp = await api_admin_quotas(r)
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_api_admin_signing_status() -> None:
    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_engine.pg_pool.acquire.return_value.__aenter__.return_value = mock_conn

    sample = {"active_key_id": None, "keys_by_status": {}}
    with (
        patch("nce.admin_state.engine", mock_engine),
        patch(
            "nce.admin_handlers.fleet.admin_signing_keys_status",
            new=AsyncMock(return_value=sample),
        ),
    ):
        from admin_server import api_admin_signing_status

        r = Request({"type": "http", "method": "GET", "path": "/api/admin/signing/status"})
        resp = await api_admin_signing_status(r)
        assert resp.status_code == 200
        assert json.loads(resp.body.decode()) == sample


@pytest.mark.asyncio
async def test_api_admin_events_bad_event_seq() -> None:
    mock_engine = MagicMock()
    with patch("nce.admin_state.engine", mock_engine):
        from admin_server import api_admin_events

        r = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/admin/events",
                "query_string": b"event_seq_gte=notint",
            }
        )
        resp = await api_admin_events(r)
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_api_admin_pii_redactions_list_ok() -> None:
    from datetime import datetime, timezone

    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_engine.pg_pool.acquire.return_value.__aenter__.return_value = mock_conn
    mock_conn.fetchrow.return_value = {"total": 1}
    mid = uuid.uuid4()
    nid = uuid.uuid4()
    mock_conn.fetch.return_value = [
        {
            "memory_id": mid,
            "namespace_id": nid,
            "entity_type": "email",
            "token": "secret-token-long",
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }
    ]
    with patch("nce.admin_state.engine", mock_engine):
        from admin_server import api_admin_pii_redactions_list

        r = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/admin/pii-redactions",
                "query_string": b"page=1&limit=10",
            }
        )
        resp = await api_admin_pii_redactions_list(r)
        assert resp.status_code == 200
        data = json.loads(resp.body.decode())
        assert data["total"] == 1
        assert len(data["items"]) == 1
        assert data["items"][0]["entity_type"] == "email"
        assert data["items"][0]["token"].endswith("…")


@pytest.mark.asyncio
async def test_api_admin_security_event_seq_gaps() -> None:
    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_engine.pg_pool.acquire.return_value.__aenter__.return_value = mock_conn
    ns = uuid.uuid4()

    async def _fetchval(q, *a):
        if "MIN" in q:
            return 1
        if "MAX" in q:
            return 5
        return None

    mock_conn.fetchval.side_effect = _fetchval
    mock_conn.fetch.return_value = [
        {"after_seq": 2, "before_seq": 4},
    ]

    with patch("nce.admin_state.engine", mock_engine):
        from admin_server import api_admin_security_event_seq_gaps

        r = Request(
            {
                "type": "http",
                "method": "GET",
                "path": f"/api/admin/security/event-seq-gaps/{ns}",
                "path_params": {"namespace_id": str(ns)},
            }
        )
        resp = await api_admin_security_event_seq_gaps(r)
        assert resp.status_code == 200
        data = json.loads(resp.body.decode())
        assert data["min_seq"] == 1
        assert data["max_seq"] == 5
        assert len(data["gaps"]) >= 1


@pytest.mark.asyncio
async def test_api_admin_security_verify_memory_sample() -> None:
    mock_engine = MagicMock()
    mock_memory = MagicMock()
    mock_engine.memory = mock_memory
    mock_conn = AsyncMock()
    mock_engine.pg_pool.acquire.return_value.__aenter__.return_value = mock_conn
    mem_id = uuid.uuid4()
    mock_conn.fetch.return_value = [{"id": mem_id}]
    mock_memory.verify_memory = AsyncMock(
        return_value={"valid": True, "reason": "ok", "key_id": "sk-1"}
    )

    with patch("nce.admin_state.engine", mock_engine):
        from admin_server import api_admin_security_verify_memory_sample

        r = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/admin/security/verify-memory-sample",
            }
        )

        async def _json():
            return {"namespace_id": str(uuid.uuid4()), "sample_size": 5}

        r.json = _json  # type: ignore[method-assign]
        resp = await api_admin_security_verify_memory_sample(r)
        assert resp.status_code == 200
        data = json.loads(resp.body.decode())
        assert data["sampled"] == 1
        assert data["invalid_count"] == 0


@pytest.mark.asyncio
async def test_api_admin_security_test_rls_isolation() -> None:
    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_engine.pg_pool.acquire.return_value.__aenter__.return_value = mock_conn

    tx_cm = AsyncMock()
    tx_cm.__aenter__ = AsyncMock(return_value=None)
    tx_cm.__aexit__ = AsyncMock(return_value=None)
    mock_conn.transaction = MagicMock(return_value=tx_cm)

    mock_conn.fetchval.side_effect = [0, 3]

    ns_a = uuid.uuid4()
    ns_b = uuid.uuid4()

    with patch("nce.admin_state.engine", mock_engine):
        from admin_server import api_admin_security_test_rls_isolation

        r = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/admin/security/test-rls-isolation",
            }
        )

        async def _json():
            return {
                "namespace_id": str(ns_a),
                "probe_namespace_id": str(ns_b),
            }

        r.json = _json  # type: ignore[method-assign]
        resp = await api_admin_security_test_rls_isolation(r)
        assert resp.status_code == 200
        data = json.loads(resp.body.decode())
        assert data["isolation_ok"] is True
        assert data["cross_tenant_rows_visible"] == 0
        assert data["same_tenant_rows_visible"] == 3
