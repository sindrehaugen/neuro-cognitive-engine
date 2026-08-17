"""Reusable canonical ``append_event(..., params=...)`` payloads for unit tests."""

from __future__ import annotations

import uuid


def minimal_store_memory_params(**extra: object) -> dict[str, object]:
    """Minimal valid ``store_memory`` params under ``EVENT_REQUIRED_PARAM_KEYS``."""
    base: dict[str, object] = {
        "saga_id": str(uuid.uuid4()),
        "memory_id": str(uuid.uuid4()),
        "payload_ref": "507f1f77bcf86cd799439011",
        "assertion_type": "fact",
        "entities": [],
        "triplets": [],
    }
    base.update(extra)
    return base
