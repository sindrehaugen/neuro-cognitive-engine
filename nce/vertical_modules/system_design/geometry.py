"""
nce/vertical_modules/system_design/geometry.py
==============================================
Domain core for System Design **canvas geometry** and the per-DESIGN
**optimistic-concurrency token** (M6.W14).

Both live in ``system_design_geometry`` (migration 060), which carries **two
key grains in one table** — see that migration's header.  Restated here because
this is the module that has to keep them apart:

* a **geometry row** is keyed by a *node* label (``DEVICE:`` / ``PORT:`` /
  ``RACK:`` / ``CABLE:`` / ``FL:``) and carries ``x``/``y``,
  ``rack_position``/``rack_face``, ``cable_length_m``/``cable_type`` and
  ``meta``.  Its ``version`` is always ``NULL``.
* the **design version row** is keyed by the *design* label
  (``DESIGN:<DESIGN_ID>``), carries ``version`` and no geometry.

``version IS NOT NULL`` is the discriminator.  Every query below states which
grain it addresses, and none of them can return the other one: the geometry
reads exclude the design label by asking for node labels explicitly, and the
version read asks for the design label explicitly.

Units and axes (Rev 2 §4, NORMATIVE)
------------------------------------
``x``/``y`` are **canvas grid units**, origin **top-left**, **y-down**.  This
module converts nothing — exporters convert.  Room dimensions are **not**
``x``/``y``: they live in ``meta`` under ``copper.room.w`` / ``copper.room.d``
/ ``copper.room.h``, in **meters**.

Naming is contractual
---------------------
``rack_position`` and ``rack_face`` carry NetBox's vocabulary, which Copper
follows as a binding ADR.  They are not to be renamed, here or in the DDL.

Namespace scoping (owner-pool invariant)
----------------------------------------
Every query in this module carries an explicit ``namespace_id = $n::uuid``
predicate.  The pools that serve requests are **owner pools and bypass
``FORCE ROW LEVEL SECURITY``**, so RLS proves nothing on the connection this
code actually runs on.  These predicates *are* the tenant boundary:

* :func:`fetch_geometry_by_labels` — the read boundary for geometry rows;
* :func:`fetch_design_version` — the read boundary for the version row;
* :func:`upsert_node_geometry` — the write boundary (it can otherwise upsert
  onto another tenant's row and silently overwrite their canvas);
* :func:`bump_design_version` — the compare-and-swap boundary, in **both** of
  its statements.

Each is individually gated by the mutation table in
``tests/test_system_design_geometry.py``.

Label construction is IMPORTED, never copied
--------------------------------------------
``device_label`` / ``port_label`` / ``rack_label`` / ``cable_label`` come from
``devices.py`` and ``_fl_label`` from ``graph.py``.  A second copy of a label
formula is how the write surface and the read surface drift apart while both
suites stay green; this module owns no label formula of its own except the
DESIGN label, which ``read.py``, ``graph.py`` and ``devices.py`` each already
spell inline as ``f"DESIGN:{design_id.upper()}"``.

Design invariants (uncle-bob-craft)
-----------------------------------
- Dependencies point **inward**: no web / HTTP / admin / MCP imports.
- This module never opens a session or a transaction.  It takes the caller's
  ``conn``, so the version increment lands in the *write's own* transaction —
  a read-modify-write across two transactions is a lost update and defeats the
  whole point of the token.
"""

from __future__ import annotations

import json
import logging
import sys
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.vertical_modules.system_design.devices import (
    cable_label,
    device_label,
    port_label,
    rack_label,
)
from nce.vertical_modules.system_design.graph import _fl_label

log = logging.getLogger("nce.vertical_modules.system_design.geometry")

__all__ = [
    "GEOMETRY_COLUMNS",
    "MAX_FINITE_MAGNITUDE",
    "MAX_RACK_POSITION",
    "RACK_FACES",
    "RACK_POSITION_STEP",
    "VersionConflictError",
    "reject_unknown_members",
    "validate_geometry",
    "bump_design_version",
    "design_version_label",
    "do_author_geometry",
    "do_author_functional_location_geometry",
    "fetch_design_version",
    "fetch_geometry_by_labels",
    "upsert_node_geometry",
]

#: The geometry projection, in contract order.  ``version`` is deliberately NOT
#: here: it belongs to the other key grain and is read by
#: :func:`fetch_design_version`.
GEOMETRY_COLUMNS: tuple[str, ...] = (
    "x",
    "y",
    "rack_position",
    "rack_face",
    "cable_length_m",
    "cable_type",
    "meta",
)

#: The version a design has before any authoring write has been recorded
#: against it.  A caller may pass this as ``expected_version`` to mean "I
#: expect this design to be untouched" — which is why the read surface returns
#: ``0`` rather than ``null`` for a design with no version row.
INITIAL_VERSION: int = 0

#: NetBox's ``face`` vocabulary — the same two values migration 060's
#: ``system_design_geometry_rack_face_check`` enforces.  Declared here so a bad
#: value is a 422/``invalid_arguments`` at the write boundary instead of a
#: ``CheckViolationError`` escaping as a 500 / JSON-RPC ``-32603``.  The DDL
#: CHECK stays: this is the client-facing guard, that one is the invariant.
RACK_FACES: frozenset[str] = frozenset({"front", "rear"})

#: The largest ``rack_position`` this engine accepts.
#:
#: ``NUMERIC(4,1)`` can hold up to 999.9, but 999.9 is **not a multiple of
#: 0.5**, so :data:`RACK_POSITION_STEP` refuses it anyway — which made an
#: earlier ``Decimal("999.9")`` here a bound that could never bind, while the
#: surrounding comments advertised 999.9 as a legal value.  999.5 is the
#: largest half-U position the column can hold, so it is both the real limit
#: and a value a caller may actually send.
MAX_RACK_POSITION: Decimal = Decimal("999.5")

#: Largest magnitude any geometry number may carry, as an exact ``Decimal``.
#:
#: 🔴 This is a **correctness** bound, not a tidiness one.  Postgres ``NUMERIC``
#: happily stores ``10**400``; the read path then does ``float(Decimal)``, which
#: does NOT raise for an over-large ``Decimal`` — it returns ``inf`` — and
#: ``JSONResponse.render`` (``allow_nan=False``) raises on that, poisoning the
#: whole design's topology response exactly the way a stored ``NaN`` did.  So a
#: value that cannot round-trip through an IEEE double is refused at the write
#: boundary for the same reason ``NaN`` is.
#:
#: Built from ``sys.float_info.max`` rather than a transcribed literal, and via
#: ``Decimal(float)`` — which is exact — so the bound is the platform's real
#: double limit and cannot drift from it.
MAX_FINITE_MAGNITUDE: Decimal = Decimal(sys.float_info.max)

#: A rack unit is a HALF-U grid.  ``NUMERIC(4,1)`` would silently round 1.27 to
#: 1.3 — not a half-U either, and the caller is never told its device moved.
#: Rejecting instead of rounding is what makes the column comment's "half-U
#: granularity" claim true rather than aspirational.  Legal values are
#: therefore 0.0, 0.5, 1.0 … up to :data:`MAX_RACK_POSITION` (999.5).
RACK_POSITION_STEP: Decimal = Decimal("0.5")

#: The largest value ``$4::bigint`` can carry.  Past it asyncpg raises
#: ``DataError`` — again not a ``ValueError``, again a 500.
BIGINT_MAX: int = 2**63 - 1

#: Geometry members that must be a finite real number when supplied.
_NUMERIC_MEMBERS: tuple[str, ...] = ("x", "y", "rack_position", "cable_length_m")

#: Members whose value is a caller-owned JSON document rather than a scalar.
_DOCUMENT_MEMBERS: tuple[str, ...] = ("meta",)


class VersionConflictError(Exception):
    """Raised when ``expected_version`` does not match the design's version.

    Its own class — deliberately **not** a ``ValueError`` — so that neither
    ``@mcp_handler``'s generic "Invalid parameters" branch nor the REST routes'
    ``except ValueError`` branch can swallow it and render it as a malformed
    argument.  "You are behind, re-read" and "your argument is malformed" are
    different facts and a caller has to be able to act on them differently.

    Attributes:
        design_id: The design whose version did not match.
        expected: The token the caller supplied.
        actual: The design's real version at the moment of the check, or
            ``None`` when the design has no version row and one could not be
            created.
    """

    reason: str = "version_conflict"

    def __init__(self, design_id: str, expected: int, actual: int | None) -> None:
        self.design_id = design_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"expected_version {expected} does not match the current version "
            f"{actual!r} for design {design_id!r}: re-read the topology and retry"
        )


def design_version_label(design_id: str) -> str:
    """Canonical DESIGN label — the key of the design version row.

    Identical to the formula ``graph.py``, ``devices.py`` and ``read.py`` each
    spell inline; kept here so this module has one name for it.
    """
    return f"DESIGN:{design_id.upper()}"


def _json_native(value: Any) -> Any:
    """Coerce a driver-native scalar into a JSON-native one.

    The ``NUMERIC`` geometry columns arrive as :class:`decimal.Decimal`, which
    neither ``json.dumps`` nor Starlette's ``JSONResponse`` can encode.  Doing
    this **here, in the core** — rather than separately in each adapter — is
    what makes the MCP tool and the REST route return the same JSON type for
    the same field.  ``read.py`` converts its capability NUMERICs for the same
    reason; the two conversions are deliberately identical.
    """
    if isinstance(value, Decimal):
        return float(value)
    return value


def _decode_meta(raw: Any) -> Any:
    """Return the ``meta`` JSONB column as the value that was stored.

    asyncpg hands JSONB back as ``str`` unless a codec is registered, and none
    is registered on this pool.  A malformed blob is returned as-is rather than
    "repaired": silently rewriting a tenant's stored value would be worse than
    handing it back unchanged.  Mirrors ``read.py``'s ``_decode_extra``.
    """
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw
    return raw


def _finite_decimal(member: str, value: Any) -> Decimal:
    """Return *value* as a finite :class:`~decimal.Decimal`, or raise.

    🔴 **Fail-closed on non-finite numerics.**  PostgreSQL ``NUMERIC`` accepts
    ``NaN`` and ``±Infinity``, Python's ``json.loads`` accepts the bare
    ``NaN``/``Infinity`` tokens **by default** (which is what
    ``Request.json()`` calls), and :func:`_json_native`'s ``float(Decimal)``
    hands them straight back on read.  Starlette's ``JSONResponse.render`` sets
    ``allow_nan=False``, so one poisoned node makes the WHOLE topology response
    raise — for every reader of that design, permanently, because no delete
    path exists and the value can only be overwritten by re-authoring that
    exact node.  ``json.dumps`` without ``allow_nan=False`` is no better: it
    emits bare ``NaN``, which is not RFC 8259 and which Copper's ``JSON.parse``
    rejects.

    This guard enforces the same RULE as ``nce/admin_handlers/_shared.py``'s
    ``_neutralise_non_finite`` — "no non-finite number reaches the JSON
    encoder" — without importing it: this module's design invariant is
    *"dependencies point inward — no web / HTTP / admin / MCP imports"*, and
    reaching into ``admin_handlers`` for a numeric predicate would break it.
    ``_shared.py`` *neutralises* on the way out because by then the value is
    already stored; here, at the write boundary, it is **refused**, because a
    stored ``NaN`` is unrecoverable.

    🔴 It does **not** copy that function's ``math.isfinite`` spelling, and the
    difference is the point.  ``math.isfinite`` coerces via ``__float__``, so a
    Python ``int`` too large for a double raises ``OverflowError`` — an
    ``ArithmeticError``, not a ``ValueError`` — which escapes both surfaces'
    ``except ValueError`` into a 500.  A round-2 draft did copy the spelling
    and reinstated exactly the escape it was written to close.  The check below
    therefore goes through ``Decimal``, which is total over every value that
    reaches it.  ``_shared.py`` is safe with ``math.isfinite`` only because it
    guards it with ``isinstance(value, float)`` first.

    ``bool`` is refused even though it is an ``int`` subclass in Python — the
    same reason :func:`expected_version_of` refuses it: ``{"x": true}`` on the
    wire otherwise coerces to the coordinate ``1``, silently placing a device
    at a position no one asked for.

    ``str`` is refused too, including a numeric-looking ``"12.5"``.  This is a
    decision, not an oversight: accepting both ``12.5`` and ``"12.5"`` lets two
    clients store the same coordinate under two wire types, and every consumer
    then has to handle both forever.  The wire type for a number is a number.

    Raises:
        ValueError: mapped to ``McpError(-32602, reason=invalid_arguments)`` by
            ``@mcp_handler`` and to HTTP 422 by the REST routes' ``except
            ValueError`` — the same shape ``expected_version`` already uses.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        # ``bool`` is listed FIRST and explicitly. Without it a ``true`` would
        # still be refused — ``Decimal(str(True))`` raises ``InvalidOperation``
        # — but with the wrong message: "is not a representable number" instead
        # of "must be a number". That message is the 422 body the caller reads,
        # and "you sent a boolean where a number goes" is actionable where
        # "unrepresentable" is not. The mutation table pins the message for
        # exactly this reason; without it this line is decorative.
        raise ValueError(
            f"geometry.{member} must be a number when supplied, got {type(value).__name__}"
        )

    # 🔴 CONVERT FIRST, AND CONVERT WITHOUT COERCING TO FLOAT.
    #
    # A previous round used ``math.isfinite(value)`` here as a literal mirror of
    # ``_shared.py``'s guard. That was itself the defect: ``math.isfinite``
    # coerces through ``__float__``, and a Python ``int`` too large for a double
    # raises ``OverflowError`` — an ``ArithmeticError``, NOT a ``ValueError`` —
    # so it escaped both surfaces' ``except ValueError`` into HTTP 500 /
    # JSON-RPC ``-32603``, reinstating the very escape the guard was written to
    # close. ``json.loads`` (what ``Request.json()`` calls) yields
    # arbitrary-precision ints, so it was reachable straight off the wire.
    #
    # ``Decimal(str(value))`` is TOTAL for every value that reaches this line:
    # arbitrary-precision ints included, and ``str`` of a non-finite float gives
    # ``'nan'``/``'inf'``, which ``Decimal`` parses rather than rejecting. The
    # rule is still ``_shared.py``'s — "no non-finite numbers" — expressed
    # without the coercion that made the rule unenforceable.
    try:
        as_decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:  # pragma: no cover - defensive
        raise ValueError(f"geometry.{member} is not a representable number") from exc

    # NaN / ±Infinity, whichever type they arrived as. Checked BEFORE the
    # magnitude comparison, because ordering a NaN is meaningless.
    if not as_decimal.is_finite():
        raise ValueError(f"geometry.{member} must be a finite number, got {value!r}")

    # Finite, but too large to survive the round trip. The read path converts
    # NUMERIC back with ``float(Decimal)``, which returns ``inf`` for an
    # over-large Decimal instead of raising — and an ``inf`` in the response is
    # the same poisoning a stored ``NaN`` used to cause. Refusing here is what
    # keeps "stored" and "serialisable" the same set.
    #
    # 🔴 ``copy_abs()``, NOT ``abs()``. ``abs()`` on a Decimal is an ARITHMETIC
    # operation: it rounds its result to the active context precision, 28
    # significant digits by default. ``MAX_FINITE_MAGNITUDE`` has 309, and
    # ``Decimal(str(value))`` above builds the operand exactly (also 309), so
    # ``abs()`` silently truncated the very value it was about to compare —
    # ``abs(M) < M`` is True for M = MAX_FINITE_MAGNITUDE. That accepted every
    # value in a window roughly 1.8e280 wide ABOVE the true maximum, which the
    # DDL's exact bound then refused with a CheckViolationError: not a
    # ValueError, so HTTP 500 / -32603. Reachable from the wire as a 309-digit
    # JSON integer. ``copy_abs()`` is defined as a sign-manipulation copy and
    # performs no rounding, so the two bounds agree exactly.
    if as_decimal.copy_abs() > MAX_FINITE_MAGNITUDE:
        raise ValueError(
            f"geometry.{member} magnitude exceeds {MAX_FINITE_MAGNITUDE:.6e}, the "
            "largest value that survives the JSON round trip; it would be read "
            "back as Infinity and make the whole design unserialisable"
        )
    return as_decimal


def reject_unknown_members(geometry: dict[str, Any]) -> None:
    """Refuse a geometry object carrying a member NCE does not store.

    A caller that sends ``rackPosition`` (or any other near-miss) is told,
    instead of being handed a 200 for a value NCE discarded; ``meta`` is the
    documented escape hatch for anything outside :data:`GEOMETRY_COLUMNS`.

    Extracted so it can run in **two** places that are not interchangeable:
    :func:`validate_geometry`, at the write boundary, and :func:`_geometry_of`,
    on the **raw** payload before ``None`` members are stripped.  Without the
    second call ``{"rackPosition": null}`` was stripped to ``{}``, read as
    absence, and answered 200 with the key silently discarded — precisely what
    this refusal exists to prevent.

    Raises:
        ValueError: any member outside :data:`GEOMETRY_COLUMNS`.
    """
    unknown = sorted(set(geometry) - set(GEOMETRY_COLUMNS))
    if unknown:
        raise ValueError(
            "unknown geometry member(s): "
            + ", ".join(unknown)
            + f"; allowed: {', '.join(GEOMETRY_COLUMNS)} "
            "(put anything else inside meta)"
        )


def validate_geometry(geometry: dict[str, Any]) -> None:
    """Validate one geometry payload **before** it reaches the database.

    Every failure this function *raises* is a ``ValueError``, which both
    surfaces already map to their malformed-argument shape (HTTP 422 /
    JSON-RPC ``-32602`` with ``reason: invalid_arguments``).  That is a claim
    about what this function raises, not a claim that nothing else on the write
    path can raise something else — the round-3 rejection was exactly such an
    over-claim, when ``math.isfinite`` here raised ``OverflowError`` while this
    sentence said otherwise.

    The coercions reached from here have each been checked for that class of
    escape, and the audit is stated as a list of what was checked rather than
    as a blanket assurance — because the round-3 remediation ALSO shipped one:
    ``abs()`` on a ``Decimal`` rounds to the context precision, so the
    magnitude bound silently truncated its own operand and let values through
    that the DDL then refused with a ``CheckViolationError`` (a 500). What has
    been checked: ``Decimal(str(...))`` is total for the values that reach it;
    ``json.dumps(..., allow_nan=False)`` on ``meta`` raises ``ValueError`` and
    is caught; ``float()`` is not called on the write path at all; and every
    magnitude comparison uses ``copy_abs()``, which does not round, so the
    application bound and the DDL bound agree exactly.  Without it, asyncpg and PostgreSQL raise
    ``CheckViolationError`` / ``DataError`` / ``NumericValueOutOfRangeError``
    — **none of which is a ``ValueError``** — so they sail past both surfaces'
    ``except ValueError`` branches into ``except Exception`` → HTTP 500 and
    ``-32603 Internal error``.  An internal-error code tells a client "retry
    later"; a malformed ``rack_face`` is permanently unretryable, and
    ``rack_face`` is the field the contract singles out as NetBox-binding.

    Unknown members are refused rather than silently dropped.  A caller that
    sends ``rackPosition`` (or any other near-miss) is told, instead of being
    handed a 200 for a value NCE discarded; ``meta`` is the documented escape
    hatch for anything not in :data:`GEOMETRY_COLUMNS`.

    Raises:
        ValueError: on any malformed member.
    """
    reject_unknown_members(geometry)

    for member in _NUMERIC_MEMBERS:
        if geometry.get(member) is None:
            continue
        as_decimal = _finite_decimal(member, geometry[member])
        if member != "rack_position":
            continue
        # copy_abs() here too. MAX_RACK_POSITION has four digits so abs()'s
        # rounding is a no-op today and this line is NOT a live bug — it is
        # spelled this way so the next person who widens the bound does not
        # reintroduce the one above.
        if as_decimal.copy_abs() > MAX_RACK_POSITION:
            raise ValueError(
                f"geometry.rack_position must be within ±{MAX_RACK_POSITION} "
                f"(the NUMERIC(4,1) column's range), got {geometry[member]!r}"
            )
        if as_decimal % RACK_POSITION_STEP != 0:
            raise ValueError(
                "geometry.rack_position must be a whole or half rack unit "
                f"(a multiple of {RACK_POSITION_STEP}), got {geometry[member]!r}; "
                "NUMERIC(4,1) would otherwise round it silently and place the "
                "device somewhere the caller did not ask for"
            )

    face = geometry.get("rack_face")
    if face is not None and (not isinstance(face, str) or face not in RACK_FACES):
        raise ValueError(
            "geometry.rack_face must be one of "
            + ", ".join(sorted(RACK_FACES))
            + f" (NetBox's vocabulary, contractual), got {face!r}"
        )

    cable_type = geometry.get("cable_type")
    if cable_type is not None and not isinstance(cable_type, str):
        raise ValueError(
            f"geometry.cable_type must be a string when supplied, got {type(cable_type).__name__}"
        )

    meta = geometry.get("meta")
    if meta is not None:
        if not isinstance(meta, dict):
            raise ValueError(
                f"geometry.meta must be an object when supplied, got {type(meta).__name__}"
            )
        # 🔴 ``meta`` is the DOCUMENTED escape hatch — this function's own
        # docstring tells callers to put anything else in it — so it is a
        # sanctioned wire path and needs the same treatment as the scalars.
        # ``json.dumps`` defaults to ``allow_nan=True`` and emits the bare
        # ``NaN``/``Infinity`` tokens, which are not RFC 8259; ``$9::jsonb``
        # then raises ``InvalidTextRepresentationError`` — not a ``ValueError``
        # — and the caller gets a 500 for a permanently unretryable request.
        # Serialising HERE with ``allow_nan=False`` turns that into the same
        # 422 every other malformed member gets, and does it once so
        # :func:`upsert_node_geometry` can reuse the result.
        #
        # Unlike the scalars this is NOT a poisoning path: jsonb rejects
        # NaN/Infinity outright, so nothing non-finite can ever be stored, and
        # a huge literal like ``1e400`` comes back out of jsonb as an exact
        # integer that ``json.loads`` returns as ``int``, never as a float.
        # It is a 500-shape defect only.
        try:
            json.dumps(meta, allow_nan=False)
        except ValueError as exc:
            raise ValueError(
                f"geometry.meta contains a value JSON cannot represent ({exc}); "
                "NaN and Infinity are not valid JSON and jsonb will not store them"
            ) from exc
        except (TypeError, RecursionError) as exc:
            # A non-JSON-serialisable object, or a self-referential document.
            # Both are malformed arguments, not server faults.
            raise ValueError(f"geometry.meta is not JSON-serialisable: {exc}") from exc


# ---------------------------------------------------------------------------
# Reads — grain 1 (geometry rows) and grain 2 (the version row).
# ---------------------------------------------------------------------------


async def fetch_geometry_by_labels(
    conn: Any,
    ns_uuid: Any,
    labels: list[str],
) -> dict[str, dict[str, Any]]:
    """Return ``{node_label: geometry}`` for *labels*, pinned to ``ns_uuid``.

    **Grain 1 only, structurally.**  ``version IS NULL`` is in the predicate,
    not merely implied by the caller passing node labels.  ``read.py`` hands
    this function the design's whole scope label set, and the DESIGN label is
    in it, so without that predicate the design version row comes back as a
    geometry entry of all-NULLs — its ``version`` column is not projected, so
    no token leaks, but a node the canvas never placed appears as though it had
    been placed, and the two key grains are visible to a caller that was
    promised one.  Caught by
    ``tests/test_system_design_author_surface.py`` before it shipped.

    The ``namespace_id`` predicate is the tenant boundary — see the module
    docstring.  Drop it and one tenant reads another tenant's canvas layout.
    """
    if not labels:
        return {}
    # The interpolated column list is a module constant (GEOMETRY_COLUMNS),
    # never caller input; the two values are bound parameters.
    rows = await conn.fetch(
        f"""
        SELECT node_label, {", ".join(GEOMETRY_COLUMNS)}
        FROM system_design_geometry
        WHERE node_label = ANY($1::text[])
          AND namespace_id = $2::uuid
          AND version IS NULL
        """,
        labels,
        ns_uuid,
    )
    by_label: dict[str, dict[str, Any]] = {}
    for row in rows:
        geom = {key: _json_native(value) for key, value in row.items() if key != "node_label"}
        geom["meta"] = _decode_meta(row["meta"])
        by_label[row["node_label"]] = geom
    return by_label


async def fetch_design_version(
    conn: Any,
    ns_uuid: Any,
    design_label: str,
) -> int:
    """Return the design's optimistic-concurrency token, pinned to ``ns_uuid``.

    **Grain 2 only.**  ``version IS NOT NULL`` is asserted in the predicate, so
    a geometry row that somehow shared the design's label could not be mistaken
    for the version row.

    Returns :data:`INITIAL_VERSION` (``0``) when no version row exists — for a
    design that has never been authored, and for a design that does not exist
    in this namespace at all.  ``0`` rather than ``None`` because ``0`` is a
    token a caller can *use*: it is exactly the value that makes the first
    checked write succeed and a second concurrent one fail.

    The ``namespace_id`` predicate is the tenant boundary — without it a caller
    reads a colliding design label's version from another tenant and every
    subsequent compare-and-swap is meaningless.
    """
    row = await conn.fetchval(
        """
        SELECT version
        FROM system_design_geometry
        WHERE node_label = $1
          AND namespace_id = $2::uuid
          AND version IS NOT NULL
        """,
        design_label,
        ns_uuid,
    )
    return INITIAL_VERSION if row is None else int(row)


# ---------------------------------------------------------------------------
# Writes — grain 1 (geometry upsert).
# ---------------------------------------------------------------------------


async def upsert_node_geometry(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    node_label: str,
    geometry: dict[str, Any],
) -> None:
    """Write one **geometry row** (grain 1) for *node_label*.

    Only keys present in *geometry* are written; absent keys keep their
    existing DB values via ``COALESCE`` on the conflict branch — the same
    partial-update contract ``devices.py``'s ``_upsert_capability`` gives the
    capability row, so a canvas that moves a device without touching its rack
    face does not blank the rack face.

    ``meta`` is the exception and is replaced wholesale, exactly as
    ``capability.extra`` is: it is a document the caller owns, and merging two
    tenants' notions of a document is a decision NCE has not been given.

    ``version`` is never written here.  This function cannot create or touch
    the design version row: it writes ``NULL`` into ``version`` on insert and
    does not mention the column on update.

    The ``namespace_id`` predicate is the tenant boundary.  It is in the
    ``INSERT`` values and, load-bearingly, in the ``ON CONFLICT`` target —
    ``(namespace_id, node_label)`` — so a colliding node label in another
    tenant is a different row, not the same one.

    Every member is validated by :func:`validate_geometry` **before** the
    statement runs.  The check lives here, at the write boundary, rather than
    in :func:`_geometry_of` where the payload is walked, precisely so a caller
    that reaches this function directly — a test, a future core, a repair
    script — cannot route around it.  One place, not two.

    Raises:
        ValueError: any malformed member (see :func:`validate_geometry`).
    """
    validate_geometry(geometry)

    meta = geometry.get("meta")
    meta_json: str | None = None
    if meta is not None:
        # 🔴 Plain json.dumps, allow_nan left at its default, DELIBERATELY.
        #
        # validate_geometry() — the first statement of this function — has
        # already proved this document serialises under allow_nan=False. A
        # second allow_nan=False here would be defence in depth that cannot be
        # tested: each of the two guards masks the other, so BOTH mutate GREEN
        # and neither can be shown load-bearing. That is exactly the trap two
        # earlier finiteness checks in this module fell into. One guard, in the
        # validator, where the error message can name the member.
        #
        # What keeps this honest is that dropping the validate_geometry() call
        # above is itself a RED mutation row, so the guard cannot silently
        # disappear.
        meta_json = json.dumps(meta)

    await conn.execute(
        """
        INSERT INTO system_design_geometry (
            namespace_id, node_label,
            x, y, rack_position, rack_face,
            cable_length_m, cable_type,
            meta
        )
        VALUES (
            $1::uuid, $2,
            $3, $4, $5, $6,
            $7, $8,
            COALESCE($9::jsonb, '{}'::jsonb)
        )
        ON CONFLICT (namespace_id, node_label) DO UPDATE
            SET x              = COALESCE(EXCLUDED.x,              system_design_geometry.x),
                y              = COALESCE(EXCLUDED.y,              system_design_geometry.y),
                rack_position  = COALESCE(EXCLUDED.rack_position,  system_design_geometry.rack_position),
                rack_face      = COALESCE(EXCLUDED.rack_face,      system_design_geometry.rack_face),
                cable_length_m = COALESCE(EXCLUDED.cable_length_m, system_design_geometry.cable_length_m),
                cable_type     = COALESCE(EXCLUDED.cable_type,     system_design_geometry.cable_type),
                meta           = COALESCE($9::jsonb, system_design_geometry.meta),
                updated_at     = NOW()
        """,
        str(ns_uuid),
        node_label,
        geometry.get("x"),
        geometry.get("y"),
        geometry.get("rack_position"),
        geometry.get("rack_face"),
        geometry.get("cable_length_m"),
        geometry.get("cable_type"),
        meta_json,
    )


# ---------------------------------------------------------------------------
# Writes — grain 2 (the compare-and-swap on the version row).
# ---------------------------------------------------------------------------


async def bump_design_version(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    design_id: str,
    expected_version: int | None,
) -> int:
    """Compare-and-swap the design's version, returning the NEW version.

    **This must be called on the same ``conn``, inside the same transaction, as
    the graph writes it guards.**  A read-modify-write split across two
    transactions is a lost update, and a version that is bumped in its own
    transaction says nothing about whether the write it claims to describe
    landed.  This function therefore never opens a transaction of its own.

    Semantics
    ---------
    * ``expected_version is None`` — no check.  The write proceeds
      (last-writer-wins, unchanged from before W14) **and the version is still
      incremented**, because a token that untracked writes do not advance is a
      token that cannot detect them.

    🔴 **The increment covers writes that go through the two authoring
    adapters, and only those.**  Three other modules under ``system_design/``
    write ``kg_nodes``/``kg_edges`` for a design without passing through them
    and therefore never move the token: ``from_quote.py`` (:372, :387),
    ``to_quote.py`` (:245) and ``netbox_bridge.py`` (:611).  All three are
    unwired today — zero non-test callers — so this is latent rather than
    exploitable, but it is NOT a hypothetical: ``read.py``'s
    ``_fetch_edges_within`` filters on ``subject_label`` only, so
    ``to_quote.py``'s ``DESIGN -[becomes]-> QUOTE`` edge really does appear in
    ``do_get_topology``'s ``edges`` while the version stands still.  Bumping in
    those three is a separate wave; do not read the sentence above as covering
    them.
    * ``expected_version == current`` — the write proceeds and the version
      becomes ``current + 1``.
    * anything else — :class:`VersionConflictError`.  The caller's transaction
      is expected to roll back, so no graph write survives a conflict.

    Concurrency
    -----------
    Two writers holding the same token cannot both win.  The ``UPDATE`` takes a
    row lock; the loser blocks on it until the winner's transaction ends, then
    re-evaluates its qualification against the **committed** row (READ
    COMMITTED re-check), sees the incremented version, matches nothing, and
    raises.  This is why the compare and the increment are one statement: a
    ``SELECT`` followed by an ``UPDATE`` would let both writers read the same
    value before either wrote.

    The seeding ``INSERT`` is a separate statement only because PostgreSQL has
    no way to make an ``ON CONFLICT DO UPDATE``'s *insert* branch conditional.
    It is ``DO NOTHING``, so it is a no-op whenever the row exists and cannot
    itself lose a race.

    Raises:
        VersionConflictError: ``expected_version`` did not match.
    """
    design_label = design_version_label(design_id)

    # 1. Seed the version row at INITIAL_VERSION if this design has never been
    #    written.  DO NOTHING: never disturbs an existing row, never resets a
    #    version.  The namespace_id is in the conflict target, so a colliding
    #    design label in another tenant is a different row.
    await conn.execute(
        """
        INSERT INTO system_design_geometry (namespace_id, node_label, version)
        VALUES ($1::uuid, $2, $3)
        ON CONFLICT (namespace_id, node_label) DO NOTHING
        """,
        str(ns_uuid),
        design_label,
        INITIAL_VERSION,
    )

    # 2. The compare-and-swap.  One statement, so the compare and the increment
    #    cannot be interleaved by a concurrent writer.
    #
    #    ``version IS NOT NULL`` is the GRAIN GUARD.  Without it,
    #    ``COALESCE(version, 0) + 1`` treats a GEOMETRY row (version NULL)
    #    sitting at the design label as a version row at 0 and silently
    #    converts it: the row's x/y survive but it vanishes from the
    #    geometry grain, which nothing would report.  With the guard the
    #    UPDATE matches nothing and the caller gets a loud
    #    VersionConflictError instead of silent grain corruption.
    #
    #    🔴 This is UNREACHABLE through the authoring surfaces today, and
    #    is defence-in-depth, not a live bug: every label builder is
    #    prefix-fixed (DEVICE:/PORT:/RACK:/CABLE:/FL:) and
    #    ``design_version_label`` is the only thing in the codebase that
    #    emits a DESIGN: label into this table.  It becomes load-bearing
    #    the day anything else writes here.  It IS reachable through this
    #    module's own public API, and is gated there — see
    #    tests/test_system_design_geometry.py.
    new_version = await conn.fetchval(
        """
        UPDATE system_design_geometry
           SET version    = COALESCE(version, $3) + 1,
               updated_at = NOW()
         WHERE namespace_id = $1::uuid
           AND node_label = $2
           AND version IS NOT NULL
           AND ($4::bigint IS NULL OR COALESCE(version, $3) = $4::bigint)
        RETURNING version
        """,
        str(ns_uuid),
        design_label,
        INITIAL_VERSION,
        expected_version,
    )

    if new_version is None:
        # No row matched.  Either the token is stale, or the seeding INSERT was
        # rolled back by a concurrent DO NOTHING race — both are "you are
        # behind, re-read".  Report the version the caller would see now.
        actual = await fetch_design_version(conn, ns_uuid, design_label)
        raise VersionConflictError(
            design_id=design_id,
            expected=int(expected_version) if expected_version is not None else INITIAL_VERSION,
            actual=actual,
        )

    return int(new_version)


# ---------------------------------------------------------------------------
# Composers — walk an authoring payload and write its geometry rows.
#
# These live in the core, not the adapter, precisely because they build labels.
# An adapter that built ``DEVICE:<ID>:<REF>`` for itself would be a second copy
# of devices.py's formula, and the two would drift.
# ---------------------------------------------------------------------------


def _geometry_of(payload: dict[str, Any], key: str = "geometry") -> dict[str, Any] | None:
    """Return the optional geometry dict on an authoring payload item.

    Absent, ``None``, ``{}`` **and an object whose every member is ``null``**
    all mean "no geometry supplied" and produce no row.

    The last of those is not pedantry.  ``{"x": null}`` used to survive the
    ``value or None`` check — a non-empty dict is truthy — and wrote exactly
    the all-NULL row ``read.py``'s contract prose says cannot exist, which
    breaks the "a node absent from the map has never been placed" distinction
    the read surface promises.  Filtering the ``None`` members here also means
    :func:`upsert_node_geometry` never sees a key whose only effect would be to
    ``COALESCE`` onto itself.

    Raises:
        ValueError: present but not an object, or carrying an unknown member.
            Member-VALUE validation is :func:`validate_geometry`'s, at the
            write boundary.
    """
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object when supplied")
    # 🔴 On the RAW dict, BEFORE the None-strip below. ``{"rackPosition": null}``
    # otherwise strips to ``{}``, reads as absence, and earns a 200 with the key
    # silently discarded — which is exactly what the unknown-member refusal
    # exists to prevent, and it slipped through because the strip ran first.
    reject_unknown_members(value)
    supplied = {member: item for member, item in value.items() if item is not None}
    return supplied or None


async def do_author_geometry(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    design_id: str,
    devices: list[dict[str, Any]],
    connections: list[dict[str, Any]] | None = None,
    racks: list[dict[str, Any]] | None = None,
) -> int:
    """Write the geometry rows carried by a ``author_topology`` payload.

    Walks the **same** input shape ``do_author_device_topology`` walks and
    picks up the optional ``geometry`` key on each device, each port and each
    rack, plus ``cable_geometry`` on each connection that names a ``cable_ref``.

    ``cable_geometry`` rather than ``geometry`` on a connection on purpose: a
    connection is an *edge*, and ``geometry`` there would read as the edge's
    own layout.  The row it writes belongs to the CABLE **node**, which is why
    it is only written when the connection actually names one.

    Additive, like every other write on this path: a device re-authored without
    a ``geometry`` key keeps the geometry it already had.  Expressing "this
    node has no geometry any more" is not possible here and is not this wave's
    to invent.

    Returns:
        The number of geometry rows written.
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    written = 0

    for rack in racks or []:
        geom = _geometry_of(rack)
        if geom is not None:
            await upsert_node_geometry(conn, ns_uuid, rack_label(design_id, rack["rack_ref"]), geom)
            written += 1

    for dev in devices:
        dev_ref: str = dev["device_ref"]
        geom = _geometry_of(dev)
        if geom is not None:
            await upsert_node_geometry(conn, ns_uuid, device_label(design_id, dev_ref), geom)
            written += 1
        for port in dev.get("ports", []):
            port_geom = _geometry_of(port)
            if port_geom is not None:
                await upsert_node_geometry(
                    conn, ns_uuid, port_label(design_id, dev_ref, port["port_ref"]), port_geom
                )
                written += 1

    for cnx in connections or []:
        cable_ref = cnx.get("cable_ref")
        cable_geom = _geometry_of(cnx, "cable_geometry")
        if cable_ref and cable_geom is not None:
            await upsert_node_geometry(conn, ns_uuid, cable_label(design_id, cable_ref), cable_geom)
            written += 1

    log.info(
        "do_author_geometry: ns=%s design=%s geometry_rows=%d",
        ns_uuid,
        design_id,
        written,
    )
    return written


async def do_author_functional_location_geometry(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    namespace_slug: str,
    site_name: str,
    buildings: list[dict[str, Any]],
) -> int:
    """Write the geometry rows carried by an ``author_functional_location`` payload.

    Optional ``geometry`` is accepted on each **building**, **floor** and
    **room** dict.  Room dimensions are the point of it: they go into ``meta``
    under ``copper.room.w`` / ``copper.room.d`` / ``copper.room.h``, in meters
    (Rev 2 §4).

    **POSITIONS carry no geometry**, and that is a shape limit, not a decision:
    ``positions`` is a list of bare strings in the tool contract, so there is
    nowhere to hang a geometry object without changing that contract.  Doing so
    is a contract change for Copper and is reported rather than absorbed.

    Labels come from ``graph.py``'s ``_fl_label`` — imported, never re-derived:
    the FL label is a slug-prefixed path join and a second copy of it would put
    geometry on labels the graph does not have.

    Returns:
        The number of geometry rows written.
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    written = 0

    for building in buildings:
        bld_name: str = building["name"]
        geom = _geometry_of(building)
        if geom is not None:
            await upsert_node_geometry(
                conn, ns_uuid, _fl_label(namespace_slug, site_name, bld_name), geom
            )
            written += 1

        for floor in building.get("floors", []):
            flr_name: str = floor["name"]
            flr_geom = _geometry_of(floor)
            if flr_geom is not None:
                await upsert_node_geometry(
                    conn,
                    ns_uuid,
                    _fl_label(namespace_slug, site_name, bld_name, flr_name),
                    flr_geom,
                )
                written += 1

            for room in floor.get("rooms", []):
                room_geom = _geometry_of(room)
                if room_geom is not None:
                    await upsert_node_geometry(
                        conn,
                        ns_uuid,
                        _fl_label(namespace_slug, site_name, bld_name, flr_name, room["name"]),
                        room_geom,
                    )
                    written += 1

    log.info(
        "do_author_functional_location_geometry: ns=%s site=%s geometry_rows=%d",
        ns_uuid,
        site_name,
        written,
    )
    return written
