"""
Tri-Stack Information Stacking Logic (The Orchestrator)
Implements the Python Saga Pattern for distributed transactions across Redis, Postgres, and MongoDB.
Rollback guarantee: any PG failure triggers Mongo cleanup to prevent orphaned documents.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from nce.orchestrators.cognitive import CognitiveOrchestrator
    from nce.orchestrators.graph import GraphOrchestrator
    from nce.orchestrators.memory import MemoryOrchestrator
    from nce.orchestrators.migration import MigrationOrchestrator
    from nce.orchestrators.namespace import NamespaceOrchestrator
    from nce.orchestrators.temporal import TemporalOrchestrator

import asyncpg
import redis.asyncio as redis
from minio import Minio
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, ConfigDict, Field

from nce import embeddings as _embeddings
from nce.config import cfg
from nce.models import (
    ArtifactPayload,
    CompareStatesRequest,
    CreateSnapshotRequest,
    DeleteSnapshotResult,
    GraphSearchRequest,
    IndexCodeFileRequest,
    ManageNamespaceRequest,
    ManageQuotasRequest,
    MediaPayload,
    SnapshotRecord,
    StateDiffResult,
    StoreMemoryRequest,
)
from nce.orchestrators._base import OrchestratorBase

# Backward-compat alias — MemoryPayload was renamed to StoreMemoryRequest
MemoryPayload = StoreMemoryRequest

log = logging.getLogger("nce-orchestrator")

# Health probes: degrade status, never raise to callers.
_HEALTH_PROBE_ERRORS: tuple[type[BaseException], ...] = (
    asyncpg.PostgresError,
    OSError,
    ConnectionError,
    asyncio.TimeoutError,
)
_QUEUE_PROBE_ERRORS: tuple[type[BaseException], ...] = (
    ImportError,
    OSError,
    ConnectionError,
    asyncio.TimeoutError,
    RuntimeError,
)


# --- Pydantic Models (Internal only) ---


class CodeChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filepath: str
    language: str
    node_type: str = Field(description="'function' or 'class'")
    name: str
    code_string: str
    start_line: int
    end_line: int


class VectorRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str | None = None
    session_id: str | None = None
    embedding: list[float]
    payload_ref: str


class MongoDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str | None = None
    session_id: str | None = None
    type: str
    raw_data: str
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# --- Config ---

# --- Engine ---


#: Advisory lock serialising every DDL path in startup: schema.sql, the
#: migration ledger, each migration, and the nce_app password refresh.
#: One constant on purpose -- the 2026-08-27 crash loop was a DDL site that
#: picked no lock at all, and a second site picking a *different* number would
#: fail the same way.
SCHEMA_ADVISORY_LOCK_ID = 123456

_ADVISORY_LOCK_SQL = "SELECT pg_advisory_xact_lock($1)"


class NCEEngine(OrchestratorBase):
    def __init__(self):
        super().__init__(None, None, None)
        self.mongo_client = None
        self.pg_pool = None
        self.pg_read_pool = None
        self.redis_client = None
        self.redis_sync_client = None
        self.minio_client = None  # New Quad-Stack MinIO property
        self._graph_traverser = None
        # Domain orchestrators (created in connect())
        self.memory: MemoryOrchestrator | None = None
        self.graph: GraphOrchestrator | None = None
        self.temporal: TemporalOrchestrator | None = None
        self.namespace: NamespaceOrchestrator | None = None
        self.cognitive: CognitiveOrchestrator | None = None
        self.migration: MigrationOrchestrator | None = None  # nce.orchestrators.migration
        self._init_lock = asyncio.Lock()

    async def connect(self):
        cfg.validate()
        self.mongo_client = AsyncIOMotorClient(
            cfg.MONGO_URI,
            serverSelectionTimeoutMS=5_000,
        )
        self.pg_pool = await asyncpg.create_pool(
            cfg.PG_DSN,
            min_size=cfg.PG_MIN_POOL,
            max_size=cfg.PG_MAX_POOL,
            command_timeout=30,
        )
        self.redis_client = redis.from_url(
            cfg.REDIS_URL,
            socket_connect_timeout=5,
            socket_timeout=5,
            max_connections=cfg.REDIS_MAX_CONNECTIONS,
            health_check_interval=30,
        )
        # RQ needs a synchronous connection
        import redis as redis_sync

        self.redis_sync_client = redis_sync.from_url(
            cfg.REDIS_URL,
            socket_connect_timeout=5,
            socket_timeout=5,
            max_connections=cfg.REDIS_MAX_CONNECTIONS,
            health_check_interval=30,
        )

        # Optional read-replica pool
        if cfg.DB_READ_URL and cfg.DB_READ_URL != cfg.PG_DSN:
            self.pg_read_pool = await asyncpg.create_pool(
                cfg.DB_READ_URL,
                min_size=cfg.PG_MIN_POOL,
                max_size=cfg.PG_MAX_POOL,
                command_timeout=30,
            )

        await self._init_pg_schema()
        await self._apply_pg_migrations()
        # Verification is cheap and read-only; seeding is expensive and mutating.
        # Validate first so a drifted deployment fails in ~1 s instead of paying
        # the full seed cost and *then* refusing to start.  Seeding depends only
        # on schema + migrations having run, not on verification order.
        # Before the enforcement checks, not after: version skew is what makes
        # those checks fail, and their error text reads like a code bug. Naming
        # the skew first turns 13 lines of "add to EXPECTED_TENANT_RLS_TABLES"
        # into "this image is older than this database".
        await self._verify_schema_version()
        await self._verify_worm_enforcement()
        await self._verify_rls_enforcement()
        await self._seed_node_ownership_all()
        await self._check_global_legacy_warning()
        await self._init_mongo_indexes()

        # Initialize MinIO
        self.minio_client = Minio(
            cfg.MINIO_ENDPOINT,
            access_key=cfg.MINIO_ACCESS_KEY,
            secret_key=cfg.MINIO_SECRET_KEY,
            secure=cfg.MINIO_SECURE,
        )

        # Ensure audio/video buckets exist asynchronously
        await asyncio.to_thread(self._init_minio_buckets)

        from nce.graph_query import GraphRAGTraverser

        self._graph_traverser = GraphRAGTraverser(
            pg_pool=self.pg_pool,
            mongo_client=self.mongo_client,
            embedding_fn=_embeddings.embed,
        )

        # --- Domain Orchestrators ---
        from nce.orchestrators.memory import MemoryOrchestrator

        self.memory = MemoryOrchestrator(
            pg_pool=self.pg_pool,
            mongo_client=self.mongo_client,
            redis_client=self.redis_client,
            minio_client=self.minio_client,
            pg_read_pool=self.pg_read_pool,
        )
        from nce.orchestrators.graph import GraphOrchestrator

        self.graph = GraphOrchestrator(
            pg_pool=self.pg_pool,
            mongo_client=self.mongo_client,
            graph_traverser=self._graph_traverser,
            embed_fn=_embeddings.embed,
        )
        from nce.orchestrators.temporal import TemporalOrchestrator

        self.temporal = TemporalOrchestrator(
            pg_pool=self.pg_pool,
            mongo_client=self.mongo_client,
            semantic_search_fn=self.semantic_search,
        )
        from nce.orchestrators.namespace import NamespaceOrchestrator

        self.namespace = NamespaceOrchestrator(
            pg_pool=self.pg_pool,
            redis_client=self.redis_client,
        )
        from nce.orchestrators.cognitive import CognitiveOrchestrator

        self.cognitive = CognitiveOrchestrator(
            pg_pool=self.pg_pool,
        )
        from nce.orchestrators.migration import MigrationOrchestrator

        self.migration = MigrationOrchestrator(
            pg_pool=self.pg_pool,
            redis_client=self.redis_client,
            redis_sync_client=self.redis_sync_client,
        )

        log.info("NCEEngine connected (Now Quad-Stack with MinIO).")

    async def disconnect(self):
        if self.mongo_client:
            self.mongo_client.close()
        if self.pg_pool:
            await self.pg_pool.close()
        if self.pg_read_pool:
            await self.pg_read_pool.close()
        if self.redis_client:
            await self.redis_client.aclose()
        if self.redis_sync_client:
            self.redis_sync_client.close()
        log.info("NCEEngine disconnected.")

    @property
    def _mongo_db(self):
        """Return the memory_archive MongoDB database instance."""
        if not self.mongo_client:
            raise RuntimeError("MongoDB client is not connected")
        return self.mongo_client.memory_archive

    def _warn_connect_not_called(self, method_name: str) -> None:
        """Warn when a lazy-init delegate is created outside of connect()."""
        log.warning(
            "Orchestrator %s called before connect() — creating delegate lazily. "
            "Call connect() before using the engine for production use.",
            method_name,
        )

    async def _ensure(self, name: str, factory: Callable[[], Any], method_name: str) -> None:
        if getattr(self, name) is not None:
            return
        async with self._init_lock:
            if getattr(self, name) is not None:
                return
            self._warn_connect_not_called(method_name)
            setattr(self, name, factory())

    async def _ensure_namespace(self, method_name: str) -> None:
        from nce.orchestrators.namespace import NamespaceOrchestrator

        await self._ensure(
            "namespace",
            lambda: NamespaceOrchestrator(self.pg_pool, redis_client=self.redis_client),
            method_name,
        )

    async def _ensure_memory(self) -> None:
        from nce.orchestrators.memory import MemoryOrchestrator

        await self._ensure(
            "memory",
            lambda: MemoryOrchestrator(
                self.pg_pool,
                self.mongo_client,
                self.redis_client,
                self.minio_client,
                pg_read_pool=self.pg_read_pool,
            ),
            "store_memory / store_artifact",
        )

    async def _ensure_graph(self, method_name: str) -> None:
        from nce.orchestrators.graph import GraphOrchestrator

        await self._ensure(
            "graph",
            lambda: GraphOrchestrator(
                self.pg_pool,
                self.mongo_client,
                self._graph_traverser,
                _embeddings.embed,
            ),
            method_name,
        )

    async def _ensure_temporal(self, method_name: str) -> None:
        from nce.orchestrators.temporal import TemporalOrchestrator

        await self._ensure(
            "temporal",
            lambda: TemporalOrchestrator(
                self.pg_pool,
                self.mongo_client,
                semantic_search_fn=self.semantic_search,
            ),
            method_name,
        )

    async def _ensure_migration(self, method_name: str) -> None:
        from nce.orchestrators.migration import MigrationOrchestrator

        await self._ensure(
            "migration",
            lambda: MigrationOrchestrator(self.pg_pool, self.redis_client, self.redis_sync_client),
            method_name,
        )

    async def _ensure_cognitive(self, method_name: str) -> None:
        from nce.orchestrators.cognitive import CognitiveOrchestrator

        await self._ensure(
            "cognitive",
            lambda: CognitiveOrchestrator(self.pg_pool),
            method_name,
        )

    def _redis_cache_key(
        self, namespace_id: str | UUID | None, user_id: str | None, filepath: str
    ) -> str:
        """Construct the Redis cache key for code file hashing."""
        scope_key = f"private:{user_id}" if user_id else "shared"
        namespace_prefix = f"{namespace_id}:" if namespace_id else ""
        return f"hash:{namespace_prefix}{scope_key}:{filepath}"

    def _init_minio_buckets(self):
        """Creates default media buckets if they do not exist."""
        from minio.error import S3Error

        buckets = ["mcp-audio", "mcp-video", "mcp-images"]
        for b in buckets:
            try:
                if not self.minio_client.bucket_exists(b):
                    self.minio_client.make_bucket(b)
                    log.debug("[MinIO] Created bucket: %s", b)
            except S3Error as exc:
                if exc.code in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                    log.debug("[MinIO] Bucket already exists: %s", b)
                    continue
                raise

    async def _init_pg_schema(self):
        """
        Load DDL from the package-bundled schema.sql and execute it as a single
        batch. Idempotent — safe to run on every startup. Keeping the schema in
        a sibling .sql file means it can be reviewed as a schema, diffed across
        versions, and fed to migration tools without touching Python.
        """
        from pathlib import Path

        from nce.config import cfg

        schema_path = Path(__file__).resolve().parent / "schema.sql"
        ddl = schema_path.read_text(encoding="utf-8")
        async with self.pg_pool.acquire(timeout=10.0) as conn:
            async with conn.transaction():
                await conn.execute(_ADVISORY_LOCK_SQL, SCHEMA_ADVISORY_LOCK_ID)
                await conn.execute(ddl)
        log.debug("[PG] schema.sql applied from %s", schema_path)

        if cfg.NCE_APP_PASSWORD:
            async with self.pg_pool.acquire(timeout=10.0) as conn:
                async with conn.transaction():
                    # Same advisory lock as the schema batch above, which itself
                    # CREATE/ALTER ROLEs nce_app. Without it, one process running
                    # schema.sql and another refreshing the password update the
                    # same pg_authid tuple concurrently, and Postgres raises
                    # "tuple concurrently updated" rather than serialising --
                    # observed crash-looping nce-admin on 2026-08-27.
                    await conn.execute(_ADVISORY_LOCK_SQL, SCHEMA_ADVISORY_LOCK_ID)
                    await conn.execute(
                        "SELECT set_config('nce.temp_password', $1, true)", cfg.NCE_APP_PASSWORD
                    )
                    await conn.execute(
                        "DO $$\n"
                        "BEGIN\n"
                        "    EXECUTE format('ALTER ROLE nce_app WITH LOGIN PASSWORD %L', current_setting('nce.temp_password'));\n"
                        "END\n"
                        "$$;"
                    )
            log.debug("[PG] nce_app login password dynamically updated from configuration")

    async def _apply_pg_migrations(self) -> None:
        """Apply SQL files from nce/migrations/ in lexical order, once each.

        Files are recorded in ``applied_migrations`` (see
        :mod:`nce.migration_ledger`) and skipped on later boots while their
        content is unchanged. Before the ledger every file re-ran on every
        start: 54 statements-batches of pure no-op work per boot, and no way to
        ask what version a database was at -- which is what made the 2026-08-27
        image-vs-database skew invisible.

        A file whose content *has* changed is re-applied and re-recorded, not
        refused: migrations here are edited in place when one turns out not to
        be idempotent, and refusing to boot on a corrected migration would be
        worse than re-running an idempotent one.
        """
        from pathlib import Path

        from nce.migration_ledger import (
            applied_checksums,
            ensure_ledger,
            migration_checksum,
            record_applied,
            should_skip,
        )

        migrations_dir = Path(__file__).resolve().parent / "migrations"
        if not migrations_dir.is_dir():
            return

        # The ledger is bookkeeping, not a safety guard: if it cannot be created
        # (an older role without DDL rights, say) fall back to the previous
        # behaviour of applying every file, rather than refusing to boot.
        # CREATE TABLE IF NOT EXISTS is not concurrency-safe against itself in
        # Postgres, and five services now boot this path at once (each one twice,
        # counting the pre-flight), so it takes the same advisory lock the
        # migrations do.
        ledger_ok = True
        applied: dict[str, str] = {}
        try:
            async with self.pg_pool.acquire(timeout=60.0) as conn:
                async with conn.transaction():
                    await conn.execute(_ADVISORY_LOCK_SQL, SCHEMA_ADVISORY_LOCK_ID)
                    await ensure_ledger(conn)
                applied = await applied_checksums(conn)
        except Exception as exc:
            log.warning("[PG] migration ledger unavailable (%s) — applying every migration", exc)
            ledger_ok = False

        skipped = 0
        for path in sorted(migrations_dir.glob("*.sql")):
            sql = path.read_text(encoding="utf-8")
            checksum = migration_checksum(sql)
            if should_skip(path.name, checksum, applied):
                skipped += 1
                continue
            if path.name in applied:
                log.warning(
                    "[PG] migration %s changed since it was applied — re-applying",
                    path.name,
                )
            async with self.pg_pool.acquire(timeout=60.0) as conn:
                async with conn.transaction():
                    await conn.execute(_ADVISORY_LOCK_SQL, SCHEMA_ADVISORY_LOCK_ID)
                    if "citus" in path.name:
                        citus_available = await conn.fetchval(
                            "SELECT EXISTS(SELECT 1 FROM pg_available_extensions WHERE name = 'citus')"
                        )
                        if not citus_available:
                            log.warning(
                                "[PG] Citus extension missing — applying fallback local topology schema for %s",
                                path.name,
                            )
                            await conn.execute("""
                                CREATE TABLE IF NOT EXISTS topology_graph (
                                    id                UUID        NOT NULL DEFAULT gen_random_uuid(),
                                    namespace_id      UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
                                    source_node_id    TEXT        NOT NULL,
                                    source_node_type  TEXT        NOT NULL,
                                    target_node_id    TEXT        NOT NULL,
                                    target_node_type  TEXT        NOT NULL,
                                    edge_type         TEXT        NOT NULL,
                                    decay_coefficient FLOAT8      NOT NULL DEFAULT 0.001,
                                    confidence_score  FLOAT8      NOT NULL DEFAULT 0.9,
                                    last_verified     TIMESTAMPTZ NOT NULL DEFAULT now(),
                                    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
                                    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
                                    metadata          JSONB       NOT NULL DEFAULT '{}'::jsonb,
                                    PRIMARY KEY (id, namespace_id)
                                );
                                ALTER TABLE topology_graph ENABLE ROW LEVEL SECURITY;
                                ALTER TABLE topology_graph FORCE ROW LEVEL SECURITY;
                                DROP POLICY IF EXISTS topology_graph_tenant_isolation ON topology_graph;
                                CREATE POLICY topology_graph_tenant_isolation ON topology_graph
                                    FOR ALL
                                    USING (namespace_id = get_nce_namespace());
                                GRANT SELECT, INSERT, UPDATE, DELETE ON topology_graph TO nce_app;
                                GRANT SELECT, INSERT, UPDATE, DELETE ON topology_graph TO nce_gc;
                            """)
                            # Deliberately NOT recorded: the real Citus
                            # migration must still run if the extension
                            # appears later, so this file stays unapplied.
                            continue
                    await conn.execute(sql)
                    if ledger_ok:
                        # Same transaction as the migration: a recorded row for
                        # a migration that did not commit would skip it forever.
                        await record_applied(conn, path.name, checksum)
            log.debug("[PG] migration applied: %s", path.name)
        if skipped:
            log.debug("[PG] %d migration(s) already applied — skipped", skipped)

    async def _verify_schema_version(self) -> None:
        """Report (and in production, refuse) an image older than its database.

        The 2026-08-27 failure was version skew: an image built one day behind
        the checkout that had migrated the live database. Its
        ``EXPECTED_TENANT_RLS_TABLES`` predated 13 tables that now existed, so
        ``_verify_rls_enforcement`` failed closed -- with a message listing
        tables to add to an allowlist, which reads like a code bug rather than a
        stale deploy. Two containers then crash-looped on it silently.

        This names the actual problem first. It is advisory outside production
        because branch images legitimately share a development database with
        different migration sets; in production it raises, unless
        ``NCE_ALLOW_SCHEMA_SKEW`` acknowledges it (the same shape as the mTLS
        boot guard).
        """
        import os
        from pathlib import Path

        from nce.build_info import describe
        from nce.migration_ledger import (
            applied_checksums,
            highest_version,
            missing_from_image,
        )

        migrations_dir = Path(__file__).resolve().parent / "migrations"
        if not migrations_dir.is_dir():
            return
        image_files = [p.name for p in sorted(migrations_dir.glob("*.sql"))]

        try:
            async with self.pg_pool.acquire(timeout=10.0) as conn:
                recorded = list(await applied_checksums(conn))
        except Exception as exc:
            log.warning("[PG] schema-version check skipped: %s", exc)
            return

        if not recorded:
            # A database that predates the ledger records nothing; this boot
            # will populate it. Nothing to compare against yet.
            return

        missing = missing_from_image(image_files, recorded)
        if not missing:
            return

        image_at = highest_version(image_files)
        db_at = highest_version(recorded)
        message = (
            f"schema skew: this image is missing {len(missing)} migration(s) the "
            f"database has already applied: {', '.join(missing[:10])}"
            + (" ..." if len(missing) > 10 else "")
            + f". This image has migrations up to {image_at}; the database is at "
            f"{db_at}. {describe()}. Rebuild or redeploy this image from a commit "
            "that includes them (or roll the database back). Until then the RLS "
            "catalog check will also fail, because this image's allowlist predates "
            "those tables -- that failure is a symptom, not the cause."
        )

        acknowledged = os.environ.get("NCE_ALLOW_SCHEMA_SKEW", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        if cfg.IS_PROD and not acknowledged:
            raise RuntimeError("FATAL: " + message)
        log.critical("%s%s", message, " (acknowledged)" if acknowledged else "")

    async def _seed_node_ownership_all(self) -> None:
        """Backfill node_ownership_registry for all existing namespaces.

        Called once during startup, after migrations are applied and after the
        WORM/RLS enforcement checks have passed -- see ``connect()``.
        A single set-based statement covers every namespace: the previous
        per-namespace loop issued one round trip per ownership entry per
        namespace, so startup cost grew with the tenant count. A failure is
        logged and skipped so seeding never aborts startup.
        """
        from nce.entity_resolution.ownership_seed import (
            seed_node_ownership_all_namespaces,
        )

        try:
            async with self.pg_pool.acquire(timeout=10.0) as conn:
                inserted = await seed_node_ownership_all_namespaces(conn)
            if inserted:
                log.info("[ownership-seed] seeded %d row(s) across all namespaces", inserted)
        except Exception as exc:
            log.warning("[ownership-seed] Bulk seed failed: %s", exc)

    async def _verify_worm_enforcement(self):
        """
        Runtime assertion that all WORM tables deny UPDATE/DELETE.

        Acquires a connection from a temporary connection established as the
        ``nce_app`` role using its configured password, eliminating superuser
        WORM bypassing in regular environments.
        """
        from urllib.parse import urlparse, urlunparse

        from nce.config import cfg
        from nce.event_log import _WORM_TABLES, verify_worm_on_table

        # Construct DSN for nce_app
        app_dsn = None
        if cfg.PG_DSN:
            try:
                parsed = urlparse(cfg.PG_DSN)
                netloc = parsed.hostname or ""
                if parsed.port:
                    netloc = f"{netloc}:{parsed.port}"
                app_pass = cfg.NCE_APP_PASSWORD or "nce_app_secret"
                netloc = f"nce_app:{app_pass}@{netloc}"
                app_dsn = urlunparse(parsed._replace(netloc=netloc))
            except Exception as exc:
                log.warning("[worm-probe] Failed to parse PG_DSN for nce_app connection: %s", exc)

        if app_dsn:
            log.debug(
                "[worm-probe] Probing WORM enforcement with actual nce_app role credentials..."
            )
            try:
                conn = await asyncpg.connect(app_dsn, timeout=10.0)
                try:
                    for table in _WORM_TABLES:
                        await verify_worm_on_table(conn, table)
                finally:
                    await conn.close()
                return
            except Exception as exc:
                log.warning(
                    "[worm-probe] Failed to connect as nce_app: %s. Falling back to default PG pool.",
                    exc,
                )

        async with self.pg_pool.acquire(timeout=10.0) as conn:
            for table in _WORM_TABLES:
                await verify_worm_on_table(conn, table)

    async def _verify_rls_enforcement(self):
        """
        Validate that all RLS-protected tables are scoped by namespace.

        Acquires a connection from the pool and runs
        ``verify_rls_catalog_consistency()`` against the PostgreSQL catalog
        to confirm all tenant tables exist, have RLS enabled, and have a
        namespace isolation policy. Raises ``RuntimeError`` on any mismatch.
        """
        from nce.event_log import (
            verify_rls_catalog_consistency,
        )

        async with self.pg_pool.acquire(timeout=10.0) as conn:
            await verify_rls_catalog_consistency(conn)

    async def _check_global_legacy_warning(self):
        """Warn if ``_global_legacy`` namespace still has KG entities.

        The ``_global_legacy`` namespace is a transitional artifact created during
        the KG RLS migration (schema.sql).  If it still contains KG data and is
        older than 30 days, operators should migrate those entities to proper
        namespaces to reduce the cross-tenant attack surface.
        """
        try:
            async with self.pg_pool.acquire(timeout=10.0) as conn:
                row = await conn.fetchrow(
                    "SELECT id, created_at FROM namespaces WHERE slug = '_global_legacy'"
                )
        except _HEALTH_PROBE_ERRORS:
            log.warning(
                "[legacy-warn] Could not query _global_legacy namespace "
                "(table may not exist yet on first run)."
            )
            return

        if row is None:
            log.info("[legacy-warn] No _global_legacy namespace found — clean start.")
            return

        ns_id = row["id"]
        now_dt = datetime.now(timezone.utc)
        created_dt = row["created_at"]
        if created_dt and created_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=None)
        age_days = (now_dt - created_dt).days if created_dt else 0

        try:
            async with self.pg_pool.acquire(timeout=10.0) as conn:
                count = await conn.fetchval(
                    "SELECT count(*) FROM kg_nodes WHERE namespace_id = $1::uuid",
                    ns_id,
                )
        except _HEALTH_PROBE_ERRORS:
            log.warning(
                "[legacy-warn] Could not query kg_nodes for _global_legacy "
                "(table may not exist yet on first run)."
            )
            return

        if count and count > 0:
            msg = f"_global_legacy namespace still has {count} KG entities (age: {age_days} days)"
            if age_days >= 30:
                log.warning(
                    "[legacy-warn] %s — entities should be migrated to proper "
                    "namespaces to reduce cross-tenant attack surface.",
                    msg,
                )
            else:
                log.info("[legacy-warn] %s — will escalate after 30 days.", msg)
        else:
            log.info("[legacy-warn] _global_legacy namespace exists but has no KG entities.")

    async def _init_mongo_indexes(self):
        db = self._mongo_db
        await db.episodes.create_index("user_id")
        await db.code_files.create_index("filepath")
        await db.code_files.create_index("user_id")

    # --- Database Helpers ---
    # NOTE: _get_db_pool was removed (R3) — it was defined but never called.
    # Read-replica routing will be wired in Phase 4 via scoped_pg_session.
    # NOTE: _generate_embedding was removed (R4) — it was a one-liner alias for
    # _embeddings.embed and offered no added value.  All three call sites now
    # reference _embeddings.embed directly.

    # --- Phase 0.1: Namespace Management ---

    async def manage_namespace(
        self,
        payload: ManageNamespaceRequest,
        admin_identity: str | None = None,
    ) -> dict:
        """[Phase 0.1] Namespace management — delegating to NamespaceOrchestrator."""
        await self._ensure_namespace("manage_namespace")
        return await self.namespace.manage_namespace(
            payload,
            admin_identity=admin_identity,
        )

    # --- Phase 0.2: Memory Integrity ---

    async def verify_memory(self, memory_id: str, as_of: datetime | None = None) -> dict:
        """[Phase 0.2] Delegate to MemoryOrchestrator."""
        await self._ensure_memory()
        return await self.memory.verify_memory(memory_id, as_of)

    # --- Phase 1.2: Consolidation Tools ---

    async def trigger_consolidation(
        self, namespace_id: str, since_timestamp: datetime | None = None
    ):
        """[Phase 1.2] Trigger consolidation — delegating to TemporalOrchestrator."""
        await self._ensure_temporal("trigger_consolidation")
        return await self.temporal.trigger_consolidation(namespace_id, since_timestamp)

    async def consolidation_status(self, run_id: str) -> dict:
        """[Phase 1.2] Consolidation status — delegating to TemporalOrchestrator."""
        await self._ensure_temporal("consolidation_status")
        return await self.temporal.consolidation_status(run_id)

    # --- Code Indexing ---

    async def index_code_file(self, payload: IndexCodeFileRequest, *, priority: int = 0) -> dict:
        """[Phase 3.2] Code indexing — delegating to MigrationOrchestrator.

        *priority* routes to queue lane: >0 = high_priority, 0 = batch_processing.
        """
        await self._ensure_migration("index_code_file")
        return await self.migration.index_code_file(payload, priority=priority)

    async def get_job_status(self, job_id: str) -> dict:
        """RQ job status — delegating to MigrationOrchestrator."""
        await self._ensure_migration("get_job_status")
        return await self.migration.get_job_status(job_id)

    # --- Graph Search ---

    async def graph_search(self, payload: GraphSearchRequest) -> dict:
        """[Phase 2.2] GraphRAG traversal — delegating to GraphOrchestrator."""
        await self._ensure_graph("graph_search")
        return await self.graph.graph_search(payload)

    # --- Codebase Search ---

    async def search_codebase(
        self,
        query: str,
        namespace_id: str | None = None,
        language_filter: str | None = None,
        top_k: int = 5,
        *,
        user_id: str | None = None,
        private: bool = False,
        aspect: str | None = None,
    ) -> list[dict]:
        """Codebase hybrid search — delegating to GraphOrchestrator."""
        await self._ensure_graph("search_codebase")
        return await self.graph.search_codebase(
            query,
            namespace_id,
            language_filter,
            top_k,
            user_id=user_id,
            private=private,
            aspect=aspect,
        )

    async def manage_quotas(self, payload: ManageQuotasRequest) -> dict:
        """[Phase 3.2] Quota management — delegating to NamespaceOrchestrator."""
        await self._ensure_namespace("manage_quotas")
        return await self.namespace.manage_quotas(payload)

    # --- Core Saga: store_memory ---
    async def store_memory(self, payload: StoreMemoryRequest) -> dict:
        """Delegate to MemoryOrchestrator (lazy-init for test compatibility)."""
        await self._ensure_memory()
        return await self.memory.store_memory(payload)

    async def store_artifact(self, payload: ArtifactPayload) -> str:
        """[Phase 1.3] High-performance artifact storage (replaces store_media)."""
        await self._ensure_memory()
        return await self.memory.store_artifact(payload)

    async def store_media(self, payload: MediaPayload) -> str:
        """[DEPRECATED] Use store_artifact instead."""
        import warnings

        warnings.warn(
            "store_media is deprecated; use store_artifact instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return await self.store_artifact(payload)

    async def force_gc(self) -> dict:
        """Manually trigger a GC pass."""
        from nce.garbage_collector import _collect_orphans

        if not self.mongo_client or not self.pg_pool:
            raise RuntimeError("Engine not connected")

        result = await _collect_orphans(self.mongo_client, self.pg_pool)

        # Check if we purged an abnormally large amount
        total_deleted = result.get("deleted_docs", 0) + result.get("deleted_nodes", 0)
        if total_deleted > cfg.GC_ALERT_THRESHOLD:
            from nce.notifications import dispatcher

            await dispatcher.dispatch_alert(
                "Large GC Purge", f"Manual GC purged {total_deleted} items."
            )

        return result

    async def check_health(self) -> dict:
        """Comprehensive health check — databases, security, cognitive, queues."""
        health: dict[str, Any] = {
            "status": "ok",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "security": {
                "master_key": (
                    "valid"
                    if (cfg.NCE_MASTER_KEY and len(cfg.NCE_MASTER_KEY) >= 32)
                    else "missing/invalid"
                )
            },
            "databases": {
                "mongo": "down",
                "postgres": "down",
                "redis": "down",
            },
            "queues": {
                "default": "unknown",
                "high_priority": "unknown",
                "batch_processing": "unknown",
            },
            "cognitive": {"backend": cfg.NCE_BACKEND or "auto", "engine": "unknown"},
        }

        # 1. Mongo
        try:
            if self.mongo_client:
                await self.mongo_client.admin.command("ping")
                health["databases"]["mongo"] = "up"
        except _HEALTH_PROBE_ERRORS:
            health["status"] = "degraded"

        # 2. Postgres (actual probe, not hard-coded)
        try:
            if self.pg_pool:
                async with self.pg_pool.acquire(timeout=10.0) as conn:
                    await conn.execute("SELECT 1")
                health["databases"]["postgres"] = "up"
        except _HEALTH_PROBE_ERRORS:
            health["status"] = "degraded"

        # 2.5 Security, Chain & RLS Deep Probes (III.4)
        health["security"]["signing_key_decryption"] = "failed"
        health["security"]["bounded_chain_sample"] = "failed"
        health["security"]["bounded_signature_sample"] = "failed"
        health["databases"]["rls_read"] = "failed"

        # (a) Decrypt the active signing key (bypassing TTLCache)
        try:
            if self.pg_pool:
                from nce.signing import decrypt_signing_key, require_master_key

                async with self.pg_pool.acquire(timeout=10.0) as conn:
                    row = await conn.fetchrow("""
                        SELECT encrypted_key
                        FROM   signing_keys
                        WHERE  status = 'active'
                        ORDER  BY created_at DESC
                        LIMIT  1
                    """)
                    if row is None:
                        health["security"]["signing_key_decryption"] = "no_active_key"
                        health["status"] = "degraded"
                    else:
                        with require_master_key() as master_key:
                            decrypt_signing_key(bytes(row["encrypted_key"]), master_key)
                        health["security"]["signing_key_decryption"] = "valid"
            else:
                health["status"] = "degraded"
        except Exception:
            log.exception("Health probe (a) decrypt active signing key failed")
            health["security"]["signing_key_decryption"] = "failed"
            health["status"] = "degraded"

        # (b) Verify bounded chain sample for active namespaces + set MERKLE_CHAIN_VALID
        try:
            from nce.event_log import verify_merkle_chain
            from nce.observability import MERKLE_CHAIN_VALID

            if self.pg_pool:
                async with self.pg_pool.acquire(timeout=10.0) as conn:
                    ns_rows = await conn.fetch("SELECT id FROM namespaces LIMIT 5")

                chain_ok = True
                for ns_row in ns_rows:
                    ns_id = ns_row["id"]
                    async with self.pg_pool.acquire(timeout=10.0) as conn:
                        max_seq = await conn.fetchval(
                            "SELECT COALESCE(max(event_seq), 0) FROM event_log WHERE namespace_id = $1",
                            ns_id,
                        )
                        if max_seq > 0:
                            start_seq = max(1, max_seq - 5 + 1)
                            res = await verify_merkle_chain(
                                conn, namespace_id=ns_id, start_seq=start_seq
                            )
                            if not res.get("valid", True):
                                chain_ok = False
                                break

                if chain_ok:
                    health["security"]["bounded_chain_sample"] = "valid"
                    MERKLE_CHAIN_VALID.set(1)
                else:
                    health["security"]["bounded_chain_sample"] = "corrupted"
                    health["status"] = "degraded"
                    MERKLE_CHAIN_VALID.set(0)
            else:
                health["status"] = "degraded"
                MERKLE_CHAIN_VALID.set(0)
        except Exception:
            log.exception("Health probe (b) verify bounded chain sample failed")
            health["security"]["bounded_chain_sample"] = "failed"
            health["status"] = "degraded"
            from nce.observability import MERKLE_CHAIN_VALID

            MERKLE_CHAIN_VALID.set(0)

        # (b2) Verify bounded event-signature sample for active namespaces
        #      Mirrors block (b): same namespace sample, bounded per-namespace row count.
        #      Uses scoped_pg_session so all event_log reads are RLS-enforced.
        try:
            from nce.db_utils import scoped_pg_session as _scoped_pg_session
            from nce.event_log import DataIntegrityError as _DataIntegrityError
            from nce.event_log import verify_event_signature as _verify_event_signature
            from nce.observability import EVENT_SIGNATURE_VALID

            if self.pg_pool:
                async with self.pg_pool.acquire(timeout=10.0) as _ns_conn:
                    _ns_rows = await _ns_conn.fetch("SELECT id FROM namespaces LIMIT 5")

                sig_ok = True
                for _ns_row in _ns_rows:
                    _ns_id = _ns_row["id"]
                    async with _scoped_pg_session(self.pg_pool, _ns_id) as _conn:
                        _max_seq = await _conn.fetchval(
                            "SELECT COALESCE(max(event_seq), 0) FROM event_log WHERE namespace_id = $1",
                            _ns_id,
                        )
                        if _max_seq and _max_seq > 0:
                            _start_seq = max(1, _max_seq - 5 + 1)
                            _sig_rows = await _conn.fetch(
                                """
                                SELECT id, namespace_id, agent_id, event_type, event_seq,
                                       occurred_at, params, parent_event_id,
                                       signature, signature_key_id, signature_version
                                FROM   event_log
                                WHERE  namespace_id = $1
                                  AND  event_seq >= $2
                                ORDER BY event_seq ASC
                                """,
                                _ns_id,
                                _start_seq,
                            )
                            for _sig_row in _sig_rows:
                                try:
                                    await _verify_event_signature(_conn, _sig_row)
                                except _DataIntegrityError:
                                    log.critical(
                                        "Health probe (b2): event signature tampered "
                                        "namespace_id=%s event_seq=%s",
                                        _ns_id,
                                        _sig_row["event_seq"],
                                    )
                                    sig_ok = False
                                    break
                    if not sig_ok:
                        break

                if sig_ok:
                    health["security"]["bounded_signature_sample"] = "valid"
                    EVENT_SIGNATURE_VALID.set(1)
                else:
                    health["security"]["bounded_signature_sample"] = "tampered"
                    health["status"] = "degraded"
                    EVENT_SIGNATURE_VALID.set(0)
            else:
                health["status"] = "degraded"
                EVENT_SIGNATURE_VALID.set(0)
        except Exception:
            log.exception("Health probe (b2) verify bounded signature sample failed")
            health["security"]["bounded_signature_sample"] = "failed"
            health["status"] = "degraded"
            from nce.observability import EVENT_SIGNATURE_VALID as _ESV

            _ESV.set(0)

        # (c) Sample RLS-scoped read
        try:
            if self.pg_pool:
                from nce.db_utils import scoped_pg_session

                dummy_ns = UUID("00000000-0000-0000-0000-000000000000")
                async with scoped_pg_session(self.pg_pool, dummy_ns) as conn:
                    # Select from memories RLS-protected table to verify isolation policy functions
                    await conn.execute("SELECT id FROM memories LIMIT 1")
                health["databases"]["rls_read"] = "valid"
            else:
                health["status"] = "degraded"
        except Exception:
            log.exception("Health probe (c) sample RLS read failed")
            health["databases"]["rls_read"] = "failed"
            health["status"] = "degraded"

        # 3. Redis
        try:
            if self.redis_client:
                await self.redis_client.ping()
                health["databases"]["redis"] = "up"
        except _HEALTH_PROBE_ERRORS:
            health["status"] = "degraded"

        # 4. RQ queues — all three lanes (sync Redis I/O → thread pool)
        try:
            if self.redis_sync_client:
                from rq import Queue

                def _get_queue_lengths():
                    lengths = {}
                    for name in ("default", "high_priority", "batch_processing"):
                        q = Queue(name, connection=self.redis_sync_client)
                        lengths[name] = len(q)
                    return lengths

                lengths = await asyncio.to_thread(_get_queue_lengths)
                for queue_name, qlen in lengths.items():
                    health["queues"][queue_name] = f"{qlen} pending jobs"
        except _QUEUE_PROBE_ERRORS:
            pass

        # 5. Cognitive / Embeddings
        import httpx

        from nce.embeddings import cognitive_health_check_url, get_backend

        try:
            backend = get_backend()
            health["cognitive"]["backend_type"] = type(backend).__name__

            async with httpx.AsyncClient(timeout=2.0) as client:
                url = cognitive_health_check_url()
                resp = await client.get(url)
                if resp.status_code == 200:
                    health["cognitive"]["engine"] = "up"
                else:
                    health["cognitive"]["engine"] = f"down ({resp.status_code})"
        except (
            *_HEALTH_PROBE_ERRORS,
            httpx.HTTPError,
            httpx.TimeoutException,
        ) as e:
            health["cognitive"]["engine"] = f"unreachable ({type(e).__name__})"
            if not cfg.NCE_BACKEND:
                health["status"] = "degraded"

        return health

    # --- Recall ---

    async def recall_memory(self, namespace_id, user_id, session_id, as_of=None):
        """Legacy single-result recall — delegate to MemoryOrchestrator."""
        await self._ensure_memory()
        return await self.memory.recall_memory(namespace_id, user_id, session_id, as_of)

    async def recall_recent(
        self,
        namespace_id,
        agent_id="default",
        limit=10,
        as_of=None,
        user_id=None,
        session_id=None,
        offset=0,
    ):
        """[Phase 2.2] Delegate to MemoryOrchestrator."""
        await self._ensure_memory()
        return await self.memory.recall_recent(
            namespace_id, agent_id, limit, as_of, user_id, session_id, offset
        )

    # --- Semantic Search ---

    async def semantic_search(
        self,
        query,
        namespace_id,
        agent_id="default",
        limit=5,
        offset=0,
        as_of=None,
    ):
        """Delegate to MemoryOrchestrator."""
        await self._ensure_memory()
        return await self.memory.semantic_search(
            query, namespace_id, agent_id, limit, offset, as_of
        )

    async def unredact_memory(self, memory_id, namespace_id, agent_id):
        """[Phase 0.3] Delegate to MemoryOrchestrator."""
        await self._ensure_memory()
        return await self.memory.unredact_memory(memory_id, namespace_id, agent_id)

    async def shred_memory(self, memory_id: str, namespace_id: str, agent_id: str) -> dict:
        """[Part II.4] Provably forget a memory — delegating to MemoryOrchestrator."""
        await self._ensure_memory()
        return await self.memory.shred_memory(memory_id, namespace_id, agent_id)

    # --- Phase 1.1: Cognitive Layer (Salience) ---

    async def boost_memory(
        self, memory_id: str, agent_id: str, namespace_id: str, factor: float = 0.2
    ) -> dict:
        """[Phase 1.1] Boost memory — delegating to CognitiveOrchestrator."""
        await self._ensure_cognitive("boost_memory")
        return await self.cognitive.boost_memory(memory_id, agent_id, namespace_id, factor)

    async def forget_memory(self, memory_id: str, agent_id: str, namespace_id: str) -> dict:
        """[Phase 1.1] Forget memory — delegating to CognitiveOrchestrator."""
        await self._ensure_cognitive("forget_memory")
        return await self.cognitive.forget_memory(memory_id, agent_id, namespace_id)

    # --- Phase 1.3: Contradictions ---

    async def list_contradictions(
        self,
        namespace_id: str,
        resolution: str | None = None,
        agent_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """[Phase 1.3] List contradictions — delegating to CognitiveOrchestrator."""
        await self._ensure_cognitive("list_contradictions")
        return await self.cognitive.list_contradictions(
            namespace_id, resolution, agent_id, limit=limit, offset=offset
        )

    async def resolve_contradiction(
        self,
        contradiction_id: str,
        namespace_id: str,
        resolution: str,
        resolved_by: str,
        note: str | None = None,
    ) -> dict:
        """[Phase 1.3] Resolve contradiction — RLS-enforced, delegating to CognitiveOrchestrator."""
        await self._ensure_cognitive("resolve_contradiction")
        return await self.cognitive.resolve_contradiction(
            contradiction_id, namespace_id, resolution, resolved_by, note
        )

    # --- Phase 2.2: Time Travel Snapshots ---

    async def create_snapshot(self, payload: CreateSnapshotRequest) -> SnapshotRecord:
        """[Phase 2.2] Create snapshot — delegating to TemporalOrchestrator."""
        await self._ensure_temporal("create_snapshot")
        return await self.temporal.create_snapshot(payload)

    async def list_snapshots(self, namespace_id: str) -> list[SnapshotRecord]:
        """[Phase 2.2] List snapshots — delegating to TemporalOrchestrator."""
        await self._ensure_temporal("list_snapshots")
        return await self.temporal.list_snapshots(namespace_id)

    async def delete_snapshot(self, snapshot_id: str, namespace_id: str) -> DeleteSnapshotResult:
        """[Phase 2.2] Delete snapshot — delegating to TemporalOrchestrator."""
        await self._ensure_temporal("delete_snapshot")
        return await self.temporal.delete_snapshot(snapshot_id, namespace_id)

    async def _fetch_memories_valid_at(
        self,
        conn: asyncpg.Connection,
        namespace_id: UUID,
        memory_ids: list[UUID],
        as_of: datetime,
    ) -> dict[str, Any]:
        """[Phase 2.2] Fetch memory rows valid at a point in time — delegating."""
        await self._ensure_temporal("_fetch_memories_valid_at")
        return await self.temporal._fetch_memories_valid_at(conn, namespace_id, memory_ids, as_of)

    async def compare_states(self, payload: CompareStatesRequest) -> StateDiffResult:
        """[Phase 2.2] Compare states — delegating to TemporalOrchestrator."""
        await self._ensure_temporal("compare_states")
        return await self.temporal.compare_states(payload)

    # --- Phase 2.1: Re-embedding Migrations ---

    async def start_migration(
        self,
        target_model_id: str,
        *,
        admin_identity: str | None = None,
    ) -> dict:
        """[Phase 2.1] Start migration — engine-layer chokepoint for the start gate.

        Runs the dimension preflight + pre-flight WORM audit (the SINGLE place
        through which both the MCP tool and the admin HTTP route converge) before
        delegating to :class:`MigrationOrchestrator`.  No caller can start a
        dimension-incompatible migration by bypassing the MCP handler.
        """
        from nce.migration_gate import enforce_start_gate

        await self._ensure_migration("start_migration")
        await enforce_start_gate(
            self.pg_pool,
            target_model_id=str(target_model_id).strip(),
            admin_identity=admin_identity,
        )
        return await self.migration.start_migration(target_model_id)

    async def migration_status(self, migration_id: str) -> dict:
        """[Phase 2.1] Migration status — delegating to MigrationOrchestrator."""
        await self._ensure_migration("migration_status")
        return await self.migration.migration_status(migration_id)

    async def validate_migration(self, migration_id: str) -> dict:
        """[Phase 2.1] Validate migration — delegating to MigrationOrchestrator."""
        await self._ensure_migration("validate_migration")
        return await self.migration.validate_migration(migration_id)

    async def commit_migration(
        self,
        migration_id: str,
        *,
        force: bool = False,
        admin_identity: str | None = None,
    ) -> dict:
        """[Phase 2.1] Commit migration — engine-layer chokepoint for the commit gate.

        Evaluates the neighbor-overlap quality gate (refusing below
        ``cfg.NCE_REEMBED_GATE_MIN_OVERLAP`` and on degenerate samples), honours the
        audited ``force`` escape, and writes the pre-flight WORM audit — BEFORE the
        schema-switching transaction.  This is the SINGLE place through which both the
        MCP tool and the admin HTTP route converge, so a bad model swap cannot be
        promoted by bypassing the MCP handler.
        """
        from nce.migration_gate import enforce_commit_gate

        await self._ensure_migration("commit_migration")
        await enforce_commit_gate(
            self.pg_pool,
            migration_id=str(migration_id),
            force=force,
            admin_identity=admin_identity,
        )
        return await self.migration.commit_migration(migration_id)

    async def abort_migration(self, migration_id: str) -> dict:
        """[Phase 2.1] Abort migration — delegating to MigrationOrchestrator."""
        await self._ensure_migration("abort_migration")
        return await self.migration.abort_migration(migration_id)
