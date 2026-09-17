"""RL-H18 — the WORM guard must refuse TRUNCATE, not just UPDATE and DELETE.

`trg_event_log_worm` and `trg_event_parents_worm` were declared
``BEFORE UPDATE OR DELETE ... FOR EACH ROW``. A row-level trigger is invoked once per
affected row, and TRUNCATE never visits rows — it deallocates the table's storage. So it
fired no trigger at all, and either append-only hash-chained table could be emptied in a
single statement with the guard silent.

Not theoretical here: the connecting role is ``rolsuper``/``rolbypassrls``, so it holds
TRUNCATE on both tables and RLS does not constrain it.

These tests run against a real Postgres. A mock cannot answer this question — the defect
was *in the database's own trigger catalogue*, and every Python-level assertion about WORM
passed happily while TRUNCATE went straight through.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_WORM_TABLES = ("event_log", "event_parents")


@pytest.mark.parametrize("table", _WORM_TABLES)
async def test_truncate_is_refused(pg_pool, table: str) -> None:
    """🔴 The positive control. This is the operation that used to succeed.

    If this test ever passes-by-not-raising, the guard is gone and the audit trail can be
    erased in one statement.
    """
    async with pg_pool.acquire() as conn:
        with pytest.raises(Exception) as exc:
            await conn.execute(f"TRUNCATE TABLE {table}")

    msg = str(exc.value)
    assert "forbidden" in msg.lower() or "immutable" in msg.lower(), (
        f"TRUNCATE on {table} raised something other than the WORM guard: {msg!r}. "
        "It must be refused by prevent_mutation(), not by a permissions accident."
    )
    assert "TRUNCATE" in msg.upper(), (
        f"the refusal should name the operation so an operator can see what was blocked; got {msg!r}"
    )


@pytest.mark.parametrize("table", _WORM_TABLES)
async def test_statement_level_truncate_trigger_exists(pg_pool, table: str) -> None:
    """Assert the trigger's *shape* in the catalogue, not just the behaviour.

    A ``BEFORE TRUNCATE`` trigger must be ``FOR EACH STATEMENT``. PostgreSQL rejects a row
    trigger on TRUNCATE outright, so a wrong-shaped one cannot exist — but pinning the row
    here means a future migration that "simplifies" the guard back to a single row trigger
    fails this test instead of silently reopening the hole.
    """
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT t.tgname,
                   (t.tgtype & 32) <> 0 AS on_truncate,
                   (t.tgtype & 1)  <> 0 AS for_each_row
            FROM   pg_trigger t
            JOIN   pg_class   c ON c.oid = t.tgrelid
            WHERE  c.relname = $1
              AND  NOT t.tgisinternal
              AND  (t.tgtype & 32) <> 0
            """,
            table,
        )

    assert row is not None, (
        f"{table} has no TRUNCATE trigger. The row-level WORM trigger cannot fire on "
        "TRUNCATE — see migration 080."
    )
    assert row["for_each_row"] is False, (
        f"{table}'s TRUNCATE trigger claims FOR EACH ROW, which cannot fire on TRUNCATE"
    )


@pytest.mark.parametrize("table", _WORM_TABLES)
async def test_the_original_row_triggers_survive(pg_pool, table: str) -> None:
    """Migration 080 must ADD a guard, never replace one.

    Asserted from the catalogue rather than by attempting a DELETE: a row-level trigger
    fires per affected row, so ``DELETE ... WHERE false`` touches nothing and raises
    nothing. That is correct behaviour, and an earlier version of this test read it as a
    missing guard — the test was wrong, not the trigger.
    """
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT (t.tgtype & 1)  <> 0 AS for_each_row,
                   (t.tgtype & 8)  <> 0 AS on_delete,
                   (t.tgtype & 16) <> 0 AS on_update
            FROM   pg_trigger t
            JOIN   pg_class   c ON c.oid = t.tgrelid
            WHERE  c.relname = $1
              AND  NOT t.tgisinternal
              AND  (t.tgtype & 32) = 0
            """,
            table,
        )

    assert row is not None, f"{table} lost its row-level WORM trigger"
    assert row["for_each_row"], "the UPDATE/DELETE guard must stay FOR EACH ROW"
    assert row["on_delete"] and row["on_update"], (
        f"{table}'s row trigger must still cover both UPDATE and DELETE"
    )
