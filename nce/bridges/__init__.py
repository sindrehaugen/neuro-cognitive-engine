"""
Document bridge providers (SharePoint / Google Drive / Dropbox).

RQ worker imports `dispatch_bridge_event` from here, or the concrete
`SharePointBridge`, `GoogleDriveBridge`, `DropboxBridge` classes to download files.
"""

from __future__ import annotations

import asyncio
from typing import Any

from nce.bridges.base import BridgeAuthError, BridgeProvider, redis_client
from nce.bridges.dropbox import DropboxBridge, process_dropbox_event
from nce.bridges.gdrive import GoogleDriveBridge, process_gdrive_event
from nce.bridges.sharepoint import SharePointBridge, process_sharepoint_event

__all__ = [
    "BridgeAuthError",
    "BridgeProvider",
    "DropboxBridge",
    "GoogleDriveBridge",
    "SharePointBridge",
    "dispatch_bridge_event",
    "process_dropbox_event",
    "process_gdrive_event",
    "process_sharepoint_event",
    "redis_client",
]


async def dispatch_bridge_event(provider: str, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Fan-in from `nce.tasks.process_bridge_event` (RQ worker).
    `provider` is one of: sharepoint, gdrive, dropbox.
    """
    p = provider.strip().lower()
    if p == "sharepoint":
        return await process_sharepoint_event(payload)
    if p in ("gdrive", "google_drive", "drive"):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, process_gdrive_event, payload)
    if p == "dropbox":
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, process_dropbox_event, payload)
    raise ValueError(f"Unknown bridge provider: {provider!r}")
