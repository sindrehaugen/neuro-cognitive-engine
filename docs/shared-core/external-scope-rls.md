> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# Doc 61 — Shared Core External Scope RLS Guide

> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

The NCE Shared Core Row-Level Security (RLS) model includes a specialized security primitive (**Component C3**) designed to enforce database isolation *below* the tenant namespace level. While tenant isolation separates namespaces (tenants) from one another, external scope isolation restricts access for external-facing actors (such as contractors, vendors, and customers) to their designated partition of data within a namespace.

This guide outlines the system design, the session Grand Unified Configuration (GUC) variables, the deny-when-unset policy, the three-tier threat model, Python execution integration, and threat mitigation logic.

---

## 1. Context & Component C3

As defined in the Shared-Core Foundation, **Component C3** provides row-level isolation below the namespace level across three principal tiers of escalating threat. 

In NCE, tables that serve internal employees and agents use a standard `tenant_isolation_policy` checking `namespace_id = get_nce_namespace()`. Tables that expose data to external parties must also isolate data based on the caller's specific external scope using an `external_isolation_policy` checking `external_scope_id = get_nce_external_scope()`. 

To prevent cross-tenant and cross-scope leaks, these policies operate as a **logical AND**:

$$\text{Authorized Rows} = (\text{Row namespace} = \text{Session namespace}) \land (\text{Row external scope} = \text{Session external scope})$$

Neither condition alone is sufficient; both conditions must evaluate to `TRUE` simultaneously to expose any record.

---

## 2. The Three-Tier Threat Model

NCE models access control across three distinct tiers, each mapped to a higher threat level and corresponding security boundary:

| Tier | Principal Type | Isolation Boundary | Enforcement Mechanism | Threat Profile |
| :--- | :--- | :--- | :--- | :--- |
| **Tier 1** | **Employee / Internal Agent** | Namespace Scope | Namespace GUC (`nce.namespace_id`) | **Low to Medium**: Trusted internal operations; requires basic protection against cross-tenant queries. |
| **Tier 2** | **Contractor / Partner** | Namespace Scope AND External Scope | Namespace GUC + External Scope GUC (`nce.external_scope_id`) | **Medium to High**: External users accessing specific supplier/contractor boundaries (e.g. Field Tech, Vendor portals). |
| **Tier 3** | **External Customer** | Namespace Scope AND External Scope + Field Projection | Namespace GUC + External Scope GUC + Allow-List Field Redactor (**Component C8**) | **Extreme (Adversarial)**: Direct internet-facing access (e.g. Customer Portal). High risk of IDOR, brute-forcing, and parameter tampering. |

---

## 3. The `nce.external_scope_id` GUC

The session configuration variable `nce.external_scope_id` is a PostgreSQL Grand Unified Configuration (GUC) parameter. It holds the active external scope UUID for the connection's transaction block.

The GUC value is accessed via the read-only PL/pgSQL helper function `get_nce_external_scope()`.

### Accessor Function Implementation
```sql
CREATE OR REPLACE FUNCTION get_nce_external_scope() RETURNS uuid AS $$
DECLARE
    val text;
BEGIN
    -- Read the session GUC. The second parameter `true` prevents an exception 
    -- if the GUC is completely unset, returning NULL instead.
    val := nullif(trim(current_setting('nce.external_scope_id', true)), '');
    
    IF val IS NULL THEN
        -- GUC is unset or empty: return the nil UUID sentinel.
        RETURN '00000000-0000-0000-0000-000000000000'::uuid;
    END IF;
    
    BEGIN
        RETURN val::uuid;
    EXCEPTION
        WHEN invalid_text_representation THEN
            -- Malformed GUC value (non-UUID): treat as unset -> nil UUID sentinel.
            RETURN '00000000-0000-0000-0000-000000000000'::uuid;
    END;
END;
$$ LANGUAGE plpgsql STABLE;

COMMENT ON FUNCTION get_nce_external_scope() IS
'C3 external-principal RLS accessor. Returns session external_scope_id or nil-UUID
deny-sentinel when unset. Use in external_isolation_policy USING expressions.';
```

### Key Design Decisions
- **`current_setting(..., true)`**: The `true` parameter makes the check permissive. If the GUC has not been defined on the current connection, PostgreSQL returns a database-level `NULL` rather than failing. This allows internal employees (who never configure an external scope) to query tables safely without database exceptions.
- **Fail-Safe Parsing**: Any malformed UUID string passed to the GUC causes the function to trap the `invalid_text_representation` exception and return the sentinel rather than throwing a query error.

---

## 4. Deny-When-Unset Sentinel Invariant

The **Deny-When-Unset** mechanism is the primary fail-closed defense for external-facing tables.

- **The Sentinel Value**: The nil UUID (`00000000-0000-0000-0000-000000000000`) is reserved as the "unset GUC" sentinel.
- **UUIDv4 Uniqueness**: NCE uses random UUIDv4 identifiers (via `gen_random_uuid()`) for real external scope IDs. The mathematical probability of a random UUIDv4 colliding with the nil-UUID sentinel is $0$.
- **Database Write Invariant**: Application code, migrations, and database operators are strictly forbidden from writing rows with a scope ID matching the nil UUID sentinel.
- **Enforcement Mechanics**: When a database session is established without setting `nce.external_scope_id`, or if the variable is set to empty or garbage, `get_nce_external_scope()` evaluates to the nil UUID. RLS policies checking `external_scope_id = get_nce_external_scope()` fail to match any rows, resulting in **zero visibility** (fails closed).

```
   Client Session (No GUC Set)
   ├── Query: SELECT * FROM contractor_profiles
   ├── RLS Evaluates: partner_scope_id = get_nce_external_scope()
   │                  partner_scope_id = '00000000-0000-0000-0000-000000000000'::uuid
   └── Result: Evaluates to FALSE for all rows -> Empty Set returned (Safe Fail-Closed)
```

---

## 5. Python Integration

Python interaction with the PostgreSQL connection layer utilizes the `scoped_pg_session` context manager combined with a transaction-specific setter.

### GUC Setting Implementation (`nce/db_utils.py`)
```python
async def set_external_scope(conn: asyncpg.Connection, scope_id: str | UUID) -> None:
    """Set the external_scope_id GUC for the current transaction.

    Symmetric to nce.auth.set_namespace_context: uses set_config(..., true)
    so the GUC is scoped strictly to the current transaction. It is automatically
    cleared when the transaction commits or rolls back. Must be called inside
    an open transaction.
    """
    await conn.execute(
        "SELECT set_config('nce.external_scope_id', $1, true)",
        str(scope_id),
    )
```

### Critical Security Constraints
1. **Transaction Scoping**: The final parameter `true` in `set_config('nce.external_scope_id', ..., true)` behaves like `SET LOCAL`. It bounds the parameter lifetime strictly to the active transaction. If connections are returned to an `asyncpg.Pool`, they carry no lingering GUC values to subsequent requests.
2. **Employee Isolation**: Employee sessions must never execute `set_external_scope()`. By omitting this call, the connection relies on the deny-when-unset sentinel for external tables, restricting employees from mistakenly accessing contractor tables without proper administrative context.

### Production Execution Pattern (`nce/vertical_modules/vendors/contractors.py`)
```python
async def do_get_contractor(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any] | None:
    ns_uuid = UUID(str(params["namespace_id"]))
    partner_scope_raw = params.get("partner_scope_id")

    # Acquire connection and open transaction (scoped_pg_session sets nce.namespace_id)
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        
        # If accessing as an external contractor, wire the scope check
        if partner_scope_raw:
            partner_scope_uuid = UUID(str(partner_scope_raw))
            await set_external_scope(conn, partner_scope_uuid)

        # Execute query. RLS automatically enforces namespace and external scope predicates.
        row = await conn.fetchrow(
            """
            SELECT contractor_id, namespace_id, partner_scope_id, profile, rates, skills
            FROM contractor_profiles
            WHERE contractor_id = $1 AND namespace_id = $2
            """,
            params["contractor_id"],
            ns_uuid,
        )
        ...
```

---

## 6. Reusable Policy Template

Every external-facing table that requires tenant and external scope isolation must apply the following SQL template:

```sql
-- Enable RLS and force it for the table owner/nce_app
ALTER TABLE T ENABLE ROW LEVEL SECURITY;
ALTER TABLE T FORCE ROW LEVEL SECURITY;

-- Drop existing policies
DROP POLICY IF EXISTS tenant_isolation_policy ON T;
DROP POLICY IF EXISTS external_isolation_policy ON T;

-- Enforce combined namespace and scope checks
CREATE POLICY external_isolation_policy ON T
    FOR ALL TO nce_app
    USING (
        namespace_id IS NOT NULL
        AND namespace_id = get_nce_namespace()
        AND external_scope_id IS NOT NULL
        AND external_scope_id = get_nce_external_scope()
    )
    WITH CHECK (
        namespace_id IS NOT NULL
        AND namespace_id = get_nce_namespace()
        AND external_scope_id IS NOT NULL
        AND external_scope_id = get_nce_external_scope()
    );
```

### Case Study: `contractor_profiles` Table
```sql
CREATE TABLE IF NOT EXISTS contractor_profiles (
    contractor_id      TEXT        NOT NULL,
    namespace_id       UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    partner_scope_id   UUID        NOT NULL,
    profile            JSONB       NOT NULL DEFAULT '{}'::jsonb,
    rates              JSONB       NOT NULL DEFAULT '{}'::jsonb,
    skills             TEXT[]      NOT NULL DEFAULT '{}'::text[],
    availability       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    performance_score  NUMERIC,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (contractor_id, namespace_id)
);

CREATE INDEX IF NOT EXISTS idx_contractor_profiles_namespace ON contractor_profiles (namespace_id);
CREATE INDEX IF NOT EXISTS idx_contractor_profiles_partner_scope ON contractor_profiles (partner_scope_id);

ALTER TABLE contractor_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE contractor_profiles FORCE ROW LEVEL SECURITY;

CREATE POLICY external_isolation_policy ON contractor_profiles
    FOR ALL TO nce_app
    USING (
        namespace_id IS NOT NULL
        AND namespace_id = get_nce_namespace()
        AND partner_scope_id IS NOT NULL
        AND partner_scope_id = get_nce_external_scope()
    )
    WITH CHECK (
        namespace_id IS NOT NULL
        AND namespace_id = get_nce_namespace()
        AND partner_scope_id IS NOT NULL
        AND partner_scope_id = get_nce_external_scope()
    );
```

---

## 7. Threat Model & Security Hardening

The C3 security primitive is hardened against adversarial manipulation across four vectors:

### Vector 1: Insecure Direct Object Reference (IDOR)
* **Threat**: A contractor belonging to Namespace A obtains a valid `partner_scope_id` belonging to Namespace B (e.g. through a compromised ticket, email, or webhook payload) and attempts to query it.
* **Mitigation**: The policy performs a strict logical `AND`. Since the contractor's authenticated session enforces Namespace A's GUC (`nce.namespace_id = Namespace A`), the comparison `namespace_id = get_nce_namespace()` evaluates to `FALSE` for the Namespace B record, regardless of whether the `partner_scope_id` matches.

### Vector 2: Scope ID Brute-Force / Enumeration
* **Threat**: An attacker attempts to iterate through potential scope IDs to find records.
* **Mitigation**: Real scope IDs are UUIDv4 values. The search space size of $2^{122}$ represents overwhelming entropy, making brute-force guessing attacks mathematically impossible.

### Vector 3: Connection Pool GUC Contamination
* **Threat**: Connection pools reuse database sockets. If a transaction sets `nce.external_scope_id` and is returned to the pool without being cleared, a subsequent user's query may inherit that scope, leaking data.
* **Mitigation**: Python utilizes `set_config('nce.external_scope_id', ..., true)`. The `true` parameter restricts the lifetime of the GUC variable strictly to the duration of the current transaction. When the transaction executes `COMMIT` or `ROLLBACK`, the GUC is instantly discarded by PostgreSQL, ensuring zero carry-over leakage between requests.

### Vector 4: NULL Scopes & Null Evaluation
* **Threat**: A row is written with `partner_scope_id = NULL`. If the accessor function evaluates to `NULL` (e.g., when the GUC is unset or empty), the comparison `NULL = NULL` could evaluate to true or throw errors depending on DB engine settings.
* **Mitigation**: The RLS policy explicitly guards against null values by checking `partner_scope_id IS NOT NULL`. Rows with a `NULL` scope are structurally unreachable by external roles.
