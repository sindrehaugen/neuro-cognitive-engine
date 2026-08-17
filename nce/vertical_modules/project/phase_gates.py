"""
nce/vertical_modules/project/phase_gates.py
============================================
Pure G0–G6 phase-gate state-machine — zero DB, zero HTTP, zero web/admin imports.

Ported near-1:1 from the design in ``docs/vertical_engines/07-project-engine.md``
(Build phase P1, ``can_enter_phase`` core function).

State-machine rules
-------------------
- ``VALID_PHASE_TRANSITIONS`` declares the only legal directed edges (G0→G1→…→G6).
  Any target phase not reachable from the current phase is structurally refused.
- ``GATE_CRITERIA`` names the criteria that must be satisfied before entering each
  target gate.  Both tables are read from ``project-gate-criteria.json``
  (config-as-IP, namespace-tunable per roadmap §2.9) — no constants live here.

Criteria contract
-----------------
Each criterion in ``GATE_CRITERIA[target_phase]`` is a named key.  The caller
supplies ``project["criteria_met"]``: a collection of criterion keys that are
currently satisfied.  Any named key absent from that collection is returned in
``missing_criteria``.

Degraded-mode note (roadmap review round-2 #4)
-----------------------------------------------
``can_enter_phase`` is intentionally pure: it does not gather cross-engine facts
itself.  ``do_advance_phase`` (future wave) is responsible for resolving criteria
that reference external engines (Sales baseline, Procurement PO status, HR PL
assignment) and for handling ``unknown``/``waived`` flags.  This function sees only
what the caller provides — if a criterion key is absent from ``criteria_met`` it is
missing, regardless of the reason.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

_CONFIG_DATA_DIR = Path(__file__).parents[3] / "nce" / "config_data"


def load_gate_config() -> dict[str, Any]:
    """Load and return the contents of ``project-gate-criteria.json``.

    Returns
    -------
    dict
        Parsed JSON with keys ``VALID_PHASE_TRANSITIONS`` and ``GATE_CRITERIA``.

    Raises
    ------
    FileNotFoundError
        When the config file is absent (misconfigured deployment).
    json.JSONDecodeError
        When the file is not valid JSON.
    KeyError
        When required top-level keys are missing.
    """
    config_path = _CONFIG_DATA_DIR / "project-gate-criteria.json"
    with config_path.open(encoding="utf-8") as fh:
        config: dict[str, Any] = json.load(fh)
    _validate_gate_config(config)
    return config


def _validate_gate_config(config: dict[str, Any]) -> None:
    """Raise KeyError if required top-level keys are absent."""
    for key in ("VALID_PHASE_TRANSITIONS", "GATE_CRITERIA"):
        if key not in config:
            raise KeyError(f"project-gate-criteria.json is missing required key '{key}'")


# ---------------------------------------------------------------------------
# Pure domain core
# ---------------------------------------------------------------------------


def can_enter_phase(project: dict[str, Any], target_phase: str) -> dict[str, Any]:
    """Return whether ``project`` can transition to ``target_phase``.

    Pure function — no DB, no HTTP, no side effects.

    Parameters
    ----------
    project:
        Must contain:
          - ``"current_phase"`` (str) — e.g. ``"G2"``
          - ``"criteria_met"`` (list[str] | set[str]) — criterion keys currently
            satisfied for this project.
    target_phase:
        The gate the caller wants to enter (e.g. ``"G3"``).

    Returns
    -------
    dict
        ``{"ok": bool, "missing_criteria": list[str]}``

        - ``ok=True, missing_criteria=[]`` — transition is legal and all criteria met.
        - ``ok=False, missing_criteria=[...]`` — legal edge but some criteria unmet;
          list contains the names of the unmet criteria.
        - ``ok=False, missing_criteria=[]`` — transition is not a legal edge (illegal
          phase, undeclared target, or non-adjacent jump); no criteria to list.

    Notes
    -----
    Never raises on an illegal transition — always returns ``ok=False`` so callers
    get a consistent response shape regardless of input.
    """
    config = load_gate_config()
    transitions: dict[str, list[str]] = config["VALID_PHASE_TRANSITIONS"]
    criteria_map: dict[str, list[str]] = config["GATE_CRITERIA"]

    current_phase: str = project.get("current_phase", "")
    criteria_met: set[str] = set(project.get("criteria_met", []))

    if not _is_legal_transition(transitions, current_phase, target_phase):
        return {"ok": False, "missing_criteria": []}

    required: list[str] = criteria_map.get(target_phase, [])
    missing: list[str] = _find_missing_criteria(required, criteria_met)

    return {"ok": len(missing) == 0, "missing_criteria": missing}


# ---------------------------------------------------------------------------
# Private helpers — single level of abstraction
# ---------------------------------------------------------------------------


def _is_legal_transition(
    transitions: dict[str, list[str]],
    current_phase: str,
    target_phase: str,
) -> bool:
    """Return True iff ``target_phase`` is a declared successor of ``current_phase``."""
    allowed_targets = transitions.get(current_phase, [])
    return target_phase in allowed_targets


def _find_missing_criteria(
    required: list[str],
    criteria_met: set[str],
) -> list[str]:
    """Return criteria in ``required`` that are absent from ``criteria_met``."""
    return [c for c in required if c not in criteria_met]
