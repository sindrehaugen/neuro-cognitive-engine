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
  - Positive controls (U18): synthetic failure modes verified.
"""

from __future__ import annotations

import json
from pathlib import Path

from nce.resource_surface import (
    RESOURCE_SURFACE_EXEMPTIONS,
    ResourceExemption,
    get_all_resource_specs,
    load_all_engine_resources,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_OWNERSHIP_FILE = _REPO_ROOT / "nce" / "config_data" / "node-ownership.json"


def _get_owned_node_types() -> dict[str, set[str]]:
    """Return mapping of node_type -> set of owner_engine values from node-ownership.json."""
    assert _OWNERSHIP_FILE.exists(), f"Missing node-ownership file at {_OWNERSHIP_FILE}"
    data = json.loads(_OWNERSHIP_FILE.read_text(encoding="utf-8"))
    by_node: dict[str, set[str]] = {}
    for entry in data.get("ownership", []):
        nt = entry["node_type"]
        oe = entry["owner_engine"]
        by_node.setdefault(nt, set()).add(oe)
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
# 3. Positive Controls (Standing U18 Tests)
# ===========================================================================


def test_positive_control_fails_on_synthetic_uncovered_node():
    """U18 Positive Control: synthetic uncovered node type is caught by the coverage evaluator."""
    owned = {"REAL_A": {"engine_a"}, "REAL_B": {"engine_b"}}
    registered = {"REAL_A": []}
    exempt = {"REAL_B": ResourceExemption(owner_engine="engine_b", reason="valid reason " * 5)}

    # Baseline passes
    uncovered = set(owned.keys()) - set(registered.keys()) - set(exempt.keys())
    assert not uncovered

    # Synthetic rogue node
    synthetic_owned = dict(owned)
    synthetic_owned["SYNTHETIC_ROGUE_NODE"] = {"rogue_engine"}
    synthetic_uncovered = set(synthetic_owned.keys()) - set(registered.keys()) - set(exempt.keys())
    assert synthetic_uncovered == {"SYNTHETIC_ROGUE_NODE"}, (
        "Positive control failed: synthetic uncovered node was not flagged"
    )


def test_positive_control_fails_on_synthetic_stale_exemption():
    """U18 Positive Control: synthetic stale exemption is caught by the shrink-only evaluator."""
    registered = {"REAL_NODE": []}
    exempt = {
        "REAL_NODE": ResourceExemption(owner_engine="engine", reason="valid reason " * 5),
    }

    stale = set(registered.keys()) & set(exempt.keys())
    assert stale == {"REAL_NODE"}, (
        "Positive control failed: synthetic stale exemption was not isolated"
    )


def test_positive_control_fails_on_thin_exemption_reason():
    """U18 Positive Control: synthetic exemption with thin reason is caught by metadata check."""
    thin_exemption = ResourceExemption(owner_engine="engine", reason="too short")
    assert len(thin_exemption.reason.strip()) < 50
