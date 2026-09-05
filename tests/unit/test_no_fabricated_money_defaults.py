"""
tests/unit/test_no_fabricated_money_defaults.py
===============================================
**For a missing money value: fail, or carry the absence. Never substitute a
plausible number.**

This ledger reached that rule six times independently before anything enforced
it: B132f carries ``manufacturer="UNKNOWN"`` rather than guessing; D35 fails
closed rather than naming a blank party in a contract clause; D51 replaced two
invented ``100.0``s with ``None`` plus a reason; D48 is the same defect hiding
behind a good name; and two further live sites (an invoice three-way match and
a rebate forecast) were found by the scan this file institutionalises.

A fabricated money default is not a rounding problem. It converts *"we do not
know"* into a confident number, and the consumer cannot tell the two apart:
a missing ``line_total`` became a "100 % discrepancy" on a payment decision,
and a missing ``unit_price`` silently *understated* annual spend feeding rebate
bands.

What this scans
---------------
An **AST** walk over ``nce/`` (a regex cannot tell a default from a
comparison), for three deliberately narrow shapes on a fixed list of money
field names:

===========================================  ======================================
shape                                        example
===========================================  ======================================
``.get("<money>", <numeric literal>)``       ``line.get("unit_price", 100.0)``
``d["<money>"] = <numeric literal>``         ``product["base_price"] = 100.0``
``<money> = ... or <numeric literal>``       ``cost = product.get("x") or 100.0``
===========================================  ======================================

**Narrowness is the design, not a shortcoming.** This ledger already records a
verifier being widened until it parsed prose, and a gate that flags fifty sites
gets switched off. A missed instance is cheaper than a disabled gate. Do **not**
add a fourth pattern to catch something clever — report it instead.

Known gaps (deliberate, do not "fix" by widening)
-------------------------------------------------
* **Named constants.** ``to_quote.py``'s ``_UNPRICED = Decimal("0.00")``
  (**D48**) is invisible here: it is a *number wearing the name of an absence*,
  and it reads correct at every call site — which is precisely what makes that
  the most deceptive form of this defect. Catching it requires resolving
  constants across modules, which is the widening that gets a gate switched
  off. D48 is a separate wave.
* **Money names not on the list**, e.g. ``expected_amount`` — the list is
  matched exactly, not by substring, because substrings pull in
  ``discount_amount``-style accumulators and config keys.
* **Non-literal defaults** (``line.get("price", fallback)``), computed
  fabrications, and ``**{"price": 0}`` splats.
* **Accumulator initialisation** (``total = 0.0``), **function-signature
  defaults**, **comparisons**, **test files** and **config defaults** are all
  explicitly *out of scope* — flagging them is how the list grows past what
  anyone reads.

Allowlist
---------
``ALLOWLISTED_SITES`` maps a *stable* key (``path::field::literal`` — line
numbers rot on the first unrelated edit) to a written reason. It carries a
**reverse assertion**: an entry that no longer matches must be *removed*, not
left as a permanent exemption. That assertion is what makes this a ratchet
rather than a snapshot of one afternoon.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import NamedTuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_ROOT = REPO_ROOT / "nce"

# Exact field names. NOT substrings — see "Known gaps".
MONEY_FIELDS: frozenset[str] = frozenset(
    {
        "unit_price",
        "total_price",
        "base_price",
        "base_cost",
        "line_total",
        "bid_price",
        "price",
        "cost",
        "amount",
        "total_price_nok",
        "sell_price",
        "sellPrice",
        "suggested_unit_price",
    }
)

# key -> written reason. See the reverse assertion below.
ALLOWLISTED_SITES: dict[str, str] = {}


class Hit(NamedTuple):
    path: str
    line: int
    field: str
    literal: str
    pattern: str

    @property
    def key(self) -> str:
        return f"{self.path}::{self.field}::{self.literal}"

    def __str__(self) -> str:  # pragma: no cover - failure formatting only
        return f"{self.path}:{self.line}  [{self.pattern}]  {self.field} <- {self.literal}  (key: {self.key})"


def _numeric_literal(node: ast.AST) -> str | None:
    """``repr`` of a bare numeric literal, else None. ``True``/``False`` are ints
    in Python but are not money, so they are excluded."""
    if isinstance(node, ast.Constant) and not isinstance(node.value, bool):
        if isinstance(node.value, (int, float)):
            return repr(node.value)
    return None


def _target_field(node: ast.AST) -> str | None:
    """The money field name a store-target names, if any."""
    if isinstance(node, ast.Subscript):
        key = node.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            return key.value if key.value in MONEY_FIELDS else None
        return None
    if isinstance(node, ast.Name):
        return node.id if node.id in MONEY_FIELDS else None
    if isinstance(node, ast.Attribute):
        return node.attr if node.attr in MONEY_FIELDS else None
    return None


def scan_source(source: str, path: str) -> list[Hit]:
    """The whole gate, on one file's text. Exposed so the patterns can be
    tested directly (guard-the-guard) instead of only through the tree."""
    hits: list[Hit] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        # 1. dict.get("<money>", <numeric literal>)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and len(node.args) == 2
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and node.args[0].value in MONEY_FIELDS
        ):
            literal = _numeric_literal(node.args[1])
            if literal is not None:
                hits.append(Hit(path, node.lineno, node.args[0].value, literal, "get-default"))

        # 2/3. assignment of a numeric literal, or of an ``or``-chain ending in one
        targets: list[ast.AST] = []
        value: ast.AST | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if value is None:
            continue
        or_tail = (
            value.values[-1]
            if isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or) and value.values
            else None
        )
        for target in targets:
            field = _target_field(target)
            if field is None:
                continue
            # A bare ``total = 0.0`` accumulator is NOT flagged: only a subscript
            # store (direct fabrication into a payload) or an ``or``-fallback is.
            if isinstance(target, ast.Subscript):
                literal = _numeric_literal(value)
                if literal is not None:
                    hits.append(Hit(path, node.lineno, field, literal, "subscript-store"))
            if or_tail is not None:
                literal = _numeric_literal(or_tail)
                if literal is not None:
                    hits.append(Hit(path, node.lineno, field, literal, "or-fallback"))
    return hits


def scan_tree() -> list[Hit]:
    hits: list[Hit] = []
    for path in sorted(SCAN_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if path.name.startswith("test_") or "/tests/" in f"/{rel}":
            continue
        hits.extend(scan_source(path.read_text(encoding="utf-8"), rel))
    return sorted(hits)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_no_fabricated_money_default_outside_the_allowlist() -> None:
    unexcused = [h for h in scan_tree() if h.key not in ALLOWLISTED_SITES]
    assert not unexcused, (
        "Fabricated money default(s) — a missing money value must fail or carry the "
        "absence, never become a plausible number:\n  "
        + "\n  ".join(str(h) for h in unexcused)
        + "\n\nFix the site, or add its key to ALLOWLISTED_SITES with a written reason."
    )


def test_an_allowlisted_site_that_no_longer_matches_is_removed() -> None:
    """The reverse assertion. Without it the allowlist rots into a permanent
    exemption: a site gets fixed, the entry lingers, and the next fabricated
    default at the same key is excused by a reason that no longer applies."""
    live_keys = {h.key for h in scan_tree()}
    stale = sorted(k for k in ALLOWLISTED_SITES if k not in live_keys)
    assert not stale, (
        "ALLOWLISTED_SITES names site(s) that no longer match the scan. Delete them — "
        "an exemption outlives its reason:\n  " + "\n  ".join(stale)
    )


def test_every_allowlist_entry_carries_a_reason() -> None:
    empty = sorted(k for k, reason in ALLOWLISTED_SITES.items() if not reason.strip())
    assert not empty, f"Allowlisted without a written reason: {empty}"


# ---------------------------------------------------------------------------
# Guard-the-guard: the patterns must actually fire, and must NOT fire on the
# shapes this gate deliberately leaves alone.
# ---------------------------------------------------------------------------

_POSITIVE = [
    ('x = line.get("unit_price", 100.0)', "get-default"),
    ('product["base_price"] = 100.0', "subscript-store"),
    ('cost = product.get("x") or 100.0', "or-fallback"),
    ('payload["line_total"] = 0', "subscript-store"),
    ('price = a() or b() or 0.0', "or-fallback"),
]


@pytest.mark.parametrize(("source", "pattern"), _POSITIVE)
def test_pattern_fires(source: str, pattern: str) -> None:
    hits = scan_source(source, "synthetic.py")
    assert [h.pattern for h in hits] == [pattern], f"{source!r} -> {hits}"


_NEGATIVE = [
    "total = 0.0",  # accumulator initialisation
    "def f(unit_price: float = 0.0): pass",  # signature default
    'if line.get("unit_price") == 100.0: pass',  # comparison
    'x = line.get("unit_price")',  # carries the absence — the fix, not the defect
    'x = line.get("quantity", 1)',  # not a money field
    'x = line.get("expected_amount", 0)',  # exact names only (known gap)
    'x = line.get("unit_price", fallback)',  # non-literal default (known gap)
    '_UNPRICED = Decimal("0.00")',  # D48 — named constant (known gap)
    'x = line.get("unit_price", None)',  # explicit absence
    'flag = row.get("price", True)',  # bool is not money
]


@pytest.mark.parametrize("source", _NEGATIVE)
def test_pattern_does_not_fire(source: str) -> None:
    assert scan_source(source, "synthetic.py") == []
