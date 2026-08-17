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
