# Business Insights Engine Admin Guide

The **Business Insights Engine** (`nce/vertical_modules/business_insights/`) delivers management and executive decision support. It correlates cross-engine signals, models forward what-if cashflow scenarios, stages draft board packs, and provides a role-scoped natural language query interface.

---

## 1. Surface of Truth & Implementation Status

### 1.1 Mounted MCP Tools

The engine exposes 6 tools registered in the central tool registry (`nce/tool_registry.py`) and advertised in `nce/mcp_stdio_tools.py`:

| Tool Name | AI Role | Cacheable | Admin Only | Mutation | Description |
|---|---|---|---|---|---|
| `business_insights_morning_brief` | Watcher/Advisor | Y | Y | N | 12-minute executive morning brief with provenance tracing. |
| `business_insights_risk_radar` | Watcher | Y | Y | N | Cross-engine collision detection (risk radar). |
| `business_insights_run_scenario` | Advisor | N | Y | N | Forward scenario modeling with Monte-Carlo simulation. |
| `business_insights_generate_board_pack` | Advisor | N | Y | N | Draft quarterly board pack generator staged for review. |
| `business_insights_kpi_dashboard` | Watcher | Y | Y | N | Multi-domain KPI cockpit and trend analysis. |
| `business_insights_ask_business` | Advisor | N | Y | N | Role-scoped natural language query interface. |

### 1.2 Mounted REST Endpoints

The engine provides 6 endpoints mounted in `nce/admin_app.py` via `nce/admin_handlers/business_insights.py`:

| Method | Route | Description |
|---|---|---|
| `GET` | `/api/business-insights/morning-brief` | Retrieve daily executive morning brief. |
| `GET` | `/api/business-insights/risk-radar` | Query active systemic risk collisions. |
| `POST` | `/api/business-insights/run-scenario` | Execute Monte-Carlo scenario simulation. |
| `GET/POST` | `/api/business-insights/board-pack` | Generate and retrieve structured draft board pack. |
| `GET` | `/api/business-insights/kpi-dashboard` | Retrieve live cockpit metrics and snapshot trends. |
| `POST` | `/api/business-insights/ask` | Natural language business question interface. |

---

## 2. Architecture & Red Lines

### 2.1 BI-1: Structural Person-Grain Barrier (EU AI Act Article 5)
Under Article 5 of the EU AI Act and corporate HR policies, comparative ranking and scoring of individual persons is prohibited. The data-access layer physically strips person identifiers and rejects person-grain groupings. Permitted grouping dimensions are `team`, `role`, `department`, `period`, and `engine`.

### 2.2 BI-2: Coverage & Confidence Indicators
Every synthesized finding calculates the ratio of live and fully reconciled operational engines. When coverage is incomplete, findings are flagged rather than asserted.

### 2.3 BI-3: Third-Party AI Data Egress Boundary
External LLM egress is shipped OFF by default. Egress requires:
1. Executive role authorization (`board` or `executive`).
2. Explicit recorded board resolution reference (`board_signoff_reference`).
3. Permanent audit trail written to `v3_cognitive_ledger`.

### 2.4 BI-4: Day-One Grace Degradation
If upstream operational engines are not landed or temporarily unavailable, affected dashboard slices degrade gracefully to `"not available yet"`, never emitting misleading 0 or empty values.

---

## 3. Database Schema & RLS

Tenant data is stored in PostgreSQL with strict Row-Level Security:

- `business_insights_kpi_snapshots`: Periodic snapshots of aggregated metrics and coverage indicators.
  - Foreign key: `namespace_id REFERENCES namespaces(id) ON DELETE CASCADE`
  - RLS: `tenant_isolation_business_insights_kpi_snapshots`
