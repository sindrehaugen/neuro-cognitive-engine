# Findings: OQ-2 Built-but-Unwired Domain Cores

> **Status:** confirmed · **Verified-against:** 7304330 · **Date:** 2026-08-17  
> **Source ground:** `nce/vertical_modules/economy/`, `nce/vertical_modules/procurement/`, `nce/tool_registry.py`, `nce/admin_app.py`  
> **Companion artifact:** `docs/_generated/surface.md` (Surface of Truth)

---

## Executive Summary

An architectural and AST analysis of the Neuro-Cognitive Engine (NCE) codebase at baseline `7304330` reveals exactly **12 pure domain calculation cores (`do_*` functions)** implemented across two critical money-touching vertical modules:
- **9 cores in Economy** (`nce/vertical_modules/economy/`)
- **3 cores in Procurement** (`nce/vertical_modules/procurement/`)

While these functions are implemented with comprehensive unit test coverage and deterministic mathematics, their exposure to the network (via Model Context Protocol `TOOL_REGISTRY` tools and Starlette REST routes) is intentionally partial, restricted, or framed strictly as **read-only dry-run advisors**.

This investigation resolves Open Question 2 (**OQ-2**): *Why are these domain cores in vertical modules but not yet wired to mutating MCP/REST dispatchers?* The finding confirms that this architecture is a **deliberate, fail-closed safety and governance design**, not an accidental integration oversight.

---

## Inventory of the 12 Domain Cores

| Module | Core Function (`do_*`) | Source File | Exposed Tool / Route | Operational Classification |
|---|---|---|---|---|
| **economy** | `do_compute_bucket_targets` | `ngaap.py` | `economy_compute_periodisering`<br>`POST /api/economy/periodisering` | **Wired (Read-Only Advisor)**: Computes Norwegian GAAP 7-bucket revenue recognition over period boundaries. |
| **economy** | `do_compute_dunning` | `dunning.py` | *Internal / Unwired* | **Unwired Pure Calculation**: Maps credit bureau risk scores to statutory Norwegian dunning notice schedules (purring/inkasso). |
| **economy** | `do_compute_recognition_schedule` | `recurring.py` | *Internal / Unwired* | **Unwired Pure Calculation**: Generates 12-month ratable MRR revenue recognition schedules from contract terms. |
| **economy** | `do_emit_financial_event` | `events.py` | `economy_emit_event`<br>`POST /api/economy/emit-event` | **Wired (Dry-Run Validator)**: Validates double-entry journal balance ($\sum \text{amount} = 0 \pm 0.01$) and computes normalized event hash. Storage persistence layer unexposed. |
| **economy** | `do_forecast_cashflow` | `forecast.py` | *Internal / Unwired* | **Unwired Pure Calculation**: Monte Carlo simulation of cashflow distributions across payables and receivables. |
| **economy** | `do_generate_kid` | `peppol.py` | *Internal / Unwired* | **Unwired Pure Utility**: Generates compliant Norwegian KID payment reference numbers with Mod10/Mod11 check digits. |
| **economy** | `do_match_invoice` | `matching.py` | `economy_match_invoice`<br>`POST /api/economy/match-invoice` | **Wired (Read-Only Advisor)**: Evaluates 130-point multi-factor invoice matching and triage (`GREEN`/`YELLOW`/`RED`). Approval cascade mutation unexposed. |
| **economy** | `do_snapshot_mrr_arr_churn` | `recurring.py` | *Internal / Unwired* | **Unwired Read Reducer**: Computes aggregated MRR/ARR/churn metrics from the active `economy_contracts` master store. |
| **economy** | `do_validate_kid` | `peppol.py` | *Internal / Unwired* | **Unwired Pure Utility**: Validates incoming Norwegian KID check digits under Mod10/Mod11 algorithms. |
| **procurement** | `do_calculate_tco` | `tco.py` | `procurement_calculate_tco`<br>`POST /api/procurement/tco` | **Wired (Read-Only Advisor)**: Computes multi-factor Total Cost of Ownership across purchase price, freight, warranty, and delivery risk. |
| **procurement** | `do_rank_suppliers` | `ranking.py` | `procurement_rank_suppliers`<br>`POST /api/procurement/rank` | **Wired (Read-Only Advisor)**: Executes the 5-step sourcing ranking pipeline applying own-stock bonus, lead time, TCO, BID price, and tier/rebate rules. |
| **procurement** | `do_evaluate_three_way_match` | `three_way_match.py` | `procurement_evaluate_match`<br>`POST /api/procurement/match` | **Wired (Read-Only Advisor)**: Evaluates Purchase Order $\times$ Goods Receipt $\times$ Supplier Invoice matching tolerances and 4-level substitution detection. |

---

## Architectural Rationale: Why Cores Remain Unwired or Advisor-Only

### 1. Money-Safety and Read-Only Advisor Posture
In financial and procurement domains, executing automated mutations over public or agent-driven network interfaces poses existential business risk. NCE enforces a strict separation between:
- **Calculation Cores**: Pure, deterministic algorithms that compute optimal sourcing, margin accruals, or invoice triage.
- **Mutating Actuators**: Operations that transfer funds, commit General Ledger entries, or issue legally binding purchase orders.

All 3 exposed Economy MCP tools (`economy_match_invoice`, `economy_compute_periodisering`, `economy_emit_event`) are flagged `cacheable=True` and `mutation=False` in `TOOL_REGISTRY`. They provide advice and dry-run validation without modifying ledger state.

### 2. CFO Policy on External General Ledger Immutability
Under Norwegian accounting regulation (regnskapsloven) and corporate policy, NCE does not replace the legal accounting system-of-record (Finago). NCE operates on a **Permanent Divergence Model**:
- NCE computes operational reality, project accruals, and 7-effect approval cascades internally.
- Normal-mode direct GL posting into Finago is deliberately locked (`NCE_ECONOMY_FINAGO_URL` reader only).
- Financial mutations remain in internal WORM tables (`economy_postings` protected by `trg_economy_postings_assert_balanced` and `GRANT SELECT, INSERT` role permissions). Exposing direct posting tools to LLM agents without human sign-off is expressly forbidden.

### 3. Interactive Tools vs. Scheduled Batch Workloads
Several unwired cores perform scheduled accounting maintenance rather than interactive conversational operations:
- `do_compute_recognition_schedule` and `do_snapshot_mrr_arr_churn` execute as periodic cron/batch tasks during end-of-month financial closing.
- `do_compute_dunning` runs on weekly batch schedules against customer ledger aging tables.
- `do_generate_kid` and `do_validate_kid` serve as internal transformation helpers for EHF/PEPPOL invoice generation and banking integration, not standalone agent tools.

### 4. Absence of Middleware Namespace Opt-In Guard in Economy
Unlike the Product and Agreements engines (which enforce `_guard.py` middleware checks on every handler), the Economy engine currently lacks a standalone `_guard.py`. As a defense-in-depth measure, mutating endpoints are kept unmounted until tenant opt-in gating (`metadata.economy.enabled`) is standardized across all handlers.

### 5. Contract B Governance and Autonomous PO Ceilings (Procurement)
In Procurement, while the 3 core calculation functions (`do_calculate_tco`, `do_rank_suppliers`, `do_evaluate_three_way_match`) are wired to MCP and REST, the lifecycle execution cores (`do_generate_po`, `do_submit_po` in `po.py`) remain unwired to autonomous network dispatchers because:
- **Autonomy Ceiling Gate**: `NCE_PROCUREMENT_AUTONOMY_PO_CEILING` defaults to `0.0` (human confirmation mandatory for all spend).
- **Transport Blocker**: Outbound distributor integration (`NetsetPoTransport`) is a stub raising `NotImplementedError`.
- **Agreements A2A Compliance Gate**: Sourcing decisions driven by supplier rebate maximization (`rebate_override=True`) legally require an A2A compliance verification (`agreements.compliance_audit`) before order dispatch to prevent anti-competitive procurement fraud.

---

## Conclusion & Governance Guidance

1. **Documentation Standard**: Engine guides (`docs/engines/economy-*.md`, `docs/engines/procurement-*.md`) must continue to accurately document the 3 exposed Economy advisor tools and 6 Procurement tools, explicitly describing unwired functions as internal domain logic.
2. **Spec Annotation**: Spec proposals in `docs/vertical_engines/` must be annotated with `<!-- BLOCKED ON OQ-2 / OQ-4: ... -->` to prevent readers and developers from assuming unmounted lifecycle actuators are available on network dispatchers.
3. **Future Wiring**: Exposing any unwired mutating core (e.g. automated PO generation or ledger cascade execution) requires a formal ADR, a namespace opt-in guard (`_guard.py`), and Contract B autonomy governor verification.
