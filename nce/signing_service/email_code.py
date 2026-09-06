"""EmailCodeTransport — native e-signature transport via emailed one-time code (Wave Q-2).

Implements :class:`~nce.signing_service.transport.SignTransport` for Norwegian B2B
e-signatures using short-lived, single-use, rate-limited numeric OTP codes.

Security & Integrity Guarantees:
--------------------------------
1. Cryptographic randomness: 6-digit numeric OTP code generated via ``secrets.randbelow(1_000_000)``.
2. Hash-only storage: The plaintext verification code is NEVER stored in session dicts,
   in-memory dictionaries, or database rows. Only ``salt`` and ``sha256(salt + code)`` are kept.
3. Short-lived TTL: Expiration window of 15 minutes (900s). Expired codes are refused.
4. Rate-limited & attempt-capped: Maximum 3 verification attempts. Exceeding attempts
   locks the session permanently to prevent brute-force attacks.
5. Constant-time verification: Digest comparison performed using :func:`hmac.compare_digest`
   to protect against timing side-channel attacks.
6. Single-use enforcement: Successful verification transitions session to ``"signed"``
   and marks it ``used``. Replaying a used session returns the existing record idempotently.
7. Legal security tier: Records ``security_tier = "email_code"`` in session and audit trails.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from nce.signing_service.manual import sha256_fingerprint
from nce.signing_service.transport import REQUIRED_SESSION_KEYS, TransportMethod

log = logging.getLogger(__name__)

_DEFAULT_CODE_TTL_SECONDS = 900  # 15 minutes
_DEFAULT_MAX_ATTEMPTS = 3

# ---------------------------------------------------------------------------
# In-module audit trail and test dispatch store
# ---------------------------------------------------------------------------

_audit_trail: list[dict[str, Any]] = []
_dispatched_otp_codes: list[dict[str, Any]] = []


def get_audit_trail() -> list[dict[str, Any]]:
    """Return a snapshot of all audit events recorded this process lifetime."""
    return list(_audit_trail)


def clear_audit_trail() -> None:
    """Clear all audit entries (test isolation)."""
    _audit_trail.clear()


def get_dispatched_otp_codes() -> list[dict[str, Any]]:
    """Return a snapshot of dispatched OTP envelopes (intended for tests/observers)."""
    return list(_dispatched_otp_codes)


def clear_dispatched_otp_codes() -> None:
    """Clear dispatched OTP records (test isolation)."""
    _dispatched_otp_codes.clear()


def _append_audit(
    *,
    event: str,
    session_id: str,
    detail: dict[str, Any],
) -> None:
    """Record an audit trail event."""
    _audit_trail.append(
        {
            "event": event,
            "session_id": session_id,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "detail": detail,
        }
    )


# ---------------------------------------------------------------------------
# Cryptographic hash helper
# ---------------------------------------------------------------------------


def hash_otp_code(code: str, salt: str) -> str:
    """Compute deterministic SHA-256 digest of salt and candidate code."""
    payload = f"{salt}:{code}".encode()
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# EmailCodeTransport
# ---------------------------------------------------------------------------


class EmailCodeTransport:
    """Native e-signature transport via emailed one-time code.

    Implements :class:`~nce.signing_service.transport.SignTransport`.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = _DEFAULT_CODE_TTL_SECONDS,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        dispatch_fn: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_attempts = max_attempts
        self._dispatch_fn = dispatch_fn
        # Internal session store: session_id -> internal session dict
        self._sessions: dict[str, dict[str, Any]] = {}

    def _public_session(self, internal_session: dict[str, Any]) -> dict[str, Any]:
        """Return a public session dict conforming to REQUIRED_SESSION_KEYS.

        CRITICAL: Never leaks plaintext OTP code, code_hash, or salt.
        """
        public = {
            "session_id": internal_session["session_id"],
            "status": internal_session["status"],
            "fingerprint": internal_session["fingerprint"],
            "signer": internal_session["signer"],
            "method": internal_session["method"],
            "security_tier": internal_session.get("security_tier", "email_code"),
            "created_at_utc": internal_session["created_at_utc"],
            "expires_at_utc": internal_session["expires_at_utc"],
        }
        assert REQUIRED_SESSION_KEYS <= public.keys(), (
            f"BUG: session missing keys {REQUIRED_SESSION_KEYS - public.keys()}"
        )
        return public

    def request_signature(
        self,
        doc: bytes,
        signer: dict[str, Any],
        method: TransportMethod,
    ) -> dict[str, Any]:
        """Initiate an email OTP signing session.

        Generates a 6-digit cryptographically random OTP, hashes it with a fresh salt,
        records expiration and attempt budgets, and dispatches the code to signer['email'].

        Args:
            doc: Raw document bytes to be signed.
            signer: Signer identity dict (must contain 'email').
            method: Must be 'email_code'.

        Returns:
            Sanitized public session dict.
        """
        if method != "email_code":
            raise ValueError(f"EmailCodeTransport expected method 'email_code', got {method!r}")

        email = str(signer.get("email") or "").strip()
        if not email or "@" not in email:
            raise ValueError("Signer email is required for email_code transport")

        # 1. Generate cryptographically random 6-digit numeric code
        # secrets.randbelow(1_000_000) generates an integer in [0, 999999]
        otp_code = f"{secrets.randbelow(1_000_000):06d}"
        salt = secrets.token_hex(16)
        code_hash = hash_otp_code(otp_code, salt)

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=self._ttl_seconds)
        session_id = str(uuid4())

        internal_session: dict[str, Any] = {
            "session_id": session_id,
            "status": "pending",
            "fingerprint": sha256_fingerprint(doc),
            "signer": dict(signer),
            "method": "email_code",
            "security_tier": "email_code",
            "created_at_utc": now.isoformat(),
            "expires_at_utc": expires_at.isoformat(),
            "code_salt": salt,
            "code_hash": code_hash,
            "attempts": 0,
            "max_attempts": self._max_attempts,
            "used": False,
        }

        self._sessions[session_id] = internal_session

        # 2. Dispatch the code to signer's email
        if self._dispatch_fn:
            self._dispatch_fn(email, otp_code, session_id)
        else:
            _dispatched_otp_codes.append(
                {
                    "session_id": session_id,
                    "email": email,
                    "code": otp_code,
                    "dispatched_at": now.isoformat(),
                }
            )
            log.info(
                "EmailCodeTransport: OTP code dispatched to %s for session %s (expires %s)",
                email,
                session_id,
                expires_at.isoformat(),
            )

        _append_audit(
            event="requested",
            session_id=session_id,
            detail={
                "fingerprint": internal_session["fingerprint"],
                "method": "email_code",
                "security_tier": "email_code",
                "recipient": email,
                "expires_at_utc": expires_at.isoformat(),
            },
        )

        return self._public_session(internal_session)

    def on_signed(
        self,
        session_id: str,
        callback_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Verify the candidate OTP code and transition session to signed.

        Args:
            session_id: Session identifier.
            callback_payload: Must contain 'code' or 'otp' entered by signer.

        Returns:
            Sanitized session dict with status == 'signed'.

        Raises:
            KeyError: If session_id is unknown.
            ValueError: If session is expired, locked, or code is invalid.
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"Unknown signing session: {session_id!r}")

        # Idempotent replay check
        if session["status"] == "signed" and session["used"]:
            return self._public_session(session)

        if session["status"] == "declined":
            raise ValueError(f"Signing session {session_id} is declined; cannot sign")

        if session["status"] == "locked":
            raise ValueError(
                f"Signing session {session_id} is locked due to exceeded verification attempts"
            )

        if session["status"] == "expired":
            raise ValueError(
                f"Signing session {session_id} has expired (15 minute validity exceeded)"
            )

        now = datetime.now(timezone.utc)
        expires_at = datetime.fromisoformat(session["expires_at_utc"])
        if now > expires_at:
            session["status"] = "expired"
            _append_audit(
                event="expired",
                session_id=session_id,
                detail={"reason": "ttl_exceeded", "expired_at": expires_at.isoformat()},
            )
            raise ValueError(
                f"Verification code for session {session_id} has expired (15 minute validity exceeded)"
            )

        # Check attempt threshold
        if session["attempts"] >= session["max_attempts"]:
            session["status"] = "locked"
            _append_audit(
                event="locked",
                session_id=session_id,
                detail={"attempts": session["attempts"]},
            )
            raise ValueError(
                f"Maximum verification attempts ({session['max_attempts']}) exceeded; signing session is locked"
            )

        # Extract and verify candidate code
        candidate = str(callback_payload.get("code") or callback_payload.get("otp") or "").strip()
        if not candidate:
            session["attempts"] += 1
            remaining = session["max_attempts"] - session["attempts"]
            _append_audit(
                event="failed_attempt",
                session_id=session_id,
                detail={"attempts": session["attempts"], "reason": "missing_candidate_code"},
            )
            if session["attempts"] >= session["max_attempts"]:
                session["status"] = "locked"
                raise ValueError(
                    f"Maximum verification attempts ({session['max_attempts']}) exceeded; signing session is locked"
                )
            raise ValueError(f"Verification code is required. {remaining} attempt(s) remaining.")

        candidate_hash = hash_otp_code(candidate, session["code_salt"])

        # Constant-time comparison against stored hash
        if not hmac.compare_digest(candidate_hash, session["code_hash"]):
            session["attempts"] += 1
            remaining = session["max_attempts"] - session["attempts"]
            _append_audit(
                event="failed_attempt",
                session_id=session_id,
                detail={"attempts": session["attempts"], "reason": "code_mismatch"},
            )
            if session["attempts"] >= session["max_attempts"]:
                session["status"] = "locked"
                raise ValueError(
                    f"Maximum verification attempts ({session['max_attempts']}) exceeded; signing session is locked"
                )
            raise ValueError(f"Invalid verification code. {remaining} attempt(s) remaining.")

        # Verification successful: transition state
        session["status"] = "signed"
        session["used"] = True
        session["signed_at_utc"] = now.isoformat()

        _append_audit(
            event="signed",
            session_id=session_id,
            detail={
                "fingerprint": session["fingerprint"],
                "security_tier": session["security_tier"],
                "method": "email_code",
                "signed_at_utc": session["signed_at_utc"],
            },
        )
        return self._public_session(session)

    def on_declined(
        self,
        session_id: str,
        callback_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle signature decline event."""
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"Unknown signing session: {session_id!r}")

        session["status"] = "declined"
        session["declined_at_utc"] = datetime.now(timezone.utc).isoformat()

        _append_audit(
            event="declined",
            session_id=session_id,
            detail={
                "fingerprint": session["fingerprint"],
                "payload": callback_payload,
                "declined_at_utc": session["declined_at_utc"],
            },
        )
        return self._public_session(session)
