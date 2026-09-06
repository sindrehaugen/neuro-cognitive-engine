"""tests/test_mailer.py
====================
Contract and unit tests for NCE mailer interface (Wave Q-3):
- Recipient validation: exactly one recipient email allowed in 'to'
- Strict CC / BCC prohibition (raises ValueError immediately)
- RecordingFakeMailer behavior and message inspection
- SMTPMailer configuration resolution via secret_env() and *_FILE secrets
- get_mailer() resolution precedence
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.mailer import (
    EmailDelivery,
    Mailer,
    RecordingFakeMailer,
    SMTPMailer,
    get_mailer,
    reset_mailer,
    set_global_mailer,
)

# ---------------------------------------------------------------------------
# 1. EmailDelivery Validation & Hard CC/BCC Prohibition
# ---------------------------------------------------------------------------


def test_email_delivery_valid():
    msg = EmailDelivery(
        to="customer@example.com",
        subject="Signed Quote",
        body_text="Your signed quote is attached.",
    )
    assert msg.to == "customer@example.com"
    assert msg.subject == "Signed Quote"
    assert msg.body_text == "Your signed quote is attached."
    assert msg.cc == ()
    assert msg.bcc == ()
    assert msg.attachments == ()


def test_email_delivery_rejects_empty_to():
    with pytest.raises(ValueError, match="Recipient 'to' must be a non-empty string"):
        EmailDelivery(to="", subject="Sub", body_text="Body")

    with pytest.raises(ValueError, match="Recipient 'to' must not be blank"):
        EmailDelivery(to="   ", subject="Sub", body_text="Body")


@pytest.mark.parametrize(
    "invalid_to",
    [
        "alice@example.com, bob@example.com",
        "alice@example.com; bob@example.com",
        "alice@example.com bob@example.com",
        "alice@example.com\nbob@example.com",
        "not-an-email",
        "user@",
        "@example.com",
    ],
)
def test_email_delivery_rejects_multiple_or_invalid_recipients(invalid_to: str):
    with pytest.raises(ValueError):
        EmailDelivery(to=invalid_to, subject="Sub", body_text="Body")


def test_email_delivery_rejects_cc():
    with pytest.raises(ValueError, match="CC is strictly forbidden"):
        EmailDelivery(
            to="alice@example.com",
            subject="Sub",
            body_text="Body",
            cc=("bob@example.com",),
        )


def test_email_delivery_rejects_bcc():
    with pytest.raises(ValueError, match="BCC is strictly forbidden"):
        EmailDelivery(
            to="alice@example.com",
            subject="Sub",
            body_text="Body",
            bcc=("manager@example.com",),
        )


def test_email_delivery_with_attachments():
    pdf_bytes = b"%PDF-1.4 mock signed pdf document"
    msg = EmailDelivery(
        to="signer@example.com",
        subject="Quote Q-1001",
        body_text="Please find signed PDF.",
        attachments=[("quote_1001_signed.pdf", pdf_bytes, "application/pdf")],
    )
    assert len(msg.attachments) == 1
    fname, data, mtype = msg.attachments[0]
    assert fname == "quote_1001_signed.pdf"
    assert data == pdf_bytes
    assert mtype == "application/pdf"


# ---------------------------------------------------------------------------
# 2. RecordingFakeMailer Contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recording_fake_mailer_captures_messages():
    fake = RecordingFakeMailer()
    assert isinstance(fake, Mailer)
    assert len(fake.sent_messages) == 0

    msg1 = EmailDelivery(to="alice@example.com", subject="Msg 1", body_text="Body 1")
    msg2 = EmailDelivery(to="bob@example.com", subject="Msg 2", body_text="Body 2")

    res1 = await fake.send(msg1)
    res2 = await fake.send(msg2)

    assert res1 is True
    assert res2 is True
    assert len(fake.sent_messages) == 2
    assert fake.sent_messages[0].to == "alice@example.com"
    assert fake.sent_messages[1].to == "bob@example.com"

    fake.clear()
    assert len(fake.sent_messages) == 0


@pytest.mark.asyncio
async def test_recording_fake_mailer_simulates_error():
    fake = RecordingFakeMailer()
    fake.send_error = ConnectionError("SMTP server down")

    msg = EmailDelivery(to="alice@example.com", subject="Sub", body_text="Body")
    with pytest.raises(ConnectionError, match="SMTP server down"):
        await fake.send(msg)


# ---------------------------------------------------------------------------
# 3. SMTPMailer Configuration & secret_env() Handling
# ---------------------------------------------------------------------------


def test_smtp_mailer_config_defaults(monkeypatch):
    monkeypatch.delenv("NCE_SMTP_HOST", raising=False)
    monkeypatch.delenv("NCE_SMTP_PORT", raising=False)
    monkeypatch.delenv("NCE_SMTP_USER", raising=False)
    monkeypatch.delenv("NCE_SMTP_PASS", raising=False)
    monkeypatch.delenv("NCE_SMTP_FROM", raising=False)
    monkeypatch.delenv("NCE_SMTP_STARTTLS", raising=False)

    mailer = SMTPMailer()
    cfg = mailer._get_config()
    assert cfg["host"] == ""
    assert cfg["port"] == 587
    assert cfg["user"] is None
    assert cfg["password"] is None
    assert cfg["from_address"] == "noreply@nce.internal"
    assert cfg["start_tls"] is True


def test_smtp_mailer_config_from_secret_env(monkeypatch, tmp_path):
    pass_file = tmp_path / "smtp_pass.txt"
    pass_file.write_text("secret-smtp-password\n", encoding="utf-8")

    monkeypatch.setenv("NCE_SMTP_HOST", "mail.internal.company.no")
    monkeypatch.setenv("NCE_SMTP_PORT", "465")
    monkeypatch.setenv("NCE_SMTP_USER", "mailer-user")
    monkeypatch.setenv("NCE_SMTP_PASS_FILE", str(pass_file))
    monkeypatch.setenv("NCE_SMTP_FROM", "contracts@company.no")
    monkeypatch.setenv("NCE_SMTP_STARTTLS", "false")

    mailer = SMTPMailer()
    cfg = mailer._get_config()
    assert cfg["host"] == "mail.internal.company.no"
    assert cfg["port"] == 465
    assert cfg["user"] == "mailer-user"
    assert cfg["password"] == "secret-smtp-password"
    assert cfg["from_address"] == "contracts@company.no"
    assert cfg["start_tls"] is False


@pytest.mark.asyncio
async def test_smtp_mailer_unconfigured_host_raises():
    mailer = SMTPMailer(host="")
    msg = EmailDelivery(to="alice@example.com", subject="Sub", body_text="Body")
    with pytest.raises(RuntimeError, match="SMTP host is not configured"):
        await mailer.send(msg)


@pytest.mark.asyncio
async def test_smtp_mailer_dispatches_via_aiosmtplib():
    mailer = SMTPMailer(
        host="smtp.relay.local",
        port=587,
        user="test-user",
        password="test-pass",
        from_address="sender@relay.local",
        start_tls=True,
    )
    msg = EmailDelivery(
        to="client@example.com",
        subject="Signed Order",
        body_text="Plain text body",
        body_html="<p>HTML body</p>",
        attachments=[("order.pdf", b"%PDF-1.4 signed", "application/pdf")],
    )

    with patch("aiosmtplib.send", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = ({}, "250 OK")
        res = await mailer.send(msg)
        assert res is True
        mock_send.assert_called_once()
        sent_msg = mock_send.call_args[0][0]
        assert sent_msg["From"] == "sender@relay.local"
        assert sent_msg["To"] == "client@example.com"
        assert sent_msg["Subject"] == "Signed Order"
        assert mock_send.call_args[1]["hostname"] == "smtp.relay.local"
        assert mock_send.call_args[1]["port"] == 587
        assert mock_send.call_args[1]["username"] == "test-user"
        assert mock_send.call_args[1]["password"] == "test-pass"
        assert mock_send.call_args[1]["start_tls"] is True


# ---------------------------------------------------------------------------
# 4. get_mailer() Precedence & Mock Isolation (Kaizen K-11)
# ---------------------------------------------------------------------------


def test_get_mailer_precedence(monkeypatch):
    reset_mailer()
    try:
        # 1. Environment toggle NCE_MAILER_FAKE
        monkeypatch.setenv("NCE_MAILER_FAKE", "1")
        m = get_mailer()
        assert isinstance(m, RecordingFakeMailer)

        # 2. Overridden global mailer
        custom_fake = RecordingFakeMailer()
        set_global_mailer(custom_fake)
        assert get_mailer() is custom_fake

        # 3. Engine with explicit mailer
        class EngineWithMailer:
            def __init__(self):
                self.mailer = RecordingFakeMailer()

        eng = EngineWithMailer()
        assert get_mailer(eng) is eng.mailer

        # 4. Engine is a MagicMock (Kaizen K-11 guard)
        # Unconfigured MagicMock has no explicit 'mailer' in __dict__
        mock_eng = MagicMock()
        # Should NOT use mock_eng.mailer auto-mock; should use global mailer
        assert get_mailer(mock_eng) is custom_fake
    finally:
        reset_mailer()
