#!/usr/bin/env python3
"""Regenerate requirements.lock from requirements.txt (requires pip-tools)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "requirements.lock"
REQ = ROOT / "requirements.txt"


def main() -> None:
    if not REQ.is_file():
        raise SystemExit(f"Missing {REQ}")
    cmd = [
        sys.executable,
        "-m",
        "piptools",
        "compile",
        "requirements.txt",
        "--output-file",
        "requirements.lock",
        "--resolver=backtracking",
        "--strip-extras",
    ]
    subprocess.check_call(cmd, cwd=ROOT)
    print(f"Wrote {LOCK}")


if __name__ == "__main__":
    main()
