"""Batch 1: overlap merging and span validation (nce/pii.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from nce.models import NamespacePIIConfig, PIIEntity, PIIPolicy
from nce.pii import _merge_overlapping_entities, process


def test_merge_overlapping_keeps_longer_span_at_same_start():
    email = PIIEntity(start=0, end=16, entity_type="EMAIL", value="alice@example.com", score=0.9)
    person = PIIEntity(start=0, end=4, entity_type="PERSON", value="alic", score=0.8)
    merged = _merge_overlapping_entities([person, email])
    assert len(merged) == 1
    assert merged[0].entity_type == "EMAIL"
    assert merged[0].start == 0
    assert merged[0].end == 16


@pytest.mark.asyncio
async def test_process_overlapping_spans_only_email_in_output():
    text = "alice@example.com"
    raw = [
        PIIEntity(
            start=0,
            end=len(text),
            entity_type="EMAIL",
            value=text,
            score=0.9,
        ),
        PIIEntity(start=0, end=4, entity_type="PERSON", value="alic", score=0.8),
    ]
    entities = _merge_overlapping_entities(raw)
    cfg = NamespacePIIConfig(entity_types=["EMAIL", "PERSON"], policy=PIIPolicy.redact)

    with patch("nce.pii.scan", new_callable=AsyncMock, return_value=entities):
        out = await process(text, cfg)

    assert "alic" not in out.sanitized_text
    assert "alice@example.com" not in out.sanitized_text
    assert out.sanitized_text == "<EMAIL>"


@pytest.mark.asyncio
async def test_process_adjacent_non_overlapping_spans_both_replaced():
    text = "aa bb"
    raw = [
        PIIEntity(start=0, end=2, entity_type="EMAIL", value="aa", score=0.9),
        PIIEntity(start=3, end=5, entity_type="PHONE", value="bb", score=0.9),
    ]
    entities = _merge_overlapping_entities(raw)
    cfg = NamespacePIIConfig(entity_types=["EMAIL", "PHONE"], policy=PIIPolicy.redact)

    with patch("nce.pii.scan", new_callable=AsyncMock, return_value=entities):
        out = await process(text, cfg)

    assert out.sanitized_text == "<EMAIL> <PHONE>"


@pytest.mark.asyncio
async def test_process_negative_start_clears_raw_values_and_raises():
    text = "hello"
    entities = [
        PIIEntity(start=-1, end=3, entity_type="EMAIL", value="hel", score=0.9),
    ]
    cfg = NamespacePIIConfig(entity_types=["EMAIL"], policy=PIIPolicy.redact)

    with patch("nce.pii.scan", new_callable=AsyncMock, return_value=entities):
        with pytest.raises(ValueError, match="Invalid entity span"):
            await process(text, cfg)

    assert entities[0].value == "[REDACTED]"


@pytest.mark.asyncio
async def test_process_end_beyond_text_clears_raw_values_and_raises():
    text = "hi"
    entities = [
        PIIEntity(start=0, end=10, entity_type="EMAIL", value="hi", score=0.9),
    ]
    cfg = NamespacePIIConfig(entity_types=["EMAIL"], policy=PIIPolicy.redact)

    with patch("nce.pii.scan", new_callable=AsyncMock, return_value=entities):
        with pytest.raises(ValueError, match="Invalid entity span"):
            await process(text, cfg)

    assert entities[0].value == "[REDACTED]"
