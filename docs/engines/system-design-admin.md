> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# System Design Engine Admin Guide (Doc 70)

> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

The **System Design Engine** (`nce/vertical_modules/system_design/`) is the Revenue↔Delivery bridge: an embedding-recall "AI Solution Agent" that proposes room BOMs, a pure SoW generator, and (Phase-2) a device/port topology model with structural validation. This guide covers enablement, configuration, database schema/RLS, the three Phase-1b adapters (NetBox, SharePoint, Lucid), autonomy posture, and troubleshooting — grounded strictly in what is implemented, not the design-intent spec.

> [!WARNING]
> **Surface Warning — Limited Network/MCP Exposure & Notice for Frontend Developers:**
> Despite full wave completion of internal modules (Phase-1a propose/SoW and Phase-2 topology/validation), the engine exposes only **2 MCP tools** (`system_design_ping` - a ping stub, and `system_design_publish_design_docs`) and **1 REST route** (`POST /api/system-design/publish-design-docs` / `/api/system-design/publish`).
>
> **Notice for Frontend & Client Developers:**
> Interactive topological design, CAD layout, functional location authoring, and automated validation **cannot be invoked over MCP or REST endpoints today**. All topological design and graph functions exist solely as in-process Python calls. Frontend client applications (e.g. host web CAD UIs) and external MCP agents cannot trigger CAD layout or interactive design workflows over the network.

---

## 1. Enablement — read this before assuming parity with other engines

> [!WARNING]
> Unlike the Product engine (`nce/vertical_modules/product/_guard.py`, `NCE_PRODUCT_ENABLED` + namespace `metadata->'product'->>'enabled'`) and the Agreements engine (`nce/vertical_modules/agreements/_guard.py`), **System Design has no `_guard.py`, no `NCE_SYSTEM_DESIGN_ENABLED` flag, and no namespace-level `metadata->'system_design'->>'enabled'` opt-in check anywhere in code.** A grep across `nce/vertical_modules/system_design/*.py` and `nce/config.py` confirms this. The design spec (`docs/vertical_engines/06-system-design-engine.md:162-163`) documents `NCE_SYSTEM_DESIGN_ENABLED` as a config key — *(planned — not yet implemented)*. Today the engine's functions are simply importable and callable for any namespace; there is no fail-closed gate to configure or audit.

Because most of the engine's functions are not registered MCP tools (see the [User Guide](system-design-user.md) §1), there is also no per-tool admin/mutation gate to reason about beyond the two tools that do exist:

| MCP tool | cacheable | admin_only | mutation |
|---|---|---|---|
| `system_design_ping` | `True` | `False` | `False` |
| `system_design_publish_design_docs` | `False` | `False` | `True` |

This is pinned by `tests/unit/test_system_design_toolcount.py` — any addition/removal of a `system_design_*` tool, or a flag change on these two, fails that test.

---

## 2. Configuration reference (env vars actually read by code)

All values are read via `nce/config.py`'s `cfg` object or `resolve_secret()` at call time (never cached at import, so tests can `monkeypatch.setenv` safely).

### 2.1 Recall / propose tuning
- **`NCE_SYSTEM_DESIGN_RECALL_TOP_K`** (int, default `5`, minimum `1`) — number of past `DESIGN`/`PROJECT` memories recalled per `do_propose_design` call (`nce/config.py:1039`).
- **`NCE_SYSTEM_DESIGN_OUTCOME_WEIGHTING_ENABLED`** (bool, default `False`) — gates the outcome-weighting hook in `propose.py`. **Do not set this to `True` in production** — the only implemented behavior when `True` is `_apply_outcome_weights` raising `NotImplementedError` (`nce/vertical_modules/system_design/propose.py:120-148`). The discount-by-change-order/ticket/margin logic has not been written; it is waiting on Project/Support engines to backfill `v3_cognitive_ledger`.

### 2.2 Lucid export (Phase 1b, `lucid.py`)
- **`NCE_SYSTEM_DESIGN_LUCID_API_KEY`** (secret) — Bearer token. When unset, `do_publish_design_docs` returns `{"lucid_url": null}` as a clean no-op (never raises).
- **`NCE_SYSTEM_DESIGN_LUCID_BASE_URL`** (string, default `https://api.lucid.co`).

Both are resolved via `resolve_secret()` at call time (`lucid.py:72-82`) and are never logged.

### 2.3 SharePoint SoW store (Phase 1b, `sharepoint.py`)
- **`NCE_SYSTEM_DESIGN_SHAREPOINT_SITE_ID`** (secret) — presence of this variable is the on/off switch; if unset, both `store_sow` and `fetch_sow` return `None` silently (`sharepoint.py:17-20, 68-80`).
- **`NCE_SYSTEM_DESIGN_SHAREPOINT_DRIVE_ID`** (default `"root"`).
- **`NCE_SYSTEM_DESIGN_SHAREPOINT_FOLDER_PATH`** (default `"SoW"`).
- **`NCE_SYSTEM_DESIGN_SHAREPOINT_ACCESS_TOKEN`** (secret) — Bearer token for Microsoft Graph API (`https://graph.microsoft.com/v1.0`).

> [!IMPORTANT]
> There is no MCP tool, admin route, or scheduled job in the current codebase that calls `store_sow`/`fetch_sow`. They exist as importable functions only — nothing in `nce/admin_app.py`, `nce/admin_handlers/`, or `nce/tool_registry.py` wires them up. Treat SharePoint delivery as a library capability, not an operational feature, until a caller is added.

### 2.4 NetBox bridge (Phase 1b, `netbox_bridge.py`)
Reuses the shared NetBox connection settings — there is no `NCE_SYSTEM_DESIGN_NETBOX_*` namespace despite the spec suggesting one:
- **`NCE_NETBOX_URL`** (`nce/config.py:912`).
- **`NCE_NETBOX_TOKEN`** (`nce/config.py:913`).

`build_bridge(conn, namespace_id, namespace_slug, netbox_url=None, netbox_token=None)` (`netbox_bridge.py:649-676`) defaults to these two `cfg` values and raises `ValueError` if either resolves empty. Like SharePoint, **no MCP tool, admin route, or scheduler currently calls `build_bridge`, `sync_fl_to_netbox`, or `reconcile_asbuilt`** — they are library functions you call from your own operational code or a REPL/script. There is no `system_design_sync_functional_locations` tool despite the spec proposing one (`docs/vertical_engines/06-system-design-engine.md:127-129, 144`).

---

## 3. NetBox bridge — functional-location sync + as-built reconcile

Source: `nce/vertical_modules/system_design/netbox_bridge.py`. Modeled on `nce/vertical_modules/dynamics365/netbox_bridge.py` (paginated REST fetch → batch edge upsert).

**Direction invariant (Correction #2 in the spec, honored in code):** design intent is authored *before* the physical room exists. NetBox is an as-built source, populated after install. The bridge therefore:

1. **Push (`sync_fl_to_netbox`)** — creates NetBox DCIM sites (for `SITE`) and locations (nested for `BUILDING`/`FLOOR`/`ROOM`/`POSITION`) from the already-authored design-intent tree. Existing NetBox objects are matched by normalized name and reused rather than duplicated (`_normalize`, case/whitespace-insensitive). Writes `FL:<ns>:<path> -[sync_to_netbox]-> NetBoxSite:<id>` / `NetBoxLocation:<id>` edges with confidence `1.0`.
2. **Reconcile (`reconcile_asbuilt`)** — given a list of `{"intent_label", "asbuilt_name", "intent_name", "confirmed"}` confirmations from NetBox/Assets:
   - Unconfirmed entries are counted as `unchanged` and skipped.
   - Confirmed entries always get a `promoted_to_asbuilt` edge (intent → as-built label, `AsBuilt:<intent_label>`) and a reverse `as_built_confirms` edge, both confidence `1.0`.
   - If the normalized `asbuilt_name` differs from `intent_name`, an additional `has_divergence` edge is written at confidence `0.7`, and the entry counts as `diverged` instead of `promoted`.
3. **The design-intent `kg_node` is never modified** — only linked via edges. This is enforced structurally: `netbox_bridge.py` has no UPDATE statement against `kg_nodes` at all, only `kg_edges` batch upserts via `_upsert_kg_edges_batch` (UNNEST pattern, same as the D365 bridge).

Both methods live on `SystemDesignNetBoxBridge`, constructed via `build_bridge()`. The caller supplies an `asyncpg.Connection` with the namespace RLS GUC already set (via `scoped_pg_session`) — the bridge itself never touches the GUC.

Failure handling: individual site/location creation failures are caught, logged, and appended to `SyncResult.errors` — a single failed room does not abort the whole sync. If the *site* creation itself fails, `sync_fl_to_netbox` returns early with the accumulated errors.

---

## 4. SharePoint SoW delivery

Source: `nce/vertical_modules/system_design/sharepoint.py`. Two functions, zero DB dependency:

- `store_sow(design_id, sow_doc) -> str | None` — `PUT`s the JSON-serialized `SoWDoc` to `{GRAPH_ROOT}/sites/{site_id}/drive/root:/{folder_path}/{documentRef}.json:content` (or the `/drives/{drive_id}/...` variant when `drive_id != "root"`), returns the SharePoint drive-item id. **Only the opaque reference should be persisted by callers — never the document body** (per the module docstring; there is no DB table for SoW bodies).
- `fetch_sow(ref) -> SoWDoc | None` — `GET`s the document back by drive-item id.

Both route through `nce.http_resilience.request_with_retry` with a 30s timeout and return `None` (not an exception) when `NCE_SYSTEM_DESIGN_SHAREPOINT_SITE_ID` is unset — this is a deliberate Phase-1b non-gate: the value core (propose → SoW → freeze) works with zero external systems.

---

## 5. Lucid diagram export

Source: `nce/vertical_modules/system_design/lucid.py`. See User Guide §7 for the call contract. Administratively relevant points:

- **Export only.** There is no import path; if a future change request asks for "parse this Lucid diagram back into DESIGN_LINEs," that is new scope, not a bug fix — the module docstring explicitly flags this as a spec correction to stop at.
- Reachable two ways: the `system_design_publish_design_docs` MCP tool, or `POST /api/system-design/publish-design-docs` (and `/api/system-design/publish`, `nce/admin_handlers/system_design.py`), registered in `nce/admin_app.py:653`.
- Credential absence is a clean no-op (`{"lucid_url": null}`), not an error — safe to leave unconfigured in any environment.
- The `TOOL_REGISTRY` entry marks this tool `mutation=True` because it performs an outbound POST to a third-party service, even though it writes nothing to NCE's own database.

---

## 6. Database schema & RLS

### 6.1 `system_design_device_capabilities` (migration 039)
The only table this engine owns. Everything else is graph-only (`kg_nodes`/`kg_edges`, shared infrastructure).

```sql
CREATE TABLE IF NOT EXISTS system_design_device_capabilities (
    id                 UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id       UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    node_label         TEXT        NOT NULL,          -- matches kg_nodes.label
    signal_format      TEXT,                          -- 'HDMI' | 'DP' | 'Dante' | 'SDI' | 'AES67' | ...
    signal_version     TEXT,                          -- '2.1' | '2.0' | '1.4' | ...
    port_direction     TEXT CHECK (port_direction IS NULL OR port_direction IN ('input','output','bidirectional')),
    poe_class          SMALLINT,                       -- IEEE 802.3 class 0-8
    poe_watts          NUMERIC,
    dante_rx_channels  SMALLINT,
    dante_tx_channels  SMALLINT,
    power_draw_watts   NUMERIC,
    heat_btu_hr        NUMERIC,
    redundancy_role    TEXT CHECK (redundancy_role IS NULL OR redundancy_role IN ('primary','secondary','standalone')),
    device_category    TEXT,
    manufacturer       TEXT,
    model_number       TEXT,
    extra              JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    UNIQUE (namespace_id, node_label)
);
```

Indexes: `(namespace_id, node_label)`, partial index on `(namespace_id, signal_format)` where not null (feeds the port-format-compatibility check), partial index on `(namespace_id, redundancy_role)` where not null (feeds the SPOF check).

**RLS:** `ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY`, single `tenant_isolation_policy` scoped by `get_nce_namespace()` (identical pattern to `procurement_bid_prices`). Grants to `nce_app`: `SELECT, INSERT, UPDATE, DELETE` (unlike Product's append-only tables, this one supports updates — capability data is expected to be revised as a design iterates).

### 6.2 `kg_nodes` / `kg_edges` — `system_design_source_id` (migration 038)
Adds a per-vertical provenance column (mirrors `procurement_source_id`) to both `kg_nodes` and `kg_edges`, plus partial indexes on `(namespace_id, system_design_source_id)` where not null. This is what enables hard-retirement of system-design-derived rows if the originating source record is deleted. Every node/edge write in `graph.py`, `devices.py`, and `netbox_bridge.py` tags this column via `COALESCE(EXCLUDED.system_design_source_id, <table>.system_design_source_id)` on conflict, so a later write with `source_id=None` never clobbers an existing tag.

> [!NOTE]
> The auto-memory ledger entry for this engine states migration 038 = `system_design_device_capabilities`. That is **stale** — verified against the repo at `7304330`, migration **038** is `system_design_source_id` and migration **039** is `system_design_device_capabilities`. Treat the numbers in this document, not the memory note, as current truth.

### 6.3 Node ownership
`nce/config_data/node-ownership.json` assigns `owner_engine: "system_design"` to seven node types: `FUNCTIONAL_LOCATION`, `DESIGN`, `DESIGN_LINE` (Phase-1a) and `DEVICE`, `PORT`, `SIGNAL_CHAIN`, `RACK`, `CABLE` (Phase-2). Every own-node write in `graph.py` and `devices.py` calls `assert_owner(conn, ns_uuid, entity_type, "system_design")` before the INSERT — deny-by-default. `PRODUCT` nodes are referenced by label only in `references` edges; System Design never writes a `PRODUCT` or `QUOTE` kg_nodes row (Contract A §9.1 — enforced by simply never containing an INSERT for those labels, not by a runtime check).

---

## 7. Autonomy & governance posture

System Design does **not** use the `@governed` / C2 human-in-the-loop decorator pattern that the Product engine's `do_enrich_product` uses. Instead, propose-only is enforced by construction:

- `do_propose_design` and the gap-fill path in `do_design_from_quote` never write graph rows — they only return dicts with `validated: False`. There is nothing to gate because nothing is committed.
- `do_validate_design` is the only place a human decision is durably recorded, and it **refuses** any request where a line lacks an explicit `accept`/`override` verdict (raises `ValueError` — see User Guide §5). There is no confidence threshold anywhere in this module that can auto-accept a line.
- The one dormant "autonomy knob," `NCE_SYSTEM_DESIGN_OUTCOME_WEIGHTING_ENABLED`, does not relax human review when enabled — it would only reorder *proposal* ranking, and today it isn't even implemented (see §2.1).

Scoped A2A enrichment (`enrichment.py`, `do_enrich_design_lines`) follows the same "never bulk, fire-and-backfill" rule as Product:
- Reads `DESIGN -[contains]-> DESIGN_LINE -[references]-> PRODUCT` edges for exactly one design (never a catalog scan).
- Deduplicates by resolved `product_catalog` UUID so N design lines referencing the same SKU fire exactly one enrichment job via `enqueue_product_enrichment` (Product's fire-and-forget enqueue).
- Every step (enqueue, price lookup, TCO calculation) is wrapped so failures are logged and swallowed — a broken enrichment or missing price can never block a proposal/quote/SoW call (`enrichment.py:176-291`).
- TCO is computed inline via `nce.vertical_modules.procurement.tco.do_calculate_tco`, a pure function; it does not wait for enrichment to complete.

`do_enrich_design_lines` is, like the flow-core functions, **not** an MCP tool — it's called by whichever flow needs scoped enrichment (design authoring, quote realization, SoW assembly).

---

## 8. Operational watchers / cron

There are **no scheduled tasks or watcher daemons** for this engine in the current codebase — no equivalent of the Product engine's `watchers.py` EOL/EOS cron. `sync_fl_to_netbox` and `reconcile_asbuilt` are invoked on demand by whatever caller you wire up; nothing polls NetBox automatically. If you need periodic reconciliation, you must build the scheduler trigger yourself and call `netbox_bridge.build_bridge(...)` from it.

---

## 9. Troubleshooting

| Symptom | Likely cause | Where to look |
|---|---|---|
| `do_design_from_quote` / `do_design_to_quote` raise `NotImplementedError` | Sales engine's A2A receiving tools (`sales_get_quote_lines`, `sales_propose_quote`) are not built yet | `from_quote.py:86-122`, `to_quote.py:77-111`. In tests, both seams are patched via `unittest.mock.patch`; in production, this is an expected gap until Sales ships those tools. |
| `do_propose_design` with `NCE_SYSTEM_DESIGN_OUTCOME_WEIGHTING_ENABLED=true` raises `NotImplementedError` | The outcome-weighting math was never implemented — the flag only exists as a forward-looking gate | `propose.py:120-148`. Keep this flag `False` (the default) until Project/Support backfill `v3_cognitive_ledger` and someone implements the discount logic. |
| `do_publish_design_docs` returns `{"lucid_url": null}` | Lucid credentials unset — this is a designed no-op, not a failure | Check `NCE_SYSTEM_DESIGN_LUCID_API_KEY` / `_BASE_URL` if you expected a real export. |
| `store_sow`/`fetch_sow` return `None` | `NCE_SYSTEM_DESIGN_SHAREPOINT_SITE_ID` unset, or (more likely) nothing in your code path calls these functions at all | No MCP tool or admin route calls them today (§2.3) — verify your integration calls the Python functions directly. |
| `sync_fl_to_netbox` raises `ValueError: NetBox URL/token is not configured` | `NCE_NETBOX_URL` / `NCE_NETBOX_TOKEN` unset | These are shared NetBox settings, not `system_design`-specific env vars (§2.4). |
| `validate_design_graph` returns `passed=False` unexpectedly on a design with no devices | Expected — checks 1/2/4/5 pass trivially on empty topology (empty input list ⇒ no dangling ports, no connections to check, no primaries, no devices missing categories); check 3 always passes. If `passed=False` with an empty topology, look for stale rows in `system_design_device_capabilities` from a previous design reusing the same `node_label` in the same namespace. | `validation_queries.py:613-647` |
| Cross-namespace test bleed in `validate_design_graph` checks | Historical bug class: an unscoped `LIMIT 1` subquery could pick an arbitrary namespace's rows for a shared design label. Already fixed — every `_fetch_*` helper threads `ns_uuid` explicitly through every JOIN, not relying on RLS alone (owner-pool test connections bypass FORCE RLS) | `validation_queries.py:388-389, 443, 498, 529` |
| Two design lines silently didn't both trigger enrichment | This is by design — `do_enrich_design_lines` dedups by resolved `product_catalog` UUID; only the first line referencing a given product fires the enqueue | `enrichment.py:390-402` |

---

## 10. Known code-level drift from the design spec (flagged, not fixed)

For anyone reconciling `docs/vertical_engines/06-system-design-engine.md` against the running system:

1. **No `NCE_SYSTEM_DESIGN_ENABLED`** anywhere in code (§1 above) — spec §"Config keys" claims a per-namespace opt-in that does not exist.
2. **Only 2 of 7 spec'd MCP tools exist.** `system_design_propose_design`, `system_design_design_from_quote`, `system_design_generate_sow`, `system_design_design_to_quote`, `system_design_validate_design`, and `system_design_sync_functional_locations` are all spec-only; only `system_design_ping` (a ping stub) and `system_design_publish_design_docs` (named `publish_design_docs`, not the spec's `publish_docs`) are registered.
3. **No REST routes exist** for `api_system_design_propose_design`, `..._design_from_quote`, `..._generate_sow`, `..._design_to_quote`, `..._validate_design`, or `..._sync_functional_locations` — only `api_system_design_publish_design_docs` (under `/api/system-design/publish-design-docs` or `/api/system-design/publish`) is wired into `admin_app.py`.
4. **No `NCE_SYSTEM_DESIGN_OUTCOME_WEIGHTS` config-as-IP JSON and no `NCE_SYSTEM_DESIGN_AUTO_CONFIDENCE_THRESHOLD`** — the outcome-weighting feature the spec describes has neither its config surface nor its implementation.
5. **No `system_design_doc_refs` table** — the spec proposes one for tracking SharePoint/Lucid refs against a design; SharePoint's `store_sow` returns a ref to the *caller*, who is responsible for persisting it (nowhere does), and Lucid's export doesn't persist its URL anywhere either.
6. **`do_publish_design_docs` only handles Lucid**, not the spec's combined `targets: ["sharepoint", "lucid"]` parameter — SharePoint delivery is a separate, uncalled function (§2.3).

None of this blocks the Phase-1a value core (propose → gap-fill → SoW → freeze), which is fully implemented and covered by `tests/test_system_design_phase1a.py`. It does mean this engine is materially less "wired up" operationally than Product or Agreements — most of its value is currently accessed by other engines calling its Python functions directly, not by MCP/REST callers.

---

## 11. Related reading
- [System Design User Guide](system-design-user.md) — tool/function call contracts, worked examples.
- `docs/vertical_engines/06-system-design-engine.md` — design-intent spec (DISCUSSION doc — treat as roadmap).
- `nce/vertical_modules/dynamics365/netbox_bridge.py` — the sibling bridge this module's NetBox client pattern was modeled on.
