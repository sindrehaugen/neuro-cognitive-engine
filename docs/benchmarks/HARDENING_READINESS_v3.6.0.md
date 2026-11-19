# NCE Core `v3.6.0` Hardening & Readiness Report

**Release:** `neuro-cognitive-engine` `v3.6.0`  
**Certification Date:** `2026-11-19`  
**Scope:** Core Connector SDK (`nce/connectors/`), Beta/Stable Maturity Contract, Encrypted `app_connections` Vault (`041`), Admin Beta UI, Pure-ASGI Middleware, Air-Gapped ONNX Cognitive Service (`num_threads=4`, batching), `nce-cron` `ZERO_TORCH` + Seccomp Parity, and 19-Container Integrated Stack Certification.  
**Prior Baseline Reports:** [PERF_REPORT_v3.5.2.md](PERF_REPORT_v3.5.2.md) · [HARDENING_READINESS_v3.5.3.md](HARDENING_READINESS_v3.5.3.md) · [HARDENING_READINESS_v3.5.4.md](HARDENING_READINESS_v3.5.4.md).

---

## 1. Telemetry Delta (`v3.5.3` Baseline -> `v3.6.0` Live Certified Stack)

All `v3.6.0` timings were captured across the live 19-container Docker stack (`19/19` healthy, `0` restarts) using `scripts/benchmark_live_stack.py` (`internal` + `edge`) and `docker stats` over a `20s` true-idle window.

| Metric | `v3.5.3` Baseline | `v3.6.0` Live Measured | Delta / Architecture Cause |
| :--- | :--- | :--- | :--- |
| Internal `/healthz` (`admin` / `a2a` / `webhook-receiver`) | `44.03–44.08 ms` | **`1.093 ms`** / **`1.456 ms`** / **`1.828 ms`** (`481.54–714.73 RPS`) | **~24x–40x faster**: pure-ASGI middleware + replay-safe receive replaces `BaseHTTPMiddleware` split writes |
| ONNX `/v1/embeddings` (`c=1` seq p50) | `44.506 ms` | **`32.173 ms`** (`p95 47.631 ms`) | `-27.7%`: baked air-gapped ONNX Runtime (`num_threads=4`, `HF_HUB_OFFLINE=1`) |
| ONNX `/v1/embeddings` (`c=10` conc p50 / RPS) | `548.228 ms` (`18.03 RPS`) | **`202.031 ms`** (`44.79 RPS`) | **`2.48x` throughput**: `5ms` dynamic micro-batching queue (`max_batch_size=32`) |
| `store_memory` (`768-d`, `n=20` p50) | `369.438 ms` | **`342.234 ms`** | `-7.4%` via lower ONNX + ASGI tail latency across Tri-Stack write path |
| `nce-cron` Memory & Seccomp | `442.30 MiB` (`86.39%`), no seccomp | **`124.5 MiB`** (`VmRSS 136.9 MiB`), **`0.03%` CPU**, **`Seccomp: 2`** | **`-71.9%` RSS**: `import torch` short-circuited (`torch_hits=0`) + `worker.json` seccomp |
| True-Idle CPU (`nce-mongo` / `webhook-receiver`) | `44.86%` / `9.58%` | **`0.54%`** / **`0.40%`** | **`-98.8%` / `-95.8%`**: calm async loops + `30s` `mongosh --quiet --norc` ping |
| ONNX vs PyTorch Embedding Parity | Verified in `v3.5.4` | **`min_cosine = 0.9999999403953552`** | `max_norm_err = 5.96e-8` (`768-d`, `l2_norm=1.0`, semantic cos `0.664696` vs `-0.022396`) |

---

## 2. Live 19-Container Latency & Store Scorecard (`v3.6.0`)

### 2.1 Internal Microservice & Vertical Endpoints (`nce_data`, `c=10`)

| Probe Target | `n` | Seq Min (ms) | Seq p50 (ms) | Seq p95 (ms) | Conc `c=10` p95 (ms) | Conc `RPS` |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| `nce-admin` `GET /healthz` | 100 | 0.902 | 1.093 | 1.634 | 24.052 | 714.73 |
| `nce-admin` `GET /readyz` | 100 | 1.137 | 1.405 | 2.041 | 44.536 | 562.02 |
| `nce-a2a` `GET /healthz` | 100 | 1.219 | 1.456 | 1.947 | 34.151 | 554.71 |
| `nce-a2a` `GET /readyz` | 100 | 1.296 | 1.530 | 2.177 | 34.879 | 505.62 |
| `nce-mcp-gateway` `GET /healthz` | 100 | 1.366 | 1.652 | 2.590 | 33.794 | 509.02 |
| `nce-mcp-gateway` `GET /readyz` | 100 | 1.368 | 1.642 | 2.183 | 33.112 | 506.19 |
| `nce-webhook-receiver` `GET /healthz` | 100 | 1.489 | 1.828 | 2.673 | 32.531 | 481.54 |
| `portal` `GET /healthz` | 50 | 1.567 | 1.859 | 2.550 | 36.783 | 468.57 |
| `odoo` `GET /healthz` | 100 | 1.448 | 1.792 | 2.439 | 38.575 | 484.38 |
| `salesforce` `GET /healthz` | 100 | 1.431 | 1.726 | 2.459 | 37.062 | 486.85 |
| `nce-cognitive` `POST /v1/embeddings` | 100 | 26.863 | 32.173 | 47.631 | 347.895 | 44.79 |

### 2.2 Caddy Loopback Edge Ingress (`http://127.0.0.1:8082`)

- **Public Probes (`p50` seq / `c=10` RPS):** `/caddy-healthz` `3.688 ms` (`187.23 RPS`), `/healthz` `5.699 ms` (`176.24 RPS`), `/readyz` `5.191 ms` (`215.27 RPS`), `/.well-known/agent-card` `5.366 ms` (`239.46 RPS`), `/portal/healthz` `4.895 ms` (`213.48 RPS`), `/odoo/healthz` `4.366 ms` (`233.65 RPS`), `/salesforce/healthz` `4.168 ms` (`252.21 RPS`).
- **HMAC-Authenticated `GET /api/health/v1` (`n=30`):** seq `p50 58.841 ms` (`p95 66.752 ms`, `21.30 RPS`).
- **Same-Second Nonce Burst & Replay Fencing:** `10/10` parallel requests HTTP `200` (`436.103 ms` wall); duplicate replay -> HTTP `401`; mutated nonce -> HTTP `401`; unauthenticated admin API -> `401`; oversized HTTP body -> `413`.

### 2.3 Quad-Stack Stores & Cognitive MCP Pipeline (`nce_data`)

- **Quad-Stack Stores (`n=100` `p50`):** PostgreSQL RLS `0.820 ms` (`p95 1.692 ms`), Redis `SET+PING` `0.320 ms` (`p95 0.525 ms`), MongoDB `ping` `0.402 ms` (`p95 0.753 ms`), MinIO `bucket_exists` `1.052 ms` (`p95 1.829 ms`).
- **Cognitive Pipeline (`p50`):** `store_memory` (`768-d`, `n=20`) `342.234 ms`, `semantic_search` (`n=50`) `23.257 ms`, `graph_search` (`n=50`) `16.246 ms`, `compile_working_memory` (`n=20`) `38.898 ms`, 5-tool MCP turn (`n=10`) `177.095 ms`.

---

## 3. Core Connector SDK, Maturity Contract & Gate Verification

- **50-Engine Ecosystem Split & Maturity Census:** Verified across the live admin catalog (`/admin/plugins` & `?maturity=beta`/`stable`) with `50` total connectors (`25` Stable, `25` Beta) spanning Core's Connector SDK (`nce/connectors/` + `041_app_connections_vault.sql`), `29` business vertical engines (`25` Stable + `4` Beta), `20` standalone Beta application connectors (`nce-app-engines`), and `1` external Beta plugin. Air-gapped UI renders inline CSS Beta/Stable pills (`ext_urls=0` external assets).
- **Gate A (Core-Only Standalone Venv & Migration Isolation):** Fresh venv with `neuro-cognitive-engine` `3.6.0` + `nce-app-engines[all]` `1.0.0` (verified `import nce_verticals` raises `ModuleNotFoundError`) applied `62` migrations (`41` Core + `21` `nce_apps`, `58` app forced-RLS tables, `0` unforced tables) against an isolated PostgreSQL probe database; `verify_rls_catalog_consistency()` passed under both `mcp_user` and `nce_app`.
- **Gate B Security, Air-Gap & Launch Checks:**
  - `nce_data` Docker network `Internal=true`; `nce-cognitive:3.6.0` `Mounts=[]`, `engine=onnxruntime`, `num_threads=4`, `offline=true`.
  - Live `/admin/plugins/api/connections` vault round-trip (`POST` -> `GET` -> `DELETE`) verified on encrypted `app_connections` table (`261` total PG relations verified, `0` unforced RLS tables).
  - `verify_v1_launch.py` inside `nce-admin`: `RESULT: PASS` (`total_events=961`, `replay_failed_runs=0`).
  - `scripts/generate_internal_mtls.py --check-expiry-days 30` and `docker compose -f docker-compose.yml -f docker-compose.mtls.yml config -q` exited `0`.
