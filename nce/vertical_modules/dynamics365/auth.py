"""
nce/vertical_modules/dynamics365/auth.py
=========================================
Azure AD client credentials OAuth 2.0 flow for the Dataverse scope.

Reuses the existing ``AZURE_CLIENT_ID`` / ``AZURE_CLIENT_SECRET`` /
``AZURE_TENANT_ID`` env vars already configured for SharePoint.  Unlike the
SharePoint bridge (user-delegated tokens with refresh), Dataverse uses pure
client credentials — no refresh token is issued, so the token is simply
re-acquired when the Redis cache TTL expires.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

from nce.config import cfg
from nce.http_resilience import oauth_token_post_form

if TYPE_CHECKING:
    import redis.asyncio as aioredis

log = logging.getLogger("nce.vertical_modules.dynamics365.auth")

_TOKEN_BUFFER_S = 120  # expire cache 2 min before the actual token TTL


def _redis_cache_key() -> str:
    return f"nce:d365:token:{cfg.AZURE_TENANT_ID}"


class DataverseTokenManager:
    """
    Manages a client-credentials bearer token for the Dataverse Web API.

    Tokens are cached in Redis to avoid an Azure AD round-trip on every cron
    tick.  When the cache misses, ``oauth_token_post_form()`` is called against
    the standard Azure AD v2 endpoint.

    Parameters
    ----------
    redis_client:
        Async Redis client (``redis.asyncio.Redis``).
    """

    def __init__(self, redis_client: aioredis.Redis) -> None:
        self._redis = redis_client

    async def get_access_token(self) -> str:
        """
        Return a valid bearer token for ``{NCE_D365_ORG_URL}/.default``.

        Order of precedence:
        1. Redis cache (``nce:d365:token:{tenant_id}``)
        2. Fresh Azure AD token request via ``oauth_token_post_form()``
        """
        # 1. Cache hit
        try:
            cached = await self._redis.get(_redis_cache_key())
            if cached:
                data = json.loads(cached)
                token = data.get("access_token", "")
                if token:
                    return token
        except Exception as exc:
            log.warning("D365 token cache read failed: %s", exc)

        # 2. Fetch fresh token
        token_data = await self._fetch_fresh()
        return token_data["access_token"]

    async def _fetch_fresh(self) -> dict[str, Any]:
        """Request a new token from Azure AD and cache the result."""
        if not cfg.AZURE_TENANT_ID:
            raise RuntimeError("AZURE_TENANT_ID must be set for D365 OAuth")
        if not cfg.AZURE_CLIENT_ID or not cfg.AZURE_CLIENT_SECRET:
            raise RuntimeError("AZURE_CLIENT_ID and AZURE_CLIENT_SECRET must be set for D365 OAuth")
        if not cfg.NCE_D365_ORG_URL:
            raise RuntimeError("NCE_D365_ORG_URL must be set for D365 OAuth scope")

        token_url = f"https://login.microsoftonline.com/{cfg.AZURE_TENANT_ID}/oauth2/v2.0/token"
        scope = f"{cfg.NCE_D365_ORG_URL}/.default"

        response = await oauth_token_post_form(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": cfg.AZURE_CLIENT_ID,
                "client_secret": cfg.AZURE_CLIENT_SECRET,
                "scope": scope,
            },
        )

        access_token = response.get("access_token", "")
        expires_in = int(response.get("expires_in", 3600))
        ttl = max(60, expires_in - _TOKEN_BUFFER_S)

        if access_token:
            try:
                payload = json.dumps({"access_token": access_token, "fetched_at": time.time()})
                await self._redis.setex(_redis_cache_key(), ttl, payload)
                log.info("D365 token cached for %s seconds", ttl)
            except Exception as exc:
                log.warning("D365 token cache write failed: %s", exc)

        return response
