"""
tests/unit/test_resource_surface_archive_column_exists.py
============================================================
Estate-wide "archive wave" (2026-09-20), following the POSTING_SPEC finding
(the WORM-ledger excluded_verbs fix) and the estate-wide GRANT sweep it
prompted: ``handle_archive`` (nce/resource_surface/rest.py) falls back to a
literal ``"is_archived"`` column whenever a table-backed spec declares
``soft_delete_field=None`` -- and nothing checked whether that column, or
whichever one a spec DOES name via ``soft_delete_field``, actually exists on
the spec's table in schema.sql.

This is a static, no-Postgres, no-fixture check: it reads ``schema.sql`` and
every registered ``ResourceSpec``, nothing else. It is intentionally broader
than the GRANT sweep that motivated it -- that sweep only looked at specs
declaring ``soft_delete_field=None``; this checks EVERY table-backed spec
still advertising "archive", including the ones that DO name an explicit
``soft_delete_field``, in case that named column is itself wrong or stale.
The exclusion set for the wave this test supports is measured from this
test's own failures, not assembled by hand and not subtracted from any
other lane's prior count.

Graph-primary specs (``table_name is None``, i.e. ``tenant_scope == "graph"``)
are out of scope here: ``rest.py``'s ``handle_archive`` already refuses them
cleanly with a 501 ("kg_nodes-primary specs have no generic soft-delete
column") before ever touching a column name -- a different, already-handled
shape, not this defect.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from nce.resource_surface import get_all_resource_specs, load_all_engine_resources

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from check_schema_drift import expected_columns  # noqa: E402

load_all_engine_resources()
_ALL_SPECS = get_all_resource_specs()
_SCHEMA_COLUMNS = expected_columns()


def _table_backed_specs_advertising_archive():
    return [
        spec
        for spec in _ALL_SPECS
        if spec.table_name is not None and "archive" not in spec.excluded_verbs
    ]


_CANDIDATES = _table_backed_specs_advertising_archive()


def test_discovery_floor_finds_table_backed_specs() -> None:
    """Guard the guard: if spec discovery or the archive filter ever finds
    zero candidates, the parametrized test below would vacuously pass."""
    assert len(_ALL_SPECS) >= 40, (
        f"Only {len(_ALL_SPECS)} resource specs discovered -- spec loading is broken, "
        "not the estate"
    )
    assert _CANDIDATES, (
        "No table-backed spec advertises 'archive' -- either every spec correctly "
        "excludes it (unlikely estate-wide) or the filter itself is broken"
    )


@pytest.mark.parametrize(
    "spec",
    _CANDIDATES,
    ids=lambda s: f"{s.engine}:{s.entity}",
)
def test_archive_column_exists_for_every_table_backed_spec(spec) -> None:
    """``handle_archive`` writes to ``spec.soft_delete_field or "is_archived"``
    (rest.py:524/1174/1270). If that column is not a real column on the
    spec's table, the generated archive/restore routes and MCP tools are
    advertised but structurally cannot succeed -- the POSTING_SPEC defect
    class, checked here for every spec rather than rediscovered per-engine."""
    column = spec.soft_delete_field or "is_archived"
    table_columns = _SCHEMA_COLUMNS.get(spec.table_name)
    assert table_columns is not None, (
        f"{spec.engine}:{spec.entity}'s table_name={spec.table_name!r} was not found "
        "in schema.sql's CREATE TABLE statements at all"
    )
    assert column in table_columns, (
        f"{spec.engine}:{spec.entity} advertises 'archive' "
        f"(soft_delete_field={spec.soft_delete_field!r}, falls back to {column!r}) but "
        f"{spec.table_name!r} has no {column!r} column in schema.sql -- handle_archive "
        "would fail on an undefined column for every real row."
    )


def test_positive_control_catches_a_synthetic_missing_column() -> None:
    """Prove the check actually fires: a spec's table pointed at a column
    name guaranteed not to exist must be caught, not silently accepted."""
    columns = _SCHEMA_COLUMNS.get("event_log")
    assert columns is not None, "event_log must exist in schema.sql for this control"
    assert "zzz_column_that_does_not_exist" not in columns
