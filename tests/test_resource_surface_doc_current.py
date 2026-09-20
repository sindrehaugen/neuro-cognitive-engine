"""Doc-gate for docs/_generated/resource_surface.md.

Confirmed ungated (Lane F's C:\\Claude\\GENERATOR_INPUT_MAP.md, 2026-09-20): a
repo-wide grep for ``gen_resource_surface`` or ``resource_surface.md`` outside
``scripts/gen_resource_surface.py`` and this file matched nothing -- the same
"instrument exists, nothing runs it" shape ``test_golden_thread_seams_current.py``
and ``test_api_docs_current.py`` closed for their own generators before this.

Not dead weight, though: ``git log --follow -- docs/_generated/resource_surface.md``
shows it regenerated on every recent C12-touching merge (five of the last five),
so lane discipline alone has kept it current -- this test makes that discipline
unnecessary rather than relied upon. ``scripts/gen_resource_surface.py`` already
ships its own ``--check`` mode; this wraps that same ``generate()`` function in
the house doc_gate pattern (mirrors ``test_host_parity_current.py``) so drift is
caught in the normal pytest sweep rather than only when someone remembers to run
the CLI by hand.
"""

from __future__ import annotations

import importlib.util
import pathlib
from dataclasses import replace

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DOC = _ROOT / "docs" / "_generated" / "resource_surface.md"


def _load_generator():
    path = _ROOT / "scripts" / "gen_resource_surface.py"
    spec = importlib.util.spec_from_file_location("gen_resource_surface", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.doc_gate
def test_resource_surface_doc_is_current():
    gen = _load_generator()
    expected = gen.generate().strip()
    actual = _DOC.read_text(encoding="utf-8").strip() if _DOC.exists() else ""
    assert actual == expected, (
        "docs/_generated/resource_surface.md is out of date with the live "
        "ResourceSpec registry. Regenerate with: python scripts/gen_resource_surface.py"
    )


def test_discovery_floor_specs_present():
    """Guard-the-guard: if get_all_resource_specs()/load_all_engine_resources()
    broke, this would render an (incorrectly) tiny but internally-consistent
    doc and the drift check above would happily pass against it."""
    from nce.resource_surface import get_all_resource_specs, load_all_engine_resources

    load_all_engine_resources()
    specs = get_all_resource_specs()
    assert len(specs) >= 30, (
        f"Only {len(specs)} ResourceSpecs found -- expected at least 30 based on the "
        "2026-09-20 census (48). Either specs were removed, or load_all_engine_resources() "
        "stopped discovering engines."
    )


def test_generator_is_sensitive_to_a_real_spec_change():
    """Positive control (U18): prove generate() is actually sensitive to the
    registry it claims to render, not a tautological "mutated string !=
    original string" check. Monkeypatches the real data source
    (get_all_resource_specs) with one extra, genuinely-constructed
    ResourceSpec and asserts the rendered output changes -- proving a real
    spec addition/removal would be caught, not just a hand-edit of the doc."""
    import nce.resource_surface as resource_surface_module

    gen = _load_generator()
    baseline = gen.generate()

    real_specs = list(resource_surface_module.get_all_resource_specs())
    assert real_specs, "no ResourceSpecs found -- fixture precondition failed"

    synthetic_spec = replace(
        real_specs[0],
        engine="k8_synthetic_probe_engine",
        entity="k8_synthetic_probes",
        node_type="K8_SYNTHETIC_PROBE",
    )
    mutated_specs = [*real_specs, synthetic_spec]

    original_getter = resource_surface_module.get_all_resource_specs
    try:
        resource_surface_module.get_all_resource_specs = lambda: mutated_specs
        mutated = gen.generate()
    finally:
        resource_surface_module.get_all_resource_specs = original_getter

    assert mutated != baseline, (
        "generate() produced identical output after adding a synthetic ResourceSpec -- "
        "the doc-gate above would never go red no matter what changed in the registry."
    )
    assert "k8_synthetic_probe_engine" in mutated
