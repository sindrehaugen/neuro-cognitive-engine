> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# 09 — Assets Management Engine  (nce/vertical_modules/assets)

**Status:** spec (Tier 3 — Operations axis) · **Owner:** NCE core (Sindre)
**Pattern companions:** `docs/VERTICAL_MODULE_PATTERN.md`, `docs/vertical_engines/00-ENGINES-ROADMAP.md` (§2 AI-roles, §4 graph catalogue, §7 spec format), `docs/vertical_engines/06-system-design-engine.md` (the shared `FUNCTIONAL_LOCATION` model + NetBox-in-loop pattern this engine inherits)

## Mission
Own the **business lifecycle of every installed device** — from the BOM line that was quoted, through install/verify, to health-monitored, warranty-tracked, eventually-retired asset — on the NCE graph spine. The engine **bridges to the existing `netbox` vertical** (modelled on `dynamics365/netbox_bridge.py`): NetBox stays the source-of-truth for **rack/IP/cabling/site** (DCIM), while Assets owns what NetBox does *not* — the **14-state lifecycle, healthScore, warranty, telemetry/digital-twin, driftsavtale (service-contract) coverage, and the Digital Product Passport**. The deep-AI angle: a Watcher that fuses NetBox MTBF (`netbox/mtbf.py` already exists) + live telemetry into a *predictive-failure* signal, and cognitive recall that answers *"what assets like this one failed, and what did we replace them with"* from the ledger — closing Andreas's **service→product silence** by emitting a `failure_pattern` edge back to Product. This is the engine that delivers *"the customer should forget the supplier exists — because everything just works."*

## Inspiration & triage
- **Andreas sources** (handoff `02-kode-virkelighet` §9, the most facade-heavy stage — *build the engines under the rich models*):
  - `schema:1143` — **Asset model** (S/N, firmware, MAC/IP, healthScore, monitoringPlatform). 🟢 rich model → lift the *shape* as the `ASSET` node attribute set.
  - `lib/asset/lifecycle.ts:37` — **idempotent lifecycle enrichment** RECEIVED→INSTALLED→VERIFIED, sets warranty. 🟢 → lift near-1:1 into `lifecycle.py` as a pure transform; extend to the full 14-state machine.
  - `lib/integrations/telemetry/` — Crestron/Q-SYS/Neat **telemetry adapters**. 🟡 *mock-with-swap* (real env-swap `CRESTRON_FUSION_REAL=1`) → lift the **mock-now/swap-ready architecture**, keep adapters env-swappable.
  - `lib/asset/service-contract-creator.ts:20` — **driftsavtale auto-create from BOM** (matches `/driftsavtale/i`). 🟢 → seed `SLA` coverage from BOM at handover.
  - `healthScore` pipeline — 🟡 **passive field, default 100, no writer**. We **build the writer** (the telemetry→health pipeline) — the highest-value gap to close.
  - QR asset register — 🟡 *pitch-QR only, no asset-QR-gen* ("promised B10 Q3 2026") → we ship `do_generate_asset_qr`.
  - DPP (Digital Product Passport) — 🔵 *time-critical, mandatory 2028-29, no code/fields* → **model the fields now** (forward-looking).
- **Existing NCE foundation to bridge/reuse:** the whole `netbox` vertical — `netbox/mtbf.py` (predictive MTBF forecaster, ready to feed the Watcher), `netbox/discovery.py` (unregistered-asset reconciliation), `netbox/circuits.py`, `netbox/contacts.py`, and the bridge template `dynamics365/netbox_bridge.py` (match cascade → `kg_edges` + mapping table — **copy this pattern** for `assets/netbox_bridge.py`).
- **Module map** (`04-virksomhets-modulkart` §12 Operations/Drift): *"asset-register seeded from BOM at handover, proactive monitoring via manufacturer-API (Cisco xAPI, QSC Reflect, Neat Pulse, Huddly, Poly Lens), **driftsavtaler follow ROOM, not customer** (SLA per room)."* Drives the recurring-revenue thesis.
- **Lysning page served:** the room-centric customer asset-register view (consumes the no-model REST surface).

## Classification
**pull + telemetry + bridges.** External systems: the **existing `netbox` vertical** (DCIM truth, via `assets/netbox_bridge.py`); **manufacturer telemetry platforms** (Crestron xAPI, Q-SYS Reflect, Neat Pulse, Huddly, Poly Lens) via per-platform adapters. Auth model: NetBox token (reused via the bridge); each telemetry adapter carries its own credential (`auth.py`), but ships **mock-now** — the real HTTP adapter is selected by env-swap so the engine is fully usable before any vendor key lands. No webhooks in Phase 1 (telemetry is pulled on a cron; vendor push can be added later like D365's `webhooks.py`). Resilience: `httpx.AsyncClient` (30s timeout) via `nce.http_resilience.request_with_retry()`.

## Graph contribution
Node `entity_type` prefixes: `ASSET_*`, `TELEMETRY`, plus shared spine nodes `ASSET`, `BOM_LINE`, `FUNCTIONAL_LOCATION`, `TICKET`, `SLA`, `PRODUCT`, `netbox_device`.
- **Nodes:** `ASSET` (S/N, firmware, MAC/IP, healthScore, monitoringPlatform, warranty, lifecycle_state, **DPP fields**), `TELEMETRY` (a sampled health/metric reading), `SLA` (a driftsavtale, keyed to a `FUNCTIONAL_LOCATION`).
- **Edges (the §4 contract, our slice):**
  - `BOM_LINE -[installed_as]-> ASSET` — **the seed edge written at handover** (Field Tech install completes → asset register row appears).
  - `ASSET -[lives_in]-> FUNCTIONAL_LOCATION` — the room; **shared node** with System Design / NetBox (roadmap §4, the room-centric anchor).
  - `ASSET -[monitored_by]-> TELEMETRY` — health/metric stream.
  - `ASSET -[covered_by]-> SLA -[for]-> FUNCTIONAL_LOCATION` — **driftsavtale follows the ROOM, not the customer** (the key differentiator: an SLA is per functional location).
  - `TICKET -[about]-> ASSET` — **inbound edge written by Support(10)**, consumed here for health context.
  - `ASSET -[maps_to]-> netbox_device` — **the bridge edge** (`assets/netbox_bridge.py`, modelled on the D365 `MAPS_TO_*` cascade): NetBox owns rack/IP/cabling, Assets owns lifecycle/health.
  - `ASSET -[failure_pattern]-> PRODUCT` — **the silence-closer**: repeated failures/short-life feed Product (better BOMs) and Sales (refresh/upsell). In NCE this is just an edge; the cognitive engine surfaces it.
- **memories/ledger:** each lifecycle transition + each health-degradation/EOL/warranty alert + each replacement recommendation → `v3_cognitive_ledger` (this is where "assets like this that failed" recall lives). Asset narrative/notes → `memories` (embedding + `content_fts`) for recall. Tag every derived row with `assets_source_id` for hard-retirement on delete (D365 retirement pattern, roadmap §2.3).

## Core functions
Pure-ish `do_<action>(engine, params) -> dict`; the lifecycle transition core lifts Andreas's idempotent `lifecycle.ts` near-1:1 and stays pure (0 DB).
- `do_seed_asset_from_bom(engine, params) -> dict` — `{bom_line_id, serial, functional_location_id}` → creates the `ASSET` node + `BOM_LINE -[installed_as]-> ASSET` + `lives_in` edge. Called at **install handover** (A2A from Field Tech). Idempotent on serial.
- `do_advance_lifecycle(engine, params) -> dict` — `{asset_id, event}` → applies the **14-state machine** (PROPOSED → QUOTED → ORDERED → RECEIVED → STAGED → INSTALLED → CONFIGURED → VERIFIED → ACTIVE → DEGRADED → MAINTENANCE → EOL → RETIRING → RETIRED). Pure transform (lifts `lifecycle.ts` enrichment); RECEIVED→INSTALLED→VERIFIED sets `warranty` from product warranty terms. Logs transition to ledger.
- `do_pull_telemetry(engine, params) -> dict` — `{asset_id|functional_location_id}` → picks the `TelemetryAdapter` for the asset's `monitoringPlatform` (crestron|qsys|neat|huddly|poly|mock), pulls samples, writes `TELEMETRY` nodes + `monitored_by` edges. **Adapter selected by env-swap**; `mock` is the default working adapter.
- `do_compute_health(engine, params) -> dict` — `{asset_id}` → **the healthScore writer Andreas never built**: fuses latest telemetry + NetBox MTBF (`netbox/mtbf.py`) + open `TICKET`s + age-vs-lifespan into a 0–100 score; persists on the `ASSET` node; flags DEGRADED transition past threshold.
- `do_check_warranty_eol(engine, params) -> dict` — namespace/window → assets with expiring warranty / firmware-EOL / lifespan-exceeded (Watcher feed).
- `do_recommend_replacement(engine, params) -> dict` — `{asset_id}` → Advisor: replacement/refresh recommendation with cognitive recall ("assets like this that failed") + an **upsell signal toward Sales**.
- `do_attach_sla(engine, params) -> dict` — `{functional_location_id, contract}` → creates/links the `SLA` node to the **room** (lifts `service-contract-creator` BOM match); writes `ASSET -[covered_by]-> SLA -[for]-> FUNCTIONAL_LOCATION`.
- `do_generate_asset_qr(engine, params) -> dict` — `{asset_id}` → asset-QR (closes the "pitch-QR only" gap); deep-links the room-centric register entry.
- `do_export_dpp(engine, params) -> dict` — `{asset_id}` → assembles the **Digital Product Passport** payload from the modelled DPP fields (forward-looking; mandatory 2028-29).
- `do_sync_netbox(engine, params) -> dict` — runs `assets/netbox_bridge.py` match cascade → `maps_to` edges + `asset_netbox_mappings` rows (operator).

## MCP tools
Registered in `nce/tool_registry.py` via `_h(...)` late-binding. AI-role tag per roadmap §2 taxonomy.

| Tool | cacheable | admin_only | mutation | AI-role |
|---|---|---|---|---|
| `assets_seed_from_bom` | ✘ | ✘ | ✔ | Actor (install handover) |
| `assets_advance_lifecycle` | ✘ | ✘ | ✔ | Actor |
| `assets_pull_telemetry` | ✘ | ✔ | ✔ | — (operator/cron) |
| `assets_compute_health` | ✘ | ✘ | ✔ | Watcher |
| `assets_check_warranty_eol` | ✔ | ✘ | ✘ | Watcher |
| `assets_recommend_replacement` | ✔ | ✘ | ✘ | Advisor |
| `assets_attach_sla` | ✘ | ✘ | ✔ | Actor |
| `assets_generate_qr` | ✔ | ✘ | ✘ | — (utility) |
| `assets_export_dpp` | ✔ | ✘ | ✘ | — (compliance) |
| `assets_sync_netbox` | ✘ | ✔ | ✔ | — (operator; bridge) |

## REST routes
No-model path for the BFF (room-centric asset register), cron (telemetry pull, health compute), scripts. Mounted via `build_app(extra_routes=...)`, HMAC/mTLS-authed in `nce/admin_handlers/assets.py`:
- `api_assets_register` (GET) — assets by `FUNCTIONAL_LOCATION` (the room-centric customer view).
- `api_assets_health` (GET) — healthScore + telemetry summary for an asset/room.
- `api_assets_seed_from_bom` / `api_assets_advance_lifecycle` (POST) — install-handover surface.
- `api_assets_check_warranty_eol` (GET) — warranty/EOL dashboard.
- `api_assets_generate_qr` (GET) — asset-QR for the register.
- `api_assets_export_dpp` (GET) — Digital Product Passport export.
- `api_assets_sync_netbox` / `api_assets_sync_status` — bridge run + mapping-confidence report.

## AI features
- **Watcher:** health-degradation alerts (the `do_compute_health` writer flips ACTIVE→DEGRADED past threshold), EOL/firmware-end-of-support detection, warranty-expiry race, and **predictive failure** by fusing `netbox/mtbf.py` (already exists) with live telemetry → "this device will likely fail in N days."
- **Advisor:** replacement/refresh recommendation with plain-language rationale; an **upsell signal emitted to Sales** when an asset nears EOL or a room's fleet is aging.
- **Cognitive recall:** "assets like this that failed" — embedding query over past `ASSET` nodes + their outcomes (tickets, short-life, failure-patterns) from `v3_cognitive_ledger`, so a recommendation prefers parts that *delivered well*, not just parts that were *cheap*.
- **Enrichment triggers (event-scoped, never a background sweep, roadmap §5):** telemetry is pulled on a cron *per monitored asset*; health is recomputed *when telemetry arrives or a ticket is filed*; replacement recall fires *when a Watcher alert opens* — never a bulk recompute of the whole fleet.

## A2A flows
- **Receives Install→Asset→Cover (roadmap §5):** Field Tech(12) work-order completion → `assets_seed_from_bom` → `assets_advance_lifecycle` (INSTALLED→VERIFIED, sets warranty) → `assets_attach_sla` (SLA activates on the room).
- **Serves Support(10):** answers `TICKET -[about]-> ASSET` queries with live healthScore + telemetry + warranty/SLA context for triage.
- **Serves Sales(5):** emits upsell/refresh signals (EOL, aging fleet) for the cross-sell motion.
- **Initiates failure-pattern feedback to Product(2):** repeated failure / short-life emits `ASSET -[failure_pattern]-> PRODUCT` — the structural close of Andreas's service→product silence.
- **Bridges NetBox:** `assets_sync_netbox` reconciles `ASSET ⇄ netbox_device`; consumes NetBox DCIM truth (rack/IP/cabling/site) without duplicating it.
- **Feeds Morning-brief (#19 aggregate):** at-risk assets + SLA-breach risk as the Operations slice of the cross-engine risk/opportunity query.

## Config keys
`NCE_ASSETS_*` in `nce/config.py`: `NCE_ASSETS_ENABLED` (per-namespace opt-in via `metadata.assets.enabled = true`), `NCE_ASSETS_NETBOX_*` (reuse the `netbox` vertical's connection for the bridge; fuzzy-match threshold like `NCE_D365_NETBOX_FUZZY_THRESHOLD`), `NCE_ASSETS_TELEMETRY_POLL_INTERVAL_MINUTES`, `NCE_ASSETS_HEALTH_DEGRADED_THRESHOLD`, `NCE_ASSETS_WARRANTY_WARN_DAYS`, `NCE_ASSETS_EOL_WARN_DAYS`. **Telemetry adapter env-swaps** (the mock-now/swap-ready architecture): `NCE_ASSETS_TELEMETRY_<PLATFORM>_REAL` (e.g. `..._CRESTRON_REAL=1` flips mock→real, mirroring Andreas's `CRESTRON_FUSION_REAL=1`) + per-platform `..._URL`/`..._TOKEN` (secret; only required when the real adapter is enabled). Never host-specific keys in `nce/config.py` (FE-5).
**Config-as-IP JSON (namespace-scoped):** `asset-lifecycle.json` (the 14-state transition map + which events set warranty, per §2.9 — each tenant tunes its own), `asset-health-weights.json` (how telemetry / MTBF / open-tickets / age weight into the 0–100 healthScore).

## Tables/migrations
**Graph-first** (ASSET/SLA/TELEMETRY live as `kg_nodes`/`kg_edges`; lifecycle + alert history in `v3_cognitive_ledger`). Two own tables where a fast keyed lookup beats the graph — both `ENABLE` + `FORCE ROW LEVEL SECURITY` + `tenant_isolation_policy USING (namespace_id = get_nce_namespace())`, mirrored into `schema.sql` + a numbered migration:
- `assets` (fast register: `asset_id, namespace_id, serial, functional_location_id, lifecycle_state, health_score, monitoring_platform, warranty_until, firmware, mac, ip, dpp jsonb, netbox_device_id, assets_source_id, updated_at`) — high-volume room-register reads.
- `telemetry_samples` (`asset_id, namespace_id, metric, value, sampled_at, raw jsonb`) — high-write telemetry stream; `TELEMETRY` graph nodes are the indexed summary over these rows.
- `asset_netbox_mappings` (mirrors `d365_netbox_mappings`: match_method/confidence/confirmed) — bridge state; confirmed rows never overwritten by sync.

## Dependencies
- **Upstream engines:** **System Design(6)** / **NetBox** vertical (the `FUNCTIONAL_LOCATION` tree this engine attaches assets to); **Field Tech(12)** (install completion seeds the register); **Product(2)** (warranty terms + the `failure_pattern` consumer); **Agreements(3)** (driftsavtale terms behind the `SLA`).
- **Downstream / served:** **Support(10)** (asset health for tickets), **Sales(5)** (upsell/refresh), **Economy(8)** (SLA → recurring-revenue / MRR recognition).
- **External blockers 🔴:** manufacturer telemetry credentials (Crestron/Q-SYS/Neat/Huddly/Poly) — abstracted behind `TelemetryAdapter` with a working `mock` default, so the engine ships fully usable (lifecycle/health/SLA/QR/DPP all work) before any vendor key arrives. **DPP** standard fields are modelled forward-looking ahead of the 2028-29 mandate.

## Review round-2 hardening (2026-06-17 — these govern the build)
1. **`FUNCTIONAL_LOCATION` is the same foundation gap as System Design — and this is where the intent→as-built model lands (roadmap §9.1).** Every asset hangs off `FUNCTIONAL_LOCATION` (`lives_in`, SLA-per-room), but the `netbox` vertical has **no site/location tree** to attach to. Assets is the node's **3rd toucher** (System Design = intent, NetBox = as-built, Assets = as-built consumer): a **Field-Tech install promotes the design-intent location to as-built**, which *is* the asset's location. The intent→as-built lifecycle must be resolved as **shared infra before either engine works**.
2. **CODEBASE CATCH — investigate the existing `netbox_nce` push plugin BEFORE building a pull bridge.** Verified: `backend/netbox-plugins/netbox_nce/{signals.py,mcp_bridge.py}` is a Django `post_save` plugin that **pushes** Device/IPAddress/Prefix changes into NCE (`sync_netbox_to_nce`). B2's "copy the D365 **pull** match-cascade" would build a **parallel** path. Read the plugin first — a signal-based **push** is the better reactive sync and partially answers both the `FUNCTIONAL_LOCATION` sync gap and the reactive-event meta-finding (roadmap §9.6 item 5). **Prefer extending the push plugin over a new pull bridge.**
3. **`BOM_LINE` 6th toucher + the competing state machine — the hand-off is now specced (roadmap §9.1).** The asset 14-state lifecycle (…`RECEIVED→INSTALLED→VERIFIED`…) overlaps `BOM_LINE.status` (`…DELIVERED→INSTALLED→TESTED`). Resolution: **Field Tech writes the line's terminal status at install AND creates the `ASSET`; `BOM_LINE.status` is truth up to install, the `ASSET` lifecycle governs operational life thereafter.** No two competing "installed" truths.
4. **`healthScore` has the sparse-input trap — expose coverage, don't fake confidence.** The 0–100 fuses telemetry (mock until vendor keys), MTBF, open tickets (Support = same tier, may be absent), and age. At launch it's effectively **age-only** but presented as a fused number. Like Vendors' sparse scorecard: **expose input coverage** ("health 80 — *age-only, no telemetry*") and **predictive-failure must NOT fire on mock telemetry** (grace-degradation).
5. **`SLA` is a 4-way co-owned node (roadmap §9.1) — Assets owns only the per-ROOM coverage link.** `do_attach_sla` creates the coverage link; **Agreements owns the driftsavtale terms, Economy owns the MRR, Support owns the running clock + breach state.** Don't let Assets imply ownership of terms or revenue.

> **Producer note:** Assets is an outcome **producer** (`failure_pattern → Product/Sales`); recall consumers degrade gracefully (Support tickets may be absent at launch — health is age/MTBF-only until they exist).

## Build phases
- **B1 — Lifecycle core + seed:** `assets` package + `mcp_handlers.py` + `TOOL_REGISTRY` entries; lift `lifecycle.ts` → `lifecycle.py` (14-state, pure, sets warranty); `do_seed_asset_from_bom` (`BOM_LINE -[installed_as]-> ASSET -[lives_in]-> FUNCTIONAL_LOCATION`); `assets` table (RLS). Wire `asset-lifecycle.json`.
- **B2 — NetBox bridge:** `assets/netbox_bridge.py` (copy the D365 match-cascade) + `asset_netbox_mappings` (RLS) + `do_sync_netbox` (`ASSET -[maps_to]-> netbox_device`); reuse `NetBoxBridgeClient`.
- **B3 — Telemetry + health writer:** `TelemetryAdapter` interface + mock + env-swap stubs (crestron/qsys/neat/huddly/poly); `telemetry_samples` (RLS); `do_pull_telemetry`; **build the healthScore writer** `do_compute_health` (telemetry + `netbox/mtbf.py` + tickets + age → 0–100), `asset-health-weights.json`.
- **B4 — SLA + Watcher/Advisor:** `do_attach_sla` (driftsavtale per ROOM, lift `service-contract-creator`); `do_check_warranty_eol`; predictive-failure Watcher (MTBF×telemetry); `do_recommend_replacement` with cognitive recall + Sales upsell signal; `failure_pattern` edge to Product.
- **B5 — QR + DPP + compliance:** `do_generate_asset_qr` (closes the gap); model + `do_export_dpp` (Digital Product Passport, forward-looking for 2028-29); room-centric register REST surface; tests + tool-count assertion.
