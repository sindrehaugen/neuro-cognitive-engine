# NCE `v3.5.4` Pure-ASGI, Baked Air-Gapped ONNX & Hardening Certification Report

> **Benchmark Scope Notice:** Figures in this report were captured on a controlled single-host Docker container testbed against synthetic R&D datasets. They document engineering regression baselines and index/middleware deltas, not commercial multi-tenant SLA guarantees.

> **Status:** shipped · **Verified-against:** 6b20959 (main) · **Last-audited:** 2026-11-18

**Release:** `neuro-cognitive-engine` `v3.5.4`  
**Methodology:** Live 19-container Docker Compose stack on isolated bridge planes (`nce_edge`, `nce_data` internal, `nce_sandbox` internal), loopback Caddy edge ingress (`127.0.0.1:8082` / `127.0.0.1:8443`), live air-gapped CPU ONNX inference (`nce-cognitive:3.5.4` with `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`), and container-native integration/POSIX/DB verification against disposable test databases.

---

## 1. Executive Summary (`v3.5.3` -> `v3.5.4` Delta)

NCE `v3.5.4` eliminates the HTTP/1.1 Delayed-ACK floor, replaces heavy runtime `PyTorch` container pulls on `nce-cognitive` and `nce-cron` with a baked air-gapped ONNX Runtime engine, enforces seccomp confinement on `nce-cron`, eliminates idle polling CPU spikes, removes skipped CI/integration paths in offline & POSIX environments, and upgrades GitHub release workflows to Node 24:

| Optimization / Hardening Area | Prior (`v3.5.3`) | Verified (`v3.5.4`) | Root Change & Proof |
| :--- | :---: | :---: | :--- |
| **Pure-ASGI Streaming + `TCP_NODELAY`** (`admin` / `a2a` / `webhook-receiver` `/healthz` internal `p50`) | `44.03–44.08 ms` | **`1.249–1.573 ms`** (`622.70–796.51 RPS`) | Replaced all `BaseHTTPMiddleware` / `@app.middleware("http")` wrappers with pure-ASGI streaming middlewares + one-shot `enable_tcp_nodelay_for_current_process()` socket option. |
| **Baked Air-Gapped ONNX Embeddings** (`/v1/embeddings` `c=1` `p50` / `c=10` `p50` & `RPS`) | `44.506 ms` / `548.228 ms` (`18.03 RPS`) | **`31.931 ms`** / **`177.800 ms`** (`53.75 RPS`) | Exported `sentence-transformers/all-mpnet-base-v2` to `model.onnx` FP32 + `ORT_ENABLE_EXTENDED` graph optimizations + `EmbeddingMicroBatcher` (`5 ms` / `32`); live PyTorch cosine parity `min_cosine = 0.9999999403953552` (`max_norm_err = 5.96e-08`). |
| **`store_memory` 768-d Pipeline** (`p50`) | `369.438 ms` | **`309.148 ms`** | Pooled per-loop `httpx.AsyncClient` and `_AsyncBatchCoalescer` in `nce/embeddings.py` + ONNX cognitive service. |
| **`nce-cron` RSS & Seccomp Posture** | `442.30 MiB` (`86.39%`), no seccomp | **`131.5 MiB` cgroup (`25.67%`, `VmRSS 151,916 kB`)**, `Seccomp: 2` | `ReembeddingWorker._vram_pressure_gate()` skips `import torch` when `NCE_COGNITIVE_BASE_URL` delegates remotely (`TORCH_MAP_HITS=0` in `/proc/1/maps`); `cron` shares `deploy/seccomp/worker.json`. |
| **Idle CPU Usage** (`nce-mongo` / `webhook-receiver`) | `44.86%` / `9.58%` | **`0.44%`** / **`0.49%`** | `30s` `mongosh --quiet --norc` ping and `30s` (`start_interval: 2s`) HTTP service healthchecks across Compose services. |
| **Offline Code AST & Container Skips** | `2` retention skips + offline tree-sitter failure | **`0 skipped, 0 failed`** (`63/63` Linux container tests) | Added stdlib `ast` Python fallback in `nce/ast_parser.py`, `REDIS_URL` env lookup in `test_worker_inflight_recovery.py`, and valid 32-byte `chain_hash` in `test_event_retention.py`. |
| **GitHub Actions Node 24 Compliance** | `action-gh-release@v2` | **`0` Node 20 annotations** | Pinned `softprops/action-gh-release@8a0cd651e5b2365bd640854a3fa5dfc70e4875f3` (`v3`), `actions/checkout@v7`, `actions/setup-python@v7`. |

---

## 2. Live HTTP Ingress & Service Health Latency

Measured via `scripts/benchmark_live_stack.py` against the live 19-container Compose stack (`v3.5.4` + `nce-vertical-engines` `4.2.2`, zero debug overlays). `admin /api/health/v1` and `portal /healthz` are sampled at `n=30` and `n=50` inside rate-limiter budgets; all other HTTP health endpoints run `n=100` sequential and `c=10` concurrent (`n=100`).

### 2.1 Edge Loopback Ingress (`http://127.0.0.1:8082` via `nce-caddy`)

| Route | Mode / Auth | `n` | Seq Min (ms) | Seq p50 (ms) | Seq p95 (ms) | Seq p99 (ms) | Seq Max (ms) | C=10 p95 (ms) | C=10 RPS |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `/caddy-healthz` | Caddy static `200` | 100 | 3.186 | 3.984 | 5.436 | 6.117 | 7.127 | 69.290 | 247.86 |
| `/healthz` | Admin pure-ASGI liveness | 100 | 3.964 | **5.459** | 11.747 | 29.116 | 56.666 | 118.308 | 159.10 |
| `/readyz` | Admin `5s` singleflight readiness | 100 | 4.506 | **6.068** | 7.379 | 7.800 | 7.821 | 128.178 | 157.11 |
| `/api/health/v1` | Admin deep check (nonce HMAC) | 30 | 68.388 | 87.469 | 124.567 | 144.971 | 151.625 | 1051.236 | 20.94 |
| `/.well-known/agent-card` | A2A metadata | 100 | 3.599 | 4.547 | 6.144 | 7.406 | 8.179 | 80.915 | 251.70 |
| `/portal/healthz` | Portal liveness | 50 | 3.331 | 4.507 | 5.797 | 6.373 | 6.558 | 122.498 | 150.58 |
| `/odoo/healthz` | Odoo connector liveness | 100 | 3.654 | 4.538 | 6.052 | 6.799 | 12.026 | 73.006 | 248.67 |
| `/salesforce/healthz` | Salesforce connector liveness | 100 | 3.413 | 3.933 | 4.803 | 5.456 | 5.807 | 67.861 | 285.09 |

- **Nonce-Bound Same-Second HMAC Burst:** `10/10` identical `GET /api/health/v1` requests sharing integer second `1795035743` with distinct `X-NCE-Nonce` headers returned HTTP `200` in `293.634 ms` wall time; exact replay returned HTTP `401`; mutated `X-NCE-Nonce` with captured signature returned HTTP `401`.

### 2.2 Internal Docker Network Mesh (`nce_data`, Zero Host NAT)

| Target Endpoint | `n` | Seq Min (ms) | Seq p50 (ms) | Seq p95 (ms) | Seq p99 (ms) | Seq Max (ms) | C=10 p95 (ms) | C=10 RPS |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `http://127.0.0.1:8003/healthz` (`admin`) | 100 | 0.911 | **1.249** | 1.526 | 1.599 | 1.643 | 23.226 | **796.51** |
| `http://127.0.0.1:8003/readyz` (`admin`) | 100 | 1.104 | **1.413** | 1.722 | 1.808 | 2.167 | 39.935 | **598.78** |
| `http://a2a:8004/healthz` | 100 | 1.117 | **1.344** | 1.645 | 1.799 | 2.249 | 27.308 | **622.70** |
| `http://a2a:8004/readyz` | 100 | 1.172 | **1.462** | 1.812 | 2.039 | 2.205 | 27.643 | **629.70** |
| `http://mcp-gateway:8005/healthz` | 100 | 1.148 | 1.346 | 1.782 | 1.925 | 1.959 | 21.658 | 694.17 |
| `http://mcp-gateway:8005/readyz` | 100 | 1.167 | 1.431 | 1.793 | 1.913 | 2.118 | 27.117 | 628.20 |
| `http://webhook-receiver:8080/healthz` | 100 | 1.237 | **1.573** | 2.091 | 2.753 | 2.763 | 24.657 | **647.39** |
| `http://portal:8010/healthz` | 50 | 1.238 | 1.484 | 1.990 | 2.063 | 2.091 | 26.045 | 585.13 |
| `http://odoo:8011/healthz` | 100 | 1.207 | 1.450 | 1.891 | 2.155 | 2.239 | 30.530 | 553.16 |
| `http://salesforce:8012/healthz` | 100 | 1.228 | 1.660 | 2.160 | 2.438 | 3.273 | 29.372 | 587.88 |

---

## 3. Baked Air-Gapped ONNX Cognitive Service & Quad-Stack Pipeline

`nce-cognitive:3.5.4` pre-exports `sentence-transformers/all-mpnet-base-v2` into `model.onnx` (`FP32`, `ORT_ENABLE_EXTENDED`) and `model_qint8.onnx` (`QInt8`, `per_channel=True`, `reduce_range=True`, `MatMulConstBOnly=True`) during multi-stage build and serves strictly with `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` on internal-only `nce_data` (`Internal=true`, zero bind mounts).

| Operation | `n` | Min (ms) | p50 (ms) | p95 (ms) | p99 (ms) | Max (ms) | Mean (ms) | Notes |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| PostgreSQL RLS `SELECT count(*)` (`nce_app`) | 100 | 0.576 | 0.766 | 1.558 | 1.903 | 3.352 | 0.915 | `scoped_pg_session` |
| Redis `SET EX` + `PING` | 100 | 0.215 | 0.264 | 0.579 | 0.699 | 3.606 | 0.347 | `nce-redis` AUTH |
| MongoDB `ping` | 100 | 0.259 | 0.368 | 0.615 | 0.796 | 0.823 | 0.390 | Motor connection pool |
| MinIO `bucket_exists` | 100 | 0.864 | 1.006 | 1.326 | 1.481 | 1.601 | 1.039 | S3 HTTP API |
| ONNX `/v1/embeddings` (`c=1` sequential) | 100 | 27.234 | **31.931** | 48.307 | 60.181 | 61.444 | 33.717 | `768`-d, `l2_norm=1.0` |
| ONNX `/v1/embeddings` (`c=10` concurrent) | 100 | 53.266 | **177.800** | 204.549 | 214.851 | 227.313 | 177.537 | **`53.75 RPS`** (`1.861 s` wall) |
| `store_memory` (`768`-d + spaCy + Saga) | 20 | 183.992 | **309.148** | 514.665 | 735.227 | 790.367 | 358.716 | Quad-stack commit + KG |
| `semantic_search` (`768`-d RRF + decrypt) | 50 | 16.849 | 20.859 | 39.505 | 82.837 | 83.622 | 25.184 | Top RRF `0.0164` vs `0.0161` |
| `graph_search` (GraphRAG BFS `max_depth=2`) | 50 | 11.017 | 15.412 | 23.693 | 49.216 | 69.511 | 17.030 | PG HNSW + recursive CTE |
| `compile_working_memory` (`max_tokens=1500`) | 20 | 32.650 | 43.754 | 120.471 | 151.235 | 158.926 | 55.500 | Token-budgeted assembly |
| End-to-End 5-Tool Sequential MCP Turn | 10 | 175.471 | 228.725 | 270.981 | 272.299 | 272.629 | 223.973 | `store` -> `semantic` -> `graph` -> `recent` -> `verify` |

**Embedding Fidelity & Air-Gapped Parity:**
- Multi-stage Dockerfile build parity gate: `fp32_min_cos = 1.000000`, `q8_min_cos = 0.811436`.
- Live `nce-cognitive --verify-parity`: `min_cosine = 0.9999999403953552`, `max_norm_err = 5.96e-08`.
- Semantic cosine check: `0.664696` (related pair) vs `-0.022396` (unrelated pair), `l2_norm = 1.0`.

---

## 4. Security, Idle Telemetry & Container Verification Summary

1. **Network & Port Exposure:** Live `docker ps` across all 19 containers confirms `restarts = 0` on every container and host port bindings strictly limited to `nce-caddy` (`127.0.0.1:8082->80/tcp`, `127.0.0.1:8443->443/tcp`); `nce_data` has `Internal = true` and `nce-cognitive:3.5.4` runs with `mounts = []`.
2. **Internal mTLS Overlay (`docker-compose.mtls.yml` + `deploy/Caddyfile.mtls`):** `generate_internal_mtls.py --check-expiry-days 30` passed; booting `caddy` + `admin` under `NCE_ENV=prod` returned HTTP `200` on authenticated `GET /api/health/v1` while an uncertified TLS probe from `nce-a2a` to `https://admin:8003/readyz` failed closed (`RemoteDisconnected`).
3. **True 20s Idle Footprint (`docker stats --no-stream`):** `nce-mongo` **`0.44%`** CPU, `nce-webhook-receiver` **`0.49%`** CPU, `nce-cron` **`0.62%`** CPU / **`131.5 MiB`** (`VmRSS: 151916 kB`, `TORCH_MAP_HITS=0`), `nce-postgres` `3.65%`, `nce-redis` `0.50%`, `nce-cognitive` `0.21%`.
4. **Live & Container Verification Suites (`0` failed, `0` skipped):**
   - `verify_v1_launch.py` via Caddy edge: `RESULT: PASS` (`total_events = 931`).
   - `tests/test_event_retention.py`: **`4 passed, 0 failed, 0 skipped`** (`9.26 s`).
   - `tests/security/test_v33_zero_trust_suite.py` + `tests/test_worker_inflight_recovery.py` + `tests/test_subprocess_rlimit.py`: **`20 passed, 0 failed, 0 skipped`** (`12.99 s`).
   - `tests/test_integration_engine.py` + `tests/test_event_log_worm_privileges_pg.py` + `tests/test_vector_tenancy.py`: **`34 passed, 0 failed, 0 skipped`** (`17.52 s`).
   - `tests/test_key_durability.py`: **`5 passed, 0 failed, 0 skipped`** (`5.59 s`).
   - `nce-vertical-engines` `4.2.2` full 49-engine + NetBox wheel CI suite (clean PyYAML-free venv): **`582 passed, 0 failed, 0 skipped`**.
