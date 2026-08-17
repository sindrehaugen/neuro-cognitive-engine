from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from functools import lru_cache
from typing import Any

import asyncpg  # type: ignore[import-untyped]
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from redis import Redis
from starlette.responses import JSONResponse

from nce import bridge_repo
from nce.config import cfg
from nce.extractors.dispatch import get_priority_queue
from nce.net_safety import BridgeURLValidationError, validate_webhook_payload_url
from nce.observability import enqueue_traced
from nce.tasks import process_bridge_event

log = logging.getLogger("nce.webhook_receiver")

app = FastAPI(title="NCE Webhook Receiver")

# Production guardrails (override via nce.config.cfg / env).
_MAX_BODY_BYTES = cfg.WEBHOOK_MAX_BODY_BYTES
_RATE_LIMIT = cfg.WEBHOOK_RATE_LIMIT
_RATE_PERIOD_S = cfg.WEBHOOK_RATE_PERIOD_SECONDS

# In-memory sliding window per client IP (per webhook-receiver instance).
_ip_windows: dict[str, list[float]] = {}

_DEDUP_TTL_S = cfg.WEBHOOK_DEDUP_TTL_SECONDS
_DEDUP_FAIL_OPEN = cfg.WEBHOOK_DEDUP_FAIL_OPEN

# Atomic sliding-window rate limit (sync Redis; mirrors nce.auth._RATE_LIMIT_LUA).
_RATE_LIMIT_LUA = """
local key = KEYS[1]
local window_start = ARGV[1]
local now = ARGV[2]
local limit = tonumber(ARGV[3])
local period = tonumber(ARGV[4])

redis.call('ZREMRANGEBYSCORE', key, 0, window_start)
local count = redis.call('ZCARD', key)
if count >= limit then
    return 0
end
redis.call('ZADD', key, now, now)
redis.call('EXPIRE', key, period)
return 1
"""


@app.get("/health")
async def health():
    """Baseline healthcheck for container orchestration."""
    return {"status": "ok"}


def _require_cfg_secret(attr: str) -> str:
    value = (os.environ.get(attr) or getattr(cfg, attr, "") or "").strip()
    if not value:
        raise RuntimeError(
            f"{attr} must be set in the environment (no default allowed for webhook secrets)"
        )
    return value


# Dropbox validates the request body HMAC against this app secret (still required).
DROPBOX_APP_SECRET = _require_cfg_secret("DROPBOX_APP_SECRET")
# NOTE: GRAPH_CLIENT_STATE / DRIVE_CHANNEL_TOKEN are intentionally no longer read
# here. Graph (clientState) and Drive (channel token) secrets are now per-bridge
# random values stored in ``bridge_subscriptions.client_state`` and validated at
# request time against the matching subscription row (see graph_webhook /
# drive_webhook). Operators no longer need to set those two global env vars.

# D365 webhook secret — only validated at request time (optional integration).
_D365_WEBHOOK_SECRET: str = (os.environ.get("NCE_D365_WEBHOOK_SECRET") or "").strip()


@lru_cache(maxsize=1)
def _redis_client() -> Redis:
    """Shared sync Redis client for RQ enqueue (one pool per process)."""
    return Redis.from_url(cfg.REDIS_URL)


# Lazy async Postgres pool (one per process). Mirrors the ``_redis_client``
# lru_cache pattern, but asyncpg pool creation is async so it cannot be wrapped
# in lru_cache directly — guard a module-global with a lock instead. Uses the
# same DSN/sizing as the orchestrator pool (cfg.PG_DSN, PG_MIN_POOL/PG_MAX_POOL).
_pg_pool: asyncpg.Pool | None = None
_pg_pool_lock = asyncio.Lock()


async def _get_pg_pool() -> asyncpg.Pool:
    """Return the shared asyncpg pool, creating it on first use (one per process)."""
    global _pg_pool
    if _pg_pool is None:
        async with _pg_pool_lock:
            if _pg_pool is None:
                _pg_pool = await asyncpg.create_pool(
                    cfg.PG_DSN,
                    min_size=cfg.PG_MIN_POOL,
                    max_size=cfg.PG_MAX_POOL,
                    command_timeout=30,
                )
    return _pg_pool


async def _fetch_bridge_subscription(provider: str, subscription_id: str) -> asyncpg.Record | None:
    """Look up the ACTIVE bridge row for (provider, external subscription id).

    The webhook receiver has no tenant namespace context at receive time — the
    inbound notification only carries the provider's external subscription /
    channel id — so this is a global metadata read keyed on that id.
    """
    pool = await _get_pg_pool()
    async with pool.acquire(timeout=10.0) as conn:
        return await bridge_repo.fetch_active_subscription(
            conn, provider, subscription_id=subscription_id
        )


def _client_ip(request: Request) -> str:
    """Client IP for rate limiting; honor X-Forwarded-For only behind a trusted proxy."""
    if cfg.NCE_WEBHOOK_TRUST_PROXY:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip() or "unknown"
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _allow_webhook_request_memory(client_ip: str, path: str) -> bool:
    """In-memory sliding window keyed by IP + path (single-instance fallback)."""
    now = time.time()
    key = f"{client_ip}:{path}"
    window = _ip_windows.setdefault(key, [])
    window[:] = [t for t in window if t > now - _RATE_PERIOD_S]
    if len(window) >= _RATE_LIMIT:
        return False
    window.append(now)
    return True


def _allow_webhook_request_redis(client_ip: str, path: str) -> bool | None:
    """Redis sliding window; returns None when Redis is unavailable."""
    try:
        now = time.time()
        redis_key = f"nce:ratelimit:webhook:{client_ip}:{path}"
        result = _redis_client().eval(
            _RATE_LIMIT_LUA,
            1,
            redis_key,
            str(now - _RATE_PERIOD_S),
            str(now),
            str(_RATE_LIMIT),
            str(_RATE_PERIOD_S),
        )
        return bool(result)
    except Exception as exc:
        log.warning("Webhook Redis rate limiter unavailable: %s", exc)
        return None


def _allow_webhook_request(client_ip: str, path: str) -> bool:
    """Sliding-window rate limit keyed by IP + path (Redis with RAM fallback)."""
    redis_allowed = _allow_webhook_request_redis(client_ip, path)
    if redis_allowed is not None:
        return redis_allowed
    if cfg.IS_PROD:
        log.warning(
            "Webhook rate limit: Redis unavailable in production; rejecting ip=%s path=%s",
            client_ip,
            path,
        )
        return False
    return _allow_webhook_request_memory(client_ip, path)


def _dedup_key(provider: str, payload: dict[str, Any]) -> str | None:
    """Stable deduplication key per provider payload (None = always process)."""
    if provider == "dropbox":
        accounts = (payload.get("list_folder") or {}).get("accounts") or []
        if not accounts:
            return None
        digest = hashlib.sha256(
            json.dumps(sorted(accounts), sort_keys=True).encode("utf-8")
        ).hexdigest()[:32]
        return f"nce:webhook:dedup:dropbox:{digest}"
    if provider == "sharepoint":
        notifications = payload.get("notifications") or []
        parts: list[str] = []
        for note in notifications:
            if not isinstance(note, dict):
                continue
            note_id = note.get("id") or note.get("subscriptionId") or ""
            resource = note.get("resource") or ""
            change = note.get("changeType") or ""
            parts.append(f"{note_id}|{resource}|{change}")
        if not parts:
            return None
        digest = hashlib.sha256("|".join(sorted(parts)).encode("utf-8")).hexdigest()[:32]
        return f"nce:webhook:dedup:sharepoint:{digest}"
    if provider == "gdrive":
        channel_id = str(payload.get("channel_id") or "")
        if not channel_id:
            return None
        resource_id = str(payload.get("resource_id") or "")
        message_number = str(payload.get("message_number") or "")
        resource_state = str(payload.get("resource_state") or "")
        raw = f"{channel_id}|{resource_id}|{message_number}|{resource_state}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        return f"nce:webhook:dedup:gdrive:{digest}"
    return None


def _claim_dedup(key: str) -> bool:
    """Return True when this delivery should be enqueued (first-seen within TTL)."""
    try:
        return bool(_redis_client().set(key, "1", nx=True, ex=_DEDUP_TTL_S))
    except Exception as exc:
        log.warning("Webhook dedup Redis unavailable: %s", exc)
        if _DEDUP_FAIL_OPEN:
            return True
        return False


def register_echo(system: str, entity_id: str, origin_event_id: str) -> None:
    """Record that NCE itself caused an outward change on *entity_id* in *system*.

    Sets ``nce:echo:{system}:{entity_id}`` in Redis with TTL ``cfg.NCE_ECHO_TTL_S``
    (default 600 s) so the matching inbound webhook is recognised as a self-echo
    and its semantic re-ingestion is suppressed.

    Called by Batch-129 mutating tools; exposed here (next to ``_claim_dedup``)
    so the consumer in ``tasks.py`` has a symmetric counterpart in the same module.
    """
    key = f"nce:echo:{system}:{entity_id}"
    try:
        _redis_client().set(key, origin_event_id, ex=cfg.NCE_ECHO_TTL_S)
    except Exception as exc:
        log.warning("Echo register failed system=%s entity_id=%s: %s", system, entity_id, exc)


def check_echo(system: str, entity_id: str) -> str | None:
    """Return the ``origin_event_id`` stored in the echo set, or ``None`` if absent.

    A non-``None`` return means this webhook was caused by NCE itself.
    """
    key = f"nce:echo:{system}:{entity_id}"
    try:
        raw = _redis_client().get(key)
        if raw is None:
            return None
        return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    except Exception as exc:
        log.warning("Echo check failed system=%s entity_id=%s: %s", system, entity_id, exc)
        return None


async def _read_body_bounded(request: Request) -> bytes:
    """Read request body and reject oversize payloads before parsing."""
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > _MAX_BODY_BYTES:
                raise HTTPException(status_code=413, detail="Request body too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from None

    body = await request.body()
    if len(body) > _MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Request body too large")
    return body


async def _read_json_bounded(request: Request) -> Any:
    body = await _read_body_bounded(request)
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from None


@app.middleware("http")
async def webhook_rate_limit_middleware(request: Request, call_next):
    """Apply per-IP rate limits on webhook routes only."""
    path = request.url.path
    if path.startswith("/webhooks/"):
        client_ip = _client_ip(request)
        if not _allow_webhook_request(client_ip, path):
            log.warning("Webhook rate limit exceeded ip=%s path=%s", client_ip, path)
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded"},
            )
    return await call_next(request)


def enqueue_process_bridge_event(provider: str, payload: dict[str, Any]) -> str:
    """Push ``process_bridge_event`` to the ``batch_processing`` queue lane.

    Webhook-triggered events are background work — they use the
    batch lane so real-time API extractions aren't starved (§5.4).
    Isolated for tests via monkeypatch.
    """
    dedup = _dedup_key(provider, payload)
    if dedup and not _claim_dedup(dedup):
        log.info("Webhook dedup skip provider=%s key=%s", provider, dedup)
        return "dedup-skipped"

    q = get_priority_queue(0, _redis_client())
    job = enqueue_traced(
        q,
        process_bridge_event,
        kwargs={"provider": provider, "payload": payload},
        job_timeout="30m",
    )
    return job.id


@app.get("/webhooks/dropbox")
async def dropbox_challenge(challenge: str = Query(..., alias="challenge")):
    """Respond to Dropbox webhook verification challenge."""
    return Response(content=challenge, media_type="text/plain")


@app.post("/webhooks/dropbox")
async def dropbox_webhook(request: Request):
    """Receive Dropbox webhook notifications."""
    signature = request.headers.get("X-Dropbox-Signature")
    if not signature:
        raise HTTPException(status_code=403, detail="Missing X-Dropbox-Signature header")

    body = await _read_body_bounded(request)
    expected_signature = hmac.new(
        DROPBOX_APP_SECRET.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(signature, expected_signature):
        raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    list_folder = parsed.get("list_folder") if isinstance(parsed, dict) else None
    payload: dict[str, Any] = {"list_folder": list_folder or {}}
    job_id = enqueue_process_bridge_event("dropbox", payload)
    return {"status": "queued", "job_id": job_id}


@app.post("/webhooks/graph")
async def graph_webhook(
    request: Request,
    validationToken: str | None = Query(None),
):
    """Receive MS Graph webhook notifications and handle validation."""
    # Handle the validation token challenge from MS Graph
    if validationToken:
        return Response(content=validationToken, media_type="text/plain")

    payload = await _read_json_bounded(request)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Validate clientState (per-bridge secret) and resource URLs for security.
    for notification in payload.get("value", []):
        client_state = notification.get("clientState")
        subscription_id = notification.get("subscriptionId")
        if not client_state or not subscription_id:
            raise HTTPException(status_code=403, detail="Invalid clientState")

        # Resolve the per-bridge secret from the matching ACTIVE subscription row.
        # Fail closed: any DB error, missing/unknown subscription, or mismatch => 403.
        try:
            row = await _fetch_bridge_subscription("sharepoint", str(subscription_id))
        except Exception as exc:
            log.warning("Graph webhook subscription lookup failed: %s", exc)
            raise HTTPException(status_code=403, detail="Invalid clientState") from None

        expected = row["client_state"] if row else None
        if not expected or not hmac.compare_digest(client_state, expected):
            raise HTTPException(status_code=403, detail="Invalid clientState")

        resource = notification.get("resource", "")
        if resource:
            try:
                await validate_webhook_payload_url(resource, field_name="resource")
            except BridgeURLValidationError as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid resource URL in webhook payload: {e}",
                )

    enqueue_payload: dict[str, Any] = {"notifications": list(payload.get("value", []))}
    job_id = enqueue_process_bridge_event("sharepoint", enqueue_payload)
    return {"status": "queued", "job_id": job_id}


@app.post("/webhooks/dynamics365")
async def dynamics365_webhook(request: Request):
    """
    Receive Dataverse service endpoint webhook notifications.

    Validates ``x-ms-signaturecontent`` HMAC-SHA256 header, deduplicates
    repeated deliveries, and enqueues ``nce.tasks.process_d365_event`` to
    the ``high_priority`` RQ lane.  Returns 200 immediately — Dataverse
    requires a response within ~30 s or it retries.
    """
    if not cfg.NCE_D365_ENABLED:
        raise HTTPException(status_code=404, detail="D365 integration not enabled")

    signature = request.headers.get("x-ms-signaturecontent", "")
    body = await _read_body_bounded(request)

    from nce.vertical_modules.dynamics365.webhooks import D365WebhookValidator

    if not D365WebhookValidator.validate_signature(body, signature, _D365_WEBHOOK_SECRET):
        log.warning("D365 webhook invalid signature — rejecting")
        raise HTTPException(status_code=403, detail="Invalid D365 webhook signature")

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from None

    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")

    dedup = D365WebhookValidator.dedup_key(parsed)
    if dedup and not _claim_dedup(dedup):
        log.info("D365 webhook dedup skip key=%s", dedup)
        return {"status": "deduplicated"}

    # Enqueue to high-priority lane — CRM events are time-sensitive
    from nce.extractors.dispatch import get_priority_queue

    q = get_priority_queue(1, _redis_client())  # high_priority lane
    job = enqueue_traced(
        q,
        "nce.tasks.process_d365_event",
        kwargs={"payload": parsed},
        job_timeout="15m",
    )
    log.info(
        "D365 webhook queued entity=%s op=%s job=%s",
        parsed.get("PrimaryEntityName"),
        parsed.get("MessageName"),
        job.id,
    )
    return {"status": "queued", "job_id": job.id}


@app.post("/webhooks/drive")
async def drive_webhook(
    request: Request,
    channel_token: str | None = Header(None, alias="X-Goog-Channel-Token"),
    resource_state: str | None = Header(None, alias="X-Goog-Resource-State"),
    channel_id: str | None = Header(None, alias="X-Goog-Channel-Id"),
    resource_id: str | None = Header(None, alias="X-Goog-Resource-Id"),
    message_number: str | None = Header(None, alias="X-Goog-Message-Number"),
):
    """Receive Google Drive webhook notifications."""
    # Validate the per-bridge channel token against the matching ACTIVE watch
    # channel row first. Fail closed: missing token/channel id, unknown channel,
    # DB error, or mismatch => 403 (never fail open).
    if not channel_token or not channel_id:
        raise HTTPException(status_code=403, detail="Invalid or missing X-Goog-Channel-Token")

    try:
        row = await _fetch_bridge_subscription("gdrive", str(channel_id))
    except Exception as exc:
        log.warning("Drive webhook subscription lookup failed: %s", exc)
        raise HTTPException(
            status_code=403, detail="Invalid or missing X-Goog-Channel-Token"
        ) from None

    expected = row["client_state"] if row else None
    if not expected or not hmac.compare_digest(channel_token, expected):
        raise HTTPException(status_code=403, detail="Invalid or missing X-Goog-Channel-Token")

    if not resource_state:
        raise HTTPException(status_code=400, detail="Missing X-Goog-Resource-State")

    if resource_state == "sync":
        return {"status": "acknowledged", "reason": "sync_handshake"}

    enqueue_payload: dict[str, Any] = {
        "channel_id": channel_id or "",
        "resource_id": resource_id or "",
        "resource_state": resource_state,
        "message_number": message_number,
    }
    job_id = enqueue_process_bridge_event("gdrive", enqueue_payload)
    return {"status": "queued", "job_id": job_id}
