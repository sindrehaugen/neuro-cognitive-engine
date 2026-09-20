"""
tests/test_engine_status_current.py
=====================================
Doc-gate ratchet for docs/vertical_engines/ENGINE_STATUS.md's byte-check.

``scripts/gen_engine_figures.py`` already ships the check this test needs --
``main()``'s ``--check`` branch compares ``ENGINE_STATUS.md`` against
``update_engine_status()``'s output whenever ``--update-status`` is also
given (see that function, and its own module docstring: "Mechanically
updates docs/vertical_engines/ENGINE_STATUS.md when --update-status is
passed"). The gap is not in the generator, it is in ci.yml's D-GEN gate,
which invokes only::

    python scripts/gen_engine_figures.py --repo . --baseline HEAD \
        --out docs/_generated/engine_figures.md --check

-- with no ``--update-status``, so ``status_path`` is ``None`` and the whole
ENGINE_STATUS.md comparison block in ``main()`` is skipped every time.
``docs/_generated/engine_figures.md`` has been gated since ci.yml's D-GEN
comment (2026-09-19); the page whose own header tells readers to trust it
over ENGINE_STATUS.md's hand-adjacent numbers was never itself checked.

This test does not add a workflow change (ci.yml is Lane H's file, open and
edited within the hour of this wave's dispatch) -- it reuses the generator's
own already-correct comparison logic by calling ``update_engine_status()``
and ``normalize_volatile()`` directly, the same functions ``--check`` calls,
against the same committed file. It runs in ci.yml's existing unconditional
``pytest tests/`` sweep with zero workflow edits.

MLV16G wave, 2026-09-20 (v1.6 lane G is struck; this wave is unrelated
backfill-importer scope, dispatched directly by ML-orch). Scope: this file
only -- does not touch ``nce/resource_surface/**`` or
``tests/integration/test_golden_thread.py`` (Lane H, live right now).
"""

from __future__ import annotations

import copy
import importlib.util
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_STATUS_DOC = _ROOT / "docs" / "vertical_engines" / "ENGINE_STATUS.md"


def _load_generator():
    path = _ROOT / "scripts" / "gen_engine_figures.py"
    spec = importlib.util.spec_from_file_location("gen_engine_figures", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _extract_stats(gen, baseline: str = "HEAD") -> dict:
    """Gather the five stat dicts update_engine_status() needs, exactly as main() does."""
    return {
        "baseline_sha": gen.git_rev_parse(str(_ROOT), baseline),
        "tool_stats": gen.extract_tool_registry_stats(str(_ROOT), baseline),
        "mig_stats": gen.extract_migration_stats(str(_ROOT), baseline),
        "gt_stats": gen.extract_golden_thread_stats(str(_ROOT), baseline),
        "v16_stats": gen.extract_v16_resource_surface_stats(str(_ROOT), baseline),
        "rls_stats": gen.extract_rls_table_stats(str(_ROOT), baseline),
    }


@pytest.mark.doc_gate
@pytest.mark.skipif(
    not _STATUS_DOC.exists(), reason="docs/vertical_engines/ENGINE_STATUS.md not present"
)
def test_engine_status_doc_matches_the_generator() -> None:
    gen = _load_generator()
    stats = _extract_stats(gen, "HEAD")
    current = _STATUS_DOC.read_text(encoding="utf-8").replace("\r\n", "\n")
    expected = gen.update_engine_status(status_text=current, **stats).replace("\r\n", "\n")
    assert gen.normalize_volatile(current) == gen.normalize_volatile(expected), (
        "docs/vertical_engines/ENGINE_STATUS.md has drifted from "
        "scripts/gen_engine_figures.py's output for the current tree -- it was "
        "hand-edited, or a tool/migration/RLS/golden-thread/v1.6-registration count "
        "changed without regenerating. Regenerate with:\n"
        "  python scripts/gen_engine_figures.py --repo . --baseline HEAD "
        "--out docs/_generated/engine_figures.md "
        "--update-status docs/vertical_engines/ENGINE_STATUS.md"
    )


def test_engine_status_generator_is_sensitive_to_a_real_change() -> None:
    """Positive control (U18): prove the ratchet above is not vacuous.

    ``--check`` already runs this exact comparison for ``engine_figures.md``
    today (ci.yml's D-GEN gate) -- what was never proven is that
    ``update_engine_status()``'s patch-in-place regex substitutions actually
    change the text when a real stat changes, as opposed to e.g. matching
    nothing and silently leaving ``status_text`` untouched (which would make
    the ratchet above pass forever regardless of drift). Mutates one real
    stat, confirms the patched text differs, and confirms comparing the
    mutated text against the unmutated original would fail -- proving this
    is a real check, not a no-op dressed as one.
    """
    gen = _load_generator()
    stats = _extract_stats(gen, "HEAD")
    current = _STATUS_DOC.read_text(encoding="utf-8").replace("\r\n", "\n")

    baseline_patched = gen.update_engine_status(status_text=current, **stats)

    mutated_stats = copy.deepcopy(stats)
    mutated_stats["tool_stats"]["total_tools"] += 1
    mutated_patched = gen.update_engine_status(status_text=current, **mutated_stats)

    assert mutated_patched != baseline_patched, (
        "update_engine_status() produced identical text after incrementing "
        "total_tools by 1 -- its TOOL_REGISTRY row substitution is not matching, "
        "so the ratchet above would never go red no matter how stale the tool "
        "count in ENGINE_STATUS.md becomes."
    )
    assert gen.normalize_volatile(mutated_patched) != gen.normalize_volatile(current), (
        "the mutated tool count round-tripped back to the committed file's content "
        "after normalisation -- normalize_volatile() is stripping more than the "
        "volatile stamp."
    )
