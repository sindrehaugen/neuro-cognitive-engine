# NCE v3.5.3 Zero-Trust Hardening & Live Readiness Certification Report

**Release Baseline:** `neuro-cognitive-engine` `v3.5.3` (`nce-vertical-engines` `v4.2.1` integration verified)  
**Certification Date:** `2026-11-18`  
**Methodology:** Live 19-container Docker Compose stack on internal bridge isolation (`nce_edge`, `nce_data` internal, `nce_sandbox` internal), loopback Caddy edge ingress (`127.0.0.1:8082`), and reproducible pure-Core/3-layer test verification.

---

## 1. Executive Summary

Release `v3.5.3` closes post-audit database, cryptographic authentication, edge exposure, sandboxing, and real-inference gaps identified in `v3.5.2`:

| Control Domain | Remediation Shipped in `v3.5.3` | Live Verification Evidence |
| :--- | :--- | :--- |
| **PostgreSQL Partition & Key Confinement** | Migration `040_partition_rls_and_key_confinement.sql`: forced RLS + `(SELECT get_nce_namespace())` policies on `kg_node_embeddings` and every tenant child/default partition; WORM child partition `REVOKE UPDATE, DELETE, TRUNCATE`; `nce_ensure_event_log_monthly_partitions(int)` inherits RLS + WORM automatically; `public.signing_keys` revoked from `nce_app` in favor of `SECURITY DEFINER` metadata/unwrap helpers. | Live PG `memory_meta` catalog verified (`0` unforced tenant tables or child partitions); `nce_app` direct queries against `public.signing_keys` and child partition `kg_nodes_0` raise `InsufficientPrivilegeError`; `DELETE` on `event_log_default` denied; pure-Core `check_schema_drift.py` reports `NO DRIFT` (`43` Core SQL tables). |
| **Nonce-Bound HMAC-SHA256 Auth** | `nce/auth.py` canonical string bound to `METHOD\nPATH\nTIMESTAMP\nNONCE[\nSHA256(BODY)]` with regex validation `^[A-Za-z0-9._:-]{16,128}$` and Redis `SETNX` on `nce:hmac_nonce:{principal_scope}:{nonce}`. | Same-second burst (`10/10` identical `GET /api/health/v1` requests within timestamp `1795025872` carrying distinct nonces): `10/10` HTTP `200`; `(timestamp, nonce)` replay: HTTP `401`; mutated `X-NCE-Nonce` with captured signature: HTTP `401`. |
| **Liveness / Readiness Decoupling** | `nce/health_probe.py`: `< 3 ms` in-memory `GET /healthz` across Admin, A2A, MCP Gateway, Webhook Receiver, Portal, Odoo, and Salesforce; `5.0 s` asyncio singleflight `GET /readyz` coalescing pooled store probes. | Zero upstream `check_health` calls on `/healthz`; singleflight `/readyz` coalesces concurrent thundering herds to 1 upstream probe per 5 s window. Notice below: `BaseHTTPMiddleware`-wrapped ASGI apps (`admin`, `a2a`, `webhook-receiver`) exhibit a `~44 ms` Linux HTTP/1.1 delayed-ACK step on chunked responses relative to pure-ASGI/lean endpoints (`0.97–1.16 ms`), tracked as an optimization item. |
| **Single-Port Edge Proxy & Internal mTLS** | Base `docker-compose.yml` isolates all datastores/internal services onto `nce_data` (`internal: true`), binds host ports exclusively on `nce-caddy` (`127.0.0.1:8082`, `127.0.0.1:8443`), hardens `X-XSS-Protection "0"` and dynamic vertical upstreams, and adds `scripts/generate_internal_mtls.py` + `docker-compose.mtls.yml` + `deploy/Caddyfile.mtls`. | Only `nce-caddy` exposes host ports in live `docker ps`; mTLS overlay (`NCE_ENV=prod`, `--ssl-cert-reqs 2`, SAN `caddy.internal.nce`) passes HMAC `GET /api/health/v1` (`200 OK`) via Caddy while uncertified TLS 1.2/1.3 probes from `nce-a2a` to `https://admin:8003/readyz` fail closed (`SSLEOFError` / `RemoteProtocolError`). |
| **Worker Syscall Sandbox** | `deploy/seccomp/worker.json` denies 21 high-risk syscalls (`ptrace`, `kexec_load`, `mount`, `bpf`, `userfaultfd`, `pivot_root`, `unshare`, etc.) on `worker-1`, `worker-2`, and `cron`. | `/proc/self/status` inside running worker containers confirms `Seccomp: 2`, `Seccomp_filters: 2`. |
| **Real CPU Inference Stack** | `deploy/cognitive-service/` (`sentence-transformers/all-mpnet-base-v2`, 768-d L2-normalized + `spaCy` `en_core_web_sm`) replaces stub defaults; `assert_cognitive_not_stub_in_prod()` fails closed in `prod` if `CognitiveStubBackend` is active without explicit CI test flag. | Live `/api/health/v1` reports `cognitive.engine: "sentence-transformers"`, `model: "sentence-transformers/all-mpnet-base-v2"`, `status: "up"`; cosine similarity `0.664696` (related) vs `-0.022396` (unrelated), `l2_norm = 1.0`. |

---

## 2. Live HTTP Ingress & Service Health Latency

Measured with `scripts/benchmark_live_stack.py` against the live 19-container Compose topology (zero debug overlays attached). Rate-limited endpoints (`admin /api/health/v1` via `AdminHTTPRateLimitMiddleware` and `portal /healthz` via `CustomerRateLimitMiddleware` `120 req/min`) are bounded cleanly below rate limits (`n=30` and `n=50`); all unthrottled endpoints run `n=100` sequential + `c=10` concurrent (`n=100`).

### 2.1 Edge Loopback Ingress (`http://127.0.0.1:8082` via `nce-caddy`)

| Route | Mode / Auth | `n` | Seq Min (ms) | Seq p50 (ms) | Seq p95 (ms) | Seq p99 (ms) | Seq Max (ms) | C=10 p95 (ms) | C=10 RPS |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `/caddy-healthz` | Caddy static 200 | 100 | 1.286 | 1.494 | 1.808 | 2.153 | 2.506 | 33.868 | 541.71 |
| `/.well-known/agent-card` | A2A metadata | 100 | 2.951 | 3.677 | 4.764 | 5.567 | 6.335 | 77.193 | 245.96 |
| `/portal/healthz` | Portal liveness | 50 | 2.807 | 3.520 | 4.835 | 5.306 | 5.484 | 71.575 | 255.55 |
| `/odoo/healthz` | Odoo connector liveness | 100 | 2.619 | 3.548 | 4.695 | 5.196 | 16.399 | 90.011 | 267.48 |
| `/salesforce/healthz` | Salesforce connector liveness | 100 | 2.546 | 3.039 | 4.057 | 4.206 | 4.220 | 59.850 | 289.87 |
| `/healthz` | Admin liveness (`BaseHTTPMiddleware` stack) | 100 | 44.037 | 47.850 | 52.092 | 52.329 | 52.920 | 57.314 | 194.89 |
| `/readyz` | Admin 5s singleflight readiness | 100 | 44.936 | 48.095 | 52.033 | 56.133 | 72.785 | 57.602 | 201.25 |
| `/api/health/v1` | Admin deep check (nonce HMAC) | 30 | 77.189 | 80.489 | 89.345 | 100.377 | 104.413 | 286.261 | 46.70 |

### 2.2 Internal Docker Network Mesh (Inside `nce-admin`, Zero Host NAT)

| Target URL | `n` | Seq Min (ms) | Seq p50 (ms) | Seq p95 (ms) | Seq p99 (ms) | Seq Max (ms) | C=10 p95 (ms) | C=10 RPS |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `http://mcp-gateway:8005/healthz` | 100 | 0.823 | 0.972 | 1.116 | 1.137 | 1.189 | 18.844 | 872.21 |
| `http://mcp-gateway:8005/readyz` | 100 | 0.849 | 1.022 | 1.308 | 1.480 | 1.496 | 21.568 | 838.62 |
| `http://salesforce:8012/healthz` | 100 | 0.850 | 1.013 | 1.214 | 1.258 | 1.259 | 21.488 | 857.45 |
| `http://odoo:8011/healthz` | 100 | 0.848 | 1.025 | 1.186 | 1.308 | 1.419 | 19.939 | 853.76 |
| `http://portal:8010/healthz` | 50 | 0.991 | 1.156 | 1.816 | 2.404 | 2.655 | 20.765 | 852.69 |
| `http://127.0.0.1:8003/healthz` | 100 | 42.916 | 44.048 | 48.082 | 48.267 | 48.467 | 48.282 | 227.45 |
| `http://127.0.0.1:8003/readyz` | 100 | 42.901 | 44.102 | 48.006 | 48.502 | 48.614 | 60.322 | 217.73 |
| `http://a2a:8004/healthz` | 100 | 41.208 | 44.082 | 48.137 | 48.357 | 48.360 | 48.109 | 223.29 |
| `http://a2a:8004/readyz` | 100 | 42.470 | 44.050 | 48.105 | 48.365 | 48.557 | 51.120 | 222.86 |
| `http://webhook-receiver:8080/healthz` | 100 | 41.945 | 44.027 | 48.150 | 48.376 | 48.629 | 48.162 | 223.34 |

> **Architectural Telemetry Note:** `mcp-gateway`, `portal`, `odoo`, and `salesforce` respond in **`0.97–1.16 ms` p50** internally (`838–872 RPS`), whereas `admin`, `a2a`, and `webhook-receiver` spend `44.0 ms` per HTTP/1.1 keep-alive frame due to Starlette `BaseHTTPMiddleware` streaming headers/body separately across TCP Delayed ACK. None of `/healthz` or `/readyz` invoke PostgreSQL, Redis, or MinIO on cache hit.

---

## 3. Quad-Stack Store & Real 768-d Cognitive Pipeline Latency

Measured inside `nce-admin` (`CognitiveRemoteBackend` backed by `http://cognitive:11435` running CPU `sentence-transformers/all-mpnet-base-v2`, `dimension = 768`, `l2_norm = 1.0`, plus `spaCy` entity/SVO graph extraction):

| Operation | `n` | Min (ms) | p50 (ms) | p95 (ms) | p99 (ms) | Max (ms) | Mean (ms) | Notes |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| PostgreSQL RLS `SELECT count(*)` (`nce_app`) | 100 | 0.612 | 0.701 | 1.043 | 1.245 | 2.188 | 0.771 | Inside `scoped_pg_session` |
| Redis `SET EX` + `PING` | 100 | 0.228 | 0.279 | 0.438 | 0.768 | 3.397 | 0.333 | `nce-redis` AUTH enabled |
| MongoDB `ping` | 100 | 0.282 | 0.336 | 0.511 | 0.883 | 1.007 | 0.374 | Motor connection pool |
| MinIO `bucket_exists` | 100 | 0.783 | 0.940 | 1.139 | 1.990 | 2.062 | 0.976 | S3 HTTP API on `nce_data` |
| Cognitive `/v1/embeddings` (sequential) | 100 | 35.396 | 44.506 | 81.598 | 109.204 | 126.679 | 49.441 | `all-mpnet-base-v2`, 768-d |
| Cognitive `/v1/embeddings` (`c=10` concurrent) | 100 | 68.767 | 548.228 | 657.197 | 685.054 | 692.519 | 531.517 | `18.03 RPS` (`5.547 s` wall) |
| `store_memory` (768-d + spaCy + Saga) | 20 | 223.964 | 369.438 | 481.254 | 534.109 | 547.323 | 367.673 | Mongo + PG + KG + Redis |
| `semantic_search` (768-d RRF + FTS + decrypt) | 50 | 12.083 | 16.542 | 23.852 | 73.014 | 92.285 | 18.788 | Warm query embedding cache |
| `graph_search` (GraphRAG BFS `max_depth=2`) | 50 | 10.060 | 12.247 | 23.852 | 66.361 | 103.717 | 15.312 | Pgvector anchor + BFS |
| `compile_working_memory` (`max_tokens=1500`) | 20 | 24.451 | 34.500 | 65.387 | 205.027 | 239.937 | 44.954 | Token-budgeted assembly |
| End-to-End 5-Tool Sequential MCP Turn | 10 | 166.105 | 213.657 | 338.928 | 374.366 | 383.226 | 228.520 | `store` -> `semantic` -> `graph` -> `recent` -> `verify` |

**Semantic Quality Check (`sentence-transformers/all-mpnet-base-v2`, 768-d):**
- Related query vs. memory vector cosine similarity: **`0.664696`**
- Unrelated query vs. memory vector cosine similarity: **`-0.022396`**
- RRF search ranker score (`k = 60`): `0.0164` (top-ranked related hit) vs. `0.0161` (fallback rank)

---

## 4. Test & Verification Summary

| Suite / Environment | Result | Notes |
| :--- | :--- | :--- |
| Core Pure-Core Schema Drift (`scripts/check_schema_drift.py`) | `NO DRIFT` | Verified `43` Core SQL tables (`33` `schema.sql` + `10` migration-created) including migration `040`. |
| Core Integration Suite (disposable pure-Core PG + Mongo + MinIO + Redis + live `cognitive`) | `158 passed, 2 skipped, 0 failed` | Skips are legacy 2017/2019 check-constraint fixture guards in `tests/test_event_retention.py`; Citus (`4`), alt-port isolated stack (`2`), throwaway MinIO `:9004` (`5`), and Linux RQ recovery (`2`, verified below) run separately. |
| Core Linux Container POSIX + Redis Recovery (`test_worker_inflight_recovery.py`, `test_v33_zero_trust_suite.py`, `test_subprocess_rlimit.py`) | `20 passed, 0 failed, 0 skipped` | Executed inside `neuro-cognitive-engine-admin` container sharing Redis netns. |
| Core DB-Gated Unit & Engine Suites (`test_integration_engine.py`, `test_event_log_worm_privileges_pg.py`, `test_key_durability.py`, `test_vector_tenancy.py`) | `39 passed, 0 failed, 0 skipped` | Verified against disposable pure-Core PG + live `cognitive`. |
| Core Unit Suite (`pytest tests/ -m "not integration and not perf and not live"`) | `3540 passed` (unit) + `17 passed` (`test_integration_engine` clean skip) | Executed cleanly with CI environment isolation after debug ports were removed (`test_v33_zero_trust_suite.py` verified `100%` in Linux above). |
| Live 3-Layer `verify_v1_launch.py` (`nce-admin` -> `http://caddy:80`) | `RESULT: PASS` | Verified `/api/health/v1`, A2A agent card, tenant RLS isolation, `signing_keys` denial, partition privilege denial, and WORM `event_log`. |
| Internal Caddy <-> Admin mTLS Overlay (`docker-compose.mtls.yml`, `NCE_ENV=prod`) | `PASS` | Proxied HMAC request `200 OK`; uncertified TLS 1.2/1.3 probe from `nce-a2a` rejected (`SSLEOFError` / `RemoteProtocolError`). |

---

## 5. Production Deployment Readiness Checklist

1. **Secrets Architecture:** Ensure `deploy/secrets/nce_master_key`, `nce_api_key`, `nce_admin_api_key`, `nce_mcp_api_key`, and non-default `POSTGRES_PASSWORD`/`REDIS_PASSWORD`/`MINIO_ROOT_PASSWORD` are provisioned outside git.
2. **Internal mTLS Activation:** Run `python scripts/generate_internal_mtls.py` (or mount CA/leaf certs from your PKI) and layer `docker-compose.mtls.yml` with `NCE_ENV=prod`.
3. **Edge Proxy & Firewall:** Keep only `nce-caddy` ports (`8082` / `8443` or `80` / `443`) exposed on the host interface; never publish `postgres`, `redis`, `mongo`, `minio`, or internal worker ports on non-loopback interfaces.
4. **Pure-ASGI Optimization Candidate:** Converting `AdminHTTPRateLimitMiddleware`, `HMACAuthMiddleware`, `a2a`, and `webhook-receiver` middlewares from `BaseHTTPMiddleware` to pure ASGI middleware removes the `44 ms` TCP delayed-ACK step on HTTP/1.1 internal health probes (`44 ms -> ~1 ms`, matching `mcp-gateway`).
