"""
nce/outbound_webhooks/service.py

C4 Outbound Webhooks (Wave A-7)
Service operations, selector matching, HMAC request signing, and delivery logic.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.parse
import uuid
from collections.abc import Sequence
from typing import Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
import httpx

from nce.outbound_webhooks.models import OutboundWebhook

log = logging.getLogger("nce.outbound_webhooks")


def matches_selector(selectors: Sequence[str], event_type: str) -> bool:
    """Check whether *event_type* matches any pattern in *selectors*.

    Supported patterns:
    - ``"*"`` matches everything.
    - ``"prefix.*"`` matches any event starting with ``"prefix."``.
    - exact string match (e.g. ``"memory.stored"``).
    """
    for sel in selectors:
        sel = sel.strip()
        if not sel:
            continue
        if sel == "*":
            return True
        if sel.endswith(".*"):
            prefix = sel[:-1]  # Keep trailing dot, e.g. "memory."
            if event_type.startswith(prefix):
                return True
        elif sel == event_type:
            return True
    return False


def compute_webhook_signature(
    secret: str,
    method: str,
    path: str,
    timestamp: int,
    body_bytes: bytes,
) -> str:
    """Compute HMAC-SHA256 signature matching nce/auth.py canonical format.

    canonical_message = METHOD\\nPATH\\nTIMESTAMP[\\nSHA256_HEX(raw_body)]
    signature = HMAC-SHA256(secret, canonical_message).hexdigest()
    """
    parts = [method.upper(), path, str(timestamp)]
    if body_bytes:
        parts.append(hashlib.sha256(body_bytes).hexdigest())
    canonical = "\n".join(parts)
    return hmac.new(
        secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def sign_outbound_webhook(
    secret: str,
    method: str,
    url: str,
    body_bytes: bytes,
    *,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """Generate headers with the three-header HMAC-SHA256 scheme for outbound webhooks.

    Headers:
      - X-NCE-Timestamp: <unix_epoch_seconds>
      - X-NCE-Nonce: <uuid4_hex>
      - Authorization: HMAC-SHA256 <hex_signature>
      - Content-Type: application/json
    """
    if timestamp is None:
        timestamp = int(time.time())
    if nonce is None:
        nonce = uuid.uuid4().hex

    parsed = urllib.parse.urlparse(url)
    path = parsed.path or "/"

    sig = compute_webhook_signature(secret, method, path, timestamp, body_bytes)
    return {
        "Content-Type": "application/json",
        "X-NCE-Timestamp": str(timestamp),
        "X-NCE-Nonce": str(nonce),
        "Authorization": f"HMAC-SHA256 {sig}",
    }


def prepare_webhook_payload(event: dict[str, Any]) -> dict[str, Any]:
    """Prepare a standardized event payload dictionary from an outbox event record."""
    raw_payload = event.get("payload")
    if isinstance(raw_payload, str):
        try:
            raw_payload = json.loads(raw_payload)
        except Exception:
            pass

    raw_headers = event.get("headers")
    if isinstance(raw_headers, str):
        try:
            raw_headers = json.loads(raw_headers)
        except Exception:
            pass

    created_at = event.get("created_at")
    if hasattr(created_at, "isoformat"):
        created_at_str = created_at.isoformat()
    else:
        created_at_str = str(created_at) if created_at else None

    return {
        "event_id": str(event["id"]),
        "namespace_id": str(event["namespace_id"]),
        "event_type": event["event_type"],
        "aggregate_type": event.get("aggregate_type"),
        "aggregate_id": str(event.get("aggregate_id")) if event.get("aggregate_id") else None,
        "payload": raw_payload or {},
        "headers": raw_headers or {},
        "created_at": created_at_str,
    }


def send_webhook_http_sync(
    url: str,
    secret: str,
    payload_dict: dict[str, Any],
    timeout: float = 5.0,
) -> bool:
    """Synchronously deliver a webhook payload via HTTP POST.

    Designed for invocation inside zero-arg PostCommitAction callables outside
    the PostgreSQL transaction block.
    """
    body_bytes = json.dumps(payload_dict, default=str).encode("utf-8")
    headers = sign_outbound_webhook(secret, "POST", url, body_bytes)
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, headers=headers, content=body_bytes)
            if resp.status_code >= 400:
                log.warning(
                    "[outbound_webhook] delivery to %s returned HTTP %d",
                    url,
                    resp.status_code,
                )
                return False
            return True
    except Exception as exc:
        log.warning("[outbound_webhook] delivery to %s failed: %s", url, exc)
        return False


def _row_to_webhook(row: Any) -> OutboundWebhook:
    selectors_val = row["selectors"]
    if isinstance(selectors_val, list):
        selectors = tuple(selectors_val)
    elif isinstance(selectors_val, tuple):
        selectors = selectors_val
    else:
        selectors = ()

    return OutboundWebhook(
        id=row["id"],
        namespace_id=row["namespace_id"],
        url=row["url"],
        secret=row["secret"],
        selectors=selectors,
        is_active=bool(row["is_active"]),
        description=row["description"] or "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _coerce_uuid(val: UUID | str) -> UUID:
    if isinstance(val, UUID):
        return val
    return UUID(str(val).strip())


async def create_webhook(
    conn: asyncpg.Connection,
    namespace_id: UUID | str,
    url: str,
    secret: str,
    selectors: Sequence[str] | None = None,
    is_active: bool = True,
    description: str = "",
) -> OutboundWebhook:
    """Create a new outbound webhook subscription."""
    ns_id = _coerce_uuid(namespace_id)
    if not url or not url.strip():
        raise ValueError("Webhook URL cannot be empty")
    if not secret or not secret.strip():
        raise ValueError("Webhook HMAC secret cannot be empty")

    selectors_list = list(selectors) if selectors is not None else ["*"]

    row = await conn.fetchrow(
        """
        INSERT INTO outbound_webhooks (
            namespace_id, url, secret, selectors, is_active, description
        )
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id, namespace_id, url, secret, selectors, is_active, description, created_at, updated_at
        """,
        ns_id,
        url.strip(),
        secret.strip(),
        selectors_list,
        is_active,
        description.strip(),
    )
    return _row_to_webhook(row)


async def list_webhooks(
    conn: asyncpg.Connection,
    namespace_id: UUID | str,
    is_active: bool | None = None,
) -> list[OutboundWebhook]:
    """List outbound webhooks for a given namespace."""
    ns_id = _coerce_uuid(namespace_id)
    if is_active is None:
        rows = await conn.fetch(
            """
            SELECT id, namespace_id, url, secret, selectors, is_active, description, created_at, updated_at
            FROM outbound_webhooks
            WHERE namespace_id = $1
            ORDER BY created_at DESC
            """,
            ns_id,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT id, namespace_id, url, secret, selectors, is_active, description, created_at, updated_at
            FROM outbound_webhooks
            WHERE namespace_id = $1 AND is_active = $2
            ORDER BY created_at DESC
            """,
            ns_id,
            is_active,
        )
    return [_row_to_webhook(r) for r in rows]


async def get_webhook(
    conn: asyncpg.Connection,
    namespace_id: UUID | str,
    webhook_id: UUID | str,
) -> OutboundWebhook | None:
    """Fetch a single webhook by ID within the tenant namespace."""
    ns_id = _coerce_uuid(namespace_id)
    wh_id = _coerce_uuid(webhook_id)
    row = await conn.fetchrow(
        """
        SELECT id, namespace_id, url, secret, selectors, is_active, description, created_at, updated_at
        FROM outbound_webhooks
        WHERE namespace_id = $1 AND id = $2
        """,
        ns_id,
        wh_id,
    )
    return _row_to_webhook(row) if row else None


async def delete_webhook(
    conn: asyncpg.Connection,
    namespace_id: UUID | str,
    webhook_id: UUID | str,
) -> bool:
    """Delete an outbound webhook by ID within the tenant namespace."""
    ns_id = _coerce_uuid(namespace_id)
    wh_id = _coerce_uuid(webhook_id)
    deleted_id = await conn.fetchval(
        """
        DELETE FROM outbound_webhooks
        WHERE namespace_id = $1 AND id = $2
        RETURNING id
        """,
        ns_id,
        wh_id,
    )
    return deleted_id is not None


async def get_matching_webhooks(
    conn: asyncpg.Connection,
    namespace_id: UUID | str,
    event_type: str,
) -> list[OutboundWebhook]:
    """Return all active webhooks for namespace_id whose selectors match event_type."""
    ns_id = _coerce_uuid(namespace_id)
    rows = await conn.fetch(
        """
        SELECT id, namespace_id, url, secret, selectors, is_active, description, created_at, updated_at
        FROM outbound_webhooks
        WHERE namespace_id = $1 AND is_active = TRUE
        """,
        ns_id,
    )
    matching: list[OutboundWebhook] = []
    for r in rows:
        wh = _row_to_webhook(r)
        if matches_selector(wh.selectors, event_type):
            matching.append(wh)
    return matching
