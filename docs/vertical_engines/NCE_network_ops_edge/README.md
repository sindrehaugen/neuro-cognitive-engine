> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# Engine 18 — Network Ops (Edge) — plan set

**Status:** spec (proposed Engine 18 — Operations axis · **first edge form factor**) · **Owner:** NCE core (Sindre)
**Companions:** `00-ENGINES-ROADMAP.md` (§2 conventions, §4 graph catalogue, §7 spec format, §9 contracts), `99-shared-core-foundation.md` (C1–C9), `12-field-tech-engine.md` (the bespoke-client + offline-sync + partner-scope pattern this reuses), the existing **`netbox` vertical** + Assets(9).

## What this is

A network-ops engine that lets an AI operate on **live network insight** — SNMP,
syslog, SNMP traps, packet capture (NPCAP/Wireshark), and Zeek — reconciled
against documented intent. It is the **first NCE engine whose data plane runs
off-platform**: an on-site **edge collector** does the local collection and
live tool execution; the engine in NCE core does the thinking, owns the graph
contribution, and exposes the MCP/REST surface.

## Why it's still a normal vertical engine (not a separate product)

It follows §2 to the letter — thin engine, contributes typed nodes/edges to the
one graph, registers `netops_*` tools dual-surface (MCP + REST), RLS-isolated,
config-as-IP. The **only** divergence from the pattern doc is the **edge data
plane**, and even that reuses an existing NCE shape (Field Tech's bespoke-client
versioned-sync + C3 external-principal RLS). The cross-engine intelligence
(network fault → Support ticket → Field Tech WO) **emerges for free** from the
shared graph — the whole reason to do this in NCE.

## File set

| File | Covers |
|------|--------|
| `EDGE_MCP_WORKER.md` | **The edge-worker platform (two profiles).** Grounded in real code — `go/launch` modes, the Fargate orchestrator/worker IAM split, `start_worker.py` (RQ + crash recovery), `go/hardware/detect.go`. The two versions = on-prem customer + local dev/super-consumer, as one runtime in a fourth `ModeEdge`. **Read this first.** |
| `18-network-ops-edge-engine.md` | The engine spec in the exact §7 format (the primary artifact) |
| `18a-edge-node-and-collector.md` | The netops collector as a concrete Profile-A instance of `EDGE_MCP_WORKER.md`: on-site agent, outbound uplink, versioned contract, C3 edge-scope, offline/idempotent replay |
| `18b-data-paths.md` | The five sources, adapted: netbox-via-vertical, SNMP, syslog/traps, Zeek/flow, capture |
| `18c-correlation-and-reconciliation.md` | Deep-AI correlation + intent-vs-observed divergence discipline (C5/C1 reuse), topology, drift |
| `18d-security-legal-ops.md` | C2/C3/C8 reuse, capture legal gate, retention, edge hardening |
| `18e-roadmap.md` | Build phases B1–B6 (RL-batch sized) + foundation (C1–C9) dependencies + Veidekke pilot |

## Adaptations from the original draft (`C:\Claude\nce-netops-edge\plans\`)

The first draft was framework-agnostic. This set re-grounds it in NCE:

- **Netbox** → consume the existing `netbox` vertical's projections via A2A; do **not** re-ingest (Contract A §9.1; carve-out §2.10).
- **Device identity** → a managed device **is an `ASSET`** (owned by Assets(9)/netbox). Network-ops references it and contributes observability; it never creates a competing device node. Observed↔documented matching is a **C1** client.
- **Federation direction** → **outbound push from the edge** (the `netbox_nce` / field-app precedent), not an inbound relay. Live/interactive tools use an outbound-initiated **command channel**.
- **Edge collector** → reuses Field Tech's **versioned sync contract + offline queue + idempotent replay (server-sequence ordering, not device-clock LWW)** and **C3 external-principal RLS** (the collector is an external principal). Does **not** copy the `netbox_nce` signal-handler antipattern (audit `98`); generalises the **C4 outbox** instead.
- **Reconciliation** → intent (netbox) vs observed (SNMP/capture) is a **§9.2 divergence log**, not a bespoke diff.
- **Autonomy** → capture / SNMP-SET / netbox-writeback pass through the **C2** wrapper; read-only by default per the AI-role taxonomy (§2).

## Open decisions (deferred — user dismissed the prompt 2026-06-18)

- **D1** v1 path order — recommend read+reconcile first (B2–B3).
- **D2** federation direction — **resolved by precedent**: outbound push + command channel.
- **D3** code reuse — it's a core vertical; reuses C1–C9, no fork.
- **D4** edge agent stack — see `18a` (Go single-binary attractive for the collector; the *engine* is Python NCE-native like every vertical).

## Not done here (out of scope of "add the plans")

`00-ENGINES-ROADMAP.md` §3 table and §8 decisions are **not** edited — registering Engine 18 in the master roadmap is a suggested follow-up, not part of filing this plan set.
