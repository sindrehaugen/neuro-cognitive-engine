"""Batch 70 — log-profiles.

Unit tests for ``nce.vertical_modules.diagnostics.profiles``.

All tests are pure-unit: no Docker, no network, no database.
"""

from __future__ import annotations

import re

import pytest

from nce.vertical_modules.diagnostics.profiles import (
    _REGISTRY,
    LogProfile,
    get_profile,
    register_profile,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def clean_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Snapshot _REGISTRY before the test and restore it afterwards.

    This prevents register_profile calls inside a test from leaking into
    subsequent tests.
    """
    snapshot = dict(_REGISTRY)
    yield  # type: ignore[misc]
    _REGISTRY.clear()
    _REGISTRY.update(snapshot)


# ── register_profile / get_profile round-trip ─────────────────────────────────


def test_register_then_get_returns_same_profile(clean_registry: None) -> None:
    """A registered profile is returned verbatim by get_profile."""
    profile = LogProfile(
        name="test_vendor",
        patterns=(("some_error", re.compile(r"BOOM"), 3),),
    )
    register_profile("test_vendor", profile)
    assert get_profile("test_vendor") is profile


def test_register_overwrites_existing_entry(clean_registry: None) -> None:
    """Re-registering under the same name replaces the previous entry."""
    p1 = LogProfile(name="v1", patterns=())
    p2 = LogProfile(name="v2", patterns=())
    register_profile("overwrite_me", p1)
    register_profile("overwrite_me", p2)
    assert get_profile("overwrite_me") is p2


# ── Generic fallback ──────────────────────────────────────────────────────────


def test_get_profile_unknown_name_returns_generic() -> None:
    """get_profile with an unregistered name returns the generic profile."""
    result = get_profile("__nonexistent_vendor_xyz__")
    assert result.name == "generic"


def test_generic_profile_is_always_present() -> None:
    """The generic profile is seeded at import time."""
    profile = get_profile("generic")
    assert profile.name == "generic"
    assert len(profile.patterns) > 0


def test_generic_fallback_never_raises_key_error() -> None:
    """get_profile must not raise even for arbitrary unknown strings."""
    for key in ("", "NONEXISTENT", "123", "vendor/subtype"):
        result = get_profile(key)
        assert result is not None


# ── Generic profile pattern tests ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("line", "expected_anomaly"),
    [
        ("2024-01-15 08:00:00 FATAL disk failure detected", "fatal_error"),
        ("CRITICAL: temperature threshold exceeded", "critical_error"),
        ("ERROR: connection to upstream timed out", "error"),
        ("[FATAL] kernel panic — not syncing", "fatal_error"),
    ],
)
def test_generic_classify_matches_expected_anomaly(line: str, expected_anomaly: str) -> None:
    """Generic profile classifies well-known severity keywords."""
    profile = get_profile("generic")
    result = profile.classify(line)
    assert result is not None, f"expected a match for: {line!r}"
    anomaly_type, _severity = result
    assert anomaly_type == expected_anomaly


def test_generic_classify_returns_none_for_clean_line() -> None:
    """A line with no fault keywords returns None."""
    profile = get_profile("generic")
    assert profile.classify("2024-01-15 INFO system started normally") is None


# ── MTR seed patterns ─────────────────────────────────────────────────────────


def test_mtr_profile_is_seeded() -> None:
    """The mtr profile is registered at import time."""
    profile = get_profile("mtr")
    assert profile.name == "mtr"


@pytest.mark.parametrize(
    ("line", "expected_anomaly"),
    [
        (
            "PTP desync detected: offset 523 us exceeds threshold",
            "ptp_desync",
        ),
        (
            "USB disconnect event on port 3 (device: Jabra Speak 510)",
            "usb_disconnect",
        ),
        (
            "HDMI HPD failure on output 1 — no display detected",
            "hdmi_hpd_fail",
        ),
        (
            "Teams app crash: com.microsoft.teams2 exit code 134",
            "teams_app_crash",
        ),
        # Case-insensitive variants
        (
            "ptp desync: high jitter measured",
            "ptp_desync",
        ),
        (
            "usb disconnect: keyboard removed",
            "usb_disconnect",
        ),
        (
            "hdmi hpd fail: port 2 no signal",
            "hdmi_hpd_fail",
        ),
        (
            "teams app crash: uncaught exception",
            "teams_app_crash",
        ),
    ],
)
def test_mtr_classify_matches_sample_lines(line: str, expected_anomaly: str) -> None:
    """Seeded MTR patterns match representative log lines."""
    profile = get_profile("mtr")
    result = profile.classify(line)
    assert result is not None, f"expected MTR pattern match for: {line!r}"
    anomaly_type, _severity = result
    assert anomaly_type == expected_anomaly


def test_mtr_classify_returns_none_for_clean_line() -> None:
    """A clean MTR line with no anomaly yields None."""
    profile = get_profile("mtr")
    assert profile.classify("2024-01-15 INFO Teams meeting started") is None


# ── LogProfile.classify contract ──────────────────────────────────────────────


def test_classify_returns_tuple_of_str_and_int() -> None:
    """classify return type is (str, int) when a pattern matches."""
    profile = get_profile("mtr")
    result = profile.classify("USB disconnect event detected")
    assert result is not None
    anomaly_type, severity = result
    assert isinstance(anomaly_type, str)
    assert isinstance(severity, int)


def test_classify_first_match_wins() -> None:
    """When multiple patterns match, the first declared pattern takes priority."""
    patterns: tuple[tuple[str, re.Pattern[str], int], ...] = (
        ("first", re.compile(r"overlap"), 5),
        ("second", re.compile(r"over"), 4),
    )
    profile = LogProfile(name="priority_test", patterns=patterns)
    result = profile.classify("overlap and over")
    assert result is not None
    assert result[0] == "first"


def test_classify_empty_patterns_always_returns_none() -> None:
    """A profile with no patterns never matches any line."""
    profile = LogProfile(name="empty", patterns=())
    assert profile.classify("FATAL ERROR CRITICAL") is None


# ── Vendor stub profiles ───────────────────────────────────────────────────────


@pytest.mark.parametrize("vendor", ["crestron", "biamp", "netgear_avline", "yealink", "neat"])
def test_vendor_stub_is_registered(vendor: str) -> None:
    """Stub vendor profiles are seeded and fall back to generic patterns."""
    profile = get_profile(vendor)
    assert profile is not None
    # Stubs reuse generic patterns — they must still classify generic keywords
    result = profile.classify("ERROR: device offline")
    assert result is not None
    anomaly_type, _severity = result
    assert anomaly_type == "error"


# ── Frozen dataclass immutability ─────────────────────────────────────────────


def test_log_profile_is_frozen() -> None:
    """LogProfile is a frozen dataclass — mutation must raise."""
    profile = get_profile("generic")
    with pytest.raises((AttributeError, TypeError)):
        profile.name = "mutated"  # type: ignore[misc]
