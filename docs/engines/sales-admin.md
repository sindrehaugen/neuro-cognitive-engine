> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# Sales Engine Admin Guide (Doc 74)

> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

This guide documents how to enable, configure, and operate the Sales Engine (`nce/vertical_modules/sales/`): the D365 source adapter and watermark sync, the C5 source-mode resolver (`d365|both|nce`) and its divergence/flip-gate machinery, the public-quote redaction surface, the C7 signing ceremony wiring, DealRoom & Sales baseline event stream / WORM posture, RLS/migrations, and exactly what governance the code enforces around Sales' AI and write paths. Every claim below is grounded in a specific file/line on `main @ 7304330`; where the design spec (`docs/vertical_engines/05-sales-engine.md`) promises more than ships, this guide says so.

---

## 1. Enablement — there is currently no opt-in gate

> [!WARNING]
> Unlike the Product engine (`require_product_enabled` / `metadata.product.enabled`, see `docs/engines/product-admin.md` §1.2), **the Sales engine has no namespace-level enablement check anywhere in the codebase.** There is no `NCE_SALES_ENABLED` config var (`nce/config.py` has zero `NCE_SALES_*` entries), no `metadata.sales.enabled` guard, and no `require_sales_enabled`/`SalesDisabledError` equivalent to Product's `_guard.py`. Every namespace that can reach the REST routes in `nce/admin_handlers/sales.py` / `nce/admin_handlers/sales_public.py`, or the `sales_get_signed_baseline` MCP tool, can use the Sales engine. If you need per-tenant opt-in today, it must be enforced at your reverse proxy / API gateway layer, or added to the codebase — it is not there yet.

The only "enablement" surface that does exist is **per-function source-mode configuration** (§2) — which controls *where* a function's data comes from, not *whether* Sales is active for a tenant.

There is likewise no `nce/vertical_modules/sales/_guard.py` (Product's pattern) — confirmed absent from the directory listing of `nce/vertical_modules/sales/`.

---

## 2. C5 Source-Mode Resolver — `d365 | both | nce`

Sales is the flagship consumer of the shared C5 source-mode resolver (`nce.source_mode`, fully documented in `docs/shared-core/source-mode-divergence.md`, which supersedes the original `docs/DATA_SOURCE_MODES.md` settings-table design). This section covers the Sales-specific wiring on top of that shared component.

### 2.1 Modes and dispatch
Every Sales read function (`nce/vertical_modules/sales/source_mode.py`) calls `resolve(pool, engine="sales", function=<name>, namespace_id=...)` to get the active `SourceMode`, then dispatches through `read_through(mode, native_reader=..., external_reader=..., parity_check=...)`:

| Mode | Native read? | External (D365) read? | Parity check runs? |
|---|:---:|:---:|:---:|
| `d365` (default when unconfigured) | No | Yes | No |
| `both` | Yes | Yes | Yes — logs any divergence |
| `nce` | Yes | No | No |

Writes go through `write_route(mode, native_writer=..., external_writer=...)` in `nce/vertical_modules/sales/write_routing.py` — `d365` writes external only, `both` writes both (write-through), `nce` writes native only.

**Functions covered today** (each independently configurable): `list_customers`, `customer_profile`, `sales_overview`, `seller_detail`, `sales_dashboard`, `sales_stats`, `sales_manager`, `list_agreements`, `agreement_detail`, `quote_detail` (reads, `source_mode.py`), plus `create_deal`, `edit_deal` (writes, `write_routing.py`).

### 2.2 Admin control surface
- `GET /api/admin/sales/source-mode?namespace_id=...` (`nce/admin_handlers/sales.py:70-122`) — returns `{namespace_id, engine: "sales", modes: {function: mode, ...}}` by reading `source_mode_config` directly.
- `PUT /api/admin/sales/source-mode` (`sales.py:130-247`) — body `{namespace_id, function, mode}`. Validates `function` against a **hardcoded allow-list of 10 names** (`sales.py:170-181` — the same 10 read functions listed above; note `create_deal`/`edit_deal` are *not* in this PUT allow-list, so write-routing modes cannot currently be changed through this admin endpoint). Validates `mode ∈ {"d365","both","nce"}`.
  - **Flip-gate check on `mode == "nce"`:** calls `flip_blocked(pool, namespace_id=ns, engine="sales", window_seconds=3600.0)` (`sales.py:198-204`) — a **hardcoded 1-hour lookback window**. If any `divergence_log` row exists for `(namespace, "sales")` within that hour, the PUT is rejected with `400 {"error": "Flip to nce mode is blocked due to recent divergences"}`.
  - On success, upserts `source_mode_config (namespace_id, engine='sales', function, mode)`.

### 2.3 A second, independent flip path exists: `do_flip_function`
`nce/vertical_modules/sales/flip.py:24-87` implements `do_flip_function(engine, params)` with params `namespace_id`, `function`, `window_days` (default `7`). It queries `divergence_log` **directly** (not via the shared `flip_blocked()` helper) for rows in the last `window_days` days, and if any exist, returns `{"ok": false, "reason": "...", "divergences_count": N}` without touching `source_mode_config`. If clean, it upserts `source_mode_config` to `mode='nce'` itself.

> [!IMPORTANT]
> **This is a real duplication, not just an alternate call style.** `api_sales_source_mode_put` (§2.2) and `do_flip_function` (§2.3) are two independently-coded paths to the same effect (flip a function to `nce`), with **different default lookback windows** (1 hour vs 7 days) and different query paths (shared `flip_blocked()` vs. an inline `COUNT(*)` query). Neither calls the other. `do_flip_function` is not wired to any REST route or MCP tool in this snapshot — it is reachable only by direct Python call. If you automate flips, know which path you are invoking and which window applies.

### 2.4 Divergence logging (`log_sales_divergence`)
`nce/vertical_modules/sales/source_mode.py:32-69` wraps the shared `record_divergence()` (see `docs/shared-core/source-mode-divergence.md` §4) with Sales-specific comparison logic:
- String values are compared as-is; if `is_numeric=True`, both sides are cast to `float` and a **materiality ratio** is computed as `abs(n - e) / max(abs(n), abs(e), 1.0)` (`:48-58`) before being passed to `record_divergence`, which alerts if materiality exceeds `NCE_DIVERGENCE_ALERT_THRESHOLD` (default `0.10`).
- Each source-mode function wires its own field-level parity checks — e.g. `do_list_customers` diffs `name`/`address1_city` per account and logs existence divergence for accounts D365 has that NCE doesn't (`source_mode.py:191-229`); `do_sales_dashboard` diffs `pipeline.openValue` (`:446-457`); `do_sales_stats` diffs `coverage.n` (`:482-491`). These are **spot-check fields per function**, not a full-row diff — a function can report "no divergence" while other unchecked fields silently differ.

Divergences land in `divergence_log` (shared table, not a Sales-specific `sales_divergence_log` — see the naming note in §7). This table is append-only: `nce_app` is granted `SELECT, INSERT` only (`nce/schema.sql:2100-2105`).

---

## 3. D365 Source Adapter (`source_adapters/d365.py`)

`nce/vertical_modules/sales/source_adapters/d365.py` syncs Dataverse entities into `sales_read_model`, reusing the shared `dynamics365` vertical's `DataverseClient`/`DataverseTokenManager` — Sales does not re-declare D365 OAuth config (`nce/vertical_modules/sales/source_mode.py:77-92`, `get_d365_client`).

### 3.1 Entity coverage and field projection
Ten Dataverse entity sets are mapped (`d365.py:36-47`): `accounts`, `contacts`, `opportunities`, `quotes`, `agreements` (→ `msdyn_agreements`), `systemusers`, `incidents`, `appointments`, `customerassets` (→ `msdyn_customerassets`), `functionallocations` (→ `msdyn_functionallocations`). Each has its own primary-key field, display-name field, and **explicit `_SELECT_FIELDS` projection** (`d365.py:75-120+`) — only listed OData fields are pulled. Notably, `quotes` projects only `["quoteid", "name", "statecode", "modifiedon"]` (`d365.py:101`) — no price, line-item, or margin fields are synced from D365 at all; any such detail on a quote comes from local `manual` enrichment, not the adapter.

### 3.2 `get_d365_client` fail-open-to-fallback behavior
`get_d365_client(engine)` (`source_mode.py:77-92`) returns `None` (not an exception) if `cfg.NCE_D365_ORG_URL` is unset or `engine.redis_client` is not initialized, and logs+swallows any exception from `DataverseTokenManager`/`DataverseClient` construction. Every `external_reader()` in `source_mode.py` checks for a `None` client and **falls back to querying `sales_read_model.source_json` locally** instead of hitting D365 (e.g. `do_list_customers`'s `external_reader`, `source_mode.py:114-189`). This means a `both`-mode read with a broken D365 connection does not fail — it silently compares NCE's native data against its own previously-synced mirror, which will trivially show zero divergence. Operationally: **a `both`-mode namespace with a dead D365 credential will look like "clean parity" in the divergence log**, because the "external" read degraded to the same local table. Verify `NCE_D365_ORG_URL`/Redis connectivity independently before trusting a clean parity window as proof.

### 3.3 Watermark / incremental sync
The adapter reuses `CURSOR_OVERLAP_SECONDS` from `nce.vertical_modules.dynamics365.client` (`d365.py:19`) for delta-cursor overlap, consistent with the shared D365 vertical's incremental-sync pattern; parsing uses `parse_datetime()` (`d365.py:24-32`) to normalize OData ISO-8601 `modifiedon` timestamps to UTC.

---

## 4. Public-Quote Redaction (C8)

The public customer-facing quote surface (`GET /public-api/sales/quotes/{id}`, `nce/admin_handlers/sales_public.py`) is Sales' implementation of the shared C8 allow-list redactor. Full C8 contract, default-deny posture, and `UnknownSurfaceError` behavior are documented in `docs/shared-core/redaction.md` §1-2 — this section covers only the Sales-specific wiring.

- **Config file:** `nce/config_data/redaction/public-quote-redaction.json` — allow-list: `id, node_type, label, description, category, manufacturer, model, part_number, quantity, unit_price, currency, lead_time_days, availability, tags, namespace_id`.
- **Call site:** `project(quote_detail, "public-quote")` (`sales_public.py:94`), applied to the result of the *internal* `do_quote_detail` read (which can include `cost`/`margin`/`commission` if present in the merged quote record).
- **Defense-in-depth check:** after redaction, the handler explicitly asserts `cost`, `margin`, `commission`, `internal-status` are absent and deletes them (logging `ERROR`) if they somehow survive (`sales_public.py:99-105`) — belt-and-suspenders on top of the allow-list, not a substitute for it.
- **Auth:** stateless HMAC token (`HMAC-SHA256(NCE_MASTER_KEY, quote_id)`, `sales_public.py:27-31`), constant-time compared. **This token is deterministic per `quote_id`** — anyone who derives or leaks it can access that quote indefinitely (there is no expiry or per-session nonce in the token itself). Rate limiting (5 req / 10 s per token via Redis) is the only mitigation against brute-force/replay in this snapshot.
- **Field-shape caveat (see the D365 sync note in §3.1):** because the allow-list expects fields like `manufacturer`, `unit_price`, `category` that are never populated by the D365 `quotes` sync, a quote that has not been separately enriched (via DealRoom or manual edits) will redact down to a near-empty payload on this surface. This is correct default-deny behavior, but worth knowing operationally when triaging a "customer says the quote link is blank" report.

---

## 5. Signing Ceremony Wiring (C7 `SignTransport`)

Full C7 `SignTransport` protocol, the fire-and-pull anti-spoofing pattern, and `ManualTransport` semantics are documented in `docs/shared-core/pricing-signing-grounding.md` §2. Sales-specific wiring:

- `nce/vertical_modules/sales/signing.py:24` instantiates a **module-level singleton** `_transport = ManualTransport()`. In this codebase snapshot, Sales is wired to the manual (zero-credential, in-memory) transport only — there is no `oneflow`/`criipto`/`signicat` transport instance constructed or selected at runtime in `signing.py`, even though `do_request_signature` accepts and validates those method names (`signing.py:55-57`, raises `ValueError` for anything outside `{"oneflow","criipto","signicat","manual"}`). Passing `method="oneflow"` today will still route through `_transport` (the `ManualTransport` instance) because `tm_method` is passed to `_transport.request_signature(...)`, not used to select a different transport object. **Do not treat `method` as functional provider selection until a per-method transport factory is wired in** — flagged as drift below.
- The freeze-triggering callback, `do_on_signed_callback`, is idempotent per `(quote_id, session_id)`: it checks `manual.signing_status == "signed"` before re-running the freeze+convert sequence (`signing.py:157-168`).
- **Money-guard validation on signing (PR #56):** `do_on_signed_callback` enforces `_require_money_field` across commercial amounts. Missing margin or total fields, booleans, non-numeric strings, `NaN`/`Inf`, and out-of-range figures raise `MissingSignedAmountError` rather than freezing fabricated defaults.
- Signing state (`signing_session_id`, `signing_status`, `signing_fingerprint`, `signer_name`) lives in `sales_read_model.manual` (JSONB), not a separate table.

---

## 6. Event Wiring & WORM Audit Trail (DealRoom, Baseline, AI Decisions)

### 6.1 DealRoom Event Posture (`dealroom.py`)
`do_open_dealroom` materializes the live interactive DealRoom web quote:
- Reads quote details from `sales_read_model` (`entity = 'quotes'`).
- Discovers BOM lines in `kg_nodes` with literal prefix matching `starts_with(label, 'BOM_LINE:{QUOTE}:')` (PR #67 / PR #76), preventing cross-quote line matching when quote IDs contain SQL `LIKE` metacharacters (`_` or `%`).
- Fetches and updates option toggle overrides (`toggled: bool`) directly within MongoDB episode payloads (`memory_archive.episodes`).
- Evaluates line pricing via the shared C6 pricing engine (`resolve_price()`).
- **Event stream posture:** DealRoom is an ephemeral presentation and price-resolution surface; it does **not** append events to `event_log` or emit C4 outbox messages upon viewing or option toggling.

### 6.2 Sales Baseline Freeze WORM Posture (`baseline.py` / `signing.py`)
- `sales_signed_baselines` is a dedicated, append-only WORM table protected by Postgres RLS (`tenant_isolation_policy` on `namespace_id = get_nce_namespace()`) with grants restricted to `SELECT, INSERT` only (no `UPDATE`/`DELETE` for `nce_app`, enforced at the database level).
- Unique constraint `UNIQUE (namespace_id, quote_id)` structurally enforces "at most one baseline per quote" independently of application logic.
- `do_freeze_baseline` is idempotent: returns `already_frozen: True` if the row already exists.
- Guarded by `_require_money_field` (PR #56) to ensure `signed_margin_pct` (clamped `[0.0, 1.0]`) and `signed_total_nok` (`>= 0.0`) are grounded in actual quote data — missing amounts raise `MissingSignedAmountError` rather than freezing fabricated figures.
- **Event stream posture:** The baseline freeze writes directly to `sales_signed_baselines` (which acts as the immutable audit table). It does not append a separate `event_log` row. The signing callback (`do_on_signed_callback`) immediately triggers `do_convert_signed_quote` in the Project engine, which initializes the `PROJECT_PROJECT` node and G0 gate (subsequent phase transitions emit `project_phase_advanced` to `event_log`).

### 6.3 Sales AI Decision Events (`sales/ai.py`)
- `do_record_ai_decision` logs human/agent AI decisions to the ledger by calling `append_event(conn, namespace_id, agent_id="sales-ai-agent", event_type="sales_ai_decision", params={"decision_type", "details"})`.
- `sales_ai_decision` is a first-class member of `EventType` (`nce/event_types.py:72`).
- Advisory AI functions (`do_score_lead`, `do_draft_quote`, `do_win_loss_recall`) perform read-only vector cosine-similarity queries over the `memories` table (`embedding <=> $1::vector`), returning proposals with `propose_only: True` and zero ledger side effects.

### 6.4 D365 Integration Events
- `d365_sla_breach` events are appended to `event_log` when Dataverse incidents breach SLAs, enforcing required payload keys `incident_id`, `breach_type`, `account_name`, `memory_id`, `mongo_id` (`event_types.py:85-93`).

---

## 7. RLS Tables & Migrations

### 7.1 Tables
| Table | Migration | RLS | Grants to `nce_app` |
|---|---|---|---|
| `sales_read_model` | `040_sales_read_model.sql` | `ENABLE` + `FORCE`, `tenant_isolation_policy` on `namespace_id = get_nce_namespace()` | `SELECT, INSERT, UPDATE, DELETE` |
| `sales_targets` | `040_sales_read_model.sql` | same pattern | `SELECT, INSERT, UPDATE, DELETE` |
| `sales_signed_baselines` | `041_sales_signed_baselines.sql` | same pattern | **`SELECT, INSERT` only** — no `UPDATE`/`DELETE` (the WORM enforcement; see user guide §3.2) |

All three are also enrolled in the generic tenant-table RLS loop in `nce/schema.sql:2073-2115` (the mirror-into-schema.sql step), which re-derives the same `tenant_isolation_policy` and grant set — `sales_signed_baselines` is explicitly named in the append-only branch of that loop (`schema.sql:2101`) alongside `event_log`, `event_parents`, `divergence_log`.

`sales_signed_baselines` additionally grants `USAGE, SELECT` on its `BIGSERIAL` sequence (`sales_signed_baselines_id_seq`) to `nce_app` — required for `INSERT ... RETURNING id` to work without granting table-level `UPDATE` (`041_sales_signed_baselines.sql:32`, `schema.sql:2112-2114`).

### 7.2 Natural keys
- `sales_read_model`: `UNIQUE (namespace_id, entity, source_id)` — one row per D365 record per namespace, regardless of source.
- `sales_signed_baselines`: `UNIQUE (namespace_id, quote_id)` — structurally enforces "at most one baseline per quote," independent of the app-level idempotency check in `baseline.py`.
- `sales_targets`: composite primary key `(namespace_id, owner_slug, metric)`.

### 7.3 Shared tables Sales depends on but does not own
`source_mode_config` (migration `030_c5_source_mode_config.sql`) and `divergence_log` (migration `031_c5_divergence_log.sql`) are shared C5 infrastructure, not Sales-specific migrations — see `docs/shared-core/source-mode-divergence.md` §2 for their full schema. Sales is simply a tenant of `engine='sales'` rows in both.

### 7.4 Graph ownership (Contract A)
`nce/config_data/node-ownership.json` assigns `owner_engine: "sales"` to node types `CUSTOMER`, `LEAD`, `OPPORTUNITY`, `DEAL`, `QUOTE`, `SIGNED_BASELINE` (lines 19-24). Every graph write in `nce/vertical_modules/sales/graph.py` calls `assert_owner(conn, namespace_id, entity_type, "sales")` before upserting a node (`graph.py:93`), enforcing that only Sales code can claim these node types in a given namespace.

---

## 8. Autonomy / Governance — what the code actually enforces

> [!CAUTION]
> **There is no C2 `@governed` gate anywhere in `nce/vertical_modules/sales/`.** A repository-wide check for `governed`/`@governed`/`autonomy` imports in the Sales module returns nothing. This is a materially different posture from the Product engine, where `do_enrich_product` is wrapped in `@governed(action_type="product_enrich")` enforcing confirm-before-write, idempotency-key replay protection, and an audit trail (see `docs/engines/product-admin.md` §6). **No Sales function — including the write-routing paths (`do_create_deal`, `do_edit_deal`) and the signing/freeze path (`do_request_signature`, `do_on_signed_callback`, `do_freeze_baseline`) — passes through an equivalent human-in-the-loop confirmation gate.**

What *is* true, structurally:
- **The signed-baseline freeze is append-only by database grant** (§7.1) — this is a real, DB-enforced ceiling: no code path, governed or not, can `UPDATE`/`DELETE` a frozen baseline, because `nce_app` lacks the privilege at the Postgres level. This is the one place Sales' governance is enforced by the database rather than by application logic.
- **The AI advisory functions (`do_score_lead`, `do_draft_quote`) are read-only over `memories`** — they compute and return a proposal (`propose_only: True` in the response dict) but perform no writes themselves. This gives them a de facto propose-only posture, but it is a property of what the function happens to do, not a decorator or governor that would block a hypothetical write. If a future change added a write to either function, nothing in the current code would stop it from executing unconfirmed.
- **Idempotency on the signing/freeze path is hand-rolled, not governor-provided.** `do_freeze_baseline` checks for an existing row before inserting (`baseline.py:129-150`); `do_on_signed_callback` checks `manual.signing_status` before re-running (`signing.py:157-168`). These are correct and tested (`tests/test_sales_signed_baseline.py`, `tests/test_sales_sign_to_project.py`), but they are bespoke idempotency checks per function, not the shared `idempotency_key` + governor-cache pattern Product uses.
- **Write-routing enforces identity collision-safety, not approval.** `do_edit_deal` (`write_routing.py:101-177`) blocks editing a non-`nce:`-prefixed (i.e., D365-sourced) deal ID natively unless a matching `kg_nodes` row already exists (`:128-147`) — this prevents silent ID collisions between D365 and native records, but it is a data-integrity check, not a governance/approval gate.

**Practical implication for operators:** treat every Sales write path (`do_create_deal`, `do_edit_deal`, `do_request_signature`, `do_on_signed_callback`, `do_flip_function`, the source-mode PUT endpoint) as **immediately effective on call** — there is no pending/confirm step to intercept before it runs, except the one flip-gate check in §2.2/§2.3 (which blocks on divergence, not on human approval) and whatever your own caller/orchestrator chooses to impose before invoking these functions.

---

## 9. Config keys reference

Contrary to the design spec's `NCE_SALES_*` family, the **only** environment configuration Sales actually reads is inherited from shared components:
- `NCE_D365_ORG_URL`, `NCE_D365_ENABLED`, `NCE_D365_WEBHOOK_SECRET` — shared `dynamics365` vertical config (`nce/config.py:921,926`), reused by `get_d365_client()`; `NCE_D365_ENABLED=true` in production requires `NCE_D365_ORG_URL` and `NCE_D365_WEBHOOK_SECRET` to be set or boot fails (`config.py:1258-1271`, `validate_d365_config`).
- `NCE_MASTER_KEY` — used directly by `generate_public_token()` for the public-quote HMAC (`sales_public.py:29`); this is the same master key used engine-wide for secrets/signing-key encryption, not a Sales-specific secret.
- `NCE_DIVERGENCE_ALERT_THRESHOLD` (default `0.10`) — shared C5 config consumed via `record_divergence()`, not read directly by Sales code.
- `NCE_PRICING_MAX_AGE` (default `86400`) — shared C6 config consumed via `resolve_price()` inside DealRoom, not Sales-specific.

**None** of `NCE_SALES_ENABLED`, `NCE_SALES_D365_ADAPTER_ENABLED`, `NCE_SALES_SYNC_INTERVAL_MINUTES`, `NCE_SALES_PAGE_SIZE`, `NCE_SALES_SIGNING_PROVIDER`, `NCE_SALES_SIGNING_WEBHOOK_SECRET`, `NCE_SALES_DEALROOM_PUBLIC_BASE_URL`, or `NCE_SALES_SLIP_DAYS` from the design spec exist in `nce/config.py` as of this audit — *(planned — not yet implemented)*.

**Config-as-IP JSON that does exist:** `nce/config_data/sales-commission.json` (versioned DB-weighted commission tiers, `"version": "1.0"`, 3 margin brackets, `hardware_rate`/`service_rate` per bracket — see user guide §7). The spec's `sales-pipeline-stages.json` (stage definitions + slip thresholds + lead-scoring weights) does **not** exist in `nce/config_data/` — *(planned — not yet implemented)*; `do_stalled_deal_watcher` (`flip.py:90-145`) instead takes `slip_days` as a plain function parameter (default `30`), not a config file.

---

## Appendix: drift/bugs flagged during this audit (not fixed)

1. **`method` param on `do_request_signature` doesn't select a transport.** All methods (`oneflow`, `criipto`, `signicat`, `manual`) route through the single module-level `ManualTransport()` instance (§5). Passing `method="oneflow"` silently behaves identically to `"manual"`.
2. **Two independent flip-gate implementations** with different default windows (1 hour via the admin PUT route vs. 7 days via `do_flip_function`) and different query paths (§2.3).
3. **`both`-mode D365 outage degrades to comparing NCE against itself**, producing false "clean parity" (§3.2) — operationally dangerous if used as the sole gate for a `both→nce` flip decision.
4. **Public-quote allow-list field names don't match the D365 sync's actual field names**, so an un-enriched, D365-sourced-only quote renders near-empty on the public surface (§3.1, §4).
5. **No governance gate on any Sales write or AI-propose path** (§8) — a structural gap relative to the Product engine's `@governed` pattern, worth closing before Sales AI features are extended to perform writes.
