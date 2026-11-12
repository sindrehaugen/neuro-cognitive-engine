"""
Base orchestrator class to deduplicate common properties and helper methods.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import asyncpg
from motor.motor_asyncio import AsyncIOMotorClient

from nce.db_utils import scoped_mongo_session, scoped_pg_session


class OrchestratorBase:
    """Base class for all domain orchestrators, consolidating common state and helpers."""

    def __init__(
        self,
        pg_pool: asyncpg.Pool,
        mongo_client: AsyncIOMotorClient | None = None,
        redis_client: Any | None = None,
    ):
        self.pg_pool = pg_pool
        self.mongo_client = mongo_client
        self.redis_client = redis_client

    def _ensure_uuid(self, val: str | UUID | None) -> UUID | None:
        """Utility to convert a string or UUID to a UUID object, handling None."""
        if val is None:
            return None
        if isinstance(val, UUID):
            return val
        return UUID(str(val))

    @asynccontextmanager
    async def scoped_session(self, namespace_id: str | UUID):
        """Tenant-isolated PostgreSQL session (RLS + transaction-scoped SET LOCAL)."""
        import sys

        mod = sys.modules[self.__class__.__module__]
        func = getattr(mod, "scoped_pg_session", scoped_pg_session)
        async with func(self.pg_pool, namespace_id) as conn:
            yield conn

    @property
    def _mongo_db(self):
        """Access the MongoDB archive database, if client is initialized."""
        if self.mongo_client is None:
            raise RuntimeError("MongoDB client is not initialized on this orchestrator")
        return self.mongo_client.memory_archive

    @asynccontextmanager
    async def _get_scoped_mongo(self, namespace_id: str | UUID | None):
        """Yield a tenant-scoped MongoDB session (ScopedMongoCollection) for UUID namespaces."""
        if self.mongo_client is None:
            raise RuntimeError("MongoDB client is not initialized on this orchestrator")
        if namespace_id is None or str(namespace_id) in ("", "default"):
            yield self._mongo_db
            return
        async with scoped_mongo_session(self.mongo_client, namespace_id) as s_db:
            yield s_db

    def _validate_path(self, filepath: str) -> None:
        """Validate filepath is within allowed directory using shared helper."""
        from nce.orchestrators._utils import _validate_path

        _validate_path(filepath)
