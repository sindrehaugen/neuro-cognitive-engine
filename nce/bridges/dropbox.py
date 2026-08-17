"""
Dropbox — list_folder/continue + webhooks (§10.3, Appendix H.5).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import httpx

from nce.bridges.base import BridgeAuthError, BridgeProvider, redis_client
from nce.config import cfg

log = logging.getLogger("nce.bridges.dropbox")

DROPBOX_API = "https://api.dropboxapi.com/2"


class DropboxBridge(BridgeProvider):
    """Dropbox API v2 delta via list_folder / continue."""

    @property
    def provider_key(self) -> str:
        return "dropbox"

    def _cursor_key(self, account_id: str) -> str:
        return f"bridge:cursor:dropbox:{account_id}"

    def walk_delta(self, context: dict[str, Any]) -> Iterator[dict[str, Any]]:
        accounts = list(context.get("accounts") or [])
        self._oauth_token_override = None
        env_tok = (cfg.DROPBOX_BRIDGE_TOKEN or "").strip()
        if env_tok:
            self._oauth_token_override = env_tok
        elif accounts:
            from nce.bridge_runtime import resolve_stored_oauth_access_token

            t = resolve_stored_oauth_access_token("dropbox", resource_id=str(accounts[0]))
            if t:
                self._oauth_token_override = t

        try:
            for account_id in accounts:
                yield from self._continue_for_account(str(account_id))
        finally:
            self._oauth_token_override = None

    def _continue_for_account(self, account_id: str) -> Iterator[dict[str, Any]]:
        r = redis_client()
        ck = self._cursor_key(account_id)
        raw = r.get(ck)
        if not raw:
            log.warning(
                "Dropbox: no cursor for account=%s — initial list_folder required during bridge connect",
                account_id,
            )
            return
        cursor = raw.decode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.bearer_token()}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=120.0, headers=headers) as client:
            while True:
                resp = client.post(
                    f"{DROPBOX_API}/files/list_folder/continue",
                    json={"cursor": cursor},
                )
                if resp.status_code == 401:
                    raise BridgeAuthError(
                        "Dropbox 401 — set DROPBOX_BRIDGE_TOKEN or reconnect OAuth"
                    )
                resp.raise_for_status()
                data = resp.json()
                yield from data.get("entries", [])
                cursor = data.get("cursor", cursor)
                r.set(ck, cursor)
                if not data.get("has_more"):
                    break

    def download_file(self, file_ref: dict[str, Any]) -> bytes:
        path = file_ref.get("path_lower") or file_ref.get("path_display", "")
        headers = {
            "Authorization": f"Bearer {self.bearer_token()}",
            "Dropbox-API-Arg": json.dumps({"path": path}),
        }
        with httpx.Client(timeout=120.0) as client:
            resp = client.post(
                "https://content.dropboxapi.com/2/files/download",
                headers=headers,
                content=b"",
            )
            resp.raise_for_status()
            return resp.content


def process_dropbox_event(payload: dict[str, Any]) -> dict[str, Any]:
    accounts: list[str] = []
    lf = payload.get("list_folder") or {}
    if isinstance(lf, dict):
        accounts = list(lf.get("accounts") or [])
    if not accounts and payload.get("accounts"):
        accounts = list(payload["accounts"])
    bridge = DropboxBridge()
    count = 0
    try:
        for _ in bridge.walk_delta({"accounts": accounts}):
            count += 1
    except BridgeAuthError as e:
        log.error("%s", e)
        raise
    return {"status": "ok", "entries_seen": count}
