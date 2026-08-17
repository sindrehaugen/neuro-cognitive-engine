"""Drift gate: ``docs/API.md`` must match ``scripts/gen_api_docs.py`` output.

Runs in the standard pytest gate, so any merge that changes a REST route or an MCP
tool without regenerating the API docs fails CI — keeping API documentation current
on every merge. Regenerate with: ``python scripts/gen_api_docs.py``.
"""

from __future__ import annotations

import importlib.util
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_generator():
    path = _ROOT / "scripts" / "gen_api_docs.py"
    spec = importlib.util.spec_from_file_location("gen_api_docs", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_api_docs_are_current():
    gen = _load_generator()
    expected = gen.generate().strip()
    doc = _ROOT / "docs" / "API.md"
    actual = doc.read_text(encoding="utf-8").strip() if doc.exists() else ""
    assert actual == expected, (
        "docs/API.md is out of date with the REST routes / MCP tool registry. "
        "Regenerate with: python scripts/gen_api_docs.py"
    )
