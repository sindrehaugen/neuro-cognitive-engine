> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# Shared Core Source Mode Divergence Guide

> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

The **Shared Core Source Mode Divergence Guide** defines the design, implementation, and operational guardrails for the **C5 Source-Mode Resolver and Divergence Audit Log** component in the Neuro-Cognitive Engine (NCE). This component provides the infrastructure to transition NCE from a Dynamics 365-dependent adapter into a self-contained, native cognitive engine per-function and per-tenant without requiring lock-step migrations or downtime.

---

## 1. Architectural Relationship to Doc 31

This guide builds upon and refines the specification outlined in **Doc 31** ([DATA_SOURCE_MODES.md](../DATA_SOURCE_MODES.md)). 

### Structural Evolution: Settings Table vs. Dedicated Config Table
* **Original Spec (Doc 31):** Envisioned using the global `settings` table (introduced in migration `015_settings_table.sql`) with keys patterned as `source_mode.<module>.<function>` to govern routing.
* **Production Implementation (Doc 63):** Because the global `settings` table is key-value structured and cannot easily support granular PostgreSQL Row Level Security (RLS) policies scoped per namespace/tenant, the design evolved. A dedicated table, `source_mode_config` (migration `030_c5_source_mode_config.sql`), was introduced. This dedicated table features `FORCE RLS` to strictly isolate configuration rows by tenant namespace, preventing cross-tenant leakage or configuration tampering.

---

## 2. Database Schema and Security Controls

The C5 source-mode framework utilizes two custom relational tables in the PostgreSQL database. Security and multi-tenant isolation are enforced directly at the database layer using Postgres Row-Level Security (RLS).

### A. Source-Mode Configuration (`source_mode_config`)
This table stores the active runtime source mode for each `(namespace, engine, function)` tuple.

* **Migration Source:** `030_c5_source_mode_config.sql`
* **Schema Definition:**
  ```sql
  CREATE TABLE source_mode_config (
      namespace_id UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
      engine       TEXT        NOT NULL,
      function     TEXT        NOT NULL,
      mode         TEXT        NOT NULL CHECK (mode IN ('d365', 'both', 'nce')),
      updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
      PRIMARY KEY (namespace_id, engine, function)
  );
  ```
* **Indices:**
  ```sql
  CREATE INDEX idx_source_mode_config_namespace_engine
      ON source_mode_config(namespace_id, engine);
  ```
* **Security & RLS Enforcement:**
  * **RLS Status:** `ENABLE ROW LEVEL SECURITY` and `FORCE ROW LEVEL SECURITY` are both applied to ensure policies apply to table owners and administrative bypasses are mitigated.
  * **Tenant Policy (`tenant_isolation_policy`):**
    ```sql
    CREATE POLICY tenant_isolation_policy ON source_mode_config
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    ```
  * **Role Grants:** Gated to the application role (`nce_app`).
    ```sql
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE source_mode_config TO nce_app;
    ```

### B. Append-Only Divergence Audit Log (`divergence_log`)
This table functions as a Write-Once-Read-Many (WORM) audit log to track discrepancies between NCE's native data store and the external systems.

* **Migration Source:** `031_c5_divergence_log.sql`
* **Schema Definition:**
  ```sql
  CREATE TABLE divergence_log (
      id           UUID        NOT NULL DEFAULT gen_random_uuid(),
      namespace_id UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
      engine       TEXT        NOT NULL,
      entity       TEXT        NOT NULL,
      field        TEXT        NOT NULL,
      nce_value    TEXT,
      ext_value    TEXT,
      materiality  NUMERIC     NOT NULL,
      detected_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
      PRIMARY KEY (id)
  );
  ```
* **Foreign Key Design Constraint:** There is **no foreign key constraint** referencing `source_mode_config`. This is a deliberate design decision ensuring that even if a function configuration row is deleted or reset, the historic divergence audit trail remains intact and immutable.
* **Indices:**
  ```sql
  CREATE INDEX idx_divergence_log_namespace_engine_detected
      ON divergence_log (namespace_id, engine, detected_at DESC);
  ```
* **Security & RLS Enforcement:**
  * **RLS Status:** `ENABLE ROW LEVEL SECURITY` and `FORCE ROW LEVEL SECURITY` are active.
  * **Tenant Policy (`tenant_isolation_policy`):**
    ```sql
    CREATE POLICY tenant_isolation_policy ON divergence_log
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    ```
  * **Append-Only Grants:** To enforce the audit trail's integrity, `nce_app` is granted `SELECT` and `INSERT` privileges. `UPDATE` and `DELETE` actions are strictly revoked.
    ```sql
    REVOKE ALL ON TABLE divergence_log FROM nce_app;
    GRANT SELECT, INSERT ON TABLE divergence_log TO nce_app;
    ```

---

## 3. The Source-Mode Resolver (`nce.source_mode.resolver`)

The Python package `nce.source_mode` provides three core functions to resolve, read, and write dispatch routing based on active source modes.

### A. Source Mode Toggles
The resolver handles three distinct operational modes:
* **`d365` (Dynamics 365 Primary):** Reads and writes route entirely to the external Dynamics 365 system. This is the **default mode** for all unconfigured functions, acting as a conservative fallback when migrations have not yet been initiated.
* **`both` (Transition Parity):** Serves reads from the NCE native store (primary) but also queries the external system asynchronously to perform a parity check. Writes are executed against **both** systems.
* **`nce` (Native NCE Only):** Reads and writes route exclusively through NCE's native cognitive ledger and memory graphs. The external Dynamics 365 system is completely bypassed.

### B. Core Interface Implementations

#### 1. `resolve`
Looks up the current source mode within the context of a scoped Postgres session, ensuring RLS GUC parameters are correctly populated.

```python
async def resolve(
    pool: asyncpg.Pool,
    *,
    engine: str,
    function: str,
    namespace_id: str | UUID,
) -> SourceMode:
```
* **Behavior:** Acquires a database connection via `scoped_pg_session(pool, namespace_id)`. If no row is returned for the given engine/function combination, it returns the safe fallback value `"d365"`.

#### 2. `read_through`
Dispatches read operations utilizing a table-driven approach to prevent branch drift.

```python
async def read_through(
    mode: SourceMode,
    *,
    native_reader: Callable[[], Awaitable[Any]],
    external_reader: Callable[[], Awaitable[Any]],
    parity_check: Callable[[Any, Any], Awaitable[None]],
) -> Any:
```
* **Dispatch Routing Matrix (`_READ_DISPATCH`):**
  | Mode | Read Native? | Read External? | Execute Parity Check? | Primary Return Source |
  | :--- | :---: | :---: | :---: | :--- |
  | `d365` | False | True | False | External Reader |
  | `both` | True | False | True | Native Reader |
  | `nce` | True | False | False | Native Reader |
* **Behavior:** When in `both` mode, both native and external readers are executed. The results are passed to `parity_check` before the native result is returned.

#### 3. `write_route`
Routes mutating writes using a similar table-driven dispatch matrix.

```python
async def write_route(
    mode: SourceMode,
    *,
    native_writer: Callable[[], Awaitable[Any]],
    external_writer: Callable[[], Awaitable[Any]],
) -> dict[str, Any]:
```
* **Dispatch Routing Matrix (`_WRITE_DISPATCH`):**
  | Mode | Write Native? | Write External? |
  | :--- | :---: | :---: |
  | `d365` | False | True |
  | `both` | True | True |
  | `nce` | True | False |
* **Return Format:** Returns a dictionary mapping results: `{"native": native_result, "external": external_result}`. Paths that were not executed for the active mode return `None`.

---

## 4. Parity Verification and the Flip-Gate Mechanism

The path from `both` (transition) to `nce` (native only) is governed by a **flip-gate mechanism** that prevents promotion if data divergence has been detected recently.

```
                  ┌──────────────────────────────┐
                  │          "d365" Mode         │
                  │   All reads/writes to D365   │
                  └──────────────┬───────────────┘
                                 │
                                 ▼
                  ┌──────────────────────────────┐
                  │          "both" Mode         │
                  │   Writes to both. Reads:     │
                  │   NCE (primary) + D365 compare│
                  └──────────────┬───────────────┘
                                 │
                     Parity Window Verification
                  ┌──────────────┴───────────────┐
                  │    Any divergence logged     ├─ YES ─┐
                  │   within lookback window?    │       │
                  └──────────────┬───────────────┘       │
                                 │ NO                    ▼
                                 │               ┌───────────────┐
                                 ▼               │  FLIP BLOCKED │
                  ┌──────────────────────────────┤ Resolve drift │
                  │          "nce" Mode          │  and restart  │
                  │  Reads/writes native-only.   │    window     │
                  │    External is decoupled.    └───────────────┘
                  └──────────────────────────────┘
```

### A. Recording Divergence
Divergences are recorded in the append-only log using `record_divergence`.

```python
async def record_divergence(
    pool: asyncpg.Pool,
    *,
    namespace_id: str | UUID,
    engine: str,
    entity: str,
    field: str,
    nce_value: str | None,
    ext_value: str | None,
    materiality: float | Decimal,
) -> None:
```

* **Classification & Alerting:**
  1. Inserts the row into `divergence_log` inside a transaction wrapped by `scoped_pg_session`.
  2. Resolves the materiality threshold dynamically at runtime via the environment variable:
     `NCE_DIVERGENCE_ALERT_THRESHOLD` (defaults to `0.1` / 10%).
  3. **Above Threshold (`materiality > threshold`):** Triggers a high-priority drift alert via the central dispatcher:
     `nce.notifications.dispatcher.dispatch_alert(title, message)`.
  4. **Below/At Threshold:** Logs the event at `DEBUG` level for telemetry purposes without paging engineers or sending notifications.

### B. The Flip Gate (`flip_blocked`)
A tenant's administrator cannot promote a function from `both` to `nce` unless the lookback parity window is completely clean of discrepancies.

```python
async def flip_blocked(
    pool: asyncpg.Pool,
    *,
    namespace_id: str | UUID,
    engine: str,
    window_seconds: float,
) -> bool:
```
* **Gate Check:** Queries `divergence_log` for the specified `namespace_id` and `engine` over the interval `now() - (window_seconds * INTERVAL '1 second')`.
* **Gate Enforcement:**
  * Returns `True` (Blocked) if *any* divergence records exist within the lookback window.
  * Returns `False` (Allowed) if the lookback window contains zero divergence records, proving operational parity.

---

## 5. Operations & Migration Rollout Workflow

To transition a feature engine capability safely from D365 to Native NCE, operators must execute the following sequence:

1. **Deploy Ingestion/Retention:** Ensure the target engine has continuous sync scripts running to mirror and write updates into the NCE memory graph.
2. **Configure to `both`:** Insert or update the configuration row in `source_mode_config` to `both`.
3. **Observe Parity:** Monitor the `divergence_log` for any reported discrepancies. If alerts are dispatched via the dispatcher, fix the ingestion pathways or sync logic.
4. **Enforce Lookback Window:** Choose a target lookback window (e.g., `86400.0` seconds for a 24-hour verification window). Ensure `flip_blocked(...)` returns `False` over this duration.
5. **Promote to `nce`:** Execute the configuration update setting `mode = 'nce'`. The engine is now native-only; external calls to Dynamics 365 are decommissioned.

---

## 6. Document Change Log

* **2026-06-24:** Created documentation unit matching implementation state on `ml/foundation` and migrations `030`/`031`. Reconciled namespace-scoping requirements with the original Doc 31 settings spec.
