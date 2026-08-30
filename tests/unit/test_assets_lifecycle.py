"""
tests/unit/test_assets_lifecycle.py
=====================================
Acceptance tests for Batch 141 — Module 9.Wave 1 (lifecycle-core).

Covers:
  (a) ALGORITHM tests — inject fixture config; prove the pure 14-state
      machine rejects illegal transitions, accepts legal ones, is
      idempotent on a self-transition, and sets warranty only on the
      configured transition when a duration is supplied. Mirrors the
      procurement-tco / project-phase-gates split (algorithm vs config).
  (b) CONFIG tests — assert the real ``asset-lifecycle.json`` loads and
      has the required structure (all 14 states, a well-formed transition
      DAG, RETIRED terminal).
  (c) Package/registration tests — the ``assets`` package imports cleanly,
      ``handle_assets_ping`` behaves like every other vertical's skeleton
      ping, and ``assets_ping`` is registered in ``TOOL_REGISTRY`` with the
      correct gating flags.

All tests are plain unit tests — no DB, no Redis, no HTTP.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Shared fixture config — algorithm tests use this, never the real JSON values
# ---------------------------------------------------------------------------

_FIXTURE_CONFIG: dict[str, Any] = {
    "STATES": [
        "PROPOSED",
        "QUOTED",
        "ORDERED",
        "RECEIVED",
        "STAGED",
        "INSTALLED",
        "CONFIGURED",
        "VERIFIED",
        "ACTIVE",
        "DEGRADED",
        "MAINTENANCE",
        "EOL",
        "RETIRING",
        "RETIRED",
    ],
    "VALID_TRANSITIONS": {
        "PROPOSED": ["QUOTED"],
        "QUOTED": ["ORDERED"],
        "ORDERED": ["RECEIVED"],
        "RECEIVED": ["STAGED"],
        "STAGED": ["INSTALLED"],
        "INSTALLED": ["CONFIGURED"],
        "CONFIGURED": ["VERIFIED"],
        "VERIFIED": ["ACTIVE"],
        "ACTIVE": ["DEGRADED"],
        "DEGRADED": ["MAINTENANCE"],
        "MAINTENANCE": ["EOL"],
        "EOL": ["RETIRING"],
        "RETIRING": ["RETIRED"],
        "RETIRED": [],
    },
    "WARRANTY_SET_ON_ENTER": ["VERIFIED"],
}

# Alternative config proving behaviour is config-driven, not hard-coded.
_FIXTURE_CONFIG_ALT: dict[str, Any] = {
    "STATES": ["A", "B", "C"],
    "VALID_TRANSITIONS": {"A": ["B", "C"], "B": ["C"], "C": []},
    "WARRANTY_SET_ON_ENTER": ["C"],
}


# ---------------------------------------------------------------------------
# Helper — call advance() with an injected config (no filesystem I/O)
# ---------------------------------------------------------------------------


def _advance(
    config: dict[str, Any],
    current_state: str,
    target_state: str,
    *,
    warranty_until: str | None = None,
    warranty_months: int | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    from nce.vertical_modules.assets.lifecycle import advance

    asset: dict[str, Any] = {
        "lifecycle_state": current_state,
        "warranty_until": warranty_until,
    }
    with patch(
        "nce.vertical_modules.assets.lifecycle.load_lifecycle_config",
        return_value=config,
    ):
        return advance(asset, target_state, warranty_months=warranty_months, today=today)


# ===========================================================================
# (a) ALGORITHM TESTS — logic proven with fixture config
# ===========================================================================

# ---------------------------------------------------------------------------
# 1. Every declared legal edge succeeds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "current,target",
    [
        ("PROPOSED", "QUOTED"),
        ("QUOTED", "ORDERED"),
        ("ORDERED", "RECEIVED"),
        ("RECEIVED", "STAGED"),
        ("STAGED", "INSTALLED"),
        ("INSTALLED", "CONFIGURED"),
        ("CONFIGURED", "VERIFIED"),
        ("VERIFIED", "ACTIVE"),
        ("ACTIVE", "DEGRADED"),
        ("DEGRADED", "MAINTENANCE"),
        ("MAINTENANCE", "EOL"),
        ("EOL", "RETIRING"),
        ("RETIRING", "RETIRED"),
    ],
)
def test_every_declared_edge_is_accepted(current: str, target: str) -> None:
    """Every one of the 13 declared forward edges must succeed."""
    result = _advance(_FIXTURE_CONFIG, current, target)
    assert result["ok"] is True, f"{current}->{target} should be a legal transition"
    assert result["changed"] is True
    assert result["new_state"] == target
    assert result["error"] is None


# ---------------------------------------------------------------------------
# 2. Illegal transitions are REFUSED — the load-bearing claim (§ front-loaded
#    context point 7: assert the refusals, not just the permissions).
# ---------------------------------------------------------------------------


def test_backward_transition_is_refused() -> None:
    """Moving backwards (ACTIVE -> INSTALLED) must be refused."""
    result = _advance(_FIXTURE_CONFIG, "ACTIVE", "INSTALLED")
    assert result["ok"] is False
    assert result["changed"] is False
    assert result["new_state"] == "ACTIVE"  # state must NOT have moved
    assert result["error"] is not None


def test_skip_ahead_transition_is_refused() -> None:
    """Jumping past intermediate states (PROPOSED -> ORDERED) must be refused."""
    result = _advance(_FIXTURE_CONFIG, "PROPOSED", "ORDERED")
    assert result["ok"] is False
    assert result["changed"] is False
    assert result["new_state"] == "PROPOSED"


def test_skip_ahead_all_the_way_to_terminal_is_refused() -> None:
    """A brand-new asset cannot jump straight to RETIRED."""
    result = _advance(_FIXTURE_CONFIG, "PROPOSED", "RETIRED")
    assert result["ok"] is False
    assert result["changed"] is False
    assert result["new_state"] == "PROPOSED"


def test_terminal_state_has_no_legal_successor() -> None:
    """RETIRED is terminal — even a legal-looking forward name is refused."""
    result = _advance(_FIXTURE_CONFIG, "RETIRED", "EOL")
    assert result["ok"] is False
    assert result["changed"] is False
    assert result["new_state"] == "RETIRED"


def test_unknown_current_state_is_refused_not_raised() -> None:
    """An asset with a garbage/unknown lifecycle_state never raises — refused."""
    result = _advance(_FIXTURE_CONFIG, "NOT_A_REAL_STATE", "QUOTED")
    assert result["ok"] is False
    assert result["changed"] is False
    assert result["error"] is not None


def test_unknown_target_state_is_refused() -> None:
    """A target name absent from the config's adjacency is refused."""
    result = _advance(_FIXTURE_CONFIG, "PROPOSED", "NOT_A_REAL_STATE")
    assert result["ok"] is False
    assert result["changed"] is False
    assert result["new_state"] == "PROPOSED"


def test_illegal_transition_never_sets_warranty() -> None:
    """A refused transition must not have a side effect on warranty either."""
    result = _advance(
        _FIXTURE_CONFIG, "ACTIVE", "INSTALLED", warranty_months=12, today=date(2026, 1, 1)
    )
    assert result["ok"] is False
    assert result["warranty_set"] is False
    assert result["warranty_until"] is None


# ---------------------------------------------------------------------------
# 3. Idempotency — replaying the current state is a safe no-op, not an error
#    (Andreas's source explicitly calls the enrichment idempotent).
# ---------------------------------------------------------------------------


def test_self_transition_is_idempotent_no_op() -> None:
    result = _advance(_FIXTURE_CONFIG, "INSTALLED", "INSTALLED")
    assert result["ok"] is True
    assert result["changed"] is False
    assert result["new_state"] == "INSTALLED"
    assert result["error"] is None


def test_idempotent_replay_does_not_re_set_warranty() -> None:
    """Replaying VERIFIED->VERIFIED must not recompute warranty_until."""
    result = _advance(
        _FIXTURE_CONFIG,
        "VERIFIED",
        "VERIFIED",
        warranty_until="2026-06-01",
        warranty_months=12,
        today=date(2027, 1, 1),
    )
    assert result["ok"] is True
    assert result["changed"] is False
    assert result["warranty_set"] is False
    assert result["warranty_until"] == "2026-06-01"  # untouched, not recomputed


def test_terminal_self_transition_is_still_a_no_op() -> None:
    """Even the terminal state's self-transition is idempotent, not illegal."""
    result = _advance(_FIXTURE_CONFIG, "RETIRED", "RETIRED")
    assert result["ok"] is True
    assert result["changed"] is False


# ---------------------------------------------------------------------------
# 4. Warranty-set behaviour — only on the configured transition, only when a
#    duration is actually supplied.
# ---------------------------------------------------------------------------


def test_warranty_set_on_configured_transition_with_duration() -> None:
    result = _advance(
        _FIXTURE_CONFIG,
        "CONFIGURED",
        "VERIFIED",
        warranty_months=12,
        today=date(2026, 1, 15),
    )
    assert result["ok"] is True
    assert result["warranty_set"] is True
    assert result["warranty_until"] == "2027-01-15"


def test_warranty_month_end_clamped_not_rolled_over() -> None:
    """2026-01-31 + 1 month -> 2026-02-28, never an invalid day-31 or a
    silent rollover into March."""
    result = _advance(
        _FIXTURE_CONFIG,
        "CONFIGURED",
        "VERIFIED",
        warranty_months=1,
        today=date(2026, 1, 31),
    )
    assert result["warranty_until"] == "2026-02-28"


def test_warranty_not_set_without_a_supplied_duration() -> None:
    """Duration not yet known (warranty_months=None) is an honest non-error:
    warranty stays unset rather than a fabricated default being invented."""
    result = _advance(_FIXTURE_CONFIG, "CONFIGURED", "VERIFIED", today=date(2026, 1, 1))
    assert result["ok"] is True
    assert result["changed"] is True
    assert result["warranty_set"] is False
    assert result["warranty_until"] is None


def test_warranty_not_set_on_a_non_triggering_transition() -> None:
    """Even with a duration supplied, only the configured state triggers it."""
    result = _advance(
        _FIXTURE_CONFIG, "STAGED", "INSTALLED", warranty_months=12, today=date(2026, 1, 1)
    )
    assert result["ok"] is True
    assert result["warranty_set"] is False
    assert result["warranty_until"] is None


# ---------------------------------------------------------------------------
# 5. Config drives behaviour — not hard-coded in lifecycle.py
# ---------------------------------------------------------------------------


def test_different_config_different_legal_edges() -> None:
    """The alt fixture allows A->C directly; the default fixture has no A/B/C states at all."""
    result_alt = _advance(_FIXTURE_CONFIG_ALT, "A", "C")
    assert result_alt["ok"] is True

    # Same edge is meaningless (and therefore illegal) under the default config.
    result_default = _advance(_FIXTURE_CONFIG, "A", "C")
    assert result_default["ok"] is False


def test_different_config_different_warranty_trigger() -> None:
    """Alt config sets warranty on entering 'C', not 'VERIFIED'."""
    result = _advance(_FIXTURE_CONFIG_ALT, "A", "C", warranty_months=6, today=date(2026, 1, 1))
    assert result["warranty_set"] is True
    assert result["warranty_until"] == "2026-07-01"


def test_result_shape_is_always_consistent() -> None:
    for current, target in [
        ("PROPOSED", "QUOTED"),
        ("PROPOSED", "RETIRED"),
        ("RETIRED", "RETIRED"),
        ("GARBAGE", "QUOTED"),
    ]:
        result = _advance(_FIXTURE_CONFIG, current, target)
        for key in ("ok", "changed", "new_state", "warranty_set", "warranty_until", "error"):
            assert key in result, f"'{key}' missing for {current}->{target}"
        assert isinstance(result["ok"], bool)
        assert isinstance(result["changed"], bool)
        assert isinstance(result["warranty_set"], bool)


# ===========================================================================
# (b) CONFIG TESTS — assert the real JSON file loads and has required structure
# ===========================================================================

_ALL_14_STATES = (
    "PROPOSED",
    "QUOTED",
    "ORDERED",
    "RECEIVED",
    "STAGED",
    "INSTALLED",
    "CONFIGURED",
    "VERIFIED",
    "ACTIVE",
    "DEGRADED",
    "MAINTENANCE",
    "EOL",
    "RETIRING",
    "RETIRED",
)


def _load_real_config() -> dict[str, Any]:
    from nce.vertical_modules.assets.lifecycle import load_lifecycle_config

    return load_lifecycle_config()


def test_real_config_loads_without_error() -> None:
    config = _load_real_config()
    assert isinstance(config, dict)


def test_real_config_has_required_keys() -> None:
    config = _load_real_config()
    for key in ("STATES", "VALID_TRANSITIONS", "WARRANTY_SET_ON_ENTER"):
        assert key in config


def test_real_config_has_exactly_14_states() -> None:
    config = _load_real_config()
    assert len(config["STATES"]) == 14
    assert set(config["STATES"]) == set(_ALL_14_STATES)


def test_real_config_transitions_cover_every_state() -> None:
    """Every declared state must be a key in VALID_TRANSITIONS (even if terminal -> [])."""
    config = _load_real_config()
    vt: dict[str, list[str]] = config["VALID_TRANSITIONS"]
    for state in config["STATES"]:
        assert state in vt, f"VALID_TRANSITIONS missing state '{state}'"


def test_real_config_transitions_form_a_dag() -> None:
    """Every declared target must itself be a declared state (no dangling refs)."""
    config = _load_real_config()
    vt: dict[str, list[str]] = config["VALID_TRANSITIONS"]
    states = set(config["STATES"])
    for source, targets in vt.items():
        for t in targets:
            assert t in states, f"VALID_TRANSITIONS['{source}'] references undeclared state '{t}'"


def test_real_config_retired_is_terminal() -> None:
    config = _load_real_config()
    assert config["VALID_TRANSITIONS"]["RETIRED"] == []


def test_real_config_warranty_trigger_is_verified() -> None:
    """VERIFIED must be the ONLY warranty trigger -- exact set, not membership.

    This assertion is deliberately `==` and not `in`. An `in` check answers
    "is VERIFIED a trigger?", but the invariant this module actually rests on
    is the stronger "is VERIFIED the only trigger?", and the two come apart
    silently: with `in`, appending "ACTIVE" to WARRANTY_SET_ON_ENTER in the
    JSON leaves this test green while `advance()` starts setting a warranty on
    VERIFIED->ACTIVE. That divergence was demonstrated against this file (an
    adversarial audit of Batch 141 mutated the config out-of-tree and the
    membership form did not catch it), which is why the exhaustiveness check
    against the REAL config file is written as equality here. The algorithm's
    own exclusivity is covered separately against a controlled fixture; this
    test exists solely to stop the shipped config from broadening underneath
    it.
    """
    config = _load_real_config()
    assert config["WARRANTY_SET_ON_ENTER"] == ["VERIFIED"]


def test_real_config_drives_advance_end_to_end() -> None:
    """Real JSON + full path through advance() — integration of config + logic."""
    from nce.vertical_modules.assets.lifecycle import advance

    asset: dict[str, Any] = {"lifecycle_state": "CONFIGURED", "warranty_until": None}

    # Legal edge, warranty-triggering, duration supplied -> warranty computed.
    result_ok = advance(asset, "VERIFIED", warranty_months=24, today=date(2026, 3, 1))
    assert result_ok["ok"] is True
    assert result_ok["new_state"] == "VERIFIED"
    assert result_ok["warranty_set"] is True
    assert result_ok["warranty_until"] == "2028-03-01"

    # Illegal edge (skip STAGED/INSTALLED) -> refused, no exception.
    fresh_asset: dict[str, Any] = {"lifecycle_state": "RECEIVED", "warranty_until": None}
    result_illegal = advance(fresh_asset, "VERIFIED")
    assert result_illegal["ok"] is False
    assert result_illegal["new_state"] == "RECEIVED"


def test_load_lifecycle_config_missing_key_raises_keyerror() -> None:
    """A malformed config (missing a required top-level key) fails loudly at
    load time — this is a deployment defect, never a silent business outcome."""
    from nce.vertical_modules.assets.lifecycle import _validate_lifecycle_config

    with pytest.raises(KeyError):
        _validate_lifecycle_config({"STATES": [], "VALID_TRANSITIONS": {}})


# ===========================================================================
# (c) PACKAGE / MCP REGISTRATION TESTS
# ===========================================================================


def test_package_imports() -> None:
    import nce.vertical_modules.assets  # noqa: F401
    import nce.vertical_modules.assets.lifecycle  # noqa: F401
    import nce.vertical_modules.assets.mcp_handlers  # noqa: F401


_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"


def _make_engine() -> MagicMock:
    """Minimal NCEEngine mock — assets_ping needs no DB calls."""
    return MagicMock()


@pytest.mark.asyncio
async def test_handle_assets_ping_ok() -> None:
    from nce.vertical_modules.assets.mcp_handlers import handle_assets_ping

    engine = _make_engine()
    result = await handle_assets_ping(engine, {"namespace_id": _NAMESPACE_ID})
    payload = json.loads(result)

    assert payload["ok"] is True
    assert payload["engine"] == "assets"


@pytest.mark.asyncio
async def test_handle_assets_ping_missing_namespace_id() -> None:
    from nce.mcp_errors import McpError
    from nce.vertical_modules.assets.mcp_handlers import handle_assets_ping

    engine = _make_engine()
    with pytest.raises(McpError) as exc_info:
        await handle_assets_ping(engine, {})

    assert exc_info.value.code == -32602  # MCP_INVALID_PARAMS


def test_assets_ping_registered_with_correct_flags() -> None:
    from nce.tool_registry import TOOL_REGISTRY

    assert "assets_ping" in TOOL_REGISTRY, "'assets_ping' not found in TOOL_REGISTRY"
    spec = TOOL_REGISTRY["assets_ping"]
    assert spec.cacheable is True
    assert spec.admin_only is False
    assert spec.mutation is False
    assert spec.migration is False


def test_assets_ping_in_cacheable_tools() -> None:
    from nce.tool_registry import CACHEABLE_TOOLS

    assert "assets_ping" in CACHEABLE_TOOLS


def test_assets_ping_not_in_mutation_or_admin_sets() -> None:
    from nce.tool_registry import ADMIN_ONLY_TOOLS, MUTATION_TOOLS

    assert "assets_ping" not in MUTATION_TOOLS
    assert "assets_ping" not in ADMIN_ONLY_TOOLS
