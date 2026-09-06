"""SignTransport — shared signing-service abstraction (C7, §9.6).

ONE interface behind which vendor implementations (oneflow, criipto, signicat)
and the credential-free ``manual`` implementation all sit.  Dependency rule:
this module imports nothing concrete — no vendor SDK, no DB, no HTTP.

Terminology
-----------
session
    A dict that carries at minimum ``session_id`` (str), ``status``
    (``"pending"`` | ``"signed"`` | ``"declined"``), and ``fingerprint``
    (hex SHA-256 of the original document bytes).
signer
    Opaque dict carrying identity fields (name, email, …) as required by the
    chosen transport.  The abstraction does not validate it.
method
    One of ``{"oneflow", "criipto", "signicat", "manual", "email_code"}``.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

# The canonical set of signing transports — callers should not hard-code
# the string literals; use this type instead.
TransportMethod = Literal["oneflow", "criipto", "signicat", "manual", "email_code"]

# Minimal required keys a signing session dict MUST carry.
REQUIRED_SESSION_KEYS: frozenset[str] = frozenset({"session_id", "status", "fingerprint"})

# Process-wide singleton registry for concrete transports
_TRANSPORTS: dict[str, Any] = {}


class UnimplementedTransportError(NotImplementedError, ValueError):
    """Raised when a requested signing transport is not yet implemented or configured."""


def get_signing_transport(method: str | TransportMethod) -> SignTransport:
    """Return the active SignTransport implementation for the requested method.

    Supported methods:
        - "manual": Credential-free in-memory transport (tests / dev)
        - "email_code": Native e-signature via emailed one-time code (Wave Q-2)

    Unbuilt methods:
        - "oneflow", "criipto", "signicat": External vendor integrations not yet built.
          Raises UnimplementedTransportError (never silently downgrades to manual).

    Raises:
        UnimplementedTransportError: If method is known but unbuilt / unconfigured.
        ValueError: If method is not a valid signing method.
    """
    if method == "manual":
        if "manual" not in _TRANSPORTS:
            from nce.signing_service.manual import ManualTransport

            _TRANSPORTS["manual"] = ManualTransport()
        return _TRANSPORTS["manual"]

    if method == "email_code":
        if "email_code" not in _TRANSPORTS:
            from nce.signing_service.email_code import EmailCodeTransport

            _TRANSPORTS["email_code"] = EmailCodeTransport()
        return _TRANSPORTS["email_code"]

    if method in ("oneflow", "criipto", "signicat"):
        raise UnimplementedTransportError(
            f"Signing transport '{method}' is not implemented or configured. "
            f"Select a configured transport ('email_code', 'manual')."
        )

    raise ValueError(f"Invalid signing method: {method!r}")


def reset_signing_transports() -> None:
    """Reset cached singleton transports (for test isolation)."""
    _TRANSPORTS.clear()


@runtime_checkable
class SignTransport(Protocol):
    """Protocol for e-sign transport implementations.

    Implementors supply the three methods; callers depend only on this
    protocol — never on a concrete class.  ``@runtime_checkable`` lets
    ``isinstance(obj, SignTransport)`` work for guard assertions without
    pulling in any concrete module.
    """

    def request_signature(
        self,
        doc: bytes,
        signer: dict[str, Any],
        method: TransportMethod,
    ) -> dict[str, Any]:
        """Initiate a signing session for *doc*.

        Args:
            doc: Raw document bytes to be signed.
            signer: Identity dict for the signer (name, email, …).
            method: Which transport rail to use.

        Returns:
            A session dict with at least the keys in ``REQUIRED_SESSION_KEYS``.
        """
        ...

    def on_signed(
        self,
        session_id: str,
        callback_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle a "signed" webhook event.

        Fire-and-pull: implementations should re-GET the upstream session to
        confirm state before recording it (avoids acting on spoofed webhooks).

        Args:
            session_id: The signing session identifier.
            callback_payload: Raw payload from the inbound webhook.

        Returns:
            Updated session dict (status == ``"signed"``).
        """
        ...

    def on_declined(
        self,
        session_id: str,
        callback_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle a "declined" webhook event.

        Same fire-and-pull contract as :meth:`on_signed`.

        Args:
            session_id: The signing session identifier.
            callback_payload: Raw payload from the inbound webhook.

        Returns:
            Updated session dict (status == ``"declined"``).
        """
        ...
