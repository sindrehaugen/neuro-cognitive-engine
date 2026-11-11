"""v3.4.0 Red-Team Security, Hybrid RRF, Working Memory & Scalability Suite."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from bson import ObjectId

from nce.a2a_server import app as a2a_app
from nce.admin_app import create_admin_app
from nce.db_utils import enable_hnsw_iterative_scan
from nce.embeddings import VECTOR_DIM
from nce.event_log import EXPECTED_TENANT_RLS_TABLES
from nce.mcp_http_gateway import create_mcp_gateway_app
from nce.net_safety import (
    BridgeURLValidationError,
    is_ip_blocked,
    validate_outbound_url,
    validate_outbound_url_async,
)
from nce.outbound_webhooks.service import create_webhook, send_webhook_http_sync
from nce.procedural_memory import distill_procedural_heuristics
from nce.semantic_search import extract_exact_identifiers, semantic_search
from nce.webhook_receiver.main import app as webhook_receiver_app
from nce.working_memory import compile_working_memory, estimate_tokens


def _fake_scoped(mock_conn: Any):
    @asynccontextmanager
    async def _scoped(_pool: Any, _namespace_id: Any):
        yield mock_conn

    return _scoped


def _make_mongo(docs_by_oid: dict[str, str]) -> Any:
    async def _find(query: dict[str, Any], projection: Any = None):
        for oid in query.get("_id", {}).get("$in", []):
            raw = docs_by_oid.get(str(oid))
            if raw is not None:
                yield {"_id": oid, "raw_data": raw}

    episodes = MagicMock()
    episodes.find = MagicMock(side_effect=_find)
    client = MagicMock()
    client.memory_archive = MagicMock()
    client.memory_archive.episodes = episodes
    return client


# ---------------------------------------------------------------------------
# 1. 3-Way Hybrid RRF + Exact SKU / orgnr / IP Boost + Epistemic Calibration
# ---------------------------------------------------------------------------


def test_extract_exact_identifiers_sku_orgnr_ip() -> None:
    query = "Check SKU-9942-X for org 987654321 at 10.42.0.15 and 2001:db8::42"
    extracted = extract_exact_identifiers(query)
    assert "SKU-9942-X" in extracted
    assert "987654321" in extracted
    assert "10.42.0.15" in extracted
    assert "2001:db8::42" in extracted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identifier", "matched_doc"),
    [
        ("SKU-9942-X", "Datasheet for hardware part SKU-9942-X rack switch"),
        ("987654321", "Enterprise registration orgnr 987654321 verified active"),
        ("10.42.0.15", "Management interface bound to 10.42.0.15 on VLAN 40"),
    ],
)
async def test_hybrid_rrf_ranks_exact_sku_orgnr_ip_first_under_noisy_vectors(
    identifier: str,
    matched_doc: str,
) -> None:
    """Exact SKU, Norwegian orgnr, and IP hits must outrank noisy cosine matches."""
    ns_id = "00000000-0000-4000-8000-000000000011"
    oid_noisy_1 = str(ObjectId())
    oid_noisy_2 = str(ObjectId())
    oid_exact = str(ObjectId())
    id_noisy_1 = uuid.uuid4()
    id_noisy_2 = uuid.uuid4()
    id_exact = uuid.uuid4()
    now = datetime.now(timezone.utc)

    rrf_rows = [
        {
            "payload_ref": oid_noisy_1,
            "memory_id": id_noisy_1,
            "final_score": 0.92,
            "raw_salience": 1.0,
            "last_updated": now,
        },
        {
            "payload_ref": oid_noisy_2,
            "memory_id": id_noisy_2,
            "final_score": 0.85,
            "raw_salience": 1.0,
            "last_updated": now,
        },
        {
            "payload_ref": oid_exact,
            "memory_id": id_exact,
            "final_score": 0.016,
            "raw_salience": 0.9,
            "last_updated": now,
        },
    ]

    executed_sqls: list[str] = []

    async def _fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        executed_sqls.append(sql)
        if "vector_ranked" in sql and "fts_ranked" in sql and "kg_ranked" in sql:
            return rrf_rows
        if "FROM memories WHERE id = ANY" in sql:
            return [
                {
                    "id": id_noisy_1,
                    "payload_ref": oid_noisy_1,
                    "wrapped_dek": None,
                    "valid_to": now - timedelta(hours=2),
                    "assertion_type": "opinion",
                    "derivation_depth": 3,
                    "metadata": {"provenance_tier": "unverified"},
                },
                {
                    "id": id_noisy_2,
                    "payload_ref": oid_noisy_2,
                    "wrapped_dek": None,
                    "valid_to": None,
                    "assertion_type": "fact",
                    "derivation_depth": 0,
                    "metadata": {},
                },
                {
                    "id": id_exact,
                    "payload_ref": oid_exact,
                    "wrapped_dek": None,
                    "valid_to": None,
                    "assertion_type": "fact",
                    "derivation_depth": 0,
                    "metadata": {"tag": identifier},
                },
            ]
        if "FROM contradictions" in sql:
            return [{"memory_a_id": id_noisy_2, "memory_b_id": uuid.uuid4()}]
        return []

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.fetchval = AsyncMock(return_value=None)
    mock_conn.fetch = AsyncMock(side_effect=_fetch)

    mongo = _make_mongo(
        {
            oid_noisy_1: "Generic discussion about enterprise networking switches",
            oid_noisy_2: "General notes on corporate registry lookups",
            oid_exact: matched_doc,
        }
    )

    async def _embed(_q: str) -> list[float]:
        return [0.01] * VECTOR_DIM

    def _discard_task(coro: Any) -> Any:
        coro.close()
        return MagicMock()

    with (
        patch("nce.semantic_search.scoped_pg_session", _fake_scoped(mock_conn)),
        patch("nce.semantic_search.asyncio.create_task", side_effect=_discard_task),
    ):
        hits = await semantic_search(
            pg_pool=MagicMock(),
            mongo_client=mongo,
            embedding_fn=_embed,
            query=f"Locate specification for {identifier}",
            namespace_id=ns_id,
            agent_id="redteam-agent",
            limit=5,
        )

    assert len(hits) == 3
    assert hits[0]["memory_id"] == id_exact
    assert any("kg_ranked" in s and "60 +" in s for s in executed_sqls)

    legacy_keys = {
        "memory_id",
        "payload_ref",
        "score",
        "raw_data",
        "salience_score",
        "last_reinforced_at",
        "confidence",
        "stale",
        "reranker_score",
        "confidence_score",
        "epistemic_warnings",
    }
    for hit in hits:
        assert legacy_keys.issubset(hit.keys())
        assert 0.0 <= hit["confidence_score"] <= 1.0

    by_id = {h["memory_id"]: h for h in hits}
    invalidated_hit = by_id[id_noisy_1]
    assert "atms_invalidated" in invalidated_hit["epistemic_warnings"]
    assert "derived_inference_depth_3" in invalidated_hit["epistemic_warnings"]
    assert "low_provenance_tier" in invalidated_hit["epistemic_warnings"]
    assert invalidated_hit["confidence_score"] <= 0.25

    contradicted_hit = by_id[id_noisy_2]
    assert "unresolved_contradiction" in contradicted_hit["epistemic_warnings"]
    assert contradicted_hit["confidence_score"] < by_id[id_exact]["confidence_score"]


# ---------------------------------------------------------------------------
# 2. compile_working_memory: Token Cap, Cyclic KG Termination & as_of Cutoff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compile_working_memory_token_budget_cycle_guard_and_as_of() -> None:
    ns_id = uuid.uuid4()
    cutoff = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    captured_sql_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def _fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        captured_sql_calls.append((sql, args))
        if "WITH RECURSIVE kg_walk" in sql:
            # Simulate cyclic graph NodeA -> NodeB -> NodeC -> NodeA bounded by CTE
            return [
                {
                    "subject_label": "NodeA",
                    "predicate": "CONNECTED_TO",
                    "object_label": "NodeB",
                    "confidence": 0.95,
                    "depth": 1,
                },
                {
                    "subject_label": "NodeB",
                    "predicate": "CONNECTED_TO",
                    "object_label": "NodeC",
                    "confidence": 0.90,
                    "depth": 2,
                },
                {
                    "subject_label": "NodeC",
                    "predicate": "CONNECTED_TO",
                    "object_label": "NodeA",
                    "confidence": 0.85,
                    "depth": 3,
                },
            ] * 8
        if "FROM procedural_heuristics" in sql:
            return [
                {
                    "id": uuid.uuid4(),
                    "namespace_id": ns_id,
                    "memory_id": None,
                    "rule_text": f"ALLOW: Deterministic rule #{idx} for core-domain switch",
                    "trigger_condition": "engine == 'system_design'",
                    "action_guidance": "Verify port capacity before committing topology change.",
                    "source_type": "governed_decision",
                    "source_refs": [{"source": "action_approval_queue", "id": str(idx)}],
                    "confidence_weight": 0.9,
                    "support_count": 4,
                    "valid_from": cutoff - timedelta(days=2),
                    "valid_to": None,
                    "created_at": cutoff - timedelta(days=2),
                    "updated_at": cutoff - timedelta(days=1),
                }
                for idx in range(6)
            ]
        if "FROM contradictions" in sql:
            return [
                {
                    "id": uuid.uuid4(),
                    "memory_a_id": uuid.uuid4(),
                    "memory_b_id": uuid.uuid4(),
                    "agent_id": "auditor",
                    "detected_at": cutoff - timedelta(hours=3),
                    "detection_path": "nli",
                    "signals": {"nli": 0.94},
                    "confidence": 0.94,
                    "note": "Conflicting port allocation",
                }
                for _ in range(4)
            ]
        return []

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(side_effect=_fetch)

    fake_engine = MagicMock()
    fake_engine.pg_pool = MagicMock()
    fake_engine.semantic_search = AsyncMock(
        return_value=[
            {
                "memory_id": str(uuid.uuid4()),
                "payload_ref": str(ObjectId()),
                "score": 0.99 - i * 0.05,
                "raw_data": ("Detailed rack signal flow telemetry block. " * 40),
                "confidence_score": 0.91,
                "epistemic_warnings": [],
            }
            for i in range(8)
        ]
    )

    with patch("nce.working_memory.scoped_pg_session", _fake_scoped(mock_conn)):
        compiled = await compile_working_memory(
            fake_engine,
            goal="Diagnose NodeA signal path instability",
            namespace_id=ns_id,
            max_tokens=260,
            as_of="2026-05-01T12:00:00Z",
        )

    assert compiled["token_count"] <= 260
    assert estimate_tokens(compiled) <= 260
    assert compiled["as_of"] == cutoff.isoformat()

    # Verify semantic_search received exact as_of cutoff
    fake_engine.semantic_search.assert_awaited_once()
    assert fake_engine.semantic_search.await_args.kwargs["as_of"] == cutoff

    # Verify KG recursive CTE carries cycle-prevention visited_path guard & as_of parameter
    kg_sqls = [sql for sql, _ in captured_sql_calls if "WITH RECURSIVE kg_walk" in sql]
    assert len(kg_sqls) == 1
    assert "ANY(w.visited_path)" in kg_sqls[0]
    assert "w.depth < 3" in kg_sqls[0]
    for _sql, call_args in captured_sql_calls:
        assert cutoff in call_args


@pytest.mark.asyncio
async def test_procedural_distillation_dedupes_and_RLS_catalog_registered() -> None:
    assert EXPECTED_TENANT_RLS_TABLES.get("procedural_heuristics") == "namespace_id"

    ns_id = uuid.uuid4()
    existing_rule_id = uuid.uuid4()
    executed_updates: list[tuple[Any, ...]] = []

    async def _fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM action_approval_queue" in sql:
            return [
                {
                    "id": uuid.uuid4(),
                    "namespace_id": ns_id,
                    "agent_id": "planner",
                    "action_type": "update_vlan",
                    "target_system": "netbox",
                    "status": "approved",
                    "resolved_by": "lead-arch",
                }
            ]
        if "FROM procedural_heuristics" in sql:
            return [
                {
                    "id": existing_rule_id,
                    "rule_text": "ALLOW: Verified execution of update_vlan on netbox.",
                    "trigger_condition": "action_type == 'update_vlan' and target_system == 'netbox'",
                    "action_guidance": "Proceed with update_vlan on netbox (settled approved by lead-arch).",
                    "source_type": "governed_decision",
                    "source_refs": [{"source": "action_approval_queue", "id": "prior-1"}],
                    "confidence_weight": 0.85,
                    "support_count": 2,
                }
            ]
        return []

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(side_effect=_fetch)
    mock_conn.execute = AsyncMock(side_effect=lambda sql, *a: executed_updates.append(a))

    rules = await distill_procedural_heuristics(mock_conn, ns_id)
    assert len(rules) == 1
    assert rules[0]["support_count"] == 3
    assert rules[0]["confidence_weight"] == pytest.approx(0.90)
    assert len(executed_updates) == 1


# ---------------------------------------------------------------------------
# 3. HTTP 413 Body Caps (Content-Length + Chunked Stream) Across All 4 Apps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("surface_name", "app_factory", "target_path"),
    [
        ("mcp_gateway", create_mcp_gateway_app, "/mcp"),
        ("admin_app", create_admin_app, "/api/search"),
        ("a2a_server", lambda: a2a_app, "/tasks/send"),
        ("webhook_receiver", lambda: webhook_receiver_app, "/webhooks/graph"),
    ],
)
async def test_http_413_content_length_and_chunked_stream_across_all_surfaces(
    monkeypatch: pytest.MonkeyPatch,
    surface_name: str,
    app_factory: Any,
    target_path: str,
) -> None:
    monkeypatch.setenv("NCE_MAX_HTTP_BODY_BYTES", "256")
    asgi_app = app_factory()

    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. Oversized Content-Length header
        resp_cl = await client.post(
            target_path,
            content=b"x" * 600,
            headers={"Content-Type": "application/json"},
        )
        assert resp_cl.status_code == 413, (
            f"{surface_name} Content-Length expected 413, got {resp_cl.status_code}"
        )
        assert resp_cl.json()["error"] == "payload_too_large"

        # 2. Oversized chunked streaming body (no Content-Length header)
        async def _chunked_stream() -> AsyncIterator[bytes]:
            for _ in range(6):
                await asyncio.sleep(0)
                yield b"z" * 100

        resp_chunked = await client.post(
            target_path,
            content=_chunked_stream(),
            headers={"Content-Type": "application/json"},
        )
        assert resp_chunked.status_code == 413, (
            f"{surface_name} chunked expected 413, got {resp_chunked.status_code}"
        )


# ---------------------------------------------------------------------------
# 4. SSRF Hardening: IPv4 Metadata, IPv4-Mapped IPv6, CGNAT & Outbound Webhooks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "blocked_url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://[::ffff:169.254.169.254]/latest/meta-data/",
        "https://[::ffff:127.0.0.1]:8443/hook",
        "http://100.64.0.1/internal",
        "https://100.127.255.254/cgnat",
        "http://host.docker.internal:8000/api",
        "http://metadata.google.internal/computeMetadata/v1/",
    ],
)
@pytest.mark.asyncio
async def test_ssrf_blocks_metadata_ipv4_mapped_cgnat_and_webhooks(
    monkeypatch: pytest.MonkeyPatch,
    blocked_url: str,
) -> None:
    monkeypatch.setenv("NCE_SSRF_ALLOWLIST_HOSTS", "")

    with pytest.raises(BridgeURLValidationError):
        validate_outbound_url(blocked_url, resolve_dns=False)

    with pytest.raises(BridgeURLValidationError):
        await validate_outbound_url_async(blocked_url, resolve_dns=False)

    mock_conn = AsyncMock()
    with pytest.raises(BridgeURLValidationError):
        await create_webhook(
            mock_conn,
            namespace_id=uuid.uuid4(),
            url=blocked_url,
            secret="super-secret-hmac",
        )
    mock_conn.fetchrow.assert_not_called()

    # Delivery-time SSRF guard must return False without dispatching HTTP traffic
    with patch("nce.outbound_webhooks.service.httpx.Client") as mock_http_cls:
        delivered = send_webhook_http_sync(blocked_url, "super-secret-hmac", {"ping": 1})
        assert delivered is False
        mock_http_cls.assert_not_called()


def test_ssrf_allowlist_and_hnsw_iterative_scan_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ipaddress

    assert is_ip_blocked(ipaddress.ip_address("::ffff:169.254.169.254")) is True
    assert is_ip_blocked(ipaddress.ip_address("100.64.0.1")) is True
    assert is_ip_blocked(ipaddress.ip_address("93.184.216.34")) is False

    monkeypatch.setenv("NCE_SSRF_ALLOWLIST_HOSTS", "host.docker.internal,100.64.0.1")
    assert validate_outbound_url("http://host.docker.internal:8080/health", resolve_dns=False)
    assert validate_outbound_url("http://100.64.0.1/status", resolve_dns=False)


@pytest.mark.asyncio
async def test_enable_hnsw_iterative_scan_sets_local_relaxed_order() -> None:
    conn = AsyncMock()
    await enable_hnsw_iterative_scan(conn)
    conn.execute.assert_awaited_once_with("SET LOCAL hnsw.iterative_scan = 'relaxed_order'")
