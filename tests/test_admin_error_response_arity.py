"""Every ``admin_error_response(...)`` call must actually be callable (**D4**).

``admin_error_response(message, exc, *, status_code=500, ...)`` takes the human
message FIRST and the exception SECOND. One call site had them collapsed into
``admin_error_response(exc, status_code=500)``, which raises

    TypeError: admin_error_response() missing 1 required positional argument: 'exc'

...*inside an exception handler*. So the route answered an unhandled 500 with a
traceback instead of the structured error body, and only ever when something
else had already gone wrong. That is why it survived: the defect is unreachable
until the day you need it most, and no test exercises a handler's own failure.

🔴 **Found by AST, not by grep, and the difference is the whole point.** There
are 138 textual occurrences of ``admin_error_response(`` in ``nce/`` --
docstrings, comments and the definition itself among them -- and exactly ONE is
wrong. A grep-based check either drowns in false positives or gets narrowed
until it stops seeing the real one. Parsing the call node makes the question
exact: how many positional arguments, and is the first one a string?

This is a static check on purpose. Exercising all ~138 error paths at runtime is
not feasible, and it is the *static* shape -- argument order -- that was wrong.

Guard-the-guard: ``_MIN_CALL_SITES`` fails loudly if the AST walk stops finding
calls, because zero calls all vacuously have the right arity.
"""

from __future__ import annotations

import ast
from pathlib import Path

_NCE_ROOT = Path(__file__).resolve().parents[1] / "nce"

_FUNC = "admin_error_response"

# Measured against the tree by this module's own walk: 138 call sites.
# Only ever added to as routes grow; a drop means the walk broke.
_MIN_CALL_SITES = 100


def _first_arg_is_a_message(node: ast.Call) -> bool:
    """True when the first positional argument is plausibly the `message: str`.

    Accepts a literal string, an f-string, and a string built by `+` or `%` --
    anything that cannot be an exception. A bare Name (e.g. `exc`) is exactly
    the defect, and a call like `str(exc)` is rejected too: `admin_error_response`
    logs `message` and `exc` separately, so passing the exception as the message
    still loses the exception argument.
    """
    if not node.args:
        return False
    first = node.args[0]
    if isinstance(first, ast.Constant):
        return isinstance(first.value, str)
    if isinstance(first, ast.JoinedStr):  # f"..."
        return True
    if isinstance(first, ast.BinOp) and isinstance(first.op, (ast.Add, ast.Mod)):
        return _first_arg_is_a_message(
            ast.Call(func=ast.Name(id="_"), args=[first.left], keywords=[])
        )
    return False


def _call_sites() -> list[tuple[Path, ast.Call]]:
    sites: list[tuple[Path, ast.Call]] = []
    for path in sorted(_NCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == _FUNC
            ):
                sites.append((path, node))
    return sites


def test_call_site_discovery_floor() -> None:
    """A broken walk finds nothing, and nothing is vacuously well-formed."""
    sites = _call_sites()
    assert len(sites) >= _MIN_CALL_SITES, (
        f"only {len(sites)} {_FUNC}() call sites found under {_NCE_ROOT} but at "
        f"least {_MIN_CALL_SITES} are expected -- the AST walk is not seeing "
        f"them, which would leave the arity check below vacuously green."
    )


def test_every_call_passes_a_message_then_an_exception() -> None:
    """`message` first, `exc` second -- both positional, at every call site."""
    offenders: list[str] = []
    for path, node in _call_sites():
        rel = path.relative_to(_NCE_ROOT.parent)
        if len(node.args) < 2:
            offenders.append(
                f"{rel}:{node.lineno} passes {len(node.args)} positional "
                f"argument(s); {_FUNC}(message, exc) needs 2"
            )
        elif not _first_arg_is_a_message(node):
            offenders.append(
                f"{rel}:{node.lineno} passes a non-string as the first argument "
                f"-- `message` comes first and `exc` second"
            )
    assert not offenders, (
        f"{len(offenders)} {_FUNC}() call site(s) would raise TypeError inside "
        "an exception handler:\n  " + "\n  ".join(offenders)
    )
