"""MCP tool definitions for the stdio transport server."""

from __future__ import annotations

from mcp.types import Tool

from nce.config import cfg

# --- Tool Definitions ---

_MIGRATION_TOOLS = [
    Tool(
        name="start_migration",
        description="[Phase 2.1] Start an embedding migration.",
        inputSchema={
            "type": "object",
            "properties": {
                "target_model_id": {"type": "string"},
            },
            "required": ["target_model_id"],
        },
    ),
    Tool(
        name="migration_status",
        description="[Phase 2.1] Check the status of an embedding migration.",
        inputSchema={
            "type": "object",
            "properties": {
                "migration_id": {"type": "string"},
            },
            "required": ["migration_id"],
        },
    ),
    Tool(
        name="validate_migration",
        description="[Phase 2.1] Run quality gate checks on a finished migration.",
        inputSchema={
            "type": "object",
            "properties": {
                "migration_id": {"type": "string"},
            },
            "required": ["migration_id"],
        },
    ),
    Tool(
        name="commit_migration",
        description="[Phase 2.1] Commit a validated migration, making it the active model.",
        inputSchema={
            "type": "object",
            "properties": {
                "migration_id": {"type": "string"},
            },
            "required": ["migration_id"],
        },
    ),
    Tool(
        name="abort_migration",
        description="[Phase 2.1] Abort a migration and clean up.",
        inputSchema={
            "type": "object",
            "properties": {
                "migration_id": {"type": "string"},
            },
            "required": ["migration_id"],
        },
    ),
]

TOOLS = [
    Tool(
        name="store_memory",
        description=(
            "Persist a memory (conversation turn, document, or summary) to the Tri-Stack. "
            "Writes heavy payload to MongoDB, vector index to PostgreSQL, summary to Redis."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string"},
                "agent_id": {"type": "string"},
                "content": {"type": "string"},
                "summary": {
                    "type": "string",
                    "description": "Short summary used for embedding",
                },
                "heavy_payload": {
                    "type": "string",
                    "description": "Full raw content to archive",
                },
                "content_type": {
                    "type": "string",
                    "enum": ["chat", "code"],
                    "description": "Type of content",
                },
                "check_contradictions": {"type": "boolean", "default": False},
            },
            "required": ["namespace_id", "agent_id", "content"],
        },
    ),
    Tool(
        name="store_artifact",
        description=(
            "Ingest large artifacts (media, PDF, log, diagnostics) into the Quad-Stack. "
            "Uploads raw file to MinIO and indexes its metadata into the Tri-Stack."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string"},
                "user_id": {"type": "string"},
                "session_id": {"type": "string"},
                "media_type": {
                    "type": "string",
                    "enum": ["audio", "video", "image", "pdf", "log", "other"],
                },
                "file_path_on_disk": {
                    "type": "string",
                    "description": "Local path to the artifact file",
                },
                "summary": {
                    "type": "string",
                    "description": "AI-generated or manual summary of the artifact",
                },
            },
            "required": [
                "namespace_id",
                "user_id",
                "session_id",
                "media_type",
                "file_path_on_disk",
                "summary",
            ],
        },
    ),
    Tool(
        name="store_media",
        description=(
            "[DEPRECATED] Alias for store_artifact. Ingest large media into the Quad-Stack."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string"},
                "user_id": {"type": "string"},
                "session_id": {"type": "string"},
                "media_type": {"type": "string", "enum": ["audio", "video", "image"]},
                "file_path_on_disk": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": [
                "namespace_id",
                "user_id",
                "session_id",
                "media_type",
                "file_path_on_disk",
                "summary",
            ],
        },
    ),
    Tool(
        name="semantic_search",
        description=(
            "Search stored memories by semantic similarity. "
            "Uses pgvector cosine search then hydrates full content from MongoDB. "
            "Supply as_of to query the state of memory at a specific point in time (Phase 2.2 time travel)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string"},
                "agent_id": {"type": "string"},
                "query": {
                    "type": "string",
                    "description": "Natural language search query",
                },
                "limit": {
                    "type": "integer",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Maximum results to return after offset",
                },
                "offset": {
                    "type": "integer",
                    "default": 0,
                    "minimum": 0,
                    "description": "Skip this many ranked hits before returning the page",
                },
                "as_of": {
                    "type": "string",
                    "format": "date-time",
                    "description": (
                        "Optional ISO 8601 UTC timestamp (e.g. '2026-01-15T10:00:00Z'). "
                        "Restricts results to memories that existed at or before this instant. "
                        "Omit to query the current state."
                    ),
                },
            },
            "required": ["namespace_id", "agent_id", "query"],
        },
    ),
    Tool(
        name="index_code_file",
        description=(
            "Index a source code file into the Tri-Stack. "
            "Parses AST nodes (functions/classes), embeds each chunk, stores full file in MongoDB. "
            "Runs asynchronously: returns a job_id immediately."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "filepath": {
                    "type": "string",
                    "description": "Absolute or relative path of the file",
                },
                "raw_code": {
                    "type": "string",
                    "description": "Full source code content",
                },
                "language": {
                    "type": "string",
                    "description": "Language: 'python', 'javascript', 'typescript', 'go', 'rust'",
                },
                "namespace_id": {
                    "type": "string",
                    "description": "Namespace ID for scoping.",
                },
                "user_id": {
                    "type": "string",
                    "description": "Optional. Required when private=true — scopes this index to the user.",
                },
                "private": {
                    "type": "boolean",
                    "default": False,
                    "description": "When true, index is private to user_id (shared corpus uses user_id unset).",
                },
            },
            "required": ["filepath", "raw_code", "language"],
        },
    ),
    Tool(
        name="check_indexing_status",
        description="Check the status of a background indexing job.",
        inputSchema={
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The job_id returned by index_code_file",
                },
            },
            "required": ["job_id"],
        },
    ),
    Tool(
        name="search_codebase",
        description=(
            "Semantic search over indexed code chunks. "
            "Returns matching functions/classes with file path and line numbers."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language description of the code to find",
                },
                "namespace_id": {
                    "type": "string",
                    "description": "Namespace ID to search within.",
                },
                "language_filter": {
                    "type": "string",
                    "description": "Optional: filter by language ('python', 'javascript')",
                },
                "top_k": {"type": "integer", "default": 5},
                "user_id": {
                    "type": "string",
                    "description": "Optional. Required when private=true — searches only that user's private index.",
                },
                "private": {
                    "type": "boolean",
                    "default": False,
                    "description": "When false (default), search the shared corpus only. When true, search only chunks for user_id.",
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="graph_search",
        description=(
            "GraphRAG traversal over the Knowledge Graph. "
            "Finds the closest entity node by vector similarity, then BFS-traverses edges "
            "to return a structured subgraph with nodes, relations, and source document excerpts. "
            "Supply as_of to traverse the graph as it existed at a specific point in time (Phase 2.2 time travel)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language query to anchor the graph search",
                },
                "namespace_id": {
                    "type": "string",
                    "description": "Namespace ID to search within.",
                },
                "max_depth": {
                    "type": "integer",
                    "default": 2,
                    "description": "BFS hop depth (1-3 recommended)",
                },
                "user_id": {
                    "type": "string",
                    "description": "Optional. When supplied, restricts hydrated sources to this user.",
                },
                "private": {
                    "type": "boolean",
                    "default": False,
                    "description": "When true, only hydrate sources owned by user_id.",
                },
                "as_of": {
                    "type": "string",
                    "format": "date-time",
                    "description": (
                        "Optional ISO 8601 UTC timestamp (e.g. '2026-01-15T10:00:00Z'). "
                        "Traverses the knowledge graph as it existed at or before this instant. "
                        "Omit to traverse the current graph."
                    ),
                },
                "max_edges_per_node": {
                    "type": "integer",
                    "default": 512,
                    "minimum": 1,
                    "maximum": 2048,
                    "description": (
                        "Max incident edges loaded per BFS hop (SQL LIMIT, highest confidence first). "
                        "Prevents OOM on hub nodes."
                    ),
                },
                "edge_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5000,
                    "description": (
                        "Optional page size on the deduplicated edge list (omit for full page from edge_offset)."
                    ),
                },
                "edge_offset": {
                    "type": "integer",
                    "default": 0,
                    "minimum": 0,
                    "description": "Offset into deduplicated edges when using edge_limit.",
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="neuromorphic_search",
        description=(
            "GraphRAG spreading activation traversal over the Knowledge Graph. "
            "Uses a spiking neural model to search and traverse the knowledge graph "
            "instead of legacy BFS, returning a structured subgraph with nodes, relations, "
            "and source document excerpts."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language query to anchor the graph search",
                },
                "namespace_id": {
                    "type": "string",
                    "description": "Namespace ID to search within.",
                },
                "max_depth": {
                    "type": "integer",
                    "default": 2,
                    "description": "Maximum BFS hop depth for traversal",
                },
                "user_id": {
                    "type": "string",
                    "description": "Optional. When supplied, restricts hydrated sources to this user.",
                },
                "private": {
                    "type": "boolean",
                    "default": False,
                    "description": "When true, only hydrate sources owned by user_id.",
                },
                "as_of": {
                    "type": "string",
                    "format": "date-time",
                    "description": (
                        "Optional ISO 8601 UTC timestamp (e.g. '2026-01-15T10:00:00Z'). "
                        "Traverses the knowledge graph as it existed at or before this instant."
                    ),
                },
                "max_edges_per_node": {
                    "type": "integer",
                    "default": 512,
                    "minimum": 1,
                    "maximum": 2048,
                    "description": "Max incident edges loaded per hop.",
                },
                "edge_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5000,
                    "description": "Optional page size on the deduplicated edge list.",
                },
                "edge_offset": {
                    "type": "integer",
                    "default": 0,
                    "minimum": 0,
                    "description": "Offset into deduplicated edges when using edge_limit.",
                },
                "telemetry_severity": {
                    "type": "number",
                    "description": "Optional system telemetry severity score to dynamically tune spreading thresholds.",
                },
                "theta": {
                    "type": "number",
                    "default": 0.5,
                    "description": "Spiking threshold potential.",
                },
                "decay": {
                    "type": "number",
                    "default": 0.85,
                    "description": "Spiking potential decay factor.",
                },
                "alpha": {
                    "type": "number",
                    "default": 1.0,
                    "description": "Transfer weight coefficient for signal propagation.",
                },
                "ticks": {
                    "type": "integer",
                    "description": "Number of propagation steps (defaults to max_depth).",
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="get_recent_context",
        description=(
            "Retrieve the N most recent episodic memories for an agent. "
            "Useful for manual context reconstruction or auditing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string"},
                "agent_id": {
                    "type": "string",
                    "description": "Agent identifier; 'default' if not specified.",
                },
                "limit": {
                    "type": "integer",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 100,
                },
                "offset": {
                    "type": "integer",
                    "default": 0,
                    "minimum": 0,
                    "description": "Skip this many most-recent rows before returning limit rows",
                },
                "as_of": {
                    "type": "string",
                    "format": "date-time",
                    "description": "Optional point-in-time reference (Phase 2.2).",
                },
            },
            "required": ["namespace_id"],
        },
    ),
    Tool(
        name="connect_bridge",
        description=(
            "Start OAuth for a document bridge (SharePoint / Google Drive / Dropbox). "
            "Creates a bridge_subscriptions row and returns auth_url when OAuth is configured."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "Owning user id"},
                "provider": {
                    "type": "string",
                    "enum": ["sharepoint", "gdrive", "dropbox"],
                    "description": "Bridge provider",
                },
            },
            "required": ["user_id", "provider"],
        },
    ),
    Tool(
        name="complete_bridge_auth",
        description=(
            "Exchange OAuth authorization code, create provider push subscription / watch "
            "when webhook base URL is set, and mark bridge ACTIVE."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "bridge_id": {
                    "type": "string",
                    "description": "UUID from connect_bridge",
                },
                "provider": {
                    "type": "string",
                    "enum": ["sharepoint", "gdrive", "dropbox"],
                },
                "authorization_code": {
                    "type": "string",
                    "description": "OAuth code from redirect",
                },
                "code": {
                    "type": "string",
                    "description": "Alias for authorization_code",
                },
                "resource_id": {
                    "type": "string",
                    "description": (
                        "Provider resource: SharePoint 'site_id|drive_id'; "
                        "Drive: folder or root as used by watch; Dropbox: account id"
                    ),
                },
            },
            "required": ["user_id", "bridge_id", "provider"],
        },
    ),
    Tool(
        name="list_bridges",
        description="List bridge subscriptions for a user.",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "include_disconnected": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include DISCONNECTED rows",
                },
            },
            "required": ["user_id"],
        },
    ),
    Tool(
        name="disconnect_bridge",
        description="Stop provider subscription / channel when tokens are configured; mark DISCONNECTED.",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "bridge_id": {"type": "string"},
            },
            "required": ["user_id", "bridge_id"],
        },
    ),
    Tool(
        name="force_resync_bridge",
        description="Clear stored cursor, optional Redis cursor key, enqueue a full bridge sync job.",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "bridge_id": {"type": "string"},
            },
            "required": ["user_id", "bridge_id"],
        },
    ),
    Tool(
        name="bridge_status",
        description="Return one bridge subscription row (public fields) and expiry hint.",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "bridge_id": {"type": "string"},
            },
            "required": ["user_id", "bridge_id"],
        },
    ),
    Tool(
        name="boost_memory",
        description="[Phase 1.1] Boosts the salience of a memory for the calling agent.",
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string"},
                "agent_id": {"type": "string"},
                "namespace_id": {"type": "string"},
                "factor": {"type": "number", "default": 0.2},
            },
            "required": ["memory_id", "agent_id", "namespace_id"],
        },
    ),
    Tool(
        name="forget_memory",
        description="[Phase 1.1] Sets salience to 0.0 for the calling agent.",
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string"},
                "agent_id": {"type": "string"},
                "namespace_id": {"type": "string"},
            },
            "required": ["memory_id", "agent_id", "namespace_id"],
        },
    ),
    Tool(
        name="list_contradictions",
        description="[Phase 1.3] List detected contradictions.",
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string"},
                "resolution": {
                    "type": "string",
                    "description": "Filter by resolution status (e.g. 'unresolved')",
                },
                "agent_id": {"type": "string"},
            },
            "required": ["namespace_id"],
        },
    ),
    Tool(
        name="resolve_contradiction",
        description="[Phase 1.3] Resolve a contradiction. Requires namespace_id for RLS enforcement.",
        inputSchema={
            "type": "object",
            "properties": {
                "contradiction_id": {"type": "string"},
                "namespace_id": {
                    "type": "string",
                    "description": "Tenant namespace (RLS-enforced — only contradictions in the caller's namespace can be resolved).",
                },
                "resolution": {
                    "type": "string",
                    "enum": [
                        "resolved_a",
                        "resolved_b",
                        "both_valid",
                        "false_positive",
                    ],
                },
                "resolved_by": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": [
                "contradiction_id",
                "namespace_id",
                "resolution",
                "resolved_by",
            ],
        },
    ),
    Tool(
        name="unredact_memory",
        description="[ADMIN] Reverses pseudonymisation for a given memory. Requires elevated permissions.",
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string"},
                "namespace_id": {"type": "string"},
                "agent_id": {"type": "string"},
                "admin_api_key": {
                    "type": "string",
                    "description": "Server-side admin API key for elevated access",
                },
            },
            "required": ["memory_id", "namespace_id", "agent_id", "admin_api_key"],
        },
    ),
    Tool(
        name="shred_memory",
        description=(
            "[ADMIN][Part II.4] Provably forget a memory across every store: destroys "
            "the per-memory DEK (making the encrypted raw payload cryptographically "
            "unrecoverable), deletes all plaintext derivatives (FTS, embeddings, KG "
            "labels/edges via ATMS cascade, PII vault), purges Redis + MinIO, and "
            "appends a signed, content-free 'memory_shredded' WORM event.  Returns a "
            "verifiable deletion receipt."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string"},
                "namespace_id": {"type": "string"},
                "agent_id": {"type": "string"},
                "admin_api_key": {
                    "type": "string",
                    "description": "Server-side admin API key for elevated access",
                },
            },
            "required": ["memory_id", "namespace_id", "agent_id", "admin_api_key"],
        },
    ),
    # Migration tools are appended conditionally below
    Tool(
        name="replay_observe",
        description=(
            "[Phase 2.3] Stream historical events from event_log back to the caller "
            "without modifying any engine state.  Returns a JSONL-encoded list of "
            "event dicts (one per line), terminated by a 'complete' summary line.  "
            "Useful for auditing, debugging, and point-in-time inspection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {
                    "type": "string",
                    "description": "Source namespace UUID to stream events from.",
                },
                "start_seq": {
                    "type": "integer",
                    "default": 1,
                    "description": "Inclusive lower bound on event_seq (default: 1).",
                },
                "end_seq": {
                    "type": "integer",
                    "description": "Inclusive upper bound on event_seq.  Omit to stream to the latest event.",
                },
                "agent_id_filter": {
                    "type": "string",
                    "description": "Optional: restrict stream to events from this agent_id.",
                },
                "max_events": {
                    "type": "integer",
                    "default": 500,
                    "description": "Hard cap on the number of events returned in one call (default: 500).",
                },
                "admin_api_key": {
                    "type": "string",
                    "description": "Server-side admin API key for elevated access",
                },
            },
            "required": ["namespace_id", "admin_api_key"],
        },
    ),
    Tool(
        name="replay_fork",
        description=(
            "[Phase 2.3] Fork a namespace by replaying its event_log into an isolated "
            "target namespace.  Events up to 'fork_seq' are applied with fresh HMAC "
            "signatures (alternate causal provenance).  In 'deterministic' mode, LLM "
            "responses are served from the MinIO payload cache for byte-identical "
            "reconstruction.  In 're-execute' mode, the LLM provider is called fresh, "
            "allowing intentional divergence (e.g. A/B testing consolidation prompts).  "
            "Returns a run_id immediately; use replay_status to poll progress."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "source_namespace_id": {
                    "type": "string",
                    "description": "Namespace to replay events FROM.",
                },
                "target_namespace_id": {
                    "type": "string",
                    "description": "Namespace to replay events INTO (must exist and be empty).",
                },
                "fork_seq": {
                    "type": "integer",
                    "description": "Inclusive upper bound: replay events with event_seq <= fork_seq.",
                },
                "start_seq": {
                    "type": "integer",
                    "default": 1,
                    "description": "Inclusive lower bound on event_seq (default: 1).",
                },
                "replay_mode": {
                    "type": "string",
                    "enum": ["deterministic", "re-execute"],
                    "default": "deterministic",
                    "description": (
                        "'deterministic': use cached MinIO LLM payloads.  "
                        "'re-execute': call LLM fresh, optionally with config_overrides."
                    ),
                },
                "config_overrides": {
                    "type": "object",
                    "properties": {
                        "llm_provider": {
                            "type": "string",
                            "enum": [
                                "local-cognitive-model",
                                "openai",
                                "azure_openai",
                                "deepseek",
                                "moonshot_kimi",
                                "openai_compatible",
                                "google_gemini",
                                "anthropic",
                            ],
                        },
                        "llm_model": {"type": "string"},
                        "llm_credentials": {"type": "string"},
                        "llm_temperature": {"type": "number"},
                    },
                    "additionalProperties": False,
                    "description": (
                        "Optional overrides for re-execute mode only. "
                        "Allowed keys: llm_provider, llm_model, llm_credentials, llm_temperature."
                    ),
                },
                "agent_id_filter": {
                    "type": "string",
                    "description": "Optional: replay only events from this agent_id.",
                },
                "admin_api_key": {
                    "type": "string",
                    "description": "Server-side admin API key for elevated access",
                },
            },
            "required": [
                "source_namespace_id",
                "target_namespace_id",
                "fork_seq",
                "admin_api_key",
            ],
        },
    ),
    Tool(
        name="replay_reconstruct",
        description=(
            "[Phase 2.3] Reconstruct a byte-identical state by replaying an empty target "
            "namespace from the source namespace's event_log up to end_seq.  All events "
            "are applied deterministically — no LLM re-execution.  UUIDs are remapped "
            "(original → new) to avoid constraint violations."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "source_namespace_id": {
                    "type": "string",
                    "description": "Namespace to replay events FROM.",
                },
                "target_namespace_id": {
                    "type": "string",
                    "description": "Namespace to replay events INTO (should be empty for true reconstruction).",
                },
                "end_seq": {
                    "type": "integer",
                    "description": "Inclusive upper bound: replay events with event_seq <= end_seq.",
                },
                "start_seq": {
                    "type": "integer",
                    "default": 1,
                    "description": "Inclusive lower bound on event_seq (default: 1).",
                },
                "agent_id_filter": {
                    "type": "string",
                    "description": "Optional: replay only events from this agent_id.",
                },
                "admin_api_key": {
                    "type": "string",
                    "description": "Server-side admin API key for elevated access",
                },
            },
            "required": [
                "source_namespace_id",
                "target_namespace_id",
                "end_seq",
                "admin_api_key",
            ],
        },
    ),
    Tool(
        name="replay_status",
        description=(
            "[Phase 2.3] Poll the status and progress of an active or completed replay run."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "UUID returned by replay_fork.",
                },
                "admin_api_key": {
                    "type": "string",
                    "description": "Server-side admin API key for elevated access",
                },
            },
            "required": ["run_id", "admin_api_key"],
        },
    ),
    Tool(
        name="get_event_provenance",
        description=(
            "[Phase 2.3] Return the full causal chain for a memory: the event_log "
            "entries that created and modified it, traversed via parent_event_id.  "
            "Useful for auditing forked replays and tracing alternate causal provenance."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "UUID of the memory whose provenance to trace.",
                },
            },
            "required": ["memory_id"],
        },
    ),
    Tool(
        name="explain_memory",
        description=(
            "Return the signed epistemic receipt for a memory (remembered event seq, "
            "agent_id, occurred_at timestamp, and cryptographic signature verification status)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "UUID of the memory to explain.",
                },
            },
            "required": ["memory_id"],
        },
    ),
    Tool(
        name="explain_past_decision",
        description=(
            "[Phase II.5] Bi-temporal accountability — reconstruct the agent's belief "
            "state as it stood at a past timestamp ('as_of'): the set of memories valid "
            "at T, each annotated with the signed epistemic receipt (provenance event) "
            "that was valid then.  Optionally run a *verified* counterfactual forked "
            "replay (supply source_namespace_id, target_namespace_id, fork_seq and "
            "expected_sha256) whose digest_match outcome proves the reconstruction is "
            "byte-identically faithful."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {
                    "type": "string",
                    "description": "Namespace whose past belief state to reconstruct.",
                },
                "as_of": {
                    "type": "string",
                    "description": (
                        "ISO 8601 timestamp (e.g. '2026-01-15T10:00:00Z').  Omit to "
                        "reconstruct the current belief set."
                    ),
                },
                "agent_id_filter": {
                    "type": "string",
                    "description": "Optional: restrict beliefs/receipts to this agent_id.",
                },
                "max_beliefs": {
                    "type": "integer",
                    "default": 200,
                    "description": "Hard cap on the number of beliefs returned (default: 200).",
                },
                "source_namespace_id": {
                    "type": "string",
                    "description": "Counterfactual: namespace to replay events FROM.",
                },
                "target_namespace_id": {
                    "type": "string",
                    "description": "Counterfactual: empty namespace to replay events INTO.",
                },
                "fork_seq": {
                    "type": "integer",
                    "description": "Counterfactual: replay events with event_seq <= fork_seq.",
                },
                "start_seq": {
                    "type": "integer",
                    "default": 1,
                    "description": "Counterfactual: inclusive lower bound on event_seq.",
                },
                "replay_mode": {
                    "type": "string",
                    "enum": ["deterministic", "re-execute"],
                    "default": "deterministic",
                    "description": "Counterfactual replay mode (default: deterministic).",
                },
                "config_overrides": {
                    "type": "object",
                    "description": "Counterfactual: optional re-execute config overrides.",
                },
                "expected_sha256": {
                    "type": "string",
                    "description": (
                        "Counterfactual: 64-char hex checksum over the canonical fork "
                        "request (required when a fork is requested)."
                    ),
                },
                "admin_api_key": {
                    "type": "string",
                    "description": "Server-side admin API key for elevated access",
                },
            },
            "required": ["namespace_id", "admin_api_key"],
        },
    ),
    Tool(
        name="explain_config_change",
        description=(
            "[Phase V.6] Config time-travel audit — return a configuration key's full "
            "change history by folding the ordered config_changed/config_reset WORM "
            "events that touched it (timestamp, actor, reason, old->new for non-secrets). "
            "Secret values are never returned: only the recorded set/unset/rotated "
            "lifecycle token. The audit companion to GET /api/admin/settings/effective?as_of=T."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Registry configuration key whose change history to return.",
                },
                "admin_api_key": {
                    "type": "string",
                    "description": "Server-side admin API key for elevated access",
                },
            },
            "required": ["key", "admin_api_key"],
        },
    ),
    Tool(
        name="a2a_create_grant",
        description=(
            "[Phase 3.1] Create an A2A sharing grant — generates a secure token "
            "that another agent can use to access your memories within the declared scopes. "
            "Returns grant_id and a one-time sharing_token to pass to the recipient out-of-band."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {
                    "type": "string",
                    "description": "Owner namespace UUID.",
                },
                "agent_id": {"type": "string", "description": "Owner agent ID."},
                "scopes": {
                    "type": "array",
                    "description": "List of scope objects. Each has resource_type, resource_id, and permissions.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "resource_type": {
                                "type": "string",
                                "enum": ["namespace", "memory", "kg_node", "subgraph"],
                            },
                            "resource_id": {"type": "string"},
                            "permissions": {
                                "type": "array",
                                "items": {"type": "string", "enum": ["read"]},
                            },
                        },
                        "required": ["resource_type", "resource_id"],
                    },
                },
                "target_namespace_id": {
                    "type": "string",
                    "description": "Optional: restrict to a specific recipient namespace.",
                },
                "target_agent_id": {
                    "type": "string",
                    "description": "Optional: restrict to a specific recipient agent.",
                },
                "expires_in_seconds": {
                    "type": "integer",
                    "default": 3600,
                    "description": "Token lifetime (60–2592000 s).",
                },
            },
            "required": ["namespace_id", "agent_id", "scopes"],
        },
    ),
    Tool(
        name="a2a_revoke_grant",
        description=(
            "[Phase 3.1] Revoke an active A2A sharing grant. "
            "Only the owning namespace can revoke its own grants."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {
                    "type": "string",
                    "description": "Owner namespace UUID.",
                },
                "agent_id": {"type": "string", "description": "Owner agent ID."},
                "grant_id": {
                    "type": "string",
                    "description": "UUID of the grant to revoke.",
                },
            },
            "required": ["namespace_id", "agent_id", "grant_id"],
        },
    ),
    Tool(
        name="a2a_list_grants",
        description=(
            "[Phase 3.1] List all active A2A sharing grants owned by this namespace. "
            "Token hashes are never returned — only grant metadata."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {
                    "type": "string",
                    "description": "Owner namespace UUID.",
                },
                "agent_id": {"type": "string", "description": "Owner agent ID."},
                "include_inactive": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include revoked and expired grants for audit purposes.",
                },
            },
            "required": ["namespace_id", "agent_id"],
        },
    ),
    Tool(
        name="a2a_query_shared",
        description=(
            "[Phase 3.1] Execute a semantic search against another agent's memories "
            "using an A2A sharing token. Validates the token, enforces scope constraints, "
            "then queries the owner's namespace under RLS. "
            "Error -32010 = unauthorized token. Error -32011 = scope violation."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "sharing_token": {
                    "type": "string",
                    "description": "Token provided by the owning agent.",
                },
                "consumer_namespace_id": {
                    "type": "string",
                    "description": "UUID of the namespace consuming the token.",
                },
                "consumer_agent_id": {
                    "type": "string",
                    "description": "Agent ID of the consumer.",
                },
                "query": {
                    "type": "string",
                    "description": "Semantic search query string.",
                },
                "resource_type": {
                    "type": "string",
                    "enum": ["namespace", "memory", "kg_node", "subgraph"],
                    "default": "namespace",
                    "description": "Resource type to validate against granted scopes.",
                },
                "resource_id": {
                    "type": "string",
                    "description": "Specific resource ID to validate; omit for namespace-level grants.",
                },
                "top_k": {"type": "integer", "default": 5},
            },
            "required": ["sharing_token", "consumer_namespace_id", "query"],
        },
    ),
    Tool(
        name="a2a_verify_grant_status",
        description=(
            "[Phase 3.1] Verify the validity, scopes, and expiration of an A2A grant. "
            "Pass namespace_id and agent_id of the caller, plus exactly one of sharing_token or grant_id."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {
                    "type": "string",
                    "description": "Calling namespace UUID (can be owner or target).",
                },
                "agent_id": {
                    "type": "string",
                    "description": "Calling agent ID.",
                },
                "sharing_token": {
                    "type": "string",
                    "description": "The raw sharing token to look up (optional).",
                },
                "grant_id": {
                    "type": "string",
                    "description": "The grant UUID to look up (optional).",
                },
            },
            "required": ["namespace_id", "agent_id"],
        },
    ),
    Tool(
        name="a2a_update_grant_scopes",
        description=(
            "[Phase 3.1] Dynamically mutate the scopes on an active grant owned by this namespace. "
            "Uses a replace or append strategy without breaking active consumer integrations."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {
                    "type": "string",
                    "description": "Owner namespace UUID.",
                },
                "agent_id": {
                    "type": "string",
                    "description": "Owner agent ID.",
                },
                "grant_id": {
                    "type": "string",
                    "description": "UUID of the grant to update.",
                },
                "scopes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "resource_type": {
                                "type": "string",
                                "enum": ["namespace", "memory", "kg_node", "subgraph"],
                            },
                            "resource_id": {"type": "string"},
                            "permissions": {
                                "type": "array",
                                "items": {"type": "string", "enum": ["read"]},
                                "default": ["read"],
                            },
                        },
                        "required": ["resource_type", "resource_id"],
                    },
                    "description": "List of scopes to apply.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["replace", "append"],
                    "default": "replace",
                    "description": "replace (overwrite) or append (merge) scopes.",
                },
            },
            "required": ["namespace_id", "agent_id", "grant_id", "scopes"],
        },
    ),
    Tool(
        name="a2a_inspect_grant",
        description=(
            "[Phase 3.1] Safe metadata inspection of a single A2A grant. "
            "Restricted to the owner namespace; ensures the token_hash is never exposed."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {
                    "type": "string",
                    "description": "Owner namespace UUID.",
                },
                "agent_id": {
                    "type": "string",
                    "description": "Owner agent ID.",
                },
                "grant_id": {
                    "type": "string",
                    "description": "UUID of the grant to inspect.",
                },
            },
            "required": ["namespace_id", "agent_id", "grant_id"],
        },
    ),
    Tool(
        name="manage_namespace",
        description="[ADMIN] Manage namespaces: create, list, grant, revoke, update_metadata.",
        inputSchema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": ["create", "list", "grant", "revoke", "update_metadata", "delete"],
                },
                "namespace_id": {"type": "string"},
                "create": {
                    "type": "object",
                    "properties": {
                        "slug": {"type": "string"},
                        "parent_id": {"type": "string"},
                        "metadata": {"type": "object", "additionalProperties": True},
                    },
                    "required": ["slug"],
                },
                "metadata_patch": {"type": "object", "additionalProperties": True},
                "grantee_namespace_id": {"type": "string"},
                "admin_api_key": {
                    "type": "string",
                    "description": "Server-side admin API key for elevated access",
                },
            },
            "required": ["command", "admin_api_key"],
        },
    ),
    Tool(
        name="verify_memory",
        description="[Phase 0.2] Verify the integrity and causal provenance of a memory.",
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string"},
                "as_of": {"type": "string", "format": "date-time"},
            },
            "required": ["memory_id"],
        },
    ),
    Tool(
        name="trigger_consolidation",
        description="[ADMIN] Manually trigger a sleep-consolidation run for a namespace.",
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string"},
                "since_timestamp": {
                    "type": "string",
                    "format": "date-time",
                    "description": "Optional filter for events since this point.",
                },
                "admin_api_key": {
                    "type": "string",
                    "description": "Server-side admin API key for elevated access",
                },
            },
            "required": ["namespace_id", "admin_api_key"],
        },
    ),
    Tool(
        name="consolidation_status",
        description="[ADMIN] Check the status of a consolidation run.",
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "admin_api_key": {
                    "type": "string",
                    "description": "Server-side admin API key for elevated access",
                },
            },
            "required": ["run_id", "admin_api_key"],
        },
    ),
    Tool(
        name="manage_quotas",
        description="[ADMIN] Manage resource quotas for a namespace.",
        inputSchema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": ["set", "list", "delete", "reset"],
                },
                "namespace_id": {"type": "string"},
                "agent_id": {"type": "string"},
                "resource_type": {
                    "type": "string",
                    "enum": ["llm_tokens", "storage_bytes", "memory_count"],
                },
                "limit": {"type": "integer"},
                "admin_api_key": {
                    "type": "string",
                    "description": "Server-side admin API key for elevated access",
                },
            },
            "required": ["command", "namespace_id", "admin_api_key"],
        },
    ),
    Tool(
        name="rotate_signing_key",
        description="[ADMIN] Generate a new active signing key and retire the current one.",
        inputSchema={
            "type": "object",
            "properties": {
                "admin_api_key": {
                    "type": "string",
                    "description": "Server-side admin API key for elevated access",
                },
            },
            "required": ["admin_api_key"],
        },
    ),
    Tool(
        name="get_health",
        description="[ADMIN] Comprehensive system health check (v1.0).",
        inputSchema={
            "type": "object",
            "properties": {
                "admin_api_key": {
                    "type": "string",
                    "description": "Server-side admin API key for elevated access",
                },
            },
            "required": ["admin_api_key"],
        },
    ),
    Tool(
        name="list_dlq",
        description="[ADMIN] List dead-letter queue entries (failed tasks that exhausted retries).",
        inputSchema={
            "type": "object",
            "properties": {
                "admin_api_key": {"type": "string", "description": "Admin API key"},
                "task_name": {"type": "string", "description": "Filter by task function name"},
                "status": {"type": "string", "enum": ["pending", "replayed", "purged"]},
                "limit": {"type": "integer", "default": 50},
                "offset": {"type": "integer", "default": 0},
            },
            "required": ["admin_api_key"],
        },
    ),
    Tool(
        name="replay_dlq",
        description="[ADMIN] Mark a dead-letter queue entry as replayed (re-enqueue manually).",
        inputSchema={
            "type": "object",
            "properties": {
                "admin_api_key": {"type": "string", "description": "Admin API key"},
                "dlq_id": {"type": "string", "description": "UUID of the DLQ entry to replay"},
            },
            "required": ["admin_api_key", "dlq_id"],
        },
    ),
    Tool(
        name="purge_dlq",
        description="[ADMIN] Permanently remove a dead-letter queue entry.",
        inputSchema={
            "type": "object",
            "properties": {
                "admin_api_key": {"type": "string", "description": "Admin API key"},
                "dlq_id": {"type": "string", "description": "UUID of the DLQ entry to purge"},
            },
            "required": ["admin_api_key", "dlq_id"],
        },
    ),
    Tool(
        name="create_snapshot",
        description="Create a named point-in-time reference (snapshot) for a namespace.",
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string"},
                "name": {"type": "string"},
                "agent_id": {"type": "string", "default": "default"},
                "snapshot_at": {"type": "string", "format": "date-time"},
                "metadata": {"type": "object", "additionalProperties": True},
            },
            "required": ["namespace_id", "name"],
        },
    ),
    Tool(
        name="list_snapshots",
        description="List all snapshots for a given namespace.",
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string"},
            },
            "required": ["namespace_id"],
        },
    ),
    Tool(
        name="delete_snapshot",
        description="Delete a point-in-time reference (snapshot) for a namespace.",
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string"},
                "snapshot_id": {"type": "string"},
            },
            "required": ["namespace_id", "snapshot_id"],
        },
    ),
    Tool(
        name="compare_states",
        description="Diff the memory state between two points in time.",
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string"},
                "as_of_a": {"type": "string", "format": "date-time"},
                "as_of_b": {"type": "string", "format": "date-time"},
                "query": {"type": "string"},
                "top_k": {"type": "integer", "default": 10},
            },
            "required": ["namespace_id", "as_of_a", "as_of_b"],
        },
    ),
    Tool(
        name="import_snapshot",
        description="Rebuild a namespace from an exported NDJSON snapshot via the Saga path.",
        inputSchema={
            "type": "object",
            "properties": {
                "target_namespace_id": {
                    "type": "string",
                    "description": "The namespace UUID to restore the snapshot into (must exist).",
                },
                "snapshot_data": {
                    "type": "string",
                    "description": "The raw NDJSON snapshot data to import.",
                },
            },
            "required": ["target_namespace_id", "snapshot_data"],
        },
    ),
    # ── Phase 1 Enterprise: Query Catalog ─────────────────────────────────────
    Tool(
        name="suggest_queries",
        description=(
            "[Phase 1E] Given a natural-language intent, returns ranked pre-optimised "
            "query templates the agent can execute without constructing raw search "
            "parameters. Use this before calling execute_query_template."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {
                    "type": "string",
                    "description": "Caller namespace UUID.",
                },
                "intent": {
                    "type": "string",
                    "description": "Natural language description of what you want to retrieve.",
                },
                "top_k": {
                    "type": "integer",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": ["namespace_id", "intent"],
        },
    ),
    Tool(
        name="execute_query_template",
        description=(
            "[Phase 1E] Execute a named query template by slug. "
            "Tenant isolation is injected server-side — do NOT pass namespace_id "
            "in the parameters object."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {
                    "type": "string",
                    "description": "Caller namespace UUID (used for RLS, not passed to template).",
                },
                "slug": {
                    "type": "string",
                    "description": "Template slug returned by suggest_queries.",
                },
                "parameters": {
                    "type": "object",
                    "description": "Slot values required by the template's param_schema.",
                    "additionalProperties": True,
                },
            },
            "required": ["namespace_id", "slug"],
        },
    ),
    Tool(
        name="describe_schema",
        description=(
            "[Phase 1E] Return the live graph schema for this namespace: distinct "
            "entity types and edge predicates.  Use before constructing graph_search "
            "constraints to avoid hallucinated predicates."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {
                    "type": "string",
                    "description": "Caller namespace UUID.",
                },
                "limit": {
                    "type": "integer",
                    "default": 50,
                    "minimum": 1,
                    "maximum": 200,
                },
            },
            "required": ["namespace_id"],
        },
    ),
    Tool(
        name="system_design_ping",
        description=(
            "[M6.W1] Liveness probe for the System Design vertical. Verifies the "
            "caller's namespace_id is present and returns "
            '{"ok": true, "engine": "system_design"}. Reads nothing and changes '
            "nothing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {
                    "type": "string",
                    "description": "Caller namespace UUID.",
                },
            },
            "required": ["namespace_id"],
        },
    ),
    Tool(
        name="system_design_publish_design_docs",
        description=(
            "[M6.W11] EXPORT ONLY - publish a System Design DESIGN and its "
            "DESIGN_LINE / FUNCTIONAL_LOCATION tree to Lucid. Lucid IMPORT is "
            "cut and is not reachable here. Returns a 'lucid_url', which is "
            "null when Lucid credentials are unset - an unconfigured export is "
            "a clean no-op, not an error. Also reachable over REST as POST "
            "/api/system-design/publish-design-docs."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {
                    "type": "string",
                    "description": "Caller namespace UUID.",
                },
                "design_id": {
                    "type": "string",
                    "description": "Design identifier (the DESIGN node's id).",
                },
            },
            "required": ["namespace_id", "design_id"],
        },
    ),
    Tool(
        name="system_design_get_topology",
        description=(
            "[M6.W13a] Read-only: return a System Design DESIGN's full topology — "
            "the design node, its functional-location tree, its devices (each with "
            "AVIXA capability attributes and ports), its RACKS (each with capability "
            "attributes), its cables, and the edges between them. Also returns "
            "'geometry' - the canvas layout of every node in the design, keyed by "
            "node label, where x/y are CANVAS GRID UNITS with the origin TOP-LEFT and "
            "y increasing DOWNWARD (room dimensions are not x/y; they are in "
            "meta.copper.room.w/d/h, in METERS) - and 'version', the design's "
            "optimistic-concurrency token to pass back as expected_version on the "
            "next write. A node absent from 'geometry' has never been placed. "
            "'version' 0 means the design has never been authored."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {
                    "type": "string",
                    "description": "Caller namespace UUID.",
                },
                "design_id": {
                    "type": "string",
                    "description": "Design identifier (the DESIGN node's id).",
                },
                "statuses": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "[M6.W16b] LIVE lifecycle filter — no longer a no-op. Narrows "
                        "'devices', 'racks' and 'cables' to nodes whose stored status is "
                        "one of these values; a device's ports follow their device. It "
                        "does NOT narrow 'design', 'functional_locations', 'edges', "
                        "'geometry', 'state' or 'version' — a filtered read is a view of "
                        "the lifecycle-bearing nodes, not a subgraph, so you can still "
                        "see what a filtered-out node was attached to and why it was "
                        "excluded. A NODE WITH NO STATE ROW NEVER MATCHES, and neither "
                        "does one whose status is null: absence of a declaration is not "
                        "the default status, and everything authored before M6.W16 is "
                        "absent. The vocabulary is NetBox's and is per node type — it is "
                        "spelled once, on system_design_author_topology's devices / "
                        "connections / racks descriptions, and is not repeated here. "
                        "Values are matched verbatim: no case folding, no trimming, and "
                        "an unknown value is accepted and simply matches nothing. Omit "
                        "it, or send [], for no filter."
                    ),
                },
            },
            "required": ["namespace_id", "design_id"],
        },
    ),
    Tool(
        name="system_design_author_topology",
        description=(
            "[M6.W13b] Author a System Design DESIGN's device topology: DEVICE / "
            "PORT / RACK / CABLE nodes, their AVIXA capability attributes, and the "
            "signal-path edges between ports. ADDITIVE ONLY: re-authoring the same "
            "input is idempotent (nothing is duplicated) and re-authoring changed "
            "input updates what you send, but nothing is ever removed. Dropping a "
            "device from the input does NOT delete it — the node and its edges stay "
            "in the graph. Removal is W17; until then this cannot express a deletion."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {
                    "type": "string",
                    "description": "Caller namespace UUID.",
                },
                "design_id": {
                    "type": "string",
                    "description": "Design identifier (the DESIGN node's id).",
                },
                "devices": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
                    "description": (
                        "Device dicts: device_ref, optional capability (AVIXA Revit "
                        "params; its 'extra' object carries the reserved copper.* "
                        "component-class keys verbatim, unvalidated), optional ports "
                        "(port_ref + capability + optional geometry), optional "
                        "rack_ref, optional geometry. GEOMETRY is an object whose members are "
                        "exactly: x, y (CANVAS GRID UNITS, origin top-left, y-DOWN - not "
                        "metres, and NCE converts nothing), rack_position, rack_face, "
                        "cable_length_m, cable_type and meta. ANY OTHER MEMBER IS REJECTED "
                        "(422) and fails the whole write - a near-miss like 'rackPosition' is "
                        "refused rather than silently dropped; put anything outside that list "
                        "inside meta, including room dimensions under copper.room.w / "
                        "copper.room.d / copper.room.h, in METRES. Numbers must be JSON "
                        "numbers: strings are rejected (\"12.5\" is NOT accepted), booleans "
                        "are rejected, and NaN / Infinity / any magnitude above the largest "
                        "finite IEEE double are rejected because they cannot survive the JSON "
                        "round trip (the bound is that double exactly, which is slightly "
                        "LARGER than the familiar 17-digit 1.797693134862316e308 form of it, "
                        "so do not treat that shorthand as the limit). rack_position must be "
                        "a multiple of 0.5 (a whole or half rack unit) between -999.5 and "
                        "999.5 - 1.27 is refused, not rounded. rack_face is 'front' or 'rear' "
                        "(NetBox vocabulary, contractual). meta must be a JSON object and "
                        "must not contain NaN or Infinity at any depth. Omitted members keep "
                        "their stored value; meta is replaced wholesale; a KNOWN member may "
                        "be null and is then treated as not supplied, so an object whose "
                        "every member is null counts as no geometry at all - but null does "
                        "NOT excuse an unknown member: {'rackPosition': null} is rejected "
                        "exactly like {'rackPosition': 3}. Omitting geometry entirely leaves "
                        "the node's existing geometry untouched - this cannot express 'no "
                        "geometry any more'. [M6.W16] A device may also carry status, "
                        "revision and salience. status is NetBox's DEVICE vocabulary and "
                        "nothing else: planned | staged | active | offline | decommissioning "
                        "| inventory | failed (a CABLE's or a RACK's vocabulary is REJECTED "
                        "per node type, as invalid params). LIFECYCLE STATE IS RECORDED ONLY "
                        "FOR A DEVICE THIS CALL CREATES, OR ONE YOU SEND A KEY FOR: re- "
                        "authoring an existing device without these keys - an ordinary canvas "
                        "save, or moving it on the canvas - records NOTHING and leaves it "
                        "with no lifecycle at all. That is deliberate: it is what keeps "
                        "already-installed equipment out of retirement flows. A device this "
                        "call creates with no status is stored as 'planned'; sending only "
                        "revision on an existing device stores the revision and leaves its "
                        "status undeclared. This tool performs no transition, so it cannot "
                        "promote planned to active. An explicit null says nothing and is read "
                        "as absent. revision is free text stored verbatim and interpreted by "
                        "the caller. salience must be a FINITE, NON-NEGATIVE number - NaN and "
                        "Infinity are rejected, because a stored NaN would sort above every "
                        "real salience. A device's PORTS take NONE of these three: a port has "
                        "no lifecycle status, and sending one on a port - in any casing, with "
                        "or without the cable_ prefix, at the top level or inside its "
                        "capability or geometry object - is REJECTED (invalid params) rather "
                        "than silently dropped. The same applies to a device: putting status, "
                        "revision or salience INSIDE capability or geometry is REJECTED, "
                        "because those objects store only their own fields and the lifecycle "
                        "key would be dropped."
                    ),
                },
                "connections": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
                    "description": (
                        "Optional port-to-port signal connections: from_device_ref, "
                        "from_port_ref, to_device_ref, to_port_ref, optional "
                        "confidence (0-1), cable_ref, and cable_geometry. "
                        "cable_geometry (the SAME object and the same rules as a device's "
                        "geometry - see the devices parameter, including the rejection of "
                        "unknown members - with cable_length_m and cable_type the useful "
                        "ones) is written to the CABLE NODE and is ignored unless cable_ref "
                        "is also given: it is not the edge's own layout, which is why it is "
                        "not called 'geometry'. [M6.W16] cable_status, cable_revision and "
                        "cable_salience follow the same rule: they describe the CABLE NODE, "
                        "not the edge. Sending them WITHOUT a cable_ref is REJECTED rather "
                        "than ignored - there is no node for them to describe - and so is "
                        "sending the unprefixed status / revision / salience on a connection, "
                        "in any casing. cable_status is NetBox's CABLE vocabulary and nothing "
                        "else: planned | connected | decommissioning. As with devices, state "
                        "is recorded only for a cable this call creates or one you send a key "
                        "for, so re-authoring existing cable records nothing."
                    ),
                },
                "racks": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
                    "description": (
                        "Optional rack dicts: rack_ref + optional capability + "
                        "optional geometry - the SAME object and the same rules as a device's "
                        "geometry, unknown-member rejection included. [M6.W16] A rack may "
                        "also carry status, revision and salience, under the same rules as a "
                        "device's: recorded only for a rack this call creates or one you send "
                        "a key for. status is NetBox's RACK vocabulary and nothing else: "
                        "reserved | available | planned | active | deprecated."
                    ),
                },
                "source_id": {
                    "type": "string",
                    "description": "Optional System Design source record id (retirement tracking).",
                },
                "actor": {
                    "type": "string",
                    "description": (
                        "Optional UPN of the human on whose behalf this write is made. "
                        "The API key authenticates the calling service; this attributes "
                        "the person. Omit it and no actor is recorded — it is never "
                        "invented or defaulted to a service identity."
                    ),
                },
                "expected_version": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "Optional optimistic-concurrency token: the 'version' you got "
                        "from system_design_get_topology. Supply it and the write is a "
                        "compare-and-swap - if the design has moved on you get a "
                        "DISTINCT conflict error (JSON-RPC code -32040, reason "
                        "'version_conflict', carrying expected_version and "
                        "actual_version) and NOTHING is written; re-read and retry. "
                        "Omit it and the write is last-writer-wins. Either way THIS TOOL "
                        "increments the design's version and returns the new value as "
                        "'version'. That covers writes made through this tool and "
                        "system_design_author_functional_location, and only those: "
                        "three other System Design modules (from_quote, to_quote, "
                        "netbox_bridge) write design nodes and edges without moving "
                        "the token. All three are unwired today, but do not assume the "
                        "token covers every possible change to a design. "
                        "0 means the design has never been authored."
                    ),
                },
            },
            "required": ["namespace_id", "design_id", "devices"],
        },
    ),
    Tool(
        name="system_design_author_functional_location",
        description=(
            "[M6.W13b] Author a System Design DESIGN plus its customer-site "
            "SITE > BUILDING > FLOOR > ROOM > POSITION functional-location tree and "
            "any DESIGN_LINE nodes. ADDITIVE ONLY: re-authoring the same input is "
            "idempotent, but omitting a building, floor, room, position or design "
            "line does NOT remove it — the node and its edges stay in the graph. "
            "Removal is W17; until then this cannot express a deletion."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {
                    "type": "string",
                    "description": "Caller namespace UUID.",
                },
                "namespace_slug": {
                    "type": "string",
                    "description": (
                        "Human-readable namespace slug. It is the deterministic prefix "
                        "of every FL: label, so the same slug must be used every time "
                        "for a site or a second parallel tree is authored."
                    ),
                },
                "design_id": {
                    "type": "string",
                    "description": "Design identifier (the DESIGN node's id).",
                },
                "site_name": {
                    "type": "string",
                    "description": "Top-level site name (root of the hierarchy).",
                },
                "buildings": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
                    "description": (
                        "Building dicts: name + optional geometry + optional floors, "
                        "each with name + optional geometry + optional rooms, each "
                        "with name + optional geometry + optional positions. "
                        "positions are bare strings and therefore cannot carry "
                        "geometry. "
                        "GEOMETRY is an object whose members are exactly: x, y "
                        "(CANVAS GRID UNITS, origin top-left, y-DOWN - not metres, and "
                        "NCE converts nothing), rack_position, rack_face, "
                        "cable_length_m, cable_type and meta. ANY OTHER MEMBER IS "
                        "REJECTED (422) and fails the whole write - a near-miss like "
                        "'rackPosition' is refused rather than silently dropped; put "
                        "anything outside that list inside meta, including room "
                        "dimensions under copper.room.w / copper.room.d / "
                        "copper.room.h, in METRES. Numbers must be JSON numbers: "
                        "strings are rejected (\"12.5\" is NOT accepted), booleans are "
                        "rejected, and NaN / Infinity / any magnitude above the "
                        "largest finite IEEE double are rejected because they cannot "
                        "survive the JSON round trip (the bound is that double exactly, "
                        "which is slightly LARGER than the familiar 17-digit "
                        "1.797693134862316e308 form of it, so do not treat that "
                        "shorthand as the limit). rack_position must be a "
                        "multiple of 0.5 (a whole or half rack unit) between -999.5 "
                        "and 999.5 - 1.27 is refused, not rounded. rack_face is "
                        "'front' or 'rear' (NetBox vocabulary, contractual). meta "
                        "must be a JSON object and must not contain NaN or Infinity "
                        "at any depth. Omitted members keep their stored value; meta "
                        "is replaced wholesale; a KNOWN member may be null and is "
                        "then treated as not supplied, so an object whose every member "
                        "is null counts as no geometry at all - but null does NOT excuse "
                        "an unknown member: {'rackPosition': null} is rejected exactly "
                        "like {'rackPosition': 3}. Omitting geometry entirely "
                        "leaves the node's existing geometry untouched - this cannot "
                        "express 'no geometry any more'."
                    ),
                },
                "design_lines": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
                    "description": (
                        "Optional DESIGN_LINE dicts: line_ref, manufacturer, "
                        "mfr_part_no, optional confidence (0-1) and source_id."
                    ),
                },
                "source_id": {
                    "type": "string",
                    "description": "Optional System Design source record id (retirement tracking).",
                },
                "actor": {
                    "type": "string",
                    "description": (
                        "Optional UPN of the human on whose behalf this write is made. "
                        "The API key authenticates the calling service; this attributes "
                        "the person. Omit it and no actor is recorded — it is never "
                        "invented or defaulted to a service identity."
                    ),
                },
                "expected_version": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "Optional optimistic-concurrency token: the 'version' you got "
                        "from system_design_get_topology. Supply it and the write is a "
                        "compare-and-swap - if the design has moved on you get a "
                        "DISTINCT conflict error (JSON-RPC code -32040, reason "
                        "'version_conflict', carrying expected_version and "
                        "actual_version) and NOTHING is written; re-read and retry. "
                        "Omit it and the write is last-writer-wins. Either way THIS TOOL "
                        "increments the design's version and returns the new value as "
                        "'version'. That covers writes made through this tool and "
                        "system_design_author_functional_location, and only those: "
                        "three other System Design modules (from_quote, to_quote, "
                        "netbox_bridge) write design nodes and edges without moving "
                        "the token. All three are unwired today, but do not assume the "
                        "token covers every possible change to a design. "
                        "0 means the design has never been authored."
                    ),
                },
            },
            "required": [
                "namespace_id",
                "namespace_slug",
                "design_id",
                "site_name",
                "buildings",
            ],
        },
    ),
    Tool(
        name="system_design_validate_design_graph",
        description=(
            "[M6.W13c] Read-only: run the five System Design design-quality checks "
            "over a DESIGN's graph — signal-flow continuity, port/format "
            "compatibility, power/heat budget, SPOF redundancy, and AVIXA "
            "checkpoint conformance — and return {passed, reasons}. 'passed' is "
            "true only when every check passes. Two behaviours are deliberate and "
            "will not change: an unknown signal format does NOT fail the design, "
            "and the power/heat budget is INFORMATIONAL — it always contributes "
            "its totals to 'reasons' and never sets passed=false, because NCE "
            "holds no budget ceiling. So a non-empty 'reasons' does not by itself "
            "mean the design failed: read 'passed'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {
                    "type": "string",
                    "description": "Caller namespace UUID.",
                },
                "design_id": {
                    "type": "string",
                    "description": "Design identifier (the DESIGN node's id).",
                },
            },
            "required": ["namespace_id", "design_id"],
        },
    ),
    Tool(
        name="system_design_delete_planned",
        description=(
            "[M6.W17] SOFT-RETIRES BY DEFAULT - the name says 'delete' and the "
            "default behaviour does not delete. The name and the matching "
            "DELETE /api/system-design/planned route are Copper's pinned "
            "contract, so neither is renamed; read this line rather than the "
            "name. By default this sets each named node's lifecycle status to "
            "its node type's retired value ('decommissioning' for a DEVICE or a "
            "CABLE, 'deprecated' for a RACK - the NetBox vocabularies are "
            "disjoint and a RACK has no 'decommissioning') and floors its "
            "salience. NOTHING IS REMOVED. Pass permanent=true for a genuine "
            "transactional delete of the nodes, their edges, the PORT children "
            "of any DEVICE, and their capability / geometry / lifecycle rows - "
            "that path additionally REQUIRES 'actor'. Only nodes whose declared "
            "status is 'planned' can be touched: a node with no lifecycle state "
            "row, or one whose status is NULL, is DENIED, and no row is the "
            "normal state of everything authored before W16. Retiring 'active' "
            "equipment is OUT OF SCOPE and cannot be expressed here. ONE DENIED "
            "NODE DENIES THE WHOLE CALL and nothing is changed."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {
                    "type": "string",
                    "description": "Caller namespace UUID.",
                },
                "design_id": {
                    "type": "string",
                    "description": (
                        "Design identifier (the DESIGN node's id). Every label in "
                        "node_labels must belong to it."
                    ),
                },
                "node_labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Canonical node labels to retire, exactly as returned by "
                        "system_design_get_topology. DEVICE / RACK / CABLE only - a "
                        "PORT label is refused, because NetBox has no lifecycle "
                        "status for a port and one cannot be declared planned; ports "
                        "are removed with their device on the permanent path. "
                        "Duplicates collapse. At least one is required."
                    ),
                },
                "permanent": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "false (default) soft-retires and removes nothing. true "
                        "PERMANENTLY DELETES and requires 'actor'. Must be a JSON "
                        "boolean: a string is refused rather than coerced, because "
                        "the string \"false\" is truthy and would delete."
                    ),
                },
                "actor": {
                    "type": "string",
                    "description": (
                        "The human's UPN. Optional on the default soft-retire path "
                        "and never invented when omitted (Rev 2 section 1), but "
                        "MANDATORY when permanent=true - an unattributable permanent "
                        "delete fails closed."
                    ),
                },
                "expected_version": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "Optimistic-concurrency token from system_design_get_topology. "
                        "Supply it and the retire is a compare-and-swap: a stale token "
                        "is refused with -32040 and nothing is changed. Omit it for "
                        "last-writer-wins. The design's version is incremented either "
                        "way, so a client polling the token sees that the retire "
                        "happened."
                    ),
                },
            },
            "required": ["namespace_id", "design_id", "node_labels"],
        },
    ),
    # -----------------------------------------------------------------
    # Inventory (Module 11). Registered in TOOL_REGISTRY since B138a but
    # advertised only from 2026-08-31: list_tools() returns TOOLS verbatim,
    # so the whole MCP half of this module was undiscoverable while being
    # callable. See tests/unit/test_mcp_tool_surface_ratchet.py.
    #
    # Advertised UNCONDITIONALLY on purpose. Inventory's gate is per-namespace
    # (metadata.inventory.enabled), enforced in the handler, which refuses with
    # McpError(-32005) + data.reason. A config flag would hide the tool from
    # namespaces that ARE entitled to it.
    #
    # qty is NUMERIC(18,3) and cost figures are money: the schema accepts a
    # string so an exact decimal survives, because coercing through float is
    # forbidden (money-module briefing #2; see inventory/stock.py).
    # -----------------------------------------------------------------
    Tool(
        name="inventory_stock_levels",
        description=(
            "Live per-SKU-per-location stock. Watcher; read-only, cacheable. "
            "Reads the authoritative inventory_items row, never the graph mirror."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "sku": {"type": "string", "description": "Optional; filter to one SKU."},
                "location": {
                    "type": "string",
                    "description": "Optional; filter to one location.",
                },
            },
            "required": ["namespace_id"],
        },
    ),
    Tool(
        name="inventory_transfer_stock",
        description=(
            "Warehouse<->van / van<->van stock move. Actor; mutation, admin_only. "
            "Physical stock moves between two locations."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "sku": {"type": "string", "description": "SKU being moved."},
                "qty": {
                    "type": ["string", "number"],
                    "description": (
                        "Quantity, NUMERIC(18,3). Send a string to keep the exact decimal."
                    ),
                },
                "from_location": {"type": "string", "description": "Source location."},
                "to_location": {"type": "string", "description": "Destination location."},
            },
            "required": ["namespace_id", "sku", "qty", "from_location", "to_location"],
        },
    ),
    Tool(
        name="inventory_record_consumption",
        description=(
            "Pick/use stock for a job. Actor; mutation, admin_only. Decrements on-hand stock."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "sku": {"type": "string", "description": "SKU consumed."},
                "qty": {
                    "type": ["string", "number"],
                    "description": (
                        "Quantity, NUMERIC(18,3). Send a string to keep the exact decimal."
                    ),
                },
                "location": {"type": "string", "description": "Location consumed from."},
                "work_order": {
                    "type": "string",
                    "description": "Optional; echoed back for traceability only.",
                },
            },
            "required": ["namespace_id", "sku", "qty", "location"],
        },
    ),
    Tool(
        name="inventory_record_goods_receipt",
        description=(
            "Record one inbound delivery. Actor; mutation, admin_only. Use this "
            "when no invoice is in hand."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "po_ref": {"type": "string", "description": "Purchase-order reference."},
                "location_id": {"type": "string", "description": "Receiving location UUID."},
                "lines": {"type": "array", "description": "Received lines."},
                "delivery_note_ref": {
                    "type": "string",
                    "description": "Optional delivery-note reference.",
                },
                "scans": {"type": "array", "description": "Optional scan records."},
            },
            "required": ["namespace_id", "po_ref", "location_id", "lines"],
        },
    ),
    Tool(
        name="inventory_recommend_restock",
        description="Per-SKU restock recommendations. Watcher; read-only, cacheable.",
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "location": {
                    "type": "string",
                    "description": "Optional; filter to one location.",
                },
                "sku": {"type": "string", "description": "Optional; filter to one SKU."},
            },
            "required": ["namespace_id"],
        },
    ),
    Tool(
        name="inventory_forecast_demand",
        description="Pipeline-weighted demand forecast. Watcher; read-only, cacheable.",
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "horizon_days": {
                    "type": "integer",
                    "description": "Optional; filters which pipeline lines count.",
                },
                "sku": {"type": "string", "description": "Optional; filter to one SKU."},
            },
            "required": ["namespace_id"],
        },
    ),
    Tool(
        name="inventory_reserve_stock",
        description=(
            "Reserve available stock for a project. Actor; mutation, admin_only. "
            "Increments qty_reserved only; no physical stock moves. Refuses with "
            "reason=insufficient_available when the request exceeds availability."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "sku": {"type": "string", "description": "SKU to reserve."},
                "qty": {
                    "type": ["string", "number"],
                    "description": (
                        "Quantity, NUMERIC(18,3). Send a string to keep the exact decimal."
                    ),
                },
                "location": {"type": "string", "description": "Location holding the stock."},
                "project_id": {
                    "type": "string",
                    "description": "Project the reservation is for.",
                },
            },
            "required": ["namespace_id", "sku", "qty", "location", "project_id"],
        },
    ),
    Tool(
        name="inventory_release_stock",
        description=(
            "Release previously-reserved stock. Actor; mutation, admin_only. "
            "Decrements qty_reserved only. Refuses with reason=over_release when "
            "releasing more than is currently reserved."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "sku": {"type": "string", "description": "SKU to release."},
                "qty": {
                    "type": ["string", "number"],
                    "description": (
                        "Quantity, NUMERIC(18,3). Send a string to keep the exact decimal."
                    ),
                },
                "location": {
                    "type": "string",
                    "description": "Location holding the reservation.",
                },
                "project_id": {
                    "type": "string",
                    "description": "Project the reservation belongs to.",
                },
            },
            "required": ["namespace_id", "sku", "qty", "location", "project_id"],
        },
    ),
    Tool(
        name="inventory_record_rma",
        description=(
            "Record a return with its WEEE state. Actor; mutation, admin_only. "
            "Moves NO stock: stock_movement_state is written 'pending'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "rma_ref": {
                    "type": "string",
                    "description": "RMA reference; unique per namespace.",
                },
                "sku": {"type": "string", "description": "Returned SKU."},
                "location": {
                    "type": "string",
                    "description": "Location the return is booked to.",
                },
                "qty": {
                    "type": ["string", "number"],
                    "description": (
                        "Quantity, NUMERIC(18,3). Send a string to keep the exact decimal."
                    ),
                },
                "reason": {"type": "string", "description": "Why the item was returned."},
                "serial": {"type": "string", "description": "Optional serial number."},
                "weee_state": {"type": "string", "description": "Optional WEEE scope state."},
                "disposal_ref": {
                    "type": "string",
                    "description": "Optional disposal reference.",
                },
            },
            "required": ["namespace_id", "rma_ref", "sku", "location", "qty", "reason"],
        },
    ),
    Tool(
        name="inventory_valuation",
        description=(
            "FIFO/average-cost money value of stock. Watcher; read-only but "
            "admin_only, and NOT cacheable: it is derived from the append-only "
            "inventory_transactions ledger and changes on every movement, and a "
            "stale money figure is a wrong number in someone's accounts."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "sku": {"type": "string", "description": "SKU to value."},
                "location": {"type": "string", "description": "Location to value."},
            },
            "required": ["namespace_id", "sku", "location"],
        },
    ),
    Tool(
        name="inventory_record_goods_receipt_and_match",
        description=(
            "Goods receipt plus three-way match. Actor; mutation, admin_only. "
            "Everything inventory_record_goods_receipt accepts, PLUS the po and "
            "invoice legs Inventory does not own. Not a wrapper: a caller with no "
            "invoice yet must use the plain tool."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "po_ref": {"type": "string", "description": "Purchase-order reference."},
                "location_id": {"type": "string", "description": "Receiving location UUID."},
                "lines": {"type": "array", "description": "Received lines."},
                "po": {"type": "object", "description": "Purchase-order leg."},
                "invoice": {"type": "object", "description": "Invoice leg."},
                "delivery_note_ref": {
                    "type": "string",
                    "description": "Optional delivery-note reference.",
                },
                "scans": {"type": "array", "description": "Optional scan records."},
            },
            "required": ["namespace_id", "po_ref", "location_id", "lines", "po", "invoice"],
        },
    ),
    Tool(
        name="inventory_reconcile_dead_stock",
        description=(
            "Reconcile dead (sku, location) pairs against the transaction ledger. "
            "admin_only, mutation=False: the core writes nothing at all, on the "
            "clean path and the raising path alike. Refuses with "
            "reason=ledger_divergence when inventory_items disagrees with the ledger."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "dead_stock_days": {
                    "type": "integer",
                    "description": "Optional; days of no movement that count as dead.",
                },
            },
            "required": ["namespace_id"],
        },
    ),
    Tool(
        name="inventory_restock_from_rma",
        description=(
            "Return a repairable unit to stock. Actor; mutation, admin_only. sku, "
            "location_id and qty are read from the claimed inventory_rma row, never "
            "from the caller."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "rma_ref": {"type": "string", "description": "RMA reference to claim."},
            },
            "required": ["namespace_id", "rma_ref"],
        },
    ),
    Tool(
        name="inventory_dispose_rma_weee",
        description=(
            "WEEE take-back disposal leg. Actor; mutation, admin_only. Stock leaves "
            "and the ledger row is what proves it happened."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "rma_ref": {"type": "string", "description": "RMA reference to claim."},
                "disposal_ref": {
                    "type": "string",
                    "description": "Disposal reference documenting the take-back.",
                },
            },
            "required": ["namespace_id", "rma_ref", "disposal_ref"],
        },
    ),
    # -----------------------------------------------------------------
    # OQ-3 tranche 2 (2026-09-01) — the six registered tools whose argument
    # contract is stated EXPLICITLY in their handler docstring, so nothing
    # here is inferred. Derived from the docstring's "Requires ..." sentence
    # cross-checked against the REST route that calls the same core.
    #
    # The eight other REST-derivable tools (economy_*, procurement_*,
    # product_get, product_search) are deliberately NOT here: their handlers
    # are thin pass-throughs with no requiredness signal, so the required/
    # optional split lives in the cores and would have to be guessed. Marking
    # a field optional when the core needs it produces a confusing server
    # error for a client that trusted the schema. See
    # tests/unit/test_mcp_tool_surface_ratchet.py.
    # -----------------------------------------------------------------
    Tool(
        name="assets_get",
        description=("Fetch one asset register row. Watcher; read-only, cacheable."),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "asset_id": {"type": "string", "description": "Asset identifier."},
            },
            "required": ["namespace_id", "asset_id"],
        },
    ),
    Tool(
        name="assets_list",
        description=(
            "List asset register rows. Watcher; read-only, cacheable. Optionally "
            "filters by functional location and/or lifecycle state."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "functional_location_id": {
                    "type": "string",
                    "description": "Optional; filter to one functional location.",
                },
                "lifecycle_state": {
                    "type": "string",
                    "description": "Optional; filter to one of the 14 lifecycle states.",
                },
            },
            "required": ["namespace_id"],
        },
    ),
    Tool(
        name="assets_advance_lifecycle",
        description=("Advance an asset through the 14-state lifecycle. Actor; mutation."),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "asset_id": {"type": "string", "description": "Asset to advance."},
                "target_state": {
                    "type": "string",
                    "description": "Destination lifecycle state.",
                },
            },
            "required": ["namespace_id", "asset_id", "target_state"],
        },
    ),
    Tool(
        name="vendors_get_vendor",
        description="Fetch a single vendor. Watcher; read-only, cacheable.",
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "vendor_id": {"type": "string", "description": "Vendor identifier."},
            },
            "required": ["namespace_id", "vendor_id"],
        },
    ),
    Tool(
        name="project_advance_phase",
        description=(
            "Phase-transition Actor: advance a project to a target phase. "
            "admin_only; mutation. Reads the current phase, evaluates the gate, "
            "and records the transition against the named actor."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "project_id": {"type": "string", "description": "Project to advance."},
                "target_phase": {"type": "string", "description": "Destination phase."},
                "actor": {
                    "type": "string",
                    "description": (
                        "The human this transition is attributed to. The API key "
                        "authenticates the service; actor attributes the person."
                    ),
                },
                "criteria_met": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                    "description": "Optional; gate criteria the caller asserts are met.",
                },
            },
            "required": ["namespace_id", "project_id", "target_phase", "actor"],
        },
    ),
    Tool(
        name="project_convert_signed_quote",
        description=(
            "Sales->Project bridge: materialise a project from a signed quote. "
            "admin_only; mutation. Reads the Sales-frozen baseline over the A2A "
            "seam, then creates the PROJECT, its G0 gate and its tasks."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "quote_id": {"type": "string", "description": "Signed quote to convert."},
                "signed_by": {
                    "type": "string",
                    "description": "Who signed the quote.",
                },
                "signature_ref": {
                    "type": "string",
                    "description": "Reference to the signature evidence.",
                },
            },
            "required": ["namespace_id", "quote_id", "signed_by", "signature_ref"],
        },
    ),
    # -----------------------------------------------------------------
    # OQ-3 tranche 3 (2026-09-01) — the two tools whose CORE raises an
    # explicit "'x' is required", so the required/optional split is read
    # from a raise rather than inferred:
    #   do_get_product      raise ValueError("'mfr_part_no' is required")
    #   do_search_products  raise ValueError("'query' is required and must
    #                                        be a non-empty string")
    # `limit`'s default (20) and cap (50) are likewise taken from the core,
    # not guessed. See tests/unit/test_mcp_tool_surface_ratchet.py for why
    # the other six REST-derivable tools are still not authored.
    # -----------------------------------------------------------------
    Tool(
        name="product_get",
        description=(
            "Fetch one product with live prices and graph edges. Watcher; read-only, cacheable."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "mfr_part_no": {
                    "type": "string",
                    "description": "Manufacturer part number identifying the product.",
                },
                "manufacturer": {
                    "type": "string",
                    "description": (
                        "Optional; disambiguates when the same part number exists "
                        "for more than one manufacturer."
                    ),
                },
            },
            "required": ["namespace_id", "mfr_part_no"],
        },
    ),
    Tool(
        name="product_search",
        description=("Keyword search over the product catalog. Watcher; read-only, cacheable."),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "query": {
                    "type": "string",
                    "description": "Search terms; must be a non-empty string.",
                },
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Optional; rows to return. Defaults to 20, capped at 50.",
                },
            },
            "required": ["namespace_id", "query"],
        },
    ),
    Tool(
        name="product_match_bom_line",
        description=(
            "Resolve a free-text BOM line to the best catalog SKU, ranked by the C1 "
            "resolve() primitive. Read-only in the match branch; when 'decision' is "
            "supplied it records accept/override feedback instead of returning matches."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "bom_line": {
                    "type": "string",
                    "description": "Raw free-text BOM line to resolve.",
                },
                "manufacturer": {
                    "type": "string",
                    "description": "Optional; parsed manufacturer hint.",
                },
                "mfr_part_no": {
                    "type": "string",
                    "description": "Optional; parsed part-number hint.",
                },
                "decision": {
                    "type": "string",
                    "enum": ["accept", "override"],
                    "description": (
                        "Optional; switches the call to the FEEDBACK branch, which "
                        "records the decision and returns a feedback_id instead of matches."
                    ),
                },
                "chosen_sku": {
                    "type": "string",
                    "description": "Feedback branch; the SKU the user accepted or chose.",
                },
                "rejected_sku": {
                    "type": "string",
                    "description": "Feedback branch; the SKU the user rejected.",
                },
                "matched_score": {
                    "type": "number",
                    "description": "Feedback branch; the score at decision time.",
                },
            },
            "required": ["namespace_id", "bom_line"],
        },
    ),
    Tool(
        name="product_price",
        description=(
            "Resolve the sales price for a (product, customer) pair via the C6 shared "
            "pricing service. Read-only; returns the winning tier (bid > supplier list "
            "> base) and a staleness signal."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "product": {
                    "type": "object",
                    "description": (
                        "Product row. Optional price keys: supplier_list_price, "
                        "supplier_list_as_of, base_price, base_as_of."
                    ),
                },
                "customer": {
                    "type": "object",
                    "default": {},
                    "description": (
                        "Optional customer row. Optional BID keys: bid_price, bid_as_of."
                    ),
                },
            },
            "required": ["namespace_id", "product"],
        },
    ),
    Tool(
        name="product_related",
        description=(
            "Derive and persist related-product graph edges (accessory_of, warranty_for, "
            "mounts, replaced_by) for one subject product. Mutating: writes kg_edges rows "
            "with confidence on the edge."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Tenant namespace UUID."},
                "mfr_part_no": {
                    "type": "string",
                    "description": "Part number of the subject product.",
                },
                "manufacturer": {
                    "type": "string",
                    "description": "Manufacturer of the subject product.",
                },
            },
            "required": ["namespace_id", "mfr_part_no", "manufacturer"],
        },
    ),
    Tool(
        name="product_enrich",
        description=(
            "On-demand enrichment of ONE product's missing fields. Behind the C2 governed "
            "gate: without confirm=true it returns {'status': 'pending_approval'} and "
            "writes nothing. With confirm=true it runs once, idempotent on replay."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "product_id": {
                    "type": "string",
                    "description": "UUID of exactly ONE product. Never a list.",
                },
                "trigger_context": {
                    "type": "object",
                    "description": (
                        "Provenance for the enrichment: "
                        "{kind, ref_id, missing_fields, source_watermark}."
                    ),
                },
                "confirm": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Optional; must be true for the side effect to run. Defaults to "
                        "false, which returns pending_approval."
                    ),
                },
                "idempotency_key": {
                    "type": "string",
                    "description": (
                        "Optional override. When absent a stable hash of "
                        "(product_id, missing_fields, source_watermark) is derived."
                    ),
                },
            },
            "required": ["namespace_id", "product_id", "trigger_context"],
        },
    ),
    Tool(
        name="resolve",
        description=(
            "RANK AND SCORE existing kg_nodes against a candidate. Read-only: this tool "
            "ranks only and never merges, writes or modifies any node or edge. Returns "
            "matches highest-score first, or an empty list."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Target namespace UUID."},
                "candidate": {
                    "type": "object",
                    "description": "Raw entity data to match against existing nodes.",
                },
                "keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Key names to compare when scoring.",
                },
                "node_type": {
                    "type": "string",
                    "description": "Entity type to filter candidates on.",
                },
            },
            "required": ["namespace_id", "candidate", "keys", "node_type"],
        },
    ),
    Tool(
        name="merge_queue_list",
        description=(
            "List all pending rows in entity_merge_queue for a namespace, oldest-first so "
            "reviewers work the backlog in arrival order. Read-only."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Target namespace UUID."},
            },
            "required": ["namespace_id"],
        },
    ),
    Tool(
        name="merge_queue_confirm",
        description=(
            "Mark one queue row as confirmed. NO-AUTO-MERGE: sets status only and never "
            "touches kg_nodes or kg_edges -- node survivorship is a later wave. Fails if "
            "the row is absent or not pending."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Target namespace UUID."},
                "queue_id": {
                    "type": "string",
                    "description": "UUID of the queue row to confirm.",
                },
                "decided_by": {
                    "type": "string",
                    "description": "Identifier of the human or agent deciding.",
                },
            },
            "required": ["namespace_id", "queue_id", "decided_by"],
        },
    ),
    Tool(
        name="merge_queue_reject",
        description=(
            "Mark one queue row as rejected. NO-AUTO-MERGE: sets status only and never "
            "touches kg_nodes or kg_edges. Fails if the row is absent or not pending."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Target namespace UUID."},
                "queue_id": {
                    "type": "string",
                    "description": "UUID of the queue row to reject.",
                },
                "decided_by": {
                    "type": "string",
                    "description": "Identifier of the human or agent deciding.",
                },
            },
            "required": ["namespace_id", "queue_id", "decided_by"],
        },
    ),
    Tool(
        name="procurement_calculate_tco",
        description=(
            "Total cost of ownership for one supplier against one BOM line. Read-only, advisory."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "supplier": {
                    "type": "object",
                    "description": "Supplier candidate; must contain unit_price (number).",
                },
                "bom_line": {
                    "type": "object",
                    "description": "BOM line; must contain quantity (integer).",
                },
            },
            "required": ["namespace_id", "supplier", "bom_line"],
        },
    ),
    Tool(
        name="procurement_evaluate_match",
        description=(
            "Three-way match across purchase order, goods receipt and invoice. "
            "Read-only; returns the discrepancies rather than resolving them."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "po": {
                    "type": "object",
                    "description": "Purchase order with article_id, quantity, unit_price.",
                },
                "goods_receipt": {
                    "type": "object",
                    "description": "Goods receipt with quantity.",
                },
                "invoice": {
                    "type": "object",
                    "description": "Invoice with article_id, quantity, unit_price.",
                },
            },
            "required": ["namespace_id", "po", "goods_receipt", "invoice"],
        },
    ),
    Tool(
        name="procurement_forecast_rebate",
        description=(
            "Forecast kickback/rebate attainment from current BOM spend against tier "
            "thresholds. Read-only, advisory."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "supplier_id": {
                    "type": "string",
                    "description": "Optional; restricts BOM rows and tiers to one supplier.",
                },
            },
            "required": ["namespace_id"],
        },
    ),
    Tool(
        name="procurement_rank_suppliers",
        description=(
            "Rank supplier candidates for one BOM line on the configured procurement "
            "weights. Read-only, advisory; does not place or modify any order."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "bom_line": {
                    "type": "object",
                    "description": "BOM line; must contain quantity (integer).",
                },
                "candidates": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Candidate suppliers; each must contain unit_price (number).",
                },
            },
            "required": ["namespace_id", "bom_line", "candidates"],
        },
    ),
    Tool(
        name="procurement_recommend_move_spend",
        description=(
            "Recommend where shifting spend would improve rebate attainment. "
            "Read-only, advisory -- it recommends and never moves anything."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
            },
            "required": ["namespace_id"],
        },
    ),
    Tool(
        name="procurement_whatif_spend",
        description=(
            "Model the effect of shifting a fraction of spend from one supplier to "
            "another. Read-only simulation; writes nothing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "from_supplier": {
                    "type": "string",
                    "description": "supplier_id to shift spend away from.",
                },
                "to_supplier": {
                    "type": "string",
                    "description": "supplier_id to shift spend toward.",
                },
                "shift_fraction": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "Fraction of current spend to shift, 0-1.",
                },
            },
            "required": [
                "namespace_id",
                "from_supplier",
                "to_supplier",
                "shift_fraction",
            ],
        },
    ),
    Tool(
        name="vendors_compute_scorecard",
        description="Compute the scorecard for one vendor. Read-only.",
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "vendor_id": {
                    "type": "string",
                    "description": "Vendor label, ID, or source ID.",
                },
            },
            "required": ["namespace_id", "vendor_id"],
        },
    ),
    Tool(
        name="vendors_get_tier_status",
        description="Current kickback-tier status for one vendor. Read-only.",
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "vendor_id": {
                    "type": "string",
                    "description": "Vendor label, ID, or source ID.",
                },
            },
            "required": ["namespace_id", "vendor_id"],
        },
    ),
    Tool(
        name="vendors_check_tier_at_risk",
        description=(
            "Check whether one vendor's kickback tier is at risk of being lost. Read-only."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "vendor_id": {
                    "type": "string",
                    "description": "Vendor label, ID, or source ID.",
                },
            },
            "required": ["namespace_id", "vendor_id"],
        },
    ),
    Tool(
        name="vendors_detect_reliability_degradation",
        description="Detect reliability degradation signals for one vendor. Read-only.",
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "vendor_id": {
                    "type": "string",
                    "description": "Vendor label, ID, or source ID.",
                },
            },
            "required": ["namespace_id", "vendor_id"],
        },
    ),
    Tool(
        name="vendors_reliability_radar",
        description=(
            "Namespace-wide supplier-risk and contractor-burnout signals. Read-only, advisory."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
            },
            "required": ["namespace_id"],
        },
    ),
    Tool(
        name="vendors_calibrate_weights",
        description=(
            "Recalibrate vendor scorecard weights from observed outcomes. Mutating: "
            "updates the stored weighting used by future scorecards."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
            },
            "required": ["namespace_id"],
        },
    ),
    Tool(
        name="vendors_match_contractor",
        description=(
            "Match and rank contractors for a job. Read-only. NOT available to "
            "contractor principals: an A2A contractor session is restricted to "
            "vendors_partner_view and is refused this skill."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "job": {
                    "type": "object",
                    "description": (
                        'Job specification, e.g. {"skills": ["dsp"], "location": "Oslo"}.'
                    ),
                },
            },
            "required": ["namespace_id", "job"],
        },
    ),
    Tool(
        name="vendors_compute_performance",
        description=(
            "Compute a contractor's performance score from work-order ratings and update "
            "contractor_profiles. Mutating. NOT available to contractor principals: an "
            "A2A contractor session is restricted to vendors_partner_view and is refused "
            "this skill."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "contractor_id": {
                    "type": "string",
                    "description": (
                        "Contractor identifier. A bare id is normalised to CONTRACTOR:<UPPERCASE>."
                    ),
                },
                "window": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional; rolling window in days for the rating average.",
                },
            },
            "required": ["namespace_id", "contractor_id"],
        },
    ),
    Tool(
        name="vendors_recall_similar_jobs",
        description="Recall contractor jobs similar to a query. Read-only.",
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "query": {
                    "type": "string",
                    "description": "Free-text description of the job to match against.",
                },
                "contractor_id": {
                    "type": "string",
                    "description": "Optional; restrict recall to one contractor.",
                },
                "limit": {
                    "type": "integer",
                    "default": 5,
                    "minimum": 1,
                    "description": "Optional; rows to return. Defaults to 5.",
                },
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional; candidate pool size before ranking.",
                },
            },
            "required": ["namespace_id", "query"],
        },
    ),
    Tool(
        name="pricing_resolve",
        description=(
            "Resolve pricing for a (product, customer) pair via the C6 shared pricing "
            "service. Read-only; returns the winning cost tier and a freshness signal."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "product": {
                    "type": "object",
                    "description": (
                        "Optional product data. Optional keys: supplier_list_price, "
                        "supplier_list_as_of, base_price, base_as_of."
                    ),
                },
                "customer": {
                    "type": "object",
                    "description": "Optional customer data. Optional keys: bid_price, bid_as_of.",
                },
            },
            "required": ["namespace_id"],
        },
    ),
    Tool(
        name="project_can_enter_phase",
        description=(
            "Phase-gate readiness check. A pure read: no DB, no HTTP, no side effects. "
            "Returns {ok, missing_criteria}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "project": {
                    "type": "object",
                    "description": "Project with current_phase and criteria_met.",
                },
                "target_phase": {
                    "type": "string",
                    "description": "The phase the caller wants to enter.",
                },
            },
            "required": ["namespace_id", "project", "target_phase"],
        },
    ),
    Tool(
        name="project_suggest_pl",
        description="Suggest a project lead for one project. Read-only, advisory.",
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "project_id": {"type": "string", "description": "Project identifier."},
            },
            "required": ["namespace_id", "project_id"],
        },
    ),
    Tool(
        name="agreements_lookup_terms",
        description=(
            "READ-ONLY agreement term lookup. Without a filter returns the 50 most "
            "recently flagged agreements. Every row carries review_status plus per-field "
            "confidence and review state -- unconfirmed rows are NOT silently filtered, "
            "so the caller judges trust."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "agreement_id": {
                    "type": "string",
                    "description": "Optional; return the single matching agreement.",
                },
                "supplier": {
                    "type": "string",
                    "description": "Optional; filter on the extracted supplierId term.",
                },
            },
            "required": ["namespace_id"],
        },
    ),
    Tool(
        name="sales_get_signed_baseline",
        description=(
            "Read the Sales-frozen SIGNED_BASELINE for one quote. Read-only, and the "
            "ONLY seam through which other engines may read it -- Sales owns and freezes "
            "it exactly once."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "quote_id": {
                    "type": "string",
                    "description": "The Sales QUOTE identifier.",
                },
            },
            "required": ["namespace_id", "quote_id"],
        },
    ),
    Tool(
        name="sales_get_quote_lines",
        description=(
            "Read every BOM_LINE on one Sales quote, ordered by line_ref. Read-only, "
            "namespace-scoped in SQL, and the seam System Design's from-quote design "
            "flow reads through. An unknown quote_id returns an empty list. Known "
            "limitation (D37): bom_line_content has no SKU, manufacturer, part-number "
            "or functional-location column, so those fields are absent -- callers "
            "apply their own defaults and nothing here fabricates one."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "quote_id": {
                    "type": "string",
                    "description": "The Sales QUOTE identifier.",
                },
            },
            "required": ["namespace_id", "quote_id"],
        },
    ),
    Tool(
        name="sales_add_quote_line",
        description=(
            "Add ONE manually picked line to a Sales quote. The manual-pick origination "
            "path for BOM_LINE: writes a single row through the sales-owned "
            "content:create:manual transition. Provenance is NOT caller-writable -- the "
            "stored origin_kind comes from the writer module's own mapping, so an "
            "origin_kind argument is never read. Idempotent on (quote_id, line_ref)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "quote_id": {
                    "type": "string",
                    "description": "The Sales QUOTE identifier.",
                },
                "line_ref": {
                    "type": "string",
                    "description": "Line reference, unique within the quote.",
                },
                "qty": {
                    "type": ["number", "string"],
                    "description": "Quantity; NUMERIC(18,3). Send a decimal string to stay exact.",
                },
                "unit_price": {
                    "type": ["number", "string"],
                    "description": "Unit price; NUMERIC. Send a decimal string to stay exact.",
                },
                "line_total": {
                    "type": ["number", "string"],
                    "description": "Optional; defaults to qty * unit_price.",
                },
                "currency": {
                    "type": "string",
                    "description": "Optional ISO-4217 code; defaults to NOK.",
                },
                "origin_ref": {
                    "type": "string",
                    "description": "Optional pointer to what the human picked.",
                },
            },
            "required": ["namespace_id", "quote_id", "line_ref", "qty", "unit_price"],
        },
    ),
    Tool(
        name="sales_ping",
        description=(
            'Liveness probe for the Sales vertical. Returns {"ok": true, "engine": "sales"}.'
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
            },
            "required": ["namespace_id"],
        },
    ),
    Tool(
        name="economy_match_invoice",
        description=(
            "READ-ONLY ADVISOR: invoice-match triage. Scores an invoice against candidate "
            "purchase records and returns the triage result; writes nothing. Thresholds "
            "always come from server config, never from caller arguments."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "invoice": {
                    "type": "object",
                    "description": "Invoice to triage; see matching.do_match_invoice for the shape.",
                },
                "candidates": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "Optional candidate pool. An absent or empty pool still scores "
                        "header and context components against a synthetic empty candidate."
                    ),
                },
            },
            "required": ["namespace_id", "invoice"],
        },
    ),
    Tool(
        name="economy_compute_periodisering",
        description=(
            "READ-ONLY ADVISOR: NGAAP bucket periodisering. Computes bucket targets and "
            "returns them; writes nothing. The chart of accounts and account mapping are "
            "always loaded server-side, never taken from caller arguments."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "params": {
                    "type": "object",
                    "description": (
                        "Computation input: buckets, project_id, period_end. See "
                        "ngaap.do_compute_bucket_targets for the exact shape."
                    ),
                },
            },
            "required": ["namespace_id", "params"],
        },
    ),
    Tool(
        name="economy_emit_event",
        description=(
            "READ-ONLY ADVISOR, DRY RUN: validates a financial event's balance and "
            "returns its normalised/hashed form. It NEVER persists anything. An "
            "unbalanced event returns a structured error and is never auto-balanced, "
            "repaired or re-ordered."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "event": {
                    "type": "object",
                    "description": (
                        "Financial event with type and optional postings. See "
                        "events.do_emit_financial_event for the exact shape."
                    ),
                },
            },
            "required": ["namespace_id", "event"],
        },
    ),
    Tool(
        name="detect_causal_cycles",
        description=(
            "[ADMIN] Detect cycles in the event_parents causal DAG for a namespace. "
            "Read-only: walks the DAG and reports cycles, never mutating it."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "depth_cap": {
                    "type": "integer",
                    "default": 50,
                    "minimum": 1,
                    "description": "Optional; traversal depth limit. Defaults to 50.",
                },
            },
            "required": ["namespace_id"],
        },
    ),
    Tool(
        name="system_design_from_quote",
        description=(
            "Realise a Sales QUOTE into a DESIGN proposal. MUTATING: lifts each quote "
            "line into a DESIGN plus one DESIGN_LINE, gap-fills missing "
            "accessories/infra/labour, and writes the cross-engine edge "
            "QUOTE -[realized_as]-> DESIGN."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "quote_id": {
                    "type": "string",
                    "description": "The Sales QUOTE identifier to realise.",
                },
                "design_id": {
                    "type": "string",
                    "description": "Optional; defaults to DESIGN-<quote_id>.",
                },
                "namespace_slug": {
                    "type": "string",
                    "description": "Optional; prefix for the functional-location label.",
                },
                "source_id": {
                    "type": "string",
                    "description": "Optional; system_design source id.",
                },
            },
            "required": ["namespace_id", "quote_id"],
        },
    ),
    Tool(
        name="system_design_to_quote",
        description=(
            "Derive quote lines back from a DESIGN. MUTATING: writes the cross-engine "
            "edge linking the design to the quote it produces. Sales still owns pricing "
            "and signing -- this returns the lines, it does not price or freeze them."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "design_id": {"type": "string", "description": "The DESIGN node id."},
                "source_id": {
                    "type": "string",
                    "description": "Optional; system_design source id.",
                },
            },
            "required": ["namespace_id", "design_id"],
        },
    ),
    Tool(
        name="system_design_generate_sow",
        description=(
            "Generate the statement of work for a DESIGN. Read-only. Freeze-on-issue: "
            "version_number is derived deterministically from the design state, so "
            "re-issuing against an unchanged design returns the same version."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "design_id": {"type": "string", "description": "The DESIGN node id."},
                "version_number": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Optional; overrides the derived version and marks the result frozen."
                    ),
                },
            },
            "required": ["namespace_id", "design_id"],
        },
    ),
    Tool(
        name="system_design_enrich_design_lines",
        description=(
            "Fire scoped Product enrichment and Procurement TCO for a design's lines. "
            "SIDE-EFFECTING: writes no graph rows itself but QUEUES enrichment work, "
            "once per unique referenced product."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "design_id": {
                    "type": "string",
                    "description": "The DESIGN whose lines should be enriched.",
                },
                "missing_fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": ["etim_specs"],
                    "description": 'Optional; fields to request. Defaults to ["etim_specs"].',
                },
            },
            "required": ["namespace_id", "design_id"],
        },
    ),
    Tool(
        name="system_design_propose_design",
        description=(
            "Recall-driven BOM proposal for a room brief. PROPOSE-ONLY: it never "
            "auto-accepts, freezes or applies a line -- every proposed line carries "
            "validated=false and must be confirmed before it is authored into the graph."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "room_brief": {
                    "type": "string",
                    "description": (
                        "Natural-language description of the room or design requirement "
                        "to match against past designs."
                    ),
                },
            },
            "required": ["namespace_id", "room_brief"],
        },
    ),
    # -----------------------------------------------------------------
    # Support vertical module tools (Module 10, Wave 5, ML10-B5)
    # -----------------------------------------------------------------
    Tool(
        name="support_query_ticket",
        description=(
            "Query a single support ticket by ID (including SLA clock) or list "
            "tickets with filters (status, priority, customer, room, asset). "
            "Watcher; read-only, cacheable."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "ticket_id": {
                    "type": "string",
                    "description": "Optional; ticket UUID to retrieve a single ticket.",
                },
                "status": {
                    "type": "string",
                    "description": "Optional; filter by status (open, in_progress, waiting_customer, waiting_parts, resolved, closed, cancelled).",
                },
                "priority": {
                    "type": "string",
                    "description": "Optional; filter by priority (low, medium, high, critical).",
                },
                "customer_id": {
                    "type": "string",
                    "description": "Optional; filter by customer ID.",
                },
                "room_id": {
                    "type": "string",
                    "description": "Optional; filter by room / functional location ID.",
                },
                "asset_id": {
                    "type": "string",
                    "description": "Optional; filter by asset UUID.",
                },
                "limit": {
                    "type": "integer",
                    "default": 50,
                    "minimum": 1,
                    "maximum": 200,
                    "description": "Optional; maximum number of tickets to return.",
                },
                "offset": {
                    "type": "integer",
                    "default": 0,
                    "minimum": 0,
                    "description": "Optional; pagination offset.",
                },
            },
            "required": ["namespace_id"],
        },
    ),
    Tool(
        name="support_open_ticket",
        description=(
            "Open a new native service ticket and initialize its running SLA clock. "
            "Actor / admin-only; mutation."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "summary": {
                    "type": "string",
                    "description": "Short summary description of the issue.",
                },
                "description": {
                    "type": "string",
                    "description": "Optional; detailed incident context.",
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                    "default": "medium",
                    "description": "Ticket priority level.",
                },
                "source": {
                    "type": "string",
                    "enum": ["nce", "d365"],
                    "default": "nce",
                    "description": "Originating source system.",
                },
                "source_id": {
                    "type": "string",
                    "description": "Optional; external system source identifier.",
                },
                "customer_id": {
                    "type": "string",
                    "description": "Optional; customer ID.",
                },
                "room_id": {
                    "type": "string",
                    "description": "Optional; functional location / room ID.",
                },
                "asset_id": {
                    "type": "string",
                    "description": "Optional; asset UUID.",
                },
                "sla_profile": {
                    "type": "string",
                    "enum": ["mission_critical", "standard", "basic", "best_effort"],
                    "default": "standard",
                    "description": "SLA profile governing target deadlines.",
                },
                "change_origin": {
                    "type": "string",
                    "enum": [
                        "sync",
                        "webhook",
                        "agent",
                        "operator",
                        "consolidation",
                        "replay",
                        "unknown",
                    ],
                    "default": "agent",
                    "description": "Origin of the ticket creation.",
                },
                "ai_diagnosis": {
                    "type": "object",
                    "description": "Optional; preliminary AI diagnostics or suggested fixes.",
                },
            },
            "required": ["namespace_id", "summary"],
        },
    ),
    Tool(
        name="support_sla_clock",
        description=(
            "Query and evaluate running SLA clock state for a support ticket. "
            "Watcher; read-only, cacheable."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "ticket_id": {
                    "type": "string",
                    "description": "Ticket UUID to inspect SLA clock for.",
                },
            },
            "required": ["namespace_id", "ticket_id"],
        },
    ),
    Tool(
        name="support_health_score",
        description=(
            "Compute and upsert customer health score and churn risk from passive support signals. "
            "Watcher; read-only, cacheable."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "customer_id": {
                    "type": "string",
                    "description": "Customer identifier.",
                },
                "lookback_days": {
                    "type": "integer",
                    "default": 30,
                    "minimum": 1,
                    "description": "Optional; lookback window in days.",
                },
            },
            "required": ["namespace_id", "customer_id"],
        },
    ),
    Tool(
        name="support_troubleshoot",
        description=(
            "AI Troubleshooter: cognitive recall over historical resolutions in "
            "v3_cognitive_ledger and memories with auditable citations. "
            "Watcher; read-only, cacheable."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "symptom_text": {
                    "type": "string",
                    "description": "Observed symptoms or problem description.",
                },
                "ticket_id": {
                    "type": "string",
                    "description": "Optional; existing ticket UUID to diagnose (extracts summary/description if symptom_text omitted).",
                },
                "asset_id": {
                    "type": "string",
                    "description": "Optional; asset UUID to prioritize asset-specific historical fixes.",
                },
                "limit": {
                    "type": "integer",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Optional; maximum citations to return.",
                },
                "min_confidence": {
                    "type": "number",
                    "default": 0.5,
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "Optional; minimum confidence score threshold.",
                },
            },
            "required": ["namespace_id"],
        },
    ),
    Tool(
        name="support_resolve_ticket",
        description=(
            "Resolve an open service ticket, record resolution in v3_cognitive_ledger, "
            "and update SLA clock. Actor / admin-only; mutation."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "ticket_id": {
                    "type": "string",
                    "description": "Ticket UUID to resolve.",
                },
                "resolution_text": {
                    "type": "string",
                    "description": "Detailed explanation of the resolution or fix.",
                },
                "was_fix": {
                    "type": "boolean",
                    "default": True,
                    "description": "Whether the action resolved the underlying issue.",
                },
                "resolution_category": {
                    "type": "string",
                    "default": "other",
                    "description": "Category slug for the resolution (hardware, firmware, configuration, network, user_error, other).",
                },
                "fixed_asset_id": {
                    "type": "string",
                    "description": "Optional; asset UUID that was repaired or replaced.",
                },
                "fixed_product_id": {
                    "type": "string",
                    "description": "Optional; product ID of replaced/repaired component.",
                },
                "resolved_by": {
                    "type": "string",
                    "default": "agent",
                    "description": "Operator or agent identity resolving the ticket.",
                },
            },
            "required": ["namespace_id", "ticket_id", "resolution_text"],
        },
    ),
    Tool(
        name="support_triage_ticket",
        description=(
            "Triage ticket priority, urgency, required engineering skill, and suggested routing. "
            "Advisor; read-only, cacheable."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "ticket_id": {
                    "type": "string",
                    "description": "Ticket UUID to triage.",
                },
            },
            "required": ["namespace_id", "ticket_id"],
        },
    ),
    Tool(
        name="support_record_touchpoint",
        description=(
            "Record an ÉT-spørsmål customer satisfaction touchpoint response and fold into "
            "rolling customer health. Actor; mutation."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "customer_id": {
                    "type": "string",
                    "description": "Customer ID associated with the touchpoint.",
                },
                "question_id": {
                    "type": "string",
                    "default": "et_sporsmal_v1",
                    "description": "Optional identifier for the touchpoint question.",
                },
                "answer": {
                    "description": "Customer response / answer content.",
                },
                "score": {
                    "type": "number",
                    "description": "Optional numeric sentiment or rating score.",
                },
            },
            "required": ["namespace_id", "customer_id", "answer"],
        },
    ),
    Tool(
        name="support_dispatch_work_order",
        description=(
            "Dispatch an open service ticket as a Field Tech work order (creates "
            "TICKET -[dispatched_as]-> WORK_ORDER boundary edge). "
            "Actor (Autonomous under threshold); mutation, admin-only."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "ticket_id": {
                    "type": "string",
                    "description": "Ticket UUID to dispatch as a work order.",
                },
                "estimated_cost": {
                    "type": "number",
                    "description": "Estimated dispatch cost evaluated against DISPATCH_CEILING. If omitted, confirm=True is required to dispatch autonomously.",
                },
                "confirm": {
                    "type": "boolean",
                    "default": False,
                    "description": "Human confirmation override for over-ceiling dispatch.",
                },
                "notes": {
                    "type": "string",
                    "description": "Optional dispatch notes for technician.",
                },
            },
            "required": ["namespace_id", "ticket_id"],
        },
    ),
    Tool(
        name="support_sync_now",
        description=(
            "Trigger an incremental D365 case sync and proactive telemetry sweep for Support. "
            "Actor / operator; mutation, admin-only."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "mode": {
                    "type": "string",
                    "enum": ["d365", "both", "nce"],
                    "default": "both",
                    "description": "Data source mode ('d365', 'both', or 'nce').",
                },
                "run_proactive_sweep": {
                    "type": "boolean",
                    "default": True,
                    "description": "Whether to run proactive telemetry sweep.",
                },
            },
            "required": ["namespace_id"],
        },
    ),
    # Field Tech vertical module tools (ML12-B5, M12.W5)
    Tool(
        name="field_tech_dispatch",
        description=(
            "AI dispatch advisor: rank candidate technicians for a work order by "
            "skill/certification, location, current load, and outcome history. Advisor; cacheable."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "work_order_id": {
                    "type": "string",
                    "description": "Work order business identifier.",
                },
                "candidates": {
                    "type": "array",
                    "description": "Optional explicit candidate technician pool.",
                    "items": {"type": "object"},
                },
                "required_skills": {
                    "type": "array",
                    "description": "Optional list of required skills/certifications.",
                    "items": {"type": "string"},
                },
            },
            "required": ["namespace_id", "work_order_id"],
        },
    ),
    Tool(
        name="field_tech_partner_view",
        description=(
            "Partner-scoped, field-redacted work order projection for external contractors. "
            "Enforces the Partner Access Model (Spec §46). Advisor; cacheable."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "partner_scope_id": {
                    "type": "string",
                    "description": "Contractor partner scope UUID.",
                },
                "work_order_id": {
                    "type": "string",
                    "description": "Optional specific work order ID.",
                },
            },
            "required": ["namespace_id", "partner_scope_id"],
        },
    ),
    Tool(
        name="field_tech_create_work_order",
        description=(
            "Create a work order for field installation or service visit. "
            "Actor / admin-only; mutation."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "work_order_id": {
                    "type": "string",
                    "description": "Work order business identifier.",
                },
                "kind": {
                    "type": "string",
                    "enum": ["install", "service"],
                    "description": "Work order kind: install or service.",
                },
                "source_kind": {
                    "type": "string",
                    "enum": ["project", "ticket", "manual"],
                    "description": "Originating source kind.",
                },
                "source_ref": {"type": "string", "description": "Originating reference ID."},
                "location_id": {"type": "string", "description": "Functional location / room ID."},
                "bom_lines": {
                    "type": "array",
                    "description": "BOM line IDs to be installed.",
                    "items": {"type": "string"},
                },
                "summary": {"type": "string", "description": "Summary description of work."},
                "priority": {"type": "string", "description": "Priority level."},
                "due_at": {"type": "string", "description": "ISO8601 deadline timestamp."},
                "raw": {"type": "object", "description": "Additional domain metadata."},
            },
            "required": ["namespace_id", "work_order_id", "kind"],
        },
    ),
    Tool(
        name="field_tech_assign",
        description=(
            "Assign a work order to an internal technician or external contractor. "
            "Actor / admin-only; mutation."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "work_order_id": {"type": "string", "description": "Work order identifier."},
                "assignee_id": {
                    "type": "string",
                    "description": "Technician or contractor identifier.",
                },
                "assignee_kind": {
                    "type": "string",
                    "enum": ["employee", "contractor"],
                    "default": "employee",
                    "description": "Assignee kind: employee or contractor.",
                },
                "partner_scope_id": {
                    "type": "string",
                    "description": "Partner scope UUID (required if contractor).",
                },
            },
            "required": ["namespace_id", "work_order_id", "assignee_id"],
        },
    ),
    Tool(
        name="field_tech_complete_checklist",
        description=(
            "Record checklist items as an ISO9001 quality verification record. Actor; mutation."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "work_order_id": {"type": "string", "description": "Work order identifier."},
                "checklist_id": {"type": "string", "description": "Checklist instance identifier."},
                "template_id": {
                    "type": "string",
                    "description": "Template ID from checklist-templates.json.",
                },
                "items": {
                    "type": "array",
                    "description": "Checklist item verification entries.",
                    "items": {"type": "object"},
                },
                "require_all_required": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, rejects completion when required items are unverified.",
                },
                "partner_scope_id": {
                    "type": "string",
                    "description": "Partner scope UUID if contractor.",
                },
            },
            "required": ["namespace_id", "work_order_id"],
        },
    ),
    Tool(
        name="field_tech_scan_serial",
        description=(
            "Scan equipment serial number at install/service, seeding the canonical "
            "BOM_LINE -[installed_as]-> ASSET edge for the Assets register. Actor; mutation."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "work_order_id": {"type": "string", "description": "Work order identifier."},
                "bom_line_id": {"type": "string", "description": "BOM line identifier."},
                "serial": {"type": "string", "description": "Scanned equipment serial number."},
                "product_id": {"type": "string", "description": "Optional product catalog ID."},
                "partner_scope_id": {
                    "type": "string",
                    "description": "Partner scope UUID if contractor.",
                },
            },
            "required": ["namespace_id", "work_order_id", "bom_line_id", "serial"],
        },
    ),
    Tool(
        name="field_tech_log_time",
        description=(
            "Log technician labor time (manual or GPS geofence) with op_id dedup. Actor; mutation."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "work_order_id": {"type": "string", "description": "Work order identifier."},
                "started_at": {"type": "string", "description": "ISO8601 start timestamp."},
                "ended_at": {"type": "string", "description": "ISO8601 end timestamp."},
                "hours": {
                    "type": "number",
                    "description": "Labor hours (alternative to explicit timestamps).",
                },
                "source": {
                    "type": "string",
                    "enum": ["manual", "gps"],
                    "default": "manual",
                    "description": "Tracking source.",
                },
                "op_id": {
                    "type": "string",
                    "description": "Client-generated idempotency operation ID.",
                },
                "approved": {
                    "type": "boolean",
                    "default": False,
                    "description": "Approval flag for billing.",
                },
                "partner_scope_id": {
                    "type": "string",
                    "description": "Partner scope UUID if contractor.",
                },
            },
            "required": ["namespace_id", "work_order_id"],
        },
    ),
    Tool(
        name="field_tech_attach_photo",
        description=("Attach photo documentation to a work order. Capture; mutation."),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "work_order_id": {"type": "string", "description": "Work order identifier."},
                "blob_ref": {
                    "type": "string",
                    "description": "Object store URI for the photo blob.",
                },
                "caption": {"type": "string", "description": "Optional descriptive caption."},
                "partner_scope_id": {
                    "type": "string",
                    "description": "Partner scope UUID if contractor.",
                },
            },
            "required": ["namespace_id", "work_order_id", "blob_ref"],
        },
    ),
    Tool(
        name="field_tech_sync",
        description=(
            "Reconcile offline client mutation batch with server-sequence ordering and conflict surfacing. "
            "Offline reconcile; mutation."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "device_id": {"type": "string", "description": "Client device identifier."},
                "ops": {
                    "type": "array",
                    "description": "Queued operation envelopes to replay.",
                    "items": {"type": "object"},
                },
                "partner_scope_id": {
                    "type": "string",
                    "description": "Partner scope UUID if contractor.",
                },
            },
            "required": ["namespace_id", "device_id", "ops"],
        },
    ),
    Tool(
        name="field_tech_record_outcome",
        description=(
            "Record work order completion quality rating and outcome in v3_cognitive_ledger "
            "tagged with field_tech_source_id. Ledger append / admin-only; mutation."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "work_order_id": {"type": "string", "description": "Work order identifier."},
                "rating": {
                    "type": "number",
                    "minimum": 1.0,
                    "maximum": 5.0,
                    "default": 5.0,
                    "description": "Customer or supervisor rating [1.0 - 5.0].",
                },
                "quality_score": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "default": 1.0,
                    "description": "Quality score [0.0 - 1.0].",
                },
                "resolution_notes": {
                    "type": "string",
                    "description": "Detailed completion or fix notes.",
                },
                "was_rework": {
                    "type": "boolean",
                    "default": False,
                    "description": "Whether visit was a rework.",
                },
                "completed_by": {
                    "type": "string",
                    "description": "Technician ID who executed work.",
                },
                "mark_completed": {
                    "type": "boolean",
                    "default": True,
                    "description": "Whether to mark WO completed.",
                },
            },
            "required": ["namespace_id", "work_order_id"],
        },
    ),
    # -----------------------------------------------------------------------
    # Module 13 — HR Engine (ML13-B3)
    # -----------------------------------------------------------------------
    Tool(
        name="hr_get_employee",
        description=(
            "Read-only: retrieve an employee profile card, skills matrix, "
            "active certifications, and leave balance (scoped by caller role)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "employee_id": {"type": "string", "description": "Unique employee identifier."},
                "caller_role": {
                    "type": "string",
                    "description": "Optional caller role (admin, manager, hr, peer).",
                    "default": "peer",
                },
                "caller_id": {
                    "type": "string",
                    "description": "Optional caller employee ID for self-lookup privileges.",
                },
            },
            "required": ["namespace_id", "employee_id"],
        },
    ),
    Tool(
        name="hr_match_skills",
        description=(
            "Read-only: match candidates against required skills and certifications with "
            "plain-language rationale. Enforces RL-1 NEVER ranking: returns requirement fit, not a leaderboard."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "required_skills": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of required technical skill names.",
                },
                "required_certs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of required certification names.",
                },
                "candidates": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Optional candidate list; when omitted queries active employees.",
                },
                "standing_ranking": {
                    "type": "boolean",
                    "description": "Prohibited by RL-1; passing true will cause an error.",
                },
                "leaderboard": {
                    "type": "boolean",
                    "description": "Prohibited by RL-1; passing true will cause an error.",
                },
            },
            "required": ["namespace_id"],
        },
    ),
    Tool(
        name="hr_capacity",
        description=(
            "Read-only: compute employee or team workload utilization over a forecast horizon "
            "from assigned work orders and approved absences."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "employee_id": {"type": "string", "description": "Optional employee ID filter."},
                "department": {"type": "string", "description": "Optional department filter."},
                "horizon_days": {
                    "type": "integer",
                    "default": 14,
                    "description": "Forecast window in calendar days [1 - 90].",
                },
            },
            "required": ["namespace_id"],
        },
    ),
    Tool(
        name="hr_cert_status",
        description=(
            "Read-only: check certification lifecycle status and impending expiration alerts "
            "across employees (Watcher surface)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "employee_id": {"type": "string", "description": "Optional employee ID filter."},
                "warn_days": {
                    "type": "integer",
                    "default": 90,
                    "description": "Warning horizon in days for impending expirations.",
                },
                "status": {
                    "type": "string",
                    "description": "Optional status filter ('active', 'expiring', 'expired', 'all').",
                    "default": "all",
                },
            },
            "required": ["namespace_id"],
        },
    ),
    Tool(
        name="hr_register_absence",
        description=(
            "Mutation: register or update an employee leave or absence event (Actor with confirmation)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "employee_id": {"type": "string", "description": "Target employee identifier."},
                "absence_type": {
                    "type": "string",
                    "description": "Type of absence: vacation, sick_leave, parental, training, compassionate, other.",
                },
                "start_date": {
                    "type": "string",
                    "description": "Start date in ISO format (YYYY-MM-DD).",
                },
                "end_date": {
                    "type": "string",
                    "description": "End date in ISO format (YYYY-MM-DD).",
                },
                "days": {"type": "number", "description": "Optional duration in days."},
                "reason": {"type": "string", "description": "Optional reason (confidential PII)."},
                "status": {
                    "type": "string",
                    "description": "Approval status.",
                    "default": "approved",
                },
                "absence_id": {"type": "string", "description": "Optional unique absence ID."},
                "hr_source_id": {
                    "type": "string",
                    "description": "Optional upstream source ID for GDPR hard retirement.",
                },
            },
            "required": ["namespace_id", "employee_id", "absence_type", "start_date", "end_date"],
        },
    ),
    Tool(
        name="hr_build_onboarding_quest",
        description=(
            "Mutation: generate or retrieve a structured 90-day onboarding checklist for an employee. Admin-only."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "employee_id": {"type": "string", "description": "Target employee identifier."},
                "role": {
                    "type": "string",
                    "description": "Role specialization.",
                    "default": "technician",
                },
                "department": {
                    "type": "string",
                    "description": "Department.",
                    "default": "operations",
                },
                "start_date": {"type": "string", "description": "Start date (YYYY-MM-DD)."},
            },
            "required": ["namespace_id", "employee_id"],
        },
    ),
    Tool(
        name="hr_log_one_on_one",
        description=(
            "Mutation: record a confidential 1-on-1 coaching or review note through the GDPR PII "
            "redaction gate into an agent-scoped memory. Admin-only."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "employee_id": {"type": "string", "description": "Target employee identifier."},
                "interviewer_id": {
                    "type": "string",
                    "description": "Manager or interviewer employee identifier.",
                },
                "notes": {
                    "type": "string",
                    "description": "Discussion notes (PII stripped via redaction gate).",
                },
                "action_items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of agreed follow-up tasks.",
                },
                "session_date": {"type": "string", "description": "Date of session (YYYY-MM-DD)."},
                "hr_source_id": {
                    "type": "string",
                    "description": "Optional upstream source ID for GDPR hard retirement.",
                },
            },
            "required": ["namespace_id", "employee_id", "interviewer_id", "notes"],
        },
    ),
    Tool(
        name="hr_coach",
        description=(
            "Read-only: individual skill advancement advisor recommending targeted training for skill gaps. "
            "Enforces RL-1: strictly individual; comparative ranking or leaderboards are prohibited."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "employee_id": {"type": "string", "description": "Target employee identifier."},
                "target_role": {
                    "type": "string",
                    "description": "Optional desired career target role.",
                },
                "focus_areas": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional technical domains (e.g. audio, video, control, network).",
                },
                "standing_ranking": {
                    "type": "boolean",
                    "description": "Prohibited by RL-1; passing true will cause an error.",
                },
                "compare_peers": {
                    "type": "boolean",
                    "description": "Prohibited by RL-1; passing true will cause an error.",
                },
            },
            "required": ["namespace_id", "employee_id"],
        },
    ),
    # -------------------------------------------------------------------
    # Module 14: Marketing Engine (ML14)
    # -------------------------------------------------------------------
    Tool(
        name="marketing_find_case_study_candidates",
        description=(
            "Find delivered projects scoring high on outcome metrics suitable for "
            "case studies. Surfaces verified graph evidence links for drafting."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "min_outcome_score": {
                    "type": "number",
                    "default": 7.5,
                    "description": "Minimum outcome score threshold (0.0 - 10.0).",
                },
                "lookback_days": {
                    "type": "integer",
                    "default": 180,
                    "description": "Lookback window in days for delivered projects.",
                },
            },
            "required": ["namespace_id"],
        },
    ),
    Tool(
        name="marketing_draft_case_study",
        description=(
            "Assemble a retrieval-grounded case study draft from verified graph facts (MK-2 & MK-3). "
            "Applies assembly-time redaction of financials and optional anonymization."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "project_id": {
                    "type": "string",
                    "description": "Delivered project UUID or identifier.",
                },
                "anonymize": {
                    "type": "boolean",
                    "default": True,
                    "description": "Mask customer and site identifying names by default.",
                },
                "room_type": {"type": "string", "description": "Room type categorization."},
                "vertical": {"type": "string", "description": "Industry vertical."},
            },
            "required": ["namespace_id", "project_id"],
        },
    ),
    Tool(
        name="marketing_request_testimonial",
        description=(
            "Issue a testimonial request for a customer, gated on high NPS >= 9.0 (MK-5). "
            "Refuses requests triggered on low customer health."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "customer_id": {"type": "string", "description": "Customer UUID."},
                "project_id": {"type": "string", "description": "Delivered project UUID."},
                "nps_score": {
                    "type": "number",
                    "description": "Customer NPS score (must be >= 9.0).",
                },
            },
            "required": ["namespace_id", "customer_id", "project_id"],
        },
    ),
    Tool(
        name="marketing_capture_testimonial",
        description=(
            "Record a customer quote with structured consent, duration, and tier (MK-4). "
            "Enforces web_retractable vs ai_citable_irrevocable consent."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "customer_id": {"type": "string", "description": "Customer UUID."},
                "project_id": {"type": "string", "description": "Project UUID."},
                "quote": {"type": "string", "description": "Customer quote text."},
                "consent": {
                    "type": "boolean",
                    "description": "Whether customer granted explicit consent.",
                },
                "consent_tier": {
                    "type": "string",
                    "enum": ["web_retractable", "ai_citable_irrevocable"],
                    "description": "Consent tier (MK-4): web_retractable or ai_citable_irrevocable.",
                },
                "attribution_name": {
                    "type": "string",
                    "description": "Optional contact attribution.",
                },
                "attribution_title": {"type": "string", "description": "Optional title/role."},
            },
            "required": [
                "namespace_id",
                "customer_id",
                "project_id",
                "quote",
                "consent",
                "consent_tier",
            ],
        },
    ),
    Tool(
        name="marketing_suggest_content",
        description=(
            "Suggest thought-leadership or drip content ideas grounded in real delivered work and failure-pattern learnings."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "theme": {"type": "string", "description": "Content theme or topic."},
                "vertical": {"type": "string", "description": "Industry vertical focus."},
                "limit": {
                    "type": "integer",
                    "default": 5,
                    "description": "Maximum suggestions to return.",
                },
            },
            "required": ["namespace_id"],
        },
    ),
    Tool(
        name="marketing_audit_seo",
        description=(
            "Audit content asset for AEO/GEO answer engine citation readiness, Schema.org JSON-LD, and structured metadata."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "asset_id": {"type": "string", "description": "Content asset UUID to audit."},
                "content": {
                    "type": "string",
                    "description": "Raw content text or markdown if unpersisted.",
                },
            },
            "required": ["namespace_id"],
        },
    ),
    Tool(
        name="marketing_approve_content",
        description=(
            "Human sign-off approval gate for drafted marketing content or case studies (MK-1). "
            "Records approver and decision in cognitive ledger."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "artifact_id": {
                    "type": "string",
                    "description": "Case study or content asset UUID.",
                },
                "approver": {"type": "string", "description": "Human approver name or identifier."},
                "decision": {
                    "type": "string",
                    "enum": ["approved", "rejected", "changes_requested"],
                    "description": "Approval decision.",
                },
                "notes": {
                    "type": "string",
                    "description": "Optional review feedback or sign-off notes.",
                },
            },
            "required": ["namespace_id", "artifact_id", "approver", "decision"],
        },
    ),
    Tool(
        name="marketing_publish_content",
        description=(
            "Publish marketing content via PublishTransport. Enforces recorded human approval (MK-1) and valid consent (MK-4)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                "artifact_id": {
                    "type": "string",
                    "description": "Case study or content asset UUID.",
                },
                "transport": {
                    "type": "string",
                    "enum": ["manual", "cms"],
                    "default": "manual",
                    "description": "Publish transport destination (manual export or cms).",
                },
            },
            "required": ["namespace_id", "artifact_id"],
        },
    ),
    # --- Staff & Resources Engine (Module 15) ---
    Tool(
        name="resources_resolve_capacity",
        description="Resolve capacity calendar and utilization for resources in a namespace.",
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Tenant namespace UUID."},
                "starts_at": {"type": "string", "description": "ISO start timestamp."},
                "ends_at": {"type": "string", "description": "ISO end timestamp."},
                "resource_id": {
                    "type": "string",
                    "description": "Optional specific resource UUID.",
                },
                "kind": {
                    "type": "string",
                    "description": "Optional filter by kind ('employee', 'contractor', 'vehicle', 'tool').",
                },
            },
            "required": ["namespace_id"],
        },
    ),
    Tool(
        name="resources_plan_allocation",
        description="AI allocation advisor: multi-objective skill matching and cognitive recall from ledger.",
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Tenant namespace UUID."},
                "demand_kind": {
                    "type": "string",
                    "description": "Demand kind (e.g. 'project', 'work_order', 'service').",
                },
                "demand_id": {"type": "string", "description": "Optional demand identifier."},
                "starts_at": {"type": "string", "description": "Required window start timestamp."},
                "ends_at": {"type": "string", "description": "Required window end timestamp."},
                "required_skills": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of required skills.",
                },
                "required_role": {
                    "type": "string",
                    "description": "Optional required role or kind.",
                },
                "functional_location_id": {
                    "type": "string",
                    "description": "Optional site location UUID.",
                },
            },
            "required": ["namespace_id", "demand_kind", "starts_at", "ends_at"],
        },
    ),
    Tool(
        name="resources_detect_conflicts",
        description="Detect overlapping double-bookings and schedule clashes across resources.",
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Tenant namespace UUID."},
                "resource_id": {"type": "string", "description": "Optional resource UUID."},
                "starts_at": {"type": "string", "description": "Optional window start."},
                "ends_at": {"type": "string", "description": "Optional window end."},
            },
            "required": ["namespace_id"],
        },
    ),
    Tool(
        name="resources_forecast_demand",
        description="Forecast staff & resource demand vs capacity across planning horizons; hire/contractor signals.",
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Tenant namespace UUID."},
                "horizon_days": {
                    "type": "integer",
                    "description": "Forecast horizon in days (default: 30).",
                },
                "role": {"type": "string", "description": "Optional role or kind filter."},
                "skills": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional skill filters.",
                },
            },
            "required": ["namespace_id"],
        },
    ),
    Tool(
        name="resources_field_schedule",
        description="Field webapp mobile read model: composed technician schedule, travel, lodging, van stock, and work orders.",
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Tenant namespace UUID."},
                "resource_id": {"type": "string", "description": "Technician resource UUID."},
                "starts_at": {"type": "string", "description": "Optional start window."},
                "ends_at": {"type": "string", "description": "Optional end window."},
            },
            "required": ["namespace_id", "resource_id"],
        },
    ),
    Tool(
        name="resources_reserve",
        description="Reserve time window for a resource; DB-enforced against double-booking via btree_gist (RS-3).",
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Tenant namespace UUID."},
                "resource_id": {"type": "string", "description": "Resource UUID to reserve."},
                "starts_at": {"type": "string", "description": "Window start timestamp."},
                "ends_at": {"type": "string", "description": "Window end timestamp."},
                "demand_kind": {
                    "type": "string",
                    "description": "Demand kind (e.g. 'project', 'work_order').",
                },
                "demand_id": {"type": "string", "description": "Optional demand identifier."},
                "functional_location_id": {
                    "type": "string",
                    "description": "Optional site location UUID.",
                },
                "attrs": {
                    "type": "object",
                    "description": "Optional arbitrary allocation attributes.",
                },
            },
            "required": ["namespace_id", "resource_id", "starts_at", "ends_at", "demand_kind"],
        },
    ),
    Tool(
        name="resources_release",
        description="Release an active resource allocation, freeing capacity and lifting exclusion.",
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Tenant namespace UUID."},
                "allocation_id": {"type": "string", "description": "Allocation UUID to release."},
            },
            "required": ["namespace_id", "allocation_id"],
        },
    ),
    Tool(
        name="resources_plan_material_flow",
        description="Coordinate material staging: warehouse pick, van loading (RS-2), transport, and delivery.",
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Tenant namespace UUID."},
                "project_id": {"type": "string", "description": "Project UUID."},
                "van_resource_id": {"type": "string", "description": "Van resource UUID (RS-2)."},
                "destination_location_id": {
                    "type": "string",
                    "description": "Delivery destination location ID.",
                },
                "staging_start": {
                    "type": "string",
                    "description": "Material pick/stage start timestamp.",
                },
                "delivery_deadline": {
                    "type": "string",
                    "description": "Delivery deadline timestamp.",
                },
                "bom_line_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional BOM line identifiers.",
                },
            },
            "required": [
                "namespace_id",
                "project_id",
                "van_resource_id",
                "destination_location_id",
                "staging_start",
                "delivery_deadline",
            ],
        },
    ),
    Tool(
        name="resources_plan_travel",
        description="Plan or book technician travel & lodging behind Contract-B spend gate (RS-5) with Norwegian diett.",
        inputSchema={
            "type": "object",
            "properties": {
                "namespace_id": {"type": "string", "description": "Tenant namespace UUID."},
                "allocation_id": {"type": "string", "description": "Allocation UUID."},
                "action": {
                    "type": "string",
                    "enum": ["plan", "book"],
                    "description": "Action: 'plan' (advisor) or 'book' (actor).",
                },
                "idempotency_key": {"type": "string", "description": "Required for 'book' action."},
                "spend_ceiling_nok": {
                    "type": "number",
                    "description": "Optional spend ceiling (default 10,000 NOK).",
                },
                "confirm": {
                    "type": "boolean",
                    "description": "Explicit confirmation if booking exceeds ceiling.",
                },
                "itinerary": {
                    "type": "object",
                    "description": "Travel itinerary including travel_legs, stays, per_diems.",
                },
            },
            "required": ["namespace_id", "allocation_id", "itinerary"],
        },
    ),
]


# Conditionally include migration tools based on operator config.
if not cfg.NCE_DISABLE_MIGRATION_MCP:
    TOOLS = TOOLS + _MIGRATION_TOOLS

# Conditionally include Dynamics 365 tools when the module is enabled.
if cfg.NCE_D365_ENABLED:
    TOOLS = TOOLS + [
        Tool(
            name="d365_query_case",
            description=(
                "Query a Dynamics 365 case (incident) by ID, enriched with "
                "NCE graph context, related annotations, and activity timeline."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                    "case_id": {
                        "type": "string",
                        "description": "Dataverse incident GUID.",
                    },
                    "include_notes": {
                        "type": "boolean",
                        "default": True,
                        "description": "Fetch linked annotations.",
                    },
                    "include_activities": {
                        "type": "boolean",
                        "default": False,
                        "description": "Fetch activity timeline.",
                    },
                },
                "required": ["namespace_id", "case_id"],
            },
        ),
        Tool(
            name="d365_sync_now",
            description=(
                "[Admin] Trigger an immediate Dynamics 365 entity sync for a namespace. "
                "Syncs Accounts, Contacts, Opportunities, and Incidents to kg_edges."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "namespace_id": {"type": "string"},
                    "entity_types": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["accounts", "contacts", "opportunities", "incidents"],
                        },
                        "description": "Subset to sync; omit for all four entity types.",
                    },
                },
                "required": ["namespace_id"],
            },
        ),
        Tool(
            name="d365_case_stress_report",
            description=(
                "Empathic Tensor frustration and burnout report for Dynamics 365 cases "
                "linked to a given account. Queries v3_cognitive_ledger for frustration "
                "trends extracted from case notes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "namespace_id": {"type": "string"},
                    "account_name": {
                        "type": "string",
                        "description": "Account name as it appears in kg_edges.",
                    },
                    "lookback_days": {
                        "type": "integer",
                        "default": 30,
                        "minimum": 1,
                        "maximum": 365,
                        "description": "How many days back to include.",
                    },
                },
                "required": ["namespace_id", "account_name"],
            },
        ),
        Tool(
            name="d365_list_sla_breaches",
            description=(
                "[Admin] List Dynamics 365 SLA breach events from the WORM event_log. "
                "Returns signed, immutable breach records since a given timestamp."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "namespace_id": {"type": "string"},
                    "since": {
                        "type": "string",
                        "description": "ISO-8601 datetime — return breaches after this time.",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 50,
                        "minimum": 1,
                        "maximum": 500,
                    },
                },
                "required": ["namespace_id", "since"],
            },
        ),
        Tool(
            name="d365_netbox_mappings",
            description=(
                "Query the D365 ↔ NetBox cross-reference mapping table. "
                "Returns identity links between Dynamics 365 Accounts/Functional Locations "
                "and NetBox Tenants/Sites, including the match method and confidence score. "
                "Use this to understand which CRM customer maps to which network tenant or site."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "namespace_id": {"type": "string"},
                    "entity_type": {
                        "type": "string",
                        "enum": ["all", "account", "functional_location"],
                        "default": "all",
                        "description": "Filter by D365 entity type.",
                    },
                    "confirmed_only": {
                        "type": "boolean",
                        "default": False,
                        "description": "Return only human-confirmed mappings.",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 100,
                        "minimum": 1,
                        "maximum": 500,
                    },
                },
                "required": ["namespace_id"],
            },
        ),
        Tool(
            name="evaluate_circuit_impact",
            description=(
                "Evaluate downstream circuit impact from telemetry degradations "
                "using do-calculus causal graphs. Identifies which NetBox circuits "
                "are causally linked to active degradations."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                    "telemetry_degradations": {
                        "type": "object",
                        "additionalProperties": {"type": "number"},
                        "description": "Mapping of telemetry node IDs to their degradation severity scores (0.0 to 1.0).",
                    },
                    "degradation_threshold": {
                        "type": "number",
                        "default": 0.5,
                        "description": "Minimum telemetry degradation score to evaluate.",
                    },
                    "causal_threshold": {
                        "type": "number",
                        "default": 0.5,
                        "description": "Minimum do-calculus causal probability to link degradation to a circuit.",
                    },
                },
                "required": ["namespace_id", "telemetry_degradations"],
            },
        ),
    ]

# Conditionally include Diagnostic Log Digestion Engine tools when enabled.
if cfg.NCE_DIAG_ENABLED:
    TOOLS = TOOLS + [
        Tool(
            name="diag_ingest_bundle",
            description=(
                "Begin a diagnostic-bundle ingestion: mint a tenant-prefixed presigned "
                "PUT URL and register a PENDING diag_ingestions row. Returns the upload "
                "URL, deterministic ingest_id, and landing_uri."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                    "vendor_profile": {
                        "type": "string",
                        "description": "Vendor profile slug (validated against the profile registry).",
                    },
                    "device_slug": {
                        "type": "string",
                        "description": "Device identifier the bundle belongs to.",
                    },
                    "object_name": {
                        "type": "string",
                        "description": "Bundle file name (extension validated by the storage layer).",
                    },
                    "source": {
                        "type": "string",
                        "enum": ["upload", "api", "ticketing"],
                        "default": "upload",
                        "description": "Ingestion source.",
                    },
                    "etag": {
                        "type": "string",
                        "description": "Optional client-supplied etag for a deterministic ingest_id.",
                    },
                },
                "required": ["namespace_id", "vendor_profile", "device_slug", "object_name"],
            },
        ),
        Tool(
            name="diag_commit_bundle",
            description=(
                "Commit an uploaded bundle: enqueue process_diag_bundle on the isolated "
                "diag_ingest RQ lane. Returns the job_id and PROCESSING status."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                    "ingest_id": {
                        "type": "string",
                        "description": "Deterministic id returned by diag_ingest_bundle.",
                    },
                    "etag": {
                        "type": "string",
                        "description": "Optional uploaded-object etag passed to the worker.",
                    },
                },
                "required": ["namespace_id", "ingest_id"],
            },
        ),
        Tool(
            name="diag_digest_status",
            description=(
                "Read-only: return ingestion status rows for a namespace, optionally "
                "filtered to a single ingest_id."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                    "ingest_id": {
                        "type": "string",
                        "description": "Optional; when omitted returns the most recent rows.",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 20,
                        "minimum": 1,
                        "maximum": 200,
                    },
                },
                "required": ["namespace_id"],
            },
        ),
        Tool(
            name="diag_device_health",
            description=(
                "Read-only: latest per-device health rollup for a namespace, optionally "
                "filtered to a single device."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                    "device_slug": {
                        "type": "string",
                        "description": "Optional; filter to a single device.",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 50,
                        "minimum": 1,
                        "maximum": 500,
                    },
                },
                "required": ["namespace_id"],
            },
        ),
        Tool(
            name="diag_list_anomalies",
            description=(
                "Read-only: list anomalies for a namespace, optionally scoped to one "
                "ingestion or device."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
                    "ingest_id": {
                        "type": "string",
                        "description": "Optional; filter to one ingestion.",
                    },
                    "device_slug": {
                        "type": "string",
                        "description": "Optional; filter to one device.",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 50,
                        "minimum": 1,
                        "maximum": 500,
                    },
                },
                "required": ["namespace_id"],
            },
        ),
    ]
