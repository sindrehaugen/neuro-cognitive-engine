"""nce.vertical_modules.agreements.room_category_pricing — C-2 category to
B-10 price-tier resolution, refuse-and-name (Sindre ruling, 2026-09-20).

Bridges two independently-built pieces that were never wired to each other:
``system_design.room_categories`` (C-2's 11 AV/engineering room categories,
assigned per-FL via kg_edges) and ``price_rules.py``'s ``sla_room_pricing``
rule (B-10's 6 priceable tiers, real NOK/month rates). Full analysis and the
proposed mapping: ``C:\\Claude\\ROOM_CATEGORY_PRICE_MAPPING.md``.

Sindre's ruling (2026-09-20), from that file:
  - The 5 CONFIDENT mappings ship as proposed.
  - ``ALL_HANDS`` / ``WAR_ROOM`` / ``OPERATIONS_CENTER`` (no price tier
    exists at all): REFUSE, never zero-rate, never default to another
    tier's rate.
  - The three-way collapse (``MEETING_SMALL`` / ``CONFERENCE_MEDIUM`` /
    ``CONFERENCE_LARGE`` -> ``standard_meeting_room``) was flagged as ONE
    decision needing Sindre's ruling. He ruled on the orphans, not this.
    Treated identically to the orphans until he rules on the collapse
    specifically -- refuse and name, not quietly resolved by shipping it.

Refusal must name WHICH category and WHICH room (``fl_label``) -- the whole
argument for refusing over zero-rating is that it is loud; a vague refusal
throws that away.
"""

from __future__ import annotations

# The 5 CONFIDENT mappings only. Every other C-2 category (the 3 true
# orphans plus the 3 BLOCKED-pending-Sindre collapse categories) is
# deliberately absent from this dict -- resolve_price_tier() refuses on
# anything not listed here, which is the point.
CATEGORY_TO_PRICE_TIER: dict[str, str] = {
    "HUDDLE": "huddle_space",
    "BOARDROOM": "large_boardroom",
    "TRAINING_ROOM": "training_room",
    "AUDITORIUM": "auditorium",
    "FLEX_SPACE": "collaboration_space",
}

# Named separately from "true orphans" so a refusal message and any future
# audit can distinguish "no price tier exists at all" from "a decision is
# pending" -- both refuse today, but they are not the same kind of gap.
_TRUE_ORPHAN_CATEGORIES: frozenset[str] = frozenset({"ALL_HANDS", "WAR_ROOM", "OPERATIONS_CENTER"})
_BLOCKED_PENDING_COLLAPSE_RULING: frozenset[str] = frozenset(
    {"MEETING_SMALL", "CONFERENCE_MEDIUM", "CONFERENCE_LARGE"}
)


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
    """Return the B-10 price tier for a C-2 room category, or refuse loudly.

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

    Raises
    ------
    UnpricedRoomCategoryError
        The category is a true orphan (no price tier exists), is one of
        the three categories still blocked on Sindre's collapse ruling, or
        is not a recognised C-2 category at all.
    """
    cat = category.strip().upper()
    tier = CATEGORY_TO_PRICE_TIER.get(cat)
    if tier is not None:
        return tier

    if cat in _TRUE_ORPHAN_CATEGORIES:
        raise UnpricedRoomCategoryError(
            cat,
            fl_label=fl_label,
            reason="refusing rather than zero-rating or defaulting (Sindre ruling 2026-09-20)",
        )
    if cat in _BLOCKED_PENDING_COLLAPSE_RULING:
        raise UnpricedRoomCategoryError(
            cat,
            fl_label=fl_label,
            reason=(
                "the MEETING_SMALL/CONFERENCE_MEDIUM/CONFERENCE_LARGE -> "
                "standard_meeting_room collapse is still awaiting Sindre's ruling "
                "(ROOM_CATEGORY_PRICE_MAPPING.md) -- not shipped by assuming it"
            ),
        )
    raise UnpricedRoomCategoryError(
        cat,
        fl_label=fl_label,
        reason="not a recognised room category",
    )
