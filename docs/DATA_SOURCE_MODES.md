> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# Data-Source Modes — per-function `d365 | both | nce` switch

> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17
>
> **Implementation status:** the storage substrate (`settings` table, migration 015) is
> **shipped** on main. The resolver (`nce/source_mode/`), per-function registry keys, and
> migrations 029/030 are **PLANNED — in-flight on the vertical-engines track** (present
> on `ml/foundation`, absent on main @ 7304330). Do **not** treat the switch as wired in
> production until those land.

**Companions:** `VERTICAL_MODULE_PATTERN.md`, `FRONTEND_READINESS.md`

## Goal

Make NCE able to become a **self-contained system** — modules only, no D365 — by
flipping each front-end function's data source in the admin panel, **with no data
migration at flip time**. A 3-way switch per function: **`d365`** (source of truth is
Dynamics), **`both`** (transition: read both, NCE primary, D365 for parity/compare),
**`nce`** (pure NCE; D365 not consulted).

## The load-bearing principle

> A function can flip to `nce` for free **only because NCE already holds its data when
> you flip.** So every flippable function must **continuously ingest + retain** its
> source data into NCE *while still in `d365`/`both` mode*. The flip then moves **zero
> data** — it just stops consulting D365.

This is why the D365 vertical's incremental sync + change-tracking retention exists: it
is the *precondition* for the switch, not an optimization. The same applies to every
non-D365 module (Sales, Product, …): build the NCE-side ingestion/store up front.

## Mechanism

### 1. Per-function source mode (setting)

**Storage substrate: shipped** (`nce/migrations/015_settings_table.sql`, main @ 7304330).

The `settings` table stores key/value pairs with JSONB values, section tagging, and
`updated_by`/`updated_at` audit columns. Access is gated to the `nce_app` role.

The source-mode keys follow the convention (keyed per `(namespace, module, function)`):

```
source_mode.<module>.<function> ∈ {d365, both, nce}   # default: d365 (or both in transition)
```

> **PLANNED:** the `source_mode.*` key family is **not yet registered** in
> `nce/settings_registry.py` on main. Registration, validation metadata, and the
> per-function default values are part of the resolver work (see below).

Surfaced in the admin panel as a **3-way control on each front-end function**, not a
global toggle — so functions migrate to pure-NCE one at a time as parity is proven.

### 2. Resolver in the endpoint (dual-surface — REST for the BFF, MCP for agents)

> **PLANNED — in-flight:** `nce/source_mode/` does **not exist on main @ 7304330**.
> The resolver module, its `source_mode()` helper, and the supporting migrations 029/030
> are on `ml/foundation` awaiting promotion. The pattern below is the agreed design
> contract for module authors.

Each capability resolves its mode and dispatches:

```python
async def do_<function>(engine, params) -> dict:
    mode = await source_mode(engine, namespace, "<module>", "<function>")   # d365|both|nce
    if mode == "nce":
        return await _from_nce(engine, params)            # NCE's own retained store/graph
    if mode == "d365":
        return await _from_d365(engine, params)           # D365 adapter (live or mirror)
    return _merge(await _from_nce(...), await _from_d365(...))  # both: NCE primary + parity
```

`_from_nce` reads the data NCE already ingested; `_from_d365` calls the D365 adapter.
The endpoint is exposed REST (BFF/frontend, no AI) **and** as an MCP tool (agents).

### 3. Rollout per function

`d365` → **`both`** (run in parallel, confirm NCE parity / backfill any gap) → **`nce`**.
When every function of every module is `nce`, D365 can be removed entirely with no
migration — the data has lived in NCE all along.

## What is shipped vs planned (summary)

| Component | Status | Location on main |
|---|---|---|
| `settings` table (migration 015) | **shipped** | `nce/migrations/015_settings_table.sql` |
| `settings_store.py` get/set API | **shipped** | `nce/settings_store.py` |
| `settings_registry.py` framework | **shipped** | `nce/settings_registry.py` |
| `nce/source_mode/` resolver module | **PLANNED** | absent — on `ml/foundation` |
| `source_mode.*` registry keys | **PLANNED** | absent — on `ml/foundation` |
| Migrations 029/030 (resolver schema) | **PLANNED** | absent — highest shipped is 024 |
| Admin panel 3-way control per function | **PLANNED** | absent — on `ml/foundation` |

## Rules for module authors (Sales, Product, …)

- Build the **NCE ingestion + retention** for a function *before* exposing its source
  switch — otherwise `nce` mode has nothing to read.
- Endpoints go through the **source-mode resolver**; never hard-wire D365.
- Default a new function to `d365` or `both`; only flip to `nce` after parity in `both`.
- A module may depend on other NCE modules in `nce` mode (e.g. Sales → Product) but must
  not depend on D365 in `nce` mode.

## Change log

- 2026-06-21 — Re-verified against main @ 7304330. Corrected provenance: resolver
  (`nce/source_mode/`), `source_mode.*` registry keys, and migrations 029/030 are absent
  on main and marked PLANNED. Settings table (migration 015) confirmed shipped. Status
  block updated from "living architecture spec" to "spec" with explicit in-flight seams.
- 2026-06-17 — Initial spec. Establishes the 3-way per-function source switch and the
  "retain-now-so-the-flip-is-free" principle; generalizes the D365 vertical's retention
  to all modules en route to a self-contained NCE.
