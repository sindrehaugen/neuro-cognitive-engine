"""nce.vertical_modules.sites.models — Data models for C17 Site Master Data."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SiteAddress(BaseModel):
    """Validated physical address structure for a site or building."""

    model_config = ConfigDict(extra="allow")

    street: str | None = None
    postal_code: str | None = None
    city: str | None = None
    country: str = "NO"
    formatted_address: str | None = None
    is_validated: bool = False


class SiteItem(BaseModel):
    """Full site item representation."""

    model_config = ConfigDict(extra="ignore")

    id: UUID
    namespace_id: UUID
    name: str
    cadastre_id: str | None = None
    site_type: str = "building"  # 'building', 'vessel', 'campus', 'outdoor'
    address: dict[str, Any] = Field(default_factory=dict)
    latitude: float | None = None
    longitude: float | None = None
    altitude: float | None = None
    height: float | None = None
    footprint_geometry: dict[str, Any] = Field(default_factory=dict)
    telemetry_stream: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    archived: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class SiteCreate(BaseModel):
    """Payload to create a new site."""

    model_config = ConfigDict(extra="allow")

    name: str
    cadastre_id: str | None = None
    site_type: str = "building"
    address: dict[str, Any] | None = None
    latitude: float | None = None
    longitude: float | None = None
    altitude: float | None = None
    height: float | None = None
    footprint_geometry: dict[str, Any] | None = None
    telemetry_stream: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None


class SiteUpdate(BaseModel):
    """Payload to update an existing site."""

    model_config = ConfigDict(extra="allow")

    name: str | None = None
    cadastre_id: str | None = None
    site_type: str | None = None
    address: dict[str, Any] | None = None
    latitude: float | None = None
    longitude: float | None = None
    altitude: float | None = None
    height: float | None = None
    footprint_geometry: dict[str, Any] | None = None
    telemetry_stream: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    archived: bool | None = None
