"""Unit tests for nce.signing_service — ManualTransport (c7-sign-transport, B029).

All tests are plain unit tests: no DB, no network, no credentials required.
Tests verify:
  1. ManualTransport signs end-to-end with no credentials.
  2. SHA-256 fingerprint is stable (same bytes → same hex digest).
  3. ``on_signed`` callback fires and is recorded in the audit trail.
  4. ``on_declined`` callback fires and is recorded in the audit trail.
  5. ``SignTransport`` Protocol is satisfied (isinstance check).
  6. Unknown session raises KeyError on callback.
"""

from __future__ import annotations

import pytest

from nce.signing_service import (
    ManualTransport,
    SignTransport,
    get_audit_trail,
    sha256_fingerprint,
)
from nce.signing_service.manual import (
    clear_audit_trail,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_audit() -> None:
    """Reset the module-level audit trail before each test."""
    clear_audit_trail()


@pytest.fixture()
def transport() -> ManualTransport:
    return ManualTransport()


_DOC = b"Contract version 1.0 - lorem ipsum dolor sit amet."
_SIGNER = {"name": "Ola Nordmann", "email": "ola@example.com"}


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------


def test_sha256_fingerprint_is_64_hex_chars() -> None:
    fp = sha256_fingerprint(_DOC)
    assert len(fp) == 64
    assert all(c in "0123456789abcdef" for c in fp)


def test_sha256_fingerprint_is_stable() -> None:
    """Same bytes must always yield the same fingerprint."""
    fp1 = sha256_fingerprint(_DOC)
    fp2 = sha256_fingerprint(_DOC)
    assert fp1 == fp2


def test_sha256_fingerprint_differs_for_different_content() -> None:
    fp1 = sha256_fingerprint(_DOC)
    fp2 = sha256_fingerprint(_DOC + b" (amended)")
    assert fp1 != fp2


# ---------------------------------------------------------------------------
# SignTransport Protocol
# ---------------------------------------------------------------------------


def test_manual_transport_satisfies_sign_transport_protocol(
    transport: ManualTransport,
) -> None:
    """ManualTransport is structurally compatible with SignTransport."""
    assert isinstance(transport, SignTransport)


# ---------------------------------------------------------------------------
# request_signature
# ---------------------------------------------------------------------------


def test_request_signature_returns_session_with_required_keys(
    transport: ManualTransport,
) -> None:
    session = transport.request_signature(_DOC, _SIGNER, "manual")
    for key in ("session_id", "status", "fingerprint"):
        assert key in session, f"Missing required key: {key}"


def test_request_signature_status_is_pending(transport: ManualTransport) -> None:
    session = transport.request_signature(_DOC, _SIGNER, "manual")
    assert session["status"] == "pending"


def test_request_signature_fingerprint_matches_doc(transport: ManualTransport) -> None:
    session = transport.request_signature(_DOC, _SIGNER, "manual")
    assert session["fingerprint"] == sha256_fingerprint(_DOC)


def test_request_signature_records_audit_entry(transport: ManualTransport) -> None:
    session = transport.request_signature(_DOC, _SIGNER, "manual")
    trail = get_audit_trail()
    assert len(trail) == 1
    entry = trail[0]
    assert entry["event"] == "requested"
    assert entry["session_id"] == session["session_id"]


# ---------------------------------------------------------------------------
# on_signed callback
# ---------------------------------------------------------------------------


def test_on_signed_transitions_status_to_signed(transport: ManualTransport) -> None:
    session = transport.request_signature(_DOC, _SIGNER, "manual")
    updated = transport.on_signed(session["session_id"], {"provider": "manual"})
    assert updated["status"] == "signed"


def test_on_signed_preserves_fingerprint(transport: ManualTransport) -> None:
    session = transport.request_signature(_DOC, _SIGNER, "manual")
    updated = transport.on_signed(session["session_id"], {})
    assert updated["fingerprint"] == session["fingerprint"]


def test_on_signed_records_audit_entry(transport: ManualTransport) -> None:
    session = transport.request_signature(_DOC, _SIGNER, "manual")
    transport.on_signed(session["session_id"], {"hook": "signed"})
    trail = get_audit_trail()
    # Should have "requested" + "signed"
    assert len(trail) == 2
    signed_entry = trail[1]
    assert signed_entry["event"] == "signed"
    assert signed_entry["session_id"] == session["session_id"]


# ---------------------------------------------------------------------------
# on_declined callback
# ---------------------------------------------------------------------------


def test_on_declined_transitions_status_to_declined(transport: ManualTransport) -> None:
    session = transport.request_signature(_DOC, _SIGNER, "manual")
    updated = transport.on_declined(session["session_id"], {"reason": "user_rejected"})
    assert updated["status"] == "declined"


def test_on_declined_preserves_fingerprint(transport: ManualTransport) -> None:
    session = transport.request_signature(_DOC, _SIGNER, "manual")
    updated = transport.on_declined(session["session_id"], {})
    assert updated["fingerprint"] == session["fingerprint"]


def test_on_declined_records_audit_entry(transport: ManualTransport) -> None:
    session = transport.request_signature(_DOC, _SIGNER, "manual")
    transport.on_declined(session["session_id"], {"hook": "declined"})
    trail = get_audit_trail()
    assert len(trail) == 2
    declined_entry = trail[1]
    assert declined_entry["event"] == "declined"
    assert declined_entry["session_id"] == session["session_id"]


# ---------------------------------------------------------------------------
# End-to-end: full sign flow
# ---------------------------------------------------------------------------


def test_end_to_end_sign_flow_no_credentials(transport: ManualTransport) -> None:
    """ManualTransport completes a full sign cycle with no external credentials."""
    session = transport.request_signature(_DOC, _SIGNER, "manual")
    assert session["status"] == "pending"

    signed = transport.on_signed(session["session_id"], {})
    assert signed["status"] == "signed"
    assert signed["fingerprint"] == sha256_fingerprint(_DOC)

    trail = get_audit_trail()
    events = [e["event"] for e in trail]
    assert events == ["requested", "signed"]


def test_end_to_end_decline_flow_no_credentials(transport: ManualTransport) -> None:
    """ManualTransport completes a full decline cycle with no external credentials."""
    session = transport.request_signature(_DOC, _SIGNER, "manual")
    declined = transport.on_declined(session["session_id"], {"reason": "too_late"})
    assert declined["status"] == "declined"

    trail = get_audit_trail()
    events = [e["event"] for e in trail]
    assert events == ["requested", "declined"]


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_on_signed_unknown_session_raises_key_error(transport: ManualTransport) -> None:
    with pytest.raises(KeyError, match="no-such-id"):
        transport.on_signed("no-such-id", {})


def test_on_declined_unknown_session_raises_key_error(transport: ManualTransport) -> None:
    with pytest.raises(KeyError, match="no-such-id"):
        transport.on_declined("no-such-id", {})
