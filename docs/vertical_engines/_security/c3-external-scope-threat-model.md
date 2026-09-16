> **Status:** audited · **Verified-against:** `628b231` (main) · **Last-audited:** 2026-09-16

# C3 External-Scope Threat Model (STRIDE, adversarial-external)

**Wave:** Batch 024 — Module 0.Wave 24 — `c3-security-review`
**Status:** the long-pole security gate that MUST pass before any external surface
(Vendors / Field-Tech / Customer-Portal) ships.
**Scope of this document:** the C3 external-principal isolation primitive only —
- **Wave 22** (`nce/migrations/028_c3_external_scope_rls.sql`): the
  `get_nce_external_scope()` SQL accessor + the `external_isolation_policy`
  template (nil-UUID deny-sentinel; ANDs the tenant predicate).
- **Wave 23** (`nce/jwt_auth.py`, `nce/auth.py`, `nce/db_utils.py`,
  `nce/a2a_server.py`): the external scope is sourced **only** from the verified
  JWT claim and set transaction-locally via `set_external_scope()`.

This is a **review/test-and-document** pass. It does not alter the primitive. If a
real residual hole were found, the fix would belong in W22/W23 — not here.

---

## 1. Trust boundary

```
   hostile external principal (contractor / external-customer browser, mobile, partner API)
        │  presents Authorization: Bearer <JWT>   (+ may forge ANY request header)
        ▼
   ── TRUST BOUNDARY ──────────────────────────────────────────────────────────
        │  JWTAuthMiddleware.decode_agent_token()  — verifies signature, exp, iss, aud
        │      → builds frozen NamespaceContext from the VERIFIED payload ONLY
        │      → principal_kind + external_scope_id come from JWT claims, never headers
        ▼
   a2a_server._external_scope_from_context(ctx)   — returns scope str only for
        │   contractor / external-customer with a non-nil verified scope; else None
        ▼
   db_utils.set_external_scope(conn, scope)        — SELECT set_config(..., true)  [SET LOCAL]
        │   called inside an open transaction; cleared at commit/rollback
        ▼
   PostgreSQL RLS:  external_isolation_policy
        USING ( namespace_id = get_nce_namespace()
                AND external_scope_id = get_nce_external_scope() )
        │   get_nce_external_scope() → nil-UUID sentinel when GUC unset/empty/malformed
        ▼
   Runtime connection: connects as mcp_user (rolsuper = true, rolbypassrls = true)
   🔴 RLS IS INERT AT RUNTIME — isolation enforced solely by WHERE-clause predicates
```

**Attacker model & runtime reality.** A *hostile authenticated external principal*: they hold a valid
JWT for their own namespace + scope and will try to reach another scope's or another
tenant's rows. They fully control all request headers and the request body.

🔴 **Deployment Reality (measured 2026-09-16, Wave A-T6 / PR #148):** The application connects
to PostgreSQL as `mcp_user` (`rolsuper = true`, `rolbypassrls = true`), not `nce_app`. Zero
RLS policies target `mcp_user`, and `scoped_pg_session` never issues `SET ROLE`. Consequently,
**PostgreSQL RLS is completely INERT as deployed**. The SQL `WHERE`-clause predicate (e.g.
`AND partner_scope_id = $2::uuid`) is the **sole active authorization and tenant/scope isolation control**.
Any architectural assumption that "Postgres RLS will catch it even if application code fails" is false
on the running estate (see [`docs/vertical_engines/_security/c3-external-scope-adversarial-review.md`](c3-external-scope-adversarial-review.md)).

**Key structural invariant.** `_external_scope_from_context()` takes a single
parameter — the verified `NamespaceContext`. There is **no parameter** for a raw
header value. The forged-header attack from the original Wave 23 design (which read
`X-NCE-Principal-Kind` / `X-NCE-External-Scope-Id` directly) is closed by construction:
those headers are no longer read anywhere on the scope path.

---

## 2. STRIDE vectors → mitigations → proving test

Each row names one adversarial-external vector, the concrete mitigation in the
W22/W23 design, and the test in `tests/test_c3_adversarial.py` that demonstrates the
mitigation is real against a live DB (or, for the header-IDOR structural proof, against
the live code path).

| # | STRIDE | Vector | Concrete mitigation (W22/W23) | Proving test |
|---|--------|--------|-------------------------------|--------------|
| V1 | Information Disclosure | **Deny-when-unset** — an external-policy table is queried with the scope GUC never set (e.g. employee session, or a bug that forgets to wire the scope). | `get_nce_external_scope()` returns the **nil-UUID sentinel** (`00000000-…-0000`) when `nce.external_scope_id` is unset/empty/malformed. No real `external_scope_id` is ever the nil UUID (`gen_random_uuid()` never emits it; the migration forbids storing it), so `external_scope_id = get_nce_external_scope()` is **always FALSE** → zero rows. Fail-closed: the accessor never raises. | `test_deny_when_scope_guc_unset_exposes_zero_rows` |
| V2 | Elevation of Privilege / Tampering | **IDOR via the scope GUC** — attacker authenticated for scope A sets / forges the GUC to scope B to read B's rows. | The GUC value the policy reads is the row's own `external_scope_id` matched against `get_nce_external_scope()`. An `nce_app` session can only ever *see* rows whose `external_scope_id` equals the GUC it set; it cannot read a different scope by naming it, because the GUC IS the filter. Setting the GUC to B shows only B's rows **iff** the principal legitimately holds B — and the GUC is sourced from the verified JWT, not chosen freely (see V3). Cross-scope read of A's rows while scoped to B returns zero. | `test_idor_cannot_read_another_scope_by_setting_guc` |
| V3 | Spoofing / Tampering | **Forged `X-NCE-External-Scope-Id` header** — attacker with a valid JWT for scope A injects a header naming scope B, hoping it sets the DB GUC. (This was the *original* Wave 23 bug.) | The scope is read **exclusively** from the verified `NamespaceContext` (built by `JWTAuthMiddleware` / `decode_agent_token` from the signed JWT payload). `_external_scope_from_context(ctx)` has **no header parameter** — the forged header has no entry point to `set_external_scope`. Structural, not heuristic. Also: an unknown/`super-admin` `principal_kind` claim normalises to `employee` (no scope); a `contractor` JWT lacking a scope claim returns `None` (deny, not default-open); the nil-UUID scope claim is rejected. | `test_forged_external_scope_header_does_not_influence_scope`, `test_unknown_principal_kind_demotes_to_employee_no_scope`, `test_contractor_jwt_without_scope_claim_denies` |
| V4 | Information Disclosure | **Scope enumeration** — attacker iterates candidate scope UUIDs (their own + guessed others) hoping a foreign scope leaks rows. | Same RLS predicate as V2: for every scope value the attacker sets, the policy returns only rows whose `external_scope_id` equals that exact GUC. Iterating foreign UUIDs yields **only** rows the principal actually owns; foreign UUIDs (including the nil UUID and random guesses) yield zero. UUIDv4 space is unguessable; even a correct guess is gated by the JWT-sourced GUC in production. | `test_scope_enumeration_yields_only_own_rows` |
| V5 | Information Disclosure (cross-tenant) | **Tenant crossing via scope** — attacker holds scope X which happens to match a row in *another* namespace; tries to read it by matching only the scope. | `external_isolation_policy` **ANDs** the tenant predicate: `namespace_id = get_nce_namespace()` **AND** `external_scope_id = get_nce_external_scope()`. Neither alone suffices. A row with the right scope but wrong namespace is invisible. | `test_external_scope_ands_namespace_no_cross_tenant` |
| V6 | Spoofing / Information Disclosure | **Session fixation / cross-request leak** — a pooled connection retains a prior request's scope GUC, so request N+1 (a different principal) inherits request N's scope. | `set_external_scope()` uses `set_config(..., true)` (= `SET LOCAL`), scoped to the **current transaction** and auto-cleared at commit/rollback. `scoped_pg_session` / the A2A path open a transaction per unit of work. A subsequent transaction on the same physical connection sees the GUC empty → deny-when-unset. No `SET` (session-wide) is ever used on this path. | `test_scope_is_transaction_local_no_pooled_leak` |
| V7 | Tampering / Information Disclosure | **Prompt-injection toward external surfaces** — a customer-facing assistant is coaxed (via prompt) to emit SQL / set a GUC / read another scope. | The scope GUC is **never** derived from model output, tool arguments, or natural-language input — only from the verified JWT context at the transport boundary. Under the deployed runtime (`mcp_user`), RLS is inert and does NOT provide an automatic database backstop. Protection against prompt injection relies on transport/application mediation (scoping parameters filtered in Python / explicit SQL `WHERE` predicates) and structural handler validation; see [`docs/vertical_engines/_security/c3-external-scope-adversarial-review.md`](c3-external-scope-adversarial-review.md). | `test_prompt_injection_attempt_to_set_foreign_scope_still_rls_bounded` |

### Defence-in-depth summary

1. **Transport:** JWT signature/exp/iss/aud verified before any scope is derived; scope claims read from the verified payload only.
2. **Application:** `_external_scope_from_context` has no header/NL input surface; employee/unknown tiers carry no scope; nil-UUID scope claim rejected; SQL queries enforce isolation via explicit `WHERE` predicates (`namespace_id = $1 AND partner_scope_id = $2`).
3. **Database (INERT as deployed):** While migrations define `external_isolation_policy` on `FORCE ROW LEVEL SECURITY` tables targeting `nce_app`, runtime application connections connect as `mcp_user` (`rolsuper = true`, `rolbypassrls = true`), and `scoped_pg_session` never issues `SET ROLE`.

🔴 **Risk statement:** As proven by Wave A-T6 adversarial review (PR #148, [`docs/vertical_engines/_security/c3-external-scope-adversarial-review.md`](c3-external-scope-adversarial-review.md)), **the database layer does NOT hold if the application layer is bypassed**. The previous claim that *"The database layer holds even if the application layer is wholly bypassed by an attacker... the application connects as nce_app (rolbypassrls = false)"* was **factually false in deployment**. RLS is inert, and explicit `WHERE`-clause filtering in application code is the sole barrier preventing cross-scope and cross-tenant leakage.

---

## 3. Residual risks & deployment posture

- **Connecting role posture (LIVE RISK):** The application connects as `mcp_user`
  (`rolsuper = true`, `rolbypassrls = true`), not `nce_app`. This is not merely an internal-operator
  trust concern: because `mcp_user` bypasses RLS and zero policies target `mcp_user`, RLS provides
  zero isolation at runtime. Any query that omits `partner_scope_id` or `namespace_id` from its
  `WHERE` clause (such as the `contractor_profiles` query in `vendors/partner_view.py:78,106`) immediately
  exposes data across scopes. See full findings and live diagnostic measurements in
  [`docs/vertical_engines/_security/c3-external-scope-adversarial-review.md`](c3-external-scope-adversarial-review.md) (PR #148).
- **Operator inserts a row with `external_scope_id = nil UUID`.** The migration
  forbids this by convention (documented in the function comment). Such a row
  would become visible to any session whose GUC is unset. This is a write-side
  data-integrity invariant; a `CHECK (external_scope_id <> '0…0')` constraint on
  each external-facing table would harden it, but adding DDL is out of scope for
  this review wave (it would belong in W22).
- **JWT signing-key compromise.** Forging a valid JWT with an arbitrary
  `external_scope_id` would defeat the application layer. Out of scope for C3;
  mitigated by key management and short token lifetimes (`exp` enforced).

None of the above is a C3 primitive defect. The five enumerated adversarial-external
vectors (deny-when-unset, IDOR via the GUC, forged-header spoofing, enumeration,
tenant-crossing) plus session-fixation and prompt-injection are all mitigated and
proven by the adversarial tests.
