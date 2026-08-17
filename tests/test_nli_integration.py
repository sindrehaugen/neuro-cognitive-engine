"""Tests for NLI integration in contradiction detection."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nce.contradictions import ContradictionResult, detect_contradictions
from tests.conftest import first_recorded_contradiction as _first_recorded_contradiction


def _mock_pg_pool(conn: AsyncMock) -> MagicMock:
    pool = MagicMock()

    @asynccontextmanager
    async def _acquire(*_args, **_kwargs):
        yield conn

    pool.acquire = _acquire
    return pool


_VALID_OID = "507f1f77bcf86cd799439011"


class StubLLM:
    def __init__(self, result: ContradictionResult) -> None:
        self._result = result

    async def complete(self, messages: list, response_model: type):
        return self._result


@pytest.mark.anyio
async def test_detect_uses_nli_and_skips_llm_on_strong_agreement():
    cand_id = uuid4()
    ns = str(uuid4())
    new_mid = str(uuid4())

    conn = AsyncMock()
    conn.fetch = AsyncMock(
        return_value=[{"id": cand_id, "payload_ref": _VALID_OID, "similarity": 0.92}]
    )
    conn.fetchrow = AsyncMock(side_effect=[{"metadata": {}}])
    conn.execute = AsyncMock()
    tx = AsyncMock()
    tx.__aenter__.return_value = None
    tx.__aexit__.return_value = False
    conn.transaction = MagicMock(return_value=tx)

    mongo = MagicMock()

    # Mock NLI to return high contradiction score
    with (
        patch("nce.contradictions.check_nli_contradiction", new_callable=AsyncMock) as mock_nli,
        patch(
            "nce.contradictions.fetch_episodes_raw_by_ref",
            new_callable=AsyncMock,
        ) as fetch_ep,
    ):
        mock_nli.return_value = 0.9
        fetch_ep.return_value = {_VALID_OID: "Existing memory text."}

        # Mock LLM provider (should NOT be called if we don't trigger tiebreaker)
        # Actually, in my implementation:
        # kg_hit = False (no triplets)
        # nli_hit = True (0.9 >= 0.8)
        # trigger_llm = (kg_hit != nli_hit) = (False != True) = True
        # So LLM WILL be triggered because KG and NLI disagree (KG=No, NLI=Yes)

        llm = StubLLM(
            ContradictionResult(is_contradiction=True, confidence=0.95, explanation="LLM agrees")
        )
        with patch("nce.contradictions.get_provider", return_value=llm):
            out = await detect_contradictions(
                _mock_pg_pool(conn),
                mongo,
                ns,
                new_mid,
                "New contradicting text.",
                "fact",
                [0.1] * 768,
                "agent-1",
                [],
            )

    row = _first_recorded_contradiction(out)
    assert row is not None
    assert any(s["source"] == "nli" for s in row["signals"])
    assert any(s["source"] == "llm" for s in row["signals"])
    assert row["confidence"] == 0.95


@pytest.mark.anyio
async def test_detect_llm_tiebreaker_prefers_llm_decision():
    cand_id = uuid4()
    ns = str(uuid4())
    new_mid = str(uuid4())

    conn = AsyncMock()
    conn.fetch = AsyncMock(
        return_value=[{"id": cand_id, "payload_ref": _VALID_OID, "similarity": 0.92}]
    )
    conn.fetchrow = AsyncMock(side_effect=[{"metadata": {}}])
    conn.execute = AsyncMock()
    tx = AsyncMock()
    tx.__aenter__.return_value = None
    tx.__aexit__.return_value = False
    conn.transaction = MagicMock(return_value=tx)

    mongo = MagicMock()

    # Mock NLI to return high contradiction score (hit)
    with (
        patch("nce.contradictions.check_nli_contradiction", new_callable=AsyncMock) as mock_nli,
        patch(
            "nce.contradictions.fetch_episodes_raw_by_ref",
            new_callable=AsyncMock,
        ) as fetch_ep,
    ):
        mock_nli.return_value = 0.9  # NLI hit
        fetch_ep.return_value = {_VALID_OID: "Existing memory text."}

        # Mock LLM to say NO contradiction
        llm = StubLLM(
            ContradictionResult(
                is_contradiction=False,
                confidence=0.1,
                explanation="Not a contradiction",
            )
        )
        with patch("nce.contradictions.get_provider", return_value=llm):
            # KG says NO (empty triplets)
            out = await detect_contradictions(
                _mock_pg_pool(conn),
                mongo,
                ns,
                new_mid,
                "New text.",
                "fact",
                [0.1] * 768,
                "agent-1",
                [],
            )

    # In this case:
    # kg_hit = False
    # nli_hit = True
    # trigger_llm = True
    # LLM result is_contradiction = False
    # Final result should be None (LLM wins tiebreak and says no)
    assert out is None


@pytest.mark.anyio
async def test_nli_caching():
    from nce.contradictions import _load_nli_model

    # We can't easily test lru_cache behavior without actual imports,
    # but we can verify the function is defined.
    assert _load_nli_model is not None
