"""tests.unit.test_c12_resource_surface_ratchet — C12 Resource Surface Coverage Ratchet.

Phase A Wave A-2:
Enforces that every distinct node_type in nce/config_data/node-ownership.json
either has:
  1. A registered ResourceSpec discovered via nce.resource_surface, OR
  2. A documented, shrink-only exemption in RESOURCE_SURFACE_EXEMPTIONS.

Follows the shrink-only pattern of tests/test_producer_coverage.py:
  - Guard-the-guard: verifies >= 4 registered specs.
  - Shrink-only: fails if a registered spec is also in the exemptions list.
  - Substantive: fails if an exemption lacks owner_engine or has a reason < 50 chars.
  - Positive controls (U18): synthetic failure modes verified, through the REAL
    production functions with monkeypatched/injected data -- never by
    re-deriving the check's own set-arithmetic on fully synthetic local dicts
    that never touch _get_owned_node_types()/_get_registered_specs_by_node_type().

Audit finding (batch-a mutation audit, 2026-09-20), authorized as a contract
change by the orchestrator: ``_get_owned_node_types()`` used to read
``data.get("ownership", [])``. If ``node-ownership.json``'s top-level key were
ever renamed or restructured, that silently degraded to ``{}`` -- the coverage
loop below then iterates zero node types and PASSES, with its entire guarantee
(every governed node type has a ResourceSpec or a reasoned exemption) gone and
no signal that anything is wrong. This is worse than an ordinary false pass:
it is a ratchet that passes HARDEST exactly when its input has disappeared.
Fixed to ``data["ownership"]`` (a renamed/missing key is now a loud
``KeyError``) plus a discovery-floor assertion right after the loop (an
empty-but-present ``"ownership": []`` is now also refused) -- see
``test_owned_node_types_loader_rejects_a_renamed_ownership_key`` and
``test_owned_node_types_loader_rejects_an_empty_ownership_list`` below, which
exercise the real loader against a monkeypatched file rather than asserting
anything about the fix in the abstract.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from nce.resource_surface import (
    RESOURCE_SURFACE_EXEMPTIONS,
    ResourceExemption,
    ResourceSpec,
    get_all_resource_specs,
    load_all_engine_resources,
    register_resource,
    unregister_resource,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_OWNERSHIP_FILE = _REPO_ROOT / "nce" / "config_data" / "node-ownership.json"


def _get_owned_node_types() -> dict[str, set[str]]:
    """Return mapping of node_type -> set of owner_engine values from node-ownership.json."""
    assert _OWNERSHIP_FILE.exists(), f"Missing node-ownership file at {_OWNERSHIP_FILE}"
    data = json.loads(_OWNERSHIP_FILE.read_text(encoding="utf-8"))
    by_node: dict[str, set[str]] = {}
    # Indexing, not .get(..., []): a renamed/restructured "ownership" key must
    # raise KeyError, never silently resolve to zero rows.
    for entry in data["ownership"]:
        nt = entry["node_type"]
        oe = entry["owner_engine"]
        by_node.setdefault(nt, set()).add(oe)
    # An empty result is never a valid state, even if "ownership" is present
    # and parses cleanly as an empty list: every coverage check below is a
    # subtraction FROM this dict, so {} makes every one of them vacuously
    # pass. Treat that as a loader failure, not "nothing to check."
    assert by_node, (
        "_get_owned_node_types() produced zero owned node types from "
        f"{_OWNERSHIP_FILE} -- the 'ownership' key is present but empty (or "
        "every entry failed to parse). This would make every C12 coverage "
        "check below vacuously pass with its entire guarantee gone, so it is "
        "refused here as a loader failure."
    )
    return by_node


def _get_registered_specs_by_node_type() -> dict[str, list]:
    """Discover all vertical engine resources and return mapping by node_type."""
    load_all_engine_resources()
    specs = get_all_resource_specs()
    by_node: dict[str, list] = {}
    for s in specs:
        by_node.setdefault(s.node_type, []).append(s)
    return by_node


# ===========================================================================
# 1. Coverage Ratchet
# ===========================================================================


def test_all_owned_node_types_accounted_for():
    """Every distinct node_type in node-ownership.json must have a ResourceSpec or an exemption."""
    owned_nodes = _get_owned_node_types()
    registered = _get_registered_specs_by_node_type()
    exempt = RESOURCE_SURFACE_EXEMPTIONS

    uncovered = set(owned_nodes.keys()) - set(registered.keys()) - set(exempt.keys())
    assert not uncovered, (
        f"C12 Resource Surface Ratchet Failure: {len(uncovered)} node types in node-ownership.json "
        f"have neither a ResourceSpec nor an exemption in RESOURCE_SURFACE_EXEMPTIONS: "
        f"{sorted(uncovered)}"
    )


def test_exemptions_are_shrink_only_no_stale_exemptions():
    """If a node_type has a registered ResourceSpec, its exemption MUST be removed (shrink-only)."""
    registered = _get_registered_specs_by_node_type()
    exempt = RESOURCE_SURFACE_EXEMPTIONS

    stale = set(registered.keys()) & set(exempt.keys())
    assert not stale, (
        f"Shrink-Only Ratchet Failure: The following node types have registered ResourceSpecs but "
        f"still retain exemptions in RESOURCE_SURFACE_EXEMPTIONS: {sorted(stale)}. "
        f"Remove them from nce/resource_surface/exemptions.py as their resources now exist."
    )


def test_no_phantom_exemptions():
    """Every exemption in RESOURCE_SURFACE_EXEMPTIONS must correspond to an actual node-ownership row."""
    owned_nodes = _get_owned_node_types()
    exempt = RESOURCE_SURFACE_EXEMPTIONS

    phantom = set(exempt.keys()) - set(owned_nodes.keys())
    assert not phantom, (
        f"Phantom Exemption Failure: The following exemptions in RESOURCE_SURFACE_EXEMPTIONS "
        f"do not exist in node-ownership.json: {sorted(phantom)}"
    )


def test_exemption_metadata_substantive():
    """All exemptions must specify an owning engine and a substantive explanation (>= 50 chars)."""
    owned_nodes = _get_owned_node_types()
    for node_type, ex in RESOURCE_SURFACE_EXEMPTIONS.items():
        assert isinstance(ex, ResourceExemption), (
            f"{node_type}: exemption is not a ResourceExemption"
        )
        assert ex.owner_engine, f"{node_type}: missing owner_engine"
        # Validate owner matches node-ownership declaration
        if node_type in owned_nodes:
            assert ex.owner_engine in owned_nodes[node_type], (
                f"{node_type}: exemption owner {ex.owner_engine!r} not in declared owners "
                f"{sorted(owned_nodes[node_type])}"
            )
        cleaned_reason = " ".join(ex.reason.split())
        assert len(cleaned_reason) >= 50, (
            f"{node_type}: exemption reason too thin ({len(cleaned_reason)} chars < 50): "
            f"{cleaned_reason!r}"
        )


# ===========================================================================
# 2. Guard the Guard (Floor Assertions)
# ===========================================================================


def test_guard_the_guard_minimum_resource_specs():
    """Assert minimum floor of registered ResourceSpecs to guard against vacuous passes."""
    load_all_engine_resources()
    specs = get_all_resource_specs()
    # Floor: Wave A-1 establishes 4 reference resources in Inventory
    assert len(specs) >= 4, (
        f"Guard-the-guard failed: expected at least 4 registered ResourceSpecs, found {len(specs)}"
    )
    registered_node_types = {s.node_type for s in specs}
    expected_inventory_nodes = {
        "STOCK_LOCATION",
        "INVENTORY_ITEM",
        "GOODS_RECEIPT",
        "INVENTORY_RMA",
    }
    missing_inv = expected_inventory_nodes - registered_node_types
    assert not missing_inv, f"Reference inventory resources missing: {missing_inv}"


# ===========================================================================
# 3. Loader Regression Tests (the {} blind spot)
# ===========================================================================
#
# These exercise the REAL _get_owned_node_types() against a monkeypatched
# _OWNERSHIP_FILE pointing at a controlled temp file -- real file I/O, real
# json.loads, real dict-building loop -- rather than asserting anything about
# the fix in the abstract. Before the fix (data.get("ownership", [])), BOTH
# scenarios below silently produced {} and every test in section 1 passed
# vacuously; this was confirmed by temporarily reverting the two-line fix
# during development and re-running these two tests, which then incorrectly
# went green (no KeyError, no AssertionError) -- proof they are testing the
# fix and not a tautology.


def test_owned_node_types_loader_rejects_a_renamed_ownership_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If node-ownership.json's top-level key is ever renamed/restructured,
    the loader must raise, not silently resolve to zero rows."""
    fake_file = tmp_path / "node-ownership-renamed-key.json"
    fake_file.write_text(json.dumps({"owners": [{"node_type": "X", "owner_engine": "y"}]}))
    monkeypatch.setattr(sys.modules[__name__], "_OWNERSHIP_FILE", fake_file)

    with pytest.raises(KeyError):
        _get_owned_node_types()


def test_owned_node_types_loader_rejects_an_empty_ownership_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An 'ownership' key that is present but empty is exactly as dangerous
    as a missing one -- both make the coverage ratchet iterate zero node
    types and pass vacuously. Must also be refused."""
    fake_file = tmp_path / "node-ownership-empty.json"
    fake_file.write_text(json.dumps({"ownership": []}))
    monkeypatch.setattr(sys.modules[__name__], "_OWNERSHIP_FILE", fake_file)

    with pytest.raises(AssertionError, match="produced zero owned node types"):
        _get_owned_node_types()


def test_coverage_ratchet_fails_loudly_instead_of_vacuously_when_ownership_is_hollow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end version of the two tests above: prove the actual coverage
    TEST (not just the loader in isolation) now fails loudly on a hollowed-out
    node-ownership.json, instead of reporting a false green with its entire
    guarantee gone. This is the exact scenario the orchestrator traced: a
    renamed/restructured "ownership" key."""
    fake_file = tmp_path / "node-ownership-hollow.json"
    fake_file.write_text(json.dumps({"ownership_renamed_by_mistake": []}))
    monkeypatch.setattr(sys.modules[__name__], "_OWNERSHIP_FILE", fake_file)

    with pytest.raises(KeyError):
        test_all_owned_node_types_accounted_for()


# ===========================================================================
# 4. Positive Controls (Standing U18 Tests)
# ===========================================================================
#
# Each of these three calls the REAL _get_owned_node_types() and/or
# _get_registered_specs_by_node_type() against monkeypatched/injected data --
# never a from-scratch re-derivation of the check's own set arithmetic on
# fully synthetic local dicts, which would prove only that Python's set
# subtraction and str.strip() work and would never notice a real defect in
# either production function.


def test_positive_control_fails_on_synthetic_uncovered_node(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """U18 Positive Control: synthetic uncovered node type is caught by the
    real coverage evaluator, through the real loader."""
    # Anchor the "covered" side on a REAL, currently-exempted node type, so
    # the only uncovered entry is the synthetic rogue one.
    real_exempt_node = next(iter(RESOURCE_SURFACE_EXEMPTIONS))
    real_owner = RESOURCE_SURFACE_EXEMPTIONS[real_exempt_node].owner_engine

    fake_file = tmp_path / "node-ownership-rogue.json"
    fake_file.write_text(
        json.dumps(
            {
                "ownership": [
                    {"node_type": real_exempt_node, "owner_engine": real_owner},
                    {"node_type": "ZZZ_SYNTHETIC_ROGUE_NODE", "owner_engine": "rogue_engine"},
                ]
            }
        )
    )
    monkeypatch.setattr(sys.modules[__name__], "_OWNERSHIP_FILE", fake_file)

    owned_nodes = _get_owned_node_types()
    registered = _get_registered_specs_by_node_type()
    exempt = RESOURCE_SURFACE_EXEMPTIONS

    uncovered = set(owned_nodes.keys()) - set(registered.keys()) - set(exempt.keys())
    assert uncovered == {"ZZZ_SYNTHETIC_ROGUE_NODE"}, (
        f"Positive control failed: expected only the synthetic rogue node to be "
        f"flagged uncovered, got {sorted(uncovered)}"
    )


def test_positive_control_fails_on_synthetic_stale_exemption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """U18 Positive Control: a node_type that is BOTH registered AND still
    exempted is caught by the real shrink-only evaluator, through a real
    ResourceSpec registration."""
    synthetic = ResourceSpec(
        engine="_audit_probe_engine",
        entity="_audit_probe_entity",
        node_type="ZZZ_SYNTHETIC_STALE_EXEMPTION_NODE",
        storage_kind="kg_nodes",
    )
    register_resource(synthetic)
    monkeypatch.setitem(
        RESOURCE_SURFACE_EXEMPTIONS,
        "ZZZ_SYNTHETIC_STALE_EXEMPTION_NODE",
        ResourceExemption(owner_engine="_audit_probe_engine", reason="valid reason " * 5),
    )
    try:
        registered = _get_registered_specs_by_node_type()
        exempt = RESOURCE_SURFACE_EXEMPTIONS

        stale = set(registered.keys()) & set(exempt.keys())
        assert "ZZZ_SYNTHETIC_STALE_EXEMPTION_NODE" in stale, (
            "Positive control failed: synthetic stale exemption was not isolated "
            f"by the real shrink-only evaluator (stale={sorted(stale)})"
        )
    finally:
        unregister_resource("_audit_probe_engine", "_audit_probe_entity")


def test_positive_control_fails_on_thin_exemption_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """U18 Positive Control: an exemption with a reason under 50 chars is
    caught by the real substantive-metadata check, not by a standalone
    len() comparison on a throwaway object."""
    monkeypatch.setitem(
        RESOURCE_SURFACE_EXEMPTIONS,
        "ZZZ_SYNTHETIC_THIN_REASON_NODE",
        ResourceExemption(owner_engine="_audit_probe_engine", reason="too short"),
    )

    with pytest.raises(AssertionError, match="exemption reason too thin"):
        test_exemption_metadata_substantive()
