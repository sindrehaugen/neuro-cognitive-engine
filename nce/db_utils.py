"""
Shared database utilities for NCE.

Extracted to break circular imports and centralise security-relevant
session management (scoped_pg_session).
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Final
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
from asyncpg.pool import PoolConnectionProxy as _PoolConnProxy  # type: ignore[import-untyped]

from nce.observability import SCOPED_SESSION_LATENCY

log = logging.getLogger(__name__)

# Pool checkout timeout — prevents indefinite event-loop stall on exhaustion (FIX-010).
POOL_ACQUIRE_TIMEOUT: Final[float] = 10.0
DEFAULT_IDLE_IN_TX_TIMEOUT_MS: Final[int] = 30_000

# Cached pgvector HNSW iterative scan capability flag
_HNSW_ITERATIVE_SCAN_SUPPORTED: bool | None = None
_REAL_AUTH_SET_NAMESPACE_CONTEXT: Any = None


async def enable_hnsw_iterative_scan(conn: Any) -> bool:
    """Enable pgvector relaxed_order iterative scan for filtered vector queries.

    Uses cached capability detection so unsupported PostgreSQL versions/mock connections
    safely degrade without repeated failed roundtrips.
    """
    global _HNSW_ITERATIVE_SCAN_SUPPORTED
    if _HNSW_ITERATIVE_SCAN_SUPPORTED is False:
        return False

    if not hasattr(conn, "execute") or not callable(conn.execute):
        return False

    try:
        await conn.execute("SET LOCAL hnsw.iterative_scan = 'relaxed_order'")
        _HNSW_ITERATIVE_SCAN_SUPPORTED = True
        return True
    except (asyncpg.UndefinedObjectError, asyncpg.PostgresError) as exc:
        exc_str = str(exc).lower()
        if "unrecognized configuration parameter" in exc_str or "hnsw.iterative_scan" in exc_str:
            _HNSW_ITERATIVE_SCAN_SUPPORTED = False
        return False
    except Exception:
        return False


# Every production use of ``unmanaged_pg_connection`` must register a stable site id
# here after security review (global tables / DDL / legacy non-tenant paths only).
UNMANAGED_PG_AUDITED_SITES: Final[frozenset[str]] = frozenset(
    {
        "cron.consolidation.namespaces_scan",
        "cron.decay_prune",
        "cron.partition_maintenance",
        "cron.saga_recovery.list_stuck",
        "cron.saga_recovery.list_recovery_needed",
        "cron.saga_recovery.mark_recovery_needed",
        "cron.saga_recovery.mark_completed_no_memory",
        "tasks.code_indexing.legacy_no_namespace",
        "cron.d365_sync.namespace_scan",
        "cron.d365_sync.update_stats",
        "cron.d365_weekly_sync.namespace_scan",
        "cron.d365_weekly_sync.update_stats",
        "cron.d365_netbox_bridge.namespace_scan",
        "cron.chain_verify.namespace_scan",
        "cron.actor_trust.namespace_scan",
        "reembedding.aspects.backfill",
        # Batch 124: tamper-anchor tick — reads namespace list + per-namespace chain head.
        "cron.anchor.namespace_scan",
        "cron.anchor.head_read",
        # TDL-10 (realignment): product-EOL watcher tick — reads the namespaces list only;
        # same shape/blast as the other ``*.namespace_scan`` global reads above.
        "cron.product_eol_watcher.namespace_scan",
        # Batch 109 (M3.W5): agreements coverage watcher tick — reads the namespaces
        # list only (agreements.enabled opt-in filter); same shape/blast as the other
        # ``*.namespace_scan`` global reads above.
        "cron.agreements_coverage_watcher.namespace_scan",
        # Batch 124 (M8.W9): economy recurring-revenue recognition tick — reads the
        # namespaces list only (economy.enabled opt-in filter); same shape/blast as
        # the other ``*.namespace_scan`` global reads above.
        "cron.economy_recurring_recognition.namespace_scan",
        # Batch 125 (M8.W10): economy contract-renewal watcher tick — reads the
        # namespaces list only (economy.enabled opt-in filter); same shape/blast as
        # the other ``*.namespace_scan`` global reads above.
        "cron.economy_contract_renewal_watcher.namespace_scan",
        # B134b (M11.W6b): inventory stock watcher tick — reads the namespaces list
        # only (no opt-in filter, scans all namespaces); same shape/blast as the
        # other ``*.namespace_scan`` global reads above.
        "cron.inventory_stock_watcher.namespace_scan",
        # Wave A-1 (M9.W5): assets telemetry watcher tick -- scans active telemetry devices
        "cron.assets_telemetry.scan",
        # Wave A-3: assets warranty/EOL watcher tick -- scans active namespaces
        "cron.assets_warranty_eol.scan",
        # Wave HR-1 (break-5a): HR cert expiry watcher tick -- scans active namespaces
        "cron.hr_cert_expiry_watcher.namespace_scan",
        # Wave V-2 (break-5a): Vendors cert expiry watcher tick -- scans active namespaces
        "cron.vendors_cert_expiry_watcher.namespace_scan",
        # Wave SU-2: Support SLA breach watcher tick -- scans active namespaces
        "cron.support_sla_watcher.namespace_scan",
        # Wave HR-5: HR compliance deadline watcher tick -- scans active namespaces
        "cron.hr_compliance_watcher.namespace_scan",
        # Wave S-4: Sales stalled deal watcher tick -- scans active namespaces
        "cron.sales_stalled_deal_watcher.namespace_scan",
        # Wave A-3: Reminders watcher tick -- scans active namespaces
        "cron.reminders_watcher.namespace_scan",
        # Wave F-15: pricing_fx_rates is a GLOBAL table (migration 092,
        # same reasoning as product_catalog) -- an exchange rate is not
        # tenant data. Persist and load both read/write it with no
        # namespace_id to scope against.
        "pricing.fx.persist",
        "pricing.fx.load",
        # v3.7.0 Day-2 Ops global control-plane tables (backup_leases, backup_manifests, ops_quarantine, governance).
        "ops.backup.control_plane",
        "ops.quarantine.control_plane",
        "ops.governance.control_plane",
    }
)


def resolve_ops_dsn() -> str:
    """Return the PostgreSQL DSN for Day-2 operational tooling (backup, drill, quarantine).

    Precedence:
      1. ``NCE_GC_DSN`` (explicit cfg override or live env / *_FILE)
      2. ``NCE_MIGRATION_PG_DSN`` (explicit cfg override or live env / *_FILE)
      3. ``PG_DSN`` / ``DATABASE_URL`` (explicit cfg override or live env / *_FILE)
      4. Import-time ``cfg`` attributes fallback.
    """
    from nce.config import cfg, secret_env

    cfg_dict = getattr(cfg, "__dict__", {})

    def _pick(key: str, *, allow_fallback_attr: bool = True) -> str:
        live = (secret_env(key) or "").strip()
        if live:
            return live
        if key in cfg_dict:
            cfg_val = str(cfg_dict[key] or "").strip()
            if cfg_val:
                return cfg_val
        if allow_fallback_attr:
            return str(getattr(cfg, key, "") or "").strip()
        return ""

    # Check live/explicit NCE_GC_DSN first; only trust cfg.NCE_GC_DSN without live env
    # when NCE_MIGRATION_PG_DSN is unset (because config.py aliases NCE_GC_DSN -> PG_DSN at import).
    gc_explicit = _pick("NCE_GC_DSN", allow_fallback_attr=False)
    if gc_explicit:
        return gc_explicit

    mig_dsn = _pick("NCE_MIGRATION_PG_DSN", allow_fallback_attr=True)
    if mig_dsn:
        return mig_dsn

    pg_live = (
        _pick("PG_DSN", allow_fallback_attr=False) or (secret_env("DATABASE_URL") or "").strip()
    )
    if pg_live:
        return pg_live

    return (
        str(getattr(cfg, "NCE_GC_DSN", "") or "").strip()
        or str(getattr(cfg, "PG_DSN", "") or "").strip()
    )


def resolve_pg_admin_dsn() -> str:
    """Return the highest-privilege PG DSN available for dump/restore/ephemeral drill DB DDL.

    Precedence:
      1. ``NCE_MIGRATION_PG_DSN`` (superuser / CREATEDB owner role for pg_dump/CREATE DATABASE)
      2. ``resolve_ops_dsn()`` fallback.
    """
    from nce.config import cfg, secret_env

    cfg_dict = getattr(cfg, "__dict__", {})
    live = (secret_env("NCE_MIGRATION_PG_DSN") or "").strip()
    if live:
        return live
    if "NCE_MIGRATION_PG_DSN" in cfg_dict:
        cfg_val = str(cfg_dict["NCE_MIGRATION_PG_DSN"] or "").strip()
        if cfg_val:
            return cfg_val
    attr_val = str(getattr(cfg, "NCE_MIGRATION_PG_DSN", "") or "").strip()
    if attr_val:
        return attr_val
    return resolve_ops_dsn()


async def create_guarded_pg_pool(
    dsn: str,
    *,
    min_size: int = 1,
    max_size: int = 4,
    command_timeout: float = 30.0,
    idle_in_transaction_timeout_ms: int = DEFAULT_IDLE_IN_TX_TIMEOUT_MS,
    server_settings: dict[str, str] | None = None,
    **kwargs: Any,
) -> asyncpg.Pool:
    """Create a bounded asyncpg pool with idle-in-transaction session protection."""
    from nce.config import cfg

    cfg_max = int(getattr(cfg, "PG_MAX_POOL", 20) or 20)
    bounded_max = max(1, min(int(max_size), max(1, cfg_max)))
    bounded_min = max(1, min(int(min_size), bounded_max))

    merged_settings = {
        "lock_timeout": "10s",
        "idle_in_transaction_session_timeout": str(max(1000, int(idle_in_transaction_timeout_ms))),
    }
    if server_settings:
        merged_settings.update(server_settings)

    return await asyncpg.create_pool(
        dsn,
        min_size=bounded_min,
        max_size=bounded_max,
        command_timeout=command_timeout,
        server_settings=merged_settings,
        **kwargs,
    )


def resolve_worker_dsn() -> str:
    """Return the DSN background maintenance workers must connect with (R4 / VI.4).

    Garbage-collection and re-embedding workers should authenticate as a
    *distinct, least-privilege* principal (provisioned as ``nce_gc``) rather
    than reusing the application role. The selection contract is:

    * ``NCE_GC_DSN`` set → use it (the worker principal, distinct from the app).
    * ``NCE_GC_DSN`` unset → fall back to ``PG_DSN`` (the app role) so existing
      deployments keep working unchanged (backward-compatible default).

    Resolving from config (rather than reading ``cfg.PG_DSN`` directly at the
    worker connect site) is what lets a deployment grant the workers their own
    credentials without the application pool ever holding ``BYPASSRLS``.

    The returned string is a secret — callers must never log it in cleartext
    (use ``config.redact_secrets_in_text``) nor return it from an endpoint.
    """
    from nce.config import cfg

    return cfg.NCE_GC_DSN


def worker_dsn_is_segregated() -> bool:
    """True when workers connect as a principal distinct from the app role.

    Equivalent to "``NCE_GC_DSN`` resolved to something other than ``PG_DSN``".
    When False, workers share the app DSN (the safe, backward-compatible
    fallback) — the app role still never gains ``BYPASSRLS`` either way; that
    attribute is a property of the *role* the DSN authenticates as, granted at
    provisioning time, not of these workers.
    """
    from nce.config import cfg

    return bool(cfg.NCE_GC_DSN) and cfg.NCE_GC_DSN != cfg.PG_DSN


def assert_namespace_filter(sql: str, params: list | tuple, namespace_id: str | UUID) -> None:
    """Assert that a ``memory_embeddings`` query carries an explicit namespace predicate.

    ``memory_embeddings`` rows are separated from other tenants only by a join
    through ``memories.namespace_id``.  RLS covers the ``memories`` table but
    the embedding table itself is not namespace-scoped at the DB level — so a
    query that loses the WHERE clause in a refactor would silently become a
    cross-tenant leak.  This guard provides a cheap structural defence-in-depth:
    it checks that (a) the rendered SQL mentions ``namespace_id`` and (b) the
    caller's ``namespace_id`` value actually appears in the bind-parameter list.

    NOTE: ``kg_node_embeddings`` carries ``namespace_id`` and enforced RLS at
    the PostgreSQL layer (migration 040); ``assert_namespace_filter`` remains
    focused on ``memory_embeddings`` join predicates.

    Raises:
        AssertionError: when the SQL text does not contain ``namespace_id`` or
            the resolved namespace UUID is not bound as a parameter.
    """
    sql_lower = sql.lower()
    if "namespace_id" not in sql_lower:
        raise AssertionError(
            "assert_namespace_filter: memory_embeddings query is missing a "
            "namespace_id predicate in the SQL text. Add an explicit WHERE / JOIN "
            "condition so cross-tenant vector leakage is structurally impossible."
        )

    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    # Check the bound parameters list — the UUID must appear as UUID or str form.
    ns_str = str(ns_uuid)
    for p in params:
        if isinstance(p, UUID) and p == ns_uuid:
            return
        if isinstance(p, str) and p == ns_str:
            return
    raise AssertionError(
        f"assert_namespace_filter: namespace_id '{ns_str}' is referenced in the "
        "SQL text but is not bound as a query parameter. Pass it via a $N "
        "placeholder to ensure tenant isolation is query-layer enforced."
    )


@asynccontextmanager
async def unmanaged_pg_connection(pool: asyncpg.Pool, *, site: str):
    """Acquire a PG connection with bounded wait — no RLS (global/admin paths only).

    WARNING: This does NOT set nce.namespace_id. It bypasses tenant Row Level
    Security entirely. Only use for schema maintenance, global metadata reads, or
    explicitly audited admin operations. Never use for tenant data paths.

    ``site`` must be listed in ``UNMANAGED_PG_AUDITED_SITES`` (enforced at runtime).
    """
    if site not in UNMANAGED_PG_AUDITED_SITES:
        plugin_sites: frozenset[str] = frozenset()
        try:
            from nce.plugins import get_all_plugin_unmanaged_pg_sites

            plugin_sites = get_all_plugin_unmanaged_pg_sites()
        except Exception:
            plugin_sites = frozenset()
        if site not in plugin_sites:
            raise ValueError(
                f"unmanaged_pg_connection site {site!r} is not audited. "
                "Add it to UNMANAGED_PG_AUDITED_SITES in nce/db_utils.py after review."
            )
    async with pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
        yield conn


async def get_nce_external_scope(conn: asyncpg.Connection) -> str | None:  # type: ignore[type-arg]
    """Return the external_scope_id GUC value for the current PG session.

    Read-only accessor symmetric to ``nce.auth.set_namespace_context``.
    Setting the GUC is the responsibility of the session-wiring layer (Wave 23).

    Returns:
        The raw UUID string when ``nce.external_scope_id`` is set and non-empty,
        or ``None`` when unset / empty (the SQL layer will use the nil-UUID sentinel).

    Note: this function does NOT call ``get_nce_external_scope()`` in SQL; it reads
    the raw GUC so callers can distinguish "unset" from "set to a scope".
    """
    raw: str = await conn.fetchval("SELECT current_setting('nce.external_scope_id', true)")
    stripped = (raw or "").strip()
    return stripped if stripped else None


async def set_external_scope(conn: asyncpg.Connection, scope_id: str | UUID) -> None:  # type: ignore[type-arg]
    """Set the external_scope_id GUC for the current transaction (C3 principal-session wiring).

    Symmetric to ``nce.auth.set_namespace_context``: uses ``set_config(..., true)``
    so the value is scoped to the current transaction only (equivalent to SET LOCAL)
    and is automatically cleared when the transaction commits or rolls back.  Must be
    called inside an open transaction — callers are responsible for wrapping in one
    (``scoped_pg_session`` already does this for namespace_id).

    Call this only for **external principals** (contractor, external-customer).
    Employee sessions must NOT call it; leaving the GUC unset preserves the
    deny-when-unset guarantee from Wave 22 (a session without a scope sees nothing
    on external_isolation_policy tables).

    Args:
        conn:     An asyncpg Connection inside an open transaction.
        scope_id: The validated external scope UUID (str or UUID).
                  Must not be the nil UUID (00000000-…-0000).
    """
    await conn.execute(
        "SELECT set_config('nce.external_scope_id', $1, true)",
        str(scope_id),
    )


async def set_namespace_context(conn: Any, namespace_id: str | UUID) -> None:
    """Set the Postgres session variable consumed by RLS policies.

    Requires an active SQL transaction (BEGIN) before executing set_config.
    """
    if hasattr(conn, "is_in_transaction") and callable(conn.is_in_transaction):
        if conn.is_in_transaction() is False:
            raise RuntimeError(
                "SECURITY VIOLATION: set_namespace_context requires an active SQL transaction (BEGIN) to prevent autocommit/PgBouncer GUC leakage."
            )
    ns_str = str(namespace_id)
    await conn.execute(
        "SELECT set_config('nce.namespace_id', $1, true)",
        ns_str,
    )


def validate_redis_namespace_key(namespace_id: str | UUID, key: str) -> str:
    """Validate namespace_id as a canonical UUID and enforce nce:ns:{ns_uuid}:{key} prefix.

    Raises ValueError if namespace_id is invalid, or if key accesses another tenant
    or uses cross-namespace wildcard *.
    """
    if not namespace_id:
        raise ValueError("namespace_id is required")
    try:
        ns_uuid = str(UUID(str(namespace_id)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"Invalid namespace_id: {namespace_id!r}") from exc

    if not isinstance(key, str) or not key.strip():
        raise ValueError("key must be a non-empty string")

    expected_prefix = f"nce:ns:{ns_uuid}:"

    if key.startswith(expected_prefix):
        return key

    if key.startswith("nce:ns:"):
        parts = key.split(":")
        if len(parts) >= 3:
            target_ns = parts[2]
            if "*" in target_ns:
                raise ValueError(f"Cross-namespace wildcard access forbidden: {key}")
            try:
                target_uuid = str(UUID(target_ns))
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError(f"Invalid namespace in key: {key}") from exc
            if target_uuid != ns_uuid:
                raise ValueError(
                    f"Cross-tenant access forbidden: key belongs to {target_uuid}, expected {ns_uuid}"
                )
        raise ValueError(f"Cross-tenant access forbidden: {key}")

    if key == "*" or key.startswith("nce:*") or key.startswith("*"):
        raise ValueError(f"Cross-namespace wildcard access forbidden: {key}")

    if key.startswith("nce:"):
        raise ValueError(f"Unscoped nce: key forbidden: {key}")

    return f"{expected_prefix}{key}"


class ScopedRedisClient:
    """Redis wrapper enforcing tenant namespace isolation on all key operations."""

    def __init__(self, client: Any, namespace_id: str | UUID):
        if not namespace_id:
            raise ValueError("namespace_id is required for ScopedRedisClient")
        try:
            self._namespace_id = str(UUID(str(namespace_id)))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError(f"Invalid namespace_id: {namespace_id!r}") from exc
        self._client = client

    @property
    def namespace_id(self) -> str:
        return self._namespace_id

    @property
    def client(self) -> Any:
        return self._client

    def _scope_key(self, key: str) -> str:
        return validate_redis_namespace_key(self._namespace_id, key)

    def get(self, name: str) -> Any:
        return self._client.get(self._scope_key(name))

    def set(self, name: str, value: Any, *args: Any, **kwargs: Any) -> Any:
        return self._client.set(self._scope_key(name), value, *args, **kwargs)

    def delete(self, *names: str) -> Any:
        scoped_names = [self._scope_key(n) for n in names]
        return self._client.delete(*scoped_names)

    def exists(self, *names: str) -> Any:
        scoped_names = [self._scope_key(n) for n in names]
        return self._client.exists(*scoped_names)

    def expire(self, name: str, time: Any) -> Any:
        return self._client.expire(self._scope_key(name), time)

    def ttl(self, name: str) -> Any:
        return self._client.ttl(self._scope_key(name))

    def hget(self, name: str, key: str) -> Any:
        return self._client.hget(self._scope_key(name), key)

    def hset(self, name: str, *args: Any, **kwargs: Any) -> Any:
        return self._client.hset(self._scope_key(name), *args, **kwargs)

    def hgetall(self, name: str) -> Any:
        return self._client.hgetall(self._scope_key(name))

    def hdel(self, name: str, *keys: Any) -> Any:
        return self._client.hdel(self._scope_key(name), *keys)

    def hkeys(self, name: str) -> Any:
        return self._client.hkeys(self._scope_key(name))

    def hexists(self, name: str, key: str) -> Any:
        return self._client.hexists(self._scope_key(name), key)

    def hincrby(self, name: str, key: str, amount: int = 1) -> Any:
        return self._client.hincrby(self._scope_key(name), key, amount)

    def hincrbyfloat(self, name: str, key: str, amount: float = 1.0) -> Any:
        return self._client.hincrbyfloat(self._scope_key(name), key, amount)

    def keys(self, pattern: str = "*") -> Any:
        if pattern == "*" or not pattern:
            scoped_pattern = f"nce:ns:{self._namespace_id}:*"
        elif pattern.startswith(f"nce:ns:{self._namespace_id}:"):
            scoped_pattern = pattern
        elif pattern.startswith("nce:ns:") or pattern.startswith("nce:"):
            raise ValueError(f"Cross-namespace pattern forbidden: {pattern}")
        else:
            scoped_pattern = f"nce:ns:{self._namespace_id}:{pattern}"
        return self._client.keys(scoped_pattern)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


@asynccontextmanager
async def scoped_pg_session(
    pool: asyncpg.Pool,
    namespace_id: str | UUID,
):
    """
    Context manager for tenant-isolated PostgreSQL sessions.
    Automatically sets 'nce.namespace_id' for RLS enforcement.

    SET LOCAL only persists inside an explicit transaction; this manager wraps
    the entire yielded block in ``conn.transaction()`` so RLS context is
    active for all statements on *conn* (FIX-011).

    The namespace setting is automatically cleared by PostgreSQL when the
    transaction commits or rolls back — no explicit reset is required or
    performed.

    WARNING: Do not perform slow external I/O (Mongo calls, HTTP calls, LLM
    calls, embedding generation) inside this context. The entire yielded
    block runs inside one database transaction; long-held transactions increase
    lock contention and vacuum table bloat.

    Instrumented with SCOPED_SESSION_LATENCY histogram to monitor RLS
    SET LOCAL overhead on the hot path.
    """
    if not namespace_id:
        raise ValueError("namespace_id is required for scoped sessions")

    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    t0 = time.perf_counter()

    async with pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
        from nce import auth as _auth

        global _REAL_AUTH_SET_NAMESPACE_CONTEXT
        if _REAL_AUTH_SET_NAMESPACE_CONTEXT is None:
            _REAL_AUTH_SET_NAMESPACE_CONTEXT = _auth.set_namespace_context

        async with conn.transaction():
            global _HNSW_ITERATIVE_SCAN_SUPPORTED
            if (
                _HNSW_ITERATIVE_SCAN_SUPPORTED is True
                and _auth.set_namespace_context is _REAL_AUTH_SET_NAMESPACE_CONTEXT
            ):
                await conn.execute(
                    "SELECT set_config('nce.namespace_id', $1, true), "
                    "set_config('hnsw.iterative_scan', 'relaxed_order', true)",
                    str(ns_uuid),
                )
            else:
                await _auth.set_namespace_context(conn, ns_uuid)
                try:
                    await enable_hnsw_iterative_scan(conn)
                except Exception:
                    pass
            if isinstance(conn, (asyncpg.Connection, _PoolConnProxy)):
                try:
                    await conn.execute("SET LOCAL idle_in_transaction_session_timeout = '30s'")
                except Exception:
                    pass
            SCOPED_SESSION_LATENCY.observe(time.perf_counter() - t0)
            yield conn
            # SET LOCAL is automatically cleared at transaction end.
            # No explicit _reset_rls_context() call: that would run inside the
            # transaction's finally block and can mask the original SQL error if
            # the transaction is already in an aborted state.


class ScopedMongoCollection:
    """Wrapper around Motor's AsyncIOMotorCollection to enforce and auto-inject namespace scoping."""

    def __init__(self, collection: Any, namespace_id: str | UUID):
        if not namespace_id:
            raise ValueError("namespace_id is required for ScopedMongoCollection")
        try:
            self._namespace_id = str(UUID(str(namespace_id)))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError(f"Invalid namespace_id: {namespace_id!r}") from exc
        self._collection = collection

    def _validate_filter_operators(self, filter_spec: Any) -> None:
        """Reject $where and un-scoped cross-tenant filter operators."""
        if isinstance(filter_spec, dict):
            for key, val in filter_spec.items():
                if key == "$where":
                    raise ValueError(
                        "SECURITY VIOLATION: $where operator is forbidden in scoped Mongo queries."
                    )
                if isinstance(val, dict):
                    self._validate_filter_operators(val)
                elif isinstance(val, list):
                    for item in val:
                        self._validate_filter_operators(item)
        elif isinstance(filter_spec, list):
            for item in filter_spec:
                self._validate_filter_operators(item)

    def _scope_filter(self, filter_spec: Any) -> dict[str, Any]:
        """Ensures namespace_id matches scope, or auto-injects it."""
        if filter_spec is None:
            filter_spec = {}

        if not isinstance(filter_spec, dict):
            raise ValueError(f"Filter must be a dictionary, got {type(filter_spec)}")

        self._validate_filter_operators(filter_spec)

        if "namespace_id" in filter_spec:
            ns_val = filter_spec["namespace_id"]
            if str(ns_val) != self._namespace_id:
                raise ValueError(
                    f"Mismatched namespace_id: query has '{ns_val}', but session scope is '{self._namespace_id}'"
                )
            new_filter = dict(filter_spec)
            new_filter["namespace_id"] = self._namespace_id
            return new_filter
        else:
            new_filter = dict(filter_spec)
            new_filter["namespace_id"] = self._namespace_id
            return new_filter

    def _inject_filter(self, filter_spec: Any) -> dict[str, Any]:
        """Alias for _scope_filter."""
        return self._scope_filter(filter_spec)

    def _scope_document(self, document: Any) -> dict[str, Any]:
        """Ensures document has correct namespace_id for writes/inserts."""
        if not isinstance(document, dict):
            raise ValueError(f"Document must be a dictionary, got {type(document)}")

        if "namespace_id" in document:
            ns_val = document["namespace_id"]
            if str(ns_val) != self._namespace_id:
                raise ValueError(
                    f"Mismatched namespace_id: document has '{ns_val}', but session scope is '{self._namespace_id}'"
                )
            new_doc = dict(document)
            new_doc["namespace_id"] = self._namespace_id
            return new_doc
        else:
            new_doc = dict(document)
            new_doc["namespace_id"] = self._namespace_id
            return new_doc

    async def find_one(self, filter: Any = None, *args: Any, **kwargs: Any) -> Any:
        scoped_filter = self._scope_filter(filter)
        return await self._collection.find_one(scoped_filter, *args, **kwargs)

    def find(self, filter: Any = None, *args: Any, **kwargs: Any) -> Any:
        scoped_filter = self._scope_filter(filter)
        return self._collection.find(scoped_filter, *args, **kwargs)

    async def insert_one(self, document: Any, *args: Any, **kwargs: Any) -> Any:
        scoped_doc = self._scope_document(document)
        return await self._collection.insert_one(scoped_doc, *args, **kwargs)

    async def insert_many(self, documents: Any, *args: Any, **kwargs: Any) -> Any:
        if not isinstance(documents, list):
            raise ValueError("documents must be a list")
        scoped_docs = [self._scope_document(doc) for doc in documents]
        return await self._collection.insert_many(scoped_docs, *args, **kwargs)

    def _validate_cow_update(self, update: Any) -> None:
        """Forbid in-place payload mutation while allowing shred_memory tombstone erasure."""
        if not isinstance(update, dict):
            return
        for op, val in update.items():
            if op == "$set" and isinstance(val, dict):
                if "namespace_id" in val and str(val["namespace_id"]) != self._namespace_id:
                    raise ValueError(
                        f"Cannot update namespace_id to '{val['namespace_id']}'; session scope is '{self._namespace_id}'"
                    )
                is_shred_tombstone = val.get("shredded") is True and val.get("raw_data") is None
                if not is_shred_tombstone and (
                    ("raw_data" in val and val["raw_data"] is not None)
                    or ("raw_code" in val and val["raw_code"] is not None)
                ):
                    raise ValueError(
                        "CoW violation: in-place mutation of raw_data/raw_code is forbidden; "
                        "mint a new ObjectId and update PG valid_from/valid_to pointers instead."
                    )

    async def update_one(self, filter: Any, update: Any, *args: Any, **kwargs: Any) -> Any:
        scoped_filter = self._scope_filter(filter)
        self._validate_cow_update(update)
        return await self._collection.update_one(scoped_filter, update, *args, **kwargs)

    async def update_many(self, filter: Any, update: Any, *args: Any, **kwargs: Any) -> Any:
        scoped_filter = self._scope_filter(filter)
        self._validate_cow_update(update)
        return await self._collection.update_many(scoped_filter, update, *args, **kwargs)

    async def replace_one(self, filter: Any, replacement: Any, *args: Any, **kwargs: Any) -> Any:
        scoped_filter = self._scope_filter(filter)
        scoped_replacement = self._scope_document(replacement)
        self._validate_cow_update({"$set": scoped_replacement})
        return await self._collection.replace_one(
            scoped_filter, scoped_replacement, *args, **kwargs
        )

    async def delete_one(self, filter: Any, *args: Any, **kwargs: Any) -> Any:
        scoped_filter = self._scope_filter(filter)
        return await self._collection.delete_one(scoped_filter, *args, **kwargs)

    async def delete_many(self, filter: Any, *args: Any, **kwargs: Any) -> Any:
        scoped_filter = self._scope_filter(filter)
        return await self._collection.delete_many(scoped_filter, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._collection, name)


class ScopedMongoDatabase:
    """Wrapper around Motor's AsyncIOMotorDatabase to return scoped collection accessors."""

    def __init__(self, database: Any, namespace_id: str):
        self._database = database
        self._namespace_id = namespace_id

    def __getitem__(self, name: str) -> ScopedMongoCollection:
        coll = self._database[name]
        return ScopedMongoCollection(coll, self._namespace_id)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._database, name)
        if name.startswith("_"):
            return attr
        from motor.core import AgnosticCollection

        if isinstance(attr, AgnosticCollection) or (
            callable(getattr(attr, "find_one", None))
            and callable(getattr(attr, "insert_one", None))
        ):
            return ScopedMongoCollection(attr, self._namespace_id)
        return attr


@asynccontextmanager
async def scoped_mongo_session(
    client: Any,
    namespace_id: str | UUID,
):
    """Context manager for tenant-isolated MongoDB sessions.

    Analogous to scoped_pg_session. Enforces that all database operations
    automatically inject/verify the namespace_id.
    """
    if not namespace_id:
        raise ValueError("namespace_id is required for scoped Mongo sessions")
    ns_str = str(namespace_id)
    db = ScopedMongoDatabase(client.memory_archive, ns_str)
    yield db
