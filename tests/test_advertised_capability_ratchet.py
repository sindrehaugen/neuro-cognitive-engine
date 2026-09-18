"""H-11: the advertised-vs-implemented ratchet (charter §9 "Lane H", filed after
today's residual-pin and Lane F findings converged on the same defect shape).

**The defect class.** A registry that maps a NAME a caller can select at
runtime to a description of a capability, where the presence of the name is
decoupled from whether anything real backs it. H-1b's residual pin found this
shape once today (a route that belonged to an engine but matched no scope
limb); Lane F found it again hours later in
``nce.vertical_modules.assets.telemetry.VENDOR_PLATFORMS``: nine advertised
platforms, and measured against the tree, only some are reachable, and among
the reachable ones some call a documented vendor API and some call a
hardcoded endpoint that references nothing anywhere in either repo (filed as
Q-38). Nothing raised, nothing failed CI, because nothing before this file
ever asked "does every advertised name have something real behind it."

**How this differs from I-1 and A-2 (say this so the next reader does not
collapse the three).**

* ``tests/test_surface_parity.py`` (I-1) asks: is every ``do_*`` CORE
  reachable from some surface (tool, route, cron, ...)? Its unit is a
  function definition; its question is "is this code called from anywhere."
* A-2 (planned) asks: does every NODE TYPE in the ownership registry have a
  ``ResourceSpec`` or a reasoned exemption? Its unit is a node type; its
  question is "does this business concept have a resource layer."
* **This file** asks: does every NAME a registry advertises for a caller to
  SELECT resolve to a real implementation of the capability that name
  promises? Its unit is a registry entry (a string key), and its question is
  "if a caller picks this name, do they get what the name says" — orthogonal
  to whether the code backing it is reachable (it is, that's why it's
  advertised) or whether some other axis has a resource layer. A platform can
  pass I-1 and A-2 completely and still fail this file, exactly as
  ``crestron``/``sennheiser``/``shure`` do today: their code IS reachable
  (wired into ``select_telemetry_adapter``'s dispatch), but nothing verifies
  the endpoint they call is real.

``nce.event_types.EVENT_CATALOGUE`` is deliberately NOT walked here — a
selector with no producer/consumer is already I-2's job, and duplicating it
here would just be a second, worse copy of that ratchet.

**Why "resolves to an implementation" cannot be a mechanical "is it real"
check, and this file does not pretend otherwise.** The three fabricated
adapters (``crestron``, ``sennheiser``, ``shure``) and the one this wave
additionally found in the same shape (``qsys`` — see below) are, byte for
byte, the SAME TEMPLATE as the two verified-real adapters (``neat``,
``yealink``/``ymcs``): an env-configurable endpoint URL, a
``NotImplementedError`` guard when unconfigured, an ``httpx`` call, and the
same degradation-logging ``except`` block. There is no property of the
Python code itself that marks one real and the other fabricated — the only
difference is external: whether the URL path and headers were derived from
an actual vendor API reference anywhere in either repo. That is exactly what
Q-38 measured (by grepping both repos for each vendor name), not something
this test can re-derive by reading the adapter file. So this file does NOT
attempt a "realness" heuristic. It proves the two things that ARE mechanical
— (1) does a name resolve to a dedicated class at all, never the
``UnimplementedVendorAdapter`` stand-in, and (2) is every registry entry
accounted for by an explicit, reasoned classification — and treats "is it
real" as an external fact that must be CITED (to Q-38, or to the wave that
built and verified it), never inferred from code shape.

**A finding of this wave's own survey, not yet in Q-38:** ``qsys`` shares the
identical fabricated shape (hardcoded ``/api/v0/cores/{id}/telemetry``,
verified via a repo-wide grep for "Q-SYS"/"qsys reflect"/"qsc" turning up only
brand-name mentions in unrelated docs, same as the three Q-38 already flags).
Q-38 does not mention it — charter §9 "Lane F" schedules Q-SYS Reflect as
F-4, still queued, so this may be pre-F-4 scaffolding rather than a
considered decision. Exempted here alongside the Q-38 three, cited
separately since it is this wave's finding, not Q-38's.

**Registries surveyed for this defect shape, and why each is or is not
here:**

* ``VENDOR_PLATFORMS`` (``nce/vertical_modules/assets/telemetry.py``) —
  **has the defect**, walked below.
* ``nce.event_types.EVENT_CATALOGUE`` — excluded; I-2 already covers
  producer/consumer completeness for selectors, and this file must not
  become a second copy of that ratchet.
* ``nce/vertical_modules/diagnostics/profiles.py``'s ``_REGISTRY`` —
  surveyed, excluded: it is populated exclusively via ``register_profile(name,
  profile)`` calls that always supply a real ``LogProfile`` object in the
  same statement. A name cannot enter this registry without an
  implementation already attached — the defect is structurally impossible
  here, not merely absent today.
* ``nce/source_mode/resolver.py``'s ``_READ_DISPATCH``/``_WRITE_DISPATCH`` —
  surveyed, excluded: keyed by a closed three-value ``SourceMode`` literal
  (``d365``/``nce``/``both``), not an open registry a caller extends by
  adding a vendor name. Every key is a language keyword, not a capability
  advertisement.
* ``nce/vertical_modules/product/sources/`` — surveyed, excluded: two
  concrete, fully-implemented source classes (``nettailer``,
  ``manufacturer_api``); no dict of advertised-but-unbacked source names.
* A repo-wide sweep for other ``dict[str, str] = {`` module-level registries
  under ``nce/`` turned up nothing else shaped like "name advertised,
  implementation optional" (see PR description for the full list checked).

Not surveyed, reported rather than silently assumed clean: the private host
portal repository (out of scope for this worktree; Lane F's own Q-38
measurement already covers the host-side half of the crestron/sennheiser/
shure/qsys "no reference anywhere" claim, which this file cannot re-verify
from here).
"""

from __future__ import annotations

import pytest

from nce.vertical_modules.assets.telemetry import (
    VENDOR_PLATFORMS,
    UnimplementedVendorAdapter,
    real_adapter_env_key,
    select_telemetry_adapter,
)

# ---------------------------------------------------------------------------
# Shrink-only exemption list, shaped like tests/test_surface_parity.py's
# internal-cores.json allowlist: every entry needs an owner, a reason, and
# (here, since the reason is itself a filed finding) a citation. An entry
# leaves this list only when a wave gives that platform a verified-real
# adapter — never by widening what "resolves to an implementation" means.
#
# status:
#   "absent"                -> no adapter branch in select_telemetry_adapter
#                               at all; always resolves to
#                               UnimplementedVendorAdapter. Mechanically
#                               reverified below on every run.
#   "unverified_scaffolding" -> DOES resolve to a dedicated class (would pass
#                               a naive "is it Unimplemented" check), but no
#                               reference to a real vendor API was found
#                               anywhere in either repo. Not reassertable
#                               mechanically -- see module docstring.
# ---------------------------------------------------------------------------
_VENDOR_PLATFORM_EXEMPTIONS: dict[str, dict[str, str]] = {
    "huddly": {
        "owner": "Lane F",
        "status": "absent",
        "reason": "Advertised in VENDOR_PLATFORMS with no adapter branch in "
        "select_telemetry_adapter at all -- always the Unimplemented stand-in.",
        "ref": "Q-38",
    },
    "poly": {
        "owner": "Lane F",
        "status": "absent",
        "reason": "Advertised in VENDOR_PLATFORMS with no adapter branch in "
        "select_telemetry_adapter at all -- always the Unimplemented stand-in.",
        "ref": "Q-38",
    },
    "crestron": {
        "owner": "Lane F",
        "status": "unverified_scaffolding",
        "reason": "CrestronXiOCloudTelemetryAdapter calls a single hardcoded "
        "GET with no reference to a real Crestron XiO Cloud API found anywhere "
        "in either repo (grepped for crestron/xio/fusion).",
        "ref": "Q-38",
    },
    "sennheiser": {
        "owner": "Lane F",
        "status": "unverified_scaffolding",
        "reason": "SennheiserTelemetryAdapter calls a single hardcoded GET "
        "with no reference to a real Sennheiser Control Cockpit API found "
        "anywhere in either repo.",
        "ref": "Q-38",
    },
    "shure": {
        "owner": "Lane F",
        "status": "unverified_scaffolding",
        "reason": "ShureCloudTelemetryAdapter calls a single hardcoded GET "
        "with no reference to a real Shure Cloud API found anywhere in "
        "either repo.",
        "ref": "Q-38",
    },
    "qsys": {
        "owner": "Lane F",
        "status": "unverified_scaffolding",
        "reason": "QSysReflectTelemetryAdapter is byte-for-byte the same "
        "fabricated template as the Q-38 three (hardcoded "
        "/api/v0/cores/{id}/telemetry). Not in Q-38 -- found independently "
        "this wave (H-11) via the same no-reference-anywhere grep. Q-SYS "
        "Reflect is charter F-4, still queued, so this is likely pre-F-4 "
        "scaffolding rather than a considered build.",
        "ref": "H-11",
    },
}

_REQUIRED_EXEMPTION_FIELDS = ("owner", "status", "reason", "ref")
_VALID_STATUSES = frozenset({"absent", "unverified_scaffolding"})


def _resolves_to_real_adapter(platform: str, monkeypatch: pytest.MonkeyPatch) -> bool:
    """True iff *platform*, with its real-adapter flag on, resolves to
    anything other than the Unimplemented stand-in. Proves reachability, not
    correctness -- see module docstring."""
    monkeypatch.setenv(real_adapter_env_key(platform), "1")
    try:
        adapter = select_telemetry_adapter(platform)
    finally:
        monkeypatch.delenv(real_adapter_env_key(platform), raising=False)
    return not isinstance(adapter, UnimplementedVendorAdapter)


def test_vendor_platforms_discovery_floor() -> None:
    """Guard-the-guard: the registry itself must not have silently collapsed."""
    assert len(VENDOR_PLATFORMS) >= 8, (
        f"Only {len(VENDOR_PLATFORMS)} entries in VENDOR_PLATFORMS -- expected at "
        "least 8 based on the 2026-09-18 census (9). Either a real platform was "
        "removed, or the import is broken."
    )


def test_every_vendor_platform_is_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every VENDOR_PLATFORMS key is EITHER verified-reachable OR exempted with
    a reason -- never silently untested. This is this file's "a family with no
    disposition is RED" (H-2's phrase, same shape here)."""
    unclassified: list[str] = []
    for platform in VENDOR_PLATFORMS:
        if platform in _VENDOR_PLATFORM_EXEMPTIONS:
            continue
        if not _resolves_to_real_adapter(platform, monkeypatch):
            unclassified.append(platform)

    assert not unclassified, (
        f"{len(unclassified)} VENDOR_PLATFORMS entries resolve to "
        f"UnimplementedVendorAdapter and are NOT in the exemption list: "
        f"{unclassified}. Either build the adapter, or add a reasoned "
        "exemption to _VENDOR_PLATFORM_EXEMPTIONS (status='absent')."
    )


def test_exemption_list_is_shrink_only_and_reasoned() -> None:
    """Mirrors test_surface_parity.py's internal-cores.json shape: no stale
    entries, every entry fully reasoned, no bare owner-less rows."""
    stale = set(_VENDOR_PLATFORM_EXEMPTIONS) - set(VENDOR_PLATFORMS)
    assert not stale, (
        f"Exemption list references platforms no longer in VENDOR_PLATFORMS: "
        f"{stale}. Remove these stale entries."
    )

    for platform, entry in _VENDOR_PLATFORM_EXEMPTIONS.items():
        for field in _REQUIRED_EXEMPTION_FIELDS:
            assert field in entry and entry[field].strip(), (
                f"{platform}: exemption entry missing or empty {field!r}"
            )
        assert entry["status"] in _VALID_STATUSES, (
            f"{platform}: status {entry['status']!r} is not one of {sorted(_VALID_STATUSES)}"
        )
        reason = " ".join(entry["reason"].split())
        assert len(reason) >= 40, f"{platform}: reason too short for review ({len(reason)} chars)"


def test_absent_exemptions_still_resolve_to_unimplemented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shrink-only regression guard for the 'absent' half of the exemption
    list: if huddly/poly ever gain a real dispatch branch, this fails,
    forcing a conscious move out of the exemption list rather than a silent
    upgrade nobody notices."""
    for platform, entry in _VENDOR_PLATFORM_EXEMPTIONS.items():
        if entry["status"] != "absent":
            continue
        monkeypatch.setenv(real_adapter_env_key(platform), "1")
        try:
            adapter = select_telemetry_adapter(platform)
        finally:
            monkeypatch.delenv(real_adapter_env_key(platform), raising=False)
        assert isinstance(adapter, UnimplementedVendorAdapter), (
            f"{platform} is exempted as status='absent' but now resolves to "
            f"{type(adapter).__name__} -- it has gained a real dispatch branch. "
            "Remove it from _VENDOR_PLATFORM_EXEMPTIONS (a real win, but it "
            "must be a conscious edit, not a silent one)."
        )


def test_positive_control_unclassified_platform_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Standing positive control (U18): prove the classification check
    actually fires on a platform that is neither wired nor exempted, using a
    synthetic name so this does not depend on today's specific gaps."""
    synthetic = "totally_fabricated_vendor_no_one_built"
    assert synthetic not in VENDOR_PLATFORMS
    assert synthetic not in _VENDOR_PLATFORM_EXEMPTIONS

    with pytest.raises(ValueError):
        # Unknown to the factory entirely today -- proves the underlying seam
        # this ratchet reads from still refuses silently-invented names.
        select_telemetry_adapter(synthetic)

    # Now add it to the REAL registry (module-level dict, restored by
    # monkeypatch) with no dispatch branch and no exemption -- exactly the
    # shape a careless PR would introduce -- and prove
    # test_every_vendor_platform_is_classified's own detection logic flags it.
    monkeypatch.setitem(VENDOR_PLATFORMS, synthetic, "Totally Fabricated Vendor Cloud API")
    assert not _resolves_to_real_adapter(synthetic, monkeypatch), (
        "A synthetic platform with no dispatch branch resolved to something "
        "other than UnimplementedVendorAdapter -- the fixture itself is broken."
    )
    unclassified = [
        p
        for p in VENDOR_PLATFORMS
        if p not in _VENDOR_PLATFORM_EXEMPTIONS and not _resolves_to_real_adapter(p, monkeypatch)
    ]
    assert synthetic in unclassified
