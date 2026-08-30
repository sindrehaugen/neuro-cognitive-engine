"""
nce/vertical_modules/inventory/triggers.py
==========================================
Receive→Match→Cascade: fire Procurement's ``procurement_evaluate_match`` on a
genuinely-new goods receipt (Module 11, Wave 5 — ``gr-match-trigger``,
Batch 133), and record the verdict in ``goods_receipts.match_result``
(migration 052's reserved column, which named THIS batch as its writer).

Per ``docs/vertical_engines/11-inventory-engine.md`` (§"Initiates
Receive→Match→Cascade": "on ``do_record_goods_receipt``, fires Procurement's
``procurement_evaluate_match`` with ``{po, goods_receipt, invoice?}``";
§"Downstream boundary": "Inventory writes the edge and *triggers* the match —
it does NOT compute the match or post") and ``00-ENGINES-ROADMAP.md``
§2.11/§9.1/§9.5.

Goal, precisely
------------------
:func:`do_record_goods_receipt_and_evaluate_match` records the receipt through
Batch 132's untouched :func:`do_record_goods_receipt`, and — **only when that
call genuinely inserted a row** — fires ``procurement_evaluate_match`` once
and stores its verdict on the receipt. Inventory owns the ``goods_receipt``
leg of the match and nothing else: the ``po`` and ``invoice`` legs are passed
through verbatim from the caller, and the GREEN/YELLOW/RED verdict is computed
entirely inside Procurement.

Idempotency is INHERITED from the DB constraint, never re-implemented
------------------------------------------------------------------------
``goods_receipts_idempotency_uq`` (migration 052) is the sole arbiter of
"is this a replay". :func:`do_record_goods_receipt` reports that arbiter's
verdict as ``duplicate``: ``False`` means its ``ON CONFLICT … DO NOTHING``
INSERT returned a row, ``True`` means it returned none. This module's entire
idempotency is ONE gate on that flag — the entry point is deliberately NOT
hooked unconditionally, because the entry point runs on every replay while the
INSERT only succeeds once.

That is the whole mechanism, and it is deliberately the ONLY one. The
``UPDATE`` in :func:`_persist_match_result` is a plain unconditional write,
NOT an ``… WHERE match_result IS NULL`` guard — and the honest reason is that
such a guard would be VACUOUS, not that it would conceal anything. Every path
that reaches that ``UPDATE`` has, in this same call, just INSERTED the row it
addresses (``duplicate is False``), and no verdict has ever been computed for
that row; ``match_result IS NULL`` is therefore true whenever the predicate
would be evaluated, and ``id`` is the primary key, so the statement already
addresses exactly one row. The unconditional write is simply the simpler
statement of what is meant.

An earlier revision of this docstring claimed instead that the guard "would
keep the replay test green even with the ``duplicate`` gate deleted". That is
false, and measurably so: with the gate deleted, the guard *raising* reddens
both replay tests on :func:`_persist_match_result`'s own ``ValueError``, and
the guard made *silent* reddens them on the fire count itself ("total fires is
2, expected 1") — the identical RED the plain unguarded mutant produces.

The reason the call counter in ``tests/test_inventory_match_trigger.py`` is
the discriminator is structural and is unchanged: it observes the FIRE, while
every DB-shaped assertion can only observe the fire's EFFECT, and a re-fire of
the same receipt writes byte-identical content. That is a property of the
write, not of the ``WHERE`` clause.

Which received lines count, and how the two articles are folded
------------------------------------------------------------------
The quantity Procurement is shown is a SUM over received lines, and two
questions decide which lines join it. Both were got wrong once and are stated
here because a wrong answer to either is a wrong money answer, not a style
lapse.

**Which articles count** (:func:`_counted_articles`). The PO's own
``article_id`` always. The INVOICE's ``article_id`` too, but ONLY when the
invoice declares a substitution relationship Procurement itself honours —
which is decided by calling Procurement's own
``three_way_match._detect_substitution`` and testing its answer against
Procurement's own ``_VALID_SUBSTITUTION_LEVELS``, never by restating either
here. An earlier revision summed the PO article ALONE, and that hard-failed
every declared substitution: ``three_way_match.py``'s "the match continues"
for a valid replacement could not continue, because a complete 10-of-10
delivery of a declared equivalent summed to ZERO and Procurement rejected the
leg as a non-positive ``quantity``. The premise that revision argued from was
true — ``_detect_substitution`` is never shown this leg's SKUs — and the
conclusion drawn from it was false: the filter removes no INFORMATION from
the substitution logic, it removes the QUANTITY ``_compute_confidence``
needs, and that is the input a valid replacement is scored on.

An UNDECLARED different article is still not a delivery of what was ordered
and still reads as a shortfall. That is the fix this module must keep: an
unfiltered sum scored a 3-of-10 delivery with 7 units of packing foam beside
it as confidence 100.0 / GREEN.

**How the comparison is folded** (:func:`_fold_article`). ONE function folds
both sides — Python ``str(x).strip().upper()``, which is literally the fold
``_detect_substitution`` applies to the articles it compares. The per-SKU
subtotals come back from Postgres UNFOLDED and are folded here.

That is a correction, not a preference. An earlier revision folded the COLUMN
with SQL ``upper()`` and the ARGUMENT with Python ``str.upper()`` while
claiming both matched ``_detect_substitution``. They are different functions:
SQL ``upper()`` is locale-dependent (``towupper``, strictly 1:1) and Python's
is full Unicode (1:N, locale-independent). Only the argument side matched
Procurement. Measured with the stored ``sku`` and the PO ``article_id``
BYTE-IDENTICAL and Procurement rating the pair EXACT, that mismatch turned a
complete, correct delivery into ``McpError -32602`` with the receipt
committed and ``match_result`` NULL: on ``LC_CTYPE=en_US.utf8`` for
``ART-WEIß`` and ``ART-ﬁX``, and on ``LC_CTYPE=C`` — the plain ``initdb``
default on a minimal Linux image, which is a deployment target — for
``høyttaler-1`` and ``kabel-å2``. Under ``C``, SQL ``upper()`` leaves every
non-ASCII character alone, so ANY lowercase ``æ ø å ä ö ü`` in a Norwegian or
German part number failed, while the identical payload scored GREEN on a
CI container running ``en_US.utf8``. A deployment-dependent wrong answer on
ordinary customer data is the worst shape of defect this module can ship.

Folding in Python rather than moving both sides into SQL (``upper(...) =
upper($3)``, which would at least be self-consistent) is the deliberate pick:
it is the SAME function on both sides by construction rather than two
functions that happen to agree on ASCII, it is locale-independent for
non-ASCII too rather than merely consistent, and it is the function
Procurement uses — so "one article to this sum" and "one article to
``_detect_substitution``" cannot diverge. The cost is that Postgres returns
one row per distinct SKU on the PO instead of one scalar; that set is
bounded by the deliveries against a single PO in a single tenant, and the
expression was never indexable anyway.

Known limitations, named rather than papered over
-----------------------------------------------------
Because the gate is strictly ``duplicate is False``, a receipt that exists
without a verdict can never acquire one through this entry point: the retry is
byte-identical, so it is a replay and is refused. TWO distinct paths still
reach that state. Both are accepted limitations of this wave, not defects this
module quietly absorbs:

  1. **A fire that RAISES after the receipt commits.** The ``po``/``invoice``
     legs are therefore validated BEFORE :func:`do_record_goods_receipt` runs,
     and :func:`_require_match_leg` checks the leg CONTENTS Procurement
     requires — a FINITE positive ``quantity`` and ``unit_price`` and a
     non-empty ``article_id`` on each leg — not merely that a non-empty object
     was supplied. A leg malformed by those rules can no longer commit a
     receipt. Procurement may still reject a leg for a rule this module does
     not mirror, and such a raise still leaves a committed receipt behind.
  2. **The ordinary "record now, invoice later" workflow** — the quiet one,
     because NOTHING fails anywhere. A caller who records the delivery through
     :func:`do_record_goods_receipt` (which this module's own parameter
     documentation recommends for exactly that situation) and then, once the
     invoice arrives, submits the same delivery here, gets ``duplicate: True``
     and ``match_fired: False``: the match fires zero times and
     ``match_result`` stays NULL permanently. No exception is raised, and the
     returned payload reports ``ok: True``.

And the reported flag cannot distinguish those states from success:
``match_fired: False`` is the SAME answer for "this receipt is already
matched" and for "this receipt was never matched and never will be through
this entry point". Only ``goods_receipts.match_result`` — NULL versus a
verdict — separates them, and this function does not read it.

  3. **A declaration on the INVOICE leg re-opens the concealment this filter
     closed.** The filter defeats the packing-foam case only while the extra
     article is UNDECLARED. One supplier-supplied boolean changes that.
     Measured against an identical physical delivery — 3 units of the ordered
     article and 7 of packing foam, against a PO for 10:

       ==========================================  ====================
       invoice leg                                 verdict
       ==========================================  ====================
       ``{article_id: PACKING-FOAM}``, undeclared  ``3.000  RED   57.0``
       ``+ equivalent_sku: true``                  ``10.000 GREEN 100.0``
       ``+ substitute_for: "<ordered>"``           ``10.000 GREEN 100.0``
       ``+ compatible_with: ["<ordered>"]``        ``10.000 GREEN 100.0``
       ==========================================  ====================

     This is FAITHFUL, not a defect: handed ``gr_qty=10`` Procurement returns
     the same GREEN/100 itself, and neither engine can know that
     ``PACKING-FOAM`` is not a genuine equivalent — that judgement is exactly
     what ``_detect_substitution`` was given the declaration to make. It is
     recorded here because the declaration comes from the INVOICE, i.e. from
     the supplier, on the money path this wave exists to protect. Whoever
     hardens substitution acceptance (a catalogue of permitted equivalents, or
     an approval step) should start from this row rather than rediscover it.
     Pinned by ``test_a_declared_equivalent_re_opens_the_concealment_path``.

A general "fire the match for a receipt that has none" path is NOT in this
wave; it is what would close both, and no code here pretends to provide it.

A THIRD path is CLOSED rather than named, deliberately
---------------------------------------------------------
A revision of this module named, as its own limitation 2, a delivery carrying
none of the countable article: the sum was zero, Procurement rejected it, and
the receipt, the ``qty_on_hand`` increment and the ledger row were already
committed — reachable by simply receiving 7 units of ``PACKING-FOAM`` against
an ``ART-1`` PO as the first delivery. Naming that was not enough. It was NEW
behaviour introduced by the very filter that closed the GREEN-on-a-shortfall
bug, it had zero test coverage, and :func:`_require_match_leg`'s own docstring
declares that exact shape — "Procurement's rejection arrives AFTER the receipt
has committed" — as the failure it exists to prevent.

So it is DETECTED BEFORE RECORDING instead (:func:`_require_countable_delivery`):
when this payload's own lines carry none of the countable articles AND no
earlier receipt against this ``po_ref`` in this namespace carries any either,
the call is refused with a ``ValueError`` naming the article and the PO, and
NOTHING is committed. That removes the dead end rather than documenting it,
and it is exact rather than approximate — every quantity is strictly positive,
so "the post-insert sum will be > 0" is precisely "this payload carries one, or
the prior sum already is". The two costs, named:

  * The pre-flight reads the DB before the recorder does, but ONLY on the
    suspicious branch — a delivery that does carry a countable article
    short-circuits before any query, so the ordinary path pays nothing and the
    pure-logic tests still reach no DB.
  * ``namespace_id`` and ``po_ref`` are coerced before the recorder's other
    fields, using the recorder's OWN :func:`_as_ns_uuid`/:func:`_as_po_ref`
    (identical messages, and already the recorder's first two steps). A
    payload malformed in BOTH a later recorder field and this check now
    reports this one. Both are receipt-free refusals; the ORDER changed, not
    the outcome.
  * Two concurrent first deliveries — one all packing material, one carrying
    the goods — can have the former refused on a prior sum the latter has not
    committed yet. A refusal with nothing written is the safe side of that
    race, and the caller's retry succeeds.

Why a direct in-process call and not ``nce.events.bus.publish``
-------------------------------------------------------------------
The brief offers "the C4 event bus / A2A" and does not choose. The bus half is
choosable only in appearance: ``register_automation_subscribers()``
(``nce/vertical_modules/project/automation.py:648``) has zero production
callers on main, which is the stated, documented reason Batch 132c — the C4
``GOODS_RECEIPT.created`` publish — is BLOCKED (``goods_receipt.py``'s
"Explicit out of scope" section: "a publish today would look green and deliver
nothing"). Publishing here would reproduce that defect under a different batch
number and this wave's own acceptance test would be green while the match
never ran. The A2A half is therefore taken literally: Procurement's registered
tool handler is invoked, in process, and its real verdict is what lands in
``match_result``. Nothing here subscribes, publishes, or registers a handler.

Namespace scoping is EXPLICIT, never delegated to RLS
--------------------------------------------------------
Every statement carries its own ``namespace_id = $n`` predicate.
``scoped_pg_session`` sets the RLS GUC, but the owner/superuser pool used by
tests BYPASSES ``FORCE ROW LEVEL SECURITY``, so a query scoped only by policy
passes its own test and leaks in production. This matters most in
:func:`_total_received_against_po`, whose other predicate is ``po_ref`` —
which is NOT unique across tenants (``idx_goods_receipts_namespace_po`` is
namespace-first for exactly that reason). Two tenants receiving against the
same supplier PO number is ordinary, and an unscoped aggregate there does not
raise: it silently returns a LARGER quantity and hands Procurement another
tenant's deliveries as evidence.

What this wave does NOT do — by name
---------------------------------------
  * **No ``BOM_LINE`` read or write, and no ``DELIVERED`` status transition.**
    That is Batch 133b (``bom-delivered-transition``), which waits on Batch
    132a to create ``BOM_LINE`` nodes at all. This module names no BOM table,
    field or node type.
  * **No ``nce/config_data/node-ownership.json`` row**, no ``assert_owner``
    call, no Contract-A grant — nothing here writes a graph node, so there is
    no ownership to assert.
  * **No migration.** ``goods_receipts.match_result`` and
    ``idx_goods_receipts_namespace_po`` both already exist (migration 052).
  * **No stock movement, no ledger row, no PO record.** The receipt's own
    effects belong to :func:`do_record_goods_receipt` and are untouched; a
    purchase order is read from the caller's payload and never persisted.
  * **No MCP tool registration and no REST route.** Those are Batch 138a's,
    so :func:`do_record_goods_receipt_and_evaluate_match` is unreachable from
    any surface when this wave lands — exactly as ``do_record_goods_receipt``
    itself still is.
  * **No ``kg_nodes``/``kg_edges`` write**, in particular not the
    ``GOODS_RECEIPT -[against]-> PO`` edge, which is Batch 132b's.

Dependency direction (uncle-bob-craft)
-----------------------------------------
This module is an ORCHESTRATION seam, not a domain core: composing Inventory's
writer with Procurement's tool is its single job, and it holds no rules of its
own. It therefore may — and does — depend on both sides. It imports
``nce.db_utils.scoped_pg_session``, its sibling ``goods_receipt.py``, and
Procurement's registered handler; no web/HTTP/admin framework import appears
here, and ``NCEEngine`` is imported under ``TYPE_CHECKING`` only, matching
``goods_receipt.py``'s and ``stock.py``'s convention.

``handle_procurement_evaluate_match`` is imported as a BARE module-level name
and called as one. That import form is load-bearing rather than stylistic: it
is the seam the acceptance test's call counter substitutes to count fires
while still delegating to the real handler, and it is the same form
``goods_receipt.py`` uses for ``append_transaction`` for the same reason.

Four UNDERSCORED names are imported across the seam, and that is the point
------------------------------------------------------------------------------
``three_way_match._detect_substitution`` / ``._VALID_SUBSTITUTION_LEVELS`` and
``goods_receipt._as_ns_uuid`` / ``._as_po_ref`` are private by name. Importing
them is deliberate and is the SAFER option here, because every alternative is
a COPY: this module must decide "does Procurement honour this declared
substitution" and "what is the normalised ``po_ref``" identically to the
module that owns each question, and a restatement of either can drift silently
into a wrong number. Calling the owner's own function cannot. Both modules are
pure at these entry points (no I/O, no DB), and neither is modified by this
wave — nothing is added to their public surface, because widening a sibling's
API is not this wave's to do.

The drift that remains is bounded and lands in the NARROW direction, but it is
NOT always loud — an earlier revision of this paragraph claimed it was, and
that claim was measurably false.

``_VALID_SUBSTITUTION_LEVELS`` is imported **by reference**, so Procurement
widening that constant propagates here immediately and correctly. Drift exists
only in the narrower case where Procurement honours a level *without* listing
it in the constant. In that case:

* a delivery carrying **only** the substitute declines to count anything, and
  :func:`_require_countable_delivery` raises a loud, receipt-free refusal
  naming the article — nothing is committed;
* a **MIXED** delivery (some of the ordered article, some of the unrecognised
  substitute) does **not** refuse. ``_payload_carries_a_counted_article``
  short-circuits on the ordered article, so the pre-flight never fires, the
  substitute's quantity is dropped, and a verdict computed on the smaller
  number is persisted to ``goods_receipts.match_result``. Measured: 5 units
  ordered-article + 5 units declared-equivalent scores ``YELLOW / 80.0`` where
  ``GREEN / 100.0`` is correct.

That is a silent wrong verdict of record. It is CONSERVATIVE — under-counting
worsens the tier and can never approve payment for goods that did not arrive —
which is why this remains the right trade against testing
``level != "DIFFERENT"``, the direction that invents quantity. But it is a
wrong verdict, and it is written down here rather than described as impossible.
"""

from __future__ import annotations

import json
import logging
import math
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.inventory.goods_receipt import (
    _as_ns_uuid,
    _as_po_ref,
    do_record_goods_receipt,
)
from nce.vertical_modules.procurement.mcp_handlers import (
    handle_procurement_evaluate_match,
)
from nce.vertical_modules.procurement.three_way_match import (
    _VALID_SUBSTITUTION_LEVELS,
    _detect_substitution,
)

if TYPE_CHECKING:
    import asyncpg  # type: ignore[import-untyped]

    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.inventory.triggers")

#: The Procurement tool this trigger fires. Recorded verbatim inside
#: ``match_result`` so a stored verdict names the tool that produced it.
MATCH_TOOL_NAME: str = "procurement_evaluate_match"


# ---------------------------------------------------------------------------
# Validation — pure, no I/O. Runs BEFORE the receipt is recorded (see the
# module docstring's "Known limitation").
# ---------------------------------------------------------------------------


def _require_match_leg(raw: Any, name: str) -> dict[str, Any]:
    """Return the caller-supplied *name* leg of the match as a plain dict.

    ``procurement_evaluate_match`` is a THREE-way match: its implementation
    (``procurement/three_way_match.py::do_evaluate_three_way_match``) requires
    a positive ``quantity`` and ``unit_price`` plus a non-empty ``article_id``
    on both the ``po`` and the ``invoice``, so neither leg can be defaulted,
    inferred or omitted here.

    Those three fields are CHECKED here, not merely enumerated. An earlier
    revision checked only "non-empty dict" and left the rest to Procurement on
    the reasoning that mirroring its rules would let the two drift. The
    reasoning was backwards for this call site: Procurement's rejection
    arrives AFTER :func:`do_record_goods_receipt` has committed the receipt,
    incremented ``qty_on_hand`` and appended the ledger row, and the caller's
    corrected retry is then a replay that this module refuses to fire — a
    receipt with a permanently NULL ``match_result`` and no error to show for
    it (module docstring, limitation 1).

    Drift is bounded by construction, in the safe direction only. Each check
    below is written in the SAME form as Procurement's
    (``_require_positive_numeric``'s ``float(value) > 0``,
    ``_require_str``'s ``str(value).strip()``), so a leg accepted here is
    accepted there — with ONE deliberate exception, stated rather than
    smuggled: non-finite numerics are refused here and ACCEPTED there. See
    :func:`_require_leg_positive_numeric` for why that narrowing is the right
    side to err on. Should Procurement's rules WIDEN elsewhere, this check is
    merely narrower than necessary and refuses a call it could have made — a
    loud, receipt-free refusal. Should they NARROW, the surplus rule raises
    inside Procurement exactly as it does today. Neither direction can invent
    a verdict, because this function computes none.

    The leg is accepted in ANY shape ``dict(...)`` accepts, not just a
    ``dict`` — ``mcp_handlers.py``'s ``dict(arguments.get("po") or {})`` is
    what Procurement itself does, so a leg supplied as a sequence of
    ``(key, value)`` pairs is legal there and must be legal here. An earlier
    revision tested ``isinstance(raw, dict)`` and refused exactly that shape.
    Everything ``dict(...)`` rejects (a string, a number, ``None``) and
    everything it maps to an empty result (``{}``, ``[]``) is still refused
    here, with a message that names the leg.

    Runs before :func:`do_record_goods_receipt`: raising afterwards is the
    whole failure being avoided.
    """
    try:
        leg = dict(raw)
    except (TypeError, ValueError):
        leg = {}
    if not leg:
        raise ValueError(
            f"do_record_goods_receipt_and_evaluate_match: '{name}' is required and must "
            f"be a non-empty object — {MATCH_TOOL_NAME} is a three-way match (po × "
            "goods_receipt × invoice) and Inventory supplies only the goods_receipt leg"
        )
    _require_leg_article(leg, name)
    _require_leg_positive_numeric(leg, name, "quantity")
    _require_leg_positive_numeric(leg, name, "unit_price")
    return leg


def _require_leg_article(leg: dict[str, Any], name: str) -> str:
    """Return ``leg["article_id"]`` stripped, or raise naming leg AND field.

    Same form as ``three_way_match.py::_require_str`` (``str(value).strip()``,
    non-empty), so this accepts exactly what Procurement accepts.
    """
    if "article_id" not in leg:
        raise ValueError(
            f"do_record_goods_receipt_and_evaluate_match: '{name}.article_id' is "
            f"required — {MATCH_TOOL_NAME} identifies the ordered and the invoiced "
            "article by it"
        )
    article = str(leg["article_id"]).strip()
    if not article:
        raise ValueError(
            f"do_record_goods_receipt_and_evaluate_match: '{name}.article_id' must not be empty"
        )
    return article


def _require_leg_positive_numeric(leg: dict[str, Any], name: str, field: str) -> float:
    """Return ``leg[field]`` as a FINITE positive float, or raise naming leg AND field.

    Same form as ``three_way_match.py::_require_positive_numeric``
    (``float(value)``, then ``> 0``), so this accepts a numeric string exactly
    as Procurement does — plus ONE rule Procurement does not have.

    ``NaN`` and the infinities are refused, and this is STRICTER than
    Procurement, deliberately
    ------------------------------------------------------------------------
    Procurement accepts them: ``float("nan") <= 0`` is ``False``, so
    ``_require_positive_numeric`` lets ``NaN`` through, and
    ``_compute_confidence`` then returns ``max(0.0, min(100.0, nan))`` —
    which is ``100.0``, because ``min(100.0, nan)`` is ``100.0`` in Python.
    Measured on a PO of 10 × ``ART-1`` @ 10.00 with 3 units delivered, an
    ``invoice.quantity`` or ``invoice.unit_price`` of the plain JSON string
    ``"nan"`` scored the 70 %-short delivery **GREEN, confidence 100.0**.
    That is the exact failure mode this batch exists to close — a short
    delivery reading as a perfect match — arriving through the one new gate
    the batch added, from valid JSON. (``"inf"`` is honest by luck: it scores
    RED / 0.0. It is refused too, because "wrong for a reason that happens to
    be safe" is not a rule.)

    Refusing here rather than scoring there lands in the NARROWER direction,
    which this function's own contract already declares as the safe one: the
    caller gets a ``ValueError`` that names the leg and the field, BEFORE the
    receipt is recorded, and can correct and resubmit. The opposite choice —
    forwarding a non-finite number and letting Procurement clamp it to a
    perfect score — writes a verdict of record that approves payment on goods
    that did not arrive. A refusal costs a round trip; that costs money. The
    fix belongs in Procurement's own clamp as well, and this module cannot
    make it there: ``three_way_match.py`` is not in this wave's scope.
    """
    if field not in leg:
        raise ValueError(
            f"do_record_goods_receipt_and_evaluate_match: '{name}.{field}' is required — "
            f"{MATCH_TOOL_NAME} cannot score a leg without it"
        )
    raw = leg[field]
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"do_record_goods_receipt_and_evaluate_match: '{name}.{field}' must be a "
            f"number, got {raw!r}"
        ) from exc
    if not math.isfinite(value):
        raise ValueError(
            f"do_record_goods_receipt_and_evaluate_match: '{name}.{field}' must be a "
            f"FINITE number, got {raw!r} — {MATCH_TOOL_NAME} accepts non-finite numbers, "
            "and NaN then clamps to confidence 100.0 (min(100.0, nan) is 100.0): a perfect "
            "match on any delivery, however short"
        )
    if value <= 0:
        raise ValueError(
            f"do_record_goods_receipt_and_evaluate_match: '{name}.{field}' must be "
            f"positive, got {value}"
        )
    return value


# ---------------------------------------------------------------------------
# Which articles count toward the matched quantity — pure, no I/O.
# ---------------------------------------------------------------------------


def _fold_article(raw: Any) -> str:
    """THE single case-fold for every article/SKU comparison in this module.

    ``str(x).strip().upper()`` — byte-for-byte the fold
    ``three_way_match._detect_substitution`` applies to the two articles it
    compares, so "one article to this sum" and "one article to Procurement"
    are the same predicate rather than two that agree on ASCII.

    Both sides of every comparison go through THIS function, in Python. SQL's
    ``upper()`` is deliberately not used on either side: it is locale-dependent
    and strictly 1:1, so under ``LC_CTYPE=C`` (the plain ``initdb`` default on
    a minimal Linux image) it leaves ``ø``/``å``/``ä``/``ü`` untouched, and a
    byte-identical Norwegian part number stopped matching itself. The module
    docstring's "How the comparison is folded" carries the measurement.
    """
    return str(raw).strip().upper()


def _counted_articles(po_article: str, invoice_article: str, invoice: dict[str, Any]) -> set[str]:
    """The FOLDED article set whose received lines count toward the match.

    Always the PO's own article. Plus the invoice's article when — and only
    when — the invoice DECLARES a substitution Procurement honours.

    Which declarations those are is not restated here. Procurement's own
    ``_detect_substitution`` is called with exactly the arguments
    ``do_evaluate_three_way_match`` will pass it moments later (the same
    stripped articles, the same invoice dict), and its answer is tested
    against Procurement's own ``_VALID_SUBSTITUTION_LEVELS``. Whatever
    ``substitute_for`` / ``equivalent_sku`` / ``compatible_with`` mean there,
    they mean here, because it is the same call.

    Why the invoice's article at all: ``three_way_match.py`` says a valid
    replacement is one where "the match continues and confidence is evaluated
    against the tolerance zone". It cannot continue on a quantity of zero.
    Summing the PO article alone turned every declared substitution — the case
    ``_detect_substitution`` exists to serve — into ``McpError -32602`` on a
    complete, correct delivery.

    Why not every different article: an UNDECLARED one is a shortfall on this
    PO line, and summing it in is precisely the defect this filter closed.
    ``_compute_confidence`` scores ``min(gr_qty, invoice_qty) / po_qty``, so
    inflating ``gr_qty`` can only ever move the ratio TOWARD 1 — concealment
    is one-directional and always in the supplier's favour.
    """
    counted = {_fold_article(po_article)}
    substitution = _detect_substitution(po_article, invoice_article, invoice)
    if substitution["level"] in _VALID_SUBSTITUTION_LEVELS:
        counted.add(_fold_article(invoice_article))
    return counted


def _payload_carries_a_counted_article(raw_lines: Any, counted: set[str]) -> bool:
    """Does THIS submission's own ``lines[]`` carry one of *counted*?

    Returns ``True`` when it does — and also when the lines cannot be read
    here at all. That second case is not a silent fallback: this predicate
    exists only to decide whether :func:`_require_countable_delivery` may add
    an EXTRA refusal, and it must never pre-empt
    ``do_record_goods_receipt``'s own, better-worded error for a malformed
    ``lines[]``. "Cannot tell" therefore means "say nothing and let the
    recorder speak".

    The sku is normalised the way ``goods_receipt.py`` normalises it on the
    way in (``str(line.get("sku") or "").strip()``) and then folded by
    :func:`_fold_article`, so this asks the same question of the payload that
    :func:`_total_received_against_po` asks of the stored rows.
    """
    if not isinstance(raw_lines, list) or not raw_lines:
        return True
    for line in raw_lines:
        if not isinstance(line, dict):
            return True
        if _fold_article(line.get("sku") or "") in counted:
            return True
    return False


# ---------------------------------------------------------------------------
# DB-touching — every statement carries its OWN namespace_id predicate.
# ---------------------------------------------------------------------------


async def _total_received_against_po(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    po_ref: str,
    counted: set[str],
) -> Decimal:
    """Total quantity of the *counted* articles received against *po_ref* IN
    THIS NAMESPACE.

    This is the ``goods_receipt.quantity`` leg of the three-way match, and it
    is CUMULATIVE across every receipt booked against the PO rather than just
    the receipt that triggered this fire. A three-way match compares ordered
    against received against invoiced, and partial deliveries against one PO
    are routine (``goods_receipt.py``'s "Two genuinely distinct deliveries"
    section) — reporting only the last pallet would score every partial
    delivery as a shortfall.

    Lines are FILTERED to *counted* — the PO's article, plus the invoice's
    when the invoice declares a substitution Procurement honours
    (:func:`_counted_articles`). Everything else is not a delivery of what was
    ordered: packing material, a free sample, a different PO line that
    travelled on the same pallet, or an undeclared different article. Summing
    those in can only ever CONCEAL a shortfall, never reveal one, and that
    asymmetry is structural rather than incidental —
    ``three_way_match.py::_compute_confidence`` scores
    ``min(gr_qty, invoice_qty) / po_qty``, so raising ``gr_qty`` moves the
    quantity ratio toward 1 and can never move it away. An earlier revision
    summed every SKU on every receipt, and a 3-of-10 delivery with 7 units of
    packing foam beside it scored confidence 100.0 / GREEN.

    The FILTER IS APPLIED IN PYTHON, and that is the whole reason the query
    returns per-SKU subtotals instead of one scalar. Postgres groups and sums;
    it does not decide article identity. :func:`_fold_article` folds the
    stored ``sku`` and the counted articles with the same call, so the
    comparison is locale-independent and is the same one
    ``_detect_substitution`` makes. The predecessor folded the column with SQL
    ``upper()`` and the argument with Python's, which are different functions:
    under ``LC_CTYPE=C`` a byte-identical ``høyttaler-1`` stopped matching
    itself (module docstring, "How the comparison is folded").

    ``po_ref`` is matched verbatim against the stored column, which
    ``goods_receipt.py::_as_po_ref`` already normalised to the stripped,
    UPPER-CASED form — this function re-normalises nothing (migration 052's
    ``COMMENT ON COLUMN goods_receipts.po_ref``: "Batch 133's matcher queries
    this column: match against the upper-cased form").

    The ``namespace_id`` predicate is NOT redundant with RLS: ``po_ref`` is
    not unique across tenants, and the owner pool bypasses FORCE RLS, so
    without it this aggregate silently returns another tenant's quantities
    added to this one's (module docstring).
    """
    rows = await conn.fetch(
        """
        SELECT line->>'sku'                  AS sku,
               SUM((line->>'qty')::numeric)  AS qty
        FROM goods_receipts gr
        CROSS JOIN LATERAL jsonb_array_elements(gr.lines) AS line
        WHERE gr.namespace_id = $1::uuid
          AND gr.po_ref = $2
        GROUP BY 1
        """,
        str(ns_uuid),
        po_ref,
    )
    # NUMERIC in, Decimal out: asyncpg already hands back exact Decimals with
    # the stored 3dp scale, and Decimal addition is exact and order-independent,
    # so the sum needs no float and no str() round-trip. A NULL qty (a stored
    # line with no ``qty`` key — impossible through do_record_goods_receipt) is
    # skipped rather than coerced to zero, so it cannot silently pad the total.
    total = Decimal("0")
    for row in rows:
        if row["qty"] is None:
            continue
        if _fold_article(row["sku"] or "") in counted:
            total += row["qty"]
    return total


async def _require_countable_delivery(
    engine: NCEEngine,
    *,
    ns_uuid: UUID,
    po_ref: str,
    counted: set[str],
    raw_lines: Any,
) -> None:
    """Refuse — BEFORE anything is recorded — a submission whose matched
    quantity could only be zero.

    This is the deliberate answer to the dead-end door the article filter
    opened: 7 units of ``PACKING-FOAM`` received against an ``ART-1`` PO
    committed the receipt, the ``qty_on_hand`` increment and the ledger row,
    THEN raised ``McpError -32602`` out of Procurement, and every retry
    afterwards returned ``{ok: True, duplicate: True, match_fired: False}``
    with ``match_result`` NULL forever. Documenting that was rejected in
    favour of removing it (module docstring, "A THIRD path is CLOSED").

    The test is exact, not heuristic. Every received quantity is strictly
    positive (``goods_receipt.py::_as_qty``), so the post-insert total is > 0
    exactly when this payload carries a counted article OR the prior total
    already is. Both are checked, cheapest first: the DB is read ONLY when the
    payload itself carries none, which is why the ordinary path pays no query
    and the pure-logic tests reach no pool.

    This does not promise Procurement will accept the call — limitation 1 is
    unchanged, and a rule this module does not mirror can still raise after
    the receipt commits. It promises only that THIS door is shut.
    """
    if _payload_carries_a_counted_article(raw_lines, counted):
        return

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        prior = await _total_received_against_po(conn, ns_uuid, po_ref, counted)
    if prior > 0:
        return

    wanted = ", ".join(sorted(counted))
    raise ValueError(
        "do_record_goods_receipt_and_evaluate_match: this delivery carries none of the "
        f"article(s) the match counts ({wanted}) against po_ref {po_ref!r}, and no earlier "
        f"receipt against it does either — {MATCH_TOOL_NAME} would be handed a quantity of "
        "zero and reject it AFTER this receipt had committed. Nothing was recorded. Record "
        "it with do_record_goods_receipt instead, or submit it with the po/invoice legs "
        "that name what actually arrived"
    )


async def _persist_match_result(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    receipt_id: str,
    match_result: dict[str, Any],
) -> None:
    """Write *match_result* onto the receipt row — migration 052's reserved
    ``match_result`` column, whose own ``COMMENT ON COLUMN`` names Batch 133 as
    its writer and this table's only ``UPDATE``.

    Deliberately unconditional: no ``AND match_result IS NULL`` guard, for the
    plain reason that the predicate would be true every time it was evaluated.
    This function is reached only after ``duplicate is False`` — the row was
    INSERTED moments ago by this same call and no verdict has been computed
    for it — and ``id`` is the primary key, so the statement already addresses
    exactly one row. The guard would decide nothing; the simpler statement is
    the one that says what is meant. It is NOT omitted because it would mask a
    broken ``duplicate`` gate: with that gate deleted the replay tests redden
    either way (module docstring).

    The ``namespace_id`` predicate is kept because every write in this program
    keeps one; unlike :func:`_total_received_against_po`'s, it is not claimed
    to discriminate — ``id`` is the primary key and already unique.
    """
    updated = await conn.fetchval(
        """
        UPDATE goods_receipts
           SET match_result = $1::jsonb,
               updated_at   = now()
         WHERE id           = $2::uuid
           AND namespace_id = $3::uuid
        RETURNING id
        """,
        json.dumps(match_result, default=str),
        receipt_id,
        str(ns_uuid),
    )
    if updated is None:
        raise ValueError(
            "do_record_goods_receipt_and_evaluate_match: receipt "
            f"{receipt_id} is not visible in namespace {ns_uuid} — the match verdict "
            "was computed but could not be recorded"
        )


# ---------------------------------------------------------------------------
# The fire itself — one tool call, outside any open transaction.
# ---------------------------------------------------------------------------


async def _fire_match(
    engine: NCEEngine,
    *,
    ns_uuid: UUID,
    receipt_id: str,
    po_ref: str,
    po_article: str,
    counted: set[str],
    po: dict[str, Any],
    invoice: dict[str, Any],
) -> dict[str, Any]:
    """Fire ``procurement_evaluate_match`` once and record its verdict.

    The read and the write use two separate ``scoped_pg_session`` blocks with
    the tool call between them, deliberately: ``scoped_pg_session``'s own
    docstring forbids I/O inside the yielded block (it is one open
    transaction), and the handler reads ``procurement-weights.json`` and
    ``procurement-tolerances.json`` from disk. Nothing here claims the read
    and the write are atomic — the verdict is advisory, and the receipt,
    stock and ledger rows this trigger does not touch were already committed
    atomically by :func:`do_record_goods_receipt`.

    Returns the stored ``match_result`` body: the tool name, the
    ``goods_receipt`` leg exactly as fired, and Procurement's verdict.
    """
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        received_qty = await _total_received_against_po(conn, ns_uuid, po_ref, counted)

    goods_receipt_leg: dict[str, Any] = {
        "quantity": received_qty,
        "po_ref": po_ref,
        # Which article that quantity counts. Procurement never reads this key
        # (its goods-receipt contract is ``quantity`` alone), so it changes no
        # verdict; it is stored so a persisted match_result says WHAT was
        # counted rather than leaving the reader to infer it from the PO leg.
        "article_id": po_article,
        # ... and, since a declared substitution widens that set, the FOLDED
        # articles actually summed. Without this a stored verdict cannot be
        # read back: "10.000 against an ART-1 PO" means something different
        # when the invoice declared SUB-9 an equivalent. Also never read by
        # Procurement.
        "counted_articles": sorted(counted),
        "receipt_id": receipt_id,
    }

    raw_verdict = await handle_procurement_evaluate_match(
        engine,
        {
            "namespace_id": str(ns_uuid),
            "po": po,
            "goods_receipt": goods_receipt_leg,
            "invoice": invoice,
        },
    )

    match_result: dict[str, Any] = {
        "tool": MATCH_TOOL_NAME,
        "goods_receipt": goods_receipt_leg,
        "verdict": json.loads(raw_verdict),
    }

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        await _persist_match_result(conn, ns_uuid, receipt_id, match_result)

    return match_result


# ---------------------------------------------------------------------------
# Public: do_record_goods_receipt_and_evaluate_match
# ---------------------------------------------------------------------------


async def do_record_goods_receipt_and_evaluate_match(
    engine: NCEEngine, params: dict[str, Any]
) -> dict[str, Any]:
    """Record one inbound delivery and fire the three-way match on it — once.

    Parameters
    ----------
    params:
        Everything :func:`do_record_goods_receipt` accepts (``namespace_id``,
        ``po_ref``, ``location_id``, ``lines``, optional ``delivery_note_ref``
        and ``scans``) — forwarded unchanged and unwidened — PLUS the two
        match legs Inventory does not own:

        ``po``      (required) — ``article_id``, ``quantity``, ``unit_price``.
        ``invoice`` (required) — ``article_id``, ``quantity``, ``unit_price``.

        Each leg may be a ``dict`` or anything ``dict(...)`` accepts, matching
        ``mcp_handlers.py``'s own coercion. All three fields on each leg are
        required and are CHECKED HERE, before the receipt is recorded
        (:func:`_require_match_leg`), because ``procurement_evaluate_match``
        is a three-way match that cannot be evaluated without them and its
        rejection would otherwise arrive after the receipt had committed.

        The two ``article_id`` values additionally select WHICH received lines
        count toward the quantity Procurement is shown: the PO's always, the
        invoice's when the invoice declares a substitution Procurement honours
        (:func:`_counted_articles`). Other SKUs on the same pallet are not a
        delivery of what was ordered, and a submission carrying none of the
        counted articles — with none banked against this ``po_ref`` already —
        is refused before anything is recorded
        (:func:`_require_countable_delivery`).

        A caller with no invoice yet should call
        :func:`do_record_goods_receipt` directly: that function is untouched
        by this wave and remains THE way to record a delivery. Know the cost
        before taking that route, though — a receipt recorded that way can
        never be matched through this entry point once the invoice arrives,
        silently (module docstring, limitation 2). This function is the
        composition for callers that hold all three legs AT ONCE.

    Returns
    -------
    dict
        :func:`do_record_goods_receipt`'s own payload, plus
        ``"match_fired": True`` and ``"match_result"`` on a genuinely-new
        receipt, or ``"match_fired": False`` and no ``match_result`` on a
        replay. ``match_fired: False`` does NOT mean "already has a verdict" —
        see the module docstring's named limitations; only
        ``goods_receipts.match_result`` distinguishes the cases.

    Fires exactly once per receipt
    ---------------------------------
    The fire is gated on ``duplicate is False`` — the verdict of
    ``goods_receipts_idempotency_uq``, not of any check this module performs.
    A replayed or concurrently-duplicated submission inserts no row, so it
    fires the match ZERO additional times; the first submission fires it
    exactly once. This function never inspects ``match_result`` to decide
    whether to fire: the constraint has already answered "is this a new
    delivery", and a second, independently-derived answer to the same question
    could disagree with it.

    Raises
    ------
    ValueError
        Every ``ValueError`` :func:`do_record_goods_receipt` documents, plus a
        missing or uncoercible ``po``/``invoice``, plus a missing, non-numeric,
        non-finite, non-positive or empty ``quantity``/``unit_price``/
        ``article_id`` on either leg, plus a delivery that carries none of the
        counted articles and follows none — all raised BEFORE the receipt is
        recorded, naming the leg and the field (or the article and the PO), so
        a caller does not leave behind a committed receipt this function would
        then refuse to match.
    McpError
        Propagated verbatim from Procurement's handler for anything its rules
        reject that the checks above do not mirror (``@mcp_handler`` maps a
        ``ValueError`` to code ``-32602``). Not translated here: Inventory does
        not own those legs and must not restate the parts of their contract it
        is not enforcing. A cumulative received quantity of zero is no longer
        among them — :func:`_require_countable_delivery` refuses that case here,
        before anything commits.
    """
    po = _require_match_leg(params.get("po"), "po")
    invoice = _require_match_leg(params.get("invoice"), "invoice")
    po_article = _require_leg_article(po, "po")
    invoice_article = _require_leg_article(invoice, "invoice")
    counted = _counted_articles(po_article, invoice_article, invoice)

    # do_record_goods_receipt's OWN first two coercions, run early because the
    # pre-flight below needs both. Same functions, so the same messages — see
    # the module docstring for the one ordering consequence.
    ns_uuid = _as_ns_uuid(params.get("namespace_id"), "namespace_id")
    po_ref = _as_po_ref(params.get("po_ref"))
    await _require_countable_delivery(
        engine,
        ns_uuid=ns_uuid,
        po_ref=po_ref,
        counted=counted,
        raw_lines=params.get("lines"),
    )

    receipt = await do_record_goods_receipt(engine, params)

    if receipt.get("duplicate"):
        # THE gate. The receipt's INSERT hit goods_receipts_idempotency_uq and
        # wrote nothing, so there is no new delivery to match — fire zero times
        # rather than re-fire and overwrite a verdict with itself.
        log.info(
            "do_record_goods_receipt_and_evaluate_match: replay, match not fired receipt_id=%s",
            receipt.get("receipt_id"),
        )
        return {**receipt, "match_fired": False}

    match_result = await _fire_match(
        engine,
        ns_uuid=ns_uuid,
        receipt_id=str(receipt["receipt_id"]),
        po_ref=str(receipt["po_ref"]),
        po_article=po_article,
        counted=counted,
        po=po,
        invoice=invoice,
    )

    log.info(
        "do_record_goods_receipt_and_evaluate_match: fired %s ns=%s po_ref=%s "
        "articles=%s qty=%s receipt_id=%s tier=%s",
        MATCH_TOOL_NAME,
        ns_uuid,
        receipt["po_ref"],
        # The quantity is article-scoped, so the article SET is part of reading
        # the line at all: po_ref alone does not say what was counted, and the
        # PO article alone does not either once a substitution is declared.
        sorted(counted),
        match_result["goods_receipt"]["quantity"],
        receipt["receipt_id"],
        match_result["verdict"].get("tier"),
    )
    return {**receipt, "match_fired": True, "match_result": match_result}
