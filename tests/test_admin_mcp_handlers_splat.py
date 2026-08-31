"""Unit tests for the admin-splat-strip fix (Batch 67N / Module 6 Wave 23).

Three admin MCP tool handlers splat the raw MCP ``arguments`` dict into a
pydantic model declaring ``extra="forbid"``. The admin auth branch in
``nce.auth.enforce_mcp_tool_auth`` leaves ``admin_api_key`` in that dict
(unlike the tenant branch, which pops it in a ``finally``), so a raw splat
raises ``ValidationError: extra_forbidden``. ``model_kwargs()`` strips the
MCP auth/transport keys before construction.

These are plain unit tests: no database, no event loop, no
``@pytest.mark.integration`` marker — they only construct pydantic models.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from nce.mcp_args import model_kwargs
from nce.models import (
    ManageNamespaceCommand,
    ManageNamespaceRequest,
    ManageQuotasCommand,
    ManageQuotasRequest,
    UnredactMemoryRequest,
)

_AUTH_NOISE = {
    "admin_api_key": "fake-admin-key",
    "mcp_api_key": "fake-mcp-key",
    "is_admin": True,
    "admin_identity": "operator@example.com",
}


def _with_auth_noise(payload: dict) -> dict:
    return {**payload, **_AUTH_NOISE}


class TestManageNamespaceRequestSplat:
    def test_raw_splat_with_admin_api_key_raises(self) -> None:
        arguments = _with_auth_noise({"command": ManageNamespaceCommand.list})
        with pytest.raises(ValidationError, match="admin_api_key"):
            ManageNamespaceRequest(**arguments)

    def test_model_kwargs_splat_succeeds(self) -> None:
        arguments = _with_auth_noise({"command": ManageNamespaceCommand.list})
        payload = ManageNamespaceRequest(**model_kwargs(arguments))
        assert payload.command == ManageNamespaceCommand.list


class TestManageQuotasRequestSplat:
    def test_raw_splat_with_admin_api_key_raises(self) -> None:
        arguments = _with_auth_noise(
            {"command": ManageQuotasCommand.list, "namespace_id": str(uuid4())}
        )
        with pytest.raises(ValidationError, match="admin_api_key"):
            ManageQuotasRequest(**arguments)

    def test_model_kwargs_splat_succeeds(self) -> None:
        namespace_id = uuid4()
        arguments = _with_auth_noise(
            {"command": ManageQuotasCommand.list, "namespace_id": str(namespace_id)}
        )
        req = ManageQuotasRequest(**model_kwargs(arguments))
        assert req.command == ManageQuotasCommand.list
        assert req.namespace_id == namespace_id


class TestUnredactMemoryRequestSplat:
    def test_raw_splat_with_admin_api_key_raises(self) -> None:
        arguments = _with_auth_noise(
            {
                "memory_id": str(uuid4()),
                "namespace_id": str(uuid4()),
                "agent_id": "agent-1",
            }
        )
        with pytest.raises(ValidationError, match="admin_api_key"):
            UnredactMemoryRequest(**arguments)

    def test_model_kwargs_splat_succeeds(self) -> None:
        memory_id = uuid4()
        namespace_id = uuid4()
        arguments = _with_auth_noise(
            {
                "memory_id": str(memory_id),
                "namespace_id": str(namespace_id),
                "agent_id": "agent-1",
            }
        )
        req = UnredactMemoryRequest(**model_kwargs(arguments))
        assert req.memory_id == memory_id
        assert req.namespace_id == namespace_id
        assert req.agent_id == "agent-1"
