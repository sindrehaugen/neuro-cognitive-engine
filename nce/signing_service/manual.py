"""ManualTransport — credential-free signing transport for local / test use.

Ships as the first concrete implementation of :class:`~nce.signing_service.transport.SignTransport`.
Requires NO vendor credentials: it completes signing locally, making it safe
to use in dev, CI, and unit tests without external service accounts.

Design notes (uncle-bob-craft)
-------------------------------
- SRP: each private function has exactly one job (fingerprint, session factory,
  audit append, state transition).
- Dependency rule: this module depends on the ``transport`` abstraction module
  (inward) — nothing in ``transport`` knows about ``manual``.
- No rigidity: the session dict is open-typed; new fields do not break callers.
- Audit trail: an in-process list of :class:`AuditEntry` dicts.  Out-of-scope
  note from the wave spec: a persistent audit table would be a migration and is
  explicitly out of scope; callers can drain :func:`get_audit_trail` to store
  entries elsewhere.
- SHA-256 fingerprint: deterministic (same bytes → same hex digest).  Used as a
  tamper-evident handle on the document — NOT a cryptographic signing operation;
  that is the vendor's job.

SCOPE LOCK: this module does NOT freeze any ``SIGNED_BASELINE``.  That is the
caller's responsibility (e.g. Sales engine on ``on_signed``).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from nce.signing_service.transport import REQUIRED_SESSION_KEYS, TransportMethod

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-module audit trail
# ---------------------------------------------------------------------------

# Each entry is a plain dict so callers can serialise without importing a type.
# Keys: event, session_id, timestamp_utc, detail.
_audit_trail: list[dict[str, Any]] = []


def get_audit_trail() -> list[dict[str, Any]]:
    """Return a snapshot of all signing events recorded this process lifetime.

    Returns:
        List of audit entry dicts (copies, safe to mutate).
    """
    return list(_audit_trail)


def clear_audit_trail() -> None:
    """Remove all entries from the in-process audit trail.

    Intended for test isolation only — do not call in production code.
    """
    _audit_trail.clear()


def _append_audit(
    *,
    event: str,
    session_id: str,
    detail: dict[str, Any],
) -> None:
    """Append one entry to the in-module audit trail.

    Single responsibility: build the standard envelope and push it.
    """
    _audit_trail.append(
        {
            "event": event,
            "session_id": session_id,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "detail": detail,
        }
    )


# ---------------------------------------------------------------------------
# SHA-256 fingerprint helper
# ---------------------------------------------------------------------------


def sha256_fingerprint(doc: bytes) -> str:
    """Return the lowercase hex SHA-256 digest of *doc*.

    Deterministic: same bytes always produce the same fingerprint.  This is a
    content-identity handle, not a signing operation.

    Args:
        doc: Raw document bytes.

    Returns:
        64-character lowercase hex string.
    """
    return hashlib.sha256(doc).hexdigest()


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def _new_session(
    doc: bytes,
    signer: dict[str, Any],
    method: TransportMethod,
) -> dict[str, Any]:
    """Construct a fresh pending signing session.

    All required keys are guaranteed to be present.
    """
    session: dict[str, Any] = {
        "session_id": str(uuid.uuid4()),
        "status": "pending",
        "fingerprint": sha256_fingerprint(doc),
        "signer": signer,
        "method": method,
    }
    # Sanity: confirm we honour the contract.
    assert REQUIRED_SESSION_KEYS <= session.keys(), (
        f"BUG: session missing keys {REQUIRED_SESSION_KEYS - session.keys()}"
    )
    return session


def _transition_session(
    session: dict[str, Any],
    *,
    new_status: str,
) -> dict[str, Any]:
    """Return an updated copy of *session* with *new_status* applied.

    Single responsibility: produce the next-state dict without side effects.
    """
    updated = dict(session)
    updated["status"] = new_status
    return updated


# ---------------------------------------------------------------------------
# ManualTransport
# ---------------------------------------------------------------------------


class ManualTransport:
    """Credential-free ``SignTransport`` implementation.

    Stores sessions in memory.  Thread safety: in CPython the GIL covers the
    dict mutations; for multi-process use callers must manage their own store.

    Implements :class:`~nce.signing_service.transport.SignTransport` structurally
    (duck-typed / Protocol-checked).
    """

    def __init__(self) -> None:
        # Session store: session_id → session dict.
        self._sessions: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # SignTransport interface
    # ------------------------------------------------------------------

    def request_signature(
        self,
        doc: bytes,
        signer: dict[str, Any],
        method: TransportMethod,
    ) -> dict[str, Any]:
        """Initiate a local signing session for *doc*.

        The ``manual`` transport immediately marks the session as ``"pending"``.
        No external service is called; no credentials are required.

        Args:
            doc: Raw document bytes.
            signer: Identity dict (name, email, …).
            method: Transport method — must be ``"manual"`` for this impl, but
                    stored verbatim so tests can verify round-trip.

        Returns:
            Session dict with keys: ``session_id``, ``status``, ``fingerprint``,
            ``signer``, ``method``.
        """
        session = _new_session(doc, signer, method)
        self._sessions[session["session_id"]] = session
        _append_audit(
            event="requested",
            session_id=session["session_id"],
            detail={"fingerprint": session["fingerprint"], "method": method},
        )
        log.debug("ManualTransport: requested session_id=%s", session["session_id"])
        return session

    def on_signed(
        self,
        session_id: str,
        callback_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle a ``signed`` event.

        Fire-and-pull: re-GETs (re-reads) the local session to confirm state
        before recording.  For the ``manual`` transport there is no upstream
        service, so the "pull" reads from the in-memory store — the same
        pattern that vendor impls will apply via ``request_with_retry``.

        Does NOT freeze any baseline — that is the caller's responsibility.

        Args:
            session_id: Session identifier previously returned by
                :meth:`request_signature`.
            callback_payload: Webhook payload (arbitrary; stored in audit).

        Returns:
            Updated session dict with ``status == "signed"``.

        Raises:
            KeyError: If *session_id* is unknown.
        """
        session = self._pull_session(session_id)
        updated = _transition_session(session, new_status="signed")
        self._sessions[session_id] = updated
        _append_audit(
            event="signed",
            session_id=session_id,
            detail={"fingerprint": updated["fingerprint"], "payload": callback_payload},
        )
        log.debug("ManualTransport: signed session_id=%s", session_id)
        return updated

    def on_declined(
        self,
        session_id: str,
        callback_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle a ``declined`` event.

        Same fire-and-pull contract as :meth:`on_signed`.

        Args:
            session_id: Session identifier.
            callback_payload: Webhook payload.

        Returns:
            Updated session dict with ``status == "declined"``.

        Raises:
            KeyError: If *session_id* is unknown.
        """
        session = self._pull_session(session_id)
        updated = _transition_session(session, new_status="declined")
        self._sessions[session_id] = updated
        _append_audit(
            event="declined",
            session_id=session_id,
            detail={"fingerprint": updated["fingerprint"], "payload": callback_payload},
        )
        log.debug("ManualTransport: declined session_id=%s", session_id)
        return updated

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _pull_session(self, session_id: str) -> dict[str, Any]:
        """Re-GET (pull) the current session from the in-memory store.

        This is the ``manual`` analogue of the fire-and-pull re-GET that vendor
        impls will perform via ``nce.http_resilience.request_with_retry``.

        Raises:
            KeyError: If *session_id* is not in the store.
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"Unknown signing session: {session_id!r}")
        return session
