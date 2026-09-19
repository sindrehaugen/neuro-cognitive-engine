"""H-11: the advertised-vs-implemented ratchet (charter §9 "Lane H", filed after
today's residual-pin and Lane F findings converged on the same defect shape;
revised the same day once Lane F's F-3..F-7 waves changed what is mechanically
provable here -- see "Revision" below).

**The defect class.** A registry that maps a NAME a caller can select at
runtime to a description of a capability, where the presence of the name is
decoupled from whether anything real backs it. H-1b's residual pin found this
shape once today (a route that belonged to an engine but matched no scope
limb); Lane F found it again hours later in
``nce.vertical_modules.assets.telemetry.VENDOR_PLATFORMS``. Nothing raised,
nothing failed CI, because nothing before this file ever asked "does every
advertised name have something real behind it."

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

**Revision, same day: a mechanical "is it real" signal DOES exist, and this
file was wrong to say otherwise.** The version of this file that shipped in
PR #219 compared ``neat_pulse.py`` against ``xio_cloud.py`` and found them
byte-for-byte the same template, and concluded no code-shape property
distinguishes real from fabricated. That was true of the code AT THAT TIME,
but it missed the actual gate the charter itself imposes: charter §9 "Lane F"
requires "an EXPLICIT ALLOW-LIST LITERAL of (method, path) pairs... the
single most important line in the lane," and "a wave without its allow-list
literal will be rejected however green its tests are." Every adapter that
went through that real Lane F review carries a module-level
``_ALLOWED_READS: frozenset[tuple[str, str]]`` (``ymcs``, ``neat_pulse``,
``qsys_reflect``, ``neowit``, ``disruptive``, ``ochno``, ``ais`` all have it,
verified by reading each file). ``xio_cloud``, ``sennheiser``, ``shure_cloud``
have none. This is not a heuristic about URL plausibility — it is checking
for a SPECIFIC STRUCTURE the codebase's own governance process mandates
before Lane D will merge an adapter, which is exactly the kind of "external,
cited fact" the original version of this file said it would defer to, except
it turns out to be mechanically checkable after all. So: "resolves to an
implementation" is now defined as (1) resolves to a dedicated class (never
``UnimplementedVendorAdapter``), AND (2) that class's module declares
``_ALLOWED_READS``. Both mechanical; neither infers anything about whether
the *endpoint itself* is correct (that remains Lane D's review + Q-38's
territory).

**Consequence of the revision:** ``qsys`` no longer needs an exemption. When
this file shipped, ``qsys_reflect.py`` was 116 lines of the fabricated
template with no allow-list, indistinguishable in code shape from
``crestron``/``sennheiser``/``shure``. Wave F-6 retrofitted it against the
host's real ``qsys_reflect_client.py`` (per that file's own docstring, which
credits this ratchet's initial flag for prompting the check) and it now
carries ``_ALLOWED_READS``. It is removed from
``_VENDOR_PLATFORM_EXEMPTIONS`` below — a wave landed and the ratchet must
show that, not keep exempting a platform that now passes on its own.

**Registries surveyed for this defect shape, and why each is or is not
here:** ``VENDOR_PLATFORMS`` (``nce/vertical_modules/assets/telemetry.py``) —
**has the defect**, walked below. ``nce.event_types.EVENT_CATALOGUE`` —
excluded; I-2's job already. ``nce/vertical_modules/diagnostics/profiles.py``'s
``_REGISTRY`` — excluded: populated exclusively via ``register_profile(name,
profile)`` calls that always supply a real object in the same statement, so
the defect is structurally impossible there. ``nce/source_mode/resolver.py``'s
dispatch tables — excluded: a closed three-value literal, not an open
registry. ``nce/vertical_modules/product/sources/`` — excluded: two concrete,
fully-implemented source classes. A repo-wide ``dict[str, str] = {`` sweep
found nothing else in this shape.

Not surveyed directly: the private host portal repository (out of scope for
this worktree). Treat any "no reference found" claim in this file as scoped
to what this worktree can see, not the whole estate, unless it cites a survey
(like Q-38's) that covered both — the ``qsys`` correction above is exactly a
case where a worktree-scoped grep looked identical to a two-sided one and was
wrong.

**Second revision, 2026-09-19 (Q-38 part 1 ruling):** ``crestron``,
``sennheiser`` and ``shure`` were "wired into ``select_telemetry_adapter``'s
dispatch" when this file was written, above — that sentence, and the file
paths ``xio_cloud.py``/``sennheiser.py``/``shure_cloud.py`` cited throughout,
described the code AT THAT TIME. Sindre's ruling on Q-38 part 1 authorized
routing all three to ``UnimplementedVendorAdapter`` and deleting the three
fabricated files outright: "worse than unimplemented because they looked
implemented" (ML-orch's words, upheld). All three now carry ``status:
"absent"`` in ``_VENDOR_PLATFORM_EXEMPTIONS`` below, the same disposition as
``huddly``/``poly`` — which are themselves now dropped from
``VENDOR_PLATFORMS`` entirely rather than merely exempted, since neither ever
had a dispatch branch or an NCE consumer. Q-38 part 2 (building any of the
five from the vendor's own public API docs) remains deferred, unauthorized,
and un-costed.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from nce.vertical_modules.assets.telemetry import (
    VENDOR_PLATFORMS,
    UnimplementedVendorAdapter,
    real_adapter_env_key,
    select_telemetry_adapter,
)

_ASSETS_DIR = pathlib.Path(__file__).resolve().parent.parent / "nce" / "vertical_modules" / "assets"
_TELEMETRY_FILE = _ASSETS_DIR / "telemetry.py"


def _platform_names_in_test(test_node: ast.expr) -> list[str]:
    """Extract platform string(s) from an `if name == "x":` or
    `if name in ("x", "y"):` comparison node."""
    if not isinstance(test_node, ast.Compare):
        return []
    names: list[str] = []
    for comparator in test_node.comparators:
        if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
            names.append(comparator.value)
        elif isinstance(comparator, (ast.Tuple, ast.List)):
            for elt in comparator.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    names.append(elt.value)
    return names


def _adapter_module_map() -> dict[str, str]:
    """platform -> dotted module it imports its real adapter class from,
    parsed from select_telemetry_adapter's own if-chain (AST, never a line
    grep -- K-0). A platform with no branch (huddly, poly) is simply absent
    from the returned mapping."""
    tree = ast.parse(_TELEMETRY_FILE.read_text(encoding="utf-8"), filename=str(_TELEMETRY_FILE))
    mapping: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "select_telemetry_adapter":
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.If):
                continue
            platforms = _platform_names_in_test(inner.test)
            if not platforms:
                continue
            for stmt in inner.body:
                if isinstance(stmt, ast.ImportFrom) and stmt.module:
                    for platform in platforms:
                        mapping[platform] = stmt.module
    return mapping


def _module_has_allow_list(dotted_module: str) -> bool:
    """True iff *dotted_module* declares a module-level `_ALLOWED_READS`
    assignment -- the charter's own Lane F review-gate marker (§9 "Lane F"),
    verified AST-side rather than by importing the module (which would
    require network-adjacent dependencies to be satisfied)."""
    rel = dotted_module.replace("nce.vertical_modules.assets.", "")
    path = _ASSETS_DIR / f"{rel}.py"
    if not path.exists():
        return False
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "_ALLOWED_READS":
                return True
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_ALLOWED_READS":
                    return True
    return False


# ---------------------------------------------------------------------------
# Shrink-only exemption list, shaped like tests/test_surface_parity.py's
# internal-cores.json allowlist: every entry needs an owner, a reason, and
# (here, since the reason is itself a filed finding) a citation. An entry
# leaves this list only when a wave gives that platform a real, allow-list-
# gated adapter -- never by widening what "resolves to an implementation"
# means.
#
# status:
#   "absent"                 -> no adapter branch in select_telemetry_adapter
#                                at all; always resolves to
#                                UnimplementedVendorAdapter. Mechanically
#                                reverified below on every run.
#   "unverified_scaffolding" -> resolves to a dedicated class, but that
#                                module has NO _ALLOWED_READS marker -- never
#                                went through Lane F's real-adapter review at
#                                all. Shrinks only when Sindre rules on Q-38
#                                and someone does the from-scratch vendor
#                                integration.
#   "pending_wave"           -> same as unverified_scaffolding today, but a
#                                real reference implementation to build it
#                                from DOES exist (a host client, a queued
#                                charter wave) -- a materially smaller and
#                                shorter-lived gap. Mechanically reverified
#                                below: if the module GAINS _ALLOWED_READS,
#                                that is exactly the win this status exists
#                                to detect, and the entry must be removed.
# ---------------------------------------------------------------------------
_VENDOR_PLATFORM_EXEMPTIONS: dict[str, dict[str, str]] = {
    "crestron": {
        "owner": "Lane F",
        "status": "absent",
        "reason": "Q-38 part 1 (2026-09-19): CrestronXiOCloudTelemetryAdapter called a "
        "single hardcoded GET with no reference to a real Crestron XiO Cloud API found "
        "anywhere in either repo -- worse than unimplemented, since it looked built. "
        "Sindre authorized deleting it (nce/vertical_modules/assets/xio_cloud.py, now "
        "removed) and routing 'crestron' to UnimplementedVendorAdapter, same as "
        "huddly/poly always were. A real client returns when a wave exists to derive "
        "one from Crestron's own public API docs (Q-38 part 2, deferred).",
        "ref": "Q-38",
    },
    "sennheiser": {
        "owner": "Lane F",
        "status": "absent",
        "reason": "Q-38 part 1 (2026-09-19): SennheiserTelemetryAdapter called a single "
        "hardcoded GET with no reference to a real Sennheiser Control Cockpit API found "
        "anywhere in either repo -- worse than unimplemented, since it looked built. "
        "Sindre authorized deleting it (nce/vertical_modules/assets/sennheiser.py, now "
        "removed) and routing 'sennheiser' to UnimplementedVendorAdapter, same as "
        "huddly/poly always were. A real client returns when a wave exists to derive "
        "one from Sennheiser's own public API docs (Q-38 part 2, deferred).",
        "ref": "Q-38",
    },
    "shure": {
        "owner": "Lane F",
        "status": "absent",
        "reason": "Q-38 part 1 (2026-09-19): ShureCloudTelemetryAdapter called a single "
        "hardcoded GET with no reference to a real Shure Cloud API found anywhere in "
        "either repo -- worse than unimplemented, since it looked built. Sindre "
        "authorized deleting it (nce/vertical_modules/assets/shure_cloud.py, now "
        "removed) and routing 'shure' to UnimplementedVendorAdapter, same as "
        "huddly/poly always were. A real client returns when a wave exists to derive "
        "one from Shure's own public API docs (Q-38 part 2, deferred).",
        "ref": "Q-38",
    },
}

_REQUIRED_EXEMPTION_FIELDS = ("owner", "status", "reason", "ref")
_VALID_STATUSES = frozenset({"absent", "unverified_scaffolding", "pending_wave"})


def _resolves_to_real_adapter(platform: str, monkeypatch: pytest.MonkeyPatch) -> bool:
    """True iff *platform*, with its real-adapter flag on, resolves to
    anything other than the Unimplemented stand-in. Necessary but not
    sufficient -- see _is_verified_real."""
    monkeypatch.setenv(real_adapter_env_key(platform), "1")
    try:
        adapter = select_telemetry_adapter(platform)
    finally:
        monkeypatch.delenv(real_adapter_env_key(platform), raising=False)
    return not isinstance(adapter, UnimplementedVendorAdapter)


def _is_verified_real(platform: str, monkeypatch: pytest.MonkeyPatch) -> bool:
    """True iff *platform* resolves to a dedicated class AND that class's
    module carries the charter's Lane F allow-list marker."""
    if not _resolves_to_real_adapter(platform, monkeypatch):
        return False
    module = _adapter_module_map().get(platform)
    return bool(module) and _module_has_allow_list(module)


def test_vendor_platforms_discovery_floor() -> None:
    """Guard-the-guard: the registry itself must not have silently collapsed.

    Floor lowered 13 -> 11 on 2026-09-19 (Q-38 part 1, a DELIBERATE,
    authorized removal, not a silent collapse): huddly/poly dropped
    entirely (never had a dispatch branch or an NCE consumer)."""
    assert len(VENDOR_PLATFORMS) >= 11, (
        f"Only {len(VENDOR_PLATFORMS)} entries in VENDOR_PLATFORMS -- expected at "
        "least 11 based on the 2026-09-19 census (11, after Q-38 part 1 dropped "
        "huddly/poly). Either a real platform was removed, or the import is broken."
    )


def test_every_vendor_platform_is_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every VENDOR_PLATFORMS key is EITHER verified-real (dedicated class +
    allow-list marker) OR exempted with a reason -- never silently untested.
    This is this file's "a family with no disposition is RED" (H-2's phrase,
    same shape here)."""
    unclassified: list[str] = []
    for platform in VENDOR_PLATFORMS:
        if platform in _VENDOR_PLATFORM_EXEMPTIONS:
            continue
        if not _is_verified_real(platform, monkeypatch):
            unclassified.append(platform)

    assert not unclassified, (
        f"{len(unclassified)} VENDOR_PLATFORMS entries are neither verified-real "
        f"(dedicated class + _ALLOWED_READS) nor in the exemption list: "
        f"{unclassified}. Either finish the adapter (add _ALLOWED_READS once it "
        "is reviewed against a real reference), or add a reasoned exemption to "
        "_VENDOR_PLATFORM_EXEMPTIONS."
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
        assert not _resolves_to_real_adapter(platform, monkeypatch), (
            f"{platform} is exempted as status='absent' but now resolves to a "
            "real dispatch branch. Remove it from _VENDOR_PLATFORM_EXEMPTIONS "
            "(a real win, but it must be a conscious edit, not a silent one)."
        )


def test_scaffolding_exemptions_have_not_quietly_become_real(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shrink-only regression guard for 'unverified_scaffolding' AND
    'pending_wave': if any exempted platform's module gains _ALLOWED_READS
    (exactly what happened to qsys in wave F-6), this fails until the entry
    is consciously removed from the exemption list. This is the check whose
    absence let PR #219 keep exempting qsys silently after F-6 made it real
    -- the finding this revision exists to fix."""
    still_scaffolding: list[str] = []
    for platform, entry in _VENDOR_PLATFORM_EXEMPTIONS.items():
        if entry["status"] not in ("unverified_scaffolding", "pending_wave"):
            continue
        if _is_verified_real(platform, monkeypatch):
            still_scaffolding.append(platform)

    assert not still_scaffolding, (
        f"{still_scaffolding} now resolve to a real, allow-list-gated adapter "
        "but are still in _VENDOR_PLATFORM_EXEMPTIONS. Remove them -- a wave "
        "landed and this exemption list must show it."
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
    assert not _is_verified_real(synthetic, monkeypatch), (
        "A synthetic platform with no dispatch branch resolved as verified-real "
        "-- the fixture itself is broken."
    )
    unclassified = [
        p
        for p in VENDOR_PLATFORMS
        if p not in _VENDOR_PLATFORM_EXEMPTIONS and not _is_verified_real(p, monkeypatch)
    ]
    assert synthetic in unclassified


def test_positive_control_allow_list_detection_is_not_vacuous() -> None:
    """U18 for _module_has_allow_list specifically: prove it distinguishes a
    real module (has the marker) from one that does not.

    No longer anchored to a real VENDOR_PLATFORMS entry for the negative
    case (crestron/sennheiser/shure were the fabricated examples until
    Q-38 part 1 deleted all three outright -- there is no longer a
    dispatch-wired module anywhere in this registry that lacks
    _ALLOWED_READS, which is the win that revision was for). A synthetic
    dotted path that resolves to no file on disk exercises the same
    False-returning code path (`if not path.exists(): return False`)
    without depending on today's specific gaps.
    """
    real_module = _adapter_module_map().get("neat")
    assert real_module and _module_has_allow_list(real_module), (
        "neat's adapter module should carry _ALLOWED_READS -- detection may be broken."
    )
    assert not _module_has_allow_list("nce.vertical_modules.assets.does_not_exist"), (
        "a dotted module with no file on disk should never report an allow-list marker "
        "-- detection may be matching too broadly."
    )
