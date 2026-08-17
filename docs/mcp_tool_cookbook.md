> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# MCP Tool Cookbook

Comprehensive integration guide and operational recipes for all **112 registered Model Context Protocol (MCP) tools** in the Neuro-Cognitive Engine (NCE v1.0).

This cookbook details required and optional parameters, four-column dispatch gating flags (`admin_only`, `mutation`, `cacheable`, `migration`), JSON-RPC 2.0 request/response wire formats, error code handling, and representative recipes across all Shared Core subsystems and 12 Vertical Engines.

---

## 1. Gating Columns & Dispatch Semantics

Every tool entry in `TOOL_REGISTRY` (`nce/tool_registry.py`) is defined by a `ToolSpec` dataclass carrying four boolean flags evaluated by `execute_call_tool` (`nce/mcp_stdio_dispatch.py`):

| Column | Type | Meaning & Dispatch Lifecycle |
|---|---|---|
| `admin_only` | `bool` | Requires admin credentials. Before invoking the handler, `_check_admin(arguments)` verifies `admin_api_key` via `nce.auth._validate_scope("admin", …)`. Rejection yields JSON-RPC error `-32001 Admin authentication required`. |
| `mutation` | `bool` | State-mutating operation. Upon successful handler completion, `bump_cache_generation(engine.redis_client)` increments the global Redis counter `mcp_cache_generation`, invalidating cached query entries. Specific deletion tools (`forget_memory`, `delete_snapshot`, `shred_memory`) additionally trigger explicit cache purges. |
| `cacheable` | `bool` | Deterministic read query. Prior to handler execution (and before quota consumption), dispatch checks Redis using key `(tool_name, namespace_id, arguments, generation)`. Cache hits return immediately with `MCP_CACHE_TTL_S` (300s default). |
| `migration` | `bool` | Embedding migration tool gated by `cfg.NCE_DISABLE_MIGRATION_MCP`. When `True` (production default), dispatch safely returns notice text without invoking the handler and without raising a JSON-RPC error. |

### Dispatch Flow Rules
1. **Cache Pre-Check:** Cache lookup runs **before** quota consumption to avoid charging tenants for cached hits.
2. **Post-Mutation Invalidation:** Cache generation bumps occur **only after** a handler returns successfully; failed calls do not invalidate cache keys.
3. **Runtime Disabling:** Any tool can be disabled at runtime via Redis key `nce:tools:disabled` → `{<tool_name>: 1}` or `POST /api/admin/tools/toggle`. Disabled tools return JSON-RPC error `-32005 Scope forbidden`.

---

## 2. Complete 112-Tool Gating Summary Table

Derived directly from `TOOL_REGISTRY` in `nce/tool_registry.py` on baseline `7304330`:

### 2.1 Shared Core Platform (66 Tools)

| Tool Name | admin\_only | mutation | cacheable | migration | Subsystem |
|---|:---:|:---:|:---:|:---:|---|
| `store_memory` | | yes | | | Memory / Epistemics |
| `store_artifact` | | yes | | | Quad-Stack Artifacts |
| `store_media` _(deprecated)_ | | yes | | | Quad-Stack Media |
| `semantic_search` | | | yes | | pgvector Vector Search |
| `get_recent_context` | | | | | Episodic Memory |
| `boost_memory` | | yes | | | Memory Salience |
| `forget_memory` | | yes | | | Memory Deactivation |
| `unredact_memory` | yes | yes | | | PII Vault Management |
| `shred_memory` | yes | yes | | | Cryptographic Erasure |
| `index_code_file` | | yes | | | AST Code Parser |
| `check_indexing_status` | | | | | Background Job Worker |
| `search_codebase` | | | yes | | Code Chunk Search |
| `graph_search` | | | yes | | Knowledge Graph BFS |
| `neuromorphic_search` | | | yes | | Spreading-Activation Search |
| `connect_bridge` | | yes | | | Document Bridge OAuth |
| `complete_bridge_auth` | | yes | | | Document Bridge Auth |
| `list_bridges` | | | | | Bridge Subscriptions |
| `disconnect_bridge` | | yes | | | Bridge Termination |
| `force_resync_bridge` | | yes | | | Bridge Resynchronization |
| `bridge_status` | | | | | Bridge Subscription Status |
| `list_contradictions` | | | | | Contradiction Review |
| `resolve_contradiction` | | yes | | | Contradiction Resolution |
| `start_migration` | | yes | | yes | Embedding Migration |
| `migration_status` | | | | yes | Migration Monitoring |
| `validate_migration` | | | | yes | Migration Quality Gate |
| `commit_migration` | | yes | | yes | Migration Activation |
| `abort_migration` | | yes | | yes | Migration Rollback |
| `replay_observe` | yes | | | | WORM Event Log Streaming |
| `replay_reconstruct` | yes | yes | | | State Reconstruction |
| `replay_fork` | yes | | | | Namespace Forking |
| `replay_status` | yes | | | | Replay Job Status |
| `get_event_provenance` | | | | | Causal Provenance Chain |
| `explain_memory` | | | | | Epistemic Verification |
| `explain_past_decision` | yes | yes | | | Bi-temporal Accountability |
| `explain_config_change` | yes | | | | System Audit Log |
| `detect_causal_cycles` | yes | | | | Causal Graph Analysis |
| `a2a_create_grant` | | yes | | | Cross-Tenant Sharing |
| `a2a_revoke_grant` | | yes | | | Grant Revocation |
| `a2a_list_grants` | | | | | Active Grant Catalog |
| `a2a_query_shared` | | | | | Shared Memory Search |
| `a2a_verify_grant_status` | | | | | Grant Validity Probe |
| `a2a_update_grant_scopes` | | | | | Grant Scope Modification |
| `a2a_inspect_grant` | | | | | Grant Detail Inspection |
| `pricing_resolve` | | | yes | | Dynamic Pricing Core |
| `resolve` | | | yes | | Entity Resolution Core |
| `merge_queue_list` | | | yes | | Entity Merge Queue |
| `merge_queue_confirm` | yes | yes | | | Entity Merge Approval |
| `merge_queue_reject` | yes | yes | | | Entity Merge Rejection |
| `manage_namespace` | | yes | | | Namespace Lifecycle |
| `verify_memory` | | | | | SHA-256 Memory Integrity |
| `trigger_consolidation` | | yes | | | Sleep Consolidation Run |
| `consolidation_status` | | | | | Consolidation Progress |
| `manage_quotas` | | yes | | | Quota Management |
| `rotate_signing_key` | | yes | | | HMAC Key Rotation |
| `get_health` | | | | | System Health Probe |
| `list_dlq` | | | | | Dead Letter Queue |
| `replay_dlq` | | yes | | | DLQ Replay |
| `purge_dlq` | | yes | | | DLQ Purge |
| `create_snapshot` | | yes | | | Memory Snapshot Creation |
| `list_snapshots` | | | | | Snapshot Catalog |
| `delete_snapshot` | | yes | | | Snapshot Removal |
| `compare_states` | | | | | State Diff Engine |
| `import_snapshot` | | yes | | | Snapshot Ingestion |
| `suggest_queries` | | | | | Query Catalog Intent Match |
| `execute_query_template` | | | | | Query Catalog Executor |
| `describe_schema` | | | | | Graph Schema Discovery |

### 2.2 Vertical Engines (46 Tools)

| Vertical Engine | Tool Name | admin\_only | mutation | cacheable | migration |
|---|---|:---:|:---:|:---:|:---:|
| **Agreements** | `agreements_lookup_terms` | | | yes | |
| **Diagnostics** | `diag_ingest_bundle` | | yes | | |
| | `diag_commit_bundle` | | yes | | |
| | `diag_digest_status` | | | yes | |
| | `diag_device_health` | | | yes | |
| | `diag_list_anomalies` | | | yes | |
| **Dynamics 365** | `d365_query_case` | | | yes | |
| | `d365_sync_now` | yes | yes | | |
| | `d365_case_stress_report` | | | yes | |
| | `d365_list_sla_breaches` | yes | | | |
| | `d365_netbox_mappings` | | | yes | |
| | `d365_sync_status` | | | | |
| **Economy** | `economy_match_invoice` | | | yes | |
| | `economy_compute_periodisering` | | | yes | |
| | `economy_emit_event` | | | yes | |
| **NetBox** | `evaluate_circuit_impact` | | | | |
| **Procurement** | `procurement_calculate_tco` | | | yes | |
| | `procurement_rank_suppliers` | | | yes | |
| | `procurement_evaluate_match` | | | yes | |
| | `procurement_forecast_rebate` | | | yes | |
| | `procurement_recommend_move_spend` | | | yes | |
| | `procurement_whatif_spend` | | | yes | |
| **Product** | `product_search` | | | yes | |
| | `product_get` | | | yes | |
| | `product_price` | | | yes | |
| | `product_related` | | | yes | |
| | `product_match_bom_line` | | | | |
| | `product_enrich` | | yes | | |
| **Project** | `project_can_enter_phase` | | | yes | |
| | `project_convert_signed_quote` | yes | yes | | |
| | `project_advance_phase` | yes | yes | | |
| | `project_suggest_pl` | | | yes | |
| **Sales** | `sales_ping` | | | yes | |
| | `sales_get_signed_baseline` | | | | |
| **System Design** | `system_design_ping` | | | yes | |
| | `system_design_publish_design_docs` | | yes | | |
| **Vendors** | `vendors_get_vendor` | | | yes | |
| | `vendors_compute_scorecard` | | | yes | |
| | `vendors_get_tier_status` | | | yes | |
| | `vendors_detect_reliability_degradation` | | | yes | |
| | `vendors_check_tier_at_risk` | | | yes | |
| | `vendors_match_contractor` | | | yes | |
| | `vendors_compute_performance` | | | yes | |
| | `vendors_recall_similar_jobs` | | | yes | |
| | `vendors_reliability_radar` | | | yes | |
| | `vendors_calibrate_weights` | | | yes | |

---

## 3. Protocol Wire Formats & Error Handling

### 3.1 JSON-RPC 2.0 stdio Transport
Clients communicate over standard input/output (`stdio`) formatted per JSON-RPC 2.0:

```json
{
  "jsonrpc": "2.0",
  "id": "req-101",
  "method": "tools/call",
  "params": {
    "name": "semantic_search",
    "arguments": {
      "namespace_id": "00000000-0000-4000-8000-000000000001",
      "agent_id": "primary-agent",
      "query": "AV rack power budget specifications",
      "limit": 3
    }
  }
}
```

### 3.2 Standard & Extended Error Matrix

The server wraps all tool execution results inside `content[0].text`. When errors occur, inspect the embedded JSON structure:

| Code | Label | Description & Recommended Resolution |
|---|---|---|
| `-32600` | `Invalid Request` | Malformed JSON-RPC 2.0 payload. |
| `-32601` | `Method Not Found` | Method is not `tools/call` or `tools/list`. |
| `-32602` | `Invalid Params` | Missing required parameters or type violation against `inputSchema`. |
| `-32603` | `Internal Error` | Unhandled backend exception. Inspect `nce-admin` container logs. |
| `-32001` | `Admin Auth Required` | Missing or invalid `admin_api_key` for `admin_only=True` tool. |
| `-32002` | `Quota Exceeded` | Tenant has exhausted memory count, storage bytes, or token limits. |
| `-32005` | `Scope Forbidden` | Tool is disabled via Redis hash `nce:tools:disabled` or tenant lacks access. |
| `-32008` | `Resource Not Found` | Specified entity ID, quote, or memory reference does not exist under tenant RLS. |
| `-32010` | `A2A Unauthorized Token` | Missing, expired, or cryptographically invalid A2A sharing token. |
| `-32011` | `A2A Scope Violation` | Attempted access outside granted resource boundaries. |
| `-32029` | `Rate Limit Exceeded` | Client exceeded maximum requests per second. Implement exponential backoff. |

---

## 4. Shared Core Platform Recipes

### 4.1 Memory & Epistemic Storage

#### `store_memory` — Ingest conversation turn or fact
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "store_memory",
    "arguments": {
      "namespace_id": "00000000-0000-4000-8000-000000000001",
      "agent_id": "project-lead",
      "content": "Audio-over-IP network requires Dante Domain Manager redundancy on VLAN 120.",
      "summary": "Dante AoIP requires DDM on VLAN 120",
      "content_type": "chat",
      "check_contradictions": true
    }
  }
}
```
*Success Response Payload:*
```json
{
  "status": "ok",
  "payload_ref": "b4e8832a-3b56-4318-971c-43f605a96894",
  "contradiction": null
}
```

#### `semantic_search` — Point-in-time pgvector search
```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "name": "semantic_search",
    "arguments": {
      "namespace_id": "00000000-0000-4000-8000-000000000001",
      "agent_id": "project-lead",
      "query": "Dante VLAN network requirements",
      "limit": 5,
      "as_of": "2026-08-01T00:00:00Z"
    }
  }
}
```

#### `shred_memory` — Cryptographic erasure (Admin only)
```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "shred_memory",
    "arguments": {
      "memory_id": "b4e8832a-3b56-4318-971c-43f605a96894",
      "namespace_id": "00000000-0000-4000-8000-000000000001",
      "agent_id": "project-lead",
      "admin_api_key": "sk-admin-key-..."
    }
  }
}
```

---

### 4.2 Knowledge Graph & GraphRAG

#### `graph_search` — BFS traversal over Knowledge Graph
Traverse causal and structural relationships between entities:
```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "method": "tools/call",
  "params": {
    "name": "graph_search",
    "arguments": {
      "query": "Which project milestones depend on Crestron DM-NVX switchgear deliveries?",
      "namespace_id": "00000000-0000-4000-8000-000000000001",
      "max_depth": 2,
      "max_edges_per_node": 128
    }
  }
}
```

#### `neuromorphic_search` — Spreading activation network search
```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "method": "tools/call",
  "params": {
    "name": "neuromorphic_search",
    "arguments": {
      "query": "Subnet routing latency cascading into audio clock jitter",
      "namespace_id": "00000000-0000-4000-8000-000000000001",
      "theta": 0.45,
      "decay": 0.85,
      "ticks": 3
    }
  }
}
```

---

### 4.3 Agent-to-Agent (A2A) Memory Sharing

#### `a2a_create_grant` — Grant scoped cross-namespace access
```json
{
  "jsonrpc": "2.0",
  "id": 6,
  "method": "tools/call",
  "params": {
    "name": "a2a_create_grant",
    "arguments": {
      "namespace_id": "00000000-0000-4000-8000-000000000001",
      "agent_id": "partner-coordinator",
      "scopes": [
        {
          "resource_type": "namespace",
          "resource_id": "00000000-0000-4000-8000-000000000001",
          "permissions": ["read"]
        }
      ],
      "target_namespace_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
      "expires_in_seconds": 86400
    }
  }
}
```

#### `a2a_query_shared` — Query external namespace via grant token
```json
{
  "jsonrpc": "2.0",
  "id": 7,
  "method": "tools/call",
  "params": {
    "name": "a2a_query_shared",
    "arguments": {
      "sharing_token": "nce-a2a-token-...",
      "consumer_namespace_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
      "query": "Approved acoustic fabric materials for auditoriums",
      "top_k": 5
    }
  }
}
```

---

## 5. Vertical Engine Integration Recipes

### 5.1 Sales Engine (`docs/engines/sales-user.md`)

#### `sales_get_signed_baseline` — Query immutable signed contract baseline
```json
{
  "jsonrpc": "2.0",
  "id": 8,
  "method": "tools/call",
  "params": {
    "name": "sales_get_signed_baseline",
    "arguments": {
      "namespace_id": "00000000-0000-4000-8000-000000000001",
      "quote_id": "Q-2026-8812"
    }
  }
}
```
*Response Output:*
```json
{
  "quote_id": "Q-2026-8812",
  "customer_id": "CUST-0492",
  "signed_amount": 148500.00,
  "currency": "NOK",
  "baseline_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "signed_at": "2026-08-15T14:22:10Z"
}
```

---

### 5.2 Vendors Engine (`docs/engines/vendors-user.md`)

#### `vendors_compute_scorecard` — Aggregate vendor reliability metrics
```json
{
  "jsonrpc": "2.0",
  "id": 9,
  "method": "tools/call",
  "params": {
    "name": "vendors_compute_scorecard",
    "arguments": {
      "namespace_id": "00000000-0000-4000-8000-000000000001",
      "vendor_id": "VEND-CRESTRON-01"
    }
  }
}
```
*Response Output:*
```json
{
  "vendor_id": "VEND-CRESTRON-01",
  "overall_score": 94.2,
  "tier": "Tier-1 Certified",
  "on_time_delivery_rate": 0.96,
  "rm_rate": 0.012,
  "price_stability_index": 0.98
}
```

#### `vendors_match_contractor` — Match sub-contractor for project requirements
```json
{
  "jsonrpc": "2.0",
  "id": 10,
  "method": "tools/call",
  "params": {
    "name": "vendors_match_contractor",
    "arguments": {
      "namespace_id": "00000000-0000-4000-8000-000000000001",
      "required_skills": ["CTS-D", "Dante Level 3", "Q-SYS Architect"],
      "location": "Oslo",
      "earliest_availability": "2026-09-01"
    }
  }
}
```

---

### 5.3 Procurement Engine (`docs/engines/procurement-user.md`)

#### `procurement_calculate_tco` — Compute Total Cost of Ownership
```json
{
  "jsonrpc": "2.0",
  "id": 11,
  "method": "tools/call",
  "params": {
    "name": "procurement_calculate_tco",
    "arguments": {
      "namespace_id": "00000000-0000-4000-8000-000000000001",
      "product_id": "DISP-LED-136",
      "lifecycle_years": 5,
      "energy_kwh_cost": 1.45,
      "maintenance_tier": "gold"
    }
  }
}
```

#### `procurement_rank_suppliers` — Rank vendor procurement bids
```json
{
  "jsonrpc": "2.0",
  "id": 12,
  "method": "tools/call",
  "params": {
    "name": "procurement_rank_suppliers",
    "arguments": {
      "namespace_id": "00000000-0000-4000-8000-000000000001",
      "bom_lines": [
        {"item": "NVX-360", "quantity": 24},
        {"item": "TSW-1070", "quantity": 8}
      ],
      "currency": "EUR"
    }
  }
}
```

---

### 5.4 Product Engine (`docs/engines/product-user.md`)

#### `product_search` — Search catalog with pgvector embedding
```json
{
  "jsonrpc": "2.0",
  "id": 13,
  "method": "tools/call",
  "params": {
    "name": "product_search",
    "arguments": {
      "namespace_id": "00000000-0000-4000-8000-000000000001",
      "query": "4K60 4:4:4 AV over IP encoder with PoE+",
      "limit": 5
    }
  }
}
```

#### `product_match_bom_line` — Automated line reconciliation
```json
{
  "jsonrpc": "2.0",
  "id": 14,
  "method": "tools/call",
  "params": {
    "name": "product_match_bom_line",
    "arguments": {
      "namespace_id": "00000000-0000-4000-8000-000000000001",
      "raw_text": "Shure MXA920W-S Ceiling Array White Square"
    }
  }
}
```

---

### 5.5 Project Engine (`docs/engines/project-user.md`)

#### `project_can_enter_phase` — Validate Phase Gate prerequisites
```json
{
  "jsonrpc": "2.0",
  "id": 15,
  "method": "tools/call",
  "params": {
    "name": "project_can_enter_phase",
    "arguments": {
      "namespace_id": "00000000-0000-4000-8000-000000000001",
      "project_id": "PRJ-2026-0042",
      "target_phase": "execution"
    }
  }
}
```

---

### 5.6 Agreements Engine (`docs/engines/agreements-user.md`)

#### `agreements_lookup_terms` — Retrieve SLA and warranty clauses
```json
{
  "jsonrpc": "2.0",
  "id": 16,
  "method": "tools/call",
  "params": {
    "name": "agreements_lookup_terms",
    "arguments": {
      "namespace_id": "00000000-0000-4000-8000-000000000001",
      "agreement_id": "AGR-2025-0199",
      "topic": "sla_response_time"
    }
  }
}
```

---

### 5.7 Diagnostics Engine (`docs/engines/diagnostics-user.md`)

#### `diag_device_health` — Probe real-time edge device telemetry
```json
{
  "jsonrpc": "2.0",
  "id": 17,
  "method": "tools/call",
  "params": {
    "name": "diag_device_health",
    "arguments": {
      "namespace_id": "00000000-0000-4000-8000-000000000001",
      "device_id": "DSP-CORE-01"
    }
  }
}
```

---

## 6. Runtime Management & Extensions

### 6.1 Disabling Tools at Runtime
Tools can be toggled without container restarts or deployments via Redis:

```bash
# Disable tool dynamically
redis-cli -a $REDIS_PASSWORD HSET nce:tools:disabled d365_sync_now 1

# Re-enable tool
redis-cli -a $REDIS_PASSWORD HDEL nce:tools:disabled d365_sync_now
```

Or via REST API:
```http
POST /api/admin/tools/toggle
Authorization: HMAC-SHA256 ...
Content-Type: application/json

{
  "tool_name": "d365_sync_now",
  "disabled": true
}
```

### 6.2 Extension Tool Registration Pattern
Register custom domain tools using `register_tool()` in `nce/tool_registry.py`:

```python
from nce.tool_registry import register_tool, ToolSpec

async def handle_custom_acoustic_calc(engine, namespace_id, arguments):
    rt60_target = arguments.get("rt60_target", 0.6)
    room_volume = arguments.get("room_volume", 250.0)
    # SABINE formula calculation
    absorption_needed = (0.161 * room_volume) / rt60_target
    return {"absorption_sabins": round(absorption_needed, 2)}

register_tool(
    "acoustic_sabine_calculator",
    ToolSpec(
        handler=handle_custom_acoustic_calc,
        mutation=False,
        cacheable=True,
        admin_only=False,
        migration=False,
    ),
    replace=True,
)
```

---

## 7. Related References & Architecture Links

- [Surface of Truth Table](_generated/surface.md) — Canonical 112-tool / 128-route AST verification
- [API Reference](api_reference.md) — Comprehensive schema specifications
- [Enterprise Security](enterprise_security.md) — Three-header HMAC protocol and mTLS boot guards
- [Database Architecture](database_architecture.md) — 57-table Row-Level Security (RLS) policies
- [Agent-to-Agent Protocol](a2a.md) — Cross-namespace trust and memory federation
- [Vertical Engine Guides](engines/) — Individual User and Admin manuals for all 12 modules
