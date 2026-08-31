> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# Edge MCP Worker — deep design (two profiles)

**Status:** design (platform capability — netops is its first concrete consumer) · **Owner:** NCE core (Sindre) · **Date:** 2026-06-18
**Grounded in real code:** `go/launch/{run,local,multiuser,cloud_mode}.go`, `go/hardware/detect.go`, `start_worker.py`, `trimcp-infra/aws/modules/fargate-worker/main.tf`, `nce/{outbox_relay,dead_letter_queue,tasks}.py`. Shared-core: `99-shared-core-foundation.md` (C1–C9). Client pattern: `12-field-tech-engine.md`.

> **Scope note.** This is a *platform* doc that happens to live in the netops folder because netops is the flagship on-prem consumer. If it survives review it likely wants to graduate to `docs/` proper (alongside `DATA_SOURCE_MODES.md`).

---

## 0. Thesis (one sentence)

**An edge MCP worker is the existing untrusted `worker` role (`start_worker.py`) detached from the cloud VPC and run at the edge, reaching the control plane over an authenticated *outbound* uplink instead of in-VPC IAM — and the "two versions" are two deployment profiles of one runtime, differing only in trust tier, tool pack, and *why* they're at the edge.**

Almost nothing here is new. The launcher already mode-switches; the worker is already the isolated, idempotent, crash-recovering executor of "untrusted MCP integration execution"; the hardware shim already runs local accelerators. The genuinely new piece is **one component**: the outbound uplink that replaces the in-VPC orchestrator API (§7).

---

## 1. The substrate that already exists (do NOT reinvent)

### 1.1 Launch modes — the spine the edge mode hangs off
`go/launch/run.go` dispatches on a mode file written by the installer:

| Mode | File | What it does today |
|------|------|--------------------|
| `ModeLocal` | `local.go` | `docker compose -f docker-compose.local.yml up -d --wait`, then starts **`start_worker.py` as a child** (`local.go:74`, tied to root ctx), then `runMCPServer`. Full stack on the user's machine. |
| `ModeMultiuser` | `multiuser.go` | TCP-probes a shared `PG_DSN` (VPN-aware retry prompt), then `runMCPServer`. No local worker, no compose — a thin client onto a shared on-prem DB. |
| `ModeCloud` | `cloud_mode.go` | MSAL/Azure-AD OAuth refresh + Postgres **TLS** check, then `runMCPServer`. Managed cloud DB. |

The edge worker is **a fourth mode** (`ModeEdge`) on this same dispatcher — or, for Profile B, an extension of `ModeLocal`. Same installer-writes-mode-file mechanism, same `runMCPServer` tail.

### 1.2 The orchestrator/worker trust split — the safety model
`fargate-worker/main.tf` already encodes the boundary the edge needs:

| Role | IAM (today, in-VPC) | Command |
|------|---------------------|---------|
| **Orchestrator** (control plane) | **all** DB secrets, **full** S3 (`main.tf:63-95`) | `python server.py` |
| **Worker** (untrusted MCP exec) | **scoped S3 prefix only** (`worker_s3_prefix/*`), **worker-only** secrets (e.g. a scoped doc-store user) — **NOT** RDS/cache master (`main.tf:101-157`) | `python start_worker.py` |

The worker comment is literal: *"untrusted MCP integration execution."* This is exactly the posture an off-cloud worker must keep — and tighten, because the edge is *more* hostile than a private subnet (§8).

### 1.3 The worker runtime — already crash-safe and idempotent
`start_worker.py`: an RQ worker over **priority lanes** (`high_priority` → `batch_processing` → `default`), wrapped as `RecoveringWorker` — on every maintenance tick it sweeps each lane's `StartedJobRegistry` and **requeues abandoned in-flight jobs** (worker-death recovery), because all NCE job classes are idempotent/re-derivable. An edge worker that loses power mid-capture or mid-poll inherits this for free; the offline/replay story (§7) is the same property pushed to the network boundary.

### 1.4 Hardware detection — the local-inference hook (Profile B's whole point)
`go/hardware/detect.go`: `DetectHardware()` probes CUDA/ROCm/IntelNPU/IntelXPU/MPS under a 5 s budget; `SuggestedBackend` → `NCE_BACKEND`, matching `nce.embeddings.detect_backend` (CUDA→ROCm→XPU→OpenVINO-NPU→MPS→CPU). **Embeddings already run on the local accelerator.** That is the concrete, shipped foundation of the token-offload argument (§5/§9) — not a wish.

### 1.5 Event/job plumbing — `nce/outbox_relay.py` (transactional outbox + DLQ), `nce/dead_letter_queue.py`, C4 (`99-...md`). The uplink rides these, not a new bus.

---

## 2. The invariant (must hold identically in every profile)

Whatever the profile, an edge worker:

1. **Holds no control-plane master credentials** — no RDS/cache master, no full-S3. (1.2 generalized.)
2. **Reaches durable data only via the orchestrator API**, never a direct DB connection. In-VPC this is network reachability + IAM; at the edge it is the §7 uplink + a per-edge token.
3. **Writes blobs only under its own scoped prefix** (per-edge, mirrors `worker_s3_prefix`).
4. **Runs only the tools its capability grant binds** — the Field Tech §2 lesson: a partner/edge agent has *only* its safe tools *registered*; dangerous tools are absent, not merely guarded.
5. **Mutates only through the C2 autonomy wrapper** (ceiling + idempotency key + ledger) — and the idempotency key holds **across the uplink and across replay** (Field Tech hardening #2).
6. **Is scoped by C3 external-principal RLS** — the edge is an external principal; deny-when-unset.
7. **Audits every act to `v3_cognitive_ledger`.**

If a profile can't keep all seven, it isn't an edge worker — it's a control-plane node in the wrong place.

---

## 3. The fourth mode: `ModeEdge` (two sub-profiles)

```
installer writes mode file ─► go/launch/run.go
   ModeLocal      → local.go        (dev: compose + child worker + MCP server)   ── Profile B base
   ModeMultiuser  → multiuser.go    (shared on-prem DB client)
   ModeCloud      → cloud_mode.go   (managed cloud)
   ModeEdge       → edge.go  (NEW)  → starts a worker bound to the UPLINK, not PG_DSN
                                      sub-profile: customer | local
```

`edge.go` differs from `local.go` in one structural way: **there is no local orchestrator and no `PG_DSN`.** The worker's "control plane" is the remote orchestrator reached over the §7 uplink. It still uses `DetectHardware()` (local backend), still runs the `RecoveringWorker` loop, still buffers to a local Redis/queue when offline.

---

## 4. Profile A — on-prem customer edge worker

**Runs on:** an IT-provisioned mini-PC/VM at the customer site (the Veidekke M4350/PR460X LAN is the pilot).
**Why at the edge:** *data-plane locality* — the resources only exist on the customer network. You cannot SNMP-poll, receive syslog/traps, or packet-capture a customer LAN from the cloud.
**Flagship tool pack:** `netops` (SNMP / syslog / traps / Zeek / NPCAP capture — see `18b-data-paths.md`) plus any customer-scoped MCP integrations (their on-prem systems).
**Trust tier:** the **most** untrusted worker in the fleet — third-party premises, physically outside the tenant's control. Gets the strictest §8 posture.
**Data egress:** raw stays on-site (pcap **never** leaves; syslog/flow summarized). Only events, findings, and scoped metadata cross the uplink. This is both a bandwidth and a legal/privacy control (`18d`).
**Channel:** outbound only — survives customer NAT/firewall with no inbound rule (the `netbox_nce` push + field-app precedent).
**Lifecycle:** signed installer, remote-managed via the uplink (pause collectors, rotate device creds, revoke capability), egress-allowlisted to the orchestrator + the customer's own devices.

This profile **is** the netops engine's data plane. The engine (Engine 18) lives in core; this worker is its hands on the wire.

---

## 5. Profile B — local dev / super-consumer edge worker

**Runs on:** a developer's or power-user's own workstation (extends `ModeLocal`).
**Why at the edge — two reasons, both real:**

1. **Local resources.** MCP integrations execute against *local* state — the dev's filesystem, repos, local services, local browser, localhost DBs — with no cloud round-trip. The worker is literally where local MCP tool calls run.
2. **Token-cost offload (the "super-consumer" case).** Heavy users — devs running agents all day, and especially the **ML.md flash-agent promptwave runner** building the engines, and `process_code_indexing` / `reembedding_migration` over big corpora — burn cloud tokens. The local worker already computes **embeddings on the local accelerator** (§1.4). The extension is **model-tiering**: route the cheap, high-volume legs (retrieval, routing/classification, embedding, draft, lint-style passes) to a **local model on the detected backend**, and reserve **cloud Opus/Sonnet for the hard reasoning**. Same `high_priority`/`batch` lane split (`start_worker.py`) maps cleanly onto local-vs-cloud tiering.

**Trust tier:** employee/namespace principal — semi-trusted (not adversarial like Profile A, but still no control-plane master creds; the invariant §2 holds).
**Data egress:** local artifacts stay local; only results/embeddings/ledger entries sync up.
**Lifecycle:** self-service — the dev runs the same launcher/installer in `ModeLocal`+edge; no IT provisioning.

> The two profiles share **one binary and one runtime**; they diverge in *mode file*, *bound tool pack*, *trust scope*, and *whether local inference is the point or a bonus*.

---

## 6. Side-by-side

| Dimension | A — on-prem customer | B — local dev / super-consumer |
|---|---|---|
| Mode | `ModeEdge` (customer) | `ModeLocal` + edge uplink |
| Host | customer mini-PC/VM | dev/power-user workstation |
| Reason for edge | data-plane locality (their network/systems) | local resources **+ token offload** |
| Trust (C3) | most untrusted (3rd-party premises) | employee/namespace principal |
| Tool pack | `netops` + customer integrations | dev/local MCP tools |
| Local inference | optional | **primary** (tiering on local backend) |
| Raw data egress | none (pcap stays); summaries only | none (local artifacts stay); results only |
| Channel | outbound uplink (NAT-pierced) | outbound uplink |
| Secrets held | device creds (SNMPv3) in local vault | local tool/model creds |
| Provisioning | IT, signed installer, remote-managed | self-service |
| Fleet shape | one per site | one per user |

---

## 7. The one genuinely new component — the control-plane uplink

In-VPC, "reach data via the orchestrator API" is just network + IAM. At the edge it must become an **authenticated, outbound-initiated uplink** that carries three flows:

- **Job pull** — the worker long-polls/streams its lanes from the remote orchestrator (the RQ lanes of §1.3, projected over the wire) instead of a local Redis-only `BLPOP`.
- **Command/response** — for live/interactive tools that *must* run on-site (SNMP get, `capture_start`, fetch pcap): the orchestrator enqueues a command; the worker executes and returns. This is the "MCP integration executes at the edge" path.
- **Result/telemetry push** — results, events, findings, ledger entries flow up; rides the **outbox** (`outbox_relay.py`) + DLQ semantics already in core.

Design rules:

1. **Outbound only.** The edge dials the orchestrator; nothing inbound. NAT/firewall-friendly by construction (the `netbox_nce` / field-app precedent). Resolves the old "federation direction" fork by *precedent*, not invention.
2. **mTLS + per-edge identity.** Each edge has a cert; the uplink is mutually authenticated; the worker presents a per-edge token that maps to its capability grant and C3 scope.
3. **Versioned contract.** The uplink envelope is a **versioned external contract** between a non-core client and core — exactly Field Tech hardening #3 (its app↔engine sync protocol). Old worker + new orchestrator must not silently corrupt.
4. **Offline buffer + idempotent replay.** When the uplink drops, the worker keeps collecting/executing and buffers results locally (bounded, drop-oldest + surfaced counter). On reconnect it replays — and because every job is idempotent (§1.3) and every mutation carries a C2 idempotency key (§2.5), replay is safe. `RecoveringWorker` already does the in-process half.
5. **Do NOT copy `netbox_nce`'s mechanism.** Audit `98` flags that plugin's hardcoded namespace + `asyncio.run`-in-signal as the antipattern. Generalize the **C4 outbox** for emit, and a clean async client for pull — not a signal-handler bridge.

This uplink is the ~80% of the build that's actually new. Everything else is configuration and tool-pack binding over existing parts.

---

## 8. Isolation & trust — generalize the Fargate boundary to hostile ground

The Fargate worker role (§1.2) is the template; the edge is more exposed, so it **adds** controls:

| Control | Fargate worker (today) | Edge worker (adds) |
|---|---|---|
| Master DB/cache creds | absent | absent (unchanged invariant) |
| Durable data | via orchestrator API (in-VPC) | via §7 uplink + per-edge token |
| Blob scope | `worker_s3_prefix/*` | per-edge prefix, same shape |
| Tool surface | bound set | bound set + **C3 scope per edge** |
| Mutations | — | **C2 wrapper across the uplink + replay** |
| Local secrets | Secrets Manager | **OS vault** (DPAPI/keychain) — never synced up |
| Network | private subnet | **egress allowlist** (orchestrator + permitted local targets only) |
| Image | ECR | **signed image + boot attestation** (it runs on a machine we don't control) |
| Audit | CloudWatch | every act → `v3_cognitive_ledger` via uplink |

Profile A is treated as adversarial-premises; Profile B as employee-trust — but both keep §2 exactly.

---

## 9. Token economics (why Profile B pays for itself)

The cheap/expensive split is already modeled as RQ lanes; tiering maps onto it:

- **Already local:** embeddings on the detected backend (`detect.go` → `nce.embeddings`). For a super-consumer indexing a large repo or re-embedding (`reembedding_migration.py`), that is the dominant token sink — and it's $0 cloud tokens today.
- **Extension:** route generative *cheap legs* — routing/classification, retrieval ranking, draft/summarize, structured-extraction first passes — to a local model on the same backend; escalate only hard reasoning to cloud Opus/Sonnet.
- **Concrete super-consumers:** the ML.md flash-agent promptwave runner (engine-building waves), code-indexing, bulk extraction. Pushing their cheap legs local caps cloud spend without changing the agent's external behavior.

Governance: tiering is a **routing policy**, not a quality compromise — the C9a grounded-generation and C2 governance rules still apply; a local-tier answer that needs a citation or a mutation still goes through the same guards.

---

## 10. Build phases (EW-1 … EW-6)

- **EW-1 — `ModeEdge` skeleton.** `edge.go` in `go/launch`; installer writes the mode + sub-profile; worker boots bound to a stub uplink (no `PG_DSN`); reuses `DetectHardware` + `RecoveringWorker`.
- **EW-2 — The uplink (the hard part).** Outbound mTLS client; job-pull + command/response + result push; versioned envelope; per-edge identity/token; offline buffer + replay over the outbox/DLQ. Security-reviewed on its own (like C3).
- **EW-3 — Isolation hardening.** Per-edge C3 scope + capability-grant binding (only granted tools registered); local OS vault; egress allowlist; signed image/attestation.
- **EW-4 — Profile A pack (netops).** Bind the `netops` tool pack; wire `18b` data paths; capture behind C2 + legal gate (`18d`). Veidekke pilot.
- **EW-5 — Profile B tiering.** Local-model routing policy on the detected backend; lane mapping (local-cheap vs cloud-hard); promptwave/code-index offload; measure token delta.
- **EW-6 — Fleet management.** Remote pause/rotate/revoke; health/telemetry per edge; multi-edge addressing; calibration of tiering thresholds from the ledger.

**Foundation dependencies:** C2 (autonomy across uplink+replay), C3 (external-principal scope — security-review gated, long pole), C4/outbox (uplink emit), C1 (Profile A: observed↔ASSET identity). Start the C3 review early.

---

## 11. Open decisions

- **OD-1 — `ModeEdge` vs extend `ModeLocal` for Profile B.** Lean: new `ModeEdge` with `sub-profile=customer|local`; Profile B is `ModeLocal`-with-uplink so devs keep the full local stack.
- **OD-2 — Uplink protocol.** gRPC bidi stream vs HTTP/2 long-poll vs MCP-over-WebSocket. Must be outbound, versioned, resumable. (Recommend gRPC bidi or WS; decide in EW-2 spike.)
- **OD-3 — Local model for Profile B.** Which model/runtime on the detected backend (llama.cpp / OpenVINO / vLLM-CPU), and the routing policy's escalation rule.
- **OD-4 — Capability-grant representation.** How the per-edge bound tool set + ceilings are expressed and pushed (extends C2/C3 config).
- **OD-5 — Graduate this doc to `docs/`?** It's platform, not netops-only.
