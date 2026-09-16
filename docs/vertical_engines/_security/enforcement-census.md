> **Document:** Wave D-ENF Enforcement Claims Census & Control Execution Reality  
> **Target:** Documentation vs. Deployment & CI Enforcement Reality  
> **Author:** Antigravity / DL Orchestrator (`DL-Orch`)  
> **Charter Reference:** `C:\Claude\DL_ORCH_CHARTER_2026-09-06.md` §13 / Wave D-ENF  
> **Status:** COMPLETE · **Date:** 2026-09-16  
> **Verified-against:** `f298d1d` (Public `main`)  

---

# Enforcement Claims Census & Control Execution Reality (Wave D-ENF)

## Executive Summary

Across the Neuro-Cognitive Engine (NCE) architecture, documentation frequently asserts that specific security boundaries, tenant isolation guarantees, and cryptographic invariants are **"enforced"**, **"guaranteed"**, or **"prevented"** at the system and database layers.

Following the findings of Wave A-T6 (PR #148) and Estate Row **ML-CI1** (`KNOWN_UNWIRED` holding 110 test files that run in zero CI workflows), this census audits every primary enforcement claim across the documentation estate against two distinct empirical standards:
1. **Deployment Reality:** Does the mechanism actively execute and constrain the live running process, or is it bypassed or inert under the deployed configuration (e.g. connecting as superuser `mcp_user`)?
2. **CI Execution Reality (ML-CI1):** Does the test suite verifying this control actually execute in continuous integration, or is it parked in `KNOWN_UNWIRED` (`tests/test_ci_integration_coverage.py`), passing only in local environments without gating PRs?

> [!CRITICAL]
> **Key Finding:** Controls fall into three clear categories across NCE:
> 1. **Active & Gated in CI:** Application-level `WHERE`-clause filtering, HMAC request signing, sliding-window rate limiting, and core schema bootstrap contracts.
> 2. **Active in Code / Runtime, but UNTESTED in CI (`KNOWN_UNWIRED`, ML-CI1):** Merkle provenance hash chain verification, MinIO object-lock tamper anchoring, C2 `@governed` mutation decorator, envelope encryption DEK handling, GDPR memory shredding, and C3 external scope adversarial protections.
> 3. **INERT as Deployed:** PostgreSQL Row-Level Security (RLS) policies and database table privilege revocations (`REVOKE UPDATE, DELETE FROM nce_app`), because runtime connections execute under `mcp_user` (`rolsuper=true`, `rolbypassrls=true`).

---

## 1. Master Enforcement Census

| # | Control Domain | Claimed In | Named Mechanism | Deployment Status | CI Test Status |
|---|---|---|---|---|---|
| **1** | **Tenant Database Isolation** | `docs/enterprise_security.md:319`<br>`docs/database_architecture.md:409`<br>`docs/multi_tenancy.md:54` | PostgreSQL RLS policies (`tenant_isolation_policy` across 87 tables in `EXPECTED_TENANT_RLS_TABLES`) | 🔴 **INERT as deployed.** Runtime connects as `mcp_user` (`rolsuper=true`, `rolbypassrls=true`). Zero policies target `mcp_user`. Isolation rests solely on application `WHERE namespace_id = $1` predicates. | 🟡 `tests/test_rls_isolation_integration.py` runs in CI, but tests against mock/isolated roles; runtime superuser bypass is not evaluated in CI. |
| **2** | **Database-Level WORM Privileges** | `docs/database_architecture.md:243-247`<br>`docs/engines/sales-user.md:125` | `REVOKE UPDATE, DELETE ON TABLE ... FROM nce_app` | 🔴 **INERT against runtime connections.** `mcp_user` is a superuser and bypasses table-level privilege grants. Table-level execution triggers (`prevent_mutation()`) provide active immutability defense. | 🔴 **UNTESTED in CI.** `tests/test_worm_db_enforcement.py` is in `KNOWN_UNWIRED` (ML-CI1). In `tests/test_worm_registry.py`, only unit tests run; marked integration tests are deselected. |
| **3** | **C3 External Scope / Adversarial Isolation** | `docs/vertical_engines/_security/c3-external-scope-threat-model.md:70-83`<br>`docs/shared-core/external-scope-rls.md:35-92` | Scope GUC `nce.external_scope_id` + nil-UUID sentinel + `external_isolation_policy` | 🟢 **Application level active** (JWT extraction, parameter validation, explicit `WHERE` predicates).<br>🔴 **Database RLS inert** under `mcp_user`. | 🔴 **UNTESTED in CI.** `tests/test_c3_adversarial.py` (9 adversarial tests) and `tests/test_external_scope_rls.py` are both parked in `KNOWN_UNWIRED` (ML-CI1). |
| **4** | **Sales Signed Baseline WORM Freeze** | `docs/engines/sales-user.md:125`<br>`docs/engines/sales-admin.md:167` | DDL privilege revoke + UNIQUE natural key + `do_freeze_baseline` checks | 🟢 **Application level active** (natural key constraint & code checks).<br>🔴 **Role revoke inert** under `mcp_user`. | 🔴 **UNTESTED in CI.** `tests/test_sales_signed_baseline.py` and `tests/test_sales_sign_to_project.py` are both in `KNOWN_UNWIRED` (ML-CI1). |
| **5** | **Cryptographic Merkle Hash Chain Verification** | `docs/architecture-v1.md:268`<br>`docs/database_architecture.md:248` | Cron job `_chain_verification_tick` in `nce/cron.py` validating `chain_hash` Merkle sequence | 🟢 **Active at runtime.** Runs every 120 minutes via APScheduler when cron daemon runs; updates `MERKLE_CHAIN_VALID` gauge. | 🔴 **UNTESTED in CI.** `tests/test_cron_chain_verify.py` is parked in `KNOWN_UNWIRED` (ML-CI1, RL-H1b). |
| **6** | **Tamper-Evident MinIO Object-Lock Anchoring** | `docs/architecture-v1.md:269`<br>`docs/enterprise_security.md:23` | Cron job `_tamper_anchor_tick` exporting Merkle chain heads to Object-Locked MinIO buckets | 🟢 **Active at runtime.** Configured hourly cron job targeting MinIO with WORM retention headers. | 🔴 **UNTESTED in CI.** `tests/test_tamper_anchor.py` is parked in `KNOWN_UNWIRED` (ML-CI1). |
| **7** | **C2 Autonomy Governance Decorator** | `docs/shared-core/autonomy-governance.md:9-20`<br>`docs/shared-core/overview.md:45,183` | `@governed` python decorator in `nce/autonomy/governor.py` on mutating MCP tools | 🟢 **Active in code.** Wraps handlers, validates confirmation flags, evaluates kill switches, logs to `event_log`. | 🔴 **UNTESTED in CI.** `tests/test_governed_decorator.py` (idempotency, confirmation, transaction rollback) is parked in `KNOWN_UNWIRED` (ML-CI1). |
| **8** | **Cryptographic Envelope Encryption** | `docs/enterprise_security.md:350-370`<br>`docs/architecture-v1.md:28` | AES-256-GCM per-memory DEK wrapped by master key (`nce/crypto.py`) | 🟢 **Active in code.** Encrypts episodic and semantic payloads before database write. | 🔴 **UNTESTED in CI.** `tests/test_envelope_encryption_integration.py` and `tests/test_envelope_read_consumers.py` are in `KNOWN_UNWIRED` (ML-CI1). |
| **9** | **Cryptographic Memory Shredding (GDPR)** | `docs/enterprise_security.md:368`<br>`docs/architecture-v1.md:28` | `shred_memory()` in `nce/shredder.py` destroying wrapped DEK in PostgreSQL | 🟢 **Active in code.** Cryptographically renders encrypted payloads unrecoverable upon deletion. | 🔴 **UNTESTED in CI.** `tests/test_shred_memory_integration.py` is parked in `KNOWN_UNWIRED` (ML-CI1). |
| **10** | **PII Redaction & WORM Side-Sink Leak Prevention** | `docs/shared-core/redaction.md:20-45`<br>`docs/enterprise_security.md:337-365` | Presidio Analyzer & Regex pipeline in `nce/pii.py` redacting text before embeddings & WORM writes | 🟢 **Active in code.** Redaction pipeline executes on ingest before vector publication. | 🟡 **Partially tested.** Unit tests run in CI, but WORM sidesink leak integration tests (`tests/test_batch44_worm_pii_sidesinks.py`, `tests/test_batch49_pii_derivation.py`) are in `KNOWN_UNWIRED` (ML-CI1). |
| **11** | **Event Retention & Cold Partition Pruning** | `docs/architecture-v1.md:270` | Daily cron job `_retention_tick` in `nce/cron.py` archiving and dropping aged monthly partitions | 🟢 **Active at runtime.** Executes retention schedule under cron daemon. | 🔴 **UNTESTED in CI.** `tests/test_event_retention.py` is parked in `KNOWN_UNWIRED` (ML-CI1). |
| **12** | **Engine Node Ownership Boundary Guards** | `docs/shared-core/overview.md:183`<br>`docs/vertical_engines/99-shared-core-foundation.md` | Core DB write-paths & `node_ownership_registry` preventing cross-engine graph mutations | 🟢 **Active in code.** Graph write paths inspect registry before modifying foreign nodes. | 🟡 **Partially tested.** `tests/test_node_ownership_seed_bulk.py` runs in CI integration smoke; however `tests/test_ownership_guard.py` and `tests/test_node_ownership_registry.py` are in `KNOWN_UNWIRED` (ML-CI1). |

---

## 2. Detailed Mechanism Audits

### 2.1 Database Row-Level Security (RLS) vs. Superuser Execution
- **The Claim:** Documentation historically claimed that PostgreSQL RLS provides an unbreakable database-level backstop preventing cross-tenant leakage even if the application layer fails or is bypassed by an attacker.
- **The Mechanism:** DDL migrations specify `ENABLE ROW LEVEL SECURITY` and `CREATE POLICY ... FOR ALL TO nce_app USING (namespace_id = get_nce_namespace())`.
- **Deployment Reality:** In the live production stack, `scoped_pg_session` connects using the credentials defined in `PG_DSN`, which resolve to `mcp_user`. As measured in Wave A-T6:
  ```sql
  SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'mcp_user';
  -- Result: {"rolname": "mcp_user", "rolsuper": true, "rolbypassrls": true}
  ```
  `scoped_pg_session` never issues `SET ROLE nce_app`. PostgreSQL completely disables RLS evaluation for superusers. Consequently, **RLS does not evaluate on production transactions.** Tenant separation is maintained solely by Python code enforcing `WHERE namespace_id = $1` in application SQL queries.
- **Documentation Action:** Every citation asserting database-layer fallback has been corrected to explicitly identify `mcp_user` superuser execution and application `WHERE`-clause reliance.

### 2.2 WORM Immutability: Triggers vs. Table Grants
- **The Claim:** `docs/database_architecture.md` and `docs/engines/sales-user.md` state that `REVOKE UPDATE, DELETE ON TABLE ... FROM nce_app` provides database-level immutability for append-only logs and signed baselines.
- **Deployment Reality:** Because the runtime connects as superuser `mcp_user`, PostgreSQL table grants on `nce_app` are completely bypassed. An accidental or malicious `UPDATE` or `DELETE` issued over `mcp_user` is **not blocked by role privileges**.
- **Active Defense:** Immutability for core WORM tables (`event_log`, `event_parents`) is enforced by table execution triggers (`prevent_mutation()`), which execute and raise exceptions regardless of caller privileges (unless explicitly disabled or configured in replica mode). However, for tables like `sales_signed_baselines` where immutability relied on role grants rather than a trigger, protection exists only via the natural key `UNIQUE (namespace_id, quote_id)` constraint and application checks.
- **Testing Reality:** The test verifying WORM role revocation (`tests/test_worm_db_enforcement.py`) connects by rewriting the DSN to `nce_app` and is parked in `KNOWN_UNWIRED` (ML-CI1).

### 2.3 C3 External Scope & Adversarial Attack Vectors
- **The Claim:** `docs/vertical_engines/_security/c3-external-scope-threat-model.md` details mitigations against 7 STRIDE vectors (deny-when-unset, IDOR, forged headers, scope enumeration, cross-tenant leaks, connection pooling leaks, prompt injection), citing `tests/test_c3_adversarial.py`.
- **Execution Reality:**
  - Transport boundary and application mediation are active in code: `_external_scope_from_context` parses JWTs, normalizes employee roles, and rejects nil UUIDs.
  - However, the 9 live integration tests in `tests/test_c3_adversarial.py` (which verify that connection pool reuse does not leak scopes across requests and that foreign scopes cannot be enumerated) reside in `KNOWN_UNWIRED` (`tests/test_ci_integration_coverage.py:78`).
  - They run in **zero CI workflows**. Wiring these tests into active CI is queued under estate decision **Q-12** in `C:\Claude\QUESTIONS_CHARTER.md`.

### 2.4 C2 `@governed` Mutation Decorator
- **The Claim:** All mutating tools execute through the `@governed` wrapper, guaranteeing idempotency key deduplication, confirmation requirements, and event logging.
- **Execution Reality:** The `@governed` decorator is wired to MCP mutation tools in code. However, its dedicated DB-dependent test suite `tests/test_governed_decorator.py` (covering transaction aborts, concurrent duplicate idempotency keys, and confirmation bypass rejection) is parked in `KNOWN_UNWIRED` (ML-CI1).

---

## 3. The ML-CI1 Blind Spot & Q-12 Gate

In `tests/test_ci_integration_coverage.py`, `KNOWN_UNWIRED` holds **110 integration test files**. While these tests pass when executed locally against a fully provisioned developer database, they do not execute in `.github/workflows/ci.yml`.

The 18 primary security-shaped unwired test suites are:
1. `tests/test_c3_adversarial.py` (Customer & partner scope adversarial probes)
2. `tests/test_governed_decorator.py` (Autonomy mutation governance & idempotency)
3. `tests/test_external_scope_rls.py` (External principal scope RLS)
4. `tests/test_worm_db_enforcement.py` (WORM table role privilege revocation)
5. `tests/test_vendors_contractor_rls.py` (Vendor portal contractor RLS)
6. `tests/test_diag_schema_rls.py` (Diagnostics schema RLS)
7. `tests/test_cron_chain_verify.py` (Merkle provenance hash chain cron verification)
8. `tests/test_tamper_anchor.py` (MinIO object-lock tamper anchoring)
9. `tests/test_envelope_encryption_integration.py` (Envelope encryption with per-memory DEK)
10. `tests/test_envelope_read_consumers.py` (Consumer read-path envelope decryption)
11. `tests/test_shred_memory_integration.py` (GDPR cryptographic memory shredding)
12. `tests/test_batch44_worm_pii_sidesinks.py` (PII side-sink leak prevention into WORM logs)
13. `tests/test_batch49_pii_derivation.py` (PII derivation & tokenization)
14. `tests/test_event_retention.py` (GDPR event log partition drop & archive)
15. `tests/test_ownership_guard.py` (Knowledge graph cross-engine ownership guards)
16. `tests/test_node_ownership_registry.py` (Engine node ownership registry)
17. `tests/test_sales_signed_baseline.py` (Sales signed baseline WORM privileges)
18. `tests/test_sales_sign_to_project.py` (Sales-to-project signed transition)

### Relationship to Q-12
In `C:\Claude\QUESTIONS_CHARTER.md`, item **Q-12** is an `OPEN` decision for Sindre:
> **Question Q-12:** ML-CI1 — 110 test files run in zero CI workflows.
> **ML-orch recommendation:** Wire the 13–18 security-shaped ones first, after the master-key swap, rather than all 110 at once.

Per charter mandate, DL does not edit `.github/workflows/ci.yml` or alter CI wiring. This wave fulfills DL's responsibility by documenting the exact reality across all relevant documentation pages, preventing operators and integrators from mistaking local passing tests for continuously enforced CI gates.
