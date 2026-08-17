"""
nce.providers.anthropic_provider
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
LLM provider for Anthropic Claude models.

Structured output strategy
--------------------------
We use Anthropic's ``tool_use`` mechanism with a single synthetic tool whose
``input_schema`` is the Pydantic model's JSON schema.  Forcing the model to
call this tool yields a well-typed ``input`` dict that we then validate with
``response_model.model_validate()``.

This is more reliable than plain ``json_object`` prompting because the
model cannot produce non-JSON preamble when constrained to a tool call.

Reference:
  https://docs.anthropic.com/en/docs/tool-use

Supported models
----------------
  claude-opus-4-6 | claude-sonnet-4-6 | claude-haiku-4-5
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from nce.providers._http_utils import post_with_error_handling
from nce.providers.base import (
    LLMProvider,
    LLMProviderError,
    LLMValidationError,
    Message,
    ResponseModelT,
    _redact_api_key,
    validate_base_url,
)

log = logging.getLogger(__name__)

_ANTHROPIC_API_BASE = "https://api.anthropic.com"
_ANTHROPIC_VERSION = "2023-06-01"
_DEFAULT_TIMEOUT = 120.0
_DEFAULT_MAX_TOKENS = 4096


class AnthropicProvider(LLMProvider):
    """Calls Anthropic's Messages API with forced tool_use for structured output.

    Parameters
    ----------
    api_key:
        Anthropic API key (``NCE_ANTHROPIC_API_KEY``).
    model:
        Anthropic model name, e.g. ``"claude-opus-4-6"``.
    max_tokens:
        Maximum tokens in the completion.
    timeout:
        Request timeout in seconds.
    base_url:
        Override the API base URL (useful for testing / proxies).
    """

    def __init__(
        self,
        api_key: str,
        model: str = "claude-opus-4-6",
        *,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        timeout: float = _DEFAULT_TIMEOUT,
        base_url: str = _ANTHROPIC_API_BASE,
    ) -> None:
        # SSRF guard — reject private / loopback IPs and enforce HTTPS.
        validate_base_url(base_url)

        self._api_key = api_key
        self._model = model
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._base_url = base_url.rstrip("/")

    # ------------------------------------------------------------------
    # LLMProvider interface
    # ------------------------------------------------------------------

    async def complete(
        self,
        messages: list[Message],
        response_model: type[ResponseModelT],
    ) -> ResponseModelT:
        tool_name = f"structured_{response_model.__name__.lower()}"
        schema = response_model.model_json_schema()

        # Separate system messages from the conversation turns.
        system_parts = [m.content for m in messages if m.role.value == "system"]
        turn_messages = [
            {"role": m.role.value, "content": m.content}
            for m in messages
            if m.role.value != "system"
        ]
        system_prompt: str | None = "\n\n".join(system_parts) or None

        body: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": turn_messages,
            "tools": [
                {
                    "name": tool_name,
                    "description": f"Return a structured {response_model.__name__} object.",
                    "input_schema": schema,
                }
            ],
            # Force the model to always call the tool.
            "tool_choice": {"type": "tool", "name": tool_name},
        }
        if system_prompt:
            body["system"] = system_prompt

        data = await self.execute_with_retry(
            lambda: post_with_error_handling(
                url=f"{self._base_url}/v1/messages",
                body=body,
                timeout=self._timeout,
                model_id=self.model_identifier(),
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": _ANTHROPIC_VERSION,
                    "Content-Type": "application/json",
                },
                error_prefix="HTTP request to Anthropic failed",
            ),
        )

        # Locate the tool_use block in the response content list.
        try:
            tool_input = self._extract_tool_input(data, tool_name)  # type: ignore[arg-type]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMProviderError(
                f"Anthropic response missing expected structure: {str(data)[:300]}",
                provider=self.model_identifier(),
            ) from exc

        try:
            return response_model.model_validate(tool_input)
        except ValidationError as exc:
            raise LLMValidationError(
                f"Anthropic tool_use response failed Pydantic validation "
                f"for {response_model.__name__}: {exc}",
                provider=self.model_identifier(),
            ) from exc

    def model_identifier(self) -> str:
        return f"anthropic/{self._model}"

    def __repr__(self) -> str:
        return (
            f"AnthropicProvider(model={self._model!r}, api_key={_redact_api_key(self._api_key)!r})"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_tool_input(self, data: dict, tool_name: str) -> dict:
        """Pull the ``input`` dict from the tool_use block in *data*."""
        content_blocks = data.get("content", [])
        for block in content_blocks:
            if block.get("type") == "tool_use" and block.get("name") == tool_name:
                return block["input"]

        # Fallback: model returned stop_reason text instead of tool call.
        stop_reason = data.get("stop_reason")
        raise LLMProviderError(
            f"Anthropic model did not call tool '{tool_name}' "
            f"(stop_reason={stop_reason!r}).  Full response: {str(data)[:300]}",
            provider=self.model_identifier(),
        )
