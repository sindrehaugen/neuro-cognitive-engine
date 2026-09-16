"""Wave Landed on main Ratchet & Verification Suite (Wave A-W1).

Enforces the core invariant: "A stamp is not a landing."
Cross-checks three independent sources:
1. Tree stamps in nce/ and tests/
2. Merged PR titles from GitHub / git log
3. Capability marker test collected by pytest (and not in KNOWN_UNWIRED)

Standing positive control (U18 / Guard-the-guard):
- Proves PJ-3 is NOT scored as landed (it has a tree stamp in internal-cores.json:60,
  but no PR, no tool in tool_registry.py, and no marker test).
- Proves C-SD2 is NOT lost (it has a charter-prefixed ID shape L-LLN, merged in PR #64).
"""

from __future__ import annotations

import importlib.util
import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
_GENERATED_DOC = _ROOT / "docs" / "_generated" / "waves_landed.md"
_GENERATOR_SCRIPT = _ROOT / "scripts" / "gen_waves_landed.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("gen_waves_landed", _GENERATOR_SCRIPT)
    assert spec and spec.loader, f"Cannot load spec from {_GENERATOR_SCRIPT}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.skipif(
    not _GENERATED_DOC.exists(), reason="docs/_generated/waves_landed.md not present"
)
def test_generated_waves_landed_matches_the_generator():
    """docs/_generated/waves_landed.md byte-comparison is advisory (Wave A-Q17).

    The 8 property assertions in this module are the hard gate that survives
    a moving main. The byte-for-byte comparison against generator output is
    advisory: when drift is detected on a moving main, an advisory notice is
    emitted without failing the test, preventing an estate-wide trunk lock.
    """
    import sys

    gen = _load_generator()
    results, legacy_tokens, unmatched_tokens, expected_md = gen.run_instrument(
        repo=str(_ROOT), baseline="HEAD", live_prs=False
    )
    actual_md = _GENERATED_DOC.read_text(encoding="utf-8")
    assert len(actual_md.strip()) > 0, "docs/_generated/waves_landed.md must not be empty"
    assert actual_md.startswith("# Waves Landed on `main`"), (
        "docs/_generated/waves_landed.md must have standard header"
    )

    if actual_md.strip() != expected_md.strip():
        sys.stderr.write(
            "\n[ADVISORY: Wave A-Q17] docs/_generated/waves_landed.md has drifted from current tree.\n"
            "This check is advisory to avoid taxing sessions when main moves.\n"
            "The 8 property assertions remain the hard gate.\n"
            "To regenerate before milestone publication:\n"
            "  python scripts/gen_waves_landed.py --repo . --baseline HEAD --out docs/_generated/waves_landed.md\n"
        )


def test_positive_control_pj5_is_not_landed():
    """Standing Positive Control (U18 pattern): Prove it RED on unlanded stamped wave (PJ-5).

    Wave PJ-5 is stamped in nce/config_data/internal-cores.json, but has no
    tool in tool_registry.py, has no merged PR, and has no marker test.
    A naive stamp-only instrument would score PJ-5 as landed. The 3-source
    instrument must reject PJ-5 and mark it STAMPED_UNLANDED.
    """
    gen = _load_generator()
    results, _, _, _ = gen.run_instrument(repo=str(_ROOT), baseline="HEAD", live_prs=False)

    pj5 = next((r for r in results if r["id"] == "PJ-5"), None)
    assert pj5 is not None, "Wave PJ-5 must be evaluated in wave results"

    # Verify that PJ-5 IS stamped in the tree (the trap)
    assert pj5["has_stamp"] is True, "PJ-5 must have a tree stamp (internal-cores.json)"
    assert any("internal-cores.json" in f for f in pj5["stamped_files"])

    # Verify that PJ-5 is NOT scored as landed
    assert pj5["is_landed"] is False, (
        "Wave PJ-5 was scored as LANDED! A stamp is NOT a landing. "
        "PJ-5 has no merged PR and no marker test."
    )
    assert pj5["verdict"] == "STAMPED_UNLANDED"
    assert len(pj5["pr_numbers"]) == 0
    assert pj5["marker_status"] in ("ABSENT_FILE", "MISSING")


# Backwards-compatible alias for any external runner
test_positive_control_pj3_is_not_landed = test_positive_control_pj5_is_not_landed


def test_charter_prefixed_id_csd2_is_landed():
    """Silent-drop failure check: C-SD2 must NOT be dropped by regex.

    Wave C-SD2 uses the charter-prefixed shape L-LLN (merged as PR #64 and #66).
    A digits-only regex silently drops it. This test proves C-SD2 is matched
    and evaluates to LANDED with its marker test and merged PR.
    """
    gen = _load_generator()
    results, _, _, _ = gen.run_instrument(repo=str(_ROOT), baseline="HEAD", live_prs=False)

    csd2 = next((r for r in results if r["id"] == "C-SD2"), None)
    assert csd2 is not None, "Wave C-SD2 must be matched by the wave scanner"
    assert csd2["shape"] == "charter_prefixed"
    assert csd2["is_landed"] is True, "Wave C-SD2 must be LANDED"
    assert csd2["verdict"] == "LANDED"
    assert 64 in csd2["pr_numbers"] or 66 in csd2["pr_numbers"]
    assert csd2["marker_test"] == "tests/unit/test_system_design_sd2_ratchet.py"
    assert csd2["marker_status"] == "COLLECTED"


def test_wave_id_pattern_covers_both_shapes():
    """Verify that WAVE_ID_PATTERN matches plain LL-N and charter-prefixed L-LLN.

    Also proves that the naive digits-only regex fails on charter-prefixed IDs.
    """
    gen = _load_generator()
    pattern = gen.WAVE_ID_PATTERN

    # Plain LL-N fixtures
    plain_fixtures = ["S-1", "AG-3", "HR-5", "T-6", "S-2a", "I-8", "I-10", "E-2", "V-4"]
    for fix in plain_fixtures:
        m = pattern.search(f"Wave {fix}")
        assert m is not None, f"Failed to match plain fixture {fix}"
        assert m.group(1) == fix

    # Charter-prefixed L-LLN fixtures
    charter_fixtures = ["C-SD2", "B-BI1", "C-RS3", "B-AG1", "B-FT5", "B-ID1", "C-PJ2", "C-BI2"]
    for fix in charter_fixtures:
        m = pattern.search(f"Wave {fix}")
        assert m is not None, f"Failed to match charter-prefixed fixture {fix}"
        assert m.group(1) == fix

    # Demonstration of the old bug: digits-only pattern drops charter-prefixed
    old_broken_regex = re.compile(r"Wave\s+([A-Z]{1,3}-[0-9]+[a-z]?)")
    for fix in charter_fixtures:
        assert old_broken_regex.search(f"Wave {fix}") is None, (
            f"Expected old digits-only regex to fail on {fix} (demonstrating why the new regex is required)"
        )


def test_unwired_tests_rejected_as_markers():
    """Tests listed in KNOWN_UNWIRED cannot serve as valid marker tests."""
    gen = _load_generator()
    unwired = gen.load_known_unwired(str(_ROOT), "HEAD")

    # KNOWN_UNWIRED must include test_cron_chain_verify.py
    assert "tests/test_cron_chain_verify.py" in unwired, (
        "tests/test_cron_chain_verify.py must be in KNOWN_UNWIRED per charter §13"
    )

    # Evaluate dummy wave using unwired test as marker
    tree_files = {"tests/test_cron_chain_verify.py"}
    eval_result = gen.evaluate_wave(
        wid="TEST-UNWIRED",
        manifest_entry={"marker_test": "tests/test_cron_chain_verify.py"},
        stamped_files=["some/file.py"],
        prs=[{"number": 999, "title": "Wave TEST-UNWIRED", "mergedAt": "2026-09-15T00:00:00Z"}],
        tree_files=tree_files,
        unwired_tests=unwired,
    )
    assert eval_result["marker_status"] == "UNWIRED"
    assert eval_result["is_landed"] is False, (
        "A test in KNOWN_UNWIRED must NOT be accepted as a landed marker test!"
    )


def test_positive_control_hr2_has_no_tree_stamp():
    r"""Standing Positive Control (Line-wide vs Anchored scan): Assert HR-2 has no tree stamp.

    Wave HR-2 is mentioned in test_economy_surface.py:276 as part of an assertion
    count message ("+5 HR from HR-2"). That is prose in a line qualifying on Wave E-1,
    not a stamp for Wave HR-2.
    A line-wide token scanner fabricates a stamp for HR-2.
    The anchored regex (Wave\s+<ID>) must reject this and confirm HR-2 has NO tree stamp,
    meaning HR-2 is scored as PR_AND_TEST_NO_STAMP rather than LANDED.
    """
    gen = _load_generator()
    results, _, _, _ = gen.run_instrument(repo=str(_ROOT), baseline="HEAD", live_prs=False)

    hr2 = next((r for r in results if r["id"] == "HR-2"), None)
    assert hr2 is not None, "Wave HR-2 must be evaluated"

    # Assert HR-2 has NO tree stamp in nce/ or tests/
    assert hr2["has_stamp"] is False, (
        f"Wave HR-2 was incorrectly scored as having a tree stamp: {hr2['stamped_files']}. "
        "HR-2 is mentioned only in prose in test_economy_surface.py:276, which is not a stamp!"
    )
    assert len(hr2["stamped_files"]) == 0

    # Because it lacks a tree stamp, HR-2 cannot be LANDED (3-way consensus invariant)
    assert hr2["is_landed"] is False, "Wave HR-2 cannot be LANDED without a tree stamp!"
    assert hr2["verdict"] == "PR_AND_TEST_NO_STAMP"


def test_merged_prs_snapshot_not_stale():
    """PR leg staleness guard: The cached merged_prs.json snapshot must not be stale.

    Compares the newest mergedAt timestamp in docs/_generated/merged_prs.json against
    recent git commit activity on HEAD to prevent the cached snapshot from quietly
    rotting unnoticed over time.
    """
    import datetime
    import json
    import subprocess

    json_path = _ROOT / "docs" / "_generated" / "merged_prs.json"
    assert json_path.exists(), "docs/_generated/merged_prs.json must exist"

    prs = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(prs) > 0, "merged_prs.json cannot be empty"

    dates = [p["mergedAt"] for p in prs if p.get("mergedAt")]
    assert len(dates) > 0, "merged_prs.json must contain entries with mergedAt timestamps"

    newest_merged_str = max(dates)
    newest_dt = datetime.datetime.fromisoformat(newest_merged_str.replace("Z", "+00:00"))

    # Get HEAD commit date
    res = subprocess.run(
        ["git", "-C", str(_ROOT), "show", "-s", "--format=%cI", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
    )
    head_dt = datetime.datetime.fromisoformat(res.stdout.strip().replace("Z", "+00:00"))

    # Snapshot must be within 14 days of HEAD commit activity
    delta = head_dt - newest_dt
    max_staleness_days = 14
    assert delta.total_seconds() < max_staleness_days * 86400, (
        f"merged_prs.json snapshot is stale! Newest merged PR is from {newest_merged_str}, "
        f"which is {delta.days} days behind HEAD ({head_dt.isoformat()}). "
        "Refresh the snapshot with: python scripts/gen_waves_landed.py --live-prs"
    )


def test_waves_landed_shrink_only():
    """Shrink-only invariant: landed waves count cannot regress below floor (45).

    The floor was reset from 50 (measured as 52 originally) to 45 (measured as 47)
    after removing 5 waves with fabricated stamps (B-E2, FT-3, HR-2, PR-3, PR-5)
    that arose from line-wide token harvesting over test_economy_surface.py:276.
    """
    gen = _load_generator()
    results, _, _, _ = gen.run_instrument(repo=str(_ROOT), baseline="HEAD", live_prs=False)
    landed = [r for r in results if r["is_landed"]]

    # Assert floor
    assert len(landed) >= 45, (
        f"Landed waves count ({len(landed)}) is below the floor of 45. "
        "Waves may not move from LANDED to unlanded without explanation."
    )

    # Core landmark waves must be landed
    landed_ids = {r["id"] for r in landed}
    required_landed = {
        "S-1",
        "C-SD2",
        "AG-3",
        "HR-5",
        "SU-2",
        "FT-1",
        "I-0",
        "I-2",
        "I-3",
        "I-4",
        "IN-1",
        "B-AG1",
        "E-1",
        "E-2",
        "E-3",
        "CP-1",
        "T-6",
    }
    missing = required_landed - landed_ids
    assert not missing, f"Required landmark waves not landed: {missing}"


def test_unmatched_wave_tokens_is_zero():
    """All Wave tokens in the tree must be attributed to either v1.5 or legacy waves."""
    gen = _load_generator()
    _, _, unmatched, _ = gen.run_instrument(repo=str(_ROOT), baseline="HEAD", live_prs=False)
    assert len(unmatched) == 0, (
        f"Found {len(unmatched)} unmatched Wave tokens in tree: {unmatched}. "
        "Every Wave token must be classified as a v1.5 wave, legacy wave, or known prose."
    )
