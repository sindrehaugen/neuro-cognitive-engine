"""
nce/vertical_modules/dynamics365/webhooks.py
=============================================
Dataverse service endpoint webhook validation and payload parsing.

Dataverse sends a POST request with:
  ``x-ms-signaturecontent`` — HMAC-SHA256 of the request body, keyed with
  the webhook secret configured on the service endpoint.

Validation follows the same ``hmac.compare_digest`` pattern used for
Dropbox webhooks in ``nce/webhook_receiver/main.py``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

log = logging.getLogger("nce.vertical_modules.dynamics365.webhooks")


class D365WebhookValidator:
    """Static helpers for Dataverse webhook security and payload normalisation."""

    @staticmethod
    def validate_signature(
        body: bytes,
        signature_header: str,
        webhook_secret: str,
    ) -> bool:
        """
        Return ``True`` when the ``x-ms-signaturecontent`` HMAC-SHA256 matches.

        Dataverse computes ``HMAC-SHA256(body, secret)`` and sends the hex
        digest in the header.  We compute the same and use ``compare_digest``
        to prevent timing attacks.
        """
        if not webhook_secret:
            log.warning("D365 webhook secret is empty — rejecting all incoming requests")
            return False
        try:
            expected = hmac.new(webhook_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
            return hmac.compare_digest(expected, signature_header.strip())
        except Exception as exc:
            log.warning("D365 signature validation error: %s", exc)
            return False

    @staticmethod
    def extract_entity_context(payload: dict[str, Any]) -> dict[str, Any]:
        """
        Normalise a Dataverse webhook payload into a flat entity context dict.

        Dataverse payload shape::

            {
              "PrimaryEntityName": "incident",
              "PrimaryEntityId": "guid",
              "MessageName": "Create" | "Update" | "Delete",
              "InputParameters": [{"Key": "Target", "Value": {...}}],
              "OrganizationName": "...",
              "OrganizationId": "guid"
            }

        Returns::

            {
              "entity_type": str,
              "entity_id": str,
              "operation": str,      # "Create" | "Update" | "Delete"
              "org_id": str,
              "org_name": str,
              "changed_fields": list[str],
              "raw_target": dict | None,
            }
        """
        entity_type = (payload.get("PrimaryEntityName") or "").lower()
        entity_id = str(payload.get("PrimaryEntityId") or "")
        operation = str(payload.get("MessageName") or "")
        org_id = str(payload.get("OrganizationId") or "")
        org_name = str(payload.get("OrganizationName") or "")

        # Extract the Target input parameter (the entity record being affected)
        raw_target: dict[str, Any] | None = None
        input_params = payload.get("InputParameters") or []
        for param in input_params:
            if isinstance(param, dict) and param.get("Key") == "Target":
                raw_target = param.get("Value") or {}
                break

        changed_fields: list[str] = list(raw_target.keys()) if raw_target else []

        return {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "operation": operation,
            "org_id": org_id,
            "org_name": org_name,
            "changed_fields": changed_fields,
            "raw_target": raw_target,
        }

    @staticmethod
    def dedup_key(payload: dict[str, Any]) -> str | None:
        """
        Return a stable deduplication key for this payload, or ``None`` if the
        payload lacks the minimum fields for a reliable key.

        Format: ``nce:webhook:dedup:d365:{sha256_hex[:32]}``
        """
        entity_type = (payload.get("PrimaryEntityName") or "").lower()
        entity_id = str(payload.get("PrimaryEntityId") or "")
        operation = str(payload.get("MessageName") or "")
        org_id = str(payload.get("OrganizationId") or "")

        if not (entity_type and entity_id and operation):
            return None

        raw = f"{org_id}|{entity_type}|{entity_id}|{operation}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        return f"nce:webhook:dedup:d365:{digest}"
