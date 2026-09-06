"""tests/unit/test_sales_signing_delivery_ratchet.py
===================================================
Ratchet tests for Wave Q-3: Deliver Signed Document by Email.

Enforces Sindre's non-negotiable recipient isolation constraints:
1. On signature, the document goes to:
   - The signer
   - The responsible salesperson / project manager / service tech / advisor for the case
   - The account-responsible person
2. Hard constraint: Separate emails, NEVER CC or BCC.
   - Assert each send carries exactly one recipient address.
   - Assert `cc` and `bcc` are strictly empty on every single sent email.
   - Zero address leakage between customer and internal stakeholders.
3. Attached PDF is the deterministic quote document rendered from the frozen baseline.
4. Idempotency: Replaying callbacks does not send duplicate emails.
5. Audit persistence: Delivery events and SHA-256 hash recorded in sales_read_model.
"""

from __future__ import annotations

import hashlib
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from nce.mailer import RecordingFakeMailer, reset_mailer, set_global_mailer
from nce.vertical_modules.sales.render import render_quote_document
from nce.vertical_modules.sales.signing import do_on_signed_callback

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_QUOTE_ID = "quote-q3-2001"
_SESSION_ID = "sess-q3-delivery-001"


@pytest.fixture
def fake_mailer():
    mailer = RecordingFakeMailer()
    set_global_mailer(mailer)
    yield mailer
    reset_mailer()


@pytest.fixture
def baseline_data():
    return {
        "quote_id": _QUOTE_ID,
        "signed_margin_pct": 0.28,
        "signed_total_nok": 185000.0,
        "currency": "NOK",
        "signed_at": "2026-09-07T01:00:00Z",
        "frozen_at_utc": "2026-09-07T01:00:00Z",
    }


def _create_mock_conn(quote_row: dict, account_row: dict | None = None):
    fake_conn = AsyncMock()

    async def _mock_fetchrow(query, *args):
        if "entity = 'quotes'" in query:
            return quote_row
        if "entity = 'accounts'" in query:
            return account_row
        return None

    fake_conn.fetchrow.side_effect = _mock_fetchrow
    fake_conn.execute = AsyncMock(return_value=None)
    fake_conn.fetch = AsyncMock(return_value=[])

    fake_cm = AsyncMock()
    fake_cm.__aenter__.return_value = fake_conn
    fake_cm.__aexit__.return_value = None
    return fake_cm, fake_conn


# ---------------------------------------------------------------------------
# Ratchet Test 1: Separate Emails Delivered to All Three Stakeholders
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_signed_delivers_separate_emails_to_all_three_stakeholders(
    monkeypatch, fake_mailer, baseline_data
):
    """Verify that on signature, separate individual emails are sent to signer, case responsible, and account responsible."""
    engine = MagicMock()
    signer_email = "signer.director@kunde.no"
    case_resp_email = "kari.nordmann.sales@integrator.no"
    account_resp_email = "ola.sjef.am@integrator.no"

    quote_row = {
        "source_id": _QUOTE_ID,
        "name": "Styremøterom AV-oppgradering",
        "source_json": {
            "name": "Styremøterom AV-oppgradering",
            "customer_name": "Kunde AS",
            "margin": 0.28,
            "total_price": 185000.0,
            "salesperson_email": case_resp_email,
            "account_manager_email": account_resp_email,
        },
        "manual": {
            "signing_session_id": _SESSION_ID,
            "signing_status": "pending",
            "signing_method": "manual",
            "signer_name": "Per Hansen",
            "signer_email": signer_email,
        },
    }

    fake_cm, _ = _create_mock_conn(quote_row)
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.scoped_pg_session", lambda _p, _ns: fake_cm
    )
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.get_signed_baseline",
        AsyncMock(return_value=baseline_data),
    )
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.list_bom_lines_for_quote",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.do_freeze_baseline",
        AsyncMock(return_value={"ok": True}),
    )
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.do_convert_signed_quote",
        AsyncMock(return_value={"ok": True, "project_id": f"PROJECT:{_QUOTE_ID.upper()}"}),
    )

    res = await do_on_signed_callback(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "session_id": _SESSION_ID,
        },
    )

    assert res["ok"] is True
    assert res["baseline_frozen"] is True
    assert res["deliveries_sent"] == 3

    # Assert exactly 3 separate messages sent
    assert len(fake_mailer.sent_messages) == 3

    # Check recipient identities
    sent_recipients = [m.to for m in fake_mailer.sent_messages]
    assert signer_email in sent_recipients
    assert case_resp_email in sent_recipients
    assert account_resp_email in sent_recipients


# ---------------------------------------------------------------------------
# Ratchet Test 2: Enforce Zero CC and Zero BCC on ALL Deliveries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_signed_enforces_zero_cc_and_zero_bcc_on_all_deliveries(
    monkeypatch, fake_mailer, baseline_data
):
    """Verify Sindre's hard constraint: every email is single-recipient with cc=() and bcc=()."""
    engine = MagicMock()
    quote_row = {
        "source_id": _QUOTE_ID,
        "name": "Videokonferanse",
        "source_json": {
            "total_price": 50000.0,
            "margin": 0.25,
            "salesperson_email": "sales@company.no",
            "account_responsible_email": "am@company.no",
        },
        "manual": {
            "signing_session_id": _SESSION_ID,
            "signing_status": "pending",
            "signing_method": "manual",
            "signer_email": "customer@client.com",
        },
    }

    fake_cm, _ = _create_mock_conn(quote_row)
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.scoped_pg_session", lambda _p, _ns: fake_cm
    )
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.get_signed_baseline",
        AsyncMock(return_value=baseline_data),
    )
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.list_bom_lines_for_quote", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.do_freeze_baseline",
        AsyncMock(return_value={"ok": True}),
    )
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.do_convert_signed_quote",
        AsyncMock(return_value={"ok": True}),
    )

    await do_on_signed_callback(engine, {"namespace_id": _NAMESPACE_ID, "session_id": _SESSION_ID})

    assert len(fake_mailer.sent_messages) > 0
    for sent_msg in fake_mailer.sent_messages:
        # Strict single-recipient validation
        assert sent_msg.cc == ()
        assert sent_msg.bcc == ()
        assert "," not in sent_msg.to
        assert ";" not in sent_msg.to
        assert " " not in sent_msg.to
        assert "@" in sent_msg.to


# ---------------------------------------------------------------------------
# Ratchet Test 3: Attached PDF Matches Frozen Baseline Deterministically
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_signed_delivers_byte_identical_pdf_attachment(
    monkeypatch, fake_mailer, baseline_data
):
    """Verify that the attachment on delivered emails matches the deterministic baseline PDF."""
    engine = MagicMock()
    signer_email = "director@kunde.no"
    quote_row = {
        "source_id": _QUOTE_ID,
        "name": "AV Rig",
        "source_json": {
            "name": "AV Rig",
            "customer_name": "Kunde AS",
            "total_price": 100000.0,
            "margin": 0.30,
            "salesperson_email": "rep@integrator.no",
        },
        "manual": {
            "signing_session_id": _SESSION_ID,
            "signing_status": "pending",
            "signing_method": "manual",
            "signer_name": "Director",
            "signer_email": signer_email,
        },
    }

    fake_cm, _ = _create_mock_conn(quote_row)
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.scoped_pg_session", lambda _p, _ns: fake_cm
    )
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.get_signed_baseline",
        AsyncMock(return_value=baseline_data),
    )
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.list_bom_lines_for_quote", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.do_freeze_baseline",
        AsyncMock(return_value={"ok": True}),
    )
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.do_convert_signed_quote",
        AsyncMock(return_value={"ok": True}),
    )

    await do_on_signed_callback(engine, {"namespace_id": _NAMESPACE_ID, "session_id": _SESSION_ID})

    # Expected deterministic PDF bytes
    expected_pdf = render_quote_document(
        baseline=baseline_data,
        quote_details={
            "name": "AV Rig",
            "customer_name": "Kunde AS",
            "currency": "NOK",
            "description": None,
        },
        line_items=[],
        signer={"name": "Director", "email": signer_email},
    )
    expected_hash = hashlib.sha256(expected_pdf).hexdigest()

    for sent_msg in fake_mailer.sent_messages:
        assert len(sent_msg.attachments) == 1
        fname, data, mtype = sent_msg.attachments[0]
        assert fname == f"quote_{_QUOTE_ID}_signed.pdf"
        assert mtype == "application/pdf"
        assert data == expected_pdf
        assert hashlib.sha256(data).hexdigest() == expected_hash


# ---------------------------------------------------------------------------
# Ratchet Test 4: Idempotency — Replay Does Not Re-Send Emails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_signed_delivery_idempotent_on_replay(monkeypatch, fake_mailer):
    """Verify that re-triggering do_on_signed_callback on an already signed quote does not send duplicate emails."""
    engine = MagicMock()
    quote_row = {
        "source_id": _QUOTE_ID,
        "name": "AV Rig",
        "source_json": {"total_price": 100000.0, "margin": 0.30},
        "manual": {
            "signing_session_id": _SESSION_ID,
            "signing_status": "signed",  # Already signed
            "signed_delivery": {"delivered_at": "2026-09-07T01:00:00Z", "deliveries": []},
        },
    }

    fake_cm, _ = _create_mock_conn(quote_row)
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.scoped_pg_session", lambda _p, _ns: fake_cm
    )

    res = await do_on_signed_callback(
        engine, {"namespace_id": _NAMESPACE_ID, "session_id": _SESSION_ID}
    )

    assert res["ok"] is True
    assert res["already_processed"] is True
    assert res["deliveries_sent"] == 0
    assert len(fake_mailer.sent_messages) == 0


# ---------------------------------------------------------------------------
# Ratchet Test 5: Audit Persistence in sales_read_model
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_signed_delivery_recorded_in_read_model(monkeypatch, fake_mailer, baseline_data):
    """Verify delivery audit is persisted in sales_read_model.manual."""
    engine = MagicMock()
    quote_row = {
        "source_id": _QUOTE_ID,
        "name": "AV Rig",
        "source_json": {
            "total_price": 80000.0,
            "margin": 0.25,
            "salesperson_email": "sales@integrator.no",
        },
        "manual": {
            "signing_session_id": _SESSION_ID,
            "signing_status": "pending",
            "signing_method": "manual",
            "signer_email": "signer@client.no",
        },
    }

    fake_cm, fake_conn = _create_mock_conn(quote_row)
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.scoped_pg_session", lambda _p, _ns: fake_cm
    )
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.get_signed_baseline",
        AsyncMock(return_value=baseline_data),
    )
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.list_bom_lines_for_quote", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.do_freeze_baseline",
        AsyncMock(return_value={"ok": True}),
    )
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.do_convert_signed_quote",
        AsyncMock(return_value={"ok": True}),
    )

    res = await do_on_signed_callback(
        engine, {"namespace_id": _NAMESPACE_ID, "session_id": _SESSION_ID}
    )

    assert res["deliveries_sent"] == 2
    assert "delivery_records" in res
    assert len(res["delivery_records"]) == 2

    # Verify SQL update carried signed_delivery payload
    update_calls = [
        c for c in fake_conn.execute.call_args_list if "UPDATE sales_read_model" in c[0][0]
    ]
    assert len(update_calls) > 0
    saved_manual = json.loads(update_calls[-1][0][1])
    assert saved_manual["signing_status"] == "signed"
    assert "signed_delivery" in saved_manual
    assert "delivered_at" in saved_manual["signed_delivery"]
    assert len(saved_manual["signed_delivery"]["deliveries"]) == 2


# ---------------------------------------------------------------------------
# Ratchet Test 6: Case Responsible Fallback Roles (PM / Tech / Advisor)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_signed_case_responsible_fallback_roles(monkeypatch, fake_mailer, baseline_data):
    """Verify that project_manager_email, service_tech_email, or advisor_email are selected for case responsible."""
    engine = MagicMock()
    tech_email = "lead.tech@integrator.no"

    quote_row = {
        "source_id": _QUOTE_ID,
        "name": "Service Case",
        "source_json": {
            "total_price": 25000.0,
            "margin": 0.20,
            "service_tech_email": tech_email,  # Technician as case responsible
        },
        "manual": {
            "signing_session_id": _SESSION_ID,
            "signing_status": "pending",
            "signing_method": "manual",
            "signer_email": "customer@client.no",
        },
    }

    fake_cm, _ = _create_mock_conn(quote_row)
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.scoped_pg_session", lambda _p, _ns: fake_cm
    )
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.get_signed_baseline",
        AsyncMock(return_value=baseline_data),
    )
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.list_bom_lines_for_quote", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.do_freeze_baseline",
        AsyncMock(return_value={"ok": True}),
    )
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.do_convert_signed_quote",
        AsyncMock(return_value={"ok": True}),
    )

    await do_on_signed_callback(engine, {"namespace_id": _NAMESPACE_ID, "session_id": _SESSION_ID})

    sent_recipients = [m.to for m in fake_mailer.sent_messages]
    assert tech_email in sent_recipients


# ---------------------------------------------------------------------------
# Ratchet Test 7: Account Responsible Resolved from Account Entity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_signed_account_responsible_from_account_entity(
    monkeypatch, fake_mailer, baseline_data
):
    """Verify that if account_id is present, account responsible is looked up from accounts entity."""
    engine = MagicMock()
    account_id = "acc-5001"
    am_email = "corporate.am@integrator.no"

    quote_row = {
        "source_id": _QUOTE_ID,
        "name": "Corporate AV",
        "source_json": {
            "total_price": 500000.0,
            "margin": 0.35,
            "account_id": account_id,
            "salesperson_email": "sales@integrator.no",
        },
        "manual": {
            "signing_session_id": _SESSION_ID,
            "signing_status": "pending",
            "signing_method": "manual",
            "signer_email": "customer@client.no",
        },
    }

    account_row = {
        "source_id": account_id,
        "source_json": {
            "name": "Storebrand ASA",
            "account_manager_email": am_email,
        },
        "manual": {},
    }

    fake_cm, _ = _create_mock_conn(quote_row, account_row=account_row)
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.scoped_pg_session", lambda _p, _ns: fake_cm
    )
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.get_signed_baseline",
        AsyncMock(return_value=baseline_data),
    )
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.list_bom_lines_for_quote", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.do_freeze_baseline",
        AsyncMock(return_value={"ok": True}),
    )
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.do_convert_signed_quote",
        AsyncMock(return_value={"ok": True}),
    )

    await do_on_signed_callback(engine, {"namespace_id": _NAMESPACE_ID, "session_id": _SESSION_ID})

    sent_recipients = [m.to for m in fake_mailer.sent_messages]
    assert am_email in sent_recipients
    assert len(fake_mailer.sent_messages) == 3
