"""Tests for nce.mcp_errors — @mcp_handler exception mapping and security."""

from __future__ import annotations

import uuid

import pytest

from nce.config import cfg
from nce.mcp_errors import (
    MCP_INTERNAL_ERROR,
    MCP_INVALID_PARAMS,
    MCP_QUOTA_EXCEEDED,
    McpError,
    mcp_handler,
)


@mcp_handler
async def raises_validation_error(engine, arguments):
    from pydantic import BaseModel

    class M(BaseModel, extra="forbid"):
        x: int

    M(x="not_an_int")


@mcp_handler
async def raises_quota_exceeded(engine, arguments):
    from nce.quotas import QuotaExceededError

    raise QuotaExceededError("Quota exceeded for namespace=abc resource='memory' (daily limit)")


@mcp_handler
async def raises_key_error(engine, arguments):
    raise KeyError("secret_field")


@mcp_handler
async def raises_value_error(engine, arguments):
    raise ValueError("namespace_id is required")


@mcp_handler
async def raises_type_error(engine, arguments):
    raise TypeError("expected str, got int")


@mcp_handler
async def raises_generic_exception(engine, arguments):
    raise RuntimeError("database connection failed: postgresql://user:pass@host/db")


@mcp_handler
async def raises_scope_error(engine, arguments):
    from nce.auth import ScopeError

    raise ScopeError("admin scope required")


@mcp_handler
async def raises_mcp_error(engine, arguments):
    raise McpError(-32999, "Custom error")


@mcp_handler
async def returns_value(engine, arguments):
    return "success"


class TestMcpHandlerMapping:
    @pytest.mark.asyncio
    async def test_success_passes_through(self):
        result = await returns_value(None, {})
        assert result == "success"

    @pytest.mark.asyncio
    async def test_validation_error_maps_to_invalid_params(self):
        with pytest.raises(McpError) as ei:
            await raises_validation_error(None, {})
        assert ei.value.code == MCP_INVALID_PARAMS
        assert ei.value.data["reason"] == "validation_error"
        assert isinstance(ei.value.data["errors"], list)

    @pytest.mark.asyncio
    async def test_quota_exceeded_maps_to_quota_code(self):
        with pytest.raises(McpError) as ei:
            await raises_quota_exceeded(None, {})
        assert ei.value.code == MCP_QUOTA_EXCEEDED
        assert ei.value.data["reason"] == "quota_exceeded"

    @pytest.mark.asyncio
    async def test_key_error_maps_to_invalid_params_missing_field(self):
        with pytest.raises(McpError) as ei:
            await raises_key_error(None, {})
        assert ei.value.code == MCP_INVALID_PARAMS
        assert ei.value.data["reason"] == "missing_field"

    @pytest.mark.asyncio
    async def test_value_error_maps_to_invalid_params(self):
        with pytest.raises(McpError) as ei:
            await raises_value_error(None, {})
        assert ei.value.code == MCP_INVALID_PARAMS
        assert ei.value.data["reason"] == "invalid_arguments"

    @pytest.mark.asyncio
    async def test_type_error_maps_to_invalid_params(self):
        with pytest.raises(McpError) as ei:
            await raises_type_error(None, {})
        assert ei.value.code == MCP_INVALID_PARAMS

    @pytest.mark.asyncio
    async def test_generic_exception_maps_to_internal_error(self):
        with pytest.raises(McpError) as ei:
            await raises_generic_exception(None, {})
        assert ei.value.code == MCP_INTERNAL_ERROR

    @pytest.mark.asyncio
    async def test_scope_error_propagates_unchanged(self):
        from nce.auth import ScopeError

        with pytest.raises(ScopeError):
            await raises_scope_error(None, {})

    @pytest.mark.asyncio
    async def test_mcp_error_propagates_unchanged(self):
        with pytest.raises(McpError) as ei:
            await raises_mcp_error(None, {})
        assert ei.value.code == -32999


class TestSecurityGuarantees:
    @pytest.mark.asyncio
    async def test_internal_error_does_not_leak_exception_text_in_prod(self, monkeypatch):
        monkeypatch.setattr(cfg, "IS_DEV", False)
        with pytest.raises(McpError) as ei:
            await raises_generic_exception(None, {})
        assert "detail" not in ei.value.data
        assert "postgresql://" not in str(ei.value.data)
        assert "user:pass" not in str(ei.value.data)

    @pytest.mark.asyncio
    async def test_internal_error_includes_detail_in_dev(self, monkeypatch):
        monkeypatch.setattr(cfg, "IS_DEV", True)
        with pytest.raises(McpError) as ei:
            await raises_generic_exception(None, {})
        assert "detail" in ei.value.data

    @pytest.mark.asyncio
    async def test_internal_error_includes_request_id(self, monkeypatch):
        monkeypatch.setattr(cfg, "IS_DEV", False)
        with pytest.raises(McpError) as ei:
            await raises_generic_exception(None, {})
        assert "request_id" in ei.value.data
        uuid.UUID(ei.value.data["request_id"])

    @pytest.mark.asyncio
    async def test_key_error_does_not_expose_field_name(self):
        with pytest.raises(McpError) as ei:
            await raises_key_error(None, {})
        assert "secret_field" not in str(ei.value.data)

    @pytest.mark.asyncio
    async def test_value_error_detail_not_in_response(self, monkeypatch):
        monkeypatch.setattr(cfg, "IS_DEV", False)

        @mcp_handler
        async def leaky(engine, arguments):
            raise ValueError("token=supersecret namespace_id=abc")

        with pytest.raises(McpError) as ei:
            await leaky(None, {})
        assert "supersecret" not in str(ei.value.data)
        assert "detail" not in ei.value.data


class TestStructuredReasons:
    @pytest.mark.asyncio
    async def test_internal_error_includes_reason_field(self, monkeypatch):
        monkeypatch.setattr(cfg, "IS_DEV", False)
        with pytest.raises(McpError) as ei:
            await raises_generic_exception(None, {})
        assert ei.value.data["reason"] == "internal_error"

    @pytest.mark.asyncio
    async def test_quota_error_includes_reason(self):
        with pytest.raises(McpError) as ei:
            await raises_quota_exceeded(None, {})
        assert ei.value.data["reason"] == "quota_exceeded"

    @pytest.mark.asyncio
    async def test_validation_error_includes_structured_errors(self):
        with pytest.raises(McpError) as ei:
            await raises_validation_error(None, {})
        errors = ei.value.data["errors"]
        assert isinstance(errors, list)
        assert len(errors) > 0
        assert "url" not in str(errors)


class TestDecoratorBehavior:
    @pytest.mark.asyncio
    async def test_sync_handler_works(self):
        @mcp_handler
        def sync_handler(engine, arguments):
            return "sync_ok"

        result = await sync_handler(None, {})
        assert result == "sync_ok"

    @pytest.mark.asyncio
    async def test_return_value_preserved_async(self):
        result = await returns_value(None, {})
        assert result == "success"

    @pytest.mark.asyncio
    async def test_every_exception_becomes_mcp_error(self):
        @mcp_handler
        async def raises_os_error(engine, arguments):
            raise OSError("disk full")

        with pytest.raises(McpError) as ei:
            await raises_os_error(None, {})
        assert ei.value.code == MCP_INTERNAL_ERROR
