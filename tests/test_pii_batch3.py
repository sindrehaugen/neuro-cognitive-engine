"""Batch 3: AnalyzerEngine cache and case-insensitive allowlist."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from nce import pii as pii_mod
from nce.models import NamespacePIIConfig
from nce.pii import _get_analyzer, _scan_sync


@pytest.fixture(autouse=True)
def reset_analyzer_cache():
    prev = pii_mod._ANALYZER
    pii_mod._ANALYZER = None
    yield
    pii_mod._ANALYZER = prev


@pytest.fixture
def fake_presidio():
    mod = MagicMock()
    engine_cls = MagicMock()
    mod.AnalyzerEngine = engine_cls
    with patch.dict(sys.modules, {"presidio_analyzer": mod}):
        yield mod, engine_cls


def test_get_analyzer_returns_same_cached_instance(fake_presidio):
    _mod, engine_cls = fake_presidio
    sentinel = object()
    engine_cls.return_value = sentinel
    a = _get_analyzer()
    b = _get_analyzer()
    assert a is b is sentinel
    engine_cls.assert_called_once()


def test_scan_sync_allowlist_alice_blocks_lowercase_in_text(fake_presidio):
    _mod, engine_cls = fake_presidio
    text = "Contact alice today"
    cfg = NamespacePIIConfig(
        entity_types=["PERSON"],
        allowlist=["Alice"],
    )
    mock_engine = MagicMock()
    mock_engine.analyze.return_value = [
        MagicMock(start=8, end=13, entity_type="PERSON", score=0.9),
    ]
    engine_cls.return_value = mock_engine
    entities = _scan_sync(text, cfg)
    assert entities == []


def test_scan_sync_allowlist_lowercase_blocks_capitalized_in_text(fake_presidio):
    _mod, engine_cls = fake_presidio
    text = "Reach Alice@example.com please"
    cfg = NamespacePIIConfig(
        entity_types=["EMAIL"],
        allowlist=["alice@example.com"],
    )
    start = text.index("Alice@example.com")
    end = start + len("Alice@example.com")
    mock_engine = MagicMock()
    mock_engine.analyze.return_value = [
        MagicMock(start=start, end=end, entity_type="EMAIL", score=0.9),
    ]
    engine_cls.return_value = mock_engine
    entities = _scan_sync(text, cfg)
    assert entities == []


def test_scan_sync_analyzer_engine_constructed_once(fake_presidio):
    _mod, engine_cls = fake_presidio
    mock_engine = MagicMock()
    mock_engine.analyze.return_value = []
    engine_cls.return_value = mock_engine
    cfg = NamespacePIIConfig(entity_types=["EMAIL"])
    _scan_sync("a@b.co", cfg)
    _scan_sync("c@d.co", cfg)
    engine_cls.assert_called_once()
