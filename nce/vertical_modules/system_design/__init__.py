"""
nce/vertical_modules/system_design/__init__.py
==============================================
System Design Engine vertical module (Module 3).

Exports public core functions for topology authoring, design proposals,
geometry, Lucidchart publishing, SOW generation, quote-design bidirectional sync,
standards reference data, signal distribution rules, and device capability sync.
"""

from __future__ import annotations

from nce.vertical_modules.system_design.capability_sync import (
    do_sync_device_capabilities,
)
from nce.vertical_modules.system_design.design_versions import (
    DesignNotFoundError,
    DesignVersionError,
    InvalidDesignError,
    InvalidRoomSpecError,
    canonical_design_label,
    create_design,
    get_active_design_for_fl,
    get_design,
    get_room_spec,
    list_designs,
    set_active_design,
    set_room_spec,
    update_design,
    validate_room_spec,
)
from nce.vertical_modules.system_design.devices import (
    do_author_device_topology,
)
from nce.vertical_modules.system_design.enrichment import (
    do_enrich_design_lines,
)
from nce.vertical_modules.system_design.fl_tree import (
    CycleDetectedError,
    FLNodeNotFoundError,
    FLTreeError,
    InvalidMoveError,
    MergeConflictError,
    derive_fl_kind,
    get_fl_ancestors,
    get_fl_children,
    get_fl_node,
    get_fl_path,
    merge_fl_nodes,
    move_fl_node,
    promote_fl_node,
    search_fl_nodes,
)
from nce.vertical_modules.system_design.fold_rules import (
    DEFAULT_FOLD_RULES,
    FoldRules,
    evaluate_fl_match,
    normalize_name,
)
from nce.vertical_modules.system_design.from_quote import (
    do_design_from_quote,
)
from nce.vertical_modules.system_design.geometry import (
    do_author_functional_location_geometry,
    do_author_geometry,
)
from nce.vertical_modules.system_design.graph import (
    do_author_functional_location,
    fl_label,
    upsert_fl_edge,
    upsert_fl_path,
)
from nce.vertical_modules.system_design.lucid import (
    do_publish_design_docs,
)
from nce.vertical_modules.system_design.procurement_view import (
    do_get_procurement_view,
)
from nce.vertical_modules.system_design.propose import (
    do_propose_design,
)
from nce.vertical_modules.system_design.read import (
    do_get_topology,
)
from nce.vertical_modules.system_design.retire import (
    do_retire_planned,
)
from nce.vertical_modules.system_design.room_categories import (
    ROOM_CATEGORIES,
    VALID_RESPONSIBLE_ROLES,
    InvalidResponsibleRoleError,
    RoomCategoryError,
    RoomCategoryNotFoundError,
    assign_fl_responsible,
    do_assign_fl_responsible,
    do_get_fl_room_category,
    do_get_room_categories,
    do_get_room_category,
    do_list_fl_responsible,
    do_list_my_responsible_fls,
    do_set_fl_room_category,
    do_unassign_fl_responsible,
    get_fl_room_category,
    get_room_category,
    list_fl_responsible,
    list_my_responsible_fls,
    list_room_categories,
    set_fl_room_category,
    unassign_fl_responsible,
)
from nce.vertical_modules.system_design.signal_distribution import (
    do_get_signal_rules,
)
from nce.vertical_modules.system_design.signal_flow import (
    do_inspect_signal_flow,
)
from nce.vertical_modules.system_design.sow import (
    do_generate_sow,
)
from nce.vertical_modules.system_design.standards import (
    do_get_standards,
)
from nce.vertical_modules.system_design.to_quote import (
    do_design_to_quote,
)
from nce.vertical_modules.system_design.validate import (
    do_validate_design,
)

__all__ = [
    "do_author_device_topology",
    "do_author_functional_location",
    "do_author_functional_location_geometry",
    "do_author_geometry",
    "do_design_from_quote",
    "do_design_to_quote",
    "do_enrich_design_lines",
    "do_generate_sow",
    "do_get_procurement_view",
    "do_get_signal_rules",
    "do_get_standards",
    "do_get_topology",
    "do_inspect_signal_flow",
    "do_propose_design",
    "do_publish_design_docs",
    "do_retire_planned",
    "do_sync_device_capabilities",
    "do_validate_design",
    "get_fl_node",
    "search_fl_nodes",
    "get_fl_children",
    "get_fl_ancestors",
    "get_fl_path",
    "move_fl_node",
    "merge_fl_nodes",
    "promote_fl_node",
    "derive_fl_kind",
    "FoldRules",
    "DEFAULT_FOLD_RULES",
    "evaluate_fl_match",
    "normalize_name",
    "FLTreeError",
    "FLNodeNotFoundError",
    "CycleDetectedError",
    "InvalidMoveError",
    "MergeConflictError",
    "upsert_fl_path",
    "upsert_fl_edge",
    "fl_label",
    "ROOM_CATEGORIES",
    "VALID_RESPONSIBLE_ROLES",
    "RoomCategoryError",
    "RoomCategoryNotFoundError",
    "InvalidResponsibleRoleError",
    "list_room_categories",
    "get_room_category",
    "set_fl_room_category",
    "get_fl_room_category",
    "assign_fl_responsible",
    "unassign_fl_responsible",
    "list_fl_responsible",
    "list_my_responsible_fls",
    "do_get_room_categories",
    "do_get_room_category",
    "do_set_fl_room_category",
    "do_get_fl_room_category",
    "do_assign_fl_responsible",
    "do_unassign_fl_responsible",
    "do_list_fl_responsible",
    "do_list_my_responsible_fls",
    "canonical_design_label",
    "validate_room_spec",
    "create_design",
    "get_design",
    "list_designs",
    "update_design",
    "set_active_design",
    "get_active_design_for_fl",
    "get_room_spec",
    "set_room_spec",
    "DesignVersionError",
    "DesignNotFoundError",
    "InvalidDesignError",
    "InvalidRoomSpecError",
]
