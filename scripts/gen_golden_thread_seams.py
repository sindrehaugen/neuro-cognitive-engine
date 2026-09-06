"""Generate ``docs/_generated/golden_thread_seams.md`` from the Golden Thread test file.

``tests/integration/test_golden_thread.py`` encodes the estate's canonical end-to-end
lifecycle as one test per step, `test_step_NN_<name>`. A step whose seam is broken
carries ``@pytest.mark.xfail(strict=True, reason="break-N: ...")``; a step whose seam
works carries no xfail marker at all. Because `strict=True`, a seam that closes while
still marked xfail makes pytest report XPASS (a failure), forcing the marker's removal
in the same commit that closes the seam (see the test file's own module docstring).

That means this page cannot go stale in the direction that matters: it is regenerated
from the test file's AST, so a step's status here is exactly the step's status in CI,
always. Prose written by hand about "which seams are open" rots the moment a wave
lands; this does not, because there is nothing to remember to update by hand.

Usage::

    python scripts/gen_golden_thread_seams.py --repo . --baseline HEAD \
        --out docs/_generated/golden_thread_seams.md
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess

TEST_FILE = "tests/integration/test_golden_thread.py"
STEP_RE = re.compile(r"^test_step_(\d+)_(\w+)$")


def git_show(repo: str, baseline: str, path: str) -> str:
    cmd = ["git", "-C", repo, "show", f"{baseline}:{path}"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True, encoding="utf-8")
    return result.stdout


def _docstring(node: ast.FunctionDef) -> str:
    doc = ast.get_docstring(node) or ""
    return doc.strip().splitlines()[0] if doc else ""


def _xfail_reason(node: ast.FunctionDef) -> str | None:
    for dec in node.decorator_list:
        call = dec if isinstance(dec, ast.Call) else None
        func = call.func if call else dec
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name != "xfail":
            continue
        if call:
            for kw in call.keywords:
                if kw.arg == "reason" and isinstance(kw.value, ast.Constant):
                    return str(kw.value.value)
        return "xfail (no reason string found)"
    return None


def extract_steps(repo: str, baseline: str) -> list[dict]:
    code = git_show(repo, baseline, TEST_FILE)
    tree = ast.parse(code)
    steps = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        m = STEP_RE.match(node.name)
        if not m:
            continue
        steps.append(
            {
                "num": int(m.group(1)),
                "name": node.name,
                "slug": m.group(2),
                "line": node.lineno,
                "doc": _docstring(node),
                "xfail_reason": _xfail_reason(node),
            }
        )
    steps.sort(key=lambda s: s["num"])
    return steps


def _break_id(reason: str) -> str:
    # reason strings start "break-N: ..." or "break-degradations: ..."
    m = re.match(r"(break-[0-9a-z]+)", reason)
    return m.group(1) if m else reason


def render(steps: list[dict], baseline_sha: str) -> str:
    open_steps = [s for s in steps if s["xfail_reason"]]
    closed_steps = [s for s in steps if not s["xfail_reason"]]
    break_ids = sorted({_break_id(s["xfail_reason"]) for s in open_steps})

    lines = []
    lines.append(
        f"> **Status:** shipped · **Verified-against:** {baseline_sha} (main) · **Last-audited:** generated"
    )
    lines.append("")
    lines.append("# Golden Thread — Seam Burndown")
    lines.append("")
    lines.append(
        f"> **Status:** shipped · **Verified-against:** {baseline_sha} (main) · **Last-audited:** generated"
    )
    lines.append("")
    lines.append(
        "**This page is generated from `tests/integration/test_golden_thread.py` — it cannot go stale.** "
        "A step is OPEN here if and only if its test carries `@pytest.mark.xfail(strict=True, reason=\"break-N: ...\")` "
        "in that file; `strict=True` means the test SUITE fails (XPASS) the moment a seam closes while its marker "
        "is still on, forcing the marker's removal in the same commit. Regenerate with:"
    )
    lines.append("```")
    lines.append(
        "python scripts/gen_golden_thread_seams.py --repo . --baseline HEAD "
        "--out docs/_generated/golden_thread_seams.md"
    )
    lines.append("```")
    lines.append("")
    lines.append(
        f"## Summary — {len(open_steps)} of {len(steps)} lifecycle steps broken, "
        f"{len(break_ids)} distinct seam(s): {', '.join(f'`{b}`' for b in break_ids)}"
    )
    lines.append("")
    lines.append("| Step | Seam | Status | Reason (from the test file) |")
    lines.append("|---:|---|---|---|")
    for s in steps:
        status = "🔴 OPEN" if s["xfail_reason"] else "✅ closed"
        reason = s["xfail_reason"] or "—"
        lines.append(
            f"| {s['num']} | {s['doc'] or s['name']} "
            f"(`{s['name']}` — `{TEST_FILE}:{s['line']}`) | {status} | {reason} |"
        )
    lines.append("")
    lines.append(
        f"**{len(closed_steps)} steps carry no `xfail` marker at all — their seam is proven live** "
        "by the test asserting the real production call path, not a mock."
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    steps = extract_steps(args.repo, args.baseline)
    sha_result = subprocess.run(
        ["git", "-C", args.repo, "rev-parse", "--short", args.baseline],
        capture_output=True,
        text=True,
        check=True,
    )
    sha = sha_result.stdout.strip()
    content = render(steps, sha)
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


if __name__ == "__main__":
    main()
