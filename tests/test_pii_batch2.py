"""Batch 2: size limits, entity cap, namespace key isolation."""

from __future__ import annotations

import builtins
from unittest.mock import patch

import pytest

from nce.models import NamespacePIIConfig, PIIEntity, PIIPolicy
from nce.pii import (
    _MAX_ENTITIES,
    _MAX_TEXT_BYTES,
    _pseudonym_hmac_key_material,
    _pseudonym_token_suffix,
    _scan_sync,
    process,
)


def test_scan_sync_text_over_max_bytes_raises():
    text = "x" * (_MAX_TEXT_BYTES + 1)
    cfg = NamespacePIIConfig(entity_types=["EMAIL"])
    with pytest.raises(ValueError, match="text exceeds maximum size"):
        _scan_sync(text, cfg)


def test_scan_sync_entity_cap_clears_values_and_raises():
    cfg = NamespacePIIConfig(entity_types=["EMAIL"])
    text = " ".join(f"u{i}@x.co" for i in range(_MAX_ENTITIES + 1))
    real_import = builtins.__import__
    cleared: list[int] = []
    orig_clear = PIIEntity.clear_raw_value

    def track_clear(self: PIIEntity) -> None:
        cleared.append(1)
        orig_clear(self)

    def block_presidio(name: str, *args, **kwargs):
        if name == "presidio_analyzer":
            raise ImportError("forced for test")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=block_presidio):
        with patch.object(PIIEntity, "clear_raw_value", track_clear):
            with pytest.raises(ValueError, match="exceeding the limit"):
                _scan_sync(text, cfg)

    assert len(cleared) >= _MAX_ENTITIES + 1


def test_master_key_fallback_differs_by_namespace_id(monkeypatch):
    monkeypatch.setenv("NCE_MASTER_KEY", "m" * 32)
    cfg_a = NamespacePIIConfig(pseudonym_hmac_key=None, namespace_id="namespace-a")
    cfg_b = NamespacePIIConfig(pseudonym_hmac_key=None, namespace_id="namespace-b")
    key_a = _pseudonym_hmac_key_material(cfg_a, namespace_id=cfg_a.namespace_id)
    key_b = _pseudonym_hmac_key_material(cfg_b, namespace_id=cfg_b.namespace_id)
    tok_a = _pseudonym_token_suffix("EMAIL", "user@example.com", key_a)
    tok_b = _pseudonym_token_suffix("EMAIL", "user@example.com", key_b)
    assert tok_a != tok_b


def test_same_namespace_master_key_fallback_is_deterministic(monkeypatch):
    monkeypatch.setenv("NCE_MASTER_KEY", "m" * 32)
    cfg = NamespacePIIConfig(pseudonym_hmac_key=None, namespace_id="ns-stable")
    key = _pseudonym_hmac_key_material(cfg, namespace_id=cfg.namespace_id)
    a = _pseudonym_token_suffix("EMAIL", "user@example.com", key)
    b = _pseudonym_token_suffix("EMAIL", "user@example.com", key)
    assert a == b


@pytest.mark.asyncio
async def test_process_same_namespace_identical_pseudonyms(monkeypatch):
    monkeypatch.setenv("NCE_MASTER_KEY", "m" * 32)
    cfg = NamespacePIIConfig(
        entity_types=["EMAIL"],
        policy=PIIPolicy.pseudonymise,
        reversible=False,
        pseudonym_hmac_key=None,
        namespace_id="ns-1",
    )
    text = "Contact user@host.com today"
    out1 = await process(text, cfg)
    out2 = await process(text, cfg)
    assert out1.sanitized_text == out2.sanitized_text
