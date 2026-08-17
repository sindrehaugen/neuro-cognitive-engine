"""Canonical document-bridge provider identifiers (§10.3 / Appendix H)."""

from __future__ import annotations

# MCP connect, repo lookups, renewal cron, and runtime token resolution must agree.
BRIDGE_PROVIDERS: frozenset[str] = frozenset({"sharepoint", "gdrive", "dropbox"})
