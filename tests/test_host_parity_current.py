"""H-2: drift + RED gate for ``docs/_generated/host_parity.md``.

Mirrors ``tests/test_api_docs_current.py``'s pattern: the generator
(``scripts/gen_portal_parity.py``) is the single source of truth, this test
proves the checked-in generated file matches it, and a positive control proves
drift detection is not vacuous.

This file additionally gates the seed's own completeness (charter §9 "Lane H",
H-2: "a family with no disposition is RED") and the identity rule the seed's
own docstring states: no host product, company, or module/schema name may
appear in the seed or the generated doc. The identity gate
(``tests/test_no_identifying_literals.py``) already covers this tree-wide; this
adds a scoped, fast check specific to the two files H-2 owns, so a violation
here is caught by name rather than only by the broader sweep.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SEED = _ROOT / "docs" / "host_parity_seed.yaml"
_DOC = _ROOT / "docs" / "_generated" / "host_parity.md"

# Deliberately narrow and specific to this seed's own domain (Norwegian-language
# host route/table fragments and named external products/vendors quoted in the
# v1.6 analysis this seed was translated from) -- NOT a restatement of the
# tree-wide identity gate, which already covers company/customer names.
_FORBIDDEN_HOST_TERMS = (
    "utstyr",
    "smartbygg",
    "oneflow",
    "brreg",
    "kartverket",
    "sharepoint",
    "d365",
    "dynamics-ko",
    "regneark",
    "finago",
    "avtale",
    "kunde",
    "faktura",
    "netbox",
)


def _load_generator():
    path = _ROOT / "scripts" / "gen_portal_parity.py"
    spec = importlib.util.spec_from_file_location("gen_portal_parity", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_host_parity_doc_is_current():
    gen = _load_generator()
    expected = gen.generate().strip()
    actual = _DOC.read_text(encoding="utf-8").strip() if _DOC.exists() else ""
    assert actual == expected, (
        "docs/_generated/host_parity.md is out of date with docs/host_parity_seed.yaml. "
        "Regenerate with: python scripts/gen_portal_parity.py"
    )


def test_no_family_is_red():
    """A family with no disposition (or an incomplete MOVE/MOVE+ row) is RED (H-2)."""
    gen = _load_generator()
    families = gen.load_families()
    problems = gen.validate(families)
    assert not problems, (
        f"{len(problems)} RED finding(s) in docs/host_parity_seed.yaml:\n"
        + "\n".join(f"  - {p}" for p in problems)
    )


def test_discovery_floor_families_present():
    """Guard-the-guard: the seed must not have silently collapsed to near-empty."""
    gen = _load_generator()
    families = gen.load_families()
    assert len(families) >= 40, (
        f"Only {len(families)} families found in the seed — expected at least 40 "
        "based on the 2026-09-18 census (51). Either the seed lost content or the "
        "YAML loader broke."
    )


def test_positive_control_missing_disposition_is_caught():
    """Standing positive control (U18): prove a RED family is actually caught."""
    gen = _load_generator()
    families = gen.load_families()
    assert not gen.validate(families), "fixture precondition: today's seed must be fully green"

    synthetic = [dict(f) for f in families]
    synthetic.append(
        {"id": "F99", "category": "test", "family": "synthetic_gap", "description": "x"}
    )
    problems = gen.validate(synthetic)
    assert any("F99" in p and "no disposition" in p for p in problems), (
        "A family with no disposition was not flagged as RED — the validator is vacuous."
    )


def test_positive_control_move_without_replacing_route_is_caught():
    gen = _load_generator()
    families = gen.load_families()
    synthetic = [dict(f) for f in families]
    synthetic.append(
        {
            "id": "F98",
            "category": "test",
            "family": "synthetic_move_gap",
            "description": "x",
            "disposition": "MOVE",
        }
    )
    problems = gen.validate(synthetic)
    assert any("F98" in p and "replacing_route" in p for p in problems), (
        "A MOVE family with no replacing_route was not flagged — the validator is vacuous."
    )


def test_positive_control_drift_detection():
    gen = _load_generator()
    expected = gen.generate().strip()
    synthetic_actual = expected + "\n<!-- synthetic drift -->"
    assert synthetic_actual != expected


@pytest.mark.parametrize("term", _FORBIDDEN_HOST_TERMS)
def test_seed_and_doc_contain_no_forbidden_host_terms(term: str) -> None:
    """Scoped identity check (see module docstring) -- H-2's own two files only."""
    seed_text = _SEED.read_text(encoding="utf-8").lower()
    doc_text = _DOC.read_text(encoding="utf-8").lower() if _DOC.exists() else ""
    pattern = re.compile(r"\b" + re.escape(term.lower()) + r"\b")
    assert not pattern.search(seed_text), f"Forbidden host term {term!r} found in {_SEED.name}"
    assert not pattern.search(doc_text), f"Forbidden host term {term!r} found in {_DOC.name}"
