from __future__ import annotations

from nce.admin_handlers import _shared
from nce.admin_handlers._shared import *  # noqa: F403


async def api_replay_observe(request):
    """POST /api/replay/observe

    Stream historical events from a namespace as a JSONL response body.
    Each line is a JSON object with ``type`` in {event, progress, complete, error}.

    Request body (JSON):
        namespace_id     str   required
        start_seq        int   optional, default 1
        end_seq          int   optional, defaults to latest
        agent_id_filter  str   optional
        max_events       int   optional, default 500
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError, TypeError):
        return JSONResponse({"error": "Request body must be valid JSON"}, status_code=400)

    if not body.get("namespace_id"):
        return JSONResponse({"error": "Missing required field: namespace_id"}, status_code=422)

    try:
        ns_id = uuid.UUID(body["namespace_id"])
    except ValueError:
        return JSONResponse({"error": "namespace_id is not a valid UUID"}, status_code=422)

    start_seq = int(body.get("start_seq", 1))
    end_seq = int(body["end_seq"]) if "end_seq" in body else None
    agent_filter = body.get("agent_id_filter")
    max_events = int(body.get("max_events", 500))

    from nce.replay import ObservationalReplay

    async def _stream_events() -> AsyncGenerator[str, None]:
        """Inner async generator — never materialises the full list in RAM."""
        replay = ObservationalReplay(pool=admin_state.engine.pg_pool)
        count = 0
        try:
            async for item in replay.execute(
                source_namespace_id=ns_id,
                start_seq=start_seq,
                end_seq=end_seq,
                agent_id_filter=agent_filter,
            ):
                yield json.dumps(item) + "\n"
                if item.get("type") == "event":
                    count += 1
                    if count >= max_events:
                        yield (
                            json.dumps(
                                {
                                    "type": "truncated",
                                    "reason": "max_events_reached",
                                    "events_returned": count,
                                }
                            )
                            + "\n"
                        )
                        return
        except Exception as exc:
            _shared.logger.exception("api_replay_observe stream failed ns=%s", ns_id)
            yield (
                json.dumps(
                    {
                        "type": "error",
                        "message": f"Stream failed after {count} events: {exc}",
                    }
                )
                + "\n"
            )

    return StreamingResponse(
        _stream_events(),
        media_type="application/x-ndjson",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-cache",
        },
    )


# ---------------------------------------------------------------------------
# Phase 3 — Snapshot export (streaming NDJSON)
# ---------------------------------------------------------------------------


async def api_snapshot_export(request):
    """POST /api/snapshot/export

    Stream all memories for a namespace (at a point in time) as NDJSON.
    Uses a server-side asyncpg cursor — orchestrator RAM stays flat
    regardless of export size.  GB-scale exports safe.

    Request body (JSON):
        namespace_id  str   required
        snapshot_id   str   optional — resolve export to a named snapshot
        as_of         str   optional ISO 8601 UTC timestamp (default: now)

    Response: ``application/x-ndjson`` with lines of type:
        metadata — export header (format version, as_of)
        memory   — one per memory record
        progress — periodic progress marker
        complete — final summary
        error    — terminal error
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError, TypeError):
        return JSONResponse({"error": "Request body must be valid JSON"}, status_code=400)

    ns_id = body.get("namespace_id")
    if not ns_id:
        return JSONResponse({"error": "Missing required field: namespace_id"}, status_code=422)

    try:
        uuid.UUID(ns_id)
    except ValueError:
        return JSONResponse({"error": "namespace_id is not a valid UUID"}, status_code=422)

    snapshot_id = body.get("snapshot_id")
    if snapshot_id:
        try:
            uuid.UUID(snapshot_id)
        except ValueError:
            return JSONResponse({"error": "snapshot_id is not a valid UUID"}, status_code=422)

    as_of_raw = body.get("as_of")
    from nce.temporal import parse_as_of

    as_of_dt = parse_as_of(as_of_raw) if as_of_raw else None

    from nce.snapshot_mcp_handlers import stream_snapshot_export

    return StreamingResponse(
        stream_snapshot_export(admin_state.engine, ns_id, as_of=as_of_dt, snapshot_id=snapshot_id),
        media_type="application/x-ndjson",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-cache",
        },
    )


async def api_replay_fork(request):
    """POST /api/replay/fork

    Start a forked replay as a background task.
    Returns ``{"run_id": "<uuid>", ...}`` immediately; poll ``/api/replay/status/<run_id>``.

    Request body (JSON):
        source_namespace_id  str   required
        target_namespace_id  str   required
        fork_seq             int   required
        start_seq            int   optional, default 1
        replay_mode          str   optional, "deterministic" | "re-execute", default "deterministic"
        config_overrides     obj   optional, used only in re-execute mode
        agent_id_filter      str   optional
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError, TypeError):
        return JSONResponse({"error": "Request body must be valid JSON"}, status_code=400)

    required = ("source_namespace_id", "target_namespace_id", "fork_seq")
    missing = [f for f in required if not body.get(f) and body.get(f) != 0]
    if missing:
        return JSONResponse(
            {"error": f"Missing required fields: {', '.join(missing)}"},
            status_code=422,
        )

    from pydantic import ValidationError

    from nce.models import ReplayForkRequest

    try:
        fork_req = ReplayForkRequest.model_validate(
            {
                "source_namespace_id": body["source_namespace_id"],
                "target_namespace_id": body["target_namespace_id"],
                "fork_seq": int(body["fork_seq"]),
                "start_seq": int(body.get("start_seq", 1)),
                "replay_mode": body.get("replay_mode", "deterministic"),
                "config_overrides": body.get("config_overrides"),
                "agent_id_filter": body.get("agent_id_filter"),
            }
        )
    except ValidationError as exc:
        return JSONResponse(
            {"error": "Invalid request", "detail": exc.errors(include_url=False)},
            status_code=422,
        )

    # ── Build the frozen execution config (immutable after this point) ──
    from nce.models import FrozenForkConfig

    frozen_config = FrozenForkConfig.from_request(fork_req)

    try:
        from nce.replay import ForkedReplay, _create_run

        # Pre-create the row so run_id is available before the background task runs.
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as pre_conn:
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

        replay = ForkedReplay(pool=admin_state.engine.pg_pool)

        async def _run_fork() -> None:
            try:
                async for _ in replay.execute(
                    frozen_config=frozen_config,
                    _existing_run_id=fork_run_id,
                ):
                    pass
            except Exception:
                _shared.logger.exception("Background ForkedReplay failed run_id=%s", fork_run_id)

        create_tracked_task(_run_fork(), name=f"fork-{fork_run_id}")
        return JSONResponse(
            {
                "status": "started",
                "run_id": str(fork_run_id),
                "source_namespace": str(frozen_config.source_namespace_id),
                "target_namespace": str(frozen_config.target_namespace_id),
                "fork_seq": frozen_config.fork_seq,
                "replay_mode": frozen_config.replay_mode,
            },
            status_code=202,
        )

    except Exception as exc:
        return admin_error_response(
            "Fork replay failed to start",
            exc,
            log_event=(
                f"api_replay_fork failed src={frozen_config.source_namespace_id} "
                f"tgt={frozen_config.target_namespace_id}"
            ),
        )


async def api_replay_status(request):
    """GET /api/replay/status/{run_id}

    Return the current status and progress of a replay run.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    run_id_str = request.path_params.get("run_id", "")
    try:
        run_id = uuid.UUID(run_id_str)
    except ValueError:
        return JSONResponse({"error": "run_id is not a valid UUID"}, status_code=422)

    try:
        from nce.replay import ReplayRunNotFoundError, get_run_status

        status = await get_run_status(pool=admin_state.engine.pg_pool, run_id=run_id)
        return JSONResponse(status)
    except ReplayRunNotFoundError as exc:
        return admin_validation_error(exc, status_code=404)
    except Exception as exc:
        return admin_error_response("Status check failed", exc, status_code=500)


async def api_event_provenance(request):
    """GET /api/replay/provenance/{memory_id}

    Trace the full causal chain for a memory via ``parent_event_id`` links.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    memory_id_str = request.path_params.get("memory_id", "")
    try:
        memory_id = uuid.UUID(memory_id_str)
    except ValueError:
        return JSONResponse({"error": "memory_id is not a valid UUID"}, status_code=422)

    try:
        from nce.replay import get_event_provenance

        provenance = await get_event_provenance(
            pool=admin_state.engine.pg_pool, memory_id=memory_id
        )
        return JSONResponse(provenance)
    except Exception as exc:
        return admin_error_response("Provenance trace failed", exc, status_code=500)
