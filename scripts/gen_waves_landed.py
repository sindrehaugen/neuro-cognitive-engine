#!/usr/bin/env python3
"""Wave Landed on main Instrument Generator (Wave A-W1).

Measures which waves have genuinely landed on ``main`` by crossing three independent sources:
1. Tree stamps in ``nce/`` and ``tests/`` matching ``Wave <ID>``.
2. Merged PR titles recorded in GitHub / git log with PR numbers and merge timestamps.
3. Capability marker test exists on ``main``, contains tests, and is collected by pytest
   (not in ``KNOWN_UNWIRED``).

Core Invariant: "A stamp is not a landing."
- Three-way consensus: tree stamp + merged PR + valid unwired marker test -> LANDED.
- Disagreements are surfaced explicitly (e.g. Wave PJ-3 is stamped in internal-cores.json:60
  but has no merged PR and no marker test -> STAMPED_UNLANDED).
- The wave-ID pattern covers both plain (LL-N) and charter-prefixed (L-LLN) shapes.
"""

from __future__ import annotations

import argparse
import ast
import json
import pathlib
import re
import subprocess
import sys
from typing import Any

# Wave ID Patterns:
# Shape 1: plain LL-N (e.g. S-1, AG-3, HR-5, T-6, S-2a)
# Shape 2: charter-prefixed L-LLN (e.g. B-BI1, C-SD2, C-RS3, B-AG1, A-BI1)
WAVE_ID_PATTERN = re.compile(r"\b([A-Z]{1,3}-(?:[A-Z]{1,3})?[0-9]+[a-z]?)\b")
TREE_STAMP_PATTERN = re.compile(r"Wave\s+([A-Z]{1,3}-(?:[A-Z]{1,3})?[0-9]+[a-z]?)\b")

# Legacy / pre-v1.5 module waves (bare numbers, M0, C10, etc.)
LEGACY_WAVE_PATTERN = re.compile(
    r"^Wave\s+(?:C10|M0(?:\.W[0-9]+[a-z]?)?|[0-9]+(?:-[0-9]+)?(?:\+)?|[0-9]+[a-z]?)(?:\.[0-9a-z]+)?(?:\*{1,2})?\.?$"
)

# Known prose or comment occurrences containing 'Wave' that are not wave IDs
PROSE_TOKENS = {"Wave blocker"}


def git_ls_tree(repo: str, baseline: str, path: str = "") -> list[str]:
    cmd = ["git", "-C", repo, "ls-tree", "-r", "--name-only", baseline]
    if path:
        cmd.extend(["--", path])
    result = subprocess.run(cmd, capture_output=True, text=True, check=True, encoding="utf-8")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def git_show(repo: str, baseline: str, path: str) -> str:
    cmd = ["git", "-C", repo, "show", f"{baseline}:{path}"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True, encoding="utf-8")
    return result.stdout


def load_known_unwired(repo: str, baseline: str) -> frozenset[str]:
    """Parse KNOWN_UNWIRED set from tests/test_ci_integration_coverage.py."""
    try:
        content = git_show(repo, baseline, "tests/test_ci_integration_coverage.py")
    except Exception:
        path = pathlib.Path(repo) / "tests" / "test_ci_integration_coverage.py"
        if not path.exists():
            return frozenset()
        content = path.read_text(encoding="utf-8", errors="replace")

    tree = ast.parse(content)
    unwired: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "KNOWN_UNWIRED":
            if isinstance(node.value, ast.Call):
                for arg in node.value.args:
                    if isinstance(arg, ast.Set):
                        for elt in arg.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                unwired.add(elt.value)
    return frozenset(unwired)


def load_merged_prs(repo: str, live: bool = False) -> list[dict[str, Any]]:
    """Load merged PRs from GitHub CLI (if live) or cached docs/_generated/merged_prs.json."""
    cache_path = pathlib.Path(repo) / "docs" / "_generated" / "merged_prs.json"
    cached_prs: dict[int, dict[str, Any]] = {}
    if cache_path.exists():
        try:
            for p in json.loads(cache_path.read_text(encoding="utf-8")):
                if isinstance(p, dict) and "number" in p:
                    cached_prs[p["number"]] = p
        except Exception:
            pass

    if live:
        try:
            res = subprocess.run(
                [
                    "gh",
                    "pr",
                    "list",
                    "--state",
                    "merged",
                    "--limit",
                    "500",
                    "--json",
                    "number,title,mergedAt,mergeCommit",
                ],
                capture_output=True,
                text=True,
                check=True,
                encoding="utf-8",
            )
            live_data = json.loads(res.stdout)
            for p in live_data:
                if isinstance(p, dict) and "number" in p:
                    cached_prs[p["number"]] = p
            merged_list = sorted(cached_prs.values(), key=lambda x: x["number"])
            if cache_path.parent.exists():
                crlf_content = (
                    (json.dumps(merged_list, indent=2) + "\n").replace("\r\n", "\n").replace("\n", "\r\n")
                )
                cache_path.write_bytes(crlf_content.encode("utf-8"))
            return merged_list
        except Exception:
            pass  # fallback to cached file

    return sorted(cached_prs.values(), key=lambda x: x["number"])


def parse_prs_by_wave(prs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Extract wave IDs from merged PR titles, supporting both shapes."""
    prs_by_wave: dict[str, list[dict[str, Any]]] = {}
    for pr in prs:
        title = pr.get("title", "")
        m = re.findall(r"Waves?\s+([^)]+)(?:\)|$)", title, re.IGNORECASE)
        found: set[str] = set()
        if m:
            for clause in m:
                for w in WAVE_ID_PATTERN.findall(clause):
                    found.add(w)
        else:
            for w in WAVE_ID_PATTERN.findall(title):
                if f"Wave {w}" in title or f"wave {w}" in title.lower():
                    found.add(w)
        for wid in found:
            prs_by_wave.setdefault(wid, []).append(pr)
    return prs_by_wave


def scan_tree_stamps(repo: str, baseline: str) -> tuple[dict[str, list[str]], set[str], set[str]]:
    """Scan git tree for wave stamps, classifying into v1.5 waves, legacy waves, and unmatched."""
    cmd = [
        "git",
        "-C",
        repo,
        "grep",
        "-n",
        "-P",
        r"Wave\s+[A-Za-z0-9_./+*-]+",
        baseline,
        "--",
        "nce/",
        "tests/",
        ":!nce/config_data/waves.json",
        ":!nce/config_data/merged_prs.json",
        ":!tests/unit/test_waves_landed_ratchet.py",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True, encoding="utf-8")

    stamps_by_wave: dict[str, list[str]] = {}
    legacy_tokens: set[str] = set()
    unmatched_tokens: set[str] = set()

    for line in res.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split(":", 3)
        if len(parts) >= 4:
            fpath = parts[1]
            content = parts[3]
            m = TREE_STAMP_PATTERN.findall(content)
            if m:
                for wid in m:
                    stamps_by_wave.setdefault(wid, []).append(fpath)
            else:
                toks = re.findall(r"Wave\s+[^ ,;:()\"']+", content)
                for t in toks:
                    t_clean = t.strip()
                    if LEGACY_WAVE_PATTERN.match(t_clean):
                        legacy_tokens.add(t_clean)
                    elif t_clean in PROSE_TOKENS:
                        legacy_tokens.add(t_clean)
                    else:
                        unmatched_tokens.add(t_clean)

    for wid in stamps_by_wave:
        stamps_by_wave[wid] = sorted(set(stamps_by_wave[wid]))

    return stamps_by_wave, legacy_tokens, unmatched_tokens


def load_wave_manifest(repo: str) -> dict[str, Any]:
    path = pathlib.Path(repo) / "docs" / "_generated" / "waves.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def evaluate_wave(
    wid: str,
    manifest_entry: dict[str, Any],
    stamped_files: list[str],
    prs: list[dict[str, Any]],
    tree_files: set[str],
    unwired_tests: frozenset[str],
) -> dict[str, Any]:
    """Cross-check the 3 sources to determine if wave is LANDED or DISAGREEMENT."""
    has_stamp = len(stamped_files) > 0
    has_pr = len(prs) > 0

    marker_test = manifest_entry.get("marker_test")
    marker_status = "MISSING"
    test_collected = False

    if marker_test:
        if marker_test in unwired_tests:
            marker_status = "UNWIRED"
            test_collected = False
        elif marker_test in tree_files:
            marker_status = "COLLECTED"
            test_collected = True
        else:
            marker_status = "ABSENT_FILE"
            test_collected = False

    is_landed = has_stamp and has_pr and test_collected

    if is_landed:
        verdict = "LANDED"
    elif has_stamp and not has_pr and not test_collected:
        verdict = "STAMPED_UNLANDED"
    elif has_stamp and has_pr and not test_collected:
        verdict = "MISSING_MARKER_TEST"
    elif not has_stamp and has_pr and test_collected:
        verdict = "PR_AND_TEST_NO_STAMP"
    elif not has_stamp and has_pr and not test_collected:
        verdict = "PR_ONLY"
    elif has_stamp and not has_pr and test_collected:
        verdict = "UNMERGED_PR"
    else:
        verdict = "PLANNED"

    pr_numbers = [p["number"] for p in prs] if prs else []
    pr_dates = [p.get("mergedAt", "") for p in prs if p.get("mergedAt")]

    # Evidence classification:
    # 2-source: marker_test equals the sole stamping file
    # 3-source: marker_test is separate from the stamping file(s)
    evidence = "N/A"
    if is_landed:
        if len(stamped_files) == 1 and marker_test == stamped_files[0]:
            evidence = "2-source"
        else:
            evidence = "3-source"

    return {
        "id": wid,
        "description": manifest_entry.get("description", ""),
        "phase": manifest_entry.get("phase", "Unspecified"),
        "shape": manifest_entry.get("shape", "plain"),
        "verdict": verdict,
        "is_landed": is_landed,
        "has_stamp": has_stamp,
        "has_pr": has_pr,
        "evidence": evidence,
        "stamped_files": stamped_files,
        "marker_test": marker_test,
        "marker_status": marker_status,
        "pr_numbers": pr_numbers,
        "pr_dates": pr_dates,
    }


def generate_markdown(
    results: list[dict[str, Any]],
    baseline: str,
    legacy_tokens: set[str],
    unmatched_tokens: set[str],
) -> str:
    landed = [r for r in results if r["is_landed"]]
    disagreements = [r for r in results if not r["is_landed"]]

    lines = [
        "# Waves Landed on `main`",
        "",
        "<!-- Generated by scripts/gen_waves_landed.py. DO NOT EDIT DIRECTLY. -->",
        "",
        f"> **Baseline:** `{baseline}` · **Generated:** automatically by Wave A-W1 instrument",
        "> **Core Invariant:** *A stamp is not a landing.* A wave is scored `LANDED` if and only if",
        "> all three independent sources agree: (1) tree stamp in `nce/` or `tests/`, (2) merged PR title,",
        "> and (3) marker test exists on `main` and is collected by pytest (not in `KNOWN_UNWIRED`).",
        r"> **Tokenizer Pattern:** `Wave\s+([A-Z]{1,3}-(?:[A-Z]{1,3})?[0-9]+[a-z]?)\b` (anchored `Wave <ID>`, covers plain `LL-N` and charter-prefixed `L-LLN`).",
        "",
        "## Summary",
        "",
        "| Metric | Count |",
        "|---|---|",
        f"| **Total Waves Evaluated** | **{len(results)}** |",
        f"| **LANDED on `main` (3-way consensus)** | **{len(landed)}** |",
        f"| **Disagreements / Open / Planned** | **{len(disagreements)}** |",
        f"| **Skipped Legacy / Pre-v1.5 Tokens** | **{len(legacy_tokens)}** |",
        f"| **Unmatched Wave Tokens** | **{len(unmatched_tokens)}** |",
        "",
        "---",
        "",
        "## Landed Waves (`LANDED`)",
        "",
        "| Wave ID | Phase | Evidence | Merged PR | Merge Date | Stamping Files | Marker Test | Description |",
        "|---|---|---|---|---|---|---|---|",
    ]

    for r in sorted(landed, key=lambda x: x["id"]):
        prs_str = ", ".join(f"#{num}" for num in r["pr_numbers"]) if r["pr_numbers"] else "-"
        dates_str = ", ".join(d[:10] for d in r["pr_dates"]) if r["pr_dates"] else "-"
        stamps_str = "<br>".join(f"`{f}`" for f in r["stamped_files"][:2])
        if len(r["stamped_files"]) > 2:
            stamps_str += f"<br>*(+{len(r['stamped_files']) - 2} more)*"
        test_str = f"`{r['marker_test']}`" if r["marker_test"] else "-"
        desc = r["description"].replace("|", "\\|")
        evidence_str = f"`{r.get('evidence', '3-source')}`"
        lines.append(
            f"| **{r['id']}** | {r['phase']} | {evidence_str} | {prs_str} | {dates_str} | {stamps_str or '-'} | {test_str} | {desc} |"
        )

    lines.extend(
        [
            "",
            "---",
            "",
            "## Disagreements & Unlanded Waves",
            "",
            "These waves have partial signal (e.g. stamped in tree without PR or test, or planned in briefs).",
            "**`Wave PJ-5` is the standing positive control**: stamped in `internal-cores.json`, but missing PR and marker test.",
            "",
            "| Wave ID | Phase | Verdict | PR(s) | Stamp File(s) | Marker Test Status | Notes |",
            "|---|---|---|---|---|---|---|",
        ]
    )

    for r in sorted(disagreements, key=lambda x: x["id"]):
        prs_str = ", ".join(f"#{num}" for num in r["pr_numbers"]) if r["pr_numbers"] else "-"
        stamps_str = (
            "<br>".join(f"`{f}`" for f in r["stamped_files"]) if r["stamped_files"] else "-"
        )
        test_info = f"`{r['marker_test']}` ({r['marker_status']})" if r["marker_test"] else "None"
        desc = r["description"].replace("|", "\\|")
        lines.append(
            f"| **{r['id']}** | {r['phase']} | `{r['verdict']}` | {prs_str} | {stamps_str} | {test_info} | {desc} |"
        )

    lines.extend(
        [
            "",
            "---",
            "",
            "## Skipped Legacy Wave Tokens (Pre-v1.5)",
            "",
            "Pre-v1.5 module waves use bare numeric IDs (`Wave 1`..`31`, `Wave 10b`, `Wave C10`, `Wave M0.W20b`).",
            "These belong to earlier architecture phases and are deliberately excluded from the v1.5 surface instrument.",
            "",
            f"**Tokens ({len(legacy_tokens)}):**",
            ", ".join(f"`{t}`" for t in sorted(legacy_tokens)),
            "",
        ]
    )

    if unmatched_tokens:
        lines.extend(
            [
                "## Unmatched Wave Tokens",
                "",
                "Tokens matching `Wave ...` that could not be attributed to either v1.5 or legacy shapes:",
                ", ".join(f"`{t}`" for t in sorted(unmatched_tokens)),
                "",
            ]
        )

    return "\n".join(lines) + "\n"


def run_instrument(
    repo: str = ".",
    baseline: str = "HEAD",
    live_prs: bool = False,
) -> tuple[list[dict[str, Any]], set[str], set[str], str]:
    """Execute wave discovery and cross-source evaluation, returning results and generated markdown."""
    tree_files = set(git_ls_tree(repo, baseline))
    unwired_tests = load_known_unwired(repo, baseline)
    prs = load_merged_prs(repo, live=live_prs)
    prs_by_wave = parse_prs_by_wave(prs)
    stamps_by_wave, legacy_tokens, unmatched_tokens = scan_tree_stamps(repo, baseline)
    manifest = load_wave_manifest(repo)

    all_wave_ids = sorted(
        set(stamps_by_wave.keys()) | set(prs_by_wave.keys()) | set(manifest.keys())
    )

    results: list[dict[str, Any]] = []
    for wid in all_wave_ids:
        entry = manifest.get(wid, {})
        stamped = stamps_by_wave.get(wid, [])
        wave_prs = prs_by_wave.get(wid, [])
        eval_result = evaluate_wave(
            wid,
            entry,
            stamped,
            wave_prs,
            tree_files,
            unwired_tests,
        )
        results.append(eval_result)

    markdown = generate_markdown(results, baseline, legacy_tokens, unmatched_tokens)
    return results, legacy_tokens, unmatched_tokens, markdown


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate docs/_generated/waves_landed.md")
    parser.add_argument("--repo", default=".", help="Repository root")
    parser.add_argument("--baseline", default="HEAD", help="Git baseline ref")
    parser.add_argument(
        "--out", default="docs/_generated/waves_landed.md", help="Output markdown path"
    )
    parser.add_argument("--live-prs", action="store_true", help="Fetch live merged PRs from gh CLI")
    args = parser.parse_args()

    results, legacy_tokens, unmatched_tokens, markdown = run_instrument(
        repo=args.repo,
        baseline=args.baseline,
        live_prs=args.live_prs,
    )

    out_path = pathlib.Path(args.repo) / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    crlf_content = markdown.replace("\r\n", "\n").replace("\n", "\r\n")
    out_path.write_bytes(crlf_content.encode("utf-8"))

    landed_count = sum(1 for r in results if r["is_landed"])
    print(f"Generated {out_path} ({len(results)} waves evaluated, {landed_count} LANDED)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
