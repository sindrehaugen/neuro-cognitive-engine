"""Documentation product version claims ratchet.

Ensures that every document stating an NCE product version matches the authoritative
single source of truth in ``pyproject.toml:3``.

Modelled on ``tests/test_docs_consistency.py``:
- Reads the authoritative version dynamically from ``pyproject.toml`` (never ``nce.__version__``,
  which resolves to ``0.0.0+dev`` in source checkouts).
- Scans ``README.md`` and all ``docs/**/*.md`` for product version claims (e.g. ``NCE v<ver>``,
  shields badge ``version-<ver>-blue``, ``NCE version <ver>``, etc.).
- Asserts that every product version claim equals the expected version from ``pyproject.toml``.
- Enforces an explicit site floor (``_MIN_PRODUCT_VERSION_SITES``) so deletion or silent
  re-wording of version claims fails loudly instead of passing vacuously.
- Scans all docs for any ``v1.0`` / ``version 1.0`` claims and asserts that any surviving
  mention is recorded in an explicit, commented allowlist (``_EXEMPT_V1_0_SITES``) covering
  Microsoft Graph API endpoints (``/v1.0/``) and the architecture spec revision label (``Spec v1.0``).
"""

from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PYPROJECT = _ROOT / "pyproject.toml"
_DOCS_DIR = _ROOT / "docs"
_README = _ROOT / "README.md"

# (label, pattern) capturing (version_string) for product version claims.
_PRODUCT_VERSION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "shields badge",
        re.compile(r"badge/version-([0-9]+(?:\.[0-9]+)+)-blue"),
    ),
    (
        "tagline footer",
        re.compile(r"NCE\s+[—–-]\s+[^·\n]*·\s*v([0-9]+(?:\.[0-9]+)+)\b"),
    ),
    (
        "prose NCE vX.Y.Z",
        re.compile(r"\bNCE\*{0,2}\s+\*{0,2}v([0-9]+(?:\.[0-9]+)+)\b"),
    ),
    (
        "prose NCE version X.Y.Z",
        re.compile(r"\bNCE\*{0,2}\s+\*{0,2}version\s+([0-9]+(?:\.[0-9]+)+)\b"),
    ),
    (
        "prose the vX.Y.Z paths",
        re.compile(r"\bthe\s+v([0-9]+(?:\.[0-9]+)+)\s+paths\b"),
    ),
    (
        "c4 diagram NCE vX.Y.Z",
        re.compile(r"NCE\[\"Neuro Cognitive Engine \(NCE\)\\nv([0-9]+(?:\.[0-9]+)+)\"\]"),
    ),
)

# Minimum number of product version claim sites expected across README.md and docs/**/*.md.
# If a doc edit removes or rewords a version claim past the pattern, this floor fails loudly.
_MIN_PRODUCT_VERSION_SITES = 9

# Explicit, commented allowlist for non-product v1.0 mentions.
# Every entry is mapped by (relative_posix_path, line_content_substring) with an explicit rationale.
# This prevents silencing real product-version gaps behind a widened regex.
_EXEMPT_V1_0_SITES: dict[tuple[str, str], str] = {
    # --- Bucket 2: Microsoft Graph API endpoints (external vendor path, DO NOT TOUCH) ---
    (
        "docs/bridge_setup_guide.md",
        "POST https://graph.microsoft.com/v1.0/subscriptions",
    ): "Microsoft Graph API v1.0 webhook registration path",
    (
        "docs/bridge_setup_guide.md",
        "PATCH /v1.0/subscriptions/{id}",
    ): "Microsoft Graph API v1.0 subscription renewal path",
    (
        "docs/bridge_setup_guide.md",
        "SP[PATCH /v1.0/subscriptions/id",
    ): "Microsoft Graph API v1.0 mermaid renewal flow",
    (
        "docs/engines/system-design-admin.md",
        "https://graph.microsoft.com/v1.0",
    ): "Microsoft Graph API v1.0 endpoint documentation",
    (
        "docs/service_integrations.md",
        "PATCH https://graph.microsoft.com/v1.0/subscriptions/{id}",
    ): "Microsoft Graph API v1.0 subscription renewal table entry",
    (
        "docs/service_integrations.md",
        "BR->>SP: PATCH /v1.0/subscriptions/{id}",
    ): "Microsoft Graph API v1.0 mermaid sequence diagram",

    # --- Bucket 3: Architecture specification revision label (Spec v1.0) ---
    (
        "docs/architecture-v1.md",
        "Spec v1.0",
    ): "Architecture specification title spec-revision label",
    (
        "docs/architecture-v1.md",
        "v1.0 revision of the architecture specification",
    ): "Architecture specification explicit version note",
    (
        "docs/README.md",
        "architecture-v1.md",
    ): "Inbound navigation link to architecture spec v1.0",
    (
        "docs/_sidebar.md",
        "architecture-v1.md",
    ): "Sidebar navigation link to architecture spec v1.0",
    (
        "docs/_404.md",
        "architecture-v1.md",
    ): "404 quick entrypoint link to architecture spec v1.0",
    (
        "docs/quick_start.md",
        "architecture-v1.md",
    ): "Quick start reference link to architecture spec v1.0",
    (
        "docs/recursive_indexing_flow.md",
        "architecture-v1.md",
    ): "Indexing flow reference link to architecture spec v1.0",
}


def _read_expected_version() -> str:
    """Read the authoritative project version from pyproject.toml:3."""
    pyproject_text = _PYPROJECT.read_text(encoding="utf-8")
    m = re.search(r"version\s*=\s*\"([^\"]+)\"", pyproject_text)
    assert m, f"pyproject.toml at {_PYPROJECT} must declare a project version"
    return m.group(1)


def _all_markdown_files() -> list[pathlib.Path]:
    """Return all markdown files in README.md and docs/**/*.md."""
    files: list[pathlib.Path] = []
    if _README.is_file():
        files.append(_README)
    if _DOCS_DIR.is_dir():
        files.extend(sorted(_DOCS_DIR.rglob("*.md")))
    return [f for f in dict.fromkeys(files) if f.is_file()]


def test_product_version_claims_match_pyproject() -> None:
    """Every product-version claim in docs and README must match pyproject.toml."""
    expected = _read_expected_version()
    assert expected, "Expected version must not be empty"

    found: list[tuple[str, int, str, str]] = []  # (path, line_no, label, got_version)
    for path in _all_markdown_files():
        rel = path.relative_to(_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        for label, pattern in _PRODUCT_VERSION_PATTERNS:
            for m in pattern.finditer(text):
                line_no = text.count("\n", 0, m.start(1)) + 1
                got = m.group(1)
                found.append((rel, line_no, label, got))

    assert len(found) >= _MIN_PRODUCT_VERSION_SITES, (
        f"Only {len(found)} product version claims matched across docs, but at least "
        f"{_MIN_PRODUCT_VERSION_SITES} are expected. A claim was deleted or reworded past its "
        f"pattern. Matched: {found}"
    )

    wrong = [item for item in found if item[3] != expected]
    assert not wrong, (
        f"Documentation states wrong product version; pyproject.toml declares {expected!r}. "
        "Wrong sites: "
        + "; ".join(f"{rel}:{line_no} says {got!r} ({lbl})" for rel, line_no, lbl, got in wrong)
    )


def test_no_unexempt_v1_legacy_version_claims() -> None:
    """No unexempted v1.0 or version 1.0 claims may exist in docs or README.

    Microsoft Graph API endpoints (/v1.0/) and the architecture spec revision label
    (Spec v1.0) are explicitly allowlisted in _EXEMPT_V1_0_SITES. Any other v1.0
    occurrence indicates a stale product version claim that must be upgraded to 3.0.0.
    """
    expected = _read_expected_version()
    v1_pattern = re.compile(r"\b(?:version[: -]+|v)1\.0(?:\.0)?\b", re.IGNORECASE)

    unexempt: list[tuple[str, int, str]] = []
    matched_exemptions: set[tuple[str, str]] = set()

    for path in _all_markdown_files():
        rel = path.relative_to(_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), start=1):
            if v1_pattern.search(line):
                is_covered = False
                for (exempt_path, exempt_substr), _reason in _EXEMPT_V1_0_SITES.items():
                    if rel == exempt_path and exempt_substr in line:
                        is_covered = True
                        matched_exemptions.add((exempt_path, exempt_substr))
                        break
                if not is_covered:
                    unexempt.append((rel, line_no, line.strip()))

    assert not unexempt, (
        f"Found {len(unexempt)} unexempted v1.0 version claim(s) across documentation! "
        f"Authoritative product version is {expected!r} (pyproject.toml:3). "
        "Unexempt sites: "
        + "; ".join(f"{rel}:{ln} -> {content!r}" for rel, ln, content in unexempt)
    )

    missing_exemptions = set(_EXEMPT_V1_0_SITES.keys()) - matched_exemptions
    assert not missing_exemptions, (
        f"Stale exemptions in _EXEMPT_V1_0_SITES that did not match any line: {missing_exemptions}"
    )
