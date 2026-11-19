> **Status:** shipped · **Verified-against:** 236e4dc (main) · **Last-audited:** 2026-11-19

# NCE Extension Plugins — Plain-Language Overview

> **Project Context:** NCE is an ongoing personal R&D project started in early May 2026. The extension modules below are experimental prototypes (`[Beta]`) built to test how AI assistants can read structured business and engineering systems safely with human approval gates—not turnkey commercial replacements for your existing software.

---

## What Problem Do Plugins Solve?

Out of the box, NCE Core provides secure multi-tenant search, versioned audit logs, and access controls. Optional plugin packages connect that foundation to specific file formats and software platforms (such as **Microsoft Dynamics 365**, **NetBox**, or CAD/BIM files) so an MCP-enabled assistant (like Claude Desktop or Cursor) can look up structured project records and draft updates for human review.

 Plugins are split into two optional R&D packages:

1. **Application Connectors (`nce-app-engines`, `[Beta]`):** 20 standalone connectors for engineering and workspace files/APIs (AutoCAD DXF, Revit/IFC BIM, KiCad, WSCAD, SketchUp, Visio, Draw.io, Power BI, Microsoft 365, and others).
2. **Business & Infrastructure Domain Engines (`nce-vertical-engines`, `[Beta]`):** 29 prototype schemas and workflows covering CRM/ERP sync, network inventory, procurement, agreements, support, and system design.

---

## Example 1: Dynamics 365 CRM Integration (`nce-vertical-engines`)

| Capability | Practical Workflow |
| :--- | :--- |
| **Case & Account Lookup** | Query open support tickets, account records, attached notes, and related history inside a tenant-isolated workspace without copying spreadsheets by hand. |
| **SLA Breach Logging** | Compare ticket timestamps against configured SLA windows and record chronological breach events in NCE's append-only audit log for review. |
| **On-Demand or Scheduled Sync** | Pull incremental updates from Dataverse on a schedule or trigger a targeted refresh for accounts, contacts, incidents, work orders, and agreements. |
| **Tenant-Scoped Isolation** | Enable or disable Dataverse sync per workspace (`namespace_id`) with encrypted credential storage (`AES-256-GCM`). *(Optional workplace/sentiment telemetry hooks exist in schema only and are disabled by default: `NCE_ENABLE_WORKPLACE_TELEMETRY=false`.)* |

---

## Example 2: NetBox Infrastructure Integration (`nce-vertical-engines`)

| Capability | Practical Workflow |
| :--- | :--- |
| **Searchable Topology Graph** | Sync sites, racks, devices, cables, and circuits from NetBox into NCE's graph tables (`kg_nodes` / `kg_edges`) to answer multi-hop topology questions (e.g., *"which devices sit downstream of rack 3?"*). |
| **Inventory Drift Flagging** | Compare observed network inventory against registered NetBox records and queue discrepancies for an engineer to review. |
| **Upstream Dependency Correlation** | Walk directed circuit-to-device dependency edges during an outage to highlight candidate shared upstream links and draft incident notes for NOC review. |
| **CRM-to-Site Cross-Reference** | Link Dynamics 365 customer sites with NetBox functional locations so support engineers can see whether an open ticket coincides with a known site maintenance window or circuit alert. |

---

## Human-in-the-Loop Safety Boundaries

- **Confirm-First Writes (`@governed`):** Actions that create or modify external records default to `pending_approval` until a human operator explicitly confirms them (`confirm=True`) within configured value and rate limits.
- **Qualified Human Review Required:** Any engineering, electrical, accounting, or compliance calculations produced by prototype plugins are drafting aids for qualified specialists, never automated sign-off.

For technical specifications, see [docs/plugin_development.md](plugin_development.md), [`nce-app-engines`](https://github.com/sindrehaugen/nce-app-engines), and [`nce-vertical-engines`](https://github.com/sindrehaugen/nce-vertical-engines).
