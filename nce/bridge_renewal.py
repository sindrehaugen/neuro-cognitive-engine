from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
import httpx
import jwt

from nce import bridge_repo
from nce.background_task_manager import create_tracked_task
from nce.config import cfg
from nce.http_resilience import oauth_token_post_form
from nce.net_safety import BridgeURLValidationError, validate_bridge_webhook_base_url
from nce.observability import inject_trace_headers
from nce.signing import decrypt_signing_key, require_master_key

# Lazy import redis.asyncio to avoid hard dependency at module load time.
try:
    from redis.asyncio import Redis as AsyncRedis
except ImportError:  # pragma: no cover
    AsyncRedis = None  # type: ignore[misc,assignment]

_REFRESH_LOCK_PREFIX: str = "bridge_refresh"
_REFRESH_LOCK_TTL_SECONDS: int = 60


def _refresh_lock_key(provider: str, bridge_id: Any) -> str:
    return f"{_REFRESH_LOCK_PREFIX}:{provider}:{bridge_id}"


class _DummyRedis:
    async def delete(self, key: str) -> None:
        pass

    async def close(self) -> None:
        pass


async def _acquire_refresh_lock(provider: str, bridge_id: Any) -> Any:
    """Try to acquire a Redis SET-NX-EX lock for *bridge_id*.

    Returns the Redis client instance on success so the caller can close it,
    or ``None`` if the lock is already held.
    """
    if not cfg.REDIS_URL or AsyncRedis is None:
        if cfg.IS_PROD:
            log.error(
                "REDIS_URL not set — skipping background OAuth refresh for bridge_id=%s in production.",
                bridge_id,
            )
            return None
        log.debug(
            "REDIS_URL not set — returning dummy lock client for bridge_id=%s in non-prod.",
            bridge_id,
        )
        return _DummyRedis()

    try:
        redis_client = AsyncRedis.from_url(cfg.REDIS_URL)
        key = _refresh_lock_key(provider, bridge_id)
        acquired = await redis_client.set(key, "1", nx=True, ex=_REFRESH_LOCK_TTL_SECONDS)
        if acquired is not None:
            return redis_client
        await redis_client.close()
        return None
    except Exception as exc:
        log.warning(
            "Redis connection error during OAuth refresh lock acquisition for bridge_id=%s: %s",
            bridge_id,
            exc,
        )
        return None


async def _release_refresh_lock(redis_client: Any, provider: str, bridge_id: Any) -> None:
    if redis_client is None:
        return
    try:
        key = _refresh_lock_key(provider, bridge_id)
        await redis_client.delete(key)
    finally:
        await redis_client.close()


log = logging.getLogger("nce.bridge_renewal")

GRAPH = "https://graph.microsoft.com/v1.0"
DRIVE = "https://www.googleapis.com/drive/v3"


def parse_graph_datetime(value: str) -> datetime:
    s = str(value).replace("Z", "+00:00")
    return datetime.fromisoformat(s)


def _graph_expiration_iso(minutes: int = 4200) -> str:
    """~Graph max ~4230 minutes for drive resource subscriptions."""
    exp = datetime.now(timezone.utc) + timedelta(minutes=min(4200, minutes))
    return exp.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def get_token_expiry(token: str) -> datetime | None:
    try:
        if token.startswith("eyJ") and "." in token:
            decoded = jwt.decode(
                token,
                options={"verify_signature": False},
                algorithms=["HS256", "RS256", "RS384", "ES256"],
            )
            exp = decoded.get("exp")
            if exp:
                return datetime.fromtimestamp(exp, tz=timezone.utc)
    except Exception:
        pass
    return None


async def _perform_oauth_refresh(provider: str, refresh_token: str) -> dict[str, Any]:
    """Call provider token endpoint to refresh the access token."""
    if provider == "sharepoint":
        if not cfg.AZURE_CLIENT_ID or not cfg.AZURE_CLIENT_SECRET:
            raise ValueError("AZURE_CLIENT_ID and AZURE_CLIENT_SECRET required for token refresh")
        tenant = cfg.AZURE_TENANT_ID or "common"
        tok = await oauth_token_post_form(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data={
                "client_id": cfg.AZURE_CLIENT_ID,
                "client_secret": cfg.AZURE_CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            operation="oauth_refresh:sharepoint",
            headers=inject_trace_headers(),
        )
        access_token = tok.get("access_token")
        if not access_token:
            raise ValueError("token refresh response missing access_token")
        expires_in = tok.get("expires_in") or 3600
        new_refresh = tok.get("refresh_token")
        return {
            "access_token": str(access_token),
            "refresh_token": str(new_refresh) if new_refresh else None,
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
            ).timestamp(),
        }

    if provider == "gdrive":
        if not cfg.GDRIVE_OAUTH_CLIENT_ID or not cfg.GDRIVE_OAUTH_CLIENT_SECRET:
            raise ValueError(
                "GDRIVE_OAUTH_CLIENT_ID and GDRIVE_OAUTH_CLIENT_SECRET required for token refresh"
            )
        tok = await oauth_token_post_form(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": cfg.GDRIVE_OAUTH_CLIENT_ID,
                "client_secret": cfg.GDRIVE_OAUTH_CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            operation="oauth_refresh:gdrive",
            headers=inject_trace_headers(),
        )
        access_token = tok.get("access_token")
        if not access_token:
            raise ValueError("token refresh response missing access_token")
        expires_in = tok.get("expires_in") or 3600
        new_refresh = tok.get("refresh_token")
        return {
            "access_token": str(access_token),
            "refresh_token": str(new_refresh) if new_refresh else None,
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
            ).timestamp(),
        }

    if provider == "dropbox":
        if not cfg.DROPBOX_OAUTH_CLIENT_ID:
            raise ValueError("DROPBOX_OAUTH_CLIENT_ID required for token refresh")
        tok = await oauth_token_post_form(
            "https://api.dropboxapi.com/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": cfg.DROPBOX_OAUTH_CLIENT_ID,
            },
            operation="oauth_refresh:dropbox",
            headers=inject_trace_headers(),
        )
        access_token = tok.get("access_token")
        if not access_token:
            raise ValueError("token refresh response missing access_token")
        expires_in = tok.get("expires_in") or 3600
        new_refresh = tok.get("refresh_token")
        return {
            "access_token": str(access_token),
            "refresh_token": str(new_refresh) if new_refresh else None,
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
            ).timestamp(),
        }

    raise ValueError(f"Unsupported refresh provider: {provider}")


async def _bg_refresh_token(
    pool: asyncpg.Pool, row: asyncpg.Record, provider: str, refresh_token: str
) -> None:
    bridge_id = row["id"]
    redis_client = await _acquire_refresh_lock(provider, bridge_id)
    if redis_client is None:
        log.debug("Refresh lock already held for bridge_id=%s — skipping bg refresh", bridge_id)
        return
    try:
        async with pool.acquire(timeout=10.0) as conn:
            async with conn.transaction():
                locked_row = await conn.fetchrow(
                    "SELECT id, oauth_access_token_enc FROM bridge_subscriptions WHERE id = $1 FOR UPDATE",
                    bridge_id,
                )
                if not locked_row:
                    return
                latest_raw = locked_row.get("oauth_access_token_enc")
                if latest_raw:
                    try:
                        with require_master_key() as mk:
                            latest_decrypted = decrypt_signing_key(bytes(latest_raw), mk).decode(
                                "utf-8"
                            )
                        latest_data = json.loads(latest_decrypted)
                        latest_expires_at_ts = latest_data.get("expires_at")
                        latest_expires_at = (
                            datetime.fromtimestamp(latest_expires_at_ts, tz=timezone.utc)
                            if latest_expires_at_ts
                            else None
                        )
                        if latest_expires_at and latest_expires_at >= datetime.now(
                            timezone.utc
                        ) + timedelta(minutes=5):
                            return
                    except Exception:
                        pass

                log.info(
                    "Background refreshing OAuth token for bridge_id=%s, provider=%s",
                    bridge_id,
                    provider,
                )
                refreshed_data = await _perform_oauth_refresh(provider, refresh_token)
                new_payload = {
                    "access_token": refreshed_data["access_token"],
                    "refresh_token": refreshed_data.get("refresh_token") or refresh_token,
                    "expires_at": refreshed_data["expires_at"],
                }
                await bridge_repo.save_token(conn, bridge_id, new_payload)
    except Exception as exc:
        log.error("Background OAuth token refresh failed for bridge_id=%s: %s", bridge_id, exc)
    finally:
        await _release_refresh_lock(redis_client, provider, bridge_id)


async def ensure_fresh_oauth_token(pool: asyncpg.Pool, row: asyncpg.Record, env_token: str) -> str:
    """Return a valid, fresh access token for the given bridge subscription.

    If the env_token is provided, use that immediately.
    Otherwise, load the stored token payload. If it is about to expire (within 5 minutes),
    perform a proactive refresh using the refresh token, holding a database lock to prevent
    concurrent refreshes.
    """
    t = (env_token or "").strip()
    if t:
        return t

    bridge_id = row["id"]
    provider = row["provider"]

    raw = row.get("oauth_access_token_enc")
    if not raw:
        return ""

    try:
        with require_master_key() as mk:
            decrypted = decrypt_signing_key(bytes(raw), mk).decode("utf-8")
    except Exception as e:
        log.warning("Stored bridge OAuth decrypt failed for bridge_id=%s: %s", bridge_id, e)
        return ""

    try:
        data = json.loads(decrypted)
    except Exception:
        # Backward compatibility fallback
        expiry = get_token_expiry(decrypted)
        if expiry and expiry < datetime.now(timezone.utc) + timedelta(minutes=5):
            log.warning(
                "Stored OAuth token for bridge_id=%s looks expired, but has no refresh token.",
                bridge_id,
            )
        return decrypted

    if not isinstance(data, dict) or "access_token" not in data:
        return decrypted

    access_token = data.get("access_token", "")
    refresh_token = data.get("refresh_token")
    expires_at_ts = data.get("expires_at")

    if not refresh_token:
        return access_token

    now = datetime.now(timezone.utc)
    expires_at = datetime.fromtimestamp(expires_at_ts, tz=timezone.utc) if expires_at_ts else None

    if expires_at is None or expires_at < now + timedelta(minutes=5):
        if expires_at and now < expires_at < now + timedelta(minutes=5):
            from rq import get_current_job

            try:
                in_worker = get_current_job() is not None
            except Exception:
                in_worker = False

            if in_worker:
                log.info(
                    "Token for bridge_id=%s is in warning window and running inside RQ worker. "
                    "Returning valid token directly and skipping background task spawn.",
                    bridge_id,
                )
                return access_token

            log.info(
                "Token for bridge_id=%s is still valid but within 5-min warning window. Spawning background refresh.",
                bridge_id,
            )
            create_tracked_task(
                _bg_refresh_token(pool, row, provider, refresh_token),
                name=f"token-refresh-{bridge_id}",
            )
            return access_token

        # Completely expired (or missing expires_at), must refresh synchronously
        async with pool.acquire(timeout=10.0) as conn:
            async with conn.transaction():
                locked_row = await conn.fetchrow(
                    "SELECT id, oauth_access_token_enc FROM bridge_subscriptions WHERE id = $1 FOR UPDATE",
                    bridge_id,
                )
                if not locked_row:
                    return access_token

                latest_raw = locked_row.get("oauth_access_token_enc")
                if not latest_raw:
                    return access_token

                try:
                    with require_master_key() as mk:
                        latest_decrypted = decrypt_signing_key(bytes(latest_raw), mk).decode(
                            "utf-8"
                        )
                    latest_data = json.loads(latest_decrypted)
                except Exception:
                    return access_token

                latest_expires_at_ts = latest_data.get("expires_at")
                latest_expires_at = (
                    datetime.fromtimestamp(latest_expires_at_ts, tz=timezone.utc)
                    if latest_expires_at_ts
                    else None
                )

                if latest_expires_at and latest_expires_at >= datetime.now(
                    timezone.utc
                ) + timedelta(minutes=5):
                    return latest_data.get("access_token", "")

                log.info(
                    "Proactively refreshing OAuth token synchronously for bridge_id=%s, provider=%s",
                    bridge_id,
                    provider,
                )
                try:
                    refreshed_data = await _perform_oauth_refresh(provider, refresh_token)
                    new_payload = {
                        "access_token": refreshed_data["access_token"],
                        "refresh_token": refreshed_data.get("refresh_token") or refresh_token,
                        "expires_at": refreshed_data["expires_at"],
                    }
                    await bridge_repo.save_token(conn, bridge_id, new_payload)
                    return refreshed_data["access_token"]
                except Exception as exc:
                    log.error(
                        "Failed to proactively refresh OAuth token synchronously for bridge_id=%s: %s",
                        bridge_id,
                        exc,
                    )
                    return access_token

    return access_token


def _oauth_bearer_for_row(row: asyncpg.Record, env_token: str) -> str:
    t = (env_token or "").strip()
    if t:
        return t
    raw = row.get("oauth_access_token_enc")
    if not raw:
        return ""
    try:
        with require_master_key() as mk:
            decrypted = decrypt_signing_key(bytes(raw), mk).decode("utf-8")
        try:
            data = json.loads(decrypted)
            if isinstance(data, dict) and "access_token" in data:
                return str(data["access_token"])
        except Exception:
            pass
        return decrypted
    except Exception as e:
        log.warning("Stored bridge OAuth decrypt failed for bridge_id=%s: %s", row.get("id"), e)
        return ""


async def renew_sharepoint(pool: asyncpg.Pool, row: asyncpg.Record) -> None:
    sub_ext = row["subscription_id"]
    if not sub_ext:
        raise RuntimeError("sharepoint renewal requires subscription_id (Graph subscription id)")
    token = await ensure_fresh_oauth_token(pool, row, cfg.GRAPH_BRIDGE_TOKEN or "")
    if not token:
        raise RuntimeError(
            "No Graph token: set GRAPH_BRIDGE_TOKEN or store OAuth on the bridge row (complete_bridge_auth)"
        )

    exp_iso = _graph_expiration_iso()
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.patch(
            f"{GRAPH}/subscriptions/{sub_ext}",
            headers=inject_trace_headers(
                {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                }
            ),
            json={"expirationDateTime": exp_iso},
        )
        if r.status_code == 404:
            raise RuntimeError("Graph subscription not found (404)")
        r.raise_for_status()
        data = r.json()
        new_exp = data.get("expirationDateTime")
        if not new_exp:
            raise RuntimeError("Graph PATCH subscription: missing expirationDateTime")

    expires_at = parse_graph_datetime(str(new_exp))
    async with pool.acquire(timeout=10.0) as conn:
        await conn.execute(
            """
            UPDATE bridge_subscriptions
            SET expires_at = $2, updated_at = NOW(), status = 'ACTIVE'
            WHERE id = $1
            """,
            row["id"],
            expires_at,
        )
    log.info("Renewed SharePoint subscription bridge_id=%s until %s", row["id"], new_exp)


async def renew_gdrive(pool: asyncpg.Pool, row: asyncpg.Record) -> None:
    """
    Drive cannot PATCH watches — stop old channel, register a new watch (Appendix H.4).
    Expects subscription_id = channel id (our UUID), resource_id = Google's resourceId.
    """
    base_raw = (cfg.BRIDGE_WEBHOOK_BASE_URL or "").strip()
    if not base_raw:
        raise RuntimeError("BRIDGE_WEBHOOK_BASE_URL not set — cannot renew Drive watch")
    try:
        base = await validate_bridge_webhook_base_url(base_raw)
    except BridgeURLValidationError as e:
        raise RuntimeError(f"BRIDGE_WEBHOOK_BASE_URL invalid: {e}") from e

    token = await ensure_fresh_oauth_token(pool, row, cfg.GDRIVE_BRIDGE_TOKEN or "")
    if not token:
        raise RuntimeError(
            "No Google token: set GDRIVE_BRIDGE_TOKEN or store OAuth on the bridge row (complete_bridge_auth)"
        )

    old_chan = row["subscription_id"]
    old_resource = row["resource_id"]
    if not old_chan or not old_resource:
        raise RuntimeError(
            "gdrive renewal requires subscription_id (channel id) and resource_id (Google resourceId)"
        )

    state = row["client_state"] or ""
    new_chan = str(uuid.uuid4())
    expiration_ms = int(
        (datetime.now(timezone.utc) + timedelta(days=6, hours=23)).timestamp() * 1000
    )
    watch_payload = {
        "id": new_chan,
        "type": "web_hook",
        "address": f"{base}/webhooks/drive",
        "token": state,
        "expiration": expiration_ms,
    }

    headers = inject_trace_headers(
        {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
    )
    async with httpx.AsyncClient(timeout=60.0) as client:
        r_stop = await client.post(
            f"{DRIVE}/channels/stop",
            headers=headers,
            json={"id": old_chan, "resourceId": old_resource},
        )
        if r_stop.status_code not in (200, 204):
            log.warning(
                "Drive channels/stop non-success: %s %s",
                r_stop.status_code,
                r_stop.text[:200],
            )

        r = await client.post(
            f"{DRIVE}/changes/watch",
            headers=headers,
            json=watch_payload,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Drive changes/watch failed: {r.status_code} {r.text[:300]}")

        data = r.json()
        new_resource = data.get("resourceId")
        exp_ms = data.get("expiration")
        if not new_resource or not exp_ms:
            raise RuntimeError("Drive watch response missing resourceId or expiration")

    expires_at = datetime.fromtimestamp(int(exp_ms) / 1000.0, tz=timezone.utc)
    async with pool.acquire(timeout=10.0) as conn:
        await conn.execute(
            """
            UPDATE bridge_subscriptions
            SET subscription_id = $2,
                resource_id = $3,
                expires_at = $4,
                status = 'ACTIVE',
                updated_at = NOW()
            WHERE id = $1
            """,
            row["id"],
            new_chan,
            new_resource,
            expires_at,
        )
    log.info("Renewed Google Drive watch bridge_id=%s", row["id"])


async def renew_dropbox(_pool: asyncpg.Pool, row: asyncpg.Record) -> None:
    """Dropbox has no subscription expiry; optional no-op to refresh updated_at."""
    log.debug("Dropbox bridge_id=%s — no subscription renewal (cursor only)", row["id"])


async def mark_degraded(pool: asyncpg.Pool, bridge_id, reason: str) -> None:
    log.error("Marking bridge DEGRADED id=%s reason=%s", bridge_id, reason)
    async with pool.acquire(timeout=10.0) as conn:
        await conn.execute(
            """
            UPDATE bridge_subscriptions
            SET status = 'DEGRADED', updated_at = NOW()
            WHERE id = $1
            """,
            bridge_id,
        )


async def renew_expiring_subscriptions(pool: asyncpg.Pool) -> dict[str, int]:
    """
    Find ACTIVE rows whose expires_at is within BRIDGE_RENEWAL_LOOKAHEAD_HOURS,
    renew per provider, mark DEGRADED on any failure.
    """
    lookahead = timedelta(hours=max(1, cfg.BRIDGE_RENEWAL_LOOKAHEAD_HOURS))
    renewed = 0
    failed = 0
    skipped = 0

    async with pool.acquire(timeout=10.0) as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM bridge_subscriptions
            WHERE status = 'ACTIVE'
              AND expires_at IS NOT NULL
              AND expires_at < NOW() + $1::interval
            ORDER BY expires_at ASC
            LIMIT 100
            """,
            lookahead,
        )

    log.info(
        "audit bridge_subscription_renewal_tick candidates=%s lookahead_hours=%s",
        len(rows),
        cfg.BRIDGE_RENEWAL_LOOKAHEAD_HOURS,
    )

    for row in rows:
        prov = row["provider"]
        try:
            if prov == "sharepoint":
                await renew_sharepoint(pool, row)
                renewed += 1
            elif prov == "gdrive":
                await renew_gdrive(pool, row)
                renewed += 1
            elif prov == "dropbox":
                await renew_dropbox(pool, row)
                skipped += 1
            else:
                skipped += 1
        except Exception as e:
            log.exception("Renewal failed for bridge %s", row["id"])
            await mark_degraded(pool, row["id"], str(e))
            failed += 1

    return {
        "renewed": renewed,
        "failed": failed,
        "skipped": skipped,
        "candidates": len(rows),
    }
