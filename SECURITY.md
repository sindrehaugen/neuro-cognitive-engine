# Security Policy & Cryptographic Architecture

The Neuro-Cognitive Engine (NCE) is built for mission-critical enterprise environments requiring rigorous data isolation, auditability, and tamper-proof cryptographic boundaries. This document outlines our supported versions, vulnerability reporting process, and cryptographic security architecture.

---

## Supported Versions

Security patches and updates are actively maintained for the following release branches:

| Version | Supported | Status |
| :--- | :--- | :--- |
| **3.4.x** | :white_check_mark: Yes | Current Stable Release (Cognitive Scalability, WORM Advisory Locks & Hybrid RRF) |
| **3.3.x** | :white_check_mark: Yes | Maintenance Release (Streamable HTTP `/mcp` Gateway & Envelope Crypto) |
| **3.0.x**–**3.2.x** | :white_check_mark: Yes | Long-Term Support (LTS) Baseline (Decoupled Core & Plugin Architecture) |
| **< 3.0.0** | :x: No | End of Life (Legacy TriMCP monolithic releases) |

---

## Vulnerability Disclosure Policy

We take system security seriously and appreciate coordinated vulnerability disclosure.

### Reporting a Vulnerability

If you discover a security vulnerability in NCE Core or official plugins:
1. **Do not create a public issue.** Public bug trackers are never used for undisclosed security flaws.
2. Email your findings directly to **security@sindrehaugen.no** (or submit via GitHub Private Vulnerability Reporting on `sindrehaugen/neuro-cognitive-engine`).
3. Include detailed reproduction steps, proof-of-concept scripts or payloads, affected components/endpoints, and an assessment of potential impact.

### Response Timelines & SLA

- **Initial Response:** Within **48 hours** of report receipt.
- **Triage & Severity Assessment:** Within **5 business days**, classifying according to CVSS v3.1.
- **Remediation & Patch Release:** Critical vulnerabilities are prioritized for targeted out-of-band hotfixes within **14 business days**.
- **Public Disclosure / Embargo:** We adhere to a standard **90-day coordinated disclosure timeline** from initial triage, allowing downstream enterprise deployments sufficient window to apply updates before technical details are published.

---

## Authentication & Cryptographic Architecture

NCE's security model enforces defense-in-depth across the entire data lifecycle and communication plane.

```
                     ┌──────────────────────────────────────────────┐
                     │            Inbound Communication             │
                     └──────────────────────┬───────────────────────┘
                                            │
               ┌────────────────────────────┴───────────────────────────┐
               │                                                        │
               ▼                                                        ▼
   ┌───────────────────────┐                                ┌───────────────────────┐
   │    Admin REST API     │                                │  Agent-to-Agent (A2A) │
   │   (admin_server.py)   │                                │   (a2a_server.py)     │
   ├───────────────────────┤                                ├───────────────────────┤
   │ - HMAC-SHA256 (3-Hdr) │                                │ - Atomic 1-time grant │
   │ - Replay Nonce Cache  │                                │ - Ed25519 / RS256 JWT │
   │ - Fail-closed prod    │                                │ - Scope confinement  │
   └───────────┬───────────┘                                └───────────┬───────────┘
               │                                                        │
               └────────────────────────────┬───────────────────────────┘
                                            │
                                            ▼
                       ┌────────────────────────────────────────┐
                       │     PostgreSQL 16 Multi-Tenant RLS     │
                       │     SET LOCAL app.current_namespace    │
                       └────────────────────┬───────────────────┘
                                            │
                                            ▼
                       ┌────────────────────────────────────────┐
                       │        WORM Append-Only Ledger         │
                       │        prevent_mutation trigger        │
                       │        SHA-256 Merkle-Chain Log        │
                       └────────────────────────────────────────┘
```

### 1. Administrative HMAC-SHA256 Three-Header Protocol

All administrative and operational REST endpoints (`/api/*`) require strict cryptographic request authentication via three mandatory HTTP headers:

- `X-NCE-Timestamp`: UNIX timestamp in UTC (seconds or milliseconds). The server enforces a strict sliding time window (`±300 seconds` by default) to reject stale or pre-dated requests.
- `X-NCE-Nonce`: A cryptographically random unique nonce string (minimum 16 bytes).
- `Authorization`: `HMAC-SHA256 <signature>`, computed as:
  $$\text{HMAC-SHA256}(\text{key} = NCE\_API\_KEY, \text{msg} = \text{timestamp} + ":" + \text{nonce} + ":" + \text{method} + ":" + \text{path} + ":" + \text{SHA256}(\text{body}))$$

### 2. Distributed Anti-Replay Nonce Store

To protect against replay attacks:
- The server validates inbound request nonces against a Redis-backed distributed cache.
- Nonces are stored with a Time-To-Live (TTL) matching the timestamp expiration window.
- Duplicate nonce presentation within the valid drift window results in immediate `HTTP 401 Unauthorized` rejection with audit log recording.

### 3. Fail-Closed Production Configuration Guards

Security controls in `nce/config.py` are engineered to fail closed:
- **Administrative Override Suppression:** `live_admin_override_enabled()` strictly enforces that whenever `NCE_ENV=production` or `cfg.IS_PROD` evaluates to true, administrative overrides (`NCE_ADMIN_OVERRIDE`) are unconditionally disabled (`False`). No environment flag can override production security constraints at runtime.
- **Secret Strength Validation:** Production startup enforces minimum key length constraints ($\ge 32$ UTF-8 bytes for `NCE_MASTER_KEY` and `NCE_API_KEY`).
- **Encrypted Storage:** Passwords require PBKDF2/Argon2 hashing; plain-text credentials in production environments raise immediate startup exceptions.

### 4. Atomic One-Time A2A Grant Tokens

Agent-to-Agent (A2A) cross-network federation operates under zero-trust authorization:
- Cross-agent memory sharing and remote tool dispatch require an ephemeral, cryptographically signed delegation token (`nce/a2a.py`).
- **Atomic Single-Use Consumption:** Tokens are redeemed via atomic database/Redis operations (`GETDEL` or SQL `UPDATE ... WHERE state = 'ACTIVE' RETURNING *`). This eliminates time-of-check to time-of-use (TOCTOU) race conditions and guarantees that tokens cannot be double-spent or concurrently replayed.
- **Scope & Principal Pinning:** Every grant token binds explicitly to issuing tenant, target tenant, allowed tool/skill namespaces, and an explicit time-to-live.

### 5. Multi-Tenant Row-Level Security (PostgreSQL RLS)

NCE isolates tenant data at the storage layer rather than relying on application-level filtering:
- Every relational table enforces PostgreSQL Row-Level Security (`ENABLE ROW LEVEL SECURITY; FORCE ROW LEVEL SECURITY;`).
- Session isolation is set per-query or per-transaction via `SET LOCAL app.current_namespace = '<namespace-uuid>'`.
- Any query attempting cross-tenant access without administrative bypass raises a database-level violation, ensuring complete tenant confinement even in the event of an application logic defect.

### 6. WORM Event Log Immutability & Advisory Locks

The central audit and event history ledger (`event_log`) guarantees Write-Once-Read-Many (WORM) auditability and strict transactional ordering:
- **Database Trigger:** The `prevent_mutation` trigger intercepts and aborts any SQL `UPDATE`, `DELETE`, or `TRUNCATE` operations on `event_log`.
- **Per-Namespace Advisory Locks:** Migration `036` introduces `acquire_event_log_worm_lock(p_namespace_id UUID)` executed via a `BEFORE INSERT` trigger (`trg_event_log_worm_advisory_lock`) using PostgreSQL `pg_advisory_xact_lock(hashtext('nce_worm_event_log'), hashtext(p_namespace_id::text))`. This serializes concurrent writes strictly per namespace, eliminating Merkle chain concurrency races while avoiding global bottlenecks.
- **Cryptographic Hash Chaining:** Every event record incorporates the SHA-256 hash of its predecessor, forming a tamper-evident Merkle chain from the namespace genesis.
- **Deterministic Replay:** The immutable event log enables deterministic point-in-time reconstruction (`as_of`) and counterfactual branching without mutating historical state.

### 7. Ingress Protection & BoundedBodyMiddleware

All HTTP entrypoints (Admin REST, Streamable HTTP `/mcp` Gateway, A2A federation, and Webhook Receiver) are protected by ASGI `BoundedBodyMiddleware`:
- **Hard Payload Ceiling:** Reads are strictly capped by `live_max_http_body_bytes()` (configured via `NCE_MAX_HTTP_BODY_BYTES`, default 10 MiB).
- **HTTP 413 Enforcement:** Payloads exceeding the configured ceiling are rejected immediately with `HTTP 413 Payload Too Large` before buffering or JSON parsing, preventing Denial of Service (DoS) and memory exhaustion attacks.

### 8. SSRF Blindspot Closures & Outbound Egress Guard

Outbound HTTP communications (OAuth document bridges, external notifications, and outbound webhooks) are validated by `validate_outbound_url`:
- **Private & Reserved Range Rejection:** Hard-blocks RFC 1918 private subnets, loopback (`127.0.0.0/8`, `::1`), link-local (`169.254.0.0/16`), and broadcast addresses.
- **CGNAT Blindspot Closure:** Explicitly blocks Carrier-Grade NAT (CGNAT) space (`100.64.0.0/10`) to prevent intranet and shared-infrastructure traversal.
- **IPv4-Mapped IPv6 Traversal Blocking:** Resolves and validates embedded IPv4 addresses in IPv6 notations (e.g. `::ffff:169.254.169.254`), closing cloud metadata exfiltration vectors (AWS/GCP/Azure IMDS).
- **Outbound Webhook Registration & Delivery Gate:** Target URLs for webhooks are verified against `NCE_SSRF_ALLOWLIST_HOSTS` at both subscription registration and outbound event dispatch.

### 9. Hierarchical KEK/DEK Envelope Encryption & Provable Crypto-Shredding

Data privacy and cryptographic isolation leverage hierarchical envelope encryption (`nce/crypto/envelope.py`):
- **Versioned KEK Ring:** Master secret (`NCE_MASTER_KEY`) derives versioned Key Encryption Keys (KEKs).
- **Per-Subject DEK Wrapping:** Relational records (`memories`, `encryption_deks`) store AES-256-GCM wrapped Data Encryption Keys (`wrapped_dek`).
- **GDPR Provable Forgetting:** Right-to-be-forgotten requests permanently nullify the wrapped DEK. Without the key, encrypted content and corresponding `search_tsv` tokens become mathematically unrecoverable, producing verifiable RFC 8785 canonical shredding receipts.

### 10. Automated Static Analysis & Quality CI Gates

Continuous Integration enforces strict automated security scanning:
- **Ruff `S` (flake8-bandit):** Strict AST-level detection of common security flaws, unsafe shell invocations, and cryptographic misconfigurations.
- **Ruff `ASYNC` & `DTZ`:** Static rules preventing event-loop stalls and timezone-naive datetime bugs.
- **Bandit Scans:** Automated high-severity Bandit scans in CI gate all PR merges with zero permitted security exceptions.

---

## Encryption Standards

| Scope | Cryptographic Primitive | Implementation |
| :--- | :--- | :--- |
| **Data in Transit** | TLS 1.3 / mTLS | Mandatory in production (`NCE_ADMIN_MTLS_ENABLED=true`) |
| **Ingress Protection** | `BoundedBodyMiddleware` | Capped at `NCE_MAX_HTTP_BODY_BYTES`, HTTP `413` |
| **API Signatures** | HMAC-SHA256 | Admin three-header protocol with distributed nonces |
| **Data at Rest (Vault)** | AES-256-GCM | Master-key wrapped secrets and credentials |
| **Envelope Encryption** | KEK / DEK AES-256-GCM | Per-record encrypted DEKs with provable GDPR crypto-shredding |
| **A2A Delegation** | RS256 / Ed25519 | Ephemeral asymmetric JWT grants, atomic single-use redemption |
| **Integrity Ledger** | SHA-256 Merkle Chain | Hash-chained WORM audit log with per-namespace advisory locks |

For further architectural details, refer to `docs/enterprise_security.md` and `docs/signing.md`.
