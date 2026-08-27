"""What commit this image was built from.

Populated at build time (``docker build --build-arg NCE_GIT_SHA=...``) and read
back at startup so a version-skew failure can name the image instead of leaving
the operator to work out which checkout is running.

Both values are advisory: an image built without the build args, or a process
started straight from a checkout, reports ``unknown`` and every caller must stay
correct in that case. Nothing here may fail — a missing stamp is a reporting
gap, never a reason to refuse a boot.
"""

from __future__ import annotations

import os

UNKNOWN = "unknown"


def git_sha() -> str:
    """The commit this image was built from, or ``"unknown"``."""
    return os.environ.get("NCE_GIT_SHA", "").strip() or UNKNOWN


def build_time() -> str:
    """When this image was built (ISO-8601), or ``"unknown"``."""
    return os.environ.get("NCE_BUILD_TIME", "").strip() or UNKNOWN


def describe() -> str:
    """One-line human description for log lines and error messages."""
    sha, built = git_sha(), build_time()
    if sha == UNKNOWN and built == UNKNOWN:
        return "image build info unavailable (NCE_GIT_SHA/NCE_BUILD_TIME unset)"
    return f"image built from {sha} at {built}"
