"""
tests/unit/test_project_phase_gates.py
========================================
Acceptance tests for Batch 068 — Module 7.Wave 1 (phase-gates).

Covers all four acceptance criteria from the wave spec:
  1. Legal G-edge with all criteria met → ok=True, missing_criteria=[].
  2. Legal G-edge with a missing criterion → ok=False + exact missing_criteria list.
  3. Illegal / undeclared transition → ok=False, no exception raised.
  4. Criteria are sourced from project-gate-criteria.json (not hard-coded).

All tests are plain unit tests — no DB, no Redis, no HTTP.

Split into two groups (mirrors the procurement-tco pattern):
  (a) ALGORITHM tests — inject fixture config; prove logic is config-driven.
  (b) CONFIG tests  — assert the real JSON loads and contains required structure.
"""

from __future__ import annotations

from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Shared fixture config — algorithm tests use these, never the real JSON values
# ---------------------------------------------------------------------------

_FIXTURE_CONFIG: dict[str, Any] = {
    "VALID_PHASE_TRANSITIONS": {
        "G0": ["G1"],
        "G1": ["G2"],
        "G2": ["G3"],
        "G3": ["G4"],
        "G4": ["G5"],
        "G5": ["G6"],
        "G6": [],
    },
    "GATE_CRITERIA": {
        "G0": [],
        "G1": ["signed_quote_attached", "project_manager_assigned"],
        "G2": ["signed_baseline_frozen", "bom_lines_linked", "kick_off_meeting_held"],
        "G3": ["design_approved", "bom_fully_specified", "site_access_confirmed"],
        "G4": ["frozen_baseline_locked", "bom_ordered", "project_lead_assigned"],
        "G5": ["all_bom_lines_delivered", "installation_complete", "testing_started"],
        "G6": [
            "all_tests_passed",
            "customer_sign_off",
            "as_built_documented",
            "handover_to_support_done",
        ],
    },
}

# Alternative config with a different legal edge set — proves config drives behaviour.
_FIXTURE_CONFIG_SKIP: dict[str, Any] = {
    "VALID_PHASE_TRANSITIONS": {
        "G0": ["G1", "G2"],  # hypothetical tenant allows G0→G2 directly
        "G1": ["G2"],
        "G2": ["G3"],
        "G3": [],
    },
    "GATE_CRITERIA": {
        "G0": [],
        "G1": ["criterion_a"],
        "G2": ["criterion_b"],
        "G3": ["criterion_c"],
    },
}


# ---------------------------------------------------------------------------
# Helper — call can_enter_phase with injected config so tests are hermetic
# ---------------------------------------------------------------------------


def _enter(
    config: dict[str, Any],
    current: str,
    target: str,
    criteria_met: list[str] | None = None,
) -> dict[str, Any]:
    """Call can_enter_phase with a patched config loader (no filesystem I/O)."""
    from unittest.mock import patch

    from nce.vertical_modules.project.phase_gates import can_enter_phase

    project: dict[str, Any] = {
        "current_phase": current,
        "criteria_met": criteria_met or [],
    }
    with patch(
        "nce.vertical_modules.project.phase_gates.load_gate_config",
        return_value=config,
    ):
        return can_enter_phase(project, target)


# ===========================================================================
# (a) ALGORITHM TESTS — logic proven with fixture config
# ===========================================================================


# ---------------------------------------------------------------------------
# 1. Legal G-edge with all criteria met → ok=True, missing_criteria=[]
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "current,target,criteria",
    [
        ("G0", "G1", ["signed_quote_attached", "project_manager_assigned"]),
        ("G1", "G2", ["signed_baseline_frozen", "bom_lines_linked", "kick_off_meeting_held"]),
        ("G2", "G3", ["design_approved", "bom_fully_specified", "site_access_confirmed"]),
        ("G3", "G4", ["frozen_baseline_locked", "bom_ordered", "project_lead_assigned"]),
        (
            "G4",
            "G5",
            ["all_bom_lines_delivered", "installation_complete", "testing_started"],
        ),
        (
            "G5",
            "G6",
            [
                "all_tests_passed",
                "customer_sign_off",
                "as_built_documented",
                "handover_to_support_done",
            ],
        ),
    ],
)
def test_legal_edge_all_criteria_met_returns_ok(
    current: str, target: str, criteria: list[str]
) -> None:
    """Every declared legal G-edge with all criteria satisfied must return ok=True."""
    result = _enter(_FIXTURE_CONFIG, current, target, criteria)
    assert result["ok"] is True, f"{current}→{target} should be ok with all criteria met"
    assert result["missing_criteria"] == [], (
        f"missing_criteria must be empty when all criteria are met, got {result['missing_criteria']}"
    )


def test_g0_has_no_criteria_so_always_ok() -> None:
    """G0 has no gate criteria — any project at G0→G1 with empty criteria_met is ok
    as long as G1 criteria are satisfied."""
    # G0 itself has no criteria; entering G1 requires two criteria.
    result = _enter(
        _FIXTURE_CONFIG, "G0", "G1", ["signed_quote_attached", "project_manager_assigned"]
    )
    assert result["ok"] is True
    assert result["missing_criteria"] == []


# ---------------------------------------------------------------------------
# 2. Legal G-edge with a missing criterion → ok=False + exact missing_criteria
# ---------------------------------------------------------------------------


def test_legal_edge_one_criterion_missing() -> None:
    """Legal transition but one criterion absent → ok=False with that criterion listed."""
    # G3→G4 requires three criteria; supply only two.
    result = _enter(
        _FIXTURE_CONFIG,
        "G3",
        "G4",
        ["frozen_baseline_locked", "bom_ordered"],  # missing: project_lead_assigned
    )
    assert result["ok"] is False
    assert result["missing_criteria"] == ["project_lead_assigned"]


def test_legal_edge_all_criteria_missing() -> None:
    """Legal transition but zero criteria met → ok=False with the full list."""
    result = _enter(_FIXTURE_CONFIG, "G3", "G4", [])
    assert result["ok"] is False
    assert set(result["missing_criteria"]) == {
        "frozen_baseline_locked",
        "bom_ordered",
        "project_lead_assigned",
    }


def test_legal_edge_multiple_criteria_missing_exact_list() -> None:
    """Missing criteria list contains exactly the absent keys, in config order."""
    # G2→G3 requires design_approved, bom_fully_specified, site_access_confirmed.
    # Provide only bom_fully_specified — other two are missing.
    result = _enter(_FIXTURE_CONFIG, "G2", "G3", ["bom_fully_specified"])
    assert result["ok"] is False
    assert result["missing_criteria"] == ["design_approved", "site_access_confirmed"]


def test_extra_criteria_met_does_not_affect_result() -> None:
    """Satisfying more criteria than required must not cause ok=False."""
    # G0→G1 needs two criteria; we also include an irrelevant extra key.
    result = _enter(
        _FIXTURE_CONFIG,
        "G0",
        "G1",
        ["signed_quote_attached", "project_manager_assigned", "extra_unrelated_key"],
    )
    assert result["ok"] is True
    assert result["missing_criteria"] == []


# ---------------------------------------------------------------------------
# 3. Illegal / undeclared transition → ok=False, no exception raised
# ---------------------------------------------------------------------------


def test_undeclared_target_phase_returns_not_ok() -> None:
    """A phase name not in the transition map returns ok=False (never raises)."""
    result = _enter(_FIXTURE_CONFIG, "G0", "GFOO")
    assert result["ok"] is False
    assert result["missing_criteria"] == []


def test_non_adjacent_jump_forward_returns_not_ok() -> None:
    """Jumping two gates ahead (G0→G2) is illegal — only adjacent edges are declared."""
    result = _enter(_FIXTURE_CONFIG, "G0", "G2")
    assert result["ok"] is False
    assert result["missing_criteria"] == []


def test_backward_transition_returns_not_ok() -> None:
    """Attempting a backwards move (G3→G1) returns ok=False (edges are one-way)."""
    result = _enter(
        _FIXTURE_CONFIG,
        "G3",
        "G1",
        ["signed_quote_attached", "project_manager_assigned"],
    )
    assert result["ok"] is False
    assert result["missing_criteria"] == []


def test_self_transition_returns_not_ok() -> None:
    """A project trying to enter its own current gate returns ok=False."""
    result = _enter(_FIXTURE_CONFIG, "G2", "G2")
    assert result["ok"] is False


def test_terminal_gate_has_no_successors() -> None:
    """G6 is terminal — attempting to advance beyond it returns ok=False."""
    result = _enter(_FIXTURE_CONFIG, "G6", "G6")
    assert result["ok"] is False


def test_empty_current_phase_returns_not_ok() -> None:
    """Missing current_phase key in project dict is treated as unknown — not ok."""
    from unittest.mock import patch

    from nce.vertical_modules.project.phase_gates import can_enter_phase

    project: dict[str, Any] = {"criteria_met": ["signed_quote_attached"]}
    with patch(
        "nce.vertical_modules.project.phase_gates.load_gate_config",
        return_value=_FIXTURE_CONFIG,
    ):
        result = can_enter_phase(project, "G1")
    assert result["ok"] is False
    assert result["missing_criteria"] == []


# ---------------------------------------------------------------------------
# 4. Config drives behaviour — criteria sourced from config, not hard-coded
# ---------------------------------------------------------------------------


def test_different_config_different_legal_edges() -> None:
    """The alternative fixture allows G0→G2 directly; the default config does not."""
    # With skip config: G0→G2 is legal (no criteria for G2 in this config).
    result_skip = _enter(_FIXTURE_CONFIG_SKIP, "G0", "G2", ["criterion_b"])
    assert result_skip["ok"] is True

    # With default config: G0→G2 is NOT legal.
    result_default = _enter(_FIXTURE_CONFIG, "G0", "G2")
    assert result_default["ok"] is False


def test_different_config_different_criteria() -> None:
    """Swapping the config changes which criteria are required for a given gate."""
    # In _FIXTURE_CONFIG_SKIP, entering G2 only requires criterion_b.
    result = _enter(_FIXTURE_CONFIG_SKIP, "G1", "G2", [])
    assert result["ok"] is False
    assert result["missing_criteria"] == ["criterion_b"]

    # Satisfying criterion_b → ok.
    result_ok = _enter(_FIXTURE_CONFIG_SKIP, "G1", "G2", ["criterion_b"])
    assert result_ok["ok"] is True


def test_result_shape_is_always_consistent() -> None:
    """can_enter_phase must always return a dict with 'ok' and 'missing_criteria'."""
    for current, target in [("G0", "G1"), ("G0", "G5"), ("G3", "G4"), ("G6", "G6")]:
        result = _enter(_FIXTURE_CONFIG, current, target)
        assert "ok" in result, f"'ok' missing for {current}→{target}"
        assert "missing_criteria" in result, f"'missing_criteria' missing for {current}→{target}"
        assert isinstance(result["ok"], bool)
        assert isinstance(result["missing_criteria"], list)


# ===========================================================================
# (b) CONFIG TESTS — assert the real JSON file loads and has required structure
# ===========================================================================


def _load_real_config() -> dict[str, Any]:
    from nce.vertical_modules.project.phase_gates import load_gate_config

    return load_gate_config()


def test_real_config_loads_without_error() -> None:
    """project-gate-criteria.json must load cleanly via load_gate_config."""
    config = _load_real_config()
    assert isinstance(config, dict)


def test_real_config_has_valid_phase_transitions_key() -> None:
    config = _load_real_config()
    assert "VALID_PHASE_TRANSITIONS" in config


def test_real_config_has_gate_criteria_key() -> None:
    config = _load_real_config()
    assert "GATE_CRITERIA" in config


def test_real_config_valid_transitions_contains_all_gates() -> None:
    """All G0–G6 must appear as keys in VALID_PHASE_TRANSITIONS."""
    config = _load_real_config()
    vpt = config["VALID_PHASE_TRANSITIONS"]
    for gate in ("G0", "G1", "G2", "G3", "G4", "G5", "G6"):
        assert gate in vpt, f"VALID_PHASE_TRANSITIONS missing gate '{gate}'"


def test_real_config_valid_transitions_form_a_dag() -> None:
    """Every declared target must itself be a key in VALID_PHASE_TRANSITIONS (no dangling refs)."""
    config = _load_real_config()
    vpt: dict[str, list[str]] = config["VALID_PHASE_TRANSITIONS"]
    for source, targets in vpt.items():
        for t in targets:
            assert t in vpt, (
                f"VALID_PHASE_TRANSITIONS['{source}'] references '{t}' which is not a declared gate"
            )


def test_real_config_gate_criteria_contains_all_gates() -> None:
    """All G0–G6 must appear as keys in GATE_CRITERIA."""
    config = _load_real_config()
    gc = config["GATE_CRITERIA"]
    for gate in ("G0", "G1", "G2", "G3", "G4", "G5", "G6"):
        assert gate in gc, f"GATE_CRITERIA missing gate '{gate}'"


def test_real_config_gate_criteria_values_are_lists_of_strings() -> None:
    """Each gate's criteria must be a list of strings."""
    config = _load_real_config()
    gc = config["GATE_CRITERIA"]
    for gate, criteria in gc.items():
        assert isinstance(criteria, list), f"GATE_CRITERIA['{gate}'] must be a list"
        for c in criteria:
            assert isinstance(c, str), (
                f"GATE_CRITERIA['{gate}'] contains a non-string criterion: {c!r}"
            )


def test_real_config_g0_has_no_criteria() -> None:
    """G0 is the entry gate — it must have no entry criteria."""
    config = _load_real_config()
    assert config["GATE_CRITERIA"]["G0"] == [], "G0 must have no entry criteria"


def test_real_config_g3_to_g4_has_key_criteria() -> None:
    """G3→G4 is the financially significant gate; it must require at least baseline + BOM + PL."""
    config = _load_real_config()
    g4_criteria: list[str] = config["GATE_CRITERIA"]["G4"]
    assert len(g4_criteria) >= 3, "G4 must require at least 3 criteria (baseline, BOM, PL)"


def test_real_config_drives_can_enter_phase_end_to_end() -> None:
    """Real JSON config + full path through can_enter_phase — integration of config + logic."""
    from nce.vertical_modules.project.phase_gates import can_enter_phase, load_gate_config

    config = load_gate_config()
    g4_criteria: list[str] = config["GATE_CRITERIA"]["G4"]

    # All criteria met → ok
    project_ok: dict[str, Any] = {
        "current_phase": "G3",
        "criteria_met": g4_criteria,
    }
    result_ok = can_enter_phase(project_ok, "G4")
    assert result_ok["ok"] is True
    assert result_ok["missing_criteria"] == []

    # No criteria met → ok=False, all criteria missing
    project_fail: dict[str, Any] = {
        "current_phase": "G3",
        "criteria_met": [],
    }
    result_fail = can_enter_phase(project_fail, "G4")
    assert result_fail["ok"] is False
    assert set(result_fail["missing_criteria"]) == set(g4_criteria)

    # Illegal transition (skip two gates) → ok=False regardless of criteria
    project_skip: dict[str, Any] = {
        "current_phase": "G1",
        "criteria_met": g4_criteria,
    }
    result_skip = can_enter_phase(project_skip, "G4")
    assert result_skip["ok"] is False
