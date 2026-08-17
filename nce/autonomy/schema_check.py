"""
Autonomy schema verification (C2 ground-truth check).

Verifies that the three tables added by migration 022_muscles_schema_contract
are present and contain their key columns before the Wave-15 governor is
allowed to run.  This module raises on any absent object and never authors DDL.

A second pass confirms that ``action_idempotency`` carries a PRIMARY KEY (or
UNIQUE) constraint covering ``(namespace_id, idempotency_key)``.  Without that
constraint the governor could insert duplicate idempotency keys, breaking the
idempotency guarantee even when the column exists.
"""

from __future__ import annotations

import asyncpg  # type: ignore[import-untyped]

# ---------------------------------------------------------------------------
# Sentinel: the exact (table, column) pairs migration 022 must have created.
# Keyed by table name; values are the single key column whose presence
# proves the table is structurally sound for the governor to consume.
# ---------------------------------------------------------------------------
_REQUIRED: dict[str, str] = {
    "action_approval_queue": "status",
    "action_idempotency": "idempotency_key",
    "processed_outbox_events": "event_id",
}

# The two columns that together form the idempotency PK/UNIQUE constraint.
_IDEMPOTENCY_TABLE = "action_idempotency"
_IDEMPOTENCY_CONSTRAINT_COLS: frozenset[str] = frozenset({"namespace_id", "idempotency_key"})

_MISSING_CAUSE = (
    "migration 022_muscles_schema_contract.sql has not been applied — "
    "merge C0/022 and restart the pool before running C2 components"
)

_MISSING_CONSTRAINT_CAUSE = (
    "action_idempotency has no PRIMARY KEY or UNIQUE constraint covering "
    "(namespace_id, idempotency_key) — migration 022_muscles_schema_contract.sql "
    "may not have been applied correctly; merge C0/022 and restart the pool"
)


async def check_autonomy_schema(conn: asyncpg.Connection) -> None:
    """Assert all three C2 autonomy tables and their key columns exist.

    Also confirms that ``action_idempotency`` carries a PRIMARY KEY or UNIQUE
    constraint on ``(namespace_id, idempotency_key)`` so the governor's
    duplicate-prevention guarantee is structurally enforced, not merely hoped
    for via column presence alone.

    Queries ``information_schema`` (catalogue read — no RLS context required).
    Raises ``RuntimeError`` for each absent object, naming the missing
    table/column/constraint and the root cause.

    Args:
        conn: An open asyncpg connection (any role that can read
              ``information_schema``).

    Raises:
        RuntimeError: When any required table, its key column, or the
                      idempotency constraint is absent.
    """
    # ------------------------------------------------------------------
    # 1. Column-existence check (three tables, three key columns).
    # ------------------------------------------------------------------
    col_rows: list[asyncpg.Record] = await conn.fetch(
        """
        SELECT table_name, column_name
        FROM   information_schema.columns
        WHERE  table_schema = 'public'
          AND  table_name   = ANY($1::text[])
        """,
        list(_REQUIRED.keys()),
    )

    # Build a set of (table, column) pairs present in the live schema.
    present: set[tuple[str, str]] = {(r["table_name"], r["column_name"]) for r in col_rows}

    for table, key_column in _REQUIRED.items():
        # Confirm the table itself has *any* rows in information_schema
        # (i.e. the table exists at all).
        table_exists = any(t == table for t, _ in present)
        if not table_exists:
            raise RuntimeError(
                f"Required autonomy table 'public.{table}' is absent — {_MISSING_CAUSE}"
            )

        if (table, key_column) not in present:
            raise RuntimeError(
                f"Required column '{key_column}' missing from 'public.{table}' — {_MISSING_CAUSE}"
            )

    # ------------------------------------------------------------------
    # 2. Constraint check: action_idempotency PK/UNIQUE on
    #    (namespace_id, idempotency_key).
    #
    #    We look up every PRIMARY KEY and UNIQUE constraint on the table,
    #    collect their covered columns, and confirm at least one such
    #    constraint covers exactly (or at least) both required columns.
    #
    #    Parameterised query — no f-string identifier interpolation.
    # ------------------------------------------------------------------
    constraint_rows: list[asyncpg.Record] = await conn.fetch(
        """
        SELECT kcu.constraint_name,
               kcu.column_name
        FROM   information_schema.table_constraints  tc
        JOIN   information_schema.key_column_usage   kcu
               ON  kcu.constraint_name = tc.constraint_name
               AND kcu.table_schema    = tc.table_schema
               AND kcu.table_name      = tc.table_name
        WHERE  tc.table_schema    = 'public'
          AND  tc.table_name      = $1
          AND  tc.constraint_type = ANY($2::text[])
        """,
        _IDEMPOTENCY_TABLE,
        ["PRIMARY KEY", "UNIQUE"],
    )

    # Group columns by constraint name.
    constraints: dict[str, set[str]] = {}
    for row in constraint_rows:
        constraints.setdefault(row["constraint_name"], set()).add(row["column_name"])

    # At least one constraint must cover both required columns.
    idempotency_constrained = any(
        _IDEMPOTENCY_CONSTRAINT_COLS <= cols for cols in constraints.values()
    )
    if not idempotency_constrained:
        raise RuntimeError(
            f"'public.{_IDEMPOTENCY_TABLE}' is missing a PRIMARY KEY or UNIQUE constraint "
            f"covering {sorted(_IDEMPOTENCY_CONSTRAINT_COLS)} — {_MISSING_CONSTRAINT_CAUSE}"
        )
