"""Producer-coverage census — declared states with nothing that produces them.

A per-wave gate structurally cannot see a **missing** wave.  All three known
instances of this class shipped green against their own wave's acceptance:
``132c`` (nothing produces the ``GOODS_RECEIPT.created`` selector), the
``BOM_LINE`` transitions that got a writer wave for ``DELIVERED`` only, and
``@governed``'s ``pending_approval``.  The hole lives *between* waves, so only
an estate-wide census finds it.  This module is that census: it does not fix
the backlog, it makes it visible, reasoned and non-growing.

**Dimension A — declared ``EventType`` values with no producer.**  Every value
in ``get_args(EventType)`` must appear as a *code* string literal somewhere
under ``nce/`` outside ``event_types.py`` (which declares them) and ``replay.py``
(which consumes them).  Anything else is a declared audit event that can never
be written.

**Dimension B — ownership transitions with no writer — deliberately NOT built.**
``node-ownership.json`` holds 49 rows, only **13** with a non-null
``transition``.  A literal search finds 10 of the 13 nowhere in ``nce/**.py``,
but that measurement is **false**: ``nce/bom_lines.py`` composes them at runtime
(``f"content:create:{flow}"``:216, ``f"content:update:{flow}"``:299,
``f"status:{status.lower()}"``:352), as does ``sales/lines.py:65``.  A
literal-only instrument cannot answer this dimension; a correct one must
propagate the *suffix* values real call sites supply into the f-string prefix —
a separate wave.  The naive version would seed a gate with 10 entries that are
mostly instrument error, i.e. a TODO list that gets switched off.  The real gap
(only ``status:delivered`` has a caller, ``inventory/triggers.py:1149``) stays
recorded as TDL Debt 20.

**Dimension C — ``pending_approval``.**  ``xfail(strict=True)``: RED on arrival
and correct.  See ``test_pending_approval_status_is_persisted``.

**Unobserved surfaces (audited per Phase 4 Wave T-5 / Charter §13 Question 3):**
1. *Dynamic interpolation*: Event types composed dynamically via f-strings or runtime concatenation
   (as documented under Dimension B) are invisible to literal extraction.
2. *Producers outside nce/*: Emitters residing in scripts, tests, or external adapters.
3. *Reflection emission*: Dynamic calls via `getattr(module, "append_event")` without direct function calls.

These are plain unit tests on purpose — they must run in the job that always
runs (``.github/workflows/ci.yml`` "Pytest (exclude integration)").
"""

from __future__ import annotations

import ast
import re
from functools import lru_cache
from pathlib import Path
from typing import get_args

from nce.event_types import EventType

_REPO_ROOT = Path(__file__).resolve().parent.parent
_NCE_DIR = _REPO_ROOT / "nce"

# ``event_types.py`` declares the values; ``replay.py`` dispatches on them.
# Neither is a producer, so neither counts as one.
_NON_PRODUCER_FILES = frozenset({"event_types.py", "replay.py"})

# Functions that write an ``event_log`` row.
_EMIT_FUNCTIONS = frozenset(
    {
        "append_event",
        "append_event_sync",
        "emit_event",
        "record_event",
        "log_event",
    }
)

# ---------------------------------------------------------------------------
# Allowlists.  ``{item: reason}``, never ``{item}``.
#
# An entry that cannot articulate why the absence is intentional is a FINDING,
# not an exemption.  ``KNOWN_UNWIRED`` in ``test_ci_integration_coverage.py``
# reached 110 entries with almost no reasons and a live defect hid behind one
# for days.  A bare name is a number with extra steps.  These lists may SHRINK
# freely; adding one means consciously accepting that a declared state can never
# be produced, so prefer wiring the producer instead.
# ---------------------------------------------------------------------------

KNOWN_UNPRODUCED_EVENT_TYPES: dict[str, str] = {
    "migration_started": (
        "LEGACY NAME, intentionally never emitted again. event_types.py marks the "
        "trio migration_started/committed/aborted 'legacy names — retained for "
        "historical event_log rows'; live code emits the pre-flight "
        "migration_start_requested instead. The Literal member exists so replay "
        "can read rows written before the rename. Removing it needs a paired "
        "change to nce/replay.py, which is a different wave."
    ),
    "migration_committed": (
        "LEGACY NAME — same rename as migration_started. Superseded by "
        "migration_commit_requested; retained only so replay can read old rows."
    ),
    "migration_aborted": (
        "LEGACY NAME — same rename as migration_started. Superseded by "
        "migration_abort_requested; retained only so replay can read old rows."
    ),
    "namespace_deletion_requested": (
        "UNPRODUCIBLE BY DESIGN, not a missing writer -- re-adjudicated "
        "2026-09-04 after a wave was dispatched to emit it and correctly "
        "REFUSED. There is no namespace soft-delete path: `namespaces` has no "
        "deleted_at/disabled/status column, and orchestrators/namespace.py's "
        "_delete_namespace is a literal `DELETE FROM namespaces`. Emitting "
        "before that delete is impossible, and the reason is already written in "
        "namespace.py: event_log.namespace_id is NOT NULL REFERENCES "
        "namespaces(id) with NO ON DELETE clause (so NO ACTION, unlike every "
        "sibling table on the teardown list, which cascade), and "
        "trg_event_log_worm (schema.sql:897) is BEFORE DELETE OR UPDATE FOR "
        "EACH ROW, so the audit row cannot be removed to satisfy the FK either. "
        "An emit inside the teardown transaction makes `DELETE FROM namespaces` "
        "fail and rolls the whole teardown back -- it would convert a working "
        "admin path into a PERMANENTLY undeletable namespace. An emit outside "
        "it splits audit from state, and would then trip the function's own "
        "PermissionError precondition (it already refuses any namespace with "
        "event_log rows) on every later delete attempt. Both are worse than no "
        "producer. The audit INTENT is already served: the soft path emits "
        "`namespace_disabled` (event_types.py:41, replay.py:1096), which does "
        "have a producer at namespace.py:236. So this type is a reserved name "
        "for a path that cannot exist. Disposition is Sindre's: retire the type "
        "(event_types.py + replay.py, which travel as a PAIR), or give "
        "event_log.namespace_id an archival story (nullable / ON DELETE SET "
        "NULL / tombstone row) in a schema wave that runs ALONE. Do NOT "
        "dispatch another emitter wave -- one already stopped here."
    ),
    "pii_redaction": (
        "TRUE POSITIVE, unfixed: declared for the 'PII / snapshot / cryptographic "
        "redaction probes' group; its siblings snapshot_created and unredact are "
        "both emitted, this one never is. The only occurrence estate-wide is an "
        "unrelated dict key in tests/test_event_types_contracts.py:45. Needs a "
        "redaction-path wave."
    ),
}

# Call sites that pass a *non-constant* ``event_type`` to an emitter.  These make
# the literal census unsound unless each one is a pass-through whose callers
# supply a literal.  A ``NAME = "literal"`` module constant is resolved
# automatically and never lands here.
KNOWN_DYNAMIC_EMIT_SITES: dict[str, str] = {
    "nce/a2a.py::_append_a2a_event": (
        "Pass-through helper: ``event_type: str`` is its own parameter, forwarded "
        "verbatim to append_event. Every caller supplies a literal, so the census "
        "still sees the value at the call site."
    ),
    "nce/auth.py::_write_audit_event": (
        "Pass-through helper: ``event_type`` is its own parameter, forwarded to "
        "append_event. Callers supply literals, which the census sees."
    ),
    "nce/migration_gate.py::audit_migration_action": (
        "Pass-through helper: ``event_type`` is its own parameter, forwarded to "
        "append_event. Callers supply literals, which the census sees."
    ),
    "nce/vertical_modules/marketing/events.py::emit_marketing_event": (
        "Pass-through helper: ``event_type`` is forwarded to append_event. Callers "
        "supply module constants or string literals, which the census discovers."
    ),
}


# ---------------------------------------------------------------------------
# Census machinery
# ---------------------------------------------------------------------------


def _declared_event_types() -> tuple[str, ...]:
    """The DERIVED truth: whatever ``EventType`` declares right now."""
    return tuple(get_args(EventType))


@lru_cache(maxsize=1)
def _producer_sources() -> tuple[tuple[str, str, ast.Module], ...]:
    """``(relpath, text, tree)`` for every candidate producer module."""
    out: list[tuple[str, str, ast.Module]] = []
    for path in sorted(_NCE_DIR.rglob("*.py")):
        if path.name in _NON_PRODUCER_FILES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(text)
        except SyntaxError:  # pragma: no cover - defensive
            continue
        rel = path.relative_to(_REPO_ROOT).as_posix()
        out.append((rel, text, tree))
    return tuple(out)


def _docstring_node_ids(tree: ast.Module) -> set[int]:
    """``id()`` of every string constant that is a bare statement.

    A mention in prose is not a producer.  Excluding bare-expression strings
    drops module, class and function docstrings in one rule.
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                ids.add(id(node.value))
    return ids


@lru_cache(maxsize=1)
def _code_string_literals() -> frozenset[str]:
    """Every string literal actually *used* as a value under ``nce/``."""
    found: set[str] = set()
    for _rel, _text, tree in _producer_sources():
        skip = _docstring_node_ids(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) not in skip:
                    found.add(node.value)
    return frozenset(found)


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` / ``NAME: str = "literal"``."""
    consts: dict[str, str] = {}
    for stmt in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(stmt, ast.Assign):
            targets, value = list(stmt.targets), stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets, value = [stmt.target], stmt.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            for tgt in targets:
                if isinstance(tgt, ast.Name):
                    consts[tgt.id] = value.value
    return consts


def _enclosing_functions(tree: ast.Module) -> dict[int, str]:
    """``id(node) -> nearest enclosing function name`` for every node."""
    owner: dict[int, str] = {}

    def walk(node: ast.AST, name: str) -> None:
        for child in ast.iter_child_nodes(node):
            child_name = name
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                child_name = child.name
            owner[id(child)] = child_name
            walk(child, child_name)

    walk(tree, "<module>")
    return owner


def _dynamic_emit_sites() -> dict[str, tuple[str, int]]:
    """Emitter calls whose ``event_type`` is neither a literal nor a constant."""
    sites: dict[str, tuple[str, int]] = {}
    for rel, _text, tree in _producer_sources():
        consts = _module_constants(tree)
        owners = _enclosing_functions(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            fname = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if fname not in _EMIT_FUNCTIONS:
                continue
            arg: ast.expr | None = None
            for kw in node.keywords:
                if kw.arg == "event_type":
                    arg = kw.value
            if arg is None or isinstance(arg, ast.Constant):
                continue
            if isinstance(arg, ast.Name) and arg.id in consts:
                continue
            key = f"{rel}::{owners.get(id(node), '<module>')}"
            sites[key] = (rel, node.lineno)
    return sites


def _unproduced_event_types() -> list[str]:
    literals = _code_string_literals()
    return sorted(t for t in _declared_event_types() if t not in literals)


def _file_writes_table_ast(tree: ast.Module, table: str) -> bool:
    """True if ``tree`` contains an actual (non-docstring) string constant that
    is an INSERT INTO / UPDATE against ``table``.

    AST-based, like Dimension A's ``_code_string_literals`` -- reuses
    ``_docstring_node_ids`` so a docstring (or, since ``ast`` never sees
    comments at all, a ``#`` comment either) mentioning the same SQL verb and
    table name in prose is never mistaken for a real write. A 2026-09-20
    mutation audit found the prior whole-file-text-regex version of this check
    could be satisfied by exactly such a prose mention (see
    ``test_sql_writers_of_ignores_docstring_only_mentions`` below for the
    reproduction).
    """
    pattern = re.compile(rf"\b(?:INSERT\s+INTO|UPDATE)\s+{re.escape(table)}\b", re.IGNORECASE)
    skip = _docstring_node_ids(tree)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in skip
            and pattern.search(node.value)
        ):
            return True
    return False


def _sql_writers_of(table: str) -> list[str]:
    """Modules under ``nce/`` containing an actual (non-docstring) INSERT/UPDATE
    against ``table``."""
    return sorted(
        rel for rel, _text, tree in _producer_sources() if _file_writes_table_ast(tree, table)
    )


# ---------------------------------------------------------------------------
# Dimension A
# ---------------------------------------------------------------------------


def test_census_discovery_floor() -> None:
    """A census that silently stops finding things reads as progress.

    Floors compare against DERIVED values, never hardcoded counts — a hardcoded
    number is something a later wave bumps to go green.
    """
    declared = _declared_event_types()
    sources = _producer_sources()
    assert declared, "get_args(EventType) returned nothing — the census has no subject"
    assert len(sources) >= len(declared), (
        f"discovery collapse: scanned only {len(sources)} producer modules under "
        f"nce/ for {len(declared)} declared event types"
    )
    literals = _code_string_literals()
    assert len(literals) >= len(declared), (
        f"discovery collapse: only {len(literals)} code string literals found "
        f"across {len(sources)} modules"
    )
    # Positive control: an event type that is definitely emitted must be seen.
    # If the scanner breaks, this fails before any absence is believed.
    assert "store_memory" in literals, (
        "positive control failed: 'store_memory' is emitted in production code "
        "but the literal scan did not see it — the instrument is broken"
    )
    assert _dynamic_emit_sites(), (
        "positive control failed: the dynamic-emitter scan found no pass-through "
        "call sites at all — the instrument is broken"
    )


def test_every_declared_event_type_has_a_producer() -> None:
    """Dimension A: a declared audit event nothing can write is a hole."""
    unproduced = _unproduced_event_types()
    unexpected = [t for t in unproduced if t not in KNOWN_UNPRODUCED_EVENT_TYPES]
    assert not unexpected, (
        "declared EventType values with NO producer anywhere under nce/ "
        f"(outside {sorted(_NON_PRODUCER_FILES)}): {unexpected}\n"
        "Either wire a producer, or add the value to "
        "KNOWN_UNPRODUCED_EVENT_TYPES *with a reason a reviewer can check*."
    )


def test_unproduced_allowlist_is_shrink_only_and_reasoned() -> None:
    """Every exemption must still be true, still be declared, and say why."""
    declared = set(_declared_event_types())
    unproduced = set(_unproduced_event_types())

    stale = sorted(set(KNOWN_UNPRODUCED_EVENT_TYPES) - declared)
    assert not stale, (
        f"allowlist names event types EventType no longer declares: {stale} — delete those entries"
    )

    now_produced = sorted(set(KNOWN_UNPRODUCED_EVENT_TYPES) - unproduced)
    assert not now_produced, (
        f"allowlist entries that gained a producer: {now_produced} — the list is "
        "shrink-only, so remove them"
    )

    for name, reason in KNOWN_UNPRODUCED_EVENT_TYPES.items():
        text = " ".join(reason.split())
        assert len(text) >= 60, f"{name}: reason too thin to review ({len(text)} chars)"
        assert text.replace(name, "").strip(), f"{name}: reason merely repeats the name"


def test_no_unreviewed_dynamic_event_type_emission() -> None:
    """Guard the instrument, not just the estate.

    ``append_event(event_type=some_var)`` is invisible to a literal census, so
    every such call site must be a reviewed pass-through whose callers supply a
    literal.  Without this, Dimension A could pass while an event type is
    emitted — or silently not emitted — through a variable.
    """
    sites = _dynamic_emit_sites()
    unreviewed = sorted(set(sites) - set(KNOWN_DYNAMIC_EMIT_SITES))
    assert not unreviewed, (
        "emitter call sites passing a non-literal event_type: "
        + ", ".join(f"{k} (line {sites[k][1]})" for k in unreviewed)
        + "\nThese make the Dimension A census unsound. Review each and record it "
        "in KNOWN_DYNAMIC_EMIT_SITES with a reason, or pass a literal."
    )
    gone = sorted(set(KNOWN_DYNAMIC_EMIT_SITES) - set(sites))
    assert not gone, (
        f"KNOWN_DYNAMIC_EMIT_SITES entries that no longer exist: {gone} — the "
        "list is shrink-only, so remove them"
    )
    for key, reason in KNOWN_DYNAMIC_EMIT_SITES.items():
        text = " ".join(reason.split())
        assert len(text) >= 60, f"{key}: reason too thin to review ({len(text)} chars)"


# ---------------------------------------------------------------------------
# Dimension C — pending_approval persisted in action_approval_queue (Wave B-B128)
# ---------------------------------------------------------------------------


def test_pending_approval_status_is_persisted() -> None:
    """If ``@governed`` returns ``pending_approval``, something must record it."""
    governor = _REPO_ROOT / "nce" / "autonomy" / "governor.py"
    source = governor.read_text(encoding="utf-8", errors="replace")
    assert '"pending_approval"' in source, (
        "premise gone: governor.py no longer returns a pending_approval status. "
        "If @governed stopped deferring actions, delete this test rather than "
        "un-xfailing it."
    )
    writers = _sql_writers_of("action_approval_queue")
    assert writers, (
        "governor.py returns 'pending_approval' but no module under nce/ writes "
        "action_approval_queue — the deferred action is announced and then lost."
    )


def test_sql_writers_of_ignores_docstring_only_mentions() -> None:
    """Regression for a 2026-09-20 mutation-audit finding on
    ``test_pending_approval_status_is_persisted``.

    ``_sql_writers_of`` used to regex the WHOLE FILE TEXT, so a bare docstring
    mentioning ``INSERT INTO <table>`` in prose (governor.py's own
    ``_record_pending_approval``-style docstring did exactly this) was
    indistinguishable from a real write. Proven by mutation: renaming every
    actual ``INSERT``/``UPDATE`` string against ``action_approval_queue`` in
    both ``nce/autonomy/governor.py`` and ``nce/admin_handlers/muscles.py``
    still left the old check green, because the docstring sentence alone
    satisfied the raw-text regex.

    This test locks in the AST-based fix (``_file_writes_table_ast``, reusing
    Dimension A's ``_docstring_node_ids`` exclusion): a docstring-only mention
    must NOT count as a writer, and a real write (a string literal actually
    passed to a call, not a bare docstring statement) still must.
    """
    docstring_only_src = '''
def f():
    """Mentions INSERT INTO mutation_probe_table in prose only, never executes it."""
    return 1
'''
    tree_docstring_only = ast.parse(docstring_only_src)

    real_write_src = '''
async def f(conn):
    await conn.execute("INSERT INTO mutation_probe_table (x) VALUES ($1)", 1)
'''
    tree_real_write = ast.parse(real_write_src)

    # Premise check: the OLD whole-file-text-regex approach WOULD have matched
    # the docstring-only source -- this is the exact bug, reproduced structurally.
    old_raw_text_pattern = re.compile(
        r"\b(?:INSERT\s+INTO|UPDATE)\s+mutation_probe_table\b", re.IGNORECASE
    )
    assert old_raw_text_pattern.search(docstring_only_src), (
        "premise check failed: the old regex-over-raw-text approach was expected "
        "to (incorrectly) match this docstring-only source"
    )

    # The FIXED, AST-based helper must not repeat that mistake.
    assert _file_writes_table_ast(tree_docstring_only, "mutation_probe_table") is False, (
        "a bare docstring mention of the SQL verb+table must NOT count as a real "
        "writer -- the ast-based exclusion regressed"
    )
    assert _file_writes_table_ast(tree_real_write, "mutation_probe_table") is True, (
        "a real INSERT/UPDATE string literal used as a value must still be "
        "detected -- the ast-based rewrite must not undercount real writers"
    )


# ---------------------------------------------------------------------------
# Phase 4 Wave T-5 Hardening: Positive Controls (U18)
# ---------------------------------------------------------------------------


def test_positive_control_fails_on_synthetic_unproduced_event() -> None:
    """Standing positive control (U18 / T-5 Q1): prove ratchet fails on unproduced event type."""
    unproduced = _unproduced_event_types()
    synthetic_unproduced = unproduced + ["synthetic_unproduced_event_type"]
    unexpected = [t for t in synthetic_unproduced if t not in KNOWN_UNPRODUCED_EVENT_TYPES]
    assert unexpected == ["synthetic_unproduced_event_type"], (
        "Positive control failed: ratchet did not isolate synthetic unproduced event"
    )


def test_positive_control_fails_on_synthetic_unreviewed_dynamic_site() -> None:
    """Standing positive control (U18 / T-5 Q1): prove dynamic scanner catches unreviewed emit sites."""
    sites = dict(_dynamic_emit_sites())
    sites["nce/synthetic_module.py::synthetic_function"] = ("nce/synthetic_module.py", 42)
    unreviewed = sorted(set(sites) - set(KNOWN_DYNAMIC_EMIT_SITES))
    assert "nce/synthetic_module.py::synthetic_function" in unreviewed, (
        "Positive control failed: dynamic emitter scanner did not isolate synthetic site"
    )
