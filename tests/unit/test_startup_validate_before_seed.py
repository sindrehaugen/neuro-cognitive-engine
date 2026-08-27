"""Startup-order invariant: ``connect()`` must validate before it mutates.

Regression guard for the 2026-08-27 crash-loop incident (fix recipe #2).
``NCEEngine.connect`` seeded ``node_ownership_registry`` for every namespace
*before* running the WORM/RLS enforcement checks, so a version-skewed
deployment -- an image whose ``EXPECTED_TENANT_RLS_TABLES`` predates the live
database -- paid the entire mutating backfill and only then refused to start.
Under ``uvicorn --workers N`` that doomed startup ran once per respawned worker,
forever, which is what saturated the database.

The enforcement checks are cheap and read-only; the backfill is expensive and
mutating. Ordering the cheap check first turns a multi-minute doomed startup
into a ~1 s failure. Seeding depends only on schema + migrations having run, not
on verification order, so the swap is safe.

Asserted against the AST of ``connect()`` rather than its text, so reformatting
does not break the guard while swapping the calls back does.

Complements ``tests/unit/test_ownership_seed_startup_scaling.py``, which pins
the *cost* of the seed step (O(1) statements); this pins its *position*.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import nce.orchestrator as orchestrator_module

_SEED = "_seed_node_ownership_all"
_VERIFY_WORM = "_verify_worm_enforcement"
_VERIFY_RLS = "_verify_rls_enforcement"
_MIGRATIONS = "_apply_pg_migrations"
_SCHEMA = "_init_pg_schema"


def _connect_self_call_order() -> list[str]:
    """Names of the ``await self.<method>()`` calls in ``connect()``, in order."""
    source = textwrap.dedent(inspect.getsource(orchestrator_module.NCEEngine.connect))
    (func,) = [n for n in ast.parse(source).body if isinstance(n, ast.AsyncFunctionDef)]

    names: list[str] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Await):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        fn = call.func
        if (
            isinstance(fn, ast.Attribute)
            and isinstance(fn.value, ast.Name)
            and fn.value.id == "self"
        ):
            names.append(fn.attr)
    return names


class TestStartupOrder:
    """``connect()`` must verify enforcement before it seeds ownership."""

    def test_connect_calls_every_step_this_module_asserts_on(self) -> None:
        """Guard the guard: a renamed step must fail loudly, not pass vacuously."""
        order = _connect_self_call_order()
        for name in (_SCHEMA, _MIGRATIONS, _VERIFY_WORM, _VERIFY_RLS, _SEED):
            assert name in order, f"{name} is no longer called from connect(): {order}"

    def test_worm_verification_runs_before_the_ownership_seed(self) -> None:
        order = _connect_self_call_order()
        assert order.index(_VERIFY_WORM) < order.index(_SEED), (
            f"{_VERIFY_WORM} must run before {_SEED}; got {order}"
        )

    def test_rls_verification_runs_before_the_ownership_seed(self) -> None:
        """The check that actually fails on version skew must come first."""
        order = _connect_self_call_order()
        assert order.index(_VERIFY_RLS) < order.index(_SEED), (
            f"{_VERIFY_RLS} must run before {_SEED}; got {order}"
        )

    def test_ownership_seed_still_runs_after_schema_and_migrations(self) -> None:
        """Seeding writes to a migrated table, so it must stay downstream of both."""
        order = _connect_self_call_order()
        seed = order.index(_SEED)
        assert order.index(_SCHEMA) < seed, order
        assert order.index(_MIGRATIONS) < seed, order

    def test_seed_docstring_no_longer_claims_it_follows_migrations_directly(self) -> None:
        """Documentation and control flow must not disagree about the order."""
        doc = inspect.getdoc(orchestrator_module.NCEEngine._seed_node_ownership_all)
        assert doc is not None
        assert "immediately after migrations are applied" not in doc
