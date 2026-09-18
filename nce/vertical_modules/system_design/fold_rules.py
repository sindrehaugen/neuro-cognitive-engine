"""nce.vertical_modules.system_design.fold_rules — Duplicate-fold rules for the FL tree.

Phase C Wave C-1:
Configurable duplicate matching and fold rules for the Functional Location tree.
Read from the module directory (not config_data) per Charter §13 midday directive,
enabling Lane G (Wave G-3) to supply the host's room-map fold rules as C1 config.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class FoldRules:
    """Configuration rules for matching and folding functional locations."""

    strip_prefixes: tuple[str, ...] = (
        "rom ",
        "room ",
        "møterom ",
        "meeting room ",
        "kontor ",
        "office ",
        "sal ",
        "hall ",
        "fl ",
    )
    strip_suffixes: tuple[str, ...] = (
        " rom",
        " room",
        " møterom",
        " meeting room",
    )
    floor_aliases: dict[str, str] = field(
        default_factory=lambda: {
            "u1": "U1",
            "k1": "U1",
            "kjeller": "U1",
            "basement": "U1",
            "0": "00",
            "00": "00",
            "u": "U1",
            "1": "01",
            "01": "01",
            "1etg": "01",
            "1.etg": "01",
            "1etasje": "01",
            "1. etasje": "01",
            "1stfloor": "01",
            "1st floor": "01",
            "2": "02",
            "02": "02",
            "2etg": "02",
            "2.etg": "02",
            "2etasje": "02",
            "2. etasje": "02",
            "3": "03",
            "03": "03",
            "3etg": "03",
            "3.etg": "03",
            "3etasje": "03",
            "4": "04",
            "04": "04",
            "5": "05",
            "05": "05",
        }
    )
    case_sensitive: bool = False
    collapse_whitespace: bool = True
    strip_punctuation: bool = True
    exact_code_match: bool = True


DEFAULT_FOLD_RULES = FoldRules()


def normalize_name(raw: str, rules: FoldRules = DEFAULT_FOLD_RULES) -> str:
    """Normalize an FL node name or label component for matching."""
    if not raw:
        return ""

    text = raw.strip()
    if not rules.case_sensitive:
        text = text.lower()

    if rules.collapse_whitespace:
        text = re.sub(r"\s+", " ", text)

    # Check floor aliases if it looks like a floor designator
    norm_floor = text.lower().replace(" ", "").replace(".", "")
    if norm_floor in rules.floor_aliases:
        return rules.floor_aliases[norm_floor]
    if text.lower() in rules.floor_aliases:
        return rules.floor_aliases[text.lower()]

    # Strip prefixes
    for prefix in rules.strip_prefixes:
        if text.startswith(prefix if rules.case_sensitive else prefix.lower()):
            text = text[len(prefix) :].strip()
            break

    # Strip suffixes
    for suffix in rules.strip_suffixes:
        if text.endswith(suffix if rules.case_sensitive else suffix.lower()):
            text = text[: -len(suffix)].strip()
            break

    if rules.strip_punctuation:
        text = re.sub(r"[^\w\s-]", "", text).strip()
        text = text.strip("- ")

    return text


def evaluate_fl_match(
    name_a: str,
    name_b: str,
    kind_a: str | None = None,
    kind_b: str | None = None,
    rules: FoldRules = DEFAULT_FOLD_RULES,
) -> tuple[bool, str]:
    """Determine whether two FL nodes represent the same functional entity.

    Returns:
        (is_match, reason)
    """
    if kind_a and kind_b and kind_a.lower() != kind_b.lower():
        return False, f"Kind mismatch: {kind_a} != {kind_b}"

    norm_a = normalize_name(name_a, rules)
    norm_b = normalize_name(name_b, rules)

    if not norm_a or not norm_b:
        return False, "Empty normalized name"

    if norm_a == norm_b:
        return True, f"Normalized exact match: {norm_a!r}"

    # Room number extraction match (e.g. 'Meeting Room 302' and 'R-302')
    code_a = re.findall(r"\b\d{2,4}[a-z]?\b", name_a.lower())
    code_b = re.findall(r"\b\d{2,4}[a-z]?\b", name_b.lower())
    if rules.exact_code_match and code_a and code_b and set(code_a) == set(code_b):
        return True, f"Room code match: {set(code_a)}"

    return False, f"Non-matching: {norm_a!r} != {norm_b!r}"
