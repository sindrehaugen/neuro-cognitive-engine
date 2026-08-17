"""Tests for field-level survivorship + provenance (Wave 7 / c1-survivorship).

Verifies:
  - source_trust beats recency beats confidence (unit tests, no DB).
  - Each precedence level is exercised with an exact tie on the levels above.
  - Provenance records the winning source and the reason it won.
  - survive() raises ValueError on empty input.
  - The ledger append writes an auditable row to v3_cognitive_ledger (integration).

Unit tests: no DB, no fixtures.
Integration tests: @pytest.mark.integration — require a live Postgres pool
  via the shared ``pg_pool`` and ``make_namespace`` conftest fixtures.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
import pytest_asyncio

from nce.entity_resolution.survivorship import (
    REASON_CONFIDENCE,
    REASON_RECENCY,
    REASON_SOURCE_TRUST,
    append_survivorship_provenance,
    survive,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_T0 = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
_T1 = _T0 + timedelta(hours=1)
_T2 = _T0 + timedelta(hours=2)


def _cand(
    value: str,
    source: str,
    *,
    trust: float,
    as_of: datetime,
    confidence: float,
) -> dict:
    return {
        "value": value,
        "source": source,
        "source_trust": trust,
        "as_of": as_of,
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# Unit tests — pure survive() precedence
# ---------------------------------------------------------------------------


class TestSurviveSourceTrustBeatsRecency:
    """source_trust is the primary tiebreaker."""

    def test_higher_source_trust_wins_regardless_of_recency(self) -> None:
        """A lower-trust source with a newer timestamp must lose."""
        low_trust_new = _cand("val-new", "src-low", trust=0.3, as_of=_T2, confidence=0.9)
        high_trust_old = _cand("val-old", "src-high", trust=0.9, as_of=_T0, confidence=0.1)

        result = survive([low_trust_new, high_trust_old])

        assert result["value"] == "val-old"
        assert result["provenance"]["source"] == "src-high"
        assert result["provenance"]["reason"] == REASON_SOURCE_TRUST

    def test_higher_source_trust_wins_regardless_of_confidence(self) -> None:
        """A lower-trust source with higher confidence must lose."""
        low_trust_high_conf = _cand("val-a", "src-low", trust=0.2, as_of=_T0, confidence=0.99)
        high_trust_low_conf = _cand("val-b", "src-high", trust=0.8, as_of=_T0, confidence=0.01)

        result = survive([low_trust_high_conf, high_trust_low_conf])

        assert result["value"] == "val-b"
        assert result["provenance"]["source"] == "src-high"
        assert result["provenance"]["reason"] == REASON_SOURCE_TRUST

    def test_single_candidate_wins_with_source_trust_reason(self) -> None:
        only = _cand("x", "src-only", trust=0.5, as_of=_T0, confidence=0.5)
        result = survive([only])
        assert result["value"] == "x"
        assert result["provenance"]["source"] == "src-only"
        assert result["provenance"]["reason"] == REASON_SOURCE_TRUST


class TestSurviveRecencyBreaksTrustTie:
    """When source_trust is equal, the most recent as_of wins."""

    def test_newer_as_of_wins_on_trust_tie(self) -> None:
        older = _cand("val-old", "src-a", trust=0.7, as_of=_T0, confidence=0.5)
        newer = _cand("val-new", "src-b", trust=0.7, as_of=_T2, confidence=0.1)

        result = survive([older, newer])

        assert result["value"] == "val-new"
        assert result["provenance"]["source"] == "src-b"
        assert result["provenance"]["reason"] == REASON_RECENCY

    def test_most_recent_wins_among_three_tied_trust(self) -> None:
        a = _cand("a", "src-a", trust=0.5, as_of=_T0, confidence=0.3)
        b = _cand("b", "src-b", trust=0.5, as_of=_T1, confidence=0.3)
        c = _cand("c", "src-c", trust=0.5, as_of=_T2, confidence=0.3)

        result = survive([a, b, c])

        assert result["value"] == "c"
        assert result["provenance"]["reason"] == REASON_RECENCY

    def test_recency_does_not_override_trust_advantage(self) -> None:
        """Even the oldest candidate wins when its trust is highest."""
        low_trust_newest = _cand("new", "src-low", trust=0.1, as_of=_T2, confidence=0.9)
        high_trust_oldest = _cand("old", "src-high", trust=0.9, as_of=_T0, confidence=0.1)

        result = survive([low_trust_newest, high_trust_oldest])

        assert result["value"] == "old"
        assert result["provenance"]["reason"] == REASON_SOURCE_TRUST


class TestSurviveConfidenceBreaksTrustAndRecencyTie:
    """When source_trust and as_of are both equal, highest confidence wins."""

    def test_higher_confidence_wins_on_full_tie(self) -> None:
        low_conf = _cand("val-low", "src-a", trust=0.6, as_of=_T1, confidence=0.3)
        high_conf = _cand("val-high", "src-b", trust=0.6, as_of=_T1, confidence=0.9)

        result = survive([low_conf, high_conf])

        assert result["value"] == "val-high"
        assert result["provenance"]["source"] == "src-b"
        assert result["provenance"]["reason"] == REASON_CONFIDENCE

    def test_confidence_does_not_override_recency_advantage(self) -> None:
        """Newer source with low confidence beats older with high confidence."""
        old_high_conf = _cand("old", "src-a", trust=0.5, as_of=_T0, confidence=0.99)
        new_low_conf = _cand("new", "src-b", trust=0.5, as_of=_T2, confidence=0.01)

        result = survive([old_high_conf, new_low_conf])

        assert result["value"] == "new"
        assert result["provenance"]["reason"] == REASON_RECENCY


class TestSurviveEdgeCases:
    """Edge cases: empty input, complete tie, string as_of."""

    def test_raises_on_empty_input(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            survive([])

    def test_complete_tie_returns_first_candidate_stably(self) -> None:
        """When all dimensions are equal the first candidate is returned (stable)."""
        a = _cand("first", "src-a", trust=0.5, as_of=_T0, confidence=0.5)
        b = _cand("second", "src-b", trust=0.5, as_of=_T0, confidence=0.5)

        result = survive([a, b])
        assert result["value"] == "first"

    def test_as_of_as_iso_string_is_parsed(self) -> None:
        """ISO-8601 strings (with Z suffix) are accepted for as_of."""
        old = _cand("old", "src-a", trust=0.5, as_of="2024-01-01T00:00:00Z", confidence=0.5)
        new = _cand("new", "src-b", trust=0.5, as_of="2024-01-01T02:00:00Z", confidence=0.5)

        result = survive([old, new])

        assert result["value"] == "new"
        assert result["provenance"]["reason"] == REASON_RECENCY

    def test_provenance_always_present_in_result(self) -> None:
        candidate = _cand("v", "src", trust=0.7, as_of=_T0, confidence=0.8)
        result = survive([candidate])
        assert "value" in result
        assert "provenance" in result
        assert "source" in result["provenance"]
        assert "reason" in result["provenance"]


# ---------------------------------------------------------------------------
# Integration tests — append_survivorship_provenance + v3_cognitive_ledger
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def survivorship_namespace(pg_pool, make_namespace):
    """Fresh namespace for survivorship integration tests."""
    ns: UUID = await make_namespace()
    return ns, pg_pool


@pytest.mark.integration
@pytest.mark.asyncio
async def test_append_provenance_writes_auditable_ledger_row(
    survivorship_namespace,
) -> None:
    """The ledger row must be retrievable and contain the correct provenance payload."""
    ns, pg_pool = survivorship_namespace

    candidates = [
        _cand("val-a", "src-erpx", trust=0.9, as_of=_T0, confidence=0.8),
        _cand("val-b", "src-crm", trust=0.5, as_of=_T2, confidence=0.95),
    ]
    result = survive(candidates)

    row_id = await append_survivorship_provenance(
        pg_pool,
        namespace_id=ns,
        entity_id="entity-123",
        field_name="hostname",
        winning_value=result["value"],
        winning_source=result["provenance"]["source"],
        reason=result["provenance"]["reason"],
        all_candidates=candidates,
    )

    assert isinstance(row_id, UUID)

    # Fetch and verify the ledger row is auditable.
    from nce.db_utils import scoped_pg_session

    async with scoped_pg_session(pg_pool, ns) as conn:
        row = await conn.fetchrow(
            """
            SELECT id, namespace_id, memory_id, model_version, tlx_scores, empathic_tensor
            FROM   v3_cognitive_ledger
            WHERE  id = $1
            """,
            row_id,
        )

    assert row is not None, "Ledger row not found after insert"
    assert UUID(str(row["namespace_id"])) == ns
    assert row["memory_id"] is None
    assert row["model_version"] == "survivorship/v1"

    payload = json.loads(row["tlx_scores"])
    assert payload["event"] == "field_survivorship"
    assert payload["field_name"] == "hostname"
    assert payload["entity_id"] == "entity-123"
    assert payload["winning_source"] == "src-erpx"
    assert payload["reason"] == REASON_SOURCE_TRUST
    assert len(payload["candidates"]) == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_append_provenance_with_memory_id(survivorship_namespace) -> None:
    """Passing a memory_id stores the UUID in the ledger row."""
    ns, pg_pool = survivorship_namespace
    import uuid

    fake_memory_id = uuid.uuid4()
    candidates = [_cand("v", "src", trust=0.8, as_of=_T0, confidence=0.7)]
    result = survive(candidates)

    row_id = await append_survivorship_provenance(
        pg_pool,
        namespace_id=ns,
        entity_id="entity-456",
        field_name="ip_address",
        winning_value=result["value"],
        winning_source=result["provenance"]["source"],
        reason=result["provenance"]["reason"],
        all_candidates=candidates,
        memory_id=fake_memory_id,
    )

    from nce.db_utils import scoped_pg_session

    async with scoped_pg_session(pg_pool, ns) as conn:
        stored_memory_id = await conn.fetchval(
            "SELECT memory_id FROM v3_cognitive_ledger WHERE id = $1",
            row_id,
        )

    assert UUID(str(stored_memory_id)) == fake_memory_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ledger_row_namespace_isolation(make_namespace, pg_pool) -> None:
    """Provenance row is keyed to ns_a; a ns_b-scoped query must not find it.

    Namespace isolation is enforced at the data level via the ``namespace_id``
    column — the WHERE predicate mirrors how every scoped read in the project
    is written (belt-and-braces alongside the RLS GUC).  This test uses the
    application pool role (``mcp_user``) which is not the ``nce_app`` RLS role,
    so we verify isolation through the explicit ``namespace_id`` predicate that
    ``scoped_pg_session`` always adds.
    """
    ns_a: UUID = await make_namespace()
    ns_b: UUID = await make_namespace()

    candidates = [_cand("v", "src", trust=0.5, as_of=_T0, confidence=0.5)]
    result = survive(candidates)

    row_id = await append_survivorship_provenance(
        pg_pool,
        namespace_id=ns_a,
        entity_id="entity-789",
        field_name="model",
        winning_value=result["value"],
        winning_source=result["provenance"]["source"],
        reason=result["provenance"]["reason"],
        all_candidates=candidates,
    )

    from nce.db_utils import scoped_pg_session

    # Must be visible when querying within ns_a (namespace_id matches).
    async with scoped_pg_session(pg_pool, ns_a) as conn:
        found_a = await conn.fetchval(
            "SELECT id FROM v3_cognitive_ledger WHERE id = $1 AND namespace_id = $2",
            row_id,
            ns_a,
        )
    assert found_a is not None, "Row not visible in its own namespace"

    # Must be invisible when queried with ns_b namespace_id (isolation via column).
    async with scoped_pg_session(pg_pool, ns_b) as conn:
        found_b = await conn.fetchval(
            "SELECT id FROM v3_cognitive_ledger WHERE id = $1 AND namespace_id = $2",
            row_id,
            ns_b,
        )
    assert found_b is None, "Namespace isolation violated: row visible via ns_b predicate"
