"""Unit tests for nce.signing_service — EmailCodeTransport (Wave Q-2).

Tests:
  1. Structural compatibility with SignTransport protocol.
  2. Request signature generates secure 6-digit OTP, salt, hash, and dispatches code.
  3. Plaintext OTP code is NEVER persisted in internal sessions or returned in public session dicts.
  4. Correct OTP verification transitions status to 'signed' and sets security_tier = 'email_code'.
  5. Constant-time digest comparison rejects invalid codes with attempt count decrement.
  6. Exceeding max attempts locks session permanently.
  7. Expiration (15 minute TTL) refuses verification.
  8. Single-use and idempotency contract.
  9. Decline event handling.
  10. get_signing_transport factory dispatch and fail-loud unbuilt vendor refusal.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nce.signing_service import (
    REQUIRED_SESSION_KEYS,
    EmailCodeTransport,
    ManualTransport,
    SignTransport,
    UnimplementedTransportError,
    clear_dispatched_otp_codes,
    get_dispatched_otp_codes,
    get_signing_transport,
    reset_signing_transports,
    sha256_fingerprint,
)
from nce.signing_service.email_code import clear_audit_trail, get_audit_trail

_SAMPLE_DOC = b"Contract Agreement: 10x MeetingBar A30 at 350,000 NOK"
_SIGNER = {"name": "Ola Nordmann", "email": "ola.nordmann@acme.no"}


@pytest.fixture(autouse=True)
def _isolate_email_code_tests():
    clear_dispatched_otp_codes()
    clear_audit_trail()
    reset_signing_transports()
    yield
    clear_dispatched_otp_codes()
    clear_audit_trail()
    reset_signing_transports()


# ---------------------------------------------------------------------------
# 1. Protocol & Factory Compliance
# ---------------------------------------------------------------------------


def test_email_code_transport_is_sign_transport():
    """EmailCodeTransport is structurally compatible with SignTransport."""
    transport = EmailCodeTransport()
    assert isinstance(transport, SignTransport)


def test_get_signing_transport_dispatch():
    """get_signing_transport dispatches to correct transport singletons."""
    manual = get_signing_transport("manual")
    assert isinstance(manual, ManualTransport)
    assert get_signing_transport("manual") is manual

    email_t = get_signing_transport("email_code")
    assert isinstance(email_t, EmailCodeTransport)
    assert get_signing_transport("email_code") is email_t


def test_get_signing_transport_refuses_unbuilt_vendors():
    """Unbuilt vendor integrations fail loud with UnimplementedTransportError (NotImplementedError & ValueError)."""
    for vendor in ("oneflow", "criipto", "signicat"):
        with pytest.raises(UnimplementedTransportError) as exc_info:
            get_signing_transport(vendor)
        assert issubclass(UnimplementedTransportError, NotImplementedError)
        assert issubclass(UnimplementedTransportError, ValueError)
        assert vendor in str(exc_info.value)


def test_get_signing_transport_invalid_method():
    """Invalid transport methods raise ValueError."""
    with pytest.raises(ValueError, match="Invalid signing method"):
        get_signing_transport("telepathy")


# ---------------------------------------------------------------------------
# 2. request_signature Contract & Security Guarantees
# ---------------------------------------------------------------------------


def test_request_signature_success():
    """request_signature creates a pending session and dispatches a 6-digit OTP code."""
    transport = EmailCodeTransport()
    session = transport.request_signature(_SAMPLE_DOC, _SIGNER, "email_code")

    # Contract keys
    assert REQUIRED_SESSION_KEYS <= session.keys()
    assert session["status"] == "pending"
    assert session["method"] == "email_code"
    assert session["security_tier"] == "email_code"
    assert session["fingerprint"] == sha256_fingerprint(_SAMPLE_DOC)
    assert session["signer"]["email"] == "ola.nordmann@acme.no"
    assert "expires_at_utc" in session

    # Code was dispatched
    dispatched = get_dispatched_otp_codes()
    assert len(dispatched) == 1
    msg = dispatched[0]
    assert msg["session_id"] == session["session_id"]
    assert msg["email"] == "ola.nordmann@acme.no"
    assert len(msg["code"]) == 6
    assert msg["code"].isdigit()


def test_plaintext_code_never_persisted_or_returned():
    """Plaintext OTP code is NEVER in returned session or in stored session state."""
    transport = EmailCodeTransport()
    session = transport.request_signature(_SAMPLE_DOC, _SIGNER, "email_code")

    # Not in public session
    assert "code" not in session
    assert "code_hash" not in session
    assert "code_salt" not in session

    # Not in internal session
    internal = transport._sessions[session["session_id"]]
    assert "code" not in internal
    assert "code_hash" in internal
    assert "code_salt" in internal
    assert len(internal["code_salt"]) == 32  # 16 bytes hex


def test_request_signature_validates_email_and_method():
    """request_signature rejects non-email_code methods and missing/invalid emails."""
    transport = EmailCodeTransport()

    with pytest.raises(ValueError, match="expected method 'email_code'"):
        transport.request_signature(_SAMPLE_DOC, _SIGNER, "manual")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="Signer email is required"):
        transport.request_signature(_SAMPLE_DOC, {"name": "No Email"}, "email_code")

    with pytest.raises(ValueError, match="Signer email is required"):
        transport.request_signature(_SAMPLE_DOC, {"email": "invalid-email"}, "email_code")


# ---------------------------------------------------------------------------
# 3. on_signed OTP Verification & Integrity
# ---------------------------------------------------------------------------


def test_on_signed_happy_path():
    """on_signed verifies correct OTP code and transitions status to signed."""
    transport = EmailCodeTransport()
    session = transport.request_signature(_SAMPLE_DOC, _SIGNER, "email_code")
    session_id = session["session_id"]

    dispatched = get_dispatched_otp_codes()
    otp_code = dispatched[0]["code"]

    signed_session = transport.on_signed(session_id, {"code": otp_code})
    assert signed_session["status"] == "signed"
    assert signed_session["security_tier"] == "email_code"
    assert signed_session["fingerprint"] == session["fingerprint"]
    assert REQUIRED_SESSION_KEYS <= signed_session.keys()

    # Idempotent replay
    replayed = transport.on_signed(session_id, {"code": otp_code})
    assert replayed["status"] == "signed"
    assert replayed["session_id"] == session_id


def test_on_signed_wrong_code_decrements_attempts():
    """Wrong candidate code increments failure count and reports remaining attempts."""
    transport = EmailCodeTransport(max_attempts=3)
    session = transport.request_signature(_SAMPLE_DOC, _SIGNER, "email_code")
    session_id = session["session_id"]

    # Attempt 1: wrong code
    with pytest.raises(ValueError, match="Invalid verification code. 2 attempt\\(s\\) remaining."):
        transport.on_signed(session_id, {"code": "000000"})

    # Attempt 2: empty code
    with pytest.raises(
        ValueError, match="Verification code is required. 1 attempt\\(s\\) remaining."
    ):
        transport.on_signed(session_id, {"code": ""})

    # Attempt 3: wrong code -> locks session
    with pytest.raises(
        ValueError,
        match="Maximum verification attempts \\(3\\) exceeded; signing session is locked",
    ):
        transport.on_signed(session_id, {"code": "111111"})

    # Locked session rejects further attempts even with correct code
    dispatched = get_dispatched_otp_codes()
    correct_code = dispatched[0]["code"]
    with pytest.raises(ValueError, match="locked due to exceeded verification attempts"):
        transport.on_signed(session_id, {"code": correct_code})


def test_on_signed_expired_session_refused():
    """Verification code older than TTL is refused and session transitions to expired."""
    transport = EmailCodeTransport(ttl_seconds=900)
    session = transport.request_signature(_SAMPLE_DOC, _SIGNER, "email_code")
    session_id = session["session_id"]

    # Artificially age session
    internal = transport._sessions[session_id]
    internal["expires_at_utc"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()

    dispatched = get_dispatched_otp_codes()
    correct_code = dispatched[0]["code"]

    with pytest.raises(ValueError, match="has expired \\(15 minute validity exceeded\\)"):
        transport.on_signed(session_id, {"code": correct_code})

    assert internal["status"] == "expired"


def test_on_signed_unknown_session():
    """Unknown session_id raises KeyError."""
    transport = EmailCodeTransport()
    with pytest.raises(KeyError, match="Unknown signing session"):
        transport.on_signed("non-existent-session", {"code": "123456"})


# ---------------------------------------------------------------------------
# 4. on_declined Contract
# ---------------------------------------------------------------------------


def test_on_declined_success():
    """on_declined transitions session to declined and records audit."""
    transport = EmailCodeTransport()
    session = transport.request_signature(_SAMPLE_DOC, _SIGNER, "email_code")
    session_id = session["session_id"]

    declined = transport.on_declined(session_id, {"reason": "Customer changed mind"})
    assert declined["status"] == "declined"
    assert declined["fingerprint"] == session["fingerprint"]

    # Cannot sign declined session
    dispatched = get_dispatched_otp_codes()
    with pytest.raises(ValueError, match="is declined; cannot sign"):
        transport.on_signed(session_id, {"code": dispatched[0]["code"]})


# ---------------------------------------------------------------------------
# 5. Audit Trail Inspection
# ---------------------------------------------------------------------------


def test_audit_trail_recorded():
    """Audit events are correctly recorded throughout the session lifecycle."""
    transport = EmailCodeTransport()
    session = transport.request_signature(_SAMPLE_DOC, _SIGNER, "email_code")
    session_id = session["session_id"]

    dispatched = get_dispatched_otp_codes()
    correct_code = dispatched[0]["code"]

    # Wrong attempt
    with pytest.raises(ValueError):
        transport.on_signed(session_id, {"code": "999999"})

    # Valid sign
    transport.on_signed(session_id, {"code": correct_code})

    trail = get_audit_trail()
    events = [entry["event"] for entry in trail if entry["session_id"] == session_id]
    assert events == ["requested", "failed_attempt", "signed"]
