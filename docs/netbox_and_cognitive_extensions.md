> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# NetBox Integration & Phase 3 Cognitive Spec

This document details the architecture, design specifications, and signal flows for the Phase 3 enhancements: **Assumption-Based Truth Maintenance System (ATMS)**, **Counterfactual Chrono-Branching**, **Spiking spreading activation**, **Longitudinal Operator Stress Tracking**, **Active Learning operator loops**, **NetBox vertical modules**, and the **NetBox Cognitive Dashboard Plugin**.

---

## 1. Assumption-Based Truth Maintenance System (ATMS)
The ATMS module ([nce/atms.py](https://github.com/sindrehaugen/NCE/blob/main/nce/atms.py)) maintains logical consistency across beliefs and causal statements derived from incoming telemetry.

### Data Structures
* **ATMSNode**: A logical unit representing a state.
  * `node_type`: `ASSUMPTION` (can be dynamically retracted), `PREMISE` (always true), or `DERIVED` (requires a justification).
  * `is_valid: bool`: `True` when the node is believed to be true; `False` when invalidated by a deprecation cascade.
* **Justification**: A logical relation mapping a set of cause nodes to a target node:
  $$\text{antecedents} \implies \text{consequent}$$

### Cyclic-Safe Recursive Belief Validation
Evaluating whether a derived node is valid involves traversing the justification graph:
1. **Premise**: Immediately valid (returns `True` unconditionally).
2. **Assumption**: Valid if `is_valid == True`.
3. **Derived Node**: Valid if there exists at least one justification where *all* antecedents are provably valid.
4. **Cycle-Prevention**: An in-memory traversal set (`active_path`) tracks visited nodes to prevent infinite recursion on self-referencing loops.

### Invalidation & Deprecation Propagation
When an `ASSUMPTION` node is invalidated, NCE triggers a recursive update:
1. Mark the assumption node's `is_valid = False`.
2. Recursively locate all `DERIVED` nodes where the invalidated node is an antecedent.
3. Re-evaluate their justifications. If no alternate valid justifications exist, mark them `is_valid = False`.
4. Persist the cascade externally: callers invoke `persist_atms_invalidation(conn, namespace_id, cascade_set)` to soft-delete (`valid_to = NOW()`) matching rows in the `memories` and `topology_graph` tables. The `ATMSEngine` itself has no DB hook; all database writes are handled by this standalone async function (`nce/atms.py`, lines 312–355).

---

## 2. Counterfactual Chrono-Branching
NCE enables timeline "What-If" counterfactual simulations using chrono-branching ([nce/causal/chrono.py](https://github.com/sindrehaugen/NCE/blob/main/nce/causal/chrono.py)).

### Thread & Task Safety
* Uses Python `contextvars.ContextVar` (`chrono_branch_var`) to bind active branch states to the current async task execution context.
* Prevents concurrent timeline operations in other tasks or web requests from interfering with or leaking data into parallel transactions.

### Memory Overlay & Isolation
When a timeline branch context is opened (`with branch_timeline(target_time, hypothetical_states):`):
1. The context manager stores `target_time` and `hypothetical_states` in `chrono_branch_var` — no raw table writes occur.
2. Callers apply in-memory overrides (node deletions, additions, edge injections) to a live `CausalGraph` via `apply_hypothetical_states()`, which returns a modified copy.
3. Production data remains isolated; the original graph is unchanged, allowing zero-risk downstream propagation modelling.

---

## 3. Spiking Spreading Activation Engine
The neuromorphic engine ([nce/graph_query.py](https://github.com/sindrehaugen/NCE/blob/main/nce/graph_query.py)) simulates charge propagation across the Knowledge Graph.

### Spiking Neural Network Mechanics
At each time step $t$:
1. **Firing Detection**: Any node $i$ with membrane potential $V_i(t) \ge \theta$ is added to the firing set. Its potential is reset to $0.0$.
2. **Charge Transfer**: Fired nodes distribute charge to their direct neighbors $j$:
   $$V_j(t+1) = V_j(t) \cdot \lambda + \alpha \cdot V_i(t) \cdot w_{ij}$$
   Where:
   * $\lambda$ = decay coefficient.
   * $\alpha$ = transfer efficiency.
   * $w_{ij}$ = weight of the edge between $i$ and $j$.
3. **Membrane Clamping**: To ensure numerical stability, all potentials are clamped at a hard limit `max_charge = 10.0`.
4. **Peak Tracking**: The engine records the historical maximum potential (`max_potentials`) reached by nodes during the simulation to retain decayed intermediate nodes in search results.

### Symmetrical Weight Adaptation (LTP/LTD)
Synaptic weights are adapted based on the outcomes of downstream decisions:
* **Success (LTP)**: Potentiates edge weight $w$:
  $$w_{\text{new}} = w + \eta \cdot (1.0 - w)$$
* **Failure (LTD)**: Depresses edge weight $w$:
  $$w_{\text{new}} = w - \eta \cdot w$$
* **Bidirectional/Symmetrical updates**: Queries and updates matching edges in *both* directions (`(src, tgt)` and `(tgt, src)`) inside `kg_edges` and `topology_graph` tables.
* **Savepoint Isolation**: Each edge update runs inside `async with conn.transaction()` — asyncpg implements this as a nested SAVEPOINT inside the outer transaction. The block catches `asyncpg.LockNotAvailableError` raised by `FOR UPDATE NOWAIT`, allowing contended edges to be skipped without poisoning the outer transaction (`nce/graph_query.py`, lines 229 and 345–346).

---

## 4. Longitudinal Operator Stress Tracking
The stress analytics module ([nce/analytics/stress.py](https://github.com/sindrehaugen/NCE/blob/main/nce/analytics/stress.py)) monitors operator cognitive load while preserving data privacy.

### Analytics Pipeline
1. **Biometric Extraction**: Extracts the operator's emotional state vector (specifically index 5 representing frustration) from `empathic_tensor` records stored in `v3_cognitive_ledger`.
2. **Burnout Standby Triggers**: If frustration remains $> 7.0$ for more than 5 consecutive shifts, NCE generates a burnout alert.
3. **On-Call Weight Redistribution**: NetBox integration hooks immediately update on-call routing weights. The burned-out operator's standby weight is set to `0.0`, and their active tickets are redistributed proportionally to healthy operators.
4. **Biometric Field Encryption**: All raw `empathic_tensor` arrays and fatigue profiles are encrypted at rest using AES-256-GCM via the NCE Master Key.

---

## 5. Active Learning Queue & Gamification
Active learning ([nce/active_learning.py](https://github.com/sindrehaugen/NCE/blob/main/nce/active_learning.py)) intercepts and quarantines low-confidence memories for operator validation.

```
                  store_memory()
                        │
             R = Confidence Score
                        │
                  ┌─────┴─────┐
              R >= 0.65    R < 0.65
                  │           │
              (Bypass)   (Quarantine)
                  │           │
           NCE Write     └──► active_learning_queue
                                          │
                                   Operator Dashboard
                                   (Confirm / Reject)
```

### Gamification & Streak Rewards
Operators are incentivized via a gamified micro-confirmation interface:
* **Confirming** a memory promotes it to the main stack and rewards **10 XP** (`NCE_ACTIVE_LEARNING_CONFIRM_XP`, default 10).
* **Rejecting** a memory flags it as discarded and rewards **5 XP** (`NCE_ACTIVE_LEARNING_REJECT_XP`, default 5).
* **Confirmation Streaks**: Consecutive validations are tracked; level thresholds advance every 100 XP.

---

## 6. NetBox Vertical Integration Modules
NCE integrates natively with NetBox infrastructure managers ([nce/vertical_modules/netbox/](https://github.com/sindrehaugen/NCE/blob/main/nce/vertical_modules/netbox/)):

| File | Batch | Responsibility |
|---|---|---|
| `graphql_activation.py` | BATCH-P3-NB-003 | Multi-hop GraphQL topology pull → `SpikingActivationEngine` seed |
| `discovery.py` | BATCH-P3-NB-005 | Unregistered asset reconciliation; staged writes via NetBox Branching API |
| `circuits.py` | BATCH-P3-NB-002 | Circuit provider escalation via Pearl do-calculus causal engine |
| `contacts.py` | BATCH-P3-NB-001 | Contact tenancy → operator stress mapping; on-call weight updates |
| `mtbf.py` | BATCH-P3-NB-004 | Predictive MTBF synthesis from hardware age + `event_log` anomaly counts |

* **GraphQL Topology Activation**: Pulls complete site, rack, device, and connection mappings in a single polymorphic query. Parses polymorphic cable terminations to construct an undirected adjacency matrix.
* **Unregistered Asset Discovery**: Compares live discovered telemetry against the cached NetBox graph. Identifies missing components and stages them on draft branches using the NetBox Branching API.
* **Circuit Provider Escalator**: Utilises Pearl's do-calculus causal engine to determine if specific circuit thresholds caused observed device failures, auto-generating upstream provider escalations.
* **Contacts → Stress Mapping**: Fetches NetBox `tenancy/contacts/` and `tenancy/contact-assignments/`; cross-references `StressTracker` burnout results to zero out on-call weights for burned-out operators.
* **MTBF Forecaster**: Joins NetBox device hardware/serial data (GraphQL) with `event_log` anomaly counts to output per-device failure probability matrices.

---

## 7. NetBox Cognitive Dashboard Plugin
Exposes NCE cognitive data directly within NetBox detail layouts using a PyPI-compatible package layout under `src/nce-netbox-plugin/`.

### Django Views & PostgreSQL RLS Context
The stats controller ([views.py](https://github.com/sindrehaugen/NCE/blob/main/src/nce-netbox-plugin/nce_netbox_plugin/api/views.py)) resolves tenant mappings:
1. Resolves the NetBox object's tenant slug to map it to an NCE namespace (supports `device`, `rack`, and `site` object types).
2. Opens a Django `transaction.atomic()` transaction.
3. Sets the PostgreSQL session variable:
   `SELECT set_config('nce.namespace_id', <ns_uuid>, true);`
4. Queries RLS-enforced database tables (`event_log`, `v3_cognitive_ledger`, `active_learning_queue`, `replay_runs`, `kg_nodes`) within that transaction scope.
5. Employs a zero-dependency fallback telemetry generator (`simulators.py`) if NCE database tables do not exist in the active schema.

**Required NCE tables** (checked via `pg_tables` before any query):

```python
REQUIRED_NCE_TABLES = ["namespaces", "event_log", "v3_cognitive_ledger", "replay_runs", "kg_nodes"]
```
