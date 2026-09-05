"""Verify ``@claim:`` annotations against the AST, so load-bearing prose can go RED.

Why this exists
---------------
Negative claims about the call graph rot silently. A wave removes the last caller
of a function, someone writes "no production callers" in a docstring, a later wave
wires it up — and the docstring still says nobody calls it. That happened here:
``inventory/goods_receipt.py`` claimed the outbox registrars had "zero production
callers" **after** M0.W20d gave them callers, and nothing failed.

So a claim may opt in to being checked:

    # @claim: no-callers nce.vertical_modules.project.automation.register_engine

and this gate fails when the claim stops being true.

What it CANNOT do — read this before extending it
-------------------------------------------------
Roughly two of every five load-bearing claims in this repo are checkable this
way. It verifies **negative claims about the call graph**. It cannot verify
positive claims about runtime behaviour — "retried via the outbox", "runs
post-commit", "fires on every tick" — because those depend on execution order
and configuration, not on whether a symbol appears in a call position.

The vocabulary is ``no-callers`` (nowhere at all), ``no-production-callers``
(nowhere outside ``tests/`` — what this repo's claims actually assert) and
``unreferenced``.

``not-registered <event_type>`` is deliberately NOT implemented. ``OUTBOX_HANDLERS``
is empty at import time and populated only by boot registration, so a check run at
collection would report every event type as unregistered — a gate that passes for
the wrong reason. It is left out rather than approximated.

The failure mode to refuse is widening this to parse prose. That is how a gate
becomes brittle, starts crying wolf, and gets switched off. If a claim is not
expressible in the closed vocabulary below, leave it unannotated and rely on the
habit instead: ask what a change makes UNTRUE, repo-wide.

Vocabulary is CLOSED. An unrecognised verb is a failure, never a skip — a claim
the gate silently ignores is worse than no claim, because it reads as verified.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Directories scanned both for claims and for call sites.
_SCAN_DIRS = ("nce", "tests", "docs")

# ``# @claim: verb argument`` in Python, ``<!-- @claim: verb argument -->`` in Markdown.
_CLAIM_RE = re.compile(r"@claim:\s*(?P<verb>[a-z][a-z0-9-]*)\s+(?P<arg>[A-Za-z_][A-Za-z0-9_.]*)")

_IMPLEMENTED_VERBS = frozenset({"no-callers", "no-production-callers", "unreferenced"})


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


@dataclass(frozen=True)
class Claim:
    """One ``@claim:`` annotation, with the source location that asserted it."""

    verb: str
    arg: str
    path: Path
    line: int
    root: Path

    @property
    def leaf(self) -> str:
        """Final dotted segment — the name that appears at a call site."""
        return self.arg.rsplit(".", 1)[-1]

    def __str__(self) -> str:
        return f"{_rel(self.path, self.root)}:{self.line}: @claim: {self.verb} {self.arg}"


def _iter_source_files(root: Path, *, exclude: frozenset[Path] = frozenset()) -> list[Path]:
    """Python and Markdown files under the scanned directories.

    ``exclude`` exists for this module itself. Its docstring and its own tests
    contain illustrative ``@claim`` lines, and on the first run the gate read
    them as real claims and failed — the example verb ``definitely-true`` tripped
    the closed-vocabulary check and the example ``no-callers register_engine``
    tripped the staleness check, since that function does have callers. A scanner
    that reads its own documentation as input is measuring the wrong thing.
    """
    files: list[Path] = []
    for sub in _SCAN_DIRS:
        base = root / sub
        if not base.is_dir():
            continue
        for pattern in ("*.py", "*.md"):
            files.extend(
                p
                for p in base.rglob(pattern)
                if "__pycache__" not in p.parts and p.resolve() not in exclude
            )
    return sorted(files)


# This module's own examples must never be treated as claims about the repo.
_SELF = frozenset({Path(__file__).resolve()})


def _collect_claims(files: list[Path], root: Path) -> list[Claim]:
    claims: list[Claim] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "@claim:" not in text:
            continue
        for lineno, raw in enumerate(text.splitlines(), start=1):
            match = _CLAIM_RE.search(raw)
            if match is not None:
                claims.append(Claim(match.group("verb"), match.group("arg"), path, lineno, root))
    return claims


def _parse_python(files: list[Path]) -> dict[Path, ast.Module]:
    """Parse each Python file EXACTLY once.

    Parsing the same file twice yields structurally equal but distinct nodes, so
    any identity comparison across parses silently misbehaves. One parse, one
    tree, reused by every check.
    """
    trees: dict[Path, ast.Module] = {}
    for path in files:
        if path.suffix != ".py":
            continue
        try:
            trees[path] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
    return trees


def _call_sites(trees: dict[Path, ast.Module], leaf: str, root: Path) -> list[str]:
    """Real call expressions naming ``leaf``, as ``path:line`` strings.

    AST-only, deliberately: ``grep`` counts docstring mentions, import lines and
    the definition itself. ``register_automation_subscribers`` appears in prose in
    four files here, which is how "zero callers" claims were being made about
    functions that prose merely discussed.
    """
    hits: list[str] = []
    for path, tree in trees.items():
        rel = _rel(path, root)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name == leaf:
                hits.append(f"{rel}:{node.lineno}")
    return sorted(hits)


def _load_references(trees: dict[Path, ast.Module], leaf: str, root: Path) -> list[str]:
    """Loads of ``leaf`` that are neither its own definition nor an import alias."""
    hits: list[str] = []
    for path, tree in trees.items():
        rel = _rel(path, root)
        skip_lines: set[int] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
                and node.name == leaf
            ):
                skip_lines.add(node.lineno)
            elif isinstance(node, ast.alias) and (node.asname or node.name) == leaf:
                skip_lines.add(getattr(node, "lineno", -1))
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Attribute):
                name = node.attr
            if name == leaf and node.lineno not in skip_lines:
                hits.append(f"{rel}:{node.lineno}")
    return sorted(hits)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_every_claim_uses_the_closed_vocabulary() -> None:
    """An unrecognised verb fails; it is never skipped.

    A claim the gate ignores is worse than no claim at all, because a reader sees
    an annotation and infers it was checked.
    """
    files = _iter_source_files(_REPO_ROOT, exclude=_SELF)
    unknown = [
        str(c) for c in _collect_claims(files, _REPO_ROOT) if c.verb not in _IMPLEMENTED_VERBS
    ]
    assert not unknown, (
        "Unrecognised @claim verb(s). The vocabulary is closed to "
        f"{sorted(_IMPLEMENTED_VERBS)} — anything else is not verified by this "
        "gate, so it must not be written as though it were:\n  " + "\n  ".join(unknown)
    )


def test_no_callers_claims_hold() -> None:
    """``no-callers X`` must mean zero real call expressions naming X, anywhere."""
    files = _iter_source_files(_REPO_ROOT, exclude=_SELF)
    trees = _parse_python(files)
    failures: list[str] = []
    for claim in _collect_claims(files, _REPO_ROOT):
        if claim.verb != "no-callers":
            continue
        hits = _call_sites(trees, claim.leaf, _REPO_ROOT)
        if hits:
            failures.append(f"{claim}\n      called at: {', '.join(hits[:8])}")
    assert not failures, "Stale no-callers claim(s):\n  " + "\n  ".join(failures)


def test_no_production_callers_claims_hold() -> None:
    """``no-production-callers X`` must mean no call sites OUTSIDE ``tests/``.

    This verb exists because the claims in this repo say "no **production**
    callers", and measurement showed that is a different statement from "no
    callers": every dormancy symbol checked — ``rq_sync_bom_on_goods_receipt``,
    ``register_redis_client``, ``_enqueue_rq_task`` — has callers, all of them in
    ``tests/``. Verifying those claims with ``no-callers`` would have failed them
    for the wrong reason, and the natural next move (loosen the check) would have
    made it verify nothing. So the vocabulary gained a verb that says what the
    claims actually say.
    """
    files = _iter_source_files(_REPO_ROOT, exclude=_SELF)
    trees = _parse_python(files)
    failures: list[str] = []
    for claim in _collect_claims(files, _REPO_ROOT):
        if claim.verb != "no-production-callers":
            continue
        hits = [h for h in _call_sites(trees, claim.leaf, _REPO_ROOT) if not h.startswith("tests/")]
        if hits:
            failures.append(f"{claim}\n      called in production at: {', '.join(hits[:8])}")
    assert not failures, (
        "Stale no-production-callers claim(s) — something wired up code that is "
        "documented as dormant:\n  " + "\n  ".join(failures)
    )


def test_unreferenced_claims_hold() -> None:
    """``unreferenced X`` must mean X is never loaded outside its definition."""
    files = _iter_source_files(_REPO_ROOT, exclude=_SELF)
    trees = _parse_python(files)
    failures: list[str] = []
    for claim in _collect_claims(files, _REPO_ROOT):
        if claim.verb != "unreferenced":
            continue
        hits = _load_references(trees, claim.leaf, _REPO_ROOT)
        if hits:
            failures.append(f"{claim}\n      referenced at: {', '.join(hits[:8])}")
    assert not failures, "Stale unreferenced claim(s):\n  " + "\n  ".join(failures)


# ---------------------------------------------------------------------------
# Tests OF the gate — it must be able to go RED, or it gates nothing
# ---------------------------------------------------------------------------


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def test_gate_sees_a_real_call_and_would_fail(tmp_path: Path) -> None:
    """A claim contradicted by a real call must be reported, with its location."""
    _write(tmp_path, "nce/mod.py", "# @claim: no-callers widget\ndef widget():\n    return 1\n")
    _write(tmp_path, "nce/caller.py", "from nce.mod import widget\n\nwidget()\n")

    files = _iter_source_files(tmp_path)
    claims = _collect_claims(files, tmp_path)
    assert len(claims) == 1, f"expected one claim, got {claims}"

    hits = _call_sites(_parse_python(files), claims[0].leaf, tmp_path)
    assert hits, "gate failed to see a real call — it would pass a false claim"
    assert any(h.endswith("caller.py:3") for h in hits), hits


def test_gate_ignores_prose_and_imports(tmp_path: Path) -> None:
    """A docstring mention is not a call — this is why grep cannot do this job."""
    _write(
        tmp_path,
        "nce/mod.py",
        '"""Discusses widget() at length, mentions widget repeatedly."""\n'
        "# @claim: no-callers widget\n"
        "def widget():\n    return 1\n",
    )
    _write(tmp_path, "nce/importer.py", "from nce.mod import widget  # never called\n")

    files = _iter_source_files(tmp_path)
    assert _call_sites(_parse_python(files), "widget", tmp_path) == [], (
        "prose or an import was miscounted as a call site"
    )


def test_gate_rejects_an_unknown_verb(tmp_path: Path) -> None:
    """The closed vocabulary must reject, not skip."""
    _write(tmp_path, "nce/mod.py", "# @claim: definitely-true something\n")
    claims = _collect_claims(_iter_source_files(tmp_path), tmp_path)
    assert len(claims) == 1
    assert claims[0].verb not in _IMPLEMENTED_VERBS


def test_gate_reads_markdown_claims(tmp_path: Path) -> None:
    """Docs carry claims too, in HTML comments."""
    _write(tmp_path, "docs/page.md", "Prose.\n\n<!-- @claim: unreferenced legacy_helper -->\n")
    claims = _collect_claims(_iter_source_files(tmp_path), tmp_path)
    assert [(c.verb, c.arg) for c in claims] == [("unreferenced", "legacy_helper")]


def test_unreferenced_ignores_the_definition_itself(tmp_path: Path) -> None:
    """A function that only defines itself is genuinely unreferenced."""
    _write(
        tmp_path,
        "nce/mod.py",
        "# @claim: unreferenced legacy_helper\ndef legacy_helper():\n    return 2\n",
    )
    files = _iter_source_files(tmp_path)
    assert _load_references(_parse_python(files), "legacy_helper", tmp_path) == []

    _write(tmp_path, "nce/user.py", "from nce.mod import legacy_helper\n\nx = legacy_helper\n")
    files = _iter_source_files(tmp_path)
    assert _load_references(_parse_python(files), "legacy_helper", tmp_path), (
        "a bare reference (not a call) must still count as a reference"
    )
