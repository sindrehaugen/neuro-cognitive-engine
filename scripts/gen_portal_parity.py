#!/usr/bin/env python
"""Generate ``docs/_generated/host_parity.md`` from ``docs/host_parity_seed.yaml``.

H-2 (charter ``MLV16_ORCH_CHARTER_2026-09-18.md`` §9 "Lane H"): a checked-in,
hand-authored seed lists every host-portal route family the v1.6 analysis found,
generalised to a generic business-capability name and description, with its
disposition (MOVE / MOVE+ / NEW / HOST / RETIRE / FEED) and, for MOVE/MOVE+ rows,
the NCE route or tool that replaces it. This script validates that seed and
renders it as a markdown table.

**Identity rule, enforced here as well as by the seed's own docstring:** the
seed, this generator, and the file it produces name NCE's own routes/tools
freely, but never a host product, company, or module/schema name — every family
is named purely by the business capability it covers.

**A family with no disposition, or a MOVE/MOVE+ family with no replacing route,
is RED** — ``validate()`` returns it as a problem, and ``main()``/the drift test
fail loudly rather than rendering a silently incomplete table.

Usage::

    python scripts/gen_portal_parity.py            # write docs/_generated/host_parity.md
    python scripts/gen_portal_parity.py --check    # exit 1 if the doc is stale or any family is RED
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import yaml

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SEED = _ROOT / "docs" / "host_parity_seed.yaml"
_DOC = _ROOT / "docs" / "_generated" / "host_parity.md"

_VALID_DISPOSITIONS = frozenset({"MOVE", "MOVE+", "NEW", "HOST", "RETIRE", "FEED"})
_REQUIRES_REPLACING_ROUTE = frozenset({"MOVE", "MOVE+"})


def load_families() -> list[dict]:
    data = yaml.safe_load(_SEED.read_text(encoding="utf-8"))
    families = data.get("families") if isinstance(data, dict) else None
    if not isinstance(families, list):
        raise ValueError(f"{_SEED}: expected a top-level 'families' list")
    return families


def validate(families: list[dict]) -> list[str]:
    """Return every RED finding: a missing disposition, an invalid one, a
    MOVE/MOVE+ row with no replacing route, or a duplicate id/family name.
    Empty return means every family is fully specified — never RED by omission.
    """
    problems: list[str] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()

    for fam in families:
        fam_id = fam.get("id") or "<no id>"
        name = fam.get("family") or "<no family name>"

        if fam_id in seen_ids:
            problems.append(f"{fam_id}: duplicate id")
        seen_ids.add(fam_id)
        if name in seen_names:
            problems.append(f"{fam_id} ({name}): duplicate family name")
        seen_names.add(name)

        disposition = (fam.get("disposition") or "").strip()
        if not disposition:
            problems.append(f"{fam_id} ({name}): RED — no disposition")
            continue
        if disposition not in _VALID_DISPOSITIONS:
            problems.append(
                f"{fam_id} ({name}): disposition {disposition!r} is not one of "
                f"{sorted(_VALID_DISPOSITIONS)}"
            )
            continue

        if (
            disposition in _REQUIRES_REPLACING_ROUTE
            and not (fam.get("replacing_route") or "").strip()
        ):
            problems.append(
                f"{fam_id} ({name}): disposition {disposition} requires a replacing_route"
            )

        if not (fam.get("description") or "").strip():
            problems.append(f"{fam_id} ({name}): missing description")

        if fam.get("verified_live"):
            if not (fam.get("verified_asof") or "").strip():
                problems.append(
                    f"{fam_id} ({name}): verified_live is set but verified_asof is missing"
                )
            if not (fam.get("verified_evidence") or "").strip():
                problems.append(
                    f"{fam_id} ({name}): verified_live is set but verified_evidence is missing"
                )

    return problems


_DISPOSITION_LABEL = {
    "MOVE": "MOVE",
    "MOVE+": "MOVE+",
    "NEW": "NEW",
    "HOST": "HOST",
    "RETIRE": "RETIRE",
    "FEED": "FEED",
}


def generate() -> str:
    families = load_families()
    problems = validate(families)

    lines: list[str] = []
    lines.append("<!-- GENERATED FILE. Do not hand-edit. -->")
    lines.append("<!-- Source: docs/host_parity_seed.yaml -->")
    lines.append("<!-- Regenerate: python scripts/gen_portal_parity.py -->")
    lines.append("")
    lines.append("# Host-parity table")
    lines.append("")
    lines.append(
        "Every host-portal route family the v1.6 analysis found, generalised to a "
        "generic business capability (never a host product, company, or module/schema "
        "name), with its disposition and — for MOVE/MOVE+ rows — the NCE route or tool "
        "that replaces it. A family with no disposition is a RED finding, listed first."
    )
    lines.append("")

    if problems:
        lines.append(f"## 🔴 RED — {len(problems)} finding(s)")
        lines.append("")
        for p in problems:
            lines.append(f"- {p}")
        lines.append("")
    else:
        lines.append("## Status: no RED findings — every family has a disposition")
        lines.append("")

    counts: dict[str, int] = {}
    for fam in families:
        d = (fam.get("disposition") or "").strip() or "(missing)"
        counts[d] = counts.get(d, 0) + 1

    lines.append("## Summary")
    lines.append("")
    lines.append(f"Total families: {len(families)}")
    lines.append("")
    lines.append("| Disposition | Count |")
    lines.append("|---|---|")
    for d in sorted(counts):
        lines.append(f"| {d} | {counts[d]} |")
    lines.append("")

    verified = [fam for fam in families if fam.get("verified_live")]
    lines.append("## Backing confirmed live")
    lines.append("")
    lines.append(
        "Families whose `replacing_route` claim has been re-checked against the live "
        "tree (not just written down when the family was authored) and found true, as "
        "of the stated date. This is the question the table exists to answer: which "
        "families are actually ready to retire today, not merely planned to be. A "
        "family absent from this list has not been re-verified — it may still be true, "
        "unverified is not the same as false."
    )
    lines.append("")
    if verified:
        lines.append("| ID | Family | As of | Evidence |")
        lines.append("|---|---|---|---|")
        for fam in sorted(verified, key=lambda f: f.get("id", "")):
            fam_id = fam.get("id", "")
            name = fam.get("family", "")
            asof = fam.get("verified_asof", "")
            evidence = (fam.get("verified_evidence") or "").replace("|", "\\|")
            lines.append(f"| {fam_id} | `{name}` | {asof} | {evidence} |")
    else:
        lines.append("(none re-verified yet)")
    lines.append("")

    by_category: dict[str, list[dict]] = {}
    for fam in families:
        by_category.setdefault(fam.get("category", "(uncategorised)"), []).append(fam)

    lines.append("## Families by category")
    lines.append("")
    for category in sorted(by_category):
        lines.append(f"### {category}")
        lines.append("")
        lines.append(
            "| ID | Family | Description | Disposition | Replacing NCE route/tool | Notes |"
        )
        lines.append("|---|---|---|---|---|---|")
        for fam in sorted(by_category[category], key=lambda f: f.get("id", "")):
            fam_id = fam.get("id", "")
            name = fam.get("family", "")
            desc = (fam.get("description") or "").replace("|", "\\|")
            disp = (fam.get("disposition") or "").strip()
            disp_label = _DISPOSITION_LABEL.get(disp, f"🔴 {disp or 'MISSING'}")
            route = (fam.get("replacing_route") or "").replace("|", "\\|")
            notes = (fam.get("notes") or "").replace("|", "\\|")
            lines.append(f"| {fam_id} | `{name}` | {desc} | {disp_label} | {route} | {notes} |")
        lines.append("")

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="fail if the doc is stale or any family is RED"
    )
    args = parser.parse_args()

    families = load_families()
    problems = validate(families)
    content = generate()

    if args.check:
        existing = _DOC.read_text(encoding="utf-8") if _DOC.exists() else ""
        stale = existing.strip() != content.strip()
        if problems:
            print(f"{len(problems)} RED finding(s) in {_SEED}:", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
        if stale:
            print(
                f"{_DOC} is stale — regenerate with: python scripts/gen_portal_parity.py",
                file=sys.stderr,
            )
        return 1 if (problems or stale) else 0

    _DOC.parent.mkdir(parents=True, exist_ok=True)
    _DOC.write_text(content, encoding="utf-8")
    print(f"wrote {_DOC} ({len(families)} families, {len(problems)} RED)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
