"""
tests/unit/test_room_category_pricing.py
==========================================
C-2 category -> B-10 price-tier resolution, refuse-and-name
(nce/vertical_modules/agreements/room_category_pricing.py, 2026-09-20).

Covers Sindre's rulings from ROOM_CATEGORY_PRICE_MAPPING.md and the
follow-up three-way-collapse question:
  - The 6 CONFIDENT mappings resolve correctly (5 original + MEETING_SMALL,
    "meeting small is a meetroom").
  - CONFERENCE_MEDIUM/CONFERENCE_LARGE ("the others is custom project")
    return the CUSTOM_PROJECT sentinel, not a tier and not a refusal --
    the room IS priced, just not by this rule.
  - The 3 true orphans (no price tier exists anywhere, custom-project
    included) refuse, naming the category and the room.
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
    CUSTOM_PROJECT,
    UnpricedRoomCategoryError,
    resolve_price_tier,
)

_CONFIDENT_CASES = [
    ("HUDDLE", "huddle_space"),
    ("BOARDROOM", "large_boardroom"),
    ("TRAINING_ROOM", "training_room"),
    ("AUDITORIUM", "auditorium"),
    ("FLEX_SPACE", "collaboration_space"),
    ("MEETING_SMALL", "standard_meeting_room"),
]

_TRUE_ORPHANS = ["ALL_HANDS", "WAR_ROOM", "OPERATIONS_CENTER"]
_CUSTOM_PROJECT_CASES = ["CONFERENCE_MEDIUM", "CONFERENCE_LARGE"]


@pytest.mark.parametrize("category,expected_tier", _CONFIDENT_CASES)
def test_confident_mappings_resolve(category: str, expected_tier: str) -> None:
    assert resolve_price_tier(category) == expected_tier


def test_confident_mappings_are_exactly_six() -> None:
    """Pins the mapping's size so a silent addition or removal is caught
    here even if no other test names the changed entry."""
    assert len(CATEGORY_TO_PRICE_TIER) == 6
    assert set(CATEGORY_TO_PRICE_TIER) == {c for c, _ in _CONFIDENT_CASES}


@pytest.mark.parametrize("category", _CUSTOM_PROJECT_CASES)
def test_custom_project_categories_return_sentinel_not_a_tier_not_a_refusal(
    category: str,
) -> None:
    """These categories ARE priced -- just not by sla_room_pricing. Must
    neither raise (that would be the false-positive failure mode the whole
    refuse-and-name design exists to avoid: a billing run refusing on a
    room that is correctly priced elsewhere) nor return something that
    could be mistaken for a real tier."""
    result = resolve_price_tier(category, fl_label="FL:ACME:SITE:BLDG:1:ROOM:3")
    assert result == CUSTOM_PROJECT
    assert result not in CATEGORY_TO_PRICE_TIER.values(), (
        "CUSTOM_PROJECT must be visually and structurally distinct from every "
        "real tier id -- if this ever collided with a real tier, a caller that "
        "forgot to check the sentinel would silently treat a custom-project "
        "room as priced by sla_room_pricing"
    )


@pytest.mark.parametrize("category", _TRUE_ORPHANS)
def test_true_orphans_refuse_naming_category_and_room(category: str) -> None:
    with pytest.raises(UnpricedRoomCategoryError) as exc_info:
        resolve_price_tier(category, fl_label="FL:ACME:SITE:BLDG:2:ROOM:9")
    message = str(exc_info.value)
    assert category in message
    assert "FL:ACME:SITE:BLDG:2:ROOM:9" in message
    assert exc_info.value.category == category
    assert exc_info.value.fl_label == "FL:ACME:SITE:BLDG:2:ROOM:9"


def test_unrecognised_category_refuses() -> None:
    with pytest.raises(UnpricedRoomCategoryError):
        resolve_price_tier("NOT_A_REAL_CATEGORY")


def test_case_insensitive_on_input() -> None:
    assert resolve_price_tier("huddle") == "huddle_space"
    assert resolve_price_tier("Boardroom") == "large_boardroom"
    assert resolve_price_tier("conference_medium") == CUSTOM_PROJECT


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
        ("FL:ACME:F", "MEETING_SMALL"),
    ]
    resolved = [resolve_price_tier(category, fl_label=fl_label) for fl_label, category in rooms]
    assert resolved == [
        "huddle_space",
        "large_boardroom",
        "training_room",
        "auditorium",
        "collaboration_space",
        "standard_meeting_room",
    ]


def test_a_run_with_only_custom_project_and_priced_rooms_succeeds_completely() -> None:
    """A run mixing priced rooms and custom-project rooms must NOT refuse --
    custom-project is not an error state. This is the test that would catch
    a regression where CUSTOM_PROJECT gets treated as an unpriced category."""
    rooms = [
        ("FL:ACME:A", "HUDDLE"),
        ("FL:ACME:B", "CONFERENCE_MEDIUM"),
        ("FL:ACME:C", "CONFERENCE_LARGE"),
    ]
    resolved = [resolve_price_tier(category, fl_label=fl_label) for fl_label, category in rooms]
    assert resolved == ["huddle_space", CUSTOM_PROJECT, CUSTOM_PROJECT]
