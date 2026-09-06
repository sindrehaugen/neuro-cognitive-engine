> **Status:** shipped · **Verified-against:** b75c873 (main) · **Last-audited:** 2026-09-06

# Marketing Engine Admin Guide (Doc 95)

> **Status:** shipped · **Verified-against:** b75c873 (main) · **Last-audited:** 2026-09-06

This guide documents how to enable, operate, and audit the Marketing Engine (`nce/vertical_modules/marketing/`): the namespace opt-in gate, the eight registered MCP tools and their `admin_only`/`mutation`/`cacheable` contract, the nine mounted REST routes, the three RLS-protected tables, the human-approval (MK-1) and consent (MK-4) enforcement mechanics, the SEO/AEO scoring rubric, and — the load-bearing finding of this audit — exactly what "publish" does and does not do. Every claim below is grounded in a specific file/line on `main @ b75c873`; where the design spec (`docs/vertical_engines/14-marketing-engine.md`) promises more than ships, this guide says so.

---

## 1. Enablement

Marketing is namespace-opt-in, enforced by `require_marketing_enabled(pool, namespace_id)` (`nce/vertical_modules/marketing/_guard.py:111-144`):

```sql
SELECT COALESCE((metadata->'marketing'->>'enabled')::boolean, false) AS marketing_enabled
FROM   namespaces
WHERE  id = $1::uuid
```

If the flag is not `true` (or the namespace ID isn't a valid UUID), every MCP tool handler raises `McpError(MCP_SCOPE_FORBIDDEN, ...)` (`mcp_handlers.py:45-51`, `_check_marketing_enabled`) and every REST route returns `409 {"error": "..."}` (e.g. `admin_handlers/marketing.py:86-90`). To enable a tenant:

```sql
UPDATE namespaces
SET    metadata = jsonb_set(metadata, '{marketing,enabled}', 'true'::jsonb, true)
WHERE  id = '<namespace-uuid>';
```

> [!WARNING]
> **None of the design spec's `NCE_MARKETING_*` config vars exist in `nce/config.py` at this commit.** A repository grep for `NCE_MARKETING` returns nothing. The spec (`docs/vertical_engines/14-marketing-engine.md` §"Config keys") describes `NCE_MARKETING_ENABLED`, `NCE_MARKETING_NPS_TESTIMONIAL_THRESHOLD`, `NCE_MARKETING_CASE_STUDY_MIN_OUTCOME_SCORE`, `NCE_MARKETING_REQUIRE_CONSENT`, `NCE_MARKETING_DEFAULT_ANONYMIZE`, `NCE_MARKETING_PUBLISH_TRANSPORT`, `NCE_MARKETING_CANDIDATE_LOOKBACK_DAYS` — **none of these are implemented as environment configuration.** The NPS threshold (`9.0`), the anonymize default (`True`), and the min-outcome-score default (`7.5`) are all hardcoded Python defaults inside the relevant `do_*` functions (`_guard.py:93`, `drafting.py:38`, `candidates.py:45`), not tunable per-tenant config. The two config-as-IP JSON files the spec describes (`marketing-brand-voice.json`, `marketing-content-templates.json`) also do not exist in `nce/config_data/` — the brand voice is the single hardcoded `DEFAULT_BRAND_VOICE` constant in `taxonomy.py:14-35`, shared by every namespace.

---

## 2. MCP tool contract (`nce/tool_registry.py:1202-1252`)

| Tool | cacheable | admin_only | mutation | Handler |
|---|:---:|:---:|:---:|---|
| `marketing_find_case_study_candidates` | Y | N | N | `mcp_handlers.py:54-63` |
| `marketing_draft_case_study` | N | Y | Y | `mcp_handlers.py:66-76` |
| `marketing_request_testimonial` | N | Y | Y | `mcp_handlers.py:79-90` |
| `marketing_capture_testimonial` | N | Y | Y | `mcp_handlers.py:93-104` |
| `marketing_suggest_content` | Y | N | N | `mcp_handlers.py:107-118` |
| `marketing_audit_seo` | Y | N | N | `mcp_handlers.py:121-132` |
| `marketing_approve_content` | N | Y | Y | `mcp_handlers.py:135-146` |
| `marketing_publish_content` | N | Y | Y | `mcp_handlers.py:149-159` |

This is a clean 3/5 split: exactly three tools (`find_case_study_candidates`, `suggest_content`, `audit_seo`) are read-only, cacheable, and open to any authenticated caller; the remaining five are all `admin_only=True` and `mutation=True` together, with no tool mixing one flag without the other.

**Why this split, mechanically:** the MCP dispatch layer (`nce/tool_registry.py:87-108` docstring; enforced in `nce/mcp_stdio_dispatch.py:134` and `nce/mcp_stdio_rpc.py:68-82`) calls `_check_admin(arguments)` before invoking any handler where `admin_only=True` — this validates an `"admin"` scope via `nce.auth._validate_scope` and raises `McpError(MCP_AUTH_FAILED, ...)` on failure. `mutation=True` separately makes the dispatcher bump a global cache-generation counter before serving the call, invalidating any cached reads. Every tool that writes a row (draft, request/capture testimonial, approve, publish) requires both: it must be an authenticated admin call, and it must invalidate cached state. **This is the human-approval-gated content pipeline made structural, not just documented**: the five mutating tools trace the exact draft → (request/capture testimonial) → approve → publish lifecycle, and none of them is reachable by an unprivileged caller. Only the three pure-advisor/watcher reads (find candidates, suggest content, audit SEO) skip the admin check, because none of them writes customer-facing content or moves an artifact toward publication.

**No tool is Autonomous** in the roadmap's AI-role taxonomy — every mutating tool requires an authenticated admin-scoped caller, and `publish` additionally requires a prior recorded human approval (§5). There is no confidence-threshold or auto-escalation path that skips either check.

---

## 3. REST routes (`nce/admin_app.py:1179-1223`, `nce/admin_handlers/marketing.py`)

All routes are mounted on `admin_app`, which is protected end-to-end by `BasicAuthMiddleware` (HMAC + mTLS, `nce/admin_app.py:54,148-159`) — the same posture as every other admin surface in this codebase, not a Marketing-specific auth layer.

| Route | Method | Handler | Backing `do_*` core |
|---|---|---|---|
| `/api/marketing/candidates` | GET | `api_marketing_candidates` | `do_find_case_study_candidates` |
| `/api/marketing/draft` | POST | `api_marketing_draft_case_study` | `do_draft_case_study` |
| `/api/marketing/testimonials` | GET | `api_marketing_testimonials` | *(inline query, no core)* |
| `/api/marketing/testimonials/capture` | POST | `api_marketing_capture_testimonial` | `do_capture_testimonial` |
| `/api/marketing/suggest-content` | POST | `api_marketing_suggest_content` | `do_suggest_content` |
| `/api/marketing/audit-seo` | POST | `api_marketing_audit_seo` | `do_audit_seo` |
| `/api/marketing/approve` | POST | `api_marketing_approve_content` | `do_approve_content` |
| `/api/marketing/assets` | GET | `api_marketing_assets` | *(inline query, no core)* |
| `/api/marketing/publish` | POST | `api_marketing_publish_content` | `do_publish_content` |

Every mutating route (`draft`, `testimonials/capture`, `approve`, `publish`) calls `bump_mcp_cache_generation(engine, route=...)` after a successful write (e.g. `admin_handlers/marketing.py:197`), keeping REST-side mutations and MCP-side cache invalidation on the same counter.

### 3.1 `/api/marketing/assets` — what it actually serves
There is no `marketing_assets` MCP tool and no `do_get_marketing_assets` core. `api_marketing_assets` (`admin_handlers/marketing.py:453-525`) is a self-contained, paginated `SELECT` directly against `content_assets` (filterable by `kind` and `status`, default `limit=50`/`offset=0`, capped at 200), used for the brand-asset / content-library listing surface the spec describes. It exists purely as a REST convenience for an admin UI — there is no 1:1 MCP equivalent to expose the same listing to an MCP-native caller today.

### 3.2 `/api/marketing/testimonials` (GET) — also core-less
Similarly, `api_marketing_testimonials` (`admin_handlers/marketing.py:214-287`) queries `testimonials` directly (filterable by `status`, `customer_id`) rather than calling into a `do_*` function — there is no corresponding MCP tool for *listing* testimonials, only for creating (`marketing_request_testimonial`) and capturing (`marketing_capture_testimonial`) them.

### 3.3 Two `do_*` cores with no route or tool at all
Per `nce/config_data/internal-cores.json:142-151`, two functions are formally tracked as unwired:

| Core | File | Tracked owner | Reason given |
|---|---|---|---|
| `do_query_marketing_a2a` | `brief.py:138-201` | `MLV15D-M1` | "A2A marketing intelligence query dispatcher; scheduled for A2A skill registration in MLV15D." |
| `do_retract_testimonial` | `testimonials.py:296-368` | `MLV15D-M2` | "Customer testimonial retraction core; scheduled for admin tool surface in MLV15D Wave M-2." |

A third function, `get_marketing_morning_brief_slice` (`brief.py:24-135`), is likewise unwired — no MCP tool, no REST route — and is **not** actually called from the real #19 Executive Morning Brief aggregator: a grep of `nce/vertical_modules/business_insights/brief.py` (the aggregator's implementation) for `"marketing"` returns nothing. The module docstring's claim that this exposes "the marketing throughput slice for the Executive Morning Brief (#19)" is aspirational — it is unit-tested (`tests/unit/test_marketing_events_brief.py`) and importable, but not yet consumed by the actual brief. All three functions are exported from `nce/vertical_modules/marketing/__init__.py` and reachable only by direct Python import.

---

## 4. Database & RLS (`nce/migrations/069_marketing_engine.sql`)

Three tables, all `ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY` with an identical `tenant_isolation_policy USING/WITH CHECK (namespace_id = get_nce_namespace())` for role `nce_app`, and identical `REVOKE ALL … GRANT SELECT, INSERT, UPDATE, DELETE` blocks (migration lines 45-60, 92-107, 136-151). As the migration's own header comment notes, the live environment connects as `mcp_user` (`rolsuper=true`, bypasses RLS) — so these policies are defense-in-depth, and every application query must still carry an explicit `WHERE namespace_id = $1` predicate (verified true of every query in this module).

| Table | Status enum | Notes |
|---|---|---|
| `case_studies` | `draft, in_review, approved, published, retracted` | `approver`, `approved_at`, `marketing_source_id`, `raw jsonb`. `CHECK (btrim(title) <> '')`. |
| `testimonials` | `requested, received, approved, declined, retracted` | `consent bool`, `consent_tier CHECK IN ('none','web_retractable','ai_citable_irrevocable')`, `consent_scope jsonb`, `nps_at_capture numeric(4,2)`. |
| `content_assets` | `draft, approved, published, archived` | `kind CHECK IN ('case_study','testimonial','blog','brand','drip')`, `seo jsonb`, `storage_uri`. |

All three carry a nullable `marketing_source_id` (indexed, `WHERE marketing_source_id IS NOT NULL`) — the tag used to cascade a testimonial retraction to derived content assets in `do_retract_testimonial` (`testimonials.py:334-346`).

None of the three tables are append-only/WORM at the grant level (unlike, e.g., Sales' `sales_signed_baselines` — see `docs/engines/sales-admin.md` §7.1); `nce_app` holds full `UPDATE`/`DELETE` on all three. The append-only guarantee for consent/approval history lives instead in `v3_cognitive_ledger` (written from `approval.py:106-134`, wrapped in a `try/except: pass` so a missing ledger table in a test environment does not break approval), not in the state tables themselves.

---

## 5. The human-approval gate (MK-1) and consent gate (MK-4) — mechanics

### 5.1 Approve (`do_approve_content`, `approval.py:25-158`)
Requires `namespace_id`, `artifact_id`, a non-blank `approver` string (`ValueError` if blank — this is the whole point of MK-1, so it is enforced with a plain value check, not just documented), and `decision ∈ {approved, rejected, changes_requested}`. It first attempts `UPDATE case_studies SET status=..., approver=..., approved_at=now() WHERE namespace_id=$1 AND id=$2`; if that returns the asyncpg command tag `"UPDATE 0"` (no matching row), it falls back to `UPDATE content_assets SET status=...` — so the same tool transparently approves either artifact type based on which table actually has the row. Only `decision="approved"` sets `status="approved"`; every other decision leaves the row at `"draft"`. A best-effort `v3_cognitive_ledger` insert records `approver`/`decision`/`notes`, wrapped so a missing ledger table doesn't fail the approval. Emits `marketing_content_approved` (`events.py:29`).

### 5.2 Publish (`do_publish_content`, `publish.py:40-231`)
Reads the target row from `case_studies` first, then `content_assets` if not found. Enforces, in order:
1. **MK-1 human gate** (`publish.py:135-144`): `status != "approved"` **or** no `approver` recorded → `MarketingUnapprovedPublishError`. Both conditions must be satisfied; approval alone with a blank approver string still refuses.
2. **MK-4 consent gate** (`publish.py:146-163`): only checked *if* `is_customer_content` is truthy (from the row or from `params`) — if so, `consent` must be truthy or it raises `MarketingConsentMissingError`. Note this means a case study or asset that never sets `is_customer_content` skips the consent check entirely; the gate is opt-in per artifact, not a blanket requirement on every publish call.
3. **MK-3 redaction** (`publish.py:165-171`): `assert_no_sensitive_financials` re-run against the full row/params dict immediately before publish, as a second check independent of the one at draft-assembly time (§6 in the user guide).

On success it updates status to `published` in *both* `case_studies` and `content_assets` unconditionally (two `UPDATE` statements, each naturally a no-op on the table that doesn't hold the row), emits `marketing_content_published`, and returns an `export_payload`.

---

## 6. The publish finding — internal status flip, not a live integration

This is the central operational fact for this engine, mirroring the honesty standard in `docs/engines/sales-admin.md` §5:

> [!CAUTION]
> **`marketing_publish_content` / `do_publish_content` (`nce/vertical_modules/marketing/publish.py:40-231`) does not push content to any external website, CMS, or answer-engine feed.** `PublishTransport` (`publish.py:35-38`) is an `Enum` with two members: `MANUAL` and `CMS`.
> - `transport="cms"` (or the design spec's default expectation of a live CMS adapter) hits `publish.py:73-76` and raises `NotImplementedError("CMS publish transport is deferred (see roadmap §6). Use manual export.")` immediately — **the REST handler surfaces this as HTTP 501** (`admin_handlers/marketing.py:568-569`). There is no CMS client, no website API call, no webhook, anywhere in this module.
> - `transport="manual"` (the default) does exactly three things: (1) flips `status` to `'published'` in the database, (2) appends a `marketing_content_published` event to `event_log`, and (3) returns an `export_payload` dict (`title`, `body`, `published_at`, `approver`, `format: "markdown"`) — text a human is expected to copy into whatever channel the tenant actually publishes through (`publish.py:215-231`).
>
> In other words, **"published" in this engine's data model means "a human has signed off and the system considers this artifact retired from the review queue" — not "this is now live on the internet."** The design spec's AEO/GEO ambition (§14a: publish JSON-LD as an AI-citable, MCP-queryable channel) is not implemented as a delivery mechanism; `do_audit_seo` generates JSON-LD (§7) but nothing in this codebase ships it anywhere external. If an operator or customer asks "is this case study actually live on our site," the honest answer from the code alone is: only if a human took the `export_payload` and posted it themselves.

---

## 7. SEO/AEO audit mechanics (`do_audit_seo`, `advisor.py:201-380`)

A deterministic, non-LLM scoring rubric — not a call to an external SEO API or LLM:

- **Metric detection** (`advisor.py:29-32`): a regex for numbers followed by `%`, `ms`, `db`, `khz`, `ghz`, `gbps`, `mbps`, `kpi`, `nps`, `hours`, `days`, `weeks`.
- **Structure detection** (`advisor.py:33-41`): counts hits against 7 keywords (`challenge`, `solution`, `outcome`, `result`, `design`, `architecture`, `verified`) case-insensitively; `has_structure` requires **≥ 2** hits.
- **Citation detection**: substring match for `"urn:nce:"`, `"evidence"`, or `"verified"` anywhere in the content.
- **Score** starts at `30`, then `+20` if `len(title) >= 10`, `+20` if `has_metrics`, `+15` if `has_structure`, `+15` if `has_citations`, clamped to `[0, 100]`.
- **Readiness bands:** `>= 80` → `"ready"`; `>= 50` → `"needs_improvement"`; else `"poor"`.
- Generates a fixed-shape `schema.org/TechArticle` JSON-LD block (`advisor.py:333-353`) — `author.name` is hardcoded to `"NCE Integrator"` and `about` is a hardcoded 3-item list (`AV-over-IP`, `Dante Audio Networking`, `Enterprise Unified Communications`) regardless of the actual content's subject matter. If you audit content about an unrelated topic, the JSON-LD `about` field will still claim these three AV-industry topics.
- If `asset_id` is supplied and the pool is connected, the full report is written back to `content_assets.seo` (`advisor.py:364-380`) and, on the REST route only (not the MCP tool), triggers a cache-generation bump.

Content is fetched from `case_studies.body` first, then `content_assets.seo` if no case study matches; `assert_no_sensitive_financials` runs against whatever text/dict is ultimately audited (`advisor.py:274-279`), so an audit request carrying a forbidden financial key (e.g. `margin`) in a `content` dict will hard-refuse before scoring.

---

## 8. Guard rail reference (`_guard.py`)

| ID | Guard | Function | Raises |
|---|---|---|---|
| MK-1 | Human gate on publish | enforced in `publish.py:135-144` (no dedicated `_guard.py` assert; the check is inline) | `MarketingUnapprovedPublishError` |
| MK-2 | Retrieval-grounded claims | `assert_claims_grounded` (`_guard.py:76-89`) | `MarketingUngroundedClaimError` |
| MK-3 | No margin/cost/rate leakage | `assert_no_sensitive_financials` (`_guard.py:67-73`), keyed off `FORBIDDEN_FINANCIAL_KEYS` (12 field names, `_guard.py:49-64`) | `MarketingSensitiveDataLeakError` |
| MK-4 | Structured consent | `assert_consent_allows_tier` (`_guard.py:101-108`, unused by any current caller — publish and capture both do their own inline consent checks instead) + inline checks in `publish.py`/`testimonials.py` | `MarketingConsentMissingError` |
| MK-5 | Positive-NPS-only testimonial trigger | `assert_positive_nps_only` (`_guard.py:92-98`, threshold `9.0`) | `MarketingLowHealthTriggerError` |

Note that `assert_consent_allows_tier` is defined but never called from any `do_*` function or handler in this snapshot — the actual consent enforcement at capture time (`testimonials.py:189-193`) and publish time (`publish.py:160-163`) is a simpler inline truthiness check on `consent`, not a tier-comparison against this helper. The two-tier distinction (`web_retractable` vs `ai_citable_irrevocable`) is captured and stored, but nothing in the code currently *branches* on which tier is present — both tiers satisfy the same boolean consent check at publish.

---

## Appendix: drift/gaps flagged during this audit (not fixed)

1. **`cms` publish transport is a stub**, not a live integration — `NotImplementedError` on every call (§6).
2. **Three `do_*` cores have no MCP tool and no REST route**: `do_query_marketing_a2a`, `do_retract_testimonial` (both tracked in `internal-cores.json`), and `get_marketing_morning_brief_slice` (untracked there, and not actually wired into the real #19 brief aggregator) (§3.3).
3. **No self-service testimonial retraction path** — `do_retract_testimonial` exists and is unit-tested but unreachable except by direct Python call (user guide §4.2).
4. **All `NCE_MARKETING_*` config vars from the design spec are unimplemented**; effective thresholds (NPS ≥ 9.0, min outcome score 7.5, anonymize-by-default) are hardcoded Python defaults, not environment-tunable (§1).
5. **No per-tenant brand-voice or content-template config files** — `marketing-brand-voice.json` / `marketing-content-templates.json` don't exist; every namespace shares the single hardcoded `DEFAULT_BRAND_VOICE` in `taxonomy.py` (§1).
6. **`assert_consent_allows_tier` is dead code** — defined in `_guard.py` but never called; consent enforcement is a simpler inline boolean check that doesn't distinguish the two consent tiers at the enforcement point (§8).
7. **`/api/marketing/assets` and `/api/marketing/testimonials` (GET) have no MCP tool equivalents** — they query their tables directly in the REST handler rather than through a `do_*` core, so an MCP-only caller cannot list assets or testimonials, only create/capture/approve/publish them (§3.1, §3.2).
8. **SEO audit JSON-LD `about`/`author` fields are hardcoded** to AV-industry placeholders regardless of the audited content's actual subject (§7).
