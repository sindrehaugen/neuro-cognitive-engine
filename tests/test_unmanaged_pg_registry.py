"""Contract: unmanaged_pg_connection bypasses RLS only at audited call sites."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from nce.db_utils import UNMANAGED_PG_AUDITED_SITES, unmanaged_pg_connection


def _collect_unmanaged_pg_sites(repo_root: Path) -> set[str]:
    sites: set[str] = set()
    for path in repo_root.rglob("*.py"):
        skip_dirs = {"tests", ".venv", "venv", "__pycache__", "node_modules", ".git"}
        if path.name.startswith(".") or skip_dirs.intersection(path.parts):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name != "unmanaged_pg_connection":
                continue
            site_kw = next((kw for kw in node.keywords if kw.arg == "site"), None)
            if site_kw is None or not isinstance(site_kw.value, ast.Constant):
                raise AssertionError(
                    f"{path}:{node.lineno}: unmanaged_pg_connection requires site=<str> keyword"
                )
            if not isinstance(site_kw.value.value, str):
                raise AssertionError(
                    f"{path}:{node.lineno}: site= must be a string literal for audit registry"
                )
            sites.add(site_kw.value.value)
    return sites


def test_unmanaged_pg_audited_sites_match_codebase() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    used = _collect_unmanaged_pg_sites(repo_root)
    assert used == set(UNMANAGED_PG_AUDITED_SITES), (
        f"registry drift: used={sorted(used)} audited={sorted(UNMANAGED_PG_AUDITED_SITES)}"
    )


@pytest.mark.asyncio
async def test_unmanaged_pg_rejects_unknown_site() -> None:
    from unittest.mock import MagicMock

    pool = MagicMock()
    with pytest.raises(ValueError, match="not audited"):
        async with unmanaged_pg_connection(pool, site="unknown.new_path"):
            pass  # pragma: no cover
