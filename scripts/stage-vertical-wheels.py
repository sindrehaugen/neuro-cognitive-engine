#!/usr/bin/env python3
"""Stage pure-Python plugin wheels into ``deploy/plugins/`` for Docker builds.

Discovers sibling plugin repositories under ``../*`` (and optional paths in
``NCE_EXTRA_PLUGIN_DIRS``) whose ``pyproject.toml`` declares
``[project.entry-points."nce.plugins"]``, builds each into a temporary directory
using ``pip wheel --no-deps``, verifies each output is a pure
``*-py3-none-any.whl`` archive, prunes stale wheels for those distributions from
``deploy/plugins/``, and copies the newly built wheels in.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PLUGINS_DIR = _REPO_ROOT / "deploy" / "plugins"
_PLUGIN_ENTRYPOINT_RE = re.compile(
    r'^\s*\[\s*project\.entry-points\.(?:"nce\.plugins"|\'nce\.plugins\'|nce\.plugins)\s*\]',
    re.MULTILINE,
)
_PROJECT_NAME_RE = re.compile(r'^\s*name\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def _discover_plugin_sources() -> list[tuple[Path, str]]:
    """Return sorted ``(repo_dir, stale_wheel_glob)`` pairs for local plugin repos."""
    candidates: list[Path] = []
    parent_dir = _REPO_ROOT.parent
    if parent_dir.is_dir():
        candidates.extend(sorted(p for p in parent_dir.iterdir() if p.is_dir()))

    extra_raw = os.environ.get("NCE_EXTRA_PLUGIN_DIRS", "")
    for chunk in re.split(rf"[,{re.escape(os.pathsep)}]", extra_raw):
        cleaned = chunk.strip()
        if cleaned:
            candidates.append(Path(cleaned).expanduser().resolve())

    discovered: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved == _REPO_ROOT.resolve() or resolved in seen:
            continue
        pyproject = resolved / "pyproject.toml"
        if not pyproject.is_file():
            continue
        raw = pyproject.read_text(encoding="utf-8", errors="ignore")
        if not _PLUGIN_ENTRYPOINT_RE.search(raw):
            continue
        m_name = _PROJECT_NAME_RE.search(raw)
        if not m_name:
            continue
        dist_norm = re.sub(r"[-_.]+", "_", m_name.group(1).strip()).lower()
        seen.add(resolved)
        discovered.append((resolved, f"{dist_norm}-*.whl"))
    return discovered


def _stage_one(src_dir: Path, stale_glob: str, *, optional: bool) -> Path | None:
    if not (src_dir / "pyproject.toml").is_file():
        msg = f"[stage-vertical-wheels] Source directory not found: {src_dir}"
        if optional:
            print(f"{msg} (skipped --optional)", file=sys.stderr)
            return None
        raise FileNotFoundError(msg)

    _PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    for stale in _PLUGINS_DIR.glob(stale_glob):
        stale.unlink()

    with tempfile.TemporaryDirectory(prefix="nce-wheel-") as tmp:
        tmp_path = Path(tmp)
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            str(src_dir),
            "-w",
            str(tmp_path),
        ]
        subprocess.run(cmd, check=True)
        built = sorted(tmp_path.glob("*.whl"))
        if len(built) != 1:
            raise RuntimeError(f"Expected 1 wheel from {src_dir}, found {len(built)}: {built}")
        wheel_path = built[0]
        if not wheel_path.name.endswith("-py3-none-any.whl"):
            raise RuntimeError(
                f"Refusing non-pure wheel {wheel_path.name!r}; expected *-py3-none-any.whl"
            )
        dest = _PLUGINS_DIR / wheel_path.name
        shutil.copy2(wheel_path, dest)
        return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage NCE external plugin wheels.")
    parser.add_argument(
        "--optional",
        action="store_true",
        help="Warn and continue if plugin source directories are missing.",
    )
    args = parser.parse_args(argv)

    sources = _discover_plugin_sources()
    if not sources:
        if not args.optional:
            print(
                "[stage-vertical-wheels] No sibling plugin repositories with nce.plugins entry points found.",
                file=sys.stderr,
            )
        print("[stage-vertical-wheels] No plugin wheels staged.")
        return 0

    staged: list[Path] = []
    for src_dir, stale_glob in sources:
        dest = _stage_one(src_dir, stale_glob, optional=args.optional)
        if dest is not None:
            staged.append(dest)
            print(f"[stage-vertical-wheels] Staged {dest.name} ({dest.stat().st_size} bytes)")

    if not staged:
        print("[stage-vertical-wheels] No plugin wheels staged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
