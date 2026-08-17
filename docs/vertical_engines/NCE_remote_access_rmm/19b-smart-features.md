> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# 19b — Smart features (NCE-backend leverage)

**Status:** design · **Owner:** NCE core (Sindre) · **Companion of:** `19-remote-access-rmm-engine.md`, `19c-security-legal-ops.md`
**Thesis:** a standalone RMM tool sees one endpoint at a time. This engine sees the endpoint as a **node in the shared cognitive graph** that also knows the room, the SLA, the signal chain, the open tickets, the customer's mood, and every past fix — and can **replay history** and **prove its reasoning**. Every feature below exploits an NCE primitive GoTo Resolve structurally lacks. Each is tagged with the primitive it needs, a feasibility verdict, what (if anything) it is **blocked on**, and its build phase.

> **Grounding note.** Verdicts are set against the actually-exposed platform primitives (`replay_reconstruct`, `compare_states`, `list_contradictions`/`resolve_contradiction`, `trigger_consolidation`, `a2a_*`, `graph_search`/`semantic_search`/`neuromorphic_search`, `get_event_provenance`, `explain_past_decision`, signing) and the shipped/spec'd engine surfaces (System Design(6) `SIGNAL_CHAIN`/`SPOF`/`validate_design_graph`; Support(10) Empathic Tensor + proactive-ticket path; `v3_cognitive_ledger`; `memories`; C2/C3/config-as-IP/source-mode from `99-shared-core-foundation.md`). Where a feature over-claimed a primitive that does not work that way, the claim is corrected here rather than carried forward.

## ⚠ The one caveat that governs everything: dangerous composition
Feature **F3 (self-writing playbooks)** and **F4 (fleet fan-out)** are individually valuable and jointly catastrophic: a machine-proposed allowlist entry, auto-applied fleet-wide, is *pushing unratified code to the entire estate from one inferred pattern*. **Hard rule:** (a) a machine-proposed `rmm-remediation-scripts.json` entry is **never** `autonomy_eligible` until a human ratifies it; (b) fleet fan-out is **always** human-confirmed and hard-capped by `NCE_RMM_AUTONOMY_BLAST_RADIUS_MAX` **regardless of confidence**; (c) the graph-computed blast radius (F1) gates every remediation, single or fleet. This is the single most important line in the plan set — see `19c`.

## Tier ⭐ — build first (buildable now, uniquely NCE, high usability)

### F1 — Graph-computed blast-radius gate  → folds into `do_run_remediation`
Before any reboot/script, walk `ASSET → PORT/SIGNAL_CHAIN → SPOF` (System Design(6)) + the `SLA` clock (Support(10)) to compute how many downstream endpoints/live signal paths the action jeopardizes; refuse/defer above the ceiling.
- **Primitive:** `kg_edges` traversal + System Design `SIGNAL_CHAIN`/`SPOF` + `validate_design_graph`; `SLA` node.
- **Verdict:** SOUND. **Correction:** the "**is the room in use *right now***" sub-claim has **no backend source** — Project(7) owns `GATE`/`TASK`, not bookings. v1 blast-radius = topology + SLA (graph-native, no external dep). "Room live now" is a **v2 enrichment** via an Outlook room-mailbox adapter (M365 is already connected). The static `BLAST_RADIUS_MAX` becomes a **ceiling on top of** the computed radius, not the whole gate.
- **Phase:** B4 (gates the live-action tier). The computed-radius component is a prerequisite for B5 autonomy.

### F2 — Session briefing card  → folds into `do_launch_session`
On launch, auto-assemble from the graph: room, live SLA countdown, open tickets, last-N fixes (`session_recall`), as-built signal chain, warranty/EOL (Assets(9)), customer health (internal-only). The tech walks in omniscient.
- **Primitive:** whole-graph read + `semantic_search`/`graph_search` + `v3_cognitive_ledger` recall.
- **Verdict:** SOUND, cheapest high-"wow". **Correction:** degrade gracefully — render whatever engines are live (room+SLA+tickets from B1–B3; warranty/EOL only once Assets(9) exists; health only once Support(10) health score exists). Never block the card on a missing engine.
- **Phase:** B3 (read-only; grows as upstream engines land).

### F3 — Self-writing remediation playbooks  → grows `rmm-remediation-scripts.json`
Deterministically aggregate `(alert-pattern → script → cleared)` fix-facts from `v3_cognitive_ledger`; where success-rate over N runs crosses a threshold, **propose** an allowlist entry citing the sessions that justify it.
- **Primitive:** `v3_cognitive_ledger` fix-facts + `get_event_provenance` (citations); `trigger_consolidation` **assists** (clusters similar symptoms) but does **not** mine rules.
- **Verdict:** SOUND with correction. **Correction:** this is a **deterministic aggregation pass**, not "consolidation emits rules." Proposed entries carry `provenance`, `proposed_at`, `ratified_by=null`, `autonomy_eligible=false` and require human ratification before F5/autonomy can touch them (see the composition caveat).
- **Phase:** B3 (proposals) — ratified entries feed B5.

### F4 — Fleet fan-out ("fix it everywhere")
A fix clearing an alert on one endpoint → find siblings (same device family/firmware/image) trending toward the same alert via `semantic_search` + `graph_search` → propose a **capped, human-confirmed** proactive fleet remediation.
- **Primitive:** `memories` embeddings + `graph_search` + the F3 fix-fact.
- **Verdict:** SOUND but **highest blast radius in the platform.** **Correction:** always human-confirmed, hard-capped by `BLAST_RADIUS_MAX`, never autonomy-eligible even at confidence 1.0; each target still passes the F1 gate individually. Stage: propose → operator selects subset → per-target F1 check → execute.
- **Phase:** B5 (needs F1 gate + ratified allowlist).

## Tier — high value (buildable soon; some enrich as engines land)

### F5 — Replay root-cause ("what changed before it broke?")
On alert onset, `replay_reconstruct` the graph/event timeline around the asset to correlate with cross-engine events (patch applied, script run, Field Tech WO closed, design change).
- **Primitive:** `replay_reconstruct`/`replay_observe` + `compare_states` + `get_event_provenance`.
- **Verdict:** SOUND. **Correction:** only as good as **event coverage** — at launch it correlates RMM's own events + whatever emits to the shared bus (Assets patch, Support ticket). **Field Tech(12) WO correlation is blocked until Field Tech emits events.** State clearly which sources are in-scope per phase.
- **Phase:** B3 (RMM+live-engine events); enriches automatically as more engines emit.

### F6 — Empathic-priority alert queue
Sort alerts by severity **fused** with the Empathic Tensor frustration trend + churn-risk (Support(10)); fix the endpoint of the customer about to leave first.
- **Primitive:** Empathic Tensor (`case_stress_report`) + customer-health score.
- **Verdict:** SOUND. **Correction:** **blocked on Support(10) health/tensor** being live; sparse at launch (expose coverage, don't fake confidence). **Hard guardrail:** the queue exposes *priority*, never the churn/health score — churn is internal-only, must never surface to the customer (Support hardening #4).
- **Phase:** B2 if Support live, else deferred to when it is.

### F7 — Drift / contradiction detection
Observed posture (GoTo agent/patch/firmware) vs documented intent (System Design as-built / driftsavtale spec) → `list_contradictions` / §9.2 divergence log flags "this NUC is 3 patch cycles behind spec."
- **Primitive:** reconciliation + `list_contradictions` + `resolve_contradiction`.
- **Verdict:** SOUND — the "system of truth" story applied to endpoints. **Correction:** intent source is System Design(6) as-built (available) and/or a config-as-IP expected-posture set; the full driftsavtale-spec intent is richer once Agreements(3) exists.
- **Phase:** B2 (against System Design/config intent); F14 closes the loop.

### F8 — Coverage-gap detection (blind-spot now, billing later)
An `ASSET` that *should* be managed but has no `RMM_AGENT` = a monitoring blind spot.
- **Primitive:** graph join `ASSET` vs `ASSET -[managed_by]-> RMM_AGENT`.
- **Verdict:** SOUND (blind-spot slice). **Correction:** the **billing-reconciliation** version (contracted coverage vs actual, mis-billing) is **blocked on Agreements(3)** (Tier 2, unbuilt). Ship blind-spot detection now against Assets(9)/config expected-set; layer billing when Agreements lands.
- **Phase:** B1 (blind-spot); billing later.

## Tier — medium / enrichment (real, but gated on unbuilt engines or lower urgency)

### F9 — Telemetry fusion prediction
Fuse GoTo RMM signals with Assets(9) manufacturer telemetry (Cisco xAPI/QSC) on the **same `ASSET` node** for higher-confidence prediction.
- **Verdict:** SOUND but **blocked on Assets(9) adapters** (mock-with-swap, unbuilt). RMM-only trend prediction (disk/mem trending on GoTo's own signal) ships standalone; fusion is the enrichment.
- **Phase:** RMM-only trend at B2; fusion when Assets(9) is live.

### F10 — Access-transparency as trust
Every session/script is a signed, WORM, replayable, provenanced record; surface it to the customer ("who touched what, when, why") in Customer Portal(17).
- **Primitive:** signing + WORM event log + `get_event_provenance`.
- **Verdict:** SOUND. **Correction:** **audit-now, expose-later** — the signed WORM record ships with B4/B5; the **portal surface is blocked on Portal(17)** (Tier 4). Split accordingly.
- **Phase:** audit at B4/B5; portal exposure when Portal(17) lands.

### F11 — Scoped A2A remote grant for contractors
A partner agent requests a time-boxed, single-asset, revocable remote session via `a2a_create_grant` + C3 external-principal RLS.
- **Primitive:** `a2a_create_grant`/`a2a_verify_grant_status`/`a2a_revoke_grant` + C3 RLS.
- **Verdict:** SOUND — grant mechanism is platform-native. **Correction:** the partner-consumer story is richer once Field Tech(12) partner scope exists, but the grant + scoped session is buildable once live actions (B4) exist.
- **Phase:** B4+.

## New features surfaced by the gap pass (unused primitives → missed value)

### F12 — Post-remediation state verification (**the real gap**)  → folds into `do_run_remediation`
After a script runs, `compare_states` the asset's graph state pre/post + confirm the target `RMM_ALERT` actually cleared (webhook) and **no new alert regressed**. Answers "did the fix actually work?" instead of trusting exit code 0.
- **Primitive:** `compare_states` (currently unused by any feature) + the alert-clear webhook.
- **Verdict:** SOUND, important — closes the remediation loop and is what makes an outcome a *trustworthy* fix-fact for F3. `do_run_remediation` returns `verified: bool`.
- **Phase:** B5 (with remediation).

### F13 — "Explain this remediation" (auditor/operator)
Ask *why* the agent ran a script and get the cited chain (which prior sessions, which policy, which allowlist entry + its provenance).
- **Primitive:** `explain_past_decision` + `get_event_provenance` (unused).
- **Verdict:** SOUND — directly strengthens governance of the sharpest action; pairs with F10.
- **Phase:** B5.

### F14 — Drift auto-reconcile (closes F7's loop)
F7 detects observed≠intent; `resolve_contradiction` drives the governed close-out — remotely apply the patch to reconcile observed→intent, **or** update the as-built if reality is correct. Human-gated.
- **Primitive:** `resolve_contradiction` (F7 only *finds*, doesn't *close*).
- **Phase:** B5 (needs remediation + F7).

### F15 — Signed per-session consent/authorization token
Mirror GoTo's zero-trust signature-key model on the NCE side: each session/script carries an NCE-signed authorization token, making the WORM record tamper-evident and independent of the vendor's own gate (defense in depth).
- **Primitive:** signing / `rotate_signing_key`.
- **Phase:** B4 (with live actions); detailed in `19c`.

### F16 — Anomaly-shaped recall (opportunistic)
Beyond semantic similarity, use `neuromorphic_search` for "this failure *pattern* rhymes with these past incidents" when embedding similarity is weak.
- **Primitive:** `neuromorphic_search`.
- **Verdict:** nice-to-have; fold into `session_recall` as a second retrieval strategy, not a standalone feature.
- **Phase:** B3+ (opportunistic).

## Shared components (build once, several features consume)
- **Graph-computed blast-radius service** — F1, F4 (and the F5/F12 "what's downstream" walk). Build once in the engine core.
- **Temporal correlation over the event bus** — F5, F7/F14, F12. One "state/timeline around an asset" helper.
- **Fix-fact aggregation** — F3, and the trustworthiness of F4/F6 recall. One deterministic pass over `v3_cognitive_ledger`.

## Feature → phase summary
| Phase | Features landing |
|---|---|
| B1 | F8 (blind-spot) |
| B2 | F6 (if Support live), F7, F9 (RMM-only trend) |
| B3 | F2 (briefing), F3 (proposals), F5 (RMM+live events), F16 |
| B4 | F1 (blast-radius gate), F10 (audit), F11, F15 |
| B5 | F4 (fleet, capped), F12 (verify), F13 (explain), F14 (reconcile) |
| Later (blocked) | F6 (needs Support), F8-billing (needs Agreements), F9-fusion (needs Assets), F10-portal (needs Portal 17), F1 "room live now" (needs Outlook adapter) |
