"""CI integration-coverage ratchet.

``ci.yml`` runs integration tests from a **hardcoded file allowlist**, while the
unit job selects ``-m "not integration and not perf"``.  Anything carrying an
``@pytest.mark.integration`` marker and absent from that allowlist therefore runs
in **no CI job at all** -- it is written, it passes locally, and it gates nothing.

That has already happened twice.  The comment above the M3 Agreements step in
``.github/workflows/ci.yml`` records it ("these 2 757 lines of tests previously ran
in NO CI job ... so the '90 M3 tests green' claim gated nothing"), and it was fixed
for those four files only.  Batch 122 hit it again.  When this ratchet was added,
120 of 131 files carrying integration markers were unwired -- 362 marked tests.

This module does not fix that backlog.  Most of those files need services CI does
not currently start, so retiring them is an incremental project.  What it does is
make the backlog **visible and non-growing**: a newly added integration file must
either be wired into a workflow or consciously listed in ``KNOWN_UNWIRED``, and an
entry that stops being accurate must be removed.

"Wired" throughout this module means **the file is named in a workflow ``run:``
scalar** -- a step CI executes passes that path to pytest.  It deliberately does not
mean "mentioned somewhere in a workflow file": until Batch 152d the token scan
regexed the raw workflow text, so a comment naming a test counted as wiring.

That gloss is **file-level, and stops there**.  ``::nodeid`` selections are not
resolved -- the token regex stops at ``.py`` -- so a file whose only appearance in a
``run:`` is a nodeid selection is recorded as wired even when the tests actually
selected are not the marked ones.  ``tests/test_worm_registry.py`` is exactly that
case today: ``ci.yml`` lines 189-190 name it only as
``::test_memory_salience_not_in_worm_tables`` and
``::test_worm_tables_contains_expected_entries``, **neither of which is marked**,
while the two that ARE marked (``test_worm_tables_db_role_cannot_update`` at line 38
and ``test_worm_tables_db_role_cannot_delete`` at line 69) are never named.  Running
precisely what ci.yml selects, under ``-m integration``, measures ``2 deselected``
and exit 5 in isolation -- inside the multi-file step it is silent.  This module
nonetheless records that file as ``wired=True marked=True``.  The ci.yml hole is
registered as its own wave and is deliberately NOT fixed here.  See
``_workflow_tokens``.

These are plain unit tests on purpose -- they must run in the job that always runs.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TESTS_DIR = _REPO_ROOT / "tests"
_WORKFLOWS_DIR = _REPO_ROOT / ".github" / "workflows"

# Files carrying integration markers that no CI workflow currently runs.
#
# This list may SHRINK freely.  Adding to it means consciously accepting that a
# test gates nothing -- do that only with a reason, and prefer wiring the file
# into the integration job in ci.yml instead.
KNOWN_UNWIRED: frozenset[str] = frozenset(
    {
        "tests/diagnostics/test_diag_worker.py",
        "tests/diagnostics/test_diagnostic_ingest.py",
        "tests/diagnostics/test_digest_writer.py",
        "tests/test_batch49_pii_derivation.py",
        "tests/test_c4_donewhen.py",
        "tests/test_chain_and_decay_integration.py",
        "tests/test_cron_chain_verify.py",
        "tests/test_dlq_triage.py",
        "tests/test_echo_suppression.py",
        "tests/test_emit_on_graph_write.py",
        "tests/test_envelope_encryption_integration.py",
        "tests/test_envelope_read_consumers.py",
        "tests/test_event_retention.py",
        "tests/test_explain_past_decision.py",
        "tests/test_garbage_collector.py",
        "tests/test_governed_decorator.py",
        "tests/test_me_app.py",
        "tests/test_product_enrich.py",
        "tests/test_product_ingestion.py",
        "tests/test_replay_handlers_integration.py",
        "tests/test_sales_graph.py",
        "tests/test_sales_public_quote.py",
        "tests/test_sales_sign_to_project.py",
        "tests/test_shred_memory_integration.py",
        "tests/test_snapshot_mcp_handlers.py",
        "tests/test_tamper_anchor.py",
        "tests/test_vendors_cert_watcher.py",
        "tests/test_vendors_registry.py",
        "tests/test_webhook_clientstate.py",
        "tests/test_worker_inflight_recovery.py",
    }
)

# Files a workflow glob matches that carry NO integration marker by design.
#
# The converse ratchet below requires every globbed file to contribute a marked
# test.  A prefix glob legitimately spans both kinds of file, so that rule needs
# an escape hatch -- but a NAMED one.  An entry here is a coder stating out loud
# that this file needs no database and belongs in the unit job, which a reviewer
# can check.  That is the whole difference between this and a count: a number is
# something you bump to go green, a filename is something you have to justify.
#
# This list may SHRINK freely.  Adding to it is a conscious admission.
GLOBBED_UNMARKED_BY_DESIGN: frozenset[str] = frozenset(
    {
        # Pure-unit A2A/seam test for Module 9.Wave 7; mocks the DB boundary and
        # says so in its own module docstring.  Matched by tests/test_assets_*.py.
        "tests/test_assets_sla.py",
        # Pure-unit / hermetic mock test for Wave A-3 (warranty/EOL + NetBox sync);
        # mocks DB and external HTTP NetBox boundaries. Matched by tests/test_assets_*.py.
        "tests/test_assets_warranty_netbox.py",
        # Pure-unit test for Wave A-4 (QR generator & room register);
        # mocks DB boundary. Matched by tests/test_assets_*.py.
        "tests/test_assets_qr_register.py",
        # Pure-unit test for Wave A-5 (Assets failure pattern recording);
        # mocks DB boundary and verifies Contract-A. Matched by tests/test_assets_*.py.
        "tests/test_assets_failure_pattern.py",
        # Pure-unit test for Wave IN-2 (Inventory kitting & package reservation);
        # tests in-memory domain logic and mocks DB boundary. Matched by tests/test_inventory_*.py.
        "tests/test_inventory_kitting.py",
    }
)


def _has_integration_marker(path: Path) -> bool:
    """True when *path* contains a real integration marker.

    AST-scoped throughout -- there is no raw-text regex fallback here anymore.
    Before Batch 155, the first check was ``re.search(r"^pytestmark\\s*=.*
    integration", src, re.M)`` over the whole file's raw source, which cannot
    tell a genuine top-level assignment from the same text appearing anywhere
    else that happens to start a line -- most concretely, inside a string
    literal used as example/fake module source for a DIFFERENT test's own
    ``ast.parse()`` call. Confirmed live: ``tests/unit/test_direct_do_call_
    mocked_db_census.py``'s own ``test_positive_control_ignores_integration_
    marked_modules`` builds exactly such a string (a fake "live module" body
    containing a literal ``pytestmark = pytest.mark.integration`` line as
    example source), and the regex read that column-0 text as a real marker
    on the census file itself -- a false positive that forced the census's
    author to assemble the marker at runtime via ``.format()`` purely to
    dodge this check (see ``CI_INTEGRATION_COVERAGE_REGEX_FRAGILITY.md`` in
    ``_internal/work-docs``, filed the same night).

    What actually closes the gap is checking each candidate assignment's own
    TARGET: only an ``ast.Assign``/``ast.AnnAssign`` node whose target is
    literally ``Name(id="pytestmark")`` counts, and its value must itself be
    inspected via ``ast.dump`` (a structural walk of that one expression's
    parsed form), never the file's raw text. The string literal from the
    incident above parses to an ``ast.Constant`` holding the marker text as a
    plain string value -- it is not an assignment to a name called
    ``pytestmark`` at all (the real target there is whatever variable the
    string was assigned to, e.g. ``live_module_code``), so no amount of
    walking finds it. ``ast.walk`` (rather than restricting to the module's
    own top-level ``tree.body``) is deliberate and matches the reference
    below: pytest honours a class-level ``pytestmark`` too (applies the
    marker to every method in that class), so a assignment nested one level
    into a ``ClassDef`` is a real marker, not a false positive to exclude.

    Mirrors ``_has_integration_marker`` in ``tests/unit/test_direct_do_call_
    mocked_db_census.py`` exactly (same ``ast.walk`` + target-name-check
    shape, same repo convention -- deliberately not a second shape), with
    one addition per the filed doc: ``ast.AnnAssign`` alongside ``ast.Assign``
    for a hypothetical ``pytestmark: ... = ...`` form -- not exercised by any
    file in the tree today, kept for parity with the recommendation rather
    than assumed unnecessary.
    """
    src = path.read_text(encoding="utf-8", errors="replace")
    if "integration" not in src:
        return False
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id == "pytestmark":
                    if "integration" in ast.dump(node.value):
                        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            for dec in node.decorator_list:
                if "pytest.mark.integration" in ast.unparse(dec):
                    return True
    return False


def _files_with_integration_markers() -> set[str]:
    return {
        p.relative_to(_REPO_ROOT).as_posix()
        for p in _TESTS_DIR.rglob("*.py")
        if _has_integration_marker(p)
    }


def _resolve_workflow_token(token: str) -> set[str]:
    """Resolve one ``tests/...py`` token found in a workflow file to real paths.

    A literal token (no ``*``) is the ratchet's original behaviour: return it
    unchanged. A glob token -- introduced by Batch 152a so a per-module CI job
    can cover a whole ``tests/test_<module>_*.py`` family instead of a
    hand-maintained file list -- is resolved with ``Path.glob`` relative to the
    repo root.

    A glob that matches **nothing** must not silently contribute nothing to the
    wired set: that is a job whose command now runs zero tests, which is a
    worse failure than a stale file list because nothing about it looks wrong
    until you count. Fail loudly here instead.
    """
    if "*" not in token:
        return {token}
    matches = {p.relative_to(_REPO_ROOT).as_posix() for p in _REPO_ROOT.glob(token)}
    assert matches, (
        f"workflow glob {token!r} matched no files under the repo root -- "
        "its CI step now runs zero tests. Fix the glob (or the job), it does "
        "not gate anything as written."
    )
    return matches


def _workflow_files() -> list[Path]:
    """Every workflow file, in both spellings of the YAML extension.

    ``glob("*.yml")`` alone silently skipped ``*.yaml``, which GitHub Actions
    accepts just as happily.  No ``.yaml`` workflow exists in this repo today --
    the directory holds ci.yml, citus-matrix.yml, dep-audit.yml,
    deploy-pages.yml and release.yml, and covering both extensions changes the
    resolved file set by nothing -- so this closes a latent hole rather than
    fixing a live bug.  Left as-is, a future ``.yaml`` workflow would be
    invisible to every ratchet in this module: its integration files would look
    unwired and its globs would go unchecked.
    """
    return sorted(
        p for p in _WORKFLOWS_DIR.iterdir() if p.is_file() and p.suffix in {".yml", ".yaml"}
    )


def _iter_run_scripts(node: object) -> Iterator[str]:
    """Yield every ``run:`` shell script in a parsed workflow document.

    Walks the parsed structure rather than the text, so a ``run:`` at any depth
    is found and nothing else is.  ``defaults.run`` is a mapping, not a script,
    and falls through the ``isinstance(value, str)`` guard into the recursive
    branch, where it contributes nothing.

    The positions this DROPS, named rather than left to be inferred from four
    lines of code: comments (the parser discards them), ``name:``, ``env:``
    values, and ``with:`` arguments.  Dropping comments and ``name:`` is the
    whole point of this batch -- they are labels, not invocations.  ``env:`` and
    ``with:`` are a different matter: a step with no ``run:`` of its own that
    hands a test path to a composite action as a ``with:`` argument genuinely
    does execute that path, and this would not see it.  Latent here -- every
    ``uses:`` in these workflows is a published third-party action and no local
    or composite action exists -- but a real hole, so it goes on the record.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "run" and isinstance(value, str):
                yield value
            else:
                yield from _iter_run_scripts(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_run_scripts(item)


def _workflow_tokens() -> set[str]:
    """Every ``tests/...py`` token a workflow actually **executes**.

    Scoped to ``run:`` scalars and parsed with ``yaml.safe_load``.  It used to
    be a regex over the concatenated raw text of every workflow file, which
    counted **comments as wiring**.  ``tests/test_assets_*.py`` occurs four
    times in ``ci.yml``: two of them (lines 291 and 317 at the time of writing)
    are prose inside a comment block, one is the step's ``name:``, and only the
    fourth is the ``run:`` that invokes pytest.  Delete the whole M9 Assets step
    but leave its comment block behind and the old scan still reported the glob
    as wired -- and the assertions added by Batches 152b and 152c would then have
    made an affirmative claim about "the step that claims to run them" for a
    step that no longer existed.  A ``name:`` must not count either: it is a
    label, not an invocation.

    Letting the YAML parser drop comments is the point.  A comment-stripping
    regex is the fragile version of this, and the failure mode it leaves behind
    is silent.

    Narrowing the scan can only ever REMOVE tokens.  Measured on the tree this
    landed on it removes exactly two, both named only in comments and both
    marker-free by design: ``tests/test_ci_integration_coverage.py`` (this
    file, referenced from four explanatory comments in ci.yml) and
    ``tests/test_mcp_cache.py`` (one comment explaining why the
    cache-invalidation job needs a real Redis).  Neither carries an integration
    marker, so the set of *marked* files considered wired is unchanged: the
    pre-existing ``test_every_integration_file_is_wired_or_explicitly_excluded``
    ratchet neither loosens nor newly fires, and no ``KNOWN_UNWIRED`` entry
    became stale.

    Two known limitations, stated rather than left implicit.  A token is a path
    ending in ``.py``, so (a) a directory-wide invocation is invisible: the unit
    job's ``pytest tests/ -m "not integration and not perf"`` contributes no
    token at all, which is harmless because that job selects integration tests
    *out* by construction; and (b) a ``::nodeid`` suffix is truncated, so a file
    selected test-by-test yields a file-level token that says nothing about
    which of its tests CI actually selected.  ``tests/test_worm_registry.py`` is
    the live case -- see the module docstring.  Both are pre-existing, both are
    out of scope here, and neither is claimed to be covered.

    An unparseable workflow raises a **named** ``AssertionError`` identifying
    the file, rather than letting a raw parser traceback surface four times over
    with no indication of which file broke.  Multi-document (``---``) and
    syntactically invalid files are not valid GitHub Actions workflows either,
    so failing loudly is right; failing anonymously is not.

    One scan shared by every reader below, so the wired-file ratchet and the two
    glob checks can never disagree about what CI actually runs.
    """
    tokens: set[str] = set()
    for path in _workflow_files():
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
        except yaml.YAMLError as exc:
            raise AssertionError(
                f".github/workflows/{path.name} is not a single loadable YAML document, "
                "so this module cannot tell which tests CI runs and every ratchet in it "
                "would be reading a partial picture. A multi-document or syntactically "
                "invalid file is not a valid GitHub Actions workflow either -- fix the "
                f"workflow rather than loosening this check. Parser said: {exc}"
            ) from exc
        for script in _iter_run_scripts(document):
            tokens.update(re.findall(r"(tests/[A-Za-z0-9_/*]+\.py)", script))
    return tokens


def _files_named_in_workflows() -> set[str]:
    return {resolved for token in _workflow_tokens() for resolved in _resolve_workflow_token(token)}


def test_every_integration_file_is_wired_or_explicitly_excluded() -> None:
    """A new integration file must gate something, or say out loud that it does not."""
    unaccounted = sorted(
        _files_with_integration_markers() - _files_named_in_workflows() - KNOWN_UNWIRED
    )
    assert not unaccounted, (
        "These files carry @pytest.mark.integration but are run by no CI workflow, "
        "so they gate nothing: " + ", ".join(unaccounted) + ". Either add them to the "
        "integration job in .github/workflows/ci.yml (preferred), or add them to "
        "KNOWN_UNWIRED in this file with a reason."
    )


def test_known_unwired_list_has_no_stale_entries() -> None:
    """Keep the backlog honest so it can only shrink.

    An entry is stale once the file is wired into a workflow, has lost its
    integration markers, or no longer exists.  Leaving stale entries in would let
    the list drift into a rubber stamp.
    """
    marked = _files_with_integration_markers()
    wired = _files_named_in_workflows()
    now_wired = sorted(KNOWN_UNWIRED & wired)
    no_longer_marked = sorted(e for e in KNOWN_UNWIRED if e not in marked)
    assert not now_wired, "Wired into a workflow now -- remove from KNOWN_UNWIRED: " + ", ".join(
        now_wired
    )
    assert not no_longer_marked, (
        "No longer carry integration markers (or no longer exist) -- remove from "
        "KNOWN_UNWIRED: " + ", ".join(no_longer_marked)
    )


def test_every_workflow_glob_matches_at_least_one_marked_test() -> None:
    """A wired glob must still resolve to at least one marked test.

    **Scope first, because the previous version of this docstring overstated
    it.**  This iterates ``t for t in _workflow_tokens() if "*" in t`` -- glob
    tokens only.  Measured on the tree this landed on, the workflows' ``run:``
    scalars name 16 ``tests/...py`` tokens, of which exactly **3** are globs
    (``tests/test_assets_*.py``, ``tests/test_economy_*.py``,
    ``tests/test_inventory_*.py``), resolving to 20 files.  The other **13 are
    literal**, and this test and its sibling
    ``test_every_globbed_file_contributes_a_marked_test`` -- which filters
    identically -- look at **neither**: the four separate literal tokens of the
    M3 step (``tests/test_agreements_authoring.py``,
    ``tests/test_agreements_compliance.py``, ``tests/test_agreements_signing.py``,
    ``tests/test_agreements_sla.py`` -- ``tests/test_agreements_*.py`` is NOT a
    workflow token and would resolve to 10 files); the seven of the
    integration-smoke step
    (``test_rls_isolation_integration``, ``test_schema_bootstrap``,
    ``test_rls_catalog``, ``test_outbox_relay``, ``test_outbox_fanout``,
    ``test_event_log_hardening``, ``test_worm_registry``);
    ``tests/test_rest_cache_invalidation.py``; and
    ``tests/integration/test_citus_rls.py``.  All 13 carry integration markers.
    Strip the **51 markers on the four literal-wired M3 files**
    (``pytest.mark.integration`` decorators on test functions, counted via AST)
    and this module stays entirely green -- so the earlier claim that a module
    family losing its markers surfaces "here, in the job that always runs" held
    for the three globbed families and for nothing else.

    That number carries its counting method deliberately.  A raw substring count
    over the same four files says **53**: two of the hits are prose inside module
    docstrings (``test_agreements_authoring.py`` line 37,
    ``test_agreements_signing.py`` line 26), and ``_has_integration_marker``
    above rejects exactly those -- "a file that merely mentions the word, or
    documents the marker in a docstring, is not marked".  A bare integer is the
    defect class, not a detail: this batch exists because a regex over raw
    workflow text counted comments as wiring, and a regex over raw test text
    counts comments as markers in precisely the same way.

    Why the predicate is ``"*" in t``, recorded as a decision rather than left
    to look like an oversight: the escape hatch this rule needs is
    ``GLOBBED_UNMARKED_BY_DESIGN``, and it is glob-shaped.  A prefix glob has to
    tolerate a marker-free file it merely happens to sweep up; a literal token
    is a path someone typed deliberately, which wants a different and probably
    stricter rule.  Before Batch 152d scoped the token scan to ``run:``,
    extending to literal tokens would additionally have self-failed on two
    comment-only tokens that are marker-free on purpose
    (``tests/test_ci_integration_coverage.py`` and ``tests/test_mcp_cache.py``);
    those are no longer tokens, so that particular obstacle is gone and the
    extension has become feasible.  It is nevertheless **deferred to Batch
    152e**, which owns generalising the escape hatch.  That is a scope decision;
    until it lands, the 13 literal-wired files above are an uncovered gap and
    this docstring is where that is written down.

    Measured exit codes for a ``pytest tests/test_x_*.py -m integration`` step
    (pytest 9 with this repo's ``pytest.ini``; CI pins 9.1.1).  **Four** states,
    not the three previously listed here:

    * the glob matches **no file** -- bash (nullglob off) passes the pattern
      through literally and ``pytest`` exits **4**.  Already covered, loudly,
      by ``_resolve_workflow_token``.
    * the glob matches files and ``-m integration`` selects **none** of them --
      ``pytest`` exits **5** (no tests collected).  Non-zero, so the step does
      go red on its own.
    * the glob matches files, selects some, and they **execute** -- exit **0**.
    * the glob matches files, selects some, and every selected test **skips at
      runtime** -- exit **0**, nothing executed, step green.  **Live today, on
      the three globbed families themselves.**  All 20 globbed files consume
      ``pg_pool`` or ``pg_app_conn`` from ``tests/conftest.py``, and those
      fixtures call ``pytest.skip()`` when no DSN is set or Postgres is
      unreachable (conftest lines 362, 373 and 431).  The skip mechanism lives
      in the fixtures, not in the test files, so grepping those files for
      ``skipif`` / ``pytest.skip`` / ``importorskip`` finds nothing and proves
      nothing -- a partial check that would be a total claim.  Measured: point
      ``PG_DSN`` at an unreachable port and ``pytest tests/test_assets_*.py -m
      integration`` reports ``23 skipped, 17 deselected`` and exits **0** --
      markers 100% intact, 23 tests selected, zero executed, step green.
      ``tests/integration/test_citus_rls.py`` is the same state made explicit
      rather than a separate one: all four of its marked tests carry
      ``skip_citus_unavailable`` (line 64) and ``citus-matrix.yml`` runs it to a
      clean exit 0 while Citus is DESCOPED (measured: ``4 skipped``, exit 0).  A
      marker check cannot see a runtime skip -- that is a different ratchet, not
      this one -- so this test calls every one of those families healthy.

    Because of the exit-5 state, this assertion is not the only thing between a
    globbed family losing every marker and a green build.  What it adds is where
    and how that failure surfaces: in the unit job, naming the glob and the
    cause, rather than as a bare "no tests ran" from a job that first has to
    stand up Postgres.  It also keeps holding if a step ever starts tolerating
    exit 5 -- ``|| [ $? -eq 5 ]`` is a common enough CI idiom to be worth a guard
    that does not depend on the exit code at all.

    "At least one" is the deliberate granularity, not a weaker convenience.  A
    prefix glob legitimately spans both kinds of file: ``tests/test_assets_*.py``
    matches the pure-unit ``tests/test_assets_sla.py`` -- marker-free on purpose,
    said out loud in its own docstring -- alongside the module's marked
    integration files.  Demanding a marker on every match would fail on that
    file and pressure a coder into marking a test that needs no database.

    What this does NOT catch, stated plainly because the gap is the interesting
    part: **partial** marker loss.  If one file in a family of eight loses its
    markers and seven keep theirs, the glob still resolves to marked tests, this
    test stays green, and the step exits 0 having run less than it did before.
    Catching that needs a per-family count ratchet, which this is not --
    ``test_every_globbed_file_contributes_a_marked_test`` covers that case per
    file for globbed files instead.  Nor does this prove the marked tests pass;
    that they are not all skipped at runtime for want of a database (they are,
    whenever no reachable Postgres is configured -- the fourth state above);
    that a file wired only by ``::nodeid`` selections selects its *marked* tests
    (``tests/test_worm_registry.py`` does not); that any literal-wired file is
    healthy; or that CI's collection matches this module's.
    """
    marked = _files_with_integration_markers()
    dead_globs = []
    for token in sorted(t for t in _workflow_tokens() if "*" in t):
        resolved = _resolve_workflow_token(token)
        if not resolved & marked:
            dead_globs.append(f"{token} (matched {len(resolved)} file(s), none marked)")
    assert not dead_globs, (
        "These workflow globs resolve to real files but to no "
        "@pytest.mark.integration test, so their CI step has nothing to run "
        "under `-m integration` and gates nothing: " + "; ".join(dead_globs) + ". "
        "Restore the integration markers on that module's tests, or remove the "
        "step that claims to run them."
    )


def test_every_globbed_file_contributes_a_marked_test() -> None:
    """The converse ratchet: globbed -> marked, per file, with no counts.

    ``test_every_integration_file_is_wired_or_explicitly_excluded`` runs one
    direction -- a *marked* file must be *wired*.  This runs the other, which is
    where coverage regresses quietly.  Its sibling
    ``test_every_workflow_glob_matches_at_least_one_marked_test`` only proves a
    family has *something* marked; losing every marker in a family is loud
    anyway (pytest exits 5 and the step goes red on its own).  **Partial** loss
    is the silent one: seven of eight files keep their markers, the glob still
    resolves to marked tests, pytest exits 0, and the step quietly runs less
    than it did yesterday.  That is the shape of the Batch 147 near-miss, caught
    only because the coder happened to notice.

    Deliberately per-file and number-free.  A committed expected-count would be
    a magic number, and this repo has the scar to prove where those go: a
    hardening wave ends up ratifying its own shortfall, and a coder who needs
    green edits the number.  A generated manifest is worse -- regenerated at test
    time it always matches and gates nothing.  A filename in
    ``GLOBBED_UNMARKED_BY_DESIGN`` has to be justified to a reviewer; an integer
    does not.  This also means no staleness when files are added or deleted, and
    a newly added file must carry markers or land on that list.

    Not vacuous on an empty glob: ``_resolve_workflow_token`` already fails
    loudly when a glob matches nothing, so a zero-file family cannot slip
    through this as "no offenders found".

    **Scope, identical to its sibling and stated here too rather than left to be
    inferred**: this filters ``t for t in _workflow_tokens() if "*" in t``, so it
    sees glob tokens only.  Measured on the tree this landed on, that is 3 of 16
    tokens and 20 of the 33 wired files.  The 13 literal-wired files -- the four
    M3 agreements files, each named by its own literal token
    (``tests/test_agreements_authoring.py``, ``..._compliance.py``,
    ``..._signing.py``, ``..._sla.py``; ``tests/test_agreements_*.py`` is not a
    workflow token), the seven of the integration-smoke step,
    ``tests/test_rest_cache_invalidation.py`` and
    ``tests/integration/test_citus_rls.py`` -- are checked by neither ratchet,
    and all 13 carry markers today.  "Carries markers" is file-level:
    ``tests/test_worm_registry.py`` is both marked and wired, yet the two tests
    ci.yml actually selects from it are the unmarked ones (module docstring).
    Extending either ratchet to literal tokens
    is **deferred to Batch 152e**, which owns generalising
    ``GLOBBED_UNMARKED_BY_DESIGN`` (a glob-shaped escape hatch) into something a
    literal token can use.  A recorded scope decision, not a silent omission;
    the sibling's docstring carries the full reasoning.

    What this still does NOT catch, on the record as a scope decision rather
    than a silent omission: partial loss *within* one file -- a file that drops
    some of its markers but keeps at least one stays marked and passes here.
    Catching that needs per-file counts, i.e. the committed-manifest route, and
    it is not worth that cost yet.  Nor does it see a file whose markers are all
    intact but whose tests all skip at runtime for want of a database -- the
    fourth exit-code state in the sibling's docstring, live on every one of the
    20 files this ratchet covers -- nor a file wired only by ``::nodeid``
    selections that miss its marked tests (``tests/test_worm_registry.py``), nor
    literal-wired files at all.
    """
    marked = _files_with_integration_markers()

    offenders = []
    for token in sorted(t for t in _workflow_tokens() if "*" in t):
        for path in sorted(_resolve_workflow_token(token) - marked):
            if path in GLOBBED_UNMARKED_BY_DESIGN:
                continue
            offenders.append(f"{path} (matched by {token})")
    assert not offenders, (
        "These files are pulled into a CI integration step by a workflow glob "
        "but carry no @pytest.mark.integration test, so that step collects them "
        "and runs nothing from them -- and because the family's other files are "
        "still marked, pytest exits 0 and the step stays green: "
        + "; ".join(offenders)
        + ". Restore the markers, or add the file to GLOBBED_UNMARKED_BY_DESIGN "
        "if it genuinely needs no database."
    )

    globbed = {f for t in _workflow_tokens() if "*" in t for f in _resolve_workflow_token(t)}
    stale = sorted(e for e in GLOBBED_UNMARKED_BY_DESIGN if e not in globbed or e in marked)
    assert not stale, (
        "These GLOBBED_UNMARKED_BY_DESIGN entries are no longer accurate -- the "
        "file is marked now, no longer matched by any glob, or gone -- so remove "
        "them before the list drifts into a rubber stamp: " + ", ".join(stale) + "."
    )


# ---------------------------------------------------------------------------
# Phase 4 Wave T-5 Hardening: Positive Controls (U18)
# ---------------------------------------------------------------------------


def test_positive_control_fails_on_unwired_marked_file() -> None:
    """Standing positive control (U18 / T-5 Q1): prove ratchet catches unwired marked files."""
    fake_marked = _files_with_integration_markers() | {"tests/test_synthetic_unwired_marked.py"}
    unaccounted = sorted(fake_marked - _files_named_in_workflows() - KNOWN_UNWIRED)
    assert unaccounted == ["tests/test_synthetic_unwired_marked.py"], (
        "Positive control failed: ratchet did not isolate synthetic unwired marked file"
    )


def test_positive_control_fails_on_unmarked_globbed_file() -> None:
    """Standing positive control (U18 / T-5 Q1): prove converse ratchet catches unmarked globbed files.

    The original version of this control never called ``_workflow_tokens()`` or
    ``_resolve_workflow_token()`` -- it recomputed the set arithmetic inline against a
    hardcoded, nonexistent filename (``tests/test_assets_synthetic_unmarked.py``), so it
    passed regardless of whether real glob resolution worked. Proven vacuous by stubbing
    ``_resolve_workflow_token`` to always return ``set()``: the real ratchets
    (``test_every_globbed_file_contributes_a_marked_test``,
    ``test_every_workflow_glob_matches_at_least_one_marked_test``) both went red, but this
    control still passed.

    Rewritten to mirror ``test_positive_control_fails_on_unwired_marked_file``: call the
    REAL ``_workflow_tokens()`` / ``_resolve_workflow_token()`` to find a file that is
    genuinely resolved by a real workflow glob and genuinely marked today, then perturb
    only the ``marked`` *input* -- as if that one file's marker had been silently
    removed -- and re-derive offenders through the same real functions. Nothing about
    glob resolution is faked; only the marked-set argument is.
    """
    marked = _files_with_integration_markers()

    globbed_marked_files: set[str] = set()
    for token in sorted(t for t in _workflow_tokens() if "*" in t):
        globbed_marked_files |= _resolve_workflow_token(token) & marked
    assert globbed_marked_files, (
        "no globbed+marked file found on this tree -- positive control has nothing to "
        "pretend-unmark (if _workflow_tokens()/_resolve_workflow_token() broke and started "
        "resolving to nothing, this is exactly how that would show up here)"
    )
    victim = sorted(globbed_marked_files)[0]

    fake_marked = marked - {victim}
    offenders = []
    for token in sorted(t for t in _workflow_tokens() if "*" in t):
        for path in sorted(_resolve_workflow_token(token) - fake_marked):
            if path in GLOBBED_UNMARKED_BY_DESIGN:
                continue
            offenders.append(path)

    assert victim in offenders, (
        f"Positive control failed: converse ratchet did not isolate {victim!r} when its "
        "marker was pretend-removed from the real, globbed-file input"
    )


# ---------------------------------------------------------------------------
# Batch 155: _has_integration_marker positive controls
# ---------------------------------------------------------------------------
#
# The controls above prove the RATCHETS (set arithmetic over whatever
# _files_with_integration_markers() returns) catch a bad input. None of them
# drive _has_integration_marker itself, so none would have caught the false
# positive it actually had: a raw-text regex fallback that read example
# marker source inside a string literal as a real marker. These do drive it
# directly, on real files via tmp_path -- the same entry point
# _files_with_integration_markers() uses -- not a re-derivation.


def test_has_integration_marker_detects_a_real_top_level_marker(tmp_path: Path) -> None:
    """The ordinary case: a genuine module-level `pytestmark = pytest.mark.
    integration` must still be detected -- this is what every real
    live-Postgres sibling in the repo relies on."""
    module_path = tmp_path / "test_synthetic_real_marker.py"
    module_path.write_text(
        "import pytest\n\npytestmark = pytest.mark.integration\n\n"
        "def test_something():\n    assert True\n",
        encoding="utf-8",
    )
    assert _has_integration_marker(module_path) is True


def test_has_integration_marker_ignores_marker_text_inside_a_string_literal(
    tmp_path: Path,
) -> None:
    """Regression test for the actual incident (2026-09-21): `tests/unit/
    test_direct_do_call_mocked_db_census.py`'s own positive control builds a
    fake "live module" body, as a plain string, containing the literal line
    `pytestmark = pytest.mark.integration` purely as example source for its
    own ast.parse() call -- never a real top-level statement in the census
    file itself. The old raw-text regex read that string's contents as a
    real marker and reported the census file as an unwired live-Postgres
    module. Reproduces that exact shape (a triple-quoted string assigned to
    a module-level variable, containing the marker text at column 0 inside
    the string) and asserts the file is NOT marked.
    """
    module_path = tmp_path / "test_synthetic_marker_in_string.py"
    module_path.write_text(
        'def test_builds_fake_source():\n'
        '    live_module_code = """\n'
        "import pytest\n\n"
        "pytestmark = pytest.mark.integration\n\n"
        "async def test_real_postgres_call(pg_pool):\n"
        "    result = await do_something_real(pg_pool)\n"
        "    assert result\n"
        '"""\n'
        "    assert \"pytestmark\" in live_module_code\n",
        encoding="utf-8",
    )
    assert _has_integration_marker(module_path) is False, (
        "false positive: marker text inside a string literal was mistaken for a "
        "real top-level pytestmark assignment"
    )


def test_has_integration_marker_detects_a_decorator_marker(tmp_path: Path) -> None:
    """The other real shape this function must still catch: a per-function
    `@pytest.mark.integration` decorator with no module-level pytestmark at
    all. Untouched by the regex removal, but not previously driven by any
    control in this file either."""
    module_path = tmp_path / "test_synthetic_decorator_marker.py"
    module_path.write_text(
        "import pytest\n\n@pytest.mark.integration\ndef test_something():\n    assert True\n",
        encoding="utf-8",
    )
    assert _has_integration_marker(module_path) is True


# Vendored, not fetched: an earlier version of this test pulled each shape via
# `git show <sha>:<path>` at test time. CI clones shallow, so `53ab458` and
# `a252043` are not in the runner's object store and `git show` exits 128 --
# the test failed loudly in CI for an environment reason, not a real
# regression (found live, 2026-09-21, when this file's own PR went red).
# Git history is where these three shapes were FOUND, not what the test is
# about: the property under test is "_has_integration_marker returns True /
# True / False for these three specific source shapes," which needs no
# repository, only the text. Vendored here as EXCERPTS of each commit's real
# content, not always byte-identical: the imports and the actual
# `pytestmark = pytest.mark.integration` statement are verbatim in all three
# (the only lines `_has_integration_marker` inspects), but the two
# true-positive modules' docstring prose is shortened for length --
# confirmed by diffing all three against fresh `git show <sha>:<path>`
# fetches (2026-09-21): `_FALSE_POSITIVE_MODULE_b46bc8c` is an exact
# substring of the real file; the other two match for the first several
# hundred characters, then their docstrings are trimmed. Re-verified after
# trimming that the mutation below still reproduces the same failures
# across the same files, so the shortening reaches no structural part of
# either shape. SHAs kept in comments so the provenance is not lost.

# tests/integration/test_resource_surface_comment_tag_validation_live.py at
# commit 53ab458 -- added with a real top-level marker, genuinely unwired
# until e46f8d1 added its ci.yml step. Must detect True.
_REAL_MARKED_MODULE_53ab458 = '''"""
tests/integration/test_resource_surface_comment_tag_validation_live.py
==========================================================================
Live-Postgres regression test for write-time identifier validation on the
comment/tag sub-resources (``handle_add_comment``, ``handle_add_tag``,
``handle_remove_tag`` in ``nce/resource_surface/rest.py``).

THE BUG
---------
``v3_cognitive_ledger``'s comment/tag entries have no FK to a real
resource -- ``entity_id`` was, before this fix, an unvalidated opaque
string. A caller who wrote a comment against a bogus, mistyped, or
(specifically) SHADOWED identifier got a silent 201 that could never be
found again.
"""

from __future__ import annotations

import uuid

import asyncpg
import httpx
import pytest
import pytest_asyncio
from starlette.applications import Starlette

from nce import admin_state
from nce.auth import set_namespace_context
from nce.engine_registry import populate_engine_modules
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.orchestrator import NCEEngine
from nce.resource_surface import get_all_resource_specs, load_all_engine_resources
from nce.resource_surface.mcp import build_mcp_tool_specs
from nce.resource_surface.rest import make_resource_routes

pytestmark = pytest.mark.integration

load_all_engine_resources()
_SPECS = {(s.engine, s.entity): s for s in get_all_resource_specs()}
_CONTACT_SPEC = _SPECS[("sales", "contacts")]
'''

# tests/integration/test_cron_saga_recovery_live.py at commit a252043 --
# same shape, genuinely unwired until its own ci.yml step landed. Must
# detect True.
_REAL_MARKED_MODULE_a252043 = '''"""
tests/integration/test_cron_saga_recovery_live.py
====================================================
Live-Postgres proof that `_saga_recovery_tick` (`nce/cron.py`) actually
decodes `saga_execution_log.payload` from a real JSONB column.

No asyncpg jsonb codec is registered anywhere in this codebase, so
`payload` always arrives from a real connection as a raw JSON string,
never a dict. Before this fix, `isinstance(row["payload"], dict)` was
therefore always `False`.
"""

from __future__ import annotations

import json
import uuid

import asyncpg
import pytest

from nce.cron import _saga_recovery_tick

pytestmark = pytest.mark.integration


async def test_saga_recovery_finds_memory_id_from_real_jsonb_payload(
    pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    """The actual regression, not a synthetic shape."""
'''

# tests/unit/test_mocked_db_dependent_test_census.py at commit b46bc8c --
# the actual false-positive incident this fix closes: this exact text,
# verbatim from that commit's test_positive_control_ignores_integration_
# marked_modules, embeds the marker line inside a STRING LITERAL assigned to
# `live_module_code` -- never a real top-level statement in the file that
# contains it. Must detect False (the old regex detected True here, which
# was the bug).
_FALSE_POSITIVE_MODULE_b46bc8c = '''def test_positive_control_ignores_integration_marked_modules() -> None:
    """A module carrying `pytestmark = pytest.mark.integration` is a live-Postgres
    sibling by this repo's own standing convention and must never be scanned,
    regardless of what it calls directly."""
    live_module_code = """
import pytest

pytestmark = pytest.mark.integration

async def test_real_postgres_call(pg_pool):
    result = await do_something_real(pg_pool)
    assert result
"""
'''


def test_has_integration_marker_still_catches_the_three_real_2026_09_21_incidents(
    tmp_path: Path,
) -> None:
    """Do not weaken this check to fix the false positive above -- prove it
    against the three real states from tonight that motivated this file's
    existence and this fix, not synthetic stand-ins.

    Vendored as literals (see the three `_REAL_MARKED_MODULE_*` /
    `_FALSE_POSITIVE_MODULE_*` constants above) rather than fetched via
    `git show <sha>:<path>` at test time -- an earlier version of this test
    did that and failed in CI with `git show ... returned non-zero exit
    status 128`: CI clones shallow, so `53ab458` and `a252043` are not in
    the runner's object store. The property under test needs no repository,
    only the text, so the text is what's committed here.
    """
    cases = [
        (
            "53ab458",
            "test_resource_surface_comment_tag_validation_live.py",
            _REAL_MARKED_MODULE_53ab458,
            True,
        ),
        ("a252043", "test_cron_saga_recovery_live.py", _REAL_MARKED_MODULE_a252043, True),
        (
            "b46bc8c",
            "test_mocked_db_dependent_test_census.py",
            _FALSE_POSITIVE_MODULE_b46bc8c,
            False,
        ),
    ]
    for sha, name, src, expected in cases:
        module_path = tmp_path / f"{sha}_{name}"
        module_path.write_text(src, encoding="utf-8")
        assert _has_integration_marker(module_path) is expected, (
            f"{name} at {sha}: expected _has_integration_marker() == {expected}, "
            f"got {not expected} -- this is one of the three real states the AST "
            f"rewrite must not regress"
        )
