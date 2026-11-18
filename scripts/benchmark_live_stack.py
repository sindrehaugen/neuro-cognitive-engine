#!/usr/bin/env python3
"""Live stack hardness, concurrency, HMAC burst, Quad-Stack, and cognitive pipeline benchmarks.

Run against Caddy edge ingress on the host:
  python scripts/benchmark_live_stack.py --mode edge --edge-url http://127.0.0.1:8082

Run internal container, Quad-Stack, and cognitive benchmarks inside nce-admin:
  Get-Content scripts/benchmark_live_stack.py -Raw | docker exec -i nce-admin python - --mode internal
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import math
import os
import secrets
import statistics
import time
import uuid
from pathlib import Path
from typing import Any

import httpx


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Compute linear-interpolated percentile from sorted millisecond samples."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = (pct / 100.0) * (len(sorted_vals) - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return sorted_vals[lo]
    frac = rank - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def _summarize(samples_ms: list[float], elapsed_s: float | None = None) -> dict[str, Any]:
    """Build deterministic latency summary dictionary."""
    s = sorted(samples_ms)
    out: dict[str, Any] = {
        "n": len(s),
        "min_ms": round(s[0], 3),
        "p50_ms": round(_percentile(s, 50.0), 3),
        "p95_ms": round(_percentile(s, 95.0), 3),
        "p99_ms": round(_percentile(s, 99.0), 3),
        "max_ms": round(s[-1], 3),
        "mean_ms": round(statistics.fmean(s), 3),
    }
    if elapsed_s is not None and elapsed_s > 0:
        out["rps"] = round(len(s) / elapsed_s, 2)
        out["wall_s"] = round(elapsed_s, 3)
    return out


def _resolve_api_key() -> str:
    """Read HMAC key from NCE_API_KEY_FILE, NCE_API_KEY, or repo deploy secret."""
    key_file = os.environ.get("NCE_API_KEY_FILE", "").strip()
    if key_file and Path(key_file).is_file():
        return Path(key_file).read_text(encoding="utf-8-sig").strip()
    env_key = os.environ.get("NCE_API_KEY", "").strip()
    if env_key:
        return env_key
    local_secret = Path(__file__).resolve().parents[1] / "deploy" / "secrets" / "nce_api_key"
    if local_secret.is_file():
        return local_secret.read_text(encoding="utf-8-sig").strip()
    raise RuntimeError("Could not resolve NCE_API_KEY for authenticated benchmark probes")


def _sign_hmac_headers(
    api_key: str,
    method: str,
    path: str,
    body: bytes = b"",
    *,
    timestamp: str | None = None,
    nonce: str | None = None,
) -> tuple[dict[str, str], str, str, str]:
    """Return nonce-bound HMAC-SHA256 headers and cryptographic tuple."""
    ts = timestamp or str(int(time.time()))
    nc = nonce or secrets.token_urlsafe(24)
    parts = [method.upper(), path, ts, nc]
    if body:
        parts.append(hashlib.sha256(body).hexdigest())
    canonical = "\n".join(parts)
    sig = hmac.new(api_key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    headers = {
        "Authorization": f"HMAC-SHA256 {sig}",
        "X-NCE-Timestamp": ts,
        "X-NCE-Nonce": nc,
    }
    return headers, ts, nc, sig


async def _bench_http_target(
    client: httpx.AsyncClient,
    name: str,
    url_or_path: str,
    *,
    n_seq: int = 100,
    n_conc: int = 100,
    concurrency: int = 10,
    api_key: str | None = None,
    sign_path: str | None = None,
) -> dict[str, Any]:
    """Measure sequential N=100 and concurrent C=10 HTTP latency on one endpoint."""

    def _headers() -> dict[str, str]:
        if api_key and sign_path:
            hdrs, _, _, _ = _sign_hmac_headers(api_key, "GET", sign_path)
            return hdrs
        return {}

    # Warm-up request (singleflight cache fill where applicable, excludes JIT/DNS startup)
    warm = await client.get(url_or_path, headers=_headers())
    if warm.status_code != 200:
        raise RuntimeError(f"Warmup failed on {name} ({url_or_path}): HTTP {warm.status_code}")

    seq_samples: list[float] = []
    for _ in range(n_seq):
        hdrs = _headers()
        t0 = time.perf_counter()
        resp = await client.get(url_or_path, headers=hdrs)
        dt = (time.perf_counter() - t0) * 1000.0
        if resp.status_code != 200:
            raise RuntimeError(f"Sequential probe failed on {name}: HTTP {resp.status_code}")
        seq_samples.append(dt)

    sem = asyncio.Semaphore(concurrency)
    conc_samples: list[float] = []

    async def _one_conc() -> None:
        async with sem:
            hdrs = _headers()
            t0 = time.perf_counter()
            resp = await client.get(url_or_path, headers=hdrs)
            dt = (time.perf_counter() - t0) * 1000.0
            if resp.status_code != 200:
                raise RuntimeError(f"Concurrent probe failed on {name}: HTTP {resp.status_code}")
            conc_samples.append(dt)

    wall0 = time.perf_counter()
    await asyncio.gather(*(_one_conc() for _ in range(n_conc)))
    wall_s = time.perf_counter() - wall0

    return {
        "sequential": _summarize(seq_samples),
        f"concurrent_c{concurrency}": _summarize(conc_samples, elapsed_s=wall_s),
    }


async def _run_hmac_burst_proof(client: httpx.AsyncClient, api_key: str) -> dict[str, Any]:
    """Verify 10x identical same-second requests with distinct nonces pass, replays/tampering fail."""
    # Align to start of an integer second so all 10 requests share one timestamp second.
    frac = time.time() % 1.0
    if frac > 0.45:
        await asyncio.sleep(1.05 - frac)

    ts = str(int(time.time()))
    burst_items = [
        _sign_hmac_headers(api_key, "GET", "/api/health/v1", timestamp=ts) for _ in range(10)
    ]
    t0 = time.perf_counter()
    responses = await asyncio.gather(
        *(client.get("/api/health/v1", headers=hdrs) for hdrs, _, _, _ in burst_items)
    )
    burst_ms = (time.perf_counter() - t0) * 1000.0
    statuses = [r.status_code for r in responses]
    if statuses != [200] * 10:
        raise RuntimeError(f"Expected 10/10 200 OK on same-second burst; got {statuses}")

    # Exact replay of (ts, nonce_0, sig_0) must return 401.
    hdr0, ts0, nc0, sig0 = burst_items[0]
    replay_resp = await client.get("/api/health/v1", headers=hdr0)
    if replay_resp.status_code != 401:
        raise RuntimeError(f"Expected 401 on HMAC nonce replay; got {replay_resp.status_code}")

    # Tampered X-NCE-Nonce carrying sig_0 must fail HMAC verification (401).
    tampered_hdrs = {
        "Authorization": f"HMAC-SHA256 {sig0}",
        "X-NCE-Timestamp": ts0,
        "X-NCE-Nonce": secrets.token_urlsafe(24),
    }
    tampered_resp = await client.get("/api/health/v1", headers=tampered_hdrs)
    if tampered_resp.status_code != 401:
        raise RuntimeError(f"Expected 401 on tampered X-NCE-Nonce; got {tampered_resp.status_code}")

    return {
        "same_second_timestamp": ts,
        "burst_count": 10,
        "burst_statuses": statuses,
        "burst_wall_ms": round(burst_ms, 3),
        "replay_status": replay_resp.status_code,
        "mutated_nonce_status": tampered_resp.status_code,
    }


async def run_edge_benchmarks(edge_url: str, n_seq: int) -> dict[str, Any]:
    """Run all edge Caddy ingress benchmarks + sub-second HMAC burst proof."""
    api_key = _resolve_api_key()
    limits = httpx.Limits(max_connections=32, max_keepalive_connections=16)
    async with httpx.AsyncClient(
        base_url=edge_url.rstrip("/"), timeout=30.0, limits=limits
    ) as client:
        routes: dict[str, Any] = {}
        for name, path, needs_auth, route_n in (
            ("caddy_healthz", "/caddy-healthz", False, n_seq),
            ("admin_healthz_edge", "/healthz", False, n_seq),
            ("admin_readyz_edge", "/readyz", False, n_seq),
            ("admin_api_health_v1_hmac", "/api/health/v1", True, min(n_seq, 30)),
            ("a2a_agent_card_edge", "/.well-known/agent-card", False, n_seq),
            ("portal_healthz_edge", "/portal/healthz", False, min(n_seq, 50)),
            ("odoo_healthz_edge", "/odoo/healthz", False, n_seq),
            ("salesforce_healthz_edge", "/salesforce/healthz", False, n_seq),
        ):
            routes[name] = await _bench_http_target(
                client,
                name,
                path,
                n_seq=route_n,
                n_conc=route_n,
                concurrency=10,
                api_key=api_key if needs_auth else None,
                sign_path=path if needs_auth else None,
            )

        hmac_proof = await _run_hmac_burst_proof(client, api_key)
        health_detail = (
            await client.get(
                "/api/health/v1", headers=_sign_hmac_headers(api_key, "GET", "/api/health/v1")[0]
            )
        ).json()

    return {
        "mode": "edge",
        "edge_url": edge_url,
        "routes": routes,
        "hmac_nonce_burst_and_replay": hmac_proof,
        "api_health_v1_snapshot": health_detail,
    }


def _cosine(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b, strict=True))
    da = math.sqrt(sum(x * x for x in a))
    db = math.sqrt(sum(y * y for y in b))
    return num / (da * db) if da > 0 and db > 0 else 0.0


async def run_internal_benchmarks(n_seq: int) -> dict[str, Any]:
    """Run internal mesh liveness/readiness, Quad-Stack, and 768-d cognitive benchmarks."""
    from nce.db_utils import scoped_pg_session
    from nce.models import GraphSearchRequest, StoreMemoryRequest
    from nce.orchestrator import NCEEngine
    from nce.working_memory import compile_working_memory

    internal_http: dict[str, Any] = {}
    limits = httpx.Limits(max_connections=32, max_keepalive_connections=16)
    async with httpx.AsyncClient(timeout=15.0, limits=limits) as client:
        for name, url, route_n in (
            ("admin_healthz_internal", "http://127.0.0.1:8003/healthz", n_seq),
            ("admin_readyz_internal", "http://127.0.0.1:8003/readyz", n_seq),
            ("a2a_healthz_internal", "http://a2a:8004/healthz", n_seq),
            ("a2a_readyz_internal", "http://a2a:8004/readyz", n_seq),
            ("mcp_gateway_healthz_internal", "http://mcp-gateway:8005/healthz", n_seq),
            ("mcp_gateway_readyz_internal", "http://mcp-gateway:8005/readyz", n_seq),
            ("webhook_healthz_internal", "http://webhook-receiver:8080/healthz", n_seq),
            ("portal_healthz_internal", "http://portal:8010/healthz", min(n_seq, 50)),
            ("odoo_healthz_internal", "http://odoo:8011/healthz", n_seq),
            ("salesforce_healthz_internal", "http://salesforce:8012/healthz", n_seq),
        ):
            internal_http[name] = await _bench_http_target(
                client,
                name,
                url,
                n_seq=route_n,
                n_conc=route_n,
                concurrency=10,
            )

        # Direct Cognitive sidecar embedding benchmark
        async def _embed_one(text: str) -> tuple[float, list[float]]:
            t0 = time.perf_counter()
            r = await client.post(
                "http://cognitive:11435/v1/embeddings",
                json={"input": [text]},
            )
            dt = (time.perf_counter() - t0) * 1000.0
            if r.status_code != 200:
                raise RuntimeError(f"Cognitive embedding failed: HTTP {r.status_code} {r.text}")
            vec = r.json()["data"][0]["embedding"]
            return dt, vec

        await _embed_one("warmup text for sentence transformers")
        cog_seq: list[float] = []
        sample_norm = 0.0
        sample_dim = 0
        for idx in range(n_seq):
            dt, vec = await _embed_one(
                f"Service telemetry sample {idx} confirms PostgreSQL RLS policy."
            )
            cog_seq.append(dt)
            if idx == 0:
                sample_dim = len(vec)
                sample_norm = round(math.sqrt(sum(v * v for v in vec)), 6)

        sem = asyncio.Semaphore(10)
        cog_conc: list[float] = []

        async def _cog_one(idx: int) -> None:
            async with sem:
                dt, _ = await _embed_one(
                    f"Concurrent embedding batch item {idx} checks caddy tls routing."
                )
                cog_conc.append(dt)

        w0 = time.perf_counter()
        await asyncio.gather(*(_cog_one(i) for i in range(n_seq)))
        cog_wall = time.perf_counter() - w0

        _, vec_pg = await _embed_one(
            "PostgreSQL row level security enforces tenant isolation across partitions"
        )
        _, vec_rel = await _embed_one(
            "Database partition RLS policies isolate tenant records cleanly"
        )
        _, vec_unrel = await _embed_one("Fresh strawberries and vanilla ice cream dessert recipe")
        cosine_related = round(_cosine(vec_pg, vec_rel), 6)
        cosine_unrelated = round(_cosine(vec_pg, vec_unrel), 6)

    # Quad-Stack & End-to-End Engine Pipeline Benchmarks
    engine = NCEEngine()
    await engine.connect()
    ns_id: str | None = None
    bench_slug = f"bench-v353-{uuid.uuid4().hex[:8]}"
    try:
        async with engine.pg_pool.acquire() as conn:
            ns_uuid = await conn.fetchval(
                "INSERT INTO namespaces (slug) VALUES ($1) RETURNING id", bench_slug
            )
            ns_id = str(ns_uuid)

        # Quad-Stack raw store latency (N=100)
        pg_times: list[float] = []
        for _ in range(n_seq):
            t0 = time.perf_counter()
            async with scoped_pg_session(engine.pg_pool, ns_id) as sconn:
                await sconn.fetchval("SELECT count(*) FROM memories")
            pg_times.append((time.perf_counter() - t0) * 1000.0)

        redis_times: list[float] = []
        rkey = f"bench:v353:{uuid.uuid4().hex}"
        for _ in range(n_seq):
            t0 = time.perf_counter()
            await engine.redis_client.set(rkey, "1", ex=30)
            await engine.redis_client.ping()
            redis_times.append((time.perf_counter() - t0) * 1000.0)
        await engine.redis_client.delete(rkey)

        mongo_times: list[float] = []
        for _ in range(n_seq):
            t0 = time.perf_counter()
            await engine.mongo_client.admin.command("ping")
            mongo_times.append((time.perf_counter() - t0) * 1000.0)

        minio_times: list[float] = []
        for _ in range(n_seq):
            t0 = time.perf_counter()
            await asyncio.to_thread(engine.minio_client.bucket_exists, "nce-artifacts")
            minio_times.append((time.perf_counter() - t0) * 1000.0)

        # Cognitive pipeline (store_memory, semantic_search, graph_search, working_memory, 5-tool MCP turn)
        corpus = [
            "Alice configured Caddy reverse proxy with mutual TLS client certificate verification for internal admin traffic.",
            "PostgreSQL row-level security policies isolate tenant partitions across memory_embeddings and kg_nodes.",
            "Redis SETNX with composite principal and nonce keys rejects replayed HMAC-SHA256 requests in constant time.",
            "CPU inference service runs sentence-transformers all-mpnet-base-v2 for 768-dimensional semantic vectors.",
            "Worker containers apply a strict Linux seccomp syscall filter blocking ptrace, bpf, and mount calls.",
        ]
        store_times: list[float] = []
        stored_ids: list[str] = []
        for idx in range(20):
            txt = f"{corpus[idx % len(corpus)]} Sample record #{idx}."
            t0 = time.perf_counter()
            res = await engine.store_memory(
                StoreMemoryRequest(
                    namespace_id=uuid.UUID(ns_id),
                    agent_id="bench-agent",
                    content=txt,
                    summary=txt,
                    heavy_payload=txt,
                )
            )
            store_times.append((time.perf_counter() - t0) * 1000.0)
            stored_ids.append(str(res["payload_ref"]))

        sem_times: list[float] = []
        for _ in range(50):
            t0 = time.perf_counter()
            await engine.semantic_search(
                "How does Caddy authenticate upstream connections to the admin service?",
                namespace_id=ns_id,
                agent_id="bench-agent",
                limit=3,
            )
            sem_times.append((time.perf_counter() - t0) * 1000.0)

        top_rel = await engine.semantic_search(
            "How does Caddy authenticate upstream connections to the admin service?",
            namespace_id=ns_id,
            agent_id="bench-agent",
            limit=3,
        )
        top_unrel = await engine.semantic_search(
            "tropical banana smoothie blender recipe without dairy",
            namespace_id=ns_id,
            agent_id="bench-agent",
            limit=3,
        )

        graph_times: list[float] = []
        for _ in range(50):
            t0 = time.perf_counter()
            await engine.graph_search(
                GraphSearchRequest(
                    namespace_id=uuid.UUID(ns_id),
                    agent_id="bench-agent",
                    query="Caddy mutual TLS admin verification",
                    max_depth=2,
                    anchor_top_k=3,
                )
            )
            graph_times.append((time.perf_counter() - t0) * 1000.0)

        wm_times: list[float] = []
        for _ in range(20):
            t0 = time.perf_counter()
            await compile_working_memory(
                engine,
                goal="Summarize mutual TLS, PostgreSQL partition RLS, and Redis nonce security controls",
                namespace_id=ns_id,
                agent_id="bench-agent",
                max_tokens=1500,
            )
            wm_times.append((time.perf_counter() - t0) * 1000.0)

        mcp_turn_times: list[float] = []
        for idx in range(10):
            t0 = time.perf_counter()
            turn_res = await engine.store_memory(
                StoreMemoryRequest(
                    namespace_id=uuid.UUID(ns_id),
                    agent_id="bench-agent",
                    content=f"Turn {idx}: Bob verified Event Log WORM child partition privileges on PostgreSQL.",
                    summary=f"Turn {idx}: Bob verified WORM privileges.",
                    heavy_payload=f"Turn {idx}: full audit verification payload.",
                )
            )
            await engine.semantic_search(
                "Event Log WORM child partition verification",
                namespace_id=ns_id,
                agent_id="bench-agent",
                limit=3,
            )
            await engine.graph_search(
                GraphSearchRequest(
                    namespace_id=uuid.UUID(ns_id),
                    agent_id="bench-agent",
                    query="Bob Event Log WORM",
                    max_depth=2,
                    anchor_top_k=3,
                )
            )
            recents = await engine.recall_recent(ns_id, agent_id="bench-agent", limit=5)
            async with scoped_pg_session(engine.pg_pool, ns_id) as vconn:
                mem_uuid = await vconn.fetchval(
                    "SELECT id FROM memories WHERE payload_ref = $1", turn_res["payload_ref"]
                )
            await engine.verify_memory(str(mem_uuid or recents[0]["id"]), namespace_id=ns_id)
            mcp_turn_times.append((time.perf_counter() - t0) * 1000.0)

        return {
            "mode": "internal",
            "internal_http": internal_http,
            "cognitive_embeddings_v1": {
                "dimension": sample_dim,
                "l2_norm": sample_norm,
                "cosine_related": cosine_related,
                "cosine_unrelated": cosine_unrelated,
                "sequential": _summarize(cog_seq),
                "concurrent_c10": _summarize(cog_conc, elapsed_s=cog_wall),
            },
            "quad_stack_stores": {
                "postgresql_rls_select": _summarize(pg_times),
                "redis_set_ping": _summarize(redis_times),
                "mongodb_ping": _summarize(mongo_times),
                "minio_bucket_exists": _summarize(minio_times),
            },
            "cognitive_pipeline": {
                "store_memory_768d_spacy": _summarize(store_times),
                "semantic_search_768d": _summarize(sem_times),
                "semantic_top_score_related": round(float(top_rel[0]["score"]), 4)
                if top_rel
                else None,
                "semantic_top_score_unrelated": round(float(top_unrel[0]["score"]), 4)
                if top_unrel
                else None,
                "graph_search_bfs": _summarize(graph_times),
                "compile_working_memory": _summarize(wm_times),
                "mcp_5tool_turn": _summarize(mcp_turn_times),
            },
        }
    finally:
        if ns_id is not None:
            try:
                # Delete test namespace using migration/DDL pool if configured, else pg_pool.
                ddl_dsn = os.environ.get("NCE_MIGRATION_PG_DSN", "").strip()
                if ddl_dsn:
                    import asyncpg

                    from nce.master_key_registry import rls_visibility_problem

                    c = await asyncpg.connect(ddl_dsn)
                    try:
                        await rls_visibility_problem(c)
                        await c.execute("DELETE FROM namespaces WHERE id = $1::uuid", ns_id)
                    finally:
                        await c.close()
            except Exception:
                pass
        await engine.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Live NCE hardening & latency benchmark runner")
    parser.add_argument("--mode", choices=["edge", "internal"], required=True)
    parser.add_argument("--edge-url", default="http://127.0.0.1:8082")
    parser.add_argument("--n", type=int, default=100)
    args = parser.parse_args()

    if args.mode == "edge":
        result = asyncio.run(run_edge_benchmarks(args.edge_url, args.n))
    else:
        result = asyncio.run(run_internal_benchmarks(args.n))

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
