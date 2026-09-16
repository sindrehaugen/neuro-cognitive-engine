"""The sweep must REFUSE to run as a role that cannot bypass row-level security (F10).

Why this file exists
--------------------
``scripts/rewrap_master_key.py`` issues a bare ``SELECT ... WHERE "<col>" IS NOT NULL`` per
registered column and **never sets a namespace context**. Five of the eight registered columns live
on tables with ``relforcerowsecurity`` -- ``memories``, ``pii_redactions``, ``signing_credentials``,
``bridge_subscriptions``, ``d365_integrations``. A role without ``BYPASSRLS`` therefore sees **zero
rows** in those five, with no error and no warning.

Measured on a restore of the live database, same database and same moment, by role:

===================================  ==================  ==================
                                     NOBYPASSRLS role    mcp_user (truth)
===================================  ==================  ==================
``pii_redactions`` rows seen         0                   4
blobs off the primary key            3                   7
actionable                           1                   5
===================================  ==================  ==================

So the sweep under-reports silently. With the two permanently-unopenable ``signing_keys`` rows
present it still exits 1, which masks the problem -- but once only FORCE-RLS columns hold
off-primary data (which is the state once the remaining registered columns start carrying rows, the same week as the master-key swap)
the restricted role gets a clean ``SAFE TO DROP NCE_MASTER_KEY_PREVIOUS`` **with exit 0** while blobs
remain wrapped under the old key. Dropping ``PREVIOUS`` there makes them permanently unopenable:
the exact outcome #122-#130 exist to prevent, reached through a green exit code.

``_preflight``'s column checks cannot catch it -- ``information_schema.columns`` is not RLS-filtered,
so they pass cleanly for a role that can see none of the data.

This is the live path, not a curiosity: runbook step 4 is ``--dsn <live>``, a placeholder the
operator fills in, and ``nce_app`` -- verified ``rolsuper=false rolbypassrls=false`` -- is the natural
DSN to reach for. The documented role happens to be safe; nothing made that a requirement.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from rewrap_master_key import _preflight  # noqa: E402


class _FakeConn:
    """Minimal asyncpg stand-in: answers the role probe, and reports no tables.

    Returning no rows from ``information_schema`` means the column checks contribute nothing,
    so any problem in the result is attributable to the RLS guard alone. That isolation is
    deliberate -- a fixture that also produced column problems could pass this test while the
    guard did nothing, which is the confounded-test failure I shipped on #127 and do not intend
    to repeat.
    """

    def __init__(self, *, can_bypass: bool | None, user: str = "probe_role") -> None:
        self._can_bypass = can_bypass
        self._user = user
        self.queries: list[str] = []

    async def fetchval(self, query: str, *_args: Any) -> Any:
        self.queries.append(query)
        if "rolbypassrls" in query:
            return self._can_bypass
        if "current_user" in query:
            return self._user
        raise AssertionError(f"unexpected fetchval: {query}")

    async def fetch(self, _query: str, *_args: Any) -> list[dict[str, Any]]:
        return []


def _rls_problems(problems: list[str]) -> list[str]:
    return [p for p in problems if "row-level security" in p]


@pytest.mark.asyncio
async def test_refuses_a_role_that_cannot_bypass_rls() -> None:
    """The F10 regression. If this goes green, the sweep can lie in the safe direction."""

    conn = _FakeConn(can_bypass=False, user="nce_app")
    problems = await _preflight(conn)  # type: ignore[arg-type]

    hits = _rls_problems(problems)
    assert hits, (
        "a role without BYPASSRLS must be REFUSED at pre-flight. Without this the sweep sees "
        "zero rows in five FORCE RLS columns and can report 'safe to drop "
        "NCE_MASTER_KEY_PREVIOUS' while blobs are still wrapped under the old key."
    )
    message = hits[0]
    assert "nce_app" in message, "must name the offending role so the operator can switch"
    assert "REFUSING TO RUN" in message
    assert "mcp_user" in message, "must name a role that works, not just refuse"


@pytest.mark.asyncio
async def test_allows_a_bypassrls_role() -> None:
    """The other direction: no false refusal for the documented role.

    A guard that refused everything would also pass the test above, so this is what makes the
    pair discriminating.
    """

    conn = _FakeConn(can_bypass=True, user="mcp_user")
    assert _rls_problems(await _preflight(conn)) == []  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_an_unresolvable_role_fails_CLOSED() -> None:
    """``NULL`` from the probe -- no matching ``pg_roles`` row -- must refuse, not proceed.

    Fail-open here would reintroduce the whole defect for any case where the role cannot be
    resolved, which is precisely the situation in which you least want to guess.
    """

    conn = _FakeConn(can_bypass=None, user="mystery")
    assert _rls_problems(await _preflight(conn)), "unknown privilege must fail closed"  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_guard_runs_BEFORE_any_column_work() -> None:
    """Ordering is load-bearing: the refusal must not depend on reaching the column loop.

    If the RLS probe ran after the per-column queries, a role lacking table privileges would
    surface as a column problem and the operator would be sent to fix the wrong thing.
    """

    conn = _FakeConn(can_bypass=False)
    await _preflight(conn)  # type: ignore[arg-type]
    assert conn.queries, "the guard issued no query at all"
    assert "rolbypassrls" in conn.queries[0], (
        f"the RLS probe must be the first query issued; got {conn.queries[0]!r}"
    )


# ---------------------------------------------------------------------------
# The ratchet (F11): the fix must not be re-losable by the next script
# ---------------------------------------------------------------------------
#
# F10 was found in one script. Looking for the same class elsewhere found it twice more --
# `rekey_master.py`, which overwrote the authoritative key file and whose own
# verify-before-commit read back only what RLS let it see, and `migrate_bridge_tokens.py`,
# which reports a clean "nothing to do". `rekey_master.py` was deleted on 2026-09-12 as a
# superseded single-key path, so two scripts remain and both are guarded. This ratchet is what
# stops a third from being written, because the defect is invisible in review: the code looks
# correct and the run reports success.


def _scripts_reading_wrapped_columns() -> dict[str, bool]:
    """Map script name -> whether it calls ``rls_visibility_problem``.

    A script is in scope if its **raw source** names a registered wrapped table. Raw source
    rather than string literals, because ``rewrap_master_key.py`` builds every query with an
    f-string from the registry (``FROM "{col.table}"``) and so contains no literal table name
    at all -- an earlier version of this scan matched ``ast.Constant`` values, found only two
    of the three scripts, and was caught by the discovery floor below. Matching raw source also
    catches a table named only in a comment, which is a false positive I want: anything working
    in this area should call the guard.
    """

    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from nce.master_key_registry import WRAPPED_COLUMNS

    tables = {column.table for column in WRAPPED_COLUMNS}
    assert tables, "registry produced no tables -- the scan cannot mean anything"

    found: dict[str, bool] = {}
    for path in sorted((root / "scripts").glob("*.py")):
        source = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - a broken script is another test's problem
            continue
        if not any(re.search(re.escape(table), source) for table in tables):
            continue
        found[path.name] = any(
            isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == "rls_visibility_problem")
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "rls_visibility_problem"
                )
            )
            for node in ast.walk(tree)
        )
    return found


def test_every_script_reading_a_wrapped_column_calls_the_rls_guard() -> None:
    """The F11 ratchet. Starts GREEN at three scripts, and that is the point."""

    found = _scripts_reading_wrapped_columns()
    unguarded = sorted(name for name, guarded in found.items() if not guarded)
    assert not unguarded, (
        f"these scripts read a master-key-wrapped column without calling "
        f"rls_visibility_problem(): {unguarded}. A role without BYPASSRLS sees ZERO rows in "
        "the five FORCE-RLS registered tables, so such a script reports success while leaving "
        "blobs wrapped under the old key. Call the guard immediately after connecting."
    )


def test_discovery_floor_for_the_script_scan() -> None:
    """Guard-the-guard: a scanner that finds nothing would pass the ratchet above.

    Measured 2026-09-12: exactly **two** scripts read a wrapped column --
    ``rewrap_master_key.py`` and ``migrate_bridge_tokens.py``.

    🔴 **This floor was lowered from 3 to 2, and the reason matters**, because lowering a
    guard-the-guard floor to make a test pass is normally the exact wrong move. It is
    legitimate here for one reason only: the third script, ``rekey_master.py``, was
    **deleted** on 2026-09-12 (superseded single-key rotation path that overwrote the
    authoritative key file). The population shrank; the scanner did not. If this number ever
    needs lowering again, check which script disappeared and why before touching it -- and if
    none did, the scanner is broken and the floor is telling you so.
    """

    found = _scripts_reading_wrapped_columns()
    assert len(found) >= 2, (
        f"AST discovery floor breached: expected >= 2 scripts reading a wrapped column, "
        f"found {len(found)} ({sorted(found)}). The scan is broken, not the estate."
    )


def test_positive_control_detects_an_unguarded_script(tmp_path: Path) -> None:
    """Prove the scan would catch a fourth offender, since the ratchet starts green."""

    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from nce.master_key_registry import WRAPPED_COLUMNS

    table = sorted({c.table for c in WRAPPED_COLUMNS})[0]
    synthetic = f'''
import asyncpg


async def go(conn):
    return await conn.fetch("SELECT id FROM {table} WHERE 1 = 0")
'''
    tree = ast.parse(synthetic)
    mentions = any(
        isinstance(n, ast.Constant) and isinstance(n.value, str) and table in n.value
        for n in ast.walk(tree)
    )
    guarded = any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "rls_visibility_problem"
        for n in ast.walk(tree)
    )
    assert mentions, "the scan's in-scope test failed to see a wrapped table name"
    assert not guarded, "the scan's guard test reported a guard that is not there"
