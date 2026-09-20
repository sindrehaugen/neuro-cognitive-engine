"""
tests/unit/test_room_category_pricing.py
==========================================
C-2 category -> B-10 price-tier resolution, refuse-and-name
(nce/vertical_modules/agreements/room_category_pricing.py, 2026-09-20).

Covers Sindre's ruling from ROOM_CATEGORY_PRICE_MAPPING.md:
  - The 5 CONFIDENT mappings resolve correctly.
  - The 3 true orphans (no price tier exists) refuse, naming the category
    and the room.
  - The 3 categories blocked on the still-open collapse ruling refuse too
    -- treated identically to the orphans, not quietly resolved.
  - An unrecognised category refuses.

Positive controls (INSTRUMENT_STANDARD.md's rule; K31 -- H, 2026-09-20):
a run containing an unpriced category must refuse; a run of ONLY priced
categories must succeed. The second is what proves refusal is conditional
on the category, not an unconditional refusal that would pass the first
test trivially by refusing everything.
"""

from __future__ import annotations

import pytest

from nce.vertical_modules.agreements.room_category_pricing import (
    CATEGORY_TO_PRICE_TIER,
    UnpricedRoomCategoryError,
    resolve_price_tier,
)

_CONFIDENT_CASES = [
    ("HUDDLE", "huddle_space"),
    ("BOARDROOM", "large_boardroom"),
    ("TRAINING_ROOM", "training_room"),
    ("AUDITORIUM", "auditorium"),
    ("FLEX_SPACE", "collaboration_space"),
]

_TRUE_ORPHANS = ["ALL_HANDS", "WAR_ROOM", "OPERATIONS_CENTER"]
_BLOCKED_COLLAPSE = ["MEETING_SMALL", "CONFERENCE_MEDIUM", "CONFERENCE_LARGE"]


@pytest.mark.parametrize("category,expected_tier", _CONFIDENT_CASES)
def test_confident_mappings_resolve(category: str, expected_tier: str) -> None:
    assert resolve_price_tier(category) == expected_tier


def test_confident_mappings_are_exactly_five() -> None:
    """Pins the mapping's size so a silent addition (e.g. quietly resolving
    the collapse) is caught here even if no other test names the new entry."""
    assert len(CATEGORY_TO_PRICE_TIER) == 5
    assert set(CATEGORY_TO_PRICE_TIER) == {c for c, _ in _CONFIDENT_CASES}


@pytest.mark.parametrize("category", _TRUE_ORPHANS)
def test_true_orphans_refuse_naming_category_and_room(category: str) -> None:
    with pytest.raises(UnpricedRoomCategoryError) as exc_info:
        resolve_price_tier(category, fl_label="FL:ACME:SITE:BLDG:2:ROOM:9")
    message = str(exc_info.value)
    assert category in message
    assert "FL:ACME:SITE:BLDG:2:ROOM:9" in message
    assert exc_info.value.category == category
    assert exc_info.value.fl_label == "FL:ACME:SITE:BLDG:2:ROOM:9"


@pytest.mark.parametrize("category", _BLOCKED_COLLAPSE)
def test_blocked_collapse_categories_refuse_not_silently_resolved(category: str) -> None:
    """The 3-way collapse was flagged as ONE decision for Sindre; he ruled on
    the orphans, not this. These three must refuse exactly like a true
    orphan until he rules -- NOT quietly default to standard_meeting_room."""
    with pytest.raises(UnpricedRoomCategoryError) as exc_info:
        resolve_price_tier(category, fl_label="FL:ACME:SITE:BLDG:1:ROOM:3")
    message = str(exc_info.value)
    assert category in message
    assert "awaiting Sindre" in message, (
        "the collapse refusal must say a ruling is pending, not just 'no price tier' -- "
        "that is what distinguishes it from a true orphan for the next reader"
    )


def test_unrecognised_category_refuses() -> None:
    with pytest.raises(UnpricedRoomCategoryError):
        resolve_price_tier("NOT_A_REAL_CATEGORY")


def test_case_insensitive_on_input() -> None:
    assert resolve_price_tier("huddle") == "huddle_space"
    assert resolve_price_tier("Boardroom") == "large_boardroom"


def test_refusal_without_fl_label_still_names_the_category() -> None:
    """fl_label is optional -- a caller with no room context yet must still
    get a message naming the category, just not the room."""
    with pytest.raises(UnpricedRoomCategoryError) as exc_info:
        resolve_price_tier("WAR_ROOM")
    assert "WAR_ROOM" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Positive controls: the guard is conditional, not unconditional.
# ---------------------------------------------------------------------------


def test_a_run_with_one_unpriced_room_refuses_naming_that_room() -> None:
    """Simulates what a real billing run will do: resolve every room's tier
    in sequence. One unpriced category anywhere in the batch must stop the
    whole run and name exactly that room -- not the batch in the abstract."""
    rooms = [
        ("FL:ACME:A", "HUDDLE"),
        ("FL:ACME:B", "BOARDROOM"),
        ("FL:ACME:C", "OPERATIONS_CENTER"),  # the one unpriced room
        ("FL:ACME:D", "AUDITORIUM"),
    ]
    with pytest.raises(UnpricedRoomCategoryError) as exc_info:
        for fl_label, category in rooms:
            resolve_price_tier(category, fl_label=fl_label)
    assert exc_info.value.category == "OPERATIONS_CENTER"
    assert exc_info.value.fl_label == "FL:ACME:C"


def test_a_run_of_only_priced_rooms_succeeds_completely() -> None:
    """The test that proves the guard is CONDITIONAL: if resolve_price_tier
    refused unconditionally, the previous test would pass for the wrong
    reason and this one would catch it -- a run with zero unpriced rooms
    must process every room with no exception."""
    rooms = [
        ("FL:ACME:A", "HUDDLE"),
        ("FL:ACME:B", "BOARDROOM"),
        ("FL:ACME:C", "TRAINING_ROOM"),
        ("FL:ACME:D", "AUDITORIUM"),
        ("FL:ACME:E", "FLEX_SPACE"),
    ]
    resolved = [resolve_price_tier(category, fl_label=fl_label) for fl_label, category in rooms]
    assert resolved == [
        "huddle_space",
        "large_boardroom",
        "training_room",
        "auditorium",
        "collaboration_space",
    ]
