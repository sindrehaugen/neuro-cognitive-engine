# Security Policy & Cryptographic Architecture

**Project Scope Notice:** The Neuro-Cognitive Engine (`neuro-cognitive-engine`) is an **ongoing personal Research & Development (R&D) project started in early May 2026** to implement and test multi-tenant database isolation, envelope cryptography, and governed MCP workflows. While security controls are engineered fail-closed and verified via automated CI suites, this repository is maintained by an individual researcher—not a commercial vendor—and has not undergone independent third-party cryptographic or penetration-test certification.

---

## Supported Branches (R&D Maintenance)

Security fixes target the active `main` branch (`3.6.x`):

| Version Line | Maintained | Notes |
| :--- | :--- | :--- |
| **`3.6.x` (`main`)** | Yes | Current R&D baseline: Core Connector SDK (`nce.connectors`), `app_connections` vault (`041`), pure-ASGI middleware, baked offline ONNX embeddings, partition forced RLS (`040`), `nce_app` least-privilege grants (`039`). |
| **`< 3.6.0`** | Best-effort / Superseded | Earlier May–November 2026 R&D iterations; upgrade to `3.6.x` on `main`. |

---

## Reporting a Security Issue

If you identify a vulnerability in `neuro-cognitive-engine` or its companion R&D plugin repositories (`nce-app-engines`, `nce-vertical-engines`):

1. **Avoid opening a public GitHub issue** for exploitable vulnerabilities before a fix is available.
2. Report via **GitHub Private Vulnerability Reporting** on `sindrehaugen/neuro-cognitive-engine` or email **security@sindrehaugen.no**.
3. Include reproduction steps, affected files/endpoints, and impact details. Reports are triaged on a best-effort basis (no commercial SLA).

---

## Implemented Security Controls & Trust Boundaries

```text
                     ┌──────────────────────────────────────────────┐
                     │             Inbound HTTP / MCP               │
                     └──────────────────────┬───────────────────────┘
                                            │
        ┌───────────────────────────────────┼───────────────────────────────────┐
        ▼                                   ▼                                   ▼
┌───────────────────────┐       ┌───────────────────────┐       ┌───────────────────────┐
│   Streamable /mcp     │       │    Admin REST API     │       │  A2A Federation API   │
│ (mcp_gateway_server)  │       │   (admin_server.py)   │       │   (a2a_server.py)     │
├───────────────────────┤       ├───────────────────────┤       ├───────────────────────┤
│ - Scoped Bearer Token │       │ - 3-Header HMAC-SHA256│       │ - Pinned RS256/ES256  │
│ - Fail-closed Redis   │       │ - Redis Nonce Replay  │       │ - SHA-256 Grant Hash  │
│ - HTTP 413 Body Cap   │       │ - Optional Edge mTLS  │       │ - Owner-RLS Execution │
└───────────┬───────────┘       └───────────┬───────────┘       └───────────┬───────────┘
            └───────────────────────────────┼───────────────────────────────────┘
                                            ▼
                       ┌────────────────────────────────────────┐
                       │     PostgreSQL 16 Forced Tenant RLS    │
                       │   set_config('nce.namespace_id', ...)  │
                       │     (SELECT get_nce_namespace())       │
                       └────────────────────┬───────────────────┘
                                            ▼
                       ┌────────────────────────────────────────┐
                       │      Append-Only WORM event_log        │
                       │   prevent_mutation + advisory xact lock│
                       │   RFC 8785 JCS HMAC-SHA256 Merkle Chain│
                       └────────────────────────────────────────┘
```

### 1. Three-Header Admin `HMAC-SHA256` & Replay Nonces

Administrative REST endpoints (`/api/*` on `admin_server.py`) verify three headers (`nce/auth.py`):

- `X-NCE-Timestamp`: UTC Unix epoch seconds checked against `NCE_CLOCK_SKEW_TOLERANCE_S` (default `90` seconds).
- `X-NCE-Nonce`: Unique per-request nonce atomically claimed in Redis (`SET nce:nonce:<nonce> 1 NX PX <2*skew_ms>`).
- `Authorization`: `HMAC-SHA256 <hex_digest>` computed with `NCE_API_KEY` over newline-joined `METHOD\nPATH\nTIMESTAMP` (appending `\nSHA256_HEX(body)` when the request body is non-empty), compared via `hmac.compare_digest`.

When `NCE_ENV=production` and `NCE_HMAC_NONCE_REQUIRED=true` (default), missing or unreachable Redis nonce state fails **closed** (`-32002` / HTTP `401`).

### 2. Fail-Closed Startup & Override Guards

- **`NCE_MASTER_KEY` Enforcement:** Importing or booting NCE without `>= 32` UTF-8 bytes of `NCE_MASTER_KEY` raises `RuntimeError` immediately; weak repeating patterns are rejected in production.
- **No Production Override:** `NCE_ADMIN_OVERRIDE=true` is hard-blocked when `NCE_ENV=production` (`live_admin_override_enabled()` returns `False` and startup raises `RuntimeError`).
- **Password Hashing:** `NCE_ADMIN_PASSWORD` requires `$pbkdf2$...` or Argon2id digests in production.

### 3. Forced PostgreSQL Row-Level Security (`scoped_pg_session`)

- All **39** tenant-scoped Core tables (**38** single-namespace tables in `EXPECTED_TENANT_RLS_TABLES` plus `a2a_grants` in `EXPECTED_SPECIAL_RLS_TABLES`, out of `42` Core SQL tables; dynamically joined by `110` forced-RLS tables in `nce-vertical-engines` and `58` forced-RLS tables in `nce-app-engines` when installed) enforce `ENABLE ROW LEVEL SECURITY` and `FORCE ROW LEVEL SECURITY` across parent tables and child partitions (`040_partition_rls_and_key_confinement.sql`).
- `scoped_pg_session(pool, namespace_id)` binds `set_config('nce.namespace_id', $1, true)` inside the transaction, evaluated via the `STABLE PARALLEL SAFE` `(SELECT get_nce_namespace())` `InitPlan` pattern (`038_v351_hotpath_rls_and_indexes.sql`).
- **Operator Role Requirement:** Database-level RLS blocks cross-tenant queries only when the runtime `PG_DSN` connects as the non-superuser `nce_app` role (`039_nce_app_runtime_role_grants.sql`) rather than a PostgreSQL `rolsuper` / `BYPASSRLS` account. `verify_rls_catalog_consistency()` validates catalog flags on boot.

### 4. Envelope Encryption (`TCDEK\x01`) & Cryptographic Erasure

- Per-record `AES-256-GCM` Data Encryption Keys (`encryption_deks` / `wrapped_dek`) are wrapped under versioned Key Encryption Keys derived from `NCE_MASTER_KEY` (Argon2id `time_cost=3, memory_cost=64 MiB` or PBKDF2-HMAC-SHA256 600k iterations).
- Unwrapped DEKs live in a bounded 300s LRU (`maxsize=512`) backed by mutable `bytearray` buffers zeroed in-place upon eviction or `forget_memory` / `shred_memory` / `crypto_shred_subject`.
- Tenant connector credentials in `app_connections` (`041_app_connections_vault.sql`) are encrypted via `TenantConnectionVault` and never returned in plaintext by `/admin/plugins/api/connections`.

### 5. Append-Only WORM `event_log` & Canonical Signing

- `prevent_mutation()` triggers block `UPDATE`, `DELETE`, and `TRUNCATE` on `event_log` and `event_parents`.
- Per-namespace `pg_advisory_xact_lock` (`acquire_event_log_worm_lock`, migration `036`) serializes concurrent event appends per tenant so `chain_hash` SHA-256 Merkle links cannot race.
- Payloads are canonically serialized per RFC 8785 (JCS) and signed with rotatable `HMAC-SHA256` keys (`signing_keys`, confined behind `SECURITY DEFINER` helpers in migration `040`).

### 6. Pure-ASGI Body Limits, SSRF Defense & Replay Fencing

- **HTTP `413` Ingress Cap:** Pure-ASGI `BoundedBodyMiddleware` (`NCE_MAX_HTTP_BODY_BYTES`, default `10 MiB`) aborts oversized request streams before buffering.
- **SSRF Egress Validation:** `validate_outbound_url` rejects RFC 1918 subnets, loopback, link-local (`169.254.0.0/16`), CGNAT (`100.64.0.0/10`), and IPv4-mapped IPv6 (`::ffff:169.254.169.254`) unless explicitly listed in `NCE_SSRF_ALLOWLIST_HOSTS`.
- **Replay Sandbox Fencing:** Forked/reconstructed replay runs mark target sessions `is_replay_sandbox` to block outbound webhooks and external connector mutations.

See [docs/enterprise_security.md](docs/enterprise_security.md), [docs/multi_tenancy.md](docs/multi_tenancy.md), [docs/signing.md](docs/signing.md), and [docs/pii.md](docs/pii.md) for full implementation details.
