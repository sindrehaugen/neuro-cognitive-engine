"""The contractor A2A allowlist must stay exactly one skill.

Batch 230d advertised ``vendors_match_contractor`` and
``vendors_compute_performance`` over MCP. Both are also reachable as A2A skills,
so the wave's acceptance criterion required proof that publishing an MCP schema
did not widen what a *contractor* principal may call.

A correction to the ledger row's own wording, worth keeping because it changes
what the test must assert: the row described these two as sitting "under a
contractor-scoped allowlist". The code is stricter. ``nce/a2a_server.py`` gives a
contractor principal

    allowed_partner_skills = {"vendors_partner_view"}

and raises ``A2AScopeViolationError`` for anything else -- so contractors are
**denied** both tools rather than granted them. The invariant to pin is therefore
"the allowlist is still exactly {vendors_partner_view}", not "these two are in
it".

Read from the SOURCE rather than by importing ``a2a_server``: importing it pulls
the engine and its config, and this assertion is about a literal in the code, so
parsing is both cheaper and unaffected by whether a server can start.
"""

from __future__ import annotations

import ast
from pathlib import Path

_A2A = Path(__file__).resolve().parents[2] / "nce" / "a2a_server.py"

#: The only skill an A2A contractor session may call.
_EXPECTED_CONTRACTOR_SKILLS = {"vendors_partner_view"}

#: Advertised over MCP by batch 230d; must NOT become contractor-reachable.
_MCP_ADVERTISED_VENDOR_SKILLS = {
    "vendors_match_contractor",
    "vendors_compute_performance",
}


def _allowed_partner_skills() -> set[str]:
    """Every literal assigned to ``allowed_partner_skills`` in a2a_server.py."""
    tree = ast.parse(_A2A.read_text(encoding="utf-8"), filename=str(_A2A))
    found: list[set[str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if getattr(target, "id", None) == "allowed_partner_skills":
                try:
                    value = ast.literal_eval(node.value)
                except ValueError:  # pragma: no cover - a computed allowlist
                    raise AssertionError(
                        "allowed_partner_skills is no longer a literal, so this gate "
                        "can no longer read it. Make it a literal again or replace "
                        "this test with one that exercises the scope check."
                    ) from None
                found.append(set(value))

    assert found, (
        "no `allowed_partner_skills` assignment found in nce/a2a_server.py -- either "
        "the contractor scope check was removed (a security regression) or it was "
        "renamed, in which case this gate is blind and must be updated, not deleted."
    )
    assert len(found) == 1, (
        f"expected exactly one contractor allowlist, found {len(found)}: {found}. "
        "Two allowlists means one of them is not being enforced."
    )
    return found[0]


def test_contractor_allowlist_is_exactly_one_skill() -> None:
    assert _allowed_partner_skills() == _EXPECTED_CONTRACTOR_SKILLS


def test_mcp_advertised_vendor_tools_are_not_contractor_reachable() -> None:
    """Publishing an MCP schema must not grant contractor A2A access."""
    leaked = _allowed_partner_skills() & _MCP_ADVERTISED_VENDOR_SKILLS
    assert not leaked, (
        "batch 230d advertised these over MCP and they have since become reachable "
        f"by a contractor A2A principal: {sorted(leaked)}. An MCP schema documents a "
        "tool; it must never widen the A2A grant."
    )


def test_the_scope_check_still_raises() -> None:
    """Guard the guard: an allowlist nobody enforces is decoration.

    Pins that the assignment is followed by a membership test that raises, so
    deleting the `raise` cannot leave this file green.
    """
    source = _A2A.read_text(encoding="utf-8")
    idx = source.index("allowed_partner_skills")
    window = source[idx : idx + 400]
    assert "not in allowed_partner_skills" in window, (
        "the contractor allowlist is no longer consulted immediately after being "
        "defined -- the membership test moved or was removed."
    )
    assert "A2AScopeViolationError" in window, (
        "the contractor scope check no longer raises A2AScopeViolationError, so an "
        "out-of-scope skill would be permitted."
    )
