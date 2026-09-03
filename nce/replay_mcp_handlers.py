"""
MCP tool handlers for event replay operations (§10). Extracted from server.py:call_tool().
Follows the same pattern as bridge_mcp_handlers.py — each handler receives the engine
and raw arguments dict, and returns a JSON string that call_tool() wraps in TextContent.

Note: Admin authorization (_check_admin) is handled by call_tool() as a cross-cutting
concern — handlers focus purely on domain logic.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from nce.background_task_manager import create_tracked_task
from nce.mcp_errors import mcp_handler
from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.replay_mcp_handlers")


@mcp_handler
async def handle_replay_observe(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """[ADMIN] Observational replay — reads event log and replays events."""
    from nce.replay import ObservationalReplay

    replay = ObservationalReplay(pool=engine.pg_pool)
    lines: list[str] = []
    count = 0
    async for item in replay.execute(
        source_namespace_id=uuid.UUID(arguments["namespace_id"]),
        start_seq=int(arguments.get("start_seq", 1)),
        end_seq=int(arguments["end_seq"]) if "end_seq" in arguments else None,
        agent_id_filter=arguments.get("agent_id_filter"),
    ):
        lines.append(json.dumps(item))
        if item.get("type") == "event":
            count += 1
            if count >= int(arguments.get("max_events", 500)):
                lines.append(json.dumps({"type": "truncated", "reason": "max_events_reached"}))
                break
    return "\n".join(lines)


@mcp_handler
async def handle_replay_fork(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """[ADMIN] Forked replay — creates a new namespace from a fork point."""
    from pydantic import ValidationError

    from nce.models import FrozenForkConfig, ReplayForkRequest
    from nce.replay import ForkedReplay, _create_run

    try:
        fork_req = ReplayForkRequest.model_validate(
            {
                "source_namespace_id": arguments["source_namespace_id"],
                "target_namespace_id": arguments["target_namespace_id"],
                "fork_seq": int(arguments["fork_seq"]),
                "start_seq": int(arguments.get("start_seq", 1)),
                "replay_mode": arguments.get("replay_mode", "deterministic"),
                "config_overrides": arguments.get("config_overrides"),
                "agent_id_filter": arguments.get("agent_id_filter"),
                "expected_sha256": arguments["expected_sha256"],
            }
        )
    except (ValidationError, KeyError) as exc:
        raise ValueError(f"Invalid replay_fork parameters: {exc}") from exc

    # ── Build the frozen execution config (immutable after this point) ──
    frozen_config = FrozenForkConfig.from_request(fork_req)

    async with engine.pg_pool.acquire(timeout=10.0) as pre_conn:
        fork_run_id = await _create_run(
            pre_conn,
            source_namespace_id=frozen_config.source_namespace_id,
            target_namespace_id=frozen_config.target_namespace_id,
            mode="forked",
            replay_mode=frozen_config.replay_mode,
            start_seq=frozen_config.start_seq,
            end_seq=frozen_config.fork_seq,
            divergence_seq=frozen_config.fork_seq,
            config_overrides=frozen_config.overrides_dict,
        )

    replay = ForkedReplay(pool=engine.pg_pool)

    async def _run_fork():
        try:
            async for _ in replay.execute(
                frozen_config=frozen_config,
                _existing_run_id=fork_run_id,
            ):
                pass
        except Exception:
            log.exception("Replay failed")

    create_tracked_task(_run_fork(), name=f"fork-{fork_run_id}")
    return json.dumps({"status": "started", "run_id": str(fork_run_id)})


@mcp_handler
async def handle_replay_reconstruct(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """[ADMIN] Reconstructive replay — reproduces byte-identical state at end_seq."""
    from nce.replay import ReconstructiveReplay, _create_run

    src_ns = uuid.UUID(arguments["source_namespace_id"])
    tgt_ns = uuid.UUID(arguments["target_namespace_id"])
    end_seq = int(arguments["end_seq"])
    start_seq = int(arguments.get("start_seq", 1))
    agent_filter = arguments.get("agent_id_filter")

    async with engine.pg_pool.acquire(timeout=10.0) as pre_conn:
        run_id = await _create_run(
            pre_conn,
            source_namespace_id=src_ns,
            target_namespace_id=tgt_ns,
            mode="reconstructive",
            replay_mode="deterministic",
            start_seq=start_seq,
            end_seq=end_seq,
            divergence_seq=None,
            config_overrides=None,
        )

    replay = ReconstructiveReplay(pool=engine.pg_pool)

    async def _run():
        try:
            async for _ in replay.execute(
                source_namespace_id=src_ns,
                target_namespace_id=tgt_ns,
                end_seq=end_seq,
                start_seq=start_seq,
                agent_id_filter=agent_filter,
                _existing_run_id=run_id,
            ):
                pass
        except Exception:
            log.exception("Reconstructive replay failed")

    create_tracked_task(_run(), name=f"reconstruct-{run_id}")
    return json.dumps({"status": "started", "run_id": str(run_id)})


@mcp_handler
async def handle_replay_status(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """[ADMIN] Check the status of a replay run."""
    from nce.replay import get_run_status

    status = await get_run_status(engine.pg_pool, uuid.UUID(arguments["run_id"]))
    return json.dumps(status)


@mcp_handler
async def handle_get_event_provenance(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Get the full provenance chain for a memory."""
    from nce.replay import get_event_provenance

    provenance = await get_event_provenance(engine.pg_pool, uuid.UUID(arguments["memory_id"]))
    return json.dumps(provenance)


@mcp_handler
async def handle_detect_causal_cycles(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """[ADMIN] Detect cycles in the event_parents causal DAG for a namespace.

    Required arguments:
        namespace_id (str, UUID)
    Optional arguments:
        depth_cap    (int, default 50) -- traversal depth limit.

    Read-only: walks event_parents and reports cycles, never mutating the DAG.
    """
    import uuid as _uuid

    from nce.admin_handlers.fleet import detect_causal_cycles

    namespace_id = _uuid.UUID(arguments["namespace_id"])
    depth_cap = int(arguments.get("depth_cap", 50))
    result = await detect_causal_cycles(engine.pg_pool, namespace_id, depth_cap=depth_cap)
    return json.dumps(result)


@mcp_handler
async def handle_explain_memory(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Client-facing tool returning the signed receipt for a memory."""
    from nce.replay import get_event_provenance

    provenance = await get_event_provenance(engine.pg_pool, uuid.UUID(arguments["memory_id"]))
    chain = provenance.get("chain", [])
    if not chain:
        return json.dumps(
            {"memory_id": str(arguments["memory_id"]), "error": "Memory provenance not found"}
        )

    evt = chain[-1]
    receipt = {
        "memory_id": str(arguments["memory_id"]),
        "event_seq": evt["event_seq"],
        "agent_id": evt["agent_id"],
        "occurred_at": evt["occurred_at"],
        "signature": evt["signature"],
        "verified": evt["verified"],
    }
    return json.dumps(receipt)


@mcp_handler
async def handle_explain_past_decision(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """[Phase II.5] Bi-temporal "explain-my-past-advice".

    Reconstructs the agent's belief state *as it stood* at ``as_of`` (the memories
    valid at T) and attaches the signed epistemic receipt — the provenance event
    valid at T — to each belief.  When a counterfactual fork is requested
    (``source_namespace_id`` + ``target_namespace_id`` + ``fork_seq``), a *verified*
    forked replay is run and its ``digest_match`` outcome is returned so the
    reconstruction is provably faithful rather than hand-waved.
    """
    from nce.db_utils import scoped_pg_session
    from nce.models import FrozenForkConfig, ReplayForkRequest
    from nce.replay import ForkedReplay, _create_run, get_event_provenance, get_run_status
    from nce.state_digest import compute_namespace_state_digest
    from nce.temporal import as_of_query, parse_as_of

    namespace_id = uuid.UUID(arguments["namespace_id"])
    as_of_dt = parse_as_of(arguments.get("as_of"))
    agent_filter = arguments.get("agent_id_filter")
    max_beliefs = int(arguments.get("max_beliefs", 200))

    # ── 1. Reconstruct the belief set valid at T (bi-temporal as_of read) ──
    clause, as_of_params = as_of_query("", as_of_dt, start_index=2)
    agent_clause = ""
    params: list[Any] = [namespace_id, *as_of_params]
    if agent_filter:
        agent_clause = f"AND agent_id = ${len(params) + 1}"
        params.append(agent_filter)

    sql = f"""
        SELECT id, agent_id, memory_type, assertion_type,
               valid_from, valid_to, created_at
        FROM memories
        WHERE namespace_id = $1 {clause} {agent_clause}
        ORDER BY valid_from ASC, id ASC
        LIMIT {max_beliefs}
    """
    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        belief_rows = await conn.fetch(sql, *params)

    # ── 2. Attach the signed receipt valid at T to each belief ──
    beliefs: list[dict[str, Any]] = []
    for row in belief_rows:
        memory_id = row["id"]
        provenance = await get_event_provenance(engine.pg_pool, memory_id)
        # Only receipts that existed *at or before* T were knowable then.
        valid_chain = [
            evt
            for evt in provenance.get("chain", [])
            if as_of_dt is None or evt["occurred_at"] <= as_of_dt.isoformat()
        ]
        receipt = valid_chain[-1] if valid_chain else None
        beliefs.append(
            {
                "memory_id": str(memory_id),
                "agent_id": row["agent_id"],
                "memory_type": row["memory_type"],
                "assertion_type": row["assertion_type"],
                "valid_from": row["valid_from"].isoformat() if row["valid_from"] else None,
                "valid_to": row["valid_to"].isoformat() if row["valid_to"] else None,
                "receipt": (
                    {
                        "event_seq": receipt["event_seq"],
                        "occurred_at": receipt["occurred_at"],
                        "signature": receipt["signature"],
                        "verified": receipt["verified"],
                    }
                    if receipt
                    else None
                ),
            }
        )

    response: dict[str, Any] = {
        "namespace_id": str(namespace_id),
        "as_of": as_of_dt.isoformat() if as_of_dt else None,
        "belief_count": len(beliefs),
        "beliefs": beliefs,
    }

    # ── 3. Optional counterfactual: a VERIFIED forked replay (digest_match) ──
    if all(k in arguments for k in ("source_namespace_id", "target_namespace_id", "fork_seq")):
        fork_req = ReplayForkRequest.model_validate(
            {
                "source_namespace_id": arguments["source_namespace_id"],
                "target_namespace_id": arguments["target_namespace_id"],
                "fork_seq": int(arguments["fork_seq"]),
                "start_seq": int(arguments.get("start_seq", 1)),
                "replay_mode": arguments.get("replay_mode", "deterministic"),
                "config_overrides": arguments.get("config_overrides"),
                "agent_id_filter": agent_filter,
                "expected_sha256": arguments["expected_sha256"],
            }
        )
        frozen_config = FrozenForkConfig.from_request(fork_req)

        async with engine.pg_pool.acquire(timeout=10.0) as pre_conn:
            fork_run_id = await _create_run(
                pre_conn,
                source_namespace_id=frozen_config.source_namespace_id,
                target_namespace_id=frozen_config.target_namespace_id,
                mode="forked",
                replay_mode=frozen_config.replay_mode,
                start_seq=frozen_config.start_seq,
                end_seq=frozen_config.fork_seq,
                divergence_seq=frozen_config.fork_seq,
                config_overrides=frozen_config.overrides_dict,
            )

        replay = ForkedReplay(pool=engine.pg_pool)
        async for _ in replay.execute(frozen_config=frozen_config, _existing_run_id=fork_run_id):
            pass

        # ForkedReplay does not compute state digests itself (only ReconstructiveReplay
        # does).  Verify the fork is byte-identically faithful by comparing the canonical
        # state digest of source vs. target *as of the fork point* — the same mechanism
        # ReconstructiveReplay uses.  This is what makes the counterfactual provable.
        async with engine.pg_pool.acquire(timeout=10.0) as digest_conn:
            fork_point_ts = await digest_conn.fetchval(
                "SELECT occurred_at FROM event_log WHERE namespace_id = $1 AND event_seq = $2",
                frozen_config.source_namespace_id,
                frozen_config.fork_seq,
            )
            source_digest = await compute_namespace_state_digest(
                digest_conn, frozen_config.source_namespace_id, as_of=fork_point_ts
            )
            target_digest = await compute_namespace_state_digest(
                digest_conn, frozen_config.target_namespace_id, as_of=fork_point_ts
            )

        status = await get_run_status(engine.pg_pool, fork_run_id)
        response["counterfactual"] = {
            "run_id": str(fork_run_id),
            "status": status["status"],
            "digest_match": source_digest == target_digest,
            "source_state_digest": source_digest,
            "target_state_digest": target_digest,
        }

    return json.dumps(response, default=str)
