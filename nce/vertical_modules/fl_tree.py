"""nce.vertical_modules.fl_tree — Canonical Functional Location tree interface.

Phase C Wave C-1:
Exposes the functional location tree service for cross-engine consumption
and satisfies predicate P10 (git ls-tree origin/main -- nce/vertical_modules/ | grep -qE 'fl_tree|functional_location').
"""

from __future__ import annotations

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

__all__ = [
    "FLTreeError",
    "FLNodeNotFoundError",
    "CycleDetectedError",
    "InvalidMoveError",
    "MergeConflictError",
    "derive_fl_kind",
    "get_fl_node",
    "search_fl_nodes",
    "get_fl_children",
    "get_fl_ancestors",
    "get_fl_path",
    "move_fl_node",
    "merge_fl_nodes",
    "promote_fl_node",
    "FoldRules",
    "DEFAULT_FOLD_RULES",
    "normalize_name",
    "evaluate_fl_match",
]
