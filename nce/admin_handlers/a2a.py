from __future__ import annotations

from nce.admin_handlers._shared import *  # noqa: F403


async def api_a2a_create_grant(request):
    """POST /api/a2a/grants/create

    Create an A2A sharing grant. The caller must be authenticated as the owner
    namespace via HMAC. Returns a one-time sharing_token to pass to the recipient.

    Body (JSON):
      namespace_id          (str, UUID, required)
      agent_id              (str, required)
      scopes                (list[{resource_type, resource_id, permissions}], required)
      target_namespace_id   (str, UUID, optional) — restrict to a specific recipient namespace
      target_agent_id       (str, optional)       — restrict to a specific recipient agent
      expires_in_seconds    (int, optional, default 3600)
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError, TypeError):
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    from nce.a2a import A2AGrantRequest, A2AScope, create_grant
    from nce.auth import NamespaceContext

    try:
        ns_id = uuid.UUID(body["namespace_id"])
        ns_ctx = getattr(request.state, "namespace_ctx", None)
        if ns_ctx is None or ns_ctx.namespace_id != ns_id:
            return JSONResponse(
                {"error": "Forbidden: namespace context mismatch"},
                status_code=403,
            )
        agent_id_val = body.get("agent_id", "default")
        caller_ctx = NamespaceContext(namespace_id=ns_id, agent_id=agent_id_val)
        scopes_raw = body.get("scopes", [])
        scopes = [A2AScope.model_validate(s) for s in scopes_raw]
        req = A2AGrantRequest(
            target_namespace_id=body.get("target_namespace_id"),
            target_agent_id=body.get("target_agent_id"),
            scopes=scopes,
            expires_in_seconds=int(body.get("expires_in_seconds", 3600)),
        )
    except (KeyError, ValueError) as exc:
        return admin_error_response("Bad request parameters", exc, status_code=422)

    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            resp = await create_grant(conn, caller_ctx, req)
    except Exception as exc:
        return admin_error_response("Failed to create grant", exc, status_code=500)

    return JSONResponse(
        {
            "grant_id": str(resp.grant_id),
            "sharing_token": resp.sharing_token,
            "expires_at": resp.expires_at.isoformat(),
        },
        status_code=201,
    )


async def api_a2a_revoke_grant(request):
    """POST /api/a2a/grants/{grant_id}/revoke

    Revoke an active A2A grant. Only the owning namespace can revoke.

    Body (JSON):
      namespace_id   (str, UUID, required)
      agent_id       (str, optional)
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    grant_id_str = request.path_params.get("grant_id", "")
    try:
        grant_id = uuid.UUID(grant_id_str)
    except ValueError:
        return JSONResponse({"error": "grant_id is not a valid UUID"}, status_code=422)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError, TypeError):
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    from nce.a2a import revoke_grant
    from nce.auth import NamespaceContext

    try:
        ns_id = uuid.UUID(body["namespace_id"])
        ns_ctx = getattr(request.state, "namespace_ctx", None)
        if ns_ctx is None or ns_ctx.namespace_id != ns_id:
            return JSONResponse(
                {"error": "Forbidden: namespace context mismatch"},
                status_code=403,
            )
        agent_id_val = body.get("agent_id", "default")
        caller_ctx = NamespaceContext(namespace_id=ns_id, agent_id=agent_id_val)
    except (KeyError, ValueError) as exc:
        return admin_error_response("Bad request parameters", exc, status_code=422)

    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            revoked = await revoke_grant(conn, grant_id, caller_ctx)
    except Exception as exc:
        return admin_error_response("Revoke failed", exc, status_code=500)

    if not revoked:
        return JSONResponse(
            {
                "error": "Grant not found or not owned by this namespace",
                "grant_id": str(grant_id),
            },
            status_code=404,
        )

    return JSONResponse({"grant_id": str(grant_id), "revoked": True})


async def api_a2a_list_grants(request):
    """GET /api/a2a/grants?namespace_id=<uuid>[&include_inactive=true]

    List all A2A grants owned by the given namespace.
    Token hashes are never returned.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    ns_id_str = request.query_params.get("namespace_id", "")
    include_inactive = request.query_params.get("include_inactive", "false").lower() == "true"

    try:
        ns_id = uuid.UUID(ns_id_str)
        ns_ctx = getattr(request.state, "namespace_ctx", None)
        if ns_ctx is None or ns_ctx.namespace_id != ns_id:
            return JSONResponse(
                {"error": "Forbidden: namespace context mismatch"},
                status_code=403,
            )
    except ValueError:
        return JSONResponse(
            {"error": "namespace_id is required and must be a valid UUID"},
            status_code=422,
        )

    from nce.a2a import list_grants
    from nce.auth import NamespaceContext

    agent_id_val = request.query_params.get("agent_id", "default")
    caller_ctx = NamespaceContext(namespace_id=ns_id, agent_id=agent_id_val)

    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            grants = await list_grants(conn, caller_ctx, include_inactive=include_inactive)
    except Exception as exc:
        return admin_error_response("List grants failed", exc, status_code=500)

    return JSONResponse({"grants": grants, "total": len(grants)})
