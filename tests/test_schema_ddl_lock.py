"""Every DDL transaction in startup must hold the schema advisory lock.

On 2026-08-27, `nce-admin` crash-looped 10 times with

    asyncpg.exceptions.InternalServerError: tuple concurrently updated

raised from ``_init_pg_schema`` while applying ``schema.sql``. The cause was not
that batch: it takes ``pg_advisory_xact_lock``. It was the *other* transaction in
the same method — the ``ALTER ROLE nce_app WITH LOGIN PASSWORD`` refresh — which
took no lock at all. ``schema.sql`` itself CREATE/ALTER ROLEs ``nce_app`` (lines
18-30), so one process applying the schema and another refreshing the password
update the same ``pg_authid`` tuple concurrently. Postgres does not serialise
concurrent catalog updates on a shared catalog; it raises.

Five services boot this path at once, each twice now that an entrypoint
pre-flight runs before the workers, so an unlocked DDL site is reached readily.

These tests assert the structural property rather than the wording: every
``async with conn.transaction():`` block inside the startup DDL methods must take
the advisory lock as its first statement, and every lock site must use the one
shared constant so a future site cannot pick a different number.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

import nce.orchestrator as orchestrator_module
from nce.orchestrator import SCHEMA_ADVISORY_LOCK_ID

# Startup methods that issue DDL and therefore must serialise against each other.
_DDL_METHODS = ("_init_pg_schema", "_apply_pg_migrations")


def _method_tree(name: str) -> ast.AsyncFunctionDef:
    source = textwrap.dedent(inspect.getsource(getattr(orchestrator_module.NCEEngine, name)))
    (func,) = [n for n in ast.parse(source).body if isinstance(n, ast.AsyncFunctionDef)]
    return func


def _is_transaction_block(node: ast.AST) -> bool:
    """True for ``async with conn.transaction():`` (any connection variable)."""
    if not isinstance(node, ast.AsyncWith):
        return False
    for item in node.items:
        call = item.context_expr
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "transaction"
        ):
            return True
    return False


def _takes_the_lock(stmt: ast.AST) -> bool:
    """True for ``await conn.execute(_ADVISORY_LOCK_SQL, SCHEMA_ADVISORY_LOCK_ID)``."""
    if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Await):
        return False
    call = stmt.value.value
    if not isinstance(call, ast.Call):
        return False
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "execute"):
        return False
    rendered = " ".join(ast.dump(a) for a in call.args)
    return "ADVISORY_LOCK" in rendered or "pg_advisory_xact_lock" in rendered


def _transaction_blocks(name: str) -> list[ast.AsyncWith]:
    return [n for n in ast.walk(_method_tree(name)) if _is_transaction_block(n)]


class TestEveryDdlTransactionTakesTheLock:
    """The bug was one transaction out of four that did not."""

    @pytest.mark.parametrize("method", _DDL_METHODS)
    def test_method_has_transaction_blocks_to_check(self, method: str) -> None:
        """Guard the guard: a refactor must not make this pass vacuously."""
        assert _transaction_blocks(method), f"{method} has no transaction block to check"

    @pytest.mark.parametrize("method", _DDL_METHODS)
    def test_the_lock_is_the_first_statement_of_every_transaction(self, method: str) -> None:
        """First statement, not merely present: DDL before it is unprotected."""
        for block in _transaction_blocks(method):
            assert block.body, f"{method}: empty transaction block"
            assert _takes_the_lock(block.body[0]), (
                f"{method}: a transaction block at line {block.lineno} does not take the "
                "schema advisory lock as its first statement — concurrent boots will "
                'raise "tuple concurrently updated"'
            )

    def test_the_role_password_refresh_is_covered(self) -> None:
        """The exact site that crash-looped nce-admin, named so it stays covered."""
        blocks = _transaction_blocks("_init_pg_schema")
        role_blocks = [
            b
            for b in blocks
            if "ALTER ROLE" in " ".join(ast.dump(n) for n in ast.walk(b))
            or "temp_password" in " ".join(ast.dump(n) for n in ast.walk(b))
        ]
        assert role_blocks, "the nce_app password refresh transaction is gone or renamed"
        for block in role_blocks:
            assert _takes_the_lock(block.body[0])


class TestOneSharedLockId:
    """A second DDL site picking a different number fails the same way."""

    def test_the_constant_is_exported(self) -> None:
        assert isinstance(SCHEMA_ADVISORY_LOCK_ID, int)

    def test_no_startup_ddl_site_hardcodes_the_lock_id(self) -> None:
        """A literal is how a future site drifts onto its own lock."""
        source = Path(inspect.getfile(orchestrator_module)).read_text(encoding="utf-8")
        occurrences = source.count(str(SCHEMA_ADVISORY_LOCK_ID))
        assert occurrences == 1, (
            f"{SCHEMA_ADVISORY_LOCK_ID} appears {occurrences} times; it must be defined "
            "once as SCHEMA_ADVISORY_LOCK_ID and referenced by name"
        )

    def test_the_schema_really_does_touch_roles(self) -> None:
        """The premise of the whole fix: schema.sql writes pg_authid.

        If this ever stops being true the lock is still harmless, but the
        reasoning recorded above would be stale.
        """
        schema = (Path(inspect.getfile(orchestrator_module)).parent / "schema.sql").read_text(
            encoding="utf-8"
        )
        assert "ALTER ROLE nce_app" in schema
