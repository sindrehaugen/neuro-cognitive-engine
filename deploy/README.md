# NCE deployment guide

Operational defaults for NCE v1.0 assume **self-hosted Docker Compose** on one machine unless your team chooses otherwise.

---

## D1 — Default path: repository-root `docker-compose.yml`

**Quick start (zero-copy):**

```bash
# If deploy/compose.stack.env is missing, copy the template:
#   cp deploy/compose.stack.env.example deploy/compose.stack.env

# Generate strong secrets (required before any non-local deployment):
python scripts/bootstrap-compose-secrets.py

docker compose up -d --build

# Gate the deploy on the stack actually coming up. Exits non-zero on any
# container that is unhealthy, exited, or restarting. Compose itself acts on a
# healthcheck verdict in no way at all -- see "Health gating" below.
python -m nce.deploy_health
```

Stamp the image with the commit it was built from so a version-skew failure can
name it (optional, recommended for anything but a throwaway stack):

```bash
export NCE_GIT_SHA=$(git rev-parse --short HEAD)
export NCE_BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)
docker compose up -d --build
```

This loads **`deploy/compose.stack.env`** (dev placeholders only; copy from **`deploy/compose.stack.env.example`** on first clone) plus optional **`deploy/compose.stack.env.generated`** from the bootstrap script above. The stack handles automated PostgreSQL schema initialization (extensions + RLS), bundles required spaCy models, and starts:

| Service | Role |
|---------|------|
| **postgres** | pgvector/pg16 — memories, graph, quotas, A2A grants, event log |
| **mongodb** | Episodic payloads / code archive |
| **redis** | RQ queue + cache |
| **minio** | Media + replay payload cache (host **9002** / **9003**) |
| **cognitive** | Embeddings sidecar [D7] (**11435**) |
| **worker** | RQ consumer — async `index_code_file`, bridge jobs |
| **cron** | APScheduler — bridge renewal, **outbox relay**, **ReembeddingWorker** sweeps, consolidation |
| **admin** | **Starlette** Admin UI + REST (**8003**) — health `/api/health` |
| **a2a** | A2A JSON-RPC / agent card (**8004**) |
| **webhook-receiver** | FastAPI bridge webhooks (**8080**) |
| **caddy** | **:80** — `/webhooks/*` → receiver; **/** → admin |

**MCP (stdio)** is not inside Compose (IDE attaches to a local process). Run on the host with the same logical config as **`.env.example`** (127.0.0.1 URLs). Example:

```bash
set PG_DSN=postgresql://mcp_user:mcp_password@127.0.0.1:5432/memory_meta
set MONGO_URI=mongodb://127.0.0.1:27017
set REDIS_URL=redis://127.0.0.1:6379/0
set MINIO_ENDPOINT=127.0.0.1:9002
python server.py
```

Optional: create a project **`.env`** for Compose **interpolation** only (`POSTGRES_PASSWORD`, port overrides, `NCE_A2A_PUBLIC_URL`). Application env for containers comes from **`deploy/compose.stack.env`**.

---

## Configuration files

| File | Purpose |
|------|---------|
| **`deploy/compose.stack.env.example`** | Tracked template (placeholders only) — copy to `compose.stack.env` locally |
| **`deploy/compose.stack.env`** | Local container defaults (gitignored) — never deploy unchanged outside local dev |
| **`deploy/compose.stack.env.generated`** | **Required for production-like stacks** — run `python scripts/bootstrap-compose-secrets.py` before `docker compose up`. **Do not commit**; rotate any secret that was ever committed by mistake. |
| **`.env.example`** | Documented template for **host** MCP + production notes |
| **`deploy/multiuser/docker-compose.yml`** | Alternate layout; prefer root compose for v1.0 |
| **`Caddyfile`** (repo root) | Edge routing for v1.0 stack |

The multiuser compose file publishes MinIO on host **9000** (API) and **9001** (console) by default, while the root compose uses **9002** and **9003**—set **`MINIO_ENDPOINT`** (and optional `MINIO_API_PORT` / `MINIO_CONSOLE_PORT`) to match whichever stack you run.

---

## Secrets management (production) — VI.1

`scripts/bootstrap-compose-secrets.py` is the **development / single-host** path: it generates strong values into `deploy/compose.stack.env.generated`. **Do not commit that file, and do not use it as the production source of truth.**

In production, source secrets from a real **secret manager** — HashiCorp Vault, AWS Secrets Manager, or Azure Key Vault — and have your orchestrator inject them into each container's environment. Plaintext secrets must never live in a committed compose/`.env` file.

NCE exposes a thin **secrets-provider seam** (`nce/config.py`) so this is a drop-in:

| Piece | Purpose |
|-------|---------|
| `SecretsProvider` | Abstract seam — `get_secret(name) -> str | None`. |
| `EnvSecretsProvider` (default) | Reads from the process environment. The orchestrator feeds the env from your secret manager. |
| `resolve_secret(name, default=...)` | Resolves through the active provider, then falls back to env. |
| `set_secrets_provider(...)` | Installs a concrete manager-backed provider at startup. |
| `NCE_SECRETS_PROVIDER` | Selects the backend (`env` by default). |

**`NCE_MASTER_KEY` is environment / secret-manager only (R3).** `resolve_secret` refuses to route the master key through any non-environment provider, and `nce.config` reads it straight from the environment — it is never stored in or returned from a database / SettingsStore.

**Production guardrails** (enforced by `cfg.validate()` and at import):

- `NCE_LOAD_DOTENV` must be `false` — no `.env` is loaded at runtime.
- `NCE_ALLOW_ADMIN_DOTENV_PERSIST` must be `false` — the admin UI cannot persist connector/datastore secrets to a local `.env`. `cfg.validate()` raises if it is true under `NCE_ENV=prod`.

> Default-path stacks that still rely on `bootstrap-compose-secrets.py` are acceptable for dev and air-gapped single-host installs; for managed/cloud production, prefer the secret-manager injection above and leave `compose.stack.env.generated` unused.

---

## Zero-trust transport (mTLS) — production boot guard

The admin and a2a servers ship an mTLS client-certificate middleware
(`nce/mtls.py`, `MTLSAuthMiddleware`) that is **disabled by default** so dev /
single-host installs work out of the box.

In production (`NCE_ENV=prod`) leaving mTLS disabled is treated as a security
defect: each server's lifespan refuses to boot when its mTLS middleware is off,
**unless** an operator explicitly accepts the weakened posture by setting
`NCE_MTLS_ACKNOWLEDGE_DISABLED=true`. The acknowledged path does not fail —
it logs `CRITICAL` and records an immutable WORM audit event (no secrets/PII)
before continuing.

| Variable | Meaning |
|----------|---------|
| `NCE_ADMIN_MTLS_ENABLED` / `NCE_A2A_MTLS_ENABLED` | Enable the per-surface mTLS middleware (with trust anchors). |
| `NCE_ADMIN_MTLS_ALLOWED_SANS` / `NCE_A2A_MTLS_ALLOWED_SANS` | Allowed client-cert SANs (comma-separated). |
| `NCE_{ADMIN,A2A}_MTLS_TRUSTED_PROXY_HOP` | Trusted reverse-proxy hops (set `1` when Caddy terminates mTLS). |
| `NCE_MTLS_CERT_PATH` / `NCE_MTLS_KEY_PATH` / `NCE_MTLS_CA_PATH` | Cert material — **environment-only**, never via DB/settings/endpoint. |
| `NCE_MTLS_ACKNOWLEDGE_DISABLED` | `false` (default) → prod refuses to boot with mTLS off; `true` → boot with CRITICAL log + WORM event. |

**Edge termination:** the root `docker-compose.yml` carries a COMMENTED
mTLS-termination example on the `caddy` service (verify client certs at the edge,
forward the verified leaf to admin/a2a via `X-Forwarded-Client-Cert`) plus
commented read-only cert mounts on the `admin` / `a2a` services. mTLS stays OFF
by default in dev — uncomment those blocks and provide certs under
`deploy/certs/` to enable it.

---

## D2 / D7 — Cognitive model

- Image **`ghcr.io/sindrehaugen/nce-cognitive:v1`** on **11435**.
- Stack sets **`NCE_COGNITIVE_BASE_URL=http://cognitive:11435`** for in-network services.

---

## Operations

- **Backups**: volumes `pg_data`, `mongo_data`, `redis_data`, `minio_data`, `caddy_*` + rotate secrets in **`deploy/compose.stack.env`**.
- **Consolidation**: `nce/cron.py` runs `ConsolidationWorker` on an interval for namespaces whose metadata sets `consolidation.enabled=true`. Use the MCP `trigger_consolidation` tool for ad-hoc runs.
- **Outbox relay**: `nce/cron.py` polls `outbox_events` on `OUTBOX_RELAY_INTERVAL_SECONDS` (default 5s) and delivers to the RQ worker queue. The MCP stdio process (`server.py` / `nce/mcp_stdio_main.py`) runs the same relay loop for single-process dev setups.

### Webhook receiver hardening

The **webhook-receiver** service depends on **Redis** for sliding-window rate limits and idempotent deduplication keys.

| Variable | Production guidance |
|----------|---------------------|
| `WEBHOOK_DEDUP_FAIL_OPEN` | Keep **`false`** (default). When Redis is down, dedup must **not** enqueue duplicate bridge jobs. |
| `NCE_WEBHOOK_TRUST_PROXY` | Set **`true`** only when **Caddy** (or another trusted reverse proxy) terminates TLS and sets `X-Forwarded-For`. Leave **`false`** if clients connect directly to the receiver. |

Bridge webhook secrets (`DROPBOX_APP_SECRET`, `GRAPH_CLIENT_STATE`, `DRIVE_CHANNEL_TOKEN`) are required at process start — generate them via `scripts/bootstrap-compose-secrets.py` and store them in **`deploy/compose.stack.env.generated`** (never commit).

### Infrastructure & Health Monitoring

The stack includes built-in HTTP-based process and connection checks suitable for reverse-proxies, load-balancers, or orchestrator probes:

- **Admin Web Server (`admin`):** Exposes `GET /healthz` on port `8003`. Returns 200 OK after checking all backend services (PostgreSQL, MongoDB, Redis, MinIO).
- **Webhook Receiver (`webhook-receiver`):** Exposes `GET /health` on port `8080` (or `8002` internally).
- **Caddy Edge (`caddy`):** Exposes `GET /health` via proxy-pass where appropriate.

### Health gating — act on `unhealthy`

Plain Compose **ignores** a healthcheck verdict: nothing alerts, nothing
restarts, nothing exits non-zero. On 2026-08-27 `nce-admin` and `nce-a2a`
crash-looped inside their own uvicorn supervisors for hours at ~137% CPU each
while `docker ps` reported both `Up`, because the supervisor process never
exited and so `restart: unless-stopped` never engaged.

Two halves close that gap.

**1. Fail-fast in the container.** The image entrypoint runs
`python -m nce.preflight` before `exec`ing the real command. If startup cannot
succeed — stale image against a migrated database, unreachable datastore — the
container *exits* instead of respawning workers forever, so the failure shows up
as a restart count and Docker's backoff applies. `NCE_PREFLIGHT=0` skips it;
`NCE_PREFLIGHT_TIMEOUT` (default 120 s) bounds it.

**2. Gate and watch from outside.**

```bash
# After `compose up`: poll until every container is healthy, or fail the deploy.
python -m nce.deploy_health --timeout 300

# One-shot verdict for a watchdog / scheduled alert. Non-zero while any
# container is unhealthy, exited, or restarting; a container still starting is
# not an alert.
python -m nce.deploy_health --once
```

Exit codes: `0` healthy, `1` something is unhealthy, `2` timed out, `3` no
containers found. Add `-f <compose-file>` / `-p <project>` for a non-default
stack. Run the `--once` form on a timer (cron, systemd timer, Task Scheduler)
and alert on non-zero — that is the minimum viable version of this.

### Running GPU-Accelerated Workloads

For re-embedding, alignment, and dense search operations, NCE supports GPU/CUDA hardware acceleration:

- **NVIDIA GPU Support:** Separate resource requirements are encapsulated under the `gpu` profile.
- **Run on GPU:** Include the `--profile gpu` flag:
  ```bash
  docker compose --profile gpu up -d
  ```
- **Local Dev CPU-Only Fallback:** If you do not have an NVIDIA GPU or local CUDA drivers, copy `docker-compose.override.example.yml` to `docker-compose.override.yml` in the root. This strips the GPU profile constraint and runs all services on CPU by default with `docker compose up -d`.

### Upgrades and Schema Migrations

When rolling out schema upgrades or applying major system changes:

1. **Volume Wipe (If Clean Setup is Required):** In staging or development environments, if you wish to wipe the database and start from a fresh slate, stop the stack and run:
   ```bash
   docker compose down -v
   ```
2. **Schema & Migration Playbooks:** The PostgreSQL schema is loaded dynamically from `nce/schema.sql` during the first database bootstrap or orchestrator connect. To manually apply migrations to a running instance, execute:
   ```bash
   python scripts/apply_integration_schema.py
   ```

---

## Native installers (`nce-launch` shim)

The **mode-aware MCP stdio shim** is built from `go/cmd/nce-launch/` (Enterprise Deployment Plan section 6.4). Release automation is **`.github/workflows/release.yml`** (runs on annotated tags `v*`).

### Build outputs (CI expectations)

| Platform | Artifact | Shim path inside package |
|----------|----------|---------------------------|
| **Windows (Inno)** | `build/windows/Output/NCE-Setup.exe` | `{app}\nce-launch.exe` — Start Menu shortcut and post-install run target (`NCE-Setup.iss`) |
| **Windows (WiX)** | `build/windows/NCE.msi` | `%ProgramFiles%\NCE\nce-launch.exe` (`NCE.wxs`). MSI ships shim + `Patch-IDEConfig.ps1` / `Write-UserConfig.ps1`; use **Inno** for embedded Python + full app tree (`nce`, `admin`, compose files, wheels). |
| **macOS** | `build/macos/NCE-universal.dmg` | `NCE.app/Contents/MacOS/NCE` — binary is the universal `build/macos/nce-launch` copied into bundle (`build/macos/build-dmg.sh`) |

### Preconditions

- **Windows:** `dotnet tool install --global wix` (CI step) consumes `TrimcpLaunchExe Source="nce-launch.exe"` relative to `build/windows/`. Inno compiles `NCE-Setup.iss` with working directory **`build/windows/`** so relative `..\..\` repo paths resolve to the project root.

- **macOS:** Produce `build/macos/nce-launch` (universal binary via `lipo`) before `./build/macos/build-dmg.sh`; script copies it as the bundle executable named `NCE` per `Info.plist`.

### Verification checklist (release engineering)

1. Tag push triggers **`NCE Enterprise Release`** workflow; confirm `nce-launch.exe` build step exits 0.
2. **Inno artifact:** Inspect `{app}` — `nce-launch.exe` present; `%APPDATA%\NCE\mode.txt` / `.env` written by Pascal `CurStepChanged` (`ssPostInstall`).
3. **MSI artifact:** `msiexec /i NCE.msi MODE=local` (or silent equivalent) — `nce-launch.exe` and `scripts\*.ps1` under install root.
4. **DMG:** `codesign --verify --deep` on `.app` when signing identities are set (`APPLE_*` secrets).
5. **Optional:** Smoke-run shim from installed location with expected `NCE_*` env (see `.env.example`).

---

## Architecture

**docs/architecture-v1.md** — runtime topology, temporal queries, A2A, workers.

**docs/multi_tenancy.md** and **docs/signing.md** — namespaces, signing.
