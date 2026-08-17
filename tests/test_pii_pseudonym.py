"""Hardening tests for PII pseudonymisation tokens (pii.py)."""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from nce.models import NamespacePIIConfig, PIIPolicy
from nce.pii import _pseudonym_token_suffix, process


def _cfg_pseudo(
    *, key: str | None = "namespace-pii-hmac-key-min-8b", **kwargs
) -> NamespacePIIConfig:
    base = {
        "entity_types": ["EMAIL"],
        "policy": PIIPolicy.pseudonymise,
        "reversible": False,
        "pseudonym_hmac_key": key,
    }
    base.update(kwargs)
    return NamespacePIIConfig(**base)


def test_pseudonym_suffix_is_base64url_22_chars():
    """Base64url encoding of 16-byte truncated HMAC-SHA256."""
    digest = _pseudonym_token_suffix(
        "EMAIL",
        "a@b.co",
        b"test-key-at-least-8-bytes-long",
    )
    # 16 bytes → ~22 base64url chars (without padding)
    assert 20 <= len(digest) <= 24
    # Only base64url characters (A-Z, a-z, 0-9, -, _)
    assert all(
        c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in digest
    )


def test_pseudonym_deterministic_same_inputs():
    k = b"same-key-material-for-hmac-test-32"
    a = _pseudonym_token_suffix("EMAIL", "u@example.com", k)
    b = _pseudonym_token_suffix("EMAIL", "u@example.com", k)
    assert a == b


def test_pseudonym_entity_type_separates_collision():
    k = b"same-key-material-for-hmac-test-32"
    v = "overlap-value"
    a = _pseudonym_token_suffix("TYPE_A", v, k)
    b = _pseudonym_token_suffix("TYPE_B", v, k)
    assert a != b


def test_pseudonym_per_namespace_key_changes_output():
    d1 = _pseudonym_token_suffix("EMAIL", "x@y.z", b"namespace-one-secret-key!!")
    d2 = _pseudonym_token_suffix("EMAIL", "x@y.z", b"namespace-two-secret-key!!")
    assert d1 != d2


def test_namespace_key_too_short_raises_at_validation():
    with pytest.raises(ValidationError, match="pseudonym_hmac_key"):
        NamespacePIIConfig(
            entity_types=["EMAIL"],
            policy=PIIPolicy.pseudonymise,
            reversible=False,
            pseudonym_hmac_key="short",
        )


@pytest.mark.asyncio
async def test_missing_master_and_no_namespace_key_raises(monkeypatch):
    monkeypatch.delenv("NCE_MASTER_KEY", raising=False)
    cfg = NamespacePIIConfig(
        entity_types=["EMAIL"],
        policy=PIIPolicy.pseudonymise,
        reversible=False,
        pseudonym_hmac_key=None,
    )
    text = "Contact user@mail.com please"
    # scan finds email; process will need key material
    with pytest.raises(ValueError, match="Pseudonymisation requires"):
        await process(text, cfg)


@pytest.mark.asyncio
async def test_process_pseudonym_uses_master_when_no_namespace_key(monkeypatch):
    monkeypatch.setenv("NCE_MASTER_KEY", "m" * 32)
    cfg = NamespacePIIConfig(
        entity_types=["EMAIL"],
        policy=PIIPolicy.pseudonymise,
        reversible=False,
        pseudonym_hmac_key=None,
    )
    text = "Contact alice@example.com"
    out = await process(text, cfg)
    assert out.redacted
    assert "alice@example.com" not in out.sanitized_text
    # Base64url token: 20–24 chars, no = padding, only url-safe chars
    m = re.search(r"<EMAIL_([A-Za-z0-9_-]{20,24})>", out.sanitized_text)
    assert m
    out2 = await process(text, cfg)
    assert out2.sanitized_text == out.sanitized_text


@pytest.mark.asyncio
async def test_process_pseudonym_with_explicit_namespace_key():
    cfg = _cfg_pseudo()
    text = "Reach me at bob@test.org"
    out = await process(text, cfg)
    assert out.redacted
    assert "bob@test.org" not in out.sanitized_text
    assert "<EMAIL_" in out.sanitized_text
    inner = out.sanitized_text.split("<EMAIL_")[1].split(">")[0]
    assert 20 <= len(inner) <= 24


@pytest.mark.asyncio
async def test_reversible_pseudonym_vault_token_matches_sanitized(monkeypatch):
    monkeypatch.setenv("NCE_MASTER_KEY", "z" * 32)
    cfg = NamespacePIIConfig(
        entity_types=["EMAIL"],
        policy=PIIPolicy.pseudonymise,
        reversible=True,
        pseudonym_hmac_key=None,
    )
    text = "Write to user@host.com today"
    out = await process(text, cfg)
    assert out.vault_entries
    tok = out.vault_entries[0]["token"]
    assert re.fullmatch(r"<EMAIL_[A-Za-z0-9_-]{20,24}>", tok)
    assert tok in out.sanitized_text
