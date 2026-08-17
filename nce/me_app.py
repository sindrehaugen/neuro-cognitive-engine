"""
nce/me_app.py

Subject-scoped `/api/me/*` surface (consent-bound read/govern surface).
Requires JWT Bearer tokens to authenticate.
"""

import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from nce.atms import evaluate_atms_intervention, persist_atms_invalidation
from nce.auth import NamespaceContext
from nce.db_utils import scoped_pg_session
from nce.event_log import append_event
from nce.jwt_auth import JWTAuthMiddleware
from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.me_app")


@asynccontextmanager
async def me_lifespan(app: Starlette):
    """Lifespan context manager for me_app.

    Initializes and manages the lifetime of NCEEngine.
    """
    engine = NCEEngine()
    await engine.connect()
    app.state.engine = engine
    log.info("Me API: NCEEngine connected.")
    try:
        yield
    finally:
        await engine.disconnect()
        app.state.engine = None
        log.info("Me API: NCEEngine disconnected.")


async def get_me_memories(request: Request) -> JSONResponse:
    """GET /api/me/memories

    Retrieve memories scoped to the caller's namespace and agent.
    Optionally filters or checks namespace_id / agent_id parameters.
    """
    ns_ctx: NamespaceContext | None = getattr(request.state, "namespace_ctx", None)
    if not ns_ctx or ns_ctx.namespace_id is None:
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32005,
                    "message": "Unauthorized",
                    "data": {"reason": "missing_namespace_context"},
                },
                "id": None,
            },
            status_code=401,
        )

    ns_id: UUID = ns_ctx.namespace_id

    # 1. Enforce that if a namespace_id query param is supplied, it must match the token's namespace
    query_ns = request.query_params.get("namespace_id")
    if query_ns:
        try:
            query_ns_uuid = UUID(str(query_ns).strip())
        except ValueError:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32007,
                        "message": "Invalid namespace_id format",
                        "data": {"reason": "invalid_namespace_format"},
                    },
                    "id": None,
                },
                status_code=400,
            )
        if query_ns_uuid != ns_id:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32005,
                        "message": "Forbidden",
                        "data": {"reason": "cross-namespace request is denied"},
                    },
                    "id": None,
                },
                status_code=403,
            )

    # 2. Enforce that if an agent_id query param is supplied, it must match the token's agent
    query_agent = request.query_params.get("agent_id")
    if query_agent and query_agent.strip() != ns_ctx.agent_id:
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32005,
                    "message": "Forbidden",
                    "data": {"reason": "cross-agent request is denied"},
                },
                "id": None,
            },
            status_code=403,
        )

    # 3. Retrieve database connection via scoped_pg_session and execute RLS-scoped query
    engine: NCEEngine = request.app.state.engine
    async with scoped_pg_session(engine.pg_pool, ns_id) as conn:
        rows = await conn.fetch(
            "SELECT id, namespace_id, agent_id, memory_type, assertion_type, payload_ref, valid_from, valid_to "
            "FROM memories WHERE agent_id = $1",
            ns_ctx.agent_id,
        )
        return JSONResponse(
            [
                {
                    "id": str(row["id"]),
                    "namespace_id": str(row["namespace_id"]),
                    "agent_id": row["agent_id"],
                    "memory_type": row["memory_type"],
                    "assertion_type": row["assertion_type"],
                    "payload_ref": row["payload_ref"],
                    "valid_from": row["valid_from"].isoformat() if row["valid_from"] else None,
                    "valid_to": row["valid_to"].isoformat() if row["valid_to"] else None,
                }
                for row in rows
            ]
        )


async def get_me_profile(request: Request) -> JSONResponse:
    """GET /api/me/profile

    Retrieve a detailed profile of active beliefs (memories) for the caller's namespace and agent,
    including salience, confidence, last reinforced, source, and associated unresolved contradictions.
    """
    ns_ctx: NamespaceContext | None = getattr(request.state, "namespace_ctx", None)
    if not ns_ctx or ns_ctx.namespace_id is None:
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32005,
                    "message": "Unauthorized",
                    "data": {"reason": "missing_namespace_context"},
                },
                "id": None,
            },
            status_code=401,
        )

    ns_id: UUID = ns_ctx.namespace_id

    # Enforce namespace matching if passed as query parameter
    query_ns = request.query_params.get("namespace_id")
    if query_ns:
        try:
            query_ns_uuid = UUID(str(query_ns).strip())
        except ValueError:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32007,
                        "message": "Invalid namespace_id format",
                        "data": {"reason": "invalid_namespace_format"},
                    },
                    "id": None,
                },
                status_code=400,
            )
        if query_ns_uuid != ns_id:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32005,
                        "message": "Forbidden",
                        "data": {"reason": "cross-namespace request is denied"},
                    },
                    "id": None,
                },
                status_code=403,
            )

    # Enforce agent matching if passed as query parameter
    query_agent = request.query_params.get("agent_id")
    if query_agent and query_agent.strip() != ns_ctx.agent_id:
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32005,
                    "message": "Forbidden",
                    "data": {"reason": "cross-agent request is denied"},
                },
                "id": None,
            },
            status_code=403,
        )

    engine: NCEEngine = request.app.state.engine
    async with scoped_pg_session(engine.pg_pool, ns_id) as conn:
        # 1. Fetch active memories with salience information
        mem_rows = await conn.fetch(
            """
            SELECT m.id, m.namespace_id, m.agent_id, m.memory_type, m.assertion_type, m.payload_ref, m.valid_from, m.metadata, m.created_at,
                   COALESCE(ms.salience_score, 1.0) AS salience,
                   COALESCE(ms.updated_at, m.created_at) AS last_reinforced
            FROM memories m
            LEFT JOIN memory_salience ms ON m.id = ms.memory_id AND ms.agent_id = m.agent_id AND ms.namespace_id = m.namespace_id
            WHERE m.agent_id = $1 AND m.namespace_id = $2 AND m.valid_to IS NULL
            """,
            ns_ctx.agent_id,
            ns_id,
        )

        # 2. Fetch active contradictions in the namespace to associate with memories
        contra_rows = await conn.fetch(
            """
            SELECT id, memory_a_id, memory_b_id, confidence, detected_at, detection_path, signals, resolution
            FROM contradictions
            WHERE namespace_id = $1 AND resolution IS NULL
            """,
            ns_id,
        )

        # Index contradictions by memory ID
        contra_map: dict[UUID, list[dict]] = {}
        for c in contra_rows:
            signals = c["signals"]
            if isinstance(signals, str):
                try:
                    signals = json.loads(signals)
                except Exception:
                    signals = {}
            contra_data = {
                "id": str(c["id"]),
                "memory_a_id": str(c["memory_a_id"]),
                "memory_b_id": str(c["memory_b_id"]),
                "confidence": float(c["confidence"]),
                "detected_at": c["detected_at"].isoformat() if c["detected_at"] else None,
                "detection_path": c["detection_path"],
                "signals": signals,
                "resolution": c["resolution"],
            }
            contra_map.setdefault(c["memory_a_id"], []).append(contra_data)
            contra_map.setdefault(c["memory_b_id"], []).append(contra_data)

        beliefs = []
        for row in mem_rows:
            mem_id = row["id"]
            metadata = row["metadata"] or {}
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except Exception:
                    metadata = {}
            confidence = metadata.get("confidence", 1.0)
            try:
                confidence = float(confidence)
            except (ValueError, TypeError):
                confidence = 1.0

            source = metadata.get("source", row["payload_ref"])

            beliefs.append(
                {
                    "id": str(mem_id),
                    "namespace_id": str(row["namespace_id"]),
                    "agent_id": row["agent_id"],
                    "memory_type": row["memory_type"],
                    "assertion_type": row["assertion_type"],
                    "payload_ref": row["payload_ref"],
                    "valid_from": row["valid_from"].isoformat() if row["valid_from"] else None,
                    "metadata": metadata,
                    "salience": float(row["salience"]),
                    "confidence": confidence,
                    "last_reinforced": row["last_reinforced"].isoformat()
                    if row["last_reinforced"]
                    else None,
                    "source": source,
                    "contradictions": contra_map.get(mem_id, []),
                }
            )

        return JSONResponse(beliefs)


async def _pseudonymize_edit_graph(
    conn,
    namespace_id: UUID,
    entities: list,
    triplets: list,
) -> tuple[list, list]:
    """Pseudonymize caller-supplied entity/triplet label strings before they
    enter the immutable event_log via the edit path (VII.5 / WORM-content gate).

    Mirrors the main store_memory path, which only ever logs labels derived from
    PII-sanitized text. Here the labels are caller-supplied, so each label string
    is run through the namespace PII pipeline; the sanitized (pseudonymized or
    redacted) text replaces the raw value. Non-conforming inputs are dropped so a
    malformed payload cannot smuggle raw content past the sanitizer.
    """
    from nce.models import NamespacePIIConfig
    from nce.pii import process as pii_process

    pii_config = NamespacePIIConfig(namespace_id=str(namespace_id))
    ns_row = await conn.fetchrow("SELECT metadata FROM namespaces WHERE id = $1", namespace_id)
    if ns_row and ns_row["metadata"]:
        meta = ns_row["metadata"]
        if isinstance(meta, str):
            meta = json.loads(meta)
        if isinstance(meta, dict) and "pii" in meta:
            pii_config = NamespacePIIConfig(**{**meta["pii"], "namespace_id": str(namespace_id)})

    async def _sanitize(text) -> str:
        if not isinstance(text, str) or not text:
            return ""
        return (await pii_process(text, pii_config)).sanitized_text

    safe_entities: list = []
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        safe_entities.append({**ent, "label": await _sanitize(ent.get("label"))})

    safe_triplets: list = []
    for tri in triplets:
        if not isinstance(tri, dict):
            continue
        safe_triplets.append(
            {
                **tri,
                "subject_label": await _sanitize(tri.get("subject_label")),
                "object_label": await _sanitize(tri.get("object_label")),
            }
        )

    return safe_entities, safe_triplets


async def post_me_govern(request: Request) -> JSONResponse:
    """POST /api/me/govern

    Govern a memory: edit, downweight, pin, or retract (which triggers the ATMS cascade).
    """
    ns_ctx: NamespaceContext | None = getattr(request.state, "namespace_ctx", None)
    if not ns_ctx or ns_ctx.namespace_id is None:
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32005,
                    "message": "Unauthorized",
                    "data": {"reason": "missing_namespace_context"},
                },
                "id": None,
            },
            status_code=401,
        )

    ns_id: UUID = ns_ctx.namespace_id

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32700,
                    "message": "Parse error",
                    "data": {"reason": "invalid_json_body"},
                },
                "id": None,
            },
            status_code=400,
        )

    memory_id_str = body.get("memory_id")
    action = body.get("action")

    if not memory_id_str or not action:
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32602,
                    "message": "Invalid params",
                    "data": {"reason": "memory_id and action are required"},
                },
                "id": None,
            },
            status_code=400,
        )

    try:
        memory_id = UUID(str(memory_id_str).strip())
    except ValueError:
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32602,
                    "message": "Invalid params",
                    "data": {"reason": "memory_id must be a valid UUID"},
                },
                "id": None,
            },
            status_code=400,
        )

    valid_actions = {"edit", "downweight", "pin", "retract"}
    if action not in valid_actions:
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32602,
                    "message": "Invalid params",
                    "data": {"reason": f"action must be one of {sorted(valid_actions)}"},
                },
                "id": None,
            },
            status_code=400,
        )

    engine: NCEEngine = request.app.state.engine
    async with scoped_pg_session(engine.pg_pool, ns_id) as conn:
        async with conn.transaction():
            # Verify the memory exists, is scoped to RLS, and is not already soft-deleted
            memory = await conn.fetchrow(
                "SELECT id, assertion_type, payload_ref, metadata FROM memories WHERE id = $1 AND namespace_id = $2 AND valid_to IS NULL",
                memory_id,
                ns_id,
            )
            if not memory:
                return JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "error": {
                            "code": -32004,
                            "message": "Method not found",
                            "data": {"reason": "memory not found or already deleted"},
                        },
                        "id": None,
                    },
                    status_code=404,
                )

            if action == "edit":
                # Edit metadata / assertion_type / payload_ref
                new_assertion_type = body.get("assertion_type", memory["assertion_type"])
                new_payload_ref = body.get("payload_ref", memory["payload_ref"])

                # Check format of new_payload_ref
                if not re.match(r"^[a-f0-9]{24}$", new_payload_ref):
                    return JSONResponse(
                        {
                            "jsonrpc": "2.0",
                            "error": {
                                "code": -32602,
                                "message": "Invalid params",
                                "data": {"reason": "payload_ref must be a 24-character hex string"},
                            },
                            "id": None,
                        },
                        status_code=400,
                    )

                metadata = memory["metadata"] or {}
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except Exception:
                        metadata = {}
                new_metadata = dict(metadata)
                if isinstance(body.get("metadata"), dict):
                    new_metadata.update(body["metadata"])

                await conn.execute(
                    """
                    UPDATE memories
                    SET assertion_type = $1,
                        payload_ref = $2,
                        metadata = $3::jsonb
                    WHERE id = $4 AND namespace_id = $5 AND valid_to IS NULL
                    """,
                    new_assertion_type,
                    new_payload_ref,
                    json.dumps(new_metadata),
                    memory_id,
                    ns_id,
                )

                # VII.5 / WORM-content gate: this is a SECOND writer into the
                # immutable event_log. Unlike the main store_memory path, the
                # entities/triplets here are caller-supplied metadata that have
                # NOT been through the PII pipeline. Pseudonymize their label
                # strings (mirroring the main path's graph-extract-on-sanitized
                # approach) so no raw PII can be injected into the WORM log.
                safe_entities, safe_triplets = await _pseudonymize_edit_graph(
                    conn,
                    ns_id,
                    new_metadata.get("entities", []),
                    new_metadata.get("triplets", []),
                )

                await append_event(
                    conn=conn,
                    namespace_id=ns_id,
                    agent_id=ns_ctx.agent_id,
                    event_type="store_memory",
                    params={
                        "saga_id": str(uuid.uuid4()),
                        "memory_id": str(memory_id),
                        "payload_ref": new_payload_ref,
                        "assertion_type": new_assertion_type,
                        "entities": safe_entities,
                        "triplets": safe_triplets,
                        "action": "edit",
                    },
                    result_summary={"status": "success", "edited": True},
                )
                return JSONResponse({"status": "success", "action": "edit"})

            elif action == "downweight":
                factor = body.get("factor", 0.2)
                try:
                    factor = float(factor)
                except (ValueError, TypeError):
                    return JSONResponse(
                        {
                            "jsonrpc": "2.0",
                            "error": {
                                "code": -32602,
                                "message": "Invalid params",
                                "data": {"reason": "factor must be a float"},
                            },
                            "id": None,
                        },
                        status_code=400,
                    )
                factor = max(0.0, min(1.0, factor))

                await conn.execute(
                    """
                    INSERT INTO memory_salience (memory_id, agent_id, namespace_id, salience_score, updated_at, access_count)
                    VALUES ($1::uuid, $2, $3::uuid, GREATEST(0.0, 1.0 - $4::real), NOW(), 1)
                    ON CONFLICT (memory_id, agent_id) DO UPDATE
                        SET salience_score = GREATEST(0.0, memory_salience.salience_score - $4::real),
                            updated_at = NOW(),
                            access_count = memory_salience.access_count + 1
                    """,
                    memory_id,
                    ns_ctx.agent_id,
                    ns_id,
                    factor,
                )

                await append_event(
                    conn=conn,
                    namespace_id=ns_id,
                    agent_id=ns_ctx.agent_id,
                    event_type="boost_memory",
                    params={"memory_id": str(memory_id), "factor": -factor},
                    result_summary={"status": "success", "action": "downweight"},
                )
                return JSONResponse({"status": "success", "action": "downweight"})

            elif action == "pin":
                await conn.execute(
                    """
                    INSERT INTO memory_salience (memory_id, agent_id, namespace_id, salience_score, updated_at, access_count)
                    VALUES ($1::uuid, $2, $3::uuid, 1.0, NOW(), 1)
                    ON CONFLICT (memory_id, agent_id) DO UPDATE
                        SET salience_score = 1.0,
                            updated_at = NOW(),
                            access_count = memory_salience.access_count + 1
                    """,
                    memory_id,
                    ns_ctx.agent_id,
                    ns_id,
                )

                metadata = memory["metadata"] or {}
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except Exception:
                        metadata = {}
                meta = dict(metadata)
                meta["pinned"] = True

                await conn.execute(
                    "UPDATE memories SET metadata = $1::jsonb WHERE id = $2 AND namespace_id = $3 AND valid_to IS NULL",
                    json.dumps(meta),
                    memory_id,
                    ns_id,
                )

                await append_event(
                    conn=conn,
                    namespace_id=ns_id,
                    agent_id=ns_ctx.agent_id,
                    event_type="boost_memory",
                    params={"memory_id": str(memory_id), "factor": 1.0},
                    result_summary={"status": "success", "action": "pin"},
                )
                return JSONResponse({"status": "success", "action": "pin"})

            else:  # action == "retract"
                # 1. Soft-delete the memory itself
                await conn.execute(
                    "UPDATE memories SET valid_to = now() WHERE id = $1 AND namespace_id = $2 AND valid_to IS NULL",
                    memory_id,
                    ns_id,
                )

                # 2. Append forget_memory event for WORM traceability
                await append_event(
                    conn=conn,
                    namespace_id=ns_id,
                    agent_id=ns_ctx.agent_id,
                    event_type="forget_memory",
                    params={"memory_id": str(memory_id)},
                    result_summary={"status": "success", "action": "retract"},
                )

                # 3. Topology / causal graph cascade
                cascade_set = {str(memory_id)}
                topo_cascade = await evaluate_atms_intervention(conn, ns_id, str(memory_id))
                cascade_set.update(topo_cascade)

                # 4. Transitive derived_from memory dependents cascade
                max_cascade = 100
                todo = [str(memory_id)]
                visited = {str(memory_id)}
                while todo and len(visited) < max_cascade:
                    current = todo.pop()
                    dep_rows = await conn.fetch(
                        """
                        SELECT id FROM memories
                        WHERE namespace_id = $1::uuid
                          AND (derived_from @> jsonb_build_array($2::text)
                               OR derived_from @> jsonb_build_array($2::uuid))
                          AND valid_to IS NULL
                        """,
                        ns_id,
                        current,
                    )
                    for r in dep_rows:
                        dep_id = str(r["id"])
                        if dep_id not in visited:
                            visited.add(dep_id)
                            todo.append(dep_id)
                            if len(visited) >= max_cascade:
                                break

                cascade_set.update(visited)

                # 5. Persist soft-deletions of all cascades in the database
                await persist_atms_invalidation(conn, ns_id, cascade_set)

                # 6. Log the atms_cascade event (using a sentinel contradiction_id)
                sentinel_contradiction_id = str(UUID(int=0))
                await append_event(
                    conn=conn,
                    namespace_id=ns_id,
                    agent_id=ns_ctx.agent_id,
                    event_type="atms_cascade",
                    params={
                        "contradiction_id": sentinel_contradiction_id,
                        "invalidated_memory_id": str(memory_id),
                        "invalidated_ids": sorted(list(cascade_set)),
                    },
                    result_summary={
                        "status": "success",
                        "cascade_count": len(cascade_set),
                        "action": "retract",
                    },
                )
                return JSONResponse(
                    {"status": "success", "action": "retract", "cascade_count": len(cascade_set)}
                )


async def get_me_dsar_export(request: Request) -> JSONResponse:
    """GET /api/me/dsar/export

    Retrieve all data associated with the subject (namespace and agent) including decrypted raw payloads.
    """
    ns_ctx: NamespaceContext | None = getattr(request.state, "namespace_ctx", None)
    if not ns_ctx or ns_ctx.namespace_id is None:
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32005,
                    "message": "Unauthorized",
                    "data": {"reason": "missing_namespace_context"},
                },
                "id": None,
            },
            status_code=401,
        )

    ns_id: UUID = ns_ctx.namespace_id

    # Enforce namespace matching if passed as query parameter
    query_ns = request.query_params.get("namespace_id")
    if query_ns:
        try:
            query_ns_uuid = UUID(str(query_ns).strip())
        except ValueError:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32007,
                        "message": "Invalid namespace_id format",
                        "data": {"reason": "invalid_namespace_format"},
                    },
                    "id": None,
                },
                status_code=400,
            )
        if query_ns_uuid != ns_id:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32005,
                        "message": "Forbidden",
                        "data": {"reason": "cross-namespace request is denied"},
                    },
                    "id": None,
                },
                status_code=403,
            )

    # Enforce agent matching if passed as query parameter
    query_agent = request.query_params.get("agent_id")
    if query_agent and query_agent.strip() != ns_ctx.agent_id:
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32005,
                    "message": "Forbidden",
                    "data": {"reason": "cross-agent request is denied"},
                },
                "id": None,
            },
            status_code=403,
        )

    engine: NCEEngine = request.app.state.engine
    async with scoped_pg_session(engine.pg_pool, ns_id) as conn:
        # Fetch all memories (active and soft-deleted) for this agent
        mem_rows = await conn.fetch(
            """
            SELECT m.id, m.namespace_id, m.agent_id, m.memory_type, m.assertion_type, m.payload_ref, 
                   m.valid_from, m.valid_to, m.metadata, m.created_at, m.wrapped_dek,
                   COALESCE(ms.salience_score, 1.0) AS salience,
                   COALESCE(ms.updated_at, m.created_at) AS last_reinforced
            FROM memories m
            LEFT JOIN memory_salience ms ON m.id = ms.memory_id AND ms.agent_id = m.agent_id AND ms.namespace_id = m.namespace_id
            WHERE m.agent_id = $1 AND m.namespace_id = $2
            """,
            ns_ctx.agent_id,
            ns_id,
        )

        # Fetch active contradictions in the namespace
        contra_rows = await conn.fetch(
            """
            SELECT id, memory_a_id, memory_b_id, confidence, detected_at, detection_path, signals, resolution
            FROM contradictions
            WHERE namespace_id = $1
            """,
            ns_id,
        )

    # Fetch MongoDB payloads
    payload_refs = [r["payload_ref"] for r in mem_rows if r["payload_ref"]]

    mongo_payloads = {}
    if payload_refs and engine.mongo_client is not None:
        from bson import ObjectId

        from nce.db_utils import scoped_mongo_session

        oids = []
        for ref in payload_refs:
            try:
                oids.append(ObjectId(ref))
            except Exception:
                pass

        async with scoped_mongo_session(engine.mongo_client, ns_id) as s_db:
            cursor = s_db.episodes.find({"_id": {"$in": oids}})
            async for doc in cursor:
                mongo_payloads[str(doc["_id"])] = doc

    # Map contradictions to memories
    contra_map: dict[UUID, list[dict]] = {}
    for c in contra_rows:
        signals = c["signals"]
        if isinstance(signals, str):
            try:
                signals = json.loads(signals)
            except Exception:
                signals = {}
        contra_data = {
            "id": str(c["id"]),
            "memory_a_id": str(c["memory_a_id"]),
            "memory_b_id": str(c["memory_b_id"]),
            "confidence": float(c["confidence"]),
            "detected_at": c["detected_at"].isoformat() if c["detected_at"] else None,
            "detection_path": c["detection_path"],
            "signals": signals,
            "resolution": c["resolution"],
        }
        contra_map.setdefault(c["memory_a_id"], []).append(contra_data)
        contra_map.setdefault(c["memory_b_id"], []).append(contra_data)

    from nce.envelope import maybe_decrypt_raw_data

    beliefs = []
    for row in mem_rows:
        mem_id = row["id"]
        metadata = row["metadata"] or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}
        confidence = metadata.get("confidence", 1.0)
        try:
            confidence = float(confidence)
        except (ValueError, TypeError):
            confidence = 1.0

        source = metadata.get("source", row["payload_ref"])

        # Fetch and decrypt raw payload if present
        raw_content = None
        payload_ref = row["payload_ref"]
        if payload_ref and payload_ref in mongo_payloads:
            doc = mongo_payloads[payload_ref]
            raw_data = doc.get("raw_data")
            wrapped = row["wrapped_dek"]
            if raw_data is not None:
                try:
                    raw_content = maybe_decrypt_raw_data(
                        raw_data, bytes(wrapped) if wrapped is not None else None
                    )
                except Exception as e:
                    log.warning("Failed to decrypt raw data for memory %s: %s", mem_id, e)
                    raw_content = "[Decryption Error]"

        beliefs.append(
            {
                "id": str(mem_id),
                "namespace_id": str(row["namespace_id"]),
                "agent_id": row["agent_id"],
                "memory_type": row["memory_type"],
                "assertion_type": row["assertion_type"],
                "payload_ref": payload_ref,
                "valid_from": row["valid_from"].isoformat() if row["valid_from"] else None,
                "valid_to": row["valid_to"].isoformat() if row["valid_to"] else None,
                "metadata": metadata,
                "salience": float(row["salience"]),
                "confidence": confidence,
                "last_reinforced": row["last_reinforced"].isoformat()
                if row["last_reinforced"]
                else None,
                "source": source,
                "content": raw_content,
                "contradictions": contra_map.get(mem_id, []),
            }
        )

    return JSONResponse(
        {
            "namespace_id": str(ns_id),
            "agent_id": ns_ctx.agent_id,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "beliefs": beliefs,
        }
    )


async def post_me_dsar_erase(request: Request) -> JSONResponse:
    """POST /api/me/dsar/erase

    Provably erase all memories associated with the subject (namespace and agent) and return deletion receipts.
    """
    ns_ctx: NamespaceContext | None = getattr(request.state, "namespace_ctx", None)
    if not ns_ctx or ns_ctx.namespace_id is None:
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32005,
                    "message": "Unauthorized",
                    "data": {"reason": "missing_namespace_context"},
                },
                "id": None,
            },
            status_code=401,
        )

    ns_id: UUID = ns_ctx.namespace_id

    # Enforce namespace matching if passed as query parameter
    query_ns = request.query_params.get("namespace_id")
    if query_ns:
        try:
            query_ns_uuid = UUID(str(query_ns).strip())
        except ValueError:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32007,
                        "message": "Invalid namespace_id format",
                        "data": {"reason": "invalid_namespace_format"},
                    },
                    "id": None,
                },
                status_code=400,
            )
        if query_ns_uuid != ns_id:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32005,
                        "message": "Forbidden",
                        "data": {"reason": "cross-namespace request is denied"},
                    },
                    "id": None,
                },
                status_code=403,
            )

    # Enforce agent matching if passed as query parameter
    query_agent = request.query_params.get("agent_id")
    if query_agent and query_agent.strip() != ns_ctx.agent_id:
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32005,
                    "message": "Forbidden",
                    "data": {"reason": "cross-agent request is denied"},
                },
                "id": None,
            },
            status_code=403,
        )

    engine: NCEEngine = request.app.state.engine

    # Fetch all memories that are not yet shredded
    async with scoped_pg_session(engine.pg_pool, ns_id) as conn:
        rows = await conn.fetch(
            """
            SELECT id FROM memories 
            WHERE agent_id = $1 AND namespace_id = $2 
              AND (wrapped_dek IS NOT NULL OR content_fts IS NOT NULL OR embedding IS NOT NULL)
            """,
            ns_ctx.agent_id,
            ns_id,
        )

    receipts = []
    errors = []

    for r in rows:
        mem_id = str(r["id"])
        try:
            shred_result = await engine.shred_memory(mem_id, str(ns_id), ns_ctx.agent_id)
            if shred_result.get("status") == "success":
                receipts.append(shred_result.get("receipt"))
            else:
                errors.append({"memory_id": mem_id, "error": "unknown_shred_failure"})
        except Exception as e:
            log.error("Failed to shred memory %s in DSAR erasure: %s", mem_id, e)
            errors.append({"memory_id": mem_id, "error": str(e)})

    return JSONResponse(
        {
            "status": "success",
            "namespace_id": str(ns_id),
            "agent_id": ns_ctx.agent_id,
            "erased_at": datetime.now(timezone.utc).isoformat(),
            "shredded_count": len(receipts),
            "receipts": receipts,
            "errors": errors,
        }
    )


app = Starlette(
    debug=False,
    lifespan=me_lifespan,
    middleware=[
        Middleware(
            JWTAuthMiddleware,
            protected_prefix="/api/me",
            expected_audience=None,
        ),
    ],
    routes=[
        Route("/api/me/memories", endpoint=get_me_memories, methods=["GET"]),
        Route("/api/me/profile", endpoint=get_me_profile, methods=["GET"]),
        Route("/api/me/govern", endpoint=post_me_govern, methods=["POST"]),
        Route("/api/me/profile/govern", endpoint=post_me_govern, methods=["POST"]),
        Route("/api/me/dsar/export", endpoint=get_me_dsar_export, methods=["GET"]),
        Route("/api/me/dsar/erase", endpoint=post_me_dsar_erase, methods=["POST"]),
    ],
)
