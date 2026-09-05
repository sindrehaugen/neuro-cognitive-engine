# Findings: OQ-4 Spec Voice vs. Guide Voice Divergence

> **Status:** confirmed · **Verified-against:** 7304330 · **Date:** 2026-08-17  
> **Source ground:** `docs/vertical_engines/*.md`, `docs/engines/*.md`, `docs/API.md`, `docs/_generated/surface.md`  
> **Companion artifact:** `docs/_generated/surface.md` (Surface of Truth)

---

## Executive Summary

An audit across the NCE documentation tree reveals a systematic **voice and tense divergence** between historical architectural specifications (`docs/vertical_engines/*.md`) and operational user/admin guides (`docs/engines/*.md`):

1. **Spec Proposal Voice (`docs/vertical_engines/`)**:  
   Authored during initial system design and domain modeling passes (e.g. 2026-06-17). These documents are written in **future / proposal tense** ("Will expose...", "Phase 1 will implement...") and use prescriptive boilerplate phrasing such as *"Registered in `nce/tool_registry.py` via `_h(...)` late-binding"* to specify the target architecture for prospective modules before implementation.
2. **Present Shipped Voice (`docs/engines/`, `docs/API.md`)**:  
   Authored as operational reference documentation for software already merged and running on `main` at baseline `7304330`. These documents are written in **present factual tense** ("Exposes 3 tools...", "The engine provides...").

This investigation resolves Open Question 4 (**OQ-4**): *How should the divergence between prospective spec proposals and shipped guide docs be reconciled without corrupting historical design records?*

---

## Concrete Divergence Patterns

| Engine | Spec Doc (`docs/vertical_engines/`) | Spec Proposal Voice | Shipped Reality at `7304330` | Guide Docs (`docs/engines/`) |
|---|---|---|---|---|
| **Sales** | `05-sales-engine.md` | Claims **14 MCP tools** registered in `tool_registry.py` (`sales_overview`, `sales_draft_quote`, etc.) and 15 REST endpoints. | **2 MCP tools** (`sales_ping`, `sales_get_signed_baseline`) + 15 REST routes (1 public). | `sales-user.md`<br>`sales-admin.md` |
| **Agreements** | `03-agreements-engine.md` | Claims **10 MCP tools** registered (`agreements_extract`, `agreements_compliance_audit`, etc.) + automated OCR promotion. | **1 MCP tool** (`agreements_lookup_terms`) + 5 REST routes. OCR and signing remain internal/gated. | `agreements-user.md`<br>`agreements-admin.md` |
| **Economy** | `08-economy-engine.md` | Claims **10 MCP tools** + automated Finago GL posting and full cascade execution. | **3 read-only MCP tools** + 3 REST routes. 9 calculation cores built; GL posting locked by CFO policy. | `economy-user.md`<br>`economy-admin.md` |
| **System Design** | `06-system-design-engine.md` | Claims **7 MCP tools** for design proposal, SoW generation, NetBox functional location sync, and CAD layout. | **2 MCP tools** (`system_design_ping`, `system_design_publish_design_docs`) + 1 REST route. CAD/interactive layout unexposed. | `system-design-user.md`<br>`system-design-admin.md` |
| **Vendors** | `04-vendors-engine.md` | Claims **6 MCP tools** (4 unlisted in reality). | **10 registered MCP tools** + 2 REST routes. Full reliability radar and contractor matching exposed. | `vendors-user.md`<br>`vendors-admin.md` |
| **Inventory** | `11-inventory-engine.md` | Claims **12-wave complete logistics platform** (WMS, barcode scanning, WEEE, RMA, reorder forecast). | **Wave 1 of 12 shipped** (migration `050`, 2 FORCE-RLS tables: `stock_locations`, `inventory_items`). **0 MCP tools, 0 REST routes**. | `inventory-user.md`<br>`inventory-admin.md` |
| **Procurement** | `01-procurement-engine.md` | Claims **8 MCP tools** including autonomous PO placement (`procurement_submit_po`). | **6 MCP tools** + 8 REST routes. `do_submit_po` is stubbed/gated under Contract B autonomy ceiling. | `procurement-user.md`<br>`procurement-admin.md` |

---

## Analysis: Why the Divergence Occurred

1. **Boilerplate Specification Templates**:  
   The phrase *"Registered in `nce/tool_registry.py` via `_h(...)` late-binding"* was included in the engine spec template (`docs/VERTICAL_MODULE_PATTERN.md`) to instruct future engineers on how tools *should* be registered when implementing the vertical module.
2. **Evolution of Autonomy & Risk Posture**:  
   Early specs envisioned direct autonomous agent execution for tasks like placing supplier purchase orders (`procurement_submit_po`), auto-approving supplier invoices, or generating design quotes. During implementation and security hardening (e.g. Contract B governance, CFO GL lock, money-safety rules), high-blast-radius mutating actions were restricted to human-confirm workflows or internal cron pipelines.
3. **Staged Wave Sequencing**:  
   Engines such as Inventory were specced as comprehensive multi-wave capabilities (Waves 1–12), but baseline `7304330` represents an intermediate release where only foundational data structures (Wave 1 schema seed) have landed.

---

## Reconciliation Strategy & Conventions

To prevent confusion among engineering teams, frontend builders, and AI agents without destroying valuable historical design artifacts:

### 1. Non-Destructive Annotation Protocol
Historical spec files (`docs/vertical_engines/*.md`) MUST NOT be destructively rewritten or stripped of their mathematical formulas, architecture context, or design proposals. Instead, clean HTML comment annotations are placed at the top and within prospective sections:
```html
<!-- BLOCKED ON OQ-2 / OQ-4: SPEC PROPOSAL VOICE. This document is an architectural design specification. At baseline 7304330, [Engine] ships [X] MCP tools and [Y] REST routes (see docs/_generated/surface.md). Unwired cores and unmerged features described below remain prospective. Refer to docs/engines/[engine]-user.md and [engine]-admin.md for shipped reality. Verified-against: 7304330 -->
```

### 2. Hierarchy of Truth
1. **Surface of Truth (`docs/_generated/surface.md`) & `docs/API.md`**:  
   Authoritative, automated, CI-gated inventory of every route actually mounted in `build_admin_routes()` and every tool registered in `TOOL_REGISTRY`.
2. **User & Admin Engine Guides (`docs/engines/`)**:  
   Authoritative narrative documentation of shipped capabilities, configuration parameters, database schemas, and operational runbooks.
3. **Vertical Engine Specifications (`docs/vertical_engines/`)**:  
   Historical design proposals and architecture blueprints, carrying `<!-- BLOCKED ON OQ-2 / OQ-4: ... -->` annotations.
