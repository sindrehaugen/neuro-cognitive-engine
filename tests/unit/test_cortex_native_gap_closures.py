"""
tests/unit/test_cortex_native_gap_closures.py
=============================================
Unit tests for Wave C-1A gap closures:
  1. Source-mode resolver default override (NCE_DEFAULT_SOURCE_MODE) & resolve_native_first
  2. Inventory trigger "record now, invoice later" match evaluation (do_evaluate_match_for_existing_receipt)
  3. Economy recalibration tenant-scoped supplier overrides & resolution
  4. Project tasks BOM delivery transition derivation from warehouse goods receipts
"""

from __future__ import annotations

import json
import os
import uuid
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.source_mode.resolver import (
    _DEFAULT_MODE,
    get_default_source_mode,
    resolve,
    resolve_native_first,
)
from nce.vertical_modules.economy.recalibration import (
    do_recalibrate_supplier,
    resolve_tenant_supplier_override,
)
from nce.vertical_modules.inventory.triggers import (
    do_evaluate_match_for_existing_receipt,
)
from nce.vertical_modules.project.tasks import (
    derive_bom_sync_from_goods_receipt_payload,
)

_NS_UUID_1 = uuid.UUID("11111111-1111-4111-8111-111111111111")
_NS_UUID_2 = uuid.UUID("22222222-2222-4222-8222-222222222222")
_RECEIPT_UUID = uuid.UUID("33333333-3333-4333-8333-333333333333")


def _make_mock_pool(conn: Any = None) -> tuple[MagicMock, MagicMock]:
    if conn is None:
        conn = MagicMock()
    conn.execute = AsyncMock()
    if not hasattr(conn, "fetchrow") or isinstance(conn.fetchrow, MagicMock) and not isinstance(conn.fetchrow, AsyncMock):
        conn.fetchrow = AsyncMock(return_value=None)
    if not hasattr(conn, "fetch") or isinstance(conn.fetch, MagicMock) and not isinstance(conn.fetch, AsyncMock):
        conn.fetch = AsyncMock(return_value=[])
    if not hasattr(conn, "fetchval") or isinstance(conn.fetchval, MagicMock) and not isinstance(conn.fetchval, AsyncMock):
        conn.fetchval = AsyncMock(return_value=None)

    pool = MagicMock()
    ctx = pool.acquire.return_value
    ctx.__aenter__.return_value = conn
    ctx.__aexit__.return_value = False

    tx = conn.transaction.return_value
    tx.__aenter__.return_value = None
    tx.__aexit__.return_value = False
    return pool, conn


# ===========================================================================
# 1. Source-Mode Resolver Gap Closures
# ===========================================================================


def test_get_default_source_mode_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NCE_DEFAULT_SOURCE_MODE", raising=False)
    assert get_default_source_mode() == _DEFAULT_MODE
    assert get_default_source_mode() == "d365"


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        ("nce", "nce"),
        ("NCE", "nce"),
        (" nce  ", "nce"),
        ("both", "both"),
        ("BOTH", "both"),
        (" both ", "both"),
        ("d365", "d365"),
        ("D365", "d365"),
        ("invalid", "d365"),
        ("random_mode", "d365"),
        ("", "d365"),
    ],
)
def test_get_default_source_mode_env_values(
    monkeypatch: pytest.MonkeyPatch, env_value: str, expected: str
) -> None:
    monkeypatch.setenv("NCE_DEFAULT_SOURCE_MODE", env_value)
    assert get_default_source_mode() == expected


@pytest.mark.asyncio
async def test_resolve_uses_default_mode_when_row_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_pool, fake_conn = _make_mock_pool()
    fake_conn.fetchrow = AsyncMock(return_value=None)

    monkeypatch.delenv("NCE_DEFAULT_SOURCE_MODE", raising=False)
    mode = await resolve(fake_pool, engine="d365_sync", function="read_contacts", namespace_id=_NS_UUID_1)
    assert mode == "d365"

    monkeypatch.setenv("NCE_DEFAULT_SOURCE_MODE", "nce")
    mode_native = await resolve(fake_pool, engine="d365_sync", function="read_contacts", namespace_id=_NS_UUID_1)
    assert mode_native == "nce"


@pytest.mark.asyncio
async def test_resolve_native_first_defaults_to_nce_when_row_missing() -> None:
    fake_pool, fake_conn = _make_mock_pool()
    fake_conn.fetchrow = AsyncMock(return_value=None)

    mode = await resolve_native_first(
        fake_pool, engine="native_cortex", function="process_event", namespace_id=_NS_UUID_1
    )
    assert mode == "nce"


@pytest.mark.asyncio
async def test_resolve_native_first_obeys_configured_row() -> None:
    fake_pool, fake_conn = _make_mock_pool()
    fake_conn.fetchrow = AsyncMock(return_value={"mode": "d365"})

    mode = await resolve_native_first(
        fake_pool, engine="native_cortex", function="process_event", namespace_id=_NS_UUID_1
    )
    assert mode == "d365"


@pytest.mark.asyncio
async def test_resolve_native_first_records_heartbeat_on_both() -> None:
    fake_pool, fake_conn = _make_mock_pool()
    fake_conn.fetchrow = AsyncMock(return_value={"mode": "both"})

    with patch("nce.source_mode.divergence.record_comparison_heartbeat", new=AsyncMock()) as hb:
        mode = await resolve_native_first(
            fake_pool, engine="native_cortex", function="process_event", namespace_id=_NS_UUID_1
        )
        assert mode == "both"
        hb.assert_awaited_once_with(fake_pool, namespace_id=_NS_UUID_1, engine="native_cortex")


# ===========================================================================
# 2. Inventory Trigger "Record Now, Invoice Later" Evaluation
# ===========================================================================


@pytest.mark.asyncio
async def test_do_evaluate_match_for_existing_receipt_validates_legs() -> None:
    fake_pool, _ = _make_mock_pool()

    with pytest.raises(ValueError, match="'po' is required"):
        await do_evaluate_match_for_existing_receipt(
            fake_pool, _NS_UUID_1, receipt_id=_RECEIPT_UUID, po={}, invoice={"article_id": "A1", "quantity": 10, "unit_price": 5}
        )

    with pytest.raises(ValueError, match="'invoice' is required"):
        await do_evaluate_match_for_existing_receipt(
            fake_pool, _NS_UUID_1, receipt_id=_RECEIPT_UUID, po={"article_id": "A1", "quantity": 10, "unit_price": 5}, invoice={}
        )

    with pytest.raises(ValueError, match="'po.article_id' is required"):
        await do_evaluate_match_for_existing_receipt(
            fake_pool,
            _NS_UUID_1,
            receipt_id=_RECEIPT_UUID,
            po={"quantity": 10, "unit_price": 5},
            invoice={"article_id": "A1", "quantity": 10, "unit_price": 5},
        )


@pytest.mark.asyncio
async def test_do_evaluate_match_for_existing_receipt_not_found() -> None:
    fake_pool, fake_conn = _make_mock_pool()
    fake_conn.fetchrow = AsyncMock(return_value=None)

    po = {"article_id": "ART-01", "quantity": 10, "unit_price": 100}
    inv = {"article_id": "ART-01", "quantity": 10, "unit_price": 100}

    with pytest.raises(ValueError, match=f"goods_receipt {_RECEIPT_UUID} not found in namespace"):
        await do_evaluate_match_for_existing_receipt(
            fake_pool, _NS_UUID_1, receipt_id=_RECEIPT_UUID, po=po, invoice=inv
        )


@pytest.mark.asyncio
async def test_do_evaluate_match_for_existing_receipt_already_matched_idempotent() -> None:
    stored_verdict = {"tier": "GREEN", "confidence": 100.0, "match_status": "EXACT"}
    fake_pool, fake_conn = _make_mock_pool()
    fake_conn.fetchrow = AsyncMock(
        return_value={
            "id": _RECEIPT_UUID,
            "po_ref": "PO-100",
            "lines": [],
            "match_result": json.dumps(stored_verdict),
        }
    )

    po = {"article_id": "ART-01", "quantity": 10, "unit_price": 100}
    inv = {"article_id": "ART-01", "quantity": 10, "unit_price": 100}
    eval_mock = AsyncMock()

    result = await do_evaluate_match_for_existing_receipt(
        fake_pool, _NS_UUID_1, receipt_id=_RECEIPT_UUID, po=po, invoice=inv, evaluate_match=eval_mock
    )

    assert result["ok"] is True
    assert result["already_matched"] is True
    assert result["match_fired"] is False
    assert result["receipt_id"] == str(_RECEIPT_UUID)
    assert result["po_ref"] == "PO-100"
    assert result["match_result"] == stored_verdict
    eval_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_do_evaluate_match_for_existing_receipt_fires_and_persists() -> None:
    fake_pool, fake_conn = _make_mock_pool()
    # First fetchrow: lookup existing receipt (match_result is None)
    fake_conn.fetchrow = AsyncMock(
        return_value={
            "id": _RECEIPT_UUID,
            "po_ref": "PO-200",
            "lines": [{"sku": "ART-01", "qty": "10"}],
            "match_result": None,
        }
    )
    # fetch for _total_received_against_po
    fake_conn.fetch = AsyncMock(
        return_value=[{"sku": "ART-01", "qty": Decimal("10.000")}]
    )
    # fetchval for _persist_match_result
    fake_conn.fetchval = AsyncMock(return_value=_RECEIPT_UUID)

    po = {"article_id": "ART-01", "quantity": 10, "unit_price": 50.0}
    inv = {"article_id": "ART-01", "quantity": 10, "unit_price": 50.0}
    mock_eval = AsyncMock(return_value={"tier": "GREEN", "confidence": 100.0})

    result = await do_evaluate_match_for_existing_receipt(
        fake_pool,
        _NS_UUID_1,
        receipt_id=_RECEIPT_UUID,
        po=po,
        invoice=inv,
        evaluate_match=mock_eval,
    )

    assert result["ok"] is True
    assert result["already_matched"] is False
    assert result["match_fired"] is True
    assert result["receipt_id"] == str(_RECEIPT_UUID)
    assert result["po_ref"] == "PO-200"
    assert result["match_result"] == {"tier": "GREEN", "confidence": 100.0}
    mock_eval.assert_awaited_once()


# ===========================================================================
# 3. Economy Recalibration: Tenant-Scoped Overrides
# ===========================================================================


def test_resolve_tenant_supplier_override() -> None:
    config = {
        "green": 120,
        "yellow": 70,
        "supplier_overrides": {
            "GLOBAL_SUPPLIER": {"green": 110, "yellow": 65},
            "SHARED_SUPPLIER": {"green": 115, "yellow": 68},
        },
        "tenant_supplier_overrides": {
            str(_NS_UUID_1): {
                "SHARED_SUPPLIER": {"green": 100, "yellow": 60},
                "TENANT_1_ONLY": {"green": 95, "yellow": 55},
            },
            str(_NS_UUID_2): {
                "SHARED_SUPPLIER": {"green": 130, "yellow": 75},
            },
        },
    }

    # Tenant 1 gets its isolated override
    t1_res = resolve_tenant_supplier_override(config, _NS_UUID_1, "SHARED_SUPPLIER")
    assert t1_res == {"green": 100, "yellow": 60}

    # Tenant 2 gets its isolated override
    t2_res = resolve_tenant_supplier_override(config, _NS_UUID_2, "SHARED_SUPPLIER")
    assert t2_res == {"green": 130, "yellow": 75}

    # Fallback to global override for an un-overridden tenant
    other_ns = uuid.uuid4()
    fallback_res = resolve_tenant_supplier_override(config, other_ns, "SHARED_SUPPLIER")
    assert fallback_res == {"green": 115, "yellow": 68}

    # Global supplier found when not in tenant overrides
    global_res = resolve_tenant_supplier_override(config, _NS_UUID_1, "GLOBAL_SUPPLIER")
    assert global_res == {"green": 110, "yellow": 65}

    # Missing supplier returns None
    assert resolve_tenant_supplier_override(config, _NS_UUID_1, "NON_EXISTENT") is None


@pytest.mark.asyncio
async def test_do_recalibrate_supplier_namespace_scoped(tmp_path: Any) -> None:
    cfg_file = tmp_path / "test-thresholds.json"
    init_config = {
        "green": 120,
        "yellow": 70,
        "supplier_overrides": {},
        "tenant_supplier_overrides": {},
    }
    cfg_file.write_text(json.dumps(init_config), encoding="utf-8")

    fake_pool, fake_conn = _make_mock_pool()
    decision_records = [
        {"id": uuid.uuid4(), "tlx_scores": json.dumps({"decision": "accept"})},
        {"id": uuid.uuid4(), "tlx_scores": json.dumps({"decision": "accept"})},
    ]
    fake_conn.fetch = AsyncMock(return_value=decision_records)

    result = await do_recalibrate_supplier(
        fake_pool,
        _NS_UUID_1,
        supplier_orgnr="SUPPLIER-X",
        window_n=2,
        config_path=cfg_file,
        namespace_scoped=True,
    )

    assert result["recalibrated"] is True
    assert result["precision"] == 1.0

    updated = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert str(_NS_UUID_1) in updated.get("tenant_supplier_overrides", {})
    t1_overrides = updated["tenant_supplier_overrides"][str(_NS_UUID_1)]
    assert "SUPPLIER-X" in t1_overrides
    assert t1_overrides["SUPPLIER-X"]["green"] == result["green"]
    assert t1_overrides["SUPPLIER-X"]["yellow"] == result["yellow"]


# ===========================================================================
# 4. Project Tasks: Warehouse Goods Receipt BOM Transition Derivation
# ===========================================================================


def test_derive_bom_sync_from_goods_receipt_payload() -> None:
    payload = {
        "namespace_id": str(_NS_UUID_1),
        "po_ref": "PO-100",
        "lines": [
            {"bom_line_label": "BOM_LINE:Q1:AMP01", "qty_received": 10, "qty_ordered": 10},
            {"sku": "BOM_LINE:Q1:SPK01", "qty_received": 5, "qty_ordered": 10},
            {"sku": "BOM_LINE:Q1:CBL01", "qty_received": 12, "qty_ordered": 10},
            {"sku": "BOM_LINE:Q1:ZERO", "qty_received": 0, "qty_ordered": 5},
            {"sku": "BOM_LINE:Q1:NEG", "qty_received": -2, "qty_ordered": 5},
            {"invalid_line": True},
        ],
    }

    transitions = derive_bom_sync_from_goods_receipt_payload(payload)

    assert len(transitions) == 3

    assert transitions[0]["bom_line_label"] == "BOM_LINE:Q1:AMP01"
    assert transitions[0]["status"] == "DELIVERED"
    assert transitions[0]["qty_received"] == 10.0
    assert transitions[0]["qty_ordered"] == 10.0

    assert transitions[1]["bom_line_label"] == "BOM_LINE:Q1:SPK01"
    assert transitions[1]["status"] == "PARTIALLY_DELIVERED"
    assert transitions[1]["qty_received"] == 5.0
    assert transitions[1]["qty_ordered"] == 10.0

    assert transitions[2]["bom_line_label"] == "BOM_LINE:Q1:CBL01"
    assert transitions[2]["status"] == "DELIVERED"
    assert transitions[2]["qty_received"] == 12.0
    assert transitions[2]["qty_ordered"] == 10.0


def test_derive_bom_sync_empty_or_malformed() -> None:
    assert derive_bom_sync_from_goods_receipt_payload({}) == []
    assert derive_bom_sync_from_goods_receipt_payload({"lines": None}) == []
    assert derive_bom_sync_from_goods_receipt_payload({"lines": "not-a-list"}) == []
    assert derive_bom_sync_from_goods_receipt_payload({"lines": []}) == []
