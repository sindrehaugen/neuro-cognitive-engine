"""Shared runtime state for the admin HTTP server (avoids circular imports)."""

from __future__ import annotations

from nce.orchestrator import NCEEngine

engine: NCEEngine | None = None
