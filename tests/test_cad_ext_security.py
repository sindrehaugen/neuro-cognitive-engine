"""CAD extractor entity iteration caps."""

from __future__ import annotations

from unittest.mock import MagicMock

from nce.extractors import cad_ext


def _text_entity():
    ent = MagicMock()
    ent.dxftype.return_value = "TEXT"
    ent.dxf.text = "line"
    return ent


def test_dxf_entity_cap_emits_warning(monkeypatch):
    monkeypatch.setattr(cad_ext, "_MAX_CAD_TEXT_ENTITIES", 3)

    msp = [_text_entity() for _ in range(8)]
    warnings: list[str] = []
    lines = cad_ext._collect_entity_texts(msp, warnings)

    assert len(lines) == 3
    assert any("cad_entity_limit" in w for w in warnings)
