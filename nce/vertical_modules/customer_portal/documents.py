"""nce/vertical_modules/customer_portal/documents.py
=================================================
Document share read projections (FDV, As-Built, SoW).
Charter Phase 3: Scoped, expiring document access under customer principal.

Enforces:
  1. Customer scope boundary (IDOR denial).
  2. Expiry and revocation gates (expired/revoked grants denied).
  3. Allow-list redaction (customer-redaction.json).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from nce.vertical_modules.customer_portal.auth import evaluate_customer_scope_access
from nce.vertical_modules.customer_portal.redaction import project_customer_safe


def _is_grant_valid(doc: dict[str, Any], now: datetime | None = None) -> bool:
    """Check whether a document share grant is active and unexpired."""
    if now is None:
        now = datetime.now(timezone.utc)

    # 1. Revocation check
    if doc.get("revoked_at"):
        return False

    # 2. Expiration check
    expires_at = doc.get("expires_at")
    if expires_at:
        try:
            if isinstance(expires_at, str):
                exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            elif isinstance(expires_at, datetime):
                exp_dt = expires_at
            else:
                return False

            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)

            if exp_dt <= now:
                return False
        except (ValueError, TypeError):
            return False

    return True


async def do_list_documents(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """List active, unexpired document shares granted to customer."""
    cust_scope = params.get("customer_scope_id")
    target_scope = params.get("target_scope_id", cust_scope)
    if not evaluate_customer_scope_access(cust_scope, target_scope):
        raise PermissionError(
            f"IDOR attempt: scope {cust_scope} denied access to scope {target_scope}"
        )

    raw_docs = params.get("documents", [])
    now = datetime.now(timezone.utc)

    valid_docs = [
        project_customer_safe("document_share", doc)
        for doc in raw_docs
        if _is_grant_valid(doc, now)
    ]

    return {
        "customer_scope_id": str(cust_scope),
        "total_documents": len(valid_docs),
        "documents": valid_docs,
    }


async def do_get_document(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Retrieve metadata and access ref for a single granted document share."""
    cust_scope = params.get("customer_scope_id")
    target_scope = params.get("target_scope_id", cust_scope)
    if not evaluate_customer_scope_access(cust_scope, target_scope):
        raise PermissionError(
            f"IDOR attempt: scope {cust_scope} denied access to scope {target_scope}"
        )

    share_id = params.get("share_id", "")
    doc = params.get("document", {})

    now = datetime.now(timezone.utc)

    if not _is_grant_valid(doc, now):
        raise PermissionError(f"Document share grant {share_id!r} has expired or was revoked")

    return project_customer_safe("document_share", doc)
