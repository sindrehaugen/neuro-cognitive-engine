"""
MCP tool handlers for document bridges (§10.6). Uses `NCEEngine.pg_pool` and `nce/bridges/`.
"""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx
from redis import Redis

from nce import bridge_repo
from nce.bridge_providers import BRIDGE_PROVIDERS
from nce.config import cfg
from nce.extractors.dispatch import get_priority_queue
from nce.http_resilience import oauth_token_post_form, post_json_with_retry
from nce.mcp_errors import mcp_handler
from nce.net_safety import BridgeURLValidationError, validate_bridge_webhook_base_url
from nce.observability import inject_trace_headers
from nce.orchestrator import NCEEngine

# Token encryption/decryption now handled by bridge_repo (canonical hooks)
from nce.tasks import process_bridge_event

log = logging.getLogger("nce.bridge_mcp_handlers")

PROVIDERS = BRIDGE_PROVIDERS


def _parse_sharepoint_resource(resource_id: str) -> tuple[str, str]:
    if "|" not in resource_id:
        raise ValueError("sharepoint resource_id must be 'site_id|drive_id'")
    site, drive = resource_id.split("|", 1)
    if not site or not drive:
        raise ValueError("Invalid site|drive pair")
    return site, drive


def _token_payload_from_oauth_response(tok: dict[str, Any]) -> dict[str, Any]:
    access_token = tok.get("access_token")
    if not access_token:
        raise ValueError("token response missing access_token")
    refresh_token = tok.get("refresh_token")
    expires_in = tok.get("expires_in") or 3600
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).timestamp()
    return {
        "access_token": str(access_token),
        "refresh_token": str(refresh_token) if refresh_token else None,
        "expires_at": expires_at,
    }


async def _exchange_oauth_code(provider: str, code: str) -> dict[str, Any]:
    """Trade the authorization code for an access token payload (provider-specific OAuth2)."""
    trace_headers = inject_trace_headers()

    if provider == "sharepoint":
        if not cfg.AZURE_CLIENT_ID or not cfg.AZURE_CLIENT_SECRET:
            raise ValueError("AZURE_CLIENT_ID and AZURE_CLIENT_SECRET required for token exchange")
        tenant = cfg.AZURE_TENANT_ID or "common"
        tok = await oauth_token_post_form(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data={
                "client_id": cfg.AZURE_CLIENT_ID,
                "client_secret": cfg.AZURE_CLIENT_SECRET,
                "code": code,
                "redirect_uri": cfg.BRIDGE_OAUTH_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            operation="oauth_exchange:sharepoint",
            timeout=60.0,
            headers=trace_headers,
        )
        return _token_payload_from_oauth_response(tok)

    if provider == "gdrive":
        if not cfg.GDRIVE_OAUTH_CLIENT_ID or not cfg.GDRIVE_OAUTH_CLIENT_SECRET:
            raise ValueError("GDRIVE_OAUTH_CLIENT_ID/SECRET required")
        tok = await oauth_token_post_form(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": cfg.GDRIVE_OAUTH_CLIENT_ID,
                "client_secret": cfg.GDRIVE_OAUTH_CLIENT_SECRET,
                "code": code,
                "redirect_uri": cfg.BRIDGE_OAUTH_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            operation="oauth_exchange:gdrive",
            timeout=60.0,
            headers=trace_headers,
        )
        return _token_payload_from_oauth_response(tok)

    if provider == "dropbox":
        if not cfg.DROPBOX_OAUTH_CLIENT_ID:
            raise ValueError("DROPBOX_OAUTH_CLIENT_ID required")
        tok = await oauth_token_post_form(
            "https://api.dropboxapi.com/oauth2/token",
            data={
                "code": code,
                "grant_type": "authorization_code",
                "client_id": cfg.DROPBOX_OAUTH_CLIENT_ID,
                "redirect_uri": cfg.BRIDGE_OAUTH_REDIRECT_URI,
            },
            operation="oauth_exchange:dropbox",
            timeout=60.0,
            headers=trace_headers,
        )
        return _token_payload_from_oauth_response(tok)

    raise ValueError(f"unknown provider: {provider!r}")


async def _setup_sharepoint_webhook(
    access_token: str,
    *,
    base: str,
    site: str,
    drive: str,
    bridge_client_state: str,
) -> tuple[str | None, datetime | None]:
    """Create a Microsoft Graph subscription; returns (subscription_id, expires_at)."""
    exp_iso = (datetime.now(timezone.utc) + timedelta(minutes=4200)).strftime(
        "%Y-%m-%dT%H:%M:%S.0000000Z"
    )
    body = {
        "changeType": "updated",
        "notificationUrl": f"{base}/webhooks/graph",
        "resource": f"/sites/{site}/drives/{drive}/root",
        "expirationDateTime": exp_iso,
        "clientState": bridge_client_state,
    }
    try:
        sub = await post_json_with_retry(
            "https://graph.microsoft.com/v1.0/subscriptions",
            body,
            operation="setup_webhook:sharepoint",
            headers=inject_trace_headers(
                {
                    "Authorization": f"Bearer {access_token}",
                }
            ),
        )
    except Exception as exc:
        raise ValueError(f"Graph subscription failed: {exc}") from exc

    sub_id = sub.get("id")
    exp_s = sub.get("expirationDateTime")
    expires_at: datetime | None = None
    if exp_s:
        expires_at = datetime.fromisoformat(str(exp_s).replace("Z", "+00:00"))
    return sub_id, expires_at


async def _setup_gdrive_webhook(
    access_token: str,
    *,
    base: str,
    bridge_client_state: str,
    resource_id: str,
) -> tuple[str | None, str, datetime | None]:
    """
    Register a Drive changes channel.
    Returns (subscription_id / channel id, resource_id from Google, expires_at).
    """
    chan = str(uuid.uuid4())
    exp_ms = int((datetime.now(timezone.utc) + timedelta(days=6, hours=23)).timestamp() * 1000)
    watch_body = {
        "id": chan,
        "type": "web_hook",
        "address": f"{base}/webhooks/drive",
        "token": bridge_client_state or "",
        "expiration": exp_ms,
    }
    try:
        sub = await post_json_with_retry(
            "https://www.googleapis.com/drive/v3/changes/watch",
            watch_body,
            operation="setup_webhook:gdrive",
            headers=inject_trace_headers(
                {
                    "Authorization": f"Bearer {access_token}",
                }
            ),
        )
    except Exception as exc:
        raise ValueError(f"Drive watch failed: {exc}") from exc

    sub_id = sub.get("id") or chan
    rid = sub.get("resourceId") or resource_id
    exp_ms2 = sub.get("expiration")
    expires_at: datetime | None = None
    if exp_ms2:
        expires_at = datetime.fromtimestamp(int(exp_ms2) / 1000.0, tz=timezone.utc)
    return sub_id, str(rid), expires_at


@mcp_handler
async def connect_bridge(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    user_id = arguments["user_id"]
    namespace_id = uuid.UUID(arguments["namespace_id"])
    provider = arguments["provider"].strip().lower()
    if provider not in PROVIDERS:
        raise ValueError(f"provider must be one of {sorted(PROVIDERS)}")

    client_state = secrets.token_urlsafe(32)
    row_id = uuid.uuid4()

    async with engine.pg_pool.acquire(timeout=10.0) as conn:
        await bridge_repo.insert_subscription(
            conn,
            user_id=user_id,
            namespace_id=namespace_id,
            provider=provider,
            resource_id="pending",
            status="REQUESTED",
            client_state=client_state,
            row_id=row_id,
        )

    auth_url: str | None = None
    if provider == "sharepoint":
        cid = (cfg.AZURE_CLIENT_ID or "").strip()
        if not cid:
            return json.dumps(
                {
                    "status": "pending_config",
                    "bridge_id": str(row_id),
                    "client_state": client_state,
                    "message": "Set AZURE_CLIENT_ID (and BRIDGE_OAUTH_REDIRECT_URI) to obtain auth_url.",
                }
            )
        tenant = cfg.AZURE_TENANT_ID or "common"
        q = urlencode(
            {
                "client_id": cid,
                "response_type": "code",
                "redirect_uri": cfg.BRIDGE_OAUTH_REDIRECT_URI,
                "response_mode": "query",
                "scope": "offline_access Files.Read.All Sites.Read.All",
                "state": f"{row_id}:{client_state}",
            }
        )
        auth_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize?{q}"
    elif provider == "gdrive":
        cid = (cfg.GDRIVE_OAUTH_CLIENT_ID or "").strip()
        if not cid:
            return json.dumps(
                {
                    "status": "pending_config",
                    "bridge_id": str(row_id),
                    "client_state": client_state,
                    "message": "Set GDRIVE_OAUTH_CLIENT_ID for Google OAuth URL.",
                }
            )
        q = urlencode(
            {
                "client_id": cid,
                "redirect_uri": cfg.BRIDGE_OAUTH_REDIRECT_URI,
                "response_type": "code",
                "scope": "https://www.googleapis.com/auth/drive.readonly",
                "access_type": "offline",
                "prompt": "consent",
                "state": f"{row_id}:{client_state}",
            }
        )
        auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{q}"
    elif provider == "dropbox":
        cid = (cfg.DROPBOX_OAUTH_CLIENT_ID or "").strip()
        if not cid:
            return json.dumps(
                {
                    "status": "pending_config",
                    "bridge_id": str(row_id),
                    "client_state": client_state,
                    "message": "Set DROPBOX_OAUTH_CLIENT_ID for Dropbox authorize URL.",
                }
            )
        q = urlencode(
            {
                "client_id": cid,
                "redirect_uri": cfg.BRIDGE_OAUTH_REDIRECT_URI,
                "response_type": "code",
                "token_access_type": "offline",
                "state": f"{row_id}:{client_state}",
            }
        )
        auth_url = f"https://www.dropbox.com/oauth2/authorize?{q}"

    return json.dumps(
        {
            "status": "ok",
            "bridge_id": str(row_id),
            "provider": provider,
            "auth_url": auth_url,
            "client_state": client_state,
            "note": "Complete OAuth in browser; call complete_bridge_auth with the authorization code.",
        }
    )


@mcp_handler
async def complete_bridge_auth(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    user_id = arguments["user_id"]
    bridge_id = uuid.UUID(arguments["bridge_id"])
    provider = arguments["provider"].strip().lower()
    code = (arguments.get("authorization_code") or arguments.get("code") or "").strip()
    resource_id = (arguments.get("resource_id") or "").strip()

    if not code:
        raise ValueError("authorization_code is required")
    if not resource_id or resource_id == "pending":
        raise ValueError(
            "resource_id required after OAuth: sharepoint 'site_id|drive_id', "
            "gdrive Google resourceId or folder Id as configured, dropbox account id (dbid:...)"
        )

    async with engine.pg_pool.acquire(timeout=10.0) as conn:
        row = await bridge_repo.get_by_id(conn, bridge_id)
        if not row or str(row["user_id"]) != str(user_id):
            raise ValueError("bridge not found for user")
        if row["provider"] != provider:
            raise ValueError("provider mismatch")
        if row["status"] not in ("REQUESTED", "VALIDATING"):
            raise ValueError(f"bridge not in connectable state (got {row['status']})")
        bridge_client_state = row["client_state"]

    token_data = await _exchange_oauth_code(provider, code)
    access_token = token_data["access_token"]

    sub_id: str | None = None
    expires_at: datetime | None = None
    final_resource_id = resource_id

    if provider in ("sharepoint", "gdrive"):
        base_raw = (cfg.BRIDGE_WEBHOOK_BASE_URL or "").strip()
        if not base_raw:
            raise ValueError(
                "BRIDGE_WEBHOOK_BASE_URL is required to register sharepoint/gdrive webhooks"
            )
        try:
            base = await validate_bridge_webhook_base_url(base_raw)
        except BridgeURLValidationError as e:
            raise ValueError(str(e)) from e

        if provider == "sharepoint":
            site, drive = _parse_sharepoint_resource(final_resource_id)
            sub_id, expires_at = await _setup_sharepoint_webhook(
                access_token,
                base=base,
                site=site,
                drive=drive,
                bridge_client_state=bridge_client_state or "",
            )
        else:
            sub_id, final_resource_id, expires_at = await _setup_gdrive_webhook(
                access_token,
                base=base,
                bridge_client_state=bridge_client_state or "",
                resource_id=final_resource_id,
            )

    async with engine.pg_pool.acquire(timeout=10.0) as conn:
        await conn.execute(
            """
            UPDATE bridge_subscriptions
            SET resource_id = $2,
                subscription_id = COALESCE($3, subscription_id),
                expires_at = COALESCE($4, expires_at),
                status = 'ACTIVE',
                updated_at = NOW()
            WHERE id = $1
            """,
            bridge_id,
            final_resource_id,
            sub_id,
            expires_at,
        )
        await bridge_repo.save_token(conn, bridge_id, token_data)

    return json.dumps(
        {
            "status": "ok",
            "bridge_id": str(bridge_id),
            "provider": provider,
            "subscription_id": sub_id,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "resource_id": final_resource_id,
            "message": (
                "OAuth access token stored encrypted server-side on this bridge row. "
                "Workers load it automatically when env bridge tokens are unset."
            ),
        }
    )


@mcp_handler
async def list_bridges(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    user_id = arguments["user_id"]
    include_disconnected = bool(arguments.get("include_disconnected", False))

    async with engine.pg_pool.acquire(timeout=10.0) as conn:
        rows = await bridge_repo.list_for_user(
            conn, user_id, include_disconnected=include_disconnected
        )
    return json.dumps({"bridges": [bridge_repo.subscription_to_public_dict(r) for r in rows]})


@mcp_handler
async def disconnect_bridge(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    user_id = arguments["user_id"]
    bridge_id = uuid.UUID(arguments["bridge_id"])
    async with engine.pg_pool.acquire(timeout=10.0) as conn:
        row = await bridge_repo.get_by_id(conn, bridge_id)
        if not row or str(row["user_id"]) != user_id:
            raise ValueError("bridge not found for user")

    prov = row["provider"]
    token = ""
    if prov == "sharepoint":
        token = (cfg.GRAPH_BRIDGE_TOKEN or "").strip()
    elif prov == "gdrive":
        token = (cfg.GDRIVE_BRIDGE_TOKEN or "").strip()
    elif prov == "dropbox":
        token = (cfg.DROPBOX_BRIDGE_TOKEN or "").strip()

    token_payload: dict[str, Any] | None = None
    if not token:
        async with engine.pg_pool.acquire(timeout=10.0) as conn:
            token_payload = await bridge_repo.get_token(conn, bridge_id)
        if token_payload:
            token = token_payload.get("access_token", "")

    if prov == "sharepoint" and row["subscription_id"] and token:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.delete(
                f"https://graph.microsoft.com/v1.0/subscriptions/{row['subscription_id']}",
                headers=inject_trace_headers({"Authorization": f"Bearer {token}"}),
            )
            if r.status_code not in (200, 204, 404):
                log.warning("Graph subscription delete: %s %s", r.status_code, r.text[:200])
    elif prov == "gdrive" and row["subscription_id"] and row["resource_id"] and token:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                "https://www.googleapis.com/drive/v3/channels/stop",
                headers=inject_trace_headers(
                    {
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    }
                ),
                json={"id": row["subscription_id"], "resourceId": row["resource_id"]},
            )
            if r.status_code not in (200, 204):
                log.warning("Drive channel stop: %s %s", r.status_code, r.text[:200])

    async with engine.pg_pool.acquire(timeout=10.0) as conn:
        await conn.execute(
            """
            UPDATE bridge_subscriptions
            SET status = 'DISCONNECTED',
                updated_at = NOW()
            WHERE id = $1
            """,
            bridge_id,
        )
        await bridge_repo.save_token(conn, bridge_id, {})

    return json.dumps({"status": "ok", "bridge_id": str(bridge_id), "state": "DISCONNECTED"})


@mcp_handler
async def force_resync_bridge(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    user_id = arguments["user_id"]
    bridge_id = uuid.UUID(arguments["bridge_id"])
    async with engine.pg_pool.acquire(timeout=10.0) as conn:
        row = await bridge_repo.get_by_id(conn, bridge_id)
        if not row or str(row["user_id"]) != user_id:
            raise ValueError("bridge not found for user")
        await conn.execute(
            "UPDATE bridge_subscriptions SET cursor = NULL, updated_at = NOW() WHERE id = $1",
            bridge_id,
        )

    prov = row["provider"]
    rcli = bridge_redis()
    rid = row["resource_id"]
    if prov == "sharepoint" and "|" in rid:
        site, drive = _parse_sharepoint_resource(rid)
        ck = f"bridge:cursor:sharepoint:{site}:{drive}"
        rcli.delete(ck)

    q = get_priority_queue(0, Redis.from_url(cfg.REDIS_URL))
    job_id: str | None = None
    if prov == "sharepoint":
        if "|" not in rid or rid == "pending":
            raise ValueError("sharepoint force_resync requires resource_id 'site_id|drive_id'")
        site, drive = _parse_sharepoint_resource(rid)
        payload: dict[str, Any] = {
            "notifications": [
                {
                    "clientState": row["client_state"],
                    "resource": f"sites/{site}/drives/{drive}/root",
                    "changeType": "updated",
                }
            ]
        }
        job = q.enqueue(
            process_bridge_event,
            kwargs={"provider": "sharepoint", "payload": payload},
            job_timeout="30m",
        )
        job_id = job.id
    elif prov == "gdrive":
        ch = row["subscription_id"] or ""
        payload = {
            "channel_id": ch,
            "resource_id": rid,
            "resource_state": "update",
            "message_number": "manual",
        }
        job = q.enqueue(
            process_bridge_event,
            kwargs={"provider": "gdrive", "payload": payload},
            job_timeout="30m",
        )
        job_id = job.id
    elif prov == "dropbox":
        payload = {"list_folder": {"accounts": [rid]}}  # type: ignore[dict-item]
        job = q.enqueue(
            process_bridge_event,
            kwargs={"provider": "dropbox", "payload": payload},
            job_timeout="30m",
        )
        job_id = job.id

    return json.dumps(
        {
            "status": "enqueued",
            "bridge_id": str(bridge_id),
            "provider": prov,
            "job_id": job_id,
        }
    )


def bridge_redis() -> Redis:
    return Redis.from_url(cfg.REDIS_URL)


@mcp_handler
async def bridge_status(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    user_id = arguments["user_id"]
    bridge_id = uuid.UUID(arguments["bridge_id"])
    async with engine.pg_pool.acquire(timeout=10.0) as conn:
        row = await bridge_repo.get_by_id(conn, bridge_id)
        if not row or str(row["user_id"]) != user_id:
            raise ValueError("bridge not found for user")
    out = bridge_repo.subscription_to_public_dict(row)
    now = datetime.now(timezone.utc)
    if row["expires_at"]:
        out["expires_in_seconds"] = max(0, int((row["expires_at"] - now).total_seconds()))
    return json.dumps(out)
