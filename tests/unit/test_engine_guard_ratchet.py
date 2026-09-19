"""tests/unit/test_engine_guard_ratchet.py — every guarded engine's specs stay gated.

Q-45/#294 retrofitted 18 registered ResourceSpecs (as of 2026-09-19) across the 7
engines that had a ``_guard.py`` AND a merged spec at that moment, wiring
``enabled_guard`` to each engine's existing ``require_*_enabled`` so the generated
C12 surface enforces the same per-namespace opt-in gate every hand-written
route/tool already does.

That retrofit is a POINT-IN-TIME snapshot, not a standing invariant -- and it
already leaked the same night. Three specs landed on guarded engines after #294
merged: hr (4 specs, PR #287), marketing (3 specs, PR #288), business_insights (1
spec, PR #289). Only hr's author noticed and wired ``enabled_guard`` during their
own rebase; marketing and business_insights did not. "Remember to do this" is not
a control -- this ratchet is.

The guarded-engine list below is DISCOVERED from ``nce/vertical_modules/*/_guard.py``
on disk, never hardcoded: a hardcoded list rot the day a new engine gains a guard,
which is the exact failure mode this ratchet exists to prevent.
"""

from __future__ import annotations

import pathlib

import pytest

from nce.resource_surface import (
    get_all_resource_specs,
    register_resource,
    unregister_resource,
)
from nce.resource_surface.spec import ResourceSpec

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
_VERTICAL_MODULES = _REPO_ROOT / "nce" / "vertical_modules"


def _discover_guarded_engines() -> set[str]:
    """Engine slugs with a ``_guard.py`` -- read from the tree, not a literal list."""
    return {p.parent.name for p in _VERTICAL_MODULES.glob("*/_guard.py")}


# Explicit, reasoned exemptions: a spec here is a REAL, registered ResourceSpec on
# a guarded engine that deliberately has no enabled_guard. An entry here must be
# argued for -- substantive technical rationale, not "not done yet" -- the same
# discipline as RESOURCE_SURFACE_EXEMPTIONS in nce/resource_surface/exemptions.py.
# Never weaken the assertion below to a warning instead of adding here: a warning
# is exactly the "remember to do this" control this ratchet exists to replace.
#
# Empty as of 2026-09-19 -- every registered spec on a guarded engine is expected
# to be gated. Add an entry only with a reason a reviewer could argue against.
_ENABLED_GUARD_EXEMPTIONS: frozenset[tuple[str, str]] = frozenset()


def _specs_missing_enabled_guard(guarded_engines: set[str]) -> list[str]:
    """Shared detection logic: every registered spec on a guarded engine, minus
    explicit exemptions, that has no ``enabled_guard``. Used by both the real
    ratchet test and its own positive control below, so the positive control
    proves the SAME code path the ratchet runs, not a re-implementation of it.
    """
    violations = []
    for spec in get_all_resource_specs():
        if spec.engine not in guarded_engines:
            continue
        if (spec.engine, spec.entity) in _ENABLED_GUARD_EXEMPTIONS:
            continue
        if spec.enabled_guard is None:
            violations.append(f"{spec.engine}:{spec.entity}")
    return violations


def test_every_spec_on_a_guarded_engine_sets_enabled_guard() -> None:
    guarded_engines = _discover_guarded_engines()
    assert guarded_engines, (
        "No nce/vertical_modules/*/_guard.py files found -- the glob is broken, "
        "not that every engine's opt-in guard vanished. Fix the discovery before "
        "trusting an empty result."
    )

    violations = _specs_missing_enabled_guard(guarded_engines)
    assert not violations, (
        f"{len(violations)} ResourceSpec(s) on a guarded engine have no "
        f"enabled_guard, so the generated C12 surface does not enforce that "
        f"engine's per-namespace opt-in for them: {violations}. Wire "
        f"enabled_guard=require_<engine>_enabled (from that engine's own "
        f"_guard.py) on the ResourceSpec, or add an explicit, reasoned entry to "
        f"_ENABLED_GUARD_EXEMPTIONS in this file if the spec is legitimately "
        f"exempt."
    )


def test_positive_control_a_spec_with_no_enabled_guard_is_caught() -> None:
    """Proves the ratchet above can actually fail -- an instrument that cannot
    fail proves nothing (the same shape as the 5 blind instruments found this
    week: a dedented assert, a scan pointing at a nonexistent path, an
    in-memory fallback with no real constraints, two permanently-inert
    _ensure_prereqs guards). Registers a real, synthetic ResourceSpec on a
    real guarded engine (inventory) with enabled_guard=None, runs the SAME
    shared detection function the real test uses, and confirms it is flagged
    -- then unregisters it via nce.resource_surface.unregister_resource
    ("used by positive controls", per its own docstring) so no other test
    ever sees it.
    """
    guarded_engines = _discover_guarded_engines()
    assert "inventory" in guarded_engines, (
        "This control assumes 'inventory' has a _guard.py -- re-pick a real "
        "guarded engine from _discover_guarded_engines() if that ever changes."
    )

    probe_entity = "synthetic-ratchet-probe"
    probe = ResourceSpec(
        engine="inventory",
        entity=probe_entity,
        node_type="SYNTHETIC_RATCHET_PROBE",
        table_name=None,  # graph-scoped: no real table needed for this probe
        enabled_guard=None,
    )
    register_resource(probe)
    try:
        violations = _specs_missing_enabled_guard(guarded_engines)
        assert f"inventory:{probe_entity}" in violations, (
            "The positive control spec (enabled_guard=None, on a guarded engine, "
            "not exempted) was NOT flagged -- the detection logic is not looking "
            "at anything. Fix _specs_missing_enabled_guard before trusting the "
            "real ratchet test above."
        )
    finally:
        assert unregister_resource("inventory", probe_entity), (
            "Failed to unregister the synthetic probe spec -- it would leak into "
            "every other test that calls get_all_resource_specs()."
        )


def test_positive_control_an_exempted_probe_is_not_flagged() -> None:
    """The mirror image of the control above: an exemption entry must actually
    suppress the violation, not just exist as documentation nobody reads. Adds
    a real exemption for the same synthetic probe used above, confirms it is
    NOT flagged, then removes both the exemption and the registration.
    """
    guarded_engines = _discover_guarded_engines()
    probe_entity = "synthetic-ratchet-exempt-probe"
    probe = ResourceSpec(
        engine="inventory",
        entity=probe_entity,
        node_type="SYNTHETIC_RATCHET_EXEMPT_PROBE",
        table_name=None,
        enabled_guard=None,
    )
    register_resource(probe)
    exemption_key = ("inventory", probe_entity)
    global _ENABLED_GUARD_EXEMPTIONS
    original = _ENABLED_GUARD_EXEMPTIONS
    _ENABLED_GUARD_EXEMPTIONS = original | {exemption_key}
    try:
        violations = _specs_missing_enabled_guard(guarded_engines)
        assert f"inventory:{probe_entity}" not in violations, (
            "An exempted spec was still flagged -- the exemption check in "
            "_specs_missing_enabled_guard is broken."
        )
    finally:
        _ENABLED_GUARD_EXEMPTIONS = original
        assert unregister_resource("inventory", probe_entity)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
