"""Unit tests for Wave SU-1 / FT-3: TICKET.dispatched event contract, emission, and subscriber."""

from __future__ import annotations

import datetime
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nce.events import catalogue
from nce.vertical_modules.field_tech.work_orders import (
    handle_ticket_dispatched,
    register_field_tech_subscribers,
)
from nce.vertical_modules.support.dispatch import do_dispatch_work_order


class TestTicketDispatchedEventContract:
    """Validate EVENT_CATALOGUE contract for TICKET.dispatched."""

    def test_ticket_dispatched_contract_is_active(self) -> None:
        contract = catalogue.EVENT_CATALOGUE.get("TICKET.dispatched")
        assert contract is not None
        assert contract.status == "ACTIVE"
        assert "nce/vertical_modules/support/dispatch.py" in contract.producers
        assert "nce/vertical_modules/field_tech/work_orders.py" in contract.consumers


class TestSupportDispatchEmitsTicketDispatched:
    """Validate that do_dispatch_work_order emits TICKET.dispatched via publish."""

    @pytest.mark.asyncio
    async def test_dispatch_emits_ticket_dispatched_event(self) -> None:
        ns_id = uuid4()
        ticket_id = uuid4()
        edge_dt = datetime.datetime.now(datetime.timezone.utc)

        mock_conn = AsyncMock()
        # 1. SELECT id, namespace_id, status, summary, events FROM service_tickets
        # 2. SELECT object_label, created_at FROM kg_edges (idempotency check -> None)
        # 3. INSERT INTO kg_edges ... RETURNING created_at
        mock_conn.fetchrow.side_effect = [
            {
                "id": ticket_id,
                "namespace_id": ns_id,
                "status": "open",
                "summary": "Audio amplifier failure in Room 101",
                "events": json.dumps([]),
            },
            None,  # not previously dispatched
            {"created_at": edge_dt},  # edge RETURNING created_at
        ]
        mock_conn.execute.return_value = None

        @asynccontextmanager
        async def fake_scoped(pool, namespace_id):
            yield mock_conn

        mock_engine = MagicMock()
        mock_engine.pg_pool = MagicMock()

        params = {
            "namespace_id": str(ns_id),
            "ticket_id": str(ticket_id),
            "cost": 0.0,
            "confirm": False,
        }

        with (
            patch("nce.vertical_modules.support.dispatch.scoped_pg_session", fake_scoped),
            patch(
                "nce.vertical_modules.support.dispatch.publish", new_callable=AsyncMock
            ) as mock_publish,
            patch(
                "nce.vertical_modules.support.dispatch.assert_owner", new_callable=AsyncMock
            ) as mock_assert_owner,
        ):
            res = await do_dispatch_work_order(mock_engine, params)

            assert res["dispatched"] is True
            assert res["ticket_id"] == str(ticket_id)
            assert "work_order_id" in res
            assert res["idempotent_replay"] is False
            assert mock_assert_owner.call_count == 1

            # Verify publish was called with TICKET.dispatched
            assert mock_publish.call_count == 1
            pub_kwargs = mock_publish.call_args.kwargs
            assert pub_kwargs["node_type"] == "TICKET"
            assert pub_kwargs["op"] == "dispatched"
            assert pub_kwargs["payload"]["ticket_id"] == str(ticket_id)
            assert pub_kwargs["payload"]["work_order_id"] == res["work_order_id"]
            assert pub_kwargs["payload"]["summary"] == "Audio amplifier failure in Room 101"
            assert pub_kwargs["payload"]["status"] == "dispatched"


class TestFieldTechHandleTicketDispatched:
    """Validate Field Tech subscriber handler for TICKET.dispatched."""

    @pytest.mark.asyncio
    async def test_handle_ticket_dispatched_creates_work_order_and_nodes(self) -> None:
        ns_id = uuid4()
        ticket_id = str(uuid4())
        wo_id = f"WO-{uuid4().hex[:8].upper()}"

        mock_conn = AsyncMock()
        mock_conn.execute.return_value = None

        event = {
            "namespace_id": str(ns_id),
            "payload": {
                "ticket_id": ticket_id,
                "work_order_id": wo_id,
                "namespace_id": str(ns_id),
                "summary": "Fix broken projector",
                "priority": "high",
                "status": "dispatched",
            },
        }

        with patch(
            "nce.vertical_modules.field_tech.work_orders.assert_owner",
            new_callable=AsyncMock,
        ) as mock_assert_owner:
            await handle_ticket_dispatched(mock_conn, event)

            # Contract-A assertion for WORK_ORDER owner
            assert mock_assert_owner.call_count == 1
            mock_assert_owner.assert_called_once_with(mock_conn, ns_id, "WORK_ORDER", "field_tech")

            # 3 SQL executions: work_orders table, kg_nodes, kg_edges
            assert mock_conn.execute.call_count == 3

            # Check work_orders table INSERT
            call1_sql = mock_conn.execute.call_args_list[0][0][0]
            assert "INSERT INTO work_orders" in call1_sql
            assert "ON CONFLICT (work_order_id, namespace_id) DO UPDATE" in call1_sql

            # Check kg_nodes INSERT
            call2_sql = mock_conn.execute.call_args_list[1][0][0]
            assert "INSERT INTO kg_nodes" in call2_sql
            assert f"WORK_ORDER:{wo_id}" == mock_conn.execute.call_args_list[1][0][1]

            # Check kg_edges INSERT (WORK_ORDER -[for]-> TICKET)
            call3_sql = mock_conn.execute.call_args_list[2][0][0]
            assert "INSERT INTO kg_edges" in call3_sql
            assert f"WORK_ORDER:{wo_id}" == mock_conn.execute.call_args_list[2][0][1]
            assert f"TICKET:{ticket_id}" == mock_conn.execute.call_args_list[2][0][2]

    @pytest.mark.asyncio
    async def test_handle_ticket_dispatched_missing_keys_graceful_ignore(self) -> None:
        mock_conn = AsyncMock()
        # Missing payload/ticket_id/work_order_id should log warning and return without error
        await handle_ticket_dispatched(mock_conn, {"namespace_id": str(uuid4()), "payload": {}})
        assert mock_conn.execute.call_count == 0


class TestFieldTechSubscriberRegistration:
    """Validate subscriber registration in C4 relay bus."""

    def test_register_subscribers_calls_bus_subscribe(self) -> None:
        with patch("nce.events.bus.subscribe") as mock_sub:
            register_field_tech_subscribers()
            assert mock_sub.call_count >= 1
            found = False
            for call in mock_sub.call_args_list:
                selector_dict = call[0][0]
                if (
                    selector_dict.get("node_type") == "TICKET"
                    and selector_dict.get("op") == "dispatched"
                ):
                    assert call[0][1] == handle_ticket_dispatched
                    found = True
                    break
            assert found, "TICKET.dispatched subscriber was not registered"
