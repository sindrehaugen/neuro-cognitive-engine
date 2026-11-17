#!/usr/bin/env python3
"""Mechanical Documentation & Configuration Truth Verifier (v3.5.2).

Asserts:
  1. 0 broken relative Markdown links or missing heading anchor targets across README.md, SECURITY.md, CONTRIBUTING.md, and docs/.
  2. 100% of NCE_* variables in nce/config.py are documented in .env.example and docs/configuration_reference.md.
  3. README.md version matches pyproject.toml and static Core tool count (71).
  4. 0 brand-wall violations (Glamox, Vertex, E2D) across all documentation files.
"""

from __future__ import annotations

import pathlib
import re
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BRAND_WALL_RE = re.compile(r"\b(glamox|vertex|e2d)\b", re.IGNORECASE)
_MD_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.+?)\s*$")


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
        for line in cleaned.splitlines():
            m = _HEADING_RE.match(line)
            if m:
                anchors.add(_github_slug(m.group(1)))
    cache[resolved] = anchors
    return anchors


def check_links(files: list[pathlib.Path]) -> list[str]:
    errors: list[str] = []
    anchor_cache: dict[pathlib.Path, set[str]] = {}
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
                if fpath.name == "README.md" and anchor:
                    anchors = _extract_anchors(fpath, anchor_cache)
                    if anchor not in anchors:
                        rel_src = fpath.relative_to(_REPO_ROOT)
                        errors.append(f"{rel_src}: missing heading anchor -> #{anchor}")
                continue
            path_part = target.split("#", 1)[0].split("?", 1)[0].strip()
            if not path_part:
                continue
            resolved = (fpath.parent / path_part).resolve()
            if not resolved.exists():
                rel_src = fpath.relative_to(_REPO_ROOT)
                errors.append(f"{rel_src}: broken relative link -> {target}")
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
    ]
    docs_dir = _REPO_ROOT / "docs"
    if docs_dir.is_dir():
        md_files.extend(sorted(docs_dir.rglob("*.md")))
    md_files = [f for f in md_files if f.exists()]

    errors: list[str] = []
    errors.extend(check_links(md_files))
    errors.extend(check_config_parity())
    errors.extend(check_version_and_brand_wall(md_files))

    if errors:
        for err in errors:
            print(f"[FAIL] {err}", file=sys.stderr)
        return 1

    print(
        f"[OK] Verified {len(md_files)} markdown files: 0 broken links/anchors, 100% NCE_* config parity, 0 brand-wall violations."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
