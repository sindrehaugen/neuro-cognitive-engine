"""
nce.providers.local_cognitive
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
LLM provider for the bundled local cognitive model [D2 / D7].

The bundled image is ``ghcr.io/sindrehaugen/nce-cognitive:v1`` and exposes
an OpenAI-compatible HTTP API on ``localhost:11435``.  Detection happens via a
``GET /health`` probe at startup; if the endpoint is unavailable the factory
logs a warning and the caller is responsible for graceful skip.

Structured output uses ``response_format={"type": "json_object"}`` with the
Pydantic schema embedded in the system prompt.  The raw content string is
validated with Pydantic V2 before return.
"""

from __future__ import annotations

import json
import logging

import httpx
from pydantic import ValidationError

from nce.providers._http_utils import post_with_error_handling
from nce.providers.base import (
    LLMProvider,
    LLMProviderError,
    LLMValidationError,
    Message,
    ResponseModelT,
    validate_base_url,
)

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 60.0
_DEFAULT_MODEL = "local-cognitive-model"


class LocalCognitiveProvider(LLMProvider):
    """Calls the bundled cognitive model over OpenAI-compatible HTTP.

    Parameters
    ----------
    base_url:
        HTTP base URL of the cognitive container, e.g.
        ``"http://localhost:11435"``.  Trailing slashes are stripped.
    model:
        Model name sent in the request body.  Defaults to
        ``"local-cognitive-model"``.
    timeout:
        Request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str,
        *,
        model: str = _DEFAULT_MODEL,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        # SSRF guard — local cognitive container runs on localhost, relax checks.
        validate_base_url(base_url, allow_http=True, allow_loopback=True)

        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Health probe (optional, called by factory)
    # ------------------------------------------------------------------

    async def is_healthy(self) -> bool:
        """Return True if ``GET /health`` responds with 2xx."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{self._base_url}/health")
                return r.is_success
        except Exception:
            return False

    # ------------------------------------------------------------------
    # LLMProvider interface
    # ------------------------------------------------------------------

    async def complete(
        self,
        messages: list[Message],
        response_model: type[ResponseModelT],
    ) -> ResponseModelT:
        payload = {
            "model": self._model,
            "messages": [{"role": m.role.value, "content": m.content} for m in messages],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": response_model.__name__,
                    "strict": True,
                    "schema": response_model.model_json_schema(),
                },
            },
        }

        data = await self.execute_with_retry(
            lambda: post_with_error_handling(
                url=f"{self._base_url}/v1/chat/completions",
                body=payload,
                timeout=self._timeout,
                model_id=self.model_identifier(),
                error_prefix="HTTP request to local cognitive model failed",
            ),
        )

        try:
            content = data["choices"][0]["message"]["content"]  # type: ignore[index]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMProviderError(
                f"Local cognitive model response missing expected structure: {str(data)[:300]}",
                provider=self.model_identifier(),
            ) from exc

        return _parse_and_validate(content, response_model, self.model_identifier())

    def model_identifier(self) -> str:
        return f"local/{self._model}"

    def __repr__(self) -> str:
        return f"LocalCognitiveProvider(model={self._model!r}, base_url={self._base_url!r})"


# ---------------------------------------------------------------------------
# Shared validation helper (used by multiple providers)
# ---------------------------------------------------------------------------


def _parse_and_validate(
    raw_content: str,
    response_model: type[ResponseModelT],
    provider_id: str,
) -> ResponseModelT:
    """Parse *raw_content* as JSON and validate against *response_model*.

    Raises
    ------
    LLMValidationError
        When the JSON parses but fails Pydantic validation.
    LLMProviderError
        When the JSON itself is malformed.
    """
    try:
        return response_model.model_validate_json(raw_content)
    except ValidationError as exc:
        raise LLMValidationError(
            f"Model response failed Pydantic validation for {response_model.__name__}: {exc}",
            provider=provider_id,
        ) from exc
    except json.JSONDecodeError as exc:
        raise LLMProviderError(
            f"Model returned non-JSON content: {raw_content[:200]!r}",
            provider=provider_id,
        ) from exc
