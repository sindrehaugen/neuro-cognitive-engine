"""Vendor-extensible log-profile registry for the Diagnostics Engine.

Each vendor plugs in via ``register_profile``; callers use ``get_profile``
to obtain the matching ``LogProfile`` (or the generic fallback when no vendor-
specific profile exists).  This seam is the only point of contact between the
diagnostics pipeline core (Batch 71 streaming, 72 enrichment) and the
per-vendor anomaly vocabulary.

Registry idiom mirrors ``nce/extractors/dispatch.py`` — the ``_REGISTRY``
dict + a thin ``register_*`` / ``get_*`` pair.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ── Data model ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LogProfile:
    """Immutable description of how to classify log lines from one vendor.

    Attributes:
        name:     Canonical profile identifier (e.g. ``"mtr"``, ``"generic"``).
        patterns: Ordered tuple of ``(anomaly_type, compiled_regex, severity)``
                  where *severity* follows syslog conventions (0 = emergency,
                  7 = debug).  Lower numbers indicate higher urgency.
    """

    name: str
    patterns: tuple[tuple[str, re.Pattern[str], int], ...]

    def classify(self, line: str) -> tuple[str, int] | None:
        """Return ``(anomaly_type, severity)`` for the first matching pattern.

        Patterns are evaluated in declaration order; the first match wins.
        Returns ``None`` when no pattern matches the line.
        """
        for anomaly_type, pattern, severity in self.patterns:
            if pattern.search(line):
                return (anomaly_type, severity)
        return None


# ── Registry ──────────────────────────────────────────────────────────────────

_REGISTRY: dict[str, LogProfile] = {}


def register_profile(name: str, profile: LogProfile) -> None:
    """Register *profile* under *name*.

    Re-registration is intentionally allowed so that test fixtures and
    third-party vendor plugins can replace stub entries with real ones.
    """
    _REGISTRY[name] = profile


def get_profile(name: str) -> LogProfile:
    """Return the ``LogProfile`` registered under *name*.

    Falls back to the ``"generic"`` profile when no vendor-specific entry
    exists.  The generic profile is always present (seeded below) so this
    function never raises ``KeyError``.
    """
    return _REGISTRY.get(name, _REGISTRY["generic"])


# ── Seed: generic ─────────────────────────────────────────────────────────────
# Broad fault-signal patterns that work across vendor log formats.
# Severity 3 = error, 2 = critical, 0 = emergency (syslog scale).

_GENERIC_PATTERNS: tuple[tuple[str, re.Pattern[str], int], ...] = (
    ("fatal_error", re.compile(r"\bFATAL\b", re.IGNORECASE), 0),
    ("critical_error", re.compile(r"\bCRITICAL\b", re.IGNORECASE), 2),
    ("error", re.compile(r"\bERROR\b", re.IGNORECASE), 3),
)

register_profile(
    "generic",
    LogProfile(name="generic", patterns=_GENERIC_PATTERNS),
)

# ── Seed: mtr ─────────────────────────────────────────────────────────────────
# Mersive Solstice / Microsoft Teams Room appliance log patterns.
# Sample lines (anonymised):
#   "PTP desync detected: offset 523 us exceeds threshold"
#   "USB disconnect event on port 3 (device: Jabra Speak 510)"
#   "HDMI HPD failure on output 1 — no display detected"
#   "Teams app crash: com.microsoft.teams2 exit code 134"

_MTR_PATTERNS: tuple[tuple[str, re.Pattern[str], int], ...] = (
    (
        "ptp_desync",
        re.compile(r"PTP\s+desync", re.IGNORECASE),
        3,
    ),
    (
        "usb_disconnect",
        re.compile(r"USB\s+disconnect", re.IGNORECASE),
        4,
    ),
    (
        "hdmi_hpd_fail",
        re.compile(r"HDMI\s+HPD\s+fail(?:ure)?", re.IGNORECASE),
        3,
    ),
    (
        "teams_app_crash",
        re.compile(r"Teams\s+app\s+crash", re.IGNORECASE),
        2,
    ),
)

register_profile(
    "mtr",
    LogProfile(name="mtr", patterns=_MTR_PATTERNS),
)

# ── Stubs: delegate to generic until real vendor samples exist ─────────────────
# TODO(batch-71+): replace each stub with real anomaly vocabularies once
#   representative log samples are collected from field deployments.
#   Vendors: Crestron (UC processors), Biamp (audio DSPs),
#   Netgear AVLine (AV switches), Yealink (video bars), Neat (video bars).

for _vendor in ("crestron", "biamp", "netgear_avline", "yealink", "neat"):
    register_profile(
        _vendor,
        LogProfile(name=_vendor, patterns=_REGISTRY["generic"].patterns),
    )
