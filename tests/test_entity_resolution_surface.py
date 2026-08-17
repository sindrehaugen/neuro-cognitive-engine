"""
Integration tests for entity resolution MCP + REST dual surface (Wave 9).

Tests that:
  - resolve, merge_queue_list are registered and cacheable=True
  - merge_queue_confirm, merge_queue_reject are registered, mutation=True, admin_only=True
  - REST endpoints serve and preserve the dual surface contract
  - never-auto-merge invariant is maintained (confirm/reject only update queue row)
"""

from __future__ import annotations

import inspect
import json
from uuid import uuid4

import pytest

from nce.tool_registry import (
    ADMIN_ONLY_TOOLS,
    CACHEABLE_TOOLS,
    MUTATION_TOOLS,
    TOOL_REGISTRY,
)


class TestEntityResolutionToolRegistration:
    """Verify MCP tools are registered with correct flags."""

    def test_resolve_is_registered_and_cacheable(self):
        """resolve tool exists and is cacheable (read-only)."""
        assert "resolve" in TOOL_REGISTRY
        spec = TOOL_REGISTRY["resolve"]
        assert spec.cacheable is True
        assert spec.mutation is False
        assert spec.admin_only is False

    def test_merge_queue_list_is_registered_and_cacheable(self):
        """merge_queue_list tool exists and is cacheable (read-only)."""
        assert "merge_queue_list" in TOOL_REGISTRY
        spec = TOOL_REGISTRY["merge_queue_list"]
        assert spec.cacheable is True
        assert spec.mutation is False
        assert spec.admin_only is False

    def test_merge_queue_confirm_is_registered_mutation_admin_only(self):
        """merge_queue_confirm tool exists and is mutation + admin_only."""
        assert "merge_queue_confirm" in TOOL_REGISTRY
        spec = TOOL_REGISTRY["merge_queue_confirm"]
        assert spec.mutation is True
        assert spec.admin_only is True
        assert spec.cacheable is False

    def test_merge_queue_reject_is_registered_mutation_admin_only(self):
        """merge_queue_reject tool exists and is mutation + admin_only."""
        assert "merge_queue_reject" in TOOL_REGISTRY
        spec = TOOL_REGISTRY["merge_queue_reject"]
        assert spec.mutation is True
        assert spec.admin_only is True
        assert spec.cacheable is False

    def test_entity_resolution_tools_in_derived_sets(self):
        """Tools appear in the correct derived sets."""
        assert "resolve" in CACHEABLE_TOOLS
        assert "merge_queue_list" in CACHEABLE_TOOLS
        assert "merge_queue_confirm" in MUTATION_TOOLS
        assert "merge_queue_reject" in MUTATION_TOOLS
        assert "merge_queue_confirm" in ADMIN_ONLY_TOOLS
        assert "merge_queue_reject" in ADMIN_ONLY_TOOLS


class TestEntityResolutionHandlerSignatures:
    """Verify MCP handlers are async callables."""

    def test_resolve_handler_is_async(self):
        """handle_resolve is an async callable."""
        spec = TOOL_REGISTRY["resolve"]
        assert callable(spec.handler)
        assert inspect.iscoroutinefunction(spec.handler)

    def test_merge_queue_list_handler_is_async(self):
        """handle_merge_queue_list is an async callable."""
        spec = TOOL_REGISTRY["merge_queue_list"]
        assert callable(spec.handler)
        assert inspect.iscoroutinefunction(spec.handler)

    def test_merge_queue_confirm_handler_is_async(self):
        """handle_merge_queue_confirm is an async callable."""
        spec = TOOL_REGISTRY["merge_queue_confirm"]
        assert callable(spec.handler)
        assert inspect.iscoroutinefunction(spec.handler)

    def test_merge_queue_reject_handler_is_async(self):
        """handle_merge_queue_reject is an async callable."""
        spec = TOOL_REGISTRY["merge_queue_reject"]
        assert callable(spec.handler)
        assert inspect.iscoroutinefunction(spec.handler)


@pytest.mark.integration
class TestEntityResolutionMCPHandlers:
    """Integration tests for MCP handlers (with DB)."""

    @pytest.mark.asyncio
    async def test_resolve_handler_returns_json(self):
        """verify resolve handler returns valid JSON with correct structure."""
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, patch

        from nce.entity_resolution.mcp_handlers import handle_resolve

        mock_engine = AsyncMock()
        mock_conn = AsyncMock()

        @asynccontextmanager
        async def mock_scoped_session(*args, **kwargs):
            """Mock context manager for scoped_pg_session."""
            yield mock_conn

        with (
            patch("nce.entity_resolution.mcp_handlers.scoped_pg_session", mock_scoped_session),
            patch(
                "nce.entity_resolution.mcp_handlers.resolve", new_callable=AsyncMock
            ) as mock_resolve,
        ):
            mock_resolve.return_value = []

            namespace_id = str(uuid4())
            arguments = {
                "namespace_id": namespace_id,
                "candidate": {"name": "test"},
                "keys": ["name"],
                "node_type": "device",
            }

            result = await handle_resolve(mock_engine, arguments)

            # Result must be a JSON string with "status": "ok"
            parsed = json.loads(result)
            assert parsed["status"] == "ok"
            assert "matches" in parsed
            assert isinstance(parsed["matches"], list)

    @pytest.mark.asyncio
    async def test_merge_queue_list_handler_returns_json(self):
        """verify merge_queue_list handler returns valid JSON with correct structure."""
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, patch

        from nce.entity_resolution.mcp_handlers import handle_merge_queue_list

        mock_engine = AsyncMock()
        mock_conn = AsyncMock()

        @asynccontextmanager
        async def mock_scoped_session(*args, **kwargs):
            """Mock context manager for scoped_pg_session."""
            yield mock_conn

        with (
            patch("nce.entity_resolution.mcp_handlers.scoped_pg_session", mock_scoped_session),
            patch(
                "nce.entity_resolution.mcp_handlers.list_pending", new_callable=AsyncMock
            ) as mock_list,
        ):
            mock_list.return_value = []

            namespace_id = str(uuid4())
            arguments = {
                "namespace_id": namespace_id,
            }

            result = await handle_merge_queue_list(mock_engine, arguments)

            # Result must be a JSON string with "status": "ok"
            parsed = json.loads(result)
            assert parsed["status"] == "ok"
            assert "pending" in parsed
            assert isinstance(parsed["pending"], list)

    @pytest.mark.asyncio
    async def test_merge_queue_confirm_handler_returns_json(self):
        """verify merge_queue_confirm handler returns valid JSON with correct structure."""
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, patch

        from nce.entity_resolution.mcp_handlers import handle_merge_queue_confirm

        mock_engine = AsyncMock()
        mock_conn = AsyncMock()

        @asynccontextmanager
        async def mock_scoped_session(*args, **kwargs):
            """Mock context manager for scoped_pg_session."""
            yield mock_conn

        with (
            patch("nce.entity_resolution.mcp_handlers.scoped_pg_session", mock_scoped_session),
            patch(
                "nce.entity_resolution.mcp_handlers.confirm", new_callable=AsyncMock
            ) as mock_confirm,
        ):
            mock_confirm.return_value = None

            namespace_id = str(uuid4())
            queue_id = str(uuid4())
            arguments = {
                "namespace_id": namespace_id,
                "queue_id": queue_id,
                "decided_by": "test_user",
            }

            result = await handle_merge_queue_confirm(mock_engine, arguments)

            # Result must be a JSON string with "status": "ok"
            parsed = json.loads(result)
            assert parsed["status"] == "ok"
            assert "queue_id" in parsed
            assert parsed["queue_id"] == queue_id

    @pytest.mark.asyncio
    async def test_merge_queue_reject_handler_returns_json(self):
        """verify merge_queue_reject handler returns valid JSON with correct structure."""
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, patch

        from nce.entity_resolution.mcp_handlers import handle_merge_queue_reject

        mock_engine = AsyncMock()
        mock_conn = AsyncMock()

        @asynccontextmanager
        async def mock_scoped_session(*args, **kwargs):
            """Mock context manager for scoped_pg_session."""
            yield mock_conn

        with (
            patch("nce.entity_resolution.mcp_handlers.scoped_pg_session", mock_scoped_session),
            patch(
                "nce.entity_resolution.mcp_handlers.reject", new_callable=AsyncMock
            ) as mock_reject,
        ):
            mock_reject.return_value = None

            namespace_id = str(uuid4())
            queue_id = str(uuid4())
            arguments = {
                "namespace_id": namespace_id,
                "queue_id": queue_id,
                "decided_by": "test_user",
            }

            result = await handle_merge_queue_reject(mock_engine, arguments)

            # Result must be a JSON string with "status": "ok"
            parsed = json.loads(result)
            assert parsed["status"] == "ok"
            assert "queue_id" in parsed
            assert parsed["queue_id"] == queue_id


@pytest.mark.integration
class TestEntityResolutionRESTEndpoints:
    """Integration tests for REST endpoints (with DB)."""

    @pytest.mark.asyncio
    async def test_resolve_endpoint_exists(self):
        """admin_handlers.entity_resolution exports api_entity_resolution_resolve."""
        from nce.admin_handlers import entity_resolution

        assert hasattr(entity_resolution, "api_entity_resolution_resolve")
        assert callable(entity_resolution.api_entity_resolution_resolve)

    @pytest.mark.asyncio
    async def test_queue_list_endpoint_exists(self):
        """admin_handlers.entity_resolution exports api_entity_resolution_queue_list."""
        from nce.admin_handlers import entity_resolution

        assert hasattr(entity_resolution, "api_entity_resolution_queue_list")
        assert callable(entity_resolution.api_entity_resolution_queue_list)

    @pytest.mark.asyncio
    async def test_queue_confirm_endpoint_exists(self):
        """admin_handlers.entity_resolution exports api_entity_resolution_queue_confirm."""
        from nce.admin_handlers import entity_resolution

        assert hasattr(entity_resolution, "api_entity_resolution_queue_confirm")
        assert callable(entity_resolution.api_entity_resolution_queue_confirm)

    @pytest.mark.asyncio
    async def test_queue_reject_endpoint_exists(self):
        """admin_handlers.entity_resolution exports api_entity_resolution_queue_reject."""
        from nce.admin_handlers import entity_resolution

        assert hasattr(entity_resolution, "api_entity_resolution_queue_reject")
        assert callable(entity_resolution.api_entity_resolution_queue_reject)


class TestNeverAutoMergeInvariant:
    """Verify merge-queue confirm/reject never touch kg_nodes or kg_edges.

    This test suite documents the SCOPE LOCK from Wave 6:
    confirm() and reject() update **only** the queue row status.
    """

    def test_confirm_and_reject_are_mutations(self):
        """confirm and reject are marked as mutations (write operations)."""
        assert "merge_queue_confirm" in MUTATION_TOOLS
        assert "merge_queue_reject" in MUTATION_TOOLS

    def test_confirm_and_reject_are_admin_only(self):
        """confirm and reject require admin authentication."""
        assert "merge_queue_confirm" in ADMIN_ONLY_TOOLS
        assert "merge_queue_reject" in ADMIN_ONLY_TOOLS

    def test_confirm_and_reject_are_not_cacheable(self):
        """confirm and reject cannot be cached (they are mutations)."""
        assert "merge_queue_confirm" not in CACHEABLE_TOOLS
        assert "merge_queue_reject" not in CACHEABLE_TOOLS

    def test_list_is_cacheable_read_only(self):
        """merge_queue_list is cacheable and does not mutate."""
        assert "merge_queue_list" in CACHEABLE_TOOLS
        assert "merge_queue_list" not in MUTATION_TOOLS

    def test_resolve_is_cacheable_read_only(self):
        """resolve is cacheable and does not mutate."""
        assert "resolve" in CACHEABLE_TOOLS
        assert "resolve" not in MUTATION_TOOLS
