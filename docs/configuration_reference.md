> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# NCE Configuration Reference

Authoritative reference for every environment variable, server entry-point, and runtime flag.
All environment reads happen in `nce/config.py` (the `_Config` class instantiated as `cfg`).
No other module calls `os.getenv()` directly.

---

## 1. Database & Storage Connections

| Variable | Type | Default | Required / Priority | Description |
|---|---|---|---|---|
| `MONGO_URI` | `str` | `mongodb://localhost:27017` | **P0 Required (prod)** | MongoDB connection URI. Used by Motor for episodic raw payload storage. Development default is rejected at startup in production (`NCE_ENV=prod`). |
| `PG_DSN` | `str` | `postgresql://mcp_user:mcp_password@localhost:5432/memory_meta` | **P0 Required (prod)** | Primary PostgreSQL DSN. Sourced via `secret_env` (`PG_DSN_FILE` supported). Also accepts `DATABASE_URL` as a 12-factor alias (`PG_DSN` wins when both are set). Development default is rejected at startup in production. |
| `DATABASE_URL` | `str` | — | No | 12-factor alias for `PG_DSN` (`DATABASE_URL_FILE` supported). Ignored when `PG_DSN` is set. |
| `DB_READ_URL` | `str` | falls back to `PG_DSN` | No | Optional read-replica DSN (`DB_READ_URL_FILE` supported). When configured and distinct from `PG_DSN`, a second `asyncpg` connection pool is initialized for read-only query paths. |
| `DB_WRITE_URL` | `str` | falls back to `PG_DSN` | No | Reserved write-DSN override (`DB_WRITE_URL_FILE` supported). Defaults to `PG_DSN`. |
| `PG_BOUNCER_URL` | `str` | `""` | No | PgBouncer connection URL when running behind a connection pooler. |
| `NCE_APP_PASSWORD` | `str` | `nce_app_secret` | No (dev) / Required (prod) | Password for the `nce_app` PostgreSQL role (`NCE_APP_PASSWORD_FILE` supported). Used when establishing RLS sessions; must be overridden in production. |
| `NCE_GC_DSN` | `str` | falls back to `PG_DSN` | No | Least-privilege worker DSN for background maintenance (`NCE_GC_DSN_FILE` supported). When set, background workers (GC, re-embedding) connect as a distinct principal (e.g. `nce_gc`) rather than reusing `nce_app`. |
| `REDIS_URL` | `str` | `redis://localhost:6379/0` | **P0 Required (prod)** | Redis connection URL (`REDIS_URL_FILE` supported). Used by async (`redis.asyncio`) and sync (RQ) clients. Carries inline AUTH password if configured (`redis://:password@host:6379/0`). Dev default rejected in production. |
| `MINIO_ENDPOINT` | `str` | `localhost:9000` | Yes (media) | MinIO host and port string (`host:port`). Used by MinIO client for object storage. |
| `MINIO_ACCESS_KEY` | `str` | `""` | **P0 Required** | MinIO access key. Must be set via environment — no default permitted (FIX-013). |
| `MINIO_SECRET_KEY` | `str` | `""` | **P0 Required** | MinIO secret key. Must be set via environment — startup raises `ValueError` if empty when `NCE_MINIO_REQUIRED=true`. |
| `MINIO_SECURE` | `bool` | `false` | No | Set `true` to use TLS (`https://`) for MinIO connections. |
| `NCE_MINIO_REQUIRED` | `bool` | `true` | No | Set `false` to bypass MinIO credential checks at startup (used in test suites or non-media deployments). |

---

## 2. PostgreSQL Connection Pool & Partitioning

| Variable | Type | Default | Description |
|---|---|---|---|
| `PG_MIN_POOL` | `int` | `1` | Minimum `asyncpg` connection pool size (`min_size`). |
| `PG_MAX_POOL` | `int` | `10` | Maximum `asyncpg` connection pool size (`max_size`). |
| `NCE_PARTITION_LOOKAHEAD_MONTHS` | `int` | `3` | Number of future months for which partition maintenance pre-creates table partitions (minimum `1`). |

*Runtime Timeouts:*
- `command_timeout` is hardcoded to `30 s` in `NCEEngine.connect()` (`nce/orchestrator.py`).
- Pool acquire timeout is `10.0 s` (`POOL_ACQUIRE_TIMEOUT` constant in `nce/db_utils.py`).

---

## 3. Redis Tuning & Caching

| Variable | Type | Default | Description |
|---|---|---|---|
| `REDIS_TTL` | `int` | `3600` | Default cache TTL in seconds for general Redis entries. |
| `REDIS_MAX_CONNECTIONS` | `int` | `20` | Maximum connections allowed in async and sync Redis connection pools. |
| `NCE_ECHO_TTL_S` | `int` | `600` | TTL in seconds for the Redis echo suppression key set (`nce:echo:{system}:{entity_id}`) to prevent self-caused webhooks from re-triggering semantic ingestion (minimum `1`). |

---

## 4. Authentication & Security

| Variable | Type | Default | P-level | Description |
|---|---|---|---|---|
| `NCE_MASTER_KEY` | `str` | `""` | **P0 Required** | AES-256 master key for encrypting signing keys and DEKs at rest (`NCE_MASTER_KEY_FILE` supported). Server refuses to start if absent or shorter than 32 UTF-8 bytes. Sourced only from environment / secret manager — never from a database (R3). |
| `NCE_ENV` | `str` | `dev` | **P0** | Runtime environment identifier (`dev`, `prod`, `production`, `test`, `ci`). Controls `IS_PROD`, `IS_TEST`, and fail-fast validation gates. |
| `NCE_API_KEY` | `str` | `""` | **P1 (prod)** | HMAC-SHA256 secret key for HTTP admin API routes (`HMACAuthMiddleware`, `NCE_API_KEY_FILE` supported). Required in production (raises `RuntimeError` if missing). |
| `NCE_ADMIN_API_KEY` | `str` | `""` | **P1 (prod)** | Admin bearer token checked by `require_scope("admin")` in A2A/MCP (`NCE_ADMIN_API_KEY_FILE` supported). Required in production. |
| `NCE_ADMIN_USERNAME` | `str` | `""` | **P1 (prod)** | HTTP Basic Auth username for web admin UI routes (`BasicAuthMiddleware`). Required in production. |
| `NCE_ADMIN_PASSWORD` | `str` | `""` | **P1 (prod)** | HTTP Basic Auth password for web admin UI routes. In production, must be a `$pbkdf2$` hash — plaintext passwords trigger an immediate startup error. |
| `NCE_MCP_API_KEY` | `str` | `""` | **P1 (prod)** | Configuration/defence-in-depth value for MCP stdio tenant tools, **not** a secret validated against a caller: the stdio client spawns the server and supplies its own env, so it already holds this value — the OS process boundary is what authenticates, not this key. Tenant isolation is enforced separately by `NCE_MCP_NAMESPACE_ID` (below). (`NCE_MCP_API_KEY_FILE` supported). Required in production. |
| `NCE_MCP_NAMESPACE_ID` | `str` | `""` | **P1 (prod)** | Tenant namespace UUID bound to MCP stdio tools. Required in production when `NCE_MCP_API_KEY` is set; must be a valid UUID. |
| `NCE_ADMIN_OVERRIDE` | `bool` | `false` | **Dev only** | Bypass admin scope checks during development. **Raises `RuntimeError` at module import when `NCE_ENV=prod`.** |
| `NCE_BYPASS_WORM` | `bool` | `false` | **Dev only** | Bypass WORM / immutability probe during startup. Forbidden in production (raises `RuntimeError`). |
| `NCE_BYPASS_RLS` | `bool` | `false` | **Dev only** | Bypass Row Level Security probe during startup. Forbidden in production (raises `RuntimeError`). |
| `NCE_ALLOW_ADMIN_DOTENV_PERSIST` | `bool` | `true` (dev) / `false` (prod) | **Dev only** | Allow admin UI to persist datastore/connector configuration edits to a local `.env` file. Forbidden in production. |
| `NCE_SECRETS_PROVIDER` | `str` | `env` | No | Secrets provider backend identifier (`env`). Production deployments can register external secret managers (Vault, AWS, Azure KV) via `set_secrets_provider()`. `NCE_MASTER_KEY` remains env-only. |
| `NCE_LOAD_DOTENV` | `bool` | `true` | No | Set `false` to prevent loading `.env` at module import. **Must be `false` in production** — raises `RuntimeError` at startup if truthy when `NCE_ENV=prod`. |
| `NCE_CLOCK_SKEW_TOLERANCE_S` | `int` | `90` | No | Maximum allowed timestamp drift in seconds for HMAC replay protection (tightened from 300 s in Batch 116). |
| `NCE_HMAC_NONCE_REQUIRED` | `bool` | `true` | No | Enforce per-request nonces on all HMAC-protected requests when Redis NonceStore is reachable. Fails closed in production if Redis is down. |
| `NCE_DISTRIBUTED_REPLAY` | `bool` | `false` | No | Enable Redis-backed distributed NonceStore for HMAC replay protection across multi-replica admin deployments. |
| `NCE_PBKDF2_ITERATIONS` | `int` | `100000` | No | PBKDF2 iteration count for legacy v2 blob decryption compatibility (NIST minimum `100000`). |
| `NCE_PBKDF2_ITERATIONS_V4` | `int` | `600000` | No | PBKDF2 iteration count for v4 new writes and admin password hashing (OWASP 2026 minimum `600000`). |
| `NCE_ALLOW_MIGRATION_MCP_IN_PROD` | `bool` | `false` | **Prod gate** | Emergency operational override to temporarily enable migration MCP tools in production. |
| `NCE_TRUST_QUARANTINE_BYPASS` | `float` | `0.8` | No | Trust score threshold (0.0–1.0) at or above which mid-confidence assertions bypass quarantine. |
| `NCE_TRUST_DEFAULT` | `float` | `0.65` | No | Fallback trust score assigned to newly observed actors (Laplace prior, range 1e-6 to 1.0). |
| `NCE_TOOL_GOVERNANCE_STALE_OK_SEC` | `int` | `30` | No | Maximum age (seconds) for serving local tool governance snapshots without querying Redis (minimum `1`). |
| `NCE_TOOL_GOVERNANCE_STALE_HARD_SEC` | `int` | `300` | No | Hard expiry (seconds) after which governance snapshot fails closed (`GovernanceUnavailable`) rather than serving stale un-revocation data (minimum `1`). |

---

### 4a. File-Based Secrets (`*_FILE`) — Docker & Kubernetes Secrets

Every secret resolved through `nce.config.secret_env` supports a companion `<NAME>_FILE` environment variable. When `<NAME>_FILE` is set, the value is read directly from the specified file path (with a single trailing newline stripped), preventing secret values from leaking into `/proc/<pid>/environ`.

| Plain Variable | File Variable | Purpose |
|---|---|---|
| `NCE_MASTER_KEY` | `NCE_MASTER_KEY_FILE` | Master AES-256 encryption key. |
| `NCE_API_KEY` | `NCE_API_KEY_FILE` | Admin HMAC API key. |
| `NCE_ADMIN_API_KEY` | `NCE_ADMIN_API_KEY_FILE` | Admin bearer token. |
| `NCE_MCP_API_KEY` | `NCE_MCP_API_KEY_FILE` | MCP stdio tenant secret. |
| `NCE_APP_PASSWORD` | `NCE_APP_PASSWORD_FILE` | PostgreSQL application role password. |
| `PG_DSN` / `DATABASE_URL` | `PG_DSN_FILE` / `DATABASE_URL_FILE` | PostgreSQL connection DSN. |
| `DB_READ_URL` / `DB_WRITE_URL` | `DB_READ_URL_FILE` / `DB_WRITE_URL_FILE` | Split database connection DSNs. |
| `NCE_GC_DSN` | `NCE_GC_DSN_FILE` | Least-privilege GC maintenance DSN. |
| `REDIS_URL` | `REDIS_URL_FILE` | Redis connection URL with AUTH credentials. |

*Precedence & Invariants:* `*_FILE` always takes precedence over the plain variable. If the file is missing or unreadable, startup fails closed with a `RuntimeError` naming the variable and path without echoing content.

---

## 5. JWT / Bearer Authentication

| Variable | Type | Default | Description |
|---|---|---|---|
| `NCE_JWT_SECRET` | `str` | `""` | HS256 shared secret for JWT verification in development/testing. |
| `NCE_JWT_PUBLIC_KEY` | `str` | `""` | RS256/ES256 PEM-encoded public key (or `file:///path/to/pub.pem`). Takes precedence over `NCE_JWT_SECRET`. |
| `NCE_JWT_ALGORITHM` | `str` | `HS256` | JWT signature algorithm (`HS256`, `RS256`, `ES256`). Production deployments log a warning if HS256 is used. |
| `NCE_JWT_ISSUER` | `str` | `""` | Expected token `iss` claim. Empty string skips issuer validation. |
| `NCE_JWT_AUDIENCE` | `str` | `""` | Expected token `aud` claim. Empty string skips audience validation. |
| `NCE_JWT_PREFIX` | `str` | `/api/v1/` | URL route prefix guarded by `JWTAuthMiddleware`. |
| `NCE_JWT_KEY_DIR` | `str` | `Path.cwd()` | Base filesystem directory used to resolve relative `file://` paths in `NCE_JWT_PUBLIC_KEY`. |
| `NCE_JWT_LEEWAY_SECONDS` | `int` | `30` | Clock skew tolerance in seconds for JWT `exp` and `nbf` claims. |
| `NCE_A2A_JWT_AUDIENCE` | `str` | `nce_a2a` (dev) / `""` (prod) | Audience override for the A2A server. **Required in production** (`NCE_ENV=prod`) to prevent cross-service replay attacks. |

---

## 6. mTLS — A2A Server

| Variable | Type | Default | Description |
|---|---|---|---|
| `NCE_A2A_MTLS_ENABLED` | `bool` | `false` | Master switch for client certificate enforcement on A2A routes. **Mandatory in production** unless `NCE_MTLS_ACKNOWLEDGE_DISABLED=true`. |
| `NCE_A2A_MTLS_STRICT` | `bool` | `true` | When `true`, reject connections lacking a valid client certificate. |
| `NCE_A2A_MTLS_TRUSTED_PROXY_HOP` | `int` | `1` | Number of trusted reverse-proxy hops for parsing `X-Forwarded-Client-Cert` (`0` = direct TLS, `1` = one reverse proxy). |
| `NCE_A2A_MTLS_ALLOWED_SANS` | `list[str]` | `""` (empty) | Comma-separated allowlist of Subject Alternative Names (DNS or URI match). |
| `NCE_A2A_MTLS_ALLOWED_FINGERPRINTS` | `list[str]` | `""` (empty) | Comma-separated allowlist of SHA-256 certificate fingerprints (hex format). |
| `NCE_A2A_HTTP_RATE_LIMIT` | `int` | `60` | Maximum `tasks/send` requests allowed per IP within the rate period (minimum `1`). |
| `NCE_A2A_HTTP_RATE_PERIOD` | `int` | `60` | Rate-limit window in seconds for A2A HTTP endpoints (minimum `1`). |
| `NCE_A2A_URL` | `str` | `http://localhost:8004` | Public base URL of the A2A server used in agent card discovery. |

---

## 7. mTLS — Admin Server

| Variable | Type | Default | Description |
|---|---|---|---|
| `NCE_ADMIN_MTLS_ENABLED` | `bool` | `false` | Master switch for client certificate enforcement on `/api/` admin routes. **Mandatory in production** unless `NCE_MTLS_ACKNOWLEDGE_DISABLED=true`. |
| `NCE_ADMIN_MTLS_STRICT` | `bool` | `true` | Reject admin API requests lacking a client certificate. |
| `NCE_ADMIN_MTLS_TRUSTED_PROXY_HOP` | `int` | `1` | Number of trusted reverse-proxy hops for parsing `X-Forwarded-Client-Cert`. |
| `NCE_ADMIN_MTLS_ALLOWED_SANS` | `list[str]` | `""` (empty) | Comma-separated allowed Subject Alternative Names for admin mTLS. |
| `NCE_ADMIN_MTLS_ALLOWED_FINGERPRINTS` | `list[str]` | `""` (empty) | Comma-separated allowed SHA-256 certificate fingerprints for admin mTLS. |
| `NCE_ADMIN_HTTP_RATE_LIMIT` | `int` | `120` | Maximum general admin requests allowed per IP within the rate period (minimum `1`). |
| `NCE_ADMIN_HTTP_RATE_PERIOD` | `int` | `60` | Rate-limit window in seconds for general admin endpoints (minimum `1`). |
| `NCE_ADMIN_HTTP_SENSITIVE_RATE_LIMIT` | `int` | `30` | Maximum sensitive POST requests allowed per IP within the sensitive rate period (minimum `1`). |
| `NCE_ADMIN_HTTP_SENSITIVE_RATE_PERIOD` | `int` | `60` | Rate-limit window in seconds for sensitive admin POST endpoints (minimum `1`). |

---

## 8. mTLS — General & Zero-Trust Transport Guards

| Variable | Type | Default | Description |
|---|---|---|---|
| `NCE_MTLS_STRICT` | `bool` | `true` | Global mTLS strict mode flag. Missing certificate is a hard rejection when `true`. |
| `NCE_MTLS_CERT_PATH` | `str` | `""` | Filesystem path to the server TLS certificate (PEM). |
| `NCE_MTLS_KEY_PATH` | `str` | `""` | Filesystem path to the server TLS private key (PEM). |
| `NCE_MTLS_CA_PATH` | `str` | `""` | Filesystem path to the CA bundle used for client certificate verification (PEM). |
| `NCE_MTLS_ACKNOWLEDGE_DISABLED` | `bool` | `false` | **Zero-trust boot guard.** In production (`NCE_ENV=prod`), `admin_server` and `a2a_server` refuse to start without mTLS enabled unless this flag is `true` (logs `CRITICAL` and records an immutable `mtls_disabled_acknowledged` audit event). |

---

## 9. LLM Provider API Keys

All provider keys default to `""`. The provider factory logs a warning if a requested provider's key is missing. Per-namespace overrides can reference environment variables using `ref:env/<VAR>` in namespace metadata.

| Variable | Type | Default | Provider / Description |
|---|---|---|---|
| `NCE_ANTHROPIC_API_KEY` | `str` | `""` | Anthropic Claude API key (`claude-opus-4-6`, etc.). |
| `NCE_OPENAI_API_KEY` | `str` | `""` | OpenAI API key (GPT-5, GPT-4.5-turbo). |
| `NCE_AZURE_OPENAI_API_KEY` | `str` | `""` | Azure OpenAI API key (`api-key` header). |
| `NCE_AZURE_OPENAI_ENDPOINT` | `str` | `""` | Azure OpenAI resource endpoint URL (required for `azure_openai`). |
| `NCE_AZURE_OPENAI_DEPLOYMENT` | `str` | `""` | Azure OpenAI model deployment name. |
| `NCE_GEMINI_API_KEY` | `str` | `""` | Google AI Studio / Gemini API key. |
| `NCE_DEEPSEEK_API_KEY` | `str` | `""` | DeepSeek API key. |
| `NCE_MOONSHOT_API_KEY` | `str` | `""` | Moonshot / Kimi API key. |
| `NCE_OPENAI_COMPAT_BASE_URL` | `str` | `""` | Base URL for generic OpenAI-compatible endpoints. |
| `NCE_OPENAI_COMPAT_API_KEY` | `str` | `""` | API key for generic OpenAI-compatible endpoints. |
| `NCE_OPENAI_COMPAT_MODEL` | `str` | `""` | Default model name for generic OpenAI-compatible endpoints. |
| `NCE_LLM_PROVIDER` | `str` | `local-cognitive-model` | Declarative default provider label (matches provider registry labels). |

---

## 10. Local Cognitive Backend, In-Process Embeddings & NLI

| Variable | Type | Default | Description |
|---|---|---|---|
| `NCE_COGNITIVE_BASE_URL` | `str` | `""` | Base URL for local cognitive HTTP service (e.g. `http://cognitive:11435`). Routes embeddings to `POST {base}/v1/embeddings`. |
| `NCE_COGNITIVE_EMBEDDING_MODEL` | `str` | `""` | Model identifier requested from cognitive HTTP endpoint. |
| `NCE_COGNITIVE_FALLBACK_MODEL` | `str` | `text-embedding-3-small` | Fallback embedding model used when primary cognitive backend returns 429 or times out. |
| `NCE_COGNITIVE_API_KEY` | `str` | `""` | Optional API key for cognitive HTTP endpoint. |
| `NCE_EMBEDDING_MODEL_ID` | `str` | `jinaai/jina-embeddings-v2-base-code` | HuggingFace model ID for in-process embedding inference. |
| `NCE_EMBEDDING_MODEL_REVISION` | `str` | `""` | HuggingFace commit SHA to pin for supply-chain integrity. Empty string selects `latest`. |
| `NCE_EMBEDDING_TRUST_REMOTE_CODE` | `bool` | `false` | Set `true` to pass `trust_remote_code=True` to HuggingFace `AutoModel.from_pretrained`. |
| `NCE_BACKEND` | `str` | `""` | Hardware acceleration backend selector (`openvino-npu` selects Intel NPU OpenVINO path; empty auto-selects CPU/CUDA). |
| `NCE_OPENVINO_MODEL_DIR` | `str` | `""` | Directory containing exported OpenVINO IR files (required when `NCE_BACKEND=openvino-npu`). |
| `NCE_OPENVINO_SEQ_LEN` | `int` | `512` | Static sequence length for compiled OpenVINO NPU computational graph. |
| `EMBEDDING_VECTOR_DIM` | `int` | `768` | Vector dimension in PostgreSQL `pgvector` (`memories.embedding`, `kg_nodes.embedding`). Changing requires database schema migration. |
| `EMBEDDING_MAX_WORKERS` | `int` | `1` | Thread-pool worker count for in-process embedding calculations (minimum `1`). |
| `EMBED_BATCH_CHUNK` | `int` | `64` | Maximum memories per embedding batch chunk (minimum `1`). |
| `NCE_EMBED_MAX_BATCH_TEXTS` | `int` | `512` | Hard input guard: maximum texts per batch (batches exceeding this are rejected, minimum `1`). |
| `NCE_EMBED_MAX_TEXT_CHARS` | `int` | `32000` | Hard input guard: maximum character count per text in an embedding batch (minimum `1`). |
| `NLI_MODEL_ID` | `str` | `cross-encoder/nli-deberta-v3-small` | HuggingFace model ID for NLI contradiction scoring. |
| `NCE_NLI_IDLE_TTL_S` | `int` | `900` | Idle timeout in seconds after which NLI model is evicted from memory (`0` disables eviction, minimum `0`). |
| `NCE_CONTRADICTION_SIMILARITY_THRESHOLD` | `float` | `0.85` | Minimum cosine similarity threshold to flag memories as contradiction candidates (minimum `0.0`). |
| `NCE_CONTRADICTION_MAX_CANDIDATES` | `int` | `3` | Maximum candidate memory pairs sent to NLI per memory (minimum `1`). |
| `NCE_CONTRADICTION_NLI_THRESHOLD` | `float` | `0.8` | NLI entailment confidence threshold; pairs exceeding this are escalated to LLM evaluation (minimum `0.0`). |
| `NCE_CONTRADICTION_LLM_MIN_CONFIDENCE` | `float` | `0.6` | Minimum LLM confidence required to confirm a contradiction verdict (minimum `0.0`). |

---

## 11. Document Bridges & OAuth

| Variable | Type | Default | Description |
|---|---|---|---|
| `BRIDGE_WEBHOOK_BASE_URL` | `str` | `""` | Public HTTPS base URL for inbound webhook delivery subscriptions. |
| `NCE_WEBHOOK_TRUST_PROXY` | `bool` | `false` | When `true`, webhook rate limiters trust `X-Forwarded-For` from reverse proxies. |
| `WEBHOOK_MAX_BODY_BYTES` | `int` | `1048576` | Maximum allowed webhook request payload size in bytes (1 MB). |
| `WEBHOOK_RATE_LIMIT` | `int` | `120` | Maximum webhook requests allowed per IP within the webhook rate period (minimum `1`). |
| `WEBHOOK_RATE_PERIOD_SECONDS` | `int` | `60` | Webhook rate limiting window in seconds (minimum `1`). |
| `WEBHOOK_DEDUP_TTL_SECONDS` | `int` | `86400` | Redis TTL in seconds for webhook delivery deduplication keys (minimum `60`). |
| `WEBHOOK_DEDUP_FAIL_OPEN` | `bool` | `false` | When `true`, webhook dedup skips checks if Redis is offline. **Must be `false` in production** (fails fast at startup). |
| `BRIDGE_RESOLVE_TIMEOUT_S` | `float` | `10.0` | Timeout in seconds for bridge worker token resolution and OAuth exchanges (minimum `0.1`). |
| `GRAPH_BRIDGE_TOKEN` | `str` | `""` | Microsoft Graph API OAuth bearer token. |
| `GDRIVE_BRIDGE_TOKEN` | `str` | `""` | Google Drive OAuth bearer token. |
| `DROPBOX_BRIDGE_TOKEN` | `str` | `""` | Dropbox OAuth bearer token. |
| `AZURE_CLIENT_ID` | `str` | `""` | Azure AD application client ID for SharePoint and OneDrive. |
| `AZURE_CLIENT_SECRET` | `str` | `""` | Azure AD application client secret. |
| `AZURE_TENANT_ID` | `str` | `common` | Azure AD tenant ID (`common` for multi-tenant apps). |
| `BRIDGE_OAUTH_REDIRECT_URI` | `str` | `http://127.0.0.1:8765/bridge/oauth/callback` | OAuth redirect URI for local bridge token acquisition. |
| `GDRIVE_OAUTH_CLIENT_ID` | `str` | `""` | Google Drive OAuth 2.0 client ID. |
| `GDRIVE_OAUTH_CLIENT_SECRET` | `str` | `""` | Google Drive OAuth 2.0 client secret. |
| `DROPBOX_OAUTH_CLIENT_ID` | `str` | `""` | Dropbox application client ID. |
| `BRIDGE_RENEWAL_LOOKAHEAD_HOURS` | `int` | `12` | Proactively renew bridge subscriptions expiring within this many hours. |
| `BRIDGE_CRON_INTERVAL_MINUTES` | `int` | `45` | Bridge subscription renewal cron interval in minutes. |

### Webhook Secrets (`nce/webhook_receiver`)

| Variable | Description |
|---|---|
| `DROPBOX_APP_SECRET` | Dropbox App Secret used to verify `X-Dropbox-Signature` HMAC-SHA256 headers. |
| `GRAPH_CLIENT_STATE` | Secret string validated against `clientState` in Microsoft Graph webhook notifications. |
| `DRIVE_CHANNEL_TOKEN` | Secret token verified against `X-Goog-Channel-Token` in Google Drive webhooks. |

---

## 12. SMTP Notifications

> Read by `nce/notifications.py`.

| Variable | Type | Default | Description |
|---|---|---|---|
| `NCE_SMTP_FROM` | `str` | `""` | Sender email address for security and system alerts (required when SMTP is enabled). |
| `NCE_SMTP_TO` | `str` | `""` | Recipient email address for system alerts (required when SMTP is enabled). |
| `NCE_SMTP_USER` | `str` | `""` | Optional SMTP authentication username. |
| `NCE_SMTP_PASS` | `str` | `""` | Optional SMTP authentication password. |

SMTP sends via port 587 using STARTTLS (`aiosmtplib`). Host is configured programmatically on `NotificationDispatcher.smtp_host`.

---

## 13. Garbage Collection, Consolidation & Re-embedding

### Orphan Garbage Collector (GC)

| Variable | Type | Default | Description |
|---|---|---|---|
| `GC_INTERVAL_SECONDS` | `int` | `3600` | How often the orphan GC background loop executes (seconds). |
| `GC_ORPHAN_AGE_SECONDS` | `int` | `86400` | Minimum age in seconds before a payload-less Mongo document is treated as an orphan. |
| `GC_PAGE_SIZE` | `int` | `500` | Keyset pagination batch size for GC database scans. |
| `GC_MAX_CONNECT_ATTEMPTS` | `int` | `5` | Reconnect retry attempts before GC loop terminates. |
| `GC_CONNECT_BASE_DELAY` | `float` | `2.0` | Exponential back-off base delay between GC reconnect attempts (seconds). |
| `GC_ALERT_THRESHOLD` | `int` | `100` | Number of detected orphan records that triggers an administrative alert dispatch. |

### Consolidation & Outbox Relay

| Variable | Type | Default | Description |
|---|---|---|---|
| `CONSOLIDATION_DECAY_SOURCES` | `bool` | `false` | When `true`, soft-decays source memories following a successful consolidation run. |
| `CONSOLIDATION_CRON_INTERVAL_MINUTES` | `int` | `360` | Interval in minutes between consolidation cron runs (6 hours default). |
| `CONSOLIDATION_HALF_LIFE_DAYS` | `float` | `30.0` | Ebbinghaus half-life decay parameter for memory salience (days). |
| `NCE_MAX_DERIVATION_DEPTH` | `int` | `2` | Derivation depth ceiling. Memories at or above this depth are excluded from clustering input to prevent runaway hallucination compounding (minimum `1`). |
| `NCE_DERIVATION_CONFIDENCE_DECAY` | `float` | `0.85` | Per-generation confidence decay factor $\gamma$: derived KG edge confidence is multiplied by $\gamma^{\text{depth}}$ upon insertion (range 0.0–1.0). |
| `CRON_STARTUP_JITTER_MAX_SECONDS` | `float` | `60.0` | Maximum random startup delay in seconds applied before first cron execution to prevent thundering-herd database spikes (`0` disables). |
| `OUTBOX_RELAY_INTERVAL_SECONDS` | `int` | `5` | Interval in seconds between outbox relay event scans (minimum `1`). |

### Re-embedding Worker & Migration Gates (Phase 2.1)

| Variable | Type | Default | Description |
|---|---|---|---|
| `REEMBED_BATCH_SIZE` | `int` | `32` | Number of memory records processed per re-embedding batch (minimum `1`). |
| `REEMBED_BATCHES_PER_MINUTE` | `int` | `20` | Rate limiter: maximum re-embedding batches processed per minute (minimum `1`). |
| `REEMBED_MAX_ROWS_PER_RUN` | `int` | `0` | Maximum total rows processed per cron invocation (`0` = unlimited). |
| `REEMBED_INCLUDE_KG_NODES` | `bool` | `false` | When `true`, includes knowledge graph node embeddings in re-embedding sweeps. |
| `REEMBED_MAX_TEXT_CHARS` | `int` | `4096` | Maximum characters sent to embedding model per memory record (minimum `256`). |
| `REEMBED_CRON_INTERVAL_MINUTES` | `int` | `60` | Interval in minutes between background re-embedding sweeps (minimum `1`). |
| `NCE_REEMBED_VRAM_HIGH_WATERMARK` | `float` | `0.85` | Fraction of total CUDA VRAM at which embedding is paused to prevent OOM (range 0.1–0.99). |
| `NCE_REEMBED_VRAM_MAX_PRESSURE_WAITS` | `int` | `12` | Number of sleep cycles under VRAM pressure before raising `VRAMPressureError` (minimum `0`). |
| `NCE_REEMBED_GATE_SAMPLE` | `int` | `200` | Number of randomly sampled memories used to evaluate nearest-neighbor overlap for migration commit (minimum `1`). |
| `NCE_REEMBED_GATE_MIN_OVERLAP` | `float` | `0.6` | Minimum Jaccard neighbor overlap ratio required to pass migration quality gate (range 0.0–1.0). |
| `NCE_REEMBED_GATE_K` | `int` | `10` | Number of nearest neighbors retrieved per sample point during migration validation (minimum `1`). |

---

## 14. Quotas & Resource Management

| Variable | Type | Default | Description |
|---|---|---|---|
| `NCE_QUOTAS_ENABLED` | `bool` | `true` | Master switch for per-namespace / per-agent quota enforcement on tool hot paths. |
| `NCE_QUOTA_TOKEN_ESTIMATE_DIVISOR` | `int` | `4` | Characters-per-token divisor used for pre-flight quota estimates. |
| `NCE_QUOTA_REDIS_COUNTERS` | `bool` | `true` | When `true`, increments quota consumption in Redis to prevent database row-lock contention. |
| `NCE_QUOTA_REDIS_FLUSH_INTERVAL_S` | `float` | `60.0` | How often Redis quota counters are flushed to PostgreSQL (seconds). |

---

## 15. Observability (OpenTelemetry & Prometheus)

| Variable | Type | Default | Description |
|---|---|---|---|
| `NCE_PROMETHEUS_PORT` | `int` | `8000` | Port for the Prometheus metrics scrape endpoint. |
| `NCE_OTEL_EXPORTER_OTLP_ENDPOINT` | `str` | `http://localhost:4318` | OpenTelemetry OTLP exporter gRPC/HTTP endpoint. |
| `NCE_OTEL_SERVICE_NAME` | `str` | `nce-python` | Service name reported in OpenTelemetry trace spans. |
| `NCE_OBSERVABILITY_ENABLED` | `bool` | `true` | Master switch for OpenTelemetry tracing and Prometheus metrics. |

---

## 16. Task Queue & Dead-Letter Queue (DLQ)

| Variable | Type | Default | Description |
|---|---|---|---|
| `TASK_MAX_RETRIES` | `int` | `5` | Maximum RQ worker retry attempts before routing a task to `dead_letter_queue` (`0` = infinite retries). |
| `TASK_DLQ_REDIS_TTL` | `int` | `86400` | TTL in seconds for Redis attempt-counter keys (24 hours default). |
| `NCE_DLQ_AUTO_REPLAY_MAX` | `int` | `3` | Maximum auto-replay attempts for transient task failures before escalating to manual intervention (minimum `0`). |
| `NCE_DLQ_CIRCUIT_THRESHOLD` | `int` | `3` | Number of matching-fingerprint DLQ failures required to open the circuit breaker for a task type (minimum `1`). |
| `NCE_DLQ_CIRCUIT_TTL_S` | `int` | `3600` | TTL in seconds for the task circuit-breaker open state in Redis (minimum `1`). |
| `NCE_DISABLE_MIGRATION_MCP` | `bool` | `true` (prod) / `false` (dev) | When `true`, excludes `start_migration`, `commit_migration`, and `abort_migration` from MCP tool registration. |

---

## 17. Temporal Queries, Retention & Immutability Anchors

| Variable | Type | Default | Description |
|---|---|---|---|
| `NCE_MAX_TEMPORAL_LOOKBACK_DAYS` | `int` | `90` | Maximum lookback window in days for `as_of` temporal queries. Prevents full-table scans on `event_log` (`0` disables). |
| `NCE_ANCHOR_BUCKET` | `str` | `nce-tamper-anchors` | Object-locked (WORM) MinIO bucket receiving Merkle chain heads. Must be created with versioning + object-lock enabled. |
| `NCE_ANCHOR_INTERVAL_MINUTES` | `int` | `60` | Interval in minutes between external tamper-anchor snapshots (minimum `1`). |
| `NCE_ANCHOR_RETENTION_DAYS` | `int` | `365` | COMPLIANCE-mode object-lock retention period in days applied to anchor blobs in MinIO (minimum `1`). |
| `NCE_EVENT_RETENTION_MONTHS` | `int` | `24` | Months of `event_log` history retained in PostgreSQL before archiving to MinIO (requires anchor confirmation, minimum `1`). |
| `NCE_CONTRADICTION_RETENTION_DAYS` | `int` | `180` | Days after which resolved contradictions are purged under tenant RLS (minimum `1`). |
| `NCE_EDGE_PRUNE_AGE_DAYS` | `int` | `90` | Days after which low-confidence (<0.15) KG edges are pruned, unless tagged `change_origin='sync'` (minimum `1`). |
| `NCE_RETENTION_INTERVAL_MINUTES` | `int` | `1440` | Retention maintenance cron execution interval in minutes (24 hours default, minimum `1`). |

---

## 18. Media Upload, Extraction & Sizing Limits

| Variable | Type | Default | Description |
|---|---|---|---|
| `NCE_MAX_ATTACHMENT_BYTES` | `int` | `20971520` (20 MB) | Maximum accepted blob size for `extract_bytes` and `store_media` to prevent worker OOM. |
| `NCE_MAX_OCR_PAGES` | `int` | `10` | Maximum pages processed by OCR per document (minimum `1`). |
| `NCE_ENVELOPE_ENCRYPTION_ENABLED` | `bool` | `false` | When `true`, `store_memory` encrypts raw payload dispatched to MongoDB `episodes.raw_data` with per-memory DEKs wrapped under `NCE_MASTER_KEY`. |
| `NCE_MAX_ARGUMENTS_JSON_SIZE` | `int` | `1000000` (1 MB) | Maximum allowed JSON size in bytes for tool call arguments (minimum `1024`). |
| `NCE_MAX_METADATA_KEYS` | `int` | `512` | Maximum number of metadata keys allowed per memory (minimum `1`). |
| `NCE_MAX_METADATA_KEY_LEN` | `int` | `256` | Maximum length in characters of a metadata key string (minimum `1`). |
| `NCE_MAX_METADATA_STRING_VALUE_LEN` | `int` | `4096` | Maximum character length of any string metadata value (minimum `1`). |
| `NCE_MAX_METADATA_LIST_ITEMS` | `int` | `256` | Maximum item count in a metadata list value (minimum `1`). |
| `NCE_MAX_CONCURRENT_TOOLS` | `int` | `16` | Maximum concurrent in-flight MCP tool dispatches permitted (minimum `1`). |
| `NCE_MAX_CODE_INDEX_BYTES` | `int` | `2097152` (2 MB) | Maximum source file size allowed through `index_code_file()` (minimum `1024`). |
| `NCE_MAX_CODE_CHUNKS_PER_FILE` | `int` | `500` | Maximum AST/line chunks extracted per source code file (minimum `1`). |
| `NCE_ARTIFACT_STAGING_DIR` | `str` | `""` | Optional local filesystem directory for orchestrator artifact staging. |

---

## 19. Vertical Modules & Business Domain Configs

### 19a. NetBox Integration

| Variable | Type | Default | Description |
|---|---|---|---|
| `NCE_NETBOX_URL` | `str` | `""` | NetBox base URL (trailing slash stripped). Required when NetBox vertical modules are active. |
| `NCE_NETBOX_TOKEN` | `str` | `""` | NetBox REST API token. Required when `NCE_NETBOX_URL` is set. |
| `NCE_NETBOX_DEFAULT_INTERFACE_TYPE` | `str` | `1000base-t` | Default interface type assigned when creating NetBox interface records without an explicit type. |

### 19b. Dynamics 365 / Dataverse

| Variable | Type | Default | P-level | Description |
|---|---|---|---|---|
| `NCE_D365_ENABLED` | `bool` | `false` | — | Master activation switch for the Dynamics 365 vertical module. |
| `NCE_D365_ORG_URL` | `str` | `""` | **P1 (prod)** | Dataverse organisation URL (e.g. `https://org.crm.dynamics.com`). Required in prod when `NCE_D365_ENABLED=true`. |
| `NCE_D365_WEBHOOK_SECRET` | `str` | `""` | **P1 (prod)** | Shared secret for validating inbound D365 webhook payloads. Required in prod when `NCE_D365_ENABLED=true`. |
| `NCE_D365_API_VERSION` | `str` | `9.2` | — | OData API version suffix used in Dataverse REST requests. |
| `NCE_D365_SYNC_INTERVAL_MINUTES` | `int` | `60` | — | Interval in minutes between D365 synchronization runs (minimum `5`). |
| `NCE_D365_SYNC_PAGE_SIZE` | `int` | `500` | — | OData `$top` page size for synchronization queries (minimum `10`). |
| `NCE_D365_HIGH_PRIORITY_SALIENCE_BOOST` | `float` | `2.0` | — | Salience multiplier applied to high-priority D365 records (minimum `1.0`). |
| `NCE_D365_EMPATHIC_URGENCY_KEYWORDS` | `str` | `urgent,critical,asap,...` | — | Comma-separated keyword list triggering urgency classification in case ingestion. |
| `NCE_D365_EMPATHIC_FRUSTRATION_KEYWORDS` | `str` | `disappointed,unacceptable,...` | — | Comma-separated keyword list triggering frustration classification in case ingestion. |
| `NCE_D365_INCREMENTAL_ENABLED` | `bool` | `false` | — | When `true`, sync pulls only Dataverse records modified since `last_sync_at`. |
| `NCE_D365_CHANGE_TRACKING_ENABLED` | `bool` | `false` | — | When `true`, detects Dataverse deletions via `odata.track-changes` deltaLinks and hard-deletes derived KG nodes. |

### 19c. D365 ↔ NetBox Cross-Reference Bridge

| Variable | Type | Default | Description |
|---|---|---|---|
| `NCE_D365_NETBOX_BRIDGE_ENABLED` | `bool` | `false` | Master switch for D365 ↔ NetBox cross-referencing (requires NetBox credentials). |
| `NCE_D365_NETBOX_BRIDGE_INTERVAL_MINUTES` | `int` | `120` | Bridge synchronization interval in minutes (minimum `10`). |
| `NCE_D365_NETBOX_FUZZY_THRESHOLD` | `float` | `0.82` | Minimum `SequenceMatcher` ratio to accept fuzzy entity name matching (range 0.5–1.0). |
| `NCE_D365_NETBOX_TENANT_CF_NAME` | `str` | `d365_account_id` | NetBox custom field name storing D365 account GUIDs on tenant objects. |

### 19d. Economy Vertical Module — PEPPOL / EHF

| Variable | Type | Default | Description |
|---|---|---|---|
| `NCE_ECONOMY_PEPPOL_ENABLED` | `bool` | `false` | Outbound PEPPOL/EHF safety interlock. When `false`, returns generated EHF without attempting network transmission or resolving credentials. |
| `NCE_ECONOMY_PEPPOL_MODE` | `str` | `sandbox` | Network environment selector (`sandbox` vs `prod`). Informational flag echoed by `do_generate_ehf`. |
| `NCE_ECONOMY_PEPPOL_API_KEY` | `str` | `""` | PEPPOL access point API key (dynamically resolved via `resolve_secret`). |
| `NCE_ECONOMY_PEPPOL_BASE_URL` | `str` | `""` | PEPPOL access point gateway URL (dynamically resolved via `resolve_secret`). |

### 19e. Watchers & Domain Workflows

| Variable | Type | Default | Description |
|---|---|---|---|
| `NCE_PRODUCT_EOL_WATCHER_INTERVAL_MINUTES` | `int` | `360` | Interval in minutes between scans for EOL/EOS hardware/software products to generate `replaced_by` KG edges (minimum `5`). |
| `NCE_AGREEMENTS_COVERAGE_WATCHER_INTERVAL_MINUTES` | `int` | `1440` | Interval in minutes between agreement coverage matrix checks for SLA leakage and expiry alerts (minimum `5`). |
| `NCE_PRICING_MAX_AGE` | `int` | `86400` | Maximum age in seconds before a pricing record in the Shared Pricing Service is flagged as stale (minimum `1`). |
| `NCE_PROCUREMENT_RECALIBRATE_AFTER_N` | `int` | `100` | Rolling window size of recorded decisions per supplier required to trigger threshold recalibration (minimum `1`). |
| `NCE_PROCUREMENT_AUTONOMY_PO_CEILING` | `float` | `0.0` | Maximum purchase order value approved autonomously by C2 governor without human confirmation (`0.0` requires confirmation for all POs). |
| `NCE_VENDORS_SCORECARD_MIN_SAMPLE` | `int` | `5` | Minimum outcome events required before vendor scorecard generates non-neutral composite score (minimum `1`). |
| `NCE_AGREEMENTS_OCR_AUTOGREEN_THRESHOLD` | `int` | `90` | OCR confidence percentage at or above which extracted fields are auto-approved (range 1–100). |
| `NCE_AGREEMENTS_OCR_REVIEW_THRESHOLD` | `int` | `70` | OCR confidence percentage below which fields are routed to manual review (range 1–100). |
| `NCE_SYSTEM_DESIGN_RECALL_TOP_K` | `int` | `5` | Number of historical design/project memories recalled per design proposal call (minimum `1`). |
| `NCE_SYSTEM_DESIGN_OUTCOME_WEIGHTING_ENABLED` | `bool` | `false` | When `true`, discounts proposal recall scores using margin and support-ticket pressure data from the project ledger. |

### 19f. Chain Integrity Verification

| Variable | Type | Default | Description |
|---|---|---|---|
| `NCE_CHAIN_VERIFY_INTERVAL_MINUTES` | `int` | `120` | Interval in minutes between cryptographic Merkle chain verification runs (minimum `5`). |
| `NCE_CHAIN_VERIFY_STARTUP_DEPTH` | `int` | `500` | Number of recent Merkle chain entries verified at server startup (`0` disables startup verification, minimum `0`). |

### 19g. Spreading Activation Telemetry & Gamification

| Variable | Type | Default | Description |
|---|---|---|---|
| `NCE_TELEMETRY_SPIKE_THRESHOLD` | `float` | `8.0` | Graph activation spike threshold for triggering telemetry alert dispatch (minimum `0.0`). |
| `NCE_TELEMETRY_SPIKE_THETA` | `float` | `0.25` | Theta parameter governing activation charge decay across graph edges (minimum `0.0`). |
| `NCE_TELEMETRY_SPIKE_CHARGE` | `float` | `2.0` | Initial activation charge injected at spike source nodes (minimum `0.0`). |
| `NCE_ACTIVE_LEARNING_CONFIRM_XP` | `int` | `10` | XP awarded to operator for confirmed active learning signals (minimum `0`). |
| `NCE_ACTIVE_LEARNING_REJECT_XP` | `int` | `5` | XP awarded to operator for rejected active learning signals (minimum `0`). |

### 19h. Diagnostic Log Digestion Engine

| Variable | Type | Default | Description |
|---|---|---|---|
| `NCE_DIAG_ENABLED` | `bool` | `false` | Master switch to enable background diagnostic log digestion engine. |
| `NCE_DIAG_LANDING_BUCKET` | `str` | `nce-diag-landing` | MinIO landing bucket name for uploaded diagnostic log bundles. |
| `NCE_DIAG_LANDING_TTL_DAYS` | `int` | `7` | Retention period in days for raw diagnostic bundles in landing bucket (minimum `1`). |
| `NCE_DIAG_MAX_BUNDLE_MB` | `int` | `700` | Maximum raw diagnostic bundle size in megabytes accepted for processing (minimum `1`). |
| `NCE_DIAG_MAX_ANOMALIES` | `int` | `50` | Maximum anomaly records extracted per diagnostic digestion run (minimum `1`). |
| `NCE_DIAG_JOB_TIMEOUT_MIN` | `int` | `45` | Processing timeout in minutes for diagnostic bundle digestion jobs (minimum `1`). |
| `NCE_DIAG_CRASH_STORM_THRESHOLD` | `int` | `10` | Crash event count within detection window triggering crash-storm classification (minimum `1`). |
| `NCE_DIAG_CRASH_STORM_WINDOW_SEC` | `int` | `300` | Sliding window in seconds for crash-storm anomaly detection (minimum `1`). |
| `NCE_DIAG_TMPDIR` | `str` | `""` | Scratch directory for uncompressing large diagnostic bundles (defaults to system tmp). |

---

## 20. Server Entry-Points

### `server.py` — MCP stdio Server

```bash
python server.py
```

Listens on **stdio** (MCP JSON-RPC 2.0). All configuration is read from environment variables via `nce.config.cfg`.
Background tasks initialized on startup:
- `run_gc_loop()` — orphan payload garbage collection
- `start_re_embedder()` — re-embedding worker loop

### `admin_server.py` — REST Admin Interface

```bash
python admin_server.py
# or via uvicorn:
uvicorn admin_server:app --host 0.0.0.0 --port 8003
```

Default port: **8003**. Middleware pipeline:
1. `OpenTelemetryTraceMiddleware` — distributed trace context propagation
2. `AdminHTTPRateLimitMiddleware` — IP sliding-window rate limiting
3. `MTLSAuthMiddleware` — client certificate enforcement (mandatory in production unless acknowledged)
4. `BasicAuthMiddleware` — HTTP Basic authentication for `/` UI routes
5. `HMACAuthMiddleware` — HMAC-SHA256 signature verification for `/api/` endpoints

### `nce/a2a_server.py` — A2A JSON-RPC Server

```bash
python -m nce.a2a_server
# or via uvicorn:
uvicorn nce.a2a_server:app --host 0.0.0.0 --port 8004
```

Default port: **8004**. Serves Agent Card discovery and JSON-RPC 2.0 task invocation endpoints. Guarded by `MTLSAuthMiddleware` and rate-limited via `NCE_A2A_HTTP_RATE_LIMIT`.

### `python -m nce.cron` — APScheduler Daemon

```bash
python -m nce.cron
```

Executes periodic background jobs:
- `bridge_subscription_renewal` — renews expiring document bridge subscriptions
- `phase_2_1_reembedding` — re-embedding maintenance
- `product_eol_watcher` — product lifecycle scans
- `agreements_coverage_watcher` — contract SLA leakage and coverage analysis
- `chain_verification` — Merkle chain integrity checks
- `external_tamper_anchor` — WORM MinIO Merkle root anchoring
- `retention_maintenance` — partition archival and edge pruning

### `start_worker.py` — RQ Asynchronous Worker

```bash
python start_worker.py
# or directly with rq:
rq worker nce-tasks --url $REDIS_URL
```

Processes asynchronous background queues (`nce-tasks`), including source code AST parsing and embedding jobs.

---

## 21. Validation & Startup Fail-Fast Gates

`_Config.validate()` is called automatically when connecting the engine (`NCEEngine.connect()`). In addition, critical guards evaluate at module import time when `NCE_ENV=prod`.

### P0 — Immediate Startup Failure in All Environments
- `NCE_MASTER_KEY` missing or shorter than 32 UTF-8 bytes (evaluated at module import and in `validate()`).
- `MINIO_ACCESS_KEY` or `MINIO_SECRET_KEY` empty (when `NCE_MINIO_REQUIRED=true`).
- `MONGO_URI`, `PG_DSN`, or `REDIS_URL` missing.

### P0 — Production-Only Failures (`NCE_ENV=prod`)
- `PG_DSN`, `MONGO_URI`, or `REDIS_URL` configured with default development connection strings.
- `NCE_BYPASS_WORM=true` or `NCE_BYPASS_RLS=true` (evaluated at import time).
- `NCE_ALLOW_ADMIN_DOTENV_PERSIST=true` (evaluated at import time and in `validate_secrets_provider`).
- `NCE_ADMIN_OVERRIDE=true` (evaluated at import time).
- `NCE_LOAD_DOTENV=true` (evaluated at import time).

### P1 — Production-Only Failures (`NCE_ENV=prod`)
- `NCE_API_KEY` absent (HMAC admin API key).
- `NCE_JWT_SECRET` and `NCE_JWT_PUBLIC_KEY` both absent when JWT authentication is used.
- `NCE_A2A_JWT_AUDIENCE` empty string.
- `NCE_MCP_API_KEY` absent for MCP stdio tenant tools.
- `NCE_MCP_NAMESPACE_ID` absent or not a valid UUID when `NCE_MCP_API_KEY` is set.
- `NCE_ADMIN_API_KEY`, `NCE_ADMIN_USERNAME`, or `NCE_ADMIN_PASSWORD` absent.
- `NCE_ADMIN_PASSWORD` not prefixed with `$pbkdf2$` (plaintext passwords forbidden).
- `NCE_DISABLE_MIGRATION_MCP=false` without `NCE_ALLOW_MIGRATION_MCP_IN_PROD=true`.
- `WEBHOOK_DEDUP_FAIL_OPEN=true`.
- `NCE_D365_ORG_URL` or `NCE_D365_WEBHOOK_SECRET` absent when `NCE_D365_ENABLED=true`.
- Server mTLS disabled without operator acknowledgement: `assert_server_mtls_or_acknowledged()` raises `MTLSNotConfiguredError` during `admin_server` and `a2a_server` boot lifespans if `NCE_ADMIN_MTLS_ENABLED=false` or `NCE_A2A_MTLS_ENABLED=false`, unless `NCE_MTLS_ACKNOWLEDGE_DISABLED=true` is set.

### Non-Halting Warnings in Development / Non-Production
- `NCE_API_KEY` absent (admin API inaccessible).
- Neither `NCE_JWT_SECRET` nor `NCE_JWT_PUBLIC_KEY` configured (A2A sharing disabled).
- `NCE_JWT_ALGORITHM=HS256` in production (warns to prefer RS256/ES256).
- `NCE_MCP_API_KEY` absent (MCP tenant tools accept unauthenticated invocations; setting it does not add caller authentication either — see `docs/enterprise_security.md` §2b).
- `NCE_MCP_NAMESPACE_ID` absent when `NCE_MCP_API_KEY` is set (accepts caller-supplied `namespace_id`).
- Incomplete admin credentials (`NCE_ADMIN_API_KEY`, `NCE_ADMIN_USERNAME`, `NCE_ADMIN_PASSWORD`).
