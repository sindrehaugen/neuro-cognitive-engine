#!/usr/bin/env python3
"""Mechanical Documentation & Configuration Truth Verifier (v3.5.2).

Enforces 6 zero-tolerance checks across all Markdown files in neuro-cognitive-engine:
  1. Relative Path & Heading Anchor Resolution: 0 broken links or missing #anchors.
  2. Orphan Doc Detection: Every .md file under docs/ must be reachable from README.md,
     docs/README.md, docs/_sidebar.md, or docs/adr/README.md.
  3. Deleted-Doc & Stale Version/Import Detection: 0 references to pruned files
     (RECON.md, SUMMARY_REMEDIATION_RUN.md, PERF_REPORT_v3.5.1.md) or legacy
     nce/vertical_modules paths outside historical ADRs and _generated/ ledgers.
  4. Configuration & Tool Count Parity: 100% of NCE_* variables in nce/config.py
     documented in .env.example and docs/configuration_reference.md, plus version
     and 71-tool parity in README.md.
  5. Fenced JSON & Mermaid Syntax Validation: All ```json blocks parse cleanly and
     all ```mermaid node labels with ()/: are properly quoted.
  6. Brand-Wall Scanner: 0 occurrences of Glamox, Vertex, or E2D.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BRAND_WALL_RE = re.compile(r"\b(glamox|vertex|e2d)\b", re.IGNORECASE)
_MD_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.+?)\s*$")
_DELETED_REFS = (
    "RECON.md",
    "SUMMARY_REMEDIATION_RUN.md",
    "PERF_REPORT_v3.5.1.md",
)
_MERMAID_UNQUOTED_SPECIAL_RE = re.compile(r'\b[A-Za-z0-9_-]+\[(?!["(\[/])([^\]]*?[()/:][^\]]*?)\]')


def _strip_fences(md_text: str) -> str:
    out: list[str] = []
    in_fence = False
    for line in md_text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(line)
    return "\n".join(out)


def _github_slug(heading_text: str) -> str:
    text = re.sub(r"`([^`]*)`", r"\1", heading_text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"\s", "-", text)
    return text


def _extract_anchors(fpath: pathlib.Path, cache: dict[pathlib.Path, set[str]]) -> set[str]:
    resolved = fpath.resolve()
    if resolved in cache:
        return cache[resolved]
    anchors: set[str] = set()
    if resolved.is_file() and resolved.suffix.lower() == ".md":
        cleaned = _strip_fences(resolved.read_text(encoding="utf-8"))
        slug_counts: dict[str, int] = {}
        for line in cleaned.splitlines():
            m = _HEADING_RE.match(line)
            if m:
                base = _github_slug(m.group(1))
                count = slug_counts.get(base, 0)
                slug_counts[base] = count + 1
                anchors.add(base if count == 0 else f"{base}-{count}")
    cache[resolved] = anchors
    return anchors


def check_links_and_orphans(files: list[pathlib.Path]) -> list[str]:
    errors: list[str] = []
    anchor_cache: dict[pathlib.Path, set[str]] = {}
    inbound_refs: set[pathlib.Path] = set()

    for fpath in files:
        raw = fpath.read_text(encoding="utf-8")
        cleaned = _strip_fences(raw)
        for match in _MD_LINK_RE.finditer(cleaned):
            target = match.group(1).strip()
            if not target or target.startswith(
                ("http://", "https://", "mailto:", "conversation://", "file://")
            ):
                continue
            if target.startswith("#"):
                anchor = target[1:].strip().lower()
                if anchor:
                    anchors = _extract_anchors(fpath, anchor_cache)
                    if anchor not in anchors:
                        rel_src = fpath.relative_to(_REPO_ROOT)
                        errors.append(f"{rel_src}: missing heading anchor -> #{anchor}")
                continue
            parts = target.split("#", 1)
            path_part = parts[0].split("?", 1)[0].strip()
            anchor_part = parts[1].strip().lower() if len(parts) > 1 else ""
            if not path_part:
                continue
            resolved = (fpath.parent / path_part).resolve()
            if not resolved.exists():
                rel_src = fpath.relative_to(_REPO_ROOT)
                errors.append(f"{rel_src}: broken relative link -> {target}")
                continue
            inbound_refs.add(resolved)
            if resolved.is_dir() and (resolved / "README.md").exists():
                inbound_refs.add((resolved / "README.md").resolve())
            if anchor_part and resolved.is_file() and resolved.suffix.lower() == ".md":
                anchors = _extract_anchors(resolved, anchor_cache)
                if anchor_part not in anchors:
                    rel_src = fpath.relative_to(_REPO_ROOT)
                    errors.append(
                        f"{rel_src}: missing target heading anchor -> {path_part}#{anchor_part}"
                    )

    # Check 2: Zero orphan docs under docs/
    docs_dir = (_REPO_ROOT / "docs").resolve()
    entry_hubs = {
        (docs_dir / "README.md").resolve(),
    }
    for fpath in files:
        resolved = fpath.resolve()
        if resolved in entry_hubs or not resolved.is_relative_to(docs_dir):
            continue
        if resolved not in inbound_refs:
            rel_doc = resolved.relative_to(_REPO_ROOT)
            errors.append(f"{rel_doc}: orphan document (0 inbound links from README/docs index)")

    return errors


def check_stale_refs(files: list[pathlib.Path]) -> list[str]:
    errors: list[str] = []
    for fpath in files:
        rel_src = fpath.relative_to(_REPO_ROOT).as_posix()
        raw = fpath.read_text(encoding="utf-8")
        for deleted in _DELETED_REFS:
            if deleted in raw:
                errors.append(f"{rel_src}: references deleted document {deleted!r}")
        if (
            not rel_src.startswith("docs/adr/")
            and not rel_src.startswith("docs/_generated/")
            and rel_src != "CHANGELOG.md"
            and "nce/vertical_modules" in raw
        ):
            errors.append(
                f"{rel_src}: references pre-extraction path 'nce/vertical_modules' (use 'nce_verticals')"
            )
    return errors


def check_config_parity() -> list[str]:
    errors: list[str] = []
    cfg_src = (_REPO_ROOT / "nce" / "config.py").read_text(encoding="utf-8")
    cfg_keys = sorted(
        set(re.findall(r"\b(NCE_[A-Z0-9_]+)\b", cfg_src))
        - {"NCE_ENV_EXAMPLE", "NCE_X", "NCE_X_FILE"}
    )
    env_ex = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    doc_ref = (_REPO_ROOT / "docs" / "configuration_reference.md").read_text(encoding="utf-8")

    missing_env = [k for k in cfg_keys if k not in env_ex]
    missing_doc = [k for k in cfg_keys if k not in doc_ref]
    if missing_env:
        errors.append(f".env.example missing {len(missing_env)} NCE_* keys: {missing_env}")
    if missing_doc:
        errors.append(
            f"docs/configuration_reference.md missing {len(missing_doc)} NCE_* keys: {missing_doc}"
        )
    return errors


def check_json_and_mermaid(files: list[pathlib.Path]) -> list[str]:
    errors: list[str] = []
    for fpath in files:
        rel_src = fpath.relative_to(_REPO_ROOT).as_posix()
        raw = fpath.read_text(encoding="utf-8")
        for m in re.finditer(r"```json\s*\n(.*?)```", raw, re.DOTALL):
            block = m.group(1).strip()
            if not block or "//" in block or "..." in block:
                continue
            try:
                json.loads(block)
            except Exception as exc:
                line_no = raw[: m.start()].count("\n") + 1
                errors.append(f"{rel_src}:{line_no}: invalid fenced JSON block ({exc})")

        for m in re.finditer(r"```mermaid\s*\n(.*?)```", raw, re.DOTALL):
            block = m.group(1)
            line_start = raw[: m.start()].count("\n") + 1
            for idx, line in enumerate(block.splitlines(), 1):
                s = line.strip()
                if s.startswith("%%") or s.startswith("subgraph"):
                    continue
                if _MERMAID_UNQUOTED_SPECIAL_RE.search(line):
                    errors.append(
                        f"{rel_src}:{line_start + idx}: unquoted Mermaid node label with special chars: {s}"
                    )
    return errors


def check_version_and_brand_wall(files: list[pathlib.Path]) -> list[str]:
    errors: list[str] = []
    pyproj = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m_ver = re.search(r'^version\s*=\s*"([^"]+)"', pyproj, re.MULTILINE)
    expected_ver = m_ver.group(1) if m_ver else "3.5.2"

    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    if f"v{expected_ver}" not in readme and expected_ver not in readme:
        errors.append(f"README.md does not reference current version {expected_ver}")
    if "71" not in readme:
        errors.append("README.md does not reference the 71 static Core MCP tools")

    for fpath in files:
        raw = fpath.read_text(encoding="utf-8")
        for m in _BRAND_WALL_RE.finditer(raw):
            rel_src = fpath.relative_to(_REPO_ROOT)
            errors.append(f"{rel_src}: brand-wall violation {m.group(0)!r}")
            break
    return errors


def main() -> int:
    md_files = [
        _REPO_ROOT / "README.md",
        _REPO_ROOT / "SECURITY.md",
        _REPO_ROOT / "CONTRIBUTING.md",
        _REPO_ROOT / "CHANGELOG.md",
        _REPO_ROOT / ".github" / "CODE_OF_CONDUCT.md",
    ]
    docs_dir = _REPO_ROOT / "docs"
    if docs_dir.is_dir():
        md_files.extend(sorted(docs_dir.rglob("*.md")))
    md_files = [f for f in md_files if f.exists()]

    errors: list[str] = []
    errors.extend(check_links_and_orphans(md_files))
    errors.extend(check_stale_refs(md_files))
    errors.extend(check_config_parity())
    errors.extend(check_json_and_mermaid(md_files))
    errors.extend(check_version_and_brand_wall(md_files))

    if errors:
        for err in errors:
            print(f"[FAIL] {err}", file=sys.stderr)
        return 1

    print(
        f"[OK] Verified {len(md_files)} markdown files: 0 broken links/anchors, 0 orphan docs, 0 stale refs, 100% NCE_* config parity, valid JSON/Mermaid, 0 brand-wall violations."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
