"""nce.vertical_modules.agreements.room_category_pricing — C-2 category to
B-10 price-tier resolution, refuse-and-name (Sindre ruling, 2026-09-20).

Bridges two independently-built pieces that were never wired to each other:
``system_design.room_categories`` (C-2's 11 AV/engineering room categories,
assigned per-FL via kg_edges) and ``price_rules.py``'s ``sla_room_pricing``
rule (B-10's 6 priceable tiers, real NOK/month rates). Full analysis and the
proposed mapping: ``C:\\Claude\\ROOM_CATEGORY_PRICE_MAPPING.md``.

Sindre's rulings (2026-09-20), from that file and the follow-up collapse
question:
  - The 5 original CONFIDENT mappings ship as proposed.
  - ``ALL_HANDS`` / ``WAR_ROOM`` / ``OPERATIONS_CENTER`` (no price tier
    exists at all): REFUSE, never zero-rate, never default to another
    tier's rate. Not extended to custom-project (below) without a further
    ruling -- refusing is the safe default and stays shipped for these
    three until Sindre says otherwise.
  - The three-way collapse question (``MEETING_SMALL`` / ``CONFERENCE_MEDIUM``
    / ``CONFERENCE_LARGE``) resolved as THREE outcomes, not two: "meeting
    small is a meetroom, the others is custom project." ``MEETING_SMALL``
    collapses into ``standard_meeting_room`` (a sixth CONFIDENT mapping).
    ``CONFERENCE_MEDIUM``/``CONFERENCE_LARGE`` are ``CUSTOM_PROJECT`` --
    priced per project, outside this rule entirely.

Three outcomes, not two -- named explicitly, not conflated
-------------------------------------------------------------
An unpriced category (the true orphans) is a GAP: something is wrong and a
human must fix the price list. A ``CUSTOM_PROJECT`` category is a deliberate
business model: the room IS priced, just not by ``sla_room_pricing``.
Conflating the two would make a billing run refuse on rooms that are
correctly priced elsewhere -- the exact false-positive failure mode the
whole B-11/B-12 refuse-and-name design exists to avoid. Callers MUST check
for the ``CUSTOM_PROJECT`` sentinel and treat it as "skip this room, it
bills elsewhere" -- never as a price tier (it is not one; passing it
through to ``price_rules.py`` unchecked would silently zero-rate the room,
``rates.get(room_cat, 0.0)``) and never as a reason to refuse the run.

Refusal must name WHICH category and WHICH room (``fl_label``) -- the whole
argument for refusing over zero-rating is that it is loud; a vague refusal
throws that away.
"""

from __future__ import annotations

# Sentinel returned by resolve_price_tier() for a category that IS priced,
# just not by this rule (Sindre ruling, 2026-09-20). Uppercase, unlike every
# real B-10 tier id (all lowercase_snake, e.g. "huddle_space") -- the casing
# convention itself makes the two kinds of return value visually distinct,
# on top of the identity check callers must do.
CUSTOM_PROJECT = "CUSTOM_PROJECT"

# The 6 CONFIDENT mappings (5 original + MEETING_SMALL). Every other C-2
# category (the 3 true orphans, the 2 CUSTOM_PROJECT categories) is
# deliberately absent from this dict -- resolve_price_tier() refuses or
# returns CUSTOM_PROJECT for anything not listed here, which is the point.
CATEGORY_TO_PRICE_TIER: dict[str, str] = {
    "HUDDLE": "huddle_space",
    "BOARDROOM": "large_boardroom",
    "TRAINING_ROOM": "training_room",
    "AUDITORIUM": "auditorium",
    "FLEX_SPACE": "collaboration_space",
    "MEETING_SMALL": "standard_meeting_room",
}

# Named separately from "true orphans" so a refusal message and any future
# audit can distinguish "no price tier exists at all" from "a decision is
# pending" -- both refuse today, but they are not the same kind of gap.
_TRUE_ORPHAN_CATEGORIES: frozenset[str] = frozenset({"ALL_HANDS", "WAR_ROOM", "OPERATIONS_CENTER"})
# Priced, just not by sla_room_pricing -- resolve_price_tier() returns
# CUSTOM_PROJECT for these, it does not raise.
_CUSTOM_PROJECT_CATEGORIES: frozenset[str] = frozenset({"CONFERENCE_MEDIUM", "CONFERENCE_LARGE"})


class UnpricedRoomCategoryError(ValueError):
    """Raised when a room's C-2 category has no B-10 price tier.

    Deliberately a refusal, not a zero-rated or default-tier fallback
    (Sindre ruling, 2026-09-20, ROOM_CATEGORY_PRICE_MAPPING.md): a
    confident, correctly-formatted billing line for the wrong amount is
    worse than a loud stop, because nothing fails and the first person to
    notice is a customer.
    """

    def __init__(self, category: str, *, fl_label: str | None = None, reason: str) -> None:
        self.category = category
        self.fl_label = fl_label
        where = f" on FL {fl_label!r}" if fl_label else ""
        super().__init__(f"{category} category{where} has no price tier -- {reason}")


def resolve_price_tier(category: str, *, fl_label: str | None = None) -> str:
    """Return the B-10 price tier for a C-2 room category, the CUSTOM_PROJECT
    sentinel, or refuse loudly.

    Parameters
    ----------
    category:
        A C-2 room category id (``system_design.room_categories``), e.g.
        ``"BOARDROOM"``. Case-insensitive on input; C-2's own ids are
        upper-case.
    fl_label:
        The FUNCTIONAL_LOCATION label this category was read from, when
        known -- carried into the refusal message so it names WHICH room,
        not just which category. Optional because some callers (e.g. a
        dry-run over a bare category list) have no room context yet.

    Returns
    -------
    str
        Either a real B-10 tier id (lowercase_snake, e.g.
        ``"huddle_space"``), or the ``CUSTOM_PROJECT`` sentinel -- callers
        MUST check ``result == CUSTOM_PROJECT`` before using the return
        value as a tier (see module docstring: "three outcomes, not two").

    Raises
    ------
    UnpricedRoomCategoryError
        The category is a true orphan (no price tier exists anywhere,
        including no custom-project arrangement) or is not a recognised
        C-2 category at all.
    """
    cat = category.strip().upper()
    tier = CATEGORY_TO_PRICE_TIER.get(cat)
    if tier is not None:
        return tier

    if cat in _CUSTOM_PROJECT_CATEGORIES:
        return CUSTOM_PROJECT

    if cat in _TRUE_ORPHAN_CATEGORIES:
        raise UnpricedRoomCategoryError(
            cat,
            fl_label=fl_label,
            reason="refusing rather than zero-rating or defaulting (Sindre ruling 2026-09-20)",
        )
    raise UnpricedRoomCategoryError(
        cat,
        fl_label=fl_label,
        reason="not a recognised room category",
    )
