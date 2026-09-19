"""
nce/outbound_webhooks/__init__.py

C4 Outbound Webhooks (Wave A-7)
Public interface for outbound webhook models, services, and HMAC signing.
"""

from __future__ import annotations

from nce.outbound_webhooks.models import OutboundWebhook
from nce.outbound_webhooks.service import (
    compute_webhook_signature,
    create_webhook,
    delete_webhook,
    get_matching_webhooks,
    get_webhook,
    list_webhooks,
    matches_selector,
    prepare_webhook_payload,
    send_webhook_http_sync,
    sign_outbound_webhook,
)

__all__ = [
    "OutboundWebhook",
    "compute_webhook_signature",
    "create_webhook",
    "delete_webhook",
    "get_matching_webhooks",
    "get_webhook",
    "list_webhooks",
    "matches_selector",
    "prepare_webhook_payload",
    "send_webhook_http_sync",
    "sign_outbound_webhook",
]
