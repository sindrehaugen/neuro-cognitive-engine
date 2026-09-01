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
