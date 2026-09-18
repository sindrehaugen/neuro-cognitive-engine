"""Generated-vs-hand-written tool-name collision ratchet (janitor pass 7, K-H5).

Reported live by Lane E while building Wave E-6: declaring a C12 ``ResourceSpec``
with ``entity="resources"`` on the ``resources`` engine generates an MCP tool
named ``resources_list_resources`` -- which collides with, and would be
silently overwritten via ``TOOL_REGISTRY.update(build_all_resource_tool_specs())``
in ``nce/tool_registry.py``, the existing hand-written tool of that exact name
(``handle_resources_list_resources``). ``dict.update()`` clobbers without
complaint: no exception, no warning, just a working tool replaced by a
different one under the same name. ``get``/``upsert``/``archive`` for that
entity would NOT have collided (the existing hand-written tools use the
singular ``resources_get_resource`` etc.) -- only ``list`` would have, which
is exactly the kind of partial, count-preserving collision a numeric
tool-count check can never surface (the registry size doesn't even change).

Lane E caught this one by diffing the exact key set before/after registering
the spec and exempted the entity (see
``nce/vertical_modules/resources/resources.py``) rather than registering it.
That is a correct one-off fix, not an instrument: nothing stops the next
lane from doing the same thing for a different engine/entity pair. This file
is that instrument.

Detection approach: the hand-written tool names are read via AST from
``nce/tool_registry.py``'s literal ``TOOL_REGISTRY: dict[str, ToolSpec] = {...}``
declaration -- the same technique ``scripts/gen_engine_figures.py`` and
``scripts/gen_surface_table.py`` already use to read this exact dict without
executing the module. AST is required here, not the live ``TOOL_REGISTRY``
object: by the time any test runs, ``TOOL_REGISTRY.update(...)`` has already
happened, so a collision would have already silently occurred and the live
dict would show only the (wrong) C12 version under that name -- there would
be nothing left to diff against.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

os.environ.setdefault("NCE_MASTER_KEY", "x" * 32)

from nce.resource_surface import (
    build_all_resource_tool_specs,
    register_resource,
    unregister_resource,
)
from nce.resource_surface.spec import ResourceSpec

_ROOT = Path(__file__).resolve().parent.parent
_TOOL_REGISTRY_PATH = _ROOT / "nce" / "tool_registry.py"


def _hand_written_tool_names() -> frozenset[str]:
    tree = ast.parse(_TOOL_REGISTRY_PATH.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "TOOL_REGISTRY":
            if isinstance(node.value, ast.Dict):
                for k in node.value.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        names.add(k.value)
    return frozenset(names)


def test_no_c12_tool_name_collides_with_a_hand_written_tool() -> None:
    hand_written = _hand_written_tool_names()
    assert hand_written, (
        "Discovery floor: AST extraction of nce/tool_registry.py's TOOL_REGISTRY "
        "literal found zero hand-written tools -- the parser broke, not the estate."
    )

    c12_names = frozenset(build_all_resource_tool_specs())
    assert c12_names, (
        "Discovery floor: no C12 resource-surface tools found -- the "
        "registration import likely broke, not the estate having none."
    )

    collisions = hand_written & c12_names
    assert not collisions, (
        f"{len(collisions)} C12-generated tool name(s) collide with an existing "
        f"hand-written tool and would be silently overwritten by "
        f"TOOL_REGISTRY.update(build_all_resource_tool_specs()) in "
        f"nce/tool_registry.py: {sorted(collisions)}. Rename the colliding "
        f"ResourceSpec's entity, or exempt it in "
        f"nce/resource_surface/exemptions.py, per the RESOURCE precedent in "
        f"nce/vertical_modules/resources/resources.py."
    )


def test_collision_check_fires_on_a_synthetic_collision() -> None:
    """Positive control (K-H5): proves the check above is not vacuous by
    reproducing the exact collision Lane E found and avoided (RESOURCE
    entity="resources" -> resources_list_resources, colliding with the real
    hand-written tool of the same name) and confirming it would be caught."""
    hand_written = _hand_written_tool_names()
    assert "resources_list_resources" in hand_written, (
        "Positive-control precondition failed: the known hand-written "
        "collision target no longer exists in TOOL_REGISTRY -- pick a "
        "different, currently-real collision target for this control."
    )

    synthetic = ResourceSpec(
        engine="resources",
        entity="resources",
        node_type="K_H5_SYNTHETIC_RESOURCE_PROBE",
        storage_kind="kg_nodes",
    )
    register_resource(synthetic)
    try:
        c12_names = frozenset(build_all_resource_tool_specs())
        collisions = hand_written & c12_names
        assert "resources_list_resources" in collisions, (
            "The collision check failed to catch the exact known collision "
            "class (RESOURCE entity='resources') -- it is vacuous."
        )
        # get/upsert/archive must NOT collide -- the hand-written tools for
        # this entity use the singular "resource", not "resources". A control
        # that flagged these too would prove the check over-fires, not that
        # it correctly discriminates list from the other three verbs.
        assert "resources_get_resources" not in hand_written
        assert "resources_upsert_resources" not in hand_written
        assert "resources_archive_resources" not in hand_written
    finally:
        unregister_resource("resources", "resources")
