"""H-7 test lift for Module 14 (Marketing Engine): property and negative tests
built from the engine's own templates (existing fixtures and constants), no
invented data.

Six groups, each deriving its cases from something already in the tree rather
than from a hand-picked example:

1. ``assert_no_sensitive_financials`` -- one property test per entry in the
   engine's own ``FORBIDDEN_FINANCIAL_KEYS`` (imported, never re-listed), so a
   key silently dropped from that set is caught by name, not only in aggregate.
2. ``assert_claims_grounded`` -- negative/positive property tests, plus the
   wave's flagship finding: a claim whose ``graph_node_id`` resolves to NO node
   at all is accepted today, because the guard checks that the field is a
   non-empty string, never that the node exists (MK-2's documented promise --
   "every claim must cite a valid graph node" -- is narrower in code than in
   its own docstring). Marked ``xfail(strict=True)`` in the same spirit as
   H-1's verb-mix floor: RED today by design, and an XPASS the day someone
   adds real graph-existence checking, forcing that marker's removal rather
   than a silent behaviour change.
3. ``assert_positive_nps_only`` -- boundary property tests around the
   threshold (the function's own ``<`` comparison, exercised at, above, below,
   and with a caller-supplied threshold).
4. ``assert_consent_allows_tier`` -- tier-combination property tests.
5. ``require_marketing_enabled`` -- namespace opt-in property tests via a
   minimal mock pool, following the same mock-pool shape used elsewhere in
   this suite (``tests/unit/test_marketing_publish.py``).
6. ``do_publish_content`` -- the publish gate itself never re-examines
   grounding at all (unlike MK-1/MK-3/MK-4, which it does enforce): an
   approved, consented artifact publishes regardless of whether the claims
   baked into it were ever grounded. Demonstrated directly, not as an xfail,
   since this is the gate behaving exactly as coded -- the gap is architectural
   (no grounding re-check at the publish boundary), not a broken assertion.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nce.vertical_modules.marketing._guard import (
    FORBIDDEN_FINANCIAL_KEYS,
    MarketingConsentMissingError,
    MarketingDisabledError,
    MarketingLowHealthTriggerError,
    MarketingSensitiveDataLeakError,
    MarketingUngroundedClaimError,
    assert_claims_grounded,
    assert_consent_allows_tier,
    assert_no_sensitive_financials,
    assert_positive_nps_only,
    require_marketing_enabled,
)
from nce.vertical_modules.marketing.publish import do_publish_content

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000002"
_ARTIFACT_ID = str(uuid4())

# ---------------------------------------------------------------------------
# Group 1 -- assert_no_sensitive_financials, one property test per forbidden
# key (derived from the engine's own FORBIDDEN_FINANCIAL_KEYS, not re-listed).
# ---------------------------------------------------------------------------


def test_rejects_forbidden_key_margin() -> None:
    with pytest.raises(MarketingSensitiveDataLeakError):
        assert_no_sensitive_financials({"margin": 0.4, "room_type": "boardroom"})


def test_rejects_forbidden_key_cost() -> None:
    with pytest.raises(MarketingSensitiveDataLeakError):
        assert_no_sensitive_financials({"cost": 1000, "room_type": "boardroom"})


def test_rejects_forbidden_key_unit_cost() -> None:
    with pytest.raises(MarketingSensitiveDataLeakError):
        assert_no_sensitive_financials({"unit_cost": 50, "room_type": "boardroom"})


def test_rejects_forbidden_key_internal_cost() -> None:
    with pytest.raises(MarketingSensitiveDataLeakError):
        assert_no_sensitive_financials({"internal_cost": 200, "room_type": "boardroom"})


def test_rejects_forbidden_key_internal_labor_cost() -> None:
    with pytest.raises(MarketingSensitiveDataLeakError):
        assert_no_sensitive_financials({"internal_labor_cost": 4500, "room_type": "boardroom"})


def test_rejects_forbidden_key_profit_margin() -> None:
    with pytest.raises(MarketingSensitiveDataLeakError):
        assert_no_sensitive_financials({"profit_margin": 0.3, "room_type": "boardroom"})


def test_rejects_forbidden_key_profit_margin_pct() -> None:
    with pytest.raises(MarketingSensitiveDataLeakError):
        assert_no_sensitive_financials({"profit_margin_pct": 30.0, "room_type": "boardroom"})


def test_rejects_forbidden_key_markup() -> None:
    with pytest.raises(MarketingSensitiveDataLeakError):
        assert_no_sensitive_financials({"markup": 0.15, "room_type": "boardroom"})


def test_rejects_forbidden_key_markup_pct() -> None:
    with pytest.raises(MarketingSensitiveDataLeakError):
        assert_no_sensitive_financials({"markup_pct": 15.0, "room_type": "boardroom"})


def test_rejects_forbidden_key_supplier_rebate() -> None:
    with pytest.raises(MarketingSensitiveDataLeakError):
        assert_no_sensitive_financials({"supplier_rebate": 500, "room_type": "boardroom"})


def test_rejects_forbidden_key_purchase_price() -> None:
    with pytest.raises(MarketingSensitiveDataLeakError):
        assert_no_sensitive_financials({"purchase_price": 900, "room_type": "boardroom"})


def test_rejects_forbidden_key_wholesale_price() -> None:
    with pytest.raises(MarketingSensitiveDataLeakError):
        assert_no_sensitive_financials({"wholesale_price": 700, "room_type": "boardroom"})


def test_forbidden_financial_key_set_is_exactly_twelve_entries() -> None:
    """Guard-the-guard: if this count changes, a test above is missing or stale."""
    assert len(FORBIDDEN_FINANCIAL_KEYS) == 12


def test_every_forbidden_key_has_a_dedicated_rejection_test_above() -> None:
    """Property test over the engine's own constant: every key it lists must
    actually trigger a rejection when present alone -- not merely assumed."""
    for key in FORBIDDEN_FINANCIAL_KEYS:
        with pytest.raises(MarketingSensitiveDataLeakError):
            assert_no_sensitive_financials({key: 1, "safe_field": "unaffected"})


def test_forbidden_key_match_is_case_insensitive() -> None:
    """The guard lower-cases before matching; an upper-case key must still be caught."""
    with pytest.raises(MarketingSensitiveDataLeakError):
        assert_no_sensitive_financials({"MARGIN": 0.4})


def test_data_with_no_forbidden_keys_passes() -> None:
    """Positive control: ordinary marketing-safe fields never trip the guard."""
    assert_no_sensitive_financials(
        {"room_type": "boardroom", "technology_stack": ["Q-SYS"], "outcomes": {"uptime": 99.9}}
    )


# ---------------------------------------------------------------------------
# Group 2 -- assert_claims_grounded property/negative tests, and the
# flagship publish-gate finding.
# ---------------------------------------------------------------------------


def test_claims_grounded_rejects_empty_list() -> None:
    with pytest.raises(MarketingUngroundedClaimError):
        assert_claims_grounded([])


def test_claims_grounded_rejects_missing_node_id_key() -> None:
    with pytest.raises(MarketingUngroundedClaimError):
        assert_claims_grounded([{"claim": "Reduced setup time by 40%"}])


def test_claims_grounded_rejects_missing_claim_key() -> None:
    with pytest.raises(MarketingUngroundedClaimError):
        assert_claims_grounded([{"graph_node_id": "node-1"}])


def test_claims_grounded_rejects_whitespace_only_node_id() -> None:
    with pytest.raises(MarketingUngroundedClaimError):
        assert_claims_grounded([{"graph_node_id": "   ", "claim": "Something happened"}])


def test_claims_grounded_rejects_whitespace_only_claim_text() -> None:
    with pytest.raises(MarketingUngroundedClaimError):
        assert_claims_grounded([{"graph_node_id": "node-1", "claim": "   "}])


def test_claims_grounded_accepts_one_well_formed_citation() -> None:
    assert_claims_grounded([{"graph_node_id": "node-1", "claim": "Deployed AV-over-IP backbone"}])


def test_claims_grounded_accepts_several_well_formed_citations() -> None:
    assert_claims_grounded(
        [
            {"graph_node_id": "node-1", "claim": "Deployed AV-over-IP backbone"},
            {"graph_node_id": "node-2", "claim": "Reduced meeting start delay by 95%"},
            {"graph_node_id": "node-3", "claim": "Deployed Shure MXA920 ceiling array"},
        ]
    )


def test_claims_grounded_one_bad_citation_among_good_ones_still_rejects() -> None:
    """A single ungrounded citation must block the whole set, not just be skipped."""
    with pytest.raises(MarketingUngroundedClaimError):
        assert_claims_grounded(
            [
                {"graph_node_id": "node-1", "claim": "Real, cited claim"},
                {"graph_node_id": "", "claim": "Uncited claim slipped in"},
            ]
        )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "MK-2 grounding (assert_claims_grounded in nce/vertical_modules/marketing/_guard.py) "
        "checks that graph_node_id is a non-empty string, never that the node actually exists "
        "in the graph. A fabricated node ID passes clean. Flagged to ML-orch (H-7, charter §9 "
        "'Lane H') as a real gap, not a broken test -- fixing it means adding a graph-existence "
        "lookup, which is outside a Sonnet test-lift wave's scope."
    ),
)
def test_publish_gate_claim_citing_a_nonexistent_node_is_not_rejected_today() -> None:
    """H-7's flagship finding.

    MK-2's own docstring promises "every claim must cite a VALID graph node."
    ``assert_claims_grounded`` only checks that ``graph_node_id`` is a non-empty
    string -- it never queries the graph to confirm the node exists. A citation
    referencing a syntactically well-formed but entirely fabricated node ID
    (a random UUID, guaranteed to exist in no graph) passes clean today. This
    is the marketing equivalent of a fabricated financial figure: the gate's
    name promises verification its code does not perform.

    xfail(strict=True), matching H-1's verb-mix-floor pattern: this documents
    a known gap rather than a broken test. The day real graph-existence
    checking is added, this XPASSes and fails CI, forcing a deliberate removal
    of the marker instead of a silent behaviour change going unnoticed.
    """
    fabricated_node_id = f"node-does-not-exist-{uuid4()}"
    with pytest.raises(MarketingUngroundedClaimError):
        assert_claims_grounded(
            [{"graph_node_id": fabricated_node_id, "claim": "Achieved 300% ROI in one quarter"}]
        )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Same gap as test_publish_gate_claim_citing_a_nonexistent_node_is_not_rejected_today: "
        "assert_claims_grounded performs no graph lookup at all, so any syntactically valid "
        "string -- including a freshly-generated random UUID with zero chance of existing -- "
        "is accepted as 'grounded'."
    ),
)
def test_publish_gate_claim_with_random_uuid_as_node_id_is_not_rejected_today() -> None:
    """Second angle on the same finding: a bare random UUID (not even shaped
    like a real node identifier) is just as acceptable to the guard as a real
    one, because the check never leaves the string domain."""
    with pytest.raises(MarketingUngroundedClaimError):
        assert_claims_grounded([{"graph_node_id": str(uuid4()), "claim": "Zero downtime achieved"}])


def _make_publish_mock_engine(artifact_row: dict[str, Any] | None = None) -> MagicMock:
    """Same mock-pool shape as tests/unit/test_marketing_publish.py's
    _make_mock_engine -- reused pattern, not a new fixture."""
    engine = MagicMock()
    conn = AsyncMock()
    conn.fetchrow.return_value = artifact_row
    conn.execute.return_value = "UPDATE 1"

    in_tx = False

    async def _tx_enter(*args: Any, **kwargs: Any) -> Any:
        nonlocal in_tx
        in_tx = True
        return tx

    async def _tx_exit(*args: Any, **kwargs: Any) -> Any:
        nonlocal in_tx
        in_tx = False
        return None

    tx = MagicMock()
    tx.__aenter__ = AsyncMock(side_effect=_tx_enter)
    tx.__aexit__ = AsyncMock(side_effect=_tx_exit)
    conn.transaction = MagicMock(return_value=tx)
    conn.is_in_transaction = MagicMock(side_effect=lambda: in_tx)

    ctx = AsyncMock()
    ctx.__aenter__.return_value = conn
    ctx.__aexit__.return_value = None

    pool = MagicMock()
    pool.acquire.return_value = ctx
    engine.pg_pool = pool
    engine.pool = pool
    return engine


@pytest.mark.asyncio
async def test_publish_gate_never_re_examines_grounding_at_all() -> None:
    """The architectural half of the same finding: do_publish_content enforces
    MK-1 (approval), MK-3 (no sensitive financials) and MK-4 (consent) -- and
    never touches citations or grounding at all. An approved, consented
    artifact publishes successfully regardless of whether its underlying
    claims were ever checked against a real graph node, because the publish
    gate's SQL SELECT does not even fetch a citations column. This is not an
    xfail: it is the gate behaving exactly as coded. The gap is architectural
    (no re-check at the publish boundary), and is reported rather than fixed
    here (fixing it is a shared-core/engine design decision, not a test-lift
    wave's scope)."""
    engine = _make_publish_mock_engine(
        {
            "id": _ARTIFACT_ID,
            "namespace_id": _NAMESPACE_ID,
            "status": "approved",
            "approver": "Jane Doe",
            "body": "Achieved a fabricated 500% ROI per an uncited, ungrounded claim.",
            "title": "Case Study With An Unverifiable Claim",
            "consent": False,
            "is_customer_content": False,
        }
    )

    res = await do_publish_content(
        engine,
        {"namespace_id": _NAMESPACE_ID, "artifact_id": _ARTIFACT_ID, "transport": "manual"},
    )

    assert res["ok"] is True
    assert res["status"] == "published"


# ---------------------------------------------------------------------------
# Group 3 -- assert_positive_nps_only boundary property tests.
# ---------------------------------------------------------------------------


def test_nps_exactly_at_default_threshold_passes() -> None:
    assert_positive_nps_only(9.0)


def test_nps_just_below_default_threshold_rejected() -> None:
    with pytest.raises(MarketingLowHealthTriggerError):
        assert_positive_nps_only(8.99)


def test_nps_well_below_default_threshold_rejected() -> None:
    with pytest.raises(MarketingLowHealthTriggerError):
        assert_positive_nps_only(2.0)


def test_nps_maximum_score_passes() -> None:
    assert_positive_nps_only(10.0)


def test_nps_zero_score_rejected() -> None:
    with pytest.raises(MarketingLowHealthTriggerError):
        assert_positive_nps_only(0.0)


def test_nps_negative_score_rejected() -> None:
    with pytest.raises(MarketingLowHealthTriggerError):
        assert_positive_nps_only(-1.0)


def test_nps_custom_threshold_allows_a_score_the_default_would_reject() -> None:
    assert_positive_nps_only(6.0, threshold=5.0)


def test_nps_custom_threshold_still_rejects_below_the_custom_threshold() -> None:
    with pytest.raises(MarketingLowHealthTriggerError):
        assert_positive_nps_only(4.0, threshold=5.0)


# ---------------------------------------------------------------------------
# Group 4 -- assert_consent_allows_tier property tests over tier combinations.
# ---------------------------------------------------------------------------


def test_consent_none_granted_rejected() -> None:
    with pytest.raises(MarketingConsentMissingError):
        assert_consent_allows_tier("web_retractable", None)


def test_consent_empty_string_granted_rejected() -> None:
    with pytest.raises(MarketingConsentMissingError):
        assert_consent_allows_tier("web_retractable", "")


def test_consent_ai_citable_required_and_granted_passes() -> None:
    assert_consent_allows_tier("ai_citable_irrevocable", "ai_citable_irrevocable")


def test_consent_ai_citable_required_but_weaker_tier_granted_rejected() -> None:
    with pytest.raises(MarketingConsentMissingError):
        assert_consent_allows_tier("ai_citable_irrevocable", "web_retractable")


def test_consent_web_retractable_required_and_web_retractable_granted_passes() -> None:
    assert_consent_allows_tier("web_retractable", "web_retractable")


def test_consent_web_retractable_required_but_stronger_tier_granted_still_passes() -> None:
    """A weaker requirement is satisfied by any recorded consent, including a
    stronger tier -- only the strictest requirement (ai_citable_irrevocable)
    demands an exact match."""
    assert_consent_allows_tier("web_retractable", "ai_citable_irrevocable")


# ---------------------------------------------------------------------------
# Group 5 -- require_marketing_enabled property tests over namespace opt-in.
# ---------------------------------------------------------------------------


def _make_namespace_pool(
    marketing_enabled: bool | None, raise_data_error: bool = False
) -> MagicMock:
    conn = AsyncMock()
    if raise_data_error:
        conn.fetchrow.side_effect = __import__("asyncpg").exceptions.DataError(
            "invalid input syntax for uuid"
        )
    else:
        conn.fetchrow.return_value = {"marketing_enabled": marketing_enabled}

    ctx = AsyncMock()
    ctx.__aenter__.return_value = conn
    ctx.__aexit__.return_value = None

    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool


@pytest.mark.asyncio
async def test_require_marketing_enabled_passes_when_flag_true() -> None:
    pool = _make_namespace_pool(marketing_enabled=True)
    await require_marketing_enabled(pool, _NAMESPACE_ID)


@pytest.mark.asyncio
async def test_require_marketing_enabled_rejects_when_flag_false() -> None:
    pool = _make_namespace_pool(marketing_enabled=False)
    with pytest.raises(MarketingDisabledError):
        await require_marketing_enabled(pool, _NAMESPACE_ID)


@pytest.mark.asyncio
async def test_require_marketing_enabled_rejects_when_flag_missing() -> None:
    pool = _make_namespace_pool(marketing_enabled=None)
    with pytest.raises(MarketingDisabledError):
        await require_marketing_enabled(pool, _NAMESPACE_ID)


@pytest.mark.asyncio
async def test_require_marketing_enabled_rejects_invalid_namespace_uuid() -> None:
    pool = _make_namespace_pool(marketing_enabled=None, raise_data_error=True)
    with pytest.raises(MarketingDisabledError):
        await require_marketing_enabled(pool, "not-a-uuid")


# ---------------------------------------------------------------------------
# Group 6 -- do_publish_content negative property tests over its own explicit
# ValueError/NotImplementedError guards.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_content_rejects_unknown_transport() -> None:
    engine = _make_publish_mock_engine(
        {
            "id": _ARTIFACT_ID,
            "namespace_id": _NAMESPACE_ID,
            "status": "approved",
            "approver": "Jane Doe",
        }
    )
    with pytest.raises(ValueError):
        await do_publish_content(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "artifact_id": _ARTIFACT_ID,
                "transport": "carrier_pigeon",
            },
        )


@pytest.mark.asyncio
async def test_publish_content_rejects_missing_namespace_id() -> None:
    engine = _make_publish_mock_engine()
    with pytest.raises(ValueError):
        await do_publish_content(engine, {"artifact_id": _ARTIFACT_ID})


@pytest.mark.asyncio
async def test_publish_content_rejects_missing_artifact_id() -> None:
    engine = _make_publish_mock_engine()
    with pytest.raises(ValueError):
        await do_publish_content(engine, {"namespace_id": _NAMESPACE_ID})
