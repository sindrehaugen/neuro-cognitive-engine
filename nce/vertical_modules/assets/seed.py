"""
nce/vertical_modules/assets/seed.py
=====================================
Seed one relational asset register row from the BOM line it originated
from (Module 9, Wave 2 — ``seed-from-bom``, Batch 142), migration 054's
``assets`` table.

Per ``docs/vertical_engines/09-assets-engine.md``'s ``do_seed_asset_from_bom``
spec and its "Tables/migrations" section (the ``assets`` table is the "fast
register" behind the room-centric asset view).

This module writes NO graph — declared here, never silently omitted
--------------------------------------------------------------------------
Batch 142 was SPLIT. This half is **relational only**. This module writes no
``kg_nodes`` row and no ``kg_edges`` row at all, calls no ``assert_owner``,
and imports nothing from ``nce.entity_resolution.ownership`` or
``nce.events.emit``. The graph projection — the ``ASSET`` node, the
``BOM_LINE -[installed_as]-> ASSET`` seed edge and the
``ASSET -[lives_in]-> FUNCTIONAL_LOCATION`` edge — together with ``ASSET``'s
Contract-A row in ``nce/config_data/node-ownership.json``, is **Batch
142b**'s.

The precedent is exact and deliberate: Batch 132's
``nce/vertical_modules/inventory/goods_receipt.py`` states in its own module
docstring that it "writes NO ``kg_nodes`` and NO ``kg_edges`` at all", while
``GOODS_RECEIPT``'s ownership row lands in a separate wave (132b). This
module says the same thing for the same reason — an omission that is named
is a scope decision; one that is left to be discovered is a defect. (That
file is not on ``main`` at the time of writing: it is in flight on branch
``vm-b132-m11-w4-goods-receipt`` / migration 052's open PR. It was read
there, not invented here.)

Note the direct consequence: with no ``ASSET`` row in
``node-ownership.json``, the deny-by-default ``assert_owner`` guard would
(correctly) refuse a graph write from this package today — exactly as
``nce/vertical_modules/assets/__init__.py`` already warns. Adding one is
Batch 142b's job, not this wave's.

Idempotency is BY DB CONSTRAINT, never a check-then-write
------------------------------------------------------------
``assets_ns_bom_line_uq`` — ``UNIQUE (namespace_id, bom_line_id)``, migration
054 — is the sole arbiter. The INSERT is ``ON CONFLICT ON CONSTRAINT
assets_ns_bom_line_uq DO NOTHING``; when it returns no row, this IS a replay
and ``created`` is ``False``. This is never re-expressed as a ``SELECT …
THEN INSERT`` pre-check: two concurrent identical seeds would both pass such
a pre-check and both insert. The database refuses; the Python only reacts.
``created`` is the gate every subsequent effect must hang off — this wave
has none (there is no graph write to gate), and it is precisely the flag
Batch 142b will use so a replayed seed does not re-emit a graph event.

The idempotency KEY deviates from the engine spec — declared, not silent
--------------------------------------------------------------------------
``docs/vertical_engines/09-assets-engine.md`` ("Core functions",
``do_seed_asset_from_bom``) specifies **"Idempotent on serial."** This wave
is idempotent on ``(namespace_id, bom_line_id)`` instead. That is a
deliberate substitution, and it is named here because the rule above cuts
both ways: an omission that is named is a scope decision, one left to be
discovered is a defect.

WHY the spec's key is not usable. ``serial`` is nullable BY DESIGN — the
engine doc calls this function at install handover, and a seed legitimately
precedes the installer's serial scan (migration 054 documents the nullable
``serial`` as an honest "not captured yet"). A ``UNIQUE (namespace_id,
serial)`` therefore cannot express this idempotency in either of its two
available shapes: under Postgres' default NULLS-DISTINCT semantics it is
VACUOUS for exactly the pre-scan case — every unscanned seed inserts, never
conflicts, and re-seeding one BOM line before the scan double-writes, so
idempotency is lost precisely where it is needed; under ``NULLS NOT
DISTINCT`` it over-constrains, collapsing every unscanned asset in a
namespace into one row and refusing the second legitimate pre-scan seed.
``bom_line_id`` is required and present at seed time, so it is the only
offered key that actually holds.

THE CONSEQUENCE, ACCEPTED HERE. The two keys are not equivalent and the
difference is observable: one physical device re-issued under a second BOM
line — ``SN-123`` seeded against ``BL-1``, the BOM revised and the same unit
re-issued as ``BL-2`` — yields TWO rows, both reporting ``created=True``.
Nothing in this wave detects that. There is no unique index on
``(namespace_id, serial)``, and no code path in this module reads by
``serial`` at all. Whether serial uniqueness is wanted is a later wave's
decision, not this one's — it cannot be a plain UNIQUE while ``serial`` is
nullable, so a partial index ``WHERE serial IS NOT NULL`` would be its
shape. This wave neither adds it nor implies it is there.

INSERT-only, never UPDATE
----------------------------
There is no update path in this module, by construction. On conflict the
EXISTING row is read back and returned unmodified — not one column changes,
``updated_at`` included. Migration 054 grants ``nce_app`` UPDATE for a later
wave's ``do_advance_lifecycle``; nothing here uses it. Re-seeding a BOM line
with different ``serial``/``functional_location_id`` values therefore returns
the ORIGINAL row's values, not the new arguments (pinned by
``tests/test_assets_seed.py``).

The initial lifecycle state comes from Batch 141, not from a literal here
--------------------------------------------------------------------------
:func:`initial_lifecycle_state` reads
``nce/config_data/asset-lifecycle.json`` through Batch 141's
:func:`nce.vertical_modules.assets.lifecycle.load_lifecycle_config`. No state
name is a Python literal in this module, matching ``lifecycle.py``'s own
config-as-IP convention. The entry state is derived structurally — the one
declared state that is nobody's declared successor — and cross-checked
against ``STATES[0]``; a config where those two disagree is ambiguous about
where an asset's life starts, and this module raises rather than guesses.
No transition logic is re-implemented here: this wave writes the entry state
once, at seed time, and never moves it.

Scoped explicitly by ``namespace_id``, never by RLS alone
-------------------------------------------------------------
Every statement below carries its own ``namespace_id = $1::uuid`` predicate
in addition to running inside ``scoped_pg_session``. The owner/superuser pool
used by integration tests BYPASSES ``FORCE ROW LEVEL SECURITY``, so an
RLS-only query passes its own test and leaks in production — this has bitten
B67, B120 and B130.

BOM_LINE is a REFERENCE on the row, not an edge — and is never authored here
--------------------------------------------------------------------------
Nothing in the program creates ``BOM_LINE`` nodes yet (Batch 132a, unbuilt).
``bom_line_id`` is stored as an identifier column on the ``assets`` row so
this wave needs no such node to exist, and this module never creates, reads
or writes a ``BOM_LINE`` anywhere.

Registration is deliberately NOT this wave's job
----------------------------------------------------
:func:`do_seed_asset_from_bom` is not registered as an MCP tool and has no
REST route here — ``nce/tool_registry.py`` and ``nce/admin_app.py`` reference
nothing in this file. It is unreachable from any surface when this wave
lands, exactly as ``goods_receipt.py`` was; registration is a later wave's.

Dependency direction (uncle-bob-craft)
------------------------------------------
This module imports only ``nce.db_utils.scoped_pg_session`` and its sibling
``lifecycle.py``'s PUBLIC ``load_lifecycle_config`` — no web/HTTP/admin
framework imports and nothing from another vertical module. ``NCEEngine`` is
imported under ``TYPE_CHECKING`` only, matching every other vertical module's
convention.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.assets.lifecycle import load_lifecycle_config

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.assets.seed")

# Engine-authored write, not an external-system sync — mirrors rma.py's /
# stock.py's own 'agent' choice ('sync' is reserved for the D365 origin).
_DEFAULT_CHANGE_ORIGIN = "agent"

# Migration 054's named idempotency arbiter. Named (not the column list) in
# the ON CONFLICT below so the constraint's IDENTITY is load-bearing: rename
# or drop it and this module fails loudly instead of silently double-seeding.
_IDEMPOTENCY_CONSTRAINT = "assets_ns_bom_line_uq"


# ---------------------------------------------------------------------------
# Entry state — read from asset-lifecycle.json via Batch 141's loader. No
# state name is a literal in this module (config-as-IP, mirroring
# lifecycle.py's own convention).
# ---------------------------------------------------------------------------


def initial_lifecycle_state() -> str:
    """Return the lifecycle's entry state — where a newly seeded asset starts.

    Derived, not hard-coded: the entry state is the one member of ``STATES``
    that is not declared as any other state's successor in
    ``VALID_TRANSITIONS``. That derivation is cross-checked against
    ``STATES[0]`` (the engine spec's declared order), and a config in which
    the two disagree — or which has zero or several entry states — is
    ambiguous about where an asset's life begins, so this raises rather than
    picking one.

    Raises
    ------
    ValueError
        ``asset-lifecycle.json`` declares no states, more than one entry
        state, or an entry state that is not ``STATES[0]``.
    """
    config = load_lifecycle_config()
    states: list[str] = list(config["STATES"])
    transitions: dict[str, list[str]] = config["VALID_TRANSITIONS"]

    if not states:
        raise ValueError("asset-lifecycle.json declares an empty STATES list")

    successors = {target for targets in transitions.values() for target in targets}
    entry_states = [state for state in states if state not in successors]

    if len(entry_states) != 1:
        raise ValueError(
            "asset-lifecycle.json must declare exactly one entry state (a state that is "
            f"no other state's successor); found {entry_states!r}"
        )
    if entry_states[0] != states[0]:
        raise ValueError(
            f"asset-lifecycle.json's entry state {entry_states[0]!r} is not STATES[0] "
            f"({states[0]!r}) — the declared order and the transition map disagree"
        )
    return entry_states[0]


# ---------------------------------------------------------------------------
# Parameter coercion — every validated field is rejected before any DB call.
# ---------------------------------------------------------------------------


def _as_ns_uuid(raw: Any, field: str) -> UUID:
    if not raw:
        raise ValueError(f"'{field}' is required")
    return UUID(str(raw)) if not isinstance(raw, UUID) else raw


def _as_required_text(raw: Any, field: str) -> str:
    """Coerce to a required, non-empty, stripped string.

    Mirrors migration 054's ``assets_bom_line_id_not_blank`` CHECK so a
    caller gets a domain error instead of a raw
    ``asyncpg.CheckViolationError``. Stripping here also means a
    whitespace-padded identifier cannot slip past the UNIQUE constraint as a
    "different" BOM line.
    """
    text = str(raw or "").strip()
    if not text:
        raise ValueError(f"do_seed_asset_from_bom: '{field}' is required")
    return text


def _as_optional_text(raw: Any) -> str | None:
    """``serial``/``functional_location_id`` are nullable — an absent or blank
    value is ``None``, never an empty string, so a direct-INSERT read-back and
    a do_seed_asset_from_bom-written row are indistinguishable. Mirrors
    migration 054's ``*_not_blank`` CHECKs."""
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


# ---------------------------------------------------------------------------
# Public: do_seed_asset_from_bom — the SOLE writer of `assets`. INSERT-only.
# ---------------------------------------------------------------------------


async def do_seed_asset_from_bom(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Seed one ``assets`` row from the BOM line it originated from.

    Writes NOTHING but that row: no ``kg_nodes``, no ``kg_edges``, no outbox
    event, no A2A call (see the module docstring — that is Batch 142b's
    half). Idempotent by DB constraint: seeding the same ``bom_line_id``
    twice in one namespace produces exactly one row.

    Parameters
    ----------
    params:
        ``{
            "namespace_id":           str | UUID,   # required
            "bom_line_id":            str,          # required — the idempotency key
            "serial":                 str | None,   # optional — serialised units only
            "functional_location_id": str | None,   # optional — the room
        }``

    Returns
    -------
    dict
        ``{"ok": True, "created": bool, "asset_id": str, "bom_line_id": str,
        "serial": str | None, "functional_location_id": str | None,
        "lifecycle_state": str}``. ``created`` is ``False`` when this
        ``bom_line_id`` had already been seeded for this namespace — the
        EXISTING row is returned, unmodified.

    Raises
    ------
    ValueError
        ``namespace_id`` or ``bom_line_id`` missing/blank, or
        ``asset-lifecycle.json`` does not declare an unambiguous entry state.
    """
    ns_uuid = _as_ns_uuid(params.get("namespace_id"), "namespace_id")
    bom_line_id = _as_required_text(params.get("bom_line_id"), "bom_line_id")
    serial = _as_optional_text(params.get("serial"))
    functional_location_id = _as_optional_text(params.get("functional_location_id"))
    lifecycle_state = initial_lifecycle_state()

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        inserted = await conn.fetchrow(
            f"""
            INSERT INTO assets
                (namespace_id, bom_line_id, serial, functional_location_id,
                 lifecycle_state, change_origin)
            VALUES ($1::uuid, $2, $3, $4, $5, $6)
            ON CONFLICT ON CONSTRAINT {_IDEMPOTENCY_CONSTRAINT} DO NOTHING
            RETURNING id, bom_line_id, serial, functional_location_id, lifecycle_state
            """,  # the only interpolated value is the module constant above
            str(ns_uuid),
            bom_line_id,
            serial,
            functional_location_id,
            lifecycle_state,
            _DEFAULT_CHANGE_ORIGIN,
        )

        created = inserted is not None
        row = inserted
        if row is None:
            # Replay. There is no update path in this module — the caller gets
            # the EXISTING row back, byte-identical, never a fresh write.
            # Scoped by namespace_id EXPLICITLY, not by RLS: the owner pool
            # used in tests bypasses FORCE RLS (module docstring).
            row = await conn.fetchrow(
                """
                SELECT id, bom_line_id, serial, functional_location_id, lifecycle_state
                FROM assets
                WHERE namespace_id = $1::uuid AND bom_line_id = $2
                """,
                str(ns_uuid),
                bom_line_id,
            )
        assert row is not None  # RETURNING or the fallback SELECT always yields one

    return {
        "ok": True,
        "created": created,
        "asset_id": str(row["id"]),
        "bom_line_id": row["bom_line_id"],
        "serial": row["serial"],
        "functional_location_id": row["functional_location_id"],
        "lifecycle_state": row["lifecycle_state"],
    }
