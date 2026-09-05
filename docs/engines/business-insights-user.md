# Business Insights Engine User Guide

The **Business Insights Engine** (`nce/vertical_modules/business_insights/`) delivers high-level management and executive decision support. It synthesizes signals across all operational engines (Economy, Project, Support, Sales, Resources), highlights systemic cross-engine collisions, models forward what-if cashflow scenarios, prepares draft board packs for review, and enables role-scoped natural language queries over corporate health.

---

## 1. Core Surfaces & Executive Workflows

The engine provides five primary surfaces for leadership:

### 1.1 Executive Morning Brief (*12-minutters morgen*)
A consolidated daily briefing spanning the four key operational pillars:
- **Financial Pulse:** Working capital, cash runway, debtor days (DSO), and margin performance.
- **Project Risks:** Milestones, hardware delivery slippage, and installation bottlenecks.
- **Support & SLA Health:** Customer satisfaction, active escalations, and SLA breach trends.
- **Sales Opportunities & Pipeline:** Deal velocity, quote conversions, and pipeline growth.

Every assertion in the briefing is mathematically grounded with provenance links to source records (invoices, tickets, quotes, projects). Claims lacking underlying evidence trigger an ungrounded claim failure.

### 1.2 Risk Radar (Cross-Engine Collision Detection — The Moat)
Isolated engine metrics often hide systemic risks. The Risk Radar correlates cross-domain anomalies to detect systemic operational collisions before they escalate:
1. **Pipeline Surge × Capacity Redlined:** Aggressive sales bookings scheduled against fully booked or unstaffed engineering resources.
2. **Margin Erosion × Dead Stock:** Discounted project quotes pairing with dead/aging inventory without written rebate offsets.
3. **SLA Breach Trend × Renewal Due:** Degrading support response times coinciding with major recurring maintenance contract renewals.

### 1.3 Forward Scenario Modeling (What-If & Monte-Carlo)
Interactive forward simulations evaluate strategic decisions under uncertainty:
- **Monte-Carlo Cashflow Simulation:** Simulates win/loss probabilities, payment delays, and cost volatility across 500+ iterations to produce P10, P50, and P90 ending cash projections.
- **Staffing & Capacity Verification:** Forecasts whether existing team capacity can deliver projected pipeline demand.

### 1.4 Draft Board Pack Generator
Generates a structured quarterly board pack draft incorporating strategic highlights, financial KPIs, sales pipeline health, capacity utilization, and top radar risks.
> [!NOTE]
> Board packs are strictly staged as drafts for human review. No autonomous decisions are made.

### 1.5 Natural Language Query ("Ask Your Business")
Role-scoped Q&A over corporate health, ARR, cash runway, and operational milestones, drawing on the cognitive graph and transactional ledgers.

---

## 2. Red Lines & Compliance Guarantees

The Business Insights Engine is governed by four strict architectural guarantees:

- **BI-1: Structural Person-Grain Barrier (EU AI Act Article 5 / HR Red Line)**
  The data-access layer physically strips person-grain identifiers and refuses ranking, peer scoring, or comparative leaderboards of individual workers. All performance data is strictly aggregated by team, role, department, or period.
- **BI-2: Confidence & Coverage Verification**
  Every finding and dashboard reports an explicit coverage indicator (`based on N engines, M fully reconciled, K with structured attribution`). Findings with insufficient data coverage are flagged rather than asserted.
- **BI-3: Third-Party AI Data Egress Boundary**
  Financial data egress to external LLMs is disabled by default. Egress requires authenticated board-level credentials, an explicit recorded sign-off reference, and full audit logging to the cognitive ledger.
- **BI-4: Day-One Grace Degradation**
  When an upstream engine is unmigrated or unavailable, its metrics degrade gracefully to `"not available yet"` rather than failing or emitting confusing zeroes.
