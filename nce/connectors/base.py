"""Core Connector SDK descriptor contracts and base class for NCE plugins."""

from __future__ import annotations

import re
from abc import ABC
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ConnectorMaturity = Literal["stable", "beta", "experimental"]
ConnectorAuthKind = Literal["oauth2", "api_key", "entra_oidc", "webhook", "local_file", "none"]

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class ConnectorDescriptor(BaseModel):
    """Typed, immutable metadata contract describing an NCE application or vertical connector."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    slug: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=128)
    description: str = Field(default="", max_length=1024)
    version: str = Field(default="1.0.0", min_length=1, max_length=64)
    types: tuple[str, ...] = Field(default=("app-connector",))
    categories: tuple[str, ...] = Field(default=("operations-delivery",))
    maturity: ConnectorMaturity = Field(default="beta")
    vendor_specific: bool = Field(default=True)
    auth_kind: ConnectorAuthKind = Field(default="api_key")
    capabilities: tuple[str, ...] = Field(default=())
    tool_count: int = Field(default=0, ge=0)
    route_count: int = Field(default=0, ge=0)
    requires_schemas: tuple[str, ...] = Field(default=())
    install_extra: str | None = Field(default=None)

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, value: str) -> str:
        clean = value.strip()
        if not _SLUG_RE.fullmatch(clean):
            raise ValueError(f"Connector slug must match {_SLUG_RE.pattern!r}, got {value!r}")
        return clean

    @field_validator("types", "categories", "capabilities", "requires_schemas", mode="before")
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,) if value.strip() else ()
        return tuple(str(item).strip() for item in value if str(item).strip())

    def to_engine_descriptor(
        self,
        plugin_id: str = "nce_apps",
        *,
        installed: bool = True,
        enabled: bool = True,
        enabled_by_env: bool = True,
    ) -> dict[str, Any]:
        """Convert this ConnectorDescriptor into the canonical Core EngineDescriptor dict."""
        return {
            "slug": self.slug,
            "id": self.slug,
            "plugin_id": plugin_id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "types": list(self.types),
            "categories": list(self.categories),
            "installed": installed,
            "enabled": enabled,
            "enabled_by_env": enabled_by_env,
            "tool_count": self.tool_count,
            "route_count": self.route_count,
            "requires_schemas": list(self.requires_schemas),
            "install_extra": self.install_extra or self.slug,
            "maturity": self.maturity,
            "vendor_specific": self.vendor_specific,
            "auth_kind": self.auth_kind,
            "capabilities": list(self.capabilities),
        }


class BaseAppConnector(ABC):
    """Base contract for Core-compatible NCE application connectors.

    All application connectors default to ``maturity = "beta"`` and
    ``vendor_specific = True`` unless explicitly promoted in their
    ``ConnectorDescriptor``.
    """

    maturity: ClassVar[ConnectorMaturity] = "beta"
    vendor_specific: ClassVar[bool] = True
    descriptor: ClassVar[ConnectorDescriptor]

    def get_descriptor(self) -> ConnectorDescriptor:
        """Return the ConnectorDescriptor bound to this connector."""
        desc = getattr(self, "descriptor", None)
        if not isinstance(desc, ConnectorDescriptor):
            raise TypeError(
                f"{type(self).__name__} must define a ConnectorDescriptor class attribute 'descriptor'"
            )
        return desc

    def to_engine_descriptor(
        self,
        plugin_id: str = "nce_apps",
        *,
        installed: bool = True,
        enabled: bool = True,
        enabled_by_env: bool = True,
    ) -> dict[str, Any]:
        """Serialize connector metadata as a normalized EngineDescriptor dictionary."""
        return self.get_descriptor().to_engine_descriptor(
            plugin_id=plugin_id,
            installed=installed,
            enabled=enabled,
            enabled_by_env=enabled_by_env,
        )

    async def healthcheck(self, ctx: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return basic connector readiness status."""
        _ = ctx
        desc = self.get_descriptor()
        return {
            "status": "ok",
            "slug": desc.slug,
            "version": desc.version,
            "maturity": desc.maturity,
            "vendor_specific": desc.vendor_specific,
        }

    async def connect(self, ctx: dict[str, Any], credentials: dict[str, Any]) -> dict[str, Any]:
        """Establish or verify an external connection for a tenant context."""
        _ = ctx
        desc = self.get_descriptor()
        if desc.auth_kind != "none" and not credentials:
            raise ValueError(
                f"Connector {desc.slug!r} requires credentials for auth_kind={desc.auth_kind!r}"
            )
        return {"status": "connected", "slug": desc.slug, "auth_kind": desc.auth_kind}

    async def disconnect(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Tear down or revoke active connection state for a tenant context."""
        _ = ctx
        desc = self.get_descriptor()
        return {"status": "disconnected", "slug": desc.slug}

    def register_tools(self, registry: dict[str, Any]) -> None:
        """Optional hook to register MCP tools into a plugin tool dictionary."""
        _ = registry

    def register_routes(self, app: Any) -> None:
        """Optional hook to register HTTP routes on an ASGI/Starlette router."""
        _ = app


__all__ = [
    "BaseAppConnector",
    "ConnectorAuthKind",
    "ConnectorDescriptor",
    "ConnectorMaturity",
]
