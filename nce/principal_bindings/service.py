"""
nce/principal_bindings/service.py

C16 Principal Mapping (Wave A-6)
Database service and resolution helpers for principal bindings.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from starlette.requests import Request

from nce.db_utils import scoped_pg_session
from nce.principal_bindings.models import PrincipalBinding, PrincipalContext

log = logging.getLogger("nce.principal_bindings")

# In-memory registry for unit testing and fast-path lookups without live Postgres
_IN_MEMORY_BINDINGS: dict[tuple[UUID, str], PrincipalBinding] = {}


def register_test_binding(binding: PrincipalBinding) -> None:
    """Register an in-memory test binding."""
    _IN_MEMORY_BINDINGS[(binding.namespace_id, binding.principal_id)] = binding


def clear_test_bindings() -> None:
    """Clear in-memory test bindings."""
    _IN_MEMORY_BINDINGS.clear()


async def get_principal_binding(
    conn_or_pool: Any,
    namespace_id: UUID,
    principal_id: str,
) -> PrincipalBinding | None:
    """Query the principal_bindings table for a specific tenant and principal_id."""
    # Check in-memory fast-path first
    key = (namespace_id, principal_id)
    if key in _IN_MEMORY_BINDINGS:
        return _IN_MEMORY_BINDINGS[key]

    if conn_or_pool is None:
        return None

    query = """
        SELECT id, namespace_id, principal_id, tier, employee_id, customer_id, contractor_id,
               roles, metadata, created_at, updated_at
        FROM principal_bindings
        WHERE namespace_id = $1 AND principal_id = $2
    """

    try:
        # Determine if conn_or_pool is an asyncpg pool or single connection
        if hasattr(conn_or_pool, "acquire"):
            async with scoped_pg_session(conn_or_pool, namespace_id) as conn:
                row = await conn.fetchrow(query, namespace_id, principal_id)
        else:
            row = await conn_or_pool.fetchrow(query, namespace_id, principal_id)

        if not row:
            return None

        roles_raw = row["roles"]
        if isinstance(roles_raw, str):
            roles_val = tuple(json.loads(roles_raw))
        elif isinstance(roles_raw, (list, tuple)):
            roles_val = tuple(roles_raw)
        else:
            roles_val = ()

        meta_raw = row["metadata"]
        if isinstance(meta_raw, str):
            meta_val = json.loads(meta_raw)
        elif isinstance(meta_raw, dict):
            meta_val = meta_raw
        else:
            meta_val = {}

        return PrincipalBinding(
            id=row["id"],
            namespace_id=row["namespace_id"],
            principal_id=row["principal_id"],
            tier=row["tier"] or "employee",
            employee_id=row["employee_id"],
            customer_id=row["customer_id"],
            contractor_id=row["contractor_id"],
            roles=roles_val,
            metadata=meta_val,
            created_at=row["created_at"].isoformat() if row["created_at"] else None,
            updated_at=row["updated_at"].isoformat() if row["updated_at"] else None,
        )
    except Exception as exc:
        log.warning("get_principal_binding query failed: %s", exc)
        return None


async def upsert_principal_binding(
    conn_or_pool: Any,
    namespace_id: UUID,
    principal_id: str,
    *,
    tier: str = "employee",
    employee_id: str | None = None,
    customer_id: str | None = None,
    contractor_id: str | None = None,
    roles: list[str] | tuple[str, ...] = (),
    metadata: dict[str, Any] | None = None,
) -> PrincipalBinding:
    """Create or update a principal_binding row."""
    meta_dict = metadata or {}
    roles_list = list(roles)

    query = """
        INSERT INTO principal_bindings (
            namespace_id, principal_id, tier, employee_id, customer_id, contractor_id,
            roles, metadata, updated_at
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, NOW()
        )
        ON CONFLICT (namespace_id, principal_id) DO UPDATE SET
            tier = EXCLUDED.tier,
            employee_id = EXCLUDED.employee_id,
            customer_id = EXCLUDED.customer_id,
            contractor_id = EXCLUDED.contractor_id,
            roles = EXCLUDED.roles,
            metadata = EXCLUDED.metadata,
            updated_at = NOW()
        RETURNING id, namespace_id, principal_id, tier, employee_id, customer_id, contractor_id,
                  roles, metadata, created_at, updated_at
    """

    if conn_or_pool is not None:
        try:
            if hasattr(conn_or_pool, "acquire"):
                async with scoped_pg_session(conn_or_pool, namespace_id) as conn:
                    row = await conn.fetchrow(
                        query,
                        namespace_id,
                        principal_id,
                        tier,
                        employee_id,
                        customer_id,
                        contractor_id,
                        json.dumps(roles_list),
                        json.dumps(meta_dict),
                    )
            else:
                row = await conn_or_pool.fetchrow(
                    query,
                    namespace_id,
                    principal_id,
                    tier,
                    employee_id,
                    customer_id,
                    contractor_id,
                    json.dumps(roles_list),
                    json.dumps(meta_dict),
                )

            if row:
                binding = PrincipalBinding(
                    id=row["id"],
                    namespace_id=row["namespace_id"],
                    principal_id=row["principal_id"],
                    tier=row["tier"],
                    employee_id=row["employee_id"],
                    customer_id=row["customer_id"],
                    contractor_id=row["contractor_id"],
                    roles=tuple(roles_list),
                    metadata=meta_dict,
                    created_at=row["created_at"].isoformat() if row["created_at"] else None,
                    updated_at=row["updated_at"].isoformat() if row["updated_at"] else None,
                )
                _IN_MEMORY_BINDINGS[(namespace_id, principal_id)] = binding
                return binding
        except Exception as exc:
            log.warning("upsert_principal_binding DB query failed, falling back to memory: %s", exc)

    # In-memory fallback
    from uuid import uuid4

    binding = PrincipalBinding(
        id=uuid4(),
        namespace_id=namespace_id,
        principal_id=principal_id,
        tier=tier,
        employee_id=employee_id,
        customer_id=customer_id,
        contractor_id=contractor_id,
        roles=tuple(roles_list),
        metadata=meta_dict,
        created_at=None,
        updated_at=None,
    )
    _IN_MEMORY_BINDINGS[(namespace_id, principal_id)] = binding
    return binding


async def delete_principal_binding(
    conn_or_pool: Any,
    namespace_id: UUID,
    principal_id: str,
) -> bool:
    """Delete a principal_binding row for a tenant."""
    _IN_MEMORY_BINDINGS.pop((namespace_id, principal_id), None)
    if conn_or_pool is None:
        return True

    query = "DELETE FROM principal_bindings WHERE namespace_id = $1 AND principal_id = $2"
    try:
        if hasattr(conn_or_pool, "acquire"):
            async with scoped_pg_session(conn_or_pool, namespace_id) as conn:
                res = await conn.execute(query, namespace_id, principal_id)
        else:
            res = await conn_or_pool.execute(query, namespace_id, principal_id)
        return "DELETE 1" in res
    except Exception as exc:
        log.warning("delete_principal_binding failed: %s", exc)
        return False


def extract_principal_identity(
    request: Request,
) -> tuple[UUID | None, str | None, str]:
    """Extract namespace_id, principal_id, and tier from request context, headers, or query params.

    Tier is read exclusively from request.state's verified principal_kind
    (or its "employee" default). It is never taken from a header or query
    param: those are attacker-controlled and would let a caller overwrite
    an already-resolved verified tier with one of their own choosing.
    """
    ns_ctx = getattr(request.state, "namespace_ctx", None)
    ns_id: UUID | None = None
    principal_id: str | None = None
    tier: str = "employee"

    if ns_ctx:
        ns_id = getattr(ns_ctx, "namespace_id", None)
        principal_id = getattr(ns_ctx, "principal_id", None) or getattr(ns_ctx, "agent_id", None)
        tier = getattr(ns_ctx, "principal_kind", None) or "employee"

    if not ns_id:
        raw_ns = request.headers.get("X-NCE-Namespace-ID") or request.query_params.get(
            "namespace_id"
        )
        if raw_ns:
            try:
                ns_id = UUID(str(raw_ns).strip())
            except ValueError:
                pass

    if not principal_id:
        principal_id = (
            request.headers.get("X-NCE-Principal-ID")
            or request.headers.get("X-Principal-ID")
            or request.query_params.get("principal_id")
        )

    return ns_id, principal_id, tier


def resolve_principal_context_sync(request: Request) -> PrincipalContext:
    """Synchronously resolve PrincipalContext from request.state or in-memory registry."""
    cached = getattr(request.state, "principal_ctx", None)
    if isinstance(cached, PrincipalContext):
        return cached

    ns_id, principal_id, tier = extract_principal_identity(request)
    if not principal_id:
        ctx = PrincipalContext(
            principal_id="anonymous",
            tier=tier,
            employee_id=None,
            customer_id=None,
            contractor_id=None,
            roles=(),
            metadata={},
            namespace_id=ns_id,
            is_bound=False,
        )
        request.state.principal_ctx = ctx
        return ctx

    # Check in-memory bindings
    if ns_id:
        key = (ns_id, principal_id)
        if key in _IN_MEMORY_BINDINGS:
            b = _IN_MEMORY_BINDINGS[key]
            ctx = PrincipalContext(
                principal_id=b.principal_id,
                tier=b.tier,
                employee_id=b.employee_id,
                customer_id=b.customer_id,
                contractor_id=b.contractor_id,
                roles=b.roles,
                metadata=b.metadata,
                namespace_id=ns_id,
                is_bound=True,
            )
            request.state.principal_ctx = ctx
            return ctx

    # Unbound principal default: Gate requires empty identities and roles
    ctx = PrincipalContext(
        principal_id=principal_id,
        tier=tier,
        employee_id=None,
        customer_id=None,
        contractor_id=None,
        roles=(),
        metadata={},
        namespace_id=ns_id,
        is_bound=False,
    )
    request.state.principal_ctx = ctx
    return ctx


async def resolve_principal_context_async(
    request: Request,
    pool: Any = None,
) -> PrincipalContext:
    """Asynchronously resolve PrincipalContext, querying PostgreSQL if available."""
    cached = getattr(request.state, "principal_ctx", None)
    if isinstance(cached, PrincipalContext):
        return cached

    ns_id, principal_id, tier = extract_principal_identity(request)
    if not principal_id:
        ctx = PrincipalContext(
            principal_id="anonymous",
            tier=tier,
            employee_id=None,
            customer_id=None,
            contractor_id=None,
            roles=(),
            metadata={},
            namespace_id=ns_id,
            is_bound=False,
        )
        request.state.principal_ctx = ctx
        return ctx

    # Check in-memory fast-path
    if ns_id:
        key = (ns_id, principal_id)
        if key in _IN_MEMORY_BINDINGS:
            b = _IN_MEMORY_BINDINGS[key]
            ctx = PrincipalContext(
                principal_id=b.principal_id,
                tier=b.tier,
                employee_id=b.employee_id,
                customer_id=b.customer_id,
                contractor_id=b.contractor_id,
                roles=b.roles,
                metadata=b.metadata,
                namespace_id=ns_id,
                is_bound=True,
            )
            request.state.principal_ctx = ctx
            return ctx

    # Try database lookup
    pg_pool = pool
    if pg_pool is None and hasattr(request.app, "state"):
        engine = getattr(request.app.state, "engine", None)
        if engine and hasattr(engine, "pg_pool"):
            pg_pool = engine.pg_pool

    if ns_id and pg_pool:
        b = await get_principal_binding(pg_pool, ns_id, principal_id)
        if b:
            ctx = PrincipalContext(
                principal_id=b.principal_id,
                tier=b.tier,
                employee_id=b.employee_id,
                customer_id=b.customer_id,
                contractor_id=b.contractor_id,
                roles=b.roles,
                metadata=b.metadata,
                namespace_id=ns_id,
                is_bound=True,
            )
            request.state.principal_ctx = ctx
            return ctx

    # Unbound principal default: Gate requires empty identities and roles
    ctx = PrincipalContext(
        principal_id=principal_id,
        tier=tier,
        employee_id=None,
        customer_id=None,
        contractor_id=None,
        roles=(),
        metadata={},
        namespace_id=ns_id,
        is_bound=False,
    )
    request.state.principal_ctx = ctx
    return ctx


class _PrincipalResolverWrapper:
    """Provides dual support for both `await current_principal(request)` and `current_principal(request).field`."""

    def __init__(self, request: Request, pool: Any = None):
        self._request = request
        self._pool = pool
        self._sync_ctx = resolve_principal_context_sync(request)

    def __await__(self):
        return resolve_principal_context_async(self._request, self._pool).__await__()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sync_ctx, name)

    def __repr__(self) -> str:
        return repr(self._sync_ctx)


def current_principal(request: Request, pool: Any = None) -> Any:
    """Central helper resolving the calling principal's context.

    Can be awaited (`ctx = await current_principal(request)`) or accessed
    synchronously (`ctx = current_principal(request); id = ctx.employee_id`).
    Gate guarantee: unbound principals get is_bound=False, employee_id=None,
    roles=(), NEVER another user's rows.
    """
    cached = getattr(request.state, "principal_ctx", None)
    if isinstance(cached, PrincipalContext):
        return _PrincipalResolverWrapper(request, pool=pool)
    return _PrincipalResolverWrapper(request, pool=pool)
