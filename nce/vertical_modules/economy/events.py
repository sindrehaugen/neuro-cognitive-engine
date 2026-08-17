"""
nce/vertical_modules/economy/events.py
=======================================
The **balance guarantee** — pure domain core. Zero DB, zero HTTP, zero web/admin imports.

Ported from Andreas's ``lib/finance/events/emit.ts`` (``UnbalancedPostingsError`` +
``assertBalanced``) and ``lib/finance/events/hash.ts`` (``canonicalHash``); reference tests
``tests/finance/events.test.ts``. Per ``docs/vertical_engines/08-economy-engine.md`` (core
function ``do_emit_financial_event``, build phase B1) and ``00-ENGINES-ROADMAP.md`` §9.1
(Economy owns POSTING).

Every financial write in the suite routes through this function. It is the single discipline
that stops an unbalanced posting reaching the ledger: postings must sum to zero within a
caller-supplied ``epsilon`` **at write time**, or the event is **rejected** with
``UnbalancedPostingsError``. Persistence into ``economy_postings`` is Wave 6; this module only
decides *whether an event is allowed to exist*, and returns its normalised, hashed form.

It NEVER auto-balances
----------------------
Silently inserting a balancing leg would defeat the entire guard: the ledger would tie out
while the underlying money did not. That is made impossible **by construction**, not by
discipline:

* the returned posting list is built by a single list comprehension over the caller's
  postings — one output per input. There is no ``append``, no ``insert``, no ``+ [...]``
  anywhere in this module's posting path, so no code path can produce a list longer than the
  input;
* amounts are never rounded, quantized or re-scaled — the normalised amount is numerically
  equal to the amount handed in;
* the balance verdict is a raise, not a repair: :func:`_assert_balanced` has exactly one
  outcome besides "return" and it is ``raise``.

``test_economy_financial_event.py`` pins all three.

Why ``Decimal`` and not ``float``
---------------------------------
A balance guard that sums money as binary floats betrays itself twice over. ``0.1 + 0.2 -
0.3`` is ``5.55e-17``, not ``0`` — so a genuinely balanced journal shows a residue; and the
epsilon that papers over that residue also becomes fuzzy, because the epsilon boundary itself
is not exactly representable. Both are cured by summing in ``Decimal``:

* ``int`` -> ``Decimal(value)`` — exact.
* ``Decimal`` -> itself — exact. (Money arrives as ``Decimal`` in this repo: there is no
  ``set_type_codec``, so a Postgres ``numeric`` column becomes a Python ``Decimal``, and
  ``Decimal - float`` raises ``TypeError``. Coercing every amount to ``Decimal`` up front is
  what keeps a mixed ``Decimal``/``float``/``int`` journal from exploding.)
* ``float`` -> ``Decimal(str(value))``, i.e. the *shortest round-tripping decimal* — the
  number the caller meant. ``Decimal(0.1)`` would instead capture the exact binary expansion
  ``0.1000000000000000055511151231257827…`` and reintroduce the very drift we are removing.
  Documented failure mode: a float that was already the wrong number (accumulated drift
  upstream, e.g. ``0.30000000000000004``) is faithfully carried through as
  ``Decimal("0.30000000000000004")``. This module cannot repair upstream drift; it only
  refuses to *add* any.

The sum is computed under a raised-precision context with the ``Inexact`` trap armed, so a
rounded (i.e. wrong) sum can never be silently compared against the tolerance — it raises
``ValueError`` instead. The comparison uses ``copy_abs()``, which is context-independent,
rather than ``abs()``, which rounds to context precision.

The epsilon boundary is exact and ported 1:1: the reference is
``Math.abs(sum) > TOLERANCE -> throw``, so a difference of **exactly** epsilon **passes**
(``>``, never ``>=``), in both directions.

Fail loud or fail conservative — never silently permissive
----------------------------------------------------------
Every rejected input below has a direction, and the direction is always "no event is
produced". The traps that would have failed *permissively* are called out because each one
silently lets unbalanced money through:

* ``float('nan')`` as an amount: ``abs(nan) > epsilon`` is ``False`` in Python — a NaN
  amount would sail past the guard. Non-finite amounts raise.
* ``epsilon = nan``: every comparison against NaN is ``False``, so **every** event would
  pass. Non-finite epsilon raises. So does ``inf`` (same effect) and a negative epsilon
  (incoherent: it rejects even a perfectly balanced event).
* ``True`` as an amount: ``isinstance(True, int)`` is ``True`` in Python, so a stray boolean
  would be summed as 1 NOK. ``bool`` is rejected before the ``int`` branch, everywhere.
* a string amount (``"100"``): never parsed. Matching the rule already established in
  ``matching.py`` — string parsing in money code must not be added here either.
* a non-list ``postings`` (a ``str`` is iterable and would be walked character by character;
  a ``dict`` iterates its keys): rejected explicitly rather than iterated.

Ported behaviour, deliberately kept: an event with **no** postings (key absent, or ``None``)
carries no balance obligation and passes — the reference's ``if (!postings ||
postings.length === 0) return``. Portal-internal cascade events (margin, scorecard, kickback
recalculations) bear no postings; the source event carries the GL impact. NOTE the boundary
this creates: this function cannot know that an event *should* have had postings, so a caller
that drops them by accident gets no complaint here. Deciding which event types require
postings belongs to the caller (Wave 6 / the tool layer), not to the balance guard.

The content hash
----------------
``canonicalHash`` (sha256 over key-sorted JSON) is ported, with the leaf encoding tightened.
The port target is *determinism*, not byte-equality with the TypeScript system — the two
write to different ledgers and the body shapes differ, so cross-system hash equality was
never achievable. What must hold is: stable across runs and processes, independent of dict
key order, and free of any collision that could let a mutated event verify as intact.

Leaf encoding (see :func:`_canonicalise`), every value tagged so no two types can collide:

===========================  ===========================================================
Python                       canonical form
===========================  ===========================================================
``None``                     JSON ``null``
``bool``                     JSON ``true`` / ``false`` (checked before ``int``)
``int``/``float``/``Decimal``  ``"d:<canonical decimal>"`` — see :func:`_decimal_str`
``str``                      ``"s:<text>"``
``datetime``                 ``"t:<UTC isoformat>"`` — aware only, normalised to UTC
``date``                     ``"t:<isoformat>"``
``list``/``tuple``           JSON array, order preserved
``dict``                     JSON object, string keys only, sorted
anything else                ``ValueError``
===========================  ===========================================================

One instant, one hash: ``datetime`` is normalised to UTC before it is rendered, and a NAIVE
``datetime`` is rejected. ``isoformat()`` renders whatever offset the object happens to
carry, so ``2026-04-18T12:00:00+02:00`` and ``2026-04-18T10:00:00+00:00`` — the same instant
— would otherwise hash differently. That is not cosmetic: Wave 6 stores ``occurred_at`` as
``timestamptz`` and asyncpg hands it back UTC-aware, so an event emitted with an Oslo-offset
datetime (the natural thing for a Norwegian caller) would verify as TAMPERED the moment its
hash was recomputed from the stored row. ``astimezone(timezone.utc)`` collapses every offset
spelling onto one. A naive datetime is *rejected* rather than assumed to be UTC, because
assuming would mint a silent third spelling of the same instant — the exact failure being
removed — and this module cannot know whether the caller meant UTC or Oslo. A plain ``date``
names a day, not an instant, so it carries no offset ambiguity and passes through as-is.
(The reference's ``Date -> toISOString()`` is UTC-with-``Z`` for the same reason; the trailing
``+00:00`` vs ``Z`` spelling is not matched, because the port target is determinism, not
byte-equality — see above.)

Consequences, all deliberate:

* **Numerically equal numbers hash identically** regardless of Python type or trailing
  zeros: ``100``, ``100.0``, ``Decimal("100.00")`` and ``Decimal("1E+2")`` all encode as
  ``"d:100"``. Same money, same hash.
* **No JSON number is ever emitted**, so the hash cannot depend on ``repr`` rules (Python
  renders ``1e16`` as ``1e+16``, JavaScript as ``10000000000000000``).
* **A string can never collide with a number**: ``"0.1"`` encodes as ``"s:0.1"``, the number
  ``0.1`` as ``"d:0.1"``. This is why the tags exist; an untagged canonical form makes those
  two indistinguishable, and a hash collision is a silent failure.
* **Unhashable objects raise** rather than falling back to ``str``/``repr``: a ``default=str``
  fallback would fold distinct objects together and, worse, embed memory addresses
  (``<X at 0x7f…>``) — a hash that changes between runs is not a hash.

Order rules, both tested: dict key order is **normalised** (``sort_keys=True``, recursive —
ported from ``canonicalJson``), so key insertion order cannot change the hash. Posting
**element** order is **not** normalised — a JSON array is ordered, the reference hashes the
array as given, and reordering a caller's journal lines to make hashes agree would be a
silent rewrite of their data. Reordered postings therefore hash differently, by design.

What the posting hash attests to: every posting field is inside the hash body, so ``account``,
``amount`` and the amount's **sign** are all attested (an amount encodes as ``"d:-100"``, not
as a magnitude). Two materially different journals therefore cannot share a hash — changing a
GL account, changing an amount, or flipping which line is the debit all move it. Because a
posting has no schema here beyond ``account``/``amount``, that holds for whatever extra
fields (``vat_code``, ``dimension``, …) the caller carries as well.

Excluded from the hash body: ``hash`` (self-reference), plus ``id`` and ``recorded_at``,
which the database fills — the reference excludes exactly these for the same reason
(including ``recordedAt`` makes the hash non-deterministic). Everything else in the event is
hashed, so no field can be tampered with unnoticed.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timezone
from decimal import Decimal, DecimalException, Inexact, localcontext
from typing import Any

# Working precision for the balance sum. Generous enough that any realistic journal adds
# exactly; anything beyond it trips the armed ``Inexact`` trap and raises rather than
# comparing a rounded sum against the tolerance.
_SUM_PRECISION = 1000

# Filled by the database (Wave 6), or by this function itself — never part of the content
# that the hash attests to. Ported from emit.ts's hashBody, which excludes id/recordedAt for
# exactly this reason. Snake_case only: this repo has no camelCase event fields, so a
# camelCase ``recordedAt`` would be treated as ordinary business content and hashed.
_HASH_EXCLUDED_KEYS = frozenset({"hash", "id", "recorded_at"})

_NUMBER_TAG = "d:"
_STRING_TAG = "s:"
_TIME_TAG = "t:"


class UnbalancedPostingsError(Exception):
    """An event's postings do not sum to zero within the tolerance — the event is rejected.

    Ported from ``emit.ts``. Attribute names are snake_cased to repo convention
    (``event_type``/``diff``/``postings`` for ``eventType``/``diff``/``postings``) and
    ``diff`` is an exact ``Decimal`` rather than a JavaScript number.
    """

    def __init__(
        self,
        event_type: str,
        diff: Decimal,
        postings: list[dict[str, Any]],
        tolerance: Decimal,
    ) -> None:
        self.event_type = event_type
        self.diff = diff
        self.postings = postings
        self.tolerance = tolerance
        super().__init__(
            f'Event "{event_type}" has unbalanced postings '
            f"(sum={_decimal_str(diff)} NOK, tolerance=+/-{_decimal_str(tolerance)}). "
            f"Debet and kredit must balance to zero."
        )


# ---------------------------------------------------------------------------
# Numeric coercion — the hostile boundary
# ---------------------------------------------------------------------------


def _as_decimal(value: Any, what: str) -> Decimal:
    """Coerce a money number to an exact ``Decimal``, or raise.

    ``bool`` is rejected FIRST because ``isinstance(True, int)`` is ``True`` in Python.
    Non-finite values are rejected because ``abs(nan) > epsilon`` is ``False`` — a NaN would
    otherwise pass the balance guard silently. Strings are never parsed. Nothing here
    degrades to a default: an amount we cannot represent exactly must stop the event, not
    quietly become zero.
    """
    if isinstance(value, bool):
        raise ValueError(f"{what}: bool is not an amount (got {value!r})")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{what}: amount must be finite (got {value!r})")
        # str() of a float is its shortest round-tripping decimal: the number the caller
        # meant, not the binary expansion Decimal(float) would capture.
        return Decimal(str(value))
    if isinstance(value, Decimal):
        if not value.is_finite():  # NaN, sNaN, +/-Infinity — non-signalling predicate
            raise ValueError(f"{what}: amount must be finite (got {value!r})")
        return value
    raise ValueError(f"{what}: expected int/float/Decimal, got {type(value).__name__}")


def _decimal_str(value: Decimal) -> str:
    """Canonical decimal text: no exponent, no trailing zeros, one spelling of zero.

    ``Decimal("100.00")``, ``Decimal("1E+2")`` and ``100`` all render as ``"100"``;
    ``Decimal("-0.0")`` renders as ``"0"``, not ``"-0"``, so negative zero cannot produce a
    second hash for the same amount.
    """
    with localcontext() as ctx:
        # normalize() is a context operation: at the default 28-digit precision it would
        # round a long amount before it ever reached the hash.
        ctx.prec = _SUM_PRECISION
        normalised = value.normalize()
    if normalised == 0:
        return "0"
    return format(normalised, "f")


# ---------------------------------------------------------------------------
# Event / posting validation and normalisation
# ---------------------------------------------------------------------------


def _event_type_of(event: dict[str, Any]) -> str:
    """Return the event's ``type``, or raise. An event with no type is not a ledger event.

    ``strip()`` is used only to detect a blank string; the value is neither rewritten nor
    used as a lookup key anywhere, so there is no normalised-vs-raw disagreement to exploit.
    """
    event_type = event.get("type")
    if not isinstance(event_type, str) or not event_type.strip():
        raise ValueError(f"financial event: 'type' must be a non-empty string, got {event_type!r}")
    return event_type


def _assert_account(raw: dict[str, Any], index: int) -> None:
    """Raise unless the posting carries a usable ``account``. A posting without one is not
    bookkeeping.

    The reference ``JournalPosting`` declares ``account: string`` non-optional and TypeScript
    enforces it at every call site; Python has no such enforcement, so the requirement has to
    be a runtime check or it does not exist. Without it a journal can be arithmetically
    perfect and financially meaningless — ``[{amount: 100}, {amount: -100}]`` balances, and
    would walk away with a hash attesting to money that is posted nowhere. B120 builds
    postings by looking the GL account up in ``finago-account-mapping.json``; a lookup miss
    yields ``None`` and a mistyped key (``acount``) yields nothing at all, and both land here.

    Rejected: absent, ``None``, empty/whitespace, and any non-``str`` — an ``int`` ``4300``
    is not the same canonical value as ``"4300"`` (``"d:4300"`` vs ``"s:4300"``), so allowing
    both would give one GL account two hashes.

    ``ValueError``, not ``UnbalancedPostingsError``: this is a malformed posting, not an
    arithmetic verdict. ``UnbalancedPostingsError`` carries ``diff``/``tolerance`` and tells
    the caller their money does not add up, which would be a false diagnosis here; it also
    matches how every other malformed-posting rejection in this module reports.

    This validates and does not rewrite: ``strip()`` only detects a blank string, and the
    caller's account is carried into the normalised posting verbatim, so there is no
    normalised-vs-raw disagreement to exploit (same rule as :func:`_event_type_of`).
    """
    account = raw.get("account")
    if not isinstance(account, str) or not account.strip():
        raise ValueError(
            f"postings[{index}]: 'account' must be a non-empty string, got {account!r}"
        )


def _normalise_posting(raw: Any, index: int) -> dict[str, Any]:
    """Copy one posting, replacing ``amount`` with its exact ``Decimal``. Never mutates *raw*."""
    if not isinstance(raw, dict):
        raise ValueError(f"postings[{index}]: expected an object, got {type(raw).__name__}")
    _assert_account(raw, index)
    if "amount" not in raw:
        raise ValueError(f"postings[{index}]: missing 'amount'")
    posting = dict(raw)
    posting["amount"] = _as_decimal(raw["amount"], f"postings[{index}].amount")
    return posting


def _normalise_postings(raw: Any) -> list[dict[str, Any]] | None:
    """Normalise the posting list, preserving order and length. ``None`` stays ``None``.

    The comprehension is the no-auto-balance proof: exactly one output posting per input
    posting, and no other statement in this module adds to the list.
    """
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)):
        # Explicit: a str would be walked character by character and a dict would yield its
        # keys, both of which "succeed" at producing nonsense.
        raise ValueError(f"financial event: 'postings' must be a list, got {type(raw).__name__}")
    return [_normalise_posting(posting, index) for index, posting in enumerate(raw)]


def _as_tolerance(epsilon: Any) -> Decimal:
    """Validate the balance tolerance. Non-finite epsilon makes every comparison ``False``,
    i.e. every event balanced — the loudest possible failure is the only safe one here."""
    if isinstance(epsilon, bool):
        raise ValueError(f"epsilon: bool is not a tolerance (got {epsilon!r})")
    tolerance = _as_decimal(epsilon, "epsilon")
    if tolerance < 0:
        raise ValueError(f"epsilon: tolerance must be >= 0, got {epsilon!r}")
    return tolerance


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


def _sum_amounts(postings: list[dict[str, Any]]) -> Decimal:
    """Sum posting amounts exactly, or raise if exactness is impossible."""
    with localcontext() as ctx:
        ctx.prec = _SUM_PRECISION
        ctx.traps[Inexact] = True
        try:
            total = Decimal(0)
            for posting in postings:
                total += posting["amount"]
        except DecimalException as exc:
            raise ValueError(
                f"financial event: posting amounts span too many digits to sum exactly "
                f"({exc.__class__.__name__}); refusing to compare a rounded sum against the "
                f"balance tolerance"
            ) from exc
    return total


def _assert_balanced(event_type: str, tolerance: Decimal, postings: list[dict[str, Any]]) -> None:
    """Raise ``UnbalancedPostingsError`` unless the postings sum to zero within *tolerance*.

    Ported 1:1 from ``assertBalanced``: the comparison is ``> tolerance``, so a difference of
    exactly the tolerance passes, in both directions. The only two outcomes are "return" and
    "raise" — there is deliberately no third branch that adjusts anything.
    """
    total = _sum_amounts(postings)
    # copy_abs(), not abs(): abs() rounds to the ambient context precision.
    if total.copy_abs() > tolerance:
        raise UnbalancedPostingsError(event_type, total, postings, tolerance)


# ---------------------------------------------------------------------------
# Canonical hashing
# ---------------------------------------------------------------------------


def _canonicalise(value: Any, path: str) -> Any:
    """Recursively convert *value* into a JSON-safe, type-tagged canonical structure."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return _NUMBER_TAG + _decimal_str(_as_decimal(value, path))
    if isinstance(value, str):
        return _STRING_TAG + value
    if isinstance(value, datetime):
        # datetime BEFORE date (datetime IS a date): only a datetime names an instant that
        # more than one offset can spell. Normalise to UTC so the offset cannot fork the
        # hash; reject naive rather than assuming UTC, which would just add a third spelling.
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError(
                f"{path}: datetime must be timezone-aware so its hash is offset-independent "
                f"(got naive {value!r})"
            )
        return _TIME_TAG + value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        # A calendar day, not an instant — no offset to normalise away.
        return _TIME_TAG + value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_canonicalise(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, dict):
        canonical: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(
                    f"{path}: object keys must be strings, got {type(key).__name__} ({key!r})"
                )
            canonical[key] = _canonicalise(item, f"{path}.{key}")
        return canonical
    raise ValueError(f"{path}: value of type {type(value).__name__} cannot be canonically hashed")


def _content_hash(event: dict[str, Any]) -> str:
    """sha256 of the key-sorted canonical JSON of *event*, minus the excluded keys."""
    body = {key: item for key, item in event.items() if key not in _HASH_EXCLUDED_KEYS}
    canonical_json = json.dumps(
        _canonicalise(body, "event"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def do_emit_financial_event(epsilon: float, event: dict[str, Any]) -> dict[str, Any]:
    """Validate an event's balance and return its normalised, hashed form.

    Parameters
    ----------
    epsilon:
        Balance tolerance in NOK (``NCE_ECONOMY_BALANCE_EPSILON``, default ``0.01`` at the
        call site — never hard-coded here). A finite, non-negative ``int``/``float``/
        ``Decimal``. ``0`` is legal and means "must balance exactly".
    event:
        The financial event. Recognised keys:
            ``type``      str, required, non-empty — the event type.
            ``postings``  list[dict] | None, optional. Each posting needs an ``account``
                          (non-empty ``str``, ported from the reference's non-optional
                          ``JournalPosting.account``) and an ``amount``
                          (``int``/``float``/``Decimal``, finite, never ``bool``); all other
                          fields (``comment``, ``vat_code``, ``dimension``, …) are carried
                          through untouched. Absent/``None``/empty = no balance obligation
                          (ported from the reference).
        Every other key is carried through unchanged and is included in the hash. Any
        ``datetime`` anywhere in the event must be timezone-aware.

    Returns
    -------
    dict
        A shallow copy of *event* with ``postings`` normalised (same order, same length,
        amounts as exact ``Decimal``) and ``hash`` set to the 64-char hex content hash. The
        caller's ``event`` and postings are never mutated.

    Raises
    ------
    UnbalancedPostingsError
        The postings do not sum to zero within *epsilon*. The event is rejected; nothing is
        corrected, balanced or rounded.
    ValueError
        The event, an account or an amount is malformed (see the module docstring's boundary
        rules), a ``datetime`` is naive, or the tolerance is not a finite non-negative
        number. Loud, and always in the direction of producing no event.
    """
    tolerance = _as_tolerance(epsilon)
    if not isinstance(event, dict):
        raise ValueError(f"financial event: expected an object, got {type(event).__name__}")

    event_type = _event_type_of(event)
    postings = _normalise_postings(event.get("postings"))

    # Ported early-return: no postings (absent, None, or empty) = no bookkeeping = nothing to
    # balance. An empty list is kept as an empty list, distinct from None, because the two
    # are distinct in the hash body exactly as in the reference.
    if postings:
        _assert_balanced(event_type, tolerance, postings)

    normalised = dict(event)
    normalised["postings"] = postings
    normalised["hash"] = _content_hash(normalised)
    return normalised
