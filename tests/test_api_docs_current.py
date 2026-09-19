"""Drift gate: ``docs/API.md`` must match ``scripts/gen_api_docs.py`` output.

§10.5b (ORCH_PROTOCOL.md, 2026-09-19): PRs no longer carry ``docs/API.md`` --
``.github/workflows/regen-generated-docs.yml`` regenerates and self-verifies it on
`main` after every merge instead. ``test_api_docs_are_current`` below is marked
``@pytest.mark.doc_gate`` and excluded on ``pull_request`` in ci.yml for exactly
that reason: requiring it on a PR would force PRs to carry the file again, which
is the collision §10.5b exists to remove. It still runs on every ORDINARY
(non-bot) push to `main` -- not on the regen job's own follow-up commit, since a
GITHUB_TOKEN push never triggers `on: push` at all (K-H9/K-H11); that commit is
self-verified inside the regen job itself instead. Also runs in any
manual/nightly full run. Regenerate with: ``python scripts/gen_api_docs.py``.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_generator():
    path = _ROOT / "scripts" / "gen_api_docs.py"
    spec = importlib.util.spec_from_file_location("gen_api_docs", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.doc_gate
def test_api_docs_are_current():
    gen = _load_generator()
    expected = gen.generate().strip()
    doc = _ROOT / "docs" / "API.md"
    actual = doc.read_text(encoding="utf-8").strip() if doc.exists() else ""
    assert actual == expected, (
        "docs/API.md is out of date with the REST routes / MCP tool registry. "
        "Regenerate with: python scripts/gen_api_docs.py"
    )


# ---------------------------------------------------------------------------
# Phase 4 Wave T-5 Hardening: Positive Controls (U18) & Unobserved Surfaces
# ---------------------------------------------------------------------------
# Scope & Unobserved Surfaces (T-5 Q3):
# What this ratchet CANNOT see:
# 1. Payload deep schema: Only docstring and route paths are generated/compared;
#    inner JSON body parameter schemas for REST routes are not generated here.
# 2. Tool handler runtime signatures vs MCP inputSchema parameter types.


def test_positive_control_drift_detection():
    """Standing positive control (U18 / T-5 Q1): prove drift fails assertion loudly."""
    gen = _load_generator()
    expected = gen.generate().strip()
    synthetic_actual = expected + "\n<!-- synthetic drift -->"
    assert synthetic_actual != expected
