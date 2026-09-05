> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# Sales Engine User Guide (Doc 73)

> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

The **Sales Engine** (`nce/vertical_modules/sales/`) owns the deal lifecycle — lead/opportunity/deal → quote → DealRoom → signature — and is the single place a **signed baseline** is frozen for a quote. This guide documents the surfaces that actually exist in code today: the tenant-isolated read-model (mirrored from Dynamics 365), the DealRoom pricing recompute, the signing-and-freeze flow, the public customer-facing quote link, and the one MCP tool other engines use to read the frozen baseline. Where the design spec (`docs/vertical_engines/05-sales-engine.md`) describes more than is built, this guide says so explicitly.

> [!IMPORTANT]
> Only **two** Sales functions are registered as MCP tools today: `sales_ping` and `sales_get_signed_baseline` (`nce/tool_registry.py:573-589`). Everything else described below — customer/quote/dashboard reads, DealRoom, signing, commission — is real, tested, `do_*`-callable code, but it is reached through **REST admin routes** (`nce/admin_handlers/sales.py`, `nce/admin_handlers/sales_public.py`), not MCP. If you are integrating an AI agent via MCP, `sales_get_signed_baseline` is the only Sales-specific tool you can call today; the rest of this guide describes the underlying engine behavior for anyone building against the REST surface.

---

## 1. Lead → Opportunity → Deal → Quote lifecycle

### 1.1 Read-model (mirrored from D365)
Sales does not (yet) run a fully independent pipeline UI. Its primary data source is `sales_read_model`, a tenant-isolated table that mirrors Dynamics 365/Dataverse entities (`accounts`, `contacts`, `opportunities`, `quotes`, `agreements`, `systemusers`, `incidents`, `appointments`, `customerassets`, `functionallocations`) — see `nce/vertical_modules/sales/source_adapters/d365.py:36-47`. Each row carries `source_json` (the raw D365 payload) and `manual` (local enrichment); readers merge the two, with `manual` taking precedence (`nce/vertical_modules/sales/read_model.py:121-131`, `_merged()`).

The **D365 `quotes` sync only pulls four fields**: `quoteid`, `name`, `statecode`, `modifiedon` (`nce/vertical_modules/sales/source_adapters/d365.py:101`). Any commercial detail you see on a quote (price, manufacturer, line items) comes from `manual` enrichment written locally (e.g. via DealRoom or signing), not from the D365 mirror itself.

### 1.2 Native graph writes
Sales also writes a small pipeline graph directly (`nce/vertical_modules/sales/graph.py`): `CUSTOMER -[has]-> LEAD -[qualifies_into]-> OPPORTUNITY -[becomes]-> DEAL -[priced_as]-> QUOTE`, plus `QUOTE -[freezes]-> SIGNED_BASELINE` (label only — the freeze itself lives in a dedicated table, see §3). `do_create_deal` / `do_edit_deal` (`nce/vertical_modules/sales/write_routing.py:26-177`) drive these upserts. Every node/edge write asserts ownership via `assert_owner()` before touching `kg_nodes`/`kg_edges` (`nce/vertical_modules/sales/graph.py:93`).

### 1.3 Source-mode-aware reads
Every list/detail read goes through the C5 source-mode resolver (`nce/vertical_modules/sales/source_mode.py`), which picks `d365` | `both` | `nce` per `(namespace, function)` — see §2 of the admin guide for how modes are configured. In `both` mode, the resolver diffs the native and D365 answers and logs any mismatch to `divergence_log` (`nce/vertical_modules/sales/source_mode.py:32-69`, `log_sales_divergence`).

Read functions available (source-mode-aware, `nce/vertical_modules/sales/source_mode.py`):

| Function | REST route | Purpose |
|---|---|---|
| `do_list_customers` | `GET /api/sales/customers` | Company list, filterable by `q` |
| `do_customer_profile` | `GET /api/sales/customers/{id}` | Company + contacts + opportunities + cases + locations |
| `do_sales_overview` | `GET /api/sales/overview` | Pipeline value by stage |
| `do_seller_detail` | `GET /api/sales/seller-detail/{user}` | Per-seller pipeline |
| `do_sales_dashboard` | `GET /api/sales/dashboard` | Team/owner dashboard |
| `do_sales_stats` | `GET /api/sales/stats` | IT/AV + sector-segmented stats |
| `do_sales_manager` | `GET /api/sales/manager` | Manager team-performance view |
| `do_list_agreements` / `do_agreement_detail` | `GET /api/sales/agreements[/{id}]` | Agreement list/detail |
| `do_quote_detail` | `GET /api/sales/quotes/{id}` | Full quote record (internal — includes cost/margin if present) |

Unknown lookups fail closed with structured errors rather than empty success, e.g. `do_customer_profile` returns `{"error": "unknown_company", "detail": "Ukjent selskap: ..."}` (`nce/vertical_modules/sales/source_mode.py:265`), and `do_quote_detail` returns `{"error": "unknown_quote", ...}` (`nce/vertical_modules/sales/read_model.py:1513-1514`).

---

## 2. DealRoom (`dealroom.py`)

`do_open_dealroom(engine, params)` (`nce/vertical_modules/sales/dealroom.py:24-240`) materialises a live quote view with toggle-able option lines.

**Params:** `namespace_id` (required), `quote_id` (required), `toggled_options` (optional `dict[str, bool]` keyed by BOM line label or line-ref suffix).

**What it does, in order:**
1. Reads the quote's `name`/`description` from `sales_read_model` (merged `source_json` + `manual`), falling back to `"DealRoom Quote"` if the quote isn't found (`:59-83`).
2. **Fetches BOM lines from `kg_nodes` using literal prefix matching** (`:85-109`): queries `WHERE entity_type = 'BOM_LINE' AND namespace_id = $1::uuid AND starts_with(label, $2)` with parameter `f"BOM_LINE:{quote_id.upper()}:"`. **Postgres `starts_with()` is used instead of SQL `LIKE`** (PR #67). Because `quote_id` is caller-supplied and DealRoom is customer-facing, SQL `LIKE` metacharacters (`_` matching any single character, `%` matching any sequence) in a quote ID would otherwise silently widen the match to a different quote's BOM lines within the tenant (e.g. `'BOM_LINE:QA1:AMP01' LIKE 'BOM_LINE:Q_1:%'` evaluates to true). `starts_with()` provides strict literal-prefix matching with zero pattern semantics.
3. For each line, pulls product/customer pricing detail from a MongoDB payload (`engine.mongo_client`) referenced by `payload_ref`, if present (`:128-158`). It then asks whether a price is **on record** at all — any of `product.base_price`, `product.supplier_list_price` or `customer.bid_price` (`:160-166`). 🔴 **A missing price is never replaced with a plausible one.** Until PR #187 this step substituted a flat `base_price: 100.0`; it no longer does, and no fallback price exists anywhere in this file.
4. Applies any `toggled_options` override for that line and persists the new toggle state back to Mongo (`:168-185`).
5. **Prices every line through the shared C6 pricing service** — `resolve_price()` then `dg_price()` (`nce/vertical_modules/sales/dealroom.py:19,187-209`) — never an inline discount formula. If no price is on record, or if `resolve_price()` raises, the line comes back **unpriced**: `base_cost`, `unit_price` and `total_price` are all `null` and the line carries `"priced": false` plus an `unpriced_reason`. The two reasons are kept distinct because they are different operator problems — `"no_price_on_record"` (nothing was ever recorded) vs `"price_resolution_failed"` (a price exists but C6 could not resolve it).
6. Sums `total_price` across lines where `toggled == True` into `total_price_nok` (`:227-231`). 🔴 **If any toggled line is unpriced, `total_price_nok` is `null`, not a partial sum** — a caller must not be able to read a confident total that silently omits money. `unpriced_line_count` says how many toggled lines were left out (`:237-238`).

**Response shape:**
```json
{
  "quote_id": "quote-12345",
  "name": "DealRoom Quote",
  "description": "",
  "total_price_nok": 4200.0,
  "unpriced_line_count": 0,
  "lines": [
    {
      "label": "BOM_LINE:QUOTE-12345:1",
      "manufacturer": "Sony",
      "model": "VPL-XW5000ES",
      "quantity": 2,
      "base_cost": 3000.0,
      "unit_price": 3600.0,
      "total_price": 7200.0,
      "priced": true,
      "unpriced_reason": null,
      "is_optional": true,
      "toggled": true
    },
    {
      "label": "BOM_LINE:QUOTE-12345:2",
      "manufacturer": "Unknown",
      "model": "Unknown",
      "quantity": 1,
      "base_cost": null,
      "unit_price": null,
      "total_price": null,
      "priced": false,
      "unpriced_reason": "no_price_on_record",
      "is_optional": true,
      "toggled": false
    }
  ]
}
```
`base_cost` and `unit_price` are present in this **internal** DealRoom payload — this is not the customer-facing shape (see §4 for what a customer actually sees). **Both are `null` on an unpriced line, and every consumer must handle that**: the second line above is what a line with no price on record looks like.

There is no `sales_open_dealroom` MCP tool registered; DealRoom is reached only by calling `do_open_dealroom` directly (no dedicated REST route exists in `nce/admin_handlers/sales.py` either — the spec's `api_sales_dealroom` route is **not yet implemented**).

---

## 3. The signed baseline: one immutable freeze per quote

This is the single most important invariant in the Sales engine: **each quote gets at most one signed baseline, ever, and it cannot be edited or deleted once written.**

### 3.1 What freezes it
`do_freeze_baseline()` (`nce/vertical_modules/sales/baseline.py:19-178`) is the only function that writes to `sales_signed_baselines`. It is invoked from the signing callback, `do_on_signed_callback()` (`nce/vertical_modules/sales/signing.py:181-187`), the moment a quote transitions to `signed`.

**Required fields:** `quote_id` (non-empty string), `signed_margin_pct` (float, must satisfy `0.0 <= x <= 1.0`, `baseline.py:58-59`), `signed_total_nok` (float). `signed_at` defaults to `now()` UTC if not supplied.

### 3.2 The immutability mechanism (exactly as coded)
Immutability is enforced at **two** independent layers, not just convention:
1. **App-level idempotency check.** Before inserting, `_do_freeze_baseline_direct()` selects the existing row for `(namespace_id, quote_id)`; if found, it returns immediately with `status: "already_frozen"` and the **original** values — it never overwrites (`nce/vertical_modules/sales/baseline.py:129-150`).
2. **Database-level WORM grant.** The migration that creates the table revokes all privileges from the application role and grants back only `SELECT, INSERT` — no `UPDATE`, no `DELETE` (`nce/migrations/041_sales_signed_baselines.sql:29-33`; mirrored in `nce/schema.sql:2100-2114`). A `UNIQUE (namespace_id, quote_id)` constraint backs the natural key (`nce/schema.sql:1642`). `tests/test_sales_signed_baseline.py:135-182` asserts directly that `UPDATE`/`DELETE` against this table raise `asyncpg.exceptions.InsufficientPrivilegeError` even from an app-role connection.

There is no cryptographic signature stored on the baseline row itself — "signed" here refers to the deal being e-signed (§3.4), not a Doc-22-style HMAC/ML-DSA integrity signature on the row. The baseline table only stores `id, namespace_id, quote_id, signed_margin_pct, signed_total_nok, signed_at, created_at`.

### 3.3 What reads it
`get_signed_baseline(conn, namespace_id, quote_id)` (`baseline.py:181-208`) returns the frozen row (or `None`) as a plain dict — this is what backs the cross-engine MCP read described in §5.

### 3.4 The signing flow that leads to a freeze
`nce/vertical_modules/sales/signing.py` coordinates signing through the shared C7 `SignTransport` (see `docs/shared-core/pricing-signing-grounding.md` §2 for the full transport contract):

1. **`do_request_signature(engine, params)`** (`signing.py:27-103`) — params: `namespace_id`, `quote_id` (required), `doc_bytes` (optional, defaults to dummy bytes), `signer` (optional dict), `method` (one of `"oneflow" | "criipto" | "signicat" | "manual"`, defaults to `"manual"`). It calls `ManualTransport().request_signature(...)` (a module-level singleton — `signing.py:24`), then stamps the quote's `manual` JSON in `sales_read_model` with `signing_session_id`, `signing_status: "pending"`, `signing_fingerprint`, `signer_name`.
2. **`do_on_signed_callback(engine, params)`** (`signing.py:106-225`) — params: `namespace_id`, `session_id` (required), `callback_payload`. Per the fire-and-pull contract, this transitions the transport session, then:
   - Looks up the quote by matching `signing_session_id` in either `manual` or `source_json` (`:131-141`).
   - **Idempotent short-circuit:** if `manual.signing_status == "signed"` already, returns immediately with `already_processed: true` and does not re-freeze (`:157-168`).
   - **Money-guard validation (`_require_money_field`, PR #56):** Resolves `signed_margin_pct` from `("margin", "signed_margin_pct")` (must satisfy `0.0 <= margin <= 1.0`) and `signed_total_nok` from `("total_price", "signed_total_nok", "unit_price")` (must satisfy `total >= 0.0`).
     - **No fabricated defaults:** Missing fields raise `MissingSignedAmountError` rather than defaulting to hardcoded numbers (e.g. 0.3 / 1000.0 NOK).
     - **Exact presence check:** Keys are checked with `is not None`, not truthiness, ensuring that a valid `0.0` amount is preserved and never dropped or replaced.
     - **Type and bounds safety:** Booleans (`bool` is an `int` subclass, where `float(True) == 1.0` would otherwise become money), non-numeric values, `NaN`/`Inf`, and out-of-bounds values raise `MissingSignedAmountError`.
     - **Fail-closed posture:** Failing closed prevents corrupt or fabricated figures from becoming permanently frozen in WORM storage. Because `do_freeze_baseline` is idempotent, an operator can correct the quote's commercial fields and safely re-fire the signed callback.
   - Calls `do_freeze_baseline(...)` (§3.1).
   - Calls `do_convert_signed_quote(...)` in the Project engine (`nce/vertical_modules/project/convert.py`) to hand off the frozen BOM — the Sales→Project A2A bridge.
   - Marks `manual.signing_status = "signed"` in `sales_read_model`.
3. **`do_on_declined_callback(engine, params)`** (`signing.py:228-291`) — marks the quote `signing_status: "declined"`; no baseline is touched.

None of `do_request_signature` / `do_on_signed_callback` / `do_on_declined_callback` are registered MCP tools or REST routes today — they are called from the signing-provider webhook plumbing, which is not itself exposed under `nce/admin_handlers/sales*.py` in this codebase snapshot. Treat this as internal orchestration code you call from your own webhook handler, not a public API.

---

## 4. Public customer-facing quote (`api_sales_quote_public`)

`GET /public-api/sales/quotes/{id}?namespace_id=...&token=...` (`nce/admin_handlers/sales_public.py:34-107`) is the only customer-facing HTTP surface Sales exposes. It bypasses the internal HMAC/mTLS admin auth entirely and instead uses:

1. **Stateless HMAC token.** `generate_public_token(quote_id) = HMAC-SHA256(NCE_MASTER_KEY, quote_id)` (`sales_public.py:27-31`). The caller must present this exact token (`?token=` query param or `Authorization: Bearer`), compared with `hmac.compare_digest` (`:64-66`) — constant-time, so no timing side-channel on token validation.
2. **Rate limiting.** 5 requests per 10 seconds per token, via `_check_admin_http_rate_limit` against Redis (`:69-76`); a 6th request in the window returns `429`.
3. **Fetch + redact.** It calls `do_quote_detail` (the *internal*, unredacted read — §1.3) and then projects the result through the C8 allow-list redactor: `project(quote_detail, "public-quote")` (`:94`, using `nce/redaction/redactor.py`).
4. **Belt-and-suspenders check.** After redaction, the handler still explicitly strips `cost`, `margin`, `commission`, `internal-status` if somehow present, logging an `ERROR`-level "Security violation" if it ever fires (`:99-105`) — this should never trigger given the allow-list is default-deny, but it is a live safety net in the code, not just documentation.

**What a customer can see** (the `public-quote` allow-list, `nce/config_data/redaction/public-quote-redaction.json`): `id`, `node_type`, `label`, `description`, `category`, `manufacturer`, `model`, `part_number`, `quantity`, `unit_price`, `currency`, `lead_time_days`, `availability`, `tags`, `namespace_id`. Anything not on this list — including `cost`, `margin`, `commission`, seller identity, or internal pipeline status — is dropped by construction (default-deny; see `docs/shared-core/redaction.md` §1-2 for the full C8 contract).

Failure modes: missing/invalid token → `401`; unknown quote → `404` (`unknown_quote` mapped from `do_quote_detail`); missing `namespace_id` → `400`; rate limit exceeded → `429`.

> [!NOTE]
> The `unit_price` field on the public surface is the customer-facing sales price, not cost — but note from §1.1 that the D365-sourced portion of a quote carries only `quoteid/name/statecode/modifiedon`. `unit_price`, `manufacturer`, etc. only appear on the public surface if something (DealRoom, manual enrichment) has actually written them into the quote's `source_json`/`manual` first. An untouched D365-mirrored quote will render an almost-empty redacted payload — this is expected default-deny behavior, not a bug, but it means the public link is only useful once a quote has been enriched with commercial line-item detail.

---

## 5. `sales_get_signed_baseline` — the one Sales MCP tool that reads business data

Registered in `nce/tool_registry.py:584-589`. Handler: `handle_sales_get_signed_baseline` (`nce/vertical_modules/sales/mcp_handlers.py:50-93`).

**Arguments:**
- `namespace_id` (str, required)
- `quote_id` (str, required — the Sales QUOTE identifier)

**Returns:** the frozen baseline row as JSON, or JSON `null` if Sales has no baseline for that quote yet:
```json
{
  "id": "1",
  "quote_id": "quote-12345",
  "signed_margin_pct": 0.35,
  "signed_total_nok": 150000.0,
  "signed_at": "2026-06-24T12:00:00+00:00"
}
```
There is no wrapper object — the JSON body **is** the baseline or `null` (`mcp_handlers.py:90-92`). This is deliberate: it's the cross-engine A2A seam `project.baseline._read_signed_baseline` resolves to at runtime, and Project is expected to degrade gracefully (`sales_available: false`) rather than fabricate a value when the result is `null`.

**Tool registry flags** (`nce/tool_registry.py:584-589`, asserted by `tests/test_sales_signed_baseline_surface.py`):
- `cacheable: false` — deliberately not cached. The freeze happens via the signing callback, not a registered MCP mutation, so no cache-generation bump would invalidate a stale pre-freeze `null` result. It reads fresh every call.
- `admin_only: false`
- `mutation: false`

**`sales_ping`** (`nce/tool_registry.py:573-578`) is the only other registered Sales MCP tool — a liveness probe returning `{"ok": true, "engine": "sales"}`, useful only to confirm the Sales handler module is wired up.

---

## 6. AI surface (lead scoring, quote-draft, win/loss recall) — propose-only by convention

`nce/vertical_modules/sales/ai.py` implements three AI-assist functions, none of which are registered as MCP tools or REST routes in this snapshot — they exist as internal `do_*` functions only:

- **`do_score_lead(engine, params)`** (`ai.py:89-152`) — scores a lead by recalling similar historical `DEAL`/`QUOTE`/`LEAD`/`OPPORTUNITY` memories (cosine similarity via `nce.embeddings`, filtered to `similarity >= 0.6`) and computing the historical win-rate among them. Returns `{"score": float, "confidence": float, "propose_only": true, "reasons": [...]}`. If no similar memories are found, it returns a neutral `score: 0.5` with an explicit reason string rather than fabricating confidence.
- **`do_draft_quote(engine, params)`** (`ai.py:155-218`) — proposes BOM lines and a suggested margin from similar historical deal memories. Returns `{"proposed_lines": [...], "suggested_margin_pct": float, "propose_only": true, "validated": false}`.
- **`do_win_loss_recall(engine, params)`** (`ai.py:58-86`) — raw similarity search over won/lost deal memories, `{"ok": true, "candidates": [...]}`.

**On the propose-only claim:** both `do_score_lead` and `do_draft_quote` set `propose_only: True` in their return payload, and neither writes anything to the graph or read-model — they are pure reads over `memories`. However, **this is a convention enforced by the function's own return shape, not a structural gate.** Unlike the Product engine's `do_enrich_product` (wrapped in `@governed(...)`, see `docs/engines/product-user.md` §3.2), **no `@governed` decorator or C2 governor wraps any function in `nce/vertical_modules/sales/`.** There is currently no code-enforced confirm-before-write gate on the Sales AI surface — the "propose-only" behavior holds only because these two functions happen not to perform writes, not because a governor would block one that did.

`do_record_ai_decision(engine, params)` (`ai.py:221-258`) appends an audit row to `event_log` with `event_type: "sales_ai_decision"` — used by the commission calculator (§7) to reconstruct historical won-deal commission, not a gate on AI actions.

---

## 7. Commission (`commission.py`) — reproducible, not tool-surfaced

`nce/vertical_modules/sales/commission.py` is **not** registered as an MCP tool or a REST route in this snapshot. It exists as internal `do_*` code:

- **`load_commission_config()`** (`commission.py:27-31`) loads `nce/config_data/sales-commission.json` — a versioned tier table (`"version": "1.0"`), currently 3 margin-percentage tiers, each with a separate `hardware_rate` and `service_rate` (service lines pay a **higher** rate than hardware at every tier, e.g. tier 3: `hardware_rate: 0.03` vs `service_rate: 0.06`).
- **`calculate_deal_commission(deal_data, config)`** (`:34-81`) — pure function: computes overall deal margin (`(total_price - total_cost) / total_price`), selects the tier whose `[min_margin_pct, max_margin_pct]` bracket contains it, then applies `hardware_rate` or `service_rate` per line item's `type` field to that line's profit (`price - cost`), summing across lines.
- **`do_calculate_commission(engine, params)`** (`:84-158`) — with `deal_data` supplied directly, computes commission ad hoc. Without it, replays `event_log` rows where `event_type = 'sales_ai_decision'` and `params->>'decision_type' = 'deal_won'`, recomputing commission per historical event via the *current* `load_commission_config()`. This is the reproducibility guarantee: commission for any past won deal can be recomputed at any time from `(ledger events, the versioned commission config)` — nothing about a payout is stored as an opaque pre-computed number.
- **`do_initiate_quote_flow`** (`:161-220`) and **`do_record_deal_loss_feedback`** (`:223-282`) implement the Quote→Design→Procure A2A flow and the loss-reason → Product failure-pattern feedback edge, respectively. Both are internal orchestration functions, not exposed tools/routes.

---

## 8. Worked example: quote through to a frozen baseline

This walks the actual code path from a quote existing in `sales_read_model` through to a frozen, immutable baseline — using only functions that exist today.

```python
# 1. Open the DealRoom for a quote (recomputes prices via C6 shared pricing)
room = await do_open_dealroom(engine, {
    "namespace_id": ns_id,
    "quote_id": "quote-12345",
})
# room["total_price_nok"] reflects only toggled=True lines, and is None if
# any toggled line came back unpriced -- check room["unpriced_line_count"] first

# 2. Request a signature (manual transport in dev/test; oneflow/criipto/signicat in prod)
session = await do_request_signature(engine, {
    "namespace_id": ns_id,
    "quote_id": "quote-12345",
    "method": "manual",
})
# session["session_id"] / session["fingerprint"]

# 3. Provider confirms signature -> your webhook calls the signed callback
#    (requires margin and total_price/signed_total_nok on the quote, else raises MissingSignedAmountError)
result = await do_on_signed_callback(engine, {
    "namespace_id": ns_id,
    "session_id": session["session_id"],
    "callback_payload": {...},
})
# result == {"ok": true, "quote_id": "quote-12345", "session_id": ...,
#            "baseline_frozen": true, "project_id": "...", "already_processed": false}

# 4. Any other engine reads the frozen baseline via the one MCP tool:
#    sales_get_signed_baseline(namespace_id=ns_id, quote_id="quote-12345")
#    -> {"id": ..., "quote_id": "quote-12345", "signed_margin_pct": 0.35,
#        "signed_total_nok": 150000.0, "signed_at": "..."}

# 5. Calling do_on_signed_callback again with the same session_id is a no-op:
#    already_processed: true, baseline is NOT re-frozen (idempotent).
```

---

## Appendix: spec vs. shipped (delta from `docs/vertical_engines/05-sales-engine.md`)

The design spec describes a much larger MCP/REST surface than exists on `main` today. As of this audit:
- **Not implemented as MCP tools:** `sales_list_customers`, `sales_customer_profile`, `sales_overview`, `sales_dashboard`, `sales_stats`, `sales_seller_detail`, `sales_quote_detail`, `sales_score_lead`, `sales_draft_quote`, `sales_open_dealroom`, `sales_request_signature`, `sales_freeze_signed_baseline`, `sales_convert_signed_quote_to_project`, `sales_sync_now` — all *(planned — not yet implemented as MCP tools)*. The underlying `do_*` functions exist for most of these (accessible via REST or direct call), except a dedicated DealRoom/signing REST route, which is also not present.
- **Not implemented anywhere:** `api_sales_dealroom` (POST), `api_sales_signing_webhook` (POST), `api_sales_sync_status`/`api_sales_sync_now` REST routes, and the `NCE_SALES_*` config-var family described in the spec (see the admin guide §1 for what actually gates the engine today — namely, nothing).
- **Implemented and matches spec:** the signed-baseline freeze mechanism (WORM), the C5 source-mode resolver + divergence log, the C8 public-quote redaction, and DealRoom's use of the shared C6 pricing service (no inline discount formula).
