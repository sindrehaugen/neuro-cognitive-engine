"""
nce.providers.openai_compat
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
LLM provider for any OpenAI-compatible HTTP API.

Covers:
  * OpenAI              — ``https://api.openai.com/v1``
  * Azure OpenAI        — ``https://<deployment>.openai.azure.com/openai/deployments/<model>``
  * DeepSeek            — ``https://api.deepseek.com/v1``   (cost-sensitive deployments) [D3]
  * Moonshot / Kimi     — ``https://api.moonshot.cn/v1``     (large-context clusters)
  * OpenAI-compatible   — any custom endpoint (Ollama, vLLM, LM Studio, …)

Structured output strategy
--------------------------
Where the upstream supports it (OpenAI >=2024-08 models, Azure parity),
we use the ``response_format.json_schema`` mode for strict schema adherence.
For models / endpoints that do not support it we fall back to
``"type": "json_object"`` with the schema embedded in the system prompt.
The raw ``choices[0].message.content`` string is always validated with
Pydantic V2 before return.

Azure particulars
-----------------
Azure OpenAI uses a different URL shape and authenticates either via
``api-key`` header (API key) or ``Authorization: Bearer <AAD token>``
(Azure AD / managed identity).  Pass ``azure_api_version`` if the default
is too new for your deployment.
"""

from __future__ import annotations

import json
import logging

from nce.providers._http_utils import post_with_error_handling
from nce.providers.base import (
    LLMProvider,
    LLMProviderError,
    LLMRetriesExhaustedError,
    Message,
    ResponseModelT,
    _redact_api_key,
    validate_base_url,
)
from nce.providers.local_cognitive import _parse_and_validate

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 120.0


class OpenAICompatProvider(LLMProvider):
    """Generic OpenAI-compatible provider.

    Parameters
    ----------
    base_url:
        API base URL **without** trailing slash.
        Example: ``"https://api.openai.com/v1"``
    api_key:
        API key sent as ``Authorization: Bearer <api_key>`` (or
        ``api-key: <api_key>`` for Azure).
    model:
        Model name, e.g. ``"gpt-5"``, ``"deepseek-v4"``, ``"kimi-2.6"``.
    provider_name:
        Short label for the ``model_identifier()`` string, e.g.
        ``"openai"``, ``"azure_openai"``, ``"deepseek"``.
    is_azure:
        Set to ``True`` when targeting Azure OpenAI.  Changes the auth
        header from ``Authorization: Bearer`` to ``api-key:``.
    azure_api_version:
        ``api-version`` query parameter required by Azure OpenAI.
        Defaults to ``"2024-10-21"``.
    use_strict_json_schema:
        Override auto-detection.  ``True`` → use ``json_schema`` mode;
        ``False`` → fall back to ``json_object`` + prompt embedding.
        ``None`` (default) → attempt ``json_schema``, catch upstream
        rejection, retry with ``json_object``.
    timeout:
        Request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        provider_name: str = "openai_compat",
        is_azure: bool = False,
        azure_api_version: str = "2024-10-21",
        use_strict_json_schema: bool | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        # SSRF guard — reject private / loopback IPs and enforce HTTPS.
        validate_base_url(base_url)

        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._provider_name = provider_name
        self._is_azure = is_azure
        self._azure_api_version = azure_api_version
        self._use_strict_json_schema = use_strict_json_schema
        self._timeout = timeout

    # ------------------------------------------------------------------
    # LLMProvider interface
    # ------------------------------------------------------------------

    async def complete(
        self,
        messages: list[Message],
        response_model: type[ResponseModelT],
    ) -> ResponseModelT:
        schema = response_model.model_json_schema()
        msg_dicts = [{"role": m.role.value, "content": m.content} for m in messages]

        strict = self._use_strict_json_schema
        if strict is None:
            strict = True  # attempt strict first

        for attempt in range(2):
            body = self._build_request_body(msg_dicts, schema, response_model.__name__, strict)
            try:
                raw = await self.execute_with_retry(
                    lambda b=body: self._post(b),
                )
            except LLMProviderError as exc:
                if isinstance(exc, LLMRetriesExhaustedError):
                    raise
                if (
                    attempt == 0
                    and strict
                    and exc.status_code in (400, 422)
                    and self._use_strict_json_schema is None
                ):
                    # Endpoint rejected json_schema mode — retry with json_object
                    log.debug(
                        "%s: json_schema mode rejected (HTTP %s), retrying with json_object",
                        self.model_identifier(),
                        exc.status_code,
                    )
                    strict = False
                    continue
                raise
            break  # success on this attempt

        return _parse_and_validate(raw, response_model, self.model_identifier())  # type: ignore[arg-type]

    def model_identifier(self) -> str:
        return f"{self._provider_name}/{self._model}"

    def __repr__(self) -> str:
        parts = [
            f"model={self._model!r}",
            f"provider={self._provider_name!r}",
            f"api_key={_redact_api_key(self._api_key)!r}",
        ]
        if self._is_azure:
            parts.append("azure=True")
        return f"OpenAICompatProvider({', '.join(parts)})"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_request_body(
        self,
        messages: list,
        schema: dict,
        schema_name: str,
        strict: bool,
    ) -> dict:
        if strict:
            response_format: dict = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            }
        else:
            # Embed schema in system prompt and request untyped JSON.
            schema_str = json.dumps(schema, indent=2)
            messages = list(messages)  # shallow copy
            messages.insert(
                0,
                {
                    "role": "system",
                    "content": (
                        "You MUST return ONLY valid JSON that strictly matches "
                        f"this JSON Schema. No markdown. No commentary.\n\n{schema_str}"
                    ),
                },
            )
            response_format = {"type": "json_object"}

        return {
            "model": self._model,
            "messages": messages,
            "response_format": response_format,
        }

    def _build_headers(self) -> dict:
        if self._is_azure:
            return {
                "api-key": self._api_key,
                "Content-Type": "application/json",
            }
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _build_url(self) -> str:
        if self._is_azure:
            return f"{self._base_url}/chat/completions?api-version={self._azure_api_version}"
        return f"{self._base_url}/chat/completions"

    async def _post(self, body: dict) -> str:
        data = await post_with_error_handling(
            url=self._build_url(),
            body=body,
            timeout=self._timeout,
            model_id=self.model_identifier(),
            headers=self._build_headers(),
            error_prefix=f"HTTP request to {self.model_identifier()} failed",
        )

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMProviderError(
                f"{self.model_identifier()} response missing expected structure: {str(data)[:300]}",
                provider=self.model_identifier(),
            ) from exc

        return content
