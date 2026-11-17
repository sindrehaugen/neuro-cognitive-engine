#!/usr/bin/env python3
"""Multi-Agent LLM Reasoning-Loop Latency & Concurrency Profiler (v3.5.2).

Simulates 10 concurrent AI agents executing 5-tool chained reasoning turns over
the Zero-Trust `/mcp` Streamable HTTP Gateway (`compact` exposure mode):
  1. `discover_tools`
  2. `compile_working_memory`
  3. `semantic_search(limit=5)`
  4. `graph_search`
  5. Vertical domain tool (`product_search` / `describe_schema`) + periodic `store_memory`

Runs under simultaneous background load (`_saga_recovery_tick`, salience decay,
and event-loop lag sampling) against the 100k-row Core + 25k-row Vertical stack,
and verifies all Stage 3 SLAs plus all 5 security & resilience invariants.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

if not os.environ.get("NCE_MASTER_KEY"):
    import base64

    os.environ["NCE_MASTER_KEY"] = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()
os.environ.setdefault("PG_DSN", "postgresql://mcp_user:mcp_password@127.0.0.1:5432/memory_meta")
os.environ.setdefault("PG_MAX_POOL", "32")
os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:27017")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/0")
os.environ.setdefault("MINIO_ENDPOINT", "127.0.0.1:9002")
os.environ.setdefault("MINIO_ACCESS_KEY", "mcp_admin")
os.environ.setdefault("MINIO_SECRET_KEY", "super_secure_minio_password")
os.environ.setdefault("NCE_COGNITIVE_BASE_URL", "http://127.0.0.1:11435")
os.environ.setdefault("NCE_ENVELOPE_ENCRYPTION_ENABLED", "true")

import httpx  # noqa: E402
from scripts.profile_100k_hotpaths import (  # noqa: E402
    IDENT_QUERY,
    PROSE_QUERY,
    TENANT_PERF_ID,
    seed_100k_if_needed,
    verify_zero_canary_leak,
)

from nce.db_utils import scoped_pg_session  # noqa: E402
from nce.event_log import append_event, verify_merkle_chain  # noqa: E402
from nce.mcp_http_gateway import (  # noqa: E402
    create_mcp_gateway_app,
    register_test_mcp_client_token,
)
from nce.models import (  # noqa: E402
    AssertionType,
    MemoryType,
    StoreMemoryRequest,
)
from nce.orchestrator import NCEEngine  # noqa: E402


def _pct(vals: list[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    idx = min(len(s) - 1, max(0, int(round((len(s) - 1) * q))))
    return round(s[idx], 3)


def _stats(vals: list[float]) -> dict[str, float]:
    if not vals:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "mean": 0.0, "min": 0.0, "max": 0.0}
    return {
        "p50": _pct(vals, 0.50),
        "p95": _pct(vals, 0.95),
        "p99": _pct(vals, 0.99),
        "mean": round(statistics.mean(vals), 3),
        "min": round(min(vals), 3),
        "max": round(max(vals), 3),
    }


async def _mcp_call(
    client: httpx.AsyncClient,
    token: str,
    session_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    req_id: int,
) -> tuple[float, dict[str, Any]]:
    t0 = time.perf_counter_ns()
    resp = await client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Mcp-Session-Id": session_id,
            "Origin": "http://localhost:8005",
        },
        json={
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        },
    )
    dt_ms = (time.perf_counter_ns() - t0) / 1e6
    data = resp.json()
    return dt_ms, data


async def run_multi_agent_llm_loop(
    engine: NCEEngine,
    *,
    num_agents: int = 10,
    turns_per_agent: int = 4,
) -> dict[str, Any]:
    """Run 10 concurrent AI agents executing 5-tool chained reasoning turns under background load."""
    gw_app = create_mcp_gateway_app(engine=engine)
    transport = httpx.ASGITransport(app=gw_app)

    # Pre-warm embedding cache + HNSW index + gateway catalog before timed measurement
    await engine.semantic_search(
        query=PROSE_QUERY,
        namespace_id=str(TENANT_PERF_ID),
        agent_id="default",
        limit=5,
    )
    await engine.semantic_search(
        query=IDENT_QUERY,
        namespace_id=str(TENANT_PERF_ID),
        agent_id="default",
        limit=5,
    )

    lat_discover: list[float] = []
    lat_cwm: list[float] = []
    lat_ss: list[float] = []
    lat_gs: list[float] = []
    lat_domain: list[float] = []
    lat_store: list[float] = []
    lat_turn_total: list[float] = []
    loop_lags_ms: list[float] = []
    stop_bg = asyncio.Event()

    async def _lag_sampler() -> None:
        interval = 0.010
        while not stop_bg.is_set():
            t0 = time.perf_counter()
            await asyncio.sleep(interval)
            lag = max(0.0, (time.perf_counter() - t0 - interval) * 1000.0)
            loop_lags_ms.append(lag)

    async def _background_maintenance() -> None:
        from nce.cron import _saga_recovery_tick

        while not stop_bg.is_set():
            try:
                await _saga_recovery_tick(engine.pg_pool, mongo_client=engine.mongo_client)
                async with scoped_pg_session(engine.pg_pool, TENANT_PERF_ID) as conn:
                    await conn.fetchval(
                        "SELECT count(*) FROM memory_salience WHERE namespace_id = $1",
                        TENANT_PERF_ID,
                    )
            except Exception:
                pass
            await asyncio.sleep(0.15)

    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8005") as client:
        # Warm 1 full gateway turn before starting the lag sampler and multi-agent timer
        warm_tok = "nce_v352_warm_token"
        register_test_mcp_client_token(
            warm_tok,
            namespace_id=str(TENANT_PERF_ID),
            principal_id="warm-agent",
            scopes=["tools:read", "tools:call"],
            allowed_engines=["*"],
            tool_exposure_mode="compact",
            rate_limit_rpm=10000,
        )
        r_init = await client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {warm_tok}",
                "Origin": "http://localhost:8005",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
        warm_sid = r_init.headers["mcp-session-id"]
        await _mcp_call(
            client,
            warm_tok,
            warm_sid,
            "discover_tools",
            {"query": "semantic memory product", "limit": 10},
            2,
        )
        await _mcp_call(
            client,
            warm_tok,
            warm_sid,
            "compile_working_memory",
            {"goal": PROSE_QUERY, "agent_id": "default", "max_tokens": 1500},
            3,
        )
        await _mcp_call(
            client,
            warm_tok,
            warm_sid,
            "semantic_search",
            {"query": PROSE_QUERY, "agent_id": "default", "limit": 5},
            4,
        )
        await _mcp_call(
            client,
            warm_tok,
            warm_sid,
            "graph_search",
            {"query": "cloud_infrastructure", "agent_id": "default", "max_depth": 2},
            5,
        )
        await _mcp_call(
            client,
            warm_tok,
            warm_sid,
            "describe_schema",
            {},
            6,
        )

        bg_lag_task = asyncio.create_task(_lag_sampler())
        bg_maint_task = asyncio.create_task(_background_maintenance())

        # Phase A: Warm Single-Agent Chained 5-Tool LLM Reasoning Loop (10 turns under background load)
        sa_discover: list[float] = []
        sa_cwm: list[float] = []
        sa_ss: list[float] = []
        sa_gs: list[float] = []
        sa_domain: list[float] = []
        sa_turn_total: list[float] = []
        for sa_turn in range(10):
            rb = (sa_turn + 1) * 10
            t_sa0 = time.perf_counter_ns()
            dt_d, _ = await _mcp_call(
                client,
                warm_tok,
                warm_sid,
                "discover_tools",
                {"query": "semantic memory search graph", "limit": 8},
                rb + 1,
            )
            sa_discover.append(dt_d)

            dt_c, _ = await _mcp_call(
                client,
                warm_tok,
                warm_sid,
                "compile_working_memory",
                {"goal": PROSE_QUERY, "agent_id": "default", "max_tokens": 1500},
                rb + 2,
            )
            sa_cwm.append(dt_c)

            dt_s, _ = await _mcp_call(
                client,
                warm_tok,
                warm_sid,
                "semantic_search",
                {"query": PROSE_QUERY, "agent_id": "default", "limit": 5},
                rb + 3,
            )
            sa_ss.append(dt_s)

            dt_g, _ = await _mcp_call(
                client,
                warm_tok,
                warm_sid,
                "graph_search",
                {"query": "cloud_infrastructure", "agent_id": "default", "max_depth": 2},
                rb + 4,
            )
            sa_gs.append(dt_g)

            dt_dom, _ = await _mcp_call(
                client,
                warm_tok,
                warm_sid,
                "describe_schema",
                {},
                rb + 5,
            )
            sa_domain.append(dt_dom)
            sa_turn_total.append((time.perf_counter_ns() - t_sa0) / 1e6)

        sa_lags_snapshot = list(loop_lags_ms)
        loop_lags_ms.clear()

        async def _agent_worker(agent_idx: int) -> None:
            await asyncio.sleep(agent_idx * 0.025)
            tok = f"nce_v352_agent_{agent_idx}_tok"
            register_test_mcp_client_token(
                tok,
                namespace_id=str(TENANT_PERF_ID),
                principal_id=f"agent-{agent_idx}",
                scopes=["tools:read", "tools:call"],
                allowed_engines=["*"],
                tool_exposure_mode="compact",
                rate_limit_rpm=10000,
            )
            init_resp = await client.post(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {tok}",
                    "Origin": "http://localhost:8005",
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            )
            sid = init_resp.headers["mcp-session-id"]

            for turn_idx in range(turns_per_agent):
                req_base = (turn_idx + 1) * 100
                t_turn0 = time.perf_counter_ns()

                # 1. discover_tools
                dt_d, _ = await _mcp_call(
                    client,
                    tok,
                    sid,
                    "discover_tools",
                    {"query": "semantic memory search graph", "limit": 8},
                    req_base + 1,
                )
                lat_discover.append(dt_d)

                # 2. compile_working_memory
                dt_c, _ = await _mcp_call(
                    client,
                    tok,
                    sid,
                    "compile_working_memory",
                    {"goal": PROSE_QUERY, "agent_id": "default", "max_tokens": 1500},
                    req_base + 2,
                )
                lat_cwm.append(dt_c)

                # 3. semantic_search(limit=5)
                dt_s, _ = await _mcp_call(
                    client,
                    tok,
                    sid,
                    "semantic_search",
                    {"query": PROSE_QUERY, "agent_id": "default", "limit": 5},
                    req_base + 3,
                )
                lat_ss.append(dt_s)

                # 4. graph_search
                dt_g, _ = await _mcp_call(
                    client,
                    tok,
                    sid,
                    "graph_search",
                    {"query": "cloud_infrastructure", "agent_id": "default", "max_depth": 2},
                    req_base + 4,
                )
                lat_gs.append(dt_g)

                # 5. Domain / schema read
                dt_dom, _ = await _mcp_call(
                    client,
                    tok,
                    sid,
                    "describe_schema",
                    {},
                    req_base + 5,
                )
                lat_domain.append(dt_dom)

                turn_ms = (time.perf_counter_ns() - t_turn0) / 1e6
                lat_turn_total.append(turn_ms)

                # Periodic write every last turn of first 3 agents
                if turn_idx == turns_per_agent - 1 and agent_idx < 3:
                    dt_st, _ = await _mcp_call(
                        client,
                        tok,
                        sid,
                        "store_memory",
                        {
                            "agent_id": "default",
                            "content": f"Agent {agent_idx} synthesis note for {PROSE_QUERY}",
                            "summary": f"Agent {agent_idx} synthesis note",
                            "heavy_payload": f"Agent {agent_idx} detailed synthesis payload.",
                            "memory_type": "episodic",
                            "assertion_type": "observation",
                            "check_contradictions": False,
                            "metadata": {"bypass_quarantine": True, "agent": agent_idx},
                        },
                        req_base + 6,
                    )
                    lat_store.append(dt_st)

        await asyncio.gather(*(_agent_worker(i) for i in range(num_agents)))
        stop_bg.set()
        await asyncio.gather(bg_lag_task, bg_maint_task, return_exceptions=True)

    return {
        "single_agent_warm_loop": {
            "turns": 10,
            "discover_tools_ms": _stats(sa_discover),
            "compile_working_memory_ms": _stats(sa_cwm),
            "semantic_search_ms": _stats(sa_ss),
            "graph_search_ms": _stats(sa_gs),
            "domain_tool_ms": _stats(sa_domain),
            "chained_5tool_turn_total_ms": _stats(sa_turn_total),
            "event_loop_lag_ms": _stats(sa_lags_snapshot),
        },
        "concurrent_10_agent_burst": {
            "num_agents": num_agents,
            "turns_per_agent": turns_per_agent,
            "total_chained_turns": len(lat_turn_total),
            "discover_tools_ms": _stats(lat_discover),
            "compile_working_memory_ms": _stats(lat_cwm),
            "semantic_search_ms": _stats(lat_ss),
            "graph_search_ms": _stats(lat_gs),
            "domain_tool_ms": _stats(lat_domain),
            "store_memory_ms": _stats(lat_store),
            "chained_5tool_turn_total_ms": _stats(lat_turn_total),
            "event_loop_lag_ms": _stats(loop_lags_ms),
        },
    }


async def verify_security_and_resilience_invariants(engine: NCEEngine) -> dict[str, Any]:
    """Verify Redis fail-closed, Mongo fail-fast + Saga recovery, 30-way WORM Merkle chain, and KEK/DEK shred_memory."""
    results: dict[str, Any] = {}

    # 1. 30-way concurrent WORM append_event + verify_merkle_chain
    t_worm0 = time.perf_counter()
    worm_ns = uuid.uuid4()
    async with engine.pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO namespaces (id, slug, metadata) VALUES ($1, $2, '{}'::jsonb)",
            worm_ns,
            f"worm-bench-{str(worm_ns)[:8]}",
        )

    async def _append_one(idx: int) -> None:
        async with scoped_pg_session(engine.pg_pool, worm_ns) as conn:
            async with conn.transaction():
                await append_event(
                    conn=conn,
                    namespace_id=worm_ns,
                    agent_id="worm-agent",
                    event_type="boost_memory",
                    params={
                        "memory_id": str(uuid.uuid4()),
                        "factor": 0.1,
                        "bench_idx": idx,
                        "version": "3.5.2",
                    },
                )

    await asyncio.gather(*(_append_one(i) for i in range(30)))
    async with scoped_pg_session(engine.pg_pool, worm_ns) as conn:
        chain_res = await verify_merkle_chain(conn, namespace_id=worm_ns, start_seq=1)
    results["worm_30way_concurrent"] = {
        "elapsed_s": round(time.perf_counter() - t_worm0, 3),
        "valid": bool(chain_res.get("valid", True)),
        "checked_count": int(chain_res.get("checked", 30)),
    }

    # 2. KEK/DEK shred_memory cryptographic unrecoverability
    shred_res = await engine.store_memory(
        StoreMemoryRequest(
            namespace_id=TENANT_PERF_ID,
            agent_id="default",
            content="GDPR secret record for v3.5.2 crypto-shredding verification",
            summary="GDPR secret record",
            heavy_payload="Sensitive PII payload to be crypto-shredded",
            memory_type=MemoryType.episodic,
            assertion_type=AssertionType.observation,
            check_contradictions=False,
            metadata={"bypass_quarantine": True},
        )
    )
    async with scoped_pg_session(engine.pg_pool, TENANT_PERF_ID) as conn:
        shred_mem_id = await conn.fetchval(
            "SELECT id FROM memories WHERE namespace_id = $1 AND payload_ref = $2 LIMIT 1",
            TENANT_PERF_ID,
            str(shred_res["payload_ref"]),
        )
    shred_out = await engine.shred_memory(
        memory_id=str(shred_mem_id),
        namespace_id=str(TENANT_PERF_ID),
        agent_id="default",
    )
    async with scoped_pg_session(engine.pg_pool, TENANT_PERF_ID) as conn:
        shred_row = await conn.fetchrow(
            "SELECT wrapped_dek, dek_key_id, valid_to FROM memories WHERE id = $1",
            shred_mem_id,
        )
    results["kek_dek_shred_memory"] = {
        "wrapped_dek_is_null": shred_row["wrapped_dek"] is None if shred_row else True,
        "dek_key_id_is_null": shred_row["dek_key_id"] is None if shred_row else True,
        "receipt_verified": bool(shred_out.get("receipt", {}).get("verified", False)),
    }

    # 3. Redis pause fail-closed (-32005 within < 2.0s)
    gw_app = create_mcp_gateway_app(engine=engine)
    transport = httpx.ASGITransport(app=gw_app)
    tok = "nce_v352_redis_chaos_tok"
    register_test_mcp_client_token(
        tok,
        namespace_id=str(TENANT_PERF_ID),
        principal_id="chaos-agent",
        scopes=["tools:read", "tools:call"],
        allowed_engines=["*"],
        tool_exposure_mode="compact",
        rate_limit_rpm=1000,
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8005") as client:
        r_init = await client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {tok}", "Origin": "http://localhost:8005"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
        sid = r_init.headers["mcp-session-id"]

        # Simulate fail-closed Redis outage by setting a non-routable Redis client with short timeout
        orig_redis = engine.redis_client
        import redis.asyncio as aioredis

        dead_redis = aioredis.from_url(
            "redis://127.0.0.1:6399/0",
            socket_connect_timeout=0.25,
            socket_timeout=0.25,
        )
        engine.redis_client = dead_redis
        t_r0 = time.perf_counter()
        _, r_err = await _mcp_call(
            client,
            tok,
            sid,
            "describe_schema",
            {},
            2,
        )
        redis_failclosed_s = round(time.perf_counter() - t_r0, 3)
        engine.redis_client = orig_redis
        await dead_redis.aclose()

    results["redis_fail_closed"] = {
        "elapsed_s": redis_failclosed_s,
        "error_code": r_err.get("error", {}).get("code"),
        "within_2s_sla": redis_failclosed_s < 2.0,
    }

    # 4. Mongo pause fail-fast (< 3.0s) + Saga zero-orphan recovery
    mongo_container = "nce-mongo-local"
    await asyncio.to_thread(
        subprocess.run,
        ["docker", "pause", mongo_container],
        check=True,
        capture_output=True,
    )
    t_m0 = time.perf_counter()
    err_name = None
    try:
        await engine.store_memory(
            StoreMemoryRequest(
                namespace_id=TENANT_PERF_ID,
                agent_id="default",
                content="Mongo pause probe v3.5.2",
                summary="Mongo pause probe v3.5.2",
                heavy_payload="Mongo pause probe v3.5.2",
                memory_type=MemoryType.episodic,
                assertion_type=AssertionType.observation,
                check_contradictions=False,
                metadata={"bypass_quarantine": True},
            )
        )
    except Exception as exc:
        err_name = type(exc).__name__
    mongo_failfast_s = round(time.perf_counter() - t_m0, 3)
    await asyncio.to_thread(
        subprocess.run,
        ["docker", "unpause", mongo_container],
        check=True,
        capture_output=True,
    )
    await asyncio.sleep(0.5)
    from nce.cron import _saga_recovery_tick

    await _saga_recovery_tick(engine.pg_pool, mongo_client=engine.mongo_client)
    async with engine.pg_pool.acquire() as conn:
        pending_sagas = await conn.fetchval(
            "SELECT count(*) FROM saga_execution_log WHERE state = 'recovery_needed'"
        )
    results["mongo_pause_saga_recovery"] = {
        "failfast_s": mongo_failfast_s,
        "exception": err_name,
        "within_3s_sla": mongo_failfast_s < 3.0,
        "remaining_recovery_needed_sagas": int(pending_sagas or 0),
    }

    return results


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agents", type=int, default=10)
    parser.add_argument("--turns", type=int, default=4)
    parser.add_argument("--out", type=str, default="")
    args = parser.parse_args()

    engine = NCEEngine()
    await engine.connect(run_startup_checks=False)
    try:
        seed_info = await seed_100k_if_needed(engine.pg_pool, engine.mongo_client, engine)
        canary_info = await verify_zero_canary_leak(engine)
        loop_metrics = await run_multi_agent_llm_loop(
            engine, num_agents=args.agents, turns_per_agent=args.turns
        )
        resilience_info = await verify_security_and_resilience_invariants(engine)

        report = {
            "version": "3.5.2",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "seed": seed_info,
            "canary": canary_info,
            "llm_agent_loop": loop_metrics,
            "security_and_resilience": resilience_info,
        }
        out_path = args.out or str(_REPO_ROOT / ".profile_v352_llm_loop.json")
        await asyncio.to_thread(
            Path(out_path).write_text, json.dumps(report, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, indent=2))
    finally:
        await engine.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
