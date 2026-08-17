"""
Google Drive — changes.list + channel watch (§10.3, Appendix H.4).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlencode

import httpx

from nce.bridges.base import BridgeAuthError, BridgeProvider, redis_client
from nce.config import cfg

log = logging.getLogger("nce.bridges.gdrive")

DRIVE_API = "https://www.googleapis.com/drive/v3"
CHANGES_FIELDS = "nextPageToken,newStartPageToken,changes(fileId,removed,file(id,name,mimeType,modifiedTime,md5Checksum))"


class GoogleDriveBridge(BridgeProvider):
    """Drive v3 changes.list pagination."""

    @property
    def provider_key(self) -> str:
        return "gdrive"

    def _cursor_key(self, channel_id: str) -> str:
        return f"bridge:cursor:gdrive:{channel_id}"

    def walk_delta(self, context: dict[str, Any]) -> Iterator[dict[str, Any]]:
        channel_id = context.get("channel_id") or ""
        self._oauth_token_override = None
        env_tok = (cfg.GDRIVE_BRIDGE_TOKEN or "").strip()
        if env_tok:
            self._oauth_token_override = env_tok
        elif channel_id:
            from nce.bridge_runtime import resolve_stored_oauth_access_token

            t = resolve_stored_oauth_access_token("gdrive", subscription_id=str(channel_id))
            if t:
                self._oauth_token_override = t

        try:
            if not channel_id:
                log.warning("Google Drive: missing channel_id in context")
                return

            r = redis_client()
            ck = self._cursor_key(channel_id)
            raw = r.get(ck)
            page_token: str | None = raw.decode("utf-8") if raw else None
            if not page_token:
                log.warning(
                    "Google Drive: no start page token for channel=%s — run initial changes.getStartPageToken or connect flow",
                    channel_id,
                )
                return

            headers = {"Authorization": f"Bearer {self.bearer_token()}"}
            with httpx.Client(timeout=60.0, headers=headers) as client:
                while page_token:
                    q = urlencode({"pageToken": page_token, "fields": CHANGES_FIELDS})
                    url = f"{DRIVE_API}/changes?{q}"
                    resp = client.get(url)
                    if resp.status_code == 401:
                        raise BridgeAuthError(
                            "Drive API 401 — set GDRIVE_BRIDGE_TOKEN or implement OAuth refresh"
                        )
                    resp.raise_for_status()
                    data = resp.json()
                    yield from data.get("changes", [])
                    page_token = data.get("nextPageToken")
                    new_start = data.get("newStartPageToken")
                    if new_start:
                        r.set(ck, new_start)
                    if not page_token:
                        break
        finally:
            self._oauth_token_override = None

    def download_file(self, file_ref: dict[str, Any]) -> bytes:
        file_id = file_ref["file_id"]
        q = urlencode({"alt": "media"})
        url = f"{DRIVE_API}/files/{file_id}?{q}"
        headers = {"Authorization": f"Bearer {self.bearer_token()}"}
        with httpx.Client(timeout=120.0, headers=headers) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.content


def process_gdrive_event(payload: dict[str, Any]) -> dict[str, Any]:
    bridge = GoogleDriveBridge()
    count = 0
    try:
        for _ in bridge.walk_delta(payload):
            count += 1
    except BridgeAuthError as e:
        log.error("%s", e)
        raise
    return {"status": "ok", "changes_seen": count}
