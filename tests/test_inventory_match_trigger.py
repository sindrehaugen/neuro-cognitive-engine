"""Tests for the Inventory engine's goods-receipt match trigger
(Module 11, Wave 5 — Batch 133 — ``nce/vertical_modules/inventory/triggers.py``).

What is under test is the CARDINALITY and the TENANT SCOPE of one cross-engine
fire, not the match algorithm — Procurement owns that and is exercised here
only as the real collaborator it is.

Covers, per :func:`do_record_goods_receipt_and_evaluate_match`:

  1. Pure-logic validation (no DB): both match legs Inventory does not own
     (``po``, ``invoice``) are required, PRESENT and WELL-FORMED — a FINITE
     positive ``quantity`` and ``unit_price`` and a non-empty ``article_id`` on
     each — and all of it is checked BEFORE the receipt is recorded. That
     ordering is asserted directly rather than assumed, for presence AND for
     contents: raising after the receipt commits would leave a delivery whose
     match can never fire, because the retry is a replay and the replay gate
     refuses it.

  2. Integration (``@pytest.mark.integration``, live Postgres):
     (a) a genuinely-new receipt fires the match EXACTLY ONCE, and the real
         verdict (not a stub's) lands in ``goods_receipts.match_result``;
     (b) an identical REPLAY fires it ZERO additional times — the total stays
         one — and reports ``match_fired: False`` with no verdict of its own;
     (c) TWO CONCURRENT identical submissions still fire it exactly once, over
         REAL separate connections (``asyncio.gather``), never sequentially —
         the by-construction claim against the actual race the unique index
         exists to close;
     (d) a second namespace's receipt against the SAME ``po_ref`` never feeds
         the first namespace's match: each fire sees only its own tenant's
         quantity;
     (e) the fired quantity is CUMULATIVE across partial deliveries within one
         namespace — the property that makes (d)'s ``namespace_id`` predicate
         load-bearing rather than decorative;
     (f) the fired quantity counts ONLY the countable articles. An unrelated
         SKU on the same pallet — packing material, a free sample, a different
         PO line — must not be added in. This is a MONEY assertion, not a
         tidiness one: the confidence formula scores ``min(gr_qty,
         invoice_qty) / po_qty``, so an inflated received quantity can only
         ever CONCEAL a shortfall. Before the filter, a 3-of-10 delivery with
         7 units of packing foam beside it scored confidence 100.0 / GREEN — a
         perfect three-way match on a 70 % short delivery;
     (g) a NON-ASCII lowercase article is one article to the filter, on every
         database locale. The filter and Procurement must fold case with the
         SAME function; when they did not, a byte-identical Norwegian or
         German part number stopped matching itself and a complete delivery
         became a committed receipt with a permanently NULL verdict;
     (h) a DECLARED substitution is counted. This is the case
         ``_detect_substitution`` exists to serve, and a PO-article-only sum
         hard-failed every instance of it;
     (i) an UNDECLARED different article is still a shortfall — the opposite
         bound on (h), and the assertion that keeps the original defect
         closed;
     (j) a delivery carrying nothing countable is refused BEFORE anything is
         recorded, with a positive control proving the refusal is about the
         PO's history and not about the SKU.

Every fire is counted by wrapping the REAL
``handle_procurement_evaluate_match`` (:class:`_MatchCounter`), never by
replacing it with a stub: a counter that also short-circuits the collaborator
would leave "the match fired" unproven, which is the whole point of the wave.

The fire count is the discriminator for (b) and (c), and the DB CANNOT be —
stated here rather than implied. A re-fire of the same receipt writes
byte-identical ``match_result`` content, so the persisted row looks the same
whether the gate fired once or twice. That is a property of the WRITE, not of
the ``UPDATE``'s ``WHERE`` clause: the production code leaves that ``UPDATE``
unconditional because an ``… WHERE match_result IS NULL`` predicate would be
true every time it was evaluated, never because such a guard would keep (b)
green with the ``duplicate`` gate deleted. It would not, and the F2 mutants
below are the measurement.

Discrimination, proven by mutation (rule 11 / §6.4: no in-tree mutation, ever
— each mutant is an out-of-tree COPY of ``triggers.py`` loaded over the real
module by an out-of-tree pytest plugin, and the RED summaries are reported
alongside this file's gate output). Every run below is this file ALONE (44
tests: 28 unit + 16 integration) against a FRESH clone of the schema, because
a database carrying rows from an earlier run reddens tests that merely reuse a
``po_ref``, which is contamination rather than proof. The pristine control is
44 passed on ``LC_CTYPE=en_US.utf8`` AND on ``LC_CTYPE=C``:

  * delete the ``duplicate`` gate            → 2 failed: (b) and (c), both on
    the FIRE COUNT itself ("total fires is 2, expected 1"), which is why the
    count is asserted before the reported flags in both. A first attempt at
    this mutant reddened both tests for the WRONG reason — with the gate gone
    the replay path reaches ``receipt["po_ref"]``, which a replay payload does
    not carry, and died on ``KeyError`` before firing anything. That mutant
    proved only that the code crashes; the one reported repairs BOTH payload
    lookups (the ``_fire_match`` argument and the trailing ``log.info``) so the
    second fire genuinely happens and the counter is what fails;
  * the same mutant PLUS an ``… AND match_result IS NULL`` guard on the
    ``UPDATE``                               → still 2 failed, the same (b) and
    (c), in both variants: with the guard RAISING, on
    ``_persist_match_result``'s own ValueError ("the match verdict was
    computed but could not be recorded"); with the guard SILENT, on the fire
    count — byte-for-byte the RED the unguarded mutant gives. A guard there
    cannot rescue a broken ``duplicate`` gate, because the fire happens BEFORE
    the write it would guard. This is the measurement behind the paragraph
    above;
  * neuter ``AND gr.namespace_id = $1``      → 1 failed: (d), on a wrong NUMBER
    (``['5.000', '12.000']`` where ``['5.000', '7.000']`` was received). (d) is
    the only test that seeds one ``po_ref`` in two tenants, and every other
    test here keeps its ``po_ref`` to itself so that stays true — including the
    parametrised (g) and (h), which carry a ``po_ref`` PER CASE for exactly
    this reason. Measured: with (h)'s three cases sharing one ``po_ref`` across
    three fresh namespaces this same mutant reddened 3 tests, and the fix was
    the ``po_ref``, not the assertion;
  * sum EVERY sku (drop the article filter)  → 3 failed: (f) on the money —
    ``('10.000', 'GREEN', 100.0)`` where ``('3.000', 'YELLOW', 72.0)`` is
    correct — plus (i) on ``('10.000', 'YELLOW', 85.0)`` and (j)'s control on
    ``('10.000', 'GREEN', 100.0)``. This is the unfiltered sum, and this RED
    is the defect it shipped;
  * count the PO's article ONLY (drop the
    declared-substitution half)              → 4 failed: all three shapes of
    (h), and its mixed shape on ``('5.000', 'YELLOW', 80.0)`` where
    ``('10.000', 'GREEN', 100.0)`` is correct. The three pure-substitute
    shapes now fail on the PRE-FLIGHT's ``ValueError`` with nothing committed;
    on the commit that shipped this filter WITHOUT the pre-flight the same
    three payloads produced ``McpError [-32602]`` with the receipt, the
    ``qty_on_hand`` increment and the ledger row already written. Same defect,
    two different exits — the second one is why (j) exists;
  * count the invoice's article ALWAYS (drop
    the declaration test)                    → 1 failed: (i), on
    ``('10.000', 'YELLOW', 85.0)`` where ``('3.000', 'RED', 57.0)`` is
    correct. (h) and (i) are the two bounds, and neither alone pins the rule;
  * fold the COLUMN with SQL ``upper()`` and
    the ARGUMENT with Python's (the shipped
    behaviour)                               → 4 failed on ``LC_CTYPE=C``:
    (g) for all four articles, each on ``McpError [-32602]`` /
    ``goods_receipt['quantity'] must be positive, got 0.0``. On
    ``LC_CTYPE=en_US.utf8`` — which is what CI runs — the SAME mutant fails
    (g) for ``ART-WEIß`` and ``ART-ﬁX`` only, 2 failed: ``ø``/``å`` fold
    identically under both functions there, so the two Norwegian cases are
    green on that locale and RED on ``C``. Both locales are reported because a
    test that only reddens off-CI gates nothing, and a defect that only
    appears on the deployment locale is the one that costs money. Every case
    uses a BYTE-IDENTICAL ``sku`` and ``article_id``;
  * compare the SKU case-sensitively         → 1 failed: (f) shape 4 only, on
    the pre-flight's ``ValueError`` (before the pre-flight existed, on
    ``McpError [-32602]``). Reported precisely because it is LESS than the
    obvious guess: (g)'s four cases survive this mutant, since their ``sku``
    and ``article_id`` are byte-identical and need no case-folding at all.
    Shape 4 is still the only case-folding assertion in (f), and (g) is about
    WHICH fold, not whether;
  * leave leg CONTENTS unchecked (the wave's
    original behaviour)                      → 14 failed: both parametrised
    contents tests, on all seven numeric-field cases. They fail by proceeding
    PAST validation — ``AttributeError`` on a ``None`` pool in the first test,
    ``KeyError: 'po_ref'`` on the stub payload in the second — rather than by
    raising a differently-worded ``ValueError``; either way the expected
    ``ValueError`` does not arrive. The two ``article_id`` cases survive it,
    honestly reported: ``_require_leg_article`` is called a second time from
    the entry point because the articles also select the filter, so removing
    the check inside ``_require_match_leg`` does not disarm it;
  * drop ``math.isfinite`` alone             → 6 failed: the three non-finite
    cases of both contents tests. Those three are the ones that matter most,
    because ``NaN`` did not merely slip past the gate — ``min(100.0, nan)``
    is ``100.0``, so a 70 %-short delivery scored GREEN / confidence 100.0
    through the gate this batch added;
  * make a contents check STRICTER than
    Procurement's (require ``article_id`` to
    BE a ``str``, which Procurement does not) → 2 failed: both cases of the
    subset test, which is the only guard against drift in the direction that
    costs a caller a match Procurement would have scored. ``math.isfinite`` is
    the ONE narrowing this file deliberately allows, and the subset test's own
    payloads are finite so it does not contradict it. A "reject numeric
    strings" mutant was tried first and rejected as a measurement: it also
    reddens the three non-finite cases, so it measures two rules at once;
  * validate the legs AFTER recording        → 27 failed: every ordering and
    leg-validation test, plus (j) — they then reach a DB call with no pool, or
    record before refusing;
  * aggregate the LAST delivery only         → 3 failed: (e) on a wrong number
    (``['5.000', '3.000']`` for ``['5.000', '8.000']``), (f) shape 3 and (j)'s
    control, whose last delivery is all foam and therefore sums to zero
    (``McpError [-32602]``);
  * ``isinstance(raw, dict)`` instead of
    ``dict(raw)``                            → 1 failed: the subset test's
    key/value-pairs case. That shape is accepted by
    ``mcp_handlers.py``'s own ``dict(...)`` and was refused here; the subset
    test claimed to guard exactly this and did not cover it until this case
    was added;
  * drop the pre-flight refusal              → 1 failed: (j), which is the
    only test that asserts NOTHING was committed. It is a whole test rather
    than a docstring paragraph because the dead end it closes — a committed
    receipt, an incremented ``qty_on_hand``, a ledger row, a permanently NULL
    verdict, and a retry that reports ``ok: True`` forever — is exactly the
    shape ``_require_match_leg``'s own docstring exists to prevent.

Not proven here, and not claimed: the match algorithm itself (Procurement's,
covered by ``tests/unit/test_procurement_surface.py``), FORCE-RLS behaviour
through a real ``nce_app`` connection (this trigger adds no table and no
policy; migration 052's policy is proven in ``tests/test_inventory_gr.py``),
and any path that gives a verdict to a receipt that has none — the module
docstring names TWO ways to reach that state and both are accepted,
out-of-scope limitations. Nothing here exercises the pre-flight's concurrency
race (two simultaneous first deliveries, one of them carrying nothing
countable): that race resolves to a refusal with nothing written, which is
the safe side, and pinning it would need a scheduler this file does not have.
Nothing here asserts Procurement's own behaviour on a non-finite number
either — it still accepts one, and fixing that is ``three_way_match.py``'s,
outside this wave's two files.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from decimal import Decimal
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest

import nce.entity_resolution.ownership_seed as ownership_seed_module
from nce.auth import set_namespace_context
from nce.bom_lines import bom_line_label, create_bom_line
from nce.entity_resolution.ownership import OwnershipError, assert_owner
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.inventory import triggers
from nce.vertical_modules.inventory.triggers import (
    MATCH_TOOL_NAME,
    do_advance_bom_line_to_delivered,
    do_record_goods_receipt_and_evaluate_match,
)
from nce.vertical_modules.procurement.three_way_match import do_evaluate_three_way_match


async def _seed_ownership(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Seed the node-ownership registry for this namespace.

    B132b made ``do_record_goods_receipt`` (called by this module's own
    ``do_record_goods_receipt_and_evaluate_match``) perform a guarded
    ``GOODS_RECEIPT`` kg_nodes write (``assert_owner``, deny-by-default,
    Contract A). Any test that reaches that call must seed first or it is
    refused with ``OwnershipError``. Identical in shape to
    ``tests/test_inventory_gr.py``'s own ``_seed_ownership`` (one idiom
    across the module's test files, not a third variant). NOT autouse and
    NOT in conftest.py: seeding globally would disarm the deliberate
    deny-by-default proofs elsewhere in the repo."""
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)
            await seed_node_ownership_registry(conn, namespace_id)


# ---------------------------------------------------------------------------
# Shared fixtures/helpers. Every helper takes an explicit namespace_id and
# scopes its own SQL by it, matching tests/test_inventory_gr.py's convention.
# ---------------------------------------------------------------------------


class _EngineStub:
    def __init__(self, pg_pool: asyncpg.Pool | None) -> None:  # type: ignore[type-arg]
        self.pg_pool = pg_pool


class _MatchCounter:
    """Counts fires of ``procurement_evaluate_match`` while DELEGATING to the
    real handler.

    A stub that returned a canned verdict would prove the call site was
    reached and nothing about whether Procurement can act on what Inventory
    sends it — this wave's entire value. Arguments are snapshotted through a
    JSON round-trip so the recorded quantity is the same string form that
    reaches ``match_result``."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, engine: Any, arguments: dict[str, Any]) -> str:
        self.calls.append(json.loads(json.dumps(arguments, default=str)))
        return await self._real(engine, arguments)  # type: ignore[no-any-return]

    @property
    def fired_quantities(self) -> list[str]:
        return [str(call["goods_receipt"]["quantity"]) for call in self.calls]


@pytest.fixture
def match_counter(monkeypatch: pytest.MonkeyPatch) -> _MatchCounter:
    """Install the counter over the bare module-level name the trigger calls."""
    counter = _MatchCounter(triggers.handle_procurement_evaluate_match)
    monkeypatch.setattr(triggers, "handle_procurement_evaluate_match", counter)
    return counter


#: The ordered article. Every received line in this file that is MEANT to
#: count toward the match carries it as its ``sku``, because the trigger sums
#: only lines whose sku is the PO's ``article_id``. Using a decorative,
#: unrelated sku here (``SKU-FIRE-A`` against an ``ART-1`` PO, as an earlier
#: revision did) would make every quantity assertion below read 0 and hide
#: what the tests are actually about.
_ARTICLE = "ART-1"


def _perfect_legs(quantity: int = 5) -> dict[str, Any]:
    """A ``po``/``invoice`` pair that matches exactly — *quantity* @ 10.00 of
    one article on both sides, so a delivery of exactly that many units scores
    deterministically (confidence 100 → GREEN) and a tier assertion cannot
    flap."""
    return {
        "po": {"article_id": _ARTICLE, "quantity": quantity, "unit_price": 10.0},
        "invoice": {"article_id": _ARTICLE, "quantity": quantity, "unit_price": 10.0},
    }


async def _seed_location(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    name: str,
) -> uuid.UUID:
    async with pg_pool.acquire() as conn:
        location_id = await conn.fetchval(
            "INSERT INTO stock_locations (namespace_id, kind, name, parent_id, level) "
            "VALUES ($1, 'warehouse', $2, NULL, 0) RETURNING id",
            namespace_id,
            name,
        )
    assert location_id is not None
    return location_id


async def _get_match_result(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    receipt_id: str,
) -> dict[str, Any] | None:
    async with pg_pool.acquire() as conn:
        raw = await conn.fetchval(
            "SELECT match_result FROM goods_receipts WHERE namespace_id = $1 AND id = $2",
            namespace_id,
            uuid.UUID(receipt_id),
        )
    if raw is None:
        return None
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


async def _count_matched_receipts(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> int:
    async with pg_pool.acquire() as conn:
        return await conn.fetchval(  # type: ignore[no-any-return]
            "SELECT COUNT(*) FROM goods_receipts "
            "WHERE namespace_id = $1 AND match_result IS NOT NULL",
            namespace_id,
        )


async def _side_effects(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> tuple[int, int, int]:
    """Everything ``do_record_goods_receipt`` writes, as three counts.

    A refusal that leaves a receipt behind is the failure this file is built
    around, and "no receipt row" alone would not catch it: the receipt, the
    ``inventory_items`` increment and the ``inventory_transactions`` ledger row
    are written in ONE transaction, so all three are the assertion."""
    async with pg_pool.acquire() as conn:
        receipts = await conn.fetchval(
            "SELECT COUNT(*) FROM goods_receipts WHERE namespace_id = $1", namespace_id
        )
        items = await conn.fetchval(
            "SELECT COUNT(*) FROM inventory_items WHERE namespace_id = $1", namespace_id
        )
        ledger = await conn.fetchval(
            "SELECT COUNT(*) FROM inventory_transactions WHERE namespace_id = $1", namespace_id
        )
    return (receipts, items, ledger)


# ---------------------------------------------------------------------------
# 1. Pure-logic validation — no DB reached (pg_pool is None; the guard under
#    test raises before engine.pg_pool is ever touched).
# ---------------------------------------------------------------------------


def _base_params(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "namespace_id": uuid.uuid4(),
        "po_ref": "PO-2001",
        "location_id": uuid.uuid4(),
        "lines": [{"sku": _ARTICLE, "qty": 5}],
        **_perfect_legs(),
    }
    params.update(overrides)
    return params


@pytest.mark.asyncio
async def test_rejects_missing_po_leg() -> None:
    """RED if the ``po`` leg becomes optional: Procurement's matcher requires a
    positive quantity and unit_price on it and cannot default them."""
    params = _base_params()
    del params["po"]
    with pytest.raises(ValueError, match="'po' is required"):
        await do_record_goods_receipt_and_evaluate_match(_EngineStub(None), params)


@pytest.mark.asyncio
async def test_rejects_missing_invoice_leg() -> None:
    """RED if the ``invoice`` leg becomes optional — a three-way match with two
    legs is not a match."""
    params = _base_params()
    del params["invoice"]
    with pytest.raises(ValueError, match="'invoice' is required"):
        await do_record_goods_receipt_and_evaluate_match(_EngineStub(None), params)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, {}, "PO-1", 7, []])
async def test_rejects_a_leg_that_is_not_a_non_empty_object(bad: Any) -> None:
    """RED if a truthy-but-wrong leg (a string, a number, an empty dict) is
    forwarded to Procurement instead of refused here."""
    with pytest.raises(ValueError, match="'po' is required"):
        await do_record_goods_receipt_and_evaluate_match(_EngineStub(None), _base_params(po=bad))


@pytest.mark.asyncio
async def test_the_legs_are_validated_before_the_receipt_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED if leg validation moves after :func:`do_record_goods_receipt`.

    That ordering is not cosmetic. A receipt commits on its own INSERT, so a
    leg rejected afterwards leaves a delivery recorded whose match can never
    fire: the caller's corrected retry is byte-identical, hits
    ``goods_receipts_idempotency_uq``, and the replay gate declines to fire.
    Asserted by counting calls to the recorder, not by inspecting a DB that
    would also be empty if the recorder merely failed."""
    recorded: list[dict[str, Any]] = []

    async def _never_reached(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
        recorded.append(params)
        return {"ok": True, "duplicate": False, "receipt_id": str(uuid.uuid4())}

    monkeypatch.setattr(triggers, "do_record_goods_receipt", _never_reached)

    params = _base_params()
    del params["invoice"]
    with pytest.raises(ValueError, match="'invoice' is required"):
        await do_record_goods_receipt_and_evaluate_match(_EngineStub(None), params)

    assert recorded == [], (
        "the receipt must NOT be recorded when a match leg is missing — a committed "
        "receipt whose match can never fire is worse than a refused call"
    )


# ---------------------------------------------------------------------------
# 1b. Leg CONTENTS, not just leg presence.
#
# A non-empty dict is not a usable match leg. Each payload below passed the
# old "is it a non-empty object" guard, committed the receipt, incremented
# qty_on_hand and appended a ledger row, and only THEN raised McpError
# [-32602] out of Procurement with its detail suppressed outside IS_DEV — and
# the caller's corrected retry then returned {ok: True, duplicate: True,
# match_fired: False}: a success payload, no error, and match_result NULL
# forever. These are the exact three payloads, measured on the wave commit.
# ---------------------------------------------------------------------------

_MALFORMED_LEGS: list[tuple[str, str, dict[str, Any], str]] = [
    # (id, leg name, leg value, expected message fragment)
    (
        "po-missing-unit_price",
        "po",
        {"article_id": _ARTICLE, "quantity": 5},
        "'po.unit_price' is required",
    ),
    (
        "invoice-quantity-zero",
        "invoice",
        {"article_id": _ARTICLE, "quantity": 0, "unit_price": 10.0},
        "'invoice.quantity' must be positive",
    ),
    ("po-unrelated-keys", "po", {"x": 1}, "'po.article_id' is required"),
    (
        "po-quantity-not-a-number",
        "po",
        {"article_id": _ARTICLE, "quantity": "many", "unit_price": 10.0},
        "'po.quantity' must be a number",
    ),
    (
        "invoice-unit_price-negative",
        "invoice",
        {"article_id": _ARTICLE, "quantity": 5, "unit_price": -1.0},
        "'invoice.unit_price' must be positive",
    ),
    (
        "po-article_id-blank",
        "po",
        {"article_id": "   ", "quantity": 5, "unit_price": 10.0},
        "'po.article_id' must not be empty",
    ),
    # The three below are NOT "more of the same". A non-finite number does not
    # merely slip past `> 0` (`float("nan") <= 0` is False, so Procurement
    # accepts it too) — it lands on `max(0.0, min(100.0, nan))`, and
    # `min(100.0, nan)` is 100.0 in Python. Measured on the commit that added
    # the article filter: PO 10 x ART-1 @ 10.00, 3 delivered, invoice quantity
    # or unit_price the plain JSON string "nan" -> qty 3.000, GREEN,
    # confidence 100.0. That is the ORIGINAL defect this batch exists to close
    # — a 70 %-short delivery reading as a perfect match — surviving inside
    # the batch that closes it, reachable from valid JSON.
    (
        "invoice-quantity-nan",
        "invoice",
        {"article_id": _ARTICLE, "quantity": "nan", "unit_price": 10.0},
        "'invoice.quantity' must be a FINITE number",
    ),
    (
        "invoice-unit_price-nan",
        "invoice",
        {"article_id": _ARTICLE, "quantity": 5, "unit_price": "nan"},
        "'invoice.unit_price' must be a FINITE number",
    ),
    # "inf" scored RED / 0.0 rather than GREEN, i.e. it was wrong in a
    # direction that happened to be safe. Refused anyway: "safe by luck" is
    # not a rule, and 1e400 is a plain JSON number that parses to it.
    (
        "po-quantity-inf",
        "po",
        {"article_id": _ARTICLE, "quantity": "inf", "unit_price": 10.0},
        "'po.quantity' must be a FINITE number",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("leg_name", "leg", "expected"),
    [(n, leg, exp) for _id, n, leg, exp in _MALFORMED_LEGS],
    ids=[_id for _id, _n, _leg, _exp in _MALFORMED_LEGS],
)
async def test_rejects_a_leg_whose_contents_procurement_cannot_evaluate(
    leg_name: str, leg: dict[str, Any], expected: str
) -> None:
    """RED if leg CONTENTS go unchecked — the state the wave shipped in.

    The message must name the offending LEG and FIELD. Procurement's own
    rejection cannot: ``@mcp_handler`` maps it to a bare
    ``[MCP -32602] Invalid parameters`` and ``client_visible_detail`` withholds
    the text outside ``IS_DEV``, so the caller learns nothing about which of
    the six fields on the two legs it got wrong."""
    with pytest.raises(ValueError, match=re.escape(expected)):
        await do_record_goods_receipt_and_evaluate_match(
            _EngineStub(None), _base_params(**{leg_name: leg})
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("leg_name", "leg"),
    [(n, leg) for _id, n, leg, _exp in _MALFORMED_LEGS],
    ids=[_id for _id, _n, _leg, _exp in _MALFORMED_LEGS],
)
async def test_malformed_leg_contents_record_no_receipt(
    monkeypatch: pytest.MonkeyPatch, leg_name: str, leg: dict[str, Any]
) -> None:
    """RED if the contents check runs AFTER the receipt is recorded.

    The presence check above already ran before the recorder; this asserts the
    same for the contents check, which is the ordering that was actually
    missing. A receipt committed here is unmatchable forever — not through an
    exception the caller can act on, but through a later ``{ok: True,
    duplicate: True, match_fired: False}`` that looks like success."""
    recorded: list[dict[str, Any]] = []

    async def _never_reached(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
        recorded.append(params)
        return {"ok": True, "duplicate": False, "receipt_id": str(uuid.uuid4())}

    monkeypatch.setattr(triggers, "do_record_goods_receipt", _never_reached)

    with pytest.raises(ValueError):
        await do_record_goods_receipt_and_evaluate_match(
            _EngineStub(None), _base_params(**{leg_name: leg})
        )

    assert recorded == [], (
        f"a {leg_name} leg Procurement cannot evaluate must be refused BEFORE the "
        "receipt is recorded; this one committed a delivery whose match can never fire"
    )


class _ReachedTheRecorder(Exception):
    """Sentinel: validation let the call through to :func:`do_record_goods_receipt`."""


#: Leg SHAPES ``do_evaluate_three_way_match`` scores. Each is used for BOTH
#: legs. The second one is the finding this test previously claimed to guard
#: and did not cover: ``mcp_handlers.py``'s ``dict(arguments.get("po") or {})``
#: accepts any sequence of key/value pairs, and ``isinstance(raw, dict)``
#: refused exactly that.
_PROCUREMENT_ACCEPTS: list[tuple[str, Any]] = [
    ("a-dict-of-numeric-strings", {"article_id": 77, "quantity": "5", "unit_price": "10.00"}),
    (
        "a-sequence-of-key-value-pairs",
        [("article_id", 77), ("quantity", "5"), ("unit_price", "10.00")],
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "leg",
    [leg for _id, leg in _PROCUREMENT_ACCEPTS],
    ids=[_id for _id, _leg in _PROCUREMENT_ACCEPTS],
)
async def test_a_leg_procurement_accepts_is_accepted_here_too(
    monkeypatch: pytest.MonkeyPatch, leg: Any
) -> None:
    """RED if this module's checks become STRICTER than Procurement's.

    Drift in that direction is the one that costs something: it refuses a call
    Procurement would have scored. ``three_way_match.py`` reads its numbers
    through ``float(value)`` and its article through ``str(value).strip()``, so
    numeric STRINGS and a non-string article are legal there; and
    ``mcp_handlers.py`` builds each leg with ``dict(...)``, so a sequence of
    key/value pairs is legal there too. Asserted against the REAL matcher, not
    against a restatement of its rules — the same leg is fed to
    ``do_evaluate_three_way_match`` through the same ``dict(...)`` the handler
    applies.

    ``math.isfinite`` is the ONE narrowing this module makes deliberately, and
    it is declared in ``_require_leg_positive_numeric``'s docstring rather
    than hidden; every payload here is finite, so this test and that rule do
    not collide.

    "Accepted here" is proven by reaching the recorder, which is stubbed to
    raise a sentinel: had validation refused the leg, a ``ValueError`` would
    arrive instead. Deliberately independent of the ``duplicate`` gate and of
    any DB, so a mutation of the gate cannot make this test speak — which is
    also why the delivered ``sku`` is the article: a payload carrying nothing
    countable would send the pre-flight to a pool that is ``None`` here."""
    # Procurement accepts it ...
    verdict = do_evaluate_three_way_match({}, dict(leg), {"quantity": 5}, dict(leg))
    assert verdict["tier"] == "GREEN"

    # ... so this module must not refuse it.
    async def _sentinel(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
        raise _ReachedTheRecorder

    monkeypatch.setattr(triggers, "do_record_goods_receipt", _sentinel)

    with pytest.raises(_ReachedTheRecorder):
        await do_record_goods_receipt_and_evaluate_match(
            _EngineStub(None),
            _base_params(po=leg, invoice=leg, lines=[{"sku": "77", "qty": 5}]),
        )


# ---------------------------------------------------------------------------
# (a) A genuinely-new receipt fires the match exactly once.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_new_receipt_fires_the_match_exactly_once(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    match_counter: _MatchCounter,
) -> None:
    """RED if the match is fired zero times, twice, or with a payload the real
    Procurement handler cannot evaluate.

    The verdict asserted here is Procurement's OWN (the counter delegates), so
    a payload Inventory shapes wrongly fails here rather than silently
    recording an error object."""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    engine = _EngineStub(pg_pool)

    result = await do_record_goods_receipt_and_evaluate_match(
        engine,
        {
            "namespace_id": namespace_id,
            "po_ref": "PO-FIRE-1",
            "location_id": loc,
            "lines": [{"sku": _ARTICLE, "qty": 5, "unit_cost": Decimal("10.00")}],
            **_perfect_legs(),
        },
    )

    assert result["duplicate"] is False
    assert result["match_fired"] is True
    assert len(match_counter.calls) == 1, (
        f"exactly one fire per genuinely-new receipt, got {len(match_counter.calls)}"
    )

    fired = match_counter.calls[0]
    assert fired["namespace_id"] == str(namespace_id)
    assert fired["goods_receipt"]["po_ref"] == "PO-FIRE-1"
    assert fired["goods_receipt"]["receipt_id"] == result["receipt_id"]
    assert fired["goods_receipt"]["quantity"] == "5.000"

    stored = await _get_match_result(pg_pool, namespace_id, result["receipt_id"])
    assert stored is not None, "the verdict must be recorded on the receipt row"
    assert stored["tool"] == MATCH_TOOL_NAME
    assert stored["goods_receipt"]["quantity"] == "5.000"
    # Procurement's real verdict: an exact 5 @ 10.00 match on both legs.
    assert stored["verdict"]["tier"] == "GREEN"
    assert stored["verdict"]["confidence"] == 100.0
    assert stored["verdict"]["substitution"]["level"] == "EXACT"
    assert await _count_matched_receipts(pg_pool, namespace_id) == 1


# ---------------------------------------------------------------------------
# (b) A replay fires zero additional times.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_replayed_receipt_fires_the_match_zero_additional_times(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    match_counter: _MatchCounter,
) -> None:
    """RED if the trigger hooks the entry point instead of the insert.

    The submission is byte-identical, so ``goods_receipts_idempotency_uq``
    refuses it and no delivery exists to match. The TOTAL fire count — not a
    delta, not the DB row — is the assertion: a re-fire would overwrite
    ``match_result`` with identical content and leave no trace in the table."""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    engine = _EngineStub(pg_pool)
    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "po_ref": "PO-REPLAY-M1",
        "location_id": loc,
        "lines": [{"sku": _ARTICLE, "qty": 4, "unit_cost": Decimal("2.50")}],
        **_perfect_legs(),
    }

    first = await do_record_goods_receipt_and_evaluate_match(engine, params)
    assert first["match_fired"] is True
    assert len(match_counter.calls) == 1

    second = await do_record_goods_receipt_and_evaluate_match(engine, params)

    # The count comes FIRST on purpose: it is what this test claims to gate, so it
    # must be the line that reddens when the gate goes, not a reported flag that
    # happens to be checked earlier.
    assert len(match_counter.calls) == 1, (
        "a replayed receipt must fire the match ZERO additional times; total fires "
        f"is {len(match_counter.calls)}, expected 1"
    )
    assert second["duplicate"] is True
    assert second["match_fired"] is False
    assert "match_result" not in second, (
        "a replay has no verdict of its own — reporting one would imply a fire"
    )
    assert second["receipt_id"] == first["receipt_id"]
    assert await _count_matched_receipts(pg_pool, namespace_id) == 1


# ---------------------------------------------------------------------------
# (c) Two CONCURRENT identical submissions fire it once.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_concurrent_identical_submissions_fire_the_match_once(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    match_counter: _MatchCounter,
) -> None:
    """RED if "exactly once" holds only for sequential replays.

    ``asyncio.gather`` over REAL separate pool connections is the actual race
    the unique index closes; a sequential double-call cannot exercise it.
    Exactly one of the two calls must see ``duplicate: False``."""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    engine = _EngineStub(pg_pool)
    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "po_ref": "PO-CONCURRENT-M1",
        "location_id": loc,
        "lines": [{"sku": _ARTICLE, "qty": 6, "unit_cost": Decimal("1.00")}],
        **_perfect_legs(),
    }

    results = await asyncio.gather(
        do_record_goods_receipt_and_evaluate_match(engine, dict(params)),
        do_record_goods_receipt_and_evaluate_match(engine, dict(params)),
    )

    # Count first, for the same reason as the replay test above.
    assert len(match_counter.calls) == 1, (
        f"two concurrent identical submissions fired the match {len(match_counter.calls)} "
        "times, expected 1"
    )
    fired_flags = sorted(bool(r["match_fired"]) for r in results)
    assert fired_flags == [False, True], (
        f"exactly one of two concurrent identical submissions may fire, got {fired_flags}"
    )
    assert await _count_matched_receipts(pg_pool, namespace_id) == 1


# ---------------------------------------------------------------------------
# (d) Tenant isolation on the SHARED key — po_ref, not the receipt id.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_second_namespaces_receipt_never_fires_the_first_namespaces_match(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    make_namespace: Any,
    match_counter: _MatchCounter,
) -> None:
    """RED if the received-quantity aggregate is scoped by RLS alone.

    Both tenants receive against the SAME ``po_ref`` on purpose — supplier PO
    numbers are not globally unique, and ``idx_goods_receipts_namespace_po`` is
    namespace-first for exactly that reason. Keying on the receipt id instead
    would make this test unfalsifiable: ids are unique, so a missing
    ``namespace_id`` predicate would be invisible. Here it shows up as a WRONG
    NUMBER — 12 where 7 was received — and this pool is the owner pool, which
    BYPASSES FORCE RLS, so the policy cannot rescue an unscoped query."""
    await _seed_ownership(pg_pool, namespace_id)
    other_ns = await make_namespace()
    await _seed_ownership(pg_pool, other_ns)
    loc_a = await _seed_location(pg_pool, namespace_id, "Warehouse A")
    loc_b = await _seed_location(pg_pool, other_ns, "Warehouse B")
    engine = _EngineStub(pg_pool)
    shared_po_ref = "PO-NS-SHARED-1"

    result_a = await do_record_goods_receipt_and_evaluate_match(
        engine,
        {
            "namespace_id": namespace_id,
            "po_ref": shared_po_ref,
            "location_id": loc_a,
            "lines": [{"sku": _ARTICLE, "qty": 5}],
            **_perfect_legs(),
        },
    )
    result_b = await do_record_goods_receipt_and_evaluate_match(
        engine,
        {
            "namespace_id": other_ns,
            "po_ref": shared_po_ref,
            "location_id": loc_b,
            "lines": [{"sku": _ARTICLE, "qty": 7}],
            **_perfect_legs(),
        },
    )

    assert match_counter.fired_quantities == ["5.000", "7.000"], (
        "each tenant's fire must see ONLY its own received quantity; "
        f"got {match_counter.fired_quantities} (12.000 = the other tenant's 7 added on)"
    )

    stored_a = await _get_match_result(pg_pool, namespace_id, result_a["receipt_id"])
    stored_b = await _get_match_result(pg_pool, other_ns, result_b["receipt_id"])
    assert stored_a is not None and stored_b is not None
    assert stored_a["goods_receipt"]["quantity"] == "5.000"
    assert stored_b["goods_receipt"]["quantity"] == "7.000"

    assert await _count_matched_receipts(pg_pool, namespace_id) == 1
    assert await _count_matched_receipts(pg_pool, other_ns) == 1


# ---------------------------------------------------------------------------
# (e) The fired quantity is cumulative within one namespace — what makes (d)'s
#     namespace predicate load-bearing.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_fired_quantity_is_cumulative_across_partial_deliveries(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    match_counter: _MatchCounter,
) -> None:
    """RED if the fire reports only the triggering delivery.

    Two GENUINE partial deliveries against one PO — distinguished by
    ``delivery_note_ref``, so the second is not a replay — must make the second
    fire see 5 + 3 = 8. A three-way match compares ordered against received;
    reporting 3 would score every partial delivery as a shortfall. This is
    also the property that gives (d)'s ``namespace_id`` predicate something to
    protect: the aggregate spans rows, so an unscoped one spans tenants."""
    await _seed_ownership(pg_pool, namespace_id)
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    engine = _EngineStub(pg_pool)
    base: dict[str, Any] = {
        "namespace_id": namespace_id,
        "po_ref": "PO-PARTIAL-M1",
        "location_id": loc,
        **_perfect_legs(),
    }

    first = await do_record_goods_receipt_and_evaluate_match(
        engine,
        {**base, "delivery_note_ref": "DN-1", "lines": [{"sku": _ARTICLE, "qty": 5}]},
    )
    second = await do_record_goods_receipt_and_evaluate_match(
        engine,
        {**base, "delivery_note_ref": "DN-2", "lines": [{"sku": _ARTICLE, "qty": 3}]},
    )

    assert first["match_fired"] is True
    assert second["match_fired"] is True, (
        "a second delivery note is a GENUINE delivery, not a replay — it must fire"
    )
    assert second["receipt_id"] != first["receipt_id"]
    assert match_counter.fired_quantities == ["5.000", "8.000"], (
        "the second fire must report the CUMULATIVE received quantity against the PO; "
        f"got {match_counter.fired_quantities}"
    )
    assert await _count_matched_receipts(pg_pool, namespace_id) == 2


# ---------------------------------------------------------------------------
# (f) The cumulative sum counts the ORDERED ARTICLE ONLY.
#
# The money test. Everything above measures cardinality and tenant scope; this
# one measures the NUMBER, and the number is what a three-way match approves
# payment against.
# ---------------------------------------------------------------------------


def _verdict_of(result: dict[str, Any]) -> tuple[str, str, float]:
    """(quantity as fired, tier, confidence) from one trigger result."""
    stored = result["match_result"]
    return (
        str(stored["goods_receipt"]["quantity"]),
        str(stored["verdict"]["tier"]),
        float(stored["verdict"]["confidence"]),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_unrelated_sku_on_the_pallet_cannot_conceal_a_short_delivery(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
    match_counter: _MatchCounter,
) -> None:
    """RED if the received-quantity sum is not filtered to the PO's article.

    Ordered and invoiced: 10 × ``ART-1`` @ 10.00. Delivered: 3. Three shapes of
    the same 70 %-short delivery, each in its own namespace so the cumulative
    sum starts clean:

      1. 3 × ART-1 alone                            → 3.000, YELLOW, 72.0
      2. 3 × ART-1 + 7 × PACKING-FOAM, one receipt  → must stay 3.000/YELLOW
      3. 3 × ART-1, then 7 × PACKING-FOAM as a
         second, genuine delivery                   → must stay 3.000/YELLOW

    Shapes 2 and 3 scored 10.000 / GREEN / confidence 100.0 with the unfiltered
    sum — a perfect three-way match on a delivery missing 70 % of the goods.
    The concealment is one-directional and therefore always in the supplier's
    favour: ``_compute_confidence`` scores ``min(gr_qty, invoice_qty) /
    po_qty``, so surplus received quantity is clamped away by the ``min`` while
    a shortfall is what the ratio is built to expose.

    Shape 1 is the CONTROL and is the assertion that keeps the other two
    honest: without it, a filter that summed to zero would also produce "not
    GREEN" and would look like a pass."""
    legs = _perfect_legs(quantity=10)
    engine = _EngineStub(pg_pool)

    async def _deliver(
        ns: uuid.UUID, po_ref: str, note: str, lines: list[dict[str, Any]]
    ) -> dict[str, Any]:
        loc = await _seed_location(pg_pool, ns, f"Warehouse {note}")
        return await do_record_goods_receipt_and_evaluate_match(
            engine,
            {
                "namespace_id": ns,
                # A po_ref PER NAMESPACE, deliberately: (d) owns the
                # shared-po_ref-across-tenants case and must stay the only test
                # a neutered namespace predicate can kill. This test is about
                # the ARTICLE filter and nothing else.
                "po_ref": po_ref,
                "location_id": loc,
                "delivery_note_ref": note,
                "lines": lines,
                **legs,
            },
        )

    # 1. Control — the honest short delivery.
    ns1 = await make_namespace()
    await _seed_ownership(pg_pool, ns1)
    alone = await _deliver(ns1, "PO-FOAM-1", "DN-ALONE", [{"sku": _ARTICLE, "qty": 3}])
    assert _verdict_of(alone) == ("3.000", "YELLOW", 72.0)

    # 2. The same shortfall with packing foam on the same pallet.
    ns2 = await make_namespace()
    await _seed_ownership(pg_pool, ns2)
    with_foam = await _deliver(
        ns2,
        "PO-FOAM-2",
        "DN-FOAM",
        [{"sku": _ARTICLE, "qty": 3}, {"sku": "PACKING-FOAM", "qty": 7}],
    )
    assert _verdict_of(with_foam) == ("3.000", "YELLOW", 72.0), (
        "7 units of packing foam are not 7 units of the ordered article — counting "
        f"them scored this 70%-short delivery {_verdict_of(with_foam)}"
    )

    # 3. The same shortfall with the foam arriving as its own later delivery.
    ns3 = await make_namespace()
    await _seed_ownership(pg_pool, ns3)
    await _deliver(ns3, "PO-FOAM-3", "DN-GOODS", [{"sku": _ARTICLE, "qty": 3}])
    foam_later = await _deliver(
        ns3, "PO-FOAM-3", "DN-FOAM-LATER", [{"sku": "PACKING-FOAM", "qty": 7}]
    )
    assert _verdict_of(foam_later) == ("3.000", "YELLOW", 72.0), (
        "a later delivery of an unrelated SKU must not raise the matched quantity for "
        f"this PO line; got {_verdict_of(foam_later)}"
    )
    # This is the verdict of record for THIS receipt. Nothing is overwritten:
    # match_result is per-receipt-row, so the first delivery keeps its own verdict
    # and there are two rows with two. A wrong value here is still the value of
    # record for the receipt a reader looks at last.
    stored = await _get_match_result(pg_pool, ns3, foam_later["receipt_id"])
    assert stored is not None
    assert stored["goods_receipt"]["quantity"] == "3.000"
    assert stored["goods_receipt"]["article_id"] == _ARTICLE
    assert stored["verdict"]["tier"] == "YELLOW"

    # 4. Case is not identity. The stored sku is stripped but NOT case-folded,
    #    while Procurement's own article comparison is strip().upper() — so a
    #    line written in another case is the SAME article to the matcher and
    #    must be the same article to this sum. RED if the filter compares
    #    case-sensitively (0 received where 3 were delivered).
    ns4 = await make_namespace()
    await _seed_ownership(pg_pool, ns4)
    lower = await _deliver(ns4, "PO-FOAM-4", "DN-LOWER", [{"sku": _ARTICLE.lower(), "qty": 3}])
    assert _verdict_of(lower) == ("3.000", "YELLOW", 72.0), (
        f"{_ARTICLE.lower()!r} and {_ARTICLE!r} are one article to _detect_substitution; "
        f"got {_verdict_of(lower)}"
    )

    assert len(match_counter.calls) == 5, (
        f"one fire per genuinely-new receipt, got {len(match_counter.calls)}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_declared_equivalent_re_opens_the_concealment_path(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
    match_counter: _MatchCounter,
) -> None:
    """The article filter closes the concealment only while the extra article is
    UNDECLARED. One supplier-supplied boolean re-opens it.

    This pins behaviour that is CORRECT, which is why it asserts the GREEN
    outcome rather than guarding against it. Handed ``gr_qty=10`` Procurement
    returns GREEN/100 itself, and neither engine can know that ``PACKING-FOAM``
    is not a genuine equivalent of the ordered article — making exactly that
    judgement is what ``_detect_substitution`` was handed the declaration for.

    It is pinned because the declaration arrives on the INVOICE leg, i.e. from
    the supplier, on the money path this wave exists to protect, and because
    one identical physical delivery scores two verdicts depending on one
    boolean the supplier controls:

        undeclared              3.000   RED     57.0
        + equivalent_sku: true  10.000  GREEN  100.0

    Whoever hardens substitution acceptance (a catalogue of permitted
    equivalents, or an approval step) should start here. If this test goes RED
    the acceptance rule changed — a decision to make deliberately, not a
    failure to repair by editing the expected value.

    Module docstring, Known limitation 3."""
    engine = _EngineStub(pg_pool)
    foam = "PACKING-FOAM"

    async def _deliver(ns: uuid.UUID, note: str, invoice: dict[str, Any]) -> dict[str, Any]:
        loc = await _seed_location(pg_pool, ns, f"Warehouse {note}")
        return await do_record_goods_receipt_and_evaluate_match(
            engine,
            {
                "namespace_id": ns,
                "po_ref": f"PO-DECL-{note}",
                "location_id": loc,
                "delivery_note_ref": note,
                # The SAME physical delivery both times: 3 of the ordered
                # article, 7 of something else.
                "lines": [{"sku": _ARTICLE, "qty": 3}, {"sku": foam, "qty": 7}],
                "po": {"article_id": _ARTICLE, "quantity": 10, "unit_price": 10.0},
                "invoice": invoice,
            },
        )

    ns_plain = await make_namespace()
    await _seed_ownership(pg_pool, ns_plain)
    plain = await _deliver(
        ns_plain, "PLAIN", {"article_id": foam, "quantity": 10, "unit_price": 10.0}
    )
    assert _verdict_of(plain) == ("3.000", "RED", 57.0), (
        "an undeclared different article must not count toward the ordered line; "
        f"got {_verdict_of(plain)}"
    )

    ns_decl = await make_namespace()
    await _seed_ownership(pg_pool, ns_decl)
    declared = await _deliver(
        ns_decl,
        "DECL",
        {
            "article_id": foam,
            "quantity": 10,
            "unit_price": 10.0,
            "equivalent_sku": True,
        },
    )
    assert _verdict_of(declared) == ("10.000", "GREEN", 100.0), (
        "a declared equivalent is counted BY DESIGN — if this changed, the "
        f"substitution acceptance rule changed; got {_verdict_of(declared)}"
    )

    stored = await _get_match_result(pg_pool, ns_decl, declared["receipt_id"])
    assert stored is not None
    assert sorted(stored["goods_receipt"]["counted_articles"]) == sorted([_ARTICLE, foam]), (
        "the stored verdict must record WHICH articles it counted, so the reason a "
        "short delivery scored GREEN is recoverable from the row alone"
    )


# ---------------------------------------------------------------------------
# (g) A non-ASCII lowercase article is ONE article — on every DB locale.
#
# Shape 4 above only ever exercised `art-1` -> `ART-1`, which folds identically
# under every case-folding function there is, and therefore structurally could
# not see the defect below.
# ---------------------------------------------------------------------------

#: Articles whose case-fold discriminates between Postgres' ``upper()`` and
#: Python's ``str.upper()``. Each is delivered with a ``sku`` BYTE-IDENTICAL to
#: the PO's ``article_id``, so Procurement rates the pair EXACT and the only
#: thing that can go wrong is the filter's own comparison.
#:
#: The two Norwegian ones are the realistic case and are the reason this test
#: exists; they redden the two-function fold only under ``LC_CTYPE=C``, which
#: is the plain ``initdb`` default on a minimal Linux image and a deployment
#: target for this product. ``ART-WEIß`` (ß -> "SS" in Python, unchanged by
#: glibc's 1:1 ``towupper``) and ``ART-ﬁX`` (U+FB01 -> "FI" likewise) redden it
#: on ``en_US.utf8`` too, which is what CI runs — so this test gates on the CI
#: locale rather than only on the one that would have shipped the bug.
_NON_ASCII_ARTICLES = ["høyttaler-1", "kabel-å2", "ART-WEIß", "ART-ﬁX"]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("article", _NON_ASCII_ARTICLES)
async def test_a_non_ascii_article_is_one_article_to_the_filter(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
    match_counter: _MatchCounter,
    article: str,
) -> None:
    """RED if the two sides of the article comparison are folded by DIFFERENT
    functions.

    They were: the stored ``sku`` went through SQL ``upper()`` (locale
    dependent, strictly 1:1) and the PO's ``article_id`` through Python
    ``str.upper()`` (full Unicode, 1:N). Only the second matches Procurement's
    own ``strip().upper()``.

    The measured consequence, with the two strings byte-identical and
    Procurement rating them EXACT: the sum came back 0, Procurement rejected a
    non-positive quantity, and the caller got ``McpError [-32602]`` with the
    receipt COMMITTED, ``qty_on_hand`` incremented and ``match_result`` NULL
    forever. On ``LC_CTYPE=C`` that happened for every lowercase ``æ ø å ä ö
    ü`` — i.e. for ordinary Norwegian and German part numbers — while the
    identical payload scored GREEN on an ``en_US.utf8`` CI container. A
    silent, deployment-dependent wrong answer is the worst shape of defect
    this module can ship, so the assertion is the full verdict, not just
    "no exception"."""
    ns = await make_namespace()
    await _seed_ownership(pg_pool, ns)
    loc = await _seed_location(pg_pool, ns, "Warehouse")
    result = await do_record_goods_receipt_and_evaluate_match(
        _EngineStub(pg_pool),
        {
            "namespace_id": ns,
            # A po_ref PER CASE, for the reason (f) states: (d) must stay the
            # only test a neutered namespace predicate can kill, and each of
            # these cases runs in its own fresh namespace.
            "po_ref": f"PO-FOLD-{article}",
            "location_id": loc,
            "lines": [{"sku": article, "qty": 4}],
            "po": {"article_id": article, "quantity": 4, "unit_price": 10.0},
            "invoice": {"article_id": article, "quantity": 4, "unit_price": 10.0},
        },
    )

    assert _verdict_of(result) == ("4.000", "GREEN", 100.0), (
        f"a delivery of {article!r} against a PO for the byte-identical {article!r} is a "
        f"complete, correct delivery; got {_verdict_of(result)}"
    )
    assert result["match_result"]["verdict"]["substitution"]["level"] == "EXACT"
    assert len(match_counter.calls) == 1


# ---------------------------------------------------------------------------
# (h) A DECLARED substitution is counted — the case _detect_substitution
#     exists to serve, and the one a PO-article-only sum hard-failed.
# ---------------------------------------------------------------------------

_SUBSTITUTE = "SUB-9"

#: Every declaration ``three_way_match._detect_substitution`` honours, read off
#: that function rather than off a summary of it: ``equivalent_sku is True``,
#: a ``substitute_for`` that folds equal to the PO article, and a
#: ``compatible_with`` list containing it. The level each produces is asserted
#: too, so a change in Procurement's classification shows up here as a name
#: rather than as a silently different number.
#: The ``po_ref`` is part of each case for the reason (f) states: every test
#: here except (d) keeps its ``po_ref`` to itself, so that (d) stays the ONLY
#: test a neutered ``namespace_id`` predicate can kill. Three parametrised
#: cases sharing one ``po_ref`` across three fresh namespaces would have
#: broken that, and did — measured, before this line existed.
_DECLARED_SUBSTITUTIONS: list[tuple[str, str, dict[str, Any], str]] = [
    ("substitute_for", "PO-SUB-SF", {"substitute_for": _ARTICLE}, "EQUIVALENT_SKU"),
    ("equivalent_sku", "PO-SUB-EQ", {"equivalent_sku": True}, "EQUIVALENT_SKU"),
    ("compatible_with", "PO-SUB-CW", {"compatible_with": [_ARTICLE]}, "COMPATIBLE"),
]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("po_ref", "declaration", "level"),
    [(ref, d, lvl) for _id, ref, d, lvl in _DECLARED_SUBSTITUTIONS],
    ids=[_id for _id, _ref, _d, _lvl in _DECLARED_SUBSTITUTIONS],
)
async def test_a_declared_substitution_is_counted_toward_the_matched_quantity(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
    match_counter: _MatchCounter,
    po_ref: str,
    declaration: dict[str, Any],
    level: str,
) -> None:
    """RED if the summed article set is the PO's article ALONE.

    ``three_way_match.py``: "A substitution that is EXACT, EQUIVALENT_SKU, or
    COMPATIBLE is a VALID REPLACEMENT — the match continues and confidence is
    evaluated against the tolerance zone." It cannot continue on a quantity of
    zero, which is what a PO-article-only sum produces for a delivery made
    entirely of the declared substitute.

    Measured on the PO-article-only revision, for all three declarations
    below: a COMPLETE 10-of-10 delivery of the substitute, which Procurement
    scores GREEN / 100.0 on the physical total, produced ``McpError
    [-32602]`` with the receipt committed and ``match_result`` NULL. The
    reasoning that shipped it was "the filter removes no information from
    ``_detect_substitution``, which never sees this leg's SKUs" — a true
    premise with a false conclusion: the filter removes the QUANTITY
    ``_compute_confidence`` needs, not the information the classifier uses."""
    ns = await make_namespace()
    await _seed_ownership(pg_pool, ns)
    loc = await _seed_location(pg_pool, ns, "Warehouse")
    result = await do_record_goods_receipt_and_evaluate_match(
        _EngineStub(pg_pool),
        {
            "namespace_id": ns,
            "po_ref": po_ref,
            "location_id": loc,
            # The physical delivery is COMPLETE — 10 of 10 — in substitute units.
            "lines": [{"sku": _SUBSTITUTE, "qty": 10}],
            "po": {"article_id": _ARTICLE, "quantity": 10, "unit_price": 10.0},
            "invoice": {
                "article_id": _SUBSTITUTE,
                "quantity": 10,
                "unit_price": 10.0,
                **declaration,
            },
        },
    )

    assert _verdict_of(result) == ("10.000", "GREEN", 100.0), (
        f"a complete delivery of a substitute declared via {sorted(declaration)} must be "
        f"scored on its real quantity; got {_verdict_of(result)}"
    )
    assert result["match_result"]["verdict"]["substitution"]["level"] == level
    assert sorted(result["match_result"]["goods_receipt"]["counted_articles"]) == sorted(
        [_ARTICLE, _SUBSTITUTE]
    ), "the stored verdict must say WHICH articles were summed, or it cannot be read back"
    assert len(match_counter.calls) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_declared_substitution_is_counted_alongside_the_ordered_article(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
    match_counter: _MatchCounter,
) -> None:
    """RED if a PART-substitute delivery is scored as a shortfall.

    5 of the ordered article plus 5 of a declared equivalent IS ten units of
    what was ordered. The PO-article-only revision scored this complete
    delivery ``('5.000', 'YELLOW', 80.0)`` — no exception, no error, just a
    50 % shortfall invented out of a delivery that arrived in full. This is
    the quiet half of the same defect and the reason the article SET, not a
    fallback, is the fix."""
    ns = await make_namespace()
    await _seed_ownership(pg_pool, ns)
    loc = await _seed_location(pg_pool, ns, "Warehouse")
    result = await do_record_goods_receipt_and_evaluate_match(
        _EngineStub(pg_pool),
        {
            "namespace_id": ns,
            "po_ref": "PO-SUB-MIX",
            "location_id": loc,
            "lines": [{"sku": _ARTICLE, "qty": 5}, {"sku": _SUBSTITUTE, "qty": 5}],
            "po": {"article_id": _ARTICLE, "quantity": 10, "unit_price": 10.0},
            "invoice": {
                "article_id": _SUBSTITUTE,
                "quantity": 10,
                "unit_price": 10.0,
                "equivalent_sku": True,
            },
        },
    )

    assert _verdict_of(result) == ("10.000", "GREEN", 100.0), (
        f"5 ordered + 5 declared-equivalent is a COMPLETE delivery; got {_verdict_of(result)}"
    )
    assert len(match_counter.calls) == 1


# ---------------------------------------------------------------------------
# (i) An UNDECLARED different article is still a shortfall — the other bound.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_undeclared_different_article_is_still_a_shortfall(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
    match_counter: _MatchCounter,
) -> None:
    """RED if (h)'s fix is implemented as "count the invoice's article too".

    The invoice names a DIFFERENT article and declares nothing —
    ``_detect_substitution`` returns ``DIFFERENT``, Procurement applies its
    15-point penalty, and the seven substitute units are NOT a delivery of
    what was ordered. Counting them anyway scores ``('10.000', 'YELLOW',
    85.0)``; the correct reading is ``('3.000', 'RED', 57.0)`` — a 70 %-short
    delivery of the ordered article.

    (h) and this test are the two bounds on the rule, and neither alone pins
    it: (h) alone is satisfied by counting every article, this one alone by
    counting only the PO's. Together they say exactly "the invoice's article
    counts when, and only when, Procurement honours the declaration"."""
    ns = await make_namespace()
    await _seed_ownership(pg_pool, ns)
    loc = await _seed_location(pg_pool, ns, "Warehouse")
    result = await do_record_goods_receipt_and_evaluate_match(
        _EngineStub(pg_pool),
        {
            "namespace_id": ns,
            "po_ref": "PO-UNDECLARED-1",
            "location_id": loc,
            "lines": [{"sku": _ARTICLE, "qty": 3}, {"sku": _SUBSTITUTE, "qty": 7}],
            "po": {"article_id": _ARTICLE, "quantity": 10, "unit_price": 10.0},
            # No substitute_for, no equivalent_sku, no compatible_with.
            "invoice": {"article_id": _SUBSTITUTE, "quantity": 10, "unit_price": 10.0},
        },
    )

    assert _verdict_of(result) == ("3.000", "RED", 57.0), (
        "an undeclared different article is not a delivery of what was ordered; "
        f"got {_verdict_of(result)}"
    )
    assert result["match_result"]["verdict"]["substitution"]["level"] == "DIFFERENT"
    assert result["match_result"]["goods_receipt"]["counted_articles"] == [_ARTICLE]
    assert len(match_counter.calls) == 1


# ---------------------------------------------------------------------------
# (j) A delivery with nothing countable is refused BEFORE anything is recorded.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_delivery_with_nothing_countable_records_nothing(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
    match_counter: _MatchCounter,
) -> None:
    """RED if the article filter's dead-end door is merely documented.

    Measured on the commit that added the filter: a FIRST delivery of 7 x
    ``PACKING-FOAM`` against an ``ART-1`` PO returned ``McpError [-32602]
    {'detail': "goods_receipt['quantity'] must be positive, got 0.0"}`` — and
    the receipt was committed, ``qty_on_hand`` was 7.000, the ledger row was
    written, ``match_result`` was NULL, and every retry afterwards returned
    ``{ok: True, duplicate: True, match_fired: False}`` permanently. That is
    the exact shape ``_require_match_leg``'s own docstring says it exists to
    prevent, it was NEW in that commit, and it had no test.

    So the door is closed rather than named: the call is refused with a
    ``ValueError`` and NOTHING is written. All three side effects are asserted
    — receipt row, ``inventory_items`` row, ledger row — because they commit in
    one transaction and checking only the first would not prove much.

    The second half is the CONTROL, and it is what makes the first half a
    claim about the PO's history rather than about the SKU: the same all-foam
    delivery, after 3 units of the ordered article have arrived against the
    same ``po_ref``, is ACCEPTED and leaves the matched quantity at 3.000. A
    refusal rule that could not tell those apart would break partial-delivery
    workflows, which is worse than the door it closes."""
    engine = _EngineStub(pg_pool)

    # 1. Nothing countable, nothing banked -> refused, nothing written.
    ns1 = await make_namespace()
    await _seed_ownership(pg_pool, ns1)
    loc1 = await _seed_location(pg_pool, ns1, "Warehouse")
    with pytest.raises(ValueError, match=re.escape("carries none of the article(s)")):
        await do_record_goods_receipt_and_evaluate_match(
            engine,
            {
                "namespace_id": ns1,
                "po_ref": "PO-NOTHING-1",
                "location_id": loc1,
                "lines": [{"sku": "PACKING-FOAM", "qty": 7}],
                **_perfect_legs(quantity=10),
            },
        )

    assert await _side_effects(pg_pool, ns1) == (0, 0, 0), (
        "a refused submission must leave NO receipt, NO stock and NO ledger row — the "
        "committed-but-unmatchable receipt is the whole failure being closed"
    )
    assert match_counter.calls == [], "nothing was recorded, so nothing may fire"

    # 2. CONTROL — the same foam AFTER a real delivery is accepted.
    ns2 = await make_namespace()
    await _seed_ownership(pg_pool, ns2)
    loc2 = await _seed_location(pg_pool, ns2, "Warehouse")
    base: dict[str, Any] = {
        "namespace_id": ns2,
        "po_ref": "PO-NOTHING-2",
        "location_id": loc2,
        **_perfect_legs(quantity=10),
    }
    await do_record_goods_receipt_and_evaluate_match(
        engine, {**base, "delivery_note_ref": "DN-GOODS", "lines": [{"sku": _ARTICLE, "qty": 3}]}
    )
    foam_later = await do_record_goods_receipt_and_evaluate_match(
        engine,
        {**base, "delivery_note_ref": "DN-FOAM", "lines": [{"sku": "PACKING-FOAM", "qty": 7}]},
    )

    assert _verdict_of(foam_later) == ("3.000", "YELLOW", 72.0), (
        "a later all-foam delivery against a PO that HAS received the ordered article is "
        f"a legitimate receipt and must still fire; got {_verdict_of(foam_later)}"
    )
    assert len(match_counter.calls) == 2


# ---------------------------------------------------------------------------
# Batch 133b — bom-delivered-transition. do_advance_bom_line_to_delivered:
# flip a BOM_LINE to DELIVERED only when fully received, guarded by Batch
# 132a's own update_bom_line_status (assert_owner, transition="status:
# delivered"). All integration — every load-bearing assertion here needs
# Postgres and the registry.
# ---------------------------------------------------------------------------


async def _seed_bom_line(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    *,
    qty: str,
    quote_id: str = "Q-1",
    line_ref: str = "L1",
) -> tuple[str, str]:
    """Author one BOM_LINE via Batch 132a's OWN guarded writer
    (``content:create:design``, ``system_design``'s registered transition) —
    never a hand-rolled INSERT. Requires the registry to already carry that
    row, which ``_seed_ownership`` provides. Returns ``(quote_id, line_ref)``.
    """
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)
            await create_bom_line(
                conn,
                namespace_id,
                flow="design",
                writer_engine="system_design",
                quote_id=quote_id,
                line_ref=line_ref,
                qty=Decimal(qty),
                unit_price=Decimal("10.00"),
                line_total=Decimal(qty) * Decimal("10.00"),
            )
    return quote_id, line_ref


async def _bom_line_row(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    quote_id: str,
    line_ref: str,
) -> asyncpg.Record:
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM bom_line_content WHERE namespace_id = $1 AND bom_line_label = $2",
            namespace_id,
            bom_line_label(quote_id, line_ref),
        )
    assert row is not None
    return row


async def _seed_ownership_without_bom_delivered(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seed every registered row EXCEPT ``BOM_LINE``/``status:delivered``.

    An entirely EMPTY registry would also refuse ``content:create:design``
    and make it impossible to author the line at all — the discriminating
    test needs everything else present and ONLY the one grant under test
    missing. Rebinds ``ownership_seed_module._OWNERSHIP_ENTRIES`` via
    pytest's own ``monkeypatch`` fixture (auto-restored at teardown) — an
    in-memory rebind, not a file mutation (rule 11); the identical technique
    this file's own ``match_counter`` fixture already uses via
    ``monkeypatch.setattr``.
    """
    filtered = [
        e
        for e in ownership_seed_module._OWNERSHIP_ENTRIES
        if not (e.get("node_type") == "BOM_LINE" and e.get("transition") == "status:delivered")
    ]
    monkeypatch.setattr(ownership_seed_module, "_OWNERSHIP_ENTRIES", filtered)
    await _seed_ownership(pg_pool, namespace_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_flip_refused_when_delivered_row_absent(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a) THE GUARD IS REACHED — refused when the row is absent.

    With ``BOM_LINE``/``status:delivered`` missing from the seeded registry
    (every other row present), the flip raises ``OwnershipError`` AND the
    line's status is unchanged AND ``status_changed_at`` stays NULL — no
    write reached the row at all.

    Discriminating: without the ``assert_owner`` call inside
    ``update_bom_line_status``, this write simply succeeds.
    """
    await _seed_ownership_without_bom_delivered(pg_pool, namespace_id, monkeypatch)
    quote_id, line_ref = await _seed_bom_line(pg_pool, namespace_id, qty="5")
    engine = _EngineStub(pg_pool)
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    await do_record_goods_receipt_and_evaluate_match(
        engine,
        {
            "namespace_id": namespace_id,
            "po_ref": "PO-A",
            "location_id": loc,
            "lines": [{"sku": _ARTICLE, "qty": 5}],
            **_perfect_legs(quantity=5),
        },
    )

    with pytest.raises(OwnershipError):
        await do_advance_bom_line_to_delivered(
            engine,
            {
                "namespace_id": namespace_id,
                "quote_id": quote_id,
                "line_ref": line_ref,
                "po_ref": "PO-A",
                "article_id": _ARTICLE,
            },
        )

    row = await _bom_line_row(pg_pool, namespace_id, quote_id, line_ref)
    assert row["status"] == "DRAFT", (
        f"a refused flip must leave status untouched, got {row['status']!r}"
    )
    assert row["status_changed_at"] is None, "a refused flip must write nothing"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_grant_is_narrow_not_whole_node(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(b) 🔴 THE GRANT IS NARROW — this wave's real load-bearing claim.

    In a FULLY seeded namespace, ``assert_owner(..., "BOM_LINE", "inventory",
    transition="status:installed")`` raises, and so does the same call with
    ``transition=None``. Discriminating in the sharpest possible way: if the
    JSON row had been written with ``"transition": null``, ``_lookup_owner``'s
    fallback would find it and the ``transition=None`` call would PERMIT.
    Proved by mutation (``b133b_widen_row``), not by narration — see the
    RED/GREEN runs in the wave report.
    """
    await _seed_ownership(pg_pool, namespace_id)
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)
            with pytest.raises(OwnershipError):
                await assert_owner(
                    conn, namespace_id, "BOM_LINE", "inventory", transition="status:installed"
                )
            with pytest.raises(OwnershipError):
                await assert_owner(conn, namespace_id, "BOM_LINE", "inventory", transition=None)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fully_received_line_flips_to_delivered(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(c) Permitted when the row IS present — happy path.

    NOT guard-discriminating: this assertion passes identically with no
    guard at all. (a) and (b) above are the discriminating proofs.
    """
    await _seed_ownership(pg_pool, namespace_id)
    quote_id, line_ref = await _seed_bom_line(pg_pool, namespace_id, qty="5")
    engine = _EngineStub(pg_pool)
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    await do_record_goods_receipt_and_evaluate_match(
        engine,
        {
            "namespace_id": namespace_id,
            "po_ref": "PO-C",
            "location_id": loc,
            "lines": [{"sku": _ARTICLE, "qty": 5}],
            **_perfect_legs(quantity=5),
        },
    )

    result = await do_advance_bom_line_to_delivered(
        engine,
        {
            "namespace_id": namespace_id,
            "quote_id": quote_id,
            "line_ref": line_ref,
            "po_ref": "PO-C",
            "article_id": _ARTICLE,
        },
    )

    assert result["advanced"] is True
    assert result["reason"] is None
    row = await _bom_line_row(pg_pool, namespace_id, quote_id, line_ref)
    assert row["status"] == "DELIVERED"
    assert row["status_changed_at"] is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_partial_receipt_does_not_flip(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(d) A partial receipt (3 of 5 ordered) leaves the status untouched —
    asserted directly on the row, not merely "no exception was raised"."""
    await _seed_ownership(pg_pool, namespace_id)
    quote_id, line_ref = await _seed_bom_line(pg_pool, namespace_id, qty="5")
    engine = _EngineStub(pg_pool)
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    await do_record_goods_receipt_and_evaluate_match(
        engine,
        {
            "namespace_id": namespace_id,
            "po_ref": "PO-D",
            "location_id": loc,
            "lines": [{"sku": _ARTICLE, "qty": 3}],
            **_perfect_legs(quantity=3),
        },
    )

    result = await do_advance_bom_line_to_delivered(
        engine,
        {
            "namespace_id": namespace_id,
            "quote_id": quote_id,
            "line_ref": line_ref,
            "po_ref": "PO-D",
            "article_id": _ARTICLE,
        },
    )

    assert result["advanced"] is False
    assert result["reason"] == "partial"
    assert Decimal(result["ordered_qty"]) == Decimal("5")
    assert Decimal(result["received_qty"]) == Decimal("3")
    row = await _bom_line_row(pg_pool, namespace_id, quote_id, line_ref)
    assert row["status"] == "DRAFT"
    assert row["status_changed_at"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_successful_flip_moves_nothing_else_on_the_line(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(e) After a successful flip, content (qty/unit_price/line_total) is
    byte-identical to what :func:`_seed_bom_line` wrote — the flip advances
    ``status``/``status_changed_at`` and NOTHING else on the row, in
    particular no ``INSTALLED``/``TESTED`` advance and nothing resembling
    ``actual_cost`` (which has no column on this table at all)."""
    await _seed_ownership(pg_pool, namespace_id)
    quote_id, line_ref = await _seed_bom_line(pg_pool, namespace_id, qty="5")
    engine = _EngineStub(pg_pool)
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    await do_record_goods_receipt_and_evaluate_match(
        engine,
        {
            "namespace_id": namespace_id,
            "po_ref": "PO-E",
            "location_id": loc,
            "lines": [{"sku": _ARTICLE, "qty": 5}],
            **_perfect_legs(quantity=5),
        },
    )

    await do_advance_bom_line_to_delivered(
        engine,
        {
            "namespace_id": namespace_id,
            "quote_id": quote_id,
            "line_ref": line_ref,
            "po_ref": "PO-E",
            "article_id": _ARTICLE,
        },
    )

    row = await _bom_line_row(pg_pool, namespace_id, quote_id, line_ref)
    assert row["status"] == "DELIVERED"
    assert Decimal(str(row["qty"])) == Decimal("5")
    assert Decimal(str(row["unit_price"])) == Decimal("10.00")
    assert Decimal(str(row["line_total"])) == Decimal("50.00")
    assert row["frozen_at"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reflip_is_idempotent_no_second_write(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(f) Idempotent re-run: flipping an already-``DELIVERED`` line is a
    no-op, not a second write and not an error — ``status_changed_at`` is
    byte-identical before and after the second call."""
    await _seed_ownership(pg_pool, namespace_id)
    quote_id, line_ref = await _seed_bom_line(pg_pool, namespace_id, qty="5")
    engine = _EngineStub(pg_pool)
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    await do_record_goods_receipt_and_evaluate_match(
        engine,
        {
            "namespace_id": namespace_id,
            "po_ref": "PO-F",
            "location_id": loc,
            "lines": [{"sku": _ARTICLE, "qty": 5}],
            **_perfect_legs(quantity=5),
        },
    )
    params = {
        "namespace_id": namespace_id,
        "quote_id": quote_id,
        "line_ref": line_ref,
        "po_ref": "PO-F",
        "article_id": _ARTICLE,
    }

    first = await do_advance_bom_line_to_delivered(engine, params)
    assert first["advanced"] is True
    row_after_first = await _bom_line_row(pg_pool, namespace_id, quote_id, line_ref)

    second = await do_advance_bom_line_to_delivered(engine, params)
    assert second == {
        "advanced": False,
        "reason": "already_delivered",
        "bom_line_label": bom_line_label(quote_id, line_ref),
    }

    row_after_second = await _bom_line_row(pg_pool, namespace_id, quote_id, line_ref)
    assert row_after_second["status_changed_at"] == row_after_first["status_changed_at"], (
        "a second call must not write status_changed_at again"
    )
    assert row_after_second["updated_at"] == row_after_first["updated_at"], (
        "a second call must not touch the row at all"
    )
