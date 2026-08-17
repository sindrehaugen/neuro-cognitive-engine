> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# Engine 19 — Remote Access & RMM (GoTo Resolve) — plan set

**Status:** spec (proposed Engine 19 — Operations axis · **first remote-hands data plane**) · **Owner:** NCE core (Sindre)
**Companions:** `00-ENGINES-ROADMAP.md` (§2 conventions, §4 graph catalogue, §7 spec format, §9 contracts), `99-shared-core-foundation.md` (C1–C9), `10-support-engine.md` (the ticket/SLA/Troubleshooter engine this **feeds**), `09-assets-engine.md` (owns `ASSET`/`TELEMETRY` — reconciliation target), `12-field-tech-engine.md` (the truck-roll this **deflects**), `DATA_SOURCE_MODES.md` (the per-function source switch), the existing **`dynamics365` vertical** (the OAuth-adapter precedent).

## What this is

A **remote-access + RMM engine** that lets an AI (and the operator) see, monitor, and *remediate* managed endpoints through a remote-support vendor — **GoTo Resolve / LogMeIn Resolve** as the first concrete adapter. It is the **first NCE engine whose actions reach out and touch a customer's live endpoint**: launch a remote-control session, run a remediation script, deploy an unattended-access agent, read the RMM alert/patch/AV telemetry stream.

It is source-agnostic by design (GoTo Resolve today; TeamViewer / NinjaOne / ConnectWise a later adapter) behind the same `<vendor>|nce|both` switch Sales and Support use — with one honest asymmetry: **the record/semantic layer flips to native (retained for recall, no migration), but the live data plane is inherently vendor-bound** — you cannot fabricate a remote session or execute a script without the vendor's endpoint agent (see `19-remote-access-rmm-engine.md` §Classification).

## Why it's still a normal vertical engine (not a separate product)

It follows §2 to the letter — thin engine, contributes typed nodes/edges to the one graph, registers `rmm_*` tools dual-surface (MCP + REST), RLS-isolated, config-as-IP. It reuses the D365 vertical's **OAuth-token-manager + OData/REST client + delta-watermark** shape and the Support engine's **proactive-ticket path** verbatim (an RMM alert crossing policy is the exact same event as an Assets telemetry drop). The cross-engine intelligence — **RMM alert → proactive Support ticket → remote-fix-first → dispatch only if remote fails, and every remote resolution captured as a structured fix fact** — **emerges for free** from the shared graph. That last point is the headline: GoTo Resolve session/script outcomes are precisely the structured "what actually fixed it" facts the **Support Troubleshooter was missing** (`10-support-engine.md` hardening #2).

## The one genuine divergence: actions that reach a live customer endpoint

Every other engine's Actor tools write NCE state or submit to a back-office SaaS. This engine's Actor tools **run code on, and open a remote session into, a customer's machine.** Remote script execution is the **sharpest autonomous action in the entire platform** — sharper than Procurement's PO submission or Support's auto-close. It therefore carries the strictest form of Contract B: a **per-script allowlist**, an idempotent-and-reversible requirement, a blast-radius ceiling, a kill-switch, and full WORM audit of every session and every execution — mirroring GoTo Resolve's own zero-trust signature-key model. This is covered in `19c-security-legal-ops.md` and is the gating reason the engine ships **read/monitor-first** (B1–B3) with live actions (B4+) behind the autonomy foundation (C2).

## File set

| File | Covers |
|------|--------|
| `19-remote-access-rmm-engine.md` | **The engine spec in the exact §7 format (the primary artifact).** Mission, classification, graph contribution, core functions, MCP tools, REST routes, AI features, A2A flows, config, tables, dependencies, hardening, build phases. |
| `19a-gotoresolve-api-reference.md` | **The concrete GoTo Resolve / LogMeIn Resolve API investigation** — OAuth model, the API surface areas (sessions, device inventory, alerts, remote execution, patch/AV, users, Notification Channel webhooks), and a **GoTo object → engine function → graph node/edge** mapping table. Flags every endpoint that must be confirmed against the live OpenAPI spec before B1. |
| `19b-smart-features.md` | **The NCE-backend leverage set** — 16 smart features (11 original + 5 gap-surfaced) each grounded in the specific primitive it needs (graph/`SIGNAL_CHAIN`/`SPOF`, `v3_cognitive_ledger`, `replay_reconstruct`/`compare_states`, Empathic Tensor, `list_contradictions`, `a2a_*`, signing), with feasibility verdict, blocked-on engine, and build-phase placement. Contains the **dangerous-composition rule** (self-writing playbook × fleet fan-out) that governs the whole engine. |
| `19c-security-legal-ops.md` | The remote-access threat model: C2/C3/C8 reuse, the script allowlist + blast-radius gate, consent/attended-vs-unattended legal gate, recording retention, zero-trust signature discipline, WORM audit. |

> The engine spec is authored; `19a` (API reference) is authored alongside it as the API investigation the request explicitly asked for. `19c` is stubbed here as the required security companion and should be expanded before B4 (live actions) — the B1–B3 read path is fully specified in the engine doc.

## Cross-engine one-liner (why it earns its place)

- **Consumes Assets(9):** a GoTo-managed endpoint **is an `ASSET`** — reconcile, never create a competing device node (the netops(18) rule).
- **Feeds Support(10):** RMM alerts become proactive `TICKET`s on the path Support already built; remote-session/script resolutions become the **structured fix facts** its Troubleshooter recalls.
- **Deflects Field Tech(12):** remote-first — resolve before the truck rolls; when remote fails, dispatch a `WORK_ORDER` with the full session context attached.
- **Feeds Business Insights(16) / #19 Morning-brief:** endpoints-at-risk, **unmanaged-endpoint coverage gap** (an `ASSET` with no agent = a monitoring blind spot), and the **remote-deflection rate** (truck-rolls avoided = hard ROI).

## Not done here (out of scope of "add the plans")

`00-ENGINES-ROADMAP.md` §3 table and §8 decisions are **not** edited — registering Engine 19 in the master roadmap (and its Module/Batch range in `ENGINE_STATUS.md` + `ML.md`) is a suggested follow-up, not part of filing this plan set. Live GoTo Resolve endpoint paths/scopes must be confirmed against the developer portal's OpenAPI + Postman collection (`19a` §Confidence) before B1 implementation.
