"""tests/unit/test_po_line_excluded_verbs.py
==================================================
Coverage for PO_LINE_SPEC's excluded_verbs
(nce/vertical_modules/procurement/resources.py) -- mutation-verify style,
matching test_signed_baseline_excluded_verbs.py's precedent.

Two unrelated reasons for the two excluded verbs, per
ResourceSpec.excluded_verbs's own docstring:

"upsert" -- reason (3), a governed writer already owns the write path.
upsert_po_line_node (po_line.py:110, assert_owner at :137) and
update_po_line_status (po_line.py:266, assert_owner at :319) both compute
transition=f"status:{...}" explicitly and call assert_owner directly. The
generic route's create/patch/bulk never do this -- assert_owner is only
reachable inside rest.py/mcp.py's `if is_graph:` branches (rest.py:747/1043,
mcp.py:563), and is_graph is structurally False for every table-backed spec
(spec.tenant_scope derivation in spec.py), PO_LINE_SPEC included.

"archive" -- fits none of the three documented reasons. soft_delete_field=
None falls back to "is_archived" (rest.py:524/1174/1270), but
procurement_po_lines has no such column anywhere in schema.sql -- confirmed
1 of 25 registered specs with this exact shape (see
_internal/work-docs/mlv16-orchestration/ARCHIVE_COLUMN_SWEEP.md, local-only).
Excluded here to stop a 500, not as a precedent for a new reason.
"""

from __future__ import annotations

from nce.resource_surface import build_all_resource_routes, build_all_resource_tool_specs
from nce.vertical_modules.procurement.resources import PO_LINE_SPEC


def test_po_line_soft_delete_field_is_none_with_no_matching_column() -> None:
    """Documents the fact that made the archive exclusion necessary.

    Pins soft_delete_field=None so a future change to procurement_po_lines
    (e.g. adding a real is_archived column) is forced to revisit this
    spec's excluded_verbs reasoning rather than silently drifting.
    """
    assert PO_LINE_SPEC.soft_delete_field is None
    assert PO_LINE_SPEC.table_name == "procurement_po_lines"


def test_po_line_excludes_upsert_and_archive() -> None:
    assert PO_LINE_SPEC.excluded_verbs == frozenset({"upsert", "archive"})


def test_po_line_generates_only_list_and_get_mcp_tools() -> None:
    """Mutation-verify: the spec declaring excluded_verbs is not the same as
    the generator honoring it -- prove the actual generated tool set."""
    all_specs = build_all_resource_tool_specs()
    po_line_tools = {n for n in all_specs if "po_lines" in n}
    assert po_line_tools == {
        "procurement_list_po_lines",
        "procurement_get_po_lines",
    }


def test_po_line_generates_no_write_rest_routes() -> None:
    """Mutation-verify on the REST side too -- no POST/PATCH core route and
    no archive/restore route, only GET (list, get-by-id) plus the
    always-present sub-resource routes (events/comments/tags/documents),
    which excluded_verbs never touches."""
    routes = build_all_resource_routes()
    po_line_routes = [r for r in routes if "/po-lines" in r.path]
    core_paths = {"/api/procurement/po-lines", "/api/procurement/po-lines/{id}"}
    archive_restore_suffixes = ("/archive", "/restore")
    for route in po_line_routes:
        if route.path in core_paths:
            assert set(route.methods) <= {"GET", "HEAD"}, (
                f"{route.path} advertises {sorted(route.methods)}, expected only "
                "GET/HEAD -- upsert must not generate a write route here"
            )
        assert not route.path.endswith(archive_restore_suffixes), (
            f"{route.path} must not exist -- archive is excluded for PO_LINE_SPEC"
        )
