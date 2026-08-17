> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# NCE Usage Modes

NCE exposes two distinct runtime surfaces. Choosing the right one depends on whether the caller is an LLM client or a programmatic service.

| | MCP / LLM stdio | Admin REST API |
|---|---|---|
| **Entry point** | `server.py` | `admin_server.py` |
| **Transport** | JSON-RPC 2.0 over stdin/stdout | HTTP/HTTPS, port 8003 (default) |
| **Auth** | Namespace token in tool args | HMAC-SHA256 header + optional mTLS |
| **Primary consumers** | Claude Desktop, Cursor, Windsurf, any MCP client | Dashboards, CI/CD pipelines, operators, service integrations |
| **Tool/endpoint set** | Memory, code, graph, media, migration, A2A, snapshot | Search, replay, snapshot export, GC, A2A grants, admin ops |
| **Response format** | `TextContent[]` (stringified JSON in `.text`) | `application/json` or `application/x-ndjson` (streaming) |
| **Quota enforcement** | Per-tool, per-namespace | Per-route (same quota table) |

---

## 1. MCP / LLM stdio Mode

### How it works

`server.py` wraps the `NCEEngine` in the [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) and communicates over stdin/stdout using JSON-RPC 2.0. An LLM client (e.g. Claude Desktop) launches the process, registers tools from `tools/list`, and calls them via `tools/call`.

Four background tasks are co-launched in the same process: `gc_loop`, `quota_redis_flush_loop`, `re_embedder`, and `outbox_relay_loop`. This ensures the LLM surface always runs against a clean, up-to-date data set without requiring a separate sidecar.

### Launch

```bash
python server.py
```

Or via Claude Desktop / Cursor config (`mcp_config.json`):

```json
{
  "mcpServers": {
    "nce-memory": {
      "command": "python",
      "args": ["/absolute/path/to/NCE/server.py"],
      "env": {
        "PG_DSN": "postgresql://mcp_user:password@127.0.0.1:5432/memory_meta",
        "MONGO_URI": "mongodb://127.0.0.1:27017",
        "REDIS_URL": "redis://127.0.0.1:6379/0",
        "MINIO_ENDPOINT": "127.0.0.1:9002",
        "NCE_MASTER_KEY": "your-32-byte-master-key",
        "NCE_MCP_API_KEY": "your-mcp-api-key",
        "NCE_MCP_NAMESPACE_ID": "00000000-0000-4000-8000-000000000001"
      }
    }
  }
}
```

### JSON-RPC wire format

All messages conform to JSON-RPC 2.0. The MCP SDK handles framing; the examples below show the logical payload.

#### tools/list response (excerpt)

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "tools": [
      {
        "name": "store_memory",
        "description": "Persist a memory (conversation turn, document, or summary) to the Tri-Stack.",
        "inputSchema": {
          "type": "object",
          "properties": {
            "namespace_id": { "type": "string" },
            "agent_id":     { "type": "string" },
            "content":      { "type": "string" },
            "summary":      { "type": "string" },
            "heavy_payload":{ "type": "string" },
            "content_type": { "type": "string", "enum": ["chat", "code"] },
            "check_contradictions": { "type": "boolean", "default": false }
          },
          "required": ["namespace_id", "agent_id", "content"]
        }
      },
      {
        "name": "semantic_search",
        "description": "Search stored memories by semantic similarity.",
        "inputSchema": {
          "type": "object",
          "properties": {
            "namespace_id": { "type": "string" },
            "agent_id":     { "type": "string" },
            "query":        { "type": "string" },
            "limit":        { "type": "integer", "default": 5, "minimum": 1, "maximum": 100 },
            "offset":       { "type": "integer", "default": 0, "minimum": 0 },
            "as_of":        { "type": "string", "format": "date-time" }
          },
          "required": ["namespace_id", "agent_id", "query"]
        }
      }
    ]
  }
}
```

#### tools/call — store_memory

Request:

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "name": "store_memory",
    "arguments": {
      "namespace_id": "550e8400-e29b-41d4-a716-446655440000",
      "agent_id":     "claude-agent-01",
      "content":      "User asked about database connection pooling best practices.",
      "summary":      "DB pooling best practices conversation",
      "heavy_payload": "Full transcript: ...",
      "content_type": "chat"
    }
  }
}
```

Success response:

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"memory_id\": \"a1b2c3d4-...\", \"mongo_ref_id\": \"64f9...\", \"status\": \"stored\"}"
      }
    ]
  }
}
```

#### tools/call — semantic_search (with time travel)

Request:

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "semantic_search",
    "arguments": {
      "namespace_id": "550e8400-e29b-41d4-a716-446655440000",
      "agent_id":     "claude-agent-01",
      "query":        "connection pool exhaustion handling",
      "limit":        5,
      "as_of":        "2026-04-01T00:00:00Z"
    }
  }
}
```

Success response:

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"results\": [{\"memory_id\": \"...\", \"score\": 0.91, \"summary\": \"DB pooling best practices\", \"content\": \"...\", \"created_at\": \"2026-03-15T09:12:00Z\"}]}"
      }
    ]
  }
}
```

#### tools/call — error (quota exceeded)

Quota errors are returned as a *successful* tool result (the MCP SDK wraps them as `TextContent`). The `.text` field contains a JSON-RPC 2.0 error object — clients should inspect `error.code` rather than `isError`.

```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"jsonrpc\": \"2.0\", \"error\": {\"code\": -32013, \"message\": \"Resource quota exceeded\", \"data\": {\"reason\": \"quota_exceeded\"}}}"
      }
    ]
  }
}
```

MCP extended error codes (defined in `nce/mcp_stdio_rpc.py`):

| Code | Meaning |
|---|---|
| `-32001` | Admin authentication required |
| `-32005` | Scope forbidden |
| `-32013` | Resource quota exceeded |
| `-32029` | Rate limit exceeded |

### Available MCP tools

Tools are conditionally included: migration tools are present unless `NCE_DISABLE_MIGRATION_MCP=true`; D365 tools appear only when `NCE_D365_ENABLED=true`. `[ADMIN]` marks any tool requiring admin privileges via one of three enforcement mechanisms: (a) `ToolSpec(admin_only=True)` in `nce/tool_registry.py` — the dispatcher calls `_check_admin` before invoking the handler (`nce/mcp_stdio_dispatch.py`); (b) membership in `MCP_ADMIN_TOOL_NAMES` — enforced by `enforce_mcp_tool_auth` via the caller scope (`nce/auth.py`); (c) a required `admin_api_key` argument in the tool `inputSchema`, validated by the handler (`nce/mcp_stdio_tools.py`). See per-section notes for which mechanism applies to each tool.

**Memory**

| Tool | Description |
|---|---|
| `store_memory` | Persist a conversation turn, document, or summary |
| `semantic_search` | pgvector cosine search + Mongo hydration; optional `as_of` time travel |
| `get_recent_context` | Retrieve the N most-recent episodic memories for an agent |
| `boost_memory` | Raise salience score of a memory |
| `forget_memory` | Soft-delete a memory (reversible via `unredact_memory`) |
| `unredact_memory` | `[ADMIN]` Restore a soft-deleted memory |
| `shred_memory` | `[ADMIN]` Hard-delete a memory (irreversible) |
| `verify_memory` | `[ADMIN]` Verify Merkle-chain integrity for a memory |
| `list_contradictions` | List detected contradictions in a namespace |
| `resolve_contradiction` | `[ADMIN]` Mark a contradiction as resolved |
| `explain_memory` | Explain why a memory was stored (provenance trace) |

**Knowledge Graph**

| Tool | Description |
|---|---|
| `graph_search` | GraphRAG BFS traversal anchored by vector similarity; optional `as_of` |
| `neuromorphic_search` | GraphRAG spreading-activation traversal (spiking neural model) |
| `describe_schema` | List live entity types and edge predicates for a namespace |
| `suggest_queries` | Discover pre-optimised query templates |
| `execute_query_template` | Run a named query template |

**Code Intelligence**

| Tool | Description |
|---|---|
| `index_code_file` | AST-parse and embed a source file (async, returns `job_id`) |
| `check_indexing_status` | Poll `index_code_file` job status |
| `search_codebase` | Semantic search over indexed code chunks |

**Media / Artifacts**

| Tool | Description |
|---|---|
| `store_artifact` | Ingest media/PDF/log/diagnostics into MinIO + index metadata |
| `store_media` | **[DEPRECATED]** Alias for `store_artifact` |

**Replay & Snapshots**

| Tool | Description |
|---|---|
| `replay_observe` | `[ADMIN]` Stream historical events from a namespace |
| `replay_fork` | `[ADMIN]` Fork a namespace to a point in time |
| `replay_reconstruct` | `[ADMIN]` Reconstruct namespace state at a timestamp |
| `replay_status` | `[ADMIN]` Check replay job status |
| `get_event_provenance` | Provenance chain for a memory |
| `create_snapshot` | Create a namespace snapshot |
| `list_snapshots` | List snapshots |
| `delete_snapshot` | Delete a snapshot |
| `compare_states` | Diff two namespace states |
| `import_snapshot` | Import a snapshot |

**A2A (Agent-to-Agent)**

| Tool | Description |
|---|---|
| `a2a_create_grant` | Create a cross-namespace access grant |
| `a2a_revoke_grant` | Revoke a grant |
| `a2a_list_grants` | List active grants |
| `a2a_query_shared` | Query memories shared via a grant |
| `a2a_verify_grant_status` | Check grant validity |
| `a2a_update_grant_scopes` | Update grant permission scopes |
| `a2a_inspect_grant` | Inspect grant metadata |

**Document Bridges**

| Tool | Description |
|---|---|
| `connect_bridge` | Start OAuth for SharePoint / Google Drive / Dropbox bridge |
| `complete_bridge_auth` | Complete OAuth callback for a bridge |
| `list_bridges` | List active bridges |
| `disconnect_bridge` | Disconnect a bridge |
| `force_resync_bridge` | Force immediate re-sync of a bridge |
| `bridge_status` | Get bridge health and last-sync status |

**Admin / Operations**

All tools in this group are admin-enforced and carry `[ADMIN]`. `explain_past_decision` and `explain_config_change` set `admin_only=True` in their ToolSpec (mechanism **a** — the dispatch layer calls `_check_admin` before the handler; source: `nce/tool_registry.py`). The remaining tools (`manage_namespace`, `manage_quotas`, `rotate_signing_key`, `get_health`, `list_dlq`, `replay_dlq`, `purge_dlq`, `trigger_consolidation`, `consolidation_status`) are **not** gated by `admin_only=True`; they are enforced via membership in `MCP_ADMIN_TOOL_NAMES` (mechanism **b** — `enforce_mcp_tool_auth`, source: `nce/auth.py`) and require `admin_api_key` as a `required` field in their `inputSchema` (mechanism **c**, source: `nce/mcp_stdio_tools.py`).

| Tool | Description |
|---|---|
| `manage_namespace` | `[ADMIN]` Create / update / deactivate namespaces — requires `admin_api_key` |
| `manage_quotas` | `[ADMIN]` Set or inspect quota limits — requires `admin_api_key` |
| `rotate_signing_key` | `[ADMIN]` Rotate the HMAC signing key — requires `admin_api_key` |
| `get_health` | `[ADMIN]` Multi-DB health check — requires `admin_api_key` |
| `list_dlq` | `[ADMIN]` List dead-letter queue entries — requires `admin_api_key` |
| `replay_dlq` | `[ADMIN]` Retry a DLQ entry — requires `admin_api_key` |
| `purge_dlq` | `[ADMIN]` Purge a DLQ entry — requires `admin_api_key` |
| `trigger_consolidation` | `[ADMIN]` Trigger memory consolidation — requires `admin_api_key` |
| `consolidation_status` | `[ADMIN]` Check consolidation job status — requires `admin_api_key` |
| `explain_past_decision` | `[ADMIN]` Explain a past tool routing decision (`admin_only=True` in ToolSpec) |
| `explain_config_change` | `[ADMIN]` Explain a configuration change event (`admin_only=True` in ToolSpec) |

**Embedding Migrations** (when `NCE_DISABLE_MIGRATION_MCP=false`)

| Tool | Description |
|---|---|
| `start_migration` | `[ADMIN]` Begin an embedding model migration |
| `migration_status` | `[ADMIN]` Check migration progress |
| `validate_migration` | `[ADMIN]` Run quality gates on a finished migration |
| `commit_migration` | `[ADMIN]` Promote a validated migration to active |
| `abort_migration` | `[ADMIN]` Cancel and clean up a migration |

---

## 2. Admin REST API Mode

### How it works

`admin_server.py` is a Starlette application running on port 8003. It provides HTTP endpoints for programmatic search, event replay, snapshot export, A2A grant management, GC control, and admin observability. All `/api/` routes require HMAC-SHA256 authentication.

### Launch

```bash
python admin_server.py
# or with uvicorn directly:
uvicorn admin_server:app --host 0.0.0.0 --port 8003
```

With mTLS:

```bash
uvicorn admin_server:app \
  --ssl-certfile /etc/tls/server.crt \
  --ssl-keyfile  /etc/tls/server.key \
  --ssl-ca-certs /etc/tls/ca.crt
```

### Authentication

Every `/api/` request requires two headers:

```
X-NCE-Timestamp:  <unix_epoch_seconds>
Authorization:    HMAC-SHA256 <hex_signature>
```

Where:

```
canonical_message = METHOD\nPATH\nTIMESTAMP[\nSHA256_HEX(raw_body)]
signature         = HMAC-SHA256(NCE_API_KEY, canonical_message)
```

Notes:
- Body hash is omitted for empty-body requests (e.g. `GET`).
- Comparison is always constant-time (`hmac.compare_digest`).
- Timestamps outside ±5 minutes are rejected (replay protection).
- Optional mTLS is layered via `MTLSAuthMiddleware` when `NCE_ADMIN_MTLS_ENABLED=true`.

See [enterprise_security.md](enterprise_security.md) §2 for the full signing algorithm and distributed replay protection (Redis-backed `NonceStore`).

### POST /api/search

Unified semantic search — equivalent to the `semantic_search` MCP tool.

Request:

```http
POST /api/search HTTP/1.1
Content-Type: application/json
X-NCE-Timestamp: 1715510400
Authorization: HMAC-SHA256 <hex_signature>

{
  "namespace_id": "550e8400-e29b-41d4-a716-446655440000",
  "agent_id":     "ci-pipeline",
  "query":        "connection pool exhaustion handling",
  "top_k":        10,
  "as_of":        "2026-04-01T00:00:00Z"
}
```

Response (`200 OK`):

```json
{
  "results": [
    {
      "memory_id":  "a1b2c3d4-...",
      "score":      0.91,
      "summary":    "DB pooling best practices",
      "content":    "Full content text...",
      "created_at": "2026-03-15T09:12:00Z"
    }
  ]
}
```

Error responses:

| Code | Condition |
|---|---|
| `400` | Malformed JSON body |
| `422` | Missing required field (`namespace_id`, `agent_id`, or `query`) |
| `429` | Quota exceeded |
| `503` | Engine not connected |

### POST /api/replay/observe (streaming NDJSON)

Stream historical events from a namespace. The response body is `application/x-ndjson` — one JSON object per line.

Request:

```http
POST /api/replay/observe HTTP/1.1
Content-Type: application/json
X-NCE-Timestamp: 1715510400
Authorization: HMAC-SHA256 <hex_signature>

{
  "namespace_id":    "550e8400-e29b-41d4-a716-446655440000",
  "start_seq":       1,
  "end_seq":         500,
  "agent_id_filter": "claude-agent-01",
  "max_events":      200
}
```

Response stream (one JSON object per line):

```ndjson
{"type": "event", "seq": 1, "event_type": "store", "agent_id": "claude-agent-01", "occurred_at": "2026-03-01T10:00:00Z", "memory_id": "..."}
{"type": "progress", "events_streamed": 100}
{"type": "event", "seq": 2, "event_type": "search", "agent_id": "claude-agent-01", "occurred_at": "2026-03-01T10:05:00Z"}
{"type": "complete", "total_events": 187}
```

### POST /api/snapshot/export (streaming NDJSON)

Export all memories for a namespace at a point in time. GB-scale safe — uses server-side cursor.

Request:

```http
POST /api/snapshot/export HTTP/1.1
Content-Type: application/json
X-NCE-Timestamp: 1715510400
Authorization: HMAC-SHA256 <hex_signature>

{
  "namespace_id": "550e8400-e29b-41d4-a716-446655440000",
  "as_of":        "2026-05-01T00:00:00Z"
}
```

Response stream:

```ndjson
{"type": "metadata", "format_version": "1.0", "as_of": "2026-05-01T00:00:00Z", "namespace_id": "550e8400-..."}
{"type": "memory", "memory_id": "...", "content": "...", "created_at": "2026-03-15T09:12:00Z"}
{"type": "progress", "memories_exported": 100}
{"type": "complete", "total_memories": 342}
```

### POST /api/replay/fork

Fork a namespace to a point in time (creates a new namespace with a snapshot of state at `fork_point`).

Request:

```http
POST /api/replay/fork HTTP/1.1
Content-Type: application/json
X-NCE-Timestamp: <epoch>
Authorization: HMAC-SHA256 <hex_signature>

{
  "source_namespace_id": "550e8400-...",
  "fork_point":          "2026-04-15T12:00:00Z",
  "target_namespace_id": "new-namespace-uuid"
}
```

### GET /api/health

HMAC-authenticated (like all `/api/` routes). Returns status for all four database connections.

```http
GET /api/health HTTP/1.1
X-NCE-Timestamp: <epoch>
Authorization: HMAC-SHA256 <hex_signature>
```

```json
{
  "status": "healthy",
  "databases": {
    "postgres": "up",
    "mongodb":  "up",
    "redis":    "up",
    "minio":    "up"
  },
  "merkle_chain_valid": true
}
```

### Admin endpoint summary

Routes confirmed in `nce/admin_app.py` `build_admin_routes()` on main. Auth required on all `/api/` paths; only `/healthz` (not under `/api/`) is unauthenticated.

**Core**

| Method | Path | Description |
|---|---|---|
| `GET` | `/healthz` | Unauthenticated liveness probe |
| `GET` | `/api/health` | Multi-DB health check (HMAC-authenticated) |
| `GET` | `/api/health/v1` | Extended health check |
| `POST` | `/api/search` | Semantic search |
| `POST` | `/api/gc/trigger` | Force GC run |

**Replay & Snapshot**

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/replay/observe` | Stream historical events (NDJSON) |
| `POST` | `/api/replay/fork` | Fork namespace to a point in time |
| `GET` | `/api/replay/status/{run_id}` | Replay job status |
| `GET` | `/api/replay/provenance/{memory_id}` | Provenance chain for a memory |
| `POST` | `/api/snapshot/export` | Full namespace export (NDJSON) |

**A2A Grants**

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/a2a/grants/create` | Create an A2A grant token |
| `POST` | `/api/a2a/grants/{grant_id}/revoke` | Revoke an A2A grant |
| `GET` | `/api/a2a/grants` | List A2A grants |

**Admin Observability**

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/admin/events` | Event log feed |
| `GET` | `/api/admin/events/summary` | Event log summary |
| `GET` | `/api/admin/quotas` | Quota usage |
| `GET` | `/api/admin/quotas/summary` | Quota usage summary |
| `POST` | `/api/admin/graph/explore` | Knowledge graph explorer |
| `GET` | `/api/admin/graph/provenance/{memory_id}` | Graph provenance for a memory |
| `GET` | `/api/admin/verify-chain/{namespace_id}` | Verify Merkle chain integrity |
| `GET` | `/api/admin/signing/status` | Signing key status |
| `GET` | `/api/admin/pii-redactions` | PII redaction log |
| `GET` | `/api/admin/security/event-seq-gaps/{namespace_id}` | Event sequence gap check |
| `POST` | `/api/admin/security/verify-memory-sample` | Spot-check memory chain |
| `POST` | `/api/admin/security/test-rls-isolation` | Test RLS cross-namespace isolation |

**Admin Config & Settings**

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/admin/settings` | List settings |
| `PATCH` | `/api/admin/settings` | Patch settings |
| `GET` | `/api/admin/settings/effective` | Effective (merged) settings |
| `GET` | `/api/admin/settings/pending` | Pending settings |
| `POST` | `/api/admin/settings/reload` | Reload settings from store |
| `POST` | `/api/admin/settings/reset` | Reset to defaults |
| `POST` | `/api/admin/settings/rollback` | Roll back last settings change |
| `GET` | `/api/admin/settings/{key}` | Get a single setting |
| `GET` | `/api/admin/tools` | List tool enable/disable state |
| `POST` | `/api/admin/tools/toggle` | Toggle a tool on/off |

**Admin Namespaces & A2A**

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/admin/namespaces` | List namespaces |
| `GET` | `/api/admin/namespaces/{namespace_id}` | Get namespace |
| `POST` | `/api/admin/namespaces/{namespace_id}/metadata` | Update namespace metadata |
| `GET` | `/api/admin/a2a/grants` | Admin view of all grants |
| `GET` | `/api/admin/a2a/grants/summary` | Grant summary |
| `POST` | `/api/admin/a2a/grants/{grant_id}/revoke` | Admin-revoke a grant |

**Database & Infrastructure**

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/admin/db/postgres/status` | Postgres status |
| `GET` | `/api/admin/db/mongo/status` | MongoDB status |
| `GET` | `/api/admin/db/redis/status` | Redis status |
| `GET` | `/api/admin/db/minio/status` | MinIO status |
| `GET` | `/api/admin/connectors/status` | Connector status |
| `POST` | `/api/admin/connectors/save` | Save connector config |
| `GET` | `/api/admin/datastores/status` | Datastore status |
| `POST` | `/api/admin/datastores/save` | Save datastore config |
| `GET` | `/api/admin/schema` | Postgres schema dump |

**DLQ**

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/admin/dlq` | Dead-letter queue list |
| `POST` | `/api/admin/dlq/{dlq_id}/replay` | Replay a dead-letter job |
| `POST` | `/api/admin/dlq/{dlq_id}/purge` | Purge a dead-letter entry |

**Embedding Migrations**

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/admin/embedding-models` | List available embedding models |
| `POST` | `/api/admin/embedding-migrations/start` | Start an embedding migration |
| `GET` | `/api/admin/embedding-migrations/{migration_id}/status` | Migration status |
| `POST` | `/api/admin/embedding-migrations/{migration_id}/validate` | Validate migration |
| `POST` | `/api/admin/embedding-migrations/{migration_id}/commit` | Commit migration |
| `POST` | `/api/admin/embedding-migrations/{migration_id}/abort` | Abort migration |

---

## 3. Choosing a Mode

**Use MCP stdio** when:
- An LLM will call tools autonomously (Claude Desktop, Cursor, Windsurf, agent frameworks).
- You want the quota + cache layer to work transparently without building HTTP signing logic.
- You are running a local or self-hosted MCP server and the LLM client manages the process lifecycle.

**Use Admin REST API** when:
- You are building a dashboard, data pipeline, or CI/CD integration that needs programmatic access.
- You want streaming NDJSON for large exports or replays without loading results into RAM.
- You need to manage A2A grants, trigger GC, inspect DLQ entries, or verify Merkle chain integrity.
- You are operating in a multi-replica deployment where HMAC + mTLS gives you the right security boundary.

Both modes share the same `NCEEngine` and enforce the same RLS, quota, and audit trail guarantees — the surface layer is the only difference.
