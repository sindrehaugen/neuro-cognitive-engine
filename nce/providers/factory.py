"""
nce.providers.factory
~~~~~~~~~~~~~~~~~~~~~~~~~
Resolves and instantiates an ``LLMProvider`` from either namespace metadata
or global environment configuration.

Resolution order
----------------
1. ``namespace_metadata["consolidation"]["llm_provider"]``   (per-namespace)
2. ``namespace_metadata["consolidation"]["llm_model"]``      (per-namespace)
3. ``namespace_metadata["consolidation"]["llm_credentials"]`` (per-namespace)
4. ``cfg.NCE_LLM_PROVIDER``                               (global default, [D2])

Credential references (per D3 — BYO keys, no shared platform key)
------------------------------------------------------------------
Credential strings follow the pattern:
  ``ref:env/<ENV_VAR_NAME>``  — read from the named environment variable
  ``ref:vault/<path>``        — reserved for Phase 3 Vault integration (not yet implemented)
  ``<literal>``               — used as-is (only in development; warn in prod)

Provider labels
---------------
  ``local-cognitive-model``   — LocalCognitiveProvider (default [D2])
  ``openai``                  — OpenAICompatProvider (OpenAI)
  ``azure_openai``            — OpenAICompatProvider (Azure)
  ``deepseek``                — OpenAICompatProvider (DeepSeek)
  ``moonshot_kimi``           — OpenAICompatProvider (Moonshot Kimi)
  ``openai_compatible``       — OpenAICompatProvider (arbitrary endpoint)
  ``anthropic``               — AnthropicProvider
  ``google_gemini``           — GoogleGeminiProvider
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from nce.providers.base import LLMProvider, LLMProviderError

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def get_provider(namespace_metadata: dict[str, Any] | None = None) -> LLMProvider:
    """Return the correct ``LLMProvider`` for a namespace / global config.

    Parameters
    ----------
    namespace_metadata:
        The ``metadata`` JSONB column value from the ``namespaces`` table.
        May be ``None`` (uses global defaults).

    Raises
    ------
    LLMProviderError
        If the resolved provider label is unknown or required credentials
        are missing.
    """
    consolidation_cfg = (namespace_metadata or {}).get("consolidation", {})

    provider_label = consolidation_cfg.get("llm_provider") or _global_provider_label()
    model = consolidation_cfg.get("llm_model") or None
    cred_ref = consolidation_cfg.get("llm_credentials") or None

    log.debug("Resolving LLM provider: label=%r model=%r", provider_label, model)
    return _cached_build_provider(provider_label, model, cred_ref)


@lru_cache(maxsize=128)
def _cached_build_provider(
    label: str,
    model: str | None,
    cred_ref: str | None,
) -> LLMProvider:
    """Memoize concrete providers — prevents repeated TLS/HTTP stack setup (FIX-033)."""

    return _build_provider(label, model=model, cred_ref=cred_ref)


# Deferred factory functions to avoid loading unused heavy dependencies at module load.
def _create_local_cognitive(model: str | None, cred_ref: str | None) -> LLMProvider:
    from nce.config import cfg
    from nce.providers.local_cognitive import LocalCognitiveProvider

    base_url = cfg.NCE_COGNITIVE_BASE_URL or "http://localhost:11435"
    return LocalCognitiveProvider(
        base_url=base_url,
        model=model or "local-cognitive-model",
    )


def _create_anthropic(model: str | None, cred_ref: str | None) -> LLMProvider:
    from nce.providers.anthropic_provider import AnthropicProvider

    api_key = _resolve_credential(cred_ref, env_fallback="NCE_ANTHROPIC_API_KEY")
    return AnthropicProvider(
        api_key=api_key,
        model=model or "claude-opus-4-6",
    )


def _create_openai(model: str | None, cred_ref: str | None) -> LLMProvider:
    from nce.providers.openai_compat import OpenAICompatProvider

    api_key = _resolve_credential(cred_ref, env_fallback="NCE_OPENAI_API_KEY")
    return OpenAICompatProvider(
        base_url="https://api.openai.com/v1",
        api_key=api_key,
        model=model or "gpt-5",
        provider_name="openai",
    )


def _create_azure_openai(model: str | None, cred_ref: str | None) -> LLMProvider:
    from nce.config import cfg
    from nce.providers.openai_compat import OpenAICompatProvider

    api_key = _resolve_credential(cred_ref, env_fallback="NCE_AZURE_OPENAI_API_KEY")
    endpoint = cfg.NCE_AZURE_OPENAI_ENDPOINT
    if not endpoint:
        raise LLMProviderError(
            "azure_openai provider requires NCE_AZURE_OPENAI_ENDPOINT",
            provider=f"azure_openai/{model}",
        )
    deployment = cfg.NCE_AZURE_OPENAI_DEPLOYMENT or model or "gpt-5"
    return OpenAICompatProvider(
        base_url=f"{endpoint.rstrip('/')}/openai/deployments/{deployment}",
        api_key=api_key,
        model=deployment,
        provider_name="azure_openai",
        is_azure=True,
    )


def _create_deepseek(model: str | None, cred_ref: str | None) -> LLMProvider:
    from nce.providers.openai_compat import OpenAICompatProvider

    api_key = _resolve_credential(cred_ref, env_fallback="NCE_DEEPSEEK_API_KEY")
    return OpenAICompatProvider(
        base_url="https://api.deepseek.com/v1",
        api_key=api_key,
        model=model or "deepseek-v4",
        provider_name="deepseek",
    )


def _create_moonshot_kimi(model: str | None, cred_ref: str | None) -> LLMProvider:
    from nce.providers.openai_compat import OpenAICompatProvider

    api_key = _resolve_credential(cred_ref, env_fallback="NCE_MOONSHOT_API_KEY")
    return OpenAICompatProvider(
        base_url="https://api.moonshot.cn/v1",
        api_key=api_key,
        model=model or "kimi-2.6",
        provider_name="moonshot_kimi",
    )


def _create_google_gemini(model: str | None, cred_ref: str | None) -> LLMProvider:
    from nce.providers.google_gemini import GoogleGeminiProvider

    api_key = _resolve_credential(cred_ref, env_fallback="NCE_GEMINI_API_KEY")
    return GoogleGeminiProvider(
        api_key=api_key,
        model=model or "gemini-2.0-flash",
    )


def _create_openai_compatible(model: str | None, cred_ref: str | None) -> LLMProvider:
    from nce.config import cfg
    from nce.providers.openai_compat import OpenAICompatProvider

    api_key = _resolve_credential(cred_ref, env_fallback="NCE_OPENAI_COMPAT_API_KEY")
    base_url = cfg.NCE_OPENAI_COMPAT_BASE_URL
    if not base_url:
        raise LLMProviderError(
            "openai_compatible provider requires NCE_OPENAI_COMPAT_BASE_URL",
            provider="openai_compatible",
        )
    compat_model = model or cfg.NCE_OPENAI_COMPAT_MODEL or "default"
    return OpenAICompatProvider(
        base_url=base_url,
        api_key=api_key,
        model=compat_model,
        provider_name="openai_compatible",
    )


_FACTORIES: dict[str, Callable[[str | None, str | None], LLMProvider]] = {
    "local-cognitive-model": _create_local_cognitive,
    "anthropic": _create_anthropic,
    "openai": _create_openai,
    "azure_openai": _create_azure_openai,
    "deepseek": _create_deepseek,
    "moonshot_kimi": _create_moonshot_kimi,
    "google_gemini": _create_google_gemini,
    "openai_compatible": _create_openai_compatible,
}


def _build_provider(
    label: str,
    *,
    model: str | None,
    cred_ref: str | None,
) -> LLMProvider:
    factory = _FACTORIES.get(label)
    if not factory:
        raise LLMProviderError(
            f"Unknown LLM provider label: {label!r}.  "
            "Valid values: local-cognitive-model, openai, azure_openai, deepseek, "
            "moonshot_kimi, google_gemini, anthropic, openai_compatible",
            provider=label,
        )
    return factory(model, cred_ref)


# ---------------------------------------------------------------------------
# Credential resolution helpers
# ---------------------------------------------------------------------------


def _resolve_credential(cred_ref: str | None, *, env_fallback: str) -> str:
    """Resolve a credential string to a plain API key.

    Supports:
      * ``None`` / empty → fall back to *env_fallback* environment variable
      * ``"ref:env/<ENV_VAR>"`` → read the named env var
      * ``"ref:vault/<path>"`` → not yet implemented; raises
      * Anything else → treat as a literal key (logs a warning)
    """
    from nce.config import cfg

    if not cred_ref:
        value = os.getenv(env_fallback, "")
        if not value:
            msg = f"LLM provider credential not set. Set the {env_fallback} environment variable."
            if cfg.IS_PROD:
                raise LLMProviderError(msg, provider="factory")
            log.warning("%s", msg)
        return value

    if cred_ref.startswith("ref:env/"):
        var_name = cred_ref[len("ref:env/") :]
        value = os.getenv(var_name, "")
        if not value:
            msg = f"Credential reference {cred_ref!r} resolved to empty string."
            if cfg.IS_PROD:
                raise LLMProviderError(msg, provider="factory")
            log.warning("%s", msg)
        return value

    if cred_ref.startswith("ref:vault/"):
        raise LLMProviderError(
            f"Vault credential references ({cred_ref!r}) are not yet implemented. "
            "Use ref:env/ for now.",
            provider="factory",
        )

    # SECURITY: Literal credential path — log only generic text; never append, format,
    # or interpolate ``cred_ref`` (the raw key) into log messages or exceptions.
    # Future debugging must use redaction (e.g. ``_redact_api_key``) if any key
    # material is ever logged.
    if cfg.IS_PROD:
        raise LLMProviderError(
            "Literal LLM credentials in namespace metadata are forbidden in production. "
            "Use ref:env/<ENV_VAR> instead.",
            provider="factory",
        )
    log.warning(
        "LLM credential is a literal string, not a ref:env/ reference.  "
        "Avoid storing keys directly in namespace metadata.",
    )
    return cred_ref


def _global_provider_label() -> str:
    from nce.config import cfg

    return cfg.NCE_LLM_PROVIDER or "local-cognitive-model"


def rebuild_provider_cache() -> str:
    """Clear the LRU cache of build provider instances, forcing them to rebuild with new config."""
    _cached_build_provider.cache_clear()
    label = _global_provider_label()
    log.info("Cleared LLM provider factory cache. Next resolution will use provider: %s", label)
    return f"provider rebuilt ({label})"
