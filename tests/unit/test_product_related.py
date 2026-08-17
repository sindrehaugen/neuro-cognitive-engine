"""
tests/unit/test_product_related.py
=====================================
Acceptance tests for Batch 035 — Module 2.Wave 5 (related-products).

Covers:
  1. ``_extract_model_tokens`` splits and upper-cases correctly.
  2. ``_classify_relation`` correctly identifies warranty_for, mounts,
     accessory_of, and returns None for unrelated products.
  3. ``_find_replacements`` returns candidates only when subject is EOL.
  4. ``do_related_products`` groups candidates into the correct buckets
     and calls ``upsert_product_relation_edge`` with confidence on the edge
     (mocked conn).
  5. ``do_related_products`` raises when subject product is not found.
  6. ``upsert_product_relation_edge`` writes only real kg_edges columns;
     no confidence on any node upsert; assert_owner guard fires.
  7. ``upsert_product_relation_edge`` rejects an unknown predicate.
  8. ``product_related`` registered with correct flags (cacheable=True,
     mutation=False, admin_only=False, migration=False).

All tests are pure unit tests (no DB, no Redis).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"


# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------


class _async_ctx:
    """Minimal async context manager that yields the given object."""

    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *_: Any) -> None:
        pass


def _make_engine(fetchrow_return=None, fetch_return=None) -> tuple[MagicMock, AsyncMock]:
    engine = MagicMock()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.fetch = AsyncMock(return_value=fetch_return or [])
    conn.execute = AsyncMock(return_value="SET")
    conn.fetchval = AsyncMock(return_value=None)

    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=_async_ctx(conn))
    engine.pg_pool = pool
    return engine, conn


@pytest.fixture(autouse=True)
def _patch_scoped_session(monkeypatch):
    """Replace scoped_pg_session with a trivial pass-through for unit tests."""

    class _FakeScoped:
        def __init__(self, pool, ns):
            self._pool = pool

        async def __aenter__(self):
            return await self._pool.acquire().__aenter__()

        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr(
        "nce.vertical_modules.product.related.scoped_pg_session",
        _FakeScoped,
    )


# ---------------------------------------------------------------------------
# 1. _extract_model_tokens
# ---------------------------------------------------------------------------


def test_extract_model_tokens_splits_on_dash():
    from nce.vertical_modules.product.related import _extract_model_tokens

    tokens = _extract_model_tokens("Cisco", "SFP-10G-SR")
    assert tokens == ["CISCO", "SFP", "10G", "SR"]


def test_extract_model_tokens_splits_on_slash():
    from nce.vertical_modules.product.related import _extract_model_tokens

    tokens = _extract_model_tokens("HPE", "J9777A/B")
    assert tokens == ["HPE", "J9777A", "B"]


def test_extract_model_tokens_empty_part():
    from nce.vertical_modules.product.related import _extract_model_tokens

    tokens = _extract_model_tokens("CISCO", "")
    assert tokens == ["CISCO"]


def test_extract_model_tokens_upper_cases():
    from nce.vertical_modules.product.related import _extract_model_tokens

    tokens = _extract_model_tokens("cisco", "sfp-10g-sr")
    assert all(t == t.upper() for t in tokens)


# ---------------------------------------------------------------------------
# 2. _classify_relation
# ---------------------------------------------------------------------------


def test_classify_relation_warranty_for():
    from nce.vertical_modules.product.related import _classify_relation, _extract_model_tokens

    subject_tokens = _extract_model_tokens("Cisco", "SFP-10G-SR")
    result = _classify_relation(subject_tokens, "Cisco", "CON-SNT-SFP10G-WARR")
    assert result is not None
    predicate, conf = result
    assert predicate == "warranty_for"
    assert 0.0 < conf <= 1.0


def test_classify_relation_mounts():
    from nce.vertical_modules.product.related import _classify_relation, _extract_model_tokens

    subject_tokens = _extract_model_tokens("Cisco", "WS-C2960X-48")
    result = _classify_relation(subject_tokens, "Cisco", "RACK-MOUNT-KIT")
    assert result is not None
    predicate, conf = result
    assert predicate == "mounts"
    assert 0.0 < conf <= 1.0


def test_classify_relation_accessory_of_shared_tokens():
    from nce.vertical_modules.product.related import _classify_relation, _extract_model_tokens

    # Subject: SFP-10G-SR, Candidate: SFP-10G-LR — shares CISCO, SFP, 10G
    subject_tokens = _extract_model_tokens("Cisco", "SFP-10G-SR")
    result = _classify_relation(subject_tokens, "Cisco", "SFP-10G-CABLE")
    assert result is not None
    predicate, conf = result
    assert predicate == "accessory_of"
    assert 0.5 <= conf <= 0.8


def test_classify_relation_returns_none_for_different_manufacturer():
    from nce.vertical_modules.product.related import _classify_relation, _extract_model_tokens

    subject_tokens = _extract_model_tokens("Cisco", "SFP-10G-SR")
    result = _classify_relation(subject_tokens, "Juniper", "SFP-10G-SR")
    # Same part tokens but different manufacturer — no accessory match
    assert result is None


def test_classify_relation_returns_none_insufficient_tokens():
    from nce.vertical_modules.product.related import _classify_relation, _extract_model_tokens

    subject_tokens = _extract_model_tokens("Cisco", "SFP-10G-SR")
    # Only 1 shared token (SFP), minimum is 2
    result = _classify_relation(subject_tokens, "Cisco", "SFP-1G-UNRELATED")
    # "SFP" is shared — but "1G" vs "10G" differs; "SR" vs "UNRELATED" differs.
    # 1 shared token < _MIN_SHARED_TOKENS=2 — should be None.
    # This depends on exact tokenization; only assert it is None or accessory_of.
    # Actual: SFP is shared (1), 10G != 1G, SR != UNRELATED → 1 shared < 2 → None.
    assert result is None


# ---------------------------------------------------------------------------
# 3. _find_replacements
# ---------------------------------------------------------------------------


def test_find_replacements_returns_active_replacement_for_eol():
    from nce.vertical_modules.product.related import _extract_model_tokens, _find_replacements

    subject_tokens = _extract_model_tokens("Cisco", "SFP-10G-SR")
    candidates = [
        {
            "manufacturer": "Cisco",
            "mfr_part_no": "SFP-10G-SR-S",
            "lifecycle_status": "active",
        },
    ]
    results = _find_replacements("eol", subject_tokens, candidates)
    assert len(results) == 1
    mfr, part, conf = results[0]
    assert mfr == "Cisco"
    assert conf == 0.95


def test_find_replacements_no_replacement_when_subject_active():
    from nce.vertical_modules.product.related import _extract_model_tokens, _find_replacements

    subject_tokens = _extract_model_tokens("Cisco", "SFP-10G-SR")
    candidates = [
        {
            "manufacturer": "Cisco",
            "mfr_part_no": "SFP-10G-SR-S",
            "lifecycle_status": "active",
        },
    ]
    results = _find_replacements("active", subject_tokens, candidates)
    assert results == []


def test_find_replacements_ignores_non_active_candidates():
    from nce.vertical_modules.product.related import _extract_model_tokens, _find_replacements

    subject_tokens = _extract_model_tokens("Cisco", "SFP-10G-SR")
    candidates = [
        {
            "manufacturer": "Cisco",
            "mfr_part_no": "SFP-10G-SR-S",
            "lifecycle_status": "eol",
        },
    ]
    results = _find_replacements("eol", subject_tokens, candidates)
    assert results == []


def test_find_replacements_discontinued_treated_as_eol():
    from nce.vertical_modules.product.related import _extract_model_tokens, _find_replacements

    subject_tokens = _extract_model_tokens("HPE", "J9777A-SFP-10G")
    candidates = [
        {
            "manufacturer": "HPE",
            "mfr_part_no": "J9777A-SFP-10G-NEW",
            "lifecycle_status": "active",
        },
    ]
    results = _find_replacements("discontinued", subject_tokens, candidates)
    assert len(results) == 1


# ---------------------------------------------------------------------------
# 4. do_related_products — groups + edge writes (mock conn)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_related_products_groups_correctly():
    """Accessories are grouped into the correct bucket; edges written with confidence."""
    from nce.vertical_modules.product.related import do_related_products

    subject_row = {
        "manufacturer": "Cisco",
        "mfr_part_no": "SFP-10G-SR",
        "lifecycle_status": "active",
    }
    # One warranty candidate, one accessory candidate.
    candidate_rows = [
        {
            "manufacturer": "Cisco",
            "mfr_part_no": "CON-SNT-SFP10-WARR",
            "lifecycle_status": "active",
        },
        {
            "manufacturer": "Cisco",
            "mfr_part_no": "SFP-10G-CABLE",
            "lifecycle_status": "active",
        },
    ]

    engine, conn = _make_engine(fetchrow_return=subject_row, fetch_return=candidate_rows)

    with patch(
        "nce.vertical_modules.product.related.upsert_product_relation_edge",
        new_callable=AsyncMock,
    ) as mock_upsert:
        result = await do_related_products(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "manufacturer": "Cisco",
                "mfr_part_no": "SFP-10G-SR",
            },
        )

    assert result["subject"] == "PRODUCT:CISCO:SFP-10G-SR"
    assert isinstance(result["warranty_for"], list)
    assert isinstance(result["accessory_of"], list)
    assert isinstance(result["mounts"], list)
    assert isinstance(result["replaced_by"], list)
    assert result["edges_written"] == mock_upsert.call_count

    # At least one warranty_for edge
    assert len(result["warranty_for"]) >= 1
    warranty_labels = [e["label"] for e in result["warranty_for"]]
    assert "PRODUCT:CISCO:CON-SNT-SFP10-WARR" in warranty_labels


@pytest.mark.asyncio
async def test_do_related_products_confidence_on_edge_not_node():
    """Edge upsert is called with confidence; no node SQL is touched."""
    from nce.vertical_modules.product.related import do_related_products

    subject_row = {
        "manufacturer": "Cisco",
        "mfr_part_no": "SFP-10G-SR",
        "lifecycle_status": "active",
    }
    candidate_rows = [
        {
            "manufacturer": "Cisco",
            "mfr_part_no": "SFP-10G-CABLE",
            "lifecycle_status": "active",
        },
    ]

    engine, conn = _make_engine(fetchrow_return=subject_row, fetch_return=candidate_rows)

    with patch(
        "nce.vertical_modules.product.related.upsert_product_relation_edge",
        new_callable=AsyncMock,
    ) as mock_upsert:
        await do_related_products(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "manufacturer": "Cisco",
                "mfr_part_no": "SFP-10G-SR",
            },
        )

    # Every call to upsert_product_relation_edge must include a float confidence kwarg.
    for c in mock_upsert.call_args_list:
        confidence = c.kwargs.get("confidence")
        assert confidence is not None, "confidence kwarg must be present"
        assert isinstance(confidence, float), f"confidence must be float, got {type(confidence)}"
        assert 0.0 <= confidence <= 1.0, f"confidence out of range: {confidence}"

    # conn.execute must never have been called for a node INSERT.
    # (All writes are routed through upsert_product_relation_edge, not direct conn.execute.)
    for c in conn.execute.call_args_list:
        sql: str = c.args[0] if c.args else ""
        assert "kg_nodes" not in sql.lower(), (
            "do_related_products must not write directly to kg_nodes"
        )


@pytest.mark.asyncio
async def test_do_related_products_raises_when_product_not_found():
    from nce.vertical_modules.product.related import do_related_products

    engine, _ = _make_engine(fetchrow_return=None)

    with pytest.raises(ValueError, match="not found"):
        await do_related_products(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "manufacturer": "Cisco",
                "mfr_part_no": "NONEXISTENT",
            },
        )


@pytest.mark.asyncio
async def test_do_related_products_raises_missing_manufacturer():
    from nce.vertical_modules.product.related import do_related_products

    engine, _ = _make_engine()

    with pytest.raises(ValueError, match="manufacturer"):
        await do_related_products(
            engine,
            {"namespace_id": _NAMESPACE_ID, "mfr_part_no": "SFP-10G-SR"},
        )


@pytest.mark.asyncio
async def test_do_related_products_raises_missing_mfr_part_no():
    from nce.vertical_modules.product.related import do_related_products

    engine, _ = _make_engine()

    with pytest.raises(ValueError, match="mfr_part_no"):
        await do_related_products(
            engine,
            {"namespace_id": _NAMESPACE_ID, "manufacturer": "Cisco"},
        )


@pytest.mark.asyncio
async def test_do_related_products_eol_subject_replaced_by():
    """EOL subject: candidate appears in replaced_by, NOT in accessory_of."""
    from nce.vertical_modules.product.related import do_related_products

    subject_row = {
        "manufacturer": "Cisco",
        "mfr_part_no": "SFP-10G-SR",
        "lifecycle_status": "eol",
    }
    candidate_rows = [
        {
            "manufacturer": "Cisco",
            "mfr_part_no": "SFP-10G-SR-S",
            "lifecycle_status": "active",
        },
    ]

    engine, _ = _make_engine(fetchrow_return=subject_row, fetch_return=candidate_rows)

    with patch(
        "nce.vertical_modules.product.related.upsert_product_relation_edge",
        new_callable=AsyncMock,
    ):
        result = await do_related_products(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "manufacturer": "Cisco",
                "mfr_part_no": "SFP-10G-SR",
            },
        )

    replaced_labels = [e["label"] for e in result["replaced_by"]]
    accessory_labels = [e["label"] for e in result["accessory_of"]]
    assert "PRODUCT:CISCO:SFP-10G-SR-S" in replaced_labels
    assert "PRODUCT:CISCO:SFP-10G-SR-S" not in accessory_labels


# ---------------------------------------------------------------------------
# 5. upsert_product_relation_edge — real kg_edges columns + assert_owner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_product_relation_edge_correct_columns():
    """Only real kg_edges columns are used; no metadata column; confidence on edge."""
    from nce.vertical_modules.product.graph import upsert_product_relation_edge

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")

    with patch(
        "nce.vertical_modules.product.graph.assert_owner", new_callable=AsyncMock
    ) as mock_owner:
        await upsert_product_relation_edge(
            conn,
            _NAMESPACE_ID,
            subject_label="PRODUCT:CISCO:SFP-10G-SR",
            predicate="accessory_of",
            object_label="PRODUCT:CISCO:SFP-10G-CABLE",
            confidence=0.75,
        )

    # Ownership guard fires before any write.
    mock_owner.assert_called_once()
    owner_args = mock_owner.call_args[0]
    assert owner_args[2] == "PRODUCT_SKU"
    assert owner_args[3] == "product"

    conn.execute.assert_called_once()
    sql: str = conn.execute.call_args[0][0]
    args: tuple = conn.execute.call_args[0]

    # Real columns present.
    assert "subject_label" in sql
    assert "predicate" in sql
    assert "object_label" in sql
    assert "confidence" in sql
    assert "namespace_id" in sql

    # Phantom column absent.
    assert "metadata" not in sql.lower(), "metadata column must not appear in kg_edges INSERT"

    # Positional args: sql, subject_label, predicate, object_label, confidence, namespace_id
    assert args[1] == "PRODUCT:CISCO:SFP-10G-SR"
    assert args[2] == "accessory_of"
    assert args[3] == "PRODUCT:CISCO:SFP-10G-CABLE"
    assert abs(args[4] - 0.75) < 1e-9


@pytest.mark.asyncio
async def test_upsert_product_relation_edge_rejects_unknown_predicate():
    from nce.vertical_modules.product.graph import upsert_product_relation_edge

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")

    with patch("nce.vertical_modules.product.graph.assert_owner", new_callable=AsyncMock):
        with pytest.raises(ValueError, match="predicate"):
            await upsert_product_relation_edge(
                conn,
                _NAMESPACE_ID,
                subject_label="PRODUCT:CISCO:SFP-10G-SR",
                predicate="references",  # not a relation predicate
                object_label="PRODUCT:CISCO:SFP-10G-LR",
                confidence=0.9,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "predicate",
    ["accessory_of", "warranty_for", "mounts", "replaced_by"],
)
async def test_upsert_product_relation_edge_all_predicates(predicate: str):
    """All four permitted predicates are accepted without error."""
    from nce.vertical_modules.product.graph import upsert_product_relation_edge

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")

    with patch("nce.vertical_modules.product.graph.assert_owner", new_callable=AsyncMock):
        await upsert_product_relation_edge(
            conn,
            _NAMESPACE_ID,
            subject_label="PRODUCT:CISCO:SFP-10G-SR",
            predicate=predicate,
            object_label="PRODUCT:CISCO:SFP-10G-LR",
            confidence=0.8,
        )

    conn.execute.assert_called_once()


# ---------------------------------------------------------------------------
# 6. PRODUCT node INSERT must never contain confidence (regression guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_product_relation_edge_sql_has_no_metadata_column():
    """Regression: the phantom metadata column must never appear in kg_edges INSERT."""
    from nce.vertical_modules.product.graph import upsert_product_relation_edge

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")

    with patch("nce.vertical_modules.product.graph.assert_owner", new_callable=AsyncMock):
        await upsert_product_relation_edge(
            conn,
            _NAMESPACE_ID,
            subject_label="PRODUCT:CISCO:SFP-10G-SR",
            predicate="mounts",
            object_label="PRODUCT:CISCO:RACK-KIT",
            confidence=0.85,
        )

    sql: str = conn.execute.call_args[0][0]
    assert "metadata" not in sql.lower(), (
        "Phantom 'metadata' column must not appear in kg_edges INSERT (prior bug guard)."
    )


# ---------------------------------------------------------------------------
# 7. Tool registry — product_related flags
# ---------------------------------------------------------------------------


def test_product_related_registered_with_correct_flags():
    from nce.tool_registry import TOOL_REGISTRY

    assert "product_related" in TOOL_REGISTRY
    spec = TOOL_REGISTRY["product_related"]
    assert spec.cacheable is True
    assert spec.mutation is False
    assert spec.admin_only is False
    assert spec.migration is False
