"""Reserved, non-tenant namespaces -- the home for GLOBAL audit events.

Some audited operations are not owned by any tenant. A master/signing key
rotation is the clearest case: signing keys are process-global, but
``event_log.namespace_id`` is ``NOT NULL`` and FK-references ``namespaces``,
so a global security event had nowhere to land. Until migration 065 that meant
:func:`nce.admin_mcp_handlers.handle_rotate_signing_key` could only log a
WARNING -- and a log line is not an immutable record.

``audit_log`` was considered and rejected: it carries **zero triggers**, so its
rows are freely deletable. Writing the only record of a global security
operation into a mutable table is weaker than not writing it at all.
``event_log`` is WORM-protected and Merkle-chained.

The precedent for a reserved non-tenant row already exists: ``_global_legacy``
(see ``nce/schema.sql``) is seeded the same way. The difference is that
``_global_legacy`` holds real, tenant-attributable pre-RLS data and therefore
still belongs in tenant enumerations; ``_system`` never holds tenant data and
must never be handed to a client as if it were a tenant -- which is what
:data:`RESERVED_NON_TENANT_SLUGS` is for.

The row itself is seeded by ``nce/migrations/066_system_namespace.sql`` ONLY.
Nothing in this module creates it: a namespace that appears because some code
path happened to run is not a reserved namespace, it is a race.
"""

from __future__ import annotations

import uuid
from typing import Any

# Must stay byte-identical to the slug seeded by migration 065.
SYSTEM_NAMESPACE_SLUG = "_system"

#: Slugs in ``namespaces`` that are NOT tenants and must be excluded from
#: tenant enumerations, counts, and per-tenant sweeps.
RESERVED_NON_TENANT_SLUGS: frozenset[str] = frozenset({SYSTEM_NAMESPACE_SLUG})


class SystemNamespaceMissingError(RuntimeError):
    """The reserved system namespace row is absent from ``namespaces``.

    Raised rather than silently creating the row: the only sanctioned creator is
    migration 065. Seeing this means the migration has not been applied to this
    database (``scripts/apply_integration_schema.py`` applies ``schema.sql``
    then every migration in order).
    """


def is_tenant_namespace(slug: str) -> bool:
    """Return ``True`` when *slug* names a real tenant namespace."""
    return slug not in RESERVED_NON_TENANT_SLUGS


async def get_system_namespace_id(conn: Any) -> uuid.UUID:
    """Resolve the reserved system namespace's id, or raise.

    *conn* is an ``asyncpg.Connection``. This function never writes.
    """
    ns_id = await conn.fetchval(
        "SELECT id FROM namespaces WHERE slug = $1",
        SYSTEM_NAMESPACE_SLUG,
    )
    if ns_id is None:
        raise SystemNamespaceMissingError(
            f"Reserved namespace {SYSTEM_NAMESPACE_SLUG!r} is missing. "
            "Apply nce/migrations/066_system_namespace.sql -- global security "
            "events cannot be audited without it."
        )
    if isinstance(ns_id, uuid.UUID):
        return ns_id
    return uuid.UUID(str(ns_id))
