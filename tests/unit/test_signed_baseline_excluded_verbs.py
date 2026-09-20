"""
tests/unit/test_signed_baseline_excluded_verbs.py
====================================================
Coverage for SIGNED_BASELINE_SPEC's excluded_verbs
(nce/vertical_modules/sales/resources.py) -- the first spec-level test this
spec has ever had, exactly the gap test_system_design_resources.py's own
docstring identifies as how #311 shipped a real bug undetected.

Measured 2026-09-20: id_field="id" pointed at a BIGSERIAL column while every
generated create/patch/bulk path unconditionally fabricates a UUID string for
spec.id_field -- confirmed via grep the ONLY registered spec anywhere with a
non-UUID primary key, confirmed via do_freeze_baseline (sales/baseline.py)
using its own hand-written INSERT and never touching the generic route,
confirmed no caller wants generic write access. Fixed by excluding the write
verbs rather than patching the shared id-fabrication code for a route with
no caller -- reason (2) in ResourceSpec.excluded_verbs's own docstring
(storage permanently forbids the verb, cited by exact schema.sql line, not a
policy choice): the tenant-RLS grant loop restricts sales_signed_baselines to
GRANT SELECT, INSERT only.
"""

from __future__ import annotations

from nce.resource_surface import build_all_resource_routes, build_all_resource_tool_specs
from nce.vertical_modules.sales.resources import SIGNED_BASELINE_SPEC


def test_signed_baseline_id_field_points_at_a_non_uuid_primary_key() -> None:
    """Documents the fact that made excluded_verbs necessary here.

    Not asserting this is wrong -- id_field="id" is correct for get-by-id,
    which is all that survives. This pins the underlying fact so a future
    change to sales_signed_baselines' primary key type is forced to revisit
    this spec's excluded_verbs reasoning rather than silently drifting.
    """
    assert SIGNED_BASELINE_SPEC.id_field == "id"
    assert SIGNED_BASELINE_SPEC.table_name == "sales_signed_baselines"


def test_signed_baseline_excludes_write_verbs() -> None:
    assert SIGNED_BASELINE_SPEC.excluded_verbs == frozenset({"upsert", "archive"})


def test_signed_baseline_generates_only_list_and_get_mcp_tools() -> None:
    """Mutation-verify: the spec declaring excluded_verbs is not the same as
    the generator honoring it -- prove the actual generated tool set."""
    all_specs = build_all_resource_tool_specs()
    baseline_tools = {n for n in all_specs if "signed_baselines" in n}
    assert baseline_tools == {
        "sales_list_signed_baselines",
        "sales_get_signed_baselines",
    }


def test_signed_baseline_generates_no_write_rest_routes() -> None:
    """Mutation-verify on the REST side too -- no POST/PATCH/DELETE core
    route, only GET (list, get-by-id) plus the always-present sub-resource
    routes (events/comments/tags/documents), which excluded_verbs never
    touches."""
    routes = build_all_resource_routes()
    baseline_routes = [r for r in routes if "/signed-baselines" in r.path]
    core_paths = {"/api/sales/signed-baselines", "/api/sales/signed-baselines/{id}"}
    for route in baseline_routes:
        if route.path in core_paths:
            assert set(route.methods) <= {"GET", "HEAD"}, (
                f"{route.path} advertises {sorted(route.methods)}, expected only "
                "GET/HEAD -- upsert/archive must not generate a write route here"
            )
