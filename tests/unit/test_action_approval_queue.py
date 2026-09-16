"""
Unit tests for action_approval_queue persistence, idempotency, and admin transitions.

Wave B-B128 (Charter §13):
- Wires both pending_approval branches in @governed to INSERT into action_approval_queue.
- Enforces fail-closed behavior on queue write failure.
- Reuses existing pending queue items for identical idempotency keys.
- Transitions pending/approved queue items to 'executed' on confirmed run.
- Admin approve/reject routes in muscles.py with strict idempotency and actor_trust tracking.

Collected by CI: Pytest (unit, py3.10/3.11/3.12) via:
    pytest tests/ -m "not integration and not perf and not live"
"""

from __future__ import annotations

import datetime
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from starlette.requests import Request

from nce.admin_handlers.muscles import (
    api_admin_approval_queue_approve,
    api_admin_approval_queue_reject,
)
from nce.autonomy.governor import (
    GovernanceError,
    governed,
)

# ---------------------------------------------------------------------------
# In-memory database simulation for contract-faithful testing
# ---------------------------------------------------------------------------


class SimulatedDatabase:
    def __init__(self) -> None:
        self.approval_queue: dict[uuid.UUID, dict[str, Any]] = {}
        self.idempotency: set[tuple[str, str]] = set()  # (str(ns_id), idem_key)
        self.actor_trust: dict[
            tuple[str, str, str], dict[str, Any]
        ] = {}  # (ns, actor, kind) -> row
        self.should_fail_insert: bool = False

    def make_connection(self, in_tx: bool = True) -> SimulatedConnection:
        return SimulatedConnection(self, in_tx=in_tx)


class SimulatedTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class SimulatedConnection:
    def __init__(self, db: SimulatedDatabase, in_tx: bool = True) -> None:
        self.db = db
        self._in_tx = in_tx

    def is_in_transaction(self) -> bool:
        return self._in_tx

    def transaction(self) -> SimulatedTransaction:
        return SimulatedTransaction()

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        # 1. SELECT 1 FROM action_idempotency
        if "FROM action_idempotency" in query:
            ns_id, idem_key = str(args[0]), str(args[1])
            if (ns_id, idem_key) in self.db.idempotency:
                return {"?column?": 1}
            return None

        # 2. SELECT id FROM action_approval_queue (check existing pending)
        if "SELECT id FROM action_approval_queue" in query:
            ns_id = str(args[0])
            action_type = args[1]
            idem_key = args[2]
            for row_id, r in self.db.approval_queue.items():
                if (
                    str(r["namespace_id"]) == ns_id
                    and r["action_type"] == action_type
                    and r["proposed_payload"].get("idempotency_key") == idem_key
                    and r["status"] == "pending"
                ):
                    return {"id": row_id}
            return None

        # 3. INSERT INTO action_approval_queue ... RETURNING id
        if "INSERT INTO action_approval_queue" in query:
            if self.db.should_fail_insert:
                raise RuntimeError("Simulated DB connection failure during approval queue insert")
            row_id = uuid.uuid4()
            ns_id = args[0]
            agent_id = args[1]
            action_type = args[2]
            target_system = args[3]
            target_entity_id = args[4]
            proposed_payload = json.loads(args[5]) if isinstance(args[5], str) else args[5]
            dry_run = (
                json.loads(args[6]) if isinstance(args[6], str) and args[6] is not None else args[6]
            )

            now = datetime.datetime.now(datetime.timezone.utc)
            row = {
                "id": row_id,
                "namespace_id": ns_id,
                "agent_id": agent_id,
                "action_type": action_type,
                "target_system": target_system,
                "target_entity_id": target_entity_id,
                "proposed_payload": proposed_payload,
                "status": "pending",
                "dry_run_result": dry_run,
                "created_at": now,
                "resolved_at": None,
                "resolved_by": None,
            }
            self.db.approval_queue[row_id] = row
            return {"id": row_id}

        # 4. SELECT ... FROM action_approval_queue WHERE id = $1 LIMIT 1
        if "FROM action_approval_queue" in query and "WHERE id = $1" in query:
            item_id = args[0]
            if isinstance(item_id, str):
                item_id = uuid.UUID(item_id)
            if item_id in self.db.approval_queue:
                return dict(self.db.approval_queue[item_id])
            return None

        # 5. UPDATE action_approval_queue SET status = 'approved' / 'rejected' ... RETURNING ...
        if (
            "UPDATE action_approval_queue" in query
            and "WHERE id = $1 AND status = 'pending'" in query
        ):
            item_id = args[0]
            resolved_by = args[1]
            if isinstance(item_id, str):
                item_id = uuid.UUID(item_id)
            row = self.db.approval_queue.get(item_id)
            if row and row["status"] == "pending":
                new_status = "approved" if "status = 'approved'" in query else "rejected"
                row["status"] = new_status
                row["resolved_by"] = resolved_by
                row["resolved_at"] = datetime.datetime.now(datetime.timezone.utc)
                return dict(row)
            return None

        return None

    async def execute(self, query: str, *args: Any) -> None:
        # 1. INSERT INTO action_idempotency
        if "INSERT INTO action_idempotency" in query:
            idem_key, ns_id, action_type = str(args[0]), str(args[1]), args[2]
            self.db.idempotency.add((ns_id, idem_key))
            return

        # 2. UPDATE action_approval_queue SET status = 'executed'
        if "UPDATE action_approval_queue" in query and "status = 'executed'" in query:
            ns_id = str(args[0])
            action_type = args[1]
            idem_key = args[2]
            for r in self.db.approval_queue.values():
                if (
                    str(r["namespace_id"]) == ns_id
                    and r["action_type"] == action_type
                    and r["proposed_payload"].get("idempotency_key") == idem_key
                    and r["status"] in ("pending", "approved")
                ):
                    r["status"] = "executed"
                    r["resolved_at"] = datetime.datetime.now(datetime.timezone.utc)
            return

        # 3. INSERT INTO actor_trust
        if "INSERT INTO actor_trust" in query:
            ns_id = str(args[0])
            actor_id = str(args[1])
            actor_kind = str(args[2]) if len(args) > 2 else "agent"
            key = (ns_id, actor_id, actor_kind)
            now = datetime.datetime.now(datetime.timezone.utc)
            is_confirm = "confirmations = 1" in query or "1, 0, now()" in query
            if key not in self.db.actor_trust:
                self.db.actor_trust[key] = {
                    "namespace_id": args[0],
                    "actor_id": actor_id,
                    "actor_kind": actor_kind,
                    "confirmations": 1 if is_confirm else 0,
                    "rejections": 0 if is_confirm else 1,
                    "contradictions_sourced": 0,
                    "trust": 0.65,
                    "updated_at": now,
                }
            else:
                entry = self.db.actor_trust[key]
                if is_confirm:
                    entry["confirmations"] += 1
                else:
                    entry["rejections"] += 1
                entry["updated_at"] = now
            return


# ---------------------------------------------------------------------------
# Test fixtures and handler helpers
# ---------------------------------------------------------------------------


def _make_governed_target() -> tuple[Any, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    @governed(action_type="deploy_firmware", risk_flags_arg="risk_flags")
    async def sample_handler(
        conn: Any,
        namespace_id: uuid.UUID,
        *,
        idempotency_key: str,
        confirm: bool = False,
        agent_id: str = "agent-x",
        target_system: str = "edge_gateway",
        target_entity_id: str | None = None,
        device_id: str = "dev-100",
        risk_flags: list[str] | None = None,
    ) -> dict[str, Any]:
        calls.append({"device_id": device_id, "idempotency_key": idempotency_key})
        return {"deployed": True, "device_id": device_id}

    return sample_handler, calls


def _build_request(
    path: str, path_params: dict[str, str], body: dict[str, Any] | None = None
) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [(b"content-type", b"application/json")],
        "path_params": path_params,
    }
    body_bytes = json.dumps(body or {}).encode("utf-8")

    async def receive():
        return {"type": "http.request", "body": body_bytes}

    return Request(scope, receive)


# ---------------------------------------------------------------------------
# @governed persistence & idempotency tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_governed_no_confirm_inserts_into_approval_queue():
    """confirm=False must INSERT a pending row into action_approval_queue inside caller transaction."""
    handler, call_log = _make_governed_target()
    db = SimulatedDatabase()
    conn = db.make_connection()
    ns_id = uuid.uuid4()
    idem_key = "k-pending-1"

    result = await handler(
        conn,
        ns_id,
        idempotency_key=idem_key,
        confirm=False,
        agent_id="test_runner",
        target_system="edge_gateway",
        target_entity_id="box-123",
        device_id="dev-999",
    )

    assert result["status"] == "pending_approval"
    assert result["idempotency_key"] == idem_key
    assert result["action_type"] == "deploy_firmware"
    approval_id = uuid.UUID(result["approval_id"])

    # Read back out of the simulated database
    assert approval_id in db.approval_queue
    row = db.approval_queue[approval_id]
    assert row["status"] == "pending"
    assert row["namespace_id"] == ns_id
    assert row["agent_id"] == "test_runner"
    assert row["action_type"] == "deploy_firmware"
    assert row["target_system"] == "edge_gateway"
    assert row["target_entity_id"] == "box-123"

    payload = row["proposed_payload"]
    assert payload["idempotency_key"] == idem_key
    assert payload["action_type"] == "deploy_firmware"
    assert payload["reason"] == "confirm_required"
    assert payload["caller_identity"] == "test_runner"
    assert payload["parameters"]["device_id"] == "dev-999"

    # Side effect must NOT run
    assert call_log == []


@pytest.mark.asyncio
async def test_governed_policy_gate_inserts_into_approval_queue_with_reason():
    """Contract-B policy gate refusal must INSERT a pending row with the policy reason."""
    handler, call_log = _make_governed_target()
    db = SimulatedDatabase()
    conn = db.make_connection()
    ns_id = uuid.uuid4()
    idem_key = "k-policy-1"

    result = await handler(
        conn,
        ns_id,
        idempotency_key=idem_key,
        confirm=True,  # Operator said confirm, but Contract-B policy gate fires
        risk_flags=["regulated"],
        device_id="dev-regulated-1",
    )

    expected_reason = "risk_flags=['regulated'] force human-confirm"
    assert result["status"] == "pending_approval"
    assert result["idempotency_key"] == idem_key
    assert result["reason"] == expected_reason
    approval_id = uuid.UUID(result["approval_id"])

    # Verify persisted row
    assert approval_id in db.approval_queue
    row = db.approval_queue[approval_id]
    assert row["status"] == "pending"
    assert row["proposed_payload"]["reason"] == expected_reason
    assert row["dry_run_result"] == {"policy_reason": expected_reason}

    # Side effect must not run
    assert call_log == []


@pytest.mark.asyncio
async def test_governed_pending_idempotency_reuses_existing_queue_item():
    """Calling repeatedly with the same key while pending must reuse the queue item, not duplicate it."""
    handler, _ = _make_governed_target()
    db = SimulatedDatabase()
    conn = db.make_connection()
    ns_id = uuid.uuid4()
    idem_key = "k-reuse-1"

    res1 = await handler(conn, ns_id, idempotency_key=idem_key, confirm=False)
    res2 = await handler(conn, ns_id, idempotency_key=idem_key, confirm=False)

    assert res1["status"] == "pending_approval"
    assert res2["status"] == "pending_approval"
    assert res1["approval_id"] == res2["approval_id"]

    # Table contains exactly one row
    assert len(db.approval_queue) == 1


@pytest.mark.asyncio
async def test_governed_already_executed_short_circuits_before_approval_queue():
    """If an idempotency key is already recorded in action_idempotency, do not create queue item."""
    handler, call_log = _make_governed_target()
    db = SimulatedDatabase()
    conn = db.make_connection()
    ns_id = uuid.uuid4()
    idem_key = "k-already-done"
    db.idempotency.add((str(ns_id), idem_key))

    res = await handler(conn, ns_id, idempotency_key=idem_key, confirm=False)

    assert res["status"] == "already_executed"
    assert res["idempotency_key"] == idem_key
    assert len(db.approval_queue) == 0
    assert call_log == []


@pytest.mark.asyncio
async def test_governed_write_failure_fails_closed():
    """If INSERT into action_approval_queue fails, fail closed (raise error, never return pending)."""
    handler, call_log = _make_governed_target()
    db = SimulatedDatabase()
    db.should_fail_insert = True
    conn = db.make_connection()
    ns_id = uuid.uuid4()

    with pytest.raises(GovernanceError, match="Simulated DB connection failure"):
        await handler(conn, ns_id, idempotency_key="k-fail-closed", confirm=False)

    assert call_log == []


@pytest.mark.asyncio
async def test_governed_confirmed_execution_transitions_queue_item_to_executed():
    """Execution with confirm=True must transition matching pending/approved queue items to executed."""
    handler, call_log = _make_governed_target()
    db = SimulatedDatabase()
    conn = db.make_connection()
    ns_id = uuid.uuid4()
    idem_key = "k-confirm-exec"

    with patch("nce.autonomy.governor.append_event", new_callable=AsyncMock):
        # 1. First call without confirm -> queues action
        pending_res = await handler(conn, ns_id, idempotency_key=idem_key, confirm=False)
        approval_id = uuid.UUID(pending_res["approval_id"])
        assert db.approval_queue[approval_id]["status"] == "pending"

        # 2. Second call with confirm=True -> executes and transitions queue item
        exec_res = await handler(conn, ns_id, idempotency_key=idem_key, confirm=True)

    assert exec_res["status"] == "executed"
    assert call_log == [{"device_id": "dev-100", "idempotency_key": idem_key}]

    # Queue item transitioned to executed
    row = db.approval_queue[approval_id]
    assert row["status"] == "executed"
    assert row["resolved_at"] is not None


@pytest.mark.asyncio
async def test_governed_missing_conn_or_namespace_fails_closed():
    """Calling @governed with missing conn or namespace_id must raise GovernanceError."""
    handler, _ = _make_governed_target()
    ns_id = uuid.uuid4()

    with pytest.raises(GovernanceError, match="requires 'conn' and 'namespace_id'"):
        await handler(None, ns_id, idempotency_key="k-no-conn", confirm=False)

    db = SimulatedDatabase()
    conn = db.make_connection()
    with pytest.raises(GovernanceError, match="requires 'conn' and 'namespace_id'"):
        await handler(conn, None, idempotency_key="k-no-ns", confirm=False)


@pytest.mark.asyncio
async def test_governed_conn_not_in_transaction_fails_closed():
    """Calling @governed with conn not in transaction must raise GovernanceError."""
    handler, _ = _make_governed_target()
    db = SimulatedDatabase()
    conn = db.make_connection(in_tx=False)

    with pytest.raises(GovernanceError, match="conn is not inside an active transaction"):
        await handler(conn, uuid.uuid4(), idempotency_key="k-no-tx", confirm=False)


# ---------------------------------------------------------------------------
# Admin API approve / reject handler tests
# ---------------------------------------------------------------------------


class FakeEngine:
    def __init__(self, db: SimulatedDatabase) -> None:
        self.db = db
        self.pg_pool = FakePgPool(db)


class FakePgPool:
    def __init__(self, db: SimulatedDatabase) -> None:
        self.db = db

    def acquire(self, timeout: float = 10.0):
        return FakeAcquireContext(self.db.make_connection())


class FakeAcquireContext:
    def __init__(self, conn: SimulatedConnection) -> None:
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


@pytest.mark.asyncio
async def test_api_approval_queue_approve_transitions_to_approved():
    """api_admin_approval_queue_approve transitions pending to approved and increments actor_trust."""
    db = SimulatedDatabase()
    item_id = uuid.uuid4()
    ns_id = uuid.uuid4()
    now = datetime.datetime.now(datetime.timezone.utc)

    db.approval_queue[item_id] = {
        "id": item_id,
        "namespace_id": ns_id,
        "agent_id": "field_agent_1",
        "action_type": "replace_module",
        "target_system": "edge",
        "target_entity_id": "mod-1",
        "proposed_payload": {"idempotency_key": "k-approve-1"},
        "status": "pending",
        "dry_run_result": None,
        "created_at": now,
        "resolved_at": None,
        "resolved_by": None,
    }

    req = _build_request(
        f"/api/admin/approval-queue/{item_id}/approve",
        {"id": str(item_id)},
        {"resolved_by": "lead_operator"},
    )

    with patch("nce.admin_state.engine", FakeEngine(db)):
        resp = await api_admin_approval_queue_approve(req)

    assert resp.status_code == 200
    data = json.loads(resp.body.decode("utf-8"))
    assert data["status"] == "approved"
    assert data["resolved_by"] == "lead_operator"

    # Verify db state
    row = db.approval_queue[item_id]
    assert row["status"] == "approved"
    assert row["resolved_by"] == "lead_operator"
    assert row["resolved_at"] is not None

    # Actor trust incremented
    trust_row = db.actor_trust[(str(ns_id), "field_agent_1", "agent")]
    assert trust_row["confirmations"] == 1
    assert trust_row["rejections"] == 0


@pytest.mark.asyncio
async def test_api_approval_queue_approve_idempotent():
    """Approving an item that is already approved or executed returns 200 OK idempotently."""
    db = SimulatedDatabase()
    item_id = uuid.uuid4()
    ns_id = uuid.uuid4()
    now = datetime.datetime.now(datetime.timezone.utc)

    db.approval_queue[item_id] = {
        "id": item_id,
        "namespace_id": ns_id,
        "agent_id": "field_agent_1",
        "action_type": "replace_module",
        "target_system": "edge",
        "target_entity_id": "mod-1",
        "proposed_payload": {"idempotency_key": "k-approve-repeat"},
        "status": "approved",
        "dry_run_result": None,
        "created_at": now,
        "resolved_at": now,
        "resolved_by": "operator_prior",
    }

    req = _build_request(
        f"/api/admin/approval-queue/{item_id}/approve",
        {"id": str(item_id)},
    )

    with patch("nce.admin_state.engine", FakeEngine(db)):
        resp = await api_admin_approval_queue_approve(req)

    assert resp.status_code == 200
    data = json.loads(resp.body.decode("utf-8"))
    assert data["status"] == "approved"


@pytest.mark.asyncio
async def test_api_approval_queue_reject_transitions_to_rejected():
    """api_admin_approval_queue_reject transitions pending to rejected and increments actor_trust rejections."""
    db = SimulatedDatabase()
    item_id = uuid.uuid4()
    ns_id = uuid.uuid4()
    now = datetime.datetime.now(datetime.timezone.utc)

    db.approval_queue[item_id] = {
        "id": item_id,
        "namespace_id": ns_id,
        "agent_id": "field_agent_2",
        "action_type": "reboot_host",
        "target_system": "edge",
        "target_entity_id": "host-2",
        "proposed_payload": {"idempotency_key": "k-reject-1"},
        "status": "pending",
        "dry_run_result": None,
        "created_at": now,
        "resolved_at": None,
        "resolved_by": None,
    }

    req = _build_request(
        f"/api/admin/approval-queue/{item_id}/reject",
        {"id": str(item_id)},
        {"resolved_by": "safety_officer"},
    )

    with patch("nce.admin_state.engine", FakeEngine(db)):
        resp = await api_admin_approval_queue_reject(req)

    assert resp.status_code == 200
    data = json.loads(resp.body.decode("utf-8"))
    assert data["status"] == "rejected"
    assert data["resolved_by"] == "safety_officer"

    # Verify db state
    row = db.approval_queue[item_id]
    assert row["status"] == "rejected"
    assert row["resolved_by"] == "safety_officer"
    assert row["resolved_at"] is not None

    # Actor trust rejections incremented
    trust_row = db.actor_trust[(str(ns_id), "field_agent_2", "agent")]
    assert trust_row["confirmations"] == 0
    assert trust_row["rejections"] == 1


@pytest.mark.asyncio
async def test_api_approval_queue_reject_idempotent():
    """Rejecting an item that is already rejected returns 200 OK idempotently."""
    db = SimulatedDatabase()
    item_id = uuid.uuid4()
    ns_id = uuid.uuid4()
    now = datetime.datetime.now(datetime.timezone.utc)

    db.approval_queue[item_id] = {
        "id": item_id,
        "namespace_id": ns_id,
        "agent_id": "field_agent_2",
        "action_type": "reboot_host",
        "target_system": "edge",
        "target_entity_id": "host-2",
        "proposed_payload": {"idempotency_key": "k-reject-repeat"},
        "status": "rejected",
        "dry_run_result": None,
        "created_at": now,
        "resolved_at": now,
        "resolved_by": "safety_officer",
    }

    req = _build_request(
        f"/api/admin/approval-queue/{item_id}/reject",
        {"id": str(item_id)},
    )

    with patch("nce.admin_state.engine", FakeEngine(db)):
        resp = await api_admin_approval_queue_reject(req)

    assert resp.status_code == 200
    data = json.loads(resp.body.decode("utf-8"))
    assert data["status"] == "rejected"


@pytest.mark.asyncio
async def test_api_approval_queue_conflict_transitions():
    """Approving rejected/expired or rejecting approved/executed items returns 409 Conflict."""
    db = SimulatedDatabase()
    item_id_rej = uuid.uuid4()
    item_id_app = uuid.uuid4()
    ns_id = uuid.uuid4()
    now = datetime.datetime.now(datetime.timezone.utc)

    db.approval_queue[item_id_rej] = {
        "id": item_id_rej,
        "namespace_id": ns_id,
        "agent_id": "a1",
        "action_type": "act",
        "target_system": "sys",
        "target_entity_id": None,
        "proposed_payload": {"idempotency_key": "k1"},
        "status": "rejected",
        "dry_run_result": None,
        "created_at": now,
        "resolved_at": now,
        "resolved_by": "admin",
    }

    db.approval_queue[item_id_app] = {
        "id": item_id_app,
        "namespace_id": ns_id,
        "agent_id": "a2",
        "action_type": "act",
        "target_system": "sys",
        "target_entity_id": None,
        "proposed_payload": {"idempotency_key": "k2"},
        "status": "approved",
        "dry_run_result": None,
        "created_at": now,
        "resolved_at": now,
        "resolved_by": "admin",
    }

    with patch("nce.admin_state.engine", FakeEngine(db)):
        # 1. Try to approve a rejected item -> 409
        req1 = _build_request(
            f"/api/admin/approval-queue/{item_id_rej}/approve", {"id": str(item_id_rej)}
        )
        r1 = await api_admin_approval_queue_approve(req1)
        assert r1.status_code == 409

        # 2. Try to reject an approved item -> 409
        req2 = _build_request(
            f"/api/admin/approval-queue/{item_id_app}/reject", {"id": str(item_id_app)}
        )
        r2 = await api_admin_approval_queue_reject(req2)
        assert r2.status_code == 409


@pytest.mark.asyncio
async def test_api_approval_queue_not_found_and_invalid_uuid():
    """Missing approval item returns 404; malformed UUID returns 422."""
    db = SimulatedDatabase()
    missing_id = uuid.uuid4()

    with patch("nce.admin_state.engine", FakeEngine(db)):
        # 1. 404 for missing item on approve & reject
        req_app = _build_request(
            f"/api/admin/approval-queue/{missing_id}/approve", {"id": str(missing_id)}
        )
        r_app = await api_admin_approval_queue_approve(req_app)
        assert r_app.status_code == 404

        req_rej = _build_request(
            f"/api/admin/approval-queue/{missing_id}/reject", {"id": str(missing_id)}
        )
        r_rej = await api_admin_approval_queue_reject(req_rej)
        assert r_rej.status_code == 404

        # 2. 422 for malformed UUID
        req_bad = _build_request("/api/admin/approval-queue/bad-id/approve", {"id": "bad-id"})
        r_bad = await api_admin_approval_queue_approve(req_bad)
        assert r_bad.status_code == 422


@pytest.mark.asyncio
async def test_api_approval_queue_missing_engine():
    """If engine is None, return 503 Service Unavailable."""
    req = _build_request(
        f"/api/admin/approval-queue/{uuid.uuid4()}/approve", {"id": str(uuid.uuid4())}
    )
    with patch("nce.admin_state.engine", None):
        r_app = await api_admin_approval_queue_approve(req)
        assert r_app.status_code == 503

        r_rej = await api_admin_approval_queue_reject(req)
        assert r_rej.status_code == 503
