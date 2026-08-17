> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# NCE Enterprise Security Guide

This document details the security model, cryptographic controls, and access authorization boundaries implemented in the Neuro Cognitive Engine (NCE).

---

## 1. Authentication Architecture

NCE exposes four distinct communication interfaces, each using an authentication mechanism tailored to its protocol and exposure surface:

```
                  ┌────────────────────────────────────────────────────────┐
                  │                   Client Applications                  │
                  └───────────────────────────┬────────────────────────────┘
                                              │
         ┌──────────────────┬─────────────────┴─────────────────┬──────────────────┐
         │ (Stdio Pipe)     │ (HTTP REST / UI)                  │ (JSON-RPC)       │ (Public HTTP GET)
         ▼                  ▼                                   ▼                  ▼
┌──────────────────┐┌──────────────────────────────────┐┌──────────────────┐┌───────────────────────────────┐
│    MCP Stdio     ││            Admin API             ││    A2A Server    ││    Public Customer Quote API  │
│  (server.py)     ││        (admin_server.py)         ││ (a2a_server.py)  ││       (admin_app.py)          │
├──────────────────┤├──────────────────────────────────┤├──────────────────┤├───────────────────────────────┤
│ - NCE_MCP_API_KEY││ - HMAC-SHA256 (API)              ││ - Bearer JWT     ││ - Stateless HMAC-SHA256 Token │
│ - Pin Namespace  ││ - HTTP Basic (UI)                ││ - A2A Grants     ││ - C8 Redactor Projection      │
│ - Admin API Key  ││ - mTLS (Mandatory in prod)       ││ - mTLS (Mandatory││ - Rate Limited (5 req / 10s)  │
│                  ││                                  ││   in prod)       ││ - Bypasses HMAC/Basic/mTLS    │
└──────────────────┘└──────────────────────────────────┘└──────────────────┘└───────────────────────────────┘
```

| Service Surface | Transport | Primary Security Protocol | Configuration Variables |
| :--- | :--- | :--- | :--- |
| **MCP Stdio Server** | Standard Process Pipes | Symmetric API Key Validation + Namespace Pinning. Admin-scoped MCP tools additionally require the `NCE_ADMIN_API_KEY` bearer token (`require_scope("admin")`). | `NCE_MCP_API_KEY`, `NCE_MCP_NAMESPACE_ID`, `NCE_ADMIN_API_KEY` (MCP admin-scope bearer token) |
| **Admin REST API & UI** | HTTP / HTTPS | HMAC-SHA256 Signature (API) / HTTP Basic (UI) + mTLS (**Mandatory in prod**) | `NCE_API_KEY` (HMAC shared secret for Admin HTTP `/api/*`), `NCE_ADMIN_USERNAME`, `NCE_ADMIN_PASSWORD`, `NCE_ADMIN_MTLS_ENABLED`, `NCE_MTLS_ACKNOWLEDGE_DISABLED` |
| **A2A (Agent-to-Agent)** | HTTP / HTTPS | Asymmetric JWT Bearer Tokens + mTLS (**Mandatory in prod**) + Sharing Grants | `NCE_JWT_SECRET`, `NCE_JWT_PUBLIC_KEY`, `NCE_A2A_MTLS_ENABLED`, `NCE_A2A_JWT_AUDIENCE`, `NCE_MTLS_ACKNOWLEDGE_DISABLED` |
| **Public Customer Quote API** (`GET /public-api/sales/quotes/{id}`) | HTTP / HTTPS | Stateless Bearer Token `HMAC-SHA256(NCE_MASTER_KEY, quote_id)` + C8 Redactor Projection (`public-quote`) + Sliding-Window Rate Limit (5 req / 10s). Bypasses Basic/HMAC/mTLS auth entirely; no expiry, no revocation. | `NCE_MASTER_KEY` (Customer link signing & verification) |

---

## 2. MCP Stdio Authentication & Namespace Pinning

The MCP stdio server (`server.py`) operates as a child process of the client IDE (such as Cursor or Claude Desktop).

### 2a. Configuration Envelope
When running in production, the client environment must inject the security keys into the launch configuration:

```json
{
  "mcpServers": {
    "nce-memory": {
      "command": "python",
      "args": ["/path/to/nce/server.py"],
      "env": {
        "NCE_MCP_API_KEY": "mcp_client_tenant_secret_key_string",
        "NCE_MASTER_KEY": "aes_256_gcm_vault_master_key_material",
        "NCE_MCP_NAMESPACE_ID": "673f8e91-654e-48bd-b7bb-ea392d4f8001"
      }
    }
  }
}
```

### 2b. Namespace Pinning Constraint
* **Tenant Isolation**: By specifying `NCE_MCP_NAMESPACE_ID`, the stdio server locks all incoming requests to that single namespace. Any payload specifying a different `namespace_id` is rejected at the entry dispatcher boundary.
* **Key Validation**: Every incoming tool call must include the correct `mcp_api_key` matching the environment's `NCE_MCP_API_KEY`. If the key is missing, invalid, or the `namespace_id` does not match the configured binding, the request fails with JSON-RPC `-32005` (`ScopeError` / scope forbidden). A structurally malformed `namespace_id` UUID also raises `-32005`.

---

## 3. HMAC-SHA256 API Authentication (Admin API)

All programmatically triggered HTTP routes exposed on the Admin API (`admin_server.py` on port `8003`) require HMAC-SHA256 request authentication to prevent payload tampering and replay attacks.

> **Key distinction**: The Admin plane uses **two separate keys** with different scopes:
> - `NCE_API_KEY` — HMAC-SHA256 shared secret used by `HMACAuthMiddleware` to authenticate HTTP admin API requests (routes under `/api/*`). Required in production.
> - `NCE_ADMIN_API_KEY` — Bearer token validated by `require_scope("admin")` for MCP admin tool calls and admin-scoped operations inside the stdio server. Required in production.
>
> Neither key substitutes for the other. `HMACAuthMiddleware` reads `cfg.NCE_API_KEY` exclusively.

### 3a. Header Signature Structure
The protocol is a **three-header** scheme. Every request to `/api/*` requires three distinct authentication headers:

```http
X-NCE-Timestamp: <unix_epoch_seconds>
Authorization: HMAC-SHA256 <hex_signature>
X-NCE-Nonce: <unique_nonce_string>
```

* **`X-NCE-Timestamp`**: Unix epoch time in seconds (integer, UTC), sent as a separate header.
* **`Authorization`**: The scheme token `HMAC-SHA256` followed by a single space and the lowercase hex-encoded HMAC computed with `NCE_API_KEY` over the canonical string. The value is the signature only — no `timestamp:` or `nonce:` prefix.
* **`X-NCE-Nonce`**: Unique per-request nonce (e.g. UUIDv4 or cryptographically random hex/string) generated by the client to prevent replay attacks across distributed replicas.

### 3b. Signature Calculation Formula
The canonical string is **method-first** and **newline-joined** (`\n`). It contains 3 parts — `METHOD`, `PATH`, `TIMESTAMP` — and a 4th part, the hex SHA-256 digest of the raw body, is appended only when the request body is non-empty (it is omitted for GET and other empty-body requests). The per-request `X-NCE-Nonce` header is verified independently by the replay store and is not part of the canonical string.

$$\text{CanonicalString} = \text{METHOD} \mathbin{\Vert} \text{"\n"} \mathbin{\Vert} \text{PATH} \mathbin{\Vert} \text{"\n"} \mathbin{\Vert} \text{TIMESTAMP} \; [\, \mathbin{\Vert} \text{"\n"} \mathbin{\Vert} \text{SHA256\_HEX(Body)} \,]$$

$$\text{Signature} = \text{HMAC-SHA256}(\text{NCE\_API\_KEY}, \text{CanonicalString})$$

The `METHOD` is uppercased and the `PATH` is the request URL path (excluding query strings). Comparison is constant-time (`hmac.compare_digest`).

### 3c. Anti-Replay Mitigation & Nonce Store Mechanics
The verification middleware (`HMACAuthMiddleware` in `nce/auth.py`) enforces two layers of replay protection:

1. **Clock Skew Tolerance**: The `X-NCE-Timestamp` value is checked against the server clock. If the absolute skew exceeds `NCE_CLOCK_SKEW_TOLERANCE_S` (default: **90 seconds**, shrunk from 300s in Batch 116), the request is rejected with JSON-RPC error code `-32002` (`replay_or_clock_skew`).
2. **Distributed Nonce Store (`NonceStore`)**: The caller-supplied `X-NCE-Nonce` is atomically recorded in Redis via `SET key 1 NX PX ttl` (atomic SETNX) under the key `nce:nonce:<nonce>` with a TTL of $2 \times \text{skew tolerance}$ (default: 180 seconds). A duplicate nonce presented within this window is rejected with `-32002` (`replay_nonce_conflict`).

#### Production Default Configuration & Fail-Closed Behavior

> [!WARNING]
> **Production Configuration Hazard (Default Prod Rejections)**:  
> - `NCE_HMAC_NONCE_REQUIRED` defaults to **`true`**.  
> - `optional_hmac_nonce_store()` returns `None` unless `NCE_DISTRIBUTED_REPLAY` is explicitly set to **`true`** (default: `false`) and `REDIS_URL` is configured.  
> - When `NCE_ENV=prod` (or `production`), `_check_nonce` enforces a strict **fail-closed** policy: if `_nonce_store` is `None` or Redis is unreachable, it raises `nonce_store_unavailable` (code `-32002`, returning HTTP 401 / JSON-RPC error).  
> 
> **Result**: Under default settings in production, **all `/api/*` requests will be rejected** unless `NCE_DISTRIBUTED_REPLAY=true` and `REDIS_URL` are configured, or `NCE_HMAC_NONCE_REQUIRED=false` is explicitly set during non-distributed or transitional deployments.

**Nonce enforcement matrix (`_check_nonce` in `nce/auth.py`):**

| `NCE_HMAC_NONCE_REQUIRED` | NonceStore State | `X-NCE-Nonce` Header | Environment | Result |
| :--- | :--- | :--- | :--- | :--- |
| `true` (default) | Not configured (`None`) | Any | `prod` | **Rejected** (`-32002`, `nonce_store_unavailable`) |
| `true` (default) | Not configured (`None`) | Any | `dev` / `test` | Allowed (warning logged; timestamp-only) |
| `true` (default) | Configured, Redis down | Any | `prod` | **Rejected** (`-32002`, `nonce_store_unavailable`) |
| `true` (default) | Configured, Redis down | Any | `dev` / `test` | Allowed (warning logged) |
| `true` (default) | Configured, Redis up | Missing / Empty | Any | **Rejected** (`-32001`, `nonce_missing`) |
| `true` (default) | Configured, Redis up | Already seen | Any | **Rejected** (`-32002`, `replay_nonce_conflict`) |
| `true` (default) | Configured, Redis up | New nonce | Any | **Accepted** |
| `false` | Not configured (`None`) | Any | Any | Allowed (timestamp-only replay check) |
| `false` | Configured, Redis up | Present | Any | SETNX check; rejected if seen before |
| `false` | Configured, Redis up | Missing / Empty | Any | Allowed (nonce check skipped) |

---

## 4. JWT Bearer Token Authentication (A2A Server)

Autonomously operating agents communicating via the Agent-to-Agent (A2A) server on port `8004` present JWT Bearer tokens to assert identity.

### 4a. Cryptographic Verification Modes
* **Symmetric (HS256)**: For deployments within a single trust boundary, the signature is verified using `NCE_JWT_SECRET` (minimum 32 bytes).
* **Asymmetric (RS256 / ES256)**: For multi-organization agent federations, NCE validates signatures using a public certificate defined in `NCE_JWT_PUBLIC_KEY` (PEM string or local file path). The issuer is configured in `NCE_JWT_ISSUER`.

### 4b. Audience Isolation Policy
To prevent a token issued for one agent network from being reused against the administrative backend, NCE supports distinct audience (`aud`) verification rules:

```bash
# A2A audience (aud) claim required for /tasks/send.
# Default is "nce_a2a" in dev (NCE_ENV not prod). In production there is NO
# fixed default — it must be set to a non-empty value or startup fails.
NCE_A2A_JWT_AUDIENCE=nce_a2a

# General JWT audience for JWTAuthMiddleware-protected routes. OPTIONAL:
# defaults to an empty string, which SKIPS the audience check. Not enforced
# in production.
NCE_JWT_AUDIENCE=
```

* **`NCE_A2A_JWT_AUDIENCE`** — Defaults to `nce_a2a` when `NCE_ENV` is not `prod`/`production`; in production the default is an empty string and the value is **required non-empty**. Choose an audience identifier appropriate to your deployment; `nce_a2a` is the dev default, not a fixed network name.
* **`NCE_JWT_AUDIENCE`** — **Optional.** Defaults to `""` (empty), in which case the `aud` claim is not checked. It is never enforced in production, so it is never required. Omit it to skip audience verification on `JWTAuthMiddleware` routes.

`NCE_A2A_JWT_AUDIENCE` is required in production (`NCE_ENV=prod`); omitting it causes `cfg.validate_jwt_config()` to raise a `RuntimeError` at startup.

---

## 5. Mutual TLS (mTLS) Transport Security & Production Mandate

mTLS is **mandatory in production** (`NCE_ENV=prod`) for both the Admin REST API and the Agent-to-Agent (A2A) server. It is not an optional feature in production environments.

### 5a. Production Zero-Trust Transport Guard (`assert_server_mtls_or_acknowledged`)

To guarantee zero-trust network boundaries, NCE implements a boot-time transport guard function `assert_server_mtls_or_acknowledged(service, mtls_enabled, pg_pool)` in `nce/mtls.py`, invoked during startup lifespans:
- In `admin_lifespan` (`nce/admin_app.py`) for the Admin server (`service="admin"`).
- In `a2a_lifespan` (`nce/a2a_server.py`) for the A2A server (`service="a2a"`).

```python
# Guard enforcement logic (nce/mtls.py)
if not cfg.IS_PROD or mtls_enabled:
    return

if not cfg.NCE_MTLS_ACKNOWLEDGE_DISABLED:
    raise MTLSNotConfiguredError(
        f"Refusing to start {service!r} server: NCE_ENV is production but mTLS "
        "is not enabled. Zero-trust transport security is mandatory in production. "
        "Enable mTLS (set the per-service NCE_*_MTLS_ENABLED + trust anchors), "
        "or set NCE_MTLS_ACKNOWLEDGE_DISABLED=true to explicitly accept the "
        "weakened security posture."
    )
```

#### Enforcement Behavior Matrix

| Environment (`NCE_ENV`) | Server mTLS Setting | `NCE_MTLS_ACKNOWLEDGE_DISABLED` | Startup Result | Audit & Logging |
| :--- | :--- | :--- | :--- | :--- |
| `prod` / `production` | Enabled (`true`) | Any | **Starts normally** | Standard startup log |
| `prod` / `production` | Disabled (`false`) | `false` (default) | **Refuses to start** (`MTLSNotConfiguredError` raised) | Boot aborted; critical error logged |
| `prod` / `production` | Disabled (`false`) | `true` | **Starts with degraded posture** | `CRITICAL` log emitted + `config_changed` WORM event recorded |
| `dev` / `test` | Any | Any | **Starts normally** | Non-halting warning logged if mTLS disabled |

When `NCE_MTLS_ACKNOWLEDGE_DISABLED=true` allows an un-hardened production instance to boot, NCE records an immutable audit entry in the write-once event log (`event_log` table) via `append_event`:
- **Event type**: `config_changed`
- **Payload**: Captures `actor: "system"`, `reason: "mtls_disabled_acknowledged"`, and `changes` recording `service`, `mtls_enabled: False`, and `environment`.

### 5b. mTLS Middleware & Validation Modes

mTLS enforcement is implemented by `MTLSAuthMiddleware` (`nce/mtls.py`), positioned before authentication middleware:
1. **Client Certificate Header Forwarding**: In front of reverse proxies (e.g. Caddy, Envoy, Nginx), client certificate metadata is extracted from `X-Forwarded-Client-Cert` up to `NCE_*_MTLS_TRUSTED_PROXY_HOP` hops. Direct TLS connections extract certificates from ASGI transport scope.
2. **Trust Anchors & Validation**: Certificates are validated against configured CA bundles (`NCE_MTLS_CA_PATH`) and allowlists:
   - **SAN Matching**: `NCE_ADMIN_MTLS_ALLOWED_SANS` / `NCE_A2A_MTLS_ALLOWED_SANS` (case-insensitive DNS/URI comparison).
   - **Fingerprint Matching**: `NCE_ADMIN_MTLS_ALLOWED_FINGERPRINTS` / `NCE_A2A_MTLS_ALLOWED_FINGERPRINTS` (SHA-256 colon-separated hex).
3. **Rejection Response**: Connections lacking a valid client certificate in strict mode (`NCE_*_MTLS_STRICT=true`) are rejected with JSON-RPC error code `-32015` (`MTLSAuthMiddleware` failure) or HTTP 403.

---

## 6. Public Customer Quote API (`GET /public-api/sales/quotes/{id}`)

The public quote endpoint (`nce/admin_handlers/sales_public.py`) provides external, browser-accessible customer access to sales quotes.

### 6a. Authentication Bypass & Stateless Bearer Scheme
Because end-customers do not hold internal credentials, certificates, or IAM tokens, this endpoint is intentionally excluded from standard middleware:
- **Excluded Prefixes**: Excluded from `BasicAuthMiddleware` and `HMACAuthMiddleware` (`/public-api/` prefix).
- **Transport**: Operates outside mTLS client certificate requirements.
- **Token Presentation**: The client presents a stateless token via query parameter `?token=<token>` or header `Authorization: Bearer <token>`.

### 6b. Token Generation & Verification Formula
The token is derived deterministically from the quote ID using HMAC-SHA256 keyed with the master secret `NCE_MASTER_KEY`:

$$\text{Token} = \text{HMAC-SHA256}(\text{NCE\_MASTER\_KEY}, \text{quote\_id}).\text{hexdigest}()$$

```python
def generate_public_token(quote_id: str) -> str:
    """Generate a secure stateless token for a quote_id using NCE_MASTER_KEY."""
    key = cfg.NCE_MASTER_KEY.encode("utf-8")
    msg = quote_id.encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()
```

### 6c. Security Properties & Master Key Rotation Consequence
* **Stateless Validation**: The token is verified on-the-fly via constant-time comparison (`hmac.compare_digest`). No database lookup is required to validate token authenticity.
* **No Expiration & No Revocation**: The token contains no embedded timestamp or expiry claim, and there is no token revocation list. A quote link remains valid indefinitely as long as the quote exists and `NCE_MASTER_KEY` remains unchanged.
* **Customer-Link-Signing Role of `NCE_MASTER_KEY`**: `NCE_MASTER_KEY` serves as the cryptographic signing key for all public customer quote links.
* **Key Rotation Consequence**: **Rotating `NCE_MASTER_KEY` immediately invalidates all outstanding public customer quote URLs.** Customers following previously shared quote links will receive `HTTP 401 Unauthorized` (`{"error": "Unauthorized: invalid token"}`). When rotating `NCE_MASTER_KEY`, customer quote links must be re-issued.

### 6d. C8 Redaction Projection & Invariant Protection
All quote payloads retrieved via this endpoint are filtered through the C8 Projection Redactor (`project(quote_detail, "public-quote")`). Furthermore, code-level assertions strip internal financial fields (`cost`, `margin`, `commission`, `internal-status`) before the response leaves the process boundary.

### 6e. Sliding Window Rate Limiting
To prevent token brute-forcing and resource exhaustion, requests are throttled to **5 requests per 10 seconds per token**:
- **Key**: `nce:ratelimit:public_quote:<token>` (tracked in Redis via atomic Lua sliding window).
- **Exceeded Response**: HTTP 429 (`{"error": "Rate limit exceeded"}`).

---

## 7. Rate Limiting & JSON-RPC Error Architecture

NCE applies layered rate limiting and standard JSON-RPC 2.0 / extended error codes across all entry points.

### 7a. Comprehensive Rate Limiting Matrix

| Surface | Rate Limit | Window | Key / Tracking | Exceeded Response |
| :--- | :--- | :--- | :--- | :--- |
| **Admin REST API (General)** | `NCE_ADMIN_HTTP_RATE_LIMIT` (default: 120 req) | `NCE_ADMIN_HTTP_RATE_PERIOD` (default: 60s) | Per client IP (`nce:ratelimit:admin:http:<ip>:general`) | HTTP 429 (`{"error": "Rate limit exceeded"}`) |
| **Admin REST API (Sensitive POST)** | `NCE_ADMIN_HTTP_SENSITIVE_RATE_LIMIT` (default: 30 req) | `NCE_ADMIN_HTTP_SENSITIVE_RATE_PERIOD` (default: 60s) | Per client IP on `/api/admin/`, `/api/gc/`, `/api/replay/`, `/api/snapshot/`, `/api/a2a/`, `/api/search` | HTTP 429 (`{"error": "Rate limit exceeded"}`) |
| **A2A Server (`/tasks/send`)** | `NCE_A2A_HTTP_RATE_LIMIT` (default: 60 req) | `NCE_A2A_HTTP_RATE_PERIOD` (default: 60s) | Per client IP (`nce:ratelimit:a2a:<ip>`) | HTTP 429 / JSON-RPC `-32029` |
| **Public Customer Quote API** | 5 requests | 10 seconds | Per quote token (`nce:ratelimit:public_quote:<token>`) | HTTP 429 (`{"error": "Rate limit exceeded"}`) |
| **Webhook Receivers** | `WEBHOOK_RATE_LIMIT` (default: 120 req) | `WEBHOOK_RATE_PERIOD_SECONDS` (default: 60s) | Per source IP (`nce:ratelimit:webhook:<ip>`) | HTTP 429 (`{"error": "Rate limit exceeded"}`) |
| **MCP Stdio Tools** | Configured per namespace / tool quotas | Sliding window | Quota tracker / Redis counters | JSON-RPC `-32029` (`MCP_RATE_LIMITED`) |

### 7b. JSON-RPC 2.0 & Extended Error Codes

All MCP tool invocations and JSON-RPC APIs return structured errors adhering to the JSON-RPC 2.0 specification and NCE extended error ranges (`nce/mcp_errors.py`):

| Code | Constant | Meaning / Trigger Condition |
| :--- | :--- | :--- |
| **`-32700`** | `MCP_PARSE_ERROR` | JSON parse failure on incoming request body. |
| **`-32600`** | `MCP_INVALID_REQUEST` | Structurally invalid JSON-RPC 2.0 envelope. |
| **`-32601`** | `MCP_METHOD_NOT_FOUND` | `UnknownToolError` — requested tool name is not registered in `TOOL_REGISTRY`. |
| **`-32602`** | `MCP_INVALID_PARAMS` | Pydantic `ValidationError`, `ValueError`, `TypeError`, or missing required parameters. |
| **`-32603`** | `MCP_INTERNAL_ERROR` | Unhandled internal exception in tool execution (sanitized in production). |
| **`-32001`** | `MCP_AUTH_FAILED` | Missing or invalid authentication credentials / headers. |
| **`-32002`** | `MCP_REPLAY_DETECTED` | Timestamp skew exceeded tolerance (>90s) or duplicate nonce encountered in `NonceStore`. |
| **`-32005`** | `MCP_SCOPE_FORBIDDEN` | `ScopeError` — insufficient privilege, missing admin key, or namespace mismatch. |
| **`-32006`** | — | `namespace_id` claim absent in JWT token. |
| **`-32007`** | — | `namespace_id` claim is not a valid UUID string. |
| **`-32010`** | `MCP_A2A_AUTH_FAILED` | A2A sharing token invalid, expired, revoked, or signature verification failed. |
| **`-32011`** | `MCP_A2A_SCOPE_VIOLATION` | Requested A2A action is outside the granted permissions in `a2a_grants.scopes`. |
| **`-32013`** | `MCP_QUOTA_EXCEEDED` | Namespace resource quota limit reached (`QuotaExceededError`). |
| **`-32015`** | `DEFAULT_MTLS_ERROR_CODE` | `MTLSAuthMiddleware` client certificate validation failure. |
| **`-32029`** | `MCP_RATE_LIMITED` | `RateLimitError` — request rate limit exceeded on MCP tool or A2A channel. |

---

## 8. PostgreSQL Row-Level Security (RLS) Policies

NCE implements tenant isolation directly at the database layer. This ensures that even if application logic fails to filter a query by tenant, PostgreSQL blocks access to unauthorized data.

### 8a. The Fail-Safe Namespace Resolver
Postgres resolves tenant identity using the session settings variable `nce.namespace_id`. This is wrapped by the stable PL/pgSQL function `get_nce_namespace()`:

```sql
CREATE OR REPLACE FUNCTION get_nce_namespace() RETURNS uuid AS $$
DECLARE
    val text;
BEGIN
    val := nullif(trim(current_setting('nce.namespace_id', true)), '');
    IF val IS NULL THEN
        RAISE EXCEPTION 'nce.namespace_id is not set for this transaction';
    END IF;
    BEGIN
        RETURN val::uuid;
    EXCEPTION
        WHEN invalid_text_representation THEN
            RAISE EXCEPTION 'nce.namespace_id is not a valid UUID: %', val;
    END;
END;
$$ LANGUAGE plpgsql STABLE;
```

### 8b. Default Table Policy Pattern
For all 57 tenant-scoped tables (`EXPECTED_TENANT_RLS_TABLES` in `nce/event_log.py`), RLS is enabled and enforced:

```sql
ALTER TABLE memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE memories FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_policy ON memories
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
```

* **RLS Enforcement Rule**: All SELECT, INSERT, UPDATE, and DELETE operations executed under the standard application role `nce_app` are restricted to the UUID returned by `get_nce_namespace()`.
* **Privileged Role Exception (`nce_gc`)**: The `nce_gc` role is defined in `schema.sql` with the database-level `BYPASSRLS` attribute as a least-privilege boundary for background maintenance workers. Workers select their DSN via `db_utils.resolve_worker_dsn()`: when `NCE_GC_DSN` is set they connect as `nce_gc` (its own credentials, distinct from `nce_app`); when it is unset they fall back to `PG_DSN` (the app role) for backward compatibility. The application role `nce_app` never holds `BYPASSRLS` in either case — that attribute belongs only to `nce_gc`. To enforce hard segregation in production, provision `nce_gc` with `LOGIN` and a dedicated password and set `NCE_GC_DSN` accordingly (`NCE_GC_DSN` is environment-only and never returned by any endpoint). The garbage collector additionally runs RLS-scoped per namespace (via `set_namespace_context`), so it does not depend on `BYPASSRLS` for correctness.

---

## 9. PII Redaction & AES-256-GCM Vault

To prevent Personal Data / PII leakage into vector databases and external LLM models, NCE executes a PII Redaction pipeline before writing data.

```
Incoming Text: "Contact Alice at alice@example.com"
       │
       ▼
[ Presidio Analyzer / Regex Engine ]
       │
       ├─► Redacts Email -> "Contact Alice at <EMAIL_1>"
       │
       └─► Extracts PII: Value="alice@example.com", Type="EMAIL"
             │
             ▼
       [ Encrypt with NCE_MASTER_KEY ]
       (AES-256-GCM, unique 12-byte IV)
             │
             ▼
       [ Write to pii_redactions ]
       Columns: namespace_id, memory_id, token, encrypted_value
```

### 9a. Cryptographic Vault Storage
* **Encryption standard**: PII entities are encrypted using AES-256-GCM.
* **Key Derivation**: The encryption key is derived from the environment variable `NCE_MASTER_KEY` (minimum 32 random bytes).
* **Storage Target**: The encrypted byte array, along with the replacement token (e.g. `<EMAIL_1>`), the entity type, and the referencing memory UUID are inserted into the `pii_redactions` table.

### 9b. Reversible Unredaction
Authorized administrative users can retrieve original values using the `unredact_memory` tool:
1. The requester must supply the `admin_api_key`.
2. The query is executed inside a `scoped_pg_session`, ensuring RLS limits lookup to the requester's namespace.
3. The cipher text is retrieved and decrypted using `NCE_MASTER_KEY` before returning the plain text to the authenticated supervisor.

---

## 10. Agent-to-Agent (A2A) Scope Enforcement

Cross-tenant data sharing is controlled through the `a2a_grants` table, which holds structured access rules.

### 10a. Structuring A2A Grants
An A2A grant specifies the owner namespace, target consumer namespace, validation timeframe, and resource scopes:

```json
{
  "grant_id": "87f0b21e-d124-4bca-89a3-fa349d3c8003",
  "owner_namespace_id": "673f8e91-654e-48bd-b7bb-ea392d4f8001",
  "consumer_namespace_id": "921a4f02-98ab-4cc1-94ef-67efab109f02",
  "scopes": [
    {
      "resource_type": "subgraph",
      "resource_id": "alice_network",
      "permissions": ["read"]
    },
    {
      "resource_type": "memory",
      "resource_id": "67f0b982-f12a-4cbd-b2bb-de882d9f8210",
      "permissions": ["read"]
    }
  ],
  "expires_at": "2026-07-07T00:00:00Z"
}
```

### 10b. Token Verification Mechanics
1. **Creation**: When a sharing grant is created, the system generates a random token and stores its SHA-256 hash in `token_hash`. The raw token is returned once to the caller.
2. **Access request**: When a consumer agent queries data via `/tasks/send` or `a2a_query_shared`, it supplies the raw token.
3. **Validation**: NCE hashes the token using SHA-256 and queries `a2a_grants` for a matching hash:
   * The status must be `'active'`.
   * The current time must be prior to `expires_at`.
   * The requested query parameters must match the permissions defined in the `scopes` JSONB array.
4. **Enforcement**: If valid, the target resources are retrieved under the owner's namespace using the owner's session context before returning them to the consumer agent.

---

## 11. Dev-Only Bypass Flags (Forbidden in Production)

The following environment variables exist exclusively for local development convenience. All are enforced at module-import time or boot lifespan: if `NCE_ENV=prod` is set and any of these flags is enabled, NCE raises a `RuntimeError` / `MTLSNotConfiguredError` and refuses to start.

| Variable | Effect | Production Status |
| :--- | :--- | :--- |
| `NCE_ADMIN_OVERRIDE` | Bypasses `require_scope("admin")` key validation entirely — any caller is treated as an authorized admin. | **Forbidden** (`RuntimeError` raised at startup) |
| `NCE_BYPASS_WORM` | Skips the WORM (write-once append-only) integrity probe at startup. | **Forbidden** |
| `NCE_BYPASS_RLS` | Skips the Row-Level Security isolation probe at startup. | **Forbidden** |
| `NCE_ALLOW_ADMIN_DOTENV_PERSIST` | Permits the admin UI to write connector/datastore secrets to a local `.env` file. | **Forbidden** (secrets must come from a secret manager) |
| `NCE_LOAD_DOTENV` | Loads a `.env` file at process start. Its default is `"true"`, so an **unset** value is also treated as enabled and triggers the production `RuntimeError`. | **Forbidden unless explicitly `false`** — under `NCE_ENV=prod`, both an explicit truthy value **and** leaving it unset raise `RuntimeError`. The safe action is to set it explicitly to `false`. |
| `NCE_MTLS_ACKNOWLEDGE_DISABLED` | Bypasses the mandatory production mTLS requirement. Default `false`. | **Permitted only with explicit operator acknowledgment**; when `true` in production, logs a `CRITICAL` alert and records a WORM security audit event. |

None of the explicit bypass flags above should appear in any production environment configuration, CI/CD pipeline, or container image. `NCE_LOAD_DOTENV` is the exception that must be present and set to `false` in production, because its default (`true`) would otherwise abort startup.

---

## 12. Cryptographic Keys & Secrets Security Checklist

This checklist defines the storage and rotation rules for system secrets:

| Secret Name | Purpose | Minimum Length | Storage Recommendation | Rotation Procedure |
| :--- | :--- | :--- | :--- | :--- |
| `NCE_MASTER_KEY` | AES-256-GCM master key for PII vault encryption, envelope DEK wrapping, and HMAC-SHA256 customer quote link signing (`generate_public_token`). Environment-only (never sourced from a database or SettingsStore — R3). | 32 UTF-8 bytes | Enterprise Key Management System (KMS) or vault. | Offline re-encryption script of `pii_redactions` and `bridge_subscriptions` tables. **Rotation consequence**: Invalidates all outstanding public customer quote URLs (since tokens are stateless HMAC signatures derived from `NCE_MASTER_KEY` with no expiry/revocation). |
| `NCE_MCP_API_KEY` | Authenticates incoming MCP stdio tenant tool calls. Required in production. | 64 characters | Client user configuration file (encrypted at rest by OS). | Generate new token, update environment configuration, and restart client. |
| `NCE_API_KEY` | HMAC-SHA256 shared secret for HTTP Admin API request authentication (`HMACAuthMiddleware`). Required in production. | 64 characters | Secrets management system (KMS). | Update environment variable on NCE and client, followed by rolling restart. |
| `NCE_ADMIN_API_KEY` | Bearer token for MCP `require_scope("admin")` checks (admin tool calls via stdio). Required in production. | 64 characters | Secrets management system (KMS). | Update environment variable on NCE and client, followed by rolling restart. |
| `NCE_ADMIN_PASSWORD` | HTTP Basic credential for the admin UI. Must be stored as a `$pbkdf2$` hash in production. | — | Secrets management system (KMS). | Re-hash with PBKDF2 (v4: ≥ 600,000 iterations), update environment variable, restart. |
| `NCE_JWT_SECRET` | HS256 shared secret for A2A JWT signing (dev / single-trust-boundary only). Prefer `NCE_JWT_PUBLIC_KEY` in production. | 32 bytes | Secrets management system (KMS). | Update environment configuration and restart NCE instances. |
| `NCE_JWT_PUBLIC_KEY` | RS256/ES256 PEM public key for A2A JWT verification. Takes precedence over `NCE_JWT_SECRET` when both are set. | 4096-bit RSA / P-256 EC | Stored as environment PEM string or `file:///path/to/pub.pem`. | Update public key file, trigger rolling deployment without downtime. |
