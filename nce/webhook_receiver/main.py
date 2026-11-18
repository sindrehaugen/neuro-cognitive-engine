from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
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

from nce import __version__ as _NCE_VERSION
from nce import bridge_repo
from nce.config import cfg
from nce.extractors.dispatch import get_priority_queue
from nce.health_probe import SingleflightReadinessCache, build_liveness_payload
from nce.http_body_limit import BoundedBodyMiddleware
from nce.net_safety import BridgeURLValidationError, validate_webhook_payload_url
from nce.observability import enqueue_traced
from nce.tasks import process_bridge_event

log = logging.getLogger("nce.webhook_receiver")

_FAST_PROBE_PATHS: frozenset[str] = frozenset(
    {"/healthz", "/health", "/readyz", "/ready", "/.well-known/agent-card"}
)

app = FastAPI(title="NCE Webhook Receiver")
app.add_middleware(BoundedBodyMiddleware)

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


_WEBHOOK_READINESS_CACHE = SingleflightReadinessCache(ttl_seconds=5.0)


@app.get("/health")
@app.get("/healthz")
async def health():
    """Fast in-memory liveness probe (< 3 ms, zero Redis/DB calls)."""
    payload, _ = build_liveness_payload("webhook-receiver", version=_NCE_VERSION)
    return payload


@app.get("/readyz")
async def readyz(response: Response):
    """Singleflight readiness probe cached for 5.0s verifying Redis connectivity."""

    async def _probe():
        reasons: list[dict[str, object]] = []
        try:
            await asyncio.to_thread(lambda: _redis_client().ping())
            redis_state = "up"
        except Exception:
            redis_state = "down"
            reasons.append({"reason": "redis_unreachable", "blocking": True})
        blocking = any(r.get("blocking") for r in reasons)
        return (
            {
                "status": "degraded" if reasons else "ok",
                "service": "webhook-receiver",
                "databases": {"redis": redis_state},
                "degraded_reasons": reasons,
            },
            503 if blocking else 200,
        )

    payload, status_code = await _WEBHOOK_READINESS_CACHE.get_or_probe(_probe)
    response.status_code = status_code
    return payload


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
    from nce._client_factory import make_sync_redis

    return make_sync_redis(cfg.REDIS_URL)


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
        if isinstance(pool, asyncpg.Pool):
            try:
                return await conn.fetchrow(
                    "SELECT * FROM nce_lookup_active_bridge_subscription($1, $2)",
                    provider,
                    subscription_id,
                )
            except asyncpg.UndefinedFunctionError:
                pass
        return await bridge_repo.fetch_active_subscription(
            conn, provider, subscription_id=subscription_id
        )


_INTERNAL_PROXY_NETS_V4: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("100.64.0.0/10"),
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("169.254.0.0/16"),
)
_INTERNAL_PROXY_NETS_V6: tuple[ipaddress.IPv6Network, ...] = (
    ipaddress.IPv6Network("::1/128"),
    ipaddress.IPv6Network("fc00::/7"),
    ipaddress.IPv6Network("fe80::/10"),
)
# Explicit proxy CIDRs (not ip.is_private, which treats RFC 5737 test/doc ranges as private).
_INTERNAL_PROXY_NETS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    *_INTERNAL_PROXY_NETS_V4,
    *_INTERNAL_PROXY_NETS_V6,
)


def _is_internal_proxy_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True when ip falls inside known loopback/RFC1918/CGNAT/link-local/ULA proxy nets."""
    if isinstance(ip, ipaddress.IPv4Address):
        return any(ip in net for net in _INTERNAL_PROXY_NETS_V4)
    if ip.ipv4_mapped is not None:
        return any(ip.ipv4_mapped in net for net in _INTERNAL_PROXY_NETS_V4)
    return any(ip in net for net in _INTERNAL_PROXY_NETS_V6)


def _client_ip(request: Request) -> str:
    """Client IP for rate limiting; select rightmost non-internal X-Forwarded-For hop."""
    if cfg.NCE_WEBHOOK_TRUST_PROXY:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            valid_ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
            for raw_hop in forwarded.split(","):
                candidate = raw_hop.strip()
                if not candidate:
                    continue
                try:
                    valid_ips.append(ipaddress.ip_address(candidate))
                except ValueError:
                    continue
            for ip in reversed(valid_ips):
                if not _is_internal_proxy_ip(ip):
                    return str(ip)
            if valid_ips:
                return str(valid_ips[-1])
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


_MAX_IP_WINDOWS = 10_000


def _allow_webhook_request_memory(client_ip: str, path: str) -> bool:
    """In-memory sliding window keyed by IP + path (single-instance fallback)."""
    now = time.time()
    key = f"{client_ip}:{path}"
    if key not in _ip_windows and len(_ip_windows) >= _MAX_IP_WINDOWS:
        _ip_windows.pop(next(iter(_ip_windows)), None)
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
        sig = str(payload.get("_dropbox_signature") or "")
        body_hash = str(payload.get("_body_sha256") or "")
        if not body_hash:
            body_hash = hashlib.sha256(
                json.dumps(sorted(accounts), sort_keys=True).encode("utf-8")
            ).hexdigest()[:16]
        raw = f"{json.dumps(sorted(accounts), sort_keys=True)}|{sig}|{body_hash}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
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
    if provider in ("manual", "oneflow", "criipto", "signicat"):
        session_id = str(payload.get("session_id") or "")
        event_status = str(payload.get("status") or payload.get("event") or "")
        if not session_id:
            return None
        raw = f"{provider}|{session_id}|{event_status}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        return f"nce:webhook:dedup:signing:{digest}"
    return None


def _claim_dedup(key: str, *, ttl_s: int | None = None) -> bool:
    """Return True when this delivery should be enqueued (first-seen within TTL)."""
    effective_ttl = ttl_s if ttl_s is not None else _DEDUP_TTL_S
    try:
        return bool(_redis_client().set(key, "1", nx=True, ex=effective_ttl))
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


class WebhookRateLimitMiddleware:
    """Pure-ASGI per-IP sliding-window rate limiter on webhook routes."""

    def __init__(self, app_inner: Any) -> None:
        self.app = app_inner

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path") or "/")
        if path in _FAST_PROBE_PATHS or not path.startswith("/webhooks/"):
            await self.app(scope, receive, send)
            return

        client_ip = _client_ip(Request(scope))
        if not _allow_webhook_request(client_ip, path):
            log.warning("Webhook rate limit exceeded ip=%s path=%s", client_ip, path)
            resp = JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded"},
            )
            await resp(scope, receive, send)
            return

        await self.app(scope, receive, send)


app.add_middleware(WebhookRateLimitMiddleware)


def enqueue_process_bridge_event(provider: str, payload: dict[str, Any]) -> str:
    """Push ``process_bridge_event`` to the ``batch_processing`` queue lane.

    Webhook-triggered events are background work — they use the
    batch lane so real-time API extractions aren't starved (§5.4).
    Isolated for tests via monkeypatch.
    """
    dedup = _dedup_key(provider, payload)
    if dedup:
        claim_ok = _claim_dedup(dedup, ttl_s=15) if provider == "dropbox" else _claim_dedup(dedup)
        if not claim_ok:
            log.info("Webhook dedup skip provider=%s key=%s", provider, dedup)
            return "dedup-skipped"

    clean_payload = {
        k: v for k, v in payload.items() if k not in ("_dropbox_signature", "_body_sha256")
    }
    q = get_priority_queue(0, _redis_client())
    job = enqueue_traced(
        q,
        process_bridge_event,
        kwargs={"provider": provider, "payload": clean_payload},
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
    payload: dict[str, Any] = {
        "list_folder": list_folder or {},
        "_dropbox_signature": signature,
        "_body_sha256": hashlib.sha256(body).hexdigest()[:16],
    }
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

    try:
        from nce_verticals.dynamics365.webhooks import D365WebhookValidator
    except ImportError as exc:
        raise HTTPException(status_code=501, detail="D365 vertical plugin not installed") from exc

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


@app.post("/webhooks/signing/{provider}")
async def signing_webhook(
    provider: str,
    request: Request,
):
    """Receive e-signature webhook notifications (Wave S-2a).

    Supports 'manual', 'oneflow', 'criipto', 'signicat'.
    Applies bounded-body reading, rate limiting, and deduplication.
    When status is 'signed', invokes do_on_signed_callback to freeze the
    quote baseline and trigger Project conversion.
    """
    valid_providers = ("manual", "oneflow", "criipto", "signicat")
    if provider not in valid_providers:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported signing provider '{provider}'. Must be one of {valid_providers}",
        )

    body_bytes = await _read_body_bounded(request)
    signing_secret = (
        os.getenv("NCE_SIGNING_WEBHOOK_SECRET", "")
        or getattr(cfg, "NCE_SIGNING_WEBHOOK_SECRET", "")
    ).strip()
    if signing_secret or cfg.IS_PROD:
        if not signing_secret:
            raise HTTPException(
                status_code=503,
                detail="Signing webhook secret not configured",
            )
        provided_secret = request.headers.get("X-NCE-Signing-Secret", "")
        provided_sig = request.headers.get("X-NCE-Signature", "")
        expected_sig = hmac.new(
            signing_secret.encode("utf-8"), body_bytes, hashlib.sha256
        ).hexdigest()
        secret_ok = bool(provided_secret) and hmac.compare_digest(provided_secret, signing_secret)
        sig_ok = bool(provided_sig) and hmac.compare_digest(provided_sig, expected_sig)
        if not (secret_ok or sig_ok):
            raise HTTPException(status_code=403, detail="Invalid signing webhook signature")

    try:
        payload = json.loads(body_bytes)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from None

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")

    session_id = payload.get("session_id")
    if not session_id or not str(session_id).strip():
        raise HTTPException(status_code=400, detail="Missing required 'session_id' in payload")

    session_id_str = str(session_id).strip()
    dedup = _dedup_key(provider, payload)
    if dedup and not _claim_dedup(dedup):
        log.info(
            "Signing webhook dedup skip provider=%s session=%s key=%s",
            provider,
            session_id_str,
            dedup,
        )
        return {"status": "deduplicated", "session_id": session_id_str}

    status = str(payload.get("status") or payload.get("event") or "").lower()

    # Unsigned / pending / unrecognized callbacks write no baseline and trigger no conversion
    if status != "signed":
        return {
            "status": "acknowledged",
            "provider": provider,
            "session_id": session_id_str,
            "event_status": status,
            "baseline_frozen": False,
        }

    pool = await _get_pg_pool()
    import types

    engine = getattr(request.app.state, "engine", None)
    if engine is None or not hasattr(engine, "pg_pool"):
        engine = types.SimpleNamespace(pg_pool=pool)

    # Resolve namespace_id
    ns_str = payload.get("namespace_id") or request.query_params.get("namespace_id")
    if not ns_str:
        async with pool.acquire(timeout=10.0) as conn:
            row = None
            if isinstance(pool, asyncpg.Pool):
                try:
                    row = await conn.fetchrow(
                        "SELECT nce_resolve_signing_session_namespace($1) AS namespace_id",
                        session_id_str,
                    )
                except asyncpg.UndefinedFunctionError:
                    row = None
            if row is None:
                row = await conn.fetchrow(
                    """
                    SELECT namespace_id
                    FROM sales_read_model
                    WHERE entity = 'quotes'
                      AND (manual->>'signing_session_id' = $1 OR source_json->>'signing_session_id' = $1)
                    LIMIT 1
                    """,
                    session_id_str,
                )
            if row and row.get("namespace_id"):
                ns_str = str(row["namespace_id"])

    if not ns_str:
        raise HTTPException(
            status_code=404,
            detail=f"Quote session '{session_id_str}' not found in read model",
        )

    try:
        from nce_verticals.sales.signing import do_on_signed_callback
    except ImportError as exc:
        raise HTTPException(status_code=501, detail="Sales vertical plugin not installed") from exc

    params = {
        "namespace_id": ns_str,
        "session_id": session_id_str,
        "callback_payload": payload.get("callback_payload") or payload,
    }

    try:
        res = await do_on_signed_callback(engine, params)
        return {"status": "processed", "provider": provider, **res}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
