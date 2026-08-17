"""
C2 autonomy — pure policy decision logic (Contract B §9.5).

This module is **I/O-free**: no DB, no HTTP, no Redis.  It receives all
state as arguments and returns a ``PolicyDecision`` dataclass.  Callers
(``governor.py``) own the I/O; this module owns the logic.

Dependency rule (uncle-bob inward-pointing): this module imports only
from the standard library.  No web / HTTP / asyncpg / Redis here.

The four Contract-B gates beyond confirm-only:
  1. Risk flags — ``flagship | first_of_kind | regulated`` force
     ``requires_confirm`` *regardless* of value band.
  2. Value ceiling — over-ceiling value forces ``requires_confirm``.
  3. Volume / rate cap — over-cap volume forces ``requires_confirm``.
  4. Counterparty allowlist — counterparty not on the allowlist forces
     ``requires_confirm``.

Gates are **additive**, not alternative: a value gate alone is
insufficient (§9.5).  Any gate tripping sets ``requires_confirm=True``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Risk-flag literals that force human confirmation regardless of value.
RISK_FLAGS_FORCE_CONFIRM: frozenset[str] = frozenset({"flagship", "first_of_kind", "regulated"})

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Result of ``evaluate_policy``.

    ``requires_confirm`` — ``True`` when any gate trips (human must
    approve before execution proceeds).  ``False`` when all gates pass
    (caller may proceed autonomously within the decorator's execution
    path).

    ``reason`` — human-readable summary of which gate(s) fired, or
    ``"ok"`` when all pass.
    """

    requires_confirm: bool
    reason: str


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------


def evaluate_policy(
    *,
    value: float | None = None,
    value_ceiling: float | None = None,
    volume_state: float | None = None,
    volume_rate_cap: float | None = None,
    counterparty: str | None = None,
    allowlist: Sequence[str] | None = None,
    risk_flags: Sequence[str] | None = None,
) -> PolicyDecision:
    """Evaluate all Contract-B gates and return a ``PolicyDecision``.

    Gates are checked in this order (all that fire are reported):
      1. Risk flags (highest priority — override value band)
      2. Value ceiling
      3. Volume / rate cap
      4. Counterparty allowlist

    Args:
        value:          Monetary or scalar value of the proposed act.
                        ``None`` means "no value gate configured".
        value_ceiling:  Maximum value allowed for autonomous execution.
                        ``None`` means "no ceiling configured".
        volume_state:   Current volume / rate accumulator (e.g. number of
                        acts in the rolling window).
                        ``None`` means "no volume gate configured".
        volume_rate_cap: Maximum allowed volume / rate.
                        ``None`` means "no cap configured".
        counterparty:   Name / ID of the counterparty.
                        ``None`` means "no allowlist gate configured".
        allowlist:      Sequence of permitted counterparties.
                        ``None`` or empty means "no allowlist gate".
        risk_flags:     Sequence of risk labels on the act
                        (e.g. ``["flagship", "regulated"]``).
                        ``None`` or empty means "no risk flags".

    Returns:
        ``PolicyDecision(requires_confirm=False, reason="ok")`` when all
        gates pass.  ``PolicyDecision(requires_confirm=True, reason=...)``
        when one or more gates fire.
    """
    reasons: list[str] = []

    # --- Gate 1: Risk flags (override everything) ---
    if risk_flags:
        fired_flags = [f for f in risk_flags if f in RISK_FLAGS_FORCE_CONFIRM]
        if fired_flags:
            reasons.append(f"risk_flags={fired_flags!r} force human-confirm")

    # --- Gate 2: Value ceiling ---
    if value is not None and value_ceiling is not None:
        if value > value_ceiling:
            reasons.append(f"value={value!r} exceeds ceiling={value_ceiling!r}")

    # --- Gate 3: Volume / rate cap ---
    if volume_state is not None and volume_rate_cap is not None:
        if volume_state > volume_rate_cap:
            reasons.append(f"volume_state={volume_state!r} exceeds cap={volume_rate_cap!r}")

    # --- Gate 4: Counterparty allowlist ---
    if counterparty is not None and allowlist is not None and len(allowlist) > 0:
        if counterparty not in allowlist:
            reasons.append(f"counterparty={counterparty!r} not in allowlist")

    if reasons:
        return PolicyDecision(requires_confirm=True, reason="; ".join(reasons))
    return PolicyDecision(requires_confirm=False, reason="ok")
