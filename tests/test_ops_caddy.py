"""Tests for Day-2 maintenance caddy probes, deep integrity checks, cron ticks, and playbooks."""

from __future__ import annotations

import json
import sys
import types
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from nce.config import cfg
from nce.ops import caddy, elm, playbooks
from nce.ops.models import OpsDiskHeadroomError, OpsRestorePreflightError


class _FakeRecord(dict):
    """dict subclass mimicking asyncpg.Record key lookup and .get()."""


class _FakeConn:
    """In-memory asyncpg connection stub tailored for Day-2 ops probe & playbook SQL."""

    def __init__(self, store: _FakeOpsStore) -> None:
        self.store = store

    async def fetchrow(self, sql: str, *args: Any) -> _FakeRecord | None:
        norm = " ".join(sql.split()).lower()
        if "from saga_execution_log" in norm:
            return _FakeRecord(self.store.saga_counts)
        if "from pg_stat_activity" in norm:
            return _FakeRecord(self.store.pg_activity)
        if "from backup_manifests" in norm and "where status = 'verified'" in norm:
            if not self.store.verified_manifests:
                return None
            return _FakeRecord(self.store.verified_manifests[0])
        if "from backup_leases" in norm:
            return _FakeRecord(
                {
                    "expired_leases": self.store.expired_leases_count,
                    "active_drill_leases": self.store.active_drill_leases_count,
                }
            )
        if "from ops_incidents" in norm and "where fingerprint = $1" in norm:
            fp = args[0]
            inc = self.store.incidents.get(fp)
            return _FakeRecord(inc) if inc is not None else None
        if "from ops_probe_runs" in norm and "probe_name = 'event_log_hash_chain'" in norm:
            for run in reversed(self.store.probe_runs):
                if run["probe_name"] == "event_log_hash_chain":
                    return _FakeRecord({"metrics_json": run["metrics_json"]})
            return None
        if "from ops_quarantine" in norm and "where quarantine_id = $1" in norm:
            qid = args[0]
            row = self.store.quarantine_rows.get(qid)
            return _FakeRecord(row) if row is not None else None
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        norm = " ".join(sql.split()).lower()
        if "from ops_quarantine" in norm:
            return sum(
                1
                for row in self.store.quarantine_rows.values()
                if row.get("status") == "quarantined"
            )
        if "from pg_database" in norm and "nce_drill_" in norm:
            return self.store.drill_db_count
        if "from saga_execution_log" in norm:
            return (
                int(self.store.saga_counts.get("stuck_started", 0))
                + int(self.store.saga_counts.get("stuck_pg_committed", 0))
                + int(self.store.saga_counts.get("recovery_needed", 0))
            )
        if "from event_log" in norm and "max(event_seq)" in norm:
            ns_id = args[0]
            return self.store.event_max_seq.get(ns_id, 0)
        if "nce_is_backup_lease_active" in norm:
            return self.store.t_cut_backup_lease_active
        if "from backup_leases" in norm and "count(*)" in norm:
            return self.store.expired_leases_count
        return 0

    async def fetch(self, sql: str, *args: Any) -> list[_FakeRecord]:
        norm = " ".join(sql.split()).lower()
        if "from namespaces" in norm:
            return [_FakeRecord({"id": ns_id}) for ns_id in self.store.namespaces]
        if "from memories" in norm:
            ns_id = args[0]
            limit = int(args[1]) if len(args) > 1 else 50
            rows = self.store.memories_by_ns.get(ns_id, [])[:limit]
            return [_FakeRecord(r) for r in rows]
        return []

    async def execute(self, sql: str, *args: Any) -> str:
        norm = " ".join(sql.split()).lower()
        self.store.executed_sql.append((sql, args))
        if "insert into ops_probe_runs" in norm:
            run_id, probe_name, status, metrics_json, created_at = args
            self.store.probe_runs.append(
                {
                    "run_id": run_id,
                    "probe_name": probe_name,
                    "status": status,
                    "metrics_json": metrics_json,
                    "created_at": created_at,
                }
            )
            return "INSERT 0 1"
        if "update ops_incidents" in norm and "set status = 'resolved'" in norm:
            fp, now = args
            inc = self.store.incidents.get(fp)
            if inc is not None and inc["status"] == "open":
                inc["status"] = "resolved"
                inc["resolved_at"] = inc["resolved_at"] or now
                inc["last_seen_at"] = now
                return "UPDATE 1"
            return "UPDATE 0"
        if "insert into ops_incidents" in norm:
            (
                incident_id,
                fp,
                severity,
                title,
                det_facts,
                elm_brief_json,
                elm_mode_used,
                rec_playbook,
                now,
            ) = args
            existing = self.store.incidents.get(fp)
            if existing is None:
                self.store.incidents[fp] = {
                    "incident_id": incident_id,
                    "fingerprint": fp,
                    "severity": severity,
                    "status": "open",
                    "title": title,
                    "deterministic_facts": det_facts,
                    "elm_brief_json": elm_brief_json,
                    "elm_mode_used": elm_mode_used,
                    "recommended_playbook": rec_playbook,
                    "occurrence_count": 1,
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "resolved_at": None,
                }
            else:
                existing["severity"] = severity
                existing["status"] = "open"
                existing["title"] = title
                existing["deterministic_facts"] = det_facts
                if elm_brief_json is not None:
                    existing["elm_brief_json"] = elm_brief_json
                    existing["elm_mode_used"] = elm_mode_used
                if rec_playbook is not None:
                    existing["recommended_playbook"] = rec_playbook
                existing["occurrence_count"] += 1
                existing["last_seen_at"] = now
                existing["resolved_at"] = None
            return "INSERT 0 1"
        if "delete from ops_probe_runs" in norm:
            cutoff = args[0]
            before = len(self.store.probe_runs)
            self.store.probe_runs = [r for r in self.store.probe_runs if r["created_at"] >= cutoff]
            return f"DELETE {before - len(self.store.probe_runs)}"
        if "delete from backup_leases" in norm:
            removed = self.store.expired_leases_count
            self.store.expired_leases_count = 0
            return f"DELETE {removed}"
        if "insert into ops_playbook_executions" in norm:
            exec_id, incident_id, playbook_id, dry_run, approved_by, result_json, executed_at = args
            self.store.playbook_executions.append(
                {
                    "execution_id": exec_id,
                    "incident_id": incident_id,
                    "playbook_id": playbook_id,
                    "dry_run": dry_run,
                    "approved_by": approved_by,
                    "result_json": json.loads(result_json),
                    "executed_at": executed_at,
                }
            )
            return "INSERT 0 1"
        return "OK"

    @asynccontextmanager
    async def transaction(self) -> Any:
        yield self


class _FakeOpsStore:
    def __init__(self) -> None:
        now = datetime.now(timezone.utc)
        self.saga_counts: dict[str, int] = {
            "stuck_started": 0,
            "stuck_pg_committed": 0,
            "recovery_needed": 0,
        }
        self.pg_activity: dict[str, int] = {
            "total_connections": 4,
            "active_connections": 1,
            "idle_in_transaction": 0,
            "waiting_locks": 0,
        }
        self.verified_manifests: list[dict[str, Any]] = [
            {
                "backup_id": uuid.uuid4(),
                "t_cut": now - timedelta(hours=1),
                "created_at": now - timedelta(hours=1),
                "status": "verified",
                "last_drill_at": now - timedelta(days=1),
                "last_drill_status": "passed",
            }
        ]
        self.expired_leases_count = 0
        self.active_drill_leases_count = 0
        self.drill_db_count = 0
        self.t_cut_backup_lease_active = False
        self.quarantine_rows: dict[uuid.UUID, dict[str, Any]] = {}
        self.probe_runs: list[dict[str, Any]] = []
        self.incidents: dict[str, dict[str, Any]] = {}
        self.playbook_executions: list[dict[str, Any]] = []
        self.executed_sql: list[tuple[str, tuple[Any, ...]]] = []
        self.namespaces: list[uuid.UUID] = []
        self.event_max_seq: dict[uuid.UUID, int] = {}
        self.memories_by_ns: dict[uuid.UUID, list[dict[str, Any]]] = {}


class _FakePool:
    def __init__(self, store: _FakeOpsStore, *, size: int = 4, max_size: int = 20) -> None:
        self.store = store
        self._size = size
        self._max_size = max_size

    def get_size(self) -> int:
        return self._size

    def get_max_size(self) -> int:
        return self._max_size

    def get_idle_size(self) -> int:
        return max(0, self._max_size - self._size)

    @asynccontextmanager
    async def acquire(self, timeout: float | None = None) -> Any:
        _ = timeout
        yield _FakeConn(self.store)


class _FakeRedis:
    def __init__(
        self,
        *,
        queues: dict[str, int] | None = None,
        failed: dict[str, int] | None = None,
        used_memory: int = 100 * 1024**2,
        maxmemory: int = 1024 * 1024**2,
        salience_buffers: dict[str, int] | None = None,
    ) -> None:
        self.queues = queues or {}
        self.failed = failed or {}
        self.used_memory = used_memory
        self.maxmemory = maxmemory
        self.salience_buffers = salience_buffers or {}
        self.closed = False

    async def llen(self, key: str) -> int:
        name = key.removeprefix("rq:queue:")
        return int(self.queues.get(name, 0))

    async def zcard(self, key: str) -> int:
        if key == "nce:ops:elm:rate_window":
            return 0
        if key == "rq:failed":
            return int(self.failed.get("_global", 0))
        name = key.removeprefix("rq:failed:")
        return int(self.failed.get(name, 0))

    async def info(self, section: str | None = None) -> dict[str, Any]:
        _ = section
        return {"used_memory": self.used_memory, "maxmemory": self.maxmemory}

    async def keys(self, pattern: str) -> list[str]:
        _ = pattern
        return list(self.salience_buffers.keys())

    async def hlen(self, key: str) -> int:
        return int(self.salience_buffers.get(key, 0))

    async def aclose(self) -> None:
        self.closed = True


@dataclass(frozen=True)
class _FakeDiskUsage:
    total: int
    used: int
    free: int


def _stub_cognitive_client(
    status_code: int = 200, payload: dict[str, Any] | None = None
) -> httpx.AsyncClient:
    def _handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload or {"status": "ok", "engine": "local-bge"})

    return httpx.AsyncClient(transport=httpx.MockTransport(_handler))


@pytest.mark.asyncio
async def test_fast_watchdog_probes_healthy_baseline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    monkeypatch.setattr(cfg, "NCE_OPS_MIN_FREE_BYTES", 10 * 1024**3)
    monkeypatch.setattr(cfg, "NCE_BACKUP_MAX_AGE_HOURS", 26)
    monkeypatch.setattr(cfg, "NCE_SALIENCE_BUFFER_MAX_LEN", 50000)
    monkeypatch.setattr(
        caddy.shutil,
        "disk_usage",
        lambda _p: _FakeDiskUsage(total=100 * 1024**3, used=40 * 1024**3, free=60 * 1024**3),
    )

    store = _FakeOpsStore()
    pool = _FakePool(store)
    redis = _FakeRedis()

    async with _stub_cognitive_client(200, {"status": "ok", "engine": "openai"}) as cog_http:
        results = await caddy.run_fast_watchdog_probes(
            pool,
            redis_client=redis,
            cognitive_url="http://cognitive:8001",
            backup_dir=tmp_path,
            cognitive_http_client=cog_http,
            invoke_elm=False,
        )

    assert len(results) == 7
    assert [r.probe_name for r in results] == [
        "stuck_sagas",
        "rq_queues_and_quarantine",
        "postgres_pool_and_locks",
        "redis_memory_and_buffers",
        "backup_freshness_and_leases",
        "cognitive_runtime_mode",
        "disk_headroom",
    ]
    assert all(r.status == "ok" for r in results)
    assert len(store.probe_runs) == 7
    assert store.incidents == {}


@pytest.mark.asyncio
async def test_fast_watchdog_probes_detect_degraded_states_across_all_seven_probes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    monkeypatch.setattr(cfg, "NCE_OPS_MIN_FREE_BYTES", 10 * 1024**3)
    monkeypatch.setattr(cfg, "NCE_BACKUP_MAX_AGE_HOURS", 26)
    monkeypatch.setattr(cfg, "NCE_SALIENCE_BUFFER_MAX_LEN", 50000)
    monkeypatch.setattr(cfg, "NCE_ALLOW_COGNITIVE_STUB", False)
    monkeypatch.setattr(cfg, "NCE_OPS_ELM_MODE", "deterministic")
    monkeypatch.setattr(
        caddy.shutil,
        "disk_usage",
        lambda _p: _FakeDiskUsage(total=100 * 1024**3, used=99 * 1024**3, free=1 * 1024**3),
    )

    store = _FakeOpsStore()
    store.saga_counts = {"stuck_started": 3, "stuck_pg_committed": 2, "recovery_needed": 1}
    store.quarantine_rows[uuid.uuid4()] = {"status": "quarantined"}
    store.pg_activity = {
        "total_connections": 90,
        "active_connections": 40,
        "idle_in_transaction": 12,
        "waiting_locks": 7,
    }
    store.verified_manifests = [
        {
            "backup_id": uuid.uuid4(),
            "t_cut": datetime.now(timezone.utc) - timedelta(hours=70),
            "created_at": datetime.now(timezone.utc) - timedelta(hours=70),
            "status": "verified",
            "last_drill_at": None,
            "last_drill_status": None,
        }
    ]
    store.expired_leases_count = 2
    store.drill_db_count = 4  # 4 stale nce_drill_* databases -> critical

    pool = _FakePool(store, size=19, max_size=20)
    redis = _FakeRedis(
        queues={"nce-critical": 600},
        failed={"nce-default": 3},
        used_memory=960 * 1024**2,
        maxmemory=1000 * 1024**2,
        salience_buffers={"nce:ns:0000:salience_buffer": 60000},
    )

    async with _stub_cognitive_client(200, {"status": "ok", "engine": "stub"}) as cog_http:
        results = await caddy.run_fast_watchdog_probes(
            pool,
            redis_client=redis,
            cognitive_url="http://cognitive:8001",
            backup_dir=tmp_path,
            cognitive_http_client=cog_http,
        )

    by_name = {r.probe_name: r for r in results}
    assert by_name["stuck_sagas"].status == "critical"
    assert by_name["stuck_sagas"].recommended_playbook == "recover_stuck_sagas"
    assert by_name["rq_queues_and_quarantine"].status == "warn"
    assert by_name["rq_queues_and_quarantine"].recommended_playbook == "resolve_quarantined_job"
    assert by_name["postgres_pool_and_locks"].status == "critical"
    assert by_name["redis_memory_and_buffers"].status == "critical"
    assert (
        by_name["redis_memory_and_buffers"].recommended_playbook
        == "cleanup_expired_leases_and_drills"
    )
    assert by_name["backup_freshness_and_leases"].status == "critical"
    assert (
        by_name["backup_freshness_and_leases"].recommended_playbook
        == "cleanup_expired_leases_and_drills"
    )
    assert by_name["cognitive_runtime_mode"].status in {"warn", "critical"}
    assert by_name["disk_headroom"].status == "critical"
    assert by_name["disk_headroom"].recommended_playbook == "sweep_confirmed_orphans"
    assert len(store.incidents) == 7
    assert all(inc["elm_brief_json"] is not None for inc in store.incidents.values())


@pytest.mark.asyncio
async def test_incident_dedupe_escalation_and_auto_resolution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    monkeypatch.setattr(cfg, "NCE_OPS_ELM_MODE", "local")
    monkeypatch.setattr(
        caddy.shutil,
        "disk_usage",
        lambda _p: _FakeDiskUsage(total=100 * 1024**3, used=20 * 1024**3, free=80 * 1024**3),
    )

    elm_calls: list[dict[str, Any]] = []
    real_diagnose = elm.diagnose_incident

    async def _spy_diagnose(facts: dict[str, Any], **kwargs: Any) -> tuple[Any, str]:
        elm_calls.append(facts)
        monkeypatch.setattr(cfg, "NCE_OPS_ELM_MODE", "deterministic")
        return await real_diagnose(facts, **kwargs)

    monkeypatch.setattr(caddy, "diagnose_incident", _spy_diagnose)

    store = _FakeOpsStore()
    store.saga_counts = {"stuck_started": 1, "stuck_pg_committed": 0, "recovery_needed": 0}
    pool = _FakePool(store)

    # 1. First warn -> opens incident + triggers ELM once.
    await caddy.run_fast_watchdog_probes(pool, backup_dir=tmp_path)
    inc = store.incidents["probe:stuck_sagas"]
    assert inc["status"] == "open"
    assert inc["severity"] == "warn"
    assert inc["occurrence_count"] == 1
    assert len(elm_calls) == 1

    # 2. Steady-state warn -> increments occurrence_count WITHOUT calling ELM again.
    await caddy.run_fast_watchdog_probes(pool, backup_dir=tmp_path)
    assert store.incidents["probe:stuck_sagas"]["occurrence_count"] == 2
    assert len(elm_calls) == 1

    # 3. Escalation warn -> critical -> invokes ELM for escalation.
    store.saga_counts = {"stuck_started": 6, "stuck_pg_committed": 0, "recovery_needed": 0}
    await caddy.run_fast_watchdog_probes(pool, backup_dir=tmp_path)
    assert store.incidents["probe:stuck_sagas"]["severity"] == "critical"
    assert store.incidents["probe:stuck_sagas"]["occurrence_count"] == 3
    assert len(elm_calls) == 2

    # 4. Recovery -> auto-resolves open incident cleanly.
    store.saga_counts = {"stuck_started": 0, "stuck_pg_committed": 0, "recovery_needed": 0}
    await caddy.run_fast_watchdog_probes(pool, backup_dir=tmp_path)
    assert store.incidents["probe:stuck_sagas"]["status"] == "resolved"
    assert store.incidents["probe:stuck_sagas"]["resolved_at"] is not None
    assert len(elm_calls) == 2


@pytest.mark.asyncio
async def test_deep_integrity_probes_merkle_dangling_pointers_and_retention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ns_ok = uuid.uuid4()
    ns_bad = uuid.uuid4()
    store = _FakeOpsStore()
    store.namespaces = [ns_ok, ns_bad]
    store.event_max_seq = {ns_ok: 12, ns_bad: 5}
    store.probe_runs = [
        {
            "run_id": uuid.uuid4(),
            "probe_name": "event_log_hash_chain",
            "status": "ok",
            "metrics_json": json.dumps({"cursors": {str(ns_ok): 10, str(ns_bad): 2}}),
            "created_at": datetime.now(timezone.utc) - timedelta(hours=1),
        },
        {
            "run_id": uuid.uuid4(),
            "probe_name": "stuck_sagas",
            "status": "ok",
            "metrics_json": "{}",
            "created_at": datetime.now(timezone.utc) - timedelta(days=45),
        },
    ]

    valid_oid_str = "507f1f77bcf86cd799439011"
    missing_oid_str = "507f1f77bcf86cd799439012"
    store.memories_by_ns = {
        ns_ok: [
            {
                "id": uuid.uuid4(),
                "payload_ref": valid_oid_str,
                "metadata": json.dumps({"bucket": "nce-artifacts", "object_name": "doc/ok.pdf"}),
            },
            {
                "id": uuid.uuid4(),
                "payload_ref": missing_oid_str,
                "metadata": json.dumps(
                    {"bucket": "nce-artifacts", "object_name": "doc/missing.pdf"}
                ),
            },
        ],
        ns_bad: [],
    }

    verify_calls: list[tuple[uuid.UUID, int]] = []

    async def _fake_verify_merkle_chain(
        conn: Any,
        *,
        namespace_id: uuid.UUID,
        start_seq: int = 1,
    ) -> dict[str, Any]:
        _ = conn
        verify_calls.append((namespace_id, start_seq))
        if namespace_id == ns_ok:
            return {"valid": True, "checked": 2, "last_verified_seq": 12}
        return {"valid": False, "checked": 3, "first_break": 4, "reason": "hash_mismatch"}

    fake_event_log = types.ModuleType("nce.event_log")
    fake_event_log.verify_merkle_chain = _fake_verify_merkle_chain  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nce.event_log", fake_event_log)

    from bson import ObjectId

    class _FakeCursor:
        async def to_list(self, length: int = 100) -> list[dict[str, Any]]:
            _ = length
            return [{"_id": ObjectId(valid_oid_str)}]

    class _FakeEpisodesCol:
        def find(self, query: dict[str, Any], projection: dict[str, Any]) -> _FakeCursor:
            _ = query, projection
            return _FakeCursor()

    class _FakeMongo:
        memory_archive = types.SimpleNamespace(episodes=_FakeEpisodesCol())

    class _FakeMinio:
        def stat_object(self, bucket: str, object_name: str) -> dict[str, Any]:
            _ = bucket
            if object_name == "doc/missing.pdf":
                raise FileNotFoundError(object_name)
            return {"size": 42}

    pool = _FakePool(store)
    results = await caddy.run_deep_integrity_probes(
        pool,
        mongo_client=_FakeMongo(),
        minio_client=_FakeMinio(),
        invoke_elm=False,
    )

    by_name = {r.probe_name: r for r in results}
    assert verify_calls == [(ns_ok, 11), (ns_bad, 3)]
    assert by_name["event_log_hash_chain"].status == "critical"
    assert by_name["event_log_hash_chain"].metrics_json["cursors"][str(ns_ok)] == 12

    dangling = by_name["dangling_payload_pointers"]
    assert dangling.status == "warn"
    assert dangling.metrics_json["missing_mongo_docs"] == 1
    assert dangling.metrics_json["missing_minio_objects"] == 1
    assert dangling.recommended_playbook == "sweep_confirmed_orphans"

    retention = by_name["ops_probe_runs_retention"]
    assert retention.status == "ok"
    assert retention.metrics_json["pruned_rows"] == 1


@pytest.mark.asyncio
async def test_playbook_governance_confirmation_and_all_five_playbooks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    monkeypatch.setattr(cfg, "NCE_MASTER_KEY", "test-master-key-for-playbook-hmac")
    monkeypatch.setattr(cfg, "NCE_OPS_MIN_FREE_BYTES", 10 * 1024**3)
    monkeypatch.setattr(cfg, "NCE_BACKUP_DIR", str(tmp_path))
    monkeypatch.setattr(
        playbooks.shutil,
        "disk_usage",
        lambda _p: _FakeDiskUsage(total=100 * 1024**3, used=20 * 1024**3, free=80 * 1024**3),
    )

    assert set(playbooks.PLAYBOOK_REGISTRY.keys()) == elm.PLAYBOOK_IDS

    store = _FakeOpsStore()
    store.expired_leases_count = 3
    store.drill_db_count = 2
    store.saga_counts = {"stuck_started": 2, "stuck_pg_committed": 0, "recovery_needed": 0}
    qid = uuid.uuid4()
    store.quarantine_rows[qid] = {
        "quarantine_id": qid,
        "job_fingerprint": "fp-1",
        "queue_name": "nce-default",
        "func_name": "nce.tasks.ingest",
        "failure_count": 4,
        "status": "quarantined",
    }
    pool = _FakePool(store)
    redis = _FakeRedis(salience_buffers={"nce:ns:abc:salience_buffer": 7})

    # Stub WORM audit dependencies so _system event emission is exercised and verified.
    audit_calls: list[dict[str, Any]] = []
    sys_ns_id = uuid.uuid4()

    fake_auth = types.ModuleType("nce.auth")
    fake_auth.set_namespace_context = lambda conn, ns_id: _noop_async(conn, ns_id)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nce.auth", fake_auth)

    async def _fake_get_sys_ns(conn: Any) -> uuid.UUID:
        _ = conn
        return sys_ns_id

    fake_sys_ns = types.ModuleType("nce.system_namespace")
    fake_sys_ns.SystemNamespaceMissingError = RuntimeError  # type: ignore[attr-defined]
    fake_sys_ns.get_system_namespace_id = _fake_get_sys_ns  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nce.system_namespace", fake_sys_ns)

    async def _fake_append_event(**kwargs: Any) -> Any:
        audit_calls.append(kwargs)
        return types.SimpleNamespace(event_id=uuid.uuid4())

    fake_event_log = types.ModuleType("nce.event_log")
    fake_event_log.append_event = _fake_append_event  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nce.event_log", fake_event_log)

    # Stub lazy nce.ops.backup & nce.ops.quarantine modules (owned by Worker-1).
    calls: dict[str, int] = {
        "orphans": 0,
        "swept": 0,
        "backup": 0,
        "drill": 0,
        "requeue": 0,
        "dismiss": 0,
    }

    fake_backup = types.ModuleType("nce.ops.backup")

    async def _sweep_orphans(**_kw: Any) -> dict[str, Any]:
        calls["orphans"] += 1
        return {"purged_count": 5}

    async def _sweep_drills(**_kw: Any) -> dict[str, Any]:
        calls["swept"] += 1
        store.drill_db_count = 0
        return {"dropped_count": 2}

    async def _create_backup(**_kw: Any) -> dict[str, Any]:
        calls["backup"] += 1
        return {"backup_id": str(uuid.uuid4()), "status": "verified"}

    async def _run_drill(**_kw: Any) -> dict[str, Any]:
        calls["drill"] += 1
        return {"status": "passed"}

    fake_backup.sweep_confirmed_orphans = _sweep_orphans  # type: ignore[attr-defined]
    fake_backup.sweep_stale_drill_targets = _sweep_drills  # type: ignore[attr-defined]
    fake_backup.create_watermark_backup = _create_backup  # type: ignore[attr-defined]
    fake_backup.run_ephemeral_restore_drill = _run_drill  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nce.ops.backup", fake_backup)

    fake_quarantine = types.ModuleType("nce.ops.quarantine")

    async def _requeue(**_kw: Any) -> dict[str, Any]:
        calls["requeue"] += 1
        return {"requeued": True}

    async def _dismiss(**_kw: Any) -> dict[str, Any]:
        calls["dismiss"] += 1
        return {"dismissed": True}

    fake_quarantine.requeue_quarantined_job = _requeue  # type: ignore[attr-defined]
    fake_quarantine.dismiss_quarantined_job = _dismiss  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nce.ops.quarantine", fake_quarantine)

    async def _fake_flush_salience(_r: Any, _p: Any) -> int:
        return 7

    fake_salience = types.ModuleType("nce.salience_buffer")
    fake_salience.flush_salience_buffer = _fake_flush_salience  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nce.salience_buffer", fake_salience)

    import nce.cron as cron_mod

    saga_recovery_calls = 0

    async def _fake_saga_tick(pool: Any, mongo_client: Any = None) -> None:
        nonlocal saga_recovery_calls
        _ = pool, mongo_client
        saga_recovery_calls += 1
        store.saga_counts = {"stuck_started": 0, "stuck_pg_committed": 0, "recovery_needed": 0}

    monkeypatch.setattr(cron_mod, "_saga_recovery_tick", _fake_saga_tick)

    # 1. Confirm token validation (tampered / wrong params / wrong playbook / expired).
    preview = await playbooks.preview_playbook(
        "cleanup_expired_leases_and_drills", {}, pg_pool=pool, redis_client=redis
    )
    assert preview.dry_run is True
    assert preview.executed is False
    assert preview.affected_count == 3 + 7 + 2
    assert preview.confirmation_token is not None

    with pytest.raises(playbooks.PlaybookConfirmationError):
        await playbooks.execute_playbook(
            "cleanup_expired_leases_and_drills",
            {},
            confirmation_token="0.bad",
            approved_by="ops-admin",
            pg_pool=pool,
        )
    with pytest.raises(playbooks.PlaybookConfirmationError):
        await playbooks.execute_playbook(
            "cleanup_expired_leases_and_drills",
            {"unexpected": 1},
            confirmation_token=preview.confirmation_token,
            approved_by="ops-admin",
            pg_pool=pool,
        )
    with pytest.raises(playbooks.PlaybookConfirmationError):
        await playbooks.execute_playbook(
            "recover_stuck_sagas",
            {},
            confirmation_token=preview.confirmation_token,
            approved_by="ops-admin",
            pg_pool=pool,
        )

    expired_tok, _ = playbooks.issue_confirmation_token("recover_stuck_sagas", {}, ttl_seconds=1)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(playbooks.time, "time", lambda: int(expired_tok.split(".")[0]) + 60)
        with pytest.raises(playbooks.PlaybookConfirmationError):
            await playbooks.execute_playbook(
                "recover_stuck_sagas",
                {},
                confirmation_token=expired_tok,
                approved_by="ops-admin",
                pg_pool=pool,
            )

    # Execute Playbook 4: cleanup_expired_leases_and_drills
    res4 = await playbooks.execute_playbook(
        "cleanup_expired_leases_and_drills",
        {},
        confirmation_token=preview.confirmation_token,
        approved_by="ops-admin",
        pg_pool=pool,
        redis_client=redis,
    )
    assert res4.executed is True
    assert res4.affected_count == 12
    assert res4.audit_event_id is not None
    assert calls["swept"] == 1

    # Execute Playbook 2: recover_stuck_sagas
    p2 = await playbooks.preview_playbook("recover_stuck_sagas", {}, pg_pool=pool)
    assert p2.affected_count == 2
    res2 = await playbooks.execute_playbook(
        "recover_stuck_sagas",
        {},
        confirmation_token=p2.confirmation_token or "",
        approved_by="ops-admin",
        pg_pool=pool,
    )
    assert res2.executed is True
    assert saga_recovery_calls == 1

    # Execute Playbook 3: resolve_quarantined_job (requeue + dismiss with missing _system fallback)
    q_params = {"action": "requeue", "quarantine_id": str(qid)}
    p3 = await playbooks.preview_playbook("resolve_quarantined_job", q_params, pg_pool=pool)
    assert p3.affected_count == 1
    res3 = await playbooks.execute_playbook(
        "resolve_quarantined_job",
        q_params,
        confirmation_token=p3.confirmation_token or "",
        approved_by="ops-admin",
        pg_pool=pool,
    )
    assert res3.executed is True
    assert calls["requeue"] == 1

    class _MissingSysError(Exception):
        pass

    async def _raise_missing_sys(_conn: Any) -> uuid.UUID:
        raise _MissingSysError("_system absent")

    d_params = {"action": "dismiss", "quarantine_id": str(qid), "reason": "false_positive"}
    p3_d = await playbooks.preview_playbook("resolve_quarantined_job", d_params, pg_pool=pool)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fake_sys_ns, "SystemNamespaceMissingError", _MissingSysError)
        mp.setattr(fake_sys_ns, "get_system_namespace_id", _raise_missing_sys)
        res3_d = await playbooks.execute_playbook(
            "resolve_quarantined_job",
            d_params,
            confirmation_token=p3_d.confirmation_token or "",
            approved_by="ops-admin",
            pg_pool=pool,
        )
    assert res3_d.executed is True
    assert res3_d.audit_event_id is None
    assert calls["dismiss"] == 1

    # Execute Playbook 1: sweep_confirmed_orphans (blocked while t_cut_backup active, then unblocked)
    store.t_cut_backup_lease_active = True
    p1_blocked = await playbooks.preview_playbook("sweep_confirmed_orphans", {}, pg_pool=pool)
    assert p1_blocked.estimated_impact["blocked_by_active_backup_lease"] is True
    res1_blocked = await playbooks.execute_playbook(
        "sweep_confirmed_orphans",
        {},
        confirmation_token=p1_blocked.confirmation_token or "",
        approved_by="ops-admin",
        pg_pool=pool,
    )
    assert res1_blocked.result_json["blocked_by_active_backup_lease"] is True
    assert calls["orphans"] == 0

    store.t_cut_backup_lease_active = False
    p1 = await playbooks.preview_playbook("sweep_confirmed_orphans", {}, pg_pool=pool)
    res1 = await playbooks.execute_playbook(
        "sweep_confirmed_orphans",
        {},
        confirmation_token=p1.confirmation_token or "",
        approved_by="ops-admin",
        pg_pool=pool,
    )
    assert res1.affected_count == 5
    assert calls["orphans"] == 1

    # Execute Playbook 5: run_immediate_backup_or_drill (backup + drill + headroom guard + missing manifest guard)
    p5_b = await playbooks.preview_playbook(
        "run_immediate_backup_or_drill", {"mode": "backup"}, pg_pool=pool
    )
    res5_b = await playbooks.execute_playbook(
        "run_immediate_backup_or_drill",
        {"mode": "backup"},
        confirmation_token=p5_b.confirmation_token or "",
        approved_by="ops-admin",
        pg_pool=pool,
    )
    assert res5_b.executed is True
    assert calls["backup"] == 1

    p5_d = await playbooks.preview_playbook(
        "run_immediate_backup_or_drill", {"mode": "drill"}, pg_pool=pool
    )
    res5_d = await playbooks.execute_playbook(
        "run_immediate_backup_or_drill",
        {"mode": "drill"},
        confirmation_token=p5_d.confirmation_token or "",
        approved_by="ops-admin",
        pg_pool=pool,
    )
    assert res5_d.executed is True
    assert calls["drill"] == 1

    store.verified_manifests = []
    p5_no_manifest = await playbooks.preview_playbook(
        "run_immediate_backup_or_drill", {"mode": "drill"}, pg_pool=pool
    )
    with pytest.raises(OpsRestorePreflightError):
        await playbooks.execute_playbook(
            "run_immediate_backup_or_drill",
            {"mode": "drill"},
            confirmation_token=p5_no_manifest.confirmation_token or "",
            approved_by="ops-admin",
            pg_pool=pool,
        )

    monkeypatch.setattr(
        playbooks.shutil,
        "disk_usage",
        lambda _p: _FakeDiskUsage(total=100 * 1024**3, used=99 * 1024**3, free=1024**3),
    )
    p5_low_disk = await playbooks.preview_playbook(
        "run_immediate_backup_or_drill", {"mode": "backup"}, pg_pool=pool
    )
    with pytest.raises(OpsDiskHeadroomError):
        await playbooks.execute_playbook(
            "run_immediate_backup_or_drill",
            {"mode": "backup"},
            confirmation_token=p5_low_disk.confirmation_token or "",
            approved_by="ops-admin",
            pg_pool=pool,
        )

    assert len(store.playbook_executions) == 8
    assert len(audit_calls) == 7
    assert all(call["event_type"] == "config_changed" for call in audit_calls)


async def _noop_async(*_args: Any, **_kwargs: Any) -> None:
    return None


@pytest.mark.asyncio
async def test_cron_ops_ticks_locking_and_backup_drill_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nce.cron as cron_mod

    lock_sentinel = object()
    released: list[Any] = []
    acquired_names: list[str] = []

    async def _acquire(name: str, ttl: int) -> Any:
        _ = ttl
        acquired_names.append(name)
        return lock_sentinel

    async def _release(lock: Any) -> None:
        released.append(lock)

    monkeypatch.setattr(cron_mod, "acquire_cron_lock", _acquire)
    monkeypatch.setattr(cron_mod, "release_cron_lock", _release)

    fast_calls = 0
    deep_calls = 0
    backup_calls = 0
    drill_calls = 0

    async def _fake_fast(_pool: Any, **_kw: Any) -> list[Any]:
        nonlocal fast_calls
        fast_calls += 1
        return []

    async def _fake_deep(_pool: Any, **_kw: Any) -> list[Any]:
        nonlocal deep_calls
        deep_calls += 1
        return []

    async def _fake_backup(**_kw: Any) -> dict[str, Any]:
        nonlocal backup_calls
        backup_calls += 1
        return {"backup_id": "test"}

    async def _fake_drill(**_kw: Any) -> dict[str, Any]:
        nonlocal drill_calls
        drill_calls += 1
        return {"status": "passed"}

    monkeypatch.setattr(caddy, "run_fast_watchdog_probes", _fake_fast)
    monkeypatch.setattr(caddy, "run_deep_integrity_probes", _fake_deep)
    fake_backup = types.ModuleType("nce.ops.backup")
    fake_backup.create_watermark_backup = _fake_backup  # type: ignore[attr-defined]
    fake_backup.run_ephemeral_restore_drill = _fake_drill  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nce.ops.backup", fake_backup)

    store = _FakeOpsStore()
    pool = _FakePool(store)
    redis = _FakeRedis()

    await cron_mod._ops_fast_watchdog_tick(pool, redis_client=redis)
    assert fast_calls == 1

    monkeypatch.setattr(cfg, "NCE_BACKUP_AUTO_DAILY", False)
    await cron_mod._ops_deep_integrity_tick(pool)
    assert deep_calls == 1
    assert backup_calls == 0

    monkeypatch.setattr(cfg, "NCE_BACKUP_AUTO_DAILY", True)
    await cron_mod._ops_deep_integrity_tick(pool)
    assert deep_calls == 2
    assert backup_calls == 1

    await cron_mod._ops_weekly_restore_drill_tick(pool)
    assert drill_calls == 1

    store.verified_manifests = []
    await cron_mod._ops_weekly_restore_drill_tick(pool)
    assert drill_calls == 1  # No verified manifest -> cleanly skipped

    assert acquired_names == [
        "ops_fast_watchdog",
        "ops_deep_integrity",
        "ops_deep_integrity",
        "ops_weekly_restore_drill",
        "ops_weekly_restore_drill",
    ]
    assert len(released) == 5

    # When CronLock is held elsewhere, ticks exit early without running probes.
    async def _no_lock(_name: str, _ttl: int) -> None:
        return None

    monkeypatch.setattr(cron_mod, "acquire_cron_lock", _no_lock)
    await cron_mod._ops_fast_watchdog_tick(pool, redis_client=redis)
    assert fast_calls == 1
