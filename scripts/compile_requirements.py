#!/usr/bin/env python3
"""Regenerate requirements.lock from requirements.txt (requires pip-tools).

Two modes, and the difference is load-bearing:

* **default (no flag)** -- ``pip-compile`` keeps every existing pin that still satisfies
  ``requirements.txt``.  This is what CI's blocking "Verify requirements.lock is current"
  gate runs, and what it checks is *consistency with the declared floors*, nothing more.
* ``--upgrade`` -- re-resolves every pin to the newest version the floors allow.  This is
  the only mode that picks up a patch release, and it is deliberately NOT what CI runs:
  with ``--upgrade`` the gate would go red the moment any upstream published anything.

IMPORTANT -- why this distinction matters.  On 2026-09-08 the committed lock was behind the actually
deployed set on **50 of 188 packages** -- ``cryptography`` 50.0.0 vs 50.0.1, ``torch``
2.13.0 vs 2.14.0 -- while the gate reported the lock "current" the whole time.  Both
statements were true: the lock agreed with the floors, and the floors are ``>=`` ranges
that the image build re-resolves to latest on every rebuild.  So the gate measured
agreement with a manifest, not currency against PyPI, and could not see the drift it was
introduced to prevent (see the ``aiosmtplib`` note in ``.github/workflows/ci.yml``).

Run ``--upgrade`` on a cadence (see ``.github/workflows/refresh-lock.yml``), never as part
of the merge gate.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "requirements.lock"
REQ = ROOT / "requirements.txt"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        # Deliberately not description=__doc__: the docstring carries non-ASCII, and
        # argparse writes help straight to a console that may be cp1252 on Windows,
        # which turns `--help` into a UnicodeEncodeError traceback.
        description="Regenerate requirements.lock from requirements.txt.",
    )
    parser.add_argument(
        "--upgrade",
        action="store_true",
        help=(
            "Re-resolve every pin to the newest version the floors allow. Required to pick "
            "up patch releases. Do NOT use in the CI merge gate -- see the module docstring."
        ),
    )
    args = parser.parse_args(argv)

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
    if args.upgrade:
        cmd.append("--upgrade")
    subprocess.check_call(cmd, cwd=ROOT)
    print(f"Wrote {LOCK}" + (" (--upgrade: pins re-resolved to latest)" if args.upgrade else ""))


if __name__ == "__main__":
    main()
