"""
HTTP mock helpers aligned with nce.auth.HMACAuthMiddleware.

Provides deterministic request signing so tests and trivial mock ASGI mounts
mirror production admin API auth without a real network boundary.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import uuid
from typing import Any


def canonical_hmac_payload(
    method: str,
    path: str,
    timestamp: int,
    body: bytes | None = None,
    nonce: str = "",
) -> str:
    """Build the newline-delimited canonical string that is MAC'd."""
    parts = [method.upper(), path, str(timestamp), nonce]
    if body:
        parts.append(hashlib.sha256(body).hexdigest())
    return "\n".join(parts)


def compute_admin_hmac(hex_key_material: str, canonical: str) -> str:
    """HMAC-SHA256 hex digest; hex_key_material decoded as UTF-8 bytes."""
    raw = hex_key_material.encode("utf-8")
    return _hmac.new(raw, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def admin_hmac_headers(
    *,
    hex_key_material: str,
    method: str,
    path: str,
    body: bytes = b"",
    timestamp: int,
    nonce: str | None = None,
) -> dict[str, str]:
    """
    Return headers dict suitable for Starlette TestClient / httpx.

    ``hex_key_material`` is the ASCII secret — it is UTF-8 encoded as the MAC
    key (same semantics as cfg.NCE_API_KEY env string).
    """
    eff_nonce = uuid.uuid4().hex if nonce is None else nonce
    canonical = canonical_hmac_payload(method, path, timestamp, body or None, nonce=eff_nonce)
    sig = compute_admin_hmac(hex_key_material, canonical)
    return {
        "X-NCE-Timestamp": str(timestamp),
        "X-NCE-Nonce": eff_nonce,
        "Authorization": f"HMAC-SHA256 {sig}",
    }


def dummy_jsonrpc_mount_response() -> dict[str, Any]:
    """Tiny JSON body for mock POST endpoints in transport-level tests."""
    return {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
