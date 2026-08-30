"""
nce/vertical_modules/assets/lifecycle.py
=========================================
Pure 14-state ASSET lifecycle state machine — zero DB, zero HTTP, zero
web/admin imports. Genuinely pure: every dependency below is stdlib.

Ported near-1:1 from Andreas's idempotent lifecycle enrichment
(``lib/asset/lifecycle.ts:37``, ``RECEIVED -> INSTALLED -> VERIFIED``, sets
warranty) and extended to the full 14-state machine named in
``docs/vertical_engines/09-assets-engine.md``'s ``do_advance_lifecycle``
spec::

    PROPOSED -> QUOTED -> ORDERED -> RECEIVED -> STAGED -> INSTALLED ->
    CONFIGURED -> VERIFIED -> ACTIVE -> DEGRADED -> MAINTENANCE -> EOL ->
    RETIRING -> RETIRED

State-machine rules
--------------------
- ``VALID_TRANSITIONS`` (read from ``nce/config_data/asset-lifecycle.json``,
  config-as-IP — no state name or edge is a Python literal in this module)
  declares the only legal directed edges. Any target state that is not a
  declared successor of the current state is structurally refused. This
  mirrors ``nce/vertical_modules/project/phase_gates.py``'s
  ``can_enter_phase`` — same shape, same "never raise on illegal input"
  contract — deliberately, for consistency across the codebase's pure
  state-machine modules.
- Re-applying the *current* state (``target_state == current_state``) is a
  no-op success, not an error. This is the "idempotent enrichment" property
  Andreas's source names explicitly: replaying the same install-completion
  event twice must never fail or double-set warranty. (This is the one
  place this module's contract diverges from ``phase_gates``, whose G-gates
  treat a self-transition as illegal — asset lifecycle events are commonly
  redelivered, e.g. by an at-least-once install-completion webhook, so
  idempotency is load-bearing here in a way it is not for phase gates.)
- ``WARRANTY_SET_ON_ENTER`` names the state(s) whose entry computes
  ``warranty_until`` from a caller-supplied ``warranty_months`` duration.
  The engine spec says warranty is set "from product warranty terms" —
  this module stays pure and never fetches those terms itself; a future
  DB-aware wave resolves the duration (Product's warranty terms) and
  passes it into :func:`advance` as ``warranty_months``. Passing ``None``
  (the default) is a valid, honest "duration not yet known" case, not an
  error: warranty is left unset (or unchanged) and ``warranty_set=False``
  in the result makes that explicit rather than silently faking a value.

Honest scope limit — disclosed, not silent
-------------------------------------------
``asset-lifecycle.json``'s ``VALID_TRANSITIONS`` encodes only the engine
spec's literal forward sequence, one successor per state, PROPOSED through
RETIRED. **No repair/return edge is modelled** (e.g. ``MAINTENANCE ->
ACTIVE`` or ``DEGRADED -> ACTIVE``) — nothing in the wave brief or the
engine spec states one, and inventing a business rule that isn't written
down is exactly the improvisation this program's waves are told to STOP
on rather than do. If a real fleet needs a degraded asset to recover to
ACTIVE without full retirement, that is a config-only change to the JSON's
``VALID_TRANSITIONS`` — this engine already supports whatever adjacency
the JSON declares; it never hard-codes the graph shape.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Config loader — reads nce/config_data/asset-lifecycle.json
# (no config class), mirrors project/phase_gates.py's load_gate_config().
# ---------------------------------------------------------------------------

_CONFIG_DATA_DIR = Path(__file__).parents[3] / "nce" / "config_data"
_CONFIG_FILENAME = "asset-lifecycle.json"

_REQUIRED_CONFIG_KEYS: tuple[str, ...] = (
    "STATES",
    "VALID_TRANSITIONS",
    "WARRANTY_SET_ON_ENTER",
)


def load_lifecycle_config() -> dict[str, Any]:
    """Load and return the contents of ``asset-lifecycle.json``.

    Returns
    -------
    dict
        Parsed JSON with keys ``STATES``, ``VALID_TRANSITIONS`` and
        ``WARRANTY_SET_ON_ENTER``.

    Raises
    ------
    FileNotFoundError
        When the config file is absent (misconfigured deployment).
    json.JSONDecodeError
        When the file is not valid JSON.
    KeyError
        When a required top-level key is missing.
    """
    config_path = _CONFIG_DATA_DIR / _CONFIG_FILENAME
    with config_path.open(encoding="utf-8") as fh:
        config: dict[str, Any] = json.load(fh)
    _validate_lifecycle_config(config)
    return config


def _validate_lifecycle_config(config: dict[str, Any]) -> None:
    """Raise KeyError if a required top-level key is absent."""
    for key in _REQUIRED_CONFIG_KEYS:
        if key not in config:
            raise KeyError(f"asset-lifecycle.json is missing required key '{key}'")


# ---------------------------------------------------------------------------
# Pure domain core
# ---------------------------------------------------------------------------


def advance(
    asset: dict[str, Any],
    target_state: str,
    *,
    warranty_months: int | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Attempt to move ``asset`` to ``target_state``.

    Pure function — no DB, no HTTP, no side effects. Never raises on an
    illegal transition; always returns a consistently-shaped dict so
    callers get uniform behaviour regardless of input (same contract as
    ``phase_gates.can_enter_phase``).

    Parameters
    ----------
    asset:
        Must contain ``"lifecycle_state"`` (str) — the asset's current
        state, e.g. ``"INSTALLED"``. May contain ``"warranty_until"``
        (str | None) — an already-set warranty end date, passed through
        unchanged unless this call newly sets it.
    target_state:
        The state the caller wants to enter (e.g. ``"VERIFIED"``).
    warranty_months:
        Warranty duration in whole months, resolved by the caller from
        Product's warranty terms. Required only to actually compute
        ``warranty_until`` on a transition named in
        ``WARRANTY_SET_ON_ENTER``; ``None`` (default) means "duration not
        yet known" — a valid, non-error input that simply leaves warranty
        unset.
    today:
        Reference date for warranty computation. Defaults to the current
        UTC date; pass an explicit value for deterministic tests.

    Returns
    -------
    dict
        ``{"ok": bool, "changed": bool, "new_state": str,
        "warranty_set": bool, "warranty_until": str | None,
        "error": str | None}``

        - ``ok=True, changed=False`` — idempotent no-op: ``target_state``
          equals the asset's current state.
        - ``ok=True, changed=True`` — legal transition applied;
          ``new_state == target_state``.
        - ``ok=False, changed=False`` — illegal transition (undeclared
          edge, unknown current state, or unknown target); ``new_state``
          is the asset's unchanged current state and ``error`` names the
          reason.

    Notes
    -----
    Never raises for a business-illegal transition. ``load_lifecycle_config``
    may still raise (``FileNotFoundError``/``json.JSONDecodeError``/``KeyError``)
    for a misconfigured deployment — that is a startup-time defect, not a
    per-call business outcome, so it is deliberately not swallowed here.
    """
    config = load_lifecycle_config()
    transitions: dict[str, list[str]] = config["VALID_TRANSITIONS"]
    warranty_states: list[str] = config["WARRANTY_SET_ON_ENTER"]

    current_state: str = asset.get("lifecycle_state", "")
    existing_warranty_until = asset.get("warranty_until")

    if target_state == current_state:
        return {
            "ok": True,
            "changed": False,
            "new_state": current_state,
            "warranty_set": False,
            "warranty_until": existing_warranty_until,
            "error": None,
        }

    if not _is_legal_transition(transitions, current_state, target_state):
        return {
            "ok": False,
            "changed": False,
            "new_state": current_state,
            "warranty_set": False,
            "warranty_until": existing_warranty_until,
            "error": f"illegal transition: {current_state!r} -> {target_state!r}",
        }

    warranty_set = False
    warranty_until = existing_warranty_until
    if target_state in warranty_states and warranty_months is not None:
        effective_today = today if today is not None else datetime.now(timezone.utc).date()
        warranty_until = _add_months(effective_today, warranty_months).isoformat()
        warranty_set = True

    return {
        "ok": True,
        "changed": True,
        "new_state": target_state,
        "warranty_set": warranty_set,
        "warranty_until": warranty_until,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Private helpers — single level of abstraction
# ---------------------------------------------------------------------------


def _is_legal_transition(
    transitions: dict[str, list[str]],
    current_state: str,
    target_state: str,
) -> bool:
    """Return True iff ``target_state`` is a declared successor of ``current_state``."""
    allowed_targets = transitions.get(current_state, [])
    return target_state in allowed_targets


def _add_months(start: date, months: int) -> date:
    """Calendar-correct month addition, clamped to the target month's last day.

    E.g. 2026-01-31 + 1 month -> 2026-02-28 (not an invalid 2026-02-31 or a
    silent rollover to 2026-03-03).
    """
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    day = min(start.day, _days_in_month(year, month))
    return date(year, month, day)


def _days_in_month(year: int, month: int) -> int:
    """Return the number of days in ``month`` of ``year``."""
    if month == 12:
        first_of_next = date(year + 1, 1, 1)
    else:
        first_of_next = date(year, month + 1, 1)
    return (first_of_next - date(year, month, 1)).days
