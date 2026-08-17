"""Batch 4: O(n) redaction assembly and Luhn credit-card filtering."""

from __future__ import annotations

import builtins
from unittest.mock import AsyncMock, patch

import pytest

from nce.models import NamespacePIIConfig, PIIEntity, PIIPolicy
from nce.pii import _luhn_valid, _merge_overlapping_entities, _scan_sync, process


def test_luhn_valid_accepts_known_card():
    assert _luhn_valid("4532015112830366")


def test_luhn_invalid_sequence_rejected():
    assert not _luhn_valid("1234567890123")


def test_scan_sync_invalid_luhn_not_flagged_as_credit_card():
    cfg = NamespacePIIConfig(entity_types=["CREDIT_CARD"])
    text = "card 1234567890123 end"
    real_import = builtins.__import__

    def block_presidio(name: str, *args, **kwargs):
        if name == "presidio_analyzer":
            raise ImportError("forced for test")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=block_presidio):
        entities = _scan_sync(text, cfg)
    assert entities == []


def test_scan_sync_valid_luhn_flagged_as_credit_card():
    cfg = NamespacePIIConfig(entity_types=["CREDIT_CARD"])
    card = "4532015112830366"
    text = f"pay {card} now"
    real_import = builtins.__import__

    def block_presidio(name: str, *args, **kwargs):
        if name == "presidio_analyzer":
            raise ImportError("forced for test")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=block_presidio):
        entities = _scan_sync(text, cfg)
    assert len(entities) == 1
    assert entities[0].entity_type == "CREDIT_CARD"
    assert card in entities[0].value


def _naive_redact(text: str, entities: list[PIIEntity]) -> str:
    out = text
    for entity in sorted(entities, key=lambda e: e.start, reverse=True):
        out = out[: entity.start] + entity.token + out[entity.end :]
    return out


@pytest.mark.asyncio
async def test_process_three_entity_redaction_matches_naive_slicing():
    text = "aa@b.co and +1-800-555-0199 and 4532015112830366"
    raw = [
        PIIEntity(
            start=text.index("aa@b.co"),
            end=text.index("aa@b.co") + len("aa@b.co"),
            entity_type="EMAIL",
            value="aa@b.co",
            score=0.9,
        ),
        PIIEntity(
            start=text.index("+1-800-555-0199"),
            end=text.index("+1-800-555-0199") + len("+1-800-555-0199"),
            entity_type="PHONE",
            value="+1-800-555-0199",
            score=0.9,
        ),
        PIIEntity(
            start=text.index("4532015112830366"),
            end=text.index("4532015112830366") + len("4532015112830366"),
            entity_type="CREDIT_CARD",
            value="4532015112830366",
            score=0.9,
        ),
    ]
    entities = _merge_overlapping_entities(raw)
    for e in entities:
        e.token = f"<{e.entity_type}>"
    cfg = NamespacePIIConfig(
        entity_types=["EMAIL", "PHONE", "CREDIT_CARD"],
        policy=PIIPolicy.redact,
    )

    with patch("nce.pii.scan", new_callable=AsyncMock, return_value=entities):
        out = await process(text, cfg)

    expected = _naive_redact(text, entities)
    assert out.sanitized_text == expected


@pytest.mark.asyncio
async def test_process_many_entities_redacts_correctly():
    emails = [f"u{i}@x.co" for i in range(500)]
    text = " ".join(emails)
    raw = [
        PIIEntity(
            start=text.index(addr),
            end=text.index(addr) + len(addr),
            entity_type="EMAIL",
            value=addr,
            score=0.9,
        )
        for addr in emails
    ]
    entities = _merge_overlapping_entities(raw)
    for e in entities:
        e.token = "<EMAIL>"
    cfg = NamespacePIIConfig(entity_types=["EMAIL"], policy=PIIPolicy.redact)

    with patch("nce.pii.scan", new_callable=AsyncMock, return_value=entities):
        out = await process(text, cfg)

    assert out.sanitized_text == _naive_redact(text, entities)
    for addr in emails:
        assert addr not in out.sanitized_text


@pytest.mark.asyncio
async def test_norwegian_locale_fodselsnummer_redacted():
    cfg = NamespacePIIConfig(
        entity_types=["NO_FODSELSNUMMER"],
        policy=PIIPolicy.redact,
        locale="no",
    )
    text = "My birth number is 12129012348."
    # With locale="no", it should be detected and redacted
    result = await process(text, cfg)
    assert "12129012348" not in result.sanitized_text
    assert "<NO_FODSELSNUMMER>" in result.sanitized_text

    # With locale="en" or default, it should NOT be detected
    cfg_en = NamespacePIIConfig(
        entity_types=["NO_FODSELSNUMMER"],
        policy=PIIPolicy.redact,
        locale="en",
    )
    result_en = await process(text, cfg_en)
    assert "12129012348" in result_en.sanitized_text


@pytest.mark.asyncio
async def test_norwegian_locale_invalid_fodselsnummer_not_redacted():
    cfg = NamespacePIIConfig(
        entity_types=["NO_FODSELSNUMMER"],
        policy=PIIPolicy.redact,
        locale="no",
    )
    text = "My birth number is 12129012347."  # invalid check digits
    result = await process(text, cfg)
    assert "12129012347" in result.sanitized_text
    assert "<NO_FODSELSNUMMER>" not in result.sanitized_text


@pytest.mark.asyncio
async def test_norwegian_locale_org_number_redacted():
    cfg = NamespacePIIConfig(
        entity_types=["NO_ORG_NUMBER"],
        policy=PIIPolicy.redact,
        locale="no",
    )
    text = "Our organization number is 999999999."
    result = await process(text, cfg)
    assert "999999999" not in result.sanitized_text
    assert "<NO_ORG_NUMBER>" in result.sanitized_text


@pytest.mark.asyncio
async def test_norwegian_locale_mobile_number_redacted():
    cfg = NamespacePIIConfig(
        entity_types=["NO_PHONE_MOBILE"],
        policy=PIIPolicy.redact,
        locale="no",
    )
    text = "Call me at +4798765432 or 49123456."
    result = await process(text, cfg)
    assert "+4798765432" not in result.sanitized_text
    assert "49123456" not in result.sanitized_text
    assert "<NO_PHONE_MOBILE>" in result.sanitized_text
