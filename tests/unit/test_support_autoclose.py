"""
tests/unit/test_support_autoclose.py
====================================
Unit tests for Module 10 Support Engine Autoclose Confidence Gate.
Validates Charter 6 & Spec 9.5:
  - Auto-close is the sharpest conservative-posture instance.
  - A sub-confidence autonomous resolution MUST be refused.
  - Proved RED before GREEN.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from nce.vertical_modules.support.tickets import (
    AutocloseConfidenceRefusalError,
    do_resolve_ticket,
)

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_TICKET_ID = str(uuid4())


@pytest.mark.asyncio
async def test_autoclose_refused_when_confidence_below_threshold():
    """Autonomous resolution below confidence threshold (e.g. 0.85 < 0.95) MUST be refused."""
    pool = MagicMock()
    params = {
        "namespace_id": _NAMESPACE_ID,
        "ticket_id": _TICKET_ID,
        "resolution_text": "Known trivial reboot",
        "autonomous": True,
        "confidence": 0.85,  # Below default 0.95
    }

    with pytest.raises(AutocloseConfidenceRefusalError) as exc_info:
        await do_resolve_ticket(pool, params)

    assert exc_info.value.confidence == 0.85
    assert exc_info.value.threshold == 0.95
    assert "Autoclose refused" in str(exc_info.value)


@pytest.mark.asyncio
async def test_autoclose_refused_when_confidence_missing_defaults_to_zero():
    """Autonomous resolution without confidence defaults to 0.0 and is refused."""
    pool = MagicMock()
    params = {
        "namespace_id": _NAMESPACE_ID,
        "ticket_id": _TICKET_ID,
        "resolution_text": "Autonomous resolution with no confidence",
        "autonomous": True,
    }

    with pytest.raises(AutocloseConfidenceRefusalError) as exc_info:
        await do_resolve_ticket(pool, params)

    assert exc_info.value.confidence == 0.0
    assert exc_info.value.threshold == 0.95


@pytest.mark.asyncio
async def test_autoclose_refused_custom_threshold():
    """Autonomous resolution respects custom autoclose_confidence threshold parameter."""
    pool = MagicMock()
    params = {
        "namespace_id": _NAMESPACE_ID,
        "ticket_id": _TICKET_ID,
        "resolution_text": "Custom threshold test",
        "autonomous": True,
        "confidence": 0.96,
        "autoclose_confidence": 0.99,  # Higher threshold
    }

    with pytest.raises(AutocloseConfidenceRefusalError) as exc_info:
        await do_resolve_ticket(pool, params)

    assert exc_info.value.confidence == 0.96
    assert exc_info.value.threshold == 0.99
