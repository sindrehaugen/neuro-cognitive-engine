"""
Tests for malformed LLM response handling (P6 — Test Coverage).

Verifies that all LLM providers correctly raise ``LLMProviderError`` (or
``LLMTimeoutError`` / ``LLMUpstreamError``) when the upstream API returns
non-JSON, structurally invalid JSON, empty responses, or error HTTP statuses.

Uses ``pytest-httpx`` to mock the httpx transport layer — no real HTTP calls.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

# ---------------------------------------------------------------------------
# Shared test models
# ---------------------------------------------------------------------------
from pydantic import BaseModel

import nce.providers.base
from nce.providers.anthropic_provider import AnthropicProvider
from nce.providers.base import LLMProviderError, LLMTimeoutError
from nce.providers.openai_compat import OpenAICompatProvider


class _DummyResponse(BaseModel):
    """Minimal Pydantic model that providers try to populate from tool calls."""

    result: str


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def anthropic_provider() -> AnthropicProvider:
    provider = AnthropicProvider(
        api_key="sk-test-fake-key-12345",
        model="claude-sonnet-4-20250514",
    )
    # Disable retries — these tests verify error classification, not retry logic.
    provider._retry_policy = nce.providers.base.RetryPolicy(max_retries=0)
    return provider


@pytest.fixture
def openai_provider() -> OpenAICompatProvider:
    provider = OpenAICompatProvider(
        api_key="sk-test-fake-key-12345",
        model="gpt-4o",
        base_url="https://api.openai.com/v1",
    )
    # Disable retries — these tests verify error classification, not retry logic.
    provider._retry_policy = nce.providers.base.RetryPolicy(max_retries=0)
    return provider


# ---------------------------------------------------------------------------
# Malformed response scenarios — Anthropic
# ---------------------------------------------------------------------------


class TestAnthropicMalformedResponses:
    """Inject garbage, empty, and structurally invalid JSON into Anthropic responses."""

    @pytest.mark.asyncio
    async def test_non_json_garbage(
        self,
        anthropic_provider: AnthropicProvider,
        httpx_mock: Any,
    ):
        """Non-JSON response body should raise LLMProviderError."""
        httpx_mock.add_response(
            url=f"{anthropic_provider._base_url}/v1/messages",
            method="POST",
            status_code=200,
            text="not-json-at-all-!!!!",
        )
        with pytest.raises(LLMProviderError, match="non-JSON"):
            await anthropic_provider.complete(
                messages=[],
                response_model=_DummyResponse,
            )

    @pytest.mark.asyncio
    async def test_empty_json_object(
        self,
        anthropic_provider: AnthropicProvider,
        httpx_mock: Any,
    ):
        """Valid JSON {} but missing content/tool_use keys should raise LLMProviderError."""
        httpx_mock.add_response(
            url=f"{anthropic_provider._base_url}/v1/messages",
            method="POST",
            status_code=200,
            json={},
        )
        with pytest.raises(LLMProviderError, match="did not call tool"):
            await anthropic_provider.complete(
                messages=[],
                response_model=_DummyResponse,
            )

    @pytest.mark.asyncio
    async def test_missing_tool_use_block(
        self,
        anthropic_provider: AnthropicProvider,
        httpx_mock: Any,
    ):
        """Valid JSON with content but no tool_use should raise LLMProviderError."""
        httpx_mock.add_response(
            url=f"{anthropic_provider._base_url}/v1/messages",
            method="POST",
            status_code=200,
            json={
                "content": [{"type": "text", "text": "Hello"}],
                "stop_reason": "end_turn",
            },
        )
        with pytest.raises(LLMProviderError, match="did not call tool"):
            await anthropic_provider.complete(
                messages=[],
                response_model=_DummyResponse,
            )

    @pytest.mark.asyncio
    async def test_http_500_upstream_error(
        self,
        anthropic_provider: AnthropicProvider,
        httpx_mock: Any,
    ):
        """HTTP 500 should raise LLMProviderError with status_code."""
        httpx_mock.add_response(
            url=f"{anthropic_provider._base_url}/v1/messages",
            method="POST",
            status_code=500,
            text="Internal Server Error",
        )
        with pytest.raises(LLMProviderError, match="HTTP 500"):
            await anthropic_provider.complete(
                messages=[],
                response_model=_DummyResponse,
            )

    @pytest.mark.asyncio
    async def test_timeout(
        self,
        anthropic_provider: AnthropicProvider,
        httpx_mock: Any,
    ):
        """Network timeout should raise LLMTimeoutError."""
        httpx_mock.add_exception(
            httpx.TimeoutException("Connection timed out"),
            url=f"{anthropic_provider._base_url}/v1/messages",
            method="POST",
        )
        with pytest.raises(LLMTimeoutError):
            await anthropic_provider.complete(
                messages=[],
                response_model=_DummyResponse,
            )


# ---------------------------------------------------------------------------
# Malformed response scenarios — OpenAI Compat
# ---------------------------------------------------------------------------


class TestOpenAIMalformedResponses:
    """Inject garbage, empty, and structurally invalid JSON into OpenAI responses."""

    @pytest.mark.asyncio
    async def test_non_json_garbage(
        self,
        openai_provider: OpenAICompatProvider,
        httpx_mock: Any,
    ):
        """Non-JSON response body should raise LLMProviderError."""
        httpx_mock.add_response(
            url=f"{openai_provider._base_url}/chat/completions",
            method="POST",
            status_code=200,
            text="<html>not-json</html>",
        )
        with pytest.raises(LLMProviderError, match="non-JSON"):
            await openai_provider.complete(
                messages=[],
                response_model=_DummyResponse,
            )

    @pytest.mark.asyncio
    async def test_empty_json_object(
        self,
        openai_provider: OpenAICompatProvider,
        httpx_mock: Any,
    ):
        """Valid JSON {} but no choices array should raise LLMProviderError."""
        httpx_mock.add_response(
            url=f"{openai_provider._base_url}/chat/completions",
            method="POST",
            status_code=200,
            json={},
        )
        with pytest.raises(LLMProviderError, match="missing expected structure"):
            await openai_provider.complete(
                messages=[],
                response_model=_DummyResponse,
            )

    @pytest.mark.asyncio
    async def test_missing_choices(
        self,
        openai_provider: OpenAICompatProvider,
        httpx_mock: Any,
    ):
        """Valid JSON with choices array but no content key should raise LLMProviderError."""
        httpx_mock.add_response(
            url=f"{openai_provider._base_url}/chat/completions",
            method="POST",
            status_code=200,
            json={"choices": [{"index": 0, "message": {}}]},
        )
        with pytest.raises(LLMProviderError, match="missing expected structure"):
            await openai_provider.complete(
                messages=[],
                response_model=_DummyResponse,
            )

    @pytest.mark.asyncio
    async def test_http_429_rate_limit(
        self,
        openai_provider: OpenAICompatProvider,
        httpx_mock: Any,
    ):
        """HTTP 429 should raise LLMProviderError (not rate limit subclass here)."""
        httpx_mock.add_response(
            url=f"{openai_provider._base_url}/chat/completions",
            method="POST",
            status_code=429,
            text="Rate limit exceeded",
        )
        with pytest.raises(LLMProviderError, match="HTTP 429"):
            await openai_provider.complete(
                messages=[],
                response_model=_DummyResponse,
            )

    @pytest.mark.asyncio
    async def test_http_401_authentication_error(
        self,
        openai_provider: OpenAICompatProvider,
        httpx_mock: Any,
    ):
        """HTTP 401 should raise LLMProviderError."""
        httpx_mock.add_response(
            url=f"{openai_provider._base_url}/chat/completions",
            method="POST",
            status_code=401,
            text="Unauthorized",
        )
        with pytest.raises(LLMProviderError, match="authentication failed"):
            await openai_provider.complete(
                messages=[],
                response_model=_DummyResponse,
            )
