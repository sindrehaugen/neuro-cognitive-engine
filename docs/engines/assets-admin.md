> **Status:** shipped (partial) · **Verified-against:** b75c873 (main) · **Last-audited:** 2026-09-06

# Assets Engine Admin Guide (Doc 91)

> **Status:** shipped (partial) · **Verified-against:** b75c873 (main) · **Last-audited:** 2026-09-06

This guide documents the administrative, operational, and architectural boundaries of the **Assets Engine** (`nce/vertical_modules/assets/`): tenant enablement (there is none), the 2 SQL migrations and their RLS/grant posture, the 8 MCP tools and 7 REST routes, the graph-projection module that exists but is never called, the telemetry mock/real-adapter swap, health-score computation and its RS-3 mock-telemetry suppression rule, and the full spec-vs-shipped delta against `docs/vertical_engines/09-assets-engine.md`. Every claim below cites a specific file/line on `main @ b75c873`.

---

## 1. Enablement — there is currently no opt-in gate

> [!WARNING]
> Unlike the Product engine's `_guard.py` pattern, **the Assets engine has no namespace-level enablement check anywhere in the codebase.** `nce/config.py` defines **zero** `NCE_ASSETS_*` keys — confirmed by a direct grep for `ASSETS` in that file returning nothing — despite the engine spec (`docs/vertical_engines/09-assets-engine.md` §"Config keys") naming `NCE_ASSETS_ENABLED`, `NCE_ASSETS_NETBOX_*`, `NCE_ASSETS_TELEMETRY_POLL_INTERVAL_MINUTES`, `NCE_ASSETS_HEALTH_DEGRADED_THRESHOLD`, `NCE_ASSETS_WARRANTY_WARN_DAYS`, and `NCE_ASSETS_EOL_WARN_DAYS`. There is no `nce/vertical_modules/assets/_guard.py` (confirmed absent from the directory), no `metadata.assets.enabled` check, and no `require_assets_enabled`/`AssetsDisabledError` equivalent anywhere in `nce/vertical_modules/assets/` or `nce/admin_handlers/assets.py`. Every namespace that can reach the 8 MCP tools or the 7 REST routes below can use the Assets engine — there is no per-tenant kill switch in this codebase; enforce one at your reverse proxy if you need it.

The only environment variable this engine actually reads is the telemetry env-swap family, `NCE_ASSETS_TELEMETRY_<PLATFORM>_REAL` (§5) — read live via `nce.config.live_env_str`, never captured at import (`telemetry.py:305-312`).

**Config-as-IP files that do exist** (both namespace-global, no per-tenant override mechanism):
- `nce/config_data/asset-lifecycle.json` — the 14-state transition map (§2 of the user guide).
- `nce/vertical_modules/assets/asset-health-weights.json` — health-score weights and thresholds (§7 below). Note this file lives *inside* the module package, not in the shared `nce/config_data/` directory every sibling engine uses — a placement inconsistency worth knowing if you go looking for it in the usual place.

---

## 2. Database migrations & RLS

The Assets engine owns exactly **2** migrations. Both are `IF NOT EXISTS`/idempotent DDL, re-run on every boot under an advisory lock (there is no migration ledger in this repo).

| Migration | Table | Natural key | `nce_app` grants | RLS |
|---|---|---|---|---|
| `054_assets.sql` | `assets` | `UNIQUE (namespace_id, bom_line_id)` (`assets_ns_bom_line_uq`) | `SELECT, INSERT, UPDATE` — **no `DELETE`** | `ENABLE` + `FORCE`, `tenant_isolation_policy` |
| `057_telemetry_samples.sql` | `telemetry_samples` | `UNIQUE (namespace_id, asset_id, metric, sampled_at)` (`telemetry_samples_idempotency_uq`) | `SELECT, INSERT` only — **no `UPDATE`, no `DELETE`** | `ENABLE` + `FORCE`, `tenant_isolation_policy` |

### 2.1 `assets` (migration 054)
```sql
CREATE TABLE IF NOT EXISTS assets (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    bom_line_id            TEXT        NOT NULL,
    serial                 TEXT,
    functional_location_id TEXT,
    lifecycle_state        TEXT        NOT NULL,
    change_origin          TEXT        NOT NULL DEFAULT 'agent',
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT assets_ns_bom_line_uq UNIQUE (namespace_id, bom_line_id),
    CONSTRAINT assets_bom_line_id_not_blank CHECK (btrim(bom_line_id) <> ''),
    CONSTRAINT assets_lifecycle_state_not_blank CHECK (btrim(lifecycle_state) <> ''),
    CONSTRAINT assets_serial_not_blank CHECK (serial IS NULL OR btrim(serial) <> ''),
    CONSTRAINT assets_functional_location_id_not_blank
        CHECK (functional_location_id IS NULL OR btrim(functional_location_id) <> ''),
    CONSTRAINT assets_change_origin_check
        CHECK (change_origin IN ('sync','webhook','agent','operator','consolidation','replay','unknown'))
);
CREATE INDEX IF NOT EXISTS idx_assets_namespace_functional_location
    ON assets (namespace_id, functional_location_id);
```
(`nce/migrations/054_assets.sql:95-148`)

**Columns the engine spec lists that this table does not have:** `health_score`, `monitoring_platform`, `warranty_until`, `firmware`, `mac`, `ip`, `dpp jsonb`, `netbox_device_id`, `assets_source_id` (spec: `09-assets-engine.md` §"Tables/migrations"). None of these exist on `assets` today — the shipped table carries only the seven columns above. Nothing in the current code (`do_compute_health`, `do_advance_lifecycle`) has anywhere to persist a health score or a warranty date even if it computed one.

**No enumerated CHECK on `lifecycle_state`** — deliberate: the 14-state vocabulary is config-as-IP (`asset-lifecycle.json`), so freezing it into DDL would make a config change require a migration. Only a structural non-blank CHECK is enforced; `lifecycle.py`'s `advance()` is the sole arbiter of which values are legal and in what order.

**No foreign key on `bom_line_id` or `functional_location_id`** — neither `BOM_LINE` nor `FUNCTIONAL_LOCATION` is a relational table in this repo (both are graph-node concepts); both columns are plain TEXT, reference-in-name-only.

**Grants:** `SELECT, INSERT, UPDATE`, never `DELETE` — retirement is the `RETIRED` lifecycle state, not a deleted row. `UPDATE` exists specifically for `do_advance_lifecycle`; `do_seed_asset_from_bom` never uses it (INSERT-only, migration 054's own table comment, lines 174-193).

### 2.2 `telemetry_samples` (migration 057)
```sql
CREATE TABLE IF NOT EXISTS telemetry_samples (
    id            UUID             NOT NULL DEFAULT gen_random_uuid(),
    namespace_id  UUID             NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    asset_id      UUID             NOT NULL,
    metric        TEXT             NOT NULL,
    value         DOUBLE PRECISION NOT NULL,
    sampled_at    TIMESTAMPTZ      NOT NULL,
    raw           JSONB            NOT NULL DEFAULT '{}'::jsonb,
    change_origin TEXT             NOT NULL DEFAULT 'agent',
    created_at    TIMESTAMPTZ      NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT telemetry_samples_asset_fk FOREIGN KEY (asset_id) REFERENCES assets (id) ON DELETE CASCADE,
    CONSTRAINT telemetry_samples_idempotency_uq
        UNIQUE (namespace_id, asset_id, metric, sampled_at),
    CONSTRAINT telemetry_samples_metric_not_blank CHECK (btrim(metric) <> ''),
    CONSTRAINT telemetry_samples_value_finite
        CHECK (value <> 'NaN'::float8 AND value <> 'Infinity'::float8 AND value <> '-Infinity'::float8),
    CONSTRAINT telemetry_samples_change_origin_check
        CHECK (change_origin IN ('sync','webhook','agent','operator','consolidation','replay','unknown'))
);
```
(`nce/migrations/057_telemetry_samples.sql:74-126`)

**The FK is single-column, not composite** — `telemetry_samples_asset_fk` proves `asset_id` exists in `assets`, but does **not** prove it belongs to the *row's own* `namespace_id`, because `assets` has no `UNIQUE (id, namespace_id)` for a composite FK to reference. What actually binds a sample to its tenant is FORCE RLS (below) plus `do_pull_telemetry`'s own namespace-scoped existence pre-check (`telemetry.py:389-404`, `_require_asset_in_namespace`) — a caller that bypasses `do_pull_telemetry` entirely and writes a raw INSERT could still, within its own namespace, point `asset_id` at another tenant's asset row. That is a mislabelled reference inside one tenant, not a cross-tenant *read* (RLS still stops that) — the migration's own header names this residual gap explicitly (`057_telemetry_samples.sql:32-53`) and does not close it.

**No `UPDATE` grant at all** — a telemetry reading, once taken, is never revised. **No `DELETE` grant** — retention/downsampling is named as a real future need but is not provisioned; nothing in this codebase deletes a sample.

**No index beyond the idempotency UNIQUE** — the migration's own comment (`057_telemetry_samples.sql:128-135`) says the "latest reading per metric" scan `do_compute_health` wants (`ORDER BY sampled_at DESC LIMIT 50`, `health.py:387-397`) has no dedicated `(namespace_id, asset_id, sampled_at DESC)` index; it is served by a sequential scan filtered through the unique index's leading columns today.

---

## 3. MCP tool registry

All 8 tools live in one contiguous block of `nce/tool_registry.py:971-1026`:

| Tool | Handler | cacheable | admin_only | mutation | Registry line |
|---|---|:---:|:---:|:---:|---|
| `assets_ping` | `handle_assets_ping` | ✔ | ✘ | ✘ | `971-976` |
| `assets_get` | `handle_assets_get` | ✔ | ✘ | ✘ | `985-990` |
| `assets_list` | `handle_assets_list` | ✔ | ✘ | ✘ | `991-996` |
| `assets_advance_lifecycle` | `handle_assets_advance_lifecycle` | ✘ | ✘ | ✔ | `997-1002` |
| `assets_seed_from_bom` | `handle_assets_seed_from_bom` | ✘ | ✘ | ✔ | `1003-1008` |
| `assets_pull_telemetry` | `handle_assets_pull_telemetry` | ✘ | **✔** | ✔ | `1009-1014` |
| `assets_attach_sla` | `handle_assets_attach_sla` | ✘ | ✘ | ✔ | `1015-1020` |
| `assets_compute_health` | `handle_assets_compute_health` | ✘ | ✘ | ✔ | `1021-1026` |

Handlers themselves are thin adapters in `nce/vertical_modules/assets/mcp_handlers.py` — every one wraps a `do_*` core one-for-one and adds no business logic (`mcp_handlers.py:83-401`).

**`admin_only` is an MCP-dispatch-layer concept only.** Per `ToolSpec`'s own docstring (`tool_registry.py:93-94`), `admin_only: True` makes the MCP dispatch loop call `_check_admin` (`nce/mcp_stdio_rpc.py:70`) before invoking the handler. `assets_pull_telemetry` is the only tool in this module flagged that way — matching the engine spec's AI-role column ("— (operator/cron)" rather than an Actor/Watcher role a normal caller would use). **The parallel REST route, `POST /api/assets/{id}/telemetry`, has no equivalent per-route admin check of its own** — `api_assets_pull_telemetry` (`nce/admin_handlers/assets.py:281-331`) runs the same generic checks every other Assets REST handler runs (engine connected, namespace UUID valid) and is gated only by the uniform middleware stack every `admin_app.py` route shares (mTLS / basic auth / HMAC, `nce/admin_app.py:134-160`). If you are relying on the MCP `admin_only` flag as your access-control boundary for telemetry pulls, know that the REST door into the same function does not re-derive that boundary — it inherits whatever the shared admin middleware already enforces for every route.

---

## 4. REST routes & handlers (`nce/admin_handlers/assets.py`)

Mounted in `nce/admin_app.py:951-990`. Literal paths (`/api/assets/seed-from-bom`, `/api/assets/sla/attach`, `/api/assets/health`) are registered **before** the `/api/assets/{id}` family — the file's own comment notes this order matters to avoid Starlette route shadowing (`admin_app.py:949-950`).

| Method & Path | Handler | Core | Success | Notes |
|---|---|---|---|---|
| `POST /api/assets/seed-from-bom` | `api_assets_seed_from_bom` | `do_seed_asset_from_bom` | 201 (created) / 200 (replay) | Bumps MCP cache generation on success. |
| `POST /api/assets/sla/attach` | `api_assets_attach_sla` | `do_attach_sla` | 200 | |
| `GET /api/assets/health` | `api_assets_health` | `do_compute_health` | 200 | `asset_id` via query param. |
| `GET /api/assets` | `api_assets_list` | `do_list_assets` | 200 | `functional_location_id`/`lifecycle_state` query filters. |
| `GET /api/assets/{id}` | `api_assets_get` | `do_get_asset` | 200 (incl. `asset: null`) | Absent asset is 200, never 404 — mirrors `api_product_get`. |
| `POST /api/assets/{id}/lifecycle` | `api_assets_advance_lifecycle` | `do_advance_lifecycle` | 200 / 404 / 409 | 404 if asset absent, 409 if the transition is business-illegal. |
| `POST /api/assets/{id}/telemetry` | `api_assets_pull_telemetry` | `do_pull_telemetry` | 200 | |
| `GET /api/assets/{id}/health` | `api_assets_health` | `do_compute_health` | 200 | Same handler as `/api/assets/health`; `id` path param takes precedence over an `asset_id` query param if both are given (`assets.py:408`). |

**Error mapping is uniform across every handler** (`assets.py:1-32` module docstring): missing/invalid `namespace_id` or path `id` → 422; a `ValueError` from the core (bad params, illegal state) → 422 *except* on the lifecycle-advance path, where a business-illegal transition is deliberately re-mapped to 409 and asset-absent to 404 (`assets.py:214-218`) rather than folding everything into 422; anything unexpected → 500 via `admin_error_response`.

**Every mutation bumps the MCP cache generation** (`bump_mcp_cache_generation`, `nce/admin_handlers/_shared.py:164`) after a successful REST write — `api_assets_advance_lifecycle`, `api_assets_seed_from_bom`, `api_assets_pull_telemetry`, and `api_assets_attach_sla` all call it (`assets.py:212,271,330,384`), so a cacheable read (`assets_get`/`assets_list`) cannot serve a pre-mutation row for the remainder of `MCP_CACHE_TTL_S` after any of these REST calls land.

**No extra JSON-serialisation pass is needed here** — unlike Inventory's handlers (which need `_json_safe` for `Decimal` quantities), every Assets `do_*` core already returns JSON-safe values: `_row_to_asset_dict` (`mcp_handlers.py:136-155`) normalises the UUID primary key to a string and both timestamps to ISO-8601 before any handler sees the row.

---

## 5. Graph contribution — coded, never called

`nce/vertical_modules/assets/graph.py` fully implements `project_asset_to_graph(conn, namespace_id, *, asset_id, bom_line_id, functional_location_id=None, namespace_slug=None)`, which would upsert:
- one `ASSET` `kg_nodes` row (`ASSET:<asset_id>`, uppercased),
- one `BOM_LINE -[installed_as]-> ASSET` edge, and
- (when a room is known) one `ASSET -[lives_in]-> FUNCTIONAL_LOCATION` edge.

**A repo-wide search for callers of `project_asset_to_graph` outside `graph.py` itself finds only two test files** (`tests/test_assets_graph.py`, `tests/test_bom_line_store.py`) — no `do_*` core, MCP handler, or REST handler in this module (or any other module) imports or calls it. **`do_seed_asset_from_bom` does not call it.** The function is fully coded, individually tested, and completely disconnected from every reachable surface. Treat `docs/vertical_engines/09-assets-engine.md`'s "Graph contribution" section (`ASSET`, the `installed_as`/`lives_in` edges) as describing code that exists in isolation, not a live data path — calling `assets_seed_from_bom` today writes only the relational `assets` row.

### 5.1 What the module docstring says about wiring it in
`graph.py`'s own module docstring (`graph.py:166-167`) states plainly that it "registers no MCP tool and mounts no REST route; wiring it to `do_seed_asset_from_bom` is a later wave's" — this is a self-declared, intentional gap, not a discovered one. Whoever picks up that wave should read the two subtleties below first, since both were found the hard way (module docstring, `graph.py:87-124`):

1. **The FL label needs the namespace slug, which the restricted `nce_app` role cannot read.** System Design's `FUNCTIONAL_LOCATION` labels embed the namespace slug (`FL:<SLUG>:<PATH>`), but `namespaces` grants **no privilege at all** to `nce_app` — verified against a real `nce_app` pool, not asserted. `project_asset_to_graph` therefore accepts an optional `namespace_slug` parameter specifically so an `nce_app`-role caller can skip the `namespaces` lookup; when omitted, the fallback SELECT raises `asyncpg.InsufficientPrivilegeError` rather than silently dropping the `lives_in` edge (dropping it would fabricate an asset with no room while reporting success). `agreements/sla.py:do_set_sla_coverage` has the identical defect and it is not fixed there either — it has simply never been hit, because that function is driven in CI only through the owner/superuser `pg_pool` fixture, which bypasses the privilege check entirely (`graph.py:110-120`).
2. **`assert_owner` is asymmetric by design.** Every `kg_nodes` write goes through `assert_owner` (`_upsert_asset_node`, one call site) — `ASSET`'s Contract-A ownership row (`{"node_type": "ASSET", "owner_engine": "assets", "transition": null}`) lives in `nce/config_data/node-ownership.json:45-46`, added specifically for this module. The `kg_edges` writes (`_upsert_edge`) do **not** go through `assert_owner` and must not: `kg_edges` has no foreign key to `kg_nodes`, and an edge naming a cross-engine endpoint (`BOM_LINE`, `FUNCTIONAL_LOCATION`) that this engine does not own is a legitimate forward assertion, not an ownership violation — the same rule `economy/graph.py` and `procurement/graph.py` already rely on for their own cross-engine edges.

---

## 6. Telemetry adapter architecture

`nce/vertical_modules/assets/telemetry.py` implements an abstract `TelemetryAdapter` (`platform` property + `fetch_samples(asset_id)`), selected exclusively through the one factory function `select_telemetry_adapter` (`telemetry.py:315-343`) — there is no `if platform == "crestron"` branch anywhere else in the module.

| Platform key | Vendor API a real adapter calls | Real adapter exists? |
|---|---|---|
| `mock` | — | **Yes** — default fixed epoch synthetic simulation |
| `crestron` | Crestron XiO Cloud REST API | **Yes** — `CrestronXiOCloudTelemetryAdapter` (<5s timeout, NVX/Flex status) |
| `neat` | Neat Pulse REST API | **Yes** — `NeatPulseTelemetryAdapter` (<5s timeout, air quality, people count) |
| `sennheiser` | Sennheiser Control Cockpit API | **Yes** — `SennheiserTelemetryAdapter` (<5s timeout, TCC2 beamforming, EW-DX) |
| `qsys` | Q-SYS Reflect Enterprise Manager API | **Yes** — `QSysReflectTelemetryAdapter` (<5s timeout, Core DSP load, PTP clock) |
| `shure` | Shure SystemOn / Cloud API | **Yes** — `ShureCloudTelemetryAdapter` (<5s timeout, MXA920 lobes, Dante clock) |
| `yealink` / `ymcs` | Yealink Management Cloud Service (YMCS) API | **Yes** — `YMCSTelemetryAdapter` (<5s timeout, MeetingBar/MVC telemetry) |
| `poly` | Poly Lens API | No — `UnimplementedVendorAdapter` |
| `huddly` | Huddly device API | No — `UnimplementedVendorAdapter` |

(`VENDOR_PLATFORMS`, `telemetry.py:154-164`)

**The swap flag is `NCE_ASSETS_TELEMETRY_<PLATFORM>_REAL`** (e.g. `NCE_ASSETS_TELEMETRY_CRESTRON_REAL`), read live via `nce.config.live_env_str` (never captured at import, so a runtime env change and `monkeypatch.setenv` in tests both take effect immediately — `telemetry.py:305-312`). Its default (unset) means mock; setting it swaps to `UnimplementedVendorAdapter`, which raises `NotImplementedError` rather than serving mock data — a deployment that flips the flag without a built adapter fails loudly instead of quietly lying about device state (`telemetry.py:265-292`). **There is no `httpx` import, no network call, and no credential-handling code anywhere in this file** — building any of the five real adapters (auth, HTTP client via `nce.http_resilience.request_with_retry`, vendor pagination) is entirely unbuilt, not partially built.

**The adapter call runs outside any database transaction, on purpose.** `do_pull_telemetry` opens two short `scoped_pg_session` blocks — a namespace-scoped existence pre-check, then the insert — with the (potentially slow, real-world) adapter call in between (`telemetry.py:62-70`). The accepted cost: an asset could in principle be deleted between the two checks, but the `ON DELETE CASCADE` FK turns that into a lost sample, not a corrupt row.

**Idempotency is by DB constraint, never check-then-write** — `telemetry_samples_idempotency_uq` on `(namespace_id, asset_id, metric, sampled_at)`, and the insert is a single set-based `INSERT ... SELECT ... FROM unnest(...) ON CONFLICT ... DO NOTHING RETURNING id` (`telemetry.py:407-443`) — `len(rows)` *is* the written count, with no second existence query. This matters because a telemetry pull is expected to be a cron re-reading overlapping windows: the same reading is expected to arrive repeatedly, and `MockTelemetryAdapter` deliberately returns timestamps derived from a **fixed epoch** (`2026-01-01`, not `now()`) so that re-pulling one asset is a genuine, testable replay rather than data that always looks new (`telemetry.py:219-256`).

---

## 7. Health scoring (`do_compute_health`)

Weights and thresholds load from `nce/vertical_modules/assets/asset-health-weights.json` (falling back to hard-coded defaults if the file is absent, `health.py:42-70`):

| Key | Value |
|---|---|
| `telemetry_weight` | `0.35` |
| `mtbf_weight` | `0.25` |
| `tickets_weight` | `0.20` |
| `age_weight` | `0.20` |
| `degraded_threshold` | `60.0` |
| `predictive_failure_threshold` | `0.60` |
| `expected_lifespan_days` | `1825` (5 years) |

`compute_asset_health` (`health.py:84-333`) is a pure reducer — zero DB, zero side effects — called by the async `do_compute_health` core, which is where the persistence happens:

1. **Reads** the asset row (raises if absent in this namespace, `health.py:377-381`), the 50 most recent `telemetry_samples` rows, and open `service_tickets` rows — the ticket read is wrapped in a bare `try/except Exception` that treats *any* failure (including the table simply not existing yet in an older migration state) as "no ticket data," never propagating the error (`health.py:409-425`). This is a deliberately soft degrade for a genuinely optional input, not a masked bug — but it also means a transient DB error on the tickets query is indistinguishable from "no open tickets" in the response.
2. **Computes** the fused score via `compute_asset_health`, honouring the RS-3 mock-telemetry suppression rule detailed in the user guide §4 (`health.py:276-296`).
3. **Persists** an `ACTIVE -> DEGRADED` transition on `assets.lifecycle_state` when the fused score drops below `degraded_threshold` (`health.py:443-457`) — this is the one place `do_compute_health` writes to the `assets` table; the health score number itself is never persisted anywhere (no such column exists, §2.1).
4. **Appends** an `asset_health_computed` event to `v3_cognitive_ledger` on a best-effort basis — the INSERT is wrapped in `try/except Exception: log.debug(...)` (`health.py:471-491`), so a ledger-write failure never blocks the health computation from returning a result to the caller.

---

## 8. SLA attachment (`do_attach_sla`)

Implements exactly one aspect of the engine spec's "4-way co-owned SLA" concept (`sla.py:1-116` module docstring): Agreements owns the terms, Economy the MRR, Support the running clock/breach state, Assets only the per-room coverage link. Mechanically:

- **Reads** `agreements/sla.py:get_sla_coverage(pool, namespace_id, agreement_id)` — read-only, the A2A seam into Agreements' own data.
- **Refuses** with `ValueError` if that read returns no `sla_terms` for the agreement — attaching coverage to an agreement Agreements has authored nothing for would fabricate coverage (`sla.py:289-292`).
- **Writes** one `kg_edges` row: `FUNCTIONAL_LOCATION -[covered_by]-> Agreement`, confidence `1.0` (structural, deterministic — not a scored match), via the standard `ON CONFLICT ... DO UPDATE` kg-upsert template.
- **No ownership check on this write** — `kg_edges` has no FK to `kg_nodes`, so this edge (naming a `FUNCTIONAL_LOCATION` endpoint Assets does not own) needs no `node-ownership.json` row, per the same rule §5.1 describes for the graph module's edges.
- **No `SLA` graph node is written, anywhere in this codebase** — confirmed by `nce/config_data/node-ownership.json` carrying no `SLA` row for the `assets` engine (the one `SLA` row that exists there belongs to `support`, a differently-scoped "operational runtime SLA profile," `node-ownership.json:62-63` — not the coverage-link concept this module implements).
- Same `nce_app`-cannot-read-`namespaces` privilege gotcha as §5.1 applies here too: pass `namespace_slug` explicitly when calling under the restricted role, or the fallback lookup raises (`sla.py:208-224`).

`do_attach_sla` registers no MCP tool or REST route of its own docstring-declared scope statement — that statement is now stale: Phase 1 of ML9b (branch `ml9b/assets-completion`) wired it to `assets_attach_sla` / `POST /api/assets/sla/attach`, both confirmed present in §3/§4 above.

---

## 9. Autonomy & governance — what the code actually enforces

A repository-wide search for `governed`/`@governed` inside `nce/vertical_modules/assets/` returns nothing — there is no C2-style human-in-the-loop confirmation gate anywhere in this module, matching Sales' posture rather than Product's `@governed` pattern.

| Operation | What actually gates it |
|---|---|
| `assets_seed_from_bom` | DB constraint idempotency only (`assets_ns_bom_line_uq`) — no approval step. |
| `assets_advance_lifecycle` | The pure state machine's legal-edge check (`ok: false` on an illegal hop) — a business-rule refusal, not a governance gate. |
| `assets_pull_telemetry` | `admin_only=True` at the MCP dispatch layer only (§3) — the REST route shares the generic admin middleware, nothing telemetry-specific. |
| `assets_attach_sla` | Fails loudly if Agreements has no terms on record — a data-integrity check, not an approval workflow. |
| `assets_compute_health` | None — a DEGRADED transition it triggers is applied immediately, with no confirm-before-write step. |

**Practical implication:** every Assets write path is immediately effective on call. There is no pending/confirm step, no idempotency-key + governor-cache pattern (Product's model), and no audit trail beyond the best-effort `v3_cognitive_ledger` append from `do_compute_health` — `do_seed_asset_from_bom` and `do_advance_lifecycle` write no ledger row at all.

---

## 10. Operational notes

- **Tenant provisioning:** there is no enablement step to run (§1) — the engine is live for every namespace as soon as the code is deployed. If your fleet workflow depends on a room having a `FUNCTIONAL_LOCATION` before seeding, that dependency is not enforced by this module: `functional_location_id` is a nullable, unvalidated TEXT column.
- **Telemetry pulls today are a smoke test, not a monitoring feed** (§6) — do not build alerting on top of `assets_pull_telemetry` output expecting real device state until at least one vendor adapter is built and its `NCE_ASSETS_TELEMETRY_<PLATFORM>_REAL` flag is flipped.
- **Predictive-failure alerts will not fire** until real (non-mock) telemetry exists — see the user guide §4. This is a structural consequence of §6, not a configuration you can turn on today.
- **The graph is not populated by any reachable code path** (§5) — if a downstream engine or dashboard expects to find `ASSET` nodes or `installed_as`/`lives_in` edges in `kg_nodes`/`kg_edges`, they are not there until `project_asset_to_graph` is wired into `do_seed_asset_from_bom` by a future wave.

---

## Appendix: Spec vs. Shipped Matrix (delta from `docs/vertical_engines/09-assets-engine.md`)

| Feature / Capability | Spec Proposal | Shipped State (main @ `b75c873`) | Notes |
|---|---|---|---|
| **MCP: `assets_seed_from_bom`** | Actor | **Shipped** | §3, §4 |
| **MCP: `assets_advance_lifecycle`** | Actor | **Shipped** | No ledger write (spec says "logs transition to ledger"; code doesn't). |
| **MCP: `assets_pull_telemetry`** | — (operator/cron) | **Shipped** | Mock-only; 5 vendor adapters unbuilt (§6). |
| **MCP: `assets_compute_health`** | Watcher | **Shipped** | Health score not persisted; predictive-failure never fires on mock data (§7). |
| **MCP: `assets_attach_sla`** | Actor | **Shipped** | §8. |
| **MCP: `assets_check_warranty_eol`** | Watcher | *Does not exist* | No `do_check_warranty_eol` function anywhere in the repo. |
| **MCP: `assets_recommend_replacement`** | Advisor | *Does not exist* | No `do_recommend_replacement` function anywhere in the repo. |
| **MCP: `assets_generate_qr`** | — (utility) | *Does not exist* | No `do_generate_asset_qr` function anywhere in the repo. |
| **MCP: `assets_export_dpp`** | — (compliance) | *Does not exist* | No `do_export_dpp` function anywhere in the repo. |
| **MCP: `assets_sync_netbox`** | — (operator; bridge) | *Does not exist* | No `do_sync_netbox` function, no `assets/netbox_bridge.py` file. |
| **REST: `/api/assets/register`, `/warranty-eol`, `/qr`, `/dpp`, `/sync-netbox`** | GET/POST | *Does not exist* | Not mounted; the underlying cores don't exist to mount. |
| **Graph: `ASSET` node + `installed_as`/`lives_in` edges** | On seed | **Coded, never called** (§5) | `project_asset_to_graph` exists and is tested in isolation only. |
| **Graph: `ASSET -[monitored_by]-> TELEMETRY`** | On pull | *Does not exist* | `telemetry.py`'s own docstring declares this out of scope; `TELEMETRY` has no `node-ownership.json` row. |
| **Graph: `ASSET -[maps_to]-> netbox_device`** | Bridge | *Does not exist* | No bridge module, no `asset_netbox_mappings` table. |
| **Graph: `ASSET -[failure_pattern]-> PRODUCT`** | Producer signal | *Does not exist* | Depends on `do_recommend_replacement`, which does not exist. |
| **SQL: `assets` (054)** | Table | **Shipped**, narrower than spec | Missing `health_score`, `warranty_until`, `firmware`, `mac`, `ip`, `dpp`, `monitoring_platform`, `netbox_device_id`, `assets_source_id` columns (§2.1). |
| **SQL: `telemetry_samples` (057)** | Table | **Shipped** | As spec'd; no dedicated `(namespace_id, asset_id, sampled_at DESC)` index yet. |
| **SQL: `asset_netbox_mappings`** | Table | *Does not exist* | Depends on the NetBox bridge, which does not exist. |
| **Config: `NCE_ASSETS_*` (6 keys)** | env vars | *None exist* | Zero `NCE_ASSETS_*` entries in `nce/config.py` (§1) — except the telemetry env-swap family, which the spec also names. |
| **Config: `asset-lifecycle.json`** | JSON | **Shipped** | `nce/config_data/asset-lifecycle.json`. |
| **Config: `asset-health-weights.json`** | JSON | **Shipped**, non-standard location | Lives in `nce/vertical_modules/assets/`, not `nce/config_data/` (§1). |
