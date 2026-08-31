"""
nce/settings_registry.py
========================
BATCH-P5-V.1a — Settings Registry Metadata.

Defines metadata schema and registry for all configuration keys in NCE,
including validators, sections, type descriptions, reload classes, secrecy,
and production-locking statuses.
"""

from __future__ import annotations

import os
from collections.abc import Callable, MutableMapping
from typing import Any, NamedTuple


class SettingMetadata(NamedTuple):
    key: str
    section: str
    type: str  # "str" | "int" | "float" | "bool" | "secret" | "list"
    reload_class: str  # "HOT" | "WARM" | "COLD"
    is_secret: bool
    prod_locked: bool
    validator: Callable[[Any], bool]
    description: str
    default: Any = None


# --- Validator Factories ---


def validate_bool(val: Any) -> bool:
    """Validate that the value is a boolean."""
    return isinstance(val, bool)


def validate_int(minimum: int | None = None) -> Callable[[Any], bool]:
    """Validate that the value is an integer, optionally enforcing a minimum."""

    def _validate(val: Any) -> bool:
        if not isinstance(val, int) or isinstance(val, bool):
            return False
        if minimum is not None and val < minimum:
            return False
        return True

    return _validate


def validate_float(minimum: float | None = None) -> Callable[[Any], bool]:
    """Validate that the value is a float (or int), optionally enforcing a minimum."""

    def _validate(val: Any) -> bool:
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            return False
        if minimum is not None and val < minimum:
            return False
        return True

    return _validate


def validate_str(allow_empty: bool = True) -> Callable[[Any], bool]:
    """Validate that the value is a string, optionally forbidding empty/whitespace-only values."""

    def _validate(val: Any) -> bool:
        if not isinstance(val, str):
            return False
        if not allow_empty and not val.strip():
            return False
        return True

    return _validate


def validate_str_list(val: Any) -> bool:
    """Validate that the value is a list of strings."""
    if not isinstance(val, list):
        return False
    return all(isinstance(item, str) for item in val)


# --- Environment Schema Validation & Defaults Auto-Loading ---


def _coerce_env_value(val_str: str, target_type: str) -> Any:
    """Coerce string environment variable value to the registry's target type."""
    if target_type == "int":
        try:
            return int(val_str)
        except ValueError:
            raise TypeError(f"Value '{val_str}' is not a valid integer")
    elif target_type == "float":
        try:
            return float(val_str)
        except ValueError:
            raise TypeError(f"Value '{val_str}' is not a valid float")
    elif target_type == "bool":
        clean = val_str.strip().lower()
        if clean in {"1", "true", "yes", "on"}:
            return True
        elif clean in {"0", "false", "no", "off"}:
            return False
        else:
            raise TypeError(f"Value '{val_str}' is not a valid boolean")
    elif target_type == "list":
        clean = val_str.strip()
        if clean.startswith("[") and clean.endswith("]"):
            try:
                import json

                parsed = json.loads(clean)
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                pass
        return [item.strip() for item in clean.split(",") if item.strip()]
    return val_str


def validate_env(env_dict: MutableMapping[str, str] | None = None) -> dict[str, str]:
    """Validate environment variables against the registry schema.

    Returns a mapping of key to error message for all failed validations.
    """
    target_env: MutableMapping[str, str] = os.environ if env_dict is None else env_dict

    errors = {}
    for key, meta in REGISTRY.items():
        if key in target_env:
            val_str = target_env[key]
            try:
                coerced = _coerce_env_value(val_str, meta.type)
                if not meta.validator(coerced):
                    errors[key] = f"Validation failed for key '{key}' with value '{val_str}'"
            except (TypeError, ValueError) as e:
                errors[key] = f"Type coercion failed for key '{key}': {e}"
    return errors


def auto_load_defaults(
    env_dict: MutableMapping[str, str] | None = None, overwrite: bool = False
) -> None:
    """Auto-load defaults from registry into the environment mapping if unset."""
    target_env: MutableMapping[str, str] = os.environ if env_dict is None else env_dict

    for key, meta in REGISTRY.items():
        if meta.default is not None:
            if overwrite or key not in target_env or not target_env[key].strip():
                val = meta.default
                if isinstance(val, bool):
                    target_env[key] = "true" if val else "false"
                elif isinstance(val, list):
                    target_env[key] = ",".join(str(item) for item in val)
                else:
                    target_env[key] = str(val)


# --- Settings Registry Mapping ---

REGISTRY: dict[str, SettingMetadata] = {
    # 1. Datastores & connections
    "MONGO_URI": SettingMetadata(
        key="MONGO_URI",
        section="Datastores & connections",
        type="secret",
        reload_class="COLD",
        is_secret=True,
        prod_locked=False,
        validator=validate_str(allow_empty=False),
        description="MongoDB connection URI.",
    ),
    "PG_DSN": SettingMetadata(
        key="PG_DSN",
        section="Datastores & connections",
        type="secret",
        reload_class="COLD",
        is_secret=True,
        prod_locked=False,
        validator=validate_str(allow_empty=False),
        description="PostgreSQL primary DSN connection string.",
    ),
    "DB_READ_URL": SettingMetadata(
        key="DB_READ_URL",
        section="Datastores & connections",
        type="secret",
        reload_class="COLD",
        is_secret=True,
        prod_locked=False,
        validator=validate_str(allow_empty=False),
        description="PostgreSQL read replica connection string.",
    ),
    "DB_WRITE_URL": SettingMetadata(
        key="DB_WRITE_URL",
        section="Datastores & connections",
        type="secret",
        reload_class="COLD",
        is_secret=True,
        prod_locked=False,
        validator=validate_str(allow_empty=False),
        description="PostgreSQL write target connection string.",
    ),
    "PG_BOUNCER_URL": SettingMetadata(
        key="PG_BOUNCER_URL",
        section="Datastores & connections",
        type="secret",
        reload_class="COLD",
        is_secret=True,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="Optional connection string through a connection bouncer.",
    ),
    "REDIS_URL": SettingMetadata(
        key="REDIS_URL",
        section="Datastores & connections",
        type="secret",
        reload_class="COLD",
        is_secret=True,
        prod_locked=False,
        validator=validate_str(allow_empty=False),
        description="Redis connection URL.",
    ),
    "MINIO_ENDPOINT": SettingMetadata(
        key="MINIO_ENDPOINT",
        section="Datastores & connections",
        type="str",
        reload_class="COLD",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=False),
        description="MinIO/S3 object storage endpoint.",
    ),
    "MINIO_ACCESS_KEY": SettingMetadata(
        key="MINIO_ACCESS_KEY",
        section="Datastores & connections",
        type="secret",
        reload_class="COLD",
        is_secret=True,
        prod_locked=False,
        validator=validate_str(allow_empty=False),
        description="MinIO/S3 access key credential.",
    ),
    "MINIO_SECRET_KEY": SettingMetadata(
        key="MINIO_SECRET_KEY",
        section="Datastores & connections",
        type="secret",
        reload_class="COLD",
        is_secret=True,
        prod_locked=False,
        validator=validate_str(allow_empty=False),
        description="MinIO/S3 secret key credential.",
    ),
    "MINIO_SECURE": SettingMetadata(
        key="MINIO_SECURE",
        section="Datastores & connections",
        type="bool",
        reload_class="COLD",
        is_secret=False,
        prod_locked=False,
        validator=validate_bool,
        description="Enforces HTTPS connection to MinIO if enabled.",
    ),
    "NCE_MINIO_REQUIRED": SettingMetadata(
        key="NCE_MINIO_REQUIRED",
        section="Datastores & connections",
        type="bool",
        reload_class="COLD",
        is_secret=False,
        prod_locked=False,
        validator=validate_bool,
        description="When true, halts startup if MinIO credentials are missing.",
    ),
    # 2. Pools & concurrency
    "PG_MIN_POOL": SettingMetadata(
        key="PG_MIN_POOL",
        section="Pools & concurrency",
        type="int",
        reload_class="COLD",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Minimum size of PostgreSQL pool.",
        default=1,
    ),
    "PG_MAX_POOL": SettingMetadata(
        key="PG_MAX_POOL",
        section="Pools & concurrency",
        type="int",
        reload_class="COLD",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Maximum size of PostgreSQL pool.",
        default=10,
    ),
    "REDIS_MAX_CONNECTIONS": SettingMetadata(
        key="REDIS_MAX_CONNECTIONS",
        section="Pools & concurrency",
        type="int",
        reload_class="COLD",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Maximum connections in Redis pool.",
    ),
    "NCE_MAX_CONCURRENT_TOOLS": SettingMetadata(
        key="NCE_MAX_CONCURRENT_TOOLS",
        section="Pools & concurrency",
        type="int",
        reload_class="COLD",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Limits the maximum active parallel tool calls.",
    ),
    "NCE_PARTITION_LOOKAHEAD_MONTHS": SettingMetadata(
        key="NCE_PARTITION_LOOKAHEAD_MONTHS",
        section="Pools & concurrency",
        type="int",
        reload_class="COLD",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Months of event_log partition lookup range.",
    ),
    # 3. Security & signing keys
    "NCE_MASTER_KEY": SettingMetadata(
        key="NCE_MASTER_KEY",
        section="Security & signing keys",
        type="secret",
        reload_class="COLD",
        is_secret=True,
        prod_locked=True,  # NEVER editable/persisted via UI
        validator=validate_str(allow_empty=False),
        description="System master key used to encrypt active signing keys at rest.",
    ),
    "NCE_API_KEY": SettingMetadata(
        key="NCE_API_KEY",
        section="Security & signing keys",
        type="secret",
        reload_class="WARM",
        is_secret=True,
        prod_locked=False,
        validator=validate_str(allow_empty=False),
        description="HMAC-SHA256 key for authenticating admin API HTTP requests.",
    ),
    "NCE_DISTRIBUTED_REPLAY": SettingMetadata(
        key="NCE_DISTRIBUTED_REPLAY",
        section="Security & signing keys",
        type="bool",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_bool,
        description="Enforces distributed Redis-backed nonce validation for HTTP admin API.",
    ),
    "NCE_PBKDF2_ITERATIONS": SettingMetadata(
        key="NCE_PBKDF2_ITERATIONS",
        section="Security & signing keys",
        type="int",
        reload_class="COLD",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=100000),
        description="PBKDF2 iteration count for legacy v2 blobs.",
    ),
    "NCE_PBKDF2_ITERATIONS_V4": SettingMetadata(
        key="NCE_PBKDF2_ITERATIONS_V4",
        section="Security & signing keys",
        type="int",
        reload_class="COLD",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=600000),
        description="PBKDF2 iteration count for password hashing and v4 key blobs.",
    ),
    # 4. Guardrails
    "NCE_BYPASS_WORM": SettingMetadata(
        key="NCE_BYPASS_WORM",
        section="Guardrails",
        type="bool",
        reload_class="COLD",
        is_secret=False,
        prod_locked=True,  # Guardrail key
        validator=validate_bool,
        description="Permits bypassing WORM log constraints (Forbidden in Prod).",
    ),
    "NCE_BYPASS_RLS": SettingMetadata(
        key="NCE_BYPASS_RLS",
        section="Guardrails",
        type="bool",
        reload_class="COLD",
        is_secret=False,
        prod_locked=True,  # Guardrail key
        validator=validate_bool,
        description="Permits bypassing Row-Level Security constraints (Forbidden in Prod).",
    ),
    "NCE_ADMIN_OVERRIDE": SettingMetadata(
        key="NCE_ADMIN_OVERRIDE",
        section="Guardrails",
        type="bool",
        reload_class="COLD",
        is_secret=False,
        prod_locked=True,  # Guardrail key
        validator=validate_bool,
        description="Enables dev-only admin check overrides (Forbidden in Prod).",
    ),
    "NCE_LOAD_DOTENV": SettingMetadata(
        key="NCE_LOAD_DOTENV",
        section="Guardrails",
        type="bool",
        reload_class="COLD",
        is_secret=False,
        prod_locked=True,  # Guardrail key
        validator=validate_bool,
        description="Allows loading local env variables from .env files (Forbidden in Prod).",
    ),
    "NCE_ALLOW_ADMIN_DOTENV_PERSIST": SettingMetadata(
        key="NCE_ALLOW_ADMIN_DOTENV_PERSIST",
        section="Guardrails",
        type="bool",
        reload_class="COLD",
        is_secret=False,
        prod_locked=True,  # Guardrail key
        validator=validate_bool,
        description="Allows saving runtime setting overrides to .env file (Forbidden in Prod).",
    ),
    # 5. Admin surface
    "NCE_ADMIN_USERNAME": SettingMetadata(
        key="NCE_ADMIN_USERNAME",
        section="Admin surface",
        type="str",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=False),
        description="Username for HTTP Basic Auth accessing admin dashboard.",
    ),
    "NCE_ADMIN_PASSWORD": SettingMetadata(
        key="NCE_ADMIN_PASSWORD",
        section="Admin surface",
        type="secret",
        reload_class="WARM",
        is_secret=True,
        prod_locked=False,
        validator=validate_str(allow_empty=False),
        description="Password for HTTP Basic Auth accessing admin dashboard.",
    ),
    "NCE_ADMIN_API_KEY": SettingMetadata(
        key="NCE_ADMIN_API_KEY",
        section="Admin surface",
        type="secret",
        reload_class="WARM",
        is_secret=True,
        prod_locked=False,
        validator=validate_str(allow_empty=False),
        description="Bearer token admin API key.",
    ),
    "NCE_ADMIN_MTLS_ENABLED": SettingMetadata(
        key="NCE_ADMIN_MTLS_ENABLED",
        section="Admin surface",
        type="bool",
        reload_class="COLD",
        is_secret=False,
        prod_locked=False,
        validator=validate_bool,
        description="Enforces mTLS on the admin web app ports.",
    ),
    "NCE_ADMIN_HTTP_RATE_LIMIT": SettingMetadata(
        key="NCE_ADMIN_HTTP_RATE_LIMIT",
        section="Admin surface",
        type="int",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Requests allowed within the admin rate-limit period.",
    ),
    "NCE_ADMIN_HTTP_RATE_PERIOD": SettingMetadata(
        key="NCE_ADMIN_HTTP_RATE_PERIOD",
        section="Admin surface",
        type="int",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Time period in seconds for the rate limits.",
    ),
    "NCE_ADMIN_HTTP_SENSITIVE_RATE_LIMIT": SettingMetadata(
        key="NCE_ADMIN_HTTP_SENSITIVE_RATE_LIMIT",
        section="Admin surface",
        type="int",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Requests allowed for sensitive endpoints.",
    ),
    "NCE_ADMIN_HTTP_SENSITIVE_RATE_PERIOD": SettingMetadata(
        key="NCE_ADMIN_HTTP_SENSITIVE_RATE_PERIOD",
        section="Admin surface",
        type="int",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Time period in seconds for sensitive rate limits.",
    ),
    # 6. MCP stdio
    "NCE_MCP_API_KEY": SettingMetadata(
        key="NCE_MCP_API_KEY",
        section="MCP stdio",
        type="secret",
        reload_class="WARM",
        is_secret=True,
        prod_locked=False,
        validator=validate_str(allow_empty=False),
        description="Shared secret for tenant tools over stdio MCP interface.",
    ),
    "NCE_MCP_NAMESPACE_ID": SettingMetadata(
        key="NCE_MCP_NAMESPACE_ID",
        section="MCP stdio",
        type="str",
        reload_class="COLD",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=False),
        description="Binds the stdio MCP provider process to this specific namespace.",
    ),
    "NCE_DISABLE_MIGRATION_MCP": SettingMetadata(
        key="NCE_DISABLE_MIGRATION_MCP",
        section="MCP stdio",
        type="bool",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_bool,
        description="Excludes database schema/re-indexing tools from MCP tool capabilities.",
    ),
    # 7. A2A / JWT
    "NCE_JWT_SECRET": SettingMetadata(
        key="NCE_JWT_SECRET",
        section="A2A / JWT",
        type="secret",
        reload_class="WARM",
        is_secret=True,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="Symmetric HS256 JWT validation secret.",
    ),
    "NCE_JWT_PUBLIC_KEY": SettingMetadata(
        key="NCE_JWT_PUBLIC_KEY",
        section="A2A / JWT",
        type="secret",
        reload_class="WARM",
        is_secret=True,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="RS256/ES256 public key contents or keyfile path (PEM file URI).",
    ),
    "NCE_JWT_ALGORITHM": SettingMetadata(
        key="NCE_JWT_ALGORITHM",
        section="A2A / JWT",
        type="str",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=False),
        description="Signature algorithm for validating tokens (e.g. HS256, RS256).",
    ),
    "NCE_JWT_ISSUER": SettingMetadata(
        key="NCE_JWT_ISSUER",
        section="A2A / JWT",
        type="str",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="Expected iss claim value on tokens.",
    ),
    "NCE_JWT_AUDIENCE": SettingMetadata(
        key="NCE_JWT_AUDIENCE",
        section="A2A / JWT",
        type="str",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="Expected default aud claim value.",
    ),
    "NCE_A2A_JWT_AUDIENCE": SettingMetadata(
        key="NCE_A2A_JWT_AUDIENCE",
        section="A2A / JWT",
        type="str",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=False),
        description="Mandatory JWT audience check specifically for A2A query endpoints.",
    ),
    "NCE_A2A_URL": SettingMetadata(
        key="NCE_A2A_URL",
        section="A2A / JWT",
        type="str",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=False),
        description="Outbound base endpoint target for inter-agent communication.",
    ),
    "NCE_A2A_MTLS_ENABLED": SettingMetadata(
        key="NCE_A2A_MTLS_ENABLED",
        section="A2A / JWT",
        type="bool",
        reload_class="COLD",
        is_secret=False,
        prod_locked=False,
        validator=validate_bool,
        description="Enforce agent client cert verification on incoming connections.",
    ),
    # 8. LLM / Cognitive
    "NCE_LLM_PROVIDER": SettingMetadata(
        key="NCE_LLM_PROVIDER",
        section="LLM / Cognitive",
        type="str",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=False),
        description="Selected backend provider for system LLM orchestration.",
    ),
    "NCE_COGNITIVE_BASE_URL": SettingMetadata(
        key="NCE_COGNITIVE_BASE_URL",
        section="LLM / Cognitive",
        type="str",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="URL mapping for the local OpenAI-compatible cognitive server.",
    ),
    "NCE_COGNITIVE_EMBEDDING_MODEL": SettingMetadata(
        key="NCE_COGNITIVE_EMBEDDING_MODEL",
        section="LLM / Cognitive",
        type="str",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="Primary model code identifier for cognitive embedding tasks.",
    ),
    "NCE_COGNITIVE_API_KEY": SettingMetadata(
        key="NCE_COGNITIVE_API_KEY",
        section="LLM / Cognitive",
        type="secret",
        reload_class="WARM",
        is_secret=True,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="Credential secret for cognitive server integration.",
    ),
    "NCE_OPENAI_API_KEY": SettingMetadata(
        key="NCE_OPENAI_API_KEY",
        section="LLM / Cognitive",
        type="secret",
        reload_class="WARM",
        is_secret=True,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="OpenAI provider API key.",
    ),
    "NCE_ANTHROPIC_API_KEY": SettingMetadata(
        key="NCE_ANTHROPIC_API_KEY",
        section="LLM / Cognitive",
        type="secret",
        reload_class="WARM",
        is_secret=True,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="Anthropic Claude API key credential.",
    ),
    "NCE_AZURE_OPENAI_API_KEY": SettingMetadata(
        key="NCE_AZURE_OPENAI_API_KEY",
        section="LLM / Cognitive",
        type="secret",
        reload_class="WARM",
        is_secret=True,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="Azure OpenAI endpoint API key.",
    ),
    "NCE_AZURE_OPENAI_ENDPOINT": SettingMetadata(
        key="NCE_AZURE_OPENAI_ENDPOINT",
        section="LLM / Cognitive",
        type="str",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="Azure OpenAI cloud base endpoint URL.",
    ),
    "NCE_AZURE_OPENAI_DEPLOYMENT": SettingMetadata(
        key="NCE_AZURE_OPENAI_DEPLOYMENT",
        section="LLM / Cognitive",
        type="str",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="Model deployment name inside Azure OpenAI resource.",
    ),
    "NCE_GEMINI_API_KEY": SettingMetadata(
        key="NCE_GEMINI_API_KEY",
        section="LLM / Cognitive",
        type="secret",
        reload_class="WARM",
        is_secret=True,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="Google Gemini / AI Studio API key.",
    ),
    "NCE_DEEPSEEK_API_KEY": SettingMetadata(
        key="NCE_DEEPSEEK_API_KEY",
        section="LLM / Cognitive",
        type="secret",
        reload_class="WARM",
        is_secret=True,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="DeepSeek provider credentials.",
    ),
    "NCE_MOONSHOT_API_KEY": SettingMetadata(
        key="NCE_MOONSHOT_API_KEY",
        section="LLM / Cognitive",
        type="secret",
        reload_class="WARM",
        is_secret=True,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="Moonshot Kimi provider credentials.",
    ),
    "NCE_OPENAI_COMPAT_BASE_URL": SettingMetadata(
        key="NCE_OPENAI_COMPAT_BASE_URL",
        section="LLM / Cognitive",
        type="str",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="Endpoint URL for compatible OpenAI backends.",
    ),
    "NCE_OPENAI_COMPAT_API_KEY": SettingMetadata(
        key="NCE_OPENAI_COMPAT_API_KEY",
        section="LLM / Cognitive",
        type="secret",
        reload_class="WARM",
        is_secret=True,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="API key for custom OpenAI-compatible server integrations.",
    ),
    "NCE_OPENAI_COMPAT_MODEL": SettingMetadata(
        key="NCE_OPENAI_COMPAT_MODEL",
        section="LLM / Cognitive",
        type="str",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="Selected fallback model key for compatible OpenAI API endpoints.",
    ),
    # 9. Embeddings & edge
    "NCE_EMBEDDING_MODEL_ID": SettingMetadata(
        key="NCE_EMBEDDING_MODEL_ID",
        section="Embeddings & edge",
        type="str",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=False),
        description="Primary model identifier for indexing embeddings (triggers re-index when changed).",
    ),
    "NCE_EMBEDDING_MODEL_REVISION": SettingMetadata(
        key="NCE_EMBEDDING_MODEL_REVISION",
        section="Embeddings & edge",
        type="str",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="Pinned huggingface/model revision identifier for dependency locking.",
    ),
    "NCE_EMBEDDING_TRUST_REMOTE_CODE": SettingMetadata(
        key="NCE_EMBEDDING_TRUST_REMOTE_CODE",
        section="Embeddings & edge",
        type="bool",
        reload_class="COLD",
        is_secret=False,
        prod_locked=False,
        validator=validate_bool,
        description="Trust remote code for Jina and related transformers models during initialization.",
    ),
    "NCE_BACKEND": SettingMetadata(
        key="NCE_BACKEND",
        section="Embeddings & edge",
        type="str",
        reload_class="COLD",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="Target execution backend ('openvino' / 'onnx' / default).",
    ),
    "NCE_OPENVINO_MODEL_DIR": SettingMetadata(
        key="NCE_OPENVINO_MODEL_DIR",
        section="Embeddings & edge",
        type="str",
        reload_class="COLD",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="Directory storage path containing exported OpenVINO XML model parameters.",
    ),
    "NCE_OPENVINO_SEQ_LEN": SettingMetadata(
        key="NCE_OPENVINO_SEQ_LEN",
        section="Embeddings & edge",
        type="int",
        reload_class="COLD",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Execution sequence length override for OpenVINO model compilation.",
    ),
    "EMBED_BATCH_CHUNK": SettingMetadata(
        key="EMBED_BATCH_CHUNK",
        section="Embeddings & edge",
        type="int",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Number of text passages to chunk into single embedding model inference batch.",
    ),
    "NCE_EMBED_MAX_BATCH_TEXTS": SettingMetadata(
        key="NCE_EMBED_MAX_BATCH_TEXTS",
        section="Embeddings & edge",
        type="int",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Max total text counts accepted in one request batch payload.",
    ),
    "NCE_EMBED_MAX_TEXT_CHARS": SettingMetadata(
        key="NCE_EMBED_MAX_TEXT_CHARS",
        section="Embeddings & edge",
        type="int",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Character count bounds validation limit for single embedding payload string.",
    ),
    "EMBEDDING_MAX_WORKERS": SettingMetadata(
        key="EMBEDDING_MAX_WORKERS",
        section="Embeddings & edge",
        type="int",
        reload_class="COLD",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Parallel workers limits allocation for in-process model operations.",
    ),
    # 10. Re-embedding worker
    "REEMBED_BATCH_SIZE": SettingMetadata(
        key="REEMBED_BATCH_SIZE",
        section="Re-embedding worker",
        type="int",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Bulk update batch size parameter for re-embedding processes.",
    ),
    "REEMBED_BATCHES_PER_MINUTE": SettingMetadata(
        key="REEMBED_BATCHES_PER_MINUTE",
        section="Re-embedding worker",
        type="int",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Throughput pacing constraint for background re-embedding batches.",
    ),
    "REEMBED_CRON_INTERVAL_MINUTES": SettingMetadata(
        key="REEMBED_CRON_INTERVAL_MINUTES",
        section="Re-embedding worker",
        type="int",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Chronological check run intervals in minutes for re-embed sweeps.",
    ),
    "REEMBED_MAX_ROWS_PER_RUN": SettingMetadata(
        key="REEMBED_MAX_ROWS_PER_RUN",
        section="Re-embedding worker",
        type="int",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=0),
        description="Upper bounds record counts limits per cron iteration. 0 means unlimited.",
    ),
    "REEMBED_INCLUDE_KG_NODES": SettingMetadata(
        key="REEMBED_INCLUDE_KG_NODES",
        section="Re-embedding worker",
        type="bool",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_bool,
        description="Binds re-embedding workers to process KG entity node labels as well.",
    ),
    "REEMBED_MAX_TEXT_CHARS": SettingMetadata(
        key="REEMBED_MAX_TEXT_CHARS",
        section="Re-embedding worker",
        type="int",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Trims raw document characters counts parsed by the re-embedding loop.",
    ),
    # 11. Cron intervals
    "BRIDGE_CRON_INTERVAL_MINUTES": SettingMetadata(
        key="BRIDGE_CRON_INTERVAL_MINUTES",
        section="Cron intervals",
        type="int",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Interval check period in minutes for sync iterations across SaaS doc bridges.",
    ),
    "BRIDGE_RENEWAL_LOOKAHEAD_HOURS": SettingMetadata(
        key="BRIDGE_RENEWAL_LOOKAHEAD_HOURS",
        section="Cron intervals",
        type="int",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="OAuth token refresh check boundary buffer in hours.",
    ),
    "CONSOLIDATION_CRON_INTERVAL_MINUTES": SettingMetadata(
        key="CONSOLIDATION_CRON_INTERVAL_MINUTES",
        section="Cron intervals",
        type="int",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Interval in minutes at which sleep-consolidation sweeps trigger.",
    ),
    "NCE_CHAIN_VERIFY_INTERVAL_MINUTES": SettingMetadata(
        key="NCE_CHAIN_VERIFY_INTERVAL_MINUTES",
        section="Cron intervals",
        type="int",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=5),
        description="Interval in minutes at which automated Merkle chain verification ticks run.",
    ),
    "OUTBOX_RELAY_INTERVAL_SECONDS": SettingMetadata(
        key="OUTBOX_RELAY_INTERVAL_SECONDS",
        section="Cron intervals",
        type="int",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Interval in seconds for scanning and relaying outbox events.",
    ),
    # 12. GC / TTL
    "GC_INTERVAL_SECONDS": SettingMetadata(
        key="GC_INTERVAL_SECONDS",
        section="GC / TTL",
        type="int",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Interval in seconds at which the background garbage collector sweeps.",
    ),
    "GC_ORPHAN_AGE_SECONDS": SettingMetadata(
        key="GC_ORPHAN_AGE_SECONDS",
        section="GC / TTL",
        type="int",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Wait time limit before unreferenced Mongo documents are collected.",
    ),
    "REDIS_TTL": SettingMetadata(
        key="REDIS_TTL",
        section="GC / TTL",
        type="int",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Standard time-to-live settings for cache records inside Redis.",
    ),
    # 13. Quotas
    "NCE_QUOTAS_ENABLED": SettingMetadata(
        key="NCE_QUOTAS_ENABLED",
        section="Quotas",
        type="bool",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_bool,
        description="Master switch to enforce global tenant request limits checks.",
    ),
    # 14. Webhooks (receiver hardening)
    "WEBHOOK_MAX_BODY_BYTES": SettingMetadata(
        key="WEBHOOK_MAX_BODY_BYTES",
        section="Webhooks (receiver hardening)",
        type="int",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Sets the upper threshold payload byte counts for webhook packets.",
    ),
    "WEBHOOK_RATE_LIMIT": SettingMetadata(
        key="WEBHOOK_RATE_LIMIT",
        section="Webhooks (receiver hardening)",
        type="int",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Webhooks rate limit allowed calls limit count.",
    ),
    "WEBHOOK_RATE_PERIOD_SECONDS": SettingMetadata(
        key="WEBHOOK_RATE_PERIOD_SECONDS",
        section="Webhooks (receiver hardening)",
        type="int",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Webhooks rate limit window limits in seconds.",
    ),
    "WEBHOOK_DEDUP_TTL_SECONDS": SettingMetadata(
        key="WEBHOOK_DEDUP_TTL_SECONDS",
        section="Webhooks (receiver hardening)",
        type="int",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=60),
        description="TTL duration limit in seconds to store dedup identifiers in cache.",
    ),
    "WEBHOOK_DEDUP_FAIL_OPEN": SettingMetadata(
        key="WEBHOOK_DEDUP_FAIL_OPEN",
        section="Webhooks (receiver hardening)",
        type="bool",
        reload_class="HOT",
        is_secret=False,
        prod_locked=True,  # Forbidden in production
        validator=validate_bool,
        description="Bypasses deduplication rules if Redis connection degrades (Forbidden in Prod).",
    ),
    "NCE_WEBHOOK_TRUST_PROXY": SettingMetadata(
        key="NCE_WEBHOOK_TRUST_PROXY",
        section="Webhooks (receiver hardening)",
        type="bool",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_bool,
        description="Honours X-Forwarded-For routing headers from upstream proxy.",
    ),
    # 15. Bridges & OAuth
    "BRIDGE_WEBHOOK_BASE_URL": SettingMetadata(
        key="BRIDGE_WEBHOOK_BASE_URL",
        section="Bridges & OAuth",
        type="str",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=False),
        description="Standard callback URL host target for external SaaS change notifications.",
    ),
    "DROPBOX_APP_SECRET": SettingMetadata(
        key="DROPBOX_APP_SECRET",
        section="Bridges & OAuth",
        type="secret",
        reload_class="WARM",
        is_secret=True,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="Dropbox App client secret credentials.",
    ),
    "GRAPH_CLIENT_STATE": SettingMetadata(
        key="GRAPH_CLIENT_STATE",
        section="Bridges & OAuth",
        type="str",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="CSRF guard state identifier for MS Graph API flow.",
    ),
    "DRIVE_CHANNEL_TOKEN": SettingMetadata(
        key="DRIVE_CHANNEL_TOKEN",
        section="Bridges & OAuth",
        type="str",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="Opaque channel payload verification token for Google Drive webhooks.",
    ),
    "AZURE_CLIENT_ID": SettingMetadata(
        key="AZURE_CLIENT_ID",
        section="Bridges & OAuth",
        type="str",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="Client Application GUID identifier for Azure Active Directory registrations.",
    ),
    "AZURE_CLIENT_SECRET": SettingMetadata(
        key="AZURE_CLIENT_SECRET",
        section="Bridges & OAuth",
        type="secret",
        reload_class="WARM",
        is_secret=True,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="Client secret credential associated with Azure AD app.",
    ),
    "AZURE_TENANT_ID": SettingMetadata(
        key="AZURE_TENANT_ID",
        section="Bridges & OAuth",
        type="str",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="Azure AD Tenant GUID or 'common' mapping.",
    ),
    "BRIDGE_OAUTH_REDIRECT_URI": SettingMetadata(
        key="BRIDGE_OAUTH_REDIRECT_URI",
        section="Bridges & OAuth",
        type="str",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=False),
        description="Callback redirect URI for local OAuth response handlers.",
    ),
    "GDRIVE_OAUTH_CLIENT_ID": SettingMetadata(
        key="GDRIVE_OAUTH_CLIENT_ID",
        section="Bridges & OAuth",
        type="str",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="Client ID credentials for Google Drive OAuth setup.",
    ),
    "GDRIVE_OAUTH_CLIENT_SECRET": SettingMetadata(
        key="GDRIVE_OAUTH_CLIENT_SECRET",
        section="Bridges & OAuth",
        type="secret",
        reload_class="WARM",
        is_secret=True,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="Client Secret associated with Google Drive OAuth config.",
    ),
    "DROPBOX_OAUTH_CLIENT_ID": SettingMetadata(
        key="DROPBOX_OAUTH_CLIENT_ID",
        section="Bridges & OAuth",
        type="str",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="Client ID associated with Dropbox OAuth registration.",
    ),
    # 16. Observability
    "NCE_OBSERVABILITY_ENABLED": SettingMetadata(
        key="NCE_OBSERVABILITY_ENABLED",
        section="Observability",
        type="bool",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_bool,
        description="Master toggle to export Prometheus/OTLP performance counters.",
    ),
    "NCE_PROMETHEUS_PORT": SettingMetadata(
        key="NCE_PROMETHEUS_PORT",
        section="Observability",
        type="int",
        reload_class="COLD",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1024),
        description="Network port bound to export metrics for Prometheus collection sweeps.",
    ),
    "NCE_OTEL_SERVICE_NAME": SettingMetadata(
        key="NCE_OTEL_SERVICE_NAME",
        section="Observability",
        type="str",
        reload_class="COLD",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=False),
        description="Name identifier tag reported in OpenTelemetry spans.",
    ),
    "NCE_OTEL_EXPORTER_OTLP_ENDPOINT": SettingMetadata(
        key="NCE_OTEL_EXPORTER_OTLP_ENDPOINT",
        section="Observability",
        type="str",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=False),
        description="URL target matching collector OTLP receiver (e.g. Jaeger or OpenTelemetry).",
    ),
    # 17. Temporal
    "NCE_MAX_TEMPORAL_LOOKBACK_DAYS": SettingMetadata(
        key="NCE_MAX_TEMPORAL_LOOKBACK_DAYS",
        section="Temporal",
        type="int",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=0),
        description="Max days query limit window for as_of lookback bounds. 0 disables limit.",
    ),
    # 18. Cognitive tuning
    "CONSOLIDATION_HALF_LIFE_DAYS": SettingMetadata(
        key="CONSOLIDATION_HALF_LIFE_DAYS",
        section="Cognitive tuning",
        type="float",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_float(minimum=0.1),
        description="Temporal half-life in days used to decay salience score computations.",
    ),
    "NCE_CONTRADICTION_SIMILARITY_THRESHOLD": SettingMetadata(
        key="NCE_CONTRADICTION_SIMILARITY_THRESHOLD",
        section="Cognitive tuning",
        type="float",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_float(minimum=0.0),
        description="Vector cosine similarity barrier matching candidate contradictions for review.",
    ),
    "NCE_CONTRADICTION_MAX_CANDIDATES": SettingMetadata(
        key="NCE_CONTRADICTION_MAX_CANDIDATES",
        section="Cognitive tuning",
        type="int",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Maximum candidates retrieved for evaluation of contradictions.",
    ),
    "NCE_CONTRADICTION_NLI_THRESHOLD": SettingMetadata(
        key="NCE_CONTRADICTION_NLI_THRESHOLD",
        section="Cognitive tuning",
        type="float",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_float(minimum=0.0),
        description="NLI confidence threshold limit to accept an invalidation cascade.",
    ),
    "NCE_CONTRADICTION_LLM_MIN_CONFIDENCE": SettingMetadata(
        key="NCE_CONTRADICTION_LLM_MIN_CONFIDENCE",
        section="Cognitive tuning",
        type="float",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_float(minimum=0.0),
        description="Min confidence score generated by LLM analysis to assert a conflict.",
    ),
    "NCE_ACTIVE_LEARNING_CONFIRM_XP": SettingMetadata(
        key="NCE_ACTIVE_LEARNING_CONFIRM_XP",
        section="Cognitive tuning",
        type="int",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=0),
        description="XP rewarded to users validating/confirming assertions.",
    ),
    "NCE_ACTIVE_LEARNING_REJECT_XP": SettingMetadata(
        key="NCE_ACTIVE_LEARNING_REJECT_XP",
        section="Cognitive tuning",
        type="int",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=0),
        description="XP rewarded to users rejecting an assertion during review.",
    ),
    "NCE_MAX_DERIVATION_DEPTH": SettingMetadata(
        key="NCE_MAX_DERIVATION_DEPTH",
        section="Cognitive tuning",
        type="int",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        default=2,
        description=(
            "Max consolidation generation depth. Memories at or above this depth "
            "are excluded from clustering input to prevent hallucination compounding."
        ),
    ),
    "NCE_DERIVATION_CONFIDENCE_DECAY": SettingMetadata(
        key="NCE_DERIVATION_CONFIDENCE_DECAY",
        section="Cognitive tuning",
        type="float",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_float(minimum=0.0),
        default=0.85,
        description=(
            "Per-generation confidence decay factor γ (0 < γ ≤ 1). "
            "Derived KG-edge confidence is multiplied by γ^depth on insert."
        ),
    ),
    "NCE_TRUST_QUARANTINE_BYPASS": SettingMetadata(
        key="NCE_TRUST_QUARANTINE_BYPASS",
        section="Cognitive tuning",
        type="float",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_float(minimum=0.0),
        default=0.8,
        description=(
            "Actor trust score threshold (0–1) at or above which a mid-confidence "
            "assertion bypasses quarantine without operator confirmation."
        ),
    ),
    "NCE_TRUST_DEFAULT": SettingMetadata(
        key="NCE_TRUST_DEFAULT",
        section="Cognitive tuning",
        type="float",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_float(minimum=1e-6),
        default=0.65,
        description=(
            "Laplace-smoothed prior trust score used for actors not yet present "
            "in the actor_trust table."
        ),
    ),
    # 19. NetBox vertical
    "NCE_NETBOX_URL": SettingMetadata(
        key="NCE_NETBOX_URL",
        section="NetBox vertical",
        type="str",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="NetBox endpoint connection URL.",
    ),
    "NCE_NETBOX_TOKEN": SettingMetadata(
        key="NCE_NETBOX_TOKEN",
        section="NetBox vertical",
        type="secret",
        reload_class="WARM",
        is_secret=True,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="Credential secret token for NetBox access authorization.",
    ),
    "NCE_NETBOX_DEFAULT_INTERFACE_TYPE": SettingMetadata(
        key="NCE_NETBOX_DEFAULT_INTERFACE_TYPE",
        section="NetBox vertical",
        type="str",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=False),
        description="Fallback physical port configuration type for newly discovered circuits.",
    ),
    # 20. D365 vertical
    "NCE_D365_ENABLED": SettingMetadata(
        key="NCE_D365_ENABLED",
        section="D365 vertical",
        type="bool",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_bool,
        description="Global enable toggle for Dynamics 365 / Dataverse synchronization module.",
    ),
    "NCE_D365_ORG_URL": SettingMetadata(
        key="NCE_D365_ORG_URL",
        section="D365 vertical",
        type="str",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="Dynamics 365 organization tenant endpoint URL.",
    ),
    "NCE_D365_WEBHOOK_SECRET": SettingMetadata(
        key="NCE_D365_WEBHOOK_SECRET",
        section="D365 vertical",
        type="secret",
        reload_class="WARM",
        is_secret=True,
        prod_locked=False,
        validator=validate_str(allow_empty=True),
        description="Shared secret key validated on incoming D365 change notifications.",
    ),
    "NCE_D365_SYNC_INTERVAL_MINUTES": SettingMetadata(
        key="NCE_D365_SYNC_INTERVAL_MINUTES",
        section="D365 vertical",
        type="int",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=5),
        description="Interval in minutes to run Dynamics 365 change scans.",
    ),
    "NCE_D365_SYNC_PAGE_SIZE": SettingMetadata(
        key="NCE_D365_SYNC_PAGE_SIZE",
        section="D365 vertical",
        type="int",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=10),
        description="Upper limits counts parameter for single batch OData request pages.",
    ),
    "NCE_D365_EMPATHIC_URGENCY_KEYWORDS": SettingMetadata(
        key="NCE_D365_EMPATHIC_URGENCY_KEYWORDS",
        section="D365 vertical",
        type="str",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=False),
        description="Comma-separated term list scanned to boost SLA urgency assessments.",
    ),
    "NCE_D365_EMPATHIC_FRUSTRATION_KEYWORDS": SettingMetadata(
        key="NCE_D365_EMPATHIC_FRUSTRATION_KEYWORDS",
        section="D365 vertical",
        type="str",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=False),
        description="Comma-separated keyword list scanned to trigger SLA frustration indicators.",
    ),
    "NCE_D365_NETBOX_BRIDGE_ENABLED": SettingMetadata(
        key="NCE_D365_NETBOX_BRIDGE_ENABLED",
        section="D365 vertical",
        type="bool",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_bool,
        description="Enables automatic bridging between D365 tenant mappings and NetBox circuits.",
    ),
    "NCE_D365_NETBOX_BRIDGE_INTERVAL_MINUTES": SettingMetadata(
        key="NCE_D365_NETBOX_BRIDGE_INTERVAL_MINUTES",
        section="D365 vertical",
        type="int",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=10),
        description="Frequency run period in minutes for bridging calculations.",
    ),
    "NCE_D365_NETBOX_FUZZY_THRESHOLD": SettingMetadata(
        key="NCE_D365_NETBOX_FUZZY_THRESHOLD",
        section="D365 vertical",
        type="float",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_float(minimum=0.5),
        description="Minimum fuzzy matching string ratio bound to link names.",
    ),
    "NCE_D365_NETBOX_TENANT_CF_NAME": SettingMetadata(
        key="NCE_D365_NETBOX_TENANT_CF_NAME",
        section="D365 vertical",
        type="str",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=False),
        description="Target custom field name mapping account GUIDs inside NetBox.",
    ),
    # 21. Ingestion / extractors
    "NCE_MAX_ATTACHMENT_BYTES": SettingMetadata(
        key="NCE_MAX_ATTACHMENT_BYTES",
        section="Ingestion / extractors",
        type="int",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Max total bytes limit for doc parser files ingestion.",
    ),
    "NCE_MAX_OCR_PAGES": SettingMetadata(
        key="NCE_MAX_OCR_PAGES",
        section="Ingestion / extractors",
        type="int",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Limits the upper page counts processed through pytesseract OCR scans.",
    ),
    "NCE_MAX_CODE_INDEX_BYTES": SettingMetadata(
        key="NCE_MAX_CODE_INDEX_BYTES",
        section="Ingestion / extractors",
        type="int",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1024),
        description="Max code files index byte size limits before skipping parsing.",
    ),
    "NCE_MAX_CODE_CHUNKS_PER_FILE": SettingMetadata(
        key="NCE_MAX_CODE_CHUNKS_PER_FILE",
        section="Ingestion / extractors",
        type="int",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Limits code passage chunks outputs created per single codebase file.",
    ),
    # 22. Tools & Skills toggles
    "NCE_TOOLS_DISABLED": SettingMetadata(
        key="NCE_TOOLS_DISABLED",
        section="Tools & Skills toggles",
        type="list",
        reload_class="HOT",
        is_secret=False,
        prod_locked=False,
        validator=validate_str_list,
        description="List of MCP tool names explicitly disabled from exposure to agents.",
    ),
    # 23. Economy vertical (PEPPOL)
    "NCE_ECONOMY_PEPPOL_ENABLED": SettingMetadata(
        key="NCE_ECONOMY_PEPPOL_ENABLED",
        section="Economy vertical",
        type="bool",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_bool,
        description=(
            "Safety interlock for outbound EHF/PEPPOL send — off by default until a "
            "PEPPOL access-point provider is selected."
        ),
    ),
    "NCE_ECONOMY_PEPPOL_MODE": SettingMetadata(
        key="NCE_ECONOMY_PEPPOL_MODE",
        section="Economy vertical",
        type="str",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_str(allow_empty=False),
        description="PEPPOL network selector ('sandbox' or 'prod'); informational only.",
    ),
    # 24. Inventory vertical (Stock Watcher, Module 11 Wave 6b)
    "NCE_INVENTORY_STOCK_WATCHER_INTERVAL_MINUTES": SettingMetadata(
        key="NCE_INVENTORY_STOCK_WATCHER_INTERVAL_MINUTES",
        section="Inventory vertical",
        type="int",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=5),
        description="How often the Stock Watcher scans all namespaces for low-stock/dead-stock flags.",
    ),
    "NCE_INVENTORY_DEAD_STOCK_DAYS": SettingMetadata(
        key="NCE_INVENTORY_DEAD_STOCK_DAYS",
        section="Inventory vertical",
        type="int",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_int(minimum=1),
        description="Days of ledger inactivity before a positive-quantity item is flagged dead stock.",
    ),
    "NCE_INVENTORY_LOW_STOCK_ALERT_ENABLED": SettingMetadata(
        key="NCE_INVENTORY_LOW_STOCK_ALERT_ENABLED",
        section="Inventory vertical",
        type="bool",
        reload_class="WARM",
        is_secret=False,
        prod_locked=False,
        validator=validate_bool,
        description="Safety interlock for Stock Watcher alerts — off by default until a deployment opts in.",
    ),
}
