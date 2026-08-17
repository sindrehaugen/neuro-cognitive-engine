from __future__ import annotations

import json
import logging

from starlette.responses import JSONResponse

from nce import admin_state
from nce.admin_handlers._shared import *  # noqa: F403

logger = logging.getLogger("nce-admin-tools")

# Standard operational impact texts for all stdio MCP tools and public A2A server skills
TOOL_IMPACTS = {
    # MCP (Stdio) Engine Tools
    "store_memory": "Disabling this prevents agents from writing new memories, which can cause them to fail to retain contextual information across turns.",
    "store_artifact": "Disabling this prevents ingestion of large documents, PDFs, or system logs into the Quad-Stack repository.",
    "store_media": "Disabling this prevents storage and ingestion of image, video, and audio assets.",
    "index_code_file": "Disabling this prevents parsing and indexing source files, breaking dynamic codebase understanding.",
    "check_indexing_status": "Disabling this stops monitoring of background codebase indexing jobs.",
    "search_codebase": "Disabling this prevents semantic searches across all indexed codebases, disabling AI coding capabilities.",
    "graph_search": "Disabling this halts Knowledge Graph (GraphRAG) BFS traversal, preventing the agent from discovering entity relationships.",
    "get_recent_context": "Disabling this prevents querying recent episodic context, making it hard to maintain immediate conversation state.",
    "connect_bridge": "Disabling this prevents starting new document provider bridges (Google Drive, SharePoint, etc.).",
    "complete_bridge_auth": "Disabling this blocks completion of the OAuth flow for document bridges.",
    "list_bridges": "Disabling this prevents administrators and agents from viewing active bridge subscriptions.",
    "disconnect_bridge": "Disabling this prevents deleting or disabling active bridge integrations.",
    "force_resync_bridge": "Disabling this stops manual resynchronization of document bridges.",
    "bridge_status": "Disabling this prevents reading bridge sync status and token expiration.",
    "boost_memory": "Disabling this prevents highlighting specific memories or bumping their relative salience.",
    "forget_memory": "Disabling this blocks memory deletion (forgetting) operations, which may impact user privacy compliance.",
    "list_contradictions": "Disabling this blocks inspection of conflicting or contradicting memories.",
    "resolve_contradiction": "Disabling this prevents fixing cognitive contradictions inside the memory graph.",
    "unredact_memory": "Disabling this prevents unmasking PII-redacted fields for authorized admin inspection.",
    "replay_observe": "Disabling this blocks historical event streaming from the event ledger.",
    "replay_fork": "Disabling this blocks forking memory states or snapshot histories.",
    "replay_reconstruct": "Disabling this prevents reconstruction of byte-identical memory structures from events.",
    "replay_status": "Disabling this prevents monitoring of background memory reconstruction and replay runs.",
    "get_event_provenance": "Disabling this halts causal provenance checks, rendering ledger trust chains unverifiable.",
    "a2a_create_grant": "Disabling this blocks creation of new Agent-to-Agent sharing tokens, preventing cross-tenant collaboration.",
    "a2a_revoke_grant": "Disabling this prevents immediate revocation of active agent sharing grants.",
    "a2a_list_grants": "Disabling this blocks listing of active sharing grants, impeding compliance audits.",
    "a2a_query_shared": "Disabling this halts cross-agent queries, blocking retrieval of shared tenant data.",
    "a2a_verify_grant_status": "Disabling this prevents checking active status or scopes of shared tokens.",
    "a2a_update_grant_scopes": "Disabling this prevents modifying or appending scopes on existing sharing grants.",
    "a2a_inspect_grant": "Disabling this prevents auditing full metadata for active sharing grants.",
    "create_snapshot": "Disabling this prevents taking point-in-time state snapshots.",
    "list_snapshots": "Disabling this prevents retrieving state snapshots.",
    "delete_snapshot": "Disabling this blocks deletion of state snapshots.",
    "compare_states": "Disabling this blocks running state comparison diffs between two points in time.",
    "start_migration": "Disabling this blocks starting new embedding migration runs.",
    "migration_status": "Disabling this blocks checking active embedding migration status.",
    "validate_migration": "Disabling this blocks running quality gate checks on finished migrations.",
    "commit_migration": "Disabling this blocks committing validated embedding migrations.",
    "abort_migration": "Disabling this blocks aborting active embedding migrations.",
    # A2A (Network) Server Skills
    "recall_relevant_context": "Disabling this prevents external recipient agents from retrieving context or memory embeddings shared by this agent.",
    "archive_session": "Disabling this prevents foreign agents from archiving memory sequences into this agent's database.",
    "find_related_decisions": "Disabling this stops foreign agents from exploring the Knowledge Graph for contextual decision nodes.",
    "verify_memory_integrity": "Disabling this blocks external callers from validating Merkle chain or state signatures of shared memories.",
    "get_cognitive_state": "Disabling this prevents external systems from querying the active episodic context of this agent's sessions.",
    "verify_grant_status_skill": "Disabling this prevents external agents from querying verification status or scope eligibility of sharing tokens.",
}

# Standard descriptions for A2A Network skills since they aren't listed in mcp_stdio_tools.py
A2A_SKILL_DESCRIPTIONS = {
    "recall_relevant_context": "Retrieve semantic memory embeddings and context subgraphs shared with a foreign agent.",
    "archive_session": "Store memory sequences or logs received from an external trusted agent.",
    "find_related_decisions": "Explore contextual decision nodes and entity relationships in the shared memory graph.",
    "verify_memory_integrity": "Validate Merkle trees and cryptographic state signatures on shared memories.",
    "get_cognitive_state": "Retrieve the current active episodic state of specific agent sessions.",
    "verify_grant_status": "Verify active verification status or scope eligibility of sharing tokens.",
}


async def api_admin_tools(request) -> JSONResponse:
    """GET /api/admin/tools

    List all local stdio MCP tools and public A2A server skills, along with
    their active enabling toggle state, descriptions, and operational impact warnings.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    disabled_keys = set()
    try:
        if admin_state.engine.redis_client:
            keys_raw = await admin_state.engine.redis_client.hkeys("nce:tools:disabled")
            disabled_keys = {
                k.decode("utf-8") if isinstance(k, bytes) else str(k) for k in keys_raw
            }
    except Exception as exc:
        logger.warning(
            "Failed to retrieve disabled tools from Redis (defaulting to empty): %s", exc
        )

    # 1. Gather Stdio MCP Tools
    from nce.mcp_stdio_tools import TOOLS

    mcp_tools = []
    for tool in TOOLS:
        name = tool.name
        mcp_tools.append(
            {
                "name": name,
                "type": "mcp",
                "description": tool.description,
                "impact": TOOL_IMPACTS.get(name, "No immediate operational impact specified."),
                "enabled": name not in disabled_keys,
            }
        )

    # 2. Gather Public A2A Server Skills
    a2a_skills = []
    for skill_name, desc in A2A_SKILL_DESCRIPTIONS.items():
        a2a_skills.append(
            {
                "name": skill_name,
                "type": "a2a",
                "description": desc,
                "impact": TOOL_IMPACTS.get(
                    skill_name,
                    TOOL_IMPACTS.get(
                        f"{skill_name}_skill", "No immediate operational impact specified."
                    ),
                ),
                "enabled": skill_name not in disabled_keys,
            }
        )

    return JSONResponse({"mcp_tools": mcp_tools, "a2a_skills": a2a_skills})


async def api_admin_tools_toggle(request) -> JSONResponse:
    """POST /api/admin/tools/toggle

    Enable or disable a specific tool or skill dynamically at runtime.
    Saves state in Redis under a custom hash `nce:tools:disabled` for instant multi-worker sync.

    Body (JSON):
      tool_name (str, required)
      tool_type (str, required)
      enabled   (bool, required)
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
        tool_name = str(body["tool_name"]).strip()
        tool_type = str(body["tool_type"]).strip()
        enabled = bool(body["enabled"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": f"Invalid body parameters: {exc}"}, status_code=400)

    if tool_type not in ("mcp", "a2a"):
        return JSONResponse({"error": "tool_type must be either 'mcp' or 'a2a'"}, status_code=422)

    try:
        if admin_state.engine.redis_client:
            if enabled:
                # Remove from disabled list to enable it
                await admin_state.engine.redis_client.hdel("nce:tools:disabled", tool_name)
                logger.info("Enabled %s tool/skill dynamically: %s", tool_type.upper(), tool_name)
            else:
                # Add to disabled list to disable it
                await admin_state.engine.redis_client.hset("nce:tools:disabled", tool_name, "1")
                logger.info("Disabled %s tool/skill dynamically: %s", tool_type.upper(), tool_name)
        else:
            return JSONResponse({"error": "Redis client not available"}, status_code=503)
    except Exception as exc:
        logger.error("Failed to persist dynamic tool toggle to Redis: %s", exc)
        return JSONResponse({"error": f"Redis synchronization failed: {exc}"}, status_code=500)

    return JSONResponse({"ok": True, "tool_name": tool_name, "enabled": enabled})
