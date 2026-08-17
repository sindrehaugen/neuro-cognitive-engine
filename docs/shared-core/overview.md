> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# Doc 58 — Shared Core Foundation Operator Guide

The Neuro-Cognitive Engine (NCE) Shared Core Foundation provides the essential, domain-agnostic capabilities and safety primitives that sit underneath the vertical modules (Tiers 1–4). Building these seams once in the core (`nce/`) ensures strict multi-tenant isolation, consistent auditing, single-writer consistency, and safety guards for autonomous model operations.

This guide outlines the nine core components (**C1–C9**), the settings substrate, the per-namespace opt-in mechanism, and the operational differences between **Contract A** (data ownership) and **Contract B** (autonomy governance).

---

## 1. The C1–C9 Shared Core Capabilities

The Shared Core is divided into nine functional components designed to be implemented once and consumed by all vertical engines.

```
┌────────────────────────────────────────────────────────────────────────┐
│                              NCE API / MCP                             │
├──────────────────┬───────────────────┬────────────────┬────────────────┤
│    C6 Pricing    │    C7 Signing     │  C8 Redactor   │   C9 Guards    │
└────────┬─────────┴─────────┬─────────┴────────┬───────┴────────┬───────┘
         │                   │                  │                │
┌────────▼───────────────────▼──────────────────▼────────────────▼───────┐
│                     C2 Autonomy Governance Wrapper                     │
├────────────────────────────────────────────────────────────────────────┤
│                       C4 Reactive Trigger Outbox                       │
├────────────────────────────────────────────────────────────────────────┤
│                       C1 Entity-Resolution Registry                    │
├────────────────────────────────────────────────────────────────────────┤
│                      C3 External-Principal RLS                         │
├────────────────────────────────────────────────────────────────────────┤
│                   C5 Source-Mode Resolver (d365|both|nce)              │
└────────────────────────────────────────────────────────────────────────┘
```

### C1 — Entity-Resolution & Node-Ownership Registry
*   **What:** Standardizes matching, candidate scoring, human-review merge queues, survivorship, and node-ownership tracking across all graph write-paths.
*   **Key Code Primitives (`nce/entity_resolution/`):**
    *   **Matching (`resolver.py`):** The `resolve(conn, *, namespace_id, candidate, keys, node_type)` function casefolds, normalizes, and ranks candidate nodes against existing `kg_nodes` using `pg_trgm` similarity averages across the requested keys.
    *   **Merge Queue (`merge_queue.py`):** mergeless queue management (`enqueue()`, `list_pending()`, `confirm()`, `reject()`) targeting the `entity_merge_queue` table. Under strict no-auto-merge guards, a confirm/reject action mutates *only* the queue row status and does not automatically alter graph nodes or edges.
    *   **Ownership (`ownership.py`):** The `assert_owner(conn, namespace_id, node_type, writer_engine, transition)` function queries `node_ownership_registry` and raises `OwnershipError` (deny-by-default) if the writer does not match the registered owner.
    *   **Survivorship (`survivorship.py`):** The `survive(field_values)` function evaluates a precedence chain of `source_trust > recency (as_of) > confidence`. Winning value provenance is audited via `append_survivorship_provenance` into `v3_cognitive_ledger` under `model_version = 'survivorship/v1'`.
    *   **Normalizers (`normalizers.py`):** Pure-function normalizers that load alias maps from config files (e.g. `nce/config_data/manufacturer-normalization.json`) and apply them to raw strings.

### C2 — Autonomy-Governance Wrapper
*   **What:** Enforces safety thresholds, idempotency, kill switches, and transaction checks on mutating tools.
*   **Key Code Primitives (`nce/autonomy/`):**
    *   **Decorator (`governor.py`):** The `@governed` decorator wraps async handlers and validates calling bounds. It checks and records keys in `action_idempotency` and appends audit events to the `event_log`.
    *   **Policy (`policy.py`):** The `evaluate_policy()` function checks risk flags (`flagship`, `first_of_kind`, `regulated`), value ceilings, volume rate caps, and counterparty allowlists.
    *   **Schema Check (`schema_check.py`):** The `check_autonomy_schema(conn)` asserts that `action_approval_queue`, `action_idempotency` (with a composite `(namespace_id, idempotency_key)` unique constraint), and `processed_outbox_events` tables are present before execution starts.
    *   **Kill Switch:** Evaluates Redis hash `nce:tools:disabled` per-tool and globally (`*`), failing closed if Redis is unreachable.

### C3 — External-Principal RLS Primitive
*   **What:** Row-level security that isolates records below the namespace tier across three user levels: employee (namespace scope), contractor (partner scope), and customer (adversarial scope).
*   **Implementation:** Leverages the session GUC variable `nce.external_scope_id` and the PL/pgSQL function `get_nce_external_scope()`. If the GUC is unset or invalid, the helper returns a nil UUID deny-sentinel (`'00000000-0000-0000-0000-000000000000'::uuid`), resulting in a default-deny posture.

### C4 — Reactive Graph-Event / Trigger Bus
*   **What:** A mechanism enabling engines to listen and react asynchronously to graph mutations written by other engines.
*   **Implementation:** Generalizes the transactional outbox substrate in `nce/outbox_relay.py`. When a graph write occurs, an event is staged in `outbox_events` and relayed post-commit. Handlers are required to be idempotent and process events at-least-once.

### C5 — `d365|both|nce` Source-Mode Resolver + Divergence Audit
*   **What:** Standardizes read/write dispatching, incremental delta retention, and continuous data reconciliation between NCE and Dynamics 365.
*   **Implementation:** Routes read/write operations based on the active mode (`d365`, `both`, or `nce`). Divergences detected during synchronization are logged to the `<engine>_divergence_log` table with materiality levels. Flip gates to native `nce` mode are blocked until the divergence log has remained clean for a configured time window.

### C6 — Shared Pricing Service
*   **What:** Consolidates pricing logic, profit margin (DG) calculations, price resolution, and cost/margin data egress filtering.
*   **Implementation:** The core pricing service resolves `customer BID > supplier list > base` pricing matrices. To prevent leaks, cost and margin fields are explicitly excluded from customer-facing surfaces.

### C7 — Shared Signing Service
*   **What:** Houses document signature generation, lifecycle handling, and verification behind a unified transport driver.
*   **Implementation:** Exposes a clean `request_signature(doc, signer, method)` interface with concrete drivers for `SignTransport` (e.g. `oneflow` for authoring, `criipto`/`signicat` direct rails, or a fallback `manual` driver).

### C8 — Allow-list Field-Redactor
*   **What:** Scopes field-level data visibility on external surfaces using a strict allow-list paradigm.
*   **Implementation:** The function `project(node, surface)` maps data payloads against a surface-specific JSON configuration (e.g., `nce/config_data/partner-redaction.json`). Unrecognized fields are hidden by default to prevent accidental data leaks.

### C9 — Structural-Enforcement Helpers
*   **What:** Implements structural guards to enforce business and regulatory rules directly at the data layer, bypassing the risk of LLM prompt evasion.
*   **Implementation:**
    *   **C9a (Retrieval-Grounded-Generation Helper):** Assembles generated prose from cited graph node facts, requiring that every statement carries a verifiable, direct relation link to a node in `kg_nodes` or a record in the ledger.
    *   **C9b (No-Person-Grain-Comparison Query Guard):** Strips individual identities at the database access boundary for aggregate metrics (such as performance ranking or comparison) to structurally enforce compliance with the EU AI Act.

---

## 2. Per-Namespace Opt-in (`metadata.<name>.enabled`)

N-Tier vertical engines (such as the `product` or `procurement` engines) are loaded globally but are isolated and locked on a per-tenant basis. A namespace must explicitly opt in to use an engine's MCP tools or REST routes.

### Configuration Substrate
Opt-in states are stored directly in the `metadata` JSONB column of the `namespaces` table. The metadata schema maps each vertical's configuration parameters:

```sql
-- namespaces table definition (schema.sql)
CREATE TABLE IF NOT EXISTS namespaces (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug       TEXT UNIQUE NOT NULL,
    parent_id  UUID REFERENCES namespaces(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata   JSONB NOT NULL DEFAULT '{}'::jsonb
);
```

Within the `metadata` column, vertical modules are assigned a nested configuration block. For example, the Product engine config is defined by `NamespaceProductConfig` in `nce/models.py`:

```python
class NamespaceProductConfig(BaseModel):
    enabled: bool = False
```

This configuration maps directly to the JSON format:
```json
{
  "product": {
    "enabled": true
  }
}
```

### In-Code Guard Checks
To enforce the opt-in gate, a guard is placed at the boundary of every MCP tool handler (`handle_*`) and REST route handler (`api_*`). The core domain business logic (`do_*`) remains un-guarded to allow programmatic callability.

For the Product engine, this is handled by `require_product_enabled` in `nce/vertical_modules/product/_guard.py`:

```python
async def require_product_enabled(pool: Any, namespace_id: str) -> None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT COALESCE(
                       (metadata->'product'->>'enabled')::boolean,
                       false
                   ) AS product_enabled
            FROM   namespaces
            WHERE  id = $1::uuid
            """,
            namespace_id,
        )

    if row is None or not row["product_enabled"]:
        raise ProductDisabledError(
            f"Product vertical is not enabled for namespace {namespace_id}. "
            "Set metadata.product.enabled=true to opt in."
        )
```

---

## 3. Settings Substrate

NCE handles environment configuration, tenant variables, and database connection settings through a multi-layered substrate.

### 1. Environment Configurations (`nce/config.py`)
All environment variables are parsed in `nce/config.py` using the `_Config` class (imported via `from nce.config import cfg`). Variables are prefixed with `NCE_` (legacy `TRIMCP_` variables raise an `OSError` at import).
*   **File-Based Secrets (`*_FILE`):** To prevent secrets from leaking into `/proc/<pid>/environ`, sensitive environment variables support a companion `_FILE` variable (e.g. `NCE_MASTER_KEY_FILE`). When set, NCE reads the secret from the designated file once at boot and strips the trailing newline.
*   **The Master Key (R3 Constraint):** The `NCE_MASTER_KEY` (minimum 32 UTF-8 bytes) is used for envelope-encrypting tenant database keys at rest. Under the **R3 security constraint**, it must only be resolved from the process environment or a secret file; it is strictly prohibited from passing through any database or file-backed config provider.

### 2. Global Runtime Settings (`settings` Table)
Global operational settings are stored in the `settings` database table:
*   **Scope:** Global across the database cluster (Primary Key is `key`).
*   **Usage:** Stores global variables like migration states and engine watermarks. It is **not** row-level security isolated.

### 3. Namespace settings
Tenant-specific configurations (such as retention limits, PII policies, and vertical opt-ins) are stored inside the `namespaces.metadata` column.

### 4. Transaction-Local Tenant Context (RLS GUC)
NCE enforces tenant isolation at the database layer using PostgreSQL Row-Level Security (RLS). 
*   **Setting Context:** When a connection is retrieved from the pool, `scoped_pg_session(pool, namespace_id)` automatically opens a transaction and sets the context:
    ```python
    await conn.execute("SELECT set_config('nce.namespace_id', $1, true)", str(namespace_id))
    ```
*   **Auto-Reset:** Using the third parameter (`true`) maps `set_config` to `SET LOCAL`. This scopes the namespace context strictly to the transaction. The database automatically clears the context on `COMMIT` or `ROLLBACK`, preventing tenant context leaks across the connection pool.

---

## 4. Cross-Engine Contracts: Contract A vs. Contract B

To manage integration risks in the seams between vertical engines, NCE enforces two distinct categories of contracts.

### Comparison Table
| Aspect | Contract A (Data Ownership) | Contract B (Autonomy Governance) |
| :--- | :--- | :--- |
| **Primary Focus** | **Node ownership and single-writer invariants** | **Safety bounds and human confirmation gates** |
| **Protected Surface** | Graph nodes, edges (`kg_nodes`, `kg_edges`), and database tables | Mutating autonomous tool execution paths |
| **Enforcement Layer** | Core DB write-paths & `node_ownership_registry` | `@governed` python decorator |
| **Trigger Event** | Direct write/update of a shared node type | Execution of an Advisor/Actor/Autonomous tool |
| **Primary Primitive** | `assert_owner(conn, ns, node_type, writer_engine)` | `evaluate_policy(value, ceiling, rate, allowlist)` |
| **Failure Response** | Raises `OwnershipError` (rejection) | Intercepts call, returns `{"status": "pending_approval"}` |

---

### Contract A: Shared-Node Ownership & Lifecycle Registry
Contract A dictates *who* may write to a specific data point. By restricting write authority to a single owning engine per lifecycle phase, NCE prevents data races and structural database conflicts.

#### Core Rules:
1.  **Sole Writer:** A node type or field has exactly one owner at any given point in time. If another engine needs to make an edit, it must either invoke an A2A tool owned by the host engine or submit a proposal node.
2.  **State-Machine Transitions:** Ownership can hand off during lifecycle state transitions, but the writer-of-record for each transition must remain explicit.

#### Example: The `BOM_LINE` Lifecycle Machine
The `BOM_LINE` record is edited by multiple engines, which could lead to race conditions. Contract A resolves this by dividing the node's properties:

```mermaid
state-machine
    [*] --> Planned : System Design (6) writes content
    Planned --> Signed : Sales (5) freezes content (Immutable)
    Signed --> Ordered : Procurement (1) updates status
    Ordered --> Delivered : Warehouse/Inventory (11) updates status
    Delivered --> Installed : Field Tech (12) updates status
    Installed --> Asset_Lifecycle : Field Tech creates ASSET; status hand-off
```

*   **Content (Product, Qty, Price):** Written by **System Design (6)**, then frozen by **Sales (5)** upon signature. It remains immutable thereafter; further modifications require a `CHANGE_ORDER` node owned by **Project (7)**.
*   **Status Transitions:** Handled sequentially on a strict state path: `PLANNED` $\rightarrow$ `ORDERED` (written by **Procurement**) $\rightarrow$ `DELIVERED` (written by **Warehouse/Inventory**) $\rightarrow$ `INSTALLED`/`TESTED` (written by **Field Tech**).
*   **Actual Cost:** Written exclusively by the **Economy (8)** posting cascade.
*   **Asset Hand-off:** At install, **Field Tech (12)** writes the terminal status and generates the related `ASSET` node (`BOM_LINE -[installed_as]-> ASSET`). The asset lifecycle then handles all operational states, avoiding duplicate status conflicts.

---

### Contract B: Autonomy Governance
Contract B dictates *how* a mutating tool can interact with the environment. It acts as an execution wrapper to prevent run-away AI loops, accidental double-submissions, and unauthorized operations.

```
Mutating Handler Call
  │
  ├── 1. Idempotency Check (Require key; look up in action_idempotency)
  │      ├── Key Exists ──► Return {"status": "already_executed"} (NO-OP)
  │      └── Key New ─────► Proceed
  │
  ├── 2. Confirm Check
  │      ├── confirm=False ──► Return {"status": "pending_approval"}
  │      └── confirm=True ───► Proceed
  │
  ├── 3. Kill Switch Check (Check Redis nce:tools:disabled)
  │      ├── Disabled ────► Raise KillSwitchError
  │      └── Enabled ─────► Proceed
  │
  ├── 4. Policy Evaluation (evaluate_policy)
  │      ├── Any Gate Trips ─► Return {"status": "pending_approval", "reason": ...}
  │      └── All Gates Pass ─► Proceed
  │
  └── 5. Database Transaction Guard (Enforce conn.is_in_transaction())
         ├── No Transaction ─► Raise GovernanceError
         └── Transaction Ok ─► Insert idempotency key, run handler, append event
```

#### Core Safeguards (Governed Wrapper):
1.  **Confirm-Only Default:** Mutating operations require `confirm=True` to execute. Without it, the handler returns `{"status": "pending_approval"}` and halts execution.
2.  **Idempotency Enforcement:** Mutating calls must carry a non-empty `idempotency_key`. The governor inserts this key into the `action_idempotency` table. Retries of the same key result in a graceful `already_executed` NO-OP.
3.  **Auditing:** Successful executions append an audit record to the `event_log` under `event_type = 'config_changed'` via `append_event()`.
4.  **Transaction Enforcement:** The governor asserts `conn.is_in_transaction()`. If a mutating handler runs outside of a transaction, the idempotency key could commit before the handler completes, leaving a **poison key** that prevents future retries. Enforcing transaction boundaries ensures that if the handler or the audit logging fails, the idempotency key rollback is handled cleanly.
5.  **Policy Gates:** Tripped policy parameters (value ceilings, rate limits, non-allowlisted counterparties, or risk flags) force the operation to drop back to human confirmation.
